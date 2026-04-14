#!/bin/bash
set -euo pipefail

# Packages the staged psmoveapi tree into a .deb named
# libpsmoveapi_<version>_<arch>.deb, emitted under packaging/dist/.

HERE="$(cd "$(dirname "$0")" && pwd)"
DIST="$HERE/../dist"
STAGE="$HERE/stage"

VERSION="${VERSION:-0.0.0}"
ARCH="${ARCH:-$(dpkg --print-architecture)}"

if [ ! -d "$STAGE/opt/joustmania/psmoveapi" ]; then
    echo "stage/ missing — run build.sh first" >&2
    exit 1
fi

mkdir -p "$DIST"

fpm -s dir -t deb \
    --name libpsmoveapi \
    --version "$VERSION" \
    --architecture "$ARCH" \
    --maintainer "JoustMania <noreply@joustmania.local>" \
    --description "Shared library and Python bindings for PS Move controllers, built for JoustMania." \
    --url "https://github.com/thp/psmoveapi" \
    --license "BSD-2-Clause" \
    --depends libbluetooth3 \
    --depends libudev1 \
    --depends "libusb-0.1-4 | libusb-1.0-0" \
    --depends python3 \
    --after-install "$HERE/postinst" \
    --after-remove "$HERE/postrm" \
    --config-files /etc/ld.so.conf.d/joustmania.conf \
    --package "$DIST/libpsmoveapi_${VERSION}_${ARCH}.deb" \
    --force \
    -C "$STAGE" \
    opt/joustmania/psmoveapi \
    etc/ld.so.conf.d/joustmania.conf

echo "Built: $DIST/libpsmoveapi_${VERSION}_${ARCH}.deb"
