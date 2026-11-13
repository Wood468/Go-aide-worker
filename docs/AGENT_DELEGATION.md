# Agent delegation v1

GO Aider is a bounded coding executor. Existing project agents can prepare work
for it now; a coordinator approves and dispatches one task at a time, and an
independent reviewer accepts the result. Approval alone does not start an idle
worker. Do not describe a published draft as a completed or deployed feature.

The measurable rollout gate is [milestone 1](https://github.com/Wood468/Go-aide-worker/milestone/1),
tracked in [issue #68](https://github.com/Wood468/Go-aide-worker/issues/68).
At creation, the clean-run count is **0/3**: app #659 produced a useful source
change, but its generated test needed coordinator repair. It is an assisted
result and does not count toward the clean-run target.

## What agents can offload

Good candidates have one behavior change, a small approved source section and
clear negative and positive acceptance cases. Examples include guarding malformed
JSON-backed input in one helper and adding its regression to an existing reviewed
source/test pair. GO Aider can perform the bounded edit and deterministic cache
updates and publish the draft while the project agent continues other work.

Agents retain requirements, architecture, ownership checks and acceptance. Broad
features across arbitrary files, new source/test pairs, generated data rewrites,
dependency changes and deployment work do not fit the current delegation contract.
Do not count saved time until preparation, review and any repair time are measured.

## Handoff

1. **Project agent:** prepare one task using the request below. Check existing
   issues and drafts first; link existing #660 rather than creating a duplicate.
   Leave approvals off. Agents may prepare requests in parallel, but overlapping
   source hashes are finalized only after the preceding integration.
2. **Coordinator:** consult current reservations on [#46](https://github.com/Wood468/Go-aide-worker/issues/46),
   own one dispatch slot, and translate the request to the current strict worker
   task schema. Validate actual main, source hash, unique ordered anchors, exact
   reviewed paths, context/file budgets, deterministic cache updates, approval,
   installed/configured state, ledger and outstanding draft ownership. Do not
   change a hash under an old approval. Only this role changes live worker
   configuration and task approvals under the owner's authorization.
3. **GO Aider:** perform one approved attempt and publish a draft. It cannot
   broaden its own scope, approve itself, merge or deploy.
4. **Independent reviewer:** inspect the exact draft head, execute the actual
   changed function with malformed and valid inputs, verify preserved behavior
   and source/cache byte scope, and check all required latest-head CI. Report
   PASS or BLOCKED against that head. Review evidence does not transfer to a
   different commit.
5. **Coordinator:** record the result and obtain the separately authorized
   integration decision. Resolve the prior draft before dispatching overlapping
   work. Finalize the next task against the actual resulting main.

### Copyable agent request

This is an intake request, **not executable task JSON or approval**. The
coordinator must supply and validate the final schema and source fingerprint.

```text
Delegate to GO Aider — prepare only; do not dispatch

Parent project / issue:
Existing worker task, if any:
One behavior to change:
Why it matters / observed failure:
Proposed existing source path and helper/section:
Proposed existing reviewed test path:
Other required approved cache paths:
Malformed/negative acceptance cases:
Valid behavior and output that must remain unchanged:
Dependencies and other PRs owning these files or hunks:
Reviewer / coordinating workstream:
Expected deliverable: one bounded source/test change and a draft PR.
Stop conditions: stale contract, changed approval, scope/budget mismatch,
existing draft/review hold, ambiguous publication or failed attempt.
```

For the currently reviewed dashboard bundle, the ordered paths are
`public/dash.js`, `test/event-detail-source-confidence.test.mjs`,
`public/app.js`, `public/index.html`, `public/sw.js`. This list grants no new
scope: the installed parser and publisher remain authoritative. The source cap
is 250,000 bytes; source context is at most 40,000 bytes, and the test/reference
combination must also fit its 40,000-byte budget. Passing the v0.5.36 early
lower-bound preflight does not guarantee the actual updated reference fits.

### Result receipt

Record the task/issue and immutable task fingerprint; approved source hash and
base commit; draft URL and exact head; changed paths; CI run IDs and conclusions;
independent review verdict; manual interventions; failure stage/category, if any;
and the coordinator's disposition. Track preparation, dispatch-to-draft and
review/repair time separately. Keep credentials and private model replies out of
the receipt.

On failure, keep the ledger and review hold, report the blocker, and investigate.
Do not replay, reset, relabel or bypass the hold to improve the milestone count.
A coding failure or manual source/test repair restarts the consecutive-success
count; it never erases historical attempts. A cache-only reconciliation caused by
newer main must be disclosed separately and cannot hide generated-code repair.

## Go/no-go gate

Before routine delegation is advertised as proven:

- The handoff document has independent review and is published in main.
- The existing #697/#704 review and integration gates are resolved.
- Three consecutive useful tasks produce independently accepted, CI-green
  source/test drafts with no manual source/test repair. #660 is the first
  candidate after #697 integrates and its contract is refreshed; two further
  useful tasks are selected and scoped when their dependencies are known.
- Prior draft disposition, review holds and duplicate prevention work throughout.
- Evidence includes exact commits, interventions and measured coordinator effort;
  a final report explicitly declares GO or NO-GO.

Passing this gate permits routine **supervised bounded delegation**. It does not
authorize unattended project ownership or automatic source integration. The
watchdog, quarantine and automatic queue-continuity proposals in
[#65](https://github.com/Wood468/Go-aide-worker/issues/65) are a later reliability
milestone; the existing fail-closed hold is acceptable for this supervised gate.
