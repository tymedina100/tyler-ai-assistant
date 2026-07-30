# Tyler AI Assistant

A focused personal operating assistant for Tyler. The MVP is limited to three weekly-use workflows: Linear-driven planning, approval-based Career Ops support, and calendar/tasks/routine help that directly supports action.

## MVP scope: three core workflows

The assistant's V1 exists to help Tyler act, not to collect infinite agents. The core workflows are:

1. **Project planning and daily priorities from Linear.** Read Linear, clarify what matters today, and turn project work into small approved next steps.
2. **Job search support through Career Ops / approval-based workflows.** Help with resume/cover-letter/application/recruiter workflows, but keep external-facing actions approval-gated.
3. **Calendar, tasks, and routine support only where it helps Tyler act.** Use Calendar, Todoist, reminders, and briefings to reduce friction around execution, not to become a general life dashboard.

These workflows remain the default product priorities. The Telegram specialists,
Company Mode, and autonomous daily-run control plane are execution systems for those
priorities; they are no longer merely "later" experiments.

The aim is a bounded, owner-directed AI team, not an uncontrolled do-everything agent
platform. Roadmap priority, budget, authorization ceilings, review limits, and human
approval gates determine what may run.

## Features

- Chat with an AI assistant from the terminal
- Short-term conversation memory during a session
- Long-term memory that survives across restarts, using a local vector database
  (see "How long-term memory works" below)
- Read files from a sandboxed `files/` folder and ask questions about them
- Search the web (via Tavily) and get an AI-summarized answer with sources
- A Manager Agent reads plain chat messages and routes each one to whichever
  specialist (or the general assistant) fits best — no command needed, see "Manager
  agent" below
- `/help` command
- `/clear` command to reset short-term memory (long-term memory is untouched)
- `/history` command to view current short-term memory
- `/read <filename>` command to print a file from `files/`
- `/askfile <filename> <question>` command to ask a question about a file
- `/search <query>` command to search the web and get a summarized answer
- `/remember <fact>` command to explicitly save something to long-term memory
- `/recall <query>` command to see what long-term memory has stored about a topic
- `/brief <notes>` command to turn your context into a focused plan for the day
- Specialist agents, each a named character with a real job title, its own
  personality, and curated tool access, including real external connectors (Todoist,
  OpenWeatherMap, Google, Gumroad) — see "Specialist agents" and "Meet the team" below
- Cost-aware model tiering: a cheaper/faster model handles routing and simple lookups
  while the premium model does the reasoning-heavy work, plus a capped history window
  and a memory-relevance cutoff to keep token cost down — see "Cost: two model tiers"
- The Manager can delegate to multiple agents in sequence for one request, passing
  one agent's findings into the next — see "Manager agent" below
- `/quit` command to exit
- API keys stored safely in a `.env` file (never committed to git)
- Reliability: automatic retries on flaky API calls, a local debug log, specific
  tool-error messages instead of crashes, and a write-overwrite warning — see
  "Reliability features" below
- A Telegram bot interface so you can message the assistant from your phone, with
  a Dockerfile for deploying it somewhere that runs 24/7 — see "Running 24/7" below
- A multi-bot Telegram group interface where every agent is its own real bot with
  its own personality, all in one shared chat with you — plain group messages are
  auto-routed to the best-fit teammate(s) (no Manager gatekeeping), and you can also
  message any agent privately 1:1, including DMing Miles to dispatch the team — see
  "Running the multi-bot group interface" below
- An opt-in autonomous weekday run that selects one actionable roadmap item, routes it
  to a budget-appropriate model, reuses Company Mode for execution/review, and writes a
  structured audit report — disabled and dry-run-only by default

## Setup

Create a virtual environment:

```powershell
python -m venv .venv
```

This project is developed against Python 3.10, which is also what the Dockerfiles
use. Newer Python 3.x versions may work, but 3.10 is the safest match.

Activate it:

```powershell
.venv\Scripts\Activate.ps1
```

Install dependencies:

```powershell
pip install -r requirements.txt
```

Create a `.env` file in the project root with your API keys. For a minimal CLI
setup, `OPENAI_API_KEY` is the only required key. The other keys are optional until
you use the feature that needs them:

You can use `.env.example` as a checklist for the available keys.

```
OPENAI_API_KEY=your-openai-key-here
DATA_DIR=
TAVILY_API_KEY=your-tavily-key-here
TELEGRAM_BOT_TOKEN=your-telegram-bot-token-here
TELEGRAM_ALLOWED_USER_IDS=your-telegram-user-id-here
OPENWEATHER_API_KEY=your-openweathermap-key-here
TODOIST_API_TOKEN=your-todoist-api-token-here

# Optional — proactive scheduling (group bot only), with sensible defaults:
HOME_LOCATION=New York
BRIEFING_TIME=08:00
TIMEZONE=America/New_York
EVENT_ALERT_MINUTES=15

# Optional — mirror every saved file to a GitHub repo (see "Saving files to GitHub"):
GITHUB_TOKEN=your-github-personal-access-token
GITHUB_REPO=your-username/your-files-repo
GITHUB_BRANCH=main

# Optional — let Patch propose PRs to a code repo (see "Self-extending: PRs"):
GITHUB_CODE_REPO=your-username/your-project-repo
GITHUB_CODE_BASE=main
```

- Get an OpenAI key from https://platform.openai.com
- Get a free Tavily key (used for `/search`) from https://tavily.com
- `TELEGRAM_BOT_TOKEN`/`TELEGRAM_ALLOWED_USER_IDS` are only needed if you're running
  the Telegram bot (`bot.py`) — see "Running 24/7" below. Not required for the CLI.
- Get a free OpenWeatherMap key (used by Cadence for weather lookups) from
  https://openweathermap.org/api — note new keys can take up to ~10 minutes (rarely,
  longer) to activate after signup, so a fresh key returning an "Invalid API key"
  error isn't necessarily wrong, just not active yet.
- Get your Todoist API token (used by Sage, the Operations Manager) from the Todoist app:
  Settings → Integrations → Developer. Works with any Todoist account, free or paid.
- Google Calendar/Gmail (used by Cadence and Piper) use OAuth, not an `.env` key —
  run `python google_auth.py` once to connect them. See "Google setup" below.
- The optional scheduling vars only affect the multi-bot group's proactive features
  (morning briefing, reminders, event alerts) — see "Proactive & scheduling" below.

## Usage

Run the assistant:

```powershell
python main.py
```

Type a message and press Enter to chat with the AI, or use one of the commands above.
Files you want to `/read` or `/askfile` must live inside the `files/` folder — paths
outside it are rejected for safety.

### Plan your day with a daily brief

Use `/brief` when you want a short command-center plan from your own context. Include
anything that matters today: deadlines, appointments, energy level, unfinished work, or
constraints. The assistant returns three priorities, flexible schedule blocks, one each
for health, career, and a project, a deliberate deferral, and a reflection prompt.

```text
/brief I have a 2pm dentist appointment, need to finish my resume revision, have low energy, and want to make progress on the card tracker. Ignore non-urgent email today.
```

The command uses only the notes you provide plus relevant saved context; it does not
create calendar events, tasks, or other external changes.

## Running the tests

The project ships with a small `unittest` suite (Company Mode's budget/ledger logic,
the safety "hardening" helpers, and a roster-consistency check that catches miswired
agents). Run it from the project root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The tests stub out the external services (OpenAI, Chroma, Tavily, etc.), so they run
offline and need no API keys. Run them before deploying a change to Railway — they're
fast and catch the obvious regressions in state handling and file safety.

## How long-term memory works

Unlike `conversation_history` (a plain in-memory list that's lost when you `/quit`),
long-term memory is stored in a local [Chroma](https://www.trychroma.com/) vector
database in the `memory_db/` folder. It survives across separate runs of the program.

- Every plain chat message and `/remember <fact>` gets converted into an embedding
  (via OpenAI's `text-embedding-3-small` model — no new API key needed, it reuses
  `OPENAI_API_KEY`) and stored.
- Before answering a plain chat message, the assistant automatically searches
  long-term memory for related past memories and includes them as context — so it can
  recall facts from previous sessions without you asking explicitly. Only memories that
  are actually similar are injected: matches farther than `MEMORY_DISTANCE_THRESHOLD`
  (in `main.py`) are dropped, so a sparse store no longer pastes unrelated facts into
  every prompt (Chroma always returns *some* closest match, relevant or not). Tune the
  threshold against the real distances you see via `/recall`.
- `/recall <query>` lets you inspect this directly: it shows the raw memories Chroma
  thinks are most similar to your query, along with a "distance" score (lower = more
  similar). `/recall` is deliberately **unfiltered** — it ignores the injection cutoff
  above and shows the raw closest matches, so you can see exactly what the store holds
  (including weak matches) instead of it being a black box.
- `memory_db/` is gitignored (it's local generated data, like `.venv/`).

## How automation works

Each capability (read a file, search the web, save/recall a memory, write a file) is
exposed as a tool the AI can call on its own instead of you needing the exact command:

- **Write a file** is the one capability that changes something on disk, so it always
  asks for confirmation first: `Allow? (y/n)`. Answering anything other than `y`
  cancels the write.
- Every autonomous tool call prints a `[tool] name(arguments)` line first, so you can
  always see what's happening and why — it's never a silent black box.
- Slash commands (`/read`, `/askfile`, `/search`, `/remember`, `/recall`, `/code`,
  `/research`, `/write`, `/task`) still work exactly as before — they're deterministic
  and call a specific agent directly, with no routing decision involved.

## Manager agent

Plain chat (anything you type that *isn't* a `/command`) now goes through a **Manager
Agent** instead of talking to one fixed assistant. The Manager's job is to read your
message and get it done by delegating — via tool calls, exactly like the other tools
in this app — to whichever agent(s) fit best:

- One of the nine specialists (`code`, `research`, `write`, `task`, `marketing`,
  `editor`, `finance`, `calendar`, `gmail`)
- The **general assistant** (the same all-purpose, all-tools assistant from Week 4),
  for anything that doesn't clearly fit a specialist

**Multi-step delegation.** Most requests need only one agent, but the Manager can
delegate to *more than one in sequence* when a request genuinely needs it — e.g.
"research the competition, then draft a launch post from the findings" delegates to
the Head of Research first, then to the Head of Marketing with the research result
included in that next delegation's prompt. Every handoff still goes
through the Manager — agents never call each other directly, and architecturally
can't (a specialist's own tool-calling loop only has access to its own curated tools,
never the delegation tools). The Manager has a higher tool-call budget than other
agents (`MAX_MANAGER_TOOL_ITERATIONS = 8` vs. `MAX_TOOL_ITERATIONS = 10`) because
the Manager is expected to spend its budget on delegation chains while specialists
may spend theirs on search, memory recall, and verification before answering.

You'll see one response block per agent involved (e.g. `Scout (Head of Research)
response: ...`, then `Sway (Head of Marketing & Growth) response: ...`), then the
Manager's final answer (`Manager
response: ...`), which relays the result. That's intentional — it's the delegation
chain made visible, not a bug. Note the Manager's routing is a real judgment call by
the model, not a fixed rule: a question like "what's the capital of Norway?" might get
routed to the Researcher (if it decides a fresh lookup is warranted) or straight to the
general assistant (if it judges a well-known fact doesn't need a web search) —
both are reasonable, and which one happens can vary.

`/history` always shows your literal message and the Manager's final answer, never the
Manager's internal tool-call phrasing of the delegated task — the Manager owns that
log entry itself rather than relying on the delegated agent to record it, specifically
so your original wording is never lost or paraphrased in the permanent record.

## Specialist agents

Beyond the general assistant, nine specialists each use the same tool-calling
machinery as plain chat, but with a different system prompt and a **curated subset**
of tools — not every specialist can do everything. Each one is also a **named
character with a real job title and its own personality** (see "Meet the team" below):

| Command | Specialist | Tools it has |
|---|---|---|
| `/code <task>` | **Patch** (Head of Engineering) | read file, write file, search the web, recall memory, run code, GitHub |
| `/research <topic>` | **Scout** (Head of Research) | search the web, recall memory, remember a fact — also runs the news desk |
| `/write <prompt>` | **Quill** (Content Lead) | read file, write file, recall memory |
| `/task <request>` | **Sage** (Operations Manager) | remember a fact, recall memory, write/read file, create/list real Todoist tasks |
| (Manager-only — no direct slash command) | **Sway** (Head of Marketing & Growth) | search the web, read/write file, remember/recall memory |
| (Manager-only — no direct slash command) | **Vera** (Managing Editor) | read file, search the web, recall memory |
| (Manager-only — no direct slash command) | **Ledger** (CFO) | company status, live revenue/P&L report, remember/recall memory |
| (Manager-only — no direct slash command) | **Cadence** (Executive Assistant) | Google Calendar events + timed reminders + weather lookup |
| (Manager-only — no direct slash command) | **Piper** (Communications & Support Lead) | search/read/draft/send email, recall memory |

**Patch can also run code.** The Head of Engineering has a `run_python` tool that actually
executes Python in a sandbox (see "Code execution" below), so it can test and verify
what it writes instead of guessing.

This is deliberate: for example, Scout (Head of Research) *cannot* call `write_file` even
if asked to — it genuinely doesn't have access to that tool, so it'll tell you it can't
and offer the content for you to save yourself, rather than faking it. Scoping each
agent to only the tools its role needs is a basic safety practice (least privilege),
not just an organizational nicety.

**Sage covers both memory and the real task list.** The Operations Manager saves
general facts and preferences to local long-term memory (the same Chroma store
everything else uses) *and* is connected to your actual Todoist account via its REST
API, creating and checking real to-do items there. (These used to be two separate
agents — a Personal Assistant and a Tasks Agent — whose split confused the Manager's
routing; merging them means "remind me to call mom" and "add buy groceries to my
Todoist" both land with Sage, who picks the right tool.)

All agents share the same `conversation_history` and long-term memory as plain
chat and each other — there's one memory store for the whole assistant, not one per
agent. A fact saved via `/task` (Personal Assistant) can be recalled later in plain
chat or by any other specialist.

Specialists share a `MAX_TOOL_ITERATIONS = 10` cap on their own
internal tool calls (separate from the Manager's delegation budget).
Occasionally an agent — the Researcher in particular, since it sometimes re-verifies
a fact with several searches before answering — can exhaust that budget and fall back
to a generic "I tried too many tool calls" message only if the higher budget is still
exhausted. The larger default is meant to keep search-heavy Scout requests from
hitting that fallback during normal use.

## Meet the team

Each agent has a real name and personality, defined once in `main.py` (as a `persona`
layered on top of its functional `role`) and used everywhere — the CLI, the single
Telegram bot, and the multi-bot group all show the same characters:

- **Miles** — the Manager / Chief of Staff. Reads your request and routes it; relays
  each specialist's answer while keeping their voice intact.
- **Patch** — Head of Engineering. Blunt, pragmatic senior engineer.
- **Scout** — Head of Research. Curious fact-hound who cites sources; also runs the
  news desk (headline-led, source-cited briefs).
- **Quill** — Content Lead. Warm wordsmith who cares about tone.
- **Sage** — Operations Manager. Calm and organized; remembers your preferences and
  runs your real Todoist list.
- **Sway** — Head of Marketing & Growth. Sharp and benefit-led; owns positioning,
  landing-page copy, SEO, and launch content. Allergic to hype without proof.
- **Vera** — Managing Editor. The final quality gate: reviews every deliverable
  against a checklist and returns "APPROVED" or a numbered list of required fixes.
- **Ledger** — CFO. Dry and numerate; owns the daily budget, spend, and per-product
  P&L. Leads with the number.
- **Cadence** — Executive Assistant. Unflappable and precise; runs your Google
  Calendar, your reminders, and the weather desk.
- **Piper** — Communications & Support Lead. Brisk and discreet; triages, drafts, and
  sends email, including customer-support triage for the company's products.
- **Dash** — Sales Lead. Energetic but honest; drafts outreach that names a real pain,
  tracks every lead's stage and next follow-up in memory, and reports the pipeline.
  Drafts only — sending stays with you (or Piper, for email).
- **Vega** — Analytics Lead. Crisp and zero-fluff; pulls the real revenue, budget, and
  task numbers and compresses them into a short digest with exactly one recommendation.
- **Robin** — the general assistant (the all-rounder for anything that doesn't fit a
  specialist).

Dash and Vega are the newest hires, added because a traffic/sales-focused solo
business needs someone who owns outreach and someone who owns the numbers (a
dedicated support seat was considered and skipped — Piper already owns customer
email). Like Cadence and Piper before them, they work via Miles's delegation out of
the box and become @mentionable group bots as soon as you create their Telegram bots
and set `TELEGRAM_SALES_BOT_TOKEN` / `TELEGRAM_ANALYTICS_BOT_TOKEN`. Earlier
roster changes: Sway, Vera, and Ledger arrived in the reorg that merged the old News
Agent into Scout and the old Tasks Agent into Sage, and retired the Weather Agent's
seat (the weather *tool* moved to Cadence).

Personalities are just prompt text, so they're easy to retune: edit the `persona`
field on each `SPECIALISTS` entry (or `MANAGER_INSTRUCTIONS` / `ASSISTANT_INSTRUCTIONS`
for Miles / Robin) in `main.py`. The character names live in `main.py` only; the
Telegram group (`group_bot.py`) derives its labels and welcome messages from there, so
a name never drifts between interfaces.

## Cost: catalog-backed model routing

Reactive chat keeps two persona defaults: `PREMIUM_MODEL` for reasoning-heavy agents and
`FAST_MODEL` for routing and routine connector work. Their defaults now come from
[`config/model-catalog.json`](config/model-catalog.json); `OPENAI_PREMIUM_MODEL` and
`OPENAI_FAST_MODEL` can override the reactive aliases.

Autonomous roadmap work is routed per task instead. `model_router.py` considers task
type, complexity, risk, required capabilities, context size, remaining budget, and prior
failures, then selects the lowest-cost enabled model likely to succeed. The chosen model
and reason are stored in the run report. Set `MODEL_CATALOG_FILE` to use a different
catalog snapshot.

Catalog capability claims, context limits, model availability, and prices are
**operator-maintained configuration snapshots**. Cost figures are routing estimates,
not guaranteed current OpenAI prices or a substitute for provider billing. The enabled
snapshot (GPT-5.4 nano, GPT-5.4 mini, and GPT-5.6 Sol) and source URLs were refreshed on
2026-07-27; still verify the catalog against the models and prices available to your
account before enabling live autonomy. Unknown models use a conservative fallback for
Company Mode metering instead of silently recording zero cost.

Two related token-cost guards also live in `main.py`:
- **History window (`MAX_HISTORY_MESSAGES`).** Plain chat resends recent conversation
  to the model each turn; this caps how many past messages are sent so cost doesn't grow
  unbounded over a long session. `/history` still shows the full record — only the
  model input is windowed, since long-term memory recall already carries older context.
- **Memory relevance cutoff (`MEMORY_DISTANCE_THRESHOLD`).** See "How long-term memory
  works" below — recalled memories that aren't actually similar are no longer injected
  into prompts.

## Code execution (Patch)

Patch (the Head of Engineering) has a `run_python` tool that actually executes Python and
returns its stdout, stderr, and exit code — so it can test what it writes rather than
guessing. Execution is **sandboxed, but pragmatically, not as a hard security jail:**

- Runs in a **child process** with a wall-clock timeout (`CODE_EXEC_TIMEOUT_SECONDS`,
  default 10s), so an infinite loop is killed rather than hanging the bot.
- Runs in an isolated throwaway working directory (`sandbox/`, gitignored), separate
  from the `files/` folder, so it can't clobber your read/write area.
- The child process gets a **secret-free environment** — `OPENAI_API_KEY`,
  `TODOIST_API_TOKEN`, etc. are stripped, so executed code can't read your keys.
- On Linux it also caps CPU, memory (`CODE_EXEC_MEMORY_MB`), and open files via
  `resource` limits. (Windows relies on the timeout — its process model has no
  equivalent.)

What it does **not** do: it can still read the local disk and reach the network. Don't
point it at untrusted third-party code. For a single-user personal assistant running
your own requests, that trade-off is deliberate.

## Saving files to GitHub

Files that agents write with `write_file` normally land in the `files/` folder — which
is fine locally, but on a cloud deploy that folder is **inside the container**, wiped on
every redeploy and unreachable from your PC. Set two env vars and every saved file is
also committed to a GitHub repo, so it persists across redeploys and you can grab it
from anywhere (`git pull`, or just github.com):

- `GITHUB_TOKEN` — a GitHub Personal Access Token with write access to the repo.
- `GITHUB_REPO` — `owner/repo` of a repo you created for this (e.g. a dedicated
  `patch-files` repo, so your app's code repo stays clean).
- `GITHUB_BRANCH` — optional, defaults to `main`.

**Setup:**
1. Create an empty GitHub repo (e.g. `patch-files`) — add a README so it has a `main`
   branch.
2. Create a token: GitHub → Settings → Developer settings → **Fine-grained tokens** →
   scope it to just that repo with **Contents: Read and write**. Copy it.
3. Put both values in `.env` locally, and set them as env vars on your deploy platform.

When configured, `write_file` commits via the GitHub contents API (one HTTPS request —
no git binary needed in the container) and the tool result includes the file's GitHub
URL, so the agent can hand you the link right in chat. If the vars aren't set, it's a
silent no-op and files just save locally as before. This is how a cloud-run bot gets
code onto your PC without touching your machine directly.

**Patch has full repo access.** Beyond the automatic `write_file` mirror, the Coding
Agent gets first-class tools to work on the repo like a developer:

- `github_list_files` — browse the repo (a folder path, or the root).
- `github_read_file` — read an existing file's contents.
- `github_save_file` — create or update a file directly at any repo path (commits
  immediately).
- `github_delete_file` — remove a file; **gated behind `/confirm`** like other
  sensitive actions. Deletes stay recoverable from git history.

All four use the same `GITHUB_TOKEN`/`GITHUB_REPO` config and need no extra GitHub
scopes beyond **Contents: Read and write**. So you can ask Patch to "read `app.py` from
the repo and fix the bug," and it'll pull it, edit it, and commit the fix back.

### Self-extending: Patch proposes code changes as pull requests

Point Patch at a *code* repo (e.g. this assistant's own repo) and it can improve the
project itself — but safely, via **pull requests you review**, never straight to the
live branch. Configure a separate code repo:

- `GITHUB_CODE_REPO` — `owner/repo` of the project Patch may propose changes to (falls
  back to `GITHUB_REPO` if unset). Keep this distinct from your file-mirror repo.
- `GITHUB_CODE_BASE` — the branch PRs target, default `main`.
- The token needs, **on that repo**, both **Contents: Read and write** *and* **Pull
  requests: Read and write** (a fine-grained token can list multiple repos).

Patch's tools for it:
- `code_list_files` / `code_read_file` — study the codebase (reads now return whole
  files, not just the first page).
- `code_edit_file(branch, path, old_snippet, new_snippet, title, body)` — the workhorse
  for changing an **existing** file: replaces one exact, unique snippet and commits to
  the branch, so Patch never has to reproduce a large file like `main.py` wholesale.
- `code_propose_change(branch, path, content, title, body)` — commit a **new** file (or
  a full-file rewrite). Reuse the branch name across a multi-file change; the PR is
  created once and reused.

**Nothing ships until you merge.** The PR is the review gate — Patch can only *propose*.
After you merge, redeploy for it to take effect. This is the "self-extending assistant"
loop: ask in Telegram ("add a Spotify agent"), Patch opens a PR, you review and merge.
Because Patch is the Head of Engineering, its internal tool-call budget is also a bit higher
(`max_iterations: 8`) to fit a browse → read → propose flow in one turn.

### Multiple projects + Linear

The assistant can work across several repos (this one, `vantage`, `card-tracker`) and
turn plans into **Linear** issues. Select an active project with `/project use <key>`
and the `code_*` tools target that project's repo; add `LINEAR_API_KEY` and the
`/linear` commands create and read issues. See **[PROJECT_WORKFLOWS.md](PROJECT_WORKFLOWS.md)**
for the full guide, commands, and example workflows.

## Proactive & scheduling (Telegram group)

When the multi-bot group (`group_bot.py`) is running, an `APScheduler` loop lets the
team message you unprompted:

- **Daily morning briefing.** At `BRIEFING_TIME` (in your `TIMEZONE`), Miles posts a
  short briefing pulling today's weather (`HOME_LOCATION`), your open Todoist tasks, and
  today's calendar. It's written in Miles's voice from the raw facts.
- **Timed reminders.** Ask Cadence (via Miles or, once she has a bot, `@mention`)
  "remind me at 3pm to call mom". Reminders are stored in `reminders.json` (gitignored)
  so they **survive a restart/redeploy** — on startup the bot re-schedules future ones
  and fires any that came due while it was offline (flagged as "missed").
- **Calendar event alerts.** A heads-up posts `EVENT_ALERT_MINUTES` before each of
  today's timed calendar events. Today's alerts are (re)scheduled at startup and again
  after each morning briefing.
- **Company Mode daily report.** At `DAILY_REPORT_TIME`, Miles posts a supervised
  startup-style report with the active project, open work, reserved/spent budget, and
  the next recommended move.
- **Autonomous roadmap run.** When `AUTONOMY_ENABLED=true`, a separate weekday cron job
  selects at most one actionable roadmap item and applies the configured budget and
  authorization gates. Live runs post the final summary/escalations; scheduled dry-runs
  write the audit report without sending Telegram.

These scheduled jobs run only in the group interface (that's where a persistent process
lives); the standalone autonomy CLI can still trigger a manual dry-run. The CLI and
single bot store reminders but don't fire them. Configure the existing briefing/report
jobs via `.env`:
`HOME_LOCATION`, `BRIEFING_TIME` (e.g. `08:00`), `TIMEZONE` (e.g. `America/New_York`),
`EVENT_ALERT_MINUTES` (e.g. `15`), and `DAILY_REPORT_TIME` (e.g. `18:00`).

## Autonomous daily runs (safe vertical slice)

The autonomous layer extends the current system rather than replacing it:

1. `autonomous_workflow.py` loads structured project/roadmap state, claims a
   cross-process file lock, and selects the highest-priority unblocked item whose
   dependencies are complete.
2. `model_router.py` chooses a configured capable model and records why.
3. `autonomy_team.py` converts a selected item into one bounded worker task plus a
   separately routed Vera acceptance-criteria review, while intersecting the agent's
   normal tool set with the roadmap authorization level.
4. A live run uses the existing Company Mode sequential runner, artifact handoff,
   bounded revisions, budget ledger, and approval gates.
5. The coordinator reconciles usage, updates state, writes a JSON run report, posts the
   substantive completed worker result through the existing chunked Telegram transport,
   and then posts a concise summary or owner escalation. Reviewer feedback remains
   separate from the worker deliverable in the audit record.
6. Only when no roadmap work is actionable may Lumen propose a limited, deduplicated
   idea. It enters the backlog as `proposed`; it is not automatically built.

### Safe setup and manual dry-run

Defaults are deliberately inert: scheduling is disabled and execution is dry-run. Copy
the autonomy block from `.env.example`, keep `AUTONOMY_ENABLED=false` and
`AUTONOMY_DRY_RUN=true`, then exercise the local control plane without an OpenAI or
connector call:

```powershell
$env:AUTONOMY_DATA_DIR = Join-Path $env:TEMP "ai-assistant-autonomy-dry-run"
.\.venv\Scripts\python.exe .\autonomous_workflow.py --dry-run --json
```

The standalone command reads the current process environment rather than `.env`; the
command above intentionally relies on conservative defaults. It writes only local audit
state and a report below the temporary directory. It performs no paid model call,
Telegram/connector call, code change, deploy, publish, delete, or other external action.

Run the critical offline tests from the repository root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_autonomous_workflow.py" -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_autonomy_team.py" -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_group_autonomy.py" -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_model_router.py" -v
```

With the multi-bot group running, ask Miles for the current configuration/next item or
trigger a dry-run. Both commands also work in a Miles DM:

```text
/autorun status
/autorun dry-run
```

After reviewing dry-run selection, routing, budget, and authorization output, live mode
requires deliberate configuration: set `AUTONOMY_ENABLED=true`, set
`AUTONOMY_DRY_RUN=false`, and choose an appropriate
`AUTONOMY_MAX_AUTHORIZATION`, then restart `group_bot.py`. `/autorun live` in the
**group operating room**
invokes the bounded Company Mode path; live starts are refused in DMs. It can spend API
budget only within the configured authorization ceiling. This vertical slice
auto-executes only `observe` and `propose`; `modify_local` and `external_action` stop for
owner review because the existing Python/GitHub helpers are not a true isolated-local
boundary. Existing `/confirm`, PR, deployment, publishing, email, and delete gates still
apply. Do not grant broader permissions merely to suppress an escalation.

A live run defers without competing for work when a supervised Company Mode plan is
already running, another Company project remains open, Company Mode is paused, or a
Telegram confirmation is already waiting for the owner.

### Scheduler and configuration

The autonomous job is registered only inside the long-running `group_bot.py` process.
Development defaults are **08:00, Monday-Friday, America/Phoenix, $5/day**, with a
**$0.25 emergency reserve**, one roadmap item, and at most one proposed idea per run.
Use these canonical environment variables (the complete copy-ready block is in
`.env.example`):

| Variable | Default / purpose |
| --- | --- |
| `AUTONOMY_ENABLED` | `false`; no autonomous cron job is registered |
| `AUTONOMY_DRY_RUN` | `true`; scheduled runs plan/report locally and send no Telegram message |
| `AUTONOMY_SCHEDULE_TIME` | `08:00` |
| `AUTONOMY_SCHEDULE_DAYS` | `mon-fri` (APScheduler weekday expression) |
| `AUTONOMY_TIMEZONE` | `America/Phoenix` (IANA name) |
| `BUDGET_TIMEZONE` | `America/Phoenix`; Company ledger day boundary, normally aligned with autonomy |
| `AUTONOMY_DAILY_BUDGET_USD` | `5.00`; standalone/control-plane fallback ceiling |
| `AUTONOMY_EMERGENCY_RESERVE_USD` | `0.25`; standalone/control-plane fallback reserve |
| `AUTONOMY_COST_ESTIMATE_MULTIPLIER` | `4.0`; expands one-response estimates for bounded tool loops |
| `AUTONOMY_MIN_TASK_RESERVATION_USD` | `0.05`; minimum live worker/reviewer reservation |
| `AUTONOMY_MAX_IDEAS_PER_RUN` | `1`; set `0` to disable idle ideation |
| `AUTONOMY_IDEA_BACKLOG_LIMIT` | `50` proposed ideas retained |
| `AUTONOMY_MAX_EXECUTION_ATTEMPTS` | `2` roadmap-level failed attempts before owner escalation |
| `AUTONOMY_TASK_TIMEOUT_SECONDS` | `900`; monitoring ceiling; late threads are awaited, charged, and not retried |
| `AUTONOMY_STALE_RUN_MINUTES` | `180` before conservative stale-run recovery |
| `AUTONOMY_MAX_AUTHORIZATION` | `propose`; one of `observe`, `propose`, `modify_local`, `external_action` |
| `AUTONOMY_LOCK_TIMEOUT_SECONDS` | `0`; an overlap is skipped immediately |
| `AUTONOMY_DATA_DIR` | optional autonomy-only state override; normally leave blank and use `DATA_DIR` |
| `AUTONOMY_ROADMAP_FILE` | `config/autonomous-roadmap.json`; first-use seed |
| `MODEL_CATALOG_FILE` | `config/model-catalog.json`; routing/pricing snapshot |

In the Telegram runtime, the persisted Company Mode ledger is the authoritative budget
source for routing and reports, including spend and reservations from other work. Use
`/setbudget` to change its daily limit. `COMPANY_EMERGENCY_RESERVE_USD` seeds the reserve
for new/legacy state; an existing `company_state.json` retains its stored value. Keep the
`AUTONOMY_*` fallback values aligned if you also use the standalone CLI.

`MAX_REVISION_ROUNDS` and `MAX_EXECUTION_ATTEMPTS` bound Company Mode's review and task
loops. `MAX_TASK_RESULT_CHARS` defaults to `5000`, giving Vera a bounded but materially
complete view of non-file worker output. Live worker/reviewer estimates and controlled idle ideation are reserved atomically
before paid calls and then released or reconciled. `ADHOC_RESERVATION_USD` defaults to
`0.10` when metered group work has no task-specific estimate. Concurrent reservations on
one shared filesystem cannot each claim the same remaining dollars. If a metered caller
is cancelled after its Python worker thread starts, the runner waits for that thread to
finish before reconciling so provider spend cannot continue outside the ledger.

### Roadmap, authorization, and reports

[`config/autonomous-roadmap.json`](config/autonomous-roadmap.json) is safe example seed
data. Each roadmap item carries priority, status, dependencies, blockers, mandatory acceptance
criteria, agent owner, task type, complexity/risk, required capabilities, authorization,
estimates, previous attempts/models, and any human decision required. On first use it is
copied into `autonomy_state.json`; after that, the persistent state is the source of truth
and later seed-file edits are not automatically merged.

Miles owns selection. If a roadmap item uses `agent_owner: "manager"`, the bounded worker
task is delegated to Robin's general worker path so execution, Telegram identity, and cost
attribution do not falsely claim that the manager persona performed specialist work.

Autonomy reports/state and Company Mode state redact secret-looking keys and embedded
credential patterns before persistence; autonomous Telegram output and model answers
printed by the core are redacted too. This remains a backstop, so roadmaps should contain
references to credentials/access requirements, never the secret values themselves.

Use `DATA_DIR` as the common persistent root. Autonomous state is stored in
`autonomy_state.json`; reports are stored in `autonomous_runs/<run-id>.json`; the idea
backlog and scheduled-date idempotency records live inside the state file. The persistent
run lock prevents overlap for processes sharing that filesystem. A report records plan,
tasks, agents, model decisions, tokens, estimated/actual-or-reconciled cost, review,
retries, blockers, escalations, changed files, tests, artifacts, and final status. See
[`docs/sample-autonomous-daily-run-report.json`](docs/sample-autonomous-daily-run-report.json)
for an illustrative dry-run report with no secrets.

If autonomy state is corrupt, the unreadable file is quarantined and
`autonomy_state.json.recovery-required` keeps every later run blocked. Inspect the
quarantined file, restore a verified `autonomy_state.json`, remove the recovery marker,
and run `/autorun dry-run` before enabling live execution. The coordinator never silently
reseeds while that marker exists.

Live reports include nested worker/reviewer agents, models, and cost splits by project,
task, agent, and model. The top-level route reason is in the run report; each nested
Company Mode task retains its own `model_reason` in `company_state.json`. Scheduled live
runs keep intermediate agent chatter quiet and send only actionable escalations plus the
final summary.

Authorization levels are ceilings, not grants: `observe` inspects, `propose` drafts,
`modify_local` conceptually covers an isolated local/branch workspace, and
`external_action` covers sending, deploying, merging, publishing, purchasing, deleting,
or production mutation. The current runtime has no killable isolated checkout executor,
so both `modify_local` and `external_action` remain human-gated even if the configured
ceiling is raised. Raising the ceiling never adds tools or bypasses confirmation.

Current limitations: runs are sequential and select one roadmap item; the JSON/file-lock
design assumes every replica shares one mounted filesystem; pending Telegram
confirmations remain process-memory state; exact provider billing is unavailable when a
response lacks usage and is then conservatively charged at the held estimate; the task
time ceiling prevents another attempt but cannot preempt a Python thread, so the runner
waits for it to finish and reconcile; modify-local automation awaits an isolated executor;
owner resolution does not yet have a dedicated `/autorun resolve|retry|skip` command;
model prices/availability are configuration snapshots; and live Telegram, OpenAI,
Railway-volume, Docker, and external-connector behavior still require credentialed smoke
tests.

## Company Mode (Telegram Startup OS)

Company Mode turns the Telegram group into a supervised operating room where you are
the CEO/founder and Miles acts as COO. It stores state in `company_state.json`
(gitignored). As of **v2** it runs on a **real, metered daily budget** and can
**autonomously work an assigned goal** one task at a time — safely and supervised.

### The v2 loop: propose → approve → work

1. `/setbudget 20` — set today's budget to $20.
2. `/assign <goal>` — Miles drafts a small work plan (research → build → write →
   editor review), reserves budget for it, and **proposes** it. Nothing runs yet.
3. `/approve` — kicks off the execution engine. Agents work the plan **one task at a
   time**, each replying as themselves, and every finished task is linked to a real
   **deliverable** (a `files/` path, a GitHub file/PR URL, or saved copy). Each task is
   handed the **previous tasks' results and deliverables**, so agents build on one
   another (research → build → copy) instead of duplicating each other's work.
4. `/dailyreport` — shipped/open/blocked work, the artifacts produced, and the next move.

Commands in the group:

- `/company` or `/status` — operating mode, budget ledger, active project, open tasks.
- `/setbudget 20` — set today's company budget.
- `/assign <goal>` — Miles reads the goal and **plans a tailored work plan** (which
  agents, in what order, however many the goal needs — not a fixed 4 every time),
  reserves budget, and proposes it. Falls back to a default plan if planning fails.
- `/approve` — start working the proposed plan.
- `/cancel` — drop the active project and release its reserved budget.
- `/publish` — package the finished project for sale (assisted; see below).
- `/launch` — draft a launch kit: LinkedIn/X posts, a launch email, and **image-
  generation prompts** for the cover, thumbnail, and social card.
- `/link <gumroad-url>` — link the finished project to its live Gumroad product.
- `/products` — list linked products with their sales and revenue.
- `/revenue` — pull live sales from Gumroad and show per-product **P&L** (spent vs earned).
- `/dailyreport` — shipped/open/blocked work, artifacts, and the next recommendation.
- `/pausecompany` / `/resumecompany` — halt/resume the engine (checked between tasks).

(`/approve`, `/cancel`, `/setbudget`, `/assign`, and pause/resume are group-only — the
Miles DM is read-only for `/company`, `/status`, `/dailyreport`.)

### Metered budget + reservation guardrail (the financial brake)

Every metered group/Company model call is priced from the operator-maintained snapshot in
`config/model-catalog.json`. When the API returns usage, input, cached-input, and output
tokens are reconciled into today's ledger; otherwise the record is explicitly labeled
estimated at the held reservation. This covers Company Mode execution, controlled
ideation, publish/launch preparation, planning/routing fallbacks, and ordinary delegated
chat in the group.
Confirm the catalog against your account before trusting it as a forecast; an unknown
model uses a conservative fallback and logs a warning rather than silently costing `$0`.

A fresh deploy starts on a **$5/day budget** by default (`DEFAULT_DAILY_BUDGET_USD` in
`company_mode.py`); change it live any time with `/setbudget <amount>`. Ordinary work
also preserves a **$0.25 emergency reserve** by default, configurable through
`COMPANY_EMERGENCY_RESERVE_USD`. If you're running a persisted `company_state.json` from
before v2, run `/setbudget 5` once to adopt the budget.

The engine atomically reserves estimated cost before work, so concurrent workers sharing
the same `DATA_DIR` cannot independently spend the same available balance. Completion
releases the hold and records actual cost when usage exists, or a labeled estimate when
it does not. Work that cannot reserve its estimate is deferred. A provider call can
still cost more than its estimate, so this is an application guardrail rather than a
guaranteed provider-side billing cap; keep limits modest and monitor real billing.

### Publishing a product (`/publish`, assisted)

Gumroad (and Lemon Squeezy) **have no API to create a product or upload its file** —
it's dashboard-only. So `/publish` does everything up to the final upload: it splits
the project's finished deliverable into a clean **buyer-download file** and a
**paste-ready Gumroad listing** (name, price, description, tags, cover idea), then
**stages a gated approval**. Reply `/confirm` to mark the project published and get the
exact Gumroad go-live steps. The one thing that stays yours is the final upload click —
which is the right control for an irreversible, money-adjacent action anyway. True
end-to-end auto-publishing would require a separately approved browser/API workflow.

### Revenue tracking (the money loop)

Once a product is live, close the loop: `/link <gumroad-url>` attaches the finished
project to its Gumroad listing, and `/revenue` pulls **real** sales from the Gumroad API
(read-only — set `GUMROAD_ACCESS_TOKEN` from Gumroad → Settings → Advanced →
Applications) to show per-product **P&L**: what the company *spent* building it (real
metered token cost) vs what it *earned*. `/products` lists everything you've shipped with
its sales. This is what makes the company measurable instead of just busy.

The same numbers are also available conversationally: **Ledger (the CFO)** has
`get_company_status` and `get_revenue_report` tools, so you can just ask "how's the
budget today?" or "which product is actually profitable?" in the group or a DM.

### What stays supervised

During owner-approved Company Mode execution, *produce* actions (writing a file, saving a file or opening
a PR on GitHub) run without a prompt — they're the deliverables. *Irreversible* actions
(**sending email, deleting a file**, and by policy publishing/deploying/paid spend/
new-agent creation) still stage behind `/confirm`: the task is marked **blocked** and the
engine stops so you can approve it (the `/confirm` prompt shows which project/task it's
for). Reply `/confirm` in the group, then `/approve` again to continue the plan.

Only agents listed in `BOT_KEYS` speak as themselves; other specialists still contribute
through Miles-labeled delegation. Revenue tracking is implemented through `/link`,
`/products`, and the read-only `/revenue` Gumroad sync; product creation/upload remains a
human dashboard step.

## Google setup (Calendar & Gmail)

Cadence (Calendar) and Piper (Gmail) talk to your real Google account. Auth is a
**one-time local consent** that mints a `token.json` the headless bot then refreshes on
its own — no browser needed after setup:

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project,
   **enable the Google Calendar API and the Gmail API**, and create an **OAuth client ID
   of type "Desktop app"**.
2. Download that client secret and save it in the project root as `credentials.json`.
3. Run `python google_auth.py` once. A browser opens; approve the scopes (calendar
   events, read email, compose, send). On success it writes `token.json`.
4. `credentials.json` and `token.json` are gitignored. To deploy, upload `token.json`
   as a secret / persistent-volume file so the worker can keep refreshing it.

**Sending email is gated.** Piper can search, read, and draft freely (drafts just wait
in your Gmail Drafts), but **`send_email` always asks you to confirm** — over Telegram
it stages the send and you reply `/confirm` (the same mechanism as a file write);
creating calendar events, like adding a Todoist task, is not gated since it's easily
undone.

## Reliability features

- **Automatic retries.** Every external API call (OpenAI chat, OpenAI embeddings,
  Tavily search) is wrapped in `call_with_retries()`, which retries up to 3 times with
  a short delay before giving up. A `[retry] ...` line prints on the console so you can
  see it happening; this is a simple "retry on any exception" approach rather than
  distinguishing transient errors (rate limits, network blips) from permanent ones
  (e.g. a bad API key) — a smarter version would fail fast on the latter instead of
  wasting two retries on something that will never succeed.
- **Local debug log (`assistant.log`).** A separate technical record from
  `conversation_history` — it logs tool calls, retry attempts, and the *real*
  exception behind any failure (the console only ever shows a friendly, generic
  message). Gitignored, since it can contain personal request details. It deliberately
  does **not** log full tool results (e.g. file contents) to avoid duplicating
  potentially sensitive data into yet another file. Sensitive tool arguments such as
  file contents, email bodies, subjects, and code snippets are redacted before logging.
- **Specific tool errors instead of crashes.** If a tool call has a missing or
  malformed argument, `execute_tool()` catches it and returns a specific message
  (e.g. "missing required argument 'filename'") instead of crashing the whole turn
  with a generic connectivity-sounding error. Returning a clear error as the tool
  result also gives the model a chance to notice the mistake and retry with corrected
  arguments on its own.
- **Write-overwrite warning.** `write_file` now warns explicitly when a file already
  exists and is about to be overwritten, on top of the existing y/n confirmation, so
  you're not approving a destructive overwrite by accident.
- **Write size limit.** `write_file` refuses content over 50,000 characters, a basic
  guard against an accidental or runaway oversized write.
- **Read size limit.** File reads are capped at 50,000 characters before they are
  printed or sent to the model, with a clear truncation note when a file is larger.

## Running 24/7 (Telegram bot)

`bot.py` is a second entry point alongside `main.py` — same assistant, same code
(`ask_manager`, the specialists, every tool), reused without duplication, but reachable
from your phone via Telegram instead of a terminal.

**Run it locally first** (no deployment needed to test):

```powershell
python bot.py
```

1. Create a bot via [@BotFather](https://t.me/BotFather) on Telegram (`/newbot`),
   copy the token it gives you into `.env` as `TELEGRAM_BOT_TOKEN`.
2. Without `TELEGRAM_ALLOWED_USER_IDS` set, the bot still starts, but safely: it runs
   in a bootstrap mode where it replies to any sender with nothing but their own
   numeric Telegram user ID (no AI calls, no tool access, no cost exposure) — this is
   deliberate, since refusing to start outright would make it impossible to ever learn
   your own ID to put in the allowlist in the first place. Message your bot once to
   get your ID, add it to `.env` as `TELEGRAM_ALLOWED_USER_IDS` (comma-separate
   multiple IDs), then restart the bot.
3. Message it again — now it should respond for real.

**What's different from the CLI:**
- **`write_file` requires an explicit `/confirm` reply over Telegram**, instead of the
  CLI's `input()`-based y/n prompt (which has no real terminal to read from in a
  deployed, headless process). When an agent wants to write a file, the bot replies
  describing what it wants to write and asks you to reply `/confirm` to proceed, or
  anything else to cancel — it won't write anything until you do. This applies to
  every specialist that has `write_file` access (Coding, Writer, Personal Assistant).
  Only one write can be staged at a time; if you don't reply `/confirm` or cancel
  before sending an unrelated message, that message is treated as a cancellation.
- A write staged in the middle of a multi-step delegation chain ends that chain's
  turn there — confirming performs exactly that one file write and nothing more; it
  doesn't resume any further steps the original request may have implied. Re-prompt
  for those once the write is confirmed.
- Everything else — plain chat through the Manager (including multi-step delegation),
  all nine specialists, file reading, web search, long-term memory — works the same as
  the CLI.
- Long Telegram messages are split into multiple replies (Telegram's limit is ~4096
  characters per message).

**Deploying it (so it's actually running when your laptop is off):**

`Dockerfile` containerizes `bot.py`. Build and run it with:

```powershell
docker build -t ai-assistant-bot .
docker run --env-file .env ai-assistant-bot
```

I haven't tested this Docker build in this environment (no Docker available here) —
verify it builds and runs for you before deploying anywhere.

To actually run it 24/7, push this image to a platform that can keep a container
running continuously as a **background worker** (not a web service — this bot doesn't
listen on a port, it polls Telegram). Railway, Render, and Fly.io all support this;
exact steps and current free-tier terms change over time, so check each platform's own
docs rather than trusting specifics here. Whichever you pick, you'll need to:

1. Set `OPENAI_API_KEY`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_ALLOWED_USER_IDS` as
   environment variables/secrets on the platform (never commit `.env`). Add
   `TAVILY_API_KEY`, `OPENWEATHER_API_KEY`, and `TODOIST_API_TOKEN` only if you want
   web search, weather, or Todoist tasks in that deployment.
2. **Mount a persistent volume at `/app/data` and set `DATA_DIR=/app/data`** if you
   want memory and other state to survive restarts and redeploys. A plain container's
   filesystem is wiped every time it restarts. If `DATA_DIR` is unset, Railway's
   `RAILWAY_VOLUME_MOUNT_PATH` is used when available, then the project directory.

## Running the multi-bot group interface (group_bot.py)

`group_bot.py` is a third entry point (alongside `main.py` and `bot.py`): each agent
(Manager + specialists) is its own real Telegram bot, all members of one shared group
chat with you. It reuses the agent logic in `main.py` and owns the long-running Telegram,
Company Mode, scheduler/autonomy, and optional Office API runtime coordination.

**Setup, per agent you want in the group:**

1. Create a bot via [@BotFather](https://t.me/BotFather) (`/newbot`). BotFather
   rate-limits how many bots you can create in a short window — if you hit "too many
   attempts," it tells you how long to wait (can be ~20 hours); this is normal, not a
   bug, and only affects *creating new* bots, not ones you already have.
2. **Disable privacy mode**: `/mybots` → select the bot → `Bot Settings` →
   `Group Privacy` → turn it off. This is the single easiest step to forget, and the
   failure mode is silent — the bot just never receives group messages, with no error
   anywhere. If a bot seems to ignore everything, this is the first thing to check
   (`https://api.telegram.org/bot<TOKEN>/getMe` → `can_read_all_group_messages` should
   be `true`).
3. Add the bot to your shared group as a member.
4. Add its token to `.env` under the matching name:
   ```
   TELEGRAM_MANAGER_BOT_TOKEN=...
   TELEGRAM_CODE_BOT_TOKEN=...
   TELEGRAM_RESEARCH_BOT_TOKEN=...
   TELEGRAM_WRITE_BOT_TOKEN=...
   TELEGRAM_TASK_BOT_TOKEN=...
   TELEGRAM_MARKETING_BOT_TOKEN=...
   TELEGRAM_EDITOR_BOT_TOKEN=...
   TELEGRAM_FINANCE_BOT_TOKEN=...
   TELEGRAM_CALENDAR_BOT_TOKEN=...
   TELEGRAM_GMAIL_BOT_TOKEN=...
   TELEGRAM_LINEAR_BOT_TOKEN=...
   TELEGRAM_GENERAL_BOT_TOKEN=...
   TELEGRAM_SALES_BOT_TOKEN=...
   TELEGRAM_ANALYTICS_BOT_TOKEN=...
   TELEGRAM_GROUP_CHAT_ID=...
   ```
   `TELEGRAM_GROUP_CHAT_ID` only needs setting once — send any message in the group,
   then check which chat ID any of your bots received it from (e.g. via
   `https://api.telegram.org/bot<TOKEN>/getUpdates`).

   **Migrating from the pre-reorg roster?** The old news/weather/tasks bots are
   repurposed for the new hires: in BotFather use `/setname` to retitle each bot
   (display name only — the @username is fixed at creation, so e.g. the old news
   bot keeps its @handle while displaying as Sway), then copy the old token value
   into the matching new var (news → marketing, weather → editor, tasks → finance)
   and delete the old var. On a deployed instance, make the same env-var swap on
   the platform and redeploy.
5. The core roster is listed in `BOT_KEYS`. Optional Linear, Calendar, Gmail, Robin,
   Sales, and Analytics bots are added automatically when their matching token is set;
   they remain available through Miles even without a dedicated bot. Adding an entirely
   new code-defined agent still requires a roster entry plus its token, then a redeploy.

**Optional: give every bot its own profile picture.**

The generated Telegram profile portraits live in `assets/telegram_profiles/`, with a
manifest that maps each agent key to the matching token env var. Telegram's Bot API
supports changing a bot's profile photo via `setMyProfilePhoto`; this project keeps
that as an explicit setup step so deploys do not unexpectedly touch bot profiles.

Preview the setup without making any Telegram API calls:

```powershell
python scripts\set_telegram_profile_photos.py --all --dry-run
```

Apply every available portrait once the matching `TELEGRAM_*_BOT_TOKEN` values are in
your `.env`:

```powershell
python scripts\set_telegram_profile_photos.py --all
```

Or update one bot at a time:

```powershell
python scripts\set_telegram_profile_photos.py --agent manager
python scripts\set_telegram_profile_photos.py --agent code
```

The script only updates the bots you select. Agents that are not yet in `BOT_KEYS` can
still have portraits ready now; they just will not speak in the group until you add
their token and enable their key.

**Run it locally:**
```powershell
python group_bot.py
```

### Virtual Office desktop app

Virtual Office is a native Windows desktop app, not a browser dashboard. It draws an
original cozy office scene with desks, small avatars, live status lights, reply bubbles,
and a recent-activity log. The deployed Telegram worker is its live source of truth:
`group_bot.py` exposes only the bounded office state (180-character previews, the 30
most recent events, and each agent's 8 most recent events) through an authenticated API.

**On Railway:**

1. Set a long random `OFFICE_API_TOKEN` secret on the Telegram worker service.
2. Give that service a Railway public domain. The worker then listens on Railway's
   assigned `$PORT` and serves `GET /api/office-state` plus `GET /api/office-metrics`
   (aggregated Gumroad sales, Linear issues completed today, and daily team activity
   counters — each source degrades gracefully when unconfigured) with bearer-token
   authentication.
3. Redeploy the worker. Keep the token private; it grants read access to office previews.

**On your Windows desktop:**

```powershell
$env:OFFICE_API_URL = "https://your-railway-service.up.railway.app"
$env:OFFICE_API_TOKEN = "the-same-long-random-secret"
python office_desktop.py
```

You can also pass `--api-url` and `--token` directly. The desktop client polls every
1.5 seconds; it does not expose a local website or require any new dependencies. If
`python` is not on PATH, use ` .\.venv\Scripts\python.exe office_desktop.py` instead.

**Virtual Office 3D (browser):**

The same Railway service also serves a real-time 3D office at `/office3d` (also `/`
and `/office`). Open `https://your-railway-service.up.railway.app/office3d` in any
browser: it renders a full room with three.js — ceiling with recessed light panels,
a night skyline behind the glass curtain wall with half-lowered blinds, framed wall
art, carpeted desk pods, and rounded furniture with proper desk legs, monitor arms,
and task chairs — and the robots physically walk around the furniture, sit down at
their desks, and type while they work. When Miles delegates work to two or more
teammates at once, the whole group walks to the rug beside the operations desk and
huddles in a circle facing each other until the work is done. The sticky notes on
the planning whiteboard are real: each one shows the text of one of the newest
office events, so the wall reads like a live kanban of what just happened. Next to
the whiteboard hangs a company dashboard screen with the numbers that matter —
revenue and sales from Gumroad, Linear tasks completed today, Telegram message
volume, and who on the team is busiest — fed by the token-gated metrics API and
refreshed every minute (sources that aren't configured show a dash instead of
breaking). The robots are fully articulated — knees
and elbows bend, they turn to face their chair and settle into it instead of
teleporting, their heads track where they're walking or whoever is speaking, they
blink — and between tasks they stretch, sip coffee, swivel their chairs, or wander
to the lounge for a break. Drag to orbit, scroll to zoom, click any robot to have
the camera follow it around while a side panel shows that teammate's role, what it
is doing right now, and its recent activity (click the floor to release), or hit
the TOUR chip for
a slow cinematic camera that's perfect for a second monitor. Name plates and reply
bubbles are crisp DOM overlays, so they stay sharp at any zoom. The office
follows your local time of day: the sun sweeps across the room and warms toward
sunset, and after dark the skyline windows light up and every desk lamp comes on.
A quality chip in the top-right corner cycles post-processing (ambient occlusion,
bloom, SMAA) between Auto/High/Medium/Low; Auto steps itself down if the machine
can't hold a smooth frame rate.
On first load the page asks for the `OFFICE_API_TOKEN` value and keeps it only in
that browser's local storage; the state API itself stays token-protected. Add
`?demo=1` to the URL to watch scripted office activity without a token.

**How it behaves:**
- Message the group normally (no `@mention`) and a lightweight **router** picks the
  best-fit teammate(s), who reply **as themselves** — the Manager is no longer a
  mandatory mouthpiece. Miles only steps in to orchestrate when a request genuinely
  needs several teammates in sequence (e.g. "look up the weather, then draft a note
  about it"); then he dispatches and recaps, as before.
- `@mention` any agent directly (e.g. `@YourResearchBot what's...`) to talk to exactly
  that agent, right in the group — this skips the router entirely.
- **Message any agent privately (1:1 DM).** Open a DM with a specialist's bot and just
  talk — no `@mention` needed; every message goes to that specialist, in its own
  conversation thread separate from the group. DM **Miles** to get something done and
  he'll dispatch the right teammates; each dispatched agent **DMs you their answer
  directly** (as themselves) and Miles also recaps in your Miles DM — so results reach
  both you and the manager, like real coworkers reporting back.
  - For an agent to DM you, it needs its own bot (in `BOT_KEYS`) **and** you must have
    opened a chat with that bot once — Telegram won't let a bot cold-message you. Until
    you do, Miles relays that agent's answer in your Miles DM as a fallback.
- Each chat (the group and every DM) keeps its **own short-term conversation thread**,
  but they all share the one long-term memory/knowledge store.
- Confirmations for sensitive actions (a `write_file` **or** a Gmail `send_email`) are
  resolved **in the chat where they were staged**: reply `/confirm` in that DM (or in
  the group, to Miles) to approve, anything else to cancel.
- Every bot ignores messages from other bots (prevents reply loops — with privacy mode
  off, every bot technically sees every message, including ones other bots post) and
  ignores any chat that isn't the configured group or an allowed user's private DM.

**Deploying it:**

`Dockerfile.group` containerizes `group_bot.py` (separate from `Dockerfile`, which
still builds `bot.py` if you'd rather deploy the simpler single-bot interface). The
group process polls Telegram and runs APScheduler; when `OFFICE_API_TOKEN` is set, that
same worker also binds `$PORT` and serves the authenticated Virtual Office API/pages:

```powershell
docker build -f Dockerfile.group -t ai-assistant-group-bot .
docker run --env-file .env ai-assistant-group-bot
```

Not build-tested in this environment (no Docker available here) — verify it builds and
runs for you first. Without `OFFICE_API_TOKEN`, it can run as a background worker. With
the Office API enabled, deploy it as a long-running service that supports both Telegram
polling and inbound HTTP, and assign a public domain if the desktop/browser office needs
remote access.

**Persistence — mount ONE volume and set `DATA_DIR`.** A plain container's filesystem
is wiped on every redeploy, so long-term memory, pending reminders, Company Mode state,
and the Google token all reset unless you persist them. The app reads a `DATA_DIR` env
var and writes all of that under it:

1. On Railway (or your platform), **attach a volume** with mount path `/app/data`.
2. Set the env var **`DATA_DIR=/app/data`**.

That's it — `memory_db/`, `reminders.json`, `company_state.json`, `office_state.json`,
`active_project.json`, `autonomy_state.json`, `autonomous_runs/`, and `token.json` now
live on the volume and survive redeploys. `projects.json` in `DATA_DIR` may override the
bundled project registry. Without `DATA_DIR`, the app falls back to
`RAILWAY_VOLUME_MOUNT_PATH` when Railway provides it, then the project directory (fine
locally, ephemeral in a plain container). Then set the
required group tokens you're using (`TELEGRAM_MANAGER_BOT_TOKEN`,
`TELEGRAM_GROUP_CHAT_ID`, etc.) plus `OPENAI_API_KEY`. Add
`TAVILY_API_KEY`/`OPENWEATHER_API_KEY`/`TODOIST_API_TOKEN` only for web search,
weather, or Todoist, and keep `HOME_LOCATION`/`BRIEFING_TIME`/`DAILY_REPORT_TIME`/
`TIMEZONE`/`EVENT_ALERT_MINUTES` as optional scheduling configuration.

**One thing to remember:** the core `BOT_KEYS` roster is code-defined. Optional bot keys
already listed in `OPTIONAL_BOT_KEYS` activate when their token secret is present. A
brand-new agent requires both a code roster entry and a token before redeploying.
