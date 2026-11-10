#!/usr/bin/env python3
"""GitHub-approved one-file queue. No arbitrary scripts, merges or deployments."""
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = "Wood468/crispy-barnacle"
ISSUES_URL = ("https://api.github.com/repos/" + REPO
              + "/issues?state=open&labels=go-worker-approved&per_page=40")
ROOT = Path("/data/workspace")
CHECKOUT = ROOT / "source"
LEDGER = ROOT / "go-queue-ledger.json"
LOCK = ROOT / ".go-queue.lock"
JOB_FILE = Path("/data/job.json")
PATH_RE = re.compile(r"docs/worker-task-[a-z0-9-]{1,50}\.md")
TASK_RE = re.compile(r"^GO_WORKER_TASK_JSON:(\{[^\n]{1,2400}\})$", re.MULTILINE)


class QueueError(Exception):
    pass


def github_read(url, opener=urllib.request.urlopen):
    if (not url.startswith("https://api.github.com/repos/" + REPO + "/")
            or any(ch in url for ch in ("\r", "\n", "#"))):
        raise QueueError("Disallowed GitHub read")
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "go-aider-task-queue"})
    try:
        with opener(req, timeout=25) as response:
            raw = response.read(180_001)
        if len(raw) > 180_000:
            raise QueueError("Queue response exceeds limit")
        return json.loads(raw)
    except (urllib.error.HTTPError, urllib.error.URLError,
            OSError, TimeoutError, ValueError):
        raise QueueError("Queue request unavailable") from None


def parse_task(issue):
    if not isinstance(issue, dict) or "pull_request" in issue:
        return None
    author = issue.get("user") or {}
    if (issue.get("state") != "open" or author.get("login", "").lower() != "wood468"
            or not isinstance(issue.get("number"), int)
            or not str(issue.get("title", "")).startswith("[GO-WORKER] ")):
        return None
    if "go-worker-approved" not in {
            label.get("name") for label in issue.get("labels", []) if isinstance(label, dict)}:
        return None
    body = issue.get("body")
    if not isinstance(body, str) or len(body) > 5000:
        return None
    parts = TASK_RE.findall(body)
    if len(parts) != 1:
        return None
    try:
        obj = json.loads(parts[0])
    except (TypeError, ValueError):
        return None
    if not isinstance(obj, dict) or set(obj) != {"path", "instruction"}:
        return None
    path, instruction = obj["path"], obj["instruction"]
    if (not isinstance(path, str) or not PATH_RE.fullmatch(path)
            or not isinstance(instruction, str)
            or not 20 <= len(instruction.strip()) <= 1500):
        return None
    return {"issue": issue["number"], "path": path, "instruction": instruction.strip()}


def candidates(fetch=github_read):
    raw = fetch(ISSUES_URL)
    if not isinstance(raw, list):
        raise QueueError("Invalid issue list")
    return sorted((t for item in raw if (t := parse_task(item)) is not None),
                  key=lambda item: item["issue"])


def read_ledger(path=LEDGER):
    if path.is_symlink():
        raise QueueError("Unsafe ledger link")
    if not path.exists():
        return {}
    if not path.is_file() or path.stat().st_size > 100_000:
        raise QueueError("Unsafe queue ledger")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise QueueError("Invalid ledger object")
    return data


def save_ledger(state, path=LEDGER):
    if path.is_symlink() or path.parent.is_symlink():
        raise QueueError("Unsafe queue ledger destination")
    raw = json.dumps(state, sort_keys=True).encode()
    if len(raw) > 100_000:
        raise QueueError("Queue ledger full")
    temp = path.with_name(".queue-ledger-next.json")
    if temp.exists() or temp.is_symlink():
        raise QueueError("Queue ledger update collision")
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def child_env(**keys):
    return dict({"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/coder",
                 "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
                 "GIT_TERMINAL_PROMPT": "0"}, **keys)


def child_run(command, env):
    try:
        result = subprocess.run(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=360, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise QueueError("Worker child did not finish") from None
    if result.returncode:
        raise QueueError("Worker child failed")
    return result.stdout[:500]


def has_pending_review(state, fetch=github_read):
    for record in state.values():
        if not isinstance(record, dict):
            return True
        if record.get("state") in {"started", "needs-review"}:
            return True
        if record.get("state") == "submitted":
            n = record.get("pr")
            if not isinstance(n, int):
                return True
            pr = fetch("https://api.github.com/repos/" + REPO + "/pulls/" + str(n))
            if not isinstance(pr, dict) or pr.get("state") != "closed":
                return True
    return False


def perform(task, state, fetch=github_read, runner=child_run):
    key = str(task["issue"])
    if key in state or has_pending_review(state, fetch):
        return False
    gemini = os.environ.get("GEMINI_API_KEY")
    github = os.environ.get("GITHUB_TOKEN")
    if not gemini or not github:
        raise QueueError("Missing queue credentials")
    state[key] = {"state": "started"}
    save_ledger(state)
    try:
        print("Queue phase: refresh", flush=True)
        runner([sys.executable, "/opt/worker/refresh_repo.py"], child_env())
        target = CHECKOUT / task["path"]
        if target.exists() or target.is_symlink() or not target.parent.is_dir():
            raise QueueError("Task must create a new documentation file")
        # Store only the editing instruction, never a credential.
        job = JOB_FILE
        if not job.is_file() or job.is_symlink():
            raise QueueError("Invalid worker job file")
        temp = job.with_name(".queue-job-next.json")
        if temp.exists() or temp.is_symlink():
            raise QueueError("Queue job file collision")
        temp.write_text(json.dumps({"action": "direct_edit", "prompt": task["instruction"]}))
        os.replace(temp, job)
        print("Queue phase: edit", flush=True)
        runner([sys.executable, "/opt/worker/direct_editor.py"],
               child_env(GEMINI_API_KEY=gemini, EDIT_FILES=task["path"]))
        import publisher
        _head, changed = publisher.safe_files(CHECKOUT)
        if [name for name, _data in changed] != [task["path"]]:
            raise QueueError("Changes outside approved file detected")
        print("Queue phase: publish", flush=True)
        out = runner([sys.executable, "/opt/worker/publisher.py"],
                     child_env(GITHUB_TOKEN=github, ENABLE_PUBLISHING="true", WORKER_ISSUE_NUMBER=key))
        matched = re.search(rb"Created draft PR #(\d+)", out)
        if matched is None:
            raise QueueError("No verified PR number; inspect GitHub before retry")
        number = int(matched.group(1))
        state[key] = {"state": "submitted", "pr": number}
        save_ledger(state)
        print("Submitted issue #" + key + " as draft PR #" + str(number), flush=True)
        return True
    except Exception:
        print("Queue halted: inspect issue #" + key + " before any retry", flush=True)
        state[key] = {"state": "needs-review"}
        save_ledger(state)
        raise


def once(scan=False, fetch=github_read, runner=child_run, ledger_path=LEDGER):
    tasks = candidates(fetch)
    state = read_ledger(ledger_path)
    if scan:
        waiting = sum(str(t["issue"]) not in state for t in tasks)
        print("Approved queued tasks: " + str(waiting) + "; no edits", flush=True)
        return waiting
    if has_pending_review(state, fetch):
        print("Queue paused for review of previous task", flush=True)
        return 0
    for task in tasks:
        if str(task["issue"]) not in state:
            return int(perform(task, state, fetch, runner))
    print("No approved unprocessed tasks", flush=True)
    return 0


def reset_reviewed_issue(issue_id):
    if not re.fullmatch(r"[1-9][0-9]{0,9}", issue_id):
        raise QueueError("Invalid recovery issue ID")
    state = read_ledger()
    entry = state.get(issue_id)
    if not isinstance(entry, dict) or entry.get("state") != "needs-review":
        raise QueueError("Only a reviewed failed task can be reset")
    # This action must be invoked by the operator after checking GitHub.
    del state[issue_id]
    save_ledger(state)
    print("Reviewed failed task #" + issue_id + " is eligible again", flush=True)


def main():
    mode = sys.argv[1] if len(sys.argv) >= 2 else None
    if mode not in ("scan", "once", "loop", "reset"):
        raise QueueError("Invalid queue mode")
    if mode == "reset" and len(sys.argv) != 3:
        raise QueueError("Recovery requires issue number")
    if mode != "reset" and len(sys.argv) != 2:
        raise QueueError("Invalid queue arguments")
    with LOCK.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise QueueError("Another queue worker holds the lock") from None
        if mode == "reset":
            reset_reviewed_issue(sys.argv[2])
        elif mode == "loop":
            print("Approved queue active, one draft PR at a time", flush=True)
            while True:
                try:
                    once()
                except (QueueError, OSError, ValueError):
                    print("Queue iteration failed; task held for review", flush=True)
                time.sleep(600)
        else:
            once(scan=(mode == "scan"))


if __name__ == "__main__":
    try:
        main()
    except (QueueError, OSError, ValueError):
        print("Worker queue rejected job safely", file=sys.stderr)
        sys.exit(2)
