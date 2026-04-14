#!/bin/bash
set -euo pipefail

# Builds both .deb packages end-to-end. Run inside a Debian trixie arm64
# environment (native Pi, arm64 Docker, or arm64 CI runner).
#
# Prereqs:
#   apt-get install -y fpm git cmake build-essential pkg-config \
#       libbluetooth-dev libudev-dev libusb-dev libv4l-dev swig \
#       python3 python3-pip rsync dpkg-dev

HERE="$(cd "$(dirname "$0")" && pwd)"

export VERSION="${VERSION:-$(git -C "$HERE/.." describe --tags --always --dirty 2>/dev/null | sed 's/^v//' || echo 0.0.0)}"
export ARCH_PSMOVE="${ARCH_PSMOVE:-$(dpkg --print-architecture)}"

echo "=== Building libpsmoveapi $VERSION ($ARCH_PSMOVE) ==="
bash "$HERE/psmoveapi/build.sh"
ARCH="$ARCH_PSMOVE" bash "$HERE/psmoveapi/fpm.sh"

echo "=== Building joustmania $VERSION (all) ==="
bash "$HERE/joustmania/build.sh"
ARCH=all bash "$HERE/joustmania/fpm.sh"

echo
echo "=== Artifacts ==="
ls -la "$HERE/dist/"
