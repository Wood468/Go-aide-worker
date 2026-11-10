#!/usr/bin/env python3
"""Single-job, local-only runner with allowlisted public Git preparation."""
import json
import hashlib
import signal
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile

WORKSPACE = Path("/data/workspace/source")
JOB_FILE = Path("/data/job.json")
MAX_JOB_BYTES = 8192
MAX_PROMPT_CHARS = 3000
TIMEOUT_SECONDS = 300
ALLOWED_ACTIONS = {"inspect", "test", "edit", "prepare", "verify", "publish", "publish_test", "probe", "direct_edit"}
MODEL = "gemini/gemini-3.5-flash-lite"


class JobError(Exception):
    pass


def selected_edit_files(workspace):
    raw = os.environ.get("EDIT_FILES", "")
    if len(raw) > 500:
        raise JobError("Invalid selected files")
    paths = [x.strip() for x in raw.split(",") if x.strip()]
    if len(paths) > 5 or len(set(paths)) != len(paths):
        raise JobError("Invalid selected files")
    for relative in paths:
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,180}", relative)
                or any(part.startswith(".") or part in {"", ".", ".."}
                       for part in relative.split("/"))
                or any("token" in part.lower() or "secret" in part.lower()
                       for part in relative.split("/"))):
            raise JobError("Unsafe selected edit path")
        current = workspace
        for part in relative.split("/")[:-1]:
            current = current / part
            if current.is_symlink():
                raise JobError("Symlinked edit parent not allowed")
        target = workspace / relative
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise JobError("Unsafe edit target")
    return paths


def initialize_selected_files(workspace, files):
    """Create only explicitly selected new files so Aider can edit them."""
    for relative in files:
        target = workspace / relative
        if target.is_symlink():
            raise JobError("Symlink edit target forbidden")
        if not target.exists():
            if not target.parent.is_dir() or target.parent.is_symlink():
                raise JobError("Selected edit parent directory must already exist")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(target, flags, 0o600)
            os.close(fd)


def selected_hashes(workspace, files):
    """Digest intended targets, never log their contents."""
    digests = {}
    for relative in files:
        path = workspace / relative
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
            raise JobError("Unsafe or oversized selected file")
        with path.open("rb") as stream:
            digests[relative] = hashlib.file_digest(stream, "sha256").digest()
    return digests


def validate_job(raw, workspace=WORKSPACE):
    if not isinstance(raw, dict) or set(raw) != {"action", "prompt"}:
        raise JobError("Job must contain only action and prompt")
    action, prompt = raw["action"], raw["prompt"]
    if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
        raise JobError("Unsupported action")
    if not isinstance(prompt, str) or len(prompt) > MAX_PROMPT_CHARS:
        raise JobError("Invalid prompt length")
    if action in {"edit", "direct_edit"} and not prompt.strip():
        raise JobError("Edit prompt must not be empty")
    if action in {"edit", "direct_edit"} and os.environ.get("ENABLE_EDITS") != "true":
        raise JobError("Edits disabled; ENABLE_EDITS must be true")
    if action in {"edit", "direct_edit"} and not os.environ.get("GEMINI_API_KEY"):
        raise JobError("Gemini key not configured")
    if action == "probe" and not os.environ.get("GEMINI_API_KEY"):
        raise JobError("Gemini API key not configured for probe")
    if action in {"publish", "publish_test"} and (os.environ.get("ENABLE_PUBLISHING") != "true" or not os.environ.get("GITHUB_TOKEN")):
        raise JobError("Publishing disabled or token missing")
    if action in {"inspect", "test", "verify", "publish", "publish_test", "probe"} and prompt.strip():
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
    if action in {"edit", "direct_edit"}:
        selected_edit_files(workspace)
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
    if action == "publish_test":
        return [sys.executable, "/opt/worker/publisher.py", "--smoke"]
    if action == "probe":
        return [sys.executable, "/opt/worker/gemini_probe.py"]
    if action == "direct_edit":
        return [sys.executable, "/opt/worker/direct_editor.py"]
    files = selected_edit_files(WORKSPACE)
    file_args = [arg for file in files for arg in ("--file", file)]
    return [
        "aider", "--model", MODEL, "--yes", "--no-auto-commits",
        "--no-gitignore", "--no-show-model-warnings",
        *file_args, "--message", prompt,
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
        if target.is_symlink() or not target.is_file():
            print("Gemini smoke file absent", flush=True)
            return 3
        if target.read_bytes().rstrip(b"\r\n") != expected.rstrip(b"\n"):
            print("Gemini smoke file exists but content differs", flush=True)
            return 3
        print("Gemini smoke file verified", flush=True)
        return 0
    files = selected_edit_files(workspace) if action == "edit" else []
    if files:
        initialize_selected_files(workspace, files)
        before = selected_hashes(workspace, files)
    else:
        before = {}
    cmd = command_for(action, prompt)
    # Never log prompts, credentials, or command arguments.
    print(f"Starting bounded local job: {action}", flush=True)
    env = dict(os.environ) if action in {"edit", "direct_edit", "publish", "publish_test"} else {
        key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "PYTHONDONTWRITEBYTECODE")
        if key in os.environ
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    if action == "prepare":
        env["ALLOWED_PUBLIC_REPOSITORIES"] = os.environ["ALLOWED_PUBLIC_REPOSITORIES"]
    if action in {"publish", "publish_test"}:
        env.pop("GEMINI_API_KEY", None)
        env.pop("ENABLE_EDITS", None)
    if action == "probe":
        env["GEMINI_API_KEY"] = os.environ["GEMINI_API_KEY"]
    # Raw model/API output is held in an unlinked temporary file, never HA logs.
    with tempfile.TemporaryFile(mode="w+b") as private_output:
        process = subprocess.Popen(
            cmd, cwd=workspace.parent if action == "prepare" else workspace,
            stdin=subprocess.DEVNULL,
            stdout=private_output if action in {"edit", "direct_edit"} else subprocess.DEVNULL,
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
            target_changes = sum(1 for key, digest in before.items()
                                 if selected_hashes(workspace, files)[key] != digest) if files else changed
            print(f"Selected edit targets changed: {target_changes}", flush=True)
            if return_code == 0 and target_changes == 0:
                print("Edit did not change intended repository files", flush=True)
                return 4
    if action == "probe":
        labels = {0: "success", 11: "authentication-or-permission", 12: "quota",
                  13: "model-or-request", 14: "network-or-service", 15: "unexpected-model-response"}
        print("Gemini API probe: " + labels.get(return_code, "unknown-error"), flush=True)
    print(f"Job finished with exit code {return_code}", flush=True)
    return return_code


if __name__ == "__main__":
    try:
        sys.exit(run())
    except (JobError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"Job rejected or failed: {type(exc).__name__}", file=sys.stderr)
        sys.exit(2)
