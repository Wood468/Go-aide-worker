#!/usr/bin/env python3
"""One-file Gemini editor in isolated checkout. No commands, GitHub access or logs of content."""
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.error
import urllib.request

import job_runner

MODEL_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent"
MAX_INPUT = 40_000
MAX_OUTPUT = 100_000


class DirectEditError(Exception):
    pass


def generate(key, prompt, path, original, opener=urllib.request.urlopen):
    if not isinstance(key, str) or not key.strip():
        raise DirectEditError("Gemini key missing")
    if len(prompt) > 3000 or not prompt.strip():
        raise DirectEditError("Invalid editing instruction")
    if len(original) > MAX_INPUT:
        raise DirectEditError("Selected file is too large")
    task = (
        "You are changing exactly one file in a temporary source checkout. "
        "Return only a valid JSON object with keys path and content. "
        "Path must exactly equal the provided path. Content must be the entire "
        "replacement file contents. Do not include Markdown fences. "
        "Do not write secrets, credentials, instructions to deploy, or unrelated changes.\n"
        "PATH: " + path + "\n"
        "CURRENT FILE:\n" + original + "\n"
        "REQUEST:\n" + prompt
    )
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": task}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                             "maxOutputTokens": 8192}
    }).encode()
    req = urllib.request.Request(MODEL_URL, data=body, method="POST", headers={
        "x-goog-api-key": key, "Content-Type": "application/json",
        "User-Agent": "go-aider-single-file-editor"})
    try:
        with opener(req, timeout=45) as response:
            if response.status != 200:
                raise DirectEditError("Gemini request failed")
            result = json.loads(response.read(MAX_OUTPUT + 1))
    except urllib.error.HTTPError as e:
        category = ("auth" if e.code in (401, 403) else
                    "quota" if e.code == 429 else
                    "model" if e.code in (400, 404) else "service")
        raise DirectEditError("Gemini request rejected: " + category) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise DirectEditError("Gemini connection or response error") from None
    try:
        candidate = result["candidates"][0]["content"]["parts"][0]["text"]
        updated = json.loads(candidate)
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise DirectEditError("Model returned invalid structured edit") from None
    if (not isinstance(updated, dict) or set(updated) != {"path", "content"}
            or updated["path"] != path or not isinstance(updated["content"], str)):
        raise DirectEditError("Model returned unapproved path or content")
    return updated["content"]


def edit(workspace, selected, prompt, key, generator=generate):
    workspace = Path(workspace)
    if workspace.is_symlink() or not workspace.is_dir() or (workspace / ".git").is_symlink() or not (workspace / ".git").is_dir():
        raise DirectEditError("Invalid isolated checkout")
    if selected != job_runner.selected_edit_files(workspace) or len(selected) != 1:
        raise DirectEditError("Exactly one validated edit target required")
    relative = selected[0]
    target = workspace / relative
    if target.is_symlink() or not target.parent.is_dir() or target.parent.is_symlink():
        raise DirectEditError("Unsafe edit destination")
    if not target.exists():
        target.touch(mode=0o600, exist_ok=False)
    if not target.is_file() or target.stat().st_size > MAX_INPUT:
        raise DirectEditError("Selected file is invalid or oversized")
    try:
        original = target.read_text(encoding="utf-8")
    except UnicodeError:
        raise DirectEditError("Only UTF-8 text files are supported") from None
    updated = generator(key, prompt, relative, original)
    if not isinstance(updated, str) or not updated or updated == original:
        raise DirectEditError("No content change produced")
    raw = updated.encode("utf-8")
    if len(raw) > MAX_INPUT or b"\0" in raw or any(marker in raw for marker in
                (b"github_pat_", b"ghp_", b"-----BEGIN PRIVATE KEY-----")):
        raise DirectEditError("Edited file failed content safety checks")
    # Atomic replacement of a regular file; never run generated code.
    with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent, prefix=".worker-edit-",
                                     delete=False) as tmp:
        staged = Path(tmp.name)
        tmp.write(raw)
        tmp.flush()
        os.fsync(tmp.fileno())
    try:
        if target.is_symlink() or not target.is_file():
            raise DirectEditError("Edit destination changed")
        os.chmod(staged, 0o600)
        os.replace(staged, target)
    finally:
        if staged.exists():
            staged.unlink()
    return True


# Queue edits are staged under the unprivileged, persistent workspace.
# Manual Home Assistant edits continue to read the root-staged /data/job.json.
# This fixed alternative is available only to the explicit queue editor child.
QUEUE_JOB = Path("/data/workspace/job.json")


def selected_job_file():
    candidate = QUEUE_JOB if os.environ.get("GO_WORKER_QUEUE_EDIT") == "1" else job_runner.JOB_FILE
    if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size > 8192:
        raise DirectEditError("Missing, unsafe or oversized edit job")
    return candidate


def main():
    try:
        selection = job_runner.selected_edit_files(job_runner.WORKSPACE)
        with selected_job_file().open(encoding="utf-8") as inp:
            job = json.load(inp)
        if not isinstance(job, dict) or job.get("action") != "direct_edit":
            raise DirectEditError("Unexpected edit job")
        edit(job_runner.WORKSPACE, selection, job.get("prompt", ""), os.environ.get("GEMINI_API_KEY", ""))
        print("Direct Gemini selected-file edit completed", flush=True)
    except (DirectEditError, job_runner.JobError, ValueError, OSError, TypeError) as e:
        # These errors are controlled, but never expose arbitrary exception content.
        print("Direct Gemini edit rejected: " +
              (str(e) if isinstance(e, DirectEditError) else type(e).__name__), flush=True)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
