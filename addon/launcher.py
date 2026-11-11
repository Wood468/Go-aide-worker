#!/usr/bin/env python3
"""Home Assistant options bridge: privileged initialization, then one-way UID drop.

No API listener. GitHub token is passed only to an explicitly opted-in publish job. Gemini credentials are passed only to
explicitly enabled edit jobs, never written into the job file or logs.
"""
import json
import os
import re
from pathlib import Path
import sys

OPTIONS = Path("/data/options.json")
WORKSPACE = Path("/data/workspace")
JOB = Path("/data/job.json")
UID = 10001
ACTION_SET = {"idle", "prepare", "refresh", "inspect", "test", "edit", "verify", "publish", "probe", "direct_edit", "publish_test", "queue_scan", "queue_once", "queue_loop", "queue_status", "queue_recover_pilot", "queue_recover_code_pilot", "queue_ack_code_pilot", "queue_recover_artwork_task", "queue_retire_artwork_task"}
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
    edit_files = raw.get("edit_files", "")
    enabled = raw.get("enable_edits", False)
    publish_enabled = raw.get("enable_publishing", False)
    queue_enabled = raw.get("enable_queue", False)
    api_key = raw.get("gemini_api_key", "")
    github_token = raw.get("github_token", "")
    if not isinstance(action, str) or action not in ACTION_SET:
        raise ConfigurationError("Unsupported action")
    if not isinstance(prompt, str) or len(prompt) > 3000:
        raise ConfigurationError("Invalid prompt")
    if not isinstance(edit_files, str) or len(edit_files) > 500:
        raise ConfigurationError("Invalid edit file selection")
    selected = [x.strip() for x in edit_files.split(",") if x.strip()]
    if len(selected) > 5 or len(selected) != len(set(selected)):
        raise ConfigurationError("Too many or duplicate edit files")
    for path in selected:
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,180}", path)
                or any(x.startswith(".") or x in {"", ".", ".."} for x in path.split("/"))
                or any("token" in x.lower() or "secret" in x.lower() for x in path.split("/"))):
            raise ConfigurationError("Unsafe edit file path")
    if action not in {"edit", "direct_edit"} and selected:
        raise ConfigurationError("File selection is only available for edit jobs")
    if not isinstance(enabled, bool) or not isinstance(publish_enabled, bool) or not isinstance(queue_enabled, bool):
        raise ConfigurationError("Enable flags must be boolean")
    if not isinstance(api_key, str) or not isinstance(github_token, str):
        raise ConfigurationError("Invalid key configuration")
    if action in {"queue_ack_code_pilot", "queue_recover_artwork_task", "queue_retire_artwork_task"} and (enabled or publish_enabled):
        raise ConfigurationError("Merged pilot acknowledgement requires editing and publishing disabled")
    if action in {"queue_recover_pilot", "queue_recover_code_pilot", "queue_ack_code_pilot", "queue_recover_artwork_task", "queue_retire_artwork_task"} and not queue_enabled:
        raise ConfigurationError("Pilot recovery requires explicit queue opt-in")
    if action in {"queue_once", "queue_loop"} and (not queue_enabled or not enabled or not publish_enabled or not api_key.strip() or not github_token.strip()):
        raise ConfigurationError("Queue requires all three opt-ins and both credentials")
    if action == "probe" and not api_key.strip():
        raise ConfigurationError("Probe requires Gemini key")
    if action in {"edit", "direct_edit"} and (not enabled or not prompt.strip() or not api_key.strip()):
        raise ConfigurationError("Edit requires enabled mode, prompt and Gemini key")
    if action in {"publish", "publish_test"} and (not publish_enabled or not github_token.strip()):
        raise ConfigurationError("Publishing requires explicit opt-in and GitHub token")
    if action in {"idle", "prepare", "refresh", "inspect", "test", "verify", "publish", "publish_test", "probe", "queue_scan", "queue_once", "queue_loop", "queue_ack_code_pilot", "queue_recover_artwork_task", "queue_retire_artwork_task"} and prompt.strip():
        raise ConfigurationError("Only edit jobs accept a prompt")
    return action, prompt, enabled, api_key, publish_enabled, github_token, ",".join(selected)


def protect_options(path=OPTIONS):
    """Keep raw Home Assistant credentials unreadable by the coder user."""
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError("Unsafe options file")
    os.chown(path, 0, 0)
    os.chmod(path, 0o600)
    info = path.stat()
    if info.st_uid != 0 or (info.st_mode & 0o077) != 0:
        raise ConfigurationError("Options file is not root-private")


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
    action, prompt, enabled, api_key, publish_enabled, github_token, edit_files = read_options()
    protect_options()
    prepare_directory()
    if action not in {"idle", "queue_status", "queue_recover_pilot", "queue_recover_code_pilot", "queue_ack_code_pilot", "queue_recover_artwork_task", "queue_retire_artwork_task"}:
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
    if action in {"edit", "direct_edit"}:
        environment["ENABLE_EDITS"] = "true"
        environment["GEMINI_API_KEY"] = api_key
        environment["EDIT_FILES"] = edit_files
    if action == "probe":
        environment["GEMINI_API_KEY"] = api_key
    if action in {"queue_once", "queue_loop"}:
        environment["GEMINI_API_KEY"] = api_key
        environment["GITHUB_TOKEN"] = github_token
        environment["ENABLE_QUEUE"] = "true"
    if action in {"publish", "publish_test"}:
        environment["ENABLE_PUBLISHING"] = "true"
        environment["GITHUB_TOKEN"] = github_token
    os.setgroups([])
    os.setgid(UID)
    os.setuid(UID)
    if action in {"queue_status", "queue_recover_pilot", "queue_recover_code_pilot", "queue_ack_code_pilot", "queue_recover_artwork_task", "queue_retire_artwork_task"}:
        # Read-only status or one tightly scoped ledger recovery. Neither
        # receives Gemini/GitHub secrets or any edit/publish permission.
        import preflight
        if preflight.check() != 0:
            raise ConfigurationError("Preflight check failed")
        mode = {"queue_status": "status", "queue_recover_pilot": "recover_pilot",
                "queue_recover_code_pilot": "recover_code_pilot",
                "queue_ack_code_pilot": "ack_code_pilot",
                "queue_recover_artwork_task": "recover_artwork_task",
                "queue_retire_artwork_task": "retire_artwork_task"}[action]
        os.execve(sys.executable, [sys.executable, "/opt/worker/queue_worker.py", mode],
                  environment)
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
