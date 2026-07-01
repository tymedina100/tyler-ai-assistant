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


load_dotenv()

GROUP_CHAT_ID = int(os.environ["TELEGRAM_GROUP_CHAT_ID"])
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
# here. Full set once every bot exists: ["manager", "code", "research", "news",
# "write", "task", "tasks", "weather", "calendar", "gmail"].
BOT_KEYS = ["manager", "weather", "code", "research"]

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
    "research": {"env_var": "TELEGRAM_RESEARCH_BOT_TOKEN", "tagline": "@mention me with something to look up."},
    "news": {"env_var": "TELEGRAM_NEWS_BOT_TOKEN", "tagline": "@mention me for headline roundups or source-cited news briefs."},
    "write": {"env_var": "TELEGRAM_WRITE_BOT_TOKEN", "tagline": "@mention me with something to draft."},
    "task": {"env_var": "TELEGRAM_TASK_BOT_TOKEN", "tagline": "@mention me to remember something."},
    "tasks": {"env_var": "TELEGRAM_TASKS_BOT_TOKEN", "tagline": "@mention me to manage your real Todoist tasks."},
    "weather": {"env_var": "TELEGRAM_WEATHER_BOT_TOKEN", "tagline": "@mention me for the forecast."},
    "calendar": {"env_var": "TELEGRAM_CALENDAR_BOT_TOKEN", "tagline": "@mention me about your calendar or to set a reminder."},
    "gmail": {"env_var": "TELEGRAM_GMAIL_BOT_TOKEN", "tagline": "@mention me to check or send email."},
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

                answer = await asyncio.to_thread(main.ask_specialist, key, request)
        except Exception as e:
            main.logger.error(f"Unhandled error in {key} specialist handler: {e}")
            await update.message.reply_text("Sorry, something went wrong processing that.")
            return

        # Split into <=4096-char chunks (and guard against an empty answer, which
        # Telegram also rejects) - see reply_chunks / TELEGRAM_LIMIT above.
        await reply_chunks(update.message, answer)

    return handle


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
                answer = await asyncio.to_thread(main.ask_manager, text)
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
                    answer = await asyncio.to_thread(main.ask_ai, text)
                else:
                    answer = await asyncio.to_thread(main.ask_specialist, key, text)
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

        company_command = company_mode.parse_company_command(text)
        if company_command is not None:
            if company_command[0] in {"/setbudget", "/assign", "/pausecompany", "/resumecompany"}:
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
            answer = await asyncio.to_thread(main.ask_manager, text)
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
        token = os.environ[AGENT_INFO[key]["env_var"]]
        app = ApplicationBuilder().token(token).build()
        applications[key] = app
        bots[key] = app.bot
        bot_usernames[key] = (await app.bot.get_me()).username
        print(f"{AGENT_INFO[key]['label']}: @{bot_usernames[key]}")

    main.on_delegation = on_delegation

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
