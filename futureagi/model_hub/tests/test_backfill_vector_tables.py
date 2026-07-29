"""Unit tests for the ``backfill_vector_tables`` management command.

These pin the safety rails without touching real ClickHouse or S3: the CH
helpers and the embedding writers are patched, so the tests assert the
command's control flow (dry-run writes nothing, execute fails closed on an
unreadable present-set, the engine preflight refuses a plain table on a
cluster, unknown tables error).
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

CMD = "backfill_vector_tables"
MOD = "model_hub.management.commands.backfill_vector_tables"


def _run(*args):
    out = StringIO()
    call_command(CMD, *args, stdout=out, stderr=out)
    return out.getvalue()


def test_unknown_table_errors():
    with pytest.raises(CommandError, match="unknown table"):
        _run("--tables", "not_a_table")


def test_invalid_org_uuid_errors():
    with pytest.raises(CommandError, match="must be a UUID"):
        _run("--tables", "syn", "--org", "not-a-uuid")


@pytest.mark.django_db
def test_dry_run_writes_nothing_even_with_present_set_readable():
    # Present-set reads fine and returns nothing; dry-run must not call the
    # ingest writer regardless.
    with (
        patch(f"{MOD}._present_eval_ids", return_value=set()),
        patch("model_hub.utils.kb_helpers.ingest_kb_files_impl") as ingest,
        patch(
            "model_hub.models.develop_dataset.KnowledgeBaseFile.objects"
        ) as kbf,
    ):
        kbf.filter.return_value.order_by.return_value.count.return_value = 0
        kbf.filter.return_value.order_by.return_value.iterator.return_value = iter([])
        out = _run("--tables", "syn")  # dry-run is the default
    assert "mode=DRY-RUN" in out
    ingest.assert_not_called()


@pytest.mark.django_db
def test_dry_run_continues_when_present_set_unreadable():
    # CH unreachable in dry-run: warn and continue (no write, so safe).
    with (
        patch(f"{MOD}._present_eval_ids", side_effect=RuntimeError("no CH")),
        patch("model_hub.utils.kb_helpers.ingest_kb_files_impl") as ingest,
        patch(
            "model_hub.models.develop_dataset.KnowledgeBaseFile.objects"
        ) as kbf,
    ):
        kbf.filter.return_value.order_by.return_value.count.return_value = 0
        kbf.filter.return_value.order_by.return_value.iterator.return_value = iter([])
        out = _run("--tables", "syn")
    assert "WARN: CH present-set unreadable" in out
    ingest.assert_not_called()


def test_execute_fails_closed_when_present_set_unreadable():
    # CH unreachable in --execute with --only-missing: must abort, not write.
    with (
        patch(f"{MOD}._assert_replicated_or_absent"),
        patch(f"{MOD}._present_eval_ids", side_effect=RuntimeError("no CH")),
        patch("model_hub.utils.kb_helpers.ingest_kb_files_impl") as ingest,
    ):
        with pytest.raises(CommandError, match="could not read the present-set"):
            _run("--tables", "syn", "--execute")
    ingest.assert_not_called()


def test_engine_preflight_refuses_plain_on_cluster():
    # A plain engine on a clustered node must abort before any write.
    with (
        patch(f"{MOD}._table_engines", return_value=(True, {"ReplacingMergeTree"})),
        patch(f"{MOD}._present_eval_ids", return_value=set()),
        patch("model_hub.utils.kb_helpers.ingest_kb_files_impl") as ingest,
    ):
        with pytest.raises(CommandError, match="NOT Replicated"):
            _run("--tables", "syn", "--execute")
    ingest.assert_not_called()


def test_engine_preflight_allows_replicated_on_cluster():
    with patch(
        f"{MOD}._table_engines",
        return_value=(True, {"ReplicatedReplacingMergeTree"}),
    ):
        from model_hub.management.commands.backfill_vector_tables import (
            _assert_replicated_or_absent,
        )

        _assert_replicated_or_absent("syn")  # must not raise


def test_engine_preflight_allows_absent_on_cluster():
    with patch(f"{MOD}._table_engines", return_value=(True, set())):
        from model_hub.management.commands.backfill_vector_tables import (
            _assert_replicated_or_absent,
        )

        _assert_replicated_or_absent("syn")  # absent -> created replicated later


def test_engine_preflight_noop_on_single_node():
    with patch(f"{MOD}._table_engines", return_value=(False, {"ReplacingMergeTree"})):
        from model_hub.management.commands.backfill_vector_tables import (
            _assert_replicated_or_absent,
        )

        _assert_replicated_or_absent("syn")  # single node: plain is fine
