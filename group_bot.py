"""
Multi-bot Telegram group interface - each agent (Manager + specialists) is its
own real Telegram bot, all members of one shared group chat. Reuses all the
existing AI logic from main.py (ask_manager, ask_specialist, every tool)
without duplicating it - this file is purely the "who's listening, who
replies as whom" transport layer.

Architecture:
- One Python process runs all enabled bots concurrently (Application
  instances share one asyncio event loop) - verified directly against the
  installed python-telegram-bot that the lower-level initialize()/start()/
  Updater.start_polling() are async and composable, unlike the blocking
  run_polling() convenience method bot.py uses (fine for a single bot, not
  for running several at once).
- Manager-initiated delegation needs no new async coordination: execute_tool's
  delegate_to_* branches in main.py already call ask_specialist()/ask_ai() as
  plain synchronous function calls, exactly as before. This file's
  on_delegation hook (set on main.py) just adds a side effect - posting the
  delegation announcement and the specialist's answer to the group using that
  specialist's own bot identity, purely for visibility.
- Only direct @mention handling is genuinely new event-driven work: each
  specialist's own bot independently watches the group for its own @username
  and responds, bypassing the Manager entirely - this is what makes agents
  directly reachable without the Manager being a mandatory gatekeeper.
- Every handler starts with the same guard: ignore messages from other bots
  (prevents reply loops - privacy mode being off means every bot sees every
  message, including ones other bots post), ignore anything outside the
  configured group, ignore anyone not on the allowlist.
"""
import asyncio
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

import main
import company_mode
import company_linear
import gumroad_helpers
import linear_helpers


load_dotenv()


def _require_env(name, hint=""):
    """Read a required environment variable, exiting with a clear message instead of
    a raw KeyError traceback if it's missing - matches the friendly-failure style the
    rest of the app uses. On Railway these are set as service variables, so a typo or
    an unset one should say exactly what to fix, not dump a stack trace into the logs."""
    value = os.environ.get(name)
    if not value:
        message = f"Missing required environment variable {name}."
        raise SystemExit(f"{message} {hint}".strip())
    return value


try:
    GROUP_CHAT_ID = int(_require_env(
        "TELEGRAM_GROUP_CHAT_ID",
        "Send any message in your group, then read the chat id from "
        "https://api.telegram.org/bot<TOKEN>/getUpdates.",
    ))
except ValueError:
    raise SystemExit("TELEGRAM_GROUP_CHAT_ID must be the numeric group chat id (e.g. -1001234567890).")

DAILY_REPORT_TIME = os.environ.get("DAILY_REPORT_TIME", "18:00")

ALLOWED_USER_IDS = {
    int(user_id.strip())
    for user_id in os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
    if user_id.strip()
}

if not ALLOWED_USER_IDS:
    raise SystemExit(
        "TELEGRAM_ALLOWED_USER_IDS is not set. Run bot.py once first if you "
        "haven't already - its bootstrap mode is how you learn your own "
        "Telegram user ID, which this file assumes is already configured."
    )

main.CONFIRMATION_MODE = "requires_confirmation"

# Build-order staging: only the agents listed here get a live Telegram bot. Agents
# NOT listed still work via Miles's delegation (their answers post under Miles) -
# they just can't be @mentioned directly until you create their bot and add its key
# here. Full set once every bot exists: ["manager", "code", "research", "write",
# "task", "marketing", "editor", "finance", "calendar", "gmail"].
BOT_KEYS = ["manager", "code", "research", "write", "task", "marketing", "editor", "finance"]

# Optional bots: enabled only if their token env var (TELEGRAM_<KEY>_BOT_TOKEN) is
# set - never required, so a missing token can't hard-exit startup. When disabled
# the agent still works via Miles's delegation; it just isn't @mentionable.
OPTIONAL_BOT_KEYS = ["linear"]
BOT_KEYS += [key for key in OPTIONAL_BOT_KEYS
             if os.environ.get(f"TELEGRAM_{key.upper()}_BOT_TOKEN")]

SPECIALIST_KEYS = [key for key in BOT_KEYS if key != "manager"]

# The Manager isn't a main.SPECIALISTS entry (it's the router persona defined by
# MANAGER_INSTRUCTIONS), so it carries its own label/welcome here. Every other
# agent's display name and role come from main.SPECIALISTS below, so the persona
# names live in exactly one place (main.py) and can't drift between the CLI and
# the group. Each entry here only adds Telegram-specific bits: the token env var
# and a one-line "@mention me for X" tagline.
AGENT_INFO = {
    "manager": {
        "env_var": "TELEGRAM_MANAGER_BOT_TOKEN",
        "label": "Miles (Manager)",
        "welcome": (
            "Hi, I'm Miles, the Chief of Staff. Message me (or just talk in the "
            "group) and I'll route your request to the right agent - or @mention "
            "an agent directly to skip me entirely. If anyone stages a sensitive "
            "action (a file write or sending an email), reply /confirm to me "
            "specifically to approve it, even if you were talking to another agent "
            "directly."
        ),
    },
    "code": {"env_var": "TELEGRAM_CODE_BOT_TOKEN", "tagline": "@mention me with a coding task."},
    "research": {"env_var": "TELEGRAM_RESEARCH_BOT_TOKEN", "tagline": "@mention me with something to look up or for a news brief."},
    "write": {"env_var": "TELEGRAM_WRITE_BOT_TOKEN", "tagline": "@mention me with something to draft."},
    "task": {"env_var": "TELEGRAM_TASK_BOT_TOKEN", "tagline": "@mention me to remember something or manage your Todoist tasks."},
    "marketing": {"env_var": "TELEGRAM_MARKETING_BOT_TOKEN", "tagline": "@mention me for positioning, launch posts, SEO, or growth ideas."},
    "editor": {"env_var": "TELEGRAM_EDITOR_BOT_TOKEN", "tagline": "@mention me to review a deliverable before it ships."},
    "finance": {"env_var": "TELEGRAM_FINANCE_BOT_TOKEN", "tagline": "@mention me for budget, P&L, or revenue questions."},
    "calendar": {"env_var": "TELEGRAM_CALENDAR_BOT_TOKEN", "tagline": "@mention me about your calendar, a reminder, or the weather."},
    "gmail": {"env_var": "TELEGRAM_GMAIL_BOT_TOKEN", "tagline": "@mention me to check or send email, or handle a customer message."},
    "linear": {"env_var": "TELEGRAM_LINEAR_BOT_TOKEN", "tagline": "@mention me to turn ideas into Linear issues."},
}

# Fill in each specialist's label + welcome from main.SPECIALISTS (the single
# source of truth for persona names). A label looks like "Scout (Researcher
# Agent)"; the part in parentheses is the human-readable role, which we reuse to
# build a greeting like "Scout here - the Researcher Agent. <tagline>".
for _key, _info in AGENT_INFO.items():
    if _key == "manager":
        continue

    _profile = main.SPECIALISTS[_key]
    _info["label"] = _profile["label"]
    _role_label = _profile["label"].split("(", 1)[1].rstrip(")") if "(" in _profile["label"] else _profile["label"]
    _info["welcome"] = f"{_profile['name']} here - the {_role_label}. {_info['tagline']}"

applications = {}
bots = {}
bot_usernames = {}
locks = {key: asyncio.Lock() for key in BOT_KEYS}
main_loop = None

# The single in-flight Company Mode plan runner (Feature: v2 checkpointed autonomy).
# One at a time - /approve refuses to start a second while this is running.
company_runner_task = None


def is_authorized(update):
    """True only for a real (non-bot) sender on the allowlist. No longer restricts
    to the group - private DMs from an allowed user are now first-class (see
    chat_kind). The bot-sender check is what still prevents reply loops."""
    user = update.effective_user

    if user is None or user.is_bot:
        return False  # never react to another bot - this is what prevents reply loops

    if user.id not in ALLOWED_USER_IDS:
        main.logger.warning(f"Rejected message from unauthorized Telegram user {user.id}")
        return False

    return True


def chat_kind(update):
    """Where a message came from: "group" (the shared team chat), "private" (a 1:1
    DM with whichever bot received it), or "other" (some group we don't serve)."""
    chat = update.effective_chat
    if chat.id == GROUP_CHAT_ID:
        return "group"
    if chat.type == "private":
        return "private"
    return "other"


async def post_agent_answer_to_group(key, answer):
    """Post an agent's answer to the group under its own bot identity, falling back
    to Miles for agents that don't have their own bot yet (e.g. Quill/Robin).
    Chunked so a long answer isn't silently dropped (see send_chunks below)."""
    bot = bots.get(key, bots["manager"])
    await send_chunks(bot, GROUP_CHAT_ID, answer)


async def _handle_pending_confirmation(update, text):
    """If the CURRENT conversation (main.set_conversation must already be called for
    this chat) has a sensitive action staged, resolve it with this message - /confirm
    runs it, anything else cancels - and return True. Returns False if nothing was
    staged, so the caller proceeds with normal handling. Per-chat, so a write staged
    while DMing one agent is never confirmed or cancelled by a message elsewhere."""
    pending = main.get_pending_action()
    if pending is None:
        return False

    main.clear_pending_action()  # resolved either way - confirm or cancel
    description = main.describe_pending_action(pending)

    # A "publish" approval is resolved here (in group_bot) rather than via
    # main.confirm_pending_action, since publishing is a Company Mode concept and
    # main.py stays independent of company_mode.
    if pending.get("type") == "publish":
        if text.strip() == "/confirm":
            msg = await asyncio.to_thread(company_mode.mark_project_published)
            main.logger.info(f"Telegram user {update.effective_user.id} confirmed {description}")
            await reply_chunks(update.message, f"{msg}\n\n{GUMROAD_GO_LIVE_STEPS}")
        else:
            main.logger.info(f"Telegram user {update.effective_user.id} cancelled {description}")
            await update.message.reply_text(f"Cancelled the {description}. Nothing was marked published.")
        return True

    if text.strip() == "/confirm":
        # confirm_pending_action can do blocking I/O (Gmail send) - keep it off the loop.
        result = await asyncio.to_thread(main.confirm_pending_action, pending)
        main.logger.info(f"Telegram user {update.effective_user.id} confirmed {description}")
        await reply_chunks(update.message, result)
    else:
        main.logger.info(f"Telegram user {update.effective_user.id} cancelled {description}")
        await update.message.reply_text(f"Cancelled the {description}.")

    return True


# Telegram hard-caps a single message at 4096 characters and rejects an empty one.
# A specialist's answer (especially Patch summarizing a multi-file change) can easily
# blow past that, so every place we post an agent's answer splits it into chunks -
# otherwise the send throws and the whole reply is lost silently.
TELEGRAM_LIMIT = 4000


def _chunks(text):
    text = text if text and text.strip() else "(no response)"
    return [text[i:i + TELEGRAM_LIMIT] for i in range(0, len(text), TELEGRAM_LIMIT)]


async def reply_chunks(message, text):
    for chunk in _chunks(text):
        await message.reply_text(chunk)


async def send_chunks(bot, chat_id, text):
    for chunk in _chunks(text):
        await bot.send_message(chat_id, chunk)


def build_specialist_handler(key):
    async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update):
            return

        kind = chat_kind(update)
        text = update.message.text

        if kind == "group":
            # In the group, a specialist only answers when explicitly @mentioned;
            # plain group messages are auto-routed by the manager bot's handler.
            mention = f"@{bot_usernames[key]}"
            if mention.lower() not in text.lower():
                return  # not addressed to this bot

            main.logger.info(f"[{key}] mention matched - processing")
            request = re.sub(re.escape(mention), "", text, count=1, flags=re.IGNORECASE).strip()
            conv_id = "group"

        elif kind == "private":
            # A 1:1 DM with this specialist's own bot: every message is for this
            # specialist, no @mention needed. Its own conversation thread + memory.
            request = text.strip()
            conv_id = f"dm:{key}:{update.effective_user.id}"

        else:
            return  # some group we don't serve

        if request in ("", "/start"):
            await update.message.reply_text(AGENT_INFO[key]["welcome"])
            return

        try:
            async with locks[key]:
                # Point this turn at the right conversation so history, memory, and any
                # staged confirmation are scoped to this exact chat (group vs. this DM).
                main.set_conversation(conv_id)
                main.set_reply_context({"kind": "group" if kind == "group" else "specialist_dm"})

                # Resolve a sensitive action staged earlier in THIS chat first (e.g. you
                # replied /confirm to Patch in his DM to approve a file write he staged).
                if await _handle_pending_confirmation(update, request):
                    return

                answer = await _run_metered(main.ask_specialist, key, request)
        except Exception as e:
            main.logger.error(f"Unhandled error in {key} specialist handler: {e}")
            await update.message.reply_text("Sorry, something went wrong processing that.")
            return

        # Split into <=4096-char chunks (and guard against an empty answer, which
        # Telegram also rejects) - see reply_chunks / TELEGRAM_LIMIT above.
        await reply_chunks(update.message, answer)

    return handle


async def _maybe_handle_project_linear_command(update, text):
    """Intercept /project and /linear slash commands (multi-project + Linear) and run
    them through main's handlers, which return a string to post. These are the same
    handlers the CLI uses; without this, the group/DM interface would route them to
    Miles as plain chat instead. Returns True if it handled the message.

    The handlers may hit the Linear API or the model (planning commands), so run them
    off the event loop in a thread. Not company-metered - same as the CLI slash
    commands."""
    stripped = (text or "").strip()
    lowered = stripped.lower()

    if lowered == "/project" or lowered.startswith("/project "):
        response = await asyncio.to_thread(main.handle_project_command, stripped[len("/project"):])
        await reply_chunks(update.message, response)
        return True

    # `/linear do <issue>` is special: it seeds a supervised Company Mode project from
    # an existing Linear issue (needs the engine + budget flow), so it's handled here
    # rather than by main.handle_linear_command (which only does read/create).
    if lowered.startswith("/linear do ") or lowered == "/linear do":
        identifier = stripped[len("/linear do"):].strip()
        await assign_from_linear(update, identifier)
        return True

    if lowered == "/linear" or lowered.startswith("/linear "):
        response = await asyncio.to_thread(main.handle_linear_command, stripped[len("/linear"):])
        await reply_chunks(update.message, response)
        return True

    return False


async def handle_manager_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The manager bot receives both the group's plain messages (which it now
    auto-routes to the right teammate instead of always answering itself) and its
    own 1:1 DMs (where Miles dispatches and the dispatched agents answer you
    directly)."""
    if not is_authorized(update):
        return

    kind = chat_kind(update)
    if kind == "group":
        await handle_group_message(update)
    elif kind == "private":
        await handle_manager_dm(update)
    # else: some group we don't serve - ignore


async def handle_group_message(update: Update):
    """A plain message in the group. Instead of Miles gatekeeping every reply, a
    lightweight router picks the best-fit teammate(s), who answer as themselves.
    Only genuinely multi-step/coordination requests go to Miles to orchestrate."""
    text = update.message.text
    lowered = text.lower()

    for key in SPECIALIST_KEYS:
        if f"@{bot_usernames[key]}".lower() in lowered:
            return  # addressed to a specific specialist - their own handler owns this

    if text.strip() in ("/start", f"@{bot_usernames['manager']}", f"@{bot_usernames['manager']} /start"):
        await update.message.reply_text(AGENT_INFO["manager"]["welcome"])
        return

    async with locks["manager"]:
        main.set_conversation("group")
        main.set_reply_context({"kind": "group"})

        # A /confirm (or cancel) for something staged in the group is resolved here.
        if await _handle_pending_confirmation(update, text):
            return

        # /project and /linear slash commands are handled directly (not routed to Miles).
        if await _maybe_handle_project_linear_command(update, text):
            return

        # /approve and /cancel drive the background execution engine, so they're
        # intercepted here rather than handled as plain string commands.
        parsed_company = company_mode.parse_company_command(text)
        if parsed_company and parsed_company[0] == "/approve":
            await start_company_plan(update)
            return
        if parsed_company and parsed_company[0] == "/cancel":
            _cancel_running_plan()
            await reply_chunks(update.message, await asyncio.to_thread(company_mode.cancel_project))
            return
        if parsed_company and parsed_company[0] == "/publish":
            await start_publish(update)
            return
        if parsed_company and parsed_company[0] == "/assign":
            await assign_with_dynamic_plan(update, parsed_company[1])
            return
        if parsed_company and parsed_company[0] == "/revenue":
            await sync_and_report_revenue(update)
            return
        if parsed_company and parsed_company[0] == "/launch":
            await start_launch(update)
            return

        company_response = company_mode.handle_company_command(
            text,
            configured_agent_keys=BOT_KEYS,
            specialist_keys=list(main.SPECIALISTS.keys()),
        )
        if company_response is not None:
            await reply_chunks(update.message, company_response)
            return

        try:
            responders = await asyncio.to_thread(main.select_group_responders, text)
        except Exception as e:
            main.logger.error(f"Group router error, falling back to Miles: {e}")
            responders = ["manager"]

        main.logger.info(f"Group router picked: {responders}")

        if responders == ["manager"]:
            # Multi-step/coordination request - Miles runs the delegation chain; each
            # delegated agent's answer is posted to the group as itself by on_delegation,
            # then Miles's recap is posted here.
            try:
                answer = await _run_metered(main.ask_manager, text)
            except Exception as e:
                main.logger.error(f"Unhandled error in manager handler: {e}")
                await update.message.reply_text("Sorry, something went wrong processing that.")
                return
            await reply_chunks(update.message, answer)
            return

        # Otherwise the chosen teammate(s) answer directly, each as themselves.
        for key in responders:
            try:
                if key == "general":
                    answer = await _run_metered(main.ask_ai, text)
                else:
                    answer = await _run_metered(main.ask_specialist, key, text)
            except Exception as e:
                main.logger.error(f"Error while '{key}' answered a group message: {e}")
                continue
            await post_agent_answer_to_group(key, answer)


async def handle_manager_dm(update: Update):
    """A 1:1 DM with Miles. He dispatches the right agents; each dispatched agent
    DMs you their answer directly (see on_delegation's manager_dm branch), and Miles
    also recaps here - so a result reaches both you and the manager, like real
    coworkers reporting back."""
    text = update.message.text
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if text.strip() == "/start":
        await update.message.reply_text(AGENT_INFO["manager"]["welcome"])
        return

    async with locks["manager"]:
        main.set_conversation(f"dm:manager:{user_id}")
        main.set_reply_context({"kind": "manager_dm", "user_id": user_id, "chat_id": chat_id})

        if await _handle_pending_confirmation(update, text):
            return

        # /project and /linear slash commands are handled directly (not routed to Miles).
        if await _maybe_handle_project_linear_command(update, text):
            return

        company_command = company_mode.parse_company_command(text)
        if company_command is not None:
            if company_command[0] in {"/setbudget", "/assign", "/approve", "/cancel", "/publish", "/launch", "/link", "/revenue", "/pausecompany", "/resumecompany"}:
                await update.message.reply_text(
                    "Company Mode changes happen in the group operating room. "
                    "Use /company, /status, or /dailyreport here for read-only context."
                )
            else:
                company_response = company_mode.handle_company_command(
                    text,
                    configured_agent_keys=BOT_KEYS,
                    specialist_keys=list(main.SPECIALISTS.keys()),
                )
                await reply_chunks(update.message, company_response)
            return

        try:
            answer = await _run_metered(main.ask_manager, text)
        except Exception as e:
            main.logger.error(f"Unhandled error in manager DM handler: {e}")
            await update.message.reply_text("Sorry, something went wrong processing that.")
            return

    await reply_chunks(update.message, answer)


def on_delegation(specialist_key, request_text, answer_text):
    """Posts a dispatched agent's answer where the current turn wants it. Called from
    execute_tool (main.py) on a worker thread, so it hands the actual Telegram calls
    back to the event loop via run_coroutine_threadsafe. The reply context (set by the
    handler that started this turn) decides the destination:
      - group:      Miles announces the hand-off and the agent posts its answer, both
                    to the group, as their own bots (unchanged group behavior).
      - manager_dm: the agent DMs the user directly as itself; if it has no bot yet or
                    the user never opened its DM (Telegram blocks a cold first message),
                    Miles relays the answer (labeled) in the manager DM instead."""
    ctx = main.current_reply_context() or {"kind": "group"}
    label = "General Assistant" if specialist_key == "general" else main.SPECIALISTS[specialist_key]["label"]
    target_bot = bots.get(specialist_key, bots["manager"])
    try:
        company_mode.record_delegation(specialist_key, request_text, answer_text)
    except Exception as e:
        main.logger.error(f"Failed to record Company Mode delegation: {e}")

    async def post_group():
        try:
            if specialist_key != "general":
                await send_chunks(bots["manager"], GROUP_CHAT_ID, f"Delegating to the {label}: {request_text}")
            await send_chunks(target_bot, GROUP_CHAT_ID, answer_text)
        except Exception as e:
            main.logger.error(f"Failed to post delegation visibility message: {e}")

    async def post_dm():
        user_id = ctx["user_id"]
        has_own_bot = specialist_key != "general" and specialist_key in bots
        if has_own_bot:
            try:
                # A user's private chat id equals their Telegram user id.
                await send_chunks(target_bot, user_id, answer_text)
                return
            except Exception as e:
                main.logger.info(f"'{specialist_key}' couldn't DM the user directly ({e}); Miles will relay.")
        try:
            await send_chunks(bots["manager"], ctx["chat_id"], f"{label} says:\n{answer_text}")
        except Exception as e:
            main.logger.error(f"Failed to relay delegation answer in the manager DM: {e}")

    coro = post_dm() if ctx.get("kind") == "manager_dm" else post_group()
    asyncio.run_coroutine_threadsafe(coro, main_loop)


# --------------------------------------------------------------------------- #
# Company Mode v2: metered budget + checkpointed autonomous execution engine.
# /assign proposes a plan; /approve starts run_company_plan, which works one task at
# a time, metering real token spend, honoring /pausecompany and a hard daily cap
# between tasks, and linking each finished task to a real deliverable (artifact).
# --------------------------------------------------------------------------- #

async def _run_metered(fn, *args):
    """Run a blocking main.ask_* call with a cost sink attached, then bill the real
    token spend (and any artifacts) against today's budget as ad-hoc chat spend. Used
    for reactive Miles delegations so the daily ledger reflects all delegated work,
    not just the autonomous engine. Does NOT set company_execution, so normal
    /confirm gating for file writes/email is unchanged in ad-hoc chat."""
    sink = {"cost_usd": 0.0, "artifacts": [], "context": "ad-hoc chat"}

    def work():
        main.set_execution_sink(sink)
        try:
            return fn(*args)
        finally:
            main.set_execution_sink(None)

    answer = await asyncio.to_thread(work)
    try:
        await asyncio.to_thread(company_mode.record_adhoc_spend, sink["cost_usd"], sink["artifacts"])
    except Exception as e:
        main.logger.error(f"Failed to record ad-hoc spend: {e}")
    return answer


def _cancel_running_plan():
    global company_runner_task
    if company_runner_task and not company_runner_task.done():
        company_runner_task.cancel()
    company_runner_task = None


async def start_company_plan(update):
    """Handle /approve: flip the project to active and kick off the background runner."""
    global company_runner_task
    if company_runner_task and not company_runner_task.done():
        await update.message.reply_text(
            "A work plan is already running. Use /pausecompany to halt it or /cancel to stop it."
        )
        return

    message, project_id = await asyncio.to_thread(company_mode.approve_project)
    await update.message.reply_text(message)
    if project_id:
        # approve_project already mirrored the tasks into Linear (via the hook);
        # surface the created issues so you get the tracker links up front.
        await _post_linear_mirror_summary(project_id)
        company_runner_task = asyncio.create_task(run_company_plan(project_id))


async def _post_linear_mirror_summary(project_id):
    """If Company Mode mirrored this project's tasks into Linear, post the issue list."""
    if not company_linear.is_enabled():
        return
    try:
        state = await asyncio.to_thread(company_mode.load_state)
    except Exception:
        return
    tasks = company_mode.project_tasks(state, project_id)
    mirrored = [t for t in tasks if t.get("linear_identifier")]
    if not mirrored:
        return
    lines = [f"Linear: mirrored {len(mirrored)} task(s) as issues you can track:"]
    for task in mirrored:
        lines.append(f"- {task['linear_identifier']}: {task['title']}\n  {task.get('linear_url', '')}".rstrip())
    await post_to_group("\n".join(lines), "manager")


async def assign_with_dynamic_plan(update, goal):
    """Handle /assign: have Miles plan a tailored work plan for THIS goal (which
    agents, in what order) instead of the fixed 4-task default, then reserve budget
    and propose it. Falls back to the default plan if planning fails."""
    goal = (goal or "").strip()
    if not goal:
        await update.message.reply_text("Usage: /assign <goal>")
        return

    await post_to_group("Planning the work for that goal...", "manager")
    # SPECIALIST_KEYS are the agents with live bots, so a planned owner can speak as itself.
    plan = await _run_metered(main.plan_company_goal, goal, SPECIALIST_KEYS)
    result = await asyncio.to_thread(
        company_mode.assign_goal,
        goal, BOT_KEYS, list(main.SPECIALISTS.keys()), company_mode.COMPANY_STATE_FILE, plan,
    )
    await reply_chunks(update.message, result)


async def assign_from_linear(update, identifier):
    """Handle /linear do <issue>: fetch the Linear issue and seed a supervised Company
    Mode project from it - routed to the best-fit owner plus an editor review - tagging
    the project with the source issue so the engine syncs status back to THAT issue.
    Then it's the normal flow: reply /approve to start."""
    identifier = (identifier or "").strip()
    if not identifier:
        await update.message.reply_text(
            "Usage: /linear do <issue id or text>  (e.g. /linear do VAN-46)"
        )
        return
    if not company_linear.is_enabled():
        await update.message.reply_text(linear_helpers.NOT_CONFIGURED)
        return

    issue, err = await asyncio.to_thread(linear_helpers.get_issue, identifier)
    if err or not issue:
        await update.message.reply_text(err or f"Couldn't find a Linear issue for '{identifier}'.")
        return

    ident = issue.get("identifier", identifier)
    title = (issue.get("title") or "").strip() or ident
    description = (issue.get("description") or "").strip()

    # Route the implementation to the best-fit owner (default to Patch/code).
    try:
        responders = await asyncio.to_thread(main.select_group_responders, f"{title}\n\n{description}")
    except Exception:
        responders = ["code"]
    owner = next((r for r in responders if r in main.SPECIALISTS), "code")

    goal = f"Complete Linear issue {ident}: {title}"
    if description:
        goal += f"\n\nIssue details / acceptance criteria:\n{description}"
    review_title = (f"Review the deliverable for {ident} against the issue's acceptance "
                    f"criteria; approve, or list the required changes.")
    tasks = [(owner, title), ("editor", review_title)]

    result = await asyncio.to_thread(
        company_mode.assign_goal,
        goal, BOT_KEYS, list(main.SPECIALISTS.keys()), company_mode.COMPANY_STATE_FILE, tasks,
    )

    # assign_goal only creates a project on success; on a block/pause it returns a
    # message and leaves the (old) active project untouched - so don't tag in that case.
    created = not result.startswith(("Blocked:", "Company Mode is paused", "Usage:"))
    linked_note = ""
    if created:
        state = await asyncio.to_thread(company_mode.load_state)
        project = company_mode.active_project(state)
        if project:
            await asyncio.to_thread(company_mode.set_project_source_issue, project["id"], issue)
            linked_note = (
                f"\n\nLinked to {ident}: it moves to In Progress on /approve and to Done "
                f"once the editor approves.\n{issue.get('url', '')}"
            )
    await reply_chunks(update.message, f"{result}{linked_note}")


async def sync_and_report_revenue(update):
    """Handle /revenue: pull live sales from Gumroad (I/O off the loop), update the
    product registry, and show the P&L. If the live pull fails (e.g. no token), still
    show the last-synced P&L plus why the pull was skipped."""
    products, err = await asyncio.to_thread(gumroad_helpers.list_products)
    note = f"\n\n(Live Gumroad sync skipped: {err})" if err else ""
    if not err:
        await asyncio.to_thread(company_mode.sync_revenue, products)
    pnl = await asyncio.to_thread(company_mode.render_pnl)

    # Miles reads the P&L and recommends the next move (only when there's data).
    state = await asyncio.to_thread(company_mode.load_state)
    if state.get("products"):
        rec = await _run_metered(main.recommend_next_move, pnl)
        pnl = f"{pnl}\n\n{rec}"

    await reply_chunks(update.message, f"{pnl}{note}")


def _defer_remaining(project_id):
    """Mark still-planned tasks as blocked (deferred) and release their reserve when
    the daily budget runs out mid-plan."""
    state = company_mode.load_state()
    for task in company_mode.project_tasks(state, project_id):
        if task["status"] == "planned":
            company_mode.update_task_status(
                task["id"], "blocked", result="Deferred: daily budget exhausted.", spent_usd=0.0
            )


def _complete_project(project_id):
    state = company_mode.load_state()
    for project in state["projects"]:
        if project["id"] == project_id:
            project["status"] = "completed"
    company_mode.save_state(state)


async def _run_one_task(project, task):
    """Execute a single task through its owner agent under company-execution rules.
    Returns "done" or "blocked" (a gated action needs the user's /confirm)."""
    owner = task["owner"] if task["owner"] in main.SPECIALISTS else None
    context = f"Project {project['id']} / task {task['id']}"
    sink = {"cost_usd": 0.0, "artifacts": [], "context": context}

    # Feed earlier tasks' summaries AND the current deliverable's real content into this
    # task's prompt so the agent builds on the actual file (even one a teammate saved to
    # GitHub that this agent can't read itself) instead of producing a duplicate.
    state_now = await asyncio.to_thread(company_mode.load_state)
    prior_work = company_mode.prior_work_summary(state_now, project["id"], task["id"])
    deliverable_name, deliverable_content = await asyncio.to_thread(
        _load_project_deliverable, state_now, project["id"]
    )
    prompt = company_mode.build_task_prompt(
        project, task, prior_work, deliverable_name, deliverable_content
    )

    await asyncio.to_thread(company_mode.update_task_status, task["id"], "in_progress")
    await post_to_group(f"Starting: {task['owner']} - {task['title']}", "manager")

    def work():
        main.set_conversation("group")
        main.set_reply_context({"kind": "group"})
        main.set_execution_sink(sink)
        main.set_company_execution(True)
        try:
            if owner:
                return main.ask_specialist(owner, prompt, record_history=False)
            return main.ask_ai(prompt, record_history=False)
        finally:
            main.set_company_execution(False)
            main.set_execution_sink(None)

    async with locks["manager"]:
        try:
            answer = await asyncio.to_thread(work)
        except Exception as e:
            main.logger.error(f"Company task {task['id']} errored: {e}")
            await asyncio.to_thread(
                company_mode.mark_task_blocked, task["id"],
                "Errored while running - see logs.", sink["cost_usd"], sink["artifacts"],
            )
            await post_to_group(f"Task {task['id']} hit an error and was parked.", "manager")
            return "blocked"

    await post_agent_answer_to_group(owner or "manager", answer)

    spent = sink["cost_usd"]
    artifacts = sink["artifacts"]

    # A gated action (send email / delete) staged during the run means the task needs
    # your approval. It's keyed to the "group" conversation; leave it staged so
    # /confirm resolves it, mark the task blocked, and stop so the next task can't
    # overwrite the pending action.
    staged = main.pending_actions.get("group")
    if staged is not None:
        reason = f"Needs your approval: {main.describe_pending_action(staged)}. Reply /confirm to proceed."
        await asyncio.to_thread(
            company_mode.mark_task_blocked, task["id"], reason, spent, artifacts
        )
        await post_to_group(reason, "manager")
        return "blocked"

    await asyncio.to_thread(
        company_mode.update_task_status, task["id"], "done", answer[:1000], artifacts, spent
    )
    if owner == "editor":
        await asyncio.to_thread(company_mode.set_project_revision_flag, project["id"], answer)
    state = await asyncio.to_thread(company_mode.load_state)
    await post_to_group(company_mode.render_money(state), "manager")
    return "done"


async def run_company_plan(project_id):
    """Work a project's tasks one at a time until done, paused, blocked, or the daily
    budget is exhausted. Checks pause + budget between every task (the checkpoints)."""
    try:
        while True:
            state = await asyncio.to_thread(company_mode.load_state)
            project = next((p for p in state["projects"] if p["id"] == project_id), None)
            if not project or project["status"] != "active":
                return  # cancelled or completed elsewhere

            if state["company"]["mode"] == "paused":
                await post_to_group(
                    "Company Mode is paused - work plan halted. /resumecompany then /approve to continue.",
                    "manager",
                )
                return

            if company_mode.remaining_budget(state) <= 0:
                await asyncio.to_thread(_defer_remaining, project_id)
                await post_to_group(
                    f"Daily budget exhausted - stopping. {company_mode.render_money(state)}. "
                    "Raise /setbudget and /approve to continue.",
                    "manager",
                )
                return

            task = company_mode.next_planned_task(state, project_id)
            if task is None:
                if project.get("needs_revision"):
                    created, note = await asyncio.to_thread(
                        company_mode.start_revision_round, project_id, BOT_KEYS
                    )
                    if created:
                        await post_to_group(f"{note} Continuing the work plan.", "manager")
                        continue  # loop picks up the freshly queued revision tasks
                    await post_to_group(f"Not starting another revision round: {note}.", "manager")

                await asyncio.to_thread(_complete_project, project_id)
                # If this was a /linear do project, close out its source Linear issue
                # (Done if approved, else comment the editor's required changes).
                await asyncio.to_thread(company_linear.finalize_source_issue, project_id)
                state = await asyncio.to_thread(company_mode.load_state)
                finished = next((p for p in state["projects"] if p["id"] == project_id), project)
                note = (
                    "The Managing Editor flagged required revisions - this is NOT ready to ship. "
                    "See /dailyreport for the list."
                    if finished.get("needs_revision")
                    else "See /dailyreport for the deliverables."
                )
                await post_to_group(
                    f"Work plan complete for {project['title']}. {company_mode.render_money(state)}. {note}",
                    "manager",
                )
                return

            outcome = await _run_one_task(project, task)
            if outcome == "blocked":
                return  # wait for the user to resolve, then re-/approve

    except asyncio.CancelledError:
        raise
    except Exception as e:
        main.logger.error(f"Company plan runner crashed: {e}")
        await post_to_group("The work plan hit an unexpected error and stopped. Check the logs.", "manager")


# --------------------------------------------------------------------------- #
# Assisted publish (/publish): prep a finished project for sale on Gumroad. Gumroad
# has no product-creation/upload API (dashboard-only), so the AI does everything up
# to the final upload - it splits the deliverable into a clean buyer-download file
# plus a paste-ready listing, then stages a gated "publish" approval. On /confirm it
# marks the project published and hands over the exact go-live steps (the upload
# click is the one thing that stays yours).
# --------------------------------------------------------------------------- #

GUMROAD_GO_LIVE_STEPS = (
    "Go-live steps (this last part is yours - Gumroad has no upload API):\n"
    "1. Open https://app.gumroad.com/products/new and choose 'Digital product'.\n"
    "2. Copy the Product name, description, price, and tags from the *-gumroad-listing.md file.\n"
    "3. Upload the *-product.md file as the content (export it to PDF first for a nicer buyer experience).\n"
    "4. Add a cover image (use the cover idea in the listing file).\n"
    "5. Set the permalink and hit Publish.\n"
    "6. Paste the product link back here so we can track it (revenue tracking lands in v3)."
)


def _load_project_deliverable(state, project_id):
    """Find the finished deliverable for a project and return (name, content), or
    (None, None). Prefers the GitHub copy (persists across redeploys) and falls back
    to the local files/ copy."""
    github_path = None
    local_name = None
    for task in company_mode.project_tasks(state, project_id):
        for art in task.get("artifacts", []):
            if art.startswith("github: "):
                github_path = art[len("github: "):].strip()
            elif art.startswith("file: files/"):
                local_name = art[len("file: files/"):].strip()

    if github_path:
        content = main.github_helpers.read_file(github_path)
        bad = ("GitHub isn't configured", "Sorry, couldn't", "File not found", "not a readable")
        if content and not any(content.startswith(b) for b in bad):
            return github_path.rsplit("/", 1)[-1], content

    if local_name:
        path = main.get_safe_file_path(local_name)
        if path and path.exists():
            return local_name, main.read_limited_text(path)

    return None, None


def _publish_prompt(slug, src_name, content):
    return (
        "You are packaging a finished digital product for sale on Gumroad. Below is the "
        "current working file, which mixes the product content with sales/marketing copy.\n\n"
        "Produce TWO files using write_file, with EXACTLY these names:\n"
        f"1. \"{slug}-product.md\" - ONLY what the buyer downloads: the actual usable "
        "product content (templates, scripts, how-to steps). Strip out the landing-page / "
        "sales copy. Keep it complete - this is what the customer pays for.\n"
        f"2. \"{slug}-gumroad-listing.md\" - the listing to paste into Gumroad: Product "
        "name, Price ($19 launch), a compelling listing description, a one-line tagline, "
        "3-5 suggested tags, and a one-line cover-image idea.\n\n"
        "Create no other files, and do not ask questions - produce both files now.\n\n"
        f"Current working file ({src_name}):\n---\n{content}\n---"
    )


async def start_publish(update):
    """Handle /publish: prep the active project's deliverable for sale, then stage a
    gated publish approval."""
    global company_runner_task
    if company_runner_task and not company_runner_task.done():
        await update.message.reply_text("Something's already running. Let it finish (or /cancel) before /publish.")
        return

    state = await asyncio.to_thread(company_mode.load_state)
    project = company_mode.active_project(state)
    if not project:
        await update.message.reply_text("No active project to publish. /assign and /approve a goal first.")
        return

    src_name, content = await asyncio.to_thread(_load_project_deliverable, state, project["id"])
    if not content:
        await update.message.reply_text(
            "I couldn't find a finished file to publish. Run /approve so the team produces a deliverable first."
        )
        return

    company_runner_task = asyncio.create_task(run_publish(project, src_name, content))


async def run_publish(project, src_name, content):
    """Split the deliverable into a buyer-download file + a paste-ready Gumroad
    listing, then stage the gated publish approval."""
    slug = src_name.rsplit(".", 1)[0]
    sink = {"cost_usd": 0.0, "artifacts": [], "context": f"Publish prep for {project['id']}"}
    prompt = _publish_prompt(slug, src_name, content)

    await post_to_group(f"Prepping the publish package for '{project['title']}'...", "manager")

    def work():
        main.set_conversation("group")
        main.set_reply_context({"kind": "group"})
        main.set_execution_sink(sink)
        main.set_company_execution(True)
        try:
            return main.ask_specialist("write", prompt, record_history=False)
        finally:
            main.set_company_execution(False)
            main.set_execution_sink(None)

    async with locks["manager"]:
        try:
            answer = await asyncio.to_thread(work)
        except Exception as e:
            main.logger.error(f"Publish prep failed: {e}")
            await post_to_group("Publish prep hit an error - check the logs.", "manager")
            return

    await post_agent_answer_to_group("write", answer)
    try:
        await asyncio.to_thread(company_mode.record_adhoc_spend, sink["cost_usd"], sink["artifacts"])
    except Exception as e:
        main.logger.error(f"Failed to record publish spend: {e}")

    # Stage the gated publish approval in the group conversation.
    main.set_conversation("group")
    main.set_pending_action({
        "type": "publish",
        "project_id": project["id"],
        "title": project["title"],
        "company_context": f"Project {project['id']}",
    })
    files = ", ".join(sink["artifacts"]) or "(no new files detected - check the message above)"
    await post_to_group(
        f"Publish package ready for '{project['title']}'.\nFiles: {files}\n\n"
        "Review them, then reply /confirm to approve publishing - I'll mark it published "
        "and hand you the exact Gumroad go-live steps. Anything else cancels.\n"
        "Heads up: Gumroad has no upload API, so the final upload click is yours; I prep "
        "everything up to it.",
        "manager",
    )


# --------------------------------------------------------------------------- #
# Auto-draft distribution (/launch): draft launch posts + a launch email + image-
# generation prompts for the product's visuals, grounded in the real deliverable.
# --------------------------------------------------------------------------- #

def _launch_prompt(slug, product_name, content):
    return (
        "You are drafting a LAUNCH KIT for a finished digital product. Use write_file to "
        f'save ONE file named "{slug}-launch-kit.md" containing, clearly sectioned:\n'
        "1. 2 LinkedIn posts - value-first, builder voice, not a hard ad - each ending with "
        "the product link placeholder [LINK].\n"
        "2. 2 short X/Twitter posts.\n"
        "3. 1 launch email (subject line + body).\n"
        "4. IMAGE PROMPTS - ready-to-paste text-to-image prompts (Canva/DALL-E style) for a "
        "product COVER (16:9), a THUMBNAIL (1:1 square), and a SOCIAL CARD. Each prompt "
        "should describe a clean, on-brand graphic with NO text/letters (the user adds the "
        "title in Canva), and leave empty space for a headline.\n\n"
        "Lead with value, keep everything tight, and ground it all in the actual product below.\n"
        f"Product: {product_name}\n---\n{content}\n---"
    )


async def start_launch(update):
    """Handle /launch: draft a launch kit (posts + email + image prompts) for the
    active/most-recent project's finished deliverable."""
    global company_runner_task
    if company_runner_task and not company_runner_task.done():
        await update.message.reply_text("Something's already running. Let it finish (or /cancel) before /launch.")
        return

    state = await asyncio.to_thread(company_mode.load_state)
    project = company_mode.active_project(state) or (state["projects"][-1] if state.get("projects") else None)
    if not project:
        await update.message.reply_text("No project to launch yet. /assign and build something first.")
        return

    src_name, content = await asyncio.to_thread(_load_project_deliverable, state, project["id"])
    if not content:
        await update.message.reply_text("No finished deliverable to base a launch kit on. Run /approve first.")
        return

    company_runner_task = asyncio.create_task(run_launch(project, src_name, content))


async def run_launch(project, src_name, content):
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", (src_name or project["title"]).rsplit(".", 1)[0]).strip("-")
    sink = {"cost_usd": 0.0, "artifacts": [], "context": f"Launch kit for {project['id']}"}
    prompt = _launch_prompt(slug, project["title"], content)

    await post_to_group(f"Drafting a launch kit for '{project['title']}'...", "manager")

    def work():
        main.set_conversation("group")
        main.set_reply_context({"kind": "group"})
        main.set_execution_sink(sink)
        main.set_company_execution(True)
        try:
            return main.ask_specialist("write", prompt, record_history=False)
        finally:
            main.set_company_execution(False)
            main.set_execution_sink(None)

    async with locks["manager"]:
        try:
            answer = await asyncio.to_thread(work)
        except Exception as e:
            main.logger.error(f"Launch kit failed: {e}")
            await post_to_group("Launch kit drafting hit an error - check the logs.", "manager")
            return

    await post_agent_answer_to_group("write", answer)
    try:
        await asyncio.to_thread(company_mode.record_adhoc_spend, sink["cost_usd"], sink["artifacts"])
    except Exception as e:
        main.logger.error(f"Failed to record launch spend: {e}")

    files = ", ".join(sink["artifacts"]) or "(see the message above)"
    await post_to_group(
        f"Launch kit ready for '{project['title']}'. Files: {files}\n"
        "It has LinkedIn/X posts, a launch email, and image prompts for your cover, "
        "thumbnail, and social card - paste those into Canva/an image generator.",
        "manager",
    )


# --------------------------------------------------------------------------- #
# Proactive scheduling: morning briefing, timed reminders, calendar event alerts.
# The AsyncIOScheduler runs on the same event loop as the bots; job functions here
# are coroutines, which APScheduler awaits directly.
# --------------------------------------------------------------------------- #

scheduler = None
# Keys of event alerts already scheduled today, so re-syncing doesn't double-post.
scheduled_event_alert_keys = set()


def _app_timezone():
    try:
        return ZoneInfo(main.TIMEZONE)
    except Exception:
        return None


def _now():
    tz = _app_timezone()
    return datetime.now(tz) if tz else datetime.now().astimezone()


async def post_to_group(text, bot_key="manager"):
    """Post a message to the group as the given agent's bot, falling back to Miles
    if that agent doesn't have its own bot yet (e.g. Cadence before you create it)."""
    bot = bots.get(bot_key, bots["manager"])
    try:
        await send_chunks(bot, GROUP_CHAT_ID, text)
    except Exception as e:
        main.logger.error(f"Failed to post scheduled message: {e}")


async def post_morning_briefing():
    # build_morning_briefing does blocking work (LLM + HTTP) - run it off the loop.
    text = await asyncio.to_thread(main.build_morning_briefing)
    await post_to_group(text, "manager")
    # Fresh day - (re)schedule today's event alerts right after the briefing.
    await schedule_todays_event_alerts()


async def post_daily_company_report():
    text = await asyncio.to_thread(company_mode.build_daily_report)
    # If products are linked, append the P&L and Miles's next-move recommendation so
    # the daily report is a real business review, not just a work log.
    state = await asyncio.to_thread(company_mode.load_state)
    if state.get("products"):
        pnl = await asyncio.to_thread(company_mode.render_pnl)
        rec = await _run_metered(main.recommend_next_move, pnl)
        text = f"{text}\n\n{pnl}\n\n{rec}"
    await post_to_group(text, "manager")


async def fire_reminder(reminder):
    await post_to_group(f"Reminder: {reminder['text']}", "calendar")
    main.mark_reminder_fired(reminder["id"])


def schedule_reminder(reminder):
    """Schedule one future reminder. Called live via main.on_reminder_set (from a
    worker thread - APScheduler's add_job is thread-safe) and during startup reload."""
    try:
        due = datetime.fromisoformat(reminder["due_iso"])
    except ValueError:
        main.logger.error(f"Bad reminder due_iso: {reminder['due_iso']!r}")
        return

    scheduler.add_job(
        fire_reminder,
        trigger=DateTrigger(run_date=due),
        args=[reminder],
        id=f"reminder-{reminder['id']}",
        replace_existing=True,
    )


async def reload_reminders():
    """On startup, re-schedule future reminders and fire any that came due while the
    bot was offline (so a redeploy doesn't silently swallow them)."""
    now = _now()
    for reminder in main.load_reminders():
        if reminder["fired"]:
            continue

        try:
            due = datetime.fromisoformat(reminder["due_iso"])
        except ValueError:
            continue

        now_cmp = now.replace(tzinfo=None) if due.tzinfo is None else now
        if due <= now_cmp:
            await post_to_group(f"Reminder (missed while I was offline): {reminder['text']}", "calendar")
            main.mark_reminder_fired(reminder["id"])
        else:
            schedule_reminder(reminder)


async def post_event_alert(summary, start):
    await post_to_group(
        f"Heads up: '{summary}' starts at {start.strftime('%H:%M')} "
        f"(in about {main.EVENT_ALERT_MINUTES} min).",
        "calendar",
    )


async def schedule_todays_event_alerts():
    """Schedule a heads-up EVENT_ALERT_MINUTES before each of today's timed events.
    Degrades to a no-op if Google isn't connected (get_today_events_raw returns [])."""
    events = await asyncio.to_thread(main.google_helpers.get_today_events_raw)
    now = _now()

    for event in events:
        start = event["start"]
        alert_time = start - timedelta(minutes=main.EVENT_ALERT_MINUTES)
        key = f"{event['summary']}|{start.isoformat()}"

        if key in scheduled_event_alert_keys or alert_time <= now:
            continue

        scheduled_event_alert_keys.add(key)
        scheduler.add_job(
            post_event_alert,
            trigger=DateTrigger(run_date=alert_time),
            args=[event["summary"], start],
            id=f"eventalert-{key}",
            replace_existing=True,
        )


def _parse_briefing_time(value):
    try:
        hour, minute = value.split(":")
        return int(hour), int(minute)
    except (ValueError, AttributeError):
        main.logger.error(f"Bad BRIEFING_TIME {value!r}, defaulting to 08:00")
        return 8, 0


async def start_scheduler():
    global scheduler
    tz = _app_timezone()
    scheduler = AsyncIOScheduler(timezone=tz) if tz else AsyncIOScheduler()

    hour, minute = _parse_briefing_time(main.BRIEFING_TIME)
    scheduler.add_job(
        post_morning_briefing,
        trigger=CronTrigger(hour=hour, minute=minute),
        id="morning-briefing",
        replace_existing=True,
    )

    report_hour, report_minute = _parse_briefing_time(DAILY_REPORT_TIME)
    scheduler.add_job(
        post_daily_company_report,
        trigger=CronTrigger(hour=report_hour, minute=report_minute),
        id="daily-company-report",
        replace_existing=True,
    )
    scheduler.start()

    # Live reminder scheduling comes through this hook from execute_tool/set_reminder.
    main.on_reminder_set = schedule_reminder

    await reload_reminders()
    await schedule_todays_event_alerts()
    print(
        f"Scheduler running - morning briefing at {main.BRIEFING_TIME}, "
        f"daily company report at {DAILY_REPORT_TIME} ({main.TIMEZONE})."
    )


async def run_all():
    global main_loop
    main_loop = asyncio.get_running_loop()

    # Build every Application and resolve every bot's own username FIRST,
    # before any of them start polling - so by the time any bot can receive
    # a message, every handler can safely look up any other bot's username
    # (e.g. the Manager checking for @mentions of specialists that haven't
    # started polling yet would otherwise KeyError).
    for key in BOT_KEYS:
        token = _require_env(
            AGENT_INFO[key]["env_var"],
            f"It's the BotFather token for the '{key}' bot listed in BOT_KEYS.",
        )
        app = ApplicationBuilder().token(token).build()
        applications[key] = app
        bots[key] = app.bot
        bot_usernames[key] = (await app.bot.get_me()).username
        print(f"{AGENT_INFO[key]['label']}: @{bot_usernames[key]}")

    main.on_delegation = on_delegation
    # Mirror Company Mode work into Linear (no-op unless LINEAR_API_KEY is set).
    company_linear.register()

    for key, app in applications.items():
        if key == "manager":
            app.add_handler(MessageHandler(filters.TEXT, handle_manager_message))
        else:
            app.add_handler(MessageHandler(filters.TEXT, build_specialist_handler(key)))

        await app.initialize()
        await app.start()
        await app.updater.start_polling()

    print(f"All {len(BOT_KEYS)} bots running (polling). Press Ctrl+C to stop.")

    await start_scheduler()

    try:
        await asyncio.Event().wait()
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        for app in applications.values():
            await app.updater.stop()
            await app.stop()
            await app.shutdown()


if __name__ == "__main__":
    asyncio.run(run_all())
