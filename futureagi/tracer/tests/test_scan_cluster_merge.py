"""Regression tests for the duplicate-cluster merge pass.

``assign_to_cluster`` is online and incremental with no merge step, so two clusters
that describe the same root cause can never join. Measured on 261 real production
issue briefs replayed through the real assignment loop: 7 groups were split across
14 clusters — 14% of the feed — with pairs as plainly identical as "Repeated same
call log chain 12 times in redundant retries" and "Retried identical chain execution
11 times producing empty outputs" shown to users as separate entries.

These pin the behaviour that fixes it. ClickHouse is mocked so the tests assert on
the reconciliation (which is where the data-loss risk lives) rather than on vector
search: what must hold is that issues and junction rows survive the move, counts add
up, the unique-per-(cluster, trace) junction constraint is never violated, and the
surviving id is the one users already have in their feed.
"""

import uuid
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from tracer.models.trace_error_analysis import (
    ErrorClusterTraces,
    FeedIssueStatus,
    TraceErrorGroup,
)
from tracer.queries.scan_clustering import merge_duplicate_clusters


def _cluster(project_id, cluster_id, *, members, traces, unique=None):
    return TraceErrorGroup.objects.create(
        project_id=project_id,
        cluster_id=cluster_id,
        title=f"cluster {cluster_id}",
        issue_group="Tool Failures",
        issue_category="Tool-related",
        fix_layer="Tools",
        error_count=members,
        total_events=members,
        unique_traces=unique if unique is not None else len(traces),
        first_seen=timezone.now(),
        last_seen=timezone.now(),
    )


def _ch(rows):
    """Mock ClickHouseVectorDB returning ``rows`` from the centroid SELECT."""
    db = MagicMock()
    db.return_value.client.execute.return_value = rows
    return db


NEAR_A = [1.0, 0.0, 0.0]
NEAR_B = [0.98, 0.02, 0.0]      # cosine distance well under threshold
FAR = [0.0, 1.0, 0.0]           # orthogonal — must never merge


@pytest.mark.django_db
class TestMergeDuplicateClusters:
    def test_merges_near_identical_centroids_and_keeps_the_larger_id(self, project):
        pid = project.id
        big = _cluster(pid, "C1", members=9, traces=[])
        small = _cluster(pid, "C2", members=2, traces=[])
        rows = [("C1", NEAR_A, 9), ("C2", NEAR_B, 2)]
        with patch("tracer.queries.scan_clustering.ClickHouseVectorDB", _ch(rows)), \
             patch("tracer.queries.scan_clustering.ensure_centroid_table"), \
             patch("tracer.queries.scan_clustering._absorb_centroid"), \
             patch("tracer.queries.scan_clustering._absorb_centroid"), \
             patch("tracer.queries.scan_clustering.delete_centroid") as dc:
            merged = merge_duplicate_clusters(pid)

        assert merged == 1
        assert TraceErrorGroup.objects.filter(project_id=pid).count() == 1
        # the id users already see in the feed is the one that survives
        survivor = TraceErrorGroup.objects.get(project_id=pid)
        assert survivor.cluster_id == "C1"
        assert survivor.error_count == 11        # 9 + 2, nothing dropped
        assert survivor.total_events == 11
        dc.assert_called_once_with("C2", pid)    # orphan centroid cleaned up
        assert not TraceErrorGroup.objects.filter(cluster_id=small.cluster_id).exists()
        assert big.cluster_id == survivor.cluster_id

    def test_leaves_unrelated_clusters_alone(self, project):
        pid = project.id
        _cluster(pid, "C1", members=5, traces=[])
        _cluster(pid, "C2", members=5, traces=[])
        rows = [("C1", NEAR_A, 5), ("C2", FAR, 5)]
        with patch("tracer.queries.scan_clustering.ClickHouseVectorDB", _ch(rows)), \
             patch("tracer.queries.scan_clustering.ensure_centroid_table"), \
             patch("tracer.queries.scan_clustering._absorb_centroid"), \
             patch("tracer.queries.scan_clustering.delete_centroid"):
            merged = merge_duplicate_clusters(pid)

        assert merged == 0
        assert TraceErrorGroup.objects.filter(project_id=pid).count() == 2

    def test_ignores_centroids_whose_cluster_row_is_gone(self, project):
        """Orphan centroids outlive their TraceErrorGroup; merging into one would
        raise DoesNotExist straight into the caller."""
        pid = project.id
        _cluster(pid, "C1", members=5, traces=[])
        rows = [("C1", NEAR_A, 5), ("GHOST", NEAR_B, 4)]
        with patch("tracer.queries.scan_clustering.ClickHouseVectorDB", _ch(rows)), \
             patch("tracer.queries.scan_clustering.ensure_centroid_table"), \
             patch("tracer.queries.scan_clustering._absorb_centroid"), \
             patch("tracer.queries.scan_clustering.delete_centroid"):
            merged = merge_duplicate_clusters(pid)

        assert merged == 0
        assert TraceErrorGroup.objects.filter(project_id=pid).count() == 1

    def test_respects_max_merges_bound(self, project):
        pid = project.id
        for i in range(6):
            _cluster(pid, f"C{i}", members=6 - i, traces=[])
        rows = [(f"C{i}", [1.0 - i * 0.005, i * 0.005, 0.0], 6 - i) for i in range(6)]
        with patch("tracer.queries.scan_clustering.ClickHouseVectorDB", _ch(rows)), \
             patch("tracer.queries.scan_clustering.ensure_centroid_table"), \
             patch("tracer.queries.scan_clustering._absorb_centroid"), \
             patch("tracer.queries.scan_clustering.delete_centroid"):
            merged = merge_duplicate_clusters(pid, max_merges=2)

        assert merged == 2
        assert TraceErrorGroup.objects.filter(project_id=pid).count() == 4

    def test_no_op_below_two_centroids(self, project):
        pid = project.id
        _cluster(pid, "C1", members=3, traces=[])
        with patch("tracer.queries.scan_clustering.ClickHouseVectorDB", _ch([("C1", NEAR_A, 3)])), \
             patch("tracer.queries.scan_clustering.ensure_centroid_table"), \
             patch("tracer.queries.scan_clustering._absorb_centroid"), \
             patch("tracer.queries.scan_clustering.delete_centroid"):
            assert merge_duplicate_clusters(pid) == 0


@pytest.mark.django_db
class TestJunctionReconciliation:
    def test_shared_trace_does_not_violate_unique_constraint(self, project):
        """A trace present in BOTH clusters must not be re-pointed onto a row that
        already exists — that is the one move in this pass that can raise."""
        pid = project.id
        keep = _cluster(pid, "C1", members=4, traces=[])
        absorb = _cluster(pid, "C2", members=2, traces=[])
        shared = uuid.uuid4()
        only_in_absorb = uuid.uuid4()
        ErrorClusterTraces.objects.create(cluster=keep, trace_id=shared)
        ErrorClusterTraces.objects.create(cluster=absorb, trace_id=shared)
        ErrorClusterTraces.objects.create(cluster=absorb, trace_id=only_in_absorb)

        rows = [("C1", NEAR_A, 4), ("C2", NEAR_B, 2)]
        with patch("tracer.queries.scan_clustering.ClickHouseVectorDB", _ch(rows)), \
             patch("tracer.queries.scan_clustering.ensure_centroid_table"), \
             patch("tracer.queries.scan_clustering._absorb_centroid"), \
             patch("tracer.queries.scan_clustering.delete_centroid"):
            assert merge_duplicate_clusters(pid) == 1

        survivor = TraceErrorGroup.objects.get(project_id=pid)
        traces = set(
            ErrorClusterTraces.objects.filter(cluster=survivor).values_list(
                "trace_id", flat=True
            )
        )
        assert traces == {shared, only_in_absorb}       # deduped, nothing lost
        assert ErrorClusterTraces.objects.filter(cluster=survivor).count() == 2
        assert survivor.unique_traces == 2


@pytest.mark.django_db
class TestTriageIsNeverTrampled:
    """A merge must not launder a human-triaged cluster back into the feed.

    ``_refresh_status`` already refuses to overwrite acknowledged/resolved. Absorbing
    such a cluster into a for_review one would dissolve that decision by the back door
    — same harm, extra steps.
    """

    def _run(self, pid):
        rows = [("C1", NEAR_A, 9), ("C2", NEAR_B, 2)]
        with patch("tracer.queries.scan_clustering.ClickHouseVectorDB", _ch(rows)), \
             patch("tracer.queries.scan_clustering.ensure_centroid_table"), \
             patch("tracer.queries.scan_clustering._absorb_centroid"), \
             patch("tracer.queries.scan_clustering.delete_centroid"):
            return merge_duplicate_clusters(pid)

    def test_triaged_cluster_survives_even_when_smaller(self, project):
        pid = project.id
        big = _cluster(pid, "C1", members=9, traces=[])          # would normally win
        small = _cluster(pid, "C2", members=2, traces=[])
        small.status = FeedIssueStatus.RESOLVED
        small.save(update_fields=["status"])

        assert self._run(pid) == 1
        survivor = TraceErrorGroup.objects.get(project_id=pid)
        assert survivor.cluster_id == "C2"                        # the triaged one survives
        assert survivor.status == FeedIssueStatus.RESOLVED        # and keeps its decision
        assert survivor.error_count == 11                         # nothing lost
        assert not TraceErrorGroup.objects.filter(cluster_id=big.cluster_id).exists()

    def test_two_triaged_clusters_are_left_alone(self, project):
        pid = project.id
        for cid, st in (("C1", FeedIssueStatus.ACKNOWLEDGED), ("C2", FeedIssueStatus.RESOLVED)):
            c = _cluster(pid, cid, members=5, traces=[])
            c.status = st
            c.save(update_fields=["status"])

        assert self._run(pid) == 0
        assert TraceErrorGroup.objects.filter(project_id=pid).count() == 2
