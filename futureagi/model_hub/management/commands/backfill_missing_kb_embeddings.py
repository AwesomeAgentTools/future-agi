"""Restore only KBs that are absent from the replicated ``syn`` table.

Existing KB vectors are never read, changed, or rewritten. Missing KBs are
indexed through the normal KBIndexer path using their retained source objects.
"""

from __future__ import annotations

import structlog
from django.core.management.base import BaseCommand, CommandError

logger = structlog.get_logger(__name__)


def _present_kb_ids() -> set[str]:
    from agentic_eval.core.database.ch_vector import ClickHouseVectorDB

    db = ClickHouseVectorDB()
    try:
        rows = db.client.execute("SELECT DISTINCT eval_id FROM syn WHERE deleted = 0")
    finally:
        db.close()
    return {str(row[0]) for row in rows}


def _assert_syn_is_replicated() -> None:
    from agentic_eval.core.database.ch_vector import (
        ClickHouseVectorDB,
        get_clickhouse_cluster_name,
    )

    db = ClickHouseVectorDB()
    try:
        if not db._is_clustered():
            return
        rows = db.client.execute(
            f"SELECT DISTINCT engine FROM clusterAllReplicas("
            f"'{get_clickhouse_cluster_name()}', system.tables) "
            "WHERE database = currentDatabase() AND name = 'syn'"
        )
    finally:
        db.close()
    engines = {row[0] for row in rows}
    if engines and any("Replicated" not in engine for engine in engines):
        raise CommandError(
            "syn must be converted to a replicated engine before restoring missing KBs"
        )


def _retained_source_files(kb):
    from tfc.utils.storage import UPLOAD_BUCKET_NAME
    from tfc.utils.storage_client import get_object_url, get_storage_client

    client = get_storage_client()
    files = []
    for file_obj in kb.files.filter(deleted=False):
        extension = file_obj.name.rsplit(".", 1)[-1].lower() if "." in file_obj.name else ""
        object_name = f"{file_obj.id}.{extension}" if extension else str(file_obj.id)
        object_key = f"knowledge-base/{kb.id}/{object_name}"
        try:
            client.stat_object(UPLOAD_BUCKET_NAME, object_key)
        except Exception:
            return None
        files.append((str(file_obj.id), get_object_url(UPLOAD_BUCKET_NAME, object_key)))
    return files or None


class Command(BaseCommand):
    help = "Restore KB embeddings only when the KB has no live vectors in replicated syn."

    def add_arguments(self, parser):
        parser.add_argument(
            "--execute", action="store_true", help="Write embeddings. Omit for dry-run."
        )
        parser.add_argument(
            "--write-freeze-confirmed",
            action="store_true",
            help="Required with --execute after KB ingestion writers are paused.",
        )

    def handle(self, *args, **opts):
        if opts["execute"] and not opts["write_freeze_confirmed"]:
            raise CommandError("--execute requires --write-freeze-confirmed")

        from model_hub.models.develop_dataset import KnowledgeBaseFile

        _assert_syn_is_replicated()
        present = _present_kb_ids()
        if opts["execute"]:
            from model_hub.utils.kb_indexer import KBIndexer

        restored = skipped = failed = 0
        queryset = KnowledgeBaseFile.objects.filter(deleted=False).order_by("created_at")
        for kb in queryset.iterator():
            kb_id = str(kb.id)
            if kb_id in present:
                skipped += 1
                continue

            source_files = _retained_source_files(kb)
            if not source_files:
                self.stdout.write(
                    f"[syn] {kb_id}: skipped; one or more source objects are unavailable"
                )
                skipped += 1
                continue
            if not opts["execute"]:
                self.stdout.write(f"[syn] {kb_id}: would restore {len(source_files)} files")
                continue

            try:
                for file_id, source_url in source_files:
                    indexer = KBIndexer()
                    try:
                        result = indexer.process_s3_file(
                            source_url, file_id, kb_id, str(kb.organization_id)
                        )
                    finally:
                        indexer.embedding_manager.close()
                    if not result or result.get("error"):
                        raise RuntimeError(
                            result.get("error") if result else "indexer returned no result"
                        )
            except Exception as exc:
                failed += 1
                logger.exception("missing_kb_embedding_restore_failed", kb_id=kb_id)
                self.stdout.write(f"[syn] {kb_id}: failed ({exc})")
                continue

            restored += 1
            self.stdout.write(f"[syn] {kb_id}: restored")

        self.stdout.write(f"[syn] restored={restored} skipped={skipped} failed={failed}")
        if failed:
            raise CommandError(f"completed with {failed} failed KB restore(s)")
