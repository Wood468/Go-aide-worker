#!/usr/bin/env python3
"""Single-job, local-only runner with allowlisted public Git preparation."""
import json
import signal
import os
from pathlib import Path
import subprocess
import sys

WORKSPACE = Path("/data/workspace/source")
JOB_FILE = Path("/data/job.json")
MAX_JOB_BYTES = 8192
MAX_PROMPT_CHARS = 3000
TIMEOUT_SECONDS = 300
ALLOWED_ACTIONS = {"inspect", "test", "edit", "prepare", "verify", "publish"}
MODEL = "gemini/gemini-2.5-flash"


class JobError(Exception):
    pass


def validate_job(raw, workspace=WORKSPACE):
    if not isinstance(raw, dict) or set(raw) != {"action", "prompt"}:
        raise JobError("Job must contain only action and prompt")
    action, prompt = raw["action"], raw["prompt"]
    if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
        raise JobError("Unsupported action")
    if not isinstance(prompt, str) or len(prompt) > MAX_PROMPT_CHARS:
        raise JobError("Invalid prompt length")
    if action == "edit" and not prompt.strip():
        raise JobError("Edit prompt must not be empty")
    if action == "edit" and os.environ.get("ENABLE_EDITS") != "true":
        raise JobError("Edits disabled; ENABLE_EDITS must be true")
    if action == "edit" and not os.environ.get("GEMINI_API_KEY"):
        raise JobError("Gemini key not configured")
    if action == "publish" and (os.environ.get("ENABLE_PUBLISHING") != "true" or not os.environ.get("GITHUB_TOKEN")):
        raise JobError("Publishing disabled or token missing")
    if action in {"inspect", "test", "verify", "publish"} and prompt.strip():
        raise JobError("Non-edit jobs cannot include a prompt")
    if action == "prepare":
        if prompt.strip() or not os.environ.get("WORKER_PUBLIC_REPOSITORY") or not os.environ.get("ALLOWED_PUBLIC_REPOSITORIES"):
            raise JobError("Preparation requires configured public repository and empty prompt")
        if workspace.exists() or workspace.is_symlink() or not workspace.parent.is_dir() or workspace.parent.is_symlink():
            raise JobError("Preparation requires an empty, safe workspace destination")
        return action, prompt
    if not workspace.is_dir() or workspace.is_symlink():
        raise JobError("Workspace must be a real directory")
    if (workspace / ".git").is_symlink() or not (workspace / ".git").is_dir():
        raise JobError("Workspace must contain a checked-out Git repository")
    return action, prompt


def command_for(action, prompt):
    if action == "prepare":
        return [sys.executable, "/opt/worker/checkout_repo.py", os.environ["WORKER_PUBLIC_REPOSITORY"]]
    if action == "inspect":
        return ["git", "status", "--short", "--branch"]
    if action == "test":
        return [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
    if action == "publish":
        return [sys.executable, "/opt/worker/publisher.py"]
    return [
        "aider", "--model", MODEL, "--yes", "--no-auto-commits",
        "--no-gitignore", "--no-show-model-warnings", "--message", prompt,
    ]


def run(job_file=JOB_FILE, workspace=WORKSPACE):
    if job_file.is_symlink() or not job_file.is_file() or job_file.stat().st_size > MAX_JOB_BYTES:
        raise JobError("Missing, symlinked, or oversized job file")
    job = json.loads(job_file.read_text(encoding="utf-8"))
    action, prompt = validate_job(job, workspace)
    if action == "verify":
        expected = b"GO Aider Gemini smoke test succeeded\n"
        target = workspace / "WORKER_SMOKE_TEST.txt"
        verified = target.is_file() and not target.is_symlink() and target.read_bytes().rstrip(b"\r\n") == expected.rstrip(b"\n")
        print("Gemini smoke file verified" if verified else "Gemini smoke file missing or incorrect", flush=True)
        return 0 if verified else 3
    cmd = command_for(action, prompt)
    # Never log prompts, credentials, or command arguments.
    print(f"Starting bounded local job: {action}", flush=True)
    env = dict(os.environ) if action in {"edit", "publish"} else {
        key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "PYTHONDONTWRITEBYTECODE")
        if key in os.environ
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    if action == "prepare":
        env["ALLOWED_PUBLIC_REPOSITORIES"] = os.environ["ALLOWED_PUBLIC_REPOSITORIES"]
    if action == "publish":
        env.pop("GEMINI_API_KEY", None)
        env.pop("ENABLE_EDITS", None)
    process = subprocess.Popen(
        cmd, cwd=workspace.parent if action == "prepare" else workspace,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    try:
        return_code = process.wait(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        # Kill the whole process group, including children that ignore SIGTERM.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # The whole group already exited.
        process.wait()
        raise JobError("Job timed out") from exc
    print(f"Job finished with exit code {return_code}", flush=True)
    return return_code


if __name__ == "__main__":
    try:
        sys.exit(run())
    except (JobError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"Job rejected or failed: {type(exc).__name__}", file=sys.stderr)
        sys.exit(2)
