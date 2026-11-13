#!/usr/bin/env python3
"""GitHub-approved bounded task queue. No arbitrary scripts, merges or deployments."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import signal
import threading
import queue_scope
from queue_scope import SOURCE_TEST_PAIRS
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


class QueueError(Exception):
    pass

# These are pre-approved diagnostic labels, not exception or model output.
# The queue never logs its GitHub payloads, paths, prompts or credentials.
PRECLAIM_ERRORS = {
    "GitHub read rate limit reached": "github-rate-limit",
    "GitHub authentication required": "github-auth",
    "GitHub read unauthorized": "github-auth",
    "Queue request unavailable": "github-unavailable",
    "Queue response exceeds limit": "github-response-size",
    "Invalid issue list": "issue-list-invalid",
    "Invalid ledger object": "ledger-invalid",
    "Unsafe ledger link": "ledger-invalid",
    "Unsafe queue ledger": "ledger-invalid",
    "Another queue worker holds the lock": "queue-locked",
    "Disallowed GitHub read": "github-url-rejected",
}
SAFE_PRECLAIM_CATEGORIES = frozenset(PRECLAIM_ERRORS.values()) | {"other"}


def safe_preclaim_category(exc):
    if not isinstance(exc, QueueError):
        return "other"
    return PRECLAIM_ERRORS.get(str(exc), "other")


# Only controlled direct-editor messages can become status categories. Never
# copy raw model responses, subprocess output, exception details or credentials.
EDITOR_FAILURES = {
    b"Direct Gemini edit rejected: Gemini request rejected: quota": "quota",
    b"Direct Gemini edit rejected: Gemini request rejected: auth": "auth",
    b"Direct Gemini edit rejected: Gemini request rejected: model": "model",
    b"Direct Gemini edit rejected: Gemini request rejected: service": "service",
    b"Direct Gemini edit rejected: Gemini connection or response error": "network",
    b"Direct Gemini edit rejected: Gemini request failed": "service",
    b"Direct Gemini edit rejected: Gemini response exceeds limit": "response-size",
    b"Direct Gemini edit rejected: Model returned invalid structured edit": "format",
    b"Direct Gemini edit rejected: Model returned invalid large-file response": "format",
    b"Direct Gemini edit rejected: Model returned unapproved path or content": "format",
    b"Direct Gemini edit rejected: No content change produced": "no-change",
    b"Direct Gemini edit rejected: Large-file replacement anchor is not unique": "anchor",
    b"Direct Gemini edit rejected: Overlapping large-file replacements": "anchor",
    b"Direct Gemini edit rejected: Edited file failed content safety checks": "safety",
    b"Direct Gemini edit rejected: Selected file is too large": "safety",
    b"Direct Gemini edit rejected: Selected file is invalid or oversized": "safety",
    b"Direct Gemini edit rejected: Exactly one validated edit target required": "safety",
    b"Direct Gemini edit rejected: JobError": "safety",
    b"Direct Gemini edit rejected: Regression source context validation failed": "safety",
    b"Direct Gemini edit rejected: Regression context exceeds limit": "safety",
}
# Safe finite HTTP status diagnostics; never copy response bodies/headers.
for _status in (500, 502, 503, 504):
    EDITOR_FAILURES[("Direct Gemini edit rejected: Gemini request rejected: service-" + str(_status)).encode("ascii")] = "service-" + str(_status)
EDITOR_CATEGORIES = frozenset(EDITOR_FAILURES.values()) | {"unknown", "timeout"}


# Publisher output is always converted into a fixed, allowlisted status.
# No child stdout, remote response, credentials or model text enter the ledger.
PUBLISH_CATEGORIES = frozenset({
    "missing-token", "stale-main", "github-unavailable",
    "approval-changed", "scope", "task-contract", "confirmation",
    "github-permission", "github-conflict", "github-rate-limit",
    "github-service", "github-api", "unknown",
})


class PublisherChildError(QueueError):
    def __init__(self, category):
        self.category = category if category in PUBLISH_CATEGORIES else "unknown"
        super().__init__("Restricted publisher failed")


class EditorChildError(QueueError):
    def __init__(self, category):
        if category not in EDITOR_CATEGORIES:
            category = "unknown"
        self.category = category
        super().__init__("Restricted editor failed")



def github_read(url, opener=urllib.request.urlopen):
    if (not url.startswith("https://api.github.com/repos/" + REPO + "/")
            or any(ch in url for ch in ("\r", "\n", "#"))):
        raise QueueError("Disallowed GitHub read")
    # Queue and publisher share the already configured repo-scoped token,
    # but Gemini editor children never receive it. Authenticated API reads
    # avoid the 60/hour anonymous limit during unattended 10-minute polling.
    token = os.environ.get("GITHUB_TOKEN", "")
    if not isinstance(token, str) or len(token) > 2048 or any(c in token for c in ("\r", "\n")):
        raise QueueError("Queue request unavailable")
    token = token.strip()
    if os.environ.get("ENABLE_QUEUE") == "true" and not token:
        raise QueueError("GitHub authentication required")
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "go-aider-task-queue",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    try:
        with opener(req, timeout=25) as response:
            raw = response.read(180_001)
        if len(raw) > 180_000:
            raise QueueError("Queue response exceeds limit")
        return json.loads(raw)
    except urllib.error.HTTPError as exc:
        # No remote content, URL or headers are logged. Distinguish only the
        # server's public quota signal to avoid speculative model retries.
        remaining = exc.headers.get("X-RateLimit-Remaining") if exc.headers else None
        if exc.code == 429 or (exc.code == 403 and remaining == "0"):
            raise QueueError("GitHub read rate limit reached") from None
        if exc.code in (401, 403) and token:
            raise QueueError("GitHub read unauthorized") from None
        raise QueueError("Queue request unavailable") from None
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        raise QueueError("Queue request unavailable") from None


def parse_task(issue, require_approval=True):
    if not isinstance(issue, dict) or "pull_request" in issue:
        return None
    author = issue.get("user") or {}
    if (issue.get("state") != "open" or author.get("login", "").lower() != "wood468"
            or not isinstance(issue.get("number"), int)
            or not str(issue.get("title", "")).startswith("[GO-WORKER] ")):
        return None
    if require_approval and "go-worker-approved" not in {
            label.get("name") for label in issue.get("labels", []) if isinstance(label, dict)}:
        return None
    body = issue.get("body")
    if not isinstance(body, str) or len(body) > 5000:
        return None
    # Section requests repeat the immutable instruction inside the envelope.
    # Retain the 5 KB issue cap, and require exactly one marker of any size.
    if sum(line.startswith('GO_WORKER_TASK_JSON:') for line in body.splitlines()) != 1:
        return None
    parts = re.findall(r'^GO_WORKER_TASK_JSON:(\{[^\n]{1,4500}\})$', body, re.MULTILINE)
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
    if set(obj) == {'version', 'paths', 'instruction', 'section'} and type(obj['version']) is int and obj['version'] == 4:
        paths = obj['paths']
        if (not isinstance(paths, list) or len(paths) not in (4, 5)
                or not all(isinstance(p, str) for p in paths)
                or tuple(paths[:2]) not in SOURCE_TEST_PAIRS | queue_scope.SECTION_SOURCE_TEST_PAIRS
                or paths[2:] != (['public/app.js', 'public/index.html', 'public/sw.js'] if paths[0] == 'public/dash.js' else ['public/index.html', 'public/sw.js'])):
            return None
        import section_editor
        try:
            section_editor.task_contract(obj['section'], paths[0])
        except section_editor.SectionEditError:
            return None
        if obj['section']['instruction'] != instruction.strip():
            return None
        return {'issue': issue['number'], 'version': 4, 'paths': list(paths),
                'instruction': instruction.strip(), 'section': dict(obj['section'])}
    if set(obj) == {"version", "paths", "instruction"} and type(obj["version"]) is int:
        version, paths = obj["version"], obj["paths"]
        if (not isinstance(paths, list) or not all(isinstance(p, str) for p in paths)
                or version not in (2, 3)):
            return None
        if version == 2 and (len(paths) != 2 or tuple(paths) not in SOURCE_TEST_PAIRS):
            return None
        if version == 3 and (len(paths) != 4
                or tuple(paths[:2]) not in SOURCE_TEST_PAIRS
                or paths[2:] != ["public/index.html", "public/sw.js"]):
            return None
        return {"issue": issue["number"], "version": version,
                "paths": list(paths), "instruction": instruction.strip()}
    return None


def task_fingerprint(task):
    """Stable identity of the complete parsed contract, including instruction."""
    return hashlib.sha256(json.dumps(task, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode("ascii")).hexdigest()


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
    except subprocess.TimeoutExpired:
        if command[1:2] == ["/opt/worker/direct_editor.py"]:
            raise EditorChildError("timeout") from None
        raise QueueError("Worker child did not finish") from None
    except OSError:
        raise QueueError("Worker child did not finish") from None
    if result.returncode:
        if command[1:2] == ["/opt/worker/direct_editor.py"]:
            # Exact-match fixed, pre-reviewed error lines ONLY. All other output
            # is untrusted and reduced to 'unknown'; nothing raw is logged.
            category = EDITOR_FAILURES.get(result.stdout.strip(), "unknown")
            raise EditorChildError(category)
        if command[1:2] == ["/opt/worker/publisher.py"]:
            # Publisher itself emits a single fixed marker. Never parse or
            # expose arbitrary stdout, API response text or exception details.
            marker = result.stdout.strip()
            prefix = b"WORKER_PUBLISH_DIAGNOSTIC:"
            category = marker[len(prefix):].decode("ascii", "ignore") if marker.startswith(prefix) else ""
            if marker != prefix + category.encode("ascii") or category not in PUBLISH_CATEGORIES:
                category = "unknown"
            raise PublisherChildError(category)
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
        stage = entry.get("failure_stage") if isinstance(entry, dict) else None
        if stage not in {"refresh", "target-validation", "approval-precheck",
                         "edit-source", "edit-test", "edit-single", "cache-sync",
                         "syntax-check", "diff-validation", "approval-postcheck",
                         "publish"}:
            stage = None
        category = entry.get("failure_category") if isinstance(entry, dict) else None
        valid = (EDITOR_CATEGORIES if stage in {"edit-single", "edit-source", "edit-test"}
                 else PUBLISH_CATEGORIES if stage == "publish" else frozenset())
        if category not in valid:
            category = None
        detail = "stage: " + stage if stage else ""
        if status == "needs-review" and category:
            detail += "; category: " + category
        print("Worker queue #" + str(key) + ": " + status +
              (" (" + detail + ")" if status == "needs-review" and detail else ""),
              flush=True)
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



def retire_abandoned_venue_clock_task(path=LEDGER, fetch=github_read):
    """Close out exact failed synthetic issue #475; never retry or publish."""
    state = read_ledger(path)
    previous = state.get("475")
    if (not isinstance(previous, dict) or previous.get("state") != "needs-review"
            or previous.get("attempts") not in (1, 2) or "pr" in previous):
        raise QueueError("Exact venue-clock task retirement not eligible")
    issue = fetch("https://api.github.com/repos/" + REPO + "/issues/475")
    if not isinstance(issue, dict):
        raise QueueError("Venue-clock task issue unavailable")
    body = issue.get("body")
    parts = TASK_RE.findall(body) if isinstance(body, str) else []
    try:
        task = json.loads(parts[0]) if len(parts) == 1 else {}
    except (TypeError, ValueError):
        task = {}
    labels = issue.get("labels")
    expected = "test/event-encounter-rotations.test.mjs"
    if (issue.get("number") != 475 or issue.get("state") != "open"
            or (issue.get("user") or {}).get("login", "").lower() != "wood468"
            or issue.get("title") != "[GO-WORKER] Future event venue-local habitat clock regression"
            or task.get("path") != expected or set(task) != {"path", "instruction"}
            or not isinstance(task.get("instruction"), str)
            or not isinstance(labels, list)
            or any(not isinstance(label, dict) or not isinstance(label.get("name"), str)
                   for label in labels)
            or any(label["name"] == "go-worker-approved" for label in labels)):
        raise QueueError("Exact venue-clock task identity or approval changed")
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
            if ("Worker: issue #475" in str(pr.get("title") or "")
                    or "Source task: #475" in str(pr.get("body") or "")):
                raise QueueError("Venue-clock task may already have a published PR")
        if verified_cutoff or len(prs) < 5:
            verified_cutoff = True
            break
    if not verified_cutoff:
        raise QueueError("Cannot verify complete venue-clock task PR history")
    state["475"] = {"state": "retired", "attempts": previous["attempts"], "reason": "abandoned-venue-clock"}
    save_ledger(state, path)
    print("Abandoned venue-clock task #475 retired without retry or PR", flush=True)
    return True



def retire_failed_four_file_pilot(path=LEDGER, fetch=github_read):
    """Close out exact failed synthetic issue #505; never retry or publish."""
    state = read_ledger(path)
    previous = state.get("505")
    if (not isinstance(previous, dict) or previous.get("state") != "needs-review"
            or previous.get("attempts") != 1 or "pr" in previous):
        raise QueueError("Exact four-file pilot retirement not eligible")
    issue = fetch("https://api.github.com/repos/" + REPO + "/issues/505")
    if not isinstance(issue, dict):
        raise QueueError("Four-file pilot issue unavailable")
    body = issue.get("body")
    parts = TASK_RE.findall(body) if isinstance(body, str) else []
    try:
        task = json.loads(parts[0]) if len(parts) == 1 else {}
    except (TypeError, ValueError):
        task = {}
    labels = issue.get("labels")
    expected = ["public/today.js", "test/today-logic.test.mjs", "public/index.html", "public/sw.js"]
    if (issue.get("number") != 505 or issue.get("state") != "open"
            or (issue.get("user") or {}).get("login", "").lower() != "wood468"
            or issue.get("title") != "[GO-WORKER] v0.5.11 disposable four-file asset-cache contract pilot"
            or task.get("version") != 3 or task.get("paths") != expected
            or not isinstance(task.get("instruction"), str)
            or not isinstance(labels, list)
            or any(not isinstance(label, dict) or not isinstance(label.get("name"), str)
                   for label in labels)
            or any(label["name"] == "go-worker-approved" for label in labels)):
        raise QueueError("Exact four-file pilot identity or approval changed")
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
            if ("Worker: issue #505" in str(pr.get("title") or "")
                    or "Source task: #505" in str(pr.get("body") or "")):
                raise QueueError("Four-file pilot may already have a published PR")
        if verified_cutoff or len(prs) < 5:
            verified_cutoff = True
            break
    if not verified_cutoff:
        raise QueueError("Cannot verify complete four-file pilot PR history")
    state["505"] = {"state": "retired", "attempts": 1, "reason": "discarded-four-file-pilot"}
    save_ledger(state, path)
    print("Failed four-file pilot #505 retired without retry or PR", flush=True)
    return True



def retire_failed_banner_cache_task(path=LEDGER, fetch=github_read):
    """Close out exact failed synthetic issue #516; never retry or publish."""
    state = read_ledger(path)
    previous = state.get("516")
    if (not isinstance(previous, dict) or previous.get("state") != "needs-review"
            or previous.get("attempts") != 1 or previous.get("failure_stage") != "edit-single" or "pr" in previous):
        raise QueueError("Exact banner-cache task retirement not eligible")
    issue = fetch("https://api.github.com/repos/" + REPO + "/issues/516")
    if not isinstance(issue, dict):
        raise QueueError("Banner-cache task issue unavailable")
    body = issue.get("body")
    parts = TASK_RE.findall(body) if isinstance(body, str) else []
    try:
        task = json.loads(parts[0]) if len(parts) == 1 else {}
    except (TypeError, ValueError):
        task = {}
    labels = issue.get("labels")
    expected = "test/offline-event-banner-cache.test.mjs"
    if (issue.get("number") != 516 or issue.get("state") != "open"
            or (issue.get("user") or {}).get("login", "").lower() != "wood468"
            or issue.get("title") != "[GO-WORKER] Test banner cache bypasses insecure and non-image requests"
            or task.get("path") != expected or set(task) != {"path", "instruction"}
            or not isinstance(task.get("instruction"), str)
            or not isinstance(labels, list)
            or any(not isinstance(label, dict) or not isinstance(label.get("name"), str)
                   for label in labels)
            or any(label["name"] == "go-worker-approved" for label in labels)):
        raise QueueError("Exact banner-cache task identity or approval changed")
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
            if ("Worker: issue #516" in str(pr.get("title") or "")
                    or "Source task: #516" in str(pr.get("body") or "")):
                raise QueueError("Banner-cache task may already have a published PR")
        if verified_cutoff or len(prs) < 5:
            verified_cutoff = True
            break
    if not verified_cutoff:
        raise QueueError("Cannot verify complete banner-cache task PR history")
    state["516"] = {"state": "retired", "attempts": 1, "reason": "discarded-banner-cache-task"}
    save_ledger(state, path)
    print("Failed banner-cache task #516 retired without retry or PR", flush=True)
    return True



def retire_failed_two_file_service_task(path=LEDGER, fetch=github_read):
    """Close out exact failed synthetic issue #531; never retry or publish."""
    state = read_ledger(path)
    previous = state.get("531")
    if (not isinstance(previous, dict) or previous.get("state") != "needs-review"
            or previous.get("attempts") != 1 or previous.get("failure_stage") != "edit-source" or previous.get("failure_category") != "service" or "pr" in previous):
        raise QueueError("Exact two-file-service task retirement not eligible")
    issue = fetch("https://api.github.com/repos/" + REPO + "/issues/531")
    if not isinstance(issue, dict):
        raise QueueError("Two-file service task issue unavailable")
    body = issue.get("body")
    parts = TASK_RE.findall(body) if isinstance(body, str) else []
    try:
        task = json.loads(parts[0]) if len(parts) == 1 else {}
    except (TypeError, ValueError):
        task = {}
    labels = issue.get("labels")
    expected = ["public/today.js", "test/today-logic.test.mjs"]
    if (issue.get("number") != 531 or issue.get("state") != "open"
            or (issue.get("user") or {}).get("login", "").lower() != "wood468"
            or issue.get("title") != "[GO-WORKER] v0.5.16 disposable two-file Today source/test acceptance pilot"
            or task.get("version") != 2 or task.get("paths") != expected or set(task) != {"version", "paths", "instruction"}
            or not isinstance(task.get("instruction"), str)
            or not isinstance(labels, list)
            or any(not isinstance(label, dict) or not isinstance(label.get("name"), str)
                   for label in labels)
            or any(label["name"] == "go-worker-approved" for label in labels)):
        raise QueueError("Exact two-file-service task identity or approval changed")
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
            if ("Worker: issue #531" in str(pr.get("title") or "")
                    or "Source task: #531" in str(pr.get("body") or "")):
                raise QueueError("Two-file service task may already have a published PR")
        if verified_cutoff or len(prs) < 5:
            verified_cutoff = True
            break
    if not verified_cutoff:
        raise QueueError("Cannot verify complete two-file-service task PR history")
    state["531"] = {"state": "retired", "attempts": 1, "reason": "failed-two-file-service-pilot"}
    save_ledger(state, path)
    print("Failed two-file-service task #531 retired without retry or PR", flush=True)
    return True



def retire_failed_places_publish_task(path=LEDGER, fetch=github_read):
    """One-way reconciliation for deapproved #550; no execution or retry.

    Require exhaustive bounded post-issue PR history and inspect all live
    worker/review branches for an ambiguous unpublished Places modification.
    Nothing changes if any evidence is missing or contradictory.
    """
    state = read_ledger(path)
    record = state.get("550")
    if (not isinstance(record, dict)
            or record != {"state": "needs-review", "attempts": 1,
                          "failure_stage": "publish"}):
        raise QueueError("Places publish task retirement is not eligible")

    issue = fetch("https://api.github.com/repos/" + REPO + "/issues/550")
    if not isinstance(issue, dict):
        raise QueueError("Places task issue unavailable")
    body = issue.get("body")
    parts = TASK_RE.findall(body) if isinstance(body, str) else []
    try:
        task = json.loads(parts[0]) if len(parts) == 1 else {}
    except (TypeError, ValueError):
        task = {}
    labels = issue.get("labels")
    if (issue.get("number") != 550 or issue.get("state") != "open"
            or (issue.get("user") or {}).get("login", "").lower() != "wood468"
            or issue.get("title") != "[GO-WORKER] Reject impossible Places event and provenance calendar dates"
            or not isinstance(labels, list)
            or any(not isinstance(label, dict) or not isinstance(label.get("name"), str)
                   for label in labels)
            or any(label["name"] == "go-worker-approved" for label in labels)
            or set(task) != {"path", "instruction"}
            or task.get("path") != "test/places.test.mjs"
            or not isinstance(task.get("instruction"), str)
            or not task["instruction"].strip()):
        raise QueueError("Places task identity, approval or contract changed")

    iso = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
    created = issue.get("created_at")
    if not isinstance(created, str) or not re.fullmatch(iso, created):
        raise QueueError("Places task issue timestamp unavailable")

    history_complete = False
    for page in range(1, 26):
        prs = fetch("https://api.github.com/repos/" + REPO +
                    "/pulls?state=all&sort=created&direction=desc&per_page=5&page=" +
                    str(page))
        if not isinstance(prs, list) or len(prs) > 5:
            raise QueueError("Invalid Places PR history")
        for pr in prs:
            if not isinstance(pr, dict):
                raise QueueError("Malformed Places PR history")
            timestamp = pr.get("created_at")
            if not isinstance(timestamp, str) or not re.fullmatch(iso, timestamp):
                raise QueueError("Places PR timestamp unavailable")
            if timestamp < created:
                history_complete = True
                break
            if ("Worker: issue #550" in str(pr.get("title") or "")
                    or "Source task: #550" in str(pr.get("body") or "")):
                raise QueueError("Places task may already have a published PR")
        if history_complete or len(prs) < 5:
            history_complete = True
            break
    if not history_complete:
        raise QueueError("Cannot verify complete Places PR history")

    # Older random worker/review-* names predate deterministic task branches.
    # Every currently live worker branch must therefore be inspected, not just
    # the newer task-specific branch name.
    branches_complete = False
    examined = set()
    for page in range(1, 11):
        branches = fetch("https://api.github.com/repos/" + REPO +
                         "/branches?per_page=100&page=" + str(page))
        if not isinstance(branches, list) or len(branches) > 100:
            raise QueueError("Invalid Places branch inventory")
        for branch in branches:
            if not isinstance(branch, dict):
                raise QueueError("Malformed Places branch inventory")
            name = branch.get("name")
            sha = (branch.get("commit") or {}).get("sha")
            if not isinstance(name, str) or not isinstance(sha, str):
                raise QueueError("Incomplete Places branch inventory")
            if name.startswith("worker/review-"):
                if not re.fullmatch(r"worker/review-[a-f0-9]{12}", name) or not re.fullmatch(r"[a-f0-9]{40}", sha):
                    raise QueueError("Unknown worker branch identity")
                if sha in examined:
                    continue
                examined.add(sha)
                commit = fetch("https://api.github.com/repos/" + REPO + "/commits/" + sha)
                if not isinstance(commit, dict) or not isinstance(commit.get("files"), list):
                    raise QueueError("Worker branch diff unavailable")
                if any(not isinstance(item, dict) or not isinstance(item.get("filename"), str)
                       for item in commit["files"]):
                    raise QueueError("Worker branch diff invalid")
                if any(item["filename"] == "test/places.test.mjs" for item in commit["files"]):
                    raise QueueError("Possible unpublished Places worker branch")
        if len(branches) < 100:
            branches_complete = True
            break
    if not branches_complete:
        raise QueueError("Cannot verify complete worker branch inventory")

    state["550"] = {"state": "retired", "attempts": 1,
                    "reason": "reconciled-failed-places-publish"}
    save_ledger(state, path)
    print("Failed Places publish task #550 retired without retry or PR", flush=True)
    return True



# Fixed identity of the failed, deapproved v1 task. Never accept a new task
# by changing the issue body after the fact.
RETIRED_563_INSTRUCTION = "Edit ONLY existing tracked test/event-encounter-rotations.test.mjs. Append exactly one deterministic node:test regression named 'future venue-local rotation respects daylight-saving offset changes'. Clone the existing fixture with copy(), set a future event ID and valid Australian DST transition weekend dates, timeBasis='venue-local', timeZone='Australia/Melbourne'; use Rot.validate and Rot.active with explicit UTC Date instants to verify venue-local habitat-hour switching on both sides of the DST transition. Preserve all existing tests, imports and source data. Do not change any other file. If the existing API does not permit a valid fixture, do not invent behavior or edit source. Publish one DRAFT PR only; never merge or deploy."


def retire_failed_rotation_service_task(path=LEDGER, fetch=github_read):
    return _retire_reviewed_edit_failure(path, fetch, 563,
        "[GO-WORKER] Unattended v1 regression: venue-local habitat DST boundaries",
        {"path": "test/event-encounter-rotations.test.mjs", "instruction": RETIRED_563_INSTRUCTION},
        {"state": "needs-review", "attempts": 1, "failure_stage": "edit-single", "failure_category": "service"},
        {"test/event-encounter-rotations.test.mjs"}, "failed-rotation-edit-service-no-retry",
        "Rotation edit-service task #563 safely retired without retry or PR")


RETIRED_594_CONTRACT = {'version': 2, 'paths': ['public/today.js', 'test/today-logic.test.mjs'], 'instruction': "Implement one reusable Max Battle classification fix. In public/today.js change only raidTier's Max-name detection: preserve ev.kind === 'max' as an explicit match, but replace the broad substring /max/i with a case-insensitive whole-word match for Max, Dynamax or Gigantamax. Preserve the existing star/Mega/Shadow/Raid Hour precedence and all other code. In test/today-logic.test.mjs append a node:test regression that uses the existing createRequire import to load ../public/today.js inside the test. Assert raidTier returns null for kind:'event' with names 'Climax Celebration' and 'Maximum Rewards'; returns {stars:0,label:'Max Battle'} for 'Max Monday', 'Dynamax Battles', 'Gigantamax Battle Day' and mixed-case equivalents; and preserves explicit kind:'max' with an otherwise unrelated name. Also assert 5-star raids and Mega raids retain their existing labels. Keep all existing tests unchanged. Edit only the currently selected file on each invocation; do not add files, change caches, dependencies, UI markup or unrelated logic."}


def retire_failed_max_anchor_task(path=LEDGER, fetch=github_read):
    return _retire_reviewed_edit_failure(path, fetch, 594,
        "[GO-WORKER] Controlled source/test pilot: match Max Battle names as whole words",
        RETIRED_594_CONTRACT,
        {"state": "needs-review", "attempts": 1, "failure_stage": "edit-source",
         "failure_category": "anchor", "task_sha256": 'c0e799e5623e65eb882adf20079f6708f32cc96a83d7f5228cf8f50794c19a61'},
        set(RETIRED_594_CONTRACT["paths"]), "failed-max-source-anchor-no-retry",
        "Max source-anchor task #594 safely retired without retry or PR")


def _retire_reviewed_edit_failure(path, fetch, number, title, contract_expected,
                                  expected_failure, owned_paths, reason, message,
                                  required_review_label=None, retired_details=None):
    """Shared no-publication audit for fixed or explicitly reviewed retirements.

    This action is deliberately NOT a generic retry/reset. It has no
    GitHub write capability and is launched with neither provider credential.
    """
    state = read_ledger(path)
    previous = state.get(str(number))
    if (not isinstance(previous, dict) or previous != expected_failure):
        raise QueueError("Rotation service retirement is not eligible")

    issue = fetch("https://api.github.com/repos/" + REPO + "/issues/" + str(number))
    if not isinstance(issue, dict):
        raise QueueError("Rotation task issue unavailable")
    raw = issue.get("body")
    matches = re.findall(r'^GO_WORKER_TASK_JSON:(\{[^\n]{1,4500}\})$', raw, re.MULTILINE) if isinstance(raw, str) and len(raw) <= 5000 else []
    try:
        contract = json.loads(matches[0]) if len(matches) == 1 else None
    except (ValueError, TypeError):
        contract = None
    labels = issue.get("labels")
    if isinstance(raw, str) and sum(line.startswith("GO_WORKER_TASK_JSON:") for line in raw.splitlines()) != 1:
        raise QueueError("Failure contract markers changed")
    if (type(issue.get("number")) is not int or issue["number"] != number
            or issue.get("state") != "open"
            or not isinstance(issue.get("user"), dict)
            or issue["user"].get("login", "").lower() != "wood468"
            or issue.get("title") != title
            or not isinstance(labels, list)
            or any(not isinstance(label, dict) or not isinstance(label.get("name"), str)
                   for label in labels)
            or any(label["name"] == "go-worker-approved" for label in labels)
            or not isinstance(contract, dict)
            or contract != contract_expected
            or (required_review_label is not None and (
                required_review_label not in {label["name"] for label in labels}
                or "go-worker-auto-merge-approved" in {label["name"] for label in labels}))):
        raise QueueError("Rotation task identity, contract or approval changed")

    iso = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
    created = issue.get("created_at")
    if not isinstance(created, str) or not re.fullmatch(iso, created):
        raise QueueError("Rotation task timestamp unavailable")

    # Follow all PR pages since the original issue, including CLOSED drafts.
    # Check both explicit task linkage and branch/file content for any worker
    # PR with erased task references. Unknown metadata fails closed.
    pr_history_complete = False
    last_created = None
    seen_prs = set()
    for page in range(1, 26):
        prs = fetch("https://api.github.com/repos/" + REPO +
                    "/pulls?state=all&sort=created&direction=desc&per_page=5&page=" +
                    str(page))
        if not isinstance(prs, list) or len(prs) > 5:
            raise QueueError("Rotation PR history unavailable")
        for pr in prs:
            if not isinstance(pr, dict):
                raise QueueError("Rotation PR history malformed")
            timestamp = pr.get("created_at")
            pr_number = pr.get("number")
            if (not isinstance(timestamp, str) or not re.fullmatch(iso, timestamp)
                    or type(pr_number) is not int or pr_number <= 0 or pr_number in seen_prs
                    or (last_created is not None and timestamp > last_created)):
                raise QueueError("Rotation PR history incomplete")
            seen_prs.add(pr_number)
            last_created = timestamp
            if timestamp < created:
                pr_history_complete = True
                break
            if ("Worker: issue #" + str(number) in str(pr.get("title") or "")
                    or "Source task: #" + str(number) in str(pr.get("body") or "")):
                raise QueueError("Rotation task may already have a worker PR")
            head = pr.get("head")
            if not isinstance(head, dict) or not isinstance(head.get("ref"), str):
                raise QueueError("Rotation PR source branch unavailable")
            if head["ref"].startswith("worker/review-"):
                if not re.fullmatch(r"worker/review-[a-f0-9]{12}", head["ref"]):
                    raise QueueError("Unexpected worker PR branch")
                # A hidden task edit could appear in an unrelatedly titled PR.
                # Check its complete changed-file inventory, never just title.
                files_complete = False
                seen_paths = set()
                for file_page in range(1, 11):
                    changed = fetch("https://api.github.com/repos/" + REPO +
                                    "/pulls/" + str(pr_number) +
                                    "/files?per_page=100&page=" + str(file_page))
                    if not isinstance(changed, list) or len(changed) > 100:
                        raise QueueError("Worker PR file history unavailable")
                    for item in changed:
                        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                            raise QueueError("Worker PR changed-file metadata invalid")
                        filename = item["filename"]
                        if filename in seen_paths:
                            raise QueueError("Worker PR file history repeated")
                        seen_paths.add(filename)
                        if filename in owned_paths:
                            raise QueueError("Potential rotation worker PR already exists")
                    if len(changed) < 100:
                        files_complete = True
                        break
                if not files_complete:
                    raise QueueError("Worker PR file pagination incomplete")
        if pr_history_complete or len(prs) < 5:
            pr_history_complete = True
            break
    if not pr_history_complete:
        raise QueueError("Rotation PR pagination incomplete")

    # Historical random and new deterministic refs both use worker/review-.
    # Never trust the branch TIP diff: a later unrelated commit could hide
    # an earlier edit. For post-issue branch tips, compare the entire branch
    # against its merge-base with main, then verify the response is complete.
    branch_history_complete = False
    seen_branches = set()
    for page in range(1, 11):
        branches = fetch("https://api.github.com/repos/" + REPO +
                         "/branches?per_page=100&page=" + str(page))
        if not isinstance(branches, list) or len(branches) > 100:
            raise QueueError("Rotation branch inventory unavailable")
        for branch in branches:
            if not isinstance(branch, dict):
                raise QueueError("Rotation branch inventory invalid")
            name = branch.get("name")
            head = branch.get("commit")
            sha = head.get("sha") if isinstance(head, dict) else None
            if (not isinstance(name, str) or name in seen_branches
                    or not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{40}", sha)):
                raise QueueError("Rotation branch inventory incomplete")
            seen_branches.add(name)
            if not name.startswith("worker/review-"):
                continue
            if not re.fullmatch(r"worker/review-[a-f0-9]{12}", name):
                raise QueueError("Unexpected worker branch identity")
            tip = fetch("https://api.github.com/repos/" + REPO + "/commits/" + sha)
            if not isinstance(tip, dict) or tip.get("sha") != sha:
                raise QueueError("Worker branch commit unverified")
            info = tip.get("commit")
            committer = info.get("committer") if isinstance(info, dict) else None
            timestamp = committer.get("date") if isinstance(committer, dict) else None
            if not isinstance(timestamp, str) or not re.fullmatch(iso, timestamp):
                raise QueueError("Worker branch timestamp unavailable")
            if timestamp < created:
                # Verified branch tip predates the task. It cannot include
                # a later edit of #563.
                continue

            # The three-dot compare includes all branch-only commits since
            # the merge base, not merely the last commit's file list.
            comparison = fetch("https://api.github.com/repos/" + REPO +
                               "/compare/main..." + name)
            if not isinstance(comparison, dict):
                raise QueueError("Worker branch comparison unavailable")
            count = comparison.get("total_commits")
            ahead = comparison.get("ahead_by")
            commits = comparison.get("commits")
            files = comparison.get("files")
            if (type(count) is not int or not 1 <= count <= 100
                    or type(ahead) is not int or ahead != count
                    or not isinstance(commits, list) or len(commits) != count
                    or not isinstance(files, list) or not 1 <= len(files) < 300
                    or comparison.get("status") not in {"ahead", "diverged"}
                    or not isinstance(commits[-1], dict)
                    or commits[-1].get("sha") != sha):
                raise QueueError("Worker branch comparison incomplete")
            seen_paths = set()
            for item in files:
                filename = item.get("filename") if isinstance(item, dict) else None
                if not isinstance(filename, str) or filename in seen_paths:
                    raise QueueError("Worker branch changed files invalid")
                seen_paths.add(filename)
                if filename in owned_paths:
                    raise QueueError("Ambiguous existing rotation worker branch")
        if len(branches) < 100:
            branch_history_complete = True
            break
    if not branch_history_complete:
        raise QueueError("Rotation branch pagination incomplete")

    # Only one immutable ledger entry changes; all prior results survive.
    state[str(number)] = {"state": "retired", "attempts": 1, "reason": reason}
    if retired_details:
        state[str(number)].update(retired_details)
    save_ledger(state, path)
    print(message, flush=True)
    return True


REVIEW_LABEL = "go-worker-failure-reviewed"
# One pre-fingerprint historical row; future failures must retain their hash.
LEGACY_SERVICE_REVIEW = {599: '7fd9519525679cbeb75a381ecdb7cdec737607c247411bda9c31b512eacf68a3'}


def diagnose_section_syntax(root, task, runner=None):
    """Reproduce parsing with no credentials; return only finite categories."""
    import publisher
    import cache_contract
    runner = runner or subprocess.run
    try:
        _head, files = publisher.safe_files(root, section_contract=task['section'])
        cache_contract.verify(root, task['paths'][0], str(task['issue']))
    except (publisher.PublishError, cache_contract.CacheContractError):
        raise QueueError('Syntax review baseline or scope invalid') from None
    if [path for path, _ in files] != sorted(task['paths']):
        raise QueueError('Syntax review scope changed')
    changed = dict(files)
    outcomes = {}
    for role, path in zip(('source', 'test'), task['paths'][:2]):
        baseline = publisher.local_git(root, 'show', 'HEAD:' + path)
        mode = 'module' if path.endswith('.mjs') else 'commonjs'
        for kind, data in (('baseline', baseline), ('updated', changed[path])):
            try:
                result = runner(['node', '--input-type=' + mode, '--check'], input=data,
                                cwd=root, env=child_env(), stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=20, check=False)
            except (subprocess.SubprocessError, OSError):
                raise QueueError('Syntax diagnosis unavailable') from None
            category = 'pass'
            if result.returncode:
                error = result.stderr
                if not isinstance(error, bytes) or len(error) > 500_000:
                    raise QueueError('Syntax diagnosis unavailable')
                if b'SyntaxError: Identifier ' in error and b'has already been declared' in error:
                    category = 'duplicate-binding'
                elif b'SyntaxError: Unexpected end of input' in error:
                    category = 'unexpected-eof'
                elif b'SyntaxError: Invalid or unexpected token' in error:
                    category = 'invalid-token'
                elif b'SyntaxError: Unexpected ' in error:
                    category = 'unexpected-token'
                elif b'SyntaxError: Invalid regular expression' in error:
                    category = 'invalid-regexp'
                elif b'SyntaxError: missing ' in error:
                    category = 'missing-delimiter'
                elif b'SyntaxError:' in error:
                    category = 'syntax-error'
                else:
                    raise QueueError('Syntax diagnosis unclassified')
            outcomes[role + '-' + kind] = category
            print('Reviewed syntax ' + role + ' ' + kind + ': ' + category, flush=True)
    if (outcomes['source-baseline'] != 'pass' or outcomes['test-baseline'] != 'pass'
            or outcomes['source-updated'] == outcomes['test-updated'] == 'pass'):
        raise QueueError('Syntax failure cannot be attributed to this edit')
    return outcomes


def isolate_reviewed_service_failure(path=LEDGER, fetch=github_read):
    """Reviewed prewrite failure isolation; never retry, edit or publish."""
    receipt = queue_scope.review_request(os.environ.get('WORKER_FAILURE_REVIEW'))
    allowed = queue_scope.parse(os.environ.get('WORKER_FILE_ALLOWLIST'))
    state = read_ledger(path)
    record = state.get(str(receipt['issue']))
    required = {'state', 'attempts', 'failure_stage', 'failure_category'}
    source_syntax = (isinstance(record, dict) and set(record) == {'state', 'attempts', 'failure_stage', 'task_sha256'}
                     and record.get('failure_stage') == 'syntax-check')
    source_format = (isinstance(record, dict) and record.get('failure_stage') == 'edit-source'
                     and record.get('failure_category') == 'format')
    service = (isinstance(record, dict) and record.get('failure_stage') == 'edit-single'
               and record.get('failure_category') in {'service', 'service-500', 'service-502', 'service-503', 'service-504'})
    if (not isinstance(record, dict) or (set(record) not in (required, required | {'task_sha256'}) and not source_syntax)
            or record.get('state') != 'needs-review' or type(record.get('attempts')) is not int
            or record['attempts'] != 1 or not (source_format or source_syntax or service)
            or ('task_sha256' not in record and ((source_format or source_syntax) or LEGACY_SERVICE_REVIEW.get(receipt['issue']) != receipt['task_sha256']))
            or ('task_sha256' in record and record['task_sha256'] != receipt['task_sha256'])):
        raise QueueError('Failure not eligible for reviewed isolation')
    issue = fetch('https://api.github.com/repos/' + REPO + '/issues/' + str(receipt['issue']))
    task = parse_task(issue, require_approval=False)
    if not task or task_fingerprint(task) != receipt['task_sha256']:
        raise QueueError('Reviewed failure contract changed')
    diagnostics = None
    if source_format or source_syntax:
        if task.get('version') != 4 or task['paths'][0] != receipt['path'] or allowed != task['paths']:
            raise QueueError('Failure review scope mismatch')
        owned = set(task['paths'])
        contract = {k: v for k, v in task.items() if k != 'issue'}
        reason = 'operator-reviewed-source-format-no-retry'
        if source_syntax:
            diagnostics = diagnose_section_syntax(CHECKOUT, task)
            reason = 'operator-reviewed-syntax-no-retry'
    else:
        if 'version' in task or task.get('path') != receipt['path'] or allowed != [task['path']]:
            raise QueueError('Failure review scope mismatch')
        owned = {task['path']}
        contract = {'path': task['path'], 'instruction': task['instruction']}
        reason = 'operator-reviewed-edit-service-no-retry'
    return _retire_reviewed_edit_failure(path, fetch, receipt['issue'], issue['title'],
        contract, record, owned, reason,
        'Reviewed prewrite task #' + str(receipt['issue']) + ' isolated without retry or PR',
        required_review_label=REVIEW_LABEL,
        retired_details={'task_sha256': receipt['task_sha256'], 'failure_stage': record['failure_stage'],
                         'failure_category': record.get('failure_category', 'syntax'),
                         **({'review_syntax': diagnostics} if diagnostics else {})})


def perform(task, state, fetch=github_read, runner=child_run):
    if "WORKER_FILE_ALLOWLIST" in os.environ and not queue_scope.permits(
            task, queue_scope.parse(os.environ["WORKER_FILE_ALLOWLIST"])):
        raise QueueError("Task outside explicit queue allowlist")
    key = str(task["issue"])
    if not approved_for_dispatch(task, state) or has_pending_review(state, fetch):
        return False
    gemini = os.environ.get("GEMINI_API_KEY")
    github = os.environ.get("GITHUB_TOKEN")
    if not gemini or not github:
        raise QueueError("Missing queue credentials")
    prior = state.get(key, {})
    attempts = 2 if isinstance(prior, dict) and prior.get("state") == "approved-pilot-retry" else 1
    contract_sha = task_fingerprint(task)
    state[key] = {"state": "started", "attempts": attempts, "task_sha256": contract_sha}
    save_ledger(state)
    stage = "refresh"
    try:
        runner([sys.executable, "/opt/worker/refresh_repo.py"], child_env())
        stage = "target-validation"
        version = task.get("version")
        multi = version in (2, 3, 4)
        paths = task["paths"] if multi else [task["path"]]
        edit_paths = paths[:2] if version in (3, 4) else paths
        if multi and tuple(edit_paths) not in (SOURCE_TEST_PAIRS | queue_scope.SECTION_SOURCE_TEST_PAIRS if version == 4 else SOURCE_TEST_PAIRS):
            raise QueueError("Disallowed source/test contract")
        if version in (3, 4) and paths[2:] != (["public/app.js", "public/index.html", "public/sw.js"] if version == 4 and paths[0] == "public/dash.js" else ["public/index.html", "public/sw.js"]):
            raise QueueError("Disallowed cache update paths")
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
        stage = "approval-precheck"
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
        for i, relative in enumerate(edit_paths):
            stage = "edit-source" if multi and i == 0 else "edit-test" if multi else "edit-single"
            runner([sys.executable, "/opt/worker/direct_editor.py"],
                   child_env(GEMINI_API_KEY=gemini, EDIT_FILES=relative,
                             GO_WORKER_QUEUE_EDIT="1",
                             **({'WORKER_SECTION_CONTRACT': json.dumps(task['section'])}
                                if version == 4 and i == 0 else
                                {'WORKER_TEST_SOURCE_TASK': json.dumps(task), 'WORKER_TASK_SHA256': contract_sha}
                                if version == 4 else {})))
        if version in (3, 4):
            stage = "cache-sync"
            # The model may never edit HTML/SW; apply exactly two deterministic
            # version-token substitutions against pristine tracked baselines.
            import cache_contract
            cache_contract.apply(CHECKOUT, paths[0], key)
        stage = "syntax-check"
        # Parse edited JS only. Never execute model-produced tests in HA.
        for relative in edit_paths:
            if multi or relative.startswith("test/"):
                runner(["node", "--check", str(CHECKOUT / relative)], child_env())
        stage = "diff-validation"
        import publisher
        _head, changed = publisher.safe_files(CHECKOUT, **({"section_contract": task["section"]} if version == 4 else {}))
        if [name for name, _data in changed] != sorted(paths):
            raise QueueError("Missing or extra modified task paths")
        stage = "approval-postcheck"
        if multi and parse_task(fetch("https://api.github.com/repos/" + REPO +
                                       "/issues/" + key)) != task:
            raise QueueError("Multi-file approval revoked before publication")
        global_scope = queue_scope.parse(os.environ["WORKER_FILE_ALLOWLIST"]) if "WORKER_FILE_ALLOWLIST" in os.environ else paths
        if not queue_scope.permits(task, global_scope):
            raise QueueError("Task outside explicit queue allowlist")
        publication_env = child_env(GITHUB_TOKEN=github, ENABLE_PUBLISHING="true",
                                    WORKER_ISSUE_NUMBER=key,
                                    WORKER_FILE_ALLOWLIST=json.dumps(global_scope),
                                    WORKER_DISPATCH_SCOPE=json.dumps(paths))
        if version == 4:
            publication_env["WORKER_SECTION_CONTRACT"] = json.dumps(task["section"])
        if multi:
            publication_env["WORKER_APPROVED_PATHS"] = json.dumps(paths)
            publication_env["WORKER_TASK_SHA256"] = contract_sha
        stage = "publish"
        out = runner([sys.executable, "/opt/worker/publisher.py"], publication_env)
        matched = re.search(rb"Created draft PR #(\d+)", out)
        if matched is None:
            raise QueueError("No verified PR number; inspect GitHub before retry")
        number = int(matched.group(1))
        state[key] = {"state": "submitted", "pr": number, "attempts": attempts}
        state[key]["task_sha256"] = contract_sha
        save_ledger(state)
        print("Submitted issue #" + key + " as draft PR #" + str(number), flush=True)
        return True
    except Exception as exc:
        # Static stage/category ONLY: no exception text, prompts, files, raw
        # child output or credentials ever reach this durable ledger.
        failure = {"state": "needs-review", "attempts": attempts,
                   "failure_stage": stage}
        if ((isinstance(exc, EditorChildError) and stage in {"edit-single", "edit-source", "edit-test"})
                or (isinstance(exc, PublisherChildError) and stage == "publish")):
            failure["failure_category"] = exc.category
        failure["task_sha256"] = contract_sha
        state[key] = failure
        save_ledger(state)
        raise


def once(scan=False, fetch=github_read, runner=child_run, ledger_path=LEDGER,
         unattended=False, allowlist=None, section_only=False):
    if section_only and (not unattended or allowlist is None):
        raise queue_scope.ScopeError('Large-file queue requires explicit unattended scope')
    if allowlist is not None:
        allowlist = queue_scope.validate(allowlist, unattended=unattended, section_only=section_only)
    tasks = candidates(fetch)
    if unattended and not section_only:
        # Continuous background execution is limited to the previously proven
        # legacy single-file docs / existing flat test contract. Version-two
        # and version-three source+test jobs require an explicit manual pilot.
        tasks = [task for task in tasks if "version" not in task]
    if allowlist is not None:
        tasks = [t for t in tasks if queue_scope.permits(t, allowlist, unattended, section_only)]
    state = read_ledger(ledger_path)
    if scan:
        waiting = sum(approved_for_dispatch(t, state) for t in tasks)
        print("Approved queued tasks: " + str(waiting) + "; no edits", flush=True)
        return waiting
    if not tasks:
        # With no approved tasks there is nothing to dispatch. Avoid probing
        # every historical submitted PR on each ten-minute idle poll: those
        # unauthenticated public GitHub reads consume scarce API quota.
        if any(isinstance(entry, dict) and
               entry.get("state") in {"started", "needs-review"}
               for entry in state.values()):
            print("Queue paused for review of previous task", flush=True)
        else:
            print("No approved unprocessed tasks", flush=True)
        return 0
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
    if mode not in ("scan", "once", "loop", "section_loop", "status", "recover_pilot", "recover_code_pilot", "recover_artwork_task", "ack_code_pilot", "retire_artwork_task", "retire_two_file_pilot", "retire_venue_clock_task", "retire_four_file_pilot", "retire_banner_cache_task", "retire_two_file_service_task", "retire_places_publish_task", "retire_rotation_service_task", "retire_max_anchor_task", "isolate_reviewed_service_failure"):
        raise QueueError("Invalid queue mode")
    allowlist = None
    if mode in {"once", "loop", "section_loop", "isolate_reviewed_service_failure"}:
        allowlist = queue_scope.parse(os.environ.get("WORKER_FILE_ALLOWLIST"),
                                     unattended=(mode in {"loop", "section_loop"}),
                                     section_only=(mode == "section_loop"))
    with LOCK.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise QueueError("Another queue worker holds the lock") from None
        if mode in {"loop", "section_loop"}:
            section_only = mode == "section_loop"
            print("Approved large-file queue active: one bounded section scope; one draft PR at a time"
                  if section_only else
                  "Approved queue active: explicit test-file allowlist only; one draft PR at a time", flush=True)
            shutdown = threading.Event()
            previous_handler = signal.getsignal(signal.SIGTERM)
            def request_shutdown(_signum, _frame):
                # No model or GitHub work in the signal handler. Drain any
                # active bounded iteration, then exit. Idle waits wake at once.
                shutdown.set()
            signal.signal(signal.SIGTERM, request_shutdown)
            try:
                while not shutdown.is_set():
                    try:
                        once(unattended=True, allowlist=allowlist, section_only=section_only)
                    except (QueueError, OSError, ValueError):
                        print("Queue iteration failed; task held for review", flush=True)
                    shutdown.wait(600)
            finally:
                signal.signal(signal.SIGTERM, previous_handler)
            print("Approved queue stopped cleanly", flush=True)
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
        elif mode == "retire_venue_clock_task":
            retire_abandoned_venue_clock_task()
        elif mode == "retire_four_file_pilot":
            retire_failed_four_file_pilot()
        elif mode == "retire_banner_cache_task":
            retire_failed_banner_cache_task()
        elif mode == "retire_two_file_service_task":
            retire_failed_two_file_service_task()
        elif mode == "retire_places_publish_task":
            retire_failed_places_publish_task()
        elif mode == "retire_rotation_service_task":
            retire_failed_rotation_service_task()
        elif mode == "retire_max_anchor_task":
            retire_failed_max_anchor_task()
        elif mode == "isolate_reviewed_service_failure":
            isolate_reviewed_service_failure()
        else:
            once(scan=(mode == "scan"), allowlist=allowlist)


if __name__ == "__main__":
    try:
        main()
    except (QueueError, OSError, ValueError) as exc:
        # The parent may render ONLY this static marker; never print exc.
        print("WORKER_QUEUE_DIAGNOSTIC:" + safe_preclaim_category(exc), flush=True)
        sys.exit(2)
