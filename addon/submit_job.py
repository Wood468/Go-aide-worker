#!/usr/bin/env python3
"""Submit one allowlisted job locally; never accepts commands or remote requests."""
import argparse
import json
import os
from pathlib import Path
import tempfile

MAX_PROMPT_CHARS = 3000
ACTIONS = ("inspect", "test", "edit", "prepare")


def submit(action, prompt, destination=Path("/data/job.json")):
    if action not in ACTIONS or not isinstance(prompt, str) or len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError("Invalid action or prompt")
    if action == "edit" and (not prompt.strip() or os.environ.get("ENABLE_EDITS") != "true"):
        raise ValueError("Edits are disabled")
    if action == "prepare" and prompt.strip():
        raise ValueError("Preparation takes no user-supplied prompt")
    destination = Path(destination)
    parent = destination.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("Invalid job directory")
    if destination.is_symlink() or destination.exists():
        raise FileExistsError("Job already exists; explicit cleanup required")
    payload = json.dumps({"action": action, "prompt": prompt}).encode("utf-8")
    if len(payload) > 8192:
        raise ValueError("Job too large")
    fd, temp_path = tempfile.mkstemp(prefix=".job-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # Hard link fails rather than replacing an existing destination.
        os.link(temp_path, destination, follow_symlinks=False)
    finally:
        os.unlink(temp_path)
    return destination


def main():
    parser = argparse.ArgumentParser(description="Submit one local, bounded job")
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--prompt", default="", help="Job prompt (avoid secrets)")
    args = parser.parse_args()
    submit(args.action, args.prompt)
    print("Local job queued. Restart with RUN_LOCAL_JOB=true to execute.")


if __name__ == "__main__":
    main()
