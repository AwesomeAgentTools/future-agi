#!/bin/bash
set -euo pipefail

# Verify image contents for the three-image Enterprise licensing build.
# Run after building all three images locally.
#
# Usage: ./scripts/verify-image-contents.sh <version>
# Example: ./scripts/verify-image-contents.sh v1.23.0

VERSION="${1:-latest}"
EE_IMAGE="futureagi/future-agi-ee:${VERSION}"
CLOUD_IMAGE="futureagi/future-agi-cloud:${VERSION}"
OSS_IMAGE="futureagi/future-agi:${VERSION}"

PASS=0
FAIL=0

check() {
  local desc="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    echo "  PASS: $desc"
    ((PASS++))
  else
    echo "  FAIL: $desc"
    ((FAIL++))
  fi
}

check_absent() {
  local desc="$1"
  shift
  if ! "$@" >/dev/null 2>&1; then
    echo "  PASS: $desc"
    ((PASS++))
  else
    echo "  FAIL: $desc"
    ((FAIL++))
  fi
}

echo "=== OSS Image: ${OSS_IMAGE} ==="
check_absent "No ee/ directory" docker run --rm "$OSS_IMAGE" test -d /app/backend/ee
check "Django boots" docker run --rm "$OSS_IMAGE" python -c "import django; django.setup()" 
echo ""

echo "=== Self-hosted EE Image: ${EE_IMAGE} ==="
check "Has ee/ directory" docker run --rm "$EE_IMAGE" test -d /app/backend/ee
check "Has ee/licensing/" docker run --rm "$EE_IMAGE" test -d /app/backend/ee/licensing
check "Has ee/falcon_ai/" docker run --rm "$EE_IMAGE" test -d /app/backend/ee/falcon_ai
check "Has ee/voice/" docker run --rm "$EE_IMAGE" test -d /app/backend/ee/voice
check "Has ee/turing/" docker run --rm "$EE_IMAGE" test -d /app/backend/ee/turing
check "Has ee/protect/" docker run --rm "$EE_IMAGE" test -d /app/backend/ee/protect
check "Has ee/evals/" docker run --rm "$EE_IMAGE" test -d /app/backend/ee/evals
check_absent "No ee/cloud/ directory" docker run --rm "$EE_IMAGE" test -d /app/backend/ee/cloud
check_absent "No license_generator" docker run --rm "$EE_IMAGE" test -f /app/backend/ee/cloud/control_plane/license_generator.py
check_absent "No stripe_service" docker run --rm "$EE_IMAGE" test -f /app/backend/ee/cloud/billing/stripe_service.py
check "Django boots (no key)" docker run --rm "$EE_IMAGE" python -c "import django; django.setup()"
check "Django boots (invalid key)" docker run --rm -e EE_LICENSE_KEY=invalid "$EE_IMAGE" python -c "import django; django.setup()"
echo ""

echo "=== Cloud Image: ${CLOUD_IMAGE} ==="
check "Has ee/ directory" docker run --rm "$CLOUD_IMAGE" test -d /app/backend/ee
check "Has ee/cloud/ directory" docker run --rm "$CLOUD_IMAGE" test -d /app/backend/ee/cloud
check "Has ee/cloud/billing/" docker run --rm "$CLOUD_IMAGE" test -d /app/backend/ee/cloud/billing
check "Has ee/cloud/control_plane/" docker run --rm "$CLOUD_IMAGE" test -d /app/backend/ee/cloud/control_plane
check "Has ee/cloud/telemetry/" docker run --rm "$CLOUD_IMAGE" test -d /app/backend/ee/cloud/telemetry
check "Has license_generator" docker run --rm "$CLOUD_IMAGE" test -f /app/backend/ee/cloud/control_plane/license_generator.py
check "Has stripe_service" docker run --rm "$CLOUD_IMAGE" test -f /app/backend/ee/cloud/billing/stripe_service.py
check "Django boots (cloud)" docker run --rm -e CLOUD_DEPLOYMENT=DEV "$CLOUD_IMAGE" python -c "import django; django.setup()"
echo ""

echo "=== Results ==="
echo "  Passed: $PASS"
echo "  Failed: $FAIL"
if [ "$FAIL" -gt 0 ]; then
  echo "  STATUS: FAILED"
  exit 1
fi
echo "  STATUS: ALL PASSED"
