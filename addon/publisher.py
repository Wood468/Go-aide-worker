#!/usr/bin/env python3
"""Publish a small reviewed-by-policy working-tree diff to a new DRAFT PR only.

This module is deliberately separate from Aider. The GitHub token is available
to this process alone for the explicitly opted-in publish action.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from text_safety import unsafe_controls
import queue_scope

REPO = "Wood468/crispy-barnacle"
ROOT = "https://api.github.com/repos/" + REPO
ALLOWED_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".css",
                    ".html", ".md", ".yaml", ".yml", ".txt"}
TEST_MJS_PATH = re.compile(r"test/[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\.test\.mjs")
FORBIDDEN_COMPONENTS = {".git", ".github", ".env", "secrets", "credentials", ".ssh"}
MAX_FILES = 10
MAX_FILE_BYTES = 100_000
MAX_TOTAL_BYTES = 250_000
SECRET_MARKERS = (b"-----BEGIN PRIVATE KEY-----", b"github_pat_", b"ghp_", b"gho_", "AIza".encode("utf-8"))


class PublishError(Exception):
    pass


# Fixed, non-sensitive error categories for queue diagnostics. Never return
# raw exception details, remote response bodies, request paths or credentials.
SAFE_PUBLISH_ERRORS = {
    "Missing GitHub token": "missing-token",
    "Queued task requires explicit file scope": "task-contract",
    "Main moved since checkout; refresh before publishing": "stale-main",
    "GitHub API unavailable": "github-unavailable",
    "Approved GitHub task was revoked or changed": "approval-changed",
    "Edited files differ from approved task scope": "scope",
    "Restricted changed path": "scope",
    "Invalid source issue identifier": "task-contract",
    "Draft PR confirmation missing": "confirmation",
    "GitHub did not confirm draft PR": "confirmation",
}
SAFE_HTTP_ERRORS = {
    401: "github-permission", 403: "github-permission",
    409: "github-conflict", 422: "github-conflict",
    429: "github-rate-limit", 500: "github-service",
    502: "github-service", 503: "github-service",
    504: "github-service",
}
PUBLISH_CATEGORIES = frozenset(SAFE_PUBLISH_ERRORS.values()) | frozenset(SAFE_HTTP_ERRORS.values()) | {"github-api", "unknown"}


def safe_publish_category(error):
    if not isinstance(error, PublishError):
        return "unknown"
    message = str(error)
    if message in SAFE_PUBLISH_ERRORS:
        return SAFE_PUBLISH_ERRORS[message]
    match = re.fullmatch(r"GitHub API refused operation \(HTTP ([0-9]{3})\)", message)
    if match:
        return SAFE_HTTP_ERRORS.get(int(match.group(1)), "github-api")
    return "unknown"


def queued_branch_suffix(issue_number, base_sha):
    """Stable per issue/main-base, so crashes cannot create fresh random refs."""
    if not re.fullmatch(r"[1-9][0-9]{0,9}", issue_number):
        raise PublishError("Invalid source issue identifier")
    if not re.fullmatch(r"[a-f0-9]{40}", base_sha):
        raise PublishError("Invalid base commit")
    return hashlib.sha256((issue_number + ":" + base_sha).encode("ascii")).hexdigest()[:12]


def local_git(workspace, *args):
    env = {"PATH": "/usr/bin:/bin", "HOME": "/home/coder", "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
           "GIT_CONFIG_VALUE_0": "/dev/null"}
    result = subprocess.run(["git", *args], cwd=workspace, env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=30, check=False)
    if result.returncode:
        raise PublishError("Local Git validation failed")
    return result.stdout


def safe_files(workspace, section_contract=None):
    workspace = Path(workspace)
    if workspace.is_symlink() or not (workspace / ".git").is_dir() or (workspace / ".git").is_symlink():
        raise PublishError("Invalid checkout")
    origin = local_git(workspace, "config", "--get", "remote.origin.url").decode().strip()
    if origin != "https://github.com/" + REPO + ".git":
        raise PublishError("Unexpected repository origin")
    head = local_git(workspace, "rev-parse", "HEAD").decode().strip()
    if not re.fullmatch("[a-f0-9]{40}", head):
        raise PublishError("Invalid base commit")
    tracked = local_git(workspace, "diff", "--no-renames", "--name-only", "-z", "HEAD")
    untracked = local_git(workspace, "ls-files", "--others", "--exclude-standard", "-z")
    untracked_names = {p for p in untracked.split(b"\0") if p}
    names = {p for p in tracked.split(b"\0") if p} | untracked_names
    if not names or len(names) > MAX_FILES:
        raise PublishError("Expected 1 to 10 changed files")
    files = []
    total = 0
    for encoded in sorted(names):
        try:
            rel = encoded.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PublishError("Invalid filename") from exc
        if (not re.fullmatch(r"[A-Za-z0-9_./-]{1,220}", rel)
                or rel.startswith("/") or any(part in {"", ".", ".."} for part in rel.split("/"))
                or any(part.lower() in FORBIDDEN_COMPONENTS for part in rel.split("/"))
                or any(term in rel.lower() for term in ("token", "password", "secret", "credential", "private_key"))
                or (Path(rel).suffix not in ALLOWED_SUFFIXES
                    and not TEST_MJS_PATH.fullmatch(rel))
                or rel == "WORKER_SMOKE_TEST.txt"):
            raise PublishError("Restricted changed path")
        if TEST_MJS_PATH.fullmatch(rel) and encoded in untracked_names:
            raise PublishError("Only existing tracked JS regression tests may be published")
        path = workspace / rel
        cursor = workspace
        for component in rel.split("/")[:-1]:
            cursor = cursor / component
            if cursor.is_symlink() or not cursor.is_dir():
                raise PublishError("Unsafe parent directory in changed path")
        if not path.is_file() or path.is_symlink():
            raise PublishError("No symlinks or deletions may be published")
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode) or mode & 0o111:
            raise PublishError("Executable and special files are disallowed")
        data = path.read_bytes()
        limit = MAX_FILE_BYTES
        if section_contract is not None and rel == section_contract.get('path'):
            import section_editor
            try:
                original = local_git(workspace, 'show', 'HEAD:' + rel)
                section_editor.verify_task(original, data, section_contract, rel)
            except (section_editor.SectionEditError, KeyError, TypeError):
                raise PublishError('Section baseline or bounds changed') from None
            limit = section_editor.MAX_SOURCE_BYTES
        if not data or len(data) > limit or unsafe_controls(data):
            raise PublishError("Empty, oversized or binary file")
        if any(marker in data for marker in SECRET_MARKERS):
            raise PublishError("Possible credential found in changed file")
        total += len(data)
        if total > (400_000 if section_contract is not None else MAX_TOTAL_BYTES):
            raise PublishError("Total patch exceeds limit")
        files.append((rel, data))
    return head, files


def api(token, method, endpoint, payload=None):
    if not endpoint.startswith("/") or endpoint.startswith("//"):
        raise PublishError("Unexpected API path")
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(ROOT + endpoint, data=body, method=method,
                                 headers={"Authorization": "Bearer " + token,
                                          "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28",
                                          "Content-Type": "application/json",
                                          "User-Agent": "go-aider-restricted-publisher"})
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            raw = response.read(200_000)
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        # Never expose response body, request, token or user-controlled payload.
        raise PublishError("GitHub API refused operation (HTTP " + str(exc.code) + ")") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise PublishError("GitHub API unavailable") from None


def publish(workspace, token, request=api, branch_id=None):
    if not isinstance(token, str) or not token.strip():
        raise PublishError("Missing GitHub token")
    section = None
    section_task = None
    if os.environ.get('WORKER_SECTION_CONTRACT') is not None:
        if not os.environ.get('WORKER_APPROVED_PATHS'):
            raise PublishError('Section publishing requires approved task')
        import queue_worker
        ident = os.environ.get('WORKER_ISSUE_NUMBER', '')
        if not re.fullmatch('[1-9][0-9]{0,9}', ident):
            raise PublishError('Approved task issue missing')
        section_task = queue_worker.parse_task(request(token, 'GET', '/issues/' + ident))
        if section_task and section_task.get('version') == 4:
            if queue_worker.task_fingerprint(section_task) != os.environ.get('WORKER_TASK_SHA256'):
                raise PublishError('Approved GitHub task was revoked or changed')
            section = section_task['section']
    local_head, files = safe_files(workspace, **({'section_contract': section} if section else {}))
    if os.environ.get("WORKER_ISSUE_NUMBER"):
        try:
            allowlist = queue_scope.parse(os.environ.get("WORKER_FILE_ALLOWLIST"))
            dispatch = queue_scope.parse(os.environ.get("WORKER_DISPATCH_SCOPE"))
        except queue_scope.ScopeError:
            raise PublishError("Queued task requires explicit file scope") from None
        if not set(dispatch).issubset(allowlist) or [p for p, _ in files] != sorted(dispatch):
            raise PublishError("Edited files differ from approved task scope")
    approved_raw = os.environ.get("WORKER_APPROVED_PATHS")
    if approved_raw is not None:
        # Multi-file queue tasks are never allowed to use the unrestricted
        # manual publishing pathway. Verify the exact task contract again.
        import queue_worker
        issue_number = os.environ.get("WORKER_ISSUE_NUMBER", "")
        if not issue_number.isdecimal() or len(issue_number) > 10:
            raise PublishError("Approved task issue missing")
        try:
            approved = json.loads(approved_raw)
        except (ValueError, TypeError):
            raise PublishError("Malformed approved paths") from None
        if not isinstance(approved, list) or not all(isinstance(p, str) for p in approved):
            raise PublishError("Invalid approved paths")
        is_pair = len(approved) == 2 and tuple(approved) in queue_worker.SOURCE_TEST_PAIRS
        is_cache = (len(approved) == 4
                    and tuple(approved[:2]) in (queue_worker.SOURCE_TEST_PAIRS | queue_scope.SECTION_SOURCE_TEST_PAIRS if section else queue_worker.SOURCE_TEST_PAIRS)
                    and approved[2:] == ["public/index.html", "public/sw.js"])
        if not (is_pair or is_cache) or [path for path, _ in files] != sorted(approved):
            raise PublishError("Edited files differ from approved task scope")
        if is_cache:
            import cache_contract
            try:
                cache_contract.verify(workspace, approved[0], issue_number)
            except cache_contract.CacheContractError:
                raise PublishError("Cache versions differ from exact expected changes") from None
        for relative in approved:
            local_git(workspace, "ls-files", "--error-unmatch", "--", relative)
        source = request(token, "GET", "/issues/" + issue_number)
        task = queue_worker.parse_task(source)
        if (not task or task.get("version") != (4 if section else 3 if is_cache else 2)
                or task["issue"] != int(issue_number)
                or task["paths"] != approved
                or queue_worker.task_fingerprint(task) != os.environ.get("WORKER_TASK_SHA256")):
            raise PublishError("Approved GitHub task was revoked or changed")
    ref = request(token, "GET", "/git/ref/heads/main")
    base_sha = ref["object"]["sha"]
    if base_sha != local_head:
        raise PublishError("Main moved since checkout; refresh before publishing")
    base = request(token, "GET", "/git/commits/" + base_sha)
    tree = []
    for path, data in files:
        blob = request(token, "POST", "/git/blobs", {
            "content": base64.b64encode(data).decode("ascii"), "encoding": "base64"})
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    new_tree = request(token, "POST", "/git/trees", {
        "base_tree": base["tree"]["sha"], "tree": tree})
    commit = request(token, "POST", "/git/commits", {
        "message": "Worker: isolated proposed changes for review",
        "tree": new_tree["sha"], "parents": [base_sha]})
    source_issue = os.environ.get("WORKER_ISSUE_NUMBER", "")
    if source_issue and (not source_issue.isdecimal() or len(source_issue) > 10):
        raise PublishError("Invalid source issue identifier")
    suffix = branch_id or (queued_branch_suffix(source_issue, base_sha)
                           if source_issue else secrets.token_hex(6))
    branch = "worker/review-" + suffix
    if not re.fullmatch(r"worker/review-[a-f0-9]{12}", branch):
        raise PublishError("Invalid worker branch")
    # A prior ambiguous attempt with this task/base leaves this same branch.
    # The second creation must fail closed (HTTP 422): never spawn a second PR.
    request(token, "POST", "/git/refs", {"ref": "refs/heads/" + branch, "sha": commit["sha"]})
    if approved_raw is not None:
        # Recheck after object/ref creation too: a changed approval must not
        # become a published draft. Ambiguous leftovers require review.
        current = queue_worker.parse_task(request(token, "GET", "/issues/" + issue_number))
        if not current or queue_worker.task_fingerprint(current) != os.environ.get("WORKER_TASK_SHA256"):
            raise PublishError("Approved GitHub task was revoked or changed")
    pr = request(token, "POST", "/pulls", {
        "title": ("Worker: issue #" + source_issue + " draft changes" if source_issue else
                  "Worker: proposed isolated changes (review required)"),
        "head": branch, "base": "main", "draft": True,
        "body": ("Source task: #" + source_issue + "\n\n" if source_issue else "") +
                "Automated isolated worker proposal. Requires human review and CI. No automatic merge or deployment."})
    if not pr.get("draft") or not isinstance(pr.get("number"), int):
        raise PublishError("Draft PR confirmation missing")
    return pr["number"]


def smoke_roundtrip(token, request=api, branch_id=None):
    """Create then close/delete one harmless draft-PR test; never touch main."""
    if not isinstance(token, str) or not token.strip():
        raise PublishError("Missing GitHub token")
    suffix = branch_id or secrets.token_hex(6)
    if not re.fullmatch(r"[a-f0-9]{12}", suffix):
        raise PublishError("Invalid test branch suffix")
    branch = "worker/connectivity-" + suffix
    base_sha = request(token, "GET", "/git/ref/heads/main")["object"]["sha"]
    if not re.fullmatch(r"[a-f0-9]{40}", base_sha):
        raise PublishError("Invalid main SHA")
    base_tree = request(token, "GET", "/git/commits/" + base_sha)["tree"]["sha"]
    # Harmless temporary artifact lives on an isolated branch only.
    path = "docs/worker-connectivity-" + suffix + ".md"
    content = ("# Temporary worker connection test\n\n"
               "This draft PR only validates branch creation, draft PR creation "
               "and cleanup. It must never be merged.\n").encode()
    blob = request(token, "POST", "/git/blobs", {
        "content": base64.b64encode(content).decode("ascii"), "encoding": "base64"})
    tree = request(token, "POST", "/git/trees", {
        "base_tree": base_tree,
        "tree": [{"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]}]})
    commit = request(token, "POST", "/git/commits", {
        "message": "test: temporary worker publishing connection check",
        "tree": tree["sha"], "parents": [base_sha]})
    branch_created = False
    number = None
    try:
        request(token, "POST", "/git/refs", {
            "ref": "refs/heads/" + branch, "sha": commit["sha"]})
        branch_created = True
        pr = request(token, "POST", "/pulls", {
            "title": "test: temporary GO Aider worker publishing check",
            "head": branch, "base": "main", "draft": True,
            "body": "Connectivity test only. Never merge. Will be closed and its branch deleted automatically."})
        if pr.get("draft") is not True or not isinstance(pr.get("number"), int):
            raise PublishError("GitHub did not confirm draft PR")
        number = pr["number"]
    finally:
        if number is not None:
            request(token, "PATCH", "/pulls/" + str(number), {"state": "closed"})
        if branch_created:
            request(token, "DELETE", "/git/refs/heads/" + branch)
    if number is None:
        raise PublishError("Draft PR was not created")
    return number


def main():
    try:
        token = os.environ.get("GITHUB_TOKEN", "")
        if len(sys.argv) == 2 and sys.argv[1] == "--smoke":
            number = smoke_roundtrip(token)
            print("Temporary draft PR #" + str(number) + " created, closed, branch deleted", flush=True)
        elif len(sys.argv) == 1:
            number = publish(Path("/data/workspace/source"), token)
            print("Created draft PR #" + str(number) + "; review required", flush=True)
        else:
            raise PublishError("Unsupported publishing action")
    except (PublishError, OSError, ValueError, KeyError, TypeError) as exc:
        # Only a fixed category, never an exception message or remote response.
        print("WORKER_PUBLISH_DIAGNOSTIC:" + safe_publish_category(exc), flush=True)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
