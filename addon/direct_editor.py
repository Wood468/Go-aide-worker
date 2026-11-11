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
MAX_INPUT = 40_000  # Small files retain the existing complete-replacement response.
MAX_FILE_BYTES = 100_000  # Never exceed the independent publisher's per-file cap.
MAX_OUTPUT = 100_000
MAX_REPLACEMENTS = 3
MAX_REPLACEMENT_CHARS = 12_000
MAX_TOUCHED_CHARS = 24_000


class DirectEditError(Exception):
    pass


def apply_replacements(original, replacements):
    """Replace only uniquely matched, nonoverlapping substrings of the original.

    All offsets refer to the immutable original, so a replacement cannot
    silently change the anchor used by another replacement.
    """
    if not isinstance(replacements, list) or not 1 <= len(replacements) <= MAX_REPLACEMENTS:
        raise DirectEditError("Invalid large-file replacement count")
    ranges = []
    touched = 0
    for item in replacements:
        if (not isinstance(item, dict) or set(item) != {"old", "new"}
                or not isinstance(item["old"], str) or not isinstance(item["new"], str)):
            raise DirectEditError("Invalid large-file replacement")
        old, new = item["old"], item["new"]
        if (len(old) < 12 or not old.strip() or old == new
                or len(old) > MAX_REPLACEMENT_CHARS
                or len(new) > MAX_REPLACEMENT_CHARS):
            raise DirectEditError("Large-file replacement exceeds scope")
        if original.count(old) != 1:
            raise DirectEditError("Large-file replacement anchor is not unique")
        pos = original.index(old)
        ranges.append((pos, pos + len(old), new))
        touched += len(old) + len(new)
    if touched > MAX_TOUCHED_CHARS:
        raise DirectEditError("Large-file edits exceed bounded scope")
    ordered = sorted(ranges)
    if any(a[0] < b[1] for b, a in zip(ordered, ordered[1:])):
        raise DirectEditError("Overlapping large-file replacements")
    updated = original
    for start, end, new in reversed(ordered):
        updated = updated[:start] + new + updated[end:]
    return updated


def generate(key, prompt, path, original, opener=urllib.request.urlopen):
    if not isinstance(key, str) or not key.strip():
        raise DirectEditError("Gemini key missing")
    if len(prompt) > 3000 or not prompt.strip():
        raise DirectEditError("Invalid editing instruction")
    size = len(original.encode("utf-8"))
    if size > MAX_FILE_BYTES:
        raise DirectEditError("Selected file is too large")
    large = size > MAX_INPUT
    instruction = (
        'Return only a JSON object with keys "path" and "replacements". '
        '"replacements" must be an array of 1 to 3 objects with exactly the '
        'keys "old" and "new". Every "old" must copy an exact, unique, '
        'contiguous substring of CURRENT FILE, including original whitespace. '
        'Return ONLY the changed fragments, not the whole file. '
        'Each old/new fragment must be at most 12000 characters and the sum '
        'of old/new lengths at most 24000. Never edit unrelated sections. '
        if large else
        'Return only a valid JSON object with keys "path" and "content". '
        '"content" must be the entire replacement file. '
    )
    task = (
        "You are changing exactly one file in a temporary source checkout. "
        + instruction +
        "Path must exactly equal the provided path. Do not include Markdown fences. "
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
            raw_result = response.read(MAX_OUTPUT + 1)
            if len(raw_result) > MAX_OUTPUT:
                raise DirectEditError("Gemini response exceeds limit")
            result = json.loads(raw_result)
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
    if not isinstance(updated, dict) or updated.get("path") != path:
        raise DirectEditError("Model returned unapproved path or content")
    if large:
        if set(updated) != {"path", "replacements"}:
            raise DirectEditError("Model returned invalid large-file response")
        return apply_replacements(original, updated["replacements"])
    if set(updated) != {"path", "content"} or not isinstance(updated["content"], str):
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
    if not target.is_file() or target.stat().st_size > MAX_FILE_BYTES:
        raise DirectEditError("Selected file is invalid or oversized")
    try:
        # Keep CRLF exactly as checked out. Path.read_text() normalises newlines
        # and would otherwise rewrite unrelated bytes in a large-file patch.
        with target.open("r", encoding="utf-8", newline="") as source:
            original = source.read()
    except UnicodeError:
        raise DirectEditError("Only UTF-8 text files are supported") from None
    updated = generator(key, prompt, relative, original)
    if not isinstance(updated, str) or not updated or updated == original:
        raise DirectEditError("No content change produced")
    raw = updated.encode("utf-8")
    if len(raw) > MAX_FILE_BYTES or b"\0" in raw or any(marker in raw for marker in
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
