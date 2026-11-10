#!/usr/bin/env python3
"""Replace worker's isolated public checkout with latest main (not GitHub main)."""
import os
from pathlib import Path
import shutil
import sys

import checkout_repo

ROOT = Path("/data/workspace")
SOURCE = ROOT / "source"
NEXT = ROOT / ".source-next"
OLD = ROOT / ".source-old"
REPO = "Wood468/crispy-barnacle"

class RefreshError(Exception):
    pass

def refresh(root=ROOT, checkout=checkout_repo.checkout):
    root = Path(root)
    source, next_dir, old_dir = root / "source", root / ".source-next", root / ".source-old"
    if (root.is_symlink() or not root.is_dir() or not source.is_dir()
            or source.is_symlink() or not (source / ".git").is_dir()
            or (source / ".git").is_symlink() or next_dir.exists()
            or next_dir.is_symlink() or old_dir.exists() or old_dir.is_symlink()):
        raise RefreshError("Unsafe isolated checkout refresh preconditions")
    # Clone first. The old checkout is kept intact if networking fails.
    checkout(REPO, REPO, destination=next_dir)
    try:
        source.rename(old_dir)
        try:
            next_dir.rename(source)
        except OSError:
            old_dir.rename(source)
            raise
    except OSError as exc:
        raise RefreshError("Checkout refresh swap failed") from None
    # Only the old, fixed-name isolated checkout is deleted.
    if old_dir.is_dir() and not old_dir.is_symlink():
        shutil.rmtree(old_dir)
    print("Fresh allowlisted public checkout prepared", flush=True)

if __name__ == "__main__":
    try:
        refresh()
    except (RefreshError, checkout_repo.CheckoutError, OSError, ValueError):
        print("Refresh failed safely", file=sys.stderr, flush=True)
        raise SystemExit(2)
