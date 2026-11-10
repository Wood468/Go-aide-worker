#!/usr/bin/env bash
set -euo pipefail
# Read options from Home Assistant's protected /data volume, then drop UID.
exec python /opt/worker/launcher.py
