# GO Field Notes — isolated Aider worker (prototype)

**Not installation-ready. Do not install on Home Assistant or supply real credentials yet.**

This draft add-on contains a **local-only restricted job runner**, not a network service or autonomous issue/PR agent.

## Implemented safeguards
- Container starts as unprivileged user `coder` (UID 10001).
- Idle by default. A local job is attempted only when `RUN_LOCAL_JOB=true`.
- Job must be a JSON file at `/data/job.json` with exactly `action` and `prompt`.
- Allowed actions: `inspect` (git status), `test` (Python unittest discovery), `edit` (Aider with fixed Gemini model).
- Edits require `ENABLE_EDITS=true` and `GEMINI_API_KEY`; no arbitrary shell command input.
- Prompt limit 3,000 characters, file limit 8 KiB, subprocess timeout 300 seconds.
- No Home Assistant/Supervisor/Docker APIs, host networking, privileged access or host config mounts.
- A restricted **public GitHub fetch helper** now exists: exact repository allowlist, no credentials, no working-tree checkout. It is not yet wired into the job runner. No push, PR creation, or deployment functionality.

## Example job format (documentation only)
```json
{"action":"inspect","prompt":""}
```

The job file and a Git checkout must be provisioned in the isolated add-on workspace through a separately designed mechanism. No mechanism exists yet to submit jobs remotely or import repositories safely.

## Remaining release gates
1. Validate Home Assistant add-on metadata, writable `/data` permissions, and full runtime on a disposable instance.
2. Verify Aider's exact CLI/model support and safe subprocess termination; ensure secrets and prompts cannot appear in logs.
3. Integrate and test the public-repository fetch with an isolated working tree. Private repository authentication and GitHub PR creation are not implemented.
4. Run disposable-repository end-to-end tests, including timeout, malformed jobs, read-only defaults and security isolation.
5. Confirm Google AI Studio quotas and user privacy implications.

No installation, merge, or production deployment has been approved. Keep secrets out of GitHub, chats, issues and commits.
