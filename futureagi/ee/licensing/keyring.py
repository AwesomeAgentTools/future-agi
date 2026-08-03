"""Public key management for license verification.

The self-hosted EE image bundles a public keyring (no private keys).
Keys are identified by `kid` in the JWT header. Supports key rotation
by retaining previous public keys.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)

# Bundled public keys. In production these would be loaded from a file
# or packaged resource. For now, they're configured via Django settings.
_KEY_RING: dict[str, "PublicKeyEntry"] = {}


@dataclass(frozen=True)
class PublicKeyEntry:
    kid: str
    algorithm: str
    public_key: str


ALLOWED_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "PS256", "PS384", "PS512"})

REQUIRED_ISSUER = "https://licenses.futureagi.com"
REQUIRED_AUDIENCE = "futureagi-self-hosted"
REQUIRED_TYPE = "futureagi-enterprise-license"

# 5 minutes default clock skew tolerance
DEFAULT_CLOCK_SKEW_SECONDS = 300


def load_keyring_from_settings() -> None:
    """Replace the process keyring with the keys configured in Django settings."""
    global _KEY_RING
    _KEY_RING = {}

    try:
        import json

        from django.conf import settings

        keyring: dict[str, PublicKeyEntry] = {}
        public_key = getattr(settings, "EE_LICENSE_PUBLIC_KEY", "").strip()
        if public_key:
            keyring["default"] = PublicKeyEntry(
                kid="default",
                algorithm="RS256",
                public_key=public_key,
            )

        keys_json = getattr(settings, "EE_LICENSE_PUBLIC_KEYS", "")
        if keys_json:
            keys_list = json.loads(keys_json)
            if not isinstance(keys_list, list):
                raise ValueError("EE_LICENSE_PUBLIC_KEYS must be a JSON list")

            for key_data in keys_list:
                if not isinstance(key_data, dict):
                    raise ValueError("Each license public key must be an object")

                kid = key_data.get("kid")
                algorithm = key_data.get("algorithm", "RS256")
                configured_key = key_data.get("public_key")
                if not isinstance(kid, str) or not kid:
                    raise ValueError("Each license public key requires a kid")
                if algorithm not in ALLOWED_ALGORITHMS:
                    raise ValueError(f"Unsupported license key algorithm: {algorithm}")
                if not isinstance(configured_key, str) or not configured_key.strip():
                    raise ValueError(f"License public key {kid} is empty")

                keyring[kid] = PublicKeyEntry(
                    kid=kid,
                    algorithm=algorithm,
                    public_key=configured_key.strip(),
                )

        _KEY_RING = keyring
        logger.debug("license_keyring_loaded", key_count=len(_KEY_RING))
    except (TypeError, ValueError, KeyError):
        logger.exception("license_keyring_load_failed")


def get_key(kid: str) -> Optional[PublicKeyEntry]:
    return _KEY_RING.get(kid)


def has_any_keys() -> bool:
    return len(_KEY_RING) > 0


def get_clock_skew_seconds() -> int:
    try:
        from django.conf import settings

        return int(getattr(settings, "EE_LICENSE_CLOCK_SKEW_SECONDS", DEFAULT_CLOCK_SKEW_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_CLOCK_SKEW_SECONDS
