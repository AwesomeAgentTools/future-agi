"""
Backfill the ``usage_apicalllog.eval_score`` materialized column.

``eval_score`` is MATERIALIZED, so its value is computed once at INSERT and
stored in the part. Changing the expression (schema.py's ``CH_EVAL_SCORE_EXPR``)
therefore only affects rows written after the change — every row already on
disk keeps whatever the old expression produced. ``ALTER TABLE ... MODIFY
COLUMN`` in ``POST_DDL_ALTERS`` updates the metadata but does NOT rewrite
existing parts; only ``MATERIALIZE COLUMN`` does, and that is a mutation, so it
must not run on every app boot.

Hence this command: run it once per environment after deploying the structured-
eval-output fix, and the already-ingested rows start reporting their real score
instead of 0.

The ``idx_eval_score`` minmax skip index has to be rebuilt in the same pass.
Re-materializing the column leaves the index holding the OLD min/max, and
``MATERIALIZE INDEX`` on its own does not repair it — verified against
ClickHouse 25.3, where a row backfilled to 1.0 stayed invisible to
``WHERE eval_score >= 1.0`` until the index was dropped and re-added. A stale
skip index prunes whole granules, so eval_score filters and breakdowns come
back empty rather than wrong-looking, which is why the sequence below is
DROP INDEX -> MODIFY -> ADD INDEX -> MATERIALIZE COLUMN -> MATERIALIZE INDEX.

Usage on prod:

    # 1. See how many rows are actually wrong before touching anything:
    python manage.py backfill_eval_score --dry-run

    # 2. Run it. Chunked by partition (toYYYYMM(created_at)) so each mutation
    #    rewrites one month of parts at a time instead of the whole table:
    python manage.py backfill_eval_score --no-confirm

    # 3. Re-run the dry-run to confirm the affected count is now 0.

Notes:
  * Idempotent — a second run finds nothing to fix and exits without
    submitting a mutation.
  * ``--force`` rebuilds and re-materializes even when no row disagrees with
    the expression. The stored values and the ``idx_eval_score`` skip index go
    stale independently, and a current column with a stale index reports
    "already up to date" while filters keep pruning real rows away.
  * Refuses to start when an eval_score MATERIALIZE mutation is already
    in flight, so overlapping deploys cannot stack mutations on one table.
  * Only rewrites the eval_score column and its index; no other column is
    touched and no row is deleted.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

TABLE = "usage_apicalllog"
COLUMN = "eval_score"
INDEX = "idx_eval_score"
INDEX_DEF = f"{INDEX} {COLUMN} TYPE minmax GRANULARITY 1"

# Rows whose stored eval_score disagrees with what the current expression
# would produce. Structured outputs stored under the old expression are 0.
_AFFECTED_COUNT = """
SELECT count()
FROM {table}
WHERE _peerdb_is_deleted = 0
  AND {predicate}
  AND {column} != {expr}
"""

# system.parts spans every database on the server, so scope it to this one.
_PARTITIONS = """
SELECT DISTINCT partition
FROM system.parts
WHERE table = '{table}' AND database = currentDatabase() AND active
ORDER BY partition DESC
"""

# position() rather than LIKE '%…%': the driver runs printf-style substitution
# over every query, so a literal % here is read as a format spec and raises.
_IN_FLIGHT = """
SELECT count()
FROM system.mutations
WHERE table = '{table}' AND NOT is_done AND position(command, '{column}') > 0
"""


def rebuild_statements(table: str = TABLE) -> list[str]:
    """The DDL that carries a new eval_score expression onto an existing table.

    The index is dropped around the MODIFY and re-added after it; leaving it in
    place keeps it pruning granules on the pre-backfill min/max, and
    MATERIALIZE INDEX alone does not repair that.
    """
    from tracer.services.clickhouse.schema import CH_EVAL_SCORE_EXPR

    return [
        f"ALTER TABLE {table} DROP INDEX IF EXISTS {INDEX}",
        f"ALTER TABLE {table} MODIFY COLUMN {COLUMN} Float64 MATERIALIZED {CH_EVAL_SCORE_EXPR}",
        f"ALTER TABLE {table} ADD INDEX IF NOT EXISTS {INDEX_DEF}",
    ]


def materialize_statements(table: str = TABLE, partition: str | None = None) -> list[str]:
    """Mutations that rewrite the stored column and rebuild its index."""
    scope = f" IN PARTITION {partition}" if partition is not None else ""
    return [
        f"ALTER TABLE {table} MATERIALIZE COLUMN {COLUMN}{scope}",
        f"ALTER TABLE {table} MATERIALIZE INDEX {INDEX}{scope}",
    ]


class Command(BaseCommand):
    help = "Re-materialize usage_apicalllog.eval_score so existing rows pick up the current extraction."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report the affected row count without submitting a mutation.",
        )
        parser.add_argument(
            "--no-confirm",
            action="store_true",
            help="Skip the interactive prompt (for CI/CD and deploy automation).",
        )
        parser.add_argument(
            "--whole-table",
            action="store_true",
            help="Materialize the whole table in one mutation instead of per partition.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Rebuild even when no row is stale, to repair a stale skip index.",
        )

    def handle(self, *args, **opts):
        from tracer.services.clickhouse.client import get_clickhouse_client
        from tracer.services.clickhouse.eval_expressions import (
            eval_has_structured_score,
        )
        from tracer.services.clickhouse.schema import (
            CH_EVAL_SCORE_EXPR,
            EVAL_OUTPUT_JSON_ARGS,
        )

        ch = get_clickhouse_client()

        predicate = eval_has_structured_score(EVAL_OUTPUT_JSON_ARGS)
        affected = self._scalar(
            ch,
            _AFFECTED_COUNT.format(
                table=TABLE, column=COLUMN, expr=CH_EVAL_SCORE_EXPR, predicate=predicate
            ),
        )
        self.stdout.write(f"rows with a stale {COLUMN}: {affected}")

        if affected == 0 and not opts["force"]:
            self.stdout.write(self.style.SUCCESS(f"✓ {COLUMN} is already up to date — nothing to do."))
            return

        if opts["dry_run"]:
            self.stdout.write("--dry-run: no mutation submitted.")
            return

        in_flight = self._scalar(ch, _IN_FLIGHT.format(table=TABLE, column=COLUMN))
        if in_flight:
            raise CommandError(
                f"{in_flight} {COLUMN} mutation(s) already running on {TABLE} — "
                "wait for them to finish (see system.mutations) before re-running."
            )

        if not opts["no_confirm"]:
            scope = f"{affected} rows" if affected else "every row (forced)"
            answer = input(f"Re-materialize {COLUMN} for {scope}? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                self.stdout.write("aborted.")
                return

        for sql in rebuild_statements():
            ch.execute(sql)
            self.stdout.write(f"  {sql}")

        if opts["whole_table"]:
            targets = [None]
        else:
            targets = [row[0] for row in ch.execute(_PARTITIONS.format(table=TABLE))]
            self.stdout.write(f"materializing across {len(targets)} partition(s)")

        for target in targets:
            for sql in materialize_statements(partition=target):
                ch.execute(sql)
                self.stdout.write(f"  submitted: {sql}")

        self.stdout.write(
            self.style.SUCCESS(
                "✓ mutation(s) submitted. They run asynchronously — poll "
                f"system.mutations for table='{TABLE}', then re-run with "
                "--dry-run to confirm the affected count is 0."
            )
        )

    @staticmethod
    def _scalar(ch, sql) -> int:
        rows = ch.execute(sql)
        if not rows:
            return 0
        first = rows[0]
        return int(first[0] if isinstance(first, (list, tuple)) else first)
