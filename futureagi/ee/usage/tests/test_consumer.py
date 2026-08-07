"""Regression tests for the usage consumer's duplicate-event handling.

TH-7056: a duplicate event (same event_id, redelivered or re-polled) was
still being credited to the Redis counter / UsageSummary even though
UsageEventLog's bulk_create(ignore_conflicts=True) silently rejects it.
counter_updates was accumulated in the same pass that built event_log_rows,
before bulk_create ever ran, so the dedupe decision at the DB layer had no
effect on what Redis got incremented by.

These are integration tests against a real Redis Stream + consumer group
and a real Postgres-backed UsageEventLog/UsageSummary, matching the
Category 16 pipeline-integration style in test_cloud_pricing.py — this is
a dedupe/ordering bug, not something a mocked unit test can catch, since
the whole defect is about the interaction between two real stores.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from accounts.models.organization import Organization
from ee.usage.models.usage import (
    OrganizationSubscription,
    PlanChoices,
    SubscriptionTier,
    UsageEventLog,
)
from ee.usage.schemas.events import UsageEvent
from ee.usage.services.consumer import (
    CONSUMER_GROUP,
    _ensure_consumer_group,
    process_batch,
)
from ee.usage.services.emitter import STREAM_KEY, emit, get_redis


@pytest.fixture
def org(db):
    return Organization.objects.create(name="test-org-consumer-dedupe")


@pytest.fixture
def free_sub(db, org):
    tier, _ = SubscriptionTier.objects.get_or_create(
        name="free", defaults={"wallet_refill_amount": 0}
    )
    return OrganizationSubscription.objects.create(
        organization=org,
        subscription_tier=tier,
        plan=PlanChoices.FREE.value,
    )


_VOLATILE_PATTERNS = ("usage:*", "plan:*", "pause:*", "budget:*")


def _purge(r):
    for pattern in _VOLATILE_PATTERNS:
        for key in r.keys(pattern):
            r.delete(key)


@pytest.fixture
def redis_client():
    r = get_redis()
    # Purge on the way in as well as out. Redis outlives the per-test database
    # rollback, so an event left in the stream by any earlier test names an
    # organization whose row is already gone; draining it here writes a
    # UsageEventLog with a dangling FK and the test errors in teardown when
    # SET CONSTRAINTS ALL IMMEDIATE runs. Cleaning up afterwards only protects
    # against tests that use this fixture — the pollution comes from ones that
    # do not.
    _purge(r)
    _ensure_consumer_group()
    yield r
    _purge(r)


def _period_key(org_id, dimension: str) -> str:
    period = datetime.utcnow().strftime("%Y-%m")
    return f"usage:{org_id}:{dimension}:{period}"


@pytest.mark.django_db
class TestConsumerDuplicateEventHandling:
    def test_duplicate_event_in_same_batch_is_not_double_counted(
        self, org, free_sub, redis_client
    ):
        """Two stream entries with the identical event_id, read in one
        XREADGROUP call, must only be counted once — matching a client
        re-poll landing in the same batch as its original delivery."""
        dup_event_id = "11111111-1111-1111-1111-111111111111"

        emit(
            UsageEvent(
                event_id=dup_event_id,
                org_id=str(org.id),
                event_type="turing_large_evaluator",
                amount=5,
            )
        )
        emit(
            UsageEvent(
                event_id=dup_event_id,
                org_id=str(org.id),
                event_type="turing_large_evaluator",
                amount=5,
            )
        )

        process_batch()

        assert UsageEventLog.objects.filter(organization=org).count() == 1

        counter = redis_client.get(_period_key(org.id, "ai_credits"))
        assert int(float(counter)) == 5, (
            f"Redis counter must reflect exactly one event's worth of "
            f"credits, got {counter}"
        )

    def test_duplicate_event_across_batches_is_not_double_counted(
        self, org, free_sub, redis_client
    ):
        """The same event_id re-emitted after the first batch already
        persisted it (e.g. a re-poll minutes later) must not add to the
        counter a second time, even though it arrives in a later,
        separate call to process_batch()."""
        dup_event_id = "22222222-2222-2222-2222-222222222222"

        emit(
            UsageEvent(
                event_id=dup_event_id,
                org_id=str(org.id),
                event_type="turing_large_evaluator",
                amount=7,
            )
        )
        process_batch()

        assert UsageEventLog.objects.filter(organization=org).count() == 1
        counter = redis_client.get(_period_key(org.id, "ai_credits"))
        assert int(float(counter)) == 7

        # Same event_id, arrives in a brand new batch.
        emit(
            UsageEvent(
                event_id=dup_event_id,
                org_id=str(org.id),
                event_type="turing_large_evaluator",
                amount=7,
            )
        )
        process_batch()

        assert UsageEventLog.objects.filter(organization=org).count() == 1
        counter = redis_client.get(_period_key(org.id, "ai_credits"))
        assert int(float(counter)) == 7, (
            f"Redis counter must not double-count a duplicate delivered in "
            f"a later batch, got {counter}"
        )

    def test_duplicate_event_still_gets_acked(self, org, free_sub, redis_client):
        """A duplicate must not stay pending forever just because it isn't
        credited — it was still successfully processed."""
        dup_event_id = "33333333-3333-3333-3333-333333333333"

        emit(
            UsageEvent(
                event_id=dup_event_id,
                org_id=str(org.id),
                event_type="turing_large_evaluator",
                amount=1,
            )
        )
        emit(
            UsageEvent(
                event_id=dup_event_id,
                org_id=str(org.id),
                event_type="turing_large_evaluator",
                amount=1,
            )
        )

        process_batch()

        pending = redis_client.xpending(STREAM_KEY, CONSUMER_GROUP)
        assert pending["pending"] == 0, (
            "Both the original and the duplicate entry must be acked, "
            f"pending summary: {pending}"
        )

    def test_distinct_events_are_both_counted(self, org, free_sub, redis_client):
        """Sanity check: the fix must not suppress genuinely distinct
        events that happen to land in the same batch."""
        emit(
            UsageEvent(
                org_id=str(org.id), event_type="turing_large_evaluator", amount=3
            )
        )
        emit(
            UsageEvent(
                org_id=str(org.id), event_type="turing_large_evaluator", amount=4
            )
        )

        process_batch()

        assert UsageEventLog.objects.filter(organization=org).count() == 2
        counter = redis_client.get(_period_key(org.id, "ai_credits"))
        assert int(float(counter)) == 7
