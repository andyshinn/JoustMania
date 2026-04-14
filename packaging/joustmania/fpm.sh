#!/bin/bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DIST="$HERE/../dist"
STAGE="$HERE/stage"
DEBIAN="$HERE/debian"

VERSION="${VERSION:-0.0.0}"
ARCH="${ARCH:-all}"

if [ ! -d "$STAGE/opt/joustmania" ]; then
    echo "stage/ missing — run build.sh first" >&2
    exit 1
fi

mkdir -p "$DIST"

fpm -s dir -t deb \
    --name joustmania \
    --version "$VERSION" \
    --architecture "$ARCH" \
    --maintainer "JoustMania <noreply@joustmania.local>" \
    --description "JoustMania — PS Move party game for Raspberry Pi." \
    --url "https://github.com/adangert/JoustMania" \
    --license "GPL-3.0" \
    --depends "libpsmoveapi" \
    --depends "python3 (>= 3.13)" \
    --depends "python3-venv" \
    --depends "python3-pip" \
    --depends "python3-flask" \
    --depends "python3-flaskext.wtf" \
    --depends "python3-pygame" \
    --depends "python3-yaml" \
    --depends "python3-dbus" \
    --depends "python3-alsaaudio" \
    --depends "python3-dotenv" \
    --depends "python3-scipy" \
    --depends "python3-numpy" \
    --depends "python3-psutil" \
    --depends "python3-pyaudio" \
    --depends "bluez" \
    --depends "bluez-tools" \
    --depends "alsa-utils" \
    --depends "rfkill" \
    --depends "iptables" \
    --depends "ffmpeg" \
    --depends "libsdl2-mixer-2.0-0" \
    --depends "debconf" \
    --config-files /lib/systemd/system/joustmania.service \
    --after-install "$DEBIAN/postinst" \
    --before-remove "$DEBIAN/prerm" \
    --after-remove "$DEBIAN/postrm" \
    --deb-templates "$DEBIAN/templates" \
    --deb-config "$DEBIAN/config" \
    --package "$DIST/joustmania_${VERSION}_${ARCH}.deb" \
    --force \
    -C "$STAGE" \
    opt/joustmania \
    lib/systemd/system

echo "Built: $DIST/joustmania_${VERSION}_${ARCH}.deb"
