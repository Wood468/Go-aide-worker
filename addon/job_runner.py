#!/usr/bin/env python3
"""Single-job, local-only Aider runner. No remote job submission or repository cloning."""
import json
import os
from pathlib import Path
import subprocess
import sys

WORKSPACE = Path("/data/workspace").resolve()
JOB_FILE = Path("/data/job.json")
MAX_JOB_BYTES = 8192
MAX_PROMPT_CHARS = 3000
TIMEOUT_SECONDS = 300
ALLOWED_ACTIONS = {"inspect", "test", "edit"}
MODEL = "gemini/gemini-2.5-flash"


class JobError(Exception):
    pass


def validate_job(raw, workspace=WORKSPACE):
    if not isinstance(raw, dict) or set(raw) != {"action", "prompt"}:
        raise JobError("Job must contain only action and prompt")
    action, prompt = raw["action"], raw["prompt"]
    if action not in ALLOWED_ACTIONS:
        raise JobError("Unsupported action")
    if not isinstance(prompt, str) or len(prompt) > MAX_PROMPT_CHARS:
        raise JobError("Invalid prompt length")
    if action == "edit" and not prompt.strip():
        raise JobError("Edit prompt must not be empty")
    if action == "edit" and os.environ.get("ENABLE_EDITS") != "true":
        raise JobError("Edits disabled; ENABLE_EDITS must be true")
    if action == "edit" and not os.environ.get("GEMINI_API_KEY"):
        raise JobError("Gemini key not configured")
    if not workspace.is_dir() or workspace.is_symlink():
        raise JobError("Workspace must be a real directory")
    if action in {"test", "edit"} and not (workspace / ".git").is_dir():
        raise JobError("Workspace must contain a checked-out Git repository")
    return action, prompt


def command_for(action, prompt):
    if action == "inspect":
        return ["git", "status", "--short", "--branch"]
    if action == "test":
        return [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
    return [
        "aider", "--model", MODEL, "--yes", "--no-auto-commits",
        "--no-gitignore", "--no-show-model-warnings", "--message", prompt,
    ]


def run(job_file=JOB_FILE, workspace=WORKSPACE):
    if job_file.is_symlink() or not job_file.is_file() or job_file.stat().st_size > MAX_JOB_BYTES:
        raise JobError("Missing, symlinked, or oversized job file")
    job = json.loads(job_file.read_text(encoding="utf-8"))
    action, prompt = validate_job(job, workspace)
    cmd = command_for(action, prompt)
    # Never log prompts, credentials, or command arguments.
    print(f"Starting bounded local job: {action}", flush=True)
    try:
        completed = subprocess.run(
            cmd, cwd=workspace, stdin=subprocess.DEVNULL,
            timeout=TIMEOUT_SECONDS, check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired as exc:
        raise JobError("Job timed out") from exc
    print(f"Job finished with exit code {completed.returncode}", flush=True)
    return completed.returncode


if __name__ == "__main__":
    try:
        sys.exit(run())
    except (JobError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"Job rejected or failed: {type(exc).__name__}", file=sys.stderr)
        sys.exit(2)
