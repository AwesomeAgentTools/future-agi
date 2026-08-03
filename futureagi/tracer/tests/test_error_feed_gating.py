"""Error-feed license gating + opened-gate regressions (TH-7256).

Error feed is the license-reserved feature: the code is public, but the
API denies (HTTP 402) without an entitlement. Scenarios, optimization
endpoints, and TTS voice creation ship open — the gates the licensing PR
added to them must stay out.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime

import pytest

from tfc.capabilities import service
from tfc.ee_gating import EEFeature, FeatureUnavailable, check_ee_feature
from tfc.licensing.types import (
    DeploymentFlavor,
    DeploymentLocation,
    LicenseSnapshot,
    LicenseState,
    LicenseType,
)
from tracer.views.feed._permissions import ErrorFeedLicenseRequired

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


class _FakeResolver:
    def __init__(self, snapshot: LicenseSnapshot):
        self._snapshot = snapshot

    def get_snapshot(self) -> LicenseSnapshot:
        return self._snapshot


def _snapshot(state: LicenseState, features: frozenset[str]) -> LicenseSnapshot:
    return LicenseSnapshot(
        state=state,
        license_type=LicenseType.PRODUCTION,
        license_id="lic_test",
        band="enterprise",
        features=features,
        validated_at=datetime(2025, 6, 1, tzinfo=UTC),
    )


def _configure_ee(snapshot: LicenseSnapshot) -> None:
    service._configured = False
    service.configure(
        flavor=DeploymentFlavor.SELF_HOSTED_EE,
        location=DeploymentLocation.SELF_HOSTED,
        license_resolver=_FakeResolver(snapshot),
    )


def _configure_oss() -> None:
    service._configured = False
    service.configure(
        flavor=DeploymentFlavor.OSS,
        location=DeploymentLocation.SELF_HOSTED,
        license_resolver=None,
    )


@pytest.fixture(autouse=True)
def _reset_service():
    yield
    _configure_oss()
    service._configured = False


class TestErrorFeedGate:
    def test_oss_denies_error_feed(self):
        _configure_oss()
        with pytest.raises(FeatureUnavailable):
            check_ee_feature(EEFeature.ERROR_FEED, org_id="org_1")

    def test_ee_license_without_error_feed_denies(self):
        _configure_ee(
            _snapshot(LicenseState.ACTIVE, frozenset({"voice_sim", "falcon_ai"}))
        )
        with pytest.raises(FeatureUnavailable):
            check_ee_feature(EEFeature.ERROR_FEED, org_id="org_1")

    def test_ee_expired_license_denies(self):
        _configure_ee(_snapshot(LicenseState.EXPIRED, frozenset({"error_feed"})))
        with pytest.raises(FeatureUnavailable):
            check_ee_feature(EEFeature.ERROR_FEED, org_id="org_1")

    def test_ee_active_license_with_error_feed_allows(self):
        _configure_ee(_snapshot(LicenseState.ACTIVE, frozenset({"error_feed"})))
        check_ee_feature(EEFeature.ERROR_FEED, org_id="org_1")

    def test_every_feed_view_carries_the_license_mixin(self):
        from tracer.views.feed.detail_view import FeedDetailView
        from tracer.views.feed.linear_issue_view import (
            CreateLinearIssueView,
            LinearTeamsView,
        )
        from tracer.views.feed.list_view import FeedListView, FeedStatsView
        from tracer.views.feed.tab_views import (
            FeedDeepAnalysisView,
            FeedOverviewView,
            FeedRootCauseView,
            FeedSidebarView,
            FeedTracesView,
            FeedTrendsView,
        )

        for view in (
            FeedListView,
            FeedStatsView,
            FeedDetailView,
            FeedOverviewView,
            FeedTracesView,
            FeedTrendsView,
            FeedSidebarView,
            FeedRootCauseView,
            FeedDeepAnalysisView,
            CreateLinearIssueView,
            LinearTeamsView,
        ):
            assert issubclass(
                view, ErrorFeedLicenseRequired
            ), f"{view.__name__} lost the error-feed license gate"


class TestOpenedGates:
    """The licensing PR gated surfaces that ship open (TH-7256)."""

    def test_scenarios_open_on_oss(self):
        _configure_oss()
        check_ee_feature(EEFeature.SCENARIOS, org_id="org_1")  # must not raise

    def test_scenarios_open_on_unlicensed_ee(self):
        _configure_ee(_snapshot(LicenseState.EXPIRED, frozenset()))
        check_ee_feature(EEFeature.SCENARIOS, org_id="org_1")  # must not raise

    @pytest.mark.parametrize(
        "relative",
        [
            "model_hub/views/develop_optimisation.py",
            "model_hub/views/tts_voices.py",
            "tfc/temporal/dataset_optimization/activities.py",
        ],
    )
    def test_reopened_modules_carry_no_view_gates(self, relative):
        """These endpoints are ungated in production; the PR-added
        check_ee_feature calls must not come back."""
        source = (_REPO_ROOT / relative).read_text()
        assert (
            "check_ee_feature" not in source
        ), f"{relative} regained a license gate; it ships open (TH-7256)"
