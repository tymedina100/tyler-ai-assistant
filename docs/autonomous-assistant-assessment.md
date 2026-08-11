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

The verification pass also closed failure paths that were not safe enough in the initial implementation: corrupt autonomy state now leaves a durable recovery-required marker instead of silently reseeding on the next run; a crashed/cancelled Company runner blocks its project and closes reservations; cancellation waits for an already-started paid worker thread before budget reconciliation; larger bounded internal worker results and explicit latest-candidate prompts prevent review feedback from chasing the smaller Telegram/report copy; an item with no explicit acceptance criteria cannot route or execute; `/autorun retry <item-id>` provides a locked, audited owner recovery path without starting paid work; and `/autorun promote <idea-id> [project-id]` stages a no-cost owner confirmation before one atomic, idempotent proposal-to-roadmap write.

A controlled production smoke test on 2026-08-02 verified the Railway mounted volume, Phoenix-time scheduler registration, Telegram `/autorun live` trigger, metered Lumen proposal generation, persisted run report, and aggregate Telegram summary. A later live test verified idea promotion and selection, but exposed a context handoff defect: the promoted worker was asked to compare recent runs without receiving their reports, spent both allowed revision rounds, and the idle creative fallback then masked the blocker as `ideas_proposed`. The current branch fixes that path with a relevance-gated, bounded/redacted five-run snapshot supplied only as transient execution context (global run fields are labeled; task details are current-project only); immediate terminal classification for explicit external dependencies; blocker-first aggregate status; and suppression of ideation after a blocker. Those corrections are offline-tested but remain unverified in production until deployment and a live retry. Sessions remain sequential; owner retry and owner-confirmed idea promotion are supported, while skip, accept-as-is, criteria editing, and automatic rescoping remain deferred.

The 2026-08-02 real-project follow-up adds the missing safe bridge from reviewed repository manifests into already-persistent Railway state. `/autorun queue <manifest-id>` plus same-owner `/confirm` revalidates an immutable manifest revision under the run/state locks, creates a pre-import backup, and appends only collision-free `observe`/`propose` goals and items with durable provenance and idempotency receipts. The included assistant production-readiness pack is offline-tested to select seven source-backed tasks and reserve about $4.572 of ordinary worker/reviewer capacity under the checked-in pricing configuration. Actual provider spend remains usage-based and may be lower; the system does not manufacture output to hit a dollar target. This extension is unverified in production until its deployment, persistent-state import, and no-spend dry-run smoke test complete.

## Verification against the 2026-08-11 Revenue Sprint working tree

The current working tree extends the autonomous control plane with one deliberately narrow, owner-confirmed revenue experiment rather than general-purpose marketing authority. The checked-in pack binds 20 Phoenix weekdays to one existing Gumroad product, one dedicated company-owned Bluesky identity, one standalone publish action per run-day, a $5 daily AI ceiling including reserve, a $100 campaign ceiling, day-5/day-15 checkpoints, a consecutive-no-progress cutoff, and an unconditional day-20 stop. Personal-account fallback and automated account registration are explicitly rejected.

Campaign execution now follows draft, review, and deterministic action stages. The worker receives propose-level authority and returns a strict campaign-draft envelope; Vera reviews that exact candidate with bounded revisions; the coordinator may execute only after atomically matching the active campaign/run/project, action target and policy revision, worker and reviewer records, final verdict, and exact approved payload digest. Model workers never receive the provider mutation tool. Revision reservations remain attributed to the campaign and must fit both the ordinary daily budget and campaign budget. A successful provider receipt is action evidence, not commercial progress, so it cannot reset the no-progress counter without a persisted sale or supported engagement signal.

The coordinator is now action-generic without broadening the checked-in campaign. A
new owner-confirmed revision can bind a signed publish webhook, one exact-recipient
company Gmail message, a company Vercel deployment pinned to an immutable commit, or
a fixed-amount signed purchase webhook to the same worker/Vera/digest/claim sequence.
Gmail and Vercel receive read-only identity preflights and execution-time rechecks;
webhook success requires an exact signed receipt; purchase amount is fixed in company
target configuration and checked against campaign plus operator caps. A process crash
that leaves a mutation `claimed` stops the next run for provider reconciliation instead
of permitting a new run ID to repeat it. These additional adapters are not present in
the included Bluesky-only manifest and remain inactive unless the owner confirms a new
revision and supplies its dedicated company configuration.

The 2026-08-11 scheduled production-readiness audit also exposed an ambiguous read-tool boundary: Patch selected the generic `GITHUB_REPO` file mirror (`patch-files`) instead of the scoped assistant code repository and correctly stopped for missing access. Enforced autonomous project work now withholds generic `github_*` mirror reads and exposes only `code_*` reads under the selected `projects.json` project scope. Manual non-autonomous GitHub behavior is unchanged.

The current working tree also closes the native Bluesky measurement gap with a
read-only public metrics adapter. Before a campaign day is claimed, the coordinator
fetches cumulative metrics for prior successful post receipts; after claim it stores
that result as the run's `before` snapshot. After a verified publish it fetches again
and stores the `after` snapshot. Every response must match one exact persisted URI and
CID. Only like, reply, repost, and quote increases above each post's persisted
high-water counts count as day-5 meaningful
interest. A publish receipt alone remains non-progress, and day 15 still requires a
Gumroad sale or an explicitly supported strong-intent signal. Read/persistence failures
fail closed; an already-verified post is not blindly retried, and dry runs do not enter
the provider path.

Activation remains a two-step owner action. Queueing previews and imports the manifest; confirmation performs read-only product/account preflight and activates the policy, but makes no OpenAI call and publishes nothing. If Railway restarts after import but before activation, re-queueing the same intact pack safely stages activation only. On 2026-08-11, the final full offline suite passed 570 tests with paid/provider traffic mocked; the modified Python modules also passed bytecode compilation and the complete diff passed whitespace validation. Live Gumroad identity, Bluesky authentication/publishing, public engagement reads, Telegram delivery, Railway-volume persistence, scheduled 08:00 execution, and every newly generalized non-Bluesky adapter remain unverified until a credentialed production smoke test is deliberately run.

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
3. The selected specialist runs through the shared bounded tool loop with its curated tools. In Telegram, ordinary reactive calls are routed deterministically against the model catalog and current per-turn budget envelope; explicit autonomous/Company routes remain authoritative.
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
- Human gates for email sending, deletion, production deploys, Railway mutation, publishing, and other staged actions. The current branch preserves those gates for ordinary work and adds only the narrow, separately owner-confirmed, revision-bound Revenue Sprint coordinator exception described above; the checked-in sprint remains Bluesky-only.
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
- Several non-campaign helpers can still mutate external systems if imported directly. Revenue Sprint mutation adapters now add their own exact-target capability, durable pre-I/O claim, and receipt checks, but that narrow defense does not make every legacy helper independently authorized.
- Company Mode auto-approves some production of files/PR artifacts. `write_file` may mirror to GitHub, so it is not always purely local.
- Railway variable reads can return raw values to a model. This should not be used for secrets.
- Full agent answers are printed to process stdout, which may place private project content in hosting logs.
- Connector errors may include remote response text and should be redacted before durable logging or Telegram delivery.
- Pending confirmations and in-process runner locks are lost on restart.
- Linear mirroring can duplicate issues if the process crashes after creating an issue but before saving its ID.
- Per-chat locks prevent some local collisions, but separate bots/chats can still execute concurrently against shared state.
- The repository correctly gitignores `.env`, credentials, tokens, memory, state, and logs. Those protections must be preserved.

## Scope decision

The autonomous session should remain sequential and bounded: at most ten distinct roadmap items, one attempt per item, and 120 minutes. Task-local blockers should not prevent unrelated work from continuing. Controlled Lumen ideation is an idle fallback only, runs as one batch of at most three ideas, and writes `proposed` backlog records that never auto-execute. Parallel agents, ordinary automatic deployment/merge/publish, broad helper rewrites, database replacement, and a full UI are intentionally deferred. The only automatic external-action exception is the deterministic post-review coordinator inside an active, separately owner-confirmed Revenue Sprint; it does not grant a model general sending, publishing, purchasing, or deployment authority.
