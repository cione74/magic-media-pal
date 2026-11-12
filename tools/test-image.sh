#!/bin/sh
# No optical devices, production folders, credentials, or external network.
set -eu
root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
image="${1:-dexter-ripper:development}"
exec docker run --rm --network none \
    --entrypoint python3 \
    -v "$root/rootfs:/workspace/rootfs:ro" \
    -v "$root/tests:/workspace/tests:ro" \
    -v "$root/tools:/workspace/tools:ro" \
    -w /workspace "$image" tools/verify-packaged-source.py
