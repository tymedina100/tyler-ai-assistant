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
import contextvars
import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

import main
import autonomous_workflow
import autonomy_team
import company_mode
import company_linear
import gumroad_helpers
import linear_helpers
import model_router
import office_api
import office_state


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
OPTIONAL_BOT_KEYS = ["linear", "calendar", "gmail", "general", "sales", "analytics"]
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
            "action (a file write or sending an email), reply /confirm in the same "
            "chat where it was staged."
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
    "sales": {"env_var": "TELEGRAM_SALES_BOT_TOKEN", "tagline": "@mention me to draft outreach or check the sales pipeline."},
    "analytics": {"env_var": "TELEGRAM_ANALYTICS_BOT_TOKEN", "tagline": "@mention me for a numbers digest and the one move to make next."},
    # Robin (the general assistant) isn't a main.SPECIALISTS entry - it's the
    # all-rounder fallback that runs through main.ask_ai with the full toolset. So,
    # like the manager, it carries its own label/welcome here and is skipped in the
    # SPECIALISTS label loop below.
    "general": {
        "env_var": "TELEGRAM_GENERAL_BOT_TOKEN",
        "label": "Robin (General Assistant)",
        "welcome": (
            "Robin here - the team's all-rounder. @mention me for anything that "
            "doesn't clearly belong to a specialist and I'll take a crack at it."
        ),
    },
}

# Fill in each specialist's label + welcome from main.SPECIALISTS (the single
# source of truth for persona names). A label looks like "Scout (Researcher
# Agent)"; the part in parentheses is the human-readable role, which we reuse to
# build a greeting like "Scout here - the Researcher Agent. <tagline>".
# manager and general aren't SPECIALISTS entries, so they keep their own labels.
for _key, _info in AGENT_INFO.items():
    if _key in ("manager", "general"):
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
autonomy_runner_task = None
_autonomy_workflow_instance = None
AUTONOMY_CONFIG = autonomous_workflow.AutonomyConfig.from_env()
AUTONOMY_ROUTER = model_router.ModelRouter()
_suppress_company_updates = contextvars.ContextVar("suppress_company_updates", default=False)
office_api_server = None

# Transient office states remain visible long enough for the browser's 1.5-second
# polling loop to show them, then office_state renders them as idle automatically.
OFFICE_REPLY_SECONDS = 12
OFFICE_ERROR_SECONDS = 15


def _office_call(method, *args, **kwargs):
    """Keep optional visual telemetry from ever affecting Telegram behavior."""
    try:
        return getattr(office_state, method)(*args, **kwargs)
    except Exception as e:
        main.logger.error(f"Virtual office state update failed ({method}): {e}")
        return None


def _office_role(label):
    """Extract the existing human-readable role from a roster label."""
    return label.split("(", 1)[1].rstrip(")") if "(" in label else label


def _office_roster():
    """Build enabled-agent display metadata from the existing roster authority."""
    roster = {}
    for key in BOT_KEYS:
        if key in main.SPECIALISTS:
            profile = main.SPECIALISTS[key]
            roster[key] = {"name": profile["name"], "role": _office_role(profile["label"])}
        else:
            label = AGENT_INFO[key]["label"]
            roster[key] = {"name": label.split("(", 1)[0].strip(), "role": _office_role(label)}
    return roster


def on_delegation_started(specialist_key, request_text):
    """Visual-only lifecycle hook fired immediately before a delegated specialist runs."""
    label = "Robin" if specialist_key == "general" else AGENT_INFO[specialist_key]["label"].split("(", 1)[0].strip()
    _office_call("set_agent_status", "manager", "delegated", f"Delegating to {label}")
    _office_call("set_agent_status", specialist_key, "thinking", request_text)
    _office_call("add_event", "delegated", "manager", f"Miles delegated work to {label}.")


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
    if _suppress_company_updates.get():
        return
    bot = bots.get(key, bots["manager"])
    await send_chunks(bot, GROUP_CHAT_ID, answer)


def _strip_bot_suffix(text):
    """Telegram appends the bot's @username to a command picked from the command menu
    in a multi-bot group (e.g. "/confirm@TyManagerBot"). Strip that suffix from the
    LEADING command token so exact-match checks like "/confirm" and "/today" still
    recognize the command - the same normalization parse_company_command does. Only
    a leading slash command is touched, so an @mention or email address in ordinary
    text is never altered."""
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return stripped
    command, sep, rest = stripped.partition(" ")
    return command.split("@", 1)[0] + sep + rest


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
    command = _strip_bot_suffix(text)

    # A "publish" approval is resolved here (in group_bot) rather than via
    # main.confirm_pending_action, since publishing is a Company Mode concept and
    # main.py stays independent of company_mode.
    if pending.get("type") == "publish":
        if command == "/confirm":
            msg = await asyncio.to_thread(company_mode.mark_project_published)
            main.logger.info(f"Telegram user {update.effective_user.id} confirmed {description}")
            await reply_chunks(update.message, f"{msg}\n\n{GUMROAD_GO_LIVE_STEPS}")
        else:
            main.logger.info(f"Telegram user {update.effective_user.id} cancelled {description}")
            await update.message.reply_text(f"Cancelled the {description}. Nothing was marked published.")
        return True

    if command == "/confirm":
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

                _office_call("set_agent_status", key, "thinking", request)
                if kind == "group":
                    _office_call("add_event", "thinking", key, "Started a direct request.")

                # Robin (general) isn't a SPECIALISTS entry; it runs through ask_ai
                # (all-rounder, full toolset) rather than ask_specialist.
                if key == "general":
                    answer = await _run_metered(main.ask_ai, request)
                else:
                    answer = await _run_metered(main.ask_specialist, key, request)
        except Exception as e:
            main.logger.error(f"Unhandled error in {key} specialist handler: {e}")
            _office_call(
                "set_agent_status", key, "error", "Could not complete that request.", OFFICE_ERROR_SECONDS
            )
            if kind == "group":
                _office_call("add_event", "error", key, "Could not complete a direct request.")
            await update.message.reply_text("Sorry, something went wrong processing that.")
            return

        # Split into <=4096-char chunks (and guard against an empty answer, which
        # Telegram also rejects) - see reply_chunks / TELEGRAM_LIMIT above.
        _office_call("set_agent_status", key, "speaking", answer, OFFICE_REPLY_SECONDS)
        if kind == "group":
            _office_call("add_event", "reply", key, answer)
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
    stripped = _strip_bot_suffix(text)
    lowered = stripped.lower()

    if lowered == "/today":
        response = await asyncio.to_thread(main.handle_today_command)
        await reply_chunks(update.message, response)
        return True

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
    _office_call("mark_message_received", text)

    for key in SPECIALIST_KEYS:
        if f"@{bot_usernames[key]}".lower() in lowered:
            _office_call("set_agent_status", "manager", "idle")
            return  # addressed to a specific specialist - their own handler owns this

    if _strip_bot_suffix(text) in ("/start", f"@{bot_usernames['manager']}", f"@{bot_usernames['manager']} /start"):
        await update.message.reply_text(AGENT_INFO["manager"]["welcome"])
        return

    async with locks["manager"]:
        main.set_conversation("group")
        main.set_reply_context({"kind": "group"})

        # A /confirm (or cancel) for something staged in the group is resolved here.
        if await _handle_pending_confirmation(update, text):
            return

        if re.match(r"^/autorun(?:@[A-Za-z0-9_]+)?(?:\s|$)", text.strip(), flags=re.I):
            await handle_autorun_command(update, text, allow_live=True)
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

        _office_call("set_agent_status", "manager", "thinking", text)
        try:
            responders = await _run_metered(
                main.select_group_responders,
                text,
                estimate_usd=0.02,
                context="group message routing",
                agent="manager",
                meter_model=main.FAST_MODEL,
            )
        except Exception as e:
            main.logger.error(f"Group router error, falling back to Miles: {e}")
            responders = ["manager"]

        main.logger.info(f"Group router picked: {responders}")

        if responders == ["manager"]:
            # Multi-step/coordination request - Miles runs the delegation chain; each
            # delegated agent's answer is posted to the group as itself by on_delegation,
            # then Miles's recap is posted here.
            try:
                _office_call("set_agent_status", "manager", "delegated", "Coordinating the request.")
                answer = await _run_metered(main.ask_manager, text)
            except Exception as e:
                main.logger.error(f"Unhandled error in manager handler: {e}")
                _office_call(
                    "set_agent_status", "manager", "error", "Could not coordinate that request.", OFFICE_ERROR_SECONDS
                )
                _office_call("add_event", "error", "manager", "Miles could not complete the request.")
                await update.message.reply_text("Sorry, something went wrong processing that.")
                return
            _office_call("set_agent_status", "manager", "speaking", answer, OFFICE_REPLY_SECONDS)
            _office_call("add_event", "reply", "manager", answer)
            await reply_chunks(update.message, answer)
            return

        # Otherwise the chosen teammate(s) answer directly, each as themselves.
        _office_call("set_agent_status", "manager", "idle")
        for key in responders:
            try:
                _office_call("set_agent_status", key, "thinking", text)
                _office_call("add_event", "thinking", key, "Picked up a routed request.")
                if key == "general":
                    answer = await _run_metered(main.ask_ai, text)
                else:
                    answer = await _run_metered(main.ask_specialist, key, text)
            except Exception as e:
                main.logger.error(f"Error while '{key}' answered a group message: {e}")
                _office_call(
                    "set_agent_status", key, "error", "Could not complete that routed request.", OFFICE_ERROR_SECONDS
                )
                _office_call("add_event", "error", key, "Could not complete a routed request.")
                continue
            _office_call("set_agent_status", key, "speaking", answer, OFFICE_REPLY_SECONDS)
            _office_call("add_event", "reply", key, answer)
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

        if re.match(r"^/autorun(?:@[A-Za-z0-9_]+)?(?:\s|$)", text.strip(), flags=re.I):
            await handle_autorun_command(update, text, allow_live=False)
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
    _office_call("set_agent_status", specialist_key, "speaking", answer_text, OFFICE_REPLY_SECONDS)
    _office_call("add_event", "reply", specialist_key, answer_text)
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

async def _run_metered(
    fn,
    *args,
    estimate_usd=None,
    context="ad-hoc chat",
    agent="manager",
    meter_model=None,
    project_id=None,
    task_id=None,
    return_receipt=False,
):
    """Reserve before a paid call, then reconcile measured usage atomically.

    This is intentionally used by reactive chat and idle ideation as well as the
    autonomous runner.  It does not enable Company Mode's produce bypass, so normal
    confirmation behavior remains unchanged outside a persisted company task.
    """
    if estimate_usd is None:
        try:
            estimate_usd = max(0.001, float(os.environ.get("ADHOC_RESERVATION_USD", "0.10")))
        except (TypeError, ValueError):
            estimate_usd = 0.10
    reservation = await asyncio.to_thread(
        company_mode.reserve_budget,
        estimate_usd,
        company_mode.COMPANY_STATE_FILE,
        context=context,
        project_id=project_id,
        task_id=task_id,
        agent=agent,
        model=meter_model or "",
        reason=f"Pre-call estimate for {context}",
    )
    sink = {
        "cost_usd": 0.0,
        "artifacts": [],
        "usage_records": [],
        "context": context,
    }

    def work():
        main.set_execution_sink(sink)
        try:
            return fn(*args)
        finally:
            main.set_execution_sink(None)

    result = None
    receipt = None
    worker_task = asyncio.create_task(asyncio.to_thread(work))
    try:
        # A Python worker thread cannot be force-cancelled safely. Shield it so a
        # task cancellation waits for the paid call to finish before reconciling
        # usage; otherwise the reservation could be released while spend continues.
        result = await asyncio.shield(worker_task)
    except asyncio.CancelledError:
        try:
            await worker_task
        except Exception as exc:
            main.logger.error(f"Metered work failed while cancellation was pending: {exc}")
        raise
    finally:
        try:
            reconciled_actual = sink["cost_usd"] if sink["usage_records"] else None
            receipt = await asyncio.to_thread(
                company_mode.reconcile_budget,
                reservation["id"],
                reconciled_actual,
                company_mode.COMPANY_STATE_FILE,
                usage_records=sink["usage_records"],
                estimated=not bool(sink["usage_records"]),
                context=context,
                project_id=project_id,
                task_id=task_id,
                agent=agent,
                model=meter_model or "",
                reason=f"Measured usage for {context}",
            )
            if sink["artifacts"]:
                await asyncio.to_thread(
                    company_mode.record_adhoc_spend,
                    0.0,
                    sink["artifacts"],
                    company_mode.COMPANY_STATE_FILE,
                    context=context,
                    project_id=project_id,
                    task_id=task_id,
                    agent=agent,
                    model=meter_model or "",
                    reason=f"Artifacts from {context}",
                )
        except Exception as e:
            main.logger.error(f"Failed to reconcile metered spend: {e}")
    if return_receipt:
        receipt = dict(receipt or {})
        receipt["artifacts"] = list(sink["artifacts"])
        receipt["usage_records"] = list(sink["usage_records"])
        return result, receipt
    return result


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
    Mode project from it - planning a tailored team for the issue (the same dynamic
    planner /assign uses) plus a guaranteed editor review - and tagging the project with
    the source issue so the engine syncs status back to THAT issue. Then it's the normal
    flow: reply /approve to start."""
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

    # Point the code tools at the repo this issue actually belongs to: map the issue's
    # Linear project (e.g. "Worthlane") to the matching assistant project and make it
    # active. Otherwise a Worthlane issue would be worked against whatever repo happened
    # to be active. No match -> leave the current active project as-is.
    linear_project = (issue.get("project") or {}).get("name", "")
    matched_key = await asyncio.to_thread(main.projects.find_by_linear_project, linear_project)
    if matched_key:
        profile, perr = await asyncio.to_thread(main.projects.set_active_project, matched_key)
        if profile and not perr:
            await post_to_group(
                f"Targeting the {profile.get('name', matched_key)} repo "
                f"({profile.get('repo')}) for {ident}.", "manager")

    goal = f"Complete Linear issue {ident}: {title}"
    if description:
        goal += f"\n\nIssue details / acceptance criteria:\n{description}"
    review_title = (f"Review the deliverable for {ident} against the issue's acceptance "
                    f"criteria; approve, or list the required changes.")

    # Plan a tailored team for the issue (research/code/write/... as it needs) with the
    # SAME dynamic planner /assign uses - not just one builder. Fall back to a single
    # routed owner if planning fails. Either way, guarantee an editor review gates it.
    await post_to_group(f"Planning the work for {ident}...", "manager")
    plan = await _run_metered(main.plan_company_goal, goal, SPECIALIST_KEYS)
    if plan:
        tasks = list(plan)
    else:
        try:
            responders = await _run_metered(
                main.select_group_responders,
                f"{title}\n\n{description}",
                context=f"Linear fallback routing for {ident}",
                agent="manager",
                meter_model=main.FAST_MODEL,
            )
        except Exception:
            responders = ["code"]
        owner = next((r for r in responders if r in main.SPECIALISTS), "code")
        tasks = [(owner, title)]
    # The editor must be the LAST task so it reviews the FINAL deliverable (see
    # company_mode.ensure_editor_gate for why a mid-plan editor isn't enough).
    tasks = company_mode.ensure_editor_gate(tasks, review_title)

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
    return company_mode.complete_project(project_id, company_mode.COMPANY_STATE_FILE)


async def _escalate_for_review(project, project_id, verdict, rounds, note=""):
    """Stop production on a project the team can't finish alone and hand it to the user.
    Marks it 'blocked' (not complete), escalates the source Linear issue (not Done), and
    posts the editor's requirements to the group so the user knows exactly what's needed."""
    await asyncio.to_thread(
        company_mode.block_project, project_id, company_mode.COMPANY_STATE_FILE
    )
    await asyncio.to_thread(company_linear.finalize_source_issue, project_id)
    state = await asyncio.to_thread(company_mode.load_state)
    blocked = next((p for p in state["projects"] if p["id"] == project_id), project)
    feedback = (blocked.get("last_editor_feedback") or "").strip()
    source = blocked.get("source_linear_issue") or {}
    ident = source.get("identifier", "")

    if verdict == "blocked":
        reason = "the editor flagged it as blocked and needing your input"
    elif note:
        reason = f"it isn't approved and can't start another revision round ({note})"
    else:
        reason = f"the editor still requires changes after {rounds} revision round(s)"

    # A blocked project can't be resumed with /approve (that only starts a proposed
    # project), so tell the user the real next step: provide what's needed, then re-run.
    resume = f"re-run /linear do {ident}" if ident else "re-assign the goal"
    message = (
        f"⚠️ Paused for your review: {project['title']} — {reason}. The team can't finish this "
        f"without your input; nothing was marked complete."
    )
    if feedback:
        message += f"\n\nWhat's needed:\n{feedback[:1500]}"
    message += (f"\n\nProvide what's needed (fix access, or paste the info here), then {resume} "
                f"to try again. {company_mode.render_money(state)}")
    await post_to_group(message, "manager")


def _decision_value(decision, name, default=None):
    if isinstance(decision, dict):
        return decision.get(name, default)
    return getattr(decision, name, default)


def _company_budget_snapshot():
    """Return Company Mode's ledger as the autonomous budget source of truth."""
    state = company_mode.load_state()
    company = state["company"]
    today = company.get("budget_date")
    estimated = any(
        entry.get("budget_date") == today and entry.get("cost_basis") == "estimated"
        for entry in state.get("cost_entries", [])
    )
    return {
        "budget_date": company.get("budget_date"),
        "budget_timezone": company_mode.budget_timezone_name(),
        "daily_budget_usd": company["daily_budget_usd"],
        "emergency_reserve_usd": company.get("emergency_reserve_usd", 0.0),
        "spent_today_usd": company.get("spent_today_usd", 0.0),
        "reserved_today_usd": company.get("reserved_today_usd", 0.0),
        "remaining_usd": company_mode.remaining_budget(state),
        "cost_is_estimated": estimated,
    }


def _company_task_route(task, *, previous_failures=0, previous_models=(), remaining_usd=None):
    owner = str(task.get("owner") or "manager")
    task_types = {
        "code": "coding",
        "research": "research",
        "write": "documentation",
        "editor": "review",
        "marketing": "documentation",
        "finance": "status_update",
        "analytics": "status_update",
        "task": "planning",
        "manager": "planning",
    }
    required = list(task.get("required_capabilities", []) or [])
    if owner == "editor" and "review" not in required:
        required.append("review")
    state = company_mode.load_state()
    envelope = (
        float(task.get("reserved_usd", task.get("estimate_usd", 0.0)) or 0.0)
        + company_mode.remaining_budget(state)
        if remaining_usd is None
        else max(0.0, float(remaining_usd))
    )
    return AUTONOMY_ROUTER.route(model_router.RoutingRequest(
        task_type=str(task.get("task_type") or task_types.get(owner, "planning")),
        complexity=str(task.get("complexity") or "standard"),
        risk=str(task.get("risk") or "low"),
        required_capabilities=tuple(required),
        estimated_input_tokens=int(task.get("estimated_input_tokens", 3000) or 3000),
        estimated_output_tokens=int(task.get("estimated_output_tokens", 800) or 800),
        remaining_budget_usd=envelope,
        previous_failures=max(0, int(previous_failures or 0)),
        previous_models=tuple(previous_models or ()),
    ))


def _task_allowed_tools(task, owner):
    if not task.get("enforce_authorization"):
        return None
    if owner and owner in main.SPECIALISTS:
        profile_names = main.SPECIALISTS[owner]["tool_names"]
    else:
        profile_names = [tool.get("name") for tool in main.TOOLS if tool.get("name")]
    return autonomy_team.allowed_tool_names(profile_names, task.get("authorization_level"))


def _answer_failure_classification(answer):
    """Recognize only explicit provider/tool failures, not ordinary critical prose."""
    text = str(answer or "").strip()
    lowered = text.lower()
    if lowered.startswith("blocked - needs human"):
        return "decision"
    hard_prefixes = (
        "sorry, something went wrong",
        "sorry, i couldn't",
        "tool error:",
        "openai_api_key is not set",
        "missing required environment variable",
    )
    if lowered.startswith(hard_prefixes):
        return company_mode.classify_failure(text)
    if "isn't configured yet" in lowered or "is not configured" in lowered:
        return "missing_access"
    return None


def _failure_action(classification):
    actions = {
        "missing_access": "Provide the named credential or access, then retry this task.",
        "missing_information": "Provide the missing information or clarify the acceptance criteria.",
        "unavailable_tool": "Configure the required integration/tool or approve a smaller scope.",
        "permission": "Grant only the required permission or choose a lower-impact alternative.",
        "budget": "Increase today's budget or defer the task to another day.",
        "decision": "Approve, reject, or provide the owner decision described above.",
        "no_progress": "Review the attempts and decide whether to rescope, accept, or stop.",
    }
    return actions.get(classification, "Inspect the run report, correct the failure, then retry.")


def _sink_spend_for_reconciliation(sink):
    """Use measured spend only when provider usage exists.

    Some API responses omit usage.  In that case ``None`` tells Company Mode to
    reconcile the conservative held estimate and label it estimated instead of
    incorrectly reopening the budget as a zero-cost call.
    """
    return sink["cost_usd"] if sink.get("usage_records") else None


async def _execute_routed_task(project, task, owner, prompt, sink):
    allowed_tools = _task_allowed_tools(task, owner)
    speaker_owner = owner or ("general" if task.get("owner") == "general" else "manager")
    model = str(task.get("model") or "").strip()
    model_reason = str(task.get("model_reason") or "").strip()
    if not model:
        decision = await asyncio.to_thread(_company_task_route, task)
        if _decision_value(decision, "deferred", False):
            reason = str(
                _decision_value(decision, "deferral_reason")
                or _decision_value(decision, "reason")
            )
            classification = "budget" if "budget" in reason else "no_progress"
            await asyncio.to_thread(
                company_mode.update_task_status,
                task["id"], "needs_human", reason, [], 0.0,
                company_mode.COMPANY_STATE_FILE,
                failure_classification=classification,
            )
            await post_to_group(f"Task {task['id']} deferred: {reason}", "manager")
            return "blocked"
        model = str(
            _decision_value(decision, "model_id") or _decision_value(decision, "model")
        )
        model_reason = str(
            _decision_value(decision, "reason", "Routed for this Company Mode task.")
        )

    max_attempts = company_mode.MAX_EXECUTION_ATTEMPTS
    attempts_already = int(task.get("execution_attempts", 0) or 0)
    attempts_remaining = max(0, max_attempts - attempts_already)
    previous_models = [
        str(value.get("model"))
        for value in task.get("attempt_history", [])
        if isinstance(value, dict) and value.get("model")
    ]
    if attempts_remaining == 0:
        reason = f"Execution attempt cap ({max_attempts}) was already reached."
        await asyncio.to_thread(
            company_mode.update_task_status,
            task["id"], "needs_human", reason, [], 0.0,
            company_mode.COMPANY_STATE_FILE,
            failure_classification="no_progress",
            model=model,
            model_reason=model_reason,
        )
        return "blocked"

    try:
        timeout_seconds = max(
            0.01, float(os.environ.get("AUTONOMY_TASK_TIMEOUT_SECONDS", "900"))
        )
    except (TypeError, ValueError):
        timeout_seconds = 900.0
    answer = ""
    failure = None
    _office_call("set_agent_status", "manager", "delegated", f"Assigned {task['owner']} a company task.")
    _office_call("set_agent_status", speaker_owner, "thinking", task["title"])
    _office_call("add_event", "delegated", "manager", f"Started company task: {task['owner']} - {task['title']}")
    await post_to_group(f"Starting: {task['owner']} - {task['title']} [{model}]", "manager")

    for attempt_index in range(attempts_remaining):
        time_limit_exceeded = False
        await asyncio.to_thread(
            company_mode.update_task_status,
            task["id"], "in_progress",
            path=company_mode.COMPANY_STATE_FILE,
            model=model,
            model_reason=model_reason,
        )

        def work():
            main.set_conversation("group")
            main.set_reply_context({"kind": "group"})
            main.set_execution_sink(sink)
            main.set_company_execution(not bool(task.get("enforce_authorization")))
            try:
                if owner:
                    return main.ask_specialist(
                        owner, prompt, record_history=False, model=model,
                        allowed_tool_names=allowed_tools,
                        include_memories=not bool(task.get("enforce_authorization")),
                    )
                return main.ask_ai(
                    prompt, record_history=False, model=model,
                    allowed_tool_names=allowed_tools,
                    include_memories=not bool(task.get("enforce_authorization")),
                )
            finally:
                main.set_company_execution(False)
                main.set_execution_sink(None)

        try:
            async with locks["manager"]:
                started = time.monotonic()
                worker_future = asyncio.create_task(asyncio.to_thread(work))
                try:
                    answer = await asyncio.shield(worker_future)
                except asyncio.CancelledError:
                    # A Python thread cannot be killed safely. Wait for it to finish so
                    # no model/tool work escapes the run ledger, then propagate cancel.
                    await worker_future
                    raise
                elapsed = time.monotonic() - started
            answer = str(autonomous_workflow.redact_secrets(str(answer or ""))).strip()
            failure = _answer_failure_classification(answer)
            if elapsed > timeout_seconds and not failure:
                time_limit_exceeded = True
                failure = "transient"
                answer = (
                    f"Task finished after {elapsed:.1f}s, beyond its {timeout_seconds:.1f}s "
                    "execution limit; no retry was started."
                )
        except Exception as exc:
            main.logger.error(f"Company task {task['id']} errored: {exc}")
            failure = company_mode.classify_failure(exc)
            answer = str(autonomous_workflow.redact_secrets(str(exc))) or "Unexpected execution error."

        if not failure:
            break

        previous_models.append(model)
        can_retry = (
            failure in {"technical", "transient"}
            and not time_limit_exceeded
            and attempt_index + 1 < attempts_remaining
        )
        if can_retry:
            remaining_reservation = max(
                0.0,
                float(task.get("estimate_usd", 0.0) or 0.0) - float(sink["cost_usd"]),
            )
            next_decision = await asyncio.to_thread(
                _company_task_route,
                task,
                previous_failures=len(previous_models),
                previous_models=tuple(previous_models),
                remaining_usd=remaining_reservation,
            )
            if not _decision_value(next_decision, "deferred", False):
                model = str(
                    _decision_value(next_decision, "model_id")
                    or _decision_value(next_decision, "model")
                )
                model_reason = str(_decision_value(next_decision, "reason"))
                await post_to_group(
                    f"Retrying {task['id']} with {model}: the prior {failure} attempt failed.",
                    "manager",
                )
                continue
            route_reason = str(
                _decision_value(next_decision, "deferral_reason")
                or _decision_value(next_decision, "reason")
            )
            failure = "budget" if "budget" in route_reason else "no_progress"
            answer = f"{answer} Stronger-model retry stopped: {route_reason}"
        break

    if failure:
        _office_call(
            "set_agent_status", speaker_owner, "error",
            "Company task stopped and needs owner attention.", OFFICE_ERROR_SECONDS,
        )
        await asyncio.to_thread(
            company_mode.update_task_status,
            task["id"], "needs_human", str(answer)[:1500], sink["artifacts"],
            _sink_spend_for_reconciliation(sink), company_mode.COMPANY_STATE_FILE,
            usage_records=sink["usage_records"],
            failure_classification=failure,
            model=model,
            model_reason=model_reason,
        )
        escalation = autonomous_workflow.format_escalation(
            project,
            task,
            f"Ran {len(previous_models) or 1} bounded execution attempt(s).",
            str(answer)[:1000],
            autonomy_team.workflow_failure(failure),
            _failure_action(failure),
            True,
        )
        await post_to_group(escalation, "manager")
        return "blocked"

    _office_call("set_agent_status", speaker_owner, "speaking", answer, OFFICE_REPLY_SECONDS)
    _office_call("add_event", "reply", speaker_owner, answer)
    await post_agent_answer_to_group(speaker_owner, answer)

    staged = main.pending_actions.get("group")
    if staged is not None:
        reason = f"Needs your approval: {main.describe_pending_action(staged)}. Reply /confirm to proceed."
        await asyncio.to_thread(
            company_mode.update_task_status,
            task["id"], "needs_human", reason, sink["artifacts"],
            _sink_spend_for_reconciliation(sink),
            company_mode.COMPANY_STATE_FILE,
            usage_records=sink["usage_records"],
            failure_classification="decision",
            model=model,
            model_reason=model_reason,
        )
        await post_to_group(reason, "manager")
        return "blocked"

    await asyncio.to_thread(
        company_mode.update_task_status,
        task["id"], "done", answer[:company_mode.MAX_TASK_RESULT_CHARS], sink["artifacts"],
        _sink_spend_for_reconciliation(sink),
        company_mode.COMPANY_STATE_FILE,
        usage_records=sink["usage_records"],
        model=model,
        model_reason=model_reason,
        feedback=answer if owner == "editor" else None,
    )
    if owner == "editor":
        await asyncio.to_thread(company_mode.set_project_revision_flag, project["id"], answer)
    state = await asyncio.to_thread(company_mode.load_state)
    await post_to_group(company_mode.render_money(state), "manager")
    return "done"


async def _run_one_task(project, task):
    """Execute one task with a routed model, bounded retries, and hard tool scope."""
    owner = task["owner"] if task["owner"] in main.SPECIALISTS else None
    context = f"Project {project['id']} / task {task['id']}"
    sink = {"cost_usd": 0.0, "artifacts": [], "usage_records": [], "context": context}

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
    return await _execute_routed_task(project, task, owner, prompt, sink)


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

            company = state["company"]
            ordinary_limit = max(
                0.0,
                float(company["daily_budget_usd"])
                - float(company.get("emergency_reserve_usd", 0.0)),
            )
            committed = (
                float(company.get("spent_today_usd", 0.0))
                + float(company.get("reserved_today_usd", 0.0))
            )
            # A task's existing reservation may consume the last available cent; that
            # is safe to execute because reconciliation replaces the hold. Stop only
            # when measured spend has pushed total commitments above the ordinary cap.
            if committed > ordinary_limit + 0.000001:
                await asyncio.to_thread(_defer_remaining, project_id)
                await post_to_group(
                    f"Daily budget overcommitted - stopping. {company_mode.render_money(state)}. "
                    "Raise /setbudget and /approve to continue.",
                    "manager",
                )
                return

            task = company_mode.next_planned_task(state, project_id)
            if task is None:
                verdict = project.get("editor_verdict")
                rounds = project.get("revision_round", 0)

                # The editor escalated (BLOCKED - needs human input), or we've revised too
                # many times without approval: STOP production and hand it to the user.
                # Do NOT mark it complete/Done - it's escalated, not finished.
                if verdict == "blocked" or (verdict == "revise" and rounds >= company_mode.MAX_REVISION_ROUNDS):
                    await _escalate_for_review(project, project_id, verdict, rounds)
                    return

                if project.get("needs_revision"):
                    created, note = await asyncio.to_thread(
                        company_mode.start_revision_round, project_id, BOT_KEYS
                    )
                    if created:
                        await post_to_group(f"{note} Continuing the work plan.", "manager")
                        continue  # loop picks up the freshly queued revision tasks
                    # Couldn't start another round (e.g. budget) and it's not approved ->
                    # escalate rather than silently marking it complete.
                    await _escalate_for_review(project, project_id, "revise", rounds, note=note)
                    return

                # Approved (or no editor task) -> complete and mark the issue Done.
                await asyncio.to_thread(_complete_project, project_id)
                await asyncio.to_thread(company_linear.finalize_source_issue, project_id)
                state = await asyncio.to_thread(company_mode.load_state)
                await post_to_group(
                    f"✅ Work plan complete for {project['title']}. {company_mode.render_money(state)}. "
                    "See /dailyreport for the deliverables.",
                    "manager",
                )
                return

            outcome = await _run_one_task(project, task)
            if outcome == "blocked":
                # Close the project and release every later task reservation. Leaving
                # an active project here would block unrelated roadmap work forever.
                await asyncio.to_thread(
                    company_mode.block_project, project_id, company_mode.COMPANY_STATE_FILE
                )
                return

    except asyncio.CancelledError:
        await asyncio.to_thread(
            company_mode.block_project,
            project_id,
            company_mode.COMPANY_STATE_FILE,
            reason="The Company plan runner was cancelled before completion.",
            failure_classification="technical",
        )
        raise
    except Exception as e:
        main.logger.error(f"Company plan runner crashed: {e}")
        await asyncio.to_thread(
            company_mode.block_project,
            project_id,
            company_mode.COMPANY_STATE_FILE,
            reason=f"The Company plan runner stopped unexpectedly: {e}",
            failure_classification="technical",
        )
        await post_to_group("The work plan hit an unexpected error and stopped. Check the logs.", "manager")


# --------------------------------------------------------------------------- #
# Autonomous daily-run bridge. The control plane stays synchronous and offline-
# testable; these callbacks hand paid/external-capable work back to this event loop,
# where the existing Company Mode engine and Telegram transport already live.
# --------------------------------------------------------------------------- #

def _autonomy_goal(item):
    lines = [str(item.get("title") or item.get("id") or "Complete roadmap item")]
    description = str(item.get("description") or "").strip()
    if description:
        lines.append(description)
    criteria = [str(value).strip() for value in item.get("acceptance_criteria", []) if str(value).strip()]
    if criteria:
        lines.append("Acceptance criteria:\n" + "\n".join(f"- {value}" for value in criteria))
    lines.append(
        "Allowlisted runtime autonomy configuration:\n"
        f"- schedule: {AUTONOMY_CONFIG.schedule_days} at {AUTONOMY_CONFIG.schedule_time}\n"
        f"- timezone: {AUTONOMY_CONFIG.timezone}\n"
        f"- configured dry_run: {AUTONOMY_CONFIG.dry_run}\n"
        f"- authorization ceiling: {AUTONOMY_CONFIG.max_authorization.value}\n"
        "Do not infer or disclose any unlisted environment value."
    )
    return "\n\n".join(lines)


async def _autonomy_runtime_deferral():
    """Return a no-spend deferral while supervised owner state has precedence."""
    if company_runner_task and not company_runner_task.done():
        return {
            "status": "deferred",
            "failure_classification": "decision_required",
            "reason": "A supervised Company Mode plan is already running.",
            "actual_cost_usd": 0.0,
            "model_invoked": False,
        }
    if main.pending_actions.get("group") is not None:
        return {
            "status": "deferred",
            "failure_classification": "decision_required",
            "reason": "A Telegram confirmation is already waiting for the owner.",
            "actual_cost_usd": 0.0,
            "model_invoked": False,
        }
    state = await asyncio.to_thread(company_mode.load_state)
    if state["company"]["mode"] == "paused":
        return {
            "status": "deferred",
            "failure_classification": "decision_required",
            "reason": "Company Mode is paused; use /resumecompany before a live autonomous run.",
            "actual_cost_usd": 0.0,
            "model_invoked": False,
        }
    open_projects = company_mode.open_projects(state)
    if open_projects:
        current = open_projects[0]
        return {
            "status": "deferred",
            "failure_classification": "decision_required",
            "reason": f"Company project {current['id']} is still {current['status']}.",
            "actual_cost_usd": 0.0,
            "model_invoked": False,
        }
    return None


async def _execute_autonomy_item(project, item, decision, run_id):
    """Create and run one review-gated Company Mode project for a roadmap item."""
    authorization = autonomy_team.normalize_authorization(item.get("authorization_level"))
    if authorization in {"modify_local", "external_action"}:
        label = "External actions" if authorization == "external_action" else "Local modification"
        return {
            "status": "needs_human",
            "failure_classification": "decision_required",
            "reason": (
                f"{label} requires an isolated executor or explicit owner approval and is not "
                "auto-executed by this vertical slice."
            ),
            "human_action": "Approve and supervise the action, or lower the item to observe/propose, then mark it ready.",
            "attempted": "Checked the roadmap authorization level; no model or tool was invoked.",
            "actual_cost_usd": 0.0,
            "model_invoked": False,
        }
    runtime_deferral = await _autonomy_runtime_deferral()
    if runtime_deferral:
        return runtime_deferral

    budget = _company_budget_snapshot()
    plan = autonomy_team.build_company_plan(
        item,
        decision,
        budget["remaining_usd"],
        router=AUTONOMY_ROUTER,
    )
    if plan["deferred"]:
        deferral_codes = {
            str(value.get("deferral_reason") or "")
            for value in plan.get("decisions", [])
            if isinstance(value, dict) and value.get("deferred")
        }
        if plan.get("deferral_reason"):
            deferral_codes.add(str(plan["deferral_reason"]))
        budget_only = bool(deferral_codes) and deferral_codes <= {"insufficient_budget"}
        return {
            "status": "deferred" if budget_only else "needs_human",
            "failure_classification": "budget_exhausted" if budget_only else "unavailable_tool",
            "reason": plan["reason"],
            "human_action": (
                "Increase today's budget or wait for the next budget day."
                if budget_only
                else "Review the model catalog and required review capabilities, then retry."
            ),
            "actual_cost_usd": 0.0,
            "model_invoked": False,
        }

    project_key = str(project.get("project_key") or project.get("id") or "").strip()
    profile, project_error, project_tokens = main.projects.begin_scoped_project(project_key)
    if project_error:
        return {
            "status": "needs_human",
            "failure_classification": "decision_required",
            "reason": project_error,
            "human_action": f"Add project key {project_key!r} to projects.json with its repo, then retry.",
            "attempted": "Tried to snapshot and activate the selected roadmap project's repository.",
            "actual_cost_usd": 0.0,
            "model_invoked": False,
        }

    suppression_token = _suppress_company_updates.set(True)
    company_project_id = None
    try:
        assignment = await asyncio.to_thread(
            company_mode.assign_goal,
            _autonomy_goal(item),
            BOT_KEYS,
            list(main.SPECIALISTS.keys()),
            company_mode.COMPANY_STATE_FILE,
            plan["tasks"],
            {
                "source": "autonomous_daily_run",
                "project_key": project_key,
                "roadmap_project_id": project.get("id"),
                "roadmap_item_id": item.get("id"),
                "autonomous_run_id": run_id,
                "authorization_level": item.get("authorization_level"),
                "acceptance_criteria": list(item.get("acceptance_criteria", []) or []),
            },
        )
        if assignment.startswith(("Blocked:", "Company Mode is paused", "Usage:")):
            classification = "budget_exhausted" if "budget" in assignment.lower() else "decision_required"
            return {
                "status": "deferred",
                "failure_classification": classification,
                "reason": assignment,
                "actual_cost_usd": 0.0,
                "model_invoked": False,
            }

        assigned_state = await asyncio.to_thread(company_mode.load_state)
        company_project = company_mode.active_project(assigned_state)
        if not company_project or company_project.get("autonomous_run_id") != run_id:
            raise RuntimeError("Autonomous assignment did not create the expected persisted project")
        company_project_id = company_project["id"]
        _message, approved_project_id = await asyncio.to_thread(
            company_mode.approve_project,
            company_mode.COMPANY_STATE_FILE,
            notify_hooks=False,
        )
        if not approved_project_id:
            raise RuntimeError("Autonomous Company Mode project could not be activated")
        company_project_id = approved_project_id

        await run_company_plan(company_project_id)
        final_state = await asyncio.to_thread(company_mode.load_state)
        return autonomy_team.aggregate_company_result(
            final_state,
            company_project_id,
            fallback_model=_decision_value(decision, "model_id") or _decision_value(decision, "model"),
        )
    except asyncio.CancelledError:
        if company_project_id:
            await asyncio.to_thread(
                company_mode.block_project,
                company_project_id,
                company_mode.COMPANY_STATE_FILE,
                reason="The autonomous run was cancelled before completion.",
                failure_classification="technical",
            )
        raise
    except Exception:
        if company_project_id:
            await asyncio.to_thread(
                company_mode.block_project,
                company_project_id,
                company_mode.COMPANY_STATE_FILE,
                reason="The autonomous run failed before it could persist a final outcome.",
                failure_classification="technical",
            )
        raise
    finally:
        _suppress_company_updates.reset(suppression_token)
        main.projects.end_scoped_project(project_tokens)


def _autonomy_executor_callback(project, item, decision, run_id):
    if main_loop is None:
        raise RuntimeError("Telegram event loop is unavailable for autonomous execution")
    future = asyncio.run_coroutine_threadsafe(
        _execute_autonomy_item(project, item, decision, run_id), main_loop
    )
    return future.result()


async def _generate_autonomy_ideas(state, limit):
    runtime_deferral = await _autonomy_runtime_deferral()
    if runtime_deferral:
        return {
            "ideas": [],
            "model": None,
            "model_reason": "Creative routing was skipped because supervised owner state has priority.",
            "estimated_cost_usd": 0.0,
            "actual_cost_usd": 0.0,
            "deferred": True,
            "deferral_reason": runtime_deferral["reason"],
        }
    budget = _company_budget_snapshot()
    decision = AUTONOMY_ROUTER.route(model_router.RoutingRequest(
        task_type="creative_ideation",
        complexity="standard",
        risk="low",
        required_capabilities=("text", "ideation"),
        estimated_input_tokens=2000,
        estimated_output_tokens=700,
        remaining_budget_usd=budget["remaining_usd"],
    ))
    if decision.deferred or not decision.model_id:
        return {
            "ideas": [],
            "model": decision.model_id,
            "model_reason": decision.reason,
            "estimated_cost_usd": decision.estimated_cost_usd,
            "deferred": True,
            "deferral_reason": decision.deferral_reason or "Creative work exceeds the ordinary remaining budget.",
        }
    try:
        ideas, receipt = await _run_metered(
            main.generate_controlled_ideas,
            autonomy_team.idea_project_context(state),
            limit,
            decision.model_id,
            estimate_usd=autonomy_team.reservation_estimate(decision),
            context="controlled idle ideation",
            agent="creative",
            meter_model=decision.model_id,
            project_id="idea_backlog",
            task_id="controlled-idle-ideation",
            return_receipt=True,
        )
        receipt = receipt or {}
        return {
            "ideas": ideas,
            "model": decision.model_id,
            "model_reason": decision.reason,
            "estimated_cost_usd": autonomy_team.reservation_estimate(decision),
            "actual_cost_usd": receipt.get("amount_usd", autonomy_team.reservation_estimate(decision)),
            "cost_is_estimated": receipt.get("cost_basis") != "actual",
            "token_usage": {
                "input_tokens": receipt.get("input_tokens", 0),
                "output_tokens": receipt.get("output_tokens", 0),
                "total_tokens": receipt.get("total_tokens", 0),
            },
            "agent": "creative",
            "project_id": "idea_backlog",
            "task_id": "controlled-idle-ideation",
        }
    except company_mode.BudgetExceededError:
        return {
            "ideas": [],
            "model": decision.model_id,
            "model_reason": decision.reason,
            "estimated_cost_usd": autonomy_team.reservation_estimate(decision),
            "deferred": True,
            "deferral_reason": "Creative work could not reserve the required ordinary budget.",
        }


def _autonomy_idea_callback(state, limit):
    if main_loop is None:
        raise RuntimeError("Telegram event loop is unavailable for controlled ideation")
    future = asyncio.run_coroutine_threadsafe(_generate_autonomy_ideas(state, limit), main_loop)
    return future.result()


def _get_autonomy_workflow():
    global _autonomy_workflow_instance
    if _autonomy_workflow_instance is None:
        _autonomy_workflow_instance = autonomous_workflow.AutonomousWorkflow(
            AUTONOMY_CONFIG,
            executor=_autonomy_executor_callback,
            idea_generator=_autonomy_idea_callback,
            budget_provider=_company_budget_snapshot,
            router=AUTONOMY_ROUTER,
        )
    return _autonomy_workflow_instance


async def _run_autonomy_cycle(trigger_source, *, dry_run=None):
    workflow = _get_autonomy_workflow()
    return await asyncio.to_thread(
        workflow.run,
        trigger_source=trigger_source,
        dry_run=dry_run,
    )


async def _run_and_post_autonomy(trigger_source, *, dry_run=None):
    global autonomy_runner_task
    current = asyncio.current_task()
    autonomy_runner_task = current
    try:
        report = await _run_autonomy_cycle(trigger_source, dry_run=dry_run)
        # A configured dry run performs no outbound action. A user-invoked dry run
        # is returned directly by its command handler instead of coming through here.
        if not report.get("dry_run"):
            for escalation in report.get("escalations", []) or []:
                await post_to_group(str(escalation), "manager")
            await post_to_group(report["telegram_summary"], "manager")
        else:
            main.logger.info(f"Autonomy dry run completed: {report.get('report_path')}")
        return report
    finally:
        if autonomy_runner_task is current:
            autonomy_runner_task = None


async def post_autonomous_daily():
    await _run_and_post_autonomy("scheduled", dry_run=None)


def _autonomy_status_text():
    workflow = _get_autonomy_workflow()
    state = workflow.load_state()
    recent = state.get("run_control", {}).get("recent_runs", [])
    last = recent[-1] if recent else None
    budget = _company_budget_snapshot()
    next_item = workflow.select_actionable_item(state)
    lines = [
        "Autonomous Team",
        f"Scheduler: {'enabled' if AUTONOMY_CONFIG.enabled else 'disabled'}; "
        f"{AUTONOMY_CONFIG.schedule_days} at {AUTONOMY_CONFIG.schedule_time} ({AUTONOMY_CONFIG.timezone})",
        f"Mode: {'dry-run' if AUTONOMY_CONFIG.dry_run else 'live'}; authorization ceiling {AUTONOMY_CONFIG.max_authorization.value}",
        f"Budget: ${budget['spent_today_usd']:.4f} spent, ${budget['reserved_today_usd']:.4f} reserved, "
        f"${budget['remaining_usd']:.4f} ordinary remaining of ${budget['daily_budget_usd']:.2f}",
        f"Next actionable item: {next_item.get('id')} - {next_item.get('title')}" if next_item else "Next actionable item: none",
        (
            f"Last run: {last.get('run_id')} - {last.get('final_status')} ({last.get('finished_at')})"
            if last else "Last run: none"
        ),
    ]
    return autonomous_workflow.redact_secrets("\n".join(lines))


async def handle_autorun_command(update, text, *, allow_live=True):
    """Handle safe status/dry-run commands and explicitly gated live execution."""
    global autonomy_runner_task
    argument = re.sub(r"^/autorun(?:@[A-Za-z0-9_]+)?", "", str(text or "").strip(), flags=re.I).strip().lower()
    if argument in {"", "dry", "dry-run", "plan"}:
        if autonomy_runner_task and not autonomy_runner_task.done():
            await update.message.reply_text("An autonomous run is already active; this trigger was not started.")
            return
        report = await _run_autonomy_cycle("telegram", dry_run=True)
        await reply_chunks(update.message, report["telegram_summary"] + f"\nReport: {report['report_path']}")
        return
    if argument == "status":
        await reply_chunks(update.message, await asyncio.to_thread(_autonomy_status_text))
        return
    if argument == "live":
        if not allow_live:
            await update.message.reply_text("Start live autonomous work from the group operating room, not a DM.")
            return
        if not AUTONOMY_CONFIG.enabled:
            await update.message.reply_text("Live autonomy is disabled. Set AUTONOMY_ENABLED=true, restart, and run a dry-run first.")
            return
        if AUTONOMY_CONFIG.dry_run:
            await update.message.reply_text("Live autonomy is still locked by AUTONOMY_DRY_RUN=true. Set it to false and restart only after reviewing a dry-run.")
            return
        if autonomy_runner_task and not autonomy_runner_task.done():
            await update.message.reply_text("An autonomous run is already active; this trigger was not started.")
            return
        autonomy_runner_task = asyncio.create_task(_run_and_post_autonomy("telegram", dry_run=False))
        await update.message.reply_text("Started one bounded autonomous run. Miles will post the final report or an exact owner action.")
        return
    await update.message.reply_text("Usage: /autorun dry-run | /autorun status | /autorun live")


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
    "6. Paste the product link back here, then use /link and /revenue to track it."
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
    prompt = _publish_prompt(slug, src_name, content)
    write_model = main.SPECIALISTS.get("write", {}).get("model") or main.FAST_MODEL

    await post_to_group(f"Prepping the publish package for '{project['title']}'...", "manager")

    def work():
        main.set_conversation("group")
        main.set_reply_context({"kind": "group"})
        main.set_company_execution(True)
        try:
            return main.ask_specialist("write", prompt, record_history=False, model=write_model)
        finally:
            main.set_company_execution(False)

    async with locks["manager"]:
        try:
            answer, receipt = await _run_metered(
                work,
                context=f"Publish prep for {project['id']}",
                agent="write",
                meter_model=write_model,
                project_id=project["id"],
                task_id="publish-prep",
                return_receipt=True,
            )
        except Exception as e:
            main.logger.error(f"Publish prep failed: {e}")
            await post_to_group("Publish prep hit an error - check the logs.", "manager")
            return

    answer = str(autonomous_workflow.redact_secrets(str(answer or "")))
    await post_agent_answer_to_group("write", answer)

    # Stage the gated publish approval in the group conversation.
    main.set_conversation("group")
    main.set_pending_action({
        "type": "publish",
        "project_id": project["id"],
        "title": project["title"],
        "company_context": f"Project {project['id']}",
    })
    files = ", ".join(receipt.get("artifacts", [])) or "(no new files detected - check the message above)"
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
    prompt = _launch_prompt(slug, project["title"], content)
    write_model = main.SPECIALISTS.get("write", {}).get("model") or main.FAST_MODEL

    await post_to_group(f"Drafting a launch kit for '{project['title']}'...", "manager")

    def work():
        main.set_conversation("group")
        main.set_reply_context({"kind": "group"})
        main.set_company_execution(True)
        try:
            return main.ask_specialist("write", prompt, record_history=False, model=write_model)
        finally:
            main.set_company_execution(False)

    async with locks["manager"]:
        try:
            answer, receipt = await _run_metered(
                work,
                context=f"Launch kit for {project['id']}",
                agent="write",
                meter_model=write_model,
                project_id=project["id"],
                task_id="launch-kit",
                return_receipt=True,
            )
        except Exception as e:
            main.logger.error(f"Launch kit failed: {e}")
            await post_to_group("Launch kit drafting hit an error - check the logs.", "manager")
            return

    answer = str(autonomous_workflow.redact_secrets(str(answer or "")))
    await post_agent_answer_to_group("write", answer)

    files = ", ".join(receipt.get("artifacts", [])) or "(see the message above)"
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
    if _suppress_company_updates.get():
        return
    bot = bots.get(bot_key, bots["manager"])
    try:
        await send_chunks(bot, GROUP_CHAT_ID, text)
    except Exception as e:
        main.logger.error(f"Failed to post scheduled message: {e}")


async def post_morning_briefing():
    # The briefing includes an LLM call, so reserve before it just like chat work.
    try:
        text = await _run_metered(
            main.build_morning_briefing,
            estimate_usd=0.15,
            context="scheduled morning briefing",
            agent="manager",
            meter_model=main.GENERAL_MODEL,
        )
    except company_mode.BudgetExceededError as exc:
        await post_to_group(f"Morning briefing deferred: {exc}", "manager")
        return
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
    autonomy_job = autonomous_workflow.register_scheduler(
        scheduler,
        post_autonomous_daily,
        AUTONOMY_CONFIG,
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
    if autonomy_job is not None:
        print(
            f"Autonomous run scheduled {AUTONOMY_CONFIG.schedule_days} at "
            f"{AUTONOMY_CONFIG.schedule_time} ({AUTONOMY_CONFIG.timezone}); "
            f"mode={'dry-run' if AUTONOMY_CONFIG.dry_run else 'live'}."
        )


async def run_all():
    global main_loop, office_api_server
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

    _office_call("configure_agents", _office_roster())
    main.on_delegation = on_delegation
    main.on_delegation_started = on_delegation_started
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

    office_api_token = os.environ.get("OFFICE_API_TOKEN")
    if office_api_token:
        try:
            office_api_server = office_api.start_server(office_api_token)
            host, port = office_api_server.server_address[:2]
            print(f"Virtual Office API listening on {host}:{port} (bearer token required).")
        except Exception as e:
            main.logger.error(f"Virtual Office API did not start: {e}")
    else:
        print("Virtual Office API disabled: set OFFICE_API_TOKEN to enable the Railway desktop connection.")

    print(f"All {len(BOT_KEYS)} bots running (polling). Press Ctrl+C to stop.")

    await start_scheduler()

    try:
        await asyncio.Event().wait()
    finally:
        if office_api_server is not None:
            office_api_server.shutdown()
            office_api_server.server_close()
            office_api_server = None
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        for app in applications.values():
            await app.updater.stop()
            await app.stop()
            await app.shutdown()


if __name__ == "__main__":
    asyncio.run(run_all())
