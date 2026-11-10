#!/usr/bin/env python3
"""Publish a small reviewed-by-policy working-tree diff to a new DRAFT PR only.

This module is deliberately separate from Aider. The GitHub token is available
to this process alone for the explicitly opted-in publish action.
"""
import base64
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

REPO = "Wood468/crispy-barnacle"
ROOT = "https://api.github.com/repos/" + REPO
ALLOWED_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".css",
                    ".html", ".md", ".yaml", ".yml", ".txt"}
FORBIDDEN_COMPONENTS = {".git", ".github", ".env", "secrets", "credentials", ".ssh"}
MAX_FILES = 10
MAX_FILE_BYTES = 100_000
MAX_TOTAL_BYTES = 250_000
SECRET_MARKERS = (b"-----BEGIN PRIVATE KEY-----", b"github_pat_", b"ghp_", b"gho_", "AIza".encode("utf-8"))


class PublishError(Exception):
    pass


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


def safe_files(workspace):
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
    names = {p for p in (tracked + untracked).split(b"\0") if p}
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
                or Path(rel).suffix not in ALLOWED_SUFFIXES
                or rel == "WORKER_SMOKE_TEST.txt"):
            raise PublishError("Restricted changed path")
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
        if not data or len(data) > MAX_FILE_BYTES or b"\0" in data:
            raise PublishError("Empty, oversized or binary file")
        if any(marker in data for marker in SECRET_MARKERS):
            raise PublishError("Possible credential found in changed file")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
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
    local_head, files = safe_files(workspace)
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
    branch = "worker/review-" + (branch_id or secrets.token_hex(6))
    if not re.fullmatch(r"worker/review-[a-f0-9]{12}", branch):
        raise PublishError("Invalid worker branch")
    request(token, "POST", "/git/refs", {"ref": "refs/heads/" + branch, "sha": commit["sha"]})
    pr = request(token, "POST", "/pulls", {
        "title": "Worker: proposed isolated changes (review required)",
        "head": branch, "base": "main", "draft": True,
        "body": "Automated isolated worker proposal. Requires human review and CI. No automatic merge or deployment."})
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
        # External exception strings could contain request information.
        print("Publishing rejected: " + (str(exc) if isinstance(exc, PublishError) else type(exc).__name__),
              flush=True)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
