#!/usr/bin/env python3
"""Deterministic, scope-limited cache references for approved source/test tasks.

This is not an HTML or service-worker editor. No Gemini, network or credentials.
Only exact original bytes in two known tracked paths can be transformed.
"""
import os
from pathlib import Path
import re
import subprocess

INDEX = "public/index.html"
SW = "public/sw.js"
MAX_CACHE_BYTES = 100_000
REF_VERSION = re.compile(rb"[0-9]{8}[a-z](?:-[A-Za-z0-9]+)?")
SHELL_NAME = re.compile(rb"gofn-shell-v[0-9]+(?:-[A-Za-z0-9]+)?")

class CacheContractError(Exception):
    pass

def git_base(root, relative):
    env = {"PATH": "/usr/bin:/bin", "HOME": "/home/coder",
           "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_COUNT": "1",
           "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/dev/null"}
    try:
        result = subprocess.run(["git", "show", "HEAD:" + relative], cwd=root,
                                env=env, check=True, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=25)
    except (subprocess.SubprocessError, OSError):
        raise CacheContractError("Baseline cache file unavailable") from None
    if not result.stdout or len(result.stdout) > MAX_CACHE_BYTES or b"\0" in result.stdout:
        raise CacheContractError("Invalid baseline cache file")
    return result.stdout

def safe_target(root, relative):
    target = root / relative
    if (root.is_symlink() or not (root / ".git").is_dir()
            or (root / ".git").is_symlink() or target.is_symlink()
            or not target.parent.is_dir() or target.parent.is_symlink()
            or not target.is_file()):
        raise CacheContractError("Unsafe cache file")
    return target

def expected(root, source, issue):
    if (not isinstance(source, str) or not re.fullmatch(r"public/[A-Za-z0-9-]+\.js", source)
            or not isinstance(issue, str) or not issue.isdecimal()
            or not 1 <= len(issue) <= 10):
        raise CacheContractError("Invalid cache task identity")
    name = source.removeprefix("public/").encode("ascii")
    original_index, original_sw = git_base(root, INDEX), git_base(root, SW)
    # Strictly one versioned script tag (not an arbitrary match elsewhere).
    pattern = re.compile(rb'(<script src="/' + re.escape(name) + rb'\?v=)([0-9]{8}[a-z](?:-[A-Za-z0-9]+)?)("></script>)')
    refs = list(pattern.finditer(original_index))
    if len(refs) != 1:
        raise CacheContractError("Expected one exact index script reference")
    old_version = refs[0].group(2)
    if not REF_VERSION.fullmatch(old_version) or b"-w" in old_version:
        raise CacheContractError("Unrecognised source cache version")
    next_version = old_version + b"-w" + issue.encode("ascii")
    old_ref = b"/" + name + b"?v=" + old_version
    new_ref = b"/" + name + b"?v=" + next_version
    # Require a single exact old reference in SW; never alter other cached assets.
    if original_sw.count(b"'" + old_ref + b"'") != 1:
        raise CacheContractError("SW cache asset reference missing or ambiguous")
    shells = SHELL_NAME.findall(original_sw)
    if len(shells) != 1 or b"-w" in shells[0]:
        raise CacheContractError("Unexpected SW shell cache name")
    sw_updated = original_sw.replace(b"'" + old_ref + b"'", b"'" + new_ref + b"'", 1)
    sw_updated = sw_updated.replace(b"'" + shells[0] + b"'", b"'" + shells[0] + b"-w" + issue.encode() + b"'", 1)
    if sw_updated == original_sw or sw_updated.count(new_ref) != 1:
        raise CacheContractError("SW replacement did not match")
    index_updated = pattern.sub(lambda m: m.group(1) + next_version + m.group(3),
                                original_index, count=1)
    if (index_updated == original_index or len(index_updated) > MAX_CACHE_BYTES
            or len(sw_updated) > MAX_CACHE_BYTES):
        raise CacheContractError("Invalid cache update output")
    return {INDEX: index_updated, SW: sw_updated}

def verify(root, source, issue):
    root = Path(root)
    wanted = expected(root, source, issue)
    for rel, data in wanted.items():
        if safe_target(root, rel).read_bytes() != data:
            raise CacheContractError("Unexpected cache file changes")
    return True

def apply(root, source, issue):
    root = Path(root)
    wanted = expected(root, source, issue)
    for rel in wanted:
        target = safe_target(root, rel)
        if target.read_bytes() != git_base(root, rel):
            raise CacheContractError("Cache file already modified")
    # Atomic replacement per file. A failure leaves a dirty checkout and
    # prevents publication; the next worker task starts with a fresh checkout.
    for rel, data in wanted.items():
        target = safe_target(root, rel)
        temp = target.with_name(".go-worker-cache-" + target.name)
        if temp.exists() or temp.is_symlink():
            raise CacheContractError("Cache staging collision")
        try:
            fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                         getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if target.is_symlink() or not target.is_file():
                raise CacheContractError("Cache target changed")
            os.replace(temp, target)
        finally:
            if temp.exists() and not temp.is_symlink():
                temp.unlink()
    return verify(root, source, issue)
