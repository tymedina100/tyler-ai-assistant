# Autonomous Assistant Repository Assessment

Date: 2026-07-27

Scope: point-in-time baseline captured before the vertical-slice implementation. See `autonomous-assistant-design.md` and the README for the implemented state and tested limitations.

## Executive finding

This repository is already a capable Telegram-first multi-agent assistant, not a blank chatbot. It has named specialists, a manager, a sequential Company Mode runner, a persisted daily budget, token-cost reconciliation, a final editor, bounded revision rounds, approval gates, project/Linear integration, and APScheduler jobs. The missing layer is a reliable autonomous control plane: structured roadmaps, scheduled work selection, transactional budget reservations, task-level model routing, durable run records, and typed escalation.

The highest-value next step was therefore a locked, sequential weekday daily run built on the existing Company Mode runner. The bounded-session follow-up keeps that architecture but allows the manager to select additional useful, affordable items without introducing parallel execution. Parallel autonomous workers should still wait until locking and budget enforcement are trustworthy.

## Verification against the 2026-07-28 working tree

The sections below remain the preimplementation baseline. The current branch now contains the recommended vertical slice:

- configurable weekday and manual dry-run triggers;
- structured project/roadmap selection with dependencies, blockers, recent-run metadata, and mandatory acceptance criteria;
- a shared Company Mode budget preflight plus atomic worker/reviewer reservations and reconciliation;
- configurable model routing, execution/review attempt caps, and typed terminal escalation;
- a bounded sequential session of up to ten distinct roadmap items or 120 minutes, with one attempt per item per session and unrelated work allowed to continue past task-local blockers;
- Telegram delivery through the existing group runtime; and
- durable run, routing, attempt, usage, cost, review, and outcome records.

The verification pass also closed failure paths that were not safe enough in the initial implementation: corrupt autonomy state now leaves a durable recovery-required marker instead of silently reseeding on the next run; a crashed/cancelled Company runner blocks its project and closes reservations; cancellation waits for an already-started paid worker thread before budget reconciliation; larger bounded internal worker results and explicit latest-candidate prompts prevent review feedback from chasing the smaller Telegram/report copy; an item with no explicit acceptance criteria cannot route or execute; and `/autorun retry <item-id>` provides a locked, audited owner recovery path without starting paid work.

Credentialed OpenAI/Telegram execution, Railway mounted-volume behavior, Docker images, and the bounded multi-item session still require controlled production verification. Sessions remain sequential; owner retry is supported, while skip, accept-as-is, criteria editing, and automatic rescoping remain deferred.

## 1. What the system currently does

- `main.py` is the hub for the CLI, OpenAI Responses API calls, tools, agent definitions, model constants, pricing, memory, and manager delegation.
- `bot.py` is a single Telegram-bot interface with an allowlist and per-chat confirmation handling.
- `group_bot.py` is the primary multi-bot interface. It routes group messages, supports direct specialist DMs, runs Company Mode, posts Telegram updates, and owns APScheduler.
- `company_mode.py` persists a company ledger, projects, tasks, events, artifacts, products, and review state in `company_state.json`.
- `projects.py` and `projects.json` select which GitHub repository the code tools target. The registry contains repository metadata and commands, not product roadmaps.
- `company_linear.py` mirrors approved Company Mode work into Linear through optional fail-soft hooks.
- The connector/helper modules provide GitHub, Linear, Google, Gumroad, Vercel, and Railway capabilities.
- The Virtual Office modules expose and render bounded agent activity and business metrics; they are an observer surface, not an orchestration engine.
- The existing offline test suite contains 187 tests. Before this assessment, all 187 passed and the repository byte-compiled successfully.

## 2. Current end-to-end workflow

The reactive path is:

1. A CLI or Telegram message arrives.
2. A lightweight group router selects a specialist, or Miles coordinates a multi-agent request.
3. The selected specialist runs through the shared bounded tool loop with its curated tools and fixed model.
4. Sensitive actions may stage a per-chat `/confirm` request.
5. Telegram posts the specialist answer or Miles's recap.
6. Ad-hoc token cost is recorded after the work completes.

The existing supervised Company Mode path is:

1. The owner sends `/assign <goal>`.
2. Miles produces a tailored list of agent tasks; a fixed plan is the fallback.
3. Company Mode creates a proposed project, creates tasks, and reserves a fixed amount per task.
4. The owner sends `/approve`.
5. `group_bot.py` runs tasks sequentially and passes prior outputs/deliverables forward.
6. Real OpenAI usage cost is reconciled when each task ends.
7. Vera reviews the work. She can approve, request revisions, or declare that human input is required.
8. At most two revision rounds are created.
9. Approved work is completed; blocked work is escalated in Telegram; `/dailyreport` shows a compact status summary.

The scheduled path currently posts a morning briefing, calendar alerts, reminders, and an evening Company Mode report. It does not select or start project work.

## 3. Product-vision capabilities that already exist

- Manager and specialized agents with functional, recognizable personalities.
- Telegram group, direct-agent DM, manager delegation, message chunking, and allowlisting.
- Persistent Company Mode projects, tasks, events, artifacts, product links, revenue, and aggregate daily spend.
- A default $5 daily Company Mode budget, soft task reservations, actual token-based reconciliation, cached-input pricing support, and a conservative unknown-model fallback.
- Goal-specific work planning, sequential execution, prior-work handoff, and a final editor.
- A three-way review verdict and a hard two-round revision ceiling.
- Human gates for email sending, deletion, production deploys, Railway mutation, publishing, and other staged actions.
- PR-oriented code changes rather than direct base-branch merges.
- APScheduler, persisted reminders, timezone configuration, and existing Telegram scheduled-message delivery.
- Project registry, active-repository routing, Linear issue creation/mirroring, and source-issue workflows.
- Tool-argument redaction, path traversal protection, bounded reads/writes, bounded tool loops, HTTP timeouts, and offline mocks.

## 4. Partially implemented capabilities

- **Autonomy:** Company Mode executes approved plans autonomously, but a human must invent and assign each goal; scheduled jobs never choose work.
- **Budgeting:** actual model usage is metered, but reservations are fixed, writes are not transactional, sub-cent precision is lost in state, and ad-hoc work is charged only after execution.
- **Model strategy:** two model tiers and centralized prices exist, but model choice is fixed per agent rather than selected per task.
- **Review safety:** revision rounds are bounded, but repeated feedback, identical outputs, stale `in_progress` tasks, and typed execution failures are not detected.
- **Escalation:** editor-declared blockers are actionable, but permissions, missing credentials, unavailable tools, ambiguous requirements, and budget failures are not consistently classified.
- **Observability:** task events, spend, artifacts, and reports exist, but there is no run ID or durable record of trigger, plan, routing reasons, tokens, retries, tests, changed files, and final outcome.
- **Scheduling:** APScheduler is present, but there is no Monday-Friday autonomous job, persistent run lock, idempotency key, or restart recovery.
- **Project state:** repository profiles and one active repository exist, but goals, milestones, roadmap items, dependencies, blockers, and acceptance criteria are not structured.

## 5. Missing capabilities

- A persistent multi-project roadmap and idea backlog.
- Manager-driven daily selection from actionable roadmap work.
- Configurable weekday autonomous runs and a safe manual dry-run command.
- Cross-process overlap prevention and scheduled-run idempotency.
- Transactional reserve/reconcile/release budget operations with an emergency reserve.
- Per-project, task, agent, and model usage/cost records.
- A configurable model capability/pricing catalog and a recorded routing decision.
- Explicit authorization levels for observe, propose, local modification, and external action.
- Structured acceptance criteria, execution/review attempt counts, failure classes, no-progress fingerprints, and terminal `needs_human` state.
- Durable run reports and concise end-of-run Telegram summaries.
- Controlled, deduplicated ideas that enter a backlog only when no higher-priority work is actionable.

## 6. Most important technical problems

1. `company_state.json` is updated through unlocked read-modify-write operations. Concurrent Telegram chats can lose updates or reserve the same dollars.
2. A malformed state file silently becomes a fresh state with a fresh budget, which can erase history and reopen spending.
3. The advertised hard cap is checked between Company Mode tasks. One task can exceed its reservation, and ad-hoc calls have no preflight reservation.
4. Model/tool failures can be converted into friendly error text and then marked `done` because no exception reached the runner.
5. Tasks do not store structured acceptance criteria, execution attempts, review attempts, failure class, model decision, usage, or progress fingerprint.
6. A process restart can leave an `in_progress` task stranded because the runner only selects `planned` tasks.
7. Company projects do not snapshot the project key/repository target; changing the global active project can redirect later execution.
8. Persistence-path resolution is duplicated and inconsistent; notably, active-project state does not honor Railway's volume variable unless `DATA_DIR` is also set.
9. The scheduler's overlap guard is process-local only, and the existing cron jobs run every day rather than weekdays.
10. `assistant.log` is unstructured, unbounded, and stored at the repository path instead of the persistent data directory.

There are few literal TODO/FIXME markers; the unfinished work is architectural. The largest duplication is path/config resolution and repeated connector request/error handling. The large `main.py` and `group_bot.py` files are maintainability risks, but a broad rewrite is not justified for this slice.

## 7. Highest-value implementation step

Implement one locked, sequential autonomous daily-session path that reuses Company Mode:

1. Load structured project and roadmap state.
2. Select the highest-priority actionable item with satisfied dependencies and no blockers, excluding items already attempted in this session.
3. Refuse overlap using a persistent run lock and scheduled-date idempotency.
4. Route the item/tasks with a configurable model catalog and record the reason.
5. Reserve estimated cost transactionally while preserving an emergency reserve.
6. Reuse the existing manager, specialist, artifact handoff, editor, Telegram, Linear, and approval mechanisms.
7. Stop spending on the affected item after repeated feedback, no progress, missing access, unavailable tools, budget exhaustion, or the configured attempt limit; continue with unrelated actionable work when safe.
8. Before each new item, enforce the ordinary-budget, ten-item, 120-minute, and one-attempt-per-item session ceilings.
9. Reconcile actual token usage, update every selected roadmap item, save one aggregate structured run report, and post one action-oriented Telegram summary.
10. After roadmap exhaustion, allow one Lumen batch of at most three deduplicated `proposed` ideas; never auto-build them.
11. In dry-run mode, perform one planning pass and report without invoking paid models or performing external/destructive actions.

This creates useful daily autonomy without introducing parallel execution, replacing the agent system, granting broader permissions, or manufacturing work merely to spend the daily budget. With the default $5 ceiling and $0.25 emergency reserve, ordinary work may use up to $4.75 and should stop below that amount when no useful complete unit fits.

## 8. Security, reliability, and runaway-cost risks

- Model-generated Python runs in a constrained child process but is not a hard security sandbox; it can still read accessible local files or use the network.
- Several helper functions can mutate external systems if called directly; authorization gates currently live mostly in `main.execute_tool`, not in the helpers themselves.
- Company Mode auto-approves some production of files/PR artifacts. `write_file` may mirror to GitHub, so it is not always purely local.
- Railway variable reads can return raw values to a model. This should not be used for secrets.
- Full agent answers are printed to process stdout, which may place private project content in hosting logs.
- Connector errors may include remote response text and should be redacted before durable logging or Telegram delivery.
- Pending confirmations and in-process runner locks are lost on restart.
- Linear mirroring can duplicate issues if the process crashes after creating an issue but before saving its ID.
- Per-chat locks prevent some local collisions, but separate bots/chats can still execute concurrently against shared state.
- The repository correctly gitignores `.env`, credentials, tokens, memory, state, and logs. Those protections must be preserved.

## Scope decision

The autonomous session should remain sequential and bounded: at most ten distinct roadmap items, one attempt per item, and 120 minutes. Task-local blockers should not prevent unrelated work from continuing. Controlled Lumen ideation is an idle fallback only, runs as one batch of at most three ideas, and writes `proposed` backlog records that never auto-execute. Parallel agents, automatic deployment/merge/publish, broad helper rewrites, database replacement, and a full UI are intentionally deferred.
