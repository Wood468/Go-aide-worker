#!/usr/bin/env python3
"""Single-job, local-only runner with allowlisted public Git preparation."""
import json
import signal
import os
from pathlib import Path
import subprocess
import sys
import tempfile

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


def classify_edit_output(raw):
    """Classify model output without disclosing it to Home Assistant logs."""
    raw = raw[:200_000].lower()
    categories = (
        ((b"rate limit", b"429", b"quota exceeded", b"resource_exhausted"), "quota-or-rate-limit"),
        ((b"401", b"invalid api key", b"authentication failed", b"api key not valid"), "authentication"),
        ((b"403", b"permission denied", b"permission_denied"), "permission"),
        ((b"model not found", b"unknown model", b"404"), "model-unavailable"),
        ((b"connection error", b"connection failed", b"timeout", b"network unreachable"), "network"),
        ((b"no files", b"no changes", b"no edits", b"did not make"), "no-edit-response"),
    )
    for needles, label in categories:
        if any(needle in raw for needle in needles):
            return label
    return "no-recognised-provider-error"


def count_changes(workspace):
    """Return only a count of dirty paths, never filenames or contents."""
    environment = {"PATH": "/usr/bin:/bin", "HOME": "/home/coder",
                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                   "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
                   "GIT_CONFIG_KEY_0": "core.hooksPath",
                   "GIT_CONFIG_VALUE_0": "/dev/null"}
    result = subprocess.run(
        ["git", "status", "--porcelain", "-z", "--untracked-files=all"],
        cwd=workspace, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, check=False)
    if result.returncode:
        raise JobError("Cannot inspect changed files")
    return sum(1 for part in result.stdout.split(b"\0") if part)

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
    # Raw model/API output is held in an unlinked temporary file, never HA logs.
    with tempfile.TemporaryFile(mode="w+b") as private_output:
        process = subprocess.Popen(
            cmd, cwd=workspace.parent if action == "prepare" else workspace,
            stdin=subprocess.DEVNULL,
            stdout=private_output if action == "edit" else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if action == "edit" else subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
        try:
            return_code = process.wait(timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise JobError("Job timed out") from exc
        if action == "edit":
            private_output.seek(0)
            category = classify_edit_output(private_output.read(200_000))
            changed = count_changes(workspace)
            print(f"Aider diagnostic: {category}; changed paths: {changed}", flush=True)
            if return_code == 0 and changed == 0:
                print("Edit did not produce repository changes", flush=True)
                return 4
    print(f"Job finished with exit code {return_code}", flush=True)
    return return_code


if __name__ == "__main__":
    try:
        sys.exit(run())
    except (JobError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"Job rejected or failed: {type(exc).__name__}", file=sys.stderr)
        sys.exit(2)
