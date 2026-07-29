"""
Convert a CH vector table (``syn`` / ``ground_truths`` / ``feedbacks``) from a
plain, non-replicated engine to ``ReplicatedReplacingMergeTree`` IN PLACE,
inside the single ``default`` database, without losing data.

Why this exists (and why it is separate from ``backfill_vector_tables``):
``backfill_vector_tables`` rebuilds a table's CONTENTS from source (S3 / PG).
This command does not touch content sources; it fixes the ENGINE of a table
whose rows already exist but are split across replicas because a plain
``MergeTree`` never replicated them. On a multi-replica cluster a plain engine
means each replica holds its own slice (e.g. 2 / 3 / 0 rows), and a query is
non-deterministic depending on which replica answers. This command gathers the
UNION of rows across every replica and lands them in a properly replicated
table, so all replicas converge on the full set.

It cannot be done with ``migrate_ch_vector_tables`` here: that command requires
``source_db != target_db`` (it copies db-to-db), and we must stay in the single
``default`` database with no new database.

How it is safe:
  - ``--dry-run`` is the DEFAULT. Nothing changes without ``--execute``.
  - Reads the UNION via ``clusterAllReplicas`` first, so no replica's slice is
    lost. Deduped by ``id`` with ``LIMIT 1 BY id``.
  - Builds the replicated copy in a temp table and verifies per-replica parity
    BEFORE any swap. If it does not converge, it aborts and leaves the temp
    table for inspection; the live table is untouched.
  - The cutover is an atomic ``EXCHANGE TABLES ... ON CLUSTER``. The old plain
    data is preserved as ``<table>__plain_backup`` (never auto-dropped), so the
    swap is reversible.
  - No-op if the table is absent or already Replicated, or on a single node
    (plain is correct there).

Usage:
    python manage.py convert_vector_table_to_replicated --table feedbacks --dry-run
    python manage.py convert_vector_table_to_replicated --table feedbacks --execute
    # after verifying, optionally:
    # DROP TABLE default.feedbacks__plain_backup ON CLUSTER '<cluster>' SYNC;
"""

from __future__ import annotations

import os

import structlog
from django.core.management.base import BaseCommand, CommandError

from agentic_eval.core.database.ch_vector import (
    ClickHouseVectorDB,
    get_clickhouse_cluster_name,
)
from agentic_eval.core.embeddings.embedding_manager import (
    FEEDBACK_TABLE_NAME,
    GROUND_TRUTH_TABLE_NAME,
)
from model_hub.services.ch_migration import (
    expected_replica_count,
    per_replica_counts,
    poll_replica_parity,
    require_identifier,
)
from model_hub.utils.kb_indexer import KB_TABLE_NAME

logger = structlog.get_logger(__name__)

KNOWN_TABLES = (FEEDBACK_TABLE_NAME, GROUND_TRUTH_TABLE_NAME, KB_TABLE_NAME)


def _distinct_engines(client, database: str, table: str, cluster: str) -> set[str]:
    rows = client.execute(
        f"SELECT DISTINCT engine FROM clusterAllReplicas('{cluster}', system.tables) "
        "WHERE database = %(d)s AND name = %(t)s",
        {"d": database, "t": table},
    )
    return {r[0] for r in rows}


def _shared_columns_same_db(client, database: str, a: str, b: str) -> list[str]:
    """Ordered columns present in BOTH tables in the same db (never SELECT *)."""

    def cols(table: str) -> list[str]:
        rows = client.execute(
            "SELECT name FROM system.columns "
            "WHERE database = %(d)s AND table = %(t)s ORDER BY position",
            {"d": database, "t": table},
        )
        return [r[0] for r in rows]

    a_cols = cols(a)
    b_set = set(cols(b))
    return [c for c in a_cols if c in b_set]


class Command(BaseCommand):
    help = "Convert a plain CH vector table to ReplicatedReplacingMergeTree in place."

    def add_arguments(self, parser):
        parser.add_argument(
            "--table", required=True,
            help=f"One of: {', '.join(KNOWN_TABLES)}",
        )
        parser.add_argument("--database", default=os.getenv("CH_DATABASE") or "default")
        parser.add_argument("--cluster", default=get_clickhouse_cluster_name())
        parser.add_argument(
            "--execute", action="store_true",
            help="Actually convert. Omit for a dry run (the default).",
        )

    def handle(self, *args, **opts):
        table = opts["table"].strip()
        if table not in KNOWN_TABLES:
            raise CommandError(f"--table must be one of {KNOWN_TABLES}, got {table!r}")
        database = require_identifier(opts["database"], "--database")
        cluster = require_identifier(opts["cluster"], "--cluster")
        execute = opts["execute"]

        db = ClickHouseVectorDB()
        client = db.client

        if not db._is_clustered():
            self.stdout.write(
                f"{database}.{table}: single-node CH, plain engine is correct. "
                "Nothing to convert."
            )
            return

        engines = _distinct_engines(client, database, table, cluster)
        if not engines:
            self.stdout.write(f"{database}.{table}: absent on the cluster. Nothing to do.")
            return
        if all("Replicated" in e for e in engines):
            self.stdout.write(
                f"{database}.{table}: already Replicated ({sorted(engines)}). Nothing to do."
            )
            return
        if any("Replicated" in e for e in engines):
            raise CommandError(
                f"{database}.{table}: engines differ across replicas ({sorted(engines)}). "
                "Mixed Replicated/plain is unsafe to auto-convert; inspect manually."
            )

        # Plain engine on a cluster: gather the union and show the divergence.
        union_count = client.execute(
            f"SELECT uniqExact(id) FROM clusterAllReplicas('{cluster}', {database}.{table})"
        )[0][0]
        before = per_replica_counts(client, database, table, cluster)
        expected_replicas = expected_replica_count(client, cluster)
        tmp = f"{table}__repl_tmp"
        backup = f"{table}__plain_backup"

        self.stdout.write(f"{database}.{table}: plain engine(s) {sorted(engines)}")
        self.stdout.write(f"  per-replica rows (diverged): {before}")
        self.stdout.write(f"  union distinct ids:          {union_count}")
        self.stdout.write(f"  expected replicas:           {expected_replicas}")

        if not execute:
            self.stdout.write(
                "\nDRY-RUN. Would, in order:\n"
                f"  1. CREATE {database}.{tmp} as ReplicatedReplacingMergeTree ON CLUSTER\n"
                f"  2. INSERT the deduped union ({union_count} rows) from all replicas\n"
                f"  3. verify parity ({union_count} on each of {expected_replicas} replicas)\n"
                f"  4. EXCHANGE TABLES {database}.{table} <-> {database}.{tmp} ON CLUSTER\n"
                f"  5. RENAME the old plain table to {database}.{backup} (kept for rollback)\n"
                "Re-run with --execute to perform it."
            )
            return

        # 1. Replicated temp table (own Keeper path via create_table).
        db.create_table(tmp, cluster=cluster, database=database)

        # 2. Insert the deduped union of every replica's slice. LIMIT 1 BY id
        #    collapses any id that appears on more than one replica.
        cols = _shared_columns_same_db(client, database, table, tmp)
        if not cols:
            raise CommandError(
                f"no shared columns between {database}.{table} and {database}.{tmp}; aborting."
            )
        col_list = ", ".join(f"`{c}`" for c in cols)
        client.execute(
            f"INSERT INTO {database}.{tmp} ({col_list}) "
            f"SELECT {col_list} FROM clusterAllReplicas('{cluster}', {database}.{table}) "
            f"LIMIT 1 BY id"
        )

        # 3. Verify parity BEFORE swapping. Do not swap a lagging copy.
        counts, converged = poll_replica_parity(
            client, database=database, table=tmp, cluster=cluster,
            expected=union_count, expected_replicas=expected_replicas,
        )
        if not converged:
            raise CommandError(
                f"{database}.{tmp} did not converge (per-replica {counts}, expected "
                f"{union_count} on {expected_replicas} replicas). Live table untouched; "
                f"temp left for inspection. Re-run once replicas catch up, or drop "
                f"{database}.{tmp} and retry."
            )

        # 4. Atomic cutover, then 5. keep the old plain data as a backup.
        client.execute(
            f"EXCHANGE TABLES {database}.{table} AND {database}.{tmp} ON CLUSTER '{cluster}'"
        )
        client.execute(
            f"RENAME TABLE {database}.{tmp} TO {database}.{backup} ON CLUSTER '{cluster}'"
        )

        after = per_replica_counts(client, database, table, cluster)
        logger.info(
            "convert_vector_table_done",
            table=f"{database}.{table}", union_count=union_count,
            per_replica_after=after, backup=f"{database}.{backup}",
        )
        self.stdout.write(self.style.SUCCESS(
            f"\nDone. {database}.{table} is now ReplicatedReplacingMergeTree with "
            f"{union_count} rows; per-replica {after}. Old plain data preserved at "
            f"{database}.{backup} (drop it once verified)."
        ))
