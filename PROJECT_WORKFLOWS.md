# Project workflows: multi-project + Linear

This assistant is a multi-project operating system. It understands several of your
repos, lets you pick an "active project", targets the GitHub pull-request workflow
at whichever project is active, and turns ideas/sprints/PRDs into **Linear**
issues — all while keeping the existing safety behavior (reads are free, code
ships only via PRs you merge, Linear issues are only created on explicit intent).

## 1. Configuring project profiles

Projects live in [`projects.json`](projects.json) at the repo root. Each entry is:

```json
"card-tracker": {
  "name": "Card Tracker",
  "repo": "tymedina100/card-tracker",
  "type": "sports and Pokemon card price comp tracker / flipping dashboard",
  "default_branch": "main",
  "commands": { "test": "pytest", "lint": "ruff check .", "dashboard": "cardtracker dashboard" }
}
```

- `repo` — `owner/name`; this is what the GitHub code tools target when the
  project is active.
- `default_branch` — the base branch PRs target.
- `commands` — a free-form map of the project's common commands (shown by
  `/project commands`).

Add a project by adding another key. To override the bundled registry on a cloud
deploy, drop a `projects.json` into your `DATA_DIR` — it's preferred over the
bundled one.

For any long-running deployment, point `DATA_DIR` at one shared mounted volume (for
example `/app/data`). Project selection, Company Mode, autonomous roadmap state, run
reports, reminders, memory, and Office state then survive redeploys together. When
`DATA_DIR` is unset, the code uses Railway's `RAILWAY_VOLUME_MOUNT_PATH` when available,
then the repository directory.

Worthlane uses the `vantage` project key and intentionally targets the
`tymedina100/vantage` GitHub repo. Local folder names may differ and should not
be treated as the canonical repo name.

## 2. Switching the active project

```
/project list                 # all projects, marks the active one
/project use vantage          # make vantage active (code tools now target its repo)
/project current              # show the active project + repo
/project status               # active project + exact code-repo/branch target
/project commands             # the active project's configured commands
```

The active choice is persisted to `active_project.json` in `DATA_DIR`, so it
survives a restart of the long-running Telegram process. When **no** project is
selected, the code tools fall back to the `GITHUB_CODE_REPO` / `GITHUB_CODE_BASE`
env vars exactly as before — nothing breaks.

## 3. GitHub PRs follow the active project

Once a project is active, Patch's code tools — `code_list_files`,
`code_read_file`, `code_propose_change`, `code_edit_file` — operate on that
project's repo and default branch. The workflow is unchanged and still safe:

- Work lands on a feature branch, never directly on `main`.
- A pull request is opened (and reused across a multi-file change).
- **Nothing ships until you merge.**

Suggested branch names follow `ai/<project-key>/<short-task>`, e.g.
`ai/vantage/add-safe-to-spend-card`, `ai/card-tracker/watchlist-alerts`. Tool
responses name the repo/branch being changed so you always know what's targeted.

> Scope note: `/project use` retargets the **code-PR repo only**. The `write_file`
> file-mirror repo (`GITHUB_REPO`) stays env-controlled and is not affected.

## 4. Configuring Linear

Linear uses environment variables only (no secrets committed). Add to `.env`:

```env
LINEAR_API_KEY=your-linear-api-key
LINEAR_TEAM_ID=your-default-linear-team-id
LINEAR_WORKSPACE_ID=optional-workspace-id
LINEAR_DEFAULT_PROJECT_ID=optional-default-project-id
```

Get the API key at **Linear → Settings → API → Personal API keys**. `LINEAR_TEAM_ID`
is the team new issues land in. Optionally map an assistant project key to a real
Linear project with `LINEAR_PROJECT_ID_<KEY>` (e.g. `LINEAR_PROJECT_ID_VANTAGE`,
`LINEAR_PROJECT_ID_CARD_TRACKER`). If no project id is configured, issues are
still created in the default team and tagged in the description with the project
key + GitHub repo, so they stay traceable:

```md
---
Created by Tyler AI Assistant
Project key: vantage
GitHub repo: tymedina100/vantage
```

If `LINEAR_API_KEY` isn't set, every `/linear` command returns a friendly
"Linear isn't configured (set LINEAR_API_KEY)." message instead of failing.

### Linear commands

```
/linear teams                              # list Linear teams
/linear projects                           # list Linear projects
/linear issues                             # recent issues
/linear search <query>                     # search issues by text
/linear create <title>                     # create an issue (tied to the active project)
/linear create-project-issue <key> <title> # create an issue for a specific project
/linear from-sprint <key> <goal>           # generate + create a sprint's worth of issues
```

Reads are always safe. Creation happens only when you explicitly ask — typing
`/linear create ...` or `/linear from-sprint ...` **is** the intent. When you ask
the Linear specialist (in chat) to create several issues at once, it shows the
proposed titles and asks before creating the batch. There is no destructive
delete command.

## 5. Planning without writing code

These generate structured plans; they don't create anything by themselves.

```
/project brainstorm [<key>|current] <idea>     # 10 ranked ideas + top 3 + which to file
/project sprint     [<key>|current] <goal>     # 1-week plan, 3-7 tasks w/ acceptance criteria + branch names
/project prd        [<key>|current] <feature>  # problem, users, solution, stories, scope, non-goals, AC, tech notes, issue breakdown
```

## 6. Example workflows

**Ship a small feature end-to-end for Card Tracker:**

```
/project use card-tracker
/project brainstorm make this sellable
/project sprint launch beta for Blake and me
/linear from-sprint card-tracker launch beta for Blake and me
/code implement the first small task from the sprint as a PR
```

**Spec then file an issue for Vantage:**

```
/project use vantage
/project prd safe-to-spend today widget
/linear create Add safe-to-spend widget
/code propose a PR for the API/model changes only
```

## 6b. Company Mode + Linear (the tracker for the autonomous company)

Company Mode is the supervised execution engine: `/assign <goal>` plans a project +
tasks and reserves budget, `/approve` runs it (research → build → write → editor
review), metering spend as it goes. The autonomous daily-run control plane described
below can invoke this same bounded engine for an approved roadmap item. When
`LINEAR_API_KEY` is set, **Company Mode mirrors that
work into Linear automatically** so Linear is your live board for the company:

- **At `/approve`** (not `/assign` — a proposal you can still `/cancel` never touches
  Linear), each task becomes its own Linear issue. The group posts the created issue
  list with links.
- **As the engine works** each task, its issue follows along:
  `Todo → In Progress → Done`, and the agent's result + any PR/file deliverables are
  posted as a comment on the issue when the task finishes. Blocked tasks get a
  `⛔ Blocked: …` comment.
- **Revision rounds** (when the Managing Editor requires changes) create issues for
  the new tasks too.
- `/status` shows each open task's Linear id (e.g. `[VAN-53]`).

Configuration: it reuses `LINEAR_API_KEY` + `LINEAR_TEAM_ID` (the same ones the
`/linear` commands use). If a project is active (`/project use …`), issues are tagged
to that project/repo; otherwise they're tagged to the company project in the
description. No Linear config → Company Mode runs exactly as before (no mirror, no
errors). Nothing here changes the budget/approval gates — it only reflects the work
onto the board.

Typical flow:

```
/setbudget 20
/assign launch a beta watchlist for card flippers
/approve            # creates the Linear issues, starts the work
/status             # see progress + Linear ids
```

### Ask the company to complete an existing Linear issue

`/linear do <issue>` turns an issue you already have (e.g. one from a sprint) into a
supervised Company Mode project:

```
/linear do VAN-46
/approve
```

- Reads the full issue (title + acceptance criteria), **plans a tailored team** for it
  (the same dynamic planner `/assign` uses — e.g. research + code + write as the issue
  needs, not just one builder), and always adds a Managing Editor review task.
- **Auto-targets the right repo:** it maps the issue's Linear project (e.g. "Worthlane")
  to the matching assistant project (`vantage`) via the `linear_project` field in
  `projects.json` and makes it active, so the code tools read that repo — no need to
  `/project use` first. Only Patch (code) can read/verify a repo, so the planner routes
  any "review the repo / verify config / implement" work to Patch (not research/task).
- The **source issue itself** is the tracker — no duplicate per-task issues. It moves to
  **In Progress** on `/approve` and to **Done** once the editor approves; if the editor
  requires changes, it stays In Progress and the required changes are posted as a comment.
- **The editor can escalate instead of looping.** If a task can't be finished by the team
  (it needs access/credentials/a dashboard-or-runtime check/a decision only you can make),
  the Managing Editor returns **"BLOCKED — needs human review"**: production stops, the
  issue is **not** marked Done, and the group posts exactly what you need to provide. Fix
  it (or paste the info), then re-run `/linear do <ID>`. A hard cap of 2 revision rounds
  also auto-escalates the same way, so an unsatisfiable task can't loop forever.
- Same budget + `/approve` gate as any Company Mode project. `/cancel` before approving
  touches nothing.

## 6c. Autonomous roadmap selection

The repository now has two complementary project representations:

- `projects.json` maps a short project key to its GitHub repo, default branch, Linear
  project, and common commands.
- `config/autonomous-roadmap.json` seeds goals and actionable roadmap items for the
  daily coordinator. It is structured work state, not a large manager prompt.

The included seed is deliberately illustrative. A roadmap item uses fields such as:

```json
{
  "id": "AUTO-EXAMPLE-001",
  "goal_id": "assistant-autonomy",
  "title": "Validate the autonomous daily-run configuration",
  "priority": 100,
  "status": "ready",
  "dependencies": [],
  "blockers": [],
  "acceptance_criteria": [
    "The schedule, timezone, and dry-run setting are reported.",
    "No external or destructive action is performed."
  ],
  "agent_owner": "manager",
  "task_type": "status_update",
  "complexity": "lightweight",
  "risk": "low",
  "required_capabilities": ["text"],
  "authorization_level": "observe",
  "estimated_input_tokens": 1200,
  "estimated_output_tokens": 300,
  "previous_attempts": [],
  "previous_models": [],
  "human_decision_required": false,
  "human_action": ""
}
```

The coordinator ignores paused/completed projects, blocked or human-decision items, and
items whose dependencies are not complete. Among the remaining `ready`, `planned`,
`pending`, `todo`, `deferred`, or `retry` items, higher item priority wins, followed by
project priority and stable source order.

On first use, the seed becomes `DATA_DIR/autonomy_state.json`. That persistent file then
owns item status, attempts, previous models, idea backlog, budget tracking, active-run
claim, scheduled-date idempotency, and recent runs; later edits to the seed are not
automatically merged. Each run report is written to
`DATA_DIR/autonomous_runs/<run-id>.json`.

Put credential names or required-access notes in blockers, never credential values.
State/report persistence applies secret-pattern redaction, but redaction is a backstop,
not a reason to store private data in the roadmap.

Safe local inspection from PowerShell:

```powershell
$env:AUTONOMY_DATA_DIR = Join-Path $env:TEMP "ai-assistant-autonomy-dry-run"
.\.venv\Scripts\python.exe .\autonomous_workflow.py --dry-run --json
```

Safe Telegram inspection while `group_bot.py` is running:

```text
/autorun status
/autorun dry-run
```

Status and dry-run work in the group or a Miles DM. `/autorun live` is group-only.
Status also lists stable IDs for current proposed ideas. Promotion is deliberately
group-only and approval-gated:

```text
/autorun promote <idea-id>
/confirm
```

The first command builds a read-only preview containing the target project, generated
acceptance criteria, `ready` status, and `propose` authorization. It invokes no model and
changes no roadmap state. `/confirm` revalidates the current persisted proposal and
atomically marks it promoted while adding exactly one roadmap item; it does not start a
run. If several projects are active, append the intended project ID to the promote
command. Use `/autorun dry-run` after confirmation to inspect what would be selected.

Scheduling is disabled by default. `AUTONOMY_ENABLED=true` registers the weekday job;
`AUTONOMY_DRY_RUN=true` keeps scheduled runs audit-only and sends no Telegram message.
The defaults are `08:00`,
`mon-fri`, `America/Phoenix`, a `$5.00` daily ceiling, and a `$0.25` emergency reserve.
Set `AUTONOMY_DRY_RUN=false` only after reviewing dry-run reports, then restart the group
worker; `/autorun live` uses the existing sequential Company Mode execution/review path.
It defers if Company Mode is paused, a supervised/open Company project exists, or an
owner confirmation is pending. In the Telegram
runtime, the persisted Company Mode ledger (`/setbudget`) is authoritative for routing
and reports; the `AUTONOMY_*` budget values are the standalone/control-plane fallback.

`AUTONOMY_MAX_AUTHORIZATION` defaults to `propose`. Items may request `observe`,
`propose`, `modify_local`, or `external_action`; a request above the ceiling stops in
`needs_human`. This slice auto-executes only observe/propose. `modify_local` also stops
for the owner because the current Python and GitHub helpers are network/remote-capable,
not an isolated checkout boundary. The ceiling does not bypass the existing PR,
`/confirm`, send, deploy, publish, delete, or production gates. `external_action`
remains human-only. A no-roadmap-work run may propose a bounded, deduplicated idea into
`idea_backlog`; it never starts building the idea and defers before routing if supervised
work or a confirmation is open. A proposal becomes actionable only through the explicit
promotion/confirmation flow above, and the queued validation remains proposal-only.

Model selection comes from `config/model-catalog.json` or `MODEL_CATALOG_FILE`. Prices,
capabilities, availability, and context limits are operator-maintained snapshots used
for estimates; enabled prices and source URLs were refreshed from official OpenAI model
pages on 2026-07-27. Live Company Mode worker/reviewer and idle-ideation reservations are
atomic on the shared filesystem and reconciled from API usage where available; missing
exact usage is labeled estimated. `AUTONOMY_COST_ESTIMATE_MULTIPLIER` and
`AUTONOMY_MIN_TASK_RESERVATION_USD` conservatively expand a one-response route estimate
for bounded tool loops. `BUDGET_TIMEZONE` controls the shared ledger date. A task beyond
`AUTONOMY_TASK_TIMEOUT_SECONDS` is awaited and charged but receives no retry; the limit
does not kill a Python thread. See the full environment variable table in
[README.md](README.md) and the illustrative report at
[`docs/sample-autonomous-daily-run-report.json`](docs/sample-autonomous-daily-run-report.json).

Current scope is intentionally narrow: one roadmap item at a time and at most ten per
session, one shared filesystem rather than distributed locking, and no claim that a new
credentialed Telegram/OpenAI/Railway path works until its controlled smoke test is run.

Live reports attribute the nested worker/reviewer models and costs by project, task,
agent, and model. Their task-level `model_reason` values remain in `company_state.json`;
the coordinator route reason is copied into the run report. Intermediate autonomous
agent chatter is suppressed, leaving final summaries and actionable escalations.

## 6d. Deploying a site live (Vercel)

Patch and Sway can put a landing page live with the `deploy_site` tool (Vercel).

Setup:
- Create a Vercel token (**Vercel → Settings → Tokens**) and set `VERCEL_TOKEN`
  (`VERCEL_TEAM_ID` too if the project belongs to a team).
- The Vercel **project must be linked to its GitHub repo** — that's what a deploy builds
  from. (One-time, in the Vercel dashboard.)

Behavior (safety):
- **Preview** deploys are throwaway URLs that don't touch your live domain — they run
  **immediately**, so the company can iterate and show work.
- **Production** deploys touch the live domain and are **gated behind `/confirm`** (the
  same staging flow as sending email). During a Company Mode run a production deploy
  blocks the task until you `/confirm`.
- `list_deploy_projects` (read-only) finds the exact project name; `check_deploy <id>`
  polls build status.

Example — the full loop, live:

```
/linear do VAN-XX          # "ship the card-flipping landing page"
/approve                   # Patch builds the page (PR) + deploys a PREVIEW URL
# you review the preview, then when it's ready:
@Patch deploy <project> to production
/confirm                   # goes live on the real domain
```

## 6e. Railway infrastructure (`railway_*`)

Patch can inspect (and, with `/confirm`, change) the backend on Railway — so a
"verify the Railway release config" task is actually verifiable instead of blocked.

Setup: create a Railway account token (**Account Settings → Tokens**) and set
`RAILWAY_TOKEN`.

Reads (safe): `railway_list_projects` → `railway_get_project` (gives service +
environment ids) → `railway_list_vars` (variable **names only**), `railway_get_var`
(one value — for checking a specific var like `EXPO_PUBLIC_API_URL`),
`railway_deploy_status`.

Writes (gated behind `/confirm`, like production deploys): `railway_set_var` (upsert a
variable) and `railway_redeploy` (restart a service). During a Company Mode run these
block the task until you `/confirm`.

> **Secrets:** Railway variables can hold secrets. `railway_list_vars` never returns
> values; `railway_get_var` returns one value on purpose (so Patch can check a specific
> non-secret var) — the value goes to the model, and it's redacted in the logs.

## 7. Telegram: optional Linear bot

The group interface (`group_bot.py`) can run a dedicated **Linear** bot. Set
`TELEGRAM_LINEAR_BOT_TOKEN` and it's enabled automatically; leave it unset and
it's simply skipped (never required, never blocks startup). Either way the Linear
specialist is reachable through Miles's delegation.

## 8. Safety summary

- Reading/listing GitHub, Linear, and projects is always safe.
- GitHub code changes go through feature branches + PRs — never a direct commit
  to `main`.
- Linear issues are created only on explicit command/intent; batch creation asks
  first; no delete command is exposed.
- Company Mode's Linear mirror is triggered by `/approve` (an explicit action);
  `/assign` proposals and `/cancel` never write to Linear. A tracker error is
  swallowed and logged — it can never stop or corrupt the company engine.
- Missing Linear or GitHub config produces a friendly "not configured" message,
  never a crash.
