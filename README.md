# GO Field Notes — isolated Aider worker (prototype)

**Not installation-ready. Do not install on Home Assistant or supply real credentials yet.**

This draft add-on contains a **local-only restricted job runner**, not a network service or autonomous issue/PR agent.

## Implemented safeguards
- Container starts as unprivileged user `coder` (UID 10001).
- Idle by default. A local job is attempted only when `RUN_LOCAL_JOB=true`.
- Job must be a JSON file at `/data/job.json` with exactly `action` and `prompt`.
- Allowed actions: `prepare` (fetch/check out one configured public GitHub repository), `inspect` (git status), `test` (Python unittest discovery), `edit` (Aider with fixed Gemini model).
- Edits require `ENABLE_EDITS=true` and `GEMINI_API_KEY`; no arbitrary shell command input.
- Prompt limit 3,000 characters, file limit 8 KiB, subprocess timeout 300 seconds.
- No Home Assistant/Supervisor/Docker APIs, host networking, privileged access or host config mounts.
- A restricted **public GitHub preparation helper** is invoked by an explicit `prepare` job. It enforces an exact repository allowlist and refuses symlinks, submodules and reserved Git paths before writing the working tree. No credentials, push, PR creation or deployment functionality.

## Example job format (documentation only)
```json
{"action":"inspect","prompt":""}
```

Before a `prepare` job, configure `WORKER_PUBLIC_REPOSITORY=Wood468/crispy-barnacle` and `ALLOWED_PUBLIC_REPOSITORIES=Wood468/crispy-barnacle` in the restricted environment. Submit `{"action":"prepare","prompt":""}` to the local job file. The container invokes its allowlisted public GitHub checkout into `/data/workspace/source`. Subsequent inspect/test/edit jobs require a separate job-file reset; there is **no automatic job queue**. No remote submission exists. The GO Field Notes source repository is public, so reading it requires no GitHub token; creating branches or PRs is a separate, unimplemented permission step. This is not yet wired to Home Assistant add-on options.

## Remaining release gates
1. Validate Home Assistant add-on metadata, writable `/data` permissions, and full runtime on a disposable instance.
2. Verify Aider's exact CLI/model support and safe subprocess termination; ensure secrets and prompts cannot appear in logs.
3. Verify public-repository preparation end-to-end on disposable infrastructure, including non-root access to Home Assistant's persistent `/data` mount. Private repository authentication and GitHub PR creation are not implemented.
4. Run disposable-repository end-to-end tests in the actual add-on container, including timeout, malformed jobs, read-only defaults and security isolation.
5. Confirm Google AI Studio quotas and user privacy implications.

No installation, merge, or production deployment has been approved. Keep secrets out of GitHub, chats, issues and commits.
