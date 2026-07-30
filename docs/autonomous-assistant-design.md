# Autonomous Assistant Vertical-Slice Design

Date: 2026-07-27

## Decision

Add a small autonomous control plane around the existing Company Mode rather than replacing the agent system. The first implementation slice selects at most one roadmap item per run and executes it sequentially through the current manager, specialists, artifact handoff, editor, Telegram transport, and approval gates. Autonomous approval intentionally suppresses Linear mirroring; offline behavior is tested and credentialed production smoke tests remain required.

The sequence is intentionally single-task. Concurrent autonomous workers are deferred until the shared ledger, state transitions, and restart recovery have proven reliable in production.

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
- a capped, deduplicated idea backlog;
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
6. It selects the highest-priority actionable item whose dependencies are complete and whose blockers/human-decision fields are empty.
7. It checks the item's authorization level and asks the model router for the least expensive capable route.
8. In dry-run mode it records the plan and stops before paid model calls, state transitions, or external/destructive actions.
9. In live mode it applies a context-scoped project target (without rewriting the owner's persisted selection), materializes one bounded worker task plus one reviewer task, attaches structured acceptance criteria and routing metadata, and reserves the full estimate atomically.
10. Company Mode executes the tasks sequentially. Each task records model decision, tokens, actual/reconciled cost, attempts, artifacts, and failure classification.
11. Vera evaluates mandatory explicit acceptance criteria. Repeated substantially identical feedback, repeated technical failure, unavailable tools, missing access, budget exhaustion, or the configured attempt limit produces a terminal `needs_human` state.
12. The coordinator reconciles reservations, updates the roadmap item from the Company Mode result, persists the worker result separately from reviewer feedback, releases the lock, sends the completed deliverable through the existing chunked Telegram transport, and then sends a concise summary.
13. If no roadmap work is actionable, a controlled creative callback may add at most the configured number of deduplicated ideas to the backlog. Ideas are never executed automatically.

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

Normal work must reserve before it starts. A reservation that would exceed spendable budget is rejected. Reconciliation releases the estimate and records actual cost; failed work still records any cost already incurred. Deterministic Telegram summaries/escalations do not consume model budget.

If an asyncio caller is cancelled after a paid Python worker thread starts, the runtime waits for that non-killable thread to finish before reconciling its reservation. This preserves accounting and prevents a cancelled call from continuing outside the ledger.

## Review and failure policy

- Execution attempts per task and review attempts per project are configurable and bounded.
- Reviewer output must begin with `APPROVED`, `REVISIONS REQUIRED`, or `BLOCKED - NEEDS HUMAN REVIEW`.
- Acceptance criteria are stored on the roadmap item and copied into the task/reviewer prompt.
- Normalized reviewer-feedback fingerprints detect repeated review cycles; bounded execution/revision caps stop repeated worker failures.
- Failures are classified as `missing_access`, `missing_information`, `unavailable_tool`, `permission_denied`, `budget_exhausted`, `transient`, `technical`, `no_progress`, or `decision_required`.
- Access, permission, unavailable-tool, ambiguity/decision, budget, no-progress, and attempt-limit failures stop spending and escalate.
- A blocked item does not prevent selection of an unrelated actionable item on the next run.
- A task that exceeds its monitoring time ceiling is allowed to finish in its existing Python thread so usage/tools cannot escape the ledger; it is charged, receives no automatic retry, and escalates. Hard process preemption is deferred with modify-local execution.

## Scheduler and manual operation

Development defaults:

- disabled until explicitly enabled;
- dry-run enabled;
- 08:00;
- Monday through Friday;
- `America/Phoenix`;
- $5 daily budget;
- small emergency reserve;
- one roadmap item and one creative idea maximum per run.

The Telegram command `/autorun dry-run` is always safe. A local CLI command provides the same selection/report path without importing Telegram or invoking paid APIs.

## Safety boundaries

- Dry-run may write only local run/audit state; it performs no model call, connector call, code change, deploy, publish, delete, send, or production mutation.
- External actions remain human-gated even when a roadmap item permits local modification.
- Autonomous/Company persistence and autonomous outbound text apply key/value and embedded-value redaction. Secret references may appear in blockers, but secret values must never be placed in roadmap state.
- The run lock prevents overlap across threads/processes on one shared volume. Multi-region distributed execution is out of scope.
- Corrupt state is quarantined and causes a conservative blocked run; it never silently grants a fresh budget.

## Implementation footprint

- New: `model_router.py`, `autonomous_workflow.py`, `autonomy_team.py`.
- New: `config/model-catalog.json`, `config/autonomous-roadmap.json`.
- Update: `company_mode.py` for atomic state/budget operations and attempt/usage metadata.
- Update: `main.py` for catalog-backed pricing, usage records, model overrides, and controlled idea generation.
- Update: `group_bot.py` for `/autorun`, weekday scheduling, roadmap execution, and summaries.
- Update: `projects.py` for consistent persistent-path resolution.
- Update: Docker copy lists, `.env.example`, `README.md`, and setup/workflow documentation.
- New/updated tests, including `tests/test_group_autonomy.py`, for scheduling, overlap, selection, routing, budget concurrency/reconciliation, review limits, escalation, Telegram delivery, redaction, reporting, and dry-run safety.
- New: a sample daily-run report.

## Deferred limitations

- Autonomous runs are sequential and select one roadmap item at a time.
- The JSON/file-lock store assumes all replicas share one filesystem volume; it is not a distributed database.
- Existing helper functions can still be imported directly, outside the centralized tool authorization path.
- Pending Telegram confirmations are still process-memory state.
- `needs_human` items do not yet have a dedicated `/autorun resolve|retry|skip` command; the owner must repair state deliberately before retrying.
- Recent-run state stores outcome metadata and per-item attempts, not full prior-run narrative content or milestones.
- The task time ceiling is a monitored stop/no-retry boundary, not a kill signal for Python threads; the runner waits for completion to preserve accounting.
- Automatic local modification is disabled until work can run in a killable, isolated checkout without remote side effects.
- Live Telegram, OpenAI billing reconciliation, Docker, Railway volume behavior, and external integrations require post-merge smoke tests with real credentials.
- Model prices and availability remain operator-maintained configuration; enabled prices/source URLs were refreshed from official OpenAI model pages on 2026-07-27.
