#!/bin/bash
set -euo pipefail

# Builds psmoveapi from upstream at the pinned commit and stages it under
# packaging/psmoveapi/stage/ in the layout the joustmania .deb expects:
#
#   /opt/joustmania/psmoveapi/libpsmoveapi.so
#   /opt/joustmania/psmoveapi/libpsmoveapi_tracker.so   (not built)
#   /opt/joustmania/psmoveapi/psmove.py
#   /opt/joustmania/psmoveapi/_psmove.so
#
# Matches the cmake flags used in setup.sh:70-86.

PSMOVEAPI_COMMIT="${PSMOVEAPI_COMMIT:-8a1f8d035e9c82c5c134d848d9fbb4dd37a34b58}"
PSMOVEAPI_REPO="${PSMOVEAPI_REPO:-https://github.com/thp/psmoveapi.git}"

HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$HERE/work"
STAGE="$HERE/stage"
INSTALL_PREFIX="/opt/joustmania/psmoveapi"

rm -rf "$WORK" "$STAGE"
mkdir -p "$WORK" "$STAGE$INSTALL_PREFIX" "$STAGE/etc/ld.so.conf.d"

# Tell the dynamic linker where to find libpsmoveapi.so so _psmove.so
# can dlopen it. ldconfig is run from the deb's postinst.
echo "$INSTALL_PREFIX" > "$STAGE/etc/ld.so.conf.d/joustmania.conf"

cd "$WORK"
git clone --recursive "$PSMOVEAPI_REPO" psmoveapi
cd psmoveapi
git checkout "$PSMOVEAPI_COMMIT"
git submodule update --init --recursive

mkdir build
cd build
cmake .. \
    -DPSMOVE_BUILD_CSHARP_BINDINGS:BOOL=OFF \
    -DPSMOVE_BUILD_EXAMPLES:BOOL=OFF \
    -DPSMOVE_BUILD_JAVA_BINDINGS:BOOL=OFF \
    -DPSMOVE_BUILD_OPENGL_EXAMPLES:BOOL=OFF \
    -DPSMOVE_BUILD_PROCESSING_BINDINGS:BOOL=OFF \
    -DPSMOVE_BUILD_PYTHON_BINDINGS:BOOL=ON \
    -DPSMOVE_BUILD_TESTS:BOOL=OFF \
    -DPSMOVE_BUILD_TRACKER:BOOL=OFF \
    -DPSMOVE_USE_PSEYE:BOOL=OFF
make -j"$(nproc)"

# Stage only the artifacts joustmania actually imports / dlopens.
shopt -s nullglob
for f in libpsmoveapi*.so* psmove.py _psmove*.so; do
    [ -e "$f" ] && cp -a "$f" "$STAGE$INSTALL_PREFIX/"
done

echo "Staged psmoveapi under $STAGE$INSTALL_PREFIX :"
ls -la "$STAGE$INSTALL_PREFIX"
