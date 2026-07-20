from __future__ import annotations

import logging

from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from tfc.capabilities import service
from tfc.capabilities.registry import FEATURE_REGISTRY
from tfc.licensing.types import (
    DeploymentFlavor,
    DeploymentLocation,
    LicenseState,
    derive_display_mode,
)

logger = logging.getLogger(__name__)


class CapabilitiesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        org = getattr(request, "organization", None)
        org_id = str(org.id) if org else None

        flavor = service.get_deployment_flavor()
        location = service.get_deployment_location()
        license_state = self._get_license_state()
        display_mode = derive_display_mode(flavor, location, license_state)

        features = {}
        for feature_id, feature_def in FEATURE_REGISTRY.items():
            decision = service.check(feature_id, org_id=org_id)
            features[feature_id] = {
                "display_name": feature_def.display_name,
                "allowed": decision.allowed,
                "reason_code": decision.reason_code,
                "requires_network": decision.requires_network,
                "oss_baseline": feature_def.oss_baseline,
            }

        response_data = {
            "deployment_flavor": flavor.value,
            "display_mode": display_mode.value,
            "license_state": license_state.value,
            "features": features,
        }

        if location == DeploymentLocation.SELF_HOSTED and self._is_admin(request):
            response_data["license"] = self._get_license_details()
            response_data["instance_id"] = self._get_instance_id()

        return Response(response_data)

    def _get_license_state(self) -> LicenseState:
        snapshot = service.get_license_snapshot()
        if snapshot is None:
            if service.get_deployment_flavor() == DeploymentFlavor.CLOUD:
                return LicenseState.NOT_APPLICABLE
            return LicenseState.MISSING
        return snapshot.state

    def _get_license_details(self) -> dict | None:
        snapshot = service.get_license_snapshot()
        if snapshot is None:
            return None
        if snapshot.state in (LicenseState.MISSING, LicenseState.NOT_APPLICABLE):
            return None
        return {
            "issued_to": snapshot.issued_to,
            "band": snapshot.band,
            "license_type": snapshot.license_type.value if snapshot.license_type else None,
            "expires_at": snapshot.expires_at.isoformat() if snapshot.expires_at else None,
            "grace_ends_at": snapshot.grace_ends_at.isoformat() if snapshot.grace_ends_at else None,
            "features_count": len(snapshot.features),
            "state": snapshot.state.value,
        }

    def _get_instance_id(self) -> str | None:
        try:
            from tfc.deployment_telemetry.state import get_or_create_telemetry_state

            state = get_or_create_telemetry_state()
            return str(state.instance_id)
        except Exception:
            logger.debug("capabilities_view_instance_id_unavailable", exc_info=True)
            return None

    def _is_admin(self, request) -> bool:
        user = request.user
        if not user or not user.is_authenticated:
            return False
        org = getattr(request, "organization", None)
        if org is None:
            return user.is_staff
        try:
            membership = user.memberships.filter(organization=org).first()
            if membership and membership.role in ("owner", "admin"):
                return True
        except Exception:
            logger.debug("capabilities_view_membership_check_failed", exc_info=True)
        return user.is_staff


class LicenseDetailView(APIView):
    """Self-hosted license info — reads from the locally validated JWT."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        snapshot = service.get_license_snapshot()
        if snapshot is None or snapshot.state in (
            LicenseState.MISSING,
            LicenseState.NOT_APPLICABLE,
        ):
            return Response(
                {"status": True, "result": {"licenses": []}},
            )

        license_entry = {
            "id": snapshot.license_id or "",
            "customer_name": snapshot.issued_to or "",
            "band": snapshot.band or "",
            "billing_interval": "annual",
            "features": sorted(snapshot.features),
            "max_traces_monthly": snapshot.limits.get("traces_monthly", -1),
            "max_gateway_monthly": snapshot.limits.get("gateway_requests_monthly", -1),
            "issued_at": snapshot.issued_at.isoformat() if snapshot.issued_at else None,
            "expires_at": snapshot.expires_at.isoformat() if snapshot.expires_at else None,
            "status": snapshot.state.value,
        }

        return Response({"status": True, "result": {"licenses": [license_entry], "read_only": True}})
