#!/bin/bash
set -euo pipefail

# Stages the joustmania application tree under packaging/joustmania/stage/
# in the layout the .deb will install:
#
#   /opt/joustmania/...              (app source)
#   /lib/systemd/system/joustmania.service

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
STAGE="$HERE/stage"

rm -rf "$STAGE"
mkdir -p "$STAGE/opt/joustmania" "$STAGE/lib/systemd/system"

# Copy the app. Excludes dev-only / packaging / VCS / runtime artefacts.
rsync -a \
    --exclude '.git' \
    --exclude '.github' \
    --exclude 'packaging' \
    --exclude 'venv' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude 'logs' \
    --exclude 'build_output.txt' \
    --exclude 'setup.log' \
    --exclude 'proc[0-9]*' \
    --exclude 'tests' \
    --exclude 'testing' \
    --exclude 'color_tests' \
    --exclude 'PyAudio-*.whl' \
    --exclude 'setup_windows.bat' \
    --exclude 'win_jm_dbus.py' \
    --exclude 'win_pair.py' \
    --exclude 'setup.sh' \
    --exclude 'joust.sh' \
    --exclude 'conf/supervisor' \
    "$REPO"/ "$STAGE/opt/joustmania/"

# logs/ is created but left empty — piparty.py writes timestamped files there.
mkdir -p "$STAGE/opt/joustmania/logs"

cp "$HERE/systemd/joustmania.service" "$STAGE/lib/systemd/system/joustmania.service"

echo "Staged joustmania under $STAGE"
