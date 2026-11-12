# Unattended coordinator result handoff

The coding worker prepares a bounded patch or draft. The independent coordinator
owns scope selection, result retrieval, credential-free tests, review and each
eligible integration. These instructions do not grant new edit or merge scopes.

## Check capabilities before allocation

Inspect the live task ledger, current main, complete open-PR file ownership and
the coordinator reservation on worker issue #46. Resume an existing allocation
before selecting another. Never reset or replay retired or needs-review rows.

Discover the working Home Assistant tools by description, not just the old
connector prefix. A missing dedicated Codex tool does not mean its documented
HA service is unavailable. Use `ha_list_services(domain="codex_cli",
detail_level="full")` and the generic `ha_call_service`:

```json
{
  "domain": "codex_cli",
  "service": "get_task",
  "data": {"task_id": "<the existing recorded task ID>"},
  "return_response": true,
  "wait": false
}
```

The task is in `service_response.task`. Read `status`, `answer_required`,
`permission_required`, `completed_at`, `summary` and public `details`. Do not
dump credentials, unrelated tasks, private connector URLs or the entire response
into a public issue. A connector returning 404 is unavailable; use a separately
working authorized connection, not guessed endpoints or alternate credentials.

Before a new `start_task`, prove both `list_tasks` and retrieval of an existing
owned result. Record the returned task ID immediately on its issue. If the start
outcome is unknown, reconcile the saved task list before any further start.
Do not allocate work through a connection that cannot return its result.

## Read-only receipt bridge

`automation.go_aider_codex_result_receipt` is a native HA automation for the
explicitly assigned task, separate from the engineering schedule. It has no
timer or coding/publishing action:

1. An exact `codex_cli_task_result` event filter matches the assigned task ID.
2. `codex_cli.get_task` reads that same literal task ID into `codex_result`.
3. `worker_receipt` exposes task identity, repository/issue/path, completion,
   public summary/details, original details length and a truncation flag.

For each new allocation, use the managed automation config API with its current
`config_hash`. Bind the trigger and service-call task IDs together and update the
receipt's repository/issue/path. Never widen the collector to unrelated tasks.
Run its actions once after binding: a fast task may finish before registration.
This backfills the saved result without replaying the implementation task.

Use `ha_get_automation_traces(automation_id="automation.go_aider_codex_result_receipt")`
to list runs, then the returned `run_id` with `sections="actions,error"` and
`deduplicate=false`. Read `worker_receipt` from action variables. Verify the task,
repository, issue and path against the allocation; require completed status,
`details_truncated=false` and complete public diff/hash evidence. Inspect errors;
a stopped trace alone is not proof of a result. Twenty traces are retained, so
copy the reviewed public receipt onto its GitHub task immediately. Traces are a
transport fallback, not the durable task ledger.

The bridge only reads results and can be disabled independently. It never
starts/replies to/retries a task, runs generated code, publishes or merges.

## Finish the result in the same workstream

Persist the public patch, exact base, original/new SHA-256 and byte-prefix proof
on the owning issue. Never claim worker-reported hashes independently verified.
Apply only after checking current ownership and the original bytes. Review the
diff and assertions, then run focused tests in a credential-free cloud checkout
and full latest-head CI. HA executors may syntax-parse only; they must not run
model-generated tests with saved HA credentials. If no cloud is available, use
a draft PR and GitHub CI. No generated tests run in HA as a substitute.

Distinguish commit statuses from check runs: Cloudflare Workers Build evidence
is at `/commits/<sha>/check-runs`; an empty `/status` list does not prove failure.
Skipped or path-filtered checks are not passing checks. Check the exact merge
SHA's build and post-merge checks separately from PR CI.

Each low-risk integration requires independent review, actual complete required
CI and a specific expected-head coordinator decision. The coding executor stays
patch/draft-only. Do not add an automatic-merge approval label to evade a denied
delegation. Preserve issue/ledger history and explicit ownership handoffs.

## Stop silent stalls

For an existing task, try its durable GitHub receipt, the working documented HA
service and the exact-task receipt trace before declaring the result missing.
If every authorized route is unavailable, record the concrete error, affected
task and next recovery action as blocked and notify the owner once. Do not keep
reporting an urgent capability blocker as normal idle or allocate a duplicate.
An unallocated, nonconflicting task may use an authorized cloud/GitHub executor
instead; an ambiguous existing allocation cannot be silently reimplemented.

A successful Gemini probe proves current connectivity only. Re-enable its queue
only for a fresh eligible exact scope with no active owner/draft or ledger hold.
Retired tasks remain retired. One provider outage must not become an unbounded
replacement-issue loop. Report active, completed, blocked and genuinely idle
states accurately; scheduled coordination is not continuous execution.
