#!/usr/bin/env python3
"""Opt-in, sequential GitHub-approved worker queue. Disabled unless explicitly selected.

Only the maintainer-reviewed manifest on the worker repository's main branch
can authorise tasks. An isolated, credential-free checkout is prepared for
each task. Gemini and GitHub credentials never share a child process.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import publisher
import refresh_repo

MANIFEST_URL = "https://raw.githubusercontent.com/Wood468/Go-aide-worker/main/queue/manifest.json"
HOME = Path("/data/workspace")
STATE = HOME / "queue-state.json"
LOCK = HOME / "queue.lock"
REPO = "Wood468/crispy-barnacle"
POLL_SECONDS = 300
MAX_MANIFEST_BYTES = 24_000
MAX_RECORDS = 500
ID_PATTERN = re.compile(r"[a-z][a-z0-9-]{5,63}\Z")
PATH_PATTERN = re.compile(r"test/[A-Za-z0-9][A-Za-z0-9_.-]{0,145}\.(?:mjs|py)\Z")
EDIT_ENTRY = (
    "import os, direct_editor, job_runner; "
    "direct_editor.edit(job_runner.WORKSPACE, [os.environ['EDIT_FILES']], "
    "os.environ['TASK_PROMPT'], os.environ['GEMINI_API_KEY'])"
)


class QueueError(Exception):
    pass


def validate_manifest(document):
    """Fail closed on unknown schema, unsafe file selections and duplicate IDs."""
    if not isinstance(document, dict) or set(document) != {"schemaVersion", "paused", "tasks"}:
        raise QueueError("Invalid queue manifest")
    if document["schemaVersion"] != 1 or not isinstance(document["paused"], bool):
        raise QueueError("Unknown queue manifest version")
    items = document["tasks"]
    if not isinstance(items, list) or len(items) > 50:
        raise QueueError("Invalid task list")
    seen = set()
    for task in items:
        if not isinstance(task, dict) or set(task) != {"id", "state", "repository", "file", "prompt"}:
            raise QueueError("Task schema rejected")
        ident, path, prompt = task["id"], task["file"], task["prompt"]
        if not isinstance(ident, str) or not ID_PATTERN.fullmatch(ident) or ident in seen:
            raise QueueError("Unsafe or duplicate task ID")
        seen.add(ident)
        if task["state"] not in ("approved", "deferred") or task["repository"] != REPO:
            raise QueueError("Unapproved repository or state")
        if not isinstance(path, str) or not PATH_PATTERN.fullmatch(path) or ".." in path:
            raise QueueError("Selected file outside initial test-only allowlist")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 1800:
            raise QueueError("Invalid prompt")
        if any(marker in prompt.lower() for marker in ("github_pat_", "ghp_", "api_key=", "bearer ")):
            raise QueueError("Credential marker in manifest")
    return document


def fetch_manifest(opener=urllib.request.urlopen):
    req = urllib.request.Request(MANIFEST_URL, headers={
        "Accept": "application/json", "User-Agent": "go-aider-approved-queue"})
    with opener(req, timeout=20) as response:
        if urllib.parse.urlparse(response.geturl()).hostname != "raw.githubusercontent.com":
            raise QueueError("Untrusted manifest redirect")
        raw = response.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise QueueError("Manifest too large")
    try:
        return validate_manifest(json.loads(raw))
    except (ValueError, UnicodeError, TypeError) as exc:
        raise QueueError("Invalid manifest JSON") from None


def state_read(path=STATE):
    if path.is_symlink() or not path.parent.is_dir() or path.parent.is_symlink():
        raise QueueError("Unsafe queue state location")
    if not path.exists():
        return {}
    if not path.is_file() or path.stat().st_size > 100_000:
        raise QueueError("Invalid queue state")
    try:
        result = json.loads(path.read_text("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise QueueError("Corrupt queue state; manual recovery required") from None
    if not isinstance(result, dict) or len(result) > MAX_RECORDS:
        raise QueueError("Invalid queue state shape")
    return result


def state_write(state, path=STATE):
    if len(state) > MAX_RECORDS or path.is_symlink() or path.parent.is_symlink():
        raise QueueError("Unsafe queue state write")
    raw = (json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(raw) > 100_000:
        raise QueueError("Oversized queue state")
    temporary = path.with_name(".queue-state-next")
    if temporary.exists() or temporary.is_symlink():
        raise QueueError("Queue state temporary path occupied")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def next_task(manifest, state):
    if manifest["paused"]:
        return None
    return next((task for task in manifest["tasks"]
                 if task["state"] == "approved" and task["id"] not in state), None)


def safe_env():
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": "/home/coder", "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1", "GIT_TERMINAL_PROMPT": "0"}


def call(argv, env, cwd, seconds=150, executor=subprocess.run):
    result = executor(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                      timeout=seconds, check=False)
    if result.returncode:
        raise QueueError("Isolated worker stage returned nonzero")
    return result.stdout.decode("utf-8", errors="replace")[:500]


def verify_changed_file(task, workspace, checker=publisher.safe_files):
    _, changed = checker(workspace)
    if len(changed) != 1 or changed[0][0] != task["file"]:
        raise QueueError("Unexpected changed files; publication refused")
    return True


def run_task(task, gemini, github, refresh=refresh_repo.refresh, executor=subprocess.run,
             checker=publisher.safe_files):
    # Refreshed repository is a clean public checkout; no credentials are sent to Git.
    refresh()
    work = refresh_repo.SOURCE
    if not work.is_dir() or work.is_symlink():
        raise QueueError("Fresh source checkout absent")
    edit_env = safe_env() | {"GEMINI_API_KEY": gemini, "EDIT_FILES": task["file"],
                             "TASK_PROMPT": task["prompt"]}
    call([sys.executable, "-c", EDIT_ENTRY], edit_env, work, executor=executor)
    verify_changed_file(task, work, checker)
    test_env = safe_env()  # Never expose either key to the generated test.
    if task["file"].endswith(".mjs"):
        command = ["node", "--test", task["file"]]
    else:
        command = [sys.executable, "-c",
                   "import ast,sys;ast.parse(open(sys.argv[1],encoding='utf-8').read())",
                   task["file"]]
    call(command, test_env, work, executor=executor)
    verify_changed_file(task, work, checker)
    publish_env = safe_env() | {"GITHUB_TOKEN": github, "WORKER_QUEUE_TASK_ID": task["id"]}
    response = call([sys.executable, "/opt/worker/publisher.py"], publish_env, work, executor=executor)
    found = re.search(r"Created draft PR #(\d+)", response)
    if not found:
        raise QueueError("Publisher did not confirm a draft PR")
    return int(found.group(1))


def report_failure(task_id, reason, token, sender=publisher.api):
    # Only deterministic non-sensitive text. GitHub Issues permission is optional;
    # the local durable terminal status is always written regardless.
    try:
        sender(token, "POST", "/issues/428/comments", {
            "body": ("Worker queue task " + task_id + " failed safely (" + reason +
                     "). No PR was merged or deployed. Manual review required; no automatic retry.")})
        return True
    except (publisher.PublishError, OSError, KeyError, TypeError):
        return False


def run_once(manifest=None, state_file=STATE, runner=run_task, sender=publisher.api):
    manifest = fetch_manifest() if manifest is None else validate_manifest(manifest)
    state = state_read(state_file)
    task = next_task(manifest, state)
    if task is None:
        return None
    task_id = task["id"]
    state[task_id] = {"status": "running", "manifestSha256":
        hashlib.sha256(json.dumps(task, sort_keys=True).encode()).hexdigest()}
    state_write(state, state_file)  # Claim before any edit: a crash is not auto-retried.
    token = os.environ.get("GITHUB_TOKEN", "")
    try:
        pr = runner(task, os.environ.get("GEMINI_API_KEY", ""), token)
        state[task_id]["status"] = "completed"
        state[task_id]["draftPr"] = pr
        print("Queue task " + task_id + " produced draft PR #" + str(pr), flush=True)
    except (QueueError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        state[task_id]["status"] = "failed"
        state[task_id]["reason"] = type(exc).__name__
        report_failure(task_id, "review-required", token, sender)
        print("Queue task " + task_id + " failed safely; no retry", flush=True)
    state_write(state, state_file)
    return state[task_id]


def serve():
    if not os.environ.get("GEMINI_API_KEY") or not os.environ.get("GITHUB_TOKEN"):
        raise QueueError("Explicit queue credentials missing")
    if LOCK.is_symlink() or not LOCK.parent.is_dir():
        raise QueueError("Unsafe queue lock")
    with LOCK.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise QueueError("Another queue runner already owns the lock") from None
        print("GO Aider approved-task queue started (fail-closed, one at a time)", flush=True)
        while True:
            try:
                run_once()
            except (QueueError, OSError, ValueError) as exc:
                print("Queue poll rejected; no work performed (" + type(exc).__name__ + ")", flush=True)
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        serve()
    except (QueueError, OSError) as exc:
        print("Queue startup refused: " + type(exc).__name__, flush=True)
        raise SystemExit(2)
