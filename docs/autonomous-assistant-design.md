# Autonomous Assistant Vertical-Slice Design

Date: 2026-07-27

## Decision

Add a small autonomous control plane around the existing Company Mode rather than replacing the agent system. A bounded daily session selects and executes useful, affordable roadmap items sequentially through the current manager, specialists, artifact handoff, editor, Telegram transport, and approval gates. It may select at most ten distinct items in 120 minutes and attempts each item at most once per session. Autonomous approval intentionally suppresses Linear mirroring; credentialed production smoke tests remain required.

Execution is intentionally sequential. Concurrent autonomous workers are deferred until the shared ledger, state transitions, and restart recovery have proven reliable in production.

## Reused components

- `group_bot.py`: Telegram delivery, APScheduler lifecycle, per-agent bots, office events, and the Company Mode runner.
- `company_mode.py`: persisted company/task state, reservations, task status transitions, prior-work context, editor verdicts, revisions, and daily reports.
- `main.py`: specialist definitions/personas, least-privilege tool sets, OpenAI Responses calls, usage extraction, planning, and artifact collection.
- `projects.py` / `projects.json`: project identity and GitHub repository routing.
- `company_linear.py`: retained for owner-approved Company Mode work; autonomous approval suppresses its mutation hooks.
- Existing `/confirm`, PR, deployment, email, delete, Railway, and publishing gates.

## New components

### Model catalog and router

`model_router.py` loads `config/model-catalog.json` (or `MODEL_CATALOG_FILE`) into typed records. Each model declares:

- capability level: lightweight, standard, or advanced;
- supported capabilities;
- context limit;
- configured input, cached-input, and output prices;
- enabled status.

The router receives task type, complexity, risk, required capabilities, estimated context/output size, remaining spendable budget, previous failures, and previous models. It returns a model, estimated cost, capability level, and a human-readable reason. It chooses the lowest-cost enabled model that is likely to succeed, promotes after a cheaper failure, and refuses a route that exceeds the task's available budget.

Prices are configuration snapshots used for estimates and reconciliation. They must not be presented as guaranteed current list prices.

### Autonomous workflow state

`autonomous_workflow.py` owns a versioned `autonomy_state.json` under `DATA_DIR`. On first use it seeds from `config/autonomous-roadmap.json`. The state contains:

- project goals and status;
- roadmap items with priority, status, dependencies, blockers, acceptance criteria, preferred agent, task type, complexity/risk, authorization level, attempt history, previous models, and human decision required;
- a capped, deduplicated idea backlog with stable IDs, proposal status, source-run
  provenance, and optional owner-approved roadmap links;
- scheduled-run idempotency metadata.

The workflow writes one structured JSON report per run under `DATA_DIR/autonomous_runs/`. Reports include the requested run, plan, routing, usage, review, retry, blocker, escalation, artifact, file/test, and final-status fields.

### Authorization levels

Roadmap items declare one of:

1. `observe`: inspect and report;
2. `propose`: plans, drafts, ideas, and review artifacts;
3. `modify_local`: intended for file/branch/workspace changes inside a future isolated checkout executor;
4. `external_action`: deploy, merge, publish, send, purchase, delete, or production mutation.

`AUTONOMY_MAX_AUTHORIZATION` is the maximum level a scheduled run may start. A higher-level item becomes `needs_human` with an exact action instead of executing. Existing tool-level confirmation gates remain authoritative. This slice does not grant new connector permissions.

Only `observe` and `propose` auto-execute in this slice. The existing `run_python` helper can access the network and the existing code-edit helpers create remote GitHub branches/PRs, so neither is truthfully local. `modify_local` and `external_action` therefore stop for owner review even if the configured ceiling is raised.

## Daily-run sequence

1. APScheduler invokes the autonomous job at the configured weekday/time/timezone, or the owner invokes `/autorun dry-run`.
2. The coordinator acquires a persistent file lock and checks the scheduled-date idempotency key.
3. It recovers stale run/task state conservatively and records a new run ID plus trigger.
4. It loads roadmap state, Company Mode state, blockers, recent-run/attempt metadata, and remaining spendable budget.
5. The deterministic router may record a no-cost candidate decision; before any paid work, an open/paused/running Company project or pending owner confirmation defers execution and idle ideation.
6. It selects the highest-priority actionable item whose dependencies are complete, whose blockers/human-decision fields are empty, and which has not already been attempted in this session.
7. It checks the item's authorization level and asks the model router for the least expensive capable route.
8. In dry-run mode it records the plan and stops before paid model calls, state transitions, or external/destructive actions.
9. In live mode it applies a context-scoped project target (without rewriting the owner's persisted selection), materializes one bounded worker task plus one reviewer task, attaches structured acceptance criteria and routing metadata, and reserves the full estimate atomically. When the item explicitly requires run history, the coordinator injects an allowlisted/redacted snapshot of up to five prior runs as transient worker/reviewer context. Global trigger/outcome fields remain explicitly labeled global, while task titles/outcomes are filtered to the current project. The snapshot is not persisted in Company goals/events and does not grant agents filesystem access to control-plane state.
10. Company Mode executes the tasks sequentially. Each task records model decision, tokens, actual/reconciled cost, attempts, artifacts, and failure classification.
11. Vera evaluates mandatory explicit acceptance criteria. Repeated substantially identical feedback, repeated technical failure, unavailable tools, missing access, missing owner information, budget exhaustion, or the configured attempt limit produces a terminal `needs_human` state for that item. An explicit external dependency blocks immediately instead of consuming a revision round; ordinary team-fixable feedback remains revisable.
12. The coordinator reconciles reservations, updates the roadmap item from the Company Mode result, and persists the worker result separately from reviewer feedback. A task-local blocker is escalated but does not prevent the session from selecting unrelated actionable work. The aggregate session remains `needs_human` until that blocker is resolved.
13. Before another item starts, the coordinator refreshes the ledger and checks the ten-item, 120-minute, one-attempt-per-item, and ordinary-budget ceilings. It stops when no useful complete worker/reviewer unit remains affordable; it does not create work merely to consume budget.
14. After roadmap work is exhausted and the session has no unresolved blocked task, Lumen may run one controlled batch containing at most the configured number of deduplicated ideas. A blocker suppresses ideation so proposals cannot mask an owner action. Ideas remain `proposed` backlog records and are never executed automatically. The owner may stage `/autorun promote <idea-id>`; the read-only preview deterministically creates explicit acceptance criteria and requires `/confirm`. Confirmation revalidates the full proposal and destination under the run/state locks, then atomically creates one `ready`, `propose` roadmap item and records bidirectional provenance. It never starts execution in the promotion turn.
15. The coordinator releases the lock, sends completed deliverables through the existing chunked Telegram transport, and sends one aggregate summary covering every selected task, route, attempt, cost, result, blocker, escalation, and proposed idea. The summary includes a deterministic `trigger | final | human_review` line derived from persisted run state without another model call.

## Budget design

Company Mode remains the source of truth for daily spend. Its JSON mutations become file-locked transactions with atomic replacement and corruption quarantine.

The ledger tracks:

- daily budget and a `BUDGET_TIMEZONE`-aware date (Phoenix by default);
- emergency reserve unavailable to ordinary work;
- active reservation IDs and estimates;
- reconciled actual or explicitly estimated cost;
- input, cached-input, and output tokens;
- project, task, agent, and model attribution;
- pricing snapshot and model-routing reason.

Normal work must reserve before it starts. A reservation that would exceed spendable budget is rejected. With the default $5 budget and $0.25 reserve, at most $4.75 is available to ordinary work. That amount is a ceiling, not a spending target: the session may finish below it when no useful complete unit fits. Each autonomous Responses request is limited to the output tokens affordable inside its task's remaining reservation after conservative fresh-input pricing and a safety margin. When accumulated tool evidence makes a later request exceed the initial task hold, the strict guard may request an atomic top-up from otherwise-uncommitted ordinary budget and then recompute the limit; concurrent task/reviewer holds and the emergency reserve remain protected. Unknown model pricing, unavailable headroom, and still-too-small envelopes fail closed before generation. Automatic transport retries are disabled inside these strict envelopes. Reconciliation releases the enlarged hold and records actual cost; failed work still records any cost already incurred. Deterministic Telegram summaries/escalations do not consume model budget. Because provider pricing can change and a lost network response may still have been processed, an OpenAI project spending limit remains the only invoice-level hard ceiling.

If an asyncio caller is cancelled after a paid Python worker thread starts, the runtime waits for that non-killable thread to finish before reconciling its reservation. This preserves accounting and prevents a cancelled call from continuing outside the ledger.

## Review and failure policy

- Execution attempts per task and review attempts per project are configurable and bounded.
- Reviewer output must begin with `APPROVED`, `REVISIONS REQUIRED`, or `BLOCKED - NEEDS HUMAN REVIEW`.
- Acceptance criteria are stored on the roadmap item and copied into the task/reviewer prompt.
- For tasks that explicitly require run history, up to five prior runs are reduced to bounded allowlisted fields, redacted, and supplied transiently as inert evidence. Run-wide fields are labeled global; project details are filtered to the current project. Raw narratives, secrets, paths, unrelated-project titles, and private result text are excluded.
- `REVISIONS REQUIRED` is reserved for changes achievable with supplied evidence and allowed tools. Explicit missing access, missing information, or unavailable tools become `BLOCKED - NEEDS HUMAN REVIEW` before another revision.
- Normalized reviewer-feedback fingerprints detect repeated review cycles; bounded execution/revision caps stop repeated worker failures.
- Failures are classified as `missing_access`, `missing_information`, `unavailable_tool`, `permission_denied`, `budget_exhausted`, `transient`, `technical`, `no_progress`, or `decision_required`.
- Access, permission, unavailable-tool, ambiguity/decision, budget, no-progress, and attempt-limit failures stop spending and escalate.
- A blocked item does not prevent selection of unrelated actionable work later in the same session.
- One roadmap item receives at most one execution selection per session, even when its result is retryable or deferred.
- A task that exceeds its monitoring time ceiling is allowed to finish in its existing Python thread so usage/tools cannot escape the ledger; it is charged, receives no automatic retry, and escalates. Hard process preemption is deferred with modify-local execution.

## Scheduler and manual operation

Development defaults:

- disabled until explicitly enabled;
- dry-run enabled;
- 08:00;
- Monday through Friday;
- `America/Phoenix`;
- $5 daily budget;
- $0.25 emergency reserve, leaving $4.75 for ordinary work;
- at most ten distinct roadmap items and 120 minutes per session;
- one Lumen batch containing at most three proposed ideas after roadmap exhaustion.

The Telegram command `/autorun dry-run` is always safe: it performs one no-spend planning pass and does not enter the live continuation loop. A local CLI command provides the same selection/report path without importing Telegram or invoking paid APIs. `/autorun live` is group-only and starts one bounded session under the same lock and limits as the scheduler. `/autorun status` exposes bounded, stable proposal IDs. `/autorun promote <idea-id> [project-id]` and `/autorun queue <manifest-id>` are also group-only; each stages a per-chat, same-owner confirmation without a model call or state mutation. Pack confirmation revalidates the exact reviewed repository manifest revision, appends only `observe`/`propose` goal and item records, writes a pre-import backup, and never starts a run.

## Safety boundaries

- Dry-run may write only local run/audit state; it performs no model call, connector call, code change, deploy, publish, delete, send, or production mutation.
- External actions remain human-gated even when a roadmap item permits local modification.
- Autonomous/Company persistence and autonomous outbound text apply key/value and embedded-value redaction. Secret references may appear in blockers, but secret values must never be placed in roadmap state.
- The run lock prevents overlap across threads/processes on one shared volume. Multi-region distributed execution is out of scope.
- Corrupt state is quarantined and causes a conservative blocked run; it never silently grants a fresh budget.
- Idea promotion is fail-closed: an unknown/duplicate/changed proposal, ambiguous or inactive project, active run, roadmap-ID collision, or failed atomic write creates no roadmap work. A successful repeat confirmation is an idempotent no-op.
- Roadmap-pack import is fail-closed and additive: unsupported schema fields, changed revisions, inactive or ambiguous projects, ID collisions, bad/cyclic dependencies, active runs, authorization above `propose`, partial prior imports, or write failures leave the primary state unchanged. A receipt binds successful repeats to the same intact goal/items.

## Implementation footprint

- New: `model_router.py`, `autonomous_workflow.py`, `autonomy_team.py`.
- New: `config/model-catalog.json`, `config/autonomous-roadmap.json`, and owner-confirmed manifests under `config/autonomous-projects/`.
- Update: `company_mode.py` for atomic state/budget operations and attempt/usage metadata.
- Update: `main.py` for catalog-backed pricing, usage records, model overrides, and controlled idea generation.
- Update: `group_bot.py` for `/autorun`, weekday scheduling, owner-confirmed idea promotion and roadmap-pack queueing, roadmap execution, and summaries.
- Update: `projects.py` for consistent persistent-path resolution.
- Update: Docker copy lists, `.env.example`, `README.md`, and setup/workflow documentation.
- New/updated tests, including `tests/test_group_autonomy.py`, for scheduling, overlap, selection, routing, budget concurrency/reconciliation, review limits, escalation, Telegram delivery, redaction, reporting, and dry-run safety.
- New: a sample daily-run report.

## Deferred limitations

- Autonomous sessions are sequential, select one roadmap item at a time, and stop after ten distinct selections or 120 minutes even when budget remains.
- The JSON/file-lock store assumes all replicas share one filesystem volume; it is not a distributed database.
- Existing helper functions can still be imported directly, outside the centralized tool authorization path.
- Pending Telegram confirmations are still process-memory state.
- `/autorun retry <item-id>` deliberately resets one `needs_human` or `blocked` item after
  the owner resolves its stated problem. It preserves attempt history and never starts a
  model call. `/autorun promote <idea-id> [project-id]` provides the explicit
  proposal-to-roadmap bridge, while `/autorun queue <manifest-id>` provides an
  owner-confirmed additive path for reviewed project manifests. Skip, accept-as-is,
  criteria editing, and automatic rescoping remain deferred.
- A run-history task's transient execution context includes only an allowlisted snapshot from up to five prior runs, with global metadata distinguished from current-project task details; it excludes full narratives, private outputs, arbitrary files, unrelated-project task text, and milestones.
- The task time ceiling is a monitored stop/no-retry boundary, not a kill signal for Python threads; the runner waits for completion to preserve accounting.
- Automatic local modification is disabled until work can run in a killable, isolated checkout without remote side effects.
- Live Telegram, OpenAI billing reconciliation, Docker, Railway volume behavior, and external integrations require post-merge smoke tests with real credentials.
- Model prices and availability remain operator-maintained configuration; enabled prices/source URLs were refreshed from official OpenAI model pages on 2026-07-27.
