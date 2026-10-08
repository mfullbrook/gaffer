---
title: Local Coding Supervisor Specification
created: 2026-10-08
updated: 2026-10-08
status: proposed
version: 0.1
owner: Mark Fullbrook
tags:
  - coding-supervisor
  - linear
  - github
  - agent-orchestration
---

# Local Coding Supervisor Specification

> Updated: 2026-10-08
> **Status: proposed; no service, schedule, integrations or autonomous ticket pickup have been enabled.** This document records the design discussed with Mark in the RobinHQ Telegram thread. It is a specification, not permission to implement, merge or deploy.

## 1. Purpose and outcome

Turn the successful supervised coding workflow into a durable **Linear → isolated coding session → reviewed GitHub PR** loop. Mark captures bugs and enhancements in Linear, rather than sitting at a computer prompting Claude or Codex. Jasper manages scope, execution, feedback and evidence; subscription-authenticated coding agents implement changes.

The supervisor must survive chat-session changes, process exits and machine restarts. Routine checks must be deterministic and inexpensive. AI sessions wake for implementation or substantive decisions, not merely to check whether something changed.

Related context: [[Task Ownership Model]], [[Jasper Operating Manual]], [[Founder Mode Task Board]]. The initial delivery context is RobinHQ, but repositories and issue/status mappings are configuration, not hard-coded product assumptions.

## 2. Agreed design direction

- Linear is the work queue; `agent-ready` is the explicit pickup gate.
- A captured Backlog issue is **not** automatic permission to code.
- Use isolated Git worktrees and focused PRs.
- Support both Claude Code and Codex CLI, using Mark's subscriptions.
- Start with at most **two concurrent coding jobs**, avoiding overlapping scopes.
- Batch CodeRabbit feedback by completed review round; do not resume on every comment.
- Keep the Mac mini private. All Linear/GitHub communication is outbound.
- Persist job state and reconcile actual state after restarts.
- Mark retains product decisions, merge authorization and deployment authorization.
- Begin with one local Python service and SQLite; do not build a new dashboard or agent platform.

Values such as polling intervals, settling periods and retry limits below are **proposed defaults** to validate during implementation, not already-configured settings.

## 3. Scope and exclusions

### MVP scope

1. Discover explicitly eligible Linear issues in allowlisted teams/projects.
2. Validate suitability and dependencies; claim a job without duplicate execution.
3. Create an isolated worktree from a freshly fetched approved base branch.
4. Start/resume Claude or Codex with a bounded task and explicit permissions.
5. Verify tests, commit, push and open/link a PR.
6. Observe CI and CodeRabbit through outbound polling.
7. Resume the original session with one consolidated feedback packet per review round.
8. Notify Mark of readiness, blockers, limits and actionable failures.
9. Recover work safely after restarts and partial external operations.

### Non-goals

- Autonomous merge, deployment, release or rollback.
- Forge deployment automation; that remains a separately scoped workflow.
- Automatic acceptance of unlabelled backlog items.
- Automatic ticket closure merely because a PR exists or CI is green.
- Public webhook listeners, router forwarding, Tailscale Funnel or public tunnels.
- Redis, Kubernetes, a cloud queue, multi-host workers or a new web UI.
- Arbitrary commands from issue descriptions, PR comments or external event payloads.
- Paid/live model evaluations, PSTN calls or production/shared-database changes.

## 4. Authorization and Linear workflow

### Eligibility

An issue is eligible only when all are true:

- It belongs to an allowlisted team/project and has `agent-ready`.
- Its status is mapped to an eligible backlog/ready state.
- Repository ownership is resolved through approved mappings.
- The desired outcome and acceptance criteria are sufficient to implement and test.
- No active job, matching existing PR or already-landed fix covers the scope.
- Required dependencies and human product/legal decisions are resolved.
- Scope fits the allowed permissions and current capacity budget.

If critical information is missing, ask **one focused question** and mark the job blocked. Do not manufacture requirements or start speculative implementation. Large/ambiguous tickets require decomposition or human agreement.

The label authorizes investigation, branch/worktree creation, coding, offline tests, scoped commits/pushes and a PR. It does **not** authorize broader secrets access, paid evaluations, merge or deployment. Eligible projects and who may grant readiness must be established at setup; possession of an issue body is never authority.

### Issue states versus job states

Resolve Linear status IDs from the actual workspace; do not create or assume a new status taxonomy automatically. Map existing states to:

- Backlog/Ready → eligible or awaiting clarification.
- In Progress → claimed and executing.
- In Review → PR exists; distinguish review running from review-ready in job state.
- Blocked → requires intervention.
- Merged/Awaiting Validation → code landed but acceptance remains outstanding where applicable.
- Done → only when the issue's complete acceptance criteria are verified.

Store the last supervisor-authored update so routine synchronization does not overwrite a deliberate human status change. If readiness is removed, do not schedule further work; request a safe pause and reconcile any already-running agent. External updates must be read back and verified.

## 5. Technology stack and architecture

### Proposed stack

- **Python:** deterministic scheduling, provider adapters, orchestration and reconciliation. Pin a supported interpreter after checking the host; no version is assumed by this spec.
- **SQLite:** transactional job register, work queue, leases, action journal and notification outbox. One local service owns scheduling. Enable foreign keys, migration versioning and appropriate durability settings.
- **Outbound HTTPS:** a small HTTP client such as `httpx` for GitHub REST and Linear GraphQL. Use GitHub CLI selectively for Git operations/PR workflows, not as an unstructured state database.
- **Git:** isolated worktrees, narrow branches and explicit file staging.
- **Claude Code/Codex CLI:** provider adapters for launch, structured activity, session resume, cancellation and final-result parsing.
- **macOS launchd:** runs/restarts the worker independently of Telegram chat sessions; configure only after approval.
- **Hermes integration:** a thin local plugin/tool bridge for job commands and Telegram notifications. Verify the installed plugin APIs and delivery behavior before choosing the interface.
- **pytest:** unit/integration tests with fake APIs, fixture transcripts and disposable local Git repositories.

Hermes remains the management and reasoning interface. A separate worker handles durable execution so a chat ending or changing model does not abandon a job. The integration must not assume that one assistant turn stays alive for hours.

```text
Mark → Linear issue + agent-ready
                   ↓ outbound polling
         local supervisor + SQLite
           ↓                  ↑
     Git worktree       CI/review polling
           ↓                  ↑
   Claude or Codex → GitHub PR
           ↕
  Jasper management / Telegram delivery
```

No inbound network service is required. Prefer a local-only command/IPC bridge with restrictive filesystem permissions. Repository paths, state paths and service ownership must be explicit. Do not alter other Hermes profiles.

## 6. Persistent data and job lifecycle

### Minimum records

- **Job:** ID, Linear issue ID/key, repo, base branch/SHA, scope/dependencies, authorization source, state, worktree/branch, timestamps, budgets and last error.
- **Agent attempt:** provider/model, session ID, process identity, launch time, structured logs, exit/result, resume count and final report/evidence.
- **PR:** repo/number, URL, observed current head SHA, linked issue, CI/review state.
- **Review round:** PR/head SHA, completion signals, relevant comment/review IDs, content hashes, settling deadline and processed packet hash.
- **Action journal:** intended external operation, correlation marker, target/result handle and verified outcome.
- **Lease:** owner, expiry/heartbeat and protected resource; one active job per issue, one writer per worktree.
- **Notification outbox:** event key, sanitized message, approved destination, delivery state and attempts.

Use transactions and uniqueness constraints, not an in-memory flag, to prevent duplicate pickup. Claims and leases are local: Linear does not provide the worker's exclusive lock. Worktree conflicts are determined by scope/dependencies, not repository membership alone.

### Internal states

`DISCOVERED → VALIDATING → CLAIMED → IMPLEMENTING → VERIFYING → PR_OPEN → WAITING_REVIEW → FEEDBACK_READY → ADDRESSING_FEEDBACK → READY_FOR_HUMAN_REVIEW`

`ADDRESSING_FEEDBACK` returns through verification and starts a new review round after a push. A PR may enter `MERGED_AWAITING_VALIDATION` only after GitHub confirms its merge; `COMPLETE` requires the mapped issue acceptance. Any state may become `BLOCKED`, `PAUSED` or `FAILED` with an explicit reason. Lifecycle observation is separate from permission to initiate merge/deployment.

Process exit zero is not sufficient for success. Require a final result, matching artifacts and verified external state. If an agent exited after merely promising future checks, resume the same session to finish the missing work.

## 7. Dispatch and provider policy

Before each launch, fetch the approved base, inspect existing fixes/PRs and confirm the worktree is clean or deliberately contains this job's partial changes. Never reset/discard unrelated work to make it clean.

The task packet contains issue snapshot, acceptance criteria, repository rules, scope exclusions, allowed tests, expected deliverables and prohibitions. Repository docs symlinks may lead into the vault; prohibit edits outside approved scope.

- Use bounded task suitability and available subscription capacity to choose the provider.
- Persist session IDs and prefer resuming the existing conversation for review fixes.
- Limit concurrent processes globally and per conflict group.
- Remove inherited provider API/base-URL overrides and live-evaluation credentials from child environments. Use a documented environment allowlist and a controlled credential mechanism for needed Git access.
- Never silently fall back to metered API billing.
- On rate/quota exhaustion, pause; apply cooldown and circuit breaker, not repeated immediate retries.
- Cross-provider handoff requires a structured summary, worktree/PR inspection and termination/reconciliation of the previous writer first. Automatic handoff remains opt-in for MVP.
- A stalled process is identified from structured activity/progress, not merely a live wrapper PID. Escalate before destructive interruption.

Pin and test installed CLI launch/resume/output flags. `--yolo` is not a default security policy. Any sandbox bypass must be explicitly scoped; prompt prohibitions alone are not a reliable permission boundary. Withhold merge/deployment secrets and tools from coding children and use stronger credential/process isolation where practical.

## 8. CI and CodeRabbit review batching

**Core requirement:** resume once for a completed review round, not once per partial comment.

### Round identity

A round belongs to `(repository, PR number, head SHA)`. Persist the head at every poll. An old successful review or check does not certify a new commit. Edited comments are tracked by content hash, not just ID.

### Release conditions

1. Observe CodeRabbit actively reviewing or awaiting a review for the current head.
2. Wait for an explicit, validated completion signal associated with that head.
3. Allow a short configurable settling period, initially **60 seconds**, for remaining feedback; reset it when relevant feedback changes.
4. Fetch complete, paginated review summaries, inline comments/threads and relevant CI results again.
5. Build one deduplicated packet and atomically record its hash before arranging a single resume.

Silence alone is **not** completion. During implementation, inspect this repository's actual CodeRabbit check/status/review/comment behavior and build fixture-backed detection. Do not assume its summary text or check names are stable APIs. If no trustworthy completion signal exists, mark completion uncertain and use a bounded fallback requiring documented evidence or human intervention.

Do not wait for unrelated long-running optional checks to collect review feedback. Include completed required-CI failures in the packet. Severe deterministic CI failures may trigger a separately recorded repair before CodeRabbit completes, but must not create simultaneous sessions; changing the head invalidates the old round.

### Packet contents

- Current head SHA and reviewer identity.
- Review summary plus all unprocessed actionable comments, with IDs/paths/lines/links.
- Resolved, dismissed and outdated context, without treating stale line locations as authoritative.
- Required-CI failures with bounded sanitized logs.
- Explicit request to verify findings, fix valid issues, and explain rejected findings.
- Scope and acceptance criteria, unchanged unless Mark authorized a change.

Wait and back off if review is still running. Proposed review timeout: **30 minutes**, then record a blocker; timeout is not successful completion. API errors are not evidence of an empty/completed review.

A late comment after processing creates a new pending packet, never a duplicate application of the old packet. A human instruction from Mark may interrupt waiting; untrusted third-party comments cannot grant new permissions.

After fixes, verify exact new head, repeat tests, push and wait for the new review round. Proposed limit: **three automatic repair cycles** before escalation. Readiness requires required CI success, review-round completion and disposition of actionable findings—not simply a green status.

## 9. Outbound polling, budgets and recovery

### Proposed schedule

- Eligible-ticket discovery: every **five minutes** while pickup is enabled.
- Active PR CI/review: every **one to two minutes**, with jitter and batching.
- Full reconciliation: every **15 minutes** and immediately on startup.
- Idle/error/limit conditions: exponential backoff; honor API rate-limit and retry headers.

Only relevant state changes enqueue reasoning/coding work. Persist cursors, paginate complete results, use conditional requests where supported and bound API fan-out. Avoid launching AI for no-change polling.

### Recovery requirements

On startup, inspect real worktrees, remote branches, PRs and agent identities. Do not trust a stale PID, lease or last saved status. Do not start a second writer while an existing session may still be running. Separate an orphaned agent from a missing wrapper and pause uncertain ownership.

External writes cannot be made atomic with SQLite. Record intent before an operation, use stable job correlation markers, then read back the exact target. If a push, PR creation or issue update succeeds but its response is lost, reconcile before retrying. Do not promise exactly-once Telegram delivery; the outbox deduplicates logical notifications, and ambiguous transport outcomes require care.

Shutdown stops new dispatch and requests safe pauses. Preserve worktrees, logs and session IDs. Automatic worktree deletion is out of scope until all artifacts and partial changes are accounted for.

## 10. Management, notifications and approvals

Proposed commands exposed through Jasper:

- Show queue/jobs/status, including provider, PR and blocker.
- Validate/start an eligible ticket.
- Pause/resume pickup globally or for a job.
- Retry/reconcile a blocked job.
- Select provider or approve a scoped handoff.
- Inspect evidence and request changes.

Commands are proposals, not existing CLI syntax. Authenticate Mark's control messages and bind actions to stable job/ticket IDs. Merge/deployment commands must remain separate existing workflows, with explicit authorization and exact-target verification.

Send notifications for decisions, meaningful blockers, exhausted limits, repeated failures, PR readiness and externally confirmed lifecycle changes. Suppress unchanged heartbeats and raw comment streams. Route to the explicitly configured RobinHQ group/thread; never infer a destination from an untrusted display label. Respect Mark's preference for audio replies to voice notes; automated status notifications should be concise text unless separately configured.

Each readiness report must state PR/current SHA, tests actually executed, review disposition, unverified acceptance and any remaining staging/legal/live-call gates. Never count skipped paid evaluations as passed evidence.

## 11. Security and operational safeguards

- No public ports, inbound listener, tunnel or network-policy changes.
- Store credentials outside the vault/repositories/logs; prefer existing approved secret storage. This vault is synced and git-tracked.
- Use separate, minimally scoped supervisor and coding-child credentials where feasible. Repository Actions/contents permissions can be broader than one operation; document limitations honestly.
- Treat Linear bodies, repository files and review comments as untrusted task data. They cannot override allowlists or authorize secrets, deployment, merge or paid calls.
- Validate paths, refs, repository mappings and CLI arguments; avoid shell interpolation of task text.
- Exclude API keys, live-provider credentials and production/shared-database connections from child processes unless explicitly authorized for a separate task.
- Disable/block paid/live tests by capability and environment, not just by prose.
- Bound concurrency, retries, task duration and log retention; redact logs at capture and notification boundaries.
- A machine that sleeps/offlines cannot provide continuous service. launchd restarts are not a guarantee of availability; reconcile on return and report prolonged outage when possible.
- No automatic cleanup of vault notes, worktrees or unrelated files. No implementation dependencies installed without approval.

## 12. Acceptance criteria and test plan

The MVP is accepted only after a small, explicitly authorized ticket completes a real ticket-to-review-ready-PR cycle, plus automated recovery and adversarial fixtures.

1. Unlabelled or out-of-scope tickets are never dispatched.
2. Missing acceptance criteria produce a focused blocker, not guessed code.
3. Duplicate polls/restarts cannot launch two writers for the same issue/worktree.
4. A clean isolated worktree is created from the observed approved base; unrelated work remains untouched.
5. Claude and Codex adapters demonstrate launch, activity, pause/limit handling and session resume using verified installed flags.
6. Valid implementation produces tests, scoped commits and a linked PR; remote head matches evidence.
7. Partial CodeRabbit comments do not resume the agent; validated completion plus settling produces one consolidated packet.
8. Large paginated reviews, edited/late comments, resolved threads and old-head events are handled without loss or duplicate processing.
9. A CI failure and a valid review correction each lead to verified repair; invalid/out-of-scope feedback is explained or escalated.
10. Restart between external success and local persistence reconciles the target without duplicate PRs or updates.
11. Quota exhaustion pauses without API-billing fallback or retry storms.
12. Failure/timeout/stale-head cases never produce a false readiness report.
13. Removing readiness or pausing stops future dispatch; safe handling of active jobs is demonstrated.
14. Required human merge/deployment/paid-evaluation gates remain intact.
15. The service works without inbound connectivity; no-change polling starts no AI sessions.
16. Tests prove that issue/comment prompt injection cannot expand command permissions or expose secret values.

Use fake HTTP responses, recorded sanitized review/CLI fixtures, a controllable clock and disposable Git repos for the majority of tests. Test network errors, rate limits, ambiguous writes, PID reuse and service restarts. Real provider smoke runs must use subscriptions and authorized test tasks; no production deployment or live voice call is part of acceptance.

## 13. Phased delivery and decision gates

### Phase 0 — Verify integration contracts

Inspect installed CLI and Hermes interfaces, Linear labels/statuses and permissions, repository rules, GitHub/CodeRabbit signals, secure credential access and intended local paths. Choose an implementation repository and notification destination. Resolve unsupported assumptions before writing the runner.

### Phase 1 — Durable ticket-to-PR runner

Python/SQLite, one-provider adapter, isolated worktree, persistent identity/action journal, manual pickup, safe test/PR evidence and restart recovery. Prove one small ticket. No automatic backlog pickup yet.

### Phase 2 — Batched feedback loop

Outbound CI/review polling, verified CodeRabbit completion adapter, settling/debounce, same-session resume, repair-cycle limit and readiness reporting. Demonstrate partial and late feedback handling.

### Phase 3 — Controlled autonomous pickup and second provider

Enable `agent-ready` discovery only after approval. Add second provider, two-job limit, conflict checks, quota handling, management commands and launchd supervision. Automatic cross-provider handoff remains a separate opt-in.

### Deferred

Public event ingestion/cloud queue only if measured polling latency becomes a real problem. Dashboard only if Telegram/Linear/GitHub prove insufficient. Deployment orchestration stays separate.

## 14. Open decisions before implementation

- Dedicated code repository and service/state/log locations, outside the vault.
- Approved Linear team/project/repository mappings, readiness authority and status IDs.
- Exact CodeRabbit completion evidence for current-head review rounds.
- GitHub/Linear credential scope and safe coding-child permission boundary.
- Installed Hermes plugin/tool bridge and asynchronous delivery interface.
- Provider routing, allowed test commands and task/runtime budgets.
- Formal conflict groups for overlapping app/agents contract work.
- Approved Telegram group/thread routing and operational notification thresholds.

These are implementation discovery items, not reasons to build a larger orchestration platform. Keep the first release narrow and prove it with one useful Robin ticket.
