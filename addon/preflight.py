#!/usr/bin/env python3
"""Credential-free add-on preflight for the unprivileged local worker."""
import os
from pathlib import Path
import shutil
import sys

WORKSPACE_PARENT = Path("/data/workspace")


def check(path=WORKSPACE_PARENT):
    failures = []
    if os.geteuid() == 0:
        failures.append("Worker must not run as root")
    if not path.is_dir() or path.is_symlink() or not os.access(path, os.W_OK | os.X_OK):
        failures.append("Workspace mount is unavailable or not writable")
    if not shutil.which("git"):
        failures.append("git executable missing")
    if not shutil.which("aider"):
        failures.append("Aider executable missing")
    if failures:
        for issue in failures:
            print(f"Preflight blocked: {issue}", file=sys.stderr)
        return 2
    print("Worker preflight passed: non-root, writable workspace, git, Aider")
    return 0


if __name__ == "__main__":
    sys.exit(check())
