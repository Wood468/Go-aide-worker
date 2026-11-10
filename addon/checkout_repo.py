#!/usr/bin/env python3
"""Prepare an explicitly allowlisted public GitHub repository, without credentials."""
import os
from pathlib import Path
import re
import subprocess
import sys

DEST = Path("/data/workspace/source")
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
MAX_TREE_BYTES = 20 * 1024 * 1024


class CheckoutError(Exception):
    pass


def validate_repository(repository, allowlist):
    if (not isinstance(repository, str) or not REPO_PATTERN.fullmatch(repository)
            or any(part.startswith(".") for part in repository.split("/"))):
        raise CheckoutError("Repository must be a safe owner/name slug")
    allowed = {x.strip() for x in allowlist.split(",") if x.strip()}
    if repository not in allowed:
        raise CheckoutError("Repository not explicitly allowlisted")
    return f"https://github.com/{repository}.git"


def git_environment():
    # Do not inherit API keys, authorization tokens or user Git configuration.
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "protocol.file.allow",
        "GIT_CONFIG_VALUE_0": "never",
        "GIT_CONFIG_KEY_1": "protocol.ext.allow",
        "GIT_CONFIG_VALUE_1": "never",
        "GIT_CONFIG_KEY_2": "core.hooksPath",
        "GIT_CONFIG_VALUE_2": "/dev/null",
    }
    return env


def run_git(command, env, cwd=None, capture=False):
    try:
        result = subprocess.run(
            command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=120, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CheckoutError("Git operation timed out") from exc
    if result.returncode:
        raise CheckoutError("Git operation failed")
    return result.stdout if capture else b""


def validate_tree(raw):
    if raw is None or len(raw) > MAX_TREE_BYTES:
        raise CheckoutError("Unexpected or oversized Git tree")
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        if b"\t" not in entry:
            raise CheckoutError("Malformed Git tree entry")
        header, path = entry.split(b"\t", 1)
        mode = header.split(b" ", 1)[0]
        if mode not in (b"100644", b"100755") or not path:
            # Reject symlinks and gitlinks/submodules, not merely dangerous names.
            raise CheckoutError("Unsupported Git tree entry")
        if b".git" in path.split(b"/"):
            raise CheckoutError("Reserved Git path")
    return True


def checkout(repository, allowlist, destination=DEST):
    url = validate_repository(repository, allowlist)
    destination = Path(destination)
    if destination.is_symlink() or destination.exists():
        raise CheckoutError("Destination must not exist")
    if not destination.parent.is_dir() or destination.parent.is_symlink():
        raise CheckoutError("Invalid destination parent")
    env = git_environment()
    run_git(
        ["git", "-c", "credential.helper=", "clone", "--depth", "1",
         "--single-branch", "--no-checkout", "--", url, str(destination)],
        env,
    )
    tree = run_git(["git", "ls-tree", "-r", "-z", "HEAD"], env,
                   cwd=destination, capture=True)
    validate_tree(tree)
    # Checkout writes files only: no submodule recursion or running project tests.
    run_git(["git", "checkout", "--force", "HEAD", "--", "."], env,
            cwd=destination)
    return destination


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise CheckoutError("Expected one owner/name repository")
        checkout(sys.argv[1], os.environ.get("ALLOWED_PUBLIC_REPOSITORIES", ""))
        print("Allowlisted public repository prepared; no project code executed")
    except (CheckoutError, OSError) as exc:
        print(f"Repository preparation rejected: {type(exc).__name__}", file=sys.stderr)
        sys.exit(2)
