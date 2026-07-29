"""
Build the ClickHouse vector tables (``syn`` = KB embeddings, ``ground_truths``,
``feedbacks``) from their sources of truth (S3 + Postgres).

Each table has a different source and a different reuse path:

  syn           <- S3 docs + PG KnowledgeBaseFile/Files   (re-ingest)
  ground_truths <- PG EvalGroundTruth.data                 (re-embed)
  feedbacks     <- PG Feedback + the evaluated input row   (see caveat)

This command reuses the existing embedding writers, it does not re-implement
embedding:
  - syn:            model_hub.utils.kb_helpers.ingest_kb_files_impl
  - ground_truths:  model_hub.services.ground_truth_service.GroundTruthService.embed_dataset

Safety model:
  - ``--dry-run`` is the DEFAULT. Nothing writes unless ``--execute`` is passed.
  - ``--only-missing`` (default on) skips any eval_id already present in the
    target table, because the KB ingest path is append-only: re-ingesting a KB
    that still has rows would double its chunks. If the present-set cannot be
    read, the run aborts (fail closed) rather than risk a double insert.
  - Engine preflight: on a multi-replica cluster, refuses to write into a table
    that already exists as a non-Replicated engine (that would land on one
    replica only). Everything stays in the single ``default`` database.
  - ``--org`` / ``--workspace`` scope the sweep so a run can be limited first.
  - Per-unit isolation: one failing KB/GT is logged and skipped, the sweep
    continues, and the final summary reports failures with a non-zero note.
  - Resumable: a per-table checkpoint file is appended after each unit.

Usage:
    python manage.py backfill_vector_tables --tables syn --dry-run
    python manage.py backfill_vector_tables --tables syn --org <ORG_UUID> --execute
    python manage.py backfill_vector_tables --tables syn,ground_truths --execute
    python manage.py backfill_vector_tables --check-engines
"""

from __future__ import annotations

import os
import uuid

import structlog
from django.core.management.base import BaseCommand, CommandError

logger = structlog.get_logger(__name__)

ALL_TABLES = ("syn", "ground_truths", "feedbacks")
CHECKPOINT_DIR = os.environ.get("BACKFILL_CHECKPOINT_DIR", "/tmp")

# The command targets a single CH database, ``default`` by default, and NEVER
# creates a database. The embedding writers and the CH client both read
# CH_DATABASE (falling back to ``default``), so a run lands in that one db.
CH_DATABASE = os.environ.get("CH_DATABASE") or "default"


# --------------------------------------------------------------------------
# checkpoint (resumability, matches the existing backfill_* command style)
# --------------------------------------------------------------------------
def _checkpoint_path(table: str) -> str:
    return os.path.join(CHECKPOINT_DIR, f"backfill_{table}.done")


def _load_done(table: str) -> set[str]:
    path = _checkpoint_path(table)
    if not os.path.exists(path):
        return set()
    with open(path) as fh:
        return {line.strip() for line in fh if line.strip()}


def _mark_done(table: str, unit_id: str) -> None:
    with open(_checkpoint_path(table), "a") as fh:
        fh.write(f"{unit_id}\n")


# --------------------------------------------------------------------------
# CH helpers (real client API: ClickHouseVectorDB().client.execute -> tuples)
# --------------------------------------------------------------------------
def _present_eval_ids(table_name: str) -> set[str]:
    """eval_ids that already have live rows in the CH table. Raises on any read
    failure; callers decide whether that is fatal (see ``_resolve_present``)."""
    from agentic_eval.core.database.ch_vector import ClickHouseVectorDB

    db = ClickHouseVectorDB()
    try:
        rows = db.client.execute(
            f"SELECT DISTINCT eval_id FROM {table_name} WHERE deleted = 0"
        )
    finally:
        db.close()
    return {str(r[0]) for r in rows}


def _resolve_present(table_name, *, only_missing, execute, stdout) -> set[str]:
    """Present-set with mode-aware failure. On --execute the present-set gates
    an append-only write, so an unreadable CH aborts (fail closed). In dry-run
    nothing writes, so an unreadable CH is a warning and the run continues."""
    if not only_missing:
        return set()
    try:
        return _present_eval_ids(table_name)
    except Exception as exc:
        if execute:
            raise CommandError(
                f"[{table_name}] could not read the present-set from CH "
                f"({exc}). Refusing --execute: re-ingesting could double an "
                f"append-only table. Fix CH connectivity, or pass "
                f"--no-only-missing only if the table is confirmed empty."
            )
        stdout(
            f"[{table_name}] WARN: CH present-set unreadable ({exc}); dry-run "
            f"continues assuming nothing is present."
        )
        return set()


def _table_engines(table_name: str) -> tuple[bool, set[str]]:
    """Return (is_clustered, distinct engines for the table in ``default``).

    On a single node the engines set may be empty (table absent) or hold the
    plain engine; on a cluster it is read across every replica.
    """
    from agentic_eval.core.database.ch_vector import (
        ClickHouseVectorDB,
        get_clickhouse_cluster_name,
    )

    db = ClickHouseVectorDB()
    try:
        clustered = ClickHouseVectorDB.is_clustered(db.client)
        if clustered:
            cluster = get_clickhouse_cluster_name()
            rows = db.client.execute(
                f"SELECT DISTINCT engine FROM clusterAllReplicas("
                f"'{cluster}', system.tables) "
                f"WHERE database = '{CH_DATABASE}' AND name = '{table_name}'"
            )
        else:
            rows = db.client.execute(
                "SELECT DISTINCT engine FROM system.tables "
                f"WHERE database = '{CH_DATABASE}' AND name = '{table_name}'"
            )
    finally:
        db.close()
    return clustered, {r[0] for r in rows}


def _assert_replicated_or_absent(table_name: str) -> None:
    """Fail closed if a target exists as a non-Replicated engine on a cluster.

    ``create_table`` is CREATE TABLE IF NOT EXISTS: it makes a
    ReplicatedReplacingMergeTree ON CLUSTER for a MISSING table on a clustered
    node, but is a no-op for a table that already exists as plain
    ReplacingMergeTree. Inserting into such a plain table on a multi-replica
    cluster writes to one replica only. So refuse and point at the in-place
    fix (no new database, single ``default`` db).
    """
    clustered, engines = _table_engines(table_name)
    if not clustered:
        return  # single node: plain ReplacingMergeTree is correct.
    if not engines:
        return  # absent: create_table() will make it Replicated ON CLUSTER.
    plain = {e for e in engines if "Replicated" not in e}
    if plain:
        from agentic_eval.core.database.ch_vector import get_clickhouse_cluster_name

        raise CommandError(
            f"{CH_DATABASE}.{table_name}: clustered node but engine(s) {plain} "
            f"are NOT Replicated. Backfilling now would write to one replica "
            f"only. Fix in place first: DROP TABLE {CH_DATABASE}.{table_name} "
            f"ON CLUSTER '{get_clickhouse_cluster_name()}' SYNC; then re-run so "
            f"the table is recreated Replicated and refilled from source."
        )


def _validate_uuid(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError):
        raise CommandError(f"--{label} must be a UUID, got {value!r}")


# ==========================================================================
# syn - KB embeddings, source = S3 + PG (re-ingest, reuses ingest_kb_files_impl)
# ==========================================================================
def _backfill_syn(*, org, workspace, only_missing, execute, stdout) -> int:
    from model_hub.models.develop_dataset import KnowledgeBaseFile
    from model_hub.utils.kb_helpers import ingest_kb_files_impl

    if execute:
        _assert_replicated_or_absent("syn")
    done = _load_done("syn")
    present = _resolve_present("syn", only_missing=only_missing, execute=execute, stdout=stdout)

    qs = KnowledgeBaseFile.objects.filter(deleted=False).order_by("created_at")
    if org:
        qs = qs.filter(organization_id=org)
    if workspace:
        qs = qs.filter(workspace_id=workspace)

    total = qs.count()
    stdout(f"[syn] {total} KnowledgeBaseFile candidates")
    rebuilt = skipped = failed = 0

    for kb in qs.iterator():
        kb_id = str(kb.id)
        if kb_id in done or (only_missing and kb_id in present):
            skipped += 1
            continue

        # file_metadata = {file_id: {name, extension}}; S3 key is
        # knowledge-base/{kb_id}/{file_id}.{extension}.
        file_metadata = {}
        for f in kb.files.all():
            ext = f.name.rsplit(".", 1)[-1] if "." in f.name else ""
            file_metadata[str(f.id)] = {"name": f.name, "extension": ext}

        if not file_metadata:
            stdout(f"[syn] {kb_id} has no Files rows, skipping")
            skipped += 1
            continue

        if not execute:
            stdout(f"[syn] DRY-RUN would re-ingest {kb_id} ({len(file_metadata)} files)")
            continue

        try:
            ingest_kb_files_impl(file_metadata, kb_id, str(kb.organization_id))
        except Exception:
            failed += 1
            logger.exception("syn_backfill_kb_failed", kb_id=kb_id)
            stdout(f"[syn] FAILED {kb_id} (see logs)")
            continue
        _mark_done("syn", kb_id)
        rebuilt += 1
        logger.info("syn_backfill_kb_done", kb_id=kb_id, files=len(file_metadata))

    stdout(f"[syn] rebuilt={rebuilt} skipped={skipped} failed={failed}")
    return failed


# ==========================================================================
# ground_truths - source = PG EvalGroundTruth (reuses embed_dataset)
# ==========================================================================
def _backfill_ground_truths(*, org, workspace, only_missing, execute, stdout) -> int:
    from model_hub.models.evals_metric import EvalGroundTruth
    from model_hub.services.ground_truth_service import GroundTruthService

    if execute:
        _assert_replicated_or_absent("ground_truths")
    done = _load_done("ground_truths")
    present = _resolve_present("ground_truths", only_missing=only_missing, execute=execute, stdout=stdout)

    qs = EvalGroundTruth.objects.filter(deleted=False).order_by("created_at")
    if org:
        qs = qs.filter(organization_id=org)
    if workspace:
        qs = qs.filter(workspace_id=workspace)

    total = qs.count()
    stdout(f"[ground_truths] {total} EvalGroundTruth candidates")
    rebuilt = skipped = failed = 0

    for gt in qs.iterator():
        eval_id = str(gt.eval_template_id)
        unit_id = str(gt.id)
        if unit_id in done or (only_missing and eval_id in present):
            skipped += 1
            continue
        if not (gt.data or []):
            skipped += 1
            continue

        if not execute:
            stdout(f"[ground_truths] DRY-RUN would re-embed gt={unit_id} eval={eval_id}")
            continue

        try:
            GroundTruthService.embed_dataset(gt=gt)
        except Exception:
            failed += 1
            logger.exception("gt_backfill_failed", gt_id=unit_id)
            stdout(f"[ground_truths] FAILED {unit_id} (see logs)")
            continue
        _mark_done("ground_truths", unit_id)
        rebuilt += 1
        logger.info("gt_backfill_done", gt_id=unit_id)

    stdout(f"[ground_truths] rebuilt={rebuilt} skipped={skipped} failed={failed}")
    return failed


# ==========================================================================
# feedbacks - report-only. The feedback vector needs the evaluated input row
# (keyed by the eval template's required input keys) plus feedback value and
# comment; that input is not on the Feedback row, and the embed is written
# inline in a view rather than a reusable helper. Blocked on: (1) extract that
# embed into a helper, (2) confirm the input rows are available. Until then
# this reports the count and never fabricates a vector.
# ==========================================================================
def _backfill_feedbacks(*, org, workspace, only_missing, execute, stdout) -> int:
    from model_hub.models.evals_metric import Feedback

    qs = Feedback.objects.filter(deleted=False)
    if org:
        qs = qs.filter(organization_id=org)
    if workspace:
        qs = qs.filter(workspace_id=workspace)

    stdout(
        f"[feedbacks] {qs.count()} Feedback rows in PG. Report-only: rebuild "
        "needs the evaluated input row and a reusable embed helper (see "
        "module docstring). No vectors written."
    )
    return 0


_DISPATCH = {
    "syn": _backfill_syn,
    "ground_truths": _backfill_ground_truths,
    "feedbacks": _backfill_feedbacks,
}


class Command(BaseCommand):
    help = "Build CH vector tables (syn / ground_truths / feedbacks) from S3 + PG."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tables", default="syn",
            help="Comma-separated subset of: syn,ground_truths,feedbacks",
        )
        parser.add_argument("--org", default=None, help="Scope to one organization UUID")
        parser.add_argument("--workspace", default=None, help="Scope to one workspace UUID")
        parser.add_argument(
            "--execute", action="store_true",
            help="Actually write. Omit for a dry run (the default).",
        )
        parser.add_argument(
            "--no-only-missing", action="store_true",
            help="Rebuild ALL, not just eval_ids absent from CH. Unsafe unless "
                 "the table is confirmed empty (ingest is append-only).",
        )
        parser.add_argument(
            "--check-engines", action="store_true",
            help="Read-only: print each table's per-replica engine and exit.",
        )

    def handle(self, *args, **opts):
        tables = [t.strip() for t in opts["tables"].split(",") if t.strip()]
        for t in tables:
            if t not in ALL_TABLES:
                raise CommandError(f"unknown table {t!r}; pick from {ALL_TABLES}")

        if opts["check_engines"]:
            for t in ALL_TABLES:
                clustered, engines = _table_engines(t)
                self.stdout.write(
                    f"{CH_DATABASE}.{t}: clustered={clustered} "
                    f"engines={sorted(engines) or ['<absent>']}"
                )
            return

        org = _validate_uuid(opts["org"], "org")
        workspace = _validate_uuid(opts["workspace"], "workspace")
        execute = opts["execute"]
        only_missing = not opts["no_only_missing"]
        mode = "EXECUTE" if execute else "DRY-RUN"
        self.stdout.write(f"mode={mode} tables={tables} only_missing={only_missing}")

        total_failed = 0
        for t in tables:
            total_failed += _DISPATCH[t](
                org=org,
                workspace=workspace,
                only_missing=only_missing,
                execute=execute,
                stdout=self.stdout.write,
            )

        if total_failed:
            raise CommandError(f"completed with {total_failed} failed unit(s); see logs")
