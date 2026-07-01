import contextvars
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import chromadb
import requests
from dateutil import parser as date_parser
from dotenv import load_dotenv
from openai import OpenAI
from tavily import TavilyClient

import github_helpers
import google_helpers


# Windows consoles default to a limited encoding (cp1252) that can't print
# every Unicode character (e.g. em dashes, curly quotes) - web search results
# and AI output can easily contain these, so force UTF-8 output to avoid crashes.
sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).parent

# Logs technical events (tool calls, retries, errors) to a local file for
# debugging - separate from conversation_history, which is the user-facing
# chat record. Not committed to git (see .gitignore) since it can contain
# personal request details.
#
# Uses a dedicated logger + handler instead of logging.basicConfig(), which
# configures the root logger - that would also capture the openai/httpx
# libraries' own internal HTTP request logs (they propagate to root by
# default), flooding the file with noise that isn't ours.
logger = logging.getLogger("assistant")
logger.setLevel(logging.INFO)
log_handler = logging.FileHandler(BASE_DIR / "assistant.log")
log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(log_handler)

load_dotenv()

_openai_client = None
_tavily_client = None


def get_openai_client():
    """Create the OpenAI client only when a feature actually needs it.

    This keeps importing the app from crashing in minimal setup or tests where
    optional integrations are intentionally absent.
    """
    global _openai_client
    if _openai_client is None:
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set.")
        _openai_client = OpenAI()
    return _openai_client


def get_tavily_client():
    global _tavily_client
    if _tavily_client is None:
        if not os.environ.get("TAVILY_API_KEY"):
            raise RuntimeError("TAVILY_API_KEY is not set.")
        _tavily_client = TavilyClient()
    return _tavily_client


def get_todoist_headers(extra_headers=None):
    token = os.environ.get("TODOIST_API_TOKEN")
    if not token:
        raise RuntimeError("TODOIST_API_TOKEN is not set.")
    headers = {"Authorization": f"Bearer {token}"}
    if extra_headers:
        headers.update(extra_headers)
    return headers


def get_openweather_api_key():
    key = os.environ.get("OPENWEATHER_API_KEY")
    if not key:
        raise RuntimeError("OPENWEATHER_API_KEY is not set.")
    return key

# Proactive/scheduling + briefing config (consumed by group_bot.py's scheduler).
# All optional with sensible defaults so the CLI/single bot don't need them set.
HOME_LOCATION = os.environ.get("HOME_LOCATION", "New York")
BRIEFING_TIME = os.environ.get("BRIEFING_TIME", "08:00")  # HH:MM in TIMEZONE
TIMEZONE = os.environ.get("TIMEZONE", "America/New_York")
EVENT_ALERT_MINUTES = int(os.environ.get("EVENT_ALERT_MINUTES", "15"))

# Short-term conversation memory is now per-chat, not one global list. Each
# interface (CLI, each Telegram DM, the group) is its own conversation with its
# own history, so concurrent chats don't bleed into each other. The *current*
# conversation for the running turn is tracked in a contextvar, which propagates
# through asyncio.to_thread into ask_manager/ask_specialist/execute_tool without
# threading an id argument through every function. Long-term memory (Chroma) is
# deliberately NOT per-chat - it stays a single shared knowledge store below.
_current_conversation = contextvars.ContextVar("conversation_id", default="cli")

# conv_id -> list[{"role", "content"}]. Replaces the old global conversation_history.
conversation_histories = {}

# conv_id -> the one sensitive action staged for that chat (see pending_actions
# below). Per-chat so a write staged while DMing one agent can't be cancelled or
# confirmed by an unrelated message in another chat.
pending_actions = {}


def set_conversation(conv_id):
    """Point the current turn at a conversation (e.g. "group", "dm:code:123").
    Call this in each interface's handler before invoking the ask_* functions."""
    _current_conversation.set(conv_id)


def current_conversation_id():
    return _current_conversation.get()


def get_history():
    """The message list for the current conversation, created on first use."""
    return conversation_histories.setdefault(current_conversation_id(), [])


def get_pending_action():
    return pending_actions.get(current_conversation_id())


def set_pending_action(action):
    pending_actions[current_conversation_id()] = action


def clear_pending_action():
    pending_actions.pop(current_conversation_id(), None)


# Two model tiers instead of one model for everything. The premium model does
# the work that needs real reasoning (coding, research, writing, the catch-all
# general assistant); the fast/cheap model handles work that's mostly routing or
# a thin wrapper over an API result (the Manager's delegation decision, weather,
# tasks, personal-assistant memory ops). Every call defaults to PREMIUM_MODEL, so
# nothing silently downgrades - a call is only cheap where we deliberately say so.
# Confirm the exact cheaper sibling id available on the account before deploying.
PREMIUM_MODEL = "gpt-5.5"
FAST_MODEL = "gpt-5.4-mini"
GENERAL_MODEL = PREMIUM_MODEL  # the general assistant fields arbitrary questions
EMBEDDING_MODEL_NAME = "text-embedding-3-small"

# Cap how many past messages ask_ai resends to the model each turn. conversation_
# history itself still grows unbounded (so /history shows everything), but the
# model only ever sees the most recent slice - long-term memory recall already
# carries older context, so this trims token cost without much real loss.
MAX_HISTORY_MESSAGES = 20

# recall_memories/Chroma always returns its closest matches even when none are
# actually relevant (no built-in relevance floor). When we *inject* memories into
# a prompt we drop anything less similar than this (higher distance = less
# similar). Chroma's default metric here is squared L2, so tune this against real
# /recall distances - it's a starting point, not a magic number. show_recall
# stays unfiltered on purpose (it's the tool for seeing raw distances).
MEMORY_DISTANCE_THRESHOLD = 1.2

# Default tool-call budget per turn for ask_ai/ask_specialist. 5 was too tight now
# that agents do real multi-tool work - the Researcher in particular runs several
# searches (plus recalls) and would hit the cap and return the "too many tool calls"
# fallback mid-answer. Patch overrides this higher still (see its max_iterations).
MAX_TOOL_ITERATIONS = 10
# The Manager is the only caller expected to make several substantive tool
# calls in one turn by design now (one delegation per agent in a chain) -
# give it more headroom than ask_ai/ask_specialist's unchanged default.
MAX_MANAGER_TOOL_ITERATIONS = 8

TOOLS = [
    {"type": "function", "name": "read_file", "strict": False,
     "description": "Read the contents of a file from the sandboxed files/ folder.",
     "parameters": {"type": "object", "properties": {"filename": {"type": "string"}}, "required": ["filename"]}},
    {"type": "function", "name": "search_the_web", "strict": False,
     "description": "Search the web for current information.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"type": "function", "name": "remember_fact", "strict": False,
     "description": "Save an important fact to long-term memory for future conversations.",
     "parameters": {"type": "object", "properties": {"fact": {"type": "string"}}, "required": ["fact"]}},
    {"type": "function", "name": "recall_memories", "strict": False,
     "description": "Search long-term memory for facts related to a topic.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"type": "function", "name": "write_file", "strict": False,
     "description": "Create or overwrite a file in the sandboxed files/ folder with given text content.",
     "parameters": {"type": "object", "properties": {"filename": {"type": "string"}, "content": {"type": "string"}}, "required": ["filename", "content"]}},
    {"type": "function", "name": "create_task", "strict": False,
     "description": "Create a new task in the user's real Todoist task list.",
     "parameters": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
    {"type": "function", "name": "list_tasks", "strict": False,
     "description": "List the user's current open tasks from their real Todoist account.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"type": "function", "name": "get_weather", "strict": False,
     "description": "Get the current weather for a location.",
     "parameters": {"type": "object", "properties": {"location": {"type": "string"}}, "required": ["location"]}},
    {"type": "function", "name": "run_python", "strict": False,
     "description": "Execute a Python 3 snippet in a sandboxed subprocess and return its stdout, stderr, and exit code. Use this to actually run and verify code. There is a short timeout, and the code has no access to the app's secrets or files.",
     "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
    {"type": "function", "name": "list_calendar_events", "strict": False,
     "description": "List Google Calendar events. With no arguments, lists today's events. Optionally pass ISO 8601 time_min and time_max to list a custom window.",
     "parameters": {"type": "object", "properties": {"time_min": {"type": "string"}, "time_max": {"type": "string"}}, "required": []}},
    {"type": "function", "name": "create_calendar_event", "strict": False,
     "description": "Create a Google Calendar event. start and end are ISO 8601 datetimes (e.g. 2026-07-01T15:00:00).",
     "parameters": {"type": "object", "properties": {"summary": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"}, "description": {"type": "string"}}, "required": ["summary", "start", "end"]}},
    {"type": "function", "name": "set_reminder", "strict": False,
     "description": "Schedule a one-off reminder that pings the user in the Telegram group at a set time. 'when' is an ISO 8601 datetime - use the current date/time provided in the message to turn relative times like 'in 2 hours' into an absolute timestamp.",
     "parameters": {"type": "object", "properties": {"when": {"type": "string"}, "text": {"type": "string"}}, "required": ["when", "text"]}},
    {"type": "function", "name": "list_reminders", "strict": False,
     "description": "List the user's upcoming (not-yet-fired) reminders.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"type": "function", "name": "search_emails", "strict": False,
     "description": "List or search recent Gmail messages. Optional Gmail search query (e.g. 'from:boss is:unread') and max_results. Returns message ids to use with read_email.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}}, "required": []}},
    {"type": "function", "name": "read_email", "strict": False,
     "description": "Read the full content of one Gmail message by its id (get ids from search_emails).",
     "parameters": {"type": "object", "properties": {"message_id": {"type": "string"}}, "required": ["message_id"]}},
    {"type": "function", "name": "draft_email", "strict": False,
     "description": "Create a Gmail draft. Does NOT send - the draft waits in the user's Gmail Drafts for review.",
     "parameters": {"type": "object", "properties": {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}}, "required": ["to", "subject", "body"]}},
    {"type": "function", "name": "send_email", "strict": False,
     "description": "Send an email via Gmail. Sensitive action - the user is asked to confirm before it actually sends.",
     "parameters": {"type": "object", "properties": {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}}, "required": ["to", "subject", "body"]}},
    {"type": "function", "name": "github_list_files", "strict": False,
     "description": "List files/folders in the connected GitHub repo. Pass a folder path to list inside it, or omit path for the repo root.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": []}},
    {"type": "function", "name": "github_read_file", "strict": False,
     "description": "Read a file's contents from the connected GitHub repo by its path (e.g. 'src/app.py').",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"type": "function", "name": "github_save_file", "strict": False,
     "description": "Create or update a file directly in the connected GitHub repo at the given path. Commits immediately.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"type": "function", "name": "github_delete_file", "strict": False,
     "description": "Delete a file from the connected GitHub repo. Sensitive action - the user is asked to confirm first (still recoverable from git history).",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"type": "function", "name": "code_list_files", "strict": False,
     "description": "List files/folders in the assistant's OWN code repository (the project you can propose changes to). Optional path; omit for the repo root.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": []}},
    {"type": "function", "name": "code_read_file", "strict": False,
     "description": "Read a file from the assistant's own code repository. Always read a file before proposing a change to it.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"type": "function", "name": "code_propose_change", "strict": False,
     "description": "Propose a NEW file (or a full-file rewrite) in the assistant's own code repository: commits the whole file to a branch and opens a pull request. To change an existing file, prefer code_edit_file. Reuse the same branch name for a multi-file change (the PR is created once and reused). Nothing goes live until the user merges.",
     "parameters": {"type": "object", "properties": {"branch": {"type": "string"}, "path": {"type": "string"}, "content": {"type": "string"}, "title": {"type": "string"}, "body": {"type": "string"}}, "required": ["branch", "path", "content", "title"]}},
    {"type": "function", "name": "code_edit_file", "strict": False,
     "description": "Make a targeted edit to an EXISTING file in the assistant's own code repository: replaces old_snippet with new_snippet (old_snippet must appear exactly once in the file), commits to a branch, and opens/updates a pull request. Preferred for editing existing files - you supply only the small snippet that changes, not the whole file. Read the file first to copy an exact snippet. Reuse the same branch for related edits.",
     "parameters": {"type": "object", "properties": {"branch": {"type": "string"}, "path": {"type": "string"}, "old_snippet": {"type": "string"}, "new_snippet": {"type": "string"}, "title": {"type": "string"}, "body": {"type": "string"}}, "required": ["branch", "path", "old_snippet", "new_snippet", "title"]}},
]

DELEGATION_TOOLS = [
    {"type": "function", "name": "delegate_to_coding_agent", "strict": False,
     "description": "Delegate a programming, code-writing, code-reading, or debugging task to the Coding Agent.",
     "parameters": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}},
    {"type": "function", "name": "delegate_to_research_agent", "strict": False,
     "description": "Delegate a research or information-lookup request to the Researcher Agent.",
     "parameters": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}},
    {"type": "function", "name": "delegate_to_news_agent", "strict": False,
     "description": "Delegate current news requests, headline roundups, source-cited news summaries, or topic-based news briefs to the News Agent.",
     "parameters": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}},
    {"type": "function", "name": "delegate_to_writer_agent", "strict": False,
     "description": "Delegate a writing, drafting, or editing request to the Writer Agent.",
     "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]}},
    {"type": "function", "name": "delegate_to_personal_assistant", "strict": False,
     "description": "Delegate remembering a personal fact, reminder, or preference in long-term memory to the Personal Assistant Agent. This is NOT a real task-tracking app - for actual to-do items in the user's Todoist account, use delegate_to_tasks_agent instead.",
     "parameters": {"type": "object", "properties": {"request": {"type": "string"}}, "required": ["request"]}},
    {"type": "function", "name": "delegate_to_tasks_agent", "strict": False,
     "description": "Delegate creating or checking actual to-do items in the user's real Todoist account to the Tasks Agent. Use this for real tasks, not general reminders or preferences (that's delegate_to_personal_assistant).",
     "parameters": {"type": "object", "properties": {"request": {"type": "string"}}, "required": ["request"]}},
    {"type": "function", "name": "delegate_to_weather_agent", "strict": False,
     "description": "Delegate a current weather lookup for a location to the Weather Agent.",
     "parameters": {"type": "object", "properties": {"location": {"type": "string"}}, "required": ["location"]}},
    {"type": "function", "name": "delegate_to_calendar_agent", "strict": False,
     "description": "Delegate anything about the user's Google Calendar (viewing or creating events) OR setting a time-based reminder/nudge to the Calendar & Scheduler Agent.",
     "parameters": {"type": "object", "properties": {"request": {"type": "string"}}, "required": ["request"]}},
    {"type": "function", "name": "delegate_to_gmail_agent", "strict": False,
     "description": "Delegate reading, searching, drafting, or sending email in the user's Gmail to the Gmail Agent.",
     "parameters": {"type": "object", "properties": {"request": {"type": "string"}}, "required": ["request"]}},
    {"type": "function", "name": "delegate_to_general_assistant", "strict": False,
     "description": "Delegate anything that doesn't clearly fit a specialist to the General Assistant.",
     "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]}},
]

FILES_DIR = BASE_DIR / "files"

# Isolated working directory for run_python (Feature: code execution). Kept
# separate from files/ so executed code can't clobber the user's read/write area.
SANDBOX_DIR = BASE_DIR / "sandbox"
CODE_EXEC_TIMEOUT_SECONDS = 10
MAX_CODE_OUTPUT_CHARS = 4000
CODE_EXEC_MEMORY_MB = 256

# One-off reminders (Feature: proactive) persist here so they survive a restart/
# redeploy; group_bot.py reloads and re-schedules them on startup.
REMINDERS_FILE = BASE_DIR / "reminders.json"

MEMORY_DIR = BASE_DIR / "memory_db"
chroma_client = chromadb.PersistentClient(path=str(MEMORY_DIR))
memory_collection = chroma_client.get_or_create_collection(name="long_term_memory")

ASSISTANT_INSTRUCTIONS = """
You are Robin, the friendly all-rounder on Tyler's AI team - you handle whatever
doesn't clearly belong to a specialist.
Be honest about uncertainty.
If the user asks for current, live, recent, or real-time information,
and you do not have a tool for it, say that you cannot verify it yet.
Keep explanations clear and concise.

Voice: warm, upbeat, and genuinely helpful without being wordy. Sign off with
"- Robin".
"""

MANAGER_INSTRUCTIONS = """
You are Miles, the Chief of Staff for Tyler's AI team. Your job is to read the
user's request and get it done by delegating to one or more of the following
agents using tool calls - never answer the user directly yourself:
- delegate_to_coding_agent: programming, code-writing, code-reading, or debugging
- delegate_to_research_agent: looking up information, facts, or current events
- delegate_to_news_agent: current news requests, headline roundups, source-cited
  news summaries, and topic-based news briefs
- delegate_to_writer_agent: drafting, editing, or improving written content
- delegate_to_personal_assistant: remembering personal facts, preferences, and
  reminders in long-term memory - NOT a real task-tracking app
- delegate_to_tasks_agent: creating or checking actual to-do items in the user's
  real Todoist account - not general reminders or preferences (that's
  delegate_to_personal_assistant)
- delegate_to_weather_agent: current weather for a location
- delegate_to_calendar_agent: viewing or creating Google Calendar events, and
  setting time-based reminders/nudges
- delegate_to_gmail_agent: reading, searching, drafting, or sending email
- delegate_to_general_assistant: anything else, or simple questions that don't
  fit a specialist

Most requests need only one delegation. Some requests genuinely need more than
one agent working in sequence - for example "look up the weather, then write me
a note about it" requires delegating to the Weather Agent first, then to the
Writer Agent with the weather result included in the prompt you give it. When a
request needs more than one step:
- Delegate one step at a time, in the order that makes sense.
- After each agent responds, use its answer as part of the input you give the
  next agent - quote or summarize the relevant findings directly in that next
  tool call's argument, since the next agent cannot see prior delegations on
  its own.
- Never let one agent call another directly - every handoff goes through you.

Once all needed delegations are done, present the final result back to the user
as your final answer. Do not significantly rewrite a specialist's own answer -
relay it, keeping their own voice and sign-off intact, with at most one short
framing sentence in your own calm, organized Chief-of-Staff tone. Each specialist
is a distinct character on the team (Patch codes, Scout researches, Herald leads
news, Quill writes, Sage assists, Roster runs the task list, Gale does weather,
Cadence handles calendar and reminders, Piper handles email) - let their
personality come through rather than flattening everyone into one voice.
"""


def call_with_retries(func, max_attempts=3, delay_seconds=2, label="API call"):
    for attempt in range(1, max_attempts + 1):
        try:
            return func()

        except Exception as e:
            logger.warning(f"{label} failed on attempt {attempt}/{max_attempts}: {e}")

            if attempt == max_attempts:
                logger.error(f"{label} failed after {max_attempts} attempts: {e}")
                raise

            print(f"[retry] {label} failed, retrying in {delay_seconds}s... (attempt {attempt}/{max_attempts})")
            time.sleep(delay_seconds)


def _now_local():
    """Current time as an aware datetime in the configured TIMEZONE, falling back
    to the system local time if the zone can't be resolved (e.g. missing tz data)."""
    try:
        return datetime.now(ZoneInfo(TIMEZONE))
    except Exception:
        return datetime.now().astimezone()


def get_ai_response(input_messages, model=PREMIUM_MODEL, instructions=ASSISTANT_INSTRUCTIONS):
    try:
        openai_client = get_openai_client()
        response = call_with_retries(
            lambda: openai_client.responses.create(
                model=model,
                instructions=instructions,
                input=input_messages
            ),
            label="OpenAI chat call"
        )

        return response.output_text

    except Exception:
        return "Sorry, something went wrong while contacting the AI service. Check your API key, internet connection, or account billing."


def get_embedding(text):
    try:
        openai_client = get_openai_client()
        response = call_with_retries(
            lambda: openai_client.embeddings.create(model=EMBEDDING_MODEL_NAME, input=text),
            label="OpenAI embedding call"
        )
        return response.data[0].embedding

    except Exception:
        print("Sorry, something went wrong while embedding text for memory.")
        return None


def build_augmented_prompt(prompt):
    # Give every agent the current date/time so time-aware ones (weather "today",
    # Cadence turning "in 2 hours" into an ISO timestamp) don't have to guess.
    now_line = f"Current date and time: {_now_local().strftime('%Y-%m-%d %H:%M %Z')}"

    memories = recall_memories(prompt, n_results=3)

    # Only inject memories that are actually similar - Chroma returns its closest
    # matches even when nothing is relevant, so without this floor a sparse store
    # pastes unrelated facts into every prompt (wasted tokens, worse answers).
    memories = [m for m in memories if m["distance"] <= MEMORY_DISTANCE_THRESHOLD]

    if len(memories) == 0:
        return f"{now_line}\n\n{prompt}"

    memory_text = "\n".join(f"- {memory['text']}" for memory in memories)
    return f"""{now_line}

Relevant memories from earlier conversations:
{memory_text}

Current message:
{prompt}
"""


def run_with_tools(instructions, input_items, tools, max_iterations=MAX_TOOL_ITERATIONS, model=PREMIUM_MODEL):
    assistant_response = "Sorry, I tried too many tool calls without finishing. Please try rephrasing."

    try:
        openai_client = get_openai_client()
        for _ in range(max_iterations):
            response = call_with_retries(
                lambda: openai_client.responses.create(
                    model=model,
                    instructions=instructions,
                    input=input_items,
                    tools=tools
                ),
                label="OpenAI chat call"
            )

            function_calls = [item for item in response.output if item.type == "function_call"]

            if not function_calls:
                assistant_response = response.output_text
                break

            input_items += response.output

            for call in function_calls:
                result = execute_tool(call.name, json.loads(call.arguments))
                input_items.append({
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": result
                })

    except Exception:
        assistant_response = "Sorry, something went wrong while contacting the AI service. Check your API key, internet connection, or account billing."

    return assistant_response


def ask_ai(prompt, record_history=True):
    augmented_prompt = build_augmented_prompt(prompt)

    if record_history:
        history = get_history()
        # Real history keeps the plain prompt; only this one-off call sees the
        # memory-augmented version, so /history never shows the injected memories.
        history.append({
            "role": "user",
            "content": prompt
        })

        # Window the history sent to the model to the most recent messages so
        # token cost doesn't climb unbounded every turn. The stored history
        # itself is untouched (so /history still shows the full record); only the
        # model input is trimmed. Slice off the just-appended message first, then
        # keep the last MAX_HISTORY_MESSAGES, then re-add the augmented version.
        recent_history = history[:-1][-MAX_HISTORY_MESSAGES:]
        input_items = recent_history + [{
            "role": "user",
            "content": augmented_prompt
        }]
    else:
        # Called as a Manager delegation target - the Manager logs the real
        # history entry itself, using the user's verbatim message.
        input_items = [{"role": "user", "content": augmented_prompt}]

    assistant_response = run_with_tools(ASSISTANT_INSTRUCTIONS, input_items, TOOLS, model=GENERAL_MODEL)

    if record_history:
        get_history().append({
            "role": "assistant",
            "content": assistant_response
        })

        store_memory(f"User said: {prompt}\nAssistant replied: {assistant_response[:200]}", source="chat")

    return assistant_response


def show_help():
    print("""
Available commands:
/help                       - Show this help menu
/clear                      - Clear the current conversation memory
/history                    - Show the current conversation memory
/read <filename>            - Read a file from the files folder
/askfile <filename> <question> - Ask a question about a file
/search <query>             - Search the web and get an AI-summarized answer
/remember <fact>            - Save a fact to long-term memory
/recall <query>             - See what long-term memory has stored about a topic
/code <task>                - Ask the Coding Agent
/research <topic>           - Ask the Researcher Agent
/write <prompt>             - Ask the Writer Agent
/task <request>             - Ask the Personal Assistant Agent
/quit                       - Exit the assistant

Anything else is routed by the Manager Agent to whichever specialist (or the
general assistant) fits best.
""")


def show_history():
    history = get_history()
    if len(history) == 0:
        print("Conversation history is empty.")
        return

    print("\nConversation history:")

    for message in history:
        role = message["role"]
        content = message["content"]

        print(f"\n{role.upper()}: {content}")


def get_safe_file_path(filename):
    files_dir_path = FILES_DIR.resolve()
    file_path = (FILES_DIR / filename).resolve()

    try:
        file_path.relative_to(files_dir_path)
    except ValueError:
        return None

    return file_path


MAX_READ_FILE_CHARS = 50_000
TRUNCATION_NOTICE = f"\n\n[File truncated after {MAX_READ_FILE_CHARS} characters.]"


def read_limited_text(file_path):
    with file_path.open("r", encoding="utf-8") as f:
        content = f.read(MAX_READ_FILE_CHARS + 1)

    if len(content) <= MAX_READ_FILE_CHARS:
        return content

    return content[:MAX_READ_FILE_CHARS] + TRUNCATION_NOTICE


def read_file(filename):
    file_path = get_safe_file_path(filename)

    if file_path is None:
        print("Access denied. You can only read files inside the files folder.")
        return

    if not file_path.exists():
        print(f"File not found: {filename}")
        return

    if not file_path.is_file():
        print(f"That is not a file: {filename}")
        return

    content = read_limited_text(file_path)

    print(f"\nContents of {filename}:")
    print(content)


MAX_WRITE_FILE_CHARS = 50_000


def write_file(filename, content):
    if len(content) > MAX_WRITE_FILE_CHARS:
        return f"Refused to write {filename}: content is too large ({len(content)} characters, limit is {MAX_WRITE_FILE_CHARS})."

    file_path = get_safe_file_path(filename)

    if file_path is None:
        return "Access denied. You can only write files inside the files folder."

    file_path.write_text(content, encoding="utf-8")
    result = f"Saved to {filename}."

    # Mirror to GitHub so the file survives redeploys and is reachable from your PC
    # (a no-op that returns None unless GITHUB_TOKEN/GITHUB_REPO are configured).
    github_note = github_helpers.push_file(filename, content)
    if github_note:
        result += f" {github_note}"

    return result


def _code_exec_env():
    """A minimal, secret-free environment for executed code. We whitelist only what
    the interpreter needs to start (on Windows, SystemRoot/PATH are required for
    Python to import its own standard library) - deliberately NOT the whole process
    env, so secrets like OPENAI_API_KEY/TODOIST_API_TOKEN never reach run code."""
    allowed = {}
    for key in ("PATH", "SYSTEMROOT", "SystemRoot", "TEMP", "TMP", "LANG", "LC_ALL", "TZ"):
        if key in os.environ:
            allowed[key] = os.environ[key]
    return allowed


def _code_exec_limits():
    """POSIX-only preexec_fn that caps CPU seconds, address space, and open files so
    runaway code can't exhaust the host. Returns None on Windows (no resource module
    - the subprocess timeout is the only guard there)."""
    if os.name != "posix":
        return None

    import resource  # POSIX-only stdlib

    def set_limits():
        cpu = CODE_EXEC_TIMEOUT_SECONDS + 1
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        mem = CODE_EXEC_MEMORY_MB * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))

    return set_limits


def run_python(code):
    """Execute a Python snippet in a sandboxed subprocess and return its output.

    This is a pragmatic sandbox for a single-user assistant, NOT a hard security
    jail: it strips secrets from the environment, runs in an isolated throwaway
    working directory, enforces a wall-clock timeout, and (on Linux) caps CPU and
    memory - but executed code can still read the local disk and reach the network.
    Don't run untrusted third-party code with it.
    """
    SANDBOX_DIR.mkdir(exist_ok=True)

    # Write the snippet to a temp file inside the sandbox dir and run it from there,
    # so the script's own working directory is the throwaway sandbox, not the app.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", dir=SANDBOX_DIR, delete=False, encoding="utf-8"
    ) as script_file:
        script_file.write(code)
        script_path = script_file.name

    logger.info(f"run_python executing {len(code)} chars")

    try:
        result = subprocess.run(
            [sys.executable, "-I", script_path],
            cwd=SANDBOX_DIR,
            capture_output=True,
            text=True,
            timeout=CODE_EXEC_TIMEOUT_SECONDS,
            env=_code_exec_env(),
            preexec_fn=_code_exec_limits(),
        )
    except subprocess.TimeoutExpired:
        logger.info("run_python timed out")
        return f"Execution timed out after {CODE_EXEC_TIMEOUT_SECONDS} seconds (likely an infinite loop or something too slow)."
    except Exception as e:
        logger.error(f"run_python failed to launch: {e}")
        return "Sorry, something went wrong while trying to run the code."
    finally:
        try:
            os.remove(script_path)
        except OSError:
            pass

    output = result.stdout or ""
    if result.stderr:
        output += ("\n[stderr]\n" if output else "[stderr]\n") + result.stderr

    output = output.strip() or "(no output)"

    if len(output) > MAX_CODE_OUTPUT_CHARS:
        output = output[:MAX_CODE_OUTPUT_CHARS] + f"\n... [truncated at {MAX_CODE_OUTPUT_CHARS} characters]"

    return f"Exit code {result.returncode}.\n{output}"


def ask_file(filename, question):
    file_path = get_safe_file_path(filename)

    if file_path is None:
        print("Access denied. You can only read files inside the files folder.")
        return

    if not file_path.exists():
        print(f"File not found: {filename}")
        return

    if not file_path.is_file():
        print(f"That is not a file: {filename}")
        return

    content = read_limited_text(file_path)

    prompt = f"""
Use the file content below to answer the user's question.

File name: {filename}

File content:
{content}

User question:
{question}
"""

    history = get_history()
    temporary_history = history + [{
        "role": "user",
        "content": prompt
    }]

    answer = get_ai_response(temporary_history)

    history.append({
        "role": "user",
        "content": f"Asked about file {filename}: {question}"
    })

    history.append({
        "role": "assistant",
        "content": answer
    })

    print()
    print("AI response:")
    print(answer)


def create_task(content):
    try:
        request_id = str(uuid.uuid4())
        headers = get_todoist_headers({"X-Request-Id": request_id})
        # Todoist's REST API v2 (/rest/v2/...) is deprecated as of this writing -
        # confirmed directly against the real API, not assumed from documentation,
        # since it returned a 410 Gone pointing to the newer /api/v1/ endpoints.
        response = call_with_retries(
            lambda: requests.post(
                "https://api.todoist.com/api/v1/tasks",
                headers=headers,
                json={"content": content},
                timeout=10
            ),
            label="Todoist create task call"
        )

        if response.status_code != 200:
            return f"Could not create task: {response.status_code} {response.text}"

        return f"Created task: {content}"

    except Exception as e:
        logger.error(f"Todoist create_task call failed: {e}")
        if isinstance(e, RuntimeError):
            return "Todoist isn't configured yet - set TODOIST_API_TOKEN to create tasks."
        return "Sorry, something went wrong while creating the task."


def list_tasks():
    try:
        headers = get_todoist_headers()
        response = call_with_retries(
            lambda: requests.get(
                "https://api.todoist.com/api/v1/tasks",
                headers=headers,
                timeout=10
            ),
            label="Todoist list tasks call"
        )

        if response.status_code != 200:
            return f"Could not list tasks: {response.status_code} {response.text}"

        # v1's list endpoint wraps results in {"results": [...], "next_cursor": ...}
        # rather than returning a bare list - confirmed against the real API.
        tasks = response.json()["results"]

        if not tasks:
            return "No open tasks."

        return "\n".join(f"- {task['content']}" for task in tasks)

    except Exception as e:
        logger.error(f"Todoist list_tasks call failed: {e}")
        if isinstance(e, RuntimeError):
            return "Todoist isn't configured yet - set TODOIST_API_TOKEN to list tasks."
        return "Sorry, something went wrong while listing tasks."


def get_weather(location):
    try:
        api_key = get_openweather_api_key()
        response = call_with_retries(
            lambda: requests.get(
                "https://api.openweathermap.org/data/2.5/weather",
                params={"q": location, "appid": api_key, "units": "imperial"},
                timeout=10
            ),
            label="OpenWeatherMap call"
        )
        data = response.json()

        if response.status_code != 200:
            return f"Could not get weather for {location}: {data.get('message', 'unknown error')}"

        description = data["weather"][0]["description"]
        temp = data["main"]["temp"]
        feels_like = data["main"]["feels_like"]
        return f"{location}: {description}, {temp}F (feels like {feels_like}F)"

    except Exception as e:
        logger.error(f"OpenWeatherMap call failed: {e}")
        if isinstance(e, RuntimeError):
            return "OpenWeatherMap isn't configured yet - set OPENWEATHER_API_KEY to get weather."
        return f"Sorry, something went wrong while getting the weather for {location}."


# Reminders (Feature: proactive). Stored as a flat JSON list so they survive a
# restart; the actual firing is done by group_bot.py via the on_reminder_set hook.
# Set by group_bot.py to a function (reminder_dict) -> None that schedules the
# reminder to fire. None (CLI/single bot) means reminders are stored but won't fire.
on_reminder_set = None


def load_reminders():
    if not REMINDERS_FILE.exists():
        return []
    try:
        return json.loads(REMINDERS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Failed to read reminders.json: {e}")
        return []


def _save_reminders(reminders):
    REMINDERS_FILE.write_text(json.dumps(reminders, indent=2), encoding="utf-8")


def add_reminder(due_iso, text):
    reminders = load_reminders()
    reminder = {"id": str(uuid.uuid4()), "due_iso": due_iso, "text": text, "fired": False}
    reminders.append(reminder)
    _save_reminders(reminders)
    return reminder


def mark_reminder_fired(reminder_id):
    reminders = load_reminders()
    for reminder in reminders:
        if reminder["id"] == reminder_id:
            reminder["fired"] = True
    _save_reminders(reminders)


def set_reminder(when, text):
    try:
        due = date_parser.parse(when)
    except (ValueError, OverflowError, TypeError):
        return f"I couldn't understand the time '{when}'. Give me an absolute time like 2026-07-01T15:00."

    now = _now_local()

    # Make both sides comparable: if the parsed time is naive, assume it's in the
    # app timezone (matching the current time we hand the model).
    if due.tzinfo is None:
        due = due.replace(tzinfo=now.tzinfo)

    if due <= now:
        return f"That time ({due.strftime('%Y-%m-%d %H:%M %Z')}) is in the past - give me a future time."

    reminder = add_reminder(due.isoformat(), text)

    if on_reminder_set:
        on_reminder_set(reminder)
        note = ""
    else:
        note = " (Heads up: reminders only actually fire when the Telegram group bot is running.)"

    return f"Reminder set for {due.strftime('%Y-%m-%d %H:%M %Z')}: {text}.{note}"


def list_reminders():
    reminders = [r for r in load_reminders() if not r["fired"]]

    if not reminders:
        return "No upcoming reminders."

    reminders.sort(key=lambda r: r["due_iso"])
    return "\n".join(f"- {r['due_iso']}: {r['text']}" for r in reminders)


def build_morning_briefing():
    """Assemble raw facts (weather, open tasks, today's calendar) and have Miles
    write a short, warm briefing. Interface-agnostic - group_bot.py posts it to the
    Telegram group on a schedule."""
    weather = get_weather(HOME_LOCATION)
    tasks = list_tasks()
    events = google_helpers.list_today_events()

    facts = f"Weather ({HOME_LOCATION}): {weather}\n\nOpen tasks:\n{tasks}\n\nToday's calendar:\n{events}"

    prompt = f"""Write Tyler a short, friendly morning briefing from the facts below.
Open with a one-line greeting, then cover the weather, today's calendar, and open
tasks in a few tight, skimmable lines. Warm but not wordy. Sign off as "- Miles".

Facts:
{facts}
"""
    return get_ai_response(
        [{"role": "user", "content": prompt}],
        model=FAST_MODEL,
        instructions="You are Miles, Tyler's calm, organized Chief of Staff writing his morning briefing.",
    )


def search_web(query):
    try:
        tavily_client = get_tavily_client()
        response = call_with_retries(
            lambda: tavily_client.search(query, max_results=5),
            label="Tavily search call"
        )
        return response["results"]

    except Exception as e:
        logger.error(f"Tavily search failed: {e}")
        if isinstance(e, RuntimeError):
            print("Tavily isn't configured yet - set TAVILY_API_KEY to search the web.")
        else:
            print("Sorry, something went wrong while searching the web. Check your Tavily API key or internet connection.")
        return None


def ask_search(query):
    results = search_web(query)

    if results is None:
        return

    if len(results) == 0:
        print(f"No search results found for: {query}")
        return

    formatted_results = ""

    for result in results:
        formatted_results += f"\nTitle: {result['title']}\nURL: {result['url']}\nContent: {result['content']}\n"

    prompt = f"""
Use the web search results below to answer the user's question.
Mention which source(s) you used when relevant.

Search query: {query}

Search results:
{formatted_results}

User question:
{query}
"""

    history = get_history()
    temporary_history = history + [{
        "role": "user",
        "content": prompt
    }]

    answer = get_ai_response(temporary_history)

    history.append({
        "role": "user",
        "content": f"Searched the web for: {query}"
    })

    history.append({
        "role": "assistant",
        "content": answer
    })

    print()
    print("AI response:")
    print(answer)


def store_memory(text, source="chat"):
    embedding = get_embedding(text)

    if embedding is None:
        return

    memory_collection.add(
        ids=[str(uuid.uuid4())],
        embeddings=[embedding],
        documents=[text],
        metadatas=[{"source": source, "timestamp": datetime.now().isoformat()}]
    )


def remember_fact(fact):
    store_memory(fact, source="remember")
    print(f"Got it, I'll remember: {fact}")


def recall_memories(query, n_results=3):
    if memory_collection.count() == 0:
        return []

    embedding = get_embedding(query)

    if embedding is None:
        return []

    results = memory_collection.query(
        query_embeddings=[embedding],
        n_results=min(n_results, memory_collection.count())
    )

    memories = []

    for document, distance in zip(results["documents"][0], results["distances"][0]):
        memories.append({"text": document, "distance": distance})

    return memories


def show_recall(query):
    # Chroma always returns its closest matches even if none are truly relevant -
    # there's no built-in relevance cutoff, so a sparse memory store can surface
    # unrelated results. Lower distance = more similar.
    memories = recall_memories(query, n_results=5)

    if len(memories) == 0:
        print("No memories stored yet.")
        return

    print(f"\nMemories related to: {query}")

    for memory in memories:
        print(f"\n[distance {memory['distance']:.4f}] {memory['text']}")


# Controls how *sensitive* actions (writing a file, sending an email) get approved,
# since different interfaces have different ways (or no way) to ask. One of:
#   "enabled"               - ask via input() and act immediately (CLI default)
#   "disabled"               - refuse the sensitive action outright
#   "requires_confirmation"  - no real terminal to read input() from (e.g. the
#                              Telegram bot) - stage the action per-chat (see
#                              pending_actions) and tell the caller how to confirm
#                              it later
CONFIRMATION_MODE = "enabled"

# A staged sensitive action is held per-conversation in pending_actions (defined near
# the top of this file), tagged with "type" ("write_file"/"send_email"/"github_delete")
# plus that action's payload. Per-chat so confirming/cancelling in one chat can't touch
# an action staged in another; the interface resolves it when the user replies /confirm.

# Set by group_bot.py to a function (specialist_key, request_text, answer_text) -> None.
# Called only from execute_tool's delegate_to_* branches (genuine Manager-mediated
# delegation), never from ask_specialist/ask_ai themselves - direct @mention calls in
# the group bypass the Manager entirely and shouldn't trigger a fake "delegating..."
# announcement. None (the default) is a no-op for the CLI and the single-bot bot.py.
on_delegation = None

# The current turn's reply destination, set by group_bot.py so on_delegation knows
# where a dispatched agent's answer should go: {"kind": "group"} posts to the group;
# {"kind": "manager_dm", "user_id": ..., "chat_id": ...} makes each dispatched agent
# DM the user directly (with Miles recapping). A contextvar so it propagates through
# asyncio.to_thread into execute_tool. None (CLI/single bot) means "no routing".
_reply_context = contextvars.ContextVar("reply_context", default=None)


def set_reply_context(ctx):
    _reply_context.set(ctx)


def current_reply_context():
    return _reply_context.get()


def confirm_pending_action(pending):
    """Execute a staged sensitive action after the user replies /confirm. Shared by
    both Telegram interfaces so the confirm/cancel dispatch lives in one place."""
    if pending["type"] == "write_file":
        return write_file(pending["filename"], pending["content"])
    if pending["type"] == "send_email":
        return google_helpers.send_email(pending["to"], pending["subject"], pending["body"])
    if pending["type"] == "github_delete":
        return github_helpers.delete_file(pending["path"])
    return "Nothing to confirm."


def describe_pending_action(pending):
    """Short human description of a staged action, for confirm/cancel messages."""
    if pending["type"] == "write_file":
        return f"write to files/{pending['filename']}"
    if pending["type"] == "send_email":
        return f"email to {pending['to']}"
    if pending["type"] == "github_delete":
        return f"deletion of {pending['path']} from GitHub"
    return "the staged action"


SENSITIVE_TOOL_ARGUMENT_KEYS = {
    "body",
    "code",
    "content",
    "new_snippet",
    "old_snippet",
    "subject",
}


def redact_tool_arguments(arguments):
    """Return a log-safe copy of tool arguments without private payload text."""
    redacted = {}
    for key, value in arguments.items():
        if key in SENSITIVE_TOOL_ARGUMENT_KEYS:
            redacted[key] = f"[redacted {type(value).__name__}, {len(str(value))} chars]"
        else:
            redacted[key] = value
    return redacted


def execute_tool(name, arguments):
    print(f"\n[tool] {name}({arguments})")
    logger.info(f"Tool call: {name}({redact_tool_arguments(arguments)})")

    try:
        if name == "read_file":
            file_path = get_safe_file_path(arguments["filename"])

            if file_path is None:
                return "Access denied. You can only read files inside the files folder."

            if not file_path.exists() or not file_path.is_file():
                return f"File not found: {arguments['filename']}"

            return read_limited_text(file_path)

        if name == "search_the_web":
            results = search_web(arguments["query"])

            if not results:
                return "No search results found."

            return "\n".join(f"{r['title']}: {r['content']} ({r['url']})" for r in results)

        if name == "remember_fact":
            store_memory(arguments["fact"], source="remember")
            return f"Saved to memory: {arguments['fact']}"

        if name == "recall_memories":
            memories = recall_memories(arguments["query"], n_results=5)

            if not memories:
                return "No memories found."

            return "\n".join(memory["text"] for memory in memories)

        if name == "create_task":
            return create_task(arguments["content"])

        if name == "list_tasks":
            return list_tasks()

        if name == "get_weather":
            return get_weather(arguments["location"])

        if name == "run_python":
            return run_python(arguments["code"])

        if name == "list_calendar_events":
            time_min = arguments.get("time_min")
            time_max = arguments.get("time_max")
            if time_min and time_max:
                return google_helpers.list_events(time_min, time_max)
            return google_helpers.list_today_events()

        if name == "create_calendar_event":
            return google_helpers.create_event(
                arguments["summary"], arguments["start"], arguments["end"], arguments.get("description", "")
            )

        if name == "set_reminder":
            return set_reminder(arguments["when"], arguments["text"])

        if name == "list_reminders":
            return list_reminders()

        if name == "search_emails":
            return google_helpers.list_recent_emails(arguments.get("query", ""), arguments.get("max_results", 10))

        if name == "read_email":
            return google_helpers.get_email(arguments["message_id"])

        if name == "draft_email":
            return google_helpers.create_draft(arguments["to"], arguments["subject"], arguments["body"])

        if name == "send_email":
            if CONFIRMATION_MODE == "disabled":
                return "Sending email is disabled in this interface."

            if CONFIRMATION_MODE == "requires_confirmation":
                set_pending_action({
                    "type": "send_email",
                    "to": arguments["to"],
                    "subject": arguments["subject"],
                    "body": arguments["body"],
                })
                logger.info(f"Staged email to {arguments['to']}, awaiting out-of-band confirmation")
                return (
                    f"Email to {arguments['to']} (subject: '{arguments['subject']}') is staged and "
                    f"waiting for your confirmation. Reply /confirm to send it, or anything else to "
                    f"cancel it."
                )

            # CONFIRMATION_MODE == "enabled" - CLI path
            answer = input(f"The AI wants to send an email to {arguments['to']} (subject: {arguments['subject']}). Allow? (y/n): ")

            if answer.strip().lower() != "y":
                logger.info(f"User denied email send to {arguments['to']}")
                return "The user declined to send this email."

            logger.info(f"User approved email send to {arguments['to']}")
            return google_helpers.send_email(arguments["to"], arguments["subject"], arguments["body"])

        if name == "github_list_files":
            return github_helpers.list_files(arguments.get("path", ""))

        if name == "github_read_file":
            return github_helpers.read_file(arguments["path"])

        if name == "github_save_file":
            return github_helpers.save_file(arguments["path"], arguments["content"])

        if name == "github_delete_file":
            if CONFIRMATION_MODE == "disabled":
                return "Deleting files is disabled in this interface."

            if CONFIRMATION_MODE == "requires_confirmation":
                set_pending_action({"type": "github_delete", "path": arguments["path"]})
                logger.info(f"Staged GitHub delete of {arguments['path']}, awaiting confirmation")
                return (
                    f"Deleting {arguments['path']} from GitHub is staged and waiting for your "
                    f"confirmation. Reply /confirm to delete it (still recoverable from git "
                    f"history), or anything else to cancel it."
                )

            # CONFIRMATION_MODE == "enabled" - CLI path
            answer = input(f"The AI wants to delete {arguments['path']} from the GitHub repo. Allow? (y/n): ")

            if answer.strip().lower() != "y":
                logger.info(f"User denied GitHub delete of {arguments['path']}")
                return "The user declined to delete the file."

            logger.info(f"User approved GitHub delete of {arguments['path']}")
            return github_helpers.delete_file(arguments["path"])

        if name == "code_list_files":
            return github_helpers.code_list_files(arguments.get("path", ""))

        if name == "code_read_file":
            return github_helpers.code_read_file(arguments["path"])

        if name == "code_propose_change":
            # No /confirm gate here: the pull request IS the review step - nothing
            # merges to the base branch (or ships) until the user approves it.
            return github_helpers.code_propose_change(
                arguments["branch"], arguments["path"], arguments["content"],
                arguments["title"], arguments.get("body", "")
            )

        if name == "code_edit_file":
            return github_helpers.code_edit_file(
                arguments["branch"], arguments["path"], arguments["old_snippet"],
                arguments["new_snippet"], arguments["title"], arguments.get("body", "")
            )

        if name == "write_file":
            if CONFIRMATION_MODE == "disabled":
                return "File writing is disabled in this interface."

            file_path = get_safe_file_path(arguments["filename"])
            file_exists = file_path is not None and file_path.exists()
            overwrite_note = f" This will OVERWRITE the existing files/{arguments['filename']}." if file_exists else ""

            if CONFIRMATION_MODE == "requires_confirmation":
                set_pending_action({
                    "type": "write_file",
                    "filename": arguments["filename"],
                    "content": arguments["content"],
                })
                logger.info(f"Staged write to {arguments['filename']}, awaiting out-of-band confirmation")
                return (
                    f"Write to files/{arguments['filename']} is staged and waiting for your "
                    f"confirmation.{overwrite_note} Reply /confirm to write it, or anything "
                    f"else to cancel it."
                )

            # CONFIRMATION_MODE == "enabled" - CLI path, unchanged from before
            if file_exists:
                print(f"\nWARNING: files/{arguments['filename']} already exists and will be OVERWRITTEN.")

            answer = input(f"The AI wants to write to files/{arguments['filename']}. Allow? (y/n): ")

            if answer.strip().lower() != "y":
                logger.info(f"User denied write to {arguments['filename']}")
                return "The user denied permission to write this file."

            logger.info(f"User approved write to {arguments['filename']} (overwrite={file_exists})")
            return write_file(arguments["filename"], arguments["content"])

        if name == "delegate_to_coding_agent":
            answer = ask_specialist("code", arguments["task"], record_history=False)
            if on_delegation:
                on_delegation("code", arguments["task"], answer)
            return answer

        if name == "delegate_to_research_agent":
            answer = ask_specialist("research", arguments["topic"], record_history=False)
            if on_delegation:
                on_delegation("research", arguments["topic"], answer)
            return answer

        if name == "delegate_to_news_agent":
            answer = ask_specialist("news", arguments["topic"], record_history=False)
            if on_delegation:
                on_delegation("news", arguments["topic"], answer)
            return answer

        if name == "delegate_to_writer_agent":
            answer = ask_specialist("write", arguments["prompt"], record_history=False)
            if on_delegation:
                on_delegation("write", arguments["prompt"], answer)
            return answer

        if name == "delegate_to_personal_assistant":
            answer = ask_specialist("task", arguments["request"], record_history=False)
            if on_delegation:
                on_delegation("task", arguments["request"], answer)
            return answer

        if name == "delegate_to_tasks_agent":
            answer = ask_specialist("tasks", arguments["request"], record_history=False)
            if on_delegation:
                on_delegation("tasks", arguments["request"], answer)
            return answer

        if name == "delegate_to_weather_agent":
            answer = ask_specialist("weather", arguments["location"], record_history=False)
            if on_delegation:
                on_delegation("weather", arguments["location"], answer)
            return answer

        if name == "delegate_to_calendar_agent":
            answer = ask_specialist("calendar", arguments["request"], record_history=False)
            if on_delegation:
                on_delegation("calendar", arguments["request"], answer)
            return answer

        if name == "delegate_to_gmail_agent":
            answer = ask_specialist("gmail", arguments["request"], record_history=False)
            if on_delegation:
                on_delegation("gmail", arguments["request"], answer)
            return answer

        if name == "delegate_to_general_assistant":
            answer = ask_ai(arguments["prompt"], record_history=False)
            if on_delegation:
                on_delegation("general", arguments["prompt"], answer)
            return answer

        return f"Unknown tool: {name}"

    except KeyError as e:
        error_message = f"Tool error: missing required argument {e} for {name}."
        logger.error(error_message)
        return error_message

    except Exception as e:
        error_message = f"Tool error: something went wrong while running {name}."
        logger.error(f"{error_message} ({e})")
        return error_message


# Each specialist is a member of the team with a real name and personality. Its
# prompt is built as role + persona (see build_persona_instructions): `role` is the
# functional, load-bearing guidance (what tools to use, safety rules like the
# Personal-Assistant-vs-Tasks distinction) and comes first so it dominates behavior;
# `persona` layers on the character's voice. `model` picks the cost tier - work that
# needs real reasoning gets PREMIUM_MODEL, thin API-wrapping work gets FAST_MODEL.
SPECIALISTS = {
    "code": {
        "name": "Patch",
        "label": "Patch (Coding Agent)",
        "model": PREMIUM_MODEL,
        # Scaffolding a whole new agent means reading a few files and then making many
        # small, precise code_edit_file changes (delegation tool, manager bullet,
        # execute_tool branch, SPECIALISTS entry, group_bot wiring, README...), so give
        # the coding agent a generous tool-call budget - 8 runs out mid-mission.
        "max_iterations": 25,
        "tool_names": ["read_file", "write_file", "search_the_web", "recall_memories", "run_python",
                       "github_list_files", "github_read_file", "github_save_file", "github_delete_file",
                       "code_list_files", "code_read_file", "code_propose_change", "code_edit_file"],
        "role": """
You are a careful coding assistant. Help the user write, read, and debug code.
Use write_file to save code you're asked to create or change, and read_file to
check existing files before editing them. Use run_python to actually execute and
verify Python before handing it over - it runs in a sandbox with a short timeout
and no access to the app's secrets or files, so test your work instead of guessing.
Use search_the_web if you need to look up an error or current documentation.

You have full access to a connected workspace GitHub repo: github_list_files and
github_read_file to browse it, github_save_file to commit files directly, and
github_delete_file to remove one (the user confirms deletes). Use this for standalone
files and code output; share the GitHub URL you get back.

You can also improve the assistant's OWN codebase, but only by proposing pull
requests the user reviews - you NEVER change the live code directly. Use
code_list_files and code_read_file to study the project first. To change an existing
file, use code_edit_file: read it, then replace one exact, unique snippet - you don't
reproduce the whole file, which is how you edit big files like main.py reliably. Use
code_propose_change only for brand-new files. Always read a file before you change it,
keep each pull request small and focused, use a clear branch name (e.g.
"add-spotify-agent"), and reuse the same branch across all edits in one change so they
land in a single PR. Remind the user a change only ships after they merge the PR and
redeploy. Explain your reasoning briefly and prefer simple, correct solutions over
clever ones.
""",
        "persona": """
You are Patch, the team's coding specialist. Voice: blunt, pragmatic senior
engineer. Short sentences. Dry humor. You distrust clever code and push for the
simplest correct solution. Skip the pleasantries, get to the point, and sign off
with "- Patch".
"""
    },
    "research": {
        "name": "Scout",
        "label": "Scout (Researcher Agent)",
        "model": PREMIUM_MODEL,
        "tool_names": ["search_the_web", "recall_memories", "remember_fact"],
        "role": """
You are a thorough research assistant. Use search_the_web to find current,
accurate information and cite your sources. Use remember_fact to save important
findings for later, and recall_memories to check what's already been researched
before searching again. Be clear about what is verified fact versus speculation.
""",
        "persona": """
You are Scout, the team's researcher. Voice: curious, energetic fact-hound who
loves a good source and always says where a claim came from. You clearly label
what's "verified" versus "unconfirmed". Sign off with "- Scout".
"""
    },
    "news": {
        "name": "Herald",
        "label": "Herald (News Agent)",
        "model": FAST_MODEL,
        "tool_names": ["search_the_web", "recall_memories"],
        "role": """
You are a news assistant. Use search_the_web for current coverage and cite the
sources you rely on. Use recall_memories to check whether the user has relevant
past context or preferences before summarizing. Lead with the headline, group
updates by topic, and separate verified reporting from uncertainty, early reports,
or analysis. Do not invent details beyond the sources you found.
""",
        "persona": """
You are Herald, the team's news specialist. Voice: crisp newsroom anchor. Lead
with the headline, group developments by topic, cite sources cleanly, and keep the
copy tight. Sign off with "- Herald".
"""
    },
    "write": {
        "name": "Quill",
        "label": "Quill (Writer Agent)",
        "model": PREMIUM_MODEL,
        "tool_names": ["read_file", "write_file", "recall_memories"],
        "role": """
You are a skilled writing assistant. Help the user draft, edit, and improve
written content. Use read_file to review an existing draft before editing it,
and write_file to save a finished draft when asked. Match the tone the user
requests and keep writing clear.
""",
        "persona": """
You are Quill, the team's writer. Voice: warm, thoughtful wordsmith who cares about
tone and rhythm. When it genuinely helps, offer a lighter option and a tighter
option so the user can choose. Sign off with "- Quill".
"""
    },
    "task": {
        "name": "Sage",
        "label": "Sage (Personal Assistant Agent)",
        "model": FAST_MODEL,
        "tool_names": ["remember_fact", "recall_memories", "write_file", "read_file"],
        "role": """
You are an organized personal assistant. Use remember_fact to save important
personal information, reminders, and preferences, and recall_memories to recall
them later. Use write_file to maintain simple notes when asked. You are NOT a
real task-tracking app - for actual to-do items, that's the Tasks Agent's job,
not yours. Be concise and proactive.
""",
        "persona": """
You are Sage, the team's personal assistant. Voice: calm, organized, quietly
proactive - you remember what matters to the user and gently keep things tidy.
Sign off with "- Sage".
"""
    },
    "tasks": {
        "name": "Roster",
        "label": "Roster (Tasks Agent)",
        "model": FAST_MODEL,
        "tool_names": ["create_task", "list_tasks"],
        "role": """
You are a task management assistant connected to the user's real Todoist
account. Use create_task to add new tasks and list_tasks to see what's
currently open before adding duplicates or when asked what's on the list.
You manage a real external to-do list - this is different from the Personal
Assistant Agent, which only saves general facts/reminders to memory. Be concise.
""",
        "persona": """
You are Roster, the team's operations specialist for the real to-do list. Voice:
crisp and reliable. You confirm exactly what landed on the list and what's still
open - no fluff. Sign off with "- Roster".
"""
    },
    "weather": {
        "name": "Gale",
        "label": "Gale (Weather Agent)",
        "model": FAST_MODEL,
        "tool_names": ["get_weather"],
        "role": """
You are a weather assistant. Use get_weather to look up current conditions
for a location the user asks about. Report temperature and conditions
briefly and clearly. If the location is ambiguous (e.g. multiple cities
share a name), ask which one or state your assumption.
""",
        "persona": """
You are Gale, the team's weather specialist. Voice: cheery weather nerd. Give the
conditions clearly, add one emoji and a short wry aside about the weather. Sign off
with "- Gale".
"""
    },
    "calendar": {
        "name": "Cadence",
        "label": "Cadence (Calendar & Scheduler Agent)",
        "model": FAST_MODEL,
        "tool_names": ["list_calendar_events", "create_calendar_event", "set_reminder", "list_reminders"],
        "role": """
You manage the user's Google Calendar and their reminders. Use list_calendar_events
to see what's scheduled, create_calendar_event to add events, set_reminder to
schedule a one-off nudge at a specific time, and list_reminders to review pending
ones. The current date and time is given at the top of each message - use it to turn
relative times ("tomorrow at 3", "in two hours") into exact ISO 8601 timestamps.
Confirm what you scheduled, clearly and briefly.
""",
        "persona": """
You are Cadence, the team's calendar and scheduling specialist. Voice: unflappable
and precise; you keep everyone on time without nagging. Sign off with "- Cadence".
"""
    },
    "gmail": {
        "name": "Piper",
        "label": "Piper (Gmail Agent)",
        "model": FAST_MODEL,
        "tool_names": ["search_emails", "read_email", "draft_email", "send_email"],
        "role": """
You manage the user's Gmail. Use search_emails to find messages, read_email to read
one in full, draft_email to prepare a reply or new message (it just waits in Drafts),
and send_email to send. Sending is sensitive: prefer drafting unless the user clearly
asks to send, and note that sending will ask them to confirm first. Summarize emails
crisply and never invent contents you haven't actually read.
""",
        "persona": """
You are Piper, the team's email specialist. Voice: brisk, discreet, and organized -
you triage a busy inbox without drama. Sign off with "- Piper".
"""
    },
}


def build_persona_instructions(profile):
    """Combine a specialist's functional role with its personality. Role comes
    first so behavior/safety guidance dominates; persona layers voice on top."""
    return profile["role"] + "\n" + profile["persona"]


def ask_specialist(specialist_key, prompt, record_history=True):
    profile = SPECIALISTS[specialist_key]
    specialist_tools = [tool for tool in TOOLS if tool["name"] in profile["tool_names"]]

    augmented_prompt = build_augmented_prompt(prompt)
    input_items = [{"role": "user", "content": augmented_prompt}]

    answer = run_with_tools(
        build_persona_instructions(profile), input_items, specialist_tools,
        max_iterations=profile.get("max_iterations", MAX_TOOL_ITERATIONS), model=profile["model"]
    )

    if record_history:
        history = get_history()
        history.append({
            "role": "user",
            "content": f"Asked {profile['label']}: {prompt}"
        })

        history.append({
            "role": "assistant",
            "content": answer
        })

        store_memory(f"Asked {profile['label']}: {prompt}\n{profile['label']} replied: {answer[:200]}", source="chat")

    print()
    print(f"{profile['name']}:")
    print(answer)

    return answer


def ask_manager(prompt):
    input_items = [{"role": "user", "content": prompt}]
    answer = run_with_tools(
        MANAGER_INSTRUCTIONS, input_items, DELEGATION_TOOLS,
        max_iterations=MAX_MANAGER_TOOL_ITERATIONS, model=FAST_MODEL
    )

    # The Manager owns the history record (using the user's literal message), not
    # the delegated specialist/general assistant, so /history always reflects what
    # the user actually typed rather than the Manager's tool-call phrasing of the
    # delegated task.
    history = get_history()
    history.append({"role": "user", "content": prompt})
    history.append({"role": "assistant", "content": answer})
    store_memory(f"User said: {prompt}\nAssistant replied: {answer[:200]}", source="chat")

    print()
    print("Miles (Manager):")
    print(answer)

    return answer


# Keys select_group_responders may return: every specialist, plus the general
# assistant and the manager (for genuinely multi-step/coordination requests).
GROUP_RESPONDER_KEYS = list(SPECIALISTS.keys()) + ["general", "manager"]

_GROUP_ROUTER_INSTRUCTIONS = """
You are a silent router for a team group chat. You never talk to the user - you only
decide which teammate(s) should reply to a message, based on their expertise:
""" + "".join(
    f"- {key}: {SPECIALISTS[key]['label']}\n" for key in SPECIALISTS
) + """- general: anything unspecialized, smalltalk, or general questions
- manager: ONLY when the request genuinely needs several teammates coordinated in
  sequence (e.g. "look up X, then draft a note about it"), or is too ambiguous to
  route to one teammate

Reply with ONLY a JSON object: {"responders": ["<key>", ...]}. Usually pick exactly
one key. Pick two only when the message clearly spans two teammates' areas. Use
"manager" alone for multi-step coordination. Never include "manager" alongside others.
"""


def select_group_responders(text):
    """Pick which agent(s) should answer a plain group message. Returns a list of
    keys from GROUP_RESPONDER_KEYS. Falls back to ["manager"] (today's behavior) on
    any model or parse error, so a routing glitch never drops the message."""
    try:
        openai_client = get_openai_client()
        response = call_with_retries(
            lambda: openai_client.responses.create(
                model=FAST_MODEL,
                instructions=_GROUP_ROUTER_INSTRUCTIONS,
                input=[{"role": "user", "content": text}],
            ),
            label="Group router call",
        )
        raw = response.output_text.strip()

        # The model may wrap JSON in prose or a ```json fence - grab the object.
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            raise ValueError(f"no JSON object in router output: {raw!r}")

        parsed = json.loads(raw[start:end + 1])
        responders = [k for k in parsed.get("responders", []) if k in GROUP_RESPONDER_KEYS]

        if not responders:
            raise ValueError(f"router returned no valid keys: {parsed!r}")

        # "manager" is a coordination signal, not a co-responder - if present, it wins alone.
        if "manager" in responders:
            return ["manager"]

        return responders

    except Exception as e:
        logger.error(f"Group router failed, falling back to manager: {e}")
        return ["manager"]


def handle_command(user_prompt):
    """Process one line of user input exactly like the CLI does: parse it as a
    command, print the result. Returns False on /quit, True otherwise - shared
    by main() and the Telegram bot (bot.py), which captures the printed output
    instead of reading it off a terminal."""
    if user_prompt.strip() == "":
        print("Please type something before pressing Enter.")
        return True

    command = user_prompt.lower().strip()

    if command == "/help":
        show_help()
        return True

    if command == "/clear":
        get_history().clear()
        print("Conversation memory cleared.")
        return True

    if command in ["quit", "/quit"]:
        print("Goodbye!")
        return False

    if command == "/history":
        show_history()
        return True

    if command.startswith("/read "):
        filename = user_prompt[6:].strip()
        read_file(filename)
        return True

    if command.startswith("/askfile "):
        parts = user_prompt.split(" ", 2)

        if len(parts) < 3:
            print("Usage: /askfile <filename> <question>")
            return True

        filename = parts[1]
        question = parts[2]

        ask_file(filename, question)
        return True

    if command.startswith("/search "):
        query = user_prompt[8:].strip()

        if query == "":
            print("Usage: /search <query>")
            return True

        ask_search(query)
        return True

    if command.startswith("/remember "):
        fact = user_prompt[10:].strip()

        if fact == "":
            print("Usage: /remember <fact>")
            return True

        remember_fact(fact)
        return True

    if command.startswith("/recall "):
        query = user_prompt[8:].strip()

        if query == "":
            print("Usage: /recall <query>")
            return True

        show_recall(query)
        return True

    if command.startswith("/code "):
        task = user_prompt[6:].strip()

        if task == "":
            print("Usage: /code <task>")
            return True

        ask_specialist("code", task)
        return True

    if command.startswith("/research "):
        topic = user_prompt[10:].strip()

        if topic == "":
            print("Usage: /research <topic>")
            return True

        ask_specialist("research", topic)
        return True

    if command.startswith("/write "):
        task = user_prompt[7:].strip()

        if task == "":
            print("Usage: /write <prompt>")
            return True

        ask_specialist("write", task)
        return True

    if command.startswith("/task "):
        request = user_prompt[6:].strip()

        if request == "":
            print("Usage: /task <request>")
            return True

        ask_specialist("task", request)
        return True

    ask_manager(user_prompt)
    return True


def main():
    while True:
        user_prompt = input("\nAsk the AI something, or type 'quit' to exit: ")

        if not handle_command(user_prompt):
            break


if __name__ == "__main__":
    main()
