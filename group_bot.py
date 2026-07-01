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

main.WRITE_FILE_MODE = "requires_confirmation"

# Build-order staging: start with just the Manager + Weather to prove the
# mechanism (see the project plan), then expand as each stage is verified.
# Full set once all stages pass: ["manager", "code", "research", "write",
# "task", "tasks", "weather"].
BOT_KEYS = ["manager", "weather", "code", "research"]

SPECIALIST_KEYS = [key for key in BOT_KEYS if key != "manager"]

AGENT_INFO = {
    "manager": {
        "env_var": "TELEGRAM_MANAGER_BOT_TOKEN",
        "label": "Manager",
        "welcome": (
            "Hi, I'm the Manager. Message me (or just talk in the group) and "
            "I'll route your request to the right agent - or @mention an agent "
            "directly to skip me entirely. If anyone stages a file write, reply "
            "/confirm to me specifically to approve it, even if you were talking "
            "to another agent directly."
        ),
    },
    "code": {
        "env_var": "TELEGRAM_CODE_BOT_TOKEN",
        "label": "Patch (Coding Agent)",
        "welcome": "Patch here - the Coding Agent. @mention me with a coding task.",
    },
    "research": {
        "env_var": "TELEGRAM_RESEARCH_BOT_TOKEN",
        "label": "Scout (Researcher Agent)",
        "welcome": "Scout here - the Researcher Agent. @mention me with something to look up.",
    },
    "write": {
        "env_var": "TELEGRAM_WRITE_BOT_TOKEN",
        "label": "Quill (Writer Agent)",
        "welcome": "Quill here - the Writer Agent. @mention me with something to draft.",
    },
    "task": {
        "env_var": "TELEGRAM_TASK_BOT_TOKEN",
        "label": "Sage (Personal Assistant Agent)",
        "welcome": "Sage here - the Personal Assistant Agent. @mention me to remember something.",
    },
    "tasks": {
        "env_var": "TELEGRAM_TASKS_BOT_TOKEN",
        "label": "Roster (Tasks Agent)",
        "welcome": "Roster here - the Tasks Agent. @mention me to manage your real Todoist tasks.",
    },
    "weather": {
        "env_var": "TELEGRAM_WEATHER_BOT_TOKEN",
        "label": "Gale (Weather Agent)",
        "welcome": "Gale here - the Weather Agent. @mention me for the forecast.",
    },
}

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

    if main.pending_write is not None:
        async with locks["manager"]:
            pending = main.pending_write
            main.pending_write = None  # resolved either way - confirm or cancel

            if text.strip() == "/confirm":
                result = main.write_file(pending["filename"], pending["content"])
                main.logger.info(f"Telegram user {update.effective_user.id} confirmed write to {pending['filename']}")
                await update.message.reply_text(result)
            else:
                main.logger.info(f"Telegram user {update.effective_user.id} cancelled pending write to {pending['filename']}")
                await update.message.reply_text(f"Cancelled the write to files/{pending['filename']}.")
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

    try:
        await asyncio.Event().wait()
    finally:
        for app in applications.values():
            await app.updater.stop()
            await app.stop()
            await app.shutdown()


if __name__ == "__main__":
    asyncio.run(run_all())
