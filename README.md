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
- Six specialist agents, each with its own role and curated tool access, including
  real external connectors (Todoist, OpenWeatherMap) — see "Specialist agents" below
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
  its own personality, all in one shared chat with you — see "Running the multi-bot
  group interface" below

## Setup

Create a virtual environment:

```powershell
python -m venv .venv
```

Activate it:

```powershell
.venv\Scripts\Activate.ps1
```

Install dependencies:

```powershell
pip install -r requirements.txt
```

Create a `.env` file in the project root with your API keys:

```
OPENAI_API_KEY=your-openai-key-here
TAVILY_API_KEY=your-tavily-key-here
TELEGRAM_BOT_TOKEN=your-telegram-bot-token-here
TELEGRAM_ALLOWED_USER_IDS=your-telegram-user-id-here
OPENWEATHER_API_KEY=your-openweathermap-key-here
TODOIST_API_TOKEN=your-todoist-api-token-here
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
  recall facts from previous sessions without you asking explicitly.
- `/recall <query>` lets you inspect this directly: it shows the raw memories Chroma
  thinks are most similar to your query, along with a "distance" score (lower = more
  similar). There's no relevance cutoff, so even an unrelated memory can show up if
  the store doesn't have anything better — `/recall` exists so you can see that
  happening instead of it being a black box.
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
agents (`MAX_MANAGER_TOOL_ITERATIONS = 8` vs. `MAX_TOOL_ITERATIONS = 5`) specifically
to leave room for chains like this.

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
of tools — not every specialist can do everything:

| Command | Specialist | Tools it has |
|---|---|---|
| `/code <task>` | Coding Agent | read file, write file, search the web, recall memory |
| `/research <topic>` | Researcher Agent | search the web, recall memory, remember a fact |
| `/write <prompt>` | Writer Agent | read file, write file, recall memory |
| `/task <request>` | Personal Assistant Agent | remember a fact, recall memory, write file, read file |
| (Manager-only — no direct slash command) | Tasks Agent | create/list real Todoist tasks |
| (Manager-only — no direct slash command) | Weather Agent | current weather lookup |

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

All specialists share the same `conversation_history` and long-term memory as plain
chat and each other — there's one memory store for the whole assistant, not one per
agent. A fact saved via `/task` (Personal Assistant) can be recalled later in plain
chat or by any other specialist.

**Known limitation:** specialists share a `MAX_TOOL_ITERATIONS = 5` cap on their own
internal tool calls (separate from the Manager's higher budget for delegation chains).
Occasionally an agent — the Researcher in particular, since it sometimes re-verifies
a fact with several searches before answering — can exhaust that budget and fall back
to a generic "I tried too many tool calls" message instead of a real answer. This is
flaky (re-asking the same thing usually succeeds) and is the existing Week 8 safety
net working as designed (a graceful message, not a crash), not a bug introduced here.

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
  potentially sensitive data into yet another file.
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

1. Set `OPENAI_API_KEY`, `TAVILY_API_KEY`, `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_ALLOWED_USER_IDS`, `OPENWEATHER_API_KEY`, and `TODOIST_API_TOKEN` as
   environment variables/secrets on the platform (never commit `.env`).
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
- Message the group normally (no `@mention`) and the **Manager** picks it up, decides
  which agent(s) to delegate to, and posts the delegation + each agent's answer as
  real messages from the right bot identities — not just a debug log line.
- `@mention` any agent directly (e.g. `@YourResearchBot what's...`) to skip the
  Manager entirely and talk to that agent one-on-one, right in the group.
- `write_file` confirmations always go through the **Manager**, regardless of which
  agent staged the write (a direct `@mention` conversation or a Manager delegation) —
  reply `/confirm` as a plain message, not addressed to anyone specifically.
- Every bot ignores messages from other bots (prevents reply loops — with privacy
  mode off, every bot technically sees every message, including ones other bots
  post) and ignores anything outside the configured group, including private DMs.

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
(not a web service, since it doesn't listen on a port), and the same
`/app/memory_db` persistent-volume requirement applies if you want long-term memory
to survive redeploys. Set every token you're using (`TELEGRAM_MANAGER_BOT_TOKEN`,
`TELEGRAM_GROUP_CHAT_ID`, etc., plus `OPENAI_API_KEY`/`TAVILY_API_KEY`/
`OPENWEATHER_API_KEY`/`TODOIST_API_TOKEN`) as environment variables/secrets on
whatever platform you deploy to.

**One thing to remember:** `BOT_KEYS` in `group_bot.py` controls which agents are
active. Whenever you create a new bot and want to add it to a *deployed* instance,
you need to both add its token as a secret on the platform *and* add its key to
`BOT_KEYS`, then redeploy — the two changes go together.
