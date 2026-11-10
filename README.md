# GO Field Notes — isolated Aider worker (prototype)

**Experimental. Manual draft-PR publishing was tested in Home Assistant. The approved queue is a new opt-in feature and has not been activated or installed until its runtime is verified.**

This draft add-on contains a **local-only restricted job runner**, not a network service or autonomous issue/PR agent.

## v0.3.8 — approved GitHub task queue (disabled by default)

`queue/manifest.json` on the worker repository's reviewed `main` branch is the only task authorisation source. It starts with `paused: true` and zero tasks. An approved task selects exactly one `test/*.mjs` or `test/*.py` file in public `Wood468/crispy-barnacle` and contains a bounded prompt. Queue mode is never started implicitly: select `job_action: queue` and enable both editing and publishing deliberately **only after this version passes its isolated tests and Home Assistant runtime verification**.

When enabled, the queue checks the public manifest every five minutes, holds one process lock, claims a task in a persistent /data/workspace ledger before starting, refreshes a clean checkout, uses Gemini to edit only the authorised file, runs a credential-free test, and uses a **separate GitHub-only subprocess** to open a draft PR. A task ID appears in the draft PR, and completed/failed tasks are terminal in the local ledger (interrupted jobs are not retried). Failures attempt a non-sensitive issue #428 comment if the scoped token permits; the local ledger and private Supervisor logs are always available.

**Emergency pause:** set `paused: true` in the reviewed manifest (halts new work on the next poll), or immediately set `job_action: idle`, turn both permissions off, and restart/stop the add-on. No queue task can merge, deploy or update app main directly. Never provide secrets in tasks; the runner excludes GitHub credentials from Gemini and test subprocesses and excludes Gemini credentials from publisher subprocesses.

**Initial rollout gate:** offline test suite green, confirm disabled default, validate a harmless approved task on a disposable checkout, verify publisher rejection/CI and absence of main writes, then separately activate the queue if desired. v0.3.7 does not know this mode and stays manual/Idle until upgraded.

## GitHub token field (v0.2.1)

Home Assistant's Configuration screen has a masked `GitHub fine-grained token` field. This release **stores and validates the token only**. The launcher deliberately does not pass it to Aider, normal jobs, or subprocess environments. Branch and draft-PR publication exists only in the explicitly gated publish action (v0.3.0). Only store a token scoped to `Wood468/crispy-barnacle`; do not paste tokens into issues, prompts or source files.

## v0.3.7: refresh the isolated public checkout

An explicitly requested `refresh` job clones current public `Wood468/crispy-barnacle` into an isolated temporary directory, swaps the checkout only on success, and deletes the prior local worker checkout. This removes stale and unreviewed local worker-only edits; it never modifies GitHub. It receives no Gemini key or GitHub token. Use before preparing a new independent task so draft-PR publication is based on current main.

## v0.3.6: GitHub draft-PR publishing roundtrip test

The explicitly opted-in `publish_test` action validates GitHub access by creating one temporary branch and draft PR based on the latest `main`. It then closes the PR and deletes the temporary branch. No change is merged into main. The test does not read the worker's edited checkout, invoke Gemini or publish any local files. Its GitHub token is only passed to the isolated publisher. Check GitHub after testing to confirm PR closure and branch deletion. Publishing remains disabled by default.

## v0.3.2: verify actual target-file edits

- If `edit_files` names a new file in an existing checkout directory, safely pre-create an empty regular file so Aider can add it to the editing context.
- Record hashes of explicitly selected files before and after the Aider run. A zero-exit result is now considered a failure unless at least one intended file's content changed.
- Temporary empty placeholders cannot be published by the draft-PR publisher.
- Aider may create its own ignored files; Git working-tree change counts alone are insufficient proof of a useful edit.
- Editing is still a manual, single-job action. No unattended jobs, automatic merges or production deployments are enabled.

## v0.3.1: no-op edit diagnostics

The first Gemini/Aider edit job returned process exit code 0 but the verification task found no matching smoke file. Aider works best when given explicit editable file paths; the add-on now has a safe, optional comma-separated `edit_files` field (up to 5 paths), passed as `--file` arguments during an Edit job only. This release keeps raw model/provider output private in an unlinked temporary file, emits only a fixed diagnostic category and changed-path count, and treats a zero-change exit code as failure. The `verify` task distinguishes a missing file from one with incorrect contents. Do not infer a successful code contribution from process exit code alone.

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
