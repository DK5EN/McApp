#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../../.."
TARBALL=scripts/container-tests/image/.cache/mcapp-v2.1.22.tar.gz
[ -f "$TARBALL" ] || gh release download v2.1.22 -R DK5EN/McApp -D scripts/container-tests/image/.cache
for arch in arm64 amd64; do
  docker buildx build --platform "linux/$arch" --load \
    -f scripts/container-tests/image/Dockerfile -t "mcapp-spike:$arch" .
done
docker image ls mcapp-spike
