#!/usr/bin/env bash
set -euo pipefail
# Explicit opt-in: no job execution by default. No network listener.
if [[ "${RUN_LOCAL_JOB:-false}" == "true" ]]; then
  exec python /opt/worker/job_runner.py
fi
echo "GO Aider Worker idle; local job execution disabled."
exec sleep infinity
