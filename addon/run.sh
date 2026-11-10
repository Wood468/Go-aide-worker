#!/usr/bin/env bash
set -euo pipefail
# Intentionally idle: no shell, jobs, repo writes, or API calls.
echo "GO Aider Worker prototype started (idle, no remote execution)."
exec sleep infinity
