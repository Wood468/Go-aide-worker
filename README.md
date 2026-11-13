# GO Field Notes — isolated Aider worker (prototype)

**Experimental. Direct Gemini editing and draft-PR publishing are verified; no automatic merges or deployments.**

This add-on can run an **opt-in GitHub-approved task queue** without exposing an inbound network service or unconstrained shell agent.

For independent overnight coordination and the existing Codex fallback, see
[the result handoff runbook](docs/coordinator-result-handoff.md). The coordinator
must prove result retrieval before allocating work; a queued job is not completion.

## v0.5.35: bind related regression generation to the edited source

Version-4 test generation now receives the actual updated, approved source
section as read-only reference data. This addresses the live pilot where a
correct dashboard source edit was followed by a regression aimed at a different
source file. The queue passes the complete immutable task and captured task
fingerprint to the related-test child, which independently checks the exact
source/test/cache bundle, selected test path and unchanged instruction. Two
supported source files can share a test file; pair membership alone is not task
identity.

The editor reads a credential-free Git HEAD baseline, verifies the original
source hash and unchanged bytes outside the approved section, and supplies only
the updated section, never the large file's unrelated prefix or suffix. Only
the selected test remains editable. Existing test text plus encoded read-only
source reference must fit within 40,000 UTF-8 bytes; oversized or unsafe context
fails before the related-test provider request. Preflight this combined budget
before approval and leave room for the intended section change. The source-edit stage, manual edits and
legacy/v2/v3 tasks retain their previous behavior.

This grounds the model request; it does not prove arbitrary generated tests are
correct. HA still performs syntax checks only. Cloud/GitHub tests, independent
review, draft-only publication, immutable approval checks, needs-review holds
and per-commit coordinator integration remain required. No failure is replayed
or skipped and no new source family or automatic merge authority is granted.

## v0.5.34: reviewed source-anchor rejection isolation

The existing credential-free review action also accepts a one-attempt v4 `edit-source/anchor` failure. These exact replacement-match/overlap rejections occur before the editor's atomic write. Stored full task fingerprint, complete ordered scope, withdrawn approvals, separate review label and complete remote PR/branch audit remain mandatory. Retirement is one-way, retains the original failure and other ledger rows, and never retries a task. Test-stage/publish-stage, unknown, repeated-attempt and missing-hash cases remain blocked. No editing, publishing, credentials, queue-family or merge authority is added.

## v0.5.33: autonomous bounded large-file queue

The new `queue_large_file_loop` action polls approved version-4 section tasks every ten minutes. It runs the existing large-file editor, related regression edit, credential-free syntax checks, deterministic cache synchronization and independent draft publisher without a separate Queue Once invocation for each task. The existing `queue_loop` remains test-only.

Files to edit must contain **one complete ordered bundle**, for example:

```text
public/dash.js,test/event-detail-source-confidence.test.mjs,public/app.js,public/index.html,public/sw.js
```

Other supported bundles are the existing reviewed source/test pairs followed by `public/index.html,public/sw.js`. Partial bundles, mixed pairs, extra files, legacy tasks and version-2/3 source tasks cannot dispatch in this mode. Each issue still requires owner authorship, `go-worker-approved`, and an immutable version-4 contract with the current source SHA-256 and unique bounded section anchors. The worker does not invent task approvals or refresh stale hashes automatically.

The named source may be up to 250,000 bytes; the model receives at most 40,000 bytes of selected context. The publisher proves all bytes outside that section unchanged and rechecks the complete task, file scope, current main and approval. Generated data, arbitrary repository files and whole-file rewrites remain outside this queue's scope. An open draft, interrupted job or needs-review record pauses later dispatch. Failures retain their one-attempt ledger and are never automatically retried or skipped. The worker does not merge or deploy source changes.

To enable after release validation: install with Job `idle` and queue/edit/publish OFF, verify the installed version and read-only `queue_status`, then select `queue_large_file_loop`, the complete file bundle, empty Job prompt, all three opt-ins and the existing configured credentials. Start the add-on and independently verify the fixed large-file queue startup message, durable ledger, exact draft diff and full app CI. An existing open worker draft must be reviewed before another task can run; changing modes does not clear history. To stop, stop the add-on, set Job `idle` and all three opt-ins OFF, then restart. Preserve the ledger and credential values during upgrades and rollback. Offline tests establish software behavior; actual installation and live execution must be verified separately.

## GitHub token field (v0.2.1)

Home Assistant's Configuration screen has a masked `GitHub fine-grained token` field. This release **stores and validates the token only**. The launcher deliberately does not pass it to Aider, normal jobs, or subprocess environments. Branch and draft-PR publication exists only in the explicitly gated publish action (v0.3.0). Only store a token scoped to `Wood468/crispy-barnacle`; do not paste tokens into issues, prompts or source files.

## v0.5.32: exact queued patches and private syntax diagnosis

Queued edits of existing text now always use bounded exact old/new replacements, including small sections and tests, instead of rewriting their entire content. Overlapping matches are ambiguous and rejected. This preserves untouched regex/string bytes without relying on model transcription. Manual small-file behavior is unchanged.

A held version-4 `syntax-check` failure can be independently diagnosed and retired through the existing credential-free review action. It first verifies the exact changed paths, source hash/outside-section bytes, and deterministic cache files. Node parses both original and updated source/test via stdin with no credentials, without executing code. Only fixed role/category values are printed or retained; generated code, raw parser output and identifiers never enter logs. Baselines must parse successfully and at least one updated file must fail with a recognized SyntaxError. Infrastructure errors, scope drift and ambiguous histories remain blocked. Complete remote PR/branch audit and withdrawn approvals/operator review are still mandatory before one-way retirement; no retry or repair is performed.

## v0.5.31: recovery CLI scope

The recovery CLI accepts the complete operator-reviewed source/test/cache scope. Queue Loop still validates test-only paths. An entry-point test proves both routes; reviewed-isolation function tests alone missed this distinction in v0.5.30. No ledger mutation happened in the rejected invocation.

## v0.5.30: typed provider responses and reviewed section-format failures

Gemini requests now include a response schema fixing the exact selected path and either full content or bounded replacements. Client validation remains mandatory; thought parts are excluded from structured output parsing. No malformed-response retry was added.

The credential-free review action additionally accepts a one-attempt version-4 source task failed at `edit-source/format`, which occurs before the editor's atomic write. It requires the retained full task hash, withdrawn edit/merge approvals, separate operator review label, and the complete ordered source/test/cache allowlist. The existing complete PR/branch audit checks every owned path before a one-way retirement retaining the failure details. Test-stage, publish-stage, unknown, quota, repeated-attempt and possibly-published source failures remain blocked. The action cannot retry or call Gemini/GitHub writes. This is independent review, never automatic failure skipping.

Controlled #615 exposed the format failure in v0.5.29 and remains preserved pending that audit. Its replacement must be a separately reviewed attempt after the provider contract fix is installed and checked.

## v0.5.28: reviewed pre-publication service failure isolation

A new credential-free `queue_isolate_reviewed_failure` action replaces per-issue recovery releases for a narrow failure class. It can retire only a one-attempt, existing flat-test task held at `edit-single/service`, before publication. It cannot retry, edit, publish, clear other records or isolate quota/auth/model/source/publish/unknown failures. New task outcomes always retain their immutable task SHA-256; the single historical #599 row has an explicitly pinned compatibility hash.

An independent operator must withdraw both worker approval labels, add `go-worker-failure-reviewed`, inspect the unchanged original contract, and provide the exact structured receipt in Job prompt: `{"issue":599,"path":"test/event-moves.test.mjs","task_sha256":"<64 lowercase hex characters>"}`. Files to edit must contain that same single test path. Run with queue opt-in ON and editing/publishing OFF. The worker receives neither provider credential, verifies owner/labels/hash/state and complete bounded PR/branch history, then retains the hash and failure details in a one-way retired record. Missing API evidence or changed approvals fail closed. Verify via queue_status, restore idle/off and never replay the failed issue.

This is explicit reviewed isolation, not automatic failure skipping or a general ledger reset. A new task is needed for any subsequent implementation. Gemini service availability remains an external dependency.

## v0.5.27: enforce an explicit operator file allowlist for queue jobs

Queue Once and Queue Loop now require the existing `edit_files` setting as an actual allowlist (one to five unique safe paths). Startup and the job runner reject an absent or invalid allowlist before dispatch. The queue filters owner-approved tasks against it before claim; the publisher independently requires both the operator allowlist and exact per-task file scope before any GitHub write. File selection is now supported for queue actions instead of being refused as a manual-edit-only option.

Queue Loop accepts **only existing flat `test/*.test.mjs` paths** in that list, and continues to reject every source/test/cache contract and new documentation task. Queue Once retains the reviewed source/test/cache contracts only when every task path is explicitly allowlisted. Existing issue author, approval, immutable source contract, ledger, CI and draft-only safeguards remain. No new credential authority, automatic retry or source merge permission is introduced.

Example bounded background scope: set Files to edit to `test/event-moves.test.mjs`; only owner-approved tasks for that file can dispatch. Leave all opt-ins OFF during installation. Verify version and read-only ledger first, then run one bounded acceptance. Empty scope fails closed on upgrade; do not treat an accepted options write as proof a job ran.

## v0.5.26: compatible, repeatable deterministic cache updates

The first source/test/cache pilot exposed that worker-suffixed shell names violate the existing application cache contract. The helper now increments the numeric `gofn-shell-vN` revision; application tests remain unchanged. Source URL tokens replace a prior worker suffix rather than rejecting or chaining it, so subsequent tasks on the same file work without code changes. Positive ASCII issue IDs, numeric revision limits, exact baseline matching and independent publisher recomputation remain fail-closed. No broader model access to cache files or merge authority is introduced.

## v0.5.25: reject hidden control bytes before writing or publishing

Independent review of source/test/cache pilot #595 / app draft #596 found literal backspace bytes in a model-generated JavaScript regex (JSON `\\b` escaping was wrong), plus a test using the wrong API. The shared UTF-8 text guard now rejects C0 controls other than tab/newline/carriage return, and DEL, in both the editor before atomic replacement and the independent publisher. Correct escaped regex bytes, Unicode and ordinary whitespace remain unchanged. This guard prevents hidden byte corruption; semantic correctness still requires independent review and full CI. No automatic repair, task retry, scope expansion or source merge permission is added.

## v0.5.24: reconcile the failed source-anchor pilot without retry

Controlled source/test task #594 failed at edit-source/anchor on installed v0.5.23: Gemini returned an old fragment that did not match uniquely. The exact-patch safety check refused it; no PR was created. The raw provider response remains private, so missing versus duplicated text is unconfirmed. Approval was removed.

The credential-free `queue_retire_max_anchor_task` can retire only this exact one-attempt failure, with its original complete contract and stored SHA-256, after deapproval and complete bounded PR/worker-branch checks. It never edits, publishes, retries or clears another record. It shares the existing fully tested #563 audit; that historical action retains its fixed contract and behavior. Install idle/off, run retirement with queue opt-in only, verify the ledger, return idle/off. Any subsequent source pilot must be a NEW issue and include the deterministic cache contract required by current app CI. No source auto-merge is permitted.

## Bounded-section single-run tasks (v0.5.29; issue #30)

Queue Once accepts an owner-approved version-4 contract with exactly `version`, `paths`, `instruction`, and `section`. Paths are an existing source/test pair followed by `public/index.html` and `public/sw.js`. The dashboard pair additionally requires `public/app.js` before those cache files, because app.js dynamically loads dash.js; the worker deterministically synchronizes its loader, the dashboard preload, the app script tag, both SW asset references and the numeric shell revision. The additional pair `public/dash.js` + `test/event-detail-source-confidence.test.mjs` is section-only. The section object binds exactly version 1, path, instruction (equal to the outer instruction), sha256, start and end anchors. The full contract fingerprint is retained and rechecked before publication and after branch creation.

Only the named source may exceed 100 KB, up to 250 KB. Gemini receives at most 40 KB of selected context. The independent publisher reads the original tracked HEAD bytes, verifies the approved hash, and proves every byte outside the section unchanged. Other files keep the 100 KB cap; section-task aggregate content is capped at 400 KB. Test edits and deterministic cache updates are required. Node parses source/test syntax without executing generated code in Home Assistant.

The legacy Queue Loop rejects all versioned source tasks; v0.5.33 adds a separate explicitly scoped `queue_large_file_loop` for version-4 tasks. Section editing cannot be requested by manual Direct Edit options. Editing and publishing require the existing explicit opt-ins and file allowlist. The coding worker creates a draft PR only; production-source auto-merge remains disabled. Failed or ambiguous publication requires review, never automatic replay.

Activation evidence and the controlled live acceptance result are tracked in PR #51 and issue #30. A repository merge alone is not an installed or accepted release.

## v0.5.23: immutable source-task publication contract

Source/test publishers now require a SHA-256 identity of the complete parsed task captured before editing, including its instruction. The isolated publisher compares the currently approved task to that identity before any GitHub write and again before creating the draft PR. Same-path instruction changes, missing fingerprints and revoked approval fail closed. A late revocation can leave an unpublished worker branch; the existing no-retry ledger requires review. No broader file grants or source auto-merge are introduced.

## v0.5.22: ship the deterministic cache contract

The image now includes `cache_contract.py`, which was present in host tests but missing from the container. Version-three source/test tasks previously failed at cache-sync before publication. A manifest regression checks every runtime Python module is shipped, and the credential-free Docker smoke test imports the cache module. No task scope, credentials, merge policy or unattended source permissions change. Install idle with all opt-ins off; a new controlled source/test/cache pilot is still required.

## v0.5.21: reliable, secret-safe one-shot results

The restricted `queue_once` action previously returned exit code zero for both a successful draft submission and an ordinary no-op. Its child stdout was private, leaving Home Assistant unable to distinguish outcomes without a separate ledger query. This caused ambiguous run reports during the verified issue #574 → draft PR #578 pilot.

One-shot runs now print only a **fixed, bounded, allowlisted disposition**: submitted issue/PR numbers, no eligible task, paused for review, or unknown (inspect durable ledger). The parent reads at most the final 4,096 bytes of private queue output, never echoes raw GitHub responses, Gemini output, task text or credentials, and retains fixed-category error diagnostics. The outcome line is informational **only**; it cannot authorise another attempt, mark a task completed, merge or deploy. Continue independently verifying GitHub PR state and the durable queue ledger after any submission.

This release adds regression coverage for safe status-only parsing, secrets exclusion, no-op and repeated/ambiguous output. It does not change queue scope, authentication, approval rules or automatic merging; app PR #578 remains governed independently by GitHub Actions and full CI.

## v0.5.20: authenticated queue polling and clean unattended shutdown

After the successful source task #565 and the first independent automatic-merge policy release, the next owner-approved task #574 was held **before claiming** with the fixed `github-rate-limit` diagnostic. Inspection verified the underlying queue's `github_read` used public **unauthenticated** GitHub REST calls despite `GITHUB_TOKEN` already being available for publishing; the 10-minute unattended loop and other agents easily exhausted GitHub's low anonymous limit. A controlled stop also exposed a Supervisor `SIGTERM` exit-143 warning because the idle queue did not handle shutdown.

**Fix:** the queue uses its existing configured GitHub token for repository-limited GitHub GET requests, with a fixed API version and strict URL allowlist. An unattended queue refuses missing credentials instead of silently falling back to anonymous reads. Read-only `queue_status` and credential-free one-off retirement continue to work without tokens. The token is **never** logged, included in exception diagnostics, or sent to Gemini editor children. Invalid token/auth gets a fixed `github-auth` diagnostic; real quota exhaustion remains `github-rate-limit`. No new credentials, API permissions, file-edit scopes, or automatic retry mechanisms are introduced.

The long-lived queue installs a minimal `SIGTERM` handler. An idle loop now exits normally and immediately when Home Assistant stops it; if a bounded task is active it finishes/records its single attempt before exiting, avoiding a deliberate arbitrary ledger reset or replay.

**Rollout:** inspect/review full worker PR and pass static/Python + Docker CI, then upgrade HA add-on to v0.5.20 with **all opt-ins OFF**. Verify historical #550/#563 retired and #565 submitted. The still-open app task #574 has both `go-worker-approved` and `go-worker-auto-merge-approved` labels, but remains unclaimed unless a current worker ledger shows otherwise. Check fresh worker/app main and PRs first, then execute exactly one `queue_once` acceptance with its existing credentials; inspect resulting draft and app CI plus independent GitHub policy. On any ambiguous failure, stop/hold for review without replay. Only after completing one accepted cycle, restore the restricted queue loop. Leave actual source/edit permissions and broader auto-merge disabled.

## v0.5.19: exact one-way retirement of failed DST regression #563

The first new legacy one-file unattended issue after the v0.5.18 recovery, app #563, failed at `edit-single` with the allowlisted Gemini `service` category. The worker correctly entered durable `needs-review`, stopped new dispatch and did not publish a PR. **Do not reapprove/retry #563.**

This version adds exactly one credential-free `queue_retire_rotation_service_task` action. It requires the single historical #563 ledger record to match `needs-review`, attempt count one, `edit-single/service`; verifies the issue creator, fixed title, deapproval and **full original** `test/event-encounter-rotations.test.mjs` task instruction; checks bounded complete post-issue PR history, including associated worker PR changed-file lists; and scans all live worker/review branches. Any possibly newer worker branch is compared **against its merge-base with main across all commits**, rather than trusting only the branch-tip commit's changed files. API errors, unexpected permissions, incomplete pagination, uncertain branch metadata or an ambiguous related branch/PR block retirement. No edit, credential, publishing, GitHub write, automatic replay or general ledger clear is exposed.

**Rollout gate:** confirm full latest-head static/Python and Docker CI, reviewed exact diff, and merge. Install version 0.5.19 with queue/edit/publishing disabled; verify #563 is still deapproved. Run this one-time action ONCE with `enable_queue:true`, `enable_edits:false`, `enable_publishing:false` and no prompt. Confirm log success, then use the credential-free `queue_status` action to prove `#563: retired` while `#550: retired` and all unrelated entries remain unchanged. Restore `idle` and queue/edit/publish OFF. A later pilot MUST use a NEW issue and a nonoverlapping tracked flat test file; no retry of failed task #563.

## v0.5.18: safe publish diagnostics and one-time #550 recovery

A failed publisher subprocess now reports only a static allowlisted failure category (for example `stale-main`, `github-conflict`, `github-permission` or `unknown`). The durable queue ledger and credential-free `queue_status` retain the category alongside the failure stage; raw GitHub errors, model output, URLs and credentials are never persisted or displayed. Historical #550 remains an unclassified `needs-review` publishing failure; the original cause cannot be reconstructed from suppressed logs.

Queue-origin publishing uses a deterministic branch suffix based on the issue number and checked-out main SHA. A failed attempt that already created its branch cannot silently create a second branch or draft PR for the same issue/base. A duplicate remote ref fails closed and requires manual reconciliation; the worker still does not retry publishing or merge automatically.

The credential-free `queue_retire_places_publish_task` action can retire **only** deapproved app issue #550 with its exact owner/title/flat test path and a single historical `publish` failure. Before touching its one ledger record, it reviews bounded complete post-issue PR history and every still-live worker/review branch's changed paths to exclude a previously published PR or ambiguous unpublished `test/places.test.mjs` modification. Any uncertainty refuses retirement. It cannot run Gemini, edit code, publish, clear other records, or retry #550.

**Rollout:** inspect and pass latest-head Python/static and Docker CI, review the full diff, merge the worker PR, then install v0.5.18 with queue/edit/publishing OFF. Confirm the live task is still deapproved. Run the one-time retirement using `job_action: queue_retire_places_publish_task`, `enable_queue: true`, and edit/publish **false**; verify `#550: retired` using `queue_status`; return immediately to idle/all flags OFF. Only then assign a **new, non-overlapping, legacy one-file** test issue for a controlled unattended queue acceptance. Do not replay #550 or enable unattended multi-file tasks.

## v0.5.17: bounded model-service retry and guarded two-file retirement

On v0.5.16, controlled two-file source+test [pilot #531](https://github.com/Wood468/crispy-barnacle/issues/531) was claimed and reached `edit-source`, then failed with the durable fixed category `service`. No draft PR was created. Editing and publication were disabled and the original approval removed. This category indicates an HTTP service failure, **not** quota/auth/safety, but the exact upstream HTTP status was intentionally not recorded.

`direct_editor.generate` now makes **at most one immediate reattempt after a one-second pause** only for Gemini HTTP 500, 502, 503 or 504 responses. A second service failure stops, fails closed and puts the issue in `needs-review` as before. HTTP 429 quota, auth 401/403, model 400/404, timeouts, malformed JSON, invalid model output, unsafe changes, symlink and size errors are **never retried**. Both requests use the same previously validated input without touching a checkout file until valid structured JSON passes safety and exact-patch checks. This is a transport retry within one approved job, **not** a second task execution or a way to bypass the ledger.

A new credential-free `queue_retire_two_file_service_task` action retires **only** exactly deapproved issue #531 after verifying owner/title, v2 two-file contract, one failed `edit-source` attempt with category `service`, and complete post-issue PR history proving no worker draft was published. Other ledger entries remain unchanged. This action cannot edit, publish, retry, clear the queue or access credentials.

Rollout requires reviewed latest-head static and Docker CI, installation with queue/edit/publish OFF, one-time issue-specific retirement, read-only ledger confirmation, then a new distinct pilot on a nonoverlapping pair of app files. Do not replay #531 or permit unattended multi-file source edits before an end-to-end accepted source+test+cache pilot.

## v0.5.16: safe diagnostics for pre-claim queue failures

The second controlled four-file acceptance task [#528](https://github.com/Wood468/crispy-barnacle/issues/528) exited with code 2 before creating any draft PR **or even a ledger entry**. This means there is no evidence that source editing or Gemini was reached. A new one-way, fixed-category output mechanism distinguishes issues with the public GitHub read path, API quota, invalid issue list or ledger, and concurrent queue locks. The subprocess's output is held privately by the job runner, and only a single exact allowlisted `WORKER_QUEUE_DIAGNOSTIC:<category>` marker can reach Home Assistant logs. Arbitrary output, prompts, API payloads, exception contents, and credentials are not logged.

The ten-minute unattended loop also now skips expensive historical submitted-PR API reads entirely when there are zero approved open tasks, reducing unauthenticated public GitHub API quota usage without ever dispatching additional work. Submitted/failed ledger records are preserved and reviewed whenever a new eligible task appears.

This update changes neither GitHub credentials distribution nor worker editing permissions, owner/path validation, retries, automatic merge, or source/test/cache scope. It adds offline safety tests for rate-limit handling, exact category mapping and sanitised parent log behaviour.

Rollout: require latest-head static+Docker CI, inspect full security/diff scope, merge, and install v0.5.16 idle/off. Preserve issue #528 as deapproved and never retry blindly. Run a separately reviewed, new controlled pilot only after reviewing the preclaim diagnostic and open PR/file conflict state. If GitHub reports rate limiting, prefer lower polling/fewer read calls over unreviewed changes to GitHub-token exposure.

## v0.5.15: controlled single-file error categorisation and task retirement

Live unattended single-file task #516 stopped at `edit-single` with no draft PR. Its exact failure was unknowable because the restricted editor child correctly discarded untrusted output. Version 0.5.15 diagnoses subsequent failures **only** from the editor's exact predefined, known-safe status line; the queue maps that line to a fixed category (`quota`, `auth`, `model`, `service`, `network`, `response-size`, `format`, `no-change`, `anchor`, `safety`, `timeout` or `unknown`). Any unrecognised response, stderr, exception text, filename, token or model output is discarded. A finite category is recorded alongside the existing stage and visible via `queue_status`; there is **no** live token/model response logging or hidden automatic retry. A failed task continues to block all dispatch until review.

The one-time `queue_retire_banner_cache_task` action permits retirement **only** of the exact deapproved [app task #516](https://github.com/Wood468/crispy-barnacle/issues/516), after verifying owner, full title, original one-file path, only the two original machine-readable fields, one failed `edit-single` attempt, and complete bounded post-issue GitHub PR history excluding prior publication. It receives no Gemini/GitHub credentials or editing/publishing authority. On success, #516 transitions from `needs-review` to `retired`; unrelated ledger entries are preserved, and no retry or task duplication is permitted.

Rollout: require latest-head static and Docker CI; inspect security diff, merge, install v0.5.15 with idle/edit/publishing/queue OFF; run the retirement once; verify `queue_status`. Then create a fresh, differently numbered, non-overlapping single-file regression assignment for an explicitly approved one-shot pilot. If it succeeds, inspect the draft PR and CI before restoring guarded unattended polling. If it fails, use the static category to fix the confirmed cause rather than expanding model privileges or using repeated retries.

## v0.5.14: safe unattended polling for proven single-file tasks

`queue_loop` is now strictly restricted to approved legacy **one-file** jobs (new `docs/worker-task-*.md` notes or exactly one existing tracked flat `test/*.test.mjs`). The loop **ignores** version 2/3 source+test/cache contracts even when labelled `go-worker-approved`. These larger jobs remain available only under deliberately triggered `queue_once` after explicit scope and conflict checks. This preserves the proven v0.5.10 two-file edit and v0.5.11 four-file implementation without prematurely allowing unverified unattended production-source edits.

The loop checks the issue owner, exact approved task JSON, strict path, durable no-retry ledger and pending draft review gate; makes at most one draft PR before blocking until review; runs as non-root with restricted Gemini and GitHub token subprocess separation, never merges or deploys, and polls every ~600 seconds. If idle with no eligible tasks it waits safely. Stop with `job_action: idle`, `enable_queue:false`, `enable_edits:false`, `enable_publishing:false` and restart. Keep automated app source changes gated until a new explicit v3 pilot passes required full CI. Check HA startup logs for the exact `legacy single-file tasks only` message and first-poll queue evidence; installation or configured options alone are not proof polling is operational.

## v0.5.13: safe queue failure-stage diagnostics

A first bounded version-three acceptance task #505 failed before creating any draft PR and left a durable `needs-review` record, as designed. The pre-v0.5.13 worker suppressed child output, leaving no safe reason to diagnose. The queue now records **only** a fixed stage identifier (`refresh`, `target-validation`, `approval-precheck`, `edit-source`, `edit-test`, `cache-sync`, `syntax-check`, `diff-validation`, `approval-postcheck`, `publish`) on failed tasks, and read-only `queue_status` reports this identifier. Never capture prompts, API/model responses, paths, keys, exception messages or the raw environment. A static failure-stage cannot authorise retries or publishing.

One credential-free, owner-issue-specific `queue_retire_four_file_pilot` action retires **only** failed issue #505 after checking its precise approved v3 four-file contract, withdrawn approval, one recorded failed attempt and complete bounded post-issue GitHub PR history showing no matching published PR. Other ledger records are preserved. No automatic retry or generic reset is available.

To diagnose next, merge only after latest-head Python+Docker CI and full diff review. Install v0.5.13 idle/off. Run `queue_retire_four_file_pilot` once with queue opt-in true, editing and publishing false; verify `#505: retired`, return idle/off. Create a new disposable owner-approved four-file task with a new number and run **one** `queue_once`. On failure, inspect the fixed stage in `queue_status`, do not retry; fix the confirmed stage before advancing. Enable unattended `queue_loop` only when the new task creates a correctly scoped draft with required app checks green and release lanes nonoverlapping.

## v0.5.12: fail-closed retirement for deapproved #475

The controlled v0.5.11 four-file queue pilot #505 was selected as the only approved task but a historical `needs-review` ledger entry for deapproved issue #475 blocked all new work. This release adds exactly one **credential-free** `queue_retire_venue_clock_task` action. With `enable_queue: true`, `enable_edits: false` and `enable_publishing: false`, the worker verifies #475's owner, title, flat test path, withdrawn approval, 1–2 failed attempts and complete bounded post-issue PR history with no matching worker PR. It only changes #475 from `needs-review` to `retired` preserving attempt count. No reapproval, retry, general ledger wipe, source edit or PR publication occurs. All other ledger entries remain unchanged. A separate new issue is required for any future venue-clock work.

Rollout: inspect latest-head static/Docker CI, merge reviewed PR, update HA add-on, run the one-time retirement action, confirm via `queue_status` that #475 is `retired` and all other records are unchanged, and restore Idle/off. Then reapprove only disposable #505 for a separate bounded `queue_once`, review all four paths and required app CI, close pilot without merging, and restore normal approvals only after safety review. Never enable unattended `queue_loop` before that pilot succeeds.

## v0.5.11: reviewed source/test plus deterministic browser-cache sync

The version-three owner-approved queue task format adds **exactly two deterministic cache files** to the two existing tracked source/test paths. Only four fixed source/test pairs are eligible. Unlike unrestricted multi-file editing, **Gemini never sees or modifies `public/index.html` or `public/sw.js`**. The credential-free cache contract updates the exact versioned source URL in those files, and changes the isolated service-worker shell cache name, preserving every unrelated byte. Any missing/ambiguous reference, altered baseline, symlink, unexpected path, invalid token, missing approval or current-main drift fails closed. The publisher independently recomputes the complete expected cache changes from the tracked Git baseline and requires precisely four changed files, followed by a fresh check of the approved issue and base commit. The legacy v1 and v2 task formats remain unchanged.

Example single-line task marker (following an approved owner-created issue):

```text
GO_WORKER_TASK_JSON:{"version":3,"paths":["public/today.js","test/today-logic.test.mjs","public/index.html","public/sw.js"],"instruction":"Make one small related change in the source and its regression test only; cache references are updated by a separate deterministic process."}
```

The four allowlisted tuples are `public/today.js` with `test/today-logic.test.mjs`, `public/event-components.js` with `test/event-components-crowned-alias.test.mjs`, `public/detail.js` with `test/event-detail-source-confidence.test.mjs`, and `public/app.js` with `test/universal-event.test.mjs`; each adds `public/index.html` and `public/sw.js` in that precise order. The worker checks JS syntax but does not execute model-produced code in Home Assistant. It publishes **draft PRs only** after complete per-file checks. GitHub Actions owns actual tests. Approval is task-specific; the queue loop must remain disabled until the exact 4-file acceptance pilot and full app CI are reviewed.

## v0.5.10: one-way retirement of failed two-file acceptance task #497

The first reviewed v0.5.8 two-file acceptance task #497 failed with exit code 2 before any confirmed draft PR. Its approval label is removed. Following v0.5.9's independently successful *manual* edits of both 20 KB Today source and the related test, the durable ledger still correctly holds `#497: needs-review`. This release adds **exactly one** credential-free `queue_retire_two_file_pilot` action, which can transition **only** #497's first failed attempt to `retired`, never to a retryable state.

The one-time action requires queue opt-in `enable_queue:true` with editing and publishing OFF. It checks #497's owner, exact title and versioned source/test paths, withdrawn approval, durable ledger first attempt, and complete bounded post-issue GitHub PR history showing no #497 worker draft. Any missing data or matching PR refuses the transition. Other queue records are preserved. It creates no PR, edits no file, and has no Gemini/GitHub token access.

**Rollout:** review complete worker diff and latest-head static/Docker CI; install and keep Idle/off. Explicitly run only `queue_retire_two_file_pilot`, inspect `queue_status` for `#497: retired` and all prior records unchanged, restore Idle/off. A future two-file pilot must use a *new owner-approved issue*, never #497. Do not enable unattended `queue_loop` until that new pilot produces a reviewed draft PR with app CI.

## v0.5.9: reliable medium-file patch response (pilot feedback)

A guarded v0.5.8 two-file acceptance task #497 failed on its **first** run (no confirmed draft PR), and a separate manual `direct_edit` of the ~20 KB `public/today.js` file reproduced exit code 2 with no GitHub publication. Previously a ~51 KB exact-patch manual edit had succeeded. The editor now selects its bounded unique-anchor replacement protocol for files **above 12 KB** (previously above 40 KB), avoiding a large full-file response for medium-sized source and test files. The same 1–3 replacement, unique anchor, 24,000-character touch, 100 KB final-size and secret checks remain. Offline regressions cover this medium-sized boundary.

**Do not automatically replay #497** or clear its `needs-review` ledger entry. Its approval label has been removed, and the older approved tasks #475/#476/#485 were restored. Keep live queue/edit/publish OFF until a separately reviewed one-time reconciliation and a new pilot; add-on installation alone never authorizes a retry.

## v0.5.8: guarded version-two source + regression test (experimental)

Owner-approved `[GO-WORKER]` issues can now specify **exactly two existing tracked files**, in a versioned `GO_WORKER_TASK_JSON:` object. The source must be one of four fixed, reviewed source-to-test pairs; no arbitrary source paths or new files are permitted:

- `public/event-components.js` + `test/event-components-crowned-alias.test.mjs`
- `public/today.js` + `test/today-logic.test.mjs`
- `public/detail.js` + `test/event-detail-source-confidence.test.mjs`
- `public/app.js` + `test/universal-event.test.mjs`

Example issue machine line (one line only):

```text
GO_WORKER_TASK_JSON:{"version":2,"paths":["public/event-components.js","test/event-components-crowned-alias.test.mjs"],"instruction":"Make one small documented source change and add a closely related regression assertion without altering unrelated behaviour."}
```

Source and test are edited sequentially using the pre-existing **one-file Gemini editor**. The worker checks exact file scope, verified tracked paths, UTF-8 limits (100 KB per file), and credential-free `node --check` on both files; it never executes generated tests. Before editing and before publishing, the queue re-reads the exact owner-approved issue and refuses changed/revoked contracts. The independent GitHub-token-only publisher repeats approval checks and requires **both files, no extras**, a current base SHA, and draft-only publication. A failed second edit or changed approval becomes `needs-review`; no partial PR is published and the next checkout refresh is clean. Existing legacy single-file tasks are unchanged.

**Rollout:** do not enable unattended `queue_loop` just because the code is installed. Review worker CI and complete diff first, install v0.5.8, verify idle/off, and use a single harmless approved two-file pilot (`queue_once`) only after checking unrelated PRs, already-submitted worker PRs and ledger. Inspect draft scope and app CI before considering more tasks. Emergency stop: set `job_action: idle`, `enable_queue:false`, `enable_edits:false`, `enable_publishing:false`, and restart the add-on. Do not merge or deploy from the worker.

## v0.5.7: one-way exhausted artwork task retirement (review first)

After two failed attempts of app task #472, the equivalent test-only coverage was separately delivered by merged app PR #488. A new **explicit** `queue_retire_artwork_task` action permits retiring only ledger entry #472 without another Gemini call, test execution, queue retry, PR publication, or GitHub mutation. It requires `enable_queue:true`, editing and publishing **both disabled**, and no job prompt. The credential-free handler must verify the recorded second failure, owner-authored issue and its withdrawn approval, exact task path, merged fallback PR #488 with one changed test file, and exhaustive bounded GitHub PR history showing no previous worker PR for #472. Any uncertainty fails closed and leaves the ledger unchanged. A retired entry cannot be dispatched/recovered.

**Install gate:** inspect full PR diff and latest-head CI first. Preserve idle/off throughout. Then use the one-time action, inspect read-only `queue_status` for #472 `retired` while existing submitted tasks stay unchanged, and immediately restore idle/off. No blanket ledger reset or automatic retry is introduced. Only after this retirement should an independently reviewed >40 KB v0.5.6 editor pilot be considered.

## v0.5.6: bounded larger-file Gemini edits (40–100 KB, experimental)

The direct editor can edit existing UTF-8 files up to **100,000 bytes**, the current draft publisher's per-file limit. Files up to 40 KB retain the existing full-file response protocol. For larger files Gemini must return **1–3 exact-match `old`/`new` replacement blocks**; each original block must occur exactly once, the blocks may not overlap, each old/new block is capped at 12,000 characters, and their combined lengths at 24,000 characters. The editor applies all replacements against fixed original offsets, preserving untouched bytes (including CRLF), and fails closed on malformed output, wrong path, ambiguous anchors, excessive changes, secrets, and size violations. Large model requests may use more tokens and can fail without changing the checkout.

**This does not broaden queue permissions.** The unattended worker still accepts one existing flat `test/*.test.mjs` file or one new approved `docs/worker-task-*.md`; source files, multiple paths, auto-retries, code execution under HA credentials and merge/deployment remain blocked. The independent two-file source+test proposal is issue #23.

**Rollout gate:** review CI and full PR diff, reconcile #472 under v0.5.5 safeguards, and keep the Home Assistant add-on Idle with queue/edit/publish OFF until a deliberately authorised one-task >40 KB pilot can be run and reviewed. Do not activate unattended operation on the strength of offline tests alone.

## v0.5.5: route artwork recovery to the credential-free queue handler

The v0.5.4 one-time action was accepted by the options schema but not included in the dispatch set, so Home Assistant handed it to the ordinary job runner and it **failed closed** before changing the ledger. v0.5.5 adds the missing handler membership, and a full launcher integration regression verifies the actual unprivileged `queue_worker.py recover_artwork_task` target, the absence of job staging, and that no Gemini/GitHub credentials are forwarded.

Keep the live worker **Idle**, all flags OFF, until v0.5.5 is installed and these tests pass. Then explicitly run the one-time review action only if #472 still meets the existing safeguards and no worker PR has been published. A rejected recovery must not be automatically retried.

## v0.5.4: one reviewed recovery for artwork test #472

This version introduces a **single explicitly approved recovery action** for a failed, owner-approved #472 test-only task targeting existing `test/variant-art.test.mjs`. It reuses the exhaustive bounded post-issue GitHub PR history audit, issue author/label/path checks, and strictly enforced one-retry ceiling from the original pilots. **No general ledger reset exists.** It cannot run if the prior entry is not exactly the first `needs-review` attempt, if any matching draft PR exists, or if approval changed.

To review and recover: inspect `queue_status` and the GitHub issue/PR history. Keep queue editing and publishing **OFF**. Explicitly enable queue, choose `queue_recover_artwork_task`, restart, inspect `queue_status` for `approved-pilot-retry`, then set Idle. Only after that decision, deliberately enable queue/edit/publish and run one `queue_once` job; inspect the resulting draft PR and full GitHub CI before any next job. Disable all flags and return Idle immediately after. A second failure cannot be recovered a third time; do not enable continuous queue until the worker's single-file tests and ledger are reviewed. No automated merges or deployments.

## v0.5.3: acknowledge the already-merged approved code pilot

The owner-approved #465 test-only worker task produced draft PR #467, which was reviewed, passed required CI and was squash-merged into the app. Its local queue ledger remained `needs-review`, which correctly blocks future automatic jobs until reconciled.

The one-off `queue_ack_code_pilot` action checks **exactly** closed owner-approved issue #465 and merged review PR #467 against the GitHub API (including task path, author, PR source, merge metadata and worker branch). It may only transition the two-attempt `needs-review` entry to `submitted` for PR #467; it never retries the task, edits a file, creates a PR or merges. No general ledger reset is available. To use, keep edits/publishing OFF, select `enable_queue: true` and action `queue_ack_code_pilot`, restart, inspect `queue_status`, then return to Idle/off. Only activate continuous `queue_loop` after complete CI verification and confirming no unreviewed draft PR remains.

## v0.5.2: exact reviewed code-pilot recovery

The first owner-approved single-file regression pilot #465 was safely held `needs-review` after v0.5.0 publishing rejected an otherwise allowed `.test.mjs` path. v0.5.1 resolves that publisher mismatch. New `queue_recover_code_pilot` is a **one-time, explicit** HA action, requiring `enable_queue:true` with editing/publishing OFF; it inspects exact owner-approved issue #465 and every recent PR since its creation, refuses any previous published draft, refuses multiple recoveries and never edits or publishes. Then a separately authorised `queue_once` performs the second and final pilot attempt. Failures remain review-only and no third attempt is possible. Keep continuous `queue_loop` disabled until the new draft's diff and CI are reviewed.

## v0.5.1: restricted JS publishing and reboot-safe startup

- Closed pilot issue #462 produced a single reviewed documentation draft PR #464, which was closed without merging. The live v0.4.3 `queue_loop` started and polled with no other approved tasks.
- The v0.5.0 test-editor extension previously allowed only `test/*.test.mjs` edits, but the publisher rejected all `.mjs` paths. Publishing now accepts **only existing, tracked, flat `test/*.test.mjs` files**, not arbitrary, nested, or newly created `.mjs` files. Offline Git fixtures verify both permitted and forbidden cases.
- Home Assistant add-on declares `boot: auto` so it can start after HAOS reboot. This does not turn on editing or publishing by itself: defaults remain `job_action: idle`, `enable_queue: false`, `enable_edits: false`, `enable_publishing: false`. Persisted operator-selected queue-loop flags are an explicit opt-in.
- Pause immediately through Home Assistant Configuration: set Job to Idle, turn off all three queue/edit/publish flags, then restart the add-on.
- A full HAOS reboot has **not** been performed for acceptance; test it when safe for other home automations.

## v0.5.0: one approved JavaScript regression-test file per task

The queue retains its GitHub-issued `[GO-WORKER]` title, owner, approval-label, single-task and persistent-ledger checks. In addition to the documentation pilot, an approved task may now name exactly one **existing, flat** `test/*.test.mjs` file in the public app repository. No `src/`, `scripts/`, `.github/`, new executable files, multiple paths or application assets are allowed.

After a fresh checkout and one-file Gemini edit, the queue runs `node --check` on that exact file **without any Gemini or GitHub credentials**. It intentionally does **not execute** generated JavaScript inside Home Assistant; potentially adversarial test code must not share the parent's process privilege or secret access. Only a draft PR is published; GitHub Actions then performs the complete JavaScript, Python, browser and WebKit suites, with human review/merge required. Failing syntax, changed-file scope or remote-head checks stop and preserve a needs-review record, with no automated retry.

Rollout: review all worker security/CI checks, install, manually run one approved synthetic test-only task and inspect its draft PR, ensure main is unchanged and the ledger stops repeats, then activate `queue_loop` only if the live test passes. Emergency pause remains Home Assistant Idle plus all flags disabled.

## v0.4.3: exhaustive bounded pilot PR audit

Before allowing a single reviewed retry of #462, inspect **all** pages of up to 1,000 recent app pull requests. A repository with 100+ PRs no longer fails merely because the first page is full; recovery still fails closed on an incomplete/malformed history or any previously published pilot PR. This remains a one-off opt-in operation, not an automatic retry. Queue execution/edit/publish defaults stay disabled.

## v0.4.2: queue status and one reviewed pilot recovery

`queue_status` is a read-only local ledger summary in Supervisor logs; it never needs edit/publish permissions or credentials. After fixing the first-run editor handoff in v0.4.1, `queue_recover_pilot` is an explicitly opted-in **single recovery for issue #462 only**, allowed solely if the persisted task is failed/interrupted on its first attempt, the approved issue is unchanged, and GitHub recent PRs contain no published draft for that issue. The recovery action never edits code or publishes. It records a one-time retry authorisation and refuses a third attempt. Run `queue_once` separately after reviewing status and GitHub.

To recover the initial known v0.4.0 pilot failure: inspect `queue_status` first; if #462 is `needs-review`, check no draft exists, deliberately set `enable_queue: true` with edits/publishing **off**, run `queue_recover_pilot`, then return all options to Idle/off. Only later execute a single `queue_once` pilot with normal permissions and inspect its draft PR. Do not enable `queue_loop` until the pilot passes. A task that already submitted a draft is never recovered.

## v0.4.1: first queue task path correction

The queue and direct editor now agree on the restricted `/data/workspace/job.json` handoff for queue children, while manual edits continue reading `/data/job.json`. A fresh queue can create its first approved job file atomically (private `0600`) without requiring a pre-existing placeholder. Negative tests cover manual/queue separation and missing, oversized or symlinked requests. Keep the add-on Idle with the queue, edit and publishing toggles disabled until the v0.4.1 pilot passes. No automatic merges or deployments are introduced.

## v0.4.0: approved task queue (limited pilot)

This version includes an **opt-in** long-running queue that polls public GitHub Issues at most every 10 minutes. Queue Loop requires `enable_queue`, `enable_edits` and `enable_publishing` to be true and both keys saved. Queue Scan is read-only and does not require credentials.

The queue requires an open issue in `Wood468/crispy-barnacle` **authored by `Wood468`**, with title beginning `[GO-WORKER] `, the `go-worker-approved` label and exactly one machine-readable line in its body, e.g.:

    GO_WORKER_TASK_JSON:{"path":"docs/worker-task-example.md","instruction":"Write a brief documentation note explaining a specific verified regression and its acceptance criteria."}

The initial pilot **only creates new `docs/worker-task-*.md` files**. It does not edit application code, execute generated code, merge, deploy or make arbitrary GitHub issue changes. It refreshes the checkout, edits one approved file with direct Gemini, confirms scope using the publisher's existing checks, and publishes a **draft PR**. It records the issue and PR number in a durable ledger and waits while the PR is open. Uncertain or interrupted tasks require manual inspection rather than being retried automatically.

To stop: switch Job to `idle`, turn **Enable approved task queue** off, and restart the add-on. The pilot retains `boot: manual` until a controlled live queue test passes; enable restart-on-boot only after that acceptance test.

### Delegation boundary

The main ChatGPT lead creates narrowly scoped issues and explicitly adds `go-worker-approved` only after checking overlap with current PRs, scope, and safety. The worker owns no merge or deployment authority. The automation must be reviewed and its pilot scope broadened only after real queue acceptance tests.

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
