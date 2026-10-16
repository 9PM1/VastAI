#!/bin/bash
# Build a SageAttention 2 wheel for every supported GPU arch, for publishing as a
# GitHub release that lib/sage.sh installs from. Run as root on a vastai/comfy
# instance (any GPU; the build does not use it):
#   bash /opt/comfy-xtra/tools/build-sage-wheel.sh /root/wheels
# then upload with the printed tag:
#   gh release create <tag> /root/wheels/*.whl --repo 9PM1/VastAI --title <tag> --notes ...
set -Eeuo pipefail
XTRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$XTRA_DIR/lib/common.sh"
. "$XTRA_DIR/lib/sage.sh"

out="${1:-/root/wheels}"
mkdir -p "$out"
build_sage2 "$SAGE_ARCHS" "$out"
ls -la "$out"
echo "Release tag: $(sage_wheel_tag)"
