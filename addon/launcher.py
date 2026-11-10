#!/usr/bin/env python3
"""Home Assistant options bridge: privileged initialization, then one-way UID drop.

No API listener. GitHub token stays in protected options; publishing is disabled. Gemini credentials are passed only to
explicitly enabled edit jobs, never written into the job file or logs.
"""
import json
import os
from pathlib import Path
import sys

OPTIONS = Path("/data/options.json")
WORKSPACE = Path("/data/workspace")
JOB = Path("/data/job.json")
UID = 10001
ACTION_SET = {"idle", "prepare", "inspect", "test", "edit"}
REPOSITORY = "Wood468/crispy-barnacle"


class ConfigurationError(Exception):
    pass


def read_options(path=OPTIONS):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
        raise ConfigurationError("Missing, symlinked, or oversized options")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigurationError("Options must be a JSON object")
    action = raw.get("job_action", "idle")
    prompt = raw.get("job_prompt", "")
    enabled = raw.get("enable_edits", False)
    api_key = raw.get("gemini_api_key", "")
    github_token = raw.get("github_token", "")
    if not isinstance(action, str) or action not in ACTION_SET:
        raise ConfigurationError("Unsupported action")
    if not isinstance(prompt, str) or len(prompt) > 3000:
        raise ConfigurationError("Invalid prompt")
    if not isinstance(enabled, bool):
        raise ConfigurationError("enable_edits must be boolean")
    if not isinstance(api_key, str) or not isinstance(github_token, str):
        raise ConfigurationError("Invalid key configuration")
    if action == "edit" and (not enabled or not prompt.strip() or not api_key.strip()):
        raise ConfigurationError("Edit requires enabled mode, prompt and Gemini key")
    if action in {"idle", "prepare", "inspect", "test"} and prompt.strip():
        raise ConfigurationError("Only edit jobs accept a prompt")
    return action, prompt, enabled, api_key


def prepare_directory(path=WORKSPACE):
    if path.is_symlink():
        raise ConfigurationError("Unsafe workspace symlink")
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    if not path.is_dir():
        raise ConfigurationError("Workspace path is not a directory")
    os.chown(path, UID, UID)
    os.chmod(path, 0o700)


def stage_job(action, prompt, path=JOB):
    if path.is_symlink():
        raise ConfigurationError("Unsafe job file symlink")
    if path.exists() and not path.is_file():
        raise ConfigurationError("Job path is not a regular file")
    payload = json.dumps({"action": action, "prompt": prompt}).encode("utf-8")
    if len(payload) > 8192:
        raise ConfigurationError("Job file too large")
    # Write through a private temporary file; replace only an ordinary job file.
    temporary = path.with_name(".job-next.json")
    if temporary.is_symlink() or temporary.exists():
        raise ConfigurationError("Temporary job file already exists")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        os.chown(temporary, UID, UID)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main():
    if os.geteuid() != 0:
        raise ConfigurationError("Bootstrap requires root solely for UID drop and storage setup")
    action, prompt, enabled, api_key = read_options()
    prepare_directory()
    if action != "idle":
        stage_job(action, prompt)
    # Set only the environment needed by the unprivileged worker.
    environment = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": "/home/coder",
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if action == "prepare":
        environment["WORKER_PUBLIC_REPOSITORY"] = REPOSITORY
        environment["ALLOWED_PUBLIC_REPOSITORIES"] = REPOSITORY
    if action == "edit":
        environment["ENABLE_EDITS"] = "true"
        environment["GEMINI_API_KEY"] = api_key
    os.setgroups([])
    os.setgid(UID)
    os.setuid(UID)
    if action == "idle":
        print("GO Aider Worker idle; no job scheduled", flush=True)
        os.execve("/usr/bin/sleep", ["sleep", "infinity"], environment)
    import preflight
    if preflight.check() != 0:
        raise ConfigurationError("Preflight check failed")
    os.execve(sys.executable, [sys.executable, "/opt/worker/job_runner.py"], environment)


if __name__ == "__main__":
    try:
        main()
    except ConfigurationError as exc:
        # Only static, controlled validation messages; never print raw options or keys.
        print(f"Worker configuration rejected: {exc}", file=sys.stderr)
        sys.exit(2)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        # Do not log exception contents: paths and external messages may be sensitive.
        print(f"Worker startup failed: {type(exc).__name__}", file=sys.stderr)
        sys.exit(2)
