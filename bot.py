"""
Telegram bot interface for the assistant - lets you message it from your phone
instead of running main.py in a terminal. Reuses all the existing logic from
main.py (ask_manager, the specialists, every tool) without duplicating it:

- handle_command() in main.py already does all the command parsing and prints
  the result, exactly like the CLI. This file captures that printed output
  (via contextlib.redirect_stdout) and relays it back as a Telegram message,
  instead of rewriting every function to return text.
- Sensitive actions (writing a file, sending an email) normally confirm via
  input() y/n, which has no real terminal here - so this interface uses
  main.CONFIRMATION_MODE = "requires_confirmation" instead: the action gets staged
  (per-chat via main.get_pending_action()) rather than performed immediately, and the
  user confirms it with a follow-up "/confirm" message, intercepted below before
  normal command handling.
- A single asyncio.Lock() serializes message handling, so two messages never
  redirect stdout at the same time. Fine for a personal, single-user bot.
"""
import asyncio
import contextlib
import io
import os

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

import main


load_dotenv()

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

ALLOWED_USER_IDS = {
    int(user_id.strip())
    for user_id in os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
    if user_id.strip()
}

if not ALLOWED_USER_IDS:
    # Don't refuse to start outright - that would make it impossible to ever learn
    # your own Telegram user ID to put in the allowlist in the first place. Instead,
    # start normally: with an empty allowlist, every sender (including you) hits the
    # "not in ALLOWED_USER_IDS" branch below, which safely just replies with their
    # ID and does nothing else - no AI calls, no tools, no cost exposure.
    print(
        "WARNING: TELEGRAM_ALLOWED_USER_IDS is not set. The bot will start, but will "
        "only reply with the sender's Telegram user ID until you set it and restart."
    )

# This interface has no real terminal, so the input()-based y/n confirmation for
# sensitive actions can't work here - see CONFIRMATION_MODE in main.py.
main.CONFIRMATION_MODE = "requires_confirmation"

WELCOME_MESSAGE = (
    "Hi! I'm Tyler's personal AI assistant, running on Telegram.\n\n"
    "Just message me normally and I'll route your request to the right "
    "specialist, or use the same commands as the CLI version: /help, /code, "
    "/research, /write, /task, /remember, /recall, /search, /read, /askfile, "
    "/history, /clear.\n\n"
    "Note: if I want to write a file, I'll ask you to reply /confirm first."
)

TELEGRAM_MESSAGE_LIMIT = 4000

processing_lock = asyncio.Lock()


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text = update.message.text

    if user_id not in ALLOWED_USER_IDS:
        main.logger.warning(f"Rejected message from unauthorized Telegram user {user_id}")
        await update.message.reply_text(
            f"Sorry, this bot is private. Your Telegram user ID is {user_id} - "
            "ask the owner to add it to TELEGRAM_ALLOWED_USER_IDS if this should be allowed."
        )
        return

    main.set_conversation(f"telegram:{chat_id}:{user_id}")

    if text.strip() == "/start":
        await update.message.reply_text(WELCOME_MESSAGE)
        return

    if text.strip().lower() in ("quit", "/quit"):
        await update.message.reply_text("This bot runs continuously - there's no need to quit, I'm always here.")
        return

    if main.get_pending_action() is not None:
        async with processing_lock:
            pending = main.get_pending_action()
            main.clear_pending_action()  # resolved either way - confirm or cancel
            description = main.describe_pending_action(pending)

            if text.strip() == "/confirm":
                result = main.confirm_pending_action(pending)
                main.logger.info(f"Telegram user {user_id} confirmed {description}")
                await update.message.reply_text(result)
            else:
                main.logger.info(f"Telegram user {user_id} cancelled {description}")
                await update.message.reply_text(f"Cancelled the {description}.")
        return

    try:
        async with processing_lock:
            output = io.StringIO()

            def run():
                with contextlib.redirect_stdout(output):
                    main.handle_command(text)

            await asyncio.to_thread(run)
            response_text = output.getvalue().strip()

    except Exception as e:
        main.logger.error(f"Unhandled error processing Telegram message: {e}")
        await update.message.reply_text("Sorry, something went wrong processing that.")
        return

    if response_text == "":
        response_text = "(no output)"

    for start in range(0, len(response_text), TELEGRAM_MESSAGE_LIMIT):
        await update.message.reply_text(response_text[start:start + TELEGRAM_MESSAGE_LIMIT])


def build_app():
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT, handle_message))
    return application


if __name__ == "__main__":
    app = build_app()
    print("Bot is running (polling). Press Ctrl+C to stop.")
    app.run_polling()
