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


load_dotenv()

GROUP_CHAT_ID = int(os.environ["TELEGRAM_GROUP_CHAT_ID"])

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
# "task", "tasks", "weather", "calendar", "gmail"].
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


def is_real_human_message(update):
    user = update.effective_user

    if user is None or user.is_bot:
        return False  # never react to another bot - this is what prevents reply loops

    if update.effective_chat.id != GROUP_CHAT_ID:
        return False  # ignore private DMs to any bot entirely

    if user.id not in ALLOWED_USER_IDS:
        main.logger.warning(f"Rejected group message from unauthorized Telegram user {user.id}")
        return False

    return True


def build_specialist_handler(key):
    async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_real_human_message(update):
            return

        text = update.message.text
        mention = f"@{bot_usernames[key]}"
        main.logger.info(f"[{key}] handler received: {text!r} (looking for {mention!r})")

        if mention.lower() not in text.lower():
            return  # not addressed to this bot

        main.logger.info(f"[{key}] mention matched - processing")

        stripped = re.sub(re.escape(mention), "", text, count=1, flags=re.IGNORECASE).strip()

        if stripped == "" or stripped == "/start":
            await update.message.reply_text(AGENT_INFO[key]["welcome"])
            return

        try:
            async with locks[key]:
                # record_history defaults to True - this conversation gets
                # logged to conversation_history/long-term memory exactly like
                # /code etc. already do, even though the Manager wasn't involved.
                answer = await asyncio.to_thread(main.ask_specialist, key, stripped)
        except Exception as e:
            main.logger.error(f"Unhandled error in {key} specialist handler: {e}")
            await update.message.reply_text("Sorry, something went wrong processing that.")
            return

        # The Responses API can occasionally return an empty output_text (e.g.
        # if a turn ends with no text after its last tool call) - Telegram's
        # API rejects an empty message outright, so guard against sending one.
        await update.message.reply_text(answer if answer.strip() else "(no response)")

    return handle


async def handle_manager_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_real_human_message(update):
        return

    text = update.message.text
    lowered = text.lower()

    for key in SPECIALIST_KEYS:
        if f"@{bot_usernames[key]}".lower() in lowered:
            return  # addressed to a specific specialist - their own handler owns this

    if text.strip() in ("/start", f"@{bot_usernames['manager']}", f"@{bot_usernames['manager']} /start"):
        await update.message.reply_text(AGENT_INFO["manager"]["welcome"])
        return

    if main.pending_action is not None:
        async with locks["manager"]:
            pending = main.pending_action
            main.pending_action = None  # resolved either way - confirm or cancel
            description = main.describe_pending_action(pending)

            if text.strip() == "/confirm":
                # confirm_pending_action can do blocking I/O (Gmail send) - run it
                # off the event loop so polling isn't stalled.
                result = await asyncio.to_thread(main.confirm_pending_action, pending)
                main.logger.info(f"Telegram user {update.effective_user.id} confirmed {description}")
                await update.message.reply_text(result)
            else:
                main.logger.info(f"Telegram user {update.effective_user.id} cancelled {description}")
                await update.message.reply_text(f"Cancelled the {description}.")
        return

    try:
        async with locks["manager"]:
            answer = await asyncio.to_thread(main.ask_manager, text)
    except Exception as e:
        main.logger.error(f"Unhandled error in manager handler: {e}")
        await update.message.reply_text("Sorry, something went wrong processing that.")
        return

    # See the matching comment in build_specialist_handler - guard against
    # Telegram rejecting an empty message.
    await update.message.reply_text(answer if answer.strip() else "(no response)")


def on_delegation(specialist_key, request_text, answer_text):
    """Posts the delegation + the specialist's answer to the group as that
    specialist's own bot, for visibility. Called from execute_tool (main.py),
    which runs on a worker thread (via asyncio.to_thread in the handlers
    above) - so this hands the actual Telegram calls back to the event loop
    thread safely via run_coroutine_threadsafe rather than awaiting directly."""
    label = "General Assistant" if specialist_key == "general" else main.SPECIALISTS[specialist_key]["label"]
    target_bot = bots.get(specialist_key, bots["manager"])

    async def post():
        try:
            if specialist_key != "general":
                await bots["manager"].send_message(GROUP_CHAT_ID, f"Delegating to the {label}: {request_text}")
            await target_bot.send_message(GROUP_CHAT_ID, answer_text)
        except Exception as e:
            main.logger.error(f"Failed to post delegation visibility message: {e}")

    asyncio.run_coroutine_threadsafe(post(), main_loop)


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
        await bot.send_message(GROUP_CHAT_ID, text if text.strip() else "(no response)")
    except Exception as e:
        main.logger.error(f"Failed to post scheduled message: {e}")


async def post_morning_briefing():
    # build_morning_briefing does blocking work (LLM + HTTP) - run it off the loop.
    text = await asyncio.to_thread(main.build_morning_briefing)
    await post_to_group(text, "manager")
    # Fresh day - (re)schedule today's event alerts right after the briefing.
    await schedule_todays_event_alerts()


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
    scheduler.start()

    # Live reminder scheduling comes through this hook from execute_tool/set_reminder.
    main.on_reminder_set = schedule_reminder

    await reload_reminders()
    await schedule_todays_event_alerts()
    print(f"Scheduler running - morning briefing at {main.BRIEFING_TIME} ({main.TIMEZONE}).")


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
