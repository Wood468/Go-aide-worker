#!/usr/bin/env python3
"""GitHub-approved bounded task queue. No arbitrary scripts, merges or deployments."""
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
PATH_RE = re.compile(r"(?:docs/worker-task-[a-z0-9-]{1,50}\.md|test/[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\.test\.mjs)")
TASK_RE = re.compile(r"^GO_WORKER_TASK_JSON:(\{[^\n]{1,2400}\})$", re.MULTILINE)
# Reviewed source-to-test pairs; no arbitrary source grant.
SOURCE_TEST_PAIRS = {
    ("public/event-components.js", "test/event-components-crowned-alias.test.mjs"),
    ("public/today.js", "test/today-logic.test.mjs"),
    ("public/detail.js", "test/event-detail-source-confidence.test.mjs"),
    ("public/app.js", "test/universal-event.test.mjs"),
}


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
    if not isinstance(obj, dict):
        return None
    instruction = obj.get("instruction")
    if not isinstance(instruction, str) or not 20 <= len(instruction.strip()) <= 1500:
        return None
    if set(obj) == {"path", "instruction"}:
        path = obj["path"]
        if not isinstance(path, str) or not PATH_RE.fullmatch(path):
            return None
        return {"issue": issue["number"], "path": path, "instruction": instruction.strip()}
    if set(obj) == {"version", "paths", "instruction"} and type(obj["version"]) is int and obj["version"] == 2:
        paths = obj["paths"]
        if (not isinstance(paths, list) or len(paths) != 2
                or not all(isinstance(p, str) for p in paths)
                or tuple(paths) not in SOURCE_TEST_PAIRS):
            return None
        return {"issue": issue["number"], "version": 2,
                "paths": list(paths), "instruction": instruction.strip()}
    return None


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



# Recovery is deliberately limited to the existing, reviewed, documentation-only
# acceptance pilot. No general-purpose ledger reset or automatic retry exists.
PILOT_ISSUE = 462
PILOT_PATH = "docs/worker-task-queue-pilot.md"
RECOVERABLE_PILOTS = {462: PILOT_PATH, 465: "test/attendance-classification.test.mjs",
                      472: "test/variant-art.test.mjs"}


def ledger_summary(path=LEDGER):
    state = read_ledger(path)
    for key, entry in sorted(state.items()):
        status = entry.get("state", "invalid") if isinstance(entry, dict) else "invalid"
        if status not in {"started", "needs-review", "submitted", "approved-pilot-retry", "retired"}:
            status = "invalid"
        print("Worker queue #" + str(key) + ": " + status, flush=True)
    if not state:
        print("Worker queue ledger empty; no tasks claimed", flush=True)
    return state


def approved_for_dispatch(task, state):
    key = str(task["issue"])
    if key not in state:
        return True
    previous = state.get(key)
    return (task["issue"] in RECOVERABLE_PILOTS and isinstance(previous, dict)
            and previous.get("state") == "approved-pilot-retry"
            and previous.get("attempts") == 1)


def recover_pilot(path=LEDGER, fetch=github_read, pilot_issue=PILOT_ISSUE):
    if pilot_issue not in RECOVERABLE_PILOTS:
        raise QueueError("Recovery is not approved for this task")
    state = read_ledger(path)
    key = str(pilot_issue)
    previous = state.get(key)
    if (not isinstance(previous, dict) or
            previous.get("state") not in {"started", "needs-review"} or
            previous.get("attempts", 1) != 1 or "pr" in previous):
        raise QueueError("Pilot recovery not eligible")
    source_issue = fetch("https://api.github.com/repos/" + REPO + "/issues/" + key)
    issue = parse_task(source_issue)
    if not issue or issue["issue"] != pilot_issue or issue["path"] != RECOVERABLE_PILOTS[pilot_issue]:
        raise QueueError("Pilot approval changed")
    # Only PRs *created after* this approved issue could have come from it.
    # Check newest first, in five-PR pages small enough for the worker's
    # existing 180 KB untrusted-response limit. Do not relax that cap.
    issued = source_issue.get("created_at") if isinstance(source_issue, dict) else None
    iso_utc = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
    if not isinstance(issued, str) or not re.fullmatch(iso_utc, issued):
        raise QueueError("Pilot issue creation time unavailable")
    verified_cutoff = False
    for page in range(1, 26):
        prs = fetch("https://api.github.com/repos/" + REPO +
                    "/pulls?state=all&sort=created&direction=desc&per_page=5&page=" + str(page))
        if not isinstance(prs, list) or len(prs) > 5:
            raise QueueError("Invalid PR history page")
        for pr in prs:
            if not isinstance(pr, dict):
                raise QueueError("Invalid PR history")
            created = pr.get("created_at")
            if not isinstance(created, str) or not re.fullmatch(iso_utc, created):
                raise QueueError("PR creation time unavailable")
            if created < issued:
                verified_cutoff = True
                break
            title, body = str(pr.get("title") or ""), str(pr.get("body") or "")
            if ("Worker: issue #" + key) in title or ("Source task: #" + key) in body:
                raise QueueError("Pilot may already have a published draft PR")
        if verified_cutoff or len(prs) < 5:
            verified_cutoff = True
            break
    if not verified_cutoff:
        raise QueueError("Cannot verify complete post-approval PR history")
    state[key] = {"state": "approved-pilot-retry", "attempts": 1}
    save_ledger(state, path)
    print("Reviewed pilot #" + key + " eligible for one final controlled retry", flush=True)
    return True



def acknowledge_merged_code_pilot(path=LEDGER, fetch=github_read):
    """One-off reconciliation only: the already-merged, reviewed issue #465 PR #467.

    This never retries an edit, authorises a new task, merges or writes GitHub.
    """
    state = read_ledger(path)
    previous = state.get("465")
    if (not isinstance(previous, dict) or previous.get("state") != "needs-review"
            or previous.get("attempts") != 2 or "pr" in previous):
        raise QueueError("Exact code pilot acknowledgement not eligible")
    issue = fetch("https://api.github.com/repos/" + REPO + "/issues/465")
    pr = fetch("https://api.github.com/repos/" + REPO + "/pulls/467")
    if not isinstance(issue, dict) or not isinstance(pr, dict):
        raise QueueError("Reviewed issue or merged PR unavailable")
    task_line = TASK_RE.findall(issue.get("body", "") or "")
    try:
        task = json.loads(task_line[0]) if len(task_line) == 1 else {}
    except (TypeError, ValueError):
        task = {}
    if (issue.get("number") != 465 or issue.get("state") != "closed"
            or (issue.get("user") or {}).get("login", "").lower() != "wood468"
            or not str(issue.get("title", "")).startswith("[GO-WORKER] ")
            or task.get("path") != "test/attendance-classification.test.mjs"):
        raise QueueError("Exact approved task identity not verified")
    base, head = pr.get("base") or {}, pr.get("head") or {}
    merged_sha = pr.get("merge_commit_sha", "")
    if (pr.get("number") != 467 or pr.get("state") != "closed"
            or pr.get("merged") is not True or not pr.get("merged_at")
            or (pr.get("merged_by") or {}).get("login", "").lower() != "wood468"
            or pr.get("title") != "Worker: issue #465 draft changes"
            or "Source task: #465" not in (pr.get("body") or "")
            or base.get("ref") != "main"
            or not str(head.get("ref", "")).startswith("worker/review-")
            or not isinstance(merged_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", merged_sha)):
        raise QueueError("Exact merged review PR not verified")
    state["465"] = {"state": "submitted", "pr": 467, "attempts": 2}
    save_ledger(state, path)
    print("Verified merged worker issue #465 and PR #467 acknowledged; no retry", flush=True)
    return True



def retire_exhausted_artwork_task(path=LEDGER, fetch=github_read):
    """One-way review acknowledgement of exhausted #472, never another retry.

    Require the recorded second failed attempt, withdrawn approval, reviewed
    merged fallback PR #488, and complete post-issue worker-PR history.
    Leave every other task record unchanged. No Gemini or GitHub writes.
    """
    state = read_ledger(path)
    previous = state.get("472")
    if (not isinstance(previous, dict) or previous.get("state") != "needs-review"
            or previous.get("attempts") != 2 or "pr" in previous):
        raise QueueError("Exhausted artwork acknowledgement not eligible")
    issue = fetch("https://api.github.com/repos/" + REPO + "/issues/472")
    if not isinstance(issue, dict):
        raise QueueError("Artwork issue unavailable")
    task_line = TASK_RE.findall(issue.get("body", "") or "")
    try:
        task = json.loads(task_line[0]) if len(task_line) == 1 else {}
    except (TypeError, ValueError):
        task = {}
    labels = issue.get("labels")
    if (issue.get("number") != 472 or issue.get("state") != "open"
            or (issue.get("user") or {}).get("login", "").lower() != "wood468"
            or not str(issue.get("title", "")).startswith("[GO-WORKER] ")
            or task.get("path") != "test/variant-art.test.mjs"
            or not isinstance(labels, list)
            or any(not isinstance(label, dict) or not isinstance(label.get("name"), str)
                   for label in labels)
            or any(label["name"] == "go-worker-approved" for label in labels)):
        raise QueueError("Artwork issue or approval state changed")

    pr = fetch("https://api.github.com/repos/" + REPO + "/pulls/488")
    if not isinstance(pr, dict):
        raise QueueError("Fallback PR unavailable")
    merged_sha = pr.get("merge_commit_sha")
    if (pr.get("number") != 488 or pr.get("state") != "closed"
            or pr.get("merged") is not True or not pr.get("merged_at")
            or (pr.get("merged_by") or {}).get("login", "").lower() != "wood468"
            or "Crowned and Roaming" not in str(pr.get("title", ""))
            or "#472" not in str(pr.get("body", ""))
            or (pr.get("base") or {}).get("ref") != "main"
            or not isinstance(merged_sha, str)
            or not re.fullmatch(r"[0-9a-f]{40}", merged_sha)):
        raise QueueError("Fallback PR has not been verified")
    files = fetch("https://api.github.com/repos/" + REPO +
                  "/pulls/488/files?per_page=100")
    if (not isinstance(files, list) or len(files) != 1
            or not isinstance(files[0], dict)
            or files[0].get("filename") != "test/variant-art.test.mjs"
            or files[0].get("status") != "modified"):
        raise QueueError("Fallback PR file scope changed")

    issued = issue.get("created_at")
    iso_utc = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
    if not isinstance(issued, str) or not re.fullmatch(iso_utc, issued):
        raise QueueError("Artwork issue creation time unavailable")
    complete = False
    for page in range(1, 26):
        prs = fetch("https://api.github.com/repos/" + REPO +
                    "/pulls?state=all&sort=created&direction=desc&per_page=5&page=" +
                    str(page))
        if not isinstance(prs, list) or len(prs) > 5:
            raise QueueError("Incomplete artwork PR history")
        for candidate in prs:
            if not isinstance(candidate, dict):
                raise QueueError("Malformed artwork PR history")
            created = candidate.get("created_at")
            if not isinstance(created, str) or not re.fullmatch(iso_utc, created):
                raise QueueError("Artwork PR creation time unavailable")
            if created < issued:
                complete = True
                break
            if ("Worker: issue #472" in str(candidate.get("title") or "")
                    or "Source task: #472" in str(candidate.get("body") or "")):
                raise QueueError("An artwork worker PR may already exist")
        if complete or len(prs) < 5:
            complete = True
            break
    if not complete:
        raise QueueError("Cannot verify full artwork PR history")

    state["472"] = {"state": "retired", "attempts": 2, "resolution_pr": 488}
    save_ledger(state, path)
    print("Exhausted issue #472 retired after verified fallback PR #488; no retry", flush=True)
    return True



def retire_failed_two_file_pilot(path=LEDGER, fetch=github_read):
    """Close out exact failed synthetic issue #497; never retry or publish."""
    state = read_ledger(path)
    previous = state.get("497")
    if (not isinstance(previous, dict) or previous.get("state") != "needs-review"
            or previous.get("attempts") != 1 or "pr" in previous):
        raise QueueError("Exact two-file pilot retirement not eligible")
    issue = fetch("https://api.github.com/repos/" + REPO + "/issues/497")
    if not isinstance(issue, dict):
        raise QueueError("Two-file pilot issue unavailable")
    body = issue.get("body")
    parts = TASK_RE.findall(body) if isinstance(body, str) else []
    try:
        task = json.loads(parts[0]) if len(parts) == 1 else {}
    except (TypeError, ValueError):
        task = {}
    labels = issue.get("labels")
    expected = ["public/today.js", "test/today-logic.test.mjs"]
    if (issue.get("number") != 497 or issue.get("state") != "open"
            or (issue.get("user") or {}).get("login", "").lower() != "wood468"
            or issue.get("title") != "[GO-WORKER] v0.5.8 harmless source-plus-test draft PR pilot"
            or task.get("version") != 2 or task.get("paths") != expected
            or not isinstance(task.get("instruction"), str)
            or not isinstance(labels, list)
            or any(not isinstance(label, dict) or not isinstance(label.get("name"), str)
                   for label in labels)
            or any(label["name"] == "go-worker-approved" for label in labels)):
        raise QueueError("Exact two-file pilot identity or approval changed")
    issued = issue.get("created_at")
    iso_utc = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
    if not isinstance(issued, str) or not re.fullmatch(iso_utc, issued):
        raise QueueError("Pilot issue creation time unavailable")
    verified_cutoff = False
    for page in range(1, 26):
        prs = fetch("https://api.github.com/repos/" + REPO +
                    "/pulls?state=all&sort=created&direction=desc&per_page=5&page=" +
                    str(page))
        if not isinstance(prs, list) or len(prs) > 5:
            raise QueueError("Invalid pilot PR history")
        for pr in prs:
            if not isinstance(pr, dict):
                raise QueueError("Malformed pilot PR history")
            created = pr.get("created_at")
            if not isinstance(created, str) or not re.fullmatch(iso_utc, created):
                raise QueueError("Pilot PR creation time unavailable")
            if created < issued:
                verified_cutoff = True
                break
            if ("Worker: issue #497" in str(pr.get("title") or "")
                    or "Source task: #497" in str(pr.get("body") or "")):
                raise QueueError("Two-file pilot may already have a published PR")
        if verified_cutoff or len(prs) < 5:
            verified_cutoff = True
            break
    if not verified_cutoff:
        raise QueueError("Cannot verify complete two-file pilot PR history")
    state["497"] = {"state": "retired", "attempts": 1, "reason": "discarded-pilot"}
    save_ledger(state, path)
    print("Failed two-file pilot #497 retired without retry or PR", flush=True)
    return True



def perform(task, state, fetch=github_read, runner=child_run):
    key = str(task["issue"])
    if not approved_for_dispatch(task, state) or has_pending_review(state, fetch):
        return False
    gemini = os.environ.get("GEMINI_API_KEY")
    github = os.environ.get("GITHUB_TOKEN")
    if not gemini or not github:
        raise QueueError("Missing queue credentials")
    prior = state.get(key, {})
    attempts = 2 if isinstance(prior, dict) and prior.get("state") == "approved-pilot-retry" else 1
    state[key] = {"state": "started", "attempts": attempts}
    save_ledger(state)
    try:
        runner([sys.executable, "/opt/worker/refresh_repo.py"], child_env())
        multi = task.get("version") == 2
        paths = task["paths"] if multi else [task["path"]]
        if multi and tuple(paths) not in SOURCE_TEST_PAIRS:
            raise QueueError("Disallowed source/test contract")
        for relative in paths:
            target = CHECKOUT / relative
            if (not target.parent.is_dir() or target.parent.is_symlink()
                    or target.is_symlink()):
                raise QueueError("Unsafe task target")
            if multi:
                if not target.is_file():
                    raise QueueError("Both source/test files must already exist")
                import publisher
                publisher.local_git(CHECKOUT, "ls-files", "--error-unmatch", "--", relative)
            elif relative.startswith("test/"):
                if not target.is_file():
                    raise QueueError("Test task must edit exactly one existing file")
            elif target.exists():
                raise QueueError("Documentation task must create exactly one new file")
        if multi and parse_task(fetch("https://api.github.com/repos/" + REPO +
                                       "/issues/" + key)) != task:
            raise QueueError("Multi-file approval changed")
        # Store only the editing instruction, never a credential.
        job = ROOT / "job.json"
        if job.is_symlink() or (job.exists() and not job.is_file()):
            raise QueueError("Invalid worker job file")
        temp = ROOT / ".queue-job-next.json"
        if temp.exists() or temp.is_symlink():
            raise QueueError("Queue job file collision")
        # Atomic, owner-only queue job: never reuse the root-staged manual
        # /data/job.json. Direct Edit reads this path only for queue children.
        payload = json.dumps({"action": "direct_edit", "prompt": task["instruction"]}).encode()
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, job)
        finally:
            if temp.exists():
                temp.unlink()
        for relative in paths:
            runner([sys.executable, "/opt/worker/direct_editor.py"],
                   child_env(GEMINI_API_KEY=gemini, EDIT_FILES=relative,
                             GO_WORKER_QUEUE_EDIT="1"))
        # Parse edited JS only. Never execute model-produced tests in HA.
        for relative in paths:
            if multi or relative.startswith("test/"):
                runner(["node", "--check", str(CHECKOUT / relative)], child_env())
        import publisher
        _head, changed = publisher.safe_files(CHECKOUT)
        if [name for name, _data in changed] != sorted(paths):
            raise QueueError("Missing or extra modified task paths")
        if multi and parse_task(fetch("https://api.github.com/repos/" + REPO +
                                       "/issues/" + key)) != task:
            raise QueueError("Multi-file approval revoked before publication")
        publication_env = child_env(GITHUB_TOKEN=github, ENABLE_PUBLISHING="true",
                                    WORKER_ISSUE_NUMBER=key)
        if multi:
            publication_env["WORKER_APPROVED_PATHS"] = json.dumps(paths)
        out = runner([sys.executable, "/opt/worker/publisher.py"], publication_env)
        matched = re.search(rb"Created draft PR #(\d+)", out)
        if matched is None:
            raise QueueError("No verified PR number; inspect GitHub before retry")
        number = int(matched.group(1))
        state[key] = {"state": "submitted", "pr": number, "attempts": attempts}
        save_ledger(state)
        print("Submitted issue #" + key + " as draft PR #" + str(number), flush=True)
        return True
    except Exception:
        state[key] = {"state": "needs-review", "attempts": attempts}
        save_ledger(state)
        raise


def once(scan=False, fetch=github_read, runner=child_run, ledger_path=LEDGER):
    tasks = candidates(fetch)
    state = read_ledger(ledger_path)
    if scan:
        waiting = sum(approved_for_dispatch(t, state) for t in tasks)
        print("Approved queued tasks: " + str(waiting) + "; no edits", flush=True)
        return waiting
    if has_pending_review(state, fetch):
        print("Queue paused for review of previous task", flush=True)
        return 0
    for task in tasks:
        if approved_for_dispatch(task, state):
            return int(perform(task, state, fetch, runner))
    print("No approved unprocessed tasks", flush=True)
    return 0


def main():
    mode = sys.argv[1] if len(sys.argv) == 2 else None
    if mode not in ("scan", "once", "loop", "status", "recover_pilot", "recover_code_pilot", "recover_artwork_task", "ack_code_pilot", "retire_artwork_task", "retire_two_file_pilot"):
        raise QueueError("Invalid queue mode")
    with LOCK.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise QueueError("Another queue worker holds the lock") from None
        if mode == "loop":
            print("Approved queue active, one draft PR at a time", flush=True)
            while True:
                try:
                    once()
                except (QueueError, OSError, ValueError):
                    print("Queue iteration failed; task held for review", flush=True)
                time.sleep(600)
        elif mode == "status":
            ledger_summary()
        elif mode == "recover_pilot":
            recover_pilot()
        elif mode == "recover_code_pilot":
            recover_pilot(pilot_issue=465)
        elif mode == "recover_artwork_task":
            recover_pilot(pilot_issue=472)
        elif mode == "ack_code_pilot":
            acknowledge_merged_code_pilot()
        elif mode == "retire_artwork_task":
            retire_exhausted_artwork_task()
        elif mode == "retire_two_file_pilot":
            retire_failed_two_file_pilot()
        else:
            once(scan=(mode == "scan"))


if __name__ == "__main__":
    try:
        main()
    except (QueueError, OSError, ValueError):
        print("Worker queue rejected job safely", file=sys.stderr)
        sys.exit(2)
