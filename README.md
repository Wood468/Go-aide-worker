# GO Field Notes — isolated Aider worker (prototype)

**Experimental. Installed in Home Assistant; GitHub publishing is not implemented.**

This draft add-on contains a **local-only restricted job runner**, not a network service or autonomous issue/PR agent.

## GitHub token field (v0.2.1)

Home Assistant's Configuration screen has a masked `GitHub fine-grained token` field. This release **stores and validates the token only**. The launcher deliberately does not pass it to Aider, normal jobs, or subprocess environments. Branch and draft-PR publication exists only in the explicitly gated publish action (v0.3.0). Only store a token scoped to `Wood468/crispy-barnacle`; do not paste tokens into issues, prompts or source files.

## v0.3.0: verification and restricted draft PR publishing

- `verify` checks whether the exact local Gemini smoke-test file was created, without displaying its contents.
- `publish` requires **enable_publishing: true** and a separately stored GitHub token. It does **not** invoke Gemini and exposes that token only to the publishing process.
- Publication accepts at most 10 small UTF-8/text files, rejects symlinks, deletions, executable files, binary files and sensitive paths, and only targets `Wood468/crispy-barnacle`.
- The current checkout commit must match the live `main` SHA. Publication uses the GitHub REST API to create a **new worker branch** and **draft PR** for review. There is no merge or deployment operation.
- The add-on is **not** an unattended job queue. Leave job_action `idle`, enable_edits `false` and enable_publishing `false` except during a controlled job. Credentials do not belong in prompts, files or GitHub.
- The first live edit returned exit code 0 but must still be verified via the explicit `verify` action after this version is installed. Do not publish an unreviewed smoke-test file to the public GO Field Notes repository.
- A GitHub token held in a public repository's add-on configuration does **not** make the user's private Home Assistant settings public; it is protected in Home Assistant, not source-controlled. Protect access to the Home Assistant UI.

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
