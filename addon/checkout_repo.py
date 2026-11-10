#!/usr/bin/env python3
"""Explicitly allowlisted, public HTTPS Git checkout. No credentials or push."""
import os
from pathlib import Path
import re
import subprocess
import sys

DEST = Path("/data/workspace")
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")


class CheckoutError(Exception):
    pass


def validate_repository(repository, allowlist):
    if not isinstance(repository, str) or not REPO_PATTERN.fullmatch(repository):
        raise CheckoutError("Repository must be an owner/name slug")
    allowed = {x.strip() for x in allowlist.split(",") if x.strip()}
    if repository not in allowed:
        raise CheckoutError("Repository not explicitly allowlisted")
    return f"https://github.com/{repository}.git"


def checkout(repository, allowlist, destination=DEST):
    url = validate_repository(repository, allowlist)
    destination = Path(destination)
    if destination.is_symlink() or destination.exists():
        raise CheckoutError("Destination must not exist")
    if not destination.parent.is_dir() or destination.parent.is_symlink():
        raise CheckoutError("Invalid destination parent")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "protocol.file.allow",
        "GIT_CONFIG_VALUE_0": "never",
        "GIT_CONFIG_KEY_1": "protocol.ext.allow",
        "GIT_CONFIG_VALUE_1": "never",
    }
    try:
        result = subprocess.run(
            ["git", "-c", "credential.helper=", "clone", "--depth", "1",
             "--single-branch", "--no-checkout", "--", url, str(destination)],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=120, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CheckoutError("Checkout timed out") from exc
    if result.returncode:
        raise CheckoutError("Clone failed")
    # No checkout of untrusted source files yet: fetch only, with no hooks or scripts.
    return destination


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise CheckoutError("Expected exactly one owner/name repository")
        checkout(sys.argv[1], os.environ.get("ALLOWED_PUBLIC_REPOSITORIES", ""))
        print("Repository fetched without working-tree checkout")
    except (CheckoutError, OSError) as exc:
        print(f"Checkout rejected: {type(exc).__name__}", file=sys.stderr)
        sys.exit(2)
