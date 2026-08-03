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

_KEY_RING: dict[str, "PublicKeyEntry"] = {}


@dataclass(frozen=True)
class PublicKeyEntry:
    kid: str
    algorithm: str
    public_key: str


# Official FutureAGI license-verification keys, baked into source so a
# deployment cannot swap the trust root via environment variables (a
# self-signed license against a self-provided env key must not validate
# without a source edit). Public keys only — signing keys live in the
# private cloud control plane. Drop the production key(s) here before GA.
# Settings/env keys (EE_LICENSE_PUBLIC_KEY[S]) may only ADD rotation kids;
# they can never replace a bundled kid.
_BUNDLED_KEYS: tuple[PublicKeyEntry, ...] = ()


ALLOWED_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "PS256", "PS384", "PS512"})

REQUIRED_ISSUER = "https://licenses.futureagi.com"
REQUIRED_AUDIENCE = "futureagi-self-hosted"
REQUIRED_TYPE = "futureagi-enterprise-license"

# 5 minutes default clock skew tolerance
DEFAULT_CLOCK_SKEW_SECONDS = 300


def load_keyring_from_settings() -> None:
    """Rebuild the process keyring: bundled trust root first, then settings.

    Settings/env-provided keys may only add new kids (rotation); a key that
    collides with a bundled kid is rejected. On any settings parse failure
    the keyring falls back to the bundled keys — never to an empty ring
    while a trust root is bundled.
    """
    global _KEY_RING
    bundled = {entry.kid: entry for entry in _BUNDLED_KEYS}
    _KEY_RING = dict(bundled)

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

        merged = dict(bundled)
        for kid, entry in keyring.items():
            if kid in bundled and bundled[kid].public_key != entry.public_key:
                logger.error(
                    "license_keyring_bundled_kid_override_rejected", kid=kid
                )
                continue
            merged[kid] = entry

        _KEY_RING = merged
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
