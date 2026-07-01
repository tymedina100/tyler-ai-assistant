# Tyler AI Assistant

A beginner-friendly command-line AI assistant built with Python and the OpenAI API.

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
- Six specialist agents, each a named character with its own personality, role, and
  curated tool access, including real external connectors (Todoist, OpenWeatherMap) —
  see "Specialist agents" and "Meet the team" below
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
- Get a free OpenWeatherMap key (used by the Weather Agent) from
  https://openweathermap.org/api — note new keys can take up to ~10 minutes (rarely,
  longer) to activate after signup, so a fresh key returning an "Invalid API key"
  error isn't necessarily wrong, just not active yet.
- Get your Todoist API token (used by the Tasks Agent) from the Todoist app:
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

- One of the six specialists (`code`, `research`, `write`, `task`, `tasks`, `weather`)
- The **general assistant** (the same all-purpose, all-tools assistant from Week 4),
  for anything that doesn't clearly fit a specialist

**Multi-step delegation.** Most requests need only one agent, but the Manager can
delegate to *more than one in sequence* when a request genuinely needs it — e.g.
"look up the weather in Seattle, then write me a note about whether I should bring an
umbrella" delegates to the Weather Agent first, then to the Writer Agent with the
weather result included in that next delegation's prompt. Every handoff still goes
through the Manager — agents never call each other directly, and architecturally
can't (a specialist's own tool-calling loop only has access to its own curated tools,
never the delegation tools). The Manager has a higher tool-call budget than other
agents (`MAX_MANAGER_TOOL_ITERATIONS = 8` vs. `MAX_TOOL_ITERATIONS = 10`) because
the Manager is expected to spend its budget on delegation chains while specialists
may spend theirs on search, memory recall, and verification before answering.

You'll see one response block per agent involved (e.g. `Weather Agent response: ...`,
then `Writer Agent response: ...`), then the Manager's final answer (`Manager
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

Beyond the general assistant, six specialist commands each use the same tool-calling
machinery as plain chat, but with a different system prompt and a **curated subset**
of tools — not every specialist can do everything. Each one is also a **named
character with its own personality** (see "Meet the team" below):

| Command | Specialist | Tools it has |
|---|---|---|
| `/code <task>` | **Patch** (Coding Agent) | read file, write file, search the web, recall memory |
| `/research <topic>` | **Scout** (Researcher Agent) | search the web, recall memory, remember a fact |
| `/write <prompt>` | **Quill** (Writer Agent) | read file, write file, recall memory |
| `/task <request>` | **Sage** (Personal Assistant Agent) | remember a fact, recall memory, write file, read file |
| (Manager-only — no direct slash command) | **Roster** (Tasks Agent) | create/list real Todoist tasks |
| (Manager-only — no direct slash command) | **Gale** (Weather Agent) | current weather lookup |
| (Manager-only — no direct slash command) | **Cadence** (Calendar & Scheduler Agent) | Google Calendar events + timed reminders |
| (Manager-only — no direct slash command) | **Piper** (Gmail Agent) | search/read/draft/send email |

**Patch can also run code.** The Coding Agent has a `run_python` tool that actually
executes Python in a sandbox (see "Code execution" below), so it can test and verify
what it writes instead of guessing.

This is deliberate: for example, the Researcher Agent *cannot* call `write_file` even
if asked to — it genuinely doesn't have access to that tool, so it'll tell you it can't
and offer the content for you to save yourself, rather than faking it. Scoping each
agent to only the tools its role needs is a basic safety practice (least privilege),
not just an organizational nicety.

**Personal Assistant Agent vs. Tasks Agent — these sound similar but are deliberately
different:** the Personal Assistant only saves general facts, reminders, and
preferences to local long-term memory (the same Chroma store everything else uses) —
it is *not* a real task tracker. The Tasks Agent is connected to your actual Todoist
account via its REST API and creates/checks real to-do items there. Both the
specialists' own instructions and the Manager's delegation tool descriptions spell
this distinction out explicitly, since without it the Manager would have no reliable
way to choose between them (e.g. "remind me to call mom" routes to the Personal
Assistant; "add buy groceries to my Todoist" routes to the Tasks Agent).

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
- **Patch** — Coding Agent. Blunt, pragmatic senior engineer.
- **Scout** — Researcher Agent. Curious fact-hound who cites sources.
- **Quill** — Writer Agent. Warm wordsmith who cares about tone.
- **Sage** — Personal Assistant Agent. Calm and organized; remembers your preferences.
- **Roster** — Tasks Agent. Crisp operator for your real Todoist list.
- **Gale** — Weather Agent. Cheery weather nerd.
- **Cadence** — Calendar & Scheduler Agent. Unflappable and precise; runs your Google
  Calendar and your reminders.
- **Piper** — Gmail Agent. Brisk and discreet; triages, drafts, and sends email.
- **Robin** — the general assistant (the all-rounder for anything that doesn't fit a
  specialist).

Cadence and Piper are the newest hires: they work today via Miles's delegation (their
answers post under Miles in the group), and get their own Telegram bots once you create
them and add `"calendar"` / `"gmail"` to `BOT_KEYS`.

Personalities are just prompt text, so they're easy to retune: edit the `persona`
field on each `SPECIALISTS` entry (or `MANAGER_INSTRUCTIONS` / `ASSISTANT_INSTRUCTIONS`
for Miles / Robin) in `main.py`. The character names live in `main.py` only; the
Telegram group (`group_bot.py`) derives its labels and welcome messages from there, so
a name never drifts between interfaces.

## Cost: two model tiers

Not every request needs the most powerful (most expensive) model, so agents run on one
of two tiers instead of one model for everything:

- **`PREMIUM_MODEL`** — used where real reasoning matters: **Patch** (coding),
  **Scout** (research), **Quill** (writing), and **Robin** (the catch-all general
  assistant).
- **`FAST_MODEL`** — a cheaper, faster model for work that's mostly routing or a thin
  wrapper over an API result: **Miles** (the Manager's delegation decision is a
  classification task), **Gale** (weather), **Roster** (Todoist), and **Sage**
  (personal-assistant memory ops).

Both are constants at the top of `main.py`, and every call defaults to `PREMIUM_MODEL`,
so nothing silently downgrades — a call is only cheap where the code deliberately passes
`FAST_MODEL`. To retune, change which tier an agent uses via the `"model"` key on its
`SPECIALISTS` entry (or the `model=` argument in `ask_manager` / `ask_ai`). **Confirm
the exact cheaper model id available on your OpenAI account** before deploying — it's a
single constant (`FAST_MODEL`) to update.

Two related token-cost guards also live in `main.py`:
- **History window (`MAX_HISTORY_MESSAGES`).** Plain chat resends recent conversation
  to the model each turn; this caps how many past messages are sent so cost doesn't grow
  unbounded over a long session. `/history` still shows the full record — only the
  model input is windowed, since long-term memory recall already carries older context.
- **Memory relevance cutoff (`MEMORY_DISTANCE_THRESHOLD`).** See "How long-term memory
  works" below — recalled memories that aren't actually similar are no longer injected
  into prompts.

## Code execution (Patch)

Patch (the Coding Agent) has a `run_python` tool that actually executes Python and
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
Because Patch is the Coding Agent, its internal tool-call budget is also a bit higher
(`max_iterations: 8`) to fit a browse → read → propose flow in one turn.

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

These only run in the group interface (that's where a persistent process lives); the CLI
and single bot store reminders but don't fire them. Configure via `.env`:
`HOME_LOCATION`, `BRIEFING_TIME` (e.g. `08:00`), `TIMEZONE` (e.g. `America/New_York`),
`EVENT_ALERT_MINUTES` (e.g. `15`), and `DAILY_REPORT_TIME` (e.g. `18:00`).

## Company Mode (Telegram Startup OS)

Company Mode turns the Telegram group into a supervised operating room where you are
the CEO/founder and Miles acts as COO. It stores state in `company_state.json`
(gitignored) and uses a daily USD budget ledger with estimates/reservations, not exact
token accounting yet.

Commands in the group:

- `/company` or `/status` — show operating mode, daily budget, active project, and
  open tasks.
- `/setbudget 20` — set today's company budget to $20.
- `/assign <goal>` — create an active project and reserve budget for a small work plan.
- `/dailyreport` — show shipped/open/blocked work and the next recommendation.
- `/pausecompany` / `/resumecompany` — stop or restart new assigned work.

Company Mode is deliberately supervised: agents can produce PRs, files, research,
copy, and validation artifacts, but sending email, deleting files, publishing,
deploying, paid spend, or new-agent creation still uses the existing `/confirm` gate.
Only agents listed in `BOT_KEYS` speak as themselves; other specialists can still
contribute through Miles-labeled delegation.

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
  all six specialists, file reading, web search, long-term memory — works the same as
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
2. **Mount a persistent volume at `/app/memory_db`** if you want long-term memory to
   survive restarts and redeploys. A plain container's filesystem is wiped every time
   it restarts — without a volume, the assistant's memory resets on every deploy,
   which defeats the point of Week 3. This is the one piece that genuinely needs your
   attention; everything else in the Dockerfile works without it, just without
   persistence.

## Running the multi-bot group interface (group_bot.py)

`group_bot.py` is a third entry point (alongside `main.py` and `bot.py`): each agent
(Manager + specialists) is its own real Telegram bot, all members of one shared group
chat with you. Same underlying AI logic as everywhere else, reused without
duplication — this file is purely the "who's listening, who replies as whom" layer.

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
   TELEGRAM_TASKS_BOT_TOKEN=...
   TELEGRAM_WEATHER_BOT_TOKEN=...
   TELEGRAM_GROUP_CHAT_ID=...
   ```
   `TELEGRAM_GROUP_CHAT_ID` only needs setting once — send any message in the group,
   then check which chat ID any of your bots received it from (e.g. via
   `https://api.telegram.org/bot<TOKEN>/getUpdates`).
5. In `group_bot.py`, add the agent's key to `BOT_KEYS` (near the top of the file) —
   this is how you control which agents are actually active without needing all 7
   bots created before anything works. Start small (e.g. just Manager + one
   specialist) and expand as you create more bots.

**Run it locally:**
```powershell
python group_bot.py
```

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
still builds `bot.py` if you'd rather deploy the simpler single-bot interface
instead):

```powershell
docker build -f Dockerfile.group -t ai-assistant-group-bot .
docker run --env-file .env ai-assistant-group-bot
```

Not build-tested in this environment (no Docker available here) — verify it builds
and runs for you first. Same deployment shape as `bot.py`: a **background worker**
(not a web service, since it doesn't listen on a port). Mount a **persistent volume**
so state survives redeploys — this now covers not just `/app/memory_db` (long-term
memory) but also `/app/reminders.json` (pending reminders), `/app/company_state.json`
(Company Mode), and `/app/token.json` (your Google login); without persistence those reset on every deploy. Set the
required group tokens you're using (`TELEGRAM_MANAGER_BOT_TOKEN`,
`TELEGRAM_GROUP_CHAT_ID`, etc.) plus `OPENAI_API_KEY`. Add
`TAVILY_API_KEY`/`OPENWEATHER_API_KEY`/`TODOIST_API_TOKEN` only for web search,
weather, or Todoist, and keep `HOME_LOCATION`/`BRIEFING_TIME`/`DAILY_REPORT_TIME`/
`TIMEZONE`/`EVENT_ALERT_MINUTES` as optional scheduling configuration.

**One thing to remember:** `BOT_KEYS` in `group_bot.py` controls which agents are
active. Whenever you create a new bot and want to add it to a *deployed* instance,
you need to both add its token as a secret on the platform *and* add its key to
`BOT_KEYS`, then redeploy — the two changes go together.
