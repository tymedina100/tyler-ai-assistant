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
import linear_helpers
import projects


# Windows consoles default to a limited encoding (cp1252) that can't print
# every Unicode character (e.g. em dashes, curly quotes) - web search results
# and AI output can easily contain these, so force UTF-8 output to avoid crashes.
# Guarded because stdout isn't always a real terminal stream: under pytest capture
# or when stdout is redirected (bot.py relays via a StringIO), reconfigure may be
# absent, and importing main must never crash just because of that.
if hasattr(sys.stdout, "reconfigure"):
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

# Where persistent state lives (long-term memory, reminders, Company Mode state, the
# Google token). A cloud host wipes the container filesystem on every redeploy, so
# state must sit on a mounted volume. Resolve the volume path in order:
#   1. DATA_DIR, if set explicitly.
#   2. RAILWAY_VOLUME_MOUNT_PATH - Railway sets this automatically when a volume is
#      attached, so persistence "just works" with a volume and no manual var to align.
#   3. the project directory (fine locally; EPHEMERAL on a cloud host).
_data_dir_env = os.environ.get("DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
DATA_DIR = Path(_data_dir_env or BASE_DIR)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Surface where state is going so a missing volume (state silently resetting every
# redeploy) is obvious at startup instead of a mystery.
if _data_dir_env:
    print(f"[state] Persistent state dir: {DATA_DIR}")
else:
    print(
        f"[state] WARNING: no mounted volume detected - state lives in {DATA_DIR}, which "
        "is EPHEMERAL on a cloud host and resets on every redeploy. Attach a Railway "
        "volume (or set DATA_DIR) to persist Company Mode state + memory."
    )

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


# Company Mode v2 (metered autonomy) execution context. When the checkpointed engine
# runs a task it sets a per-task "sink" that model calls accrue real USD cost and
# produced-artifact links into, and flips _company_execution so execute_tool auto-
# approves *produce* actions (file/PR writes) while still staging *irreversible* ones
# (email/delete). Both default to "off", so the CLI and ordinary bot turns are
# completely unaffected - the sink being None makes every accrue call a no-op.
_execution_sink = contextvars.ContextVar("execution_sink", default=None)
_company_execution = contextvars.ContextVar("company_execution", default=False)


def set_execution_sink(sink):
    """Point the current turn's cost/artifact accrual at `sink` (a dict with keys
    cost_usd, artifacts, context) or None to disable it."""
    _execution_sink.set(sink)


def current_execution_sink():
    return _execution_sink.get()


def set_company_execution(value):
    _company_execution.set(bool(value))


def in_company_execution():
    return _company_execution.get()


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

# USD price per 1,000 tokens as (input_rate, output_rate) per model. Company Mode
# meters real token usage against these so the daily budget reflects actual spend,
# not a flat estimate. This is the ONE place to update when prices or model ids
# change. Values below are OpenAI list pricing divided by 1,000 (list is per 1M
# tokens). An unknown model falls back to DEFAULT_MODEL_PRICE and logs a warning, so
# a model swap can never silently meter $0 and hide runaway cost - the fallback is
# deliberately on the high side so an unpriced model over-counts rather than under.
DEFAULT_MODEL_PRICE = (0.01, 0.03)
MODEL_PRICING = {
    PREMIUM_MODEL: (0.005, 0.030),         # gpt-5.5:      $5.00 / $30.00 per 1M
    FAST_MODEL: (0.00075, 0.0045),         # gpt-5.4-mini: $0.75 / $4.50 per 1M
    EMBEDDING_MODEL_NAME: (0.00002, 0.0),  # text-embedding-3-small: ~$0.02 per 1M
    # Other current OpenAI models, priced ahead of time for easy model swaps:
    "gpt-5.5-pro": (0.030, 0.180),         # $30.00 / $180.00 per 1M
    "gpt-5.4": (0.0025, 0.015),            # $2.50 / $15.00 per 1M
    "gpt-5.4-nano": (0.0002, 0.00125),     # $0.20 / $1.25 per 1M
    "o4-mini": (0.00055, 0.0022),          # $0.55 / $2.20 per 1M
}


def usage_to_usd(model, usage):
    """Convert an OpenAI usage object to USD via MODEL_PRICING. Tolerant of the two
    usage shapes we see - the Responses API (input_tokens/output_tokens) and the
    embeddings API (prompt_tokens/total_tokens) - and of a missing/odd shape, which
    degrades to $0 plus a warning rather than crashing a turn. VERIFY the real
    attribute names against the installed openai SDK; getattr guards keep a mismatch
    from throwing."""
    if usage is None:
        return 0.0

    in_rate, out_rate = MODEL_PRICING.get(model, DEFAULT_MODEL_PRICE)
    if model not in MODEL_PRICING:
        logger.warning(f"No price for model {model!r}; using default rate for metering.")

    input_tokens = getattr(usage, "input_tokens", None)
    if input_tokens is None:
        input_tokens = getattr(usage, "prompt_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", None)
    if output_tokens is None:
        # Embeddings report only total_tokens; treat the remainder as "input".
        total = getattr(usage, "total_tokens", 0) or 0
        output_tokens = max(0, total - input_tokens)

    return round((input_tokens / 1000.0) * in_rate + (output_tokens / 1000.0) * out_rate, 6)


def _accrue_cost(model, usage):
    """Add a model call's USD cost to the active execution sink, if any. A no-op
    outside Company Mode execution (sink is None), so ordinary turns are unaffected."""
    sink = current_execution_sink()
    if sink is not None:
        sink["cost_usd"] = round(sink.get("cost_usd", 0.0) + usage_to_usd(model, usage), 6)


def _record_artifact(note):
    """Append a produced-deliverable note (a file path or PR/URL) to the active
    execution sink, if any. A no-op outside Company Mode execution."""
    sink = current_execution_sink()
    if sink is not None and note:
        sink.setdefault("artifacts", []).append(note)

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
    {"type": "function", "name": "project_list", "strict": False,
     "description": "List the configured projects (keys, names, repos) the assistant can work on, and show which one is active.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"type": "function", "name": "project_current", "strict": False,
     "description": "Show the currently active project and which GitHub repo the code tools are targeting.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"type": "function", "name": "project_use", "strict": False,
     "description": "Set the active project by its key (e.g. 'vantage', 'card-tracker', 'assistant'). Afterwards the code repo tools target that project's repo. Reading/listing is safe; nothing is written.",
     "parameters": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
    {"type": "function", "name": "linear_list_issues", "strict": False,
     "description": "List recent Linear issues. Safe read-only action.",
     "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}, "required": []}},
    {"type": "function", "name": "linear_search_issues", "strict": False,
     "description": "Search Linear issues by text. Safe read-only action.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}},
    {"type": "function", "name": "linear_get_issue", "strict": False,
     "description": "Read one Linear issue's FULL details, including its description/acceptance criteria, by identifier (e.g. 'VAN-46') or by text. Safe read-only action. Use this to read the full requirements of an issue before implementing it.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"type": "function", "name": "linear_list_teams", "strict": False,
     "description": "List the Linear teams (name, key, and id). Safe read-only action. Useful for finding the team id to configure or create issues in.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"type": "function", "name": "linear_list_projects", "strict": False,
     "description": "List the Linear projects (name and id). Safe read-only action.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"type": "function", "name": "linear_create_issue", "strict": False,
     "description": "Create a Linear issue with a title and optional description. Creates immediately - only call when the user clearly wants a new issue. When several issues would be created at once, ask the user first.",
     "parameters": {"type": "object", "properties": {"title": {"type": "string"}, "description": {"type": "string"}}, "required": ["title"]}},
    {"type": "function", "name": "linear_create_project_issue", "strict": False,
     "description": "Create a Linear issue tied to one of the assistant's projects (by project key, e.g. 'card-tracker'). The issue is tagged with the project key and GitHub repo. Creates immediately - only call on clear user intent; ask first before creating several at once.",
     "parameters": {"type": "object", "properties": {"project_key": {"type": "string"}, "title": {"type": "string"}, "description": {"type": "string"}}, "required": ["project_key", "title"]}},
    {"type": "function", "name": "get_company_status", "strict": False,
     "description": "Read Company Mode's current status: operating mode, today's budget/reserved/spent ledger, and the active project's open tasks.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"type": "function", "name": "get_revenue_report", "strict": False,
     "description": "Pull live Gumroad sales, sync the product registry, and return the company P&L (spend vs revenue per product, plus totals). Reports last-synced numbers with a note if Gumroad isn't configured.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
]

DELEGATION_TOOLS = [
    {"type": "function", "name": "delegate_to_coding_agent", "strict": False,
     "description": "Delegate a programming, code-writing, code-reading, or debugging task to the Coding Agent.",
     "parameters": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}},
    {"type": "function", "name": "delegate_to_research_agent", "strict": False,
     "description": "Delegate a research or information-lookup request to the Researcher Agent.",
     "parameters": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}},
    {"type": "function", "name": "delegate_to_writer_agent", "strict": False,
     "description": "Delegate a writing, drafting, or editing request to the Content Lead.",
     "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]}},
    {"type": "function", "name": "delegate_to_marketing_agent", "strict": False,
     "description": "Delegate positioning, landing-page copy, SEO/keyword research, launch posts, or content-calendar work to the Head of Marketing & Growth.",
     "parameters": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}},
    {"type": "function", "name": "delegate_to_editor_agent", "strict": False,
     "description": "Delegate reviewing a finished deliverable to the Managing Editor for a quality verdict: approved, or a list of required revisions. Use before anything ships to a customer.",
     "parameters": {"type": "object", "properties": {"request": {"type": "string"}}, "required": ["request"]}},
    {"type": "function", "name": "delegate_to_finance_agent", "strict": False,
     "description": "Delegate budget, spend, P&L, or revenue questions about the company to the CFO.",
     "parameters": {"type": "object", "properties": {"request": {"type": "string"}}, "required": ["request"]}},
    {"type": "function", "name": "delegate_to_personal_assistant", "strict": False,
     "description": "Delegate remembering a personal fact or preference in long-term memory, keeping notes, or creating/checking actual to-do items in the user's real Todoist account to the Operations Manager.",
     "parameters": {"type": "object", "properties": {"request": {"type": "string"}}, "required": ["request"]}},
    {"type": "function", "name": "delegate_to_calendar_agent", "strict": False,
     "description": "Delegate anything about the user's Google Calendar (viewing or creating events), setting a time-based reminder/nudge, OR a current weather lookup to the Executive Assistant.",
     "parameters": {"type": "object", "properties": {"request": {"type": "string"}}, "required": ["request"]}},
    {"type": "function", "name": "delegate_to_gmail_agent", "strict": False,
     "description": "Delegate reading, searching, drafting, or sending email in the user's Gmail - including triaging and answering customer-support email - to the Communications & Support Lead.",
     "parameters": {"type": "object", "properties": {"request": {"type": "string"}}, "required": ["request"]}},
    {"type": "function", "name": "delegate_to_linear_agent", "strict": False,
     "description": "Delegate project-management work to the Linear Agent: turning ideas, bugs, sprint plans, or PRD tasks into clean Linear issues, and reading/searching/organizing Linear issues. Use this for anything about Linear or tracking project tasks.",
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
REMINDERS_FILE = DATA_DIR / "reminders.json"

MEMORY_DIR = DATA_DIR / "memory_db"
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
- delegate_to_research_agent: looking up information, facts, or current events -
  including news requests, headline roundups, and source-cited news briefs
- delegate_to_writer_agent: drafting, editing, or improving written content
- delegate_to_marketing_agent: positioning, landing-page copy, SEO/keyword
  research, launch posts, and content calendars
- delegate_to_editor_agent: reviewing a finished deliverable for quality before
  it ships - returns an approval or a list of required revisions
- delegate_to_finance_agent: the company's budget, spend, P&L, and revenue
- delegate_to_personal_assistant: remembering personal facts and preferences in
  long-term memory, keeping notes, AND creating or checking actual to-do items
  in the user's real Todoist account
- delegate_to_calendar_agent: viewing or creating Google Calendar events,
  setting time-based reminders/nudges, and current weather lookups
- delegate_to_gmail_agent: reading, searching, drafting, or sending email,
  including triaging and answering customer-support email
- delegate_to_linear_agent: project management in Linear - turning ideas, bugs,
  sprint plans, or PRD tasks into clean issues, and reading/searching issues
- delegate_to_general_assistant: anything else, or simple questions that don't
  fit a specialist

Most requests need only one delegation. Some requests genuinely need more than
one agent working in sequence - for example "research the competition, then
draft a launch post from the findings" requires delegating to the research
agent first, then to the marketing agent with the research result included in
the prompt you give it. When a request needs more than one step:
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
is a distinct character on the team (Patch codes, Scout researches and covers
the news, Quill writes, Sway drives marketing and growth, Vera reviews
deliverables, Ledger watches the money, Sage runs operations and the task list,
Cadence handles calendar, scheduling and weather, Piper handles email and
customer support) - let their personality come through rather than flattening
everyone into one voice.
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

        _accrue_cost(model, getattr(response, "usage", None))
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
        _accrue_cost(EMBEDDING_MODEL_NAME, getattr(response, "usage", None))
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

            _accrue_cost(model, getattr(response, "usage", None))

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
        else:
            # The for loop ran out of iterations without ever producing a plain text
            # answer (the model kept asking for tools). Rather than discard everything
            # it gathered and return a canned "too many tool calls" failure, make one
            # final call with NO tools so the model is forced to synthesize an answer
            # from what it already has. This turns a wasted, answer-less run (e.g. a
            # research agent that kept searching) into a usable, if partial, reply.
            final_response = call_with_retries(
                lambda: openai_client.responses.create(
                    model=model,
                    instructions=instructions + (
                        "\n\nYou have used all of your tool calls for this turn. Do not "
                        "request any more tools - answer now using what you have already "
                        "gathered, and clearly note anything you could not fully verify."
                    ),
                    input=input_items,
                ),
                label="OpenAI final synthesis call",
            )
            _accrue_cost(model, getattr(final_response, "usage", None))
            if final_response.output_text:
                assistant_response = final_response.output_text

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
/code <task>                - Ask Patch, the Head of Engineering
/research <topic>           - Ask Scout, the Head of Research
/write <prompt>             - Ask Quill, the Content Lead
/task <request>             - Ask Sage, the Operations Manager
/project list               - List configured projects and the active one
/project use <key>          - Set the active project (code tools target its repo)
/project current            - Show the active project and repo target
/project status             - Show the active project + code-repo target
/project commands           - Show the active project's configured commands
/project brainstorm [<key>|current] <idea> - 10 ranked ideas + top 3
/project sprint [<key>|current] <goal>      - 1-week sprint plan (3-7 tasks)
/project prd [<key>|current] <feature>      - Draft a PRD for a feature
/linear teams|projects|issues               - List Linear teams/projects/issues
/linear search <query>      - Search Linear issues by text
/linear create <title>      - Create a Linear issue (tied to active project)
/linear create-project-issue <key> <title>  - Create an issue for a project
/linear from-sprint <key> <goal>            - Generate + create a sprint's issues
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

    # Agents sometimes pass a path that already includes the "files/" prefix (they
    # copy it from an artifact note like "file: files/pack.md"). Left as-is that
    # resolves to files/files/pack.md and 404s. Drop a leading "./" and a single
    # leading "files/" so both "pack.md" and "files/pack.md" work. Everything lives
    # under files/ anyway, and "../" traversal is still caught by the check below.
    cleaned = filename.strip()
    if cleaned.startswith("./") or cleaned.startswith(".\\"):
        cleaned = cleaned[2:]
    if cleaned.lower().startswith("files/") or cleaned.lower().startswith("files\\"):
        cleaned = cleaned[len("files/"):]

    file_path = (FILES_DIR / cleaned).resolve()

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


# Company finance tools (Feature: CFO agent). Lazy imports keep main.py free of a
# module-level company_mode dependency - the same separation group_bot.py relies on.
def get_company_status():
    import company_mode
    try:
        return company_mode.render_company_status()
    except Exception as e:
        logger.error(f"get_company_status failed: {e}")
        return "Sorry, couldn't read Company Mode state."


def get_revenue_report():
    """Same path as the group's /revenue command: live Gumroad pull -> sync_revenue ->
    render_pnl. Degrades to the last-synced P&L plus a skip note when Gumroad is
    unreachable or unconfigured."""
    import company_mode
    import gumroad_helpers
    try:
        products, err = gumroad_helpers.list_products()
        if not err:
            company_mode.sync_revenue(products)
        note = f"\n\n(Live Gumroad sync skipped: {err})" if err else ""
        return company_mode.render_pnl() + note
    except Exception as e:
        logger.error(f"get_revenue_report failed: {e}")
        return "Sorry, couldn't build the revenue report."


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
    context = pending.get("company_context", "")
    if pending["type"] == "write_file":
        description = f"write to files/{pending['filename']}"
        return f"{description} ({context})" if context else description
    if pending["type"] == "send_email":
        description = f"email to {pending['to']}"
        return f"{description} ({context})" if context else description
    if pending["type"] == "github_delete":
        description = f"deletion of {pending['path']} from GitHub"
        return f"{description} ({context})" if context else description
    if pending["type"] == "publish":
        description = f"publishing of {pending.get('title', 'the product')}"
        return f"{description} ({context})" if context else description
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


# --------------------------------------------------------------------------- #
# Multi-project + Linear formatting helpers and planning generator.
# --------------------------------------------------------------------------- #

def format_project_list():
    """Human-readable list of configured projects, marking the active one."""
    pairs = projects.list_projects()
    if not pairs:
        return "No projects configured (projects.json is missing or empty)."
    active_key, _ = projects.get_active_project()
    lines = ["Projects:"]
    for key, profile in pairs:
        marker = " (active)" if key == active_key else ""
        lines.append(f"- {key}{marker}: {profile.get('name', key)} -> {profile.get('repo', '(no repo)')}")
    if not active_key:
        lines.append("\nNo active project selected (code tools use env-var config).")
    return "\n".join(lines)


def format_project_commands():
    """Show the configured commands for the active project."""
    key, profile = projects.get_active_project()
    if not profile:
        return "No active project selected. Use /project use <key> first."
    commands = profile.get("commands", {})
    if not commands:
        return f"{profile.get('name', key)} has no commands configured."
    lines = [f"Commands for {profile.get('name', key)} ({key}):"]
    for label, cmd in commands.items():
        lines.append(f"- {label}: {cmd}")
    return "\n".join(lines)


def format_linear_issues(issues):
    if not issues:
        return "No Linear issues found."
    lines = ["Linear issues:"]
    for it in issues:
        state = (it.get("state") or {}).get("name", "?")
        lines.append(f"- [{it.get('identifier', '?')}] {it.get('title', '(untitled)')} ({state}) {it.get('url', '')}".rstrip())
    return "\n".join(lines)


def format_linear_created(issue):
    ident = issue.get("identifier", "?")
    return f"Created Linear issue {ident}: {issue.get('title', '')}\n{issue.get('url', '')}".rstrip()


def format_linear_issue_detail(issue):
    state = (issue.get("state") or {}).get("name", "?")
    description = issue.get("description") or "(no description)"
    return (f"[{issue.get('identifier', '?')}] {issue.get('title', '(untitled)')} ({state})\n"
            f"{issue.get('url', '')}\n\n{description}").strip()


# System prompts for /project planning commands. Each asks for a specific,
# scannable structure so a sprint plan can feed /linear from-sprint.
_PLAN_INSTRUCTIONS = {
    "brainstorm": (
        "You are a sharp product strategist. Given a project and an idea, return:\n"
        "1) 10 concrete ideas, 2) a ranking by impact, difficulty, and demo value,\n"
        "3) the recommended top 3, and 4) which of them should become Linear issues.\n"
        "Be concise and specific to the project."
    ),
    "sprint": (
        "You are a pragmatic engineering lead. Given a project and a goal, return a\n"
        "1-week sprint plan with 3-7 tasks. For EACH task give: a short title, acceptance\n"
        "criteria, a suggested Linear issue title, and a suggested branch name in the form\n"
        "ai/<project-key>/<short-task>. Number the tasks. Keep it tight and buildable."
    ),
    "prd": (
        "You are a product manager. Given a project and a feature idea, write a short PRD\n"
        "with these sections: Problem, Target user, Proposed solution, User stories, Scope,\n"
        "Non-goals, Acceptance criteria, Technical notes, and a Linear issue breakdown\n"
        "(3-7 issues with titles)."
    ),
}


def generate_plan(kind, project_key, text):
    """Generate a brainstorm/sprint/prd plan for a project. Returns plan text.
    Does not create anything - planning only."""
    instructions = _PLAN_INSTRUCTIONS.get(kind)
    if not instructions:
        return f"Unknown planning kind: {kind}."

    profile = projects.get_project(project_key) if project_key else None
    if profile:
        context = (f"Project: {profile.get('name', project_key)} ({project_key})\n"
                   f"Repo: {profile.get('repo', 'unknown')}\n"
                   f"Type: {profile.get('type', 'unknown')}")
    else:
        context = f"Project key: {project_key or 'current'} (no profile found; use general judgment)."

    prompt = f"{context}\n\n{kind.capitalize()} request:\n{text}"
    return get_ai_response(
        [{"role": "user", "content": prompt}],
        model=PREMIUM_MODEL,
        instructions=instructions,
    )


def _resolve_project_and_text(rest):
    """Split '<key|current> <text>' where the leading key is optional. Returns
    (project_key_or_None, text). If the first word is a known project key it's
    used; 'current' (or a missing/unknown key) falls back to the active project."""
    rest = (rest or "").strip()
    if not rest:
        return None, ""
    first, _, remainder = rest.partition(" ")
    if projects.get_project(first):
        return first, remainder.strip()
    if first.lower() == "current":
        key, _ = projects.get_active_project()
        return key, remainder.strip()
    # No explicit key - use the active project (may be None) and the whole text.
    key, _ = projects.get_active_project()
    return key, rest


def sprint_issue_specs(project_key, goal, max_issues=7):
    """Ask the model for a small set of issues (title + one-line description) for a
    sprint goal, in a parseable format. Returns a list of (title, description)."""
    instructions = (
        "You are an engineering lead. Turn the sprint goal into 3-7 small, buildable "
        "Linear issues. Output ONLY the issues, one per line, in the exact format:\n"
        "TITLE :: one-line description with acceptance criteria\n"
        "No numbering, no headers, no extra prose."
    )
    profile = projects.get_project(project_key) if project_key else None
    context = ""
    if profile:
        context = (f"Project: {profile.get('name', project_key)} ({project_key}), "
                   f"repo {profile.get('repo')}, {profile.get('type', '')}\n")
    raw = get_ai_response(
        [{"role": "user", "content": f"{context}Sprint goal: {goal}"}],
        model=PREMIUM_MODEL,
        instructions=instructions,
    )
    specs = []
    for line in (raw or "").splitlines():
        line = line.strip().lstrip("-*0123456789. ").strip()
        if not line or "::" not in line:
            continue
        title, _, desc = line.partition("::")
        title = title.strip()
        if title:
            specs.append((title, desc.strip()))
        if len(specs) >= max_issues:
            break
    return specs


def _format_named_nodes(nodes, label):
    if not nodes:
        return f"No Linear {label} found."
    lines = [f"Linear {label}:"]
    for n in nodes:
        extra = f" [{n['key']}]" if n.get("key") else ""
        lines.append(f"- {n.get('name', '(unnamed)')}{extra} ({n.get('id', '')})")
    return "\n".join(lines)


def handle_project_command(rest):
    """Handle '/project ...' subcommands. Returns a status string to print."""
    rest = (rest or "").strip()
    if not rest:
        return format_project_list()
    sub, _, arg = rest.partition(" ")
    sub = sub.lower()
    arg = arg.strip()

    if sub == "list":
        return format_project_list()
    if sub == "current":
        return projects.describe_active()
    if sub == "status":
        target = projects.active_code_target()
        target_line = (f"Code tools target: {target[0]} (branch {target[1]})"
                       if target else "Code tools target: env-var config (no active project).")
        return projects.describe_active() + "\n" + target_line
    if sub == "commands":
        return format_project_commands()
    if sub == "use":
        if not arg:
            return "Usage: /project use <key>"
        profile, err = projects.set_active_project(arg)
        if err:
            return err
        return (f"Active project set to {profile.get('name', arg)} ({arg}). "
                f"Code tools now target {profile.get('repo')} "
                f"(branch {profile.get('default_branch', 'main')}).")
    if sub in ("brainstorm", "sprint", "prd"):
        if not arg:
            return f"Usage: /project {sub} <project-key or current> <text>"
        key, text = _resolve_project_and_text(arg)
        if not text:
            return f"Usage: /project {sub} <project-key or current> <text>"
        return generate_plan(sub, key, text)

    return ("Unknown /project subcommand. Try: list, current, status, commands, "
            "use <key>, brainstorm <text>, sprint <goal>, prd <feature>.")


def handle_linear_command(rest):
    """Handle '/linear ...' subcommands. Returns a status string to print."""
    rest = (rest or "").strip()
    if not rest:
        return ("Usage: /linear teams|projects|issues|search <q>|create <title>|"
                "create-project-issue <key> <title>|from-sprint <key> <goal>")
    sub, _, arg = rest.partition(" ")
    sub = sub.lower()
    arg = arg.strip()

    if sub == "teams":
        nodes, err = linear_helpers.list_teams()
        return err if err else _format_named_nodes(nodes, "teams")
    if sub == "projects":
        nodes, err = linear_helpers.list_projects()
        return err if err else _format_named_nodes(nodes, "projects")
    if sub == "issues":
        issues, err = linear_helpers.list_issues()
        return err if err else format_linear_issues(issues)
    if sub == "search":
        if not arg:
            return "Usage: /linear search <query>"
        issues, err = linear_helpers.search_issues(arg)
        return err if err else format_linear_issues(issues)
    if sub == "create":
        if not arg:
            return "Usage: /linear create <title>"
        # Explicit command = intent. If a project is active, tie the issue to it.
        key, _ = projects.get_active_project()
        if key:
            issue, err = linear_helpers.create_project_issue(key, arg)
        else:
            issue, err = linear_helpers.create_issue(arg)
        return err if err else format_linear_created(issue)
    if sub == "create-project-issue":
        key, _, title = arg.partition(" ")
        if not key or not title.strip():
            return "Usage: /linear create-project-issue <project-key> <title>"
        if not projects.get_project(key):
            return f"Unknown project '{key}'. Try /project list."
        issue, err = linear_helpers.create_project_issue(key, title.strip())
        return err if err else format_linear_created(issue)
    if sub == "from-sprint":
        key, _, goal = arg.partition(" ")
        if not key or not goal.strip():
            return "Usage: /linear from-sprint <project-key> <sprint goal>"
        if not projects.get_project(key):
            return f"Unknown project '{key}'. Try /project list."
        if not linear_helpers.is_configured():
            return linear_helpers.NOT_CONFIGURED
        specs = sprint_issue_specs(key, goal.strip())
        if not specs:
            return "Couldn't derive any issues from that sprint goal - try rephrasing."
        results = [f"Creating {len(specs)} Linear issues for {key} from: {goal.strip()}"]
        for title, desc in specs:
            issue, err = linear_helpers.create_project_issue(key, title, desc)
            results.append(f"- {title}: " + (err if err else format_linear_created(issue).replace(chr(10), " ")))
        return "\n".join(results)

    return ("Unknown /linear subcommand. Try: teams, projects, issues, search <q>, "
            "create <title>, create-project-issue <key> <title>, from-sprint <key> <goal>.")


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

        if name == "get_company_status":
            return get_company_status()

        if name == "get_revenue_report":
            return get_revenue_report()

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
                sink = current_execution_sink()
                set_pending_action({
                    "type": "send_email",
                    "to": arguments["to"],
                    "subject": arguments["subject"],
                    "body": arguments["body"],
                    "company_context": sink.get("context", "") if sink else "",
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
            result = github_helpers.save_file(arguments["path"], arguments["content"])
            _record_artifact(f"github: {arguments['path']}")
            return result

        if name == "github_delete_file":
            if CONFIRMATION_MODE == "disabled":
                return "Deleting files is disabled in this interface."

            if CONFIRMATION_MODE == "requires_confirmation":
                sink = current_execution_sink()
                set_pending_action({
                    "type": "github_delete",
                    "path": arguments["path"],
                    "company_context": sink.get("context", "") if sink else "",
                })
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
            result = github_helpers.code_propose_change(
                arguments["branch"], arguments["path"], arguments["content"],
                arguments["title"], arguments.get("body", "")
            )
            _record_artifact(result)
            return result

        if name == "code_edit_file":
            result = github_helpers.code_edit_file(
                arguments["branch"], arguments["path"], arguments["old_snippet"],
                arguments["new_snippet"], arguments["title"], arguments.get("body", "")
            )
            _record_artifact(result)
            return result

        if name == "project_list":
            return format_project_list()

        if name == "project_current":
            return projects.describe_active()

        if name == "project_use":
            profile, err = projects.set_active_project(arguments["key"].strip())
            if err:
                return err
            return (f"Active project set to {profile.get('name', arguments['key'])} "
                    f"({arguments['key'].strip()}). Code tools now target repo "
                    f"{profile.get('repo')} (branch {profile.get('default_branch', 'main')}).")

        if name == "linear_list_issues":
            issues, err = linear_helpers.list_issues(arguments.get("limit", 20))
            return err if err else format_linear_issues(issues)

        if name == "linear_search_issues":
            issues, err = linear_helpers.search_issues(arguments["query"], arguments.get("limit", 20))
            return err if err else format_linear_issues(issues)

        if name == "linear_get_issue":
            issue, err = linear_helpers.get_issue(arguments["query"])
            return err if err else format_linear_issue_detail(issue)

        if name == "linear_list_teams":
            nodes, err = linear_helpers.list_teams()
            return err if err else _format_named_nodes(nodes, "teams")

        if name == "linear_list_projects":
            nodes, err = linear_helpers.list_projects()
            return err if err else _format_named_nodes(nodes, "projects")

        if name == "linear_create_issue":
            issue, err = linear_helpers.create_issue(
                arguments["title"], arguments.get("description", ""))
            return err if err else format_linear_created(issue)

        if name == "linear_create_project_issue":
            issue, err = linear_helpers.create_project_issue(
                arguments["project_key"].strip(), arguments["title"],
                arguments.get("description", ""))
            return err if err else format_linear_created(issue)

        if name == "write_file":
            # During supervised Company Mode execution, writing a file is a *produce*
            # action (a deliverable, reversible, sandboxed) so it runs without a
            # /confirm - but it's recorded as an artifact of the current task.
            if in_company_execution():
                result = write_file(arguments["filename"], arguments["content"])
                _record_artifact(f"file: files/{arguments['filename']}")
                return result

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

        if name == "delegate_to_writer_agent":
            answer = ask_specialist("write", arguments["prompt"], record_history=False)
            if on_delegation:
                on_delegation("write", arguments["prompt"], answer)
            return answer

        if name == "delegate_to_marketing_agent":
            answer = ask_specialist("marketing", arguments["task"], record_history=False)
            if on_delegation:
                on_delegation("marketing", arguments["task"], answer)
            return answer

        if name == "delegate_to_editor_agent":
            answer = ask_specialist("editor", arguments["request"], record_history=False)
            if on_delegation:
                on_delegation("editor", arguments["request"], answer)
            return answer

        if name == "delegate_to_finance_agent":
            answer = ask_specialist("finance", arguments["request"], record_history=False)
            if on_delegation:
                on_delegation("finance", arguments["request"], answer)
            return answer

        if name == "delegate_to_personal_assistant":
            answer = ask_specialist("task", arguments["request"], record_history=False)
            if on_delegation:
                on_delegation("task", arguments["request"], answer)
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

        if name == "delegate_to_linear_agent":
            answer = ask_specialist("linear", arguments["request"], record_history=False)
            if on_delegation:
                on_delegation("linear", arguments["request"], answer)
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
        "label": "Patch (Head of Engineering)",
        "model": PREMIUM_MODEL,
        # Scaffolding a whole new agent means reading a few files and then making many
        # small, precise code_edit_file changes (delegation tool, manager bullet,
        # execute_tool branch, SPECIALISTS entry, group_bot wiring, README...), so give
        # the coding agent a generous tool-call budget - 8 runs out mid-mission.
        "max_iterations": 25,
        "tool_names": ["read_file", "write_file", "search_the_web", "recall_memories", "run_python",
                       "github_list_files", "github_read_file", "github_save_file", "github_delete_file",
                       "code_list_files", "code_read_file", "code_propose_change", "code_edit_file",
                       "linear_get_issue", "linear_search_issues", "linear_list_issues"],
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

IMPORTANT for team projects: when you produce the main deliverable file that other
teammates (Quill, Sage, Sway, Vera) will build on, save it with write_file, NOT
github_save_file. write_file saves it locally AND mirrors it to GitHub, so teammates
who only have local file access can actually read and extend it. Use github_save_file
only for extra repo files teammates won't need to open.

If a task calls for a payment or checkout link (e.g. Gumroad, Stripe) and no real
product exists yet, do NOT invent a URL that looks live - these platforms have no
API to create a product, so such a link only becomes real once the user creates it
by hand in that platform's dashboard. Use an obvious placeholder instead (e.g.
CHECKOUT_URL = "REPLACE_WITH_REAL_GUMROAD_LINK") and say plainly in your summary
that the checkout is not yet live.

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

When a task references a Linear issue (e.g. "VAN-46"), read it first with
linear_get_issue to get the full description and acceptance criteria, then implement
exactly that scope. linear_search_issues / linear_list_issues are available too.
These are read-only; you don't create or edit Linear issues (that's the Linear agent's
job).
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
        "label": "Scout (Head of Research)",
        "model": PREMIUM_MODEL,
        # Research is search-heavy, so give Scout more headroom than the default 10 -
        # but the role below tells it to converge, and run_with_tools now forces a
        # final synthesis if it still runs out, so it can't return an empty failure.
        "max_iterations": 15,
        "tool_names": ["search_the_web", "recall_memories", "remember_fact"],
        "role": """
You are a thorough research assistant. Use search_the_web to find current,
accurate information and cite your sources. Use remember_fact to save important
findings for later, and recall_memories to check what's already been researched
before searching again. Be clear about what is verified fact versus speculation.

Be decisive: run at most 3-4 web searches, then STOP searching and write your
answer from what you found. Do not keep re-searching slight variations of the same
query - if a couple of searches don't fully answer it, say what you found and flag
what's still uncertain rather than searching again.

You also run the news desk. For current-events requests, deliver a headline-led
brief: lead with the headline, group updates by topic, cite the source for each
claim, and separate verified reporting from uncertainty, early reports, or
analysis. Do not invent details beyond the sources you found.
""",
        "persona": """
You are Scout, the team's researcher. Voice: curious, energetic fact-hound who
loves a good source and always says where a claim came from. You clearly label
what's "verified" versus "unconfirmed". Sign off with "- Scout".
"""
    },
    "write": {
        "name": "Quill",
        "label": "Quill (Content Lead)",
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
        "label": "Sage (Operations Manager)",
        "model": FAST_MODEL,
        "tool_names": ["remember_fact", "recall_memories", "write_file", "read_file",
                       "create_task", "list_tasks"],
        "role": """
You are the team's operations manager: you keep both the user's context and their
real to-do list tidy. Use remember_fact to save important personal information and
preferences, and recall_memories to recall them later. Use write_file/read_file to
maintain simple notes when asked. You also manage the user's real Todoist account:
create_task to add to-do items and list_tasks to see what's currently open - check
list_tasks before adding something that might already be there. Be concise and
proactive.
""",
        "persona": """
You are Sage, the team's operations manager. Voice: calm, organized, quietly
proactive - you remember what matters to the user, keep the task list honest, and
confirm exactly what landed where. Sign off with "- Sage".
"""
    },
    "calendar": {
        "name": "Cadence",
        "label": "Cadence (Executive Assistant - calendar, scheduling & weather)",
        "model": FAST_MODEL,
        "tool_names": ["list_calendar_events", "create_calendar_event", "set_reminder", "list_reminders",
                       "get_weather"],
        "role": """
You manage the user's Google Calendar and their reminders. Use list_calendar_events
to see what's scheduled, create_calendar_event to add events, set_reminder to
schedule a one-off nudge at a specific time, and list_reminders to review pending
ones. The current date and time is given at the top of each message - use it to turn
relative times ("tomorrow at 3", "in two hours") into exact ISO 8601 timestamps.
Confirm what you scheduled, clearly and briefly. You also cover weather: use
get_weather for current conditions when asked, or when it informs scheduling advice.
""",
        "persona": """
You are Cadence, the team's calendar and scheduling specialist. Voice: unflappable
and precise; you keep everyone on time without nagging. Sign off with "- Cadence".
"""
    },
    "gmail": {
        "name": "Piper",
        "label": "Piper (Communications & Support Lead)",
        "model": FAST_MODEL,
        "tool_names": ["search_emails", "read_email", "draft_email", "send_email", "recall_memories"],
        "role": """
You manage the user's Gmail. Use search_emails to find messages, read_email to read
one in full, draft_email to prepare a reply or new message (it just waits in Drafts),
and send_email to send. Sending is sensitive: prefer drafting unless the user clearly
asks to send, and note that sending will ask them to confirm first. Summarize emails
crisply and never invent contents you haven't actually read.

You also handle customer support for the company's products. When triaging an
inbound customer email, identify what the customer needs, how urgent it is, and
what kind of message it is (question, bug report, refund request, complaint). Use
recall_memories to check for saved reply templates and tone guidance before
drafting. Draft replies rather than sending, and escalate to the user - clearly
flagged - anything involving refunds, legal risk, or an angry customer.
""",
        "persona": """
You are Piper, the team's communications and support lead. Voice: brisk, discreet,
and organized - you triage a busy inbox without drama and keep customers feeling
heard. Sign off with "- Piper".
"""
    },
    "marketing": {
        "name": "Sway",
        "label": "Sway (Head of Marketing & Growth)",
        "model": PREMIUM_MODEL,
        "tool_names": ["search_the_web", "read_file", "write_file", "recall_memories", "remember_fact"],
        "role": """
You own the company's marketing: positioning, landing-page copy, SEO and keyword
research, launch posts, and content calendars. Use search_the_web for keyword and
competitor research, read_file to review the actual product or deliverable before
you market it, and write_file to save finished marketing assets. Use remember_fact
to keep positioning, audience, and brand-voice decisions consistent over time, and
recall_memories to check them before writing. Ground every claim in what the
product actually does - never invent features or results.
""",
        "persona": """
You are Sway, the team's head of marketing and growth. Voice: sharp and benefit-led,
allergic to hype without proof. You always name the target customer and the one
message that matters most. Sign off with "- Sway".
"""
    },
    "editor": {
        "name": "Vera",
        "label": "Vera (Managing Editor)",
        "model": PREMIUM_MODEL,
        "tool_names": ["read_file", "search_the_web", "recall_memories"],
        "role": """
You are the team's final quality gate: you review finished deliverables before they
ship to a customer. Always read the actual deliverable with read_file - never review
from a description alone. Use search_the_web to spot-check factual claims and
recall_memories for the project's context and requirements. Grade against this
checklist: (1) are claims sourced or verifiable, (2) does the format match what was
asked for, (3) is it complete and self-contained, (4) is it good enough for a paying
customer. Your verdict is binary: "APPROVED" with a one-line reason, or "REVISIONS
REQUIRED" with a numbered list of specific fixes. You never rewrite the work
yourself - you say exactly what must change and who should change it.
""",
        "persona": """
You are Vera, the team's managing editor. Voice: exacting but fair - every note
names the exact spot and the exact fix, and praise is earned, not padded. Sign off
with "- Vera".
"""
    },
    "finance": {
        "name": "Ledger",
        "label": "Ledger (CFO)",
        "model": FAST_MODEL,
        "tool_names": ["get_company_status", "get_revenue_report", "recall_memories", "remember_fact"],
        "role": """
You own the company's money picture: the daily budget, spend, and revenue. Use
get_company_status for today's budget ledger and the active project's open work,
and get_revenue_report for live sales and per-product P&L (it notes when Gumroad
isn't configured). Report numbers exactly as the tools return them - never estimate
or round beyond what they show. Flag plainly when a product is unprofitable or when
today's spend is close to the budget cap. Use remember_fact to record financial
decisions worth keeping and recall_memories to check them.
""",
        "persona": """
You are Ledger, the team's CFO. Voice: dry, numerate, unhurried. Lead with the
number, then the one-sentence takeaway. Sign off with "- Ledger".
"""
    },
    "linear": {
        "name": "Linear",
        "label": "Linear (Project Management Agent)",
        "model": PREMIUM_MODEL,
        "tool_names": ["linear_list_issues", "linear_search_issues", "linear_get_issue",
                       "linear_list_teams", "linear_list_projects", "linear_create_issue",
                       "linear_create_project_issue", "project_list", "project_current",
                       "code_read_file", "recall_memories"],
        "role": """
You turn rough ideas, bugs, sprint plans, and PRD tasks into clean, well-scoped
Linear issues. Each issue you create should have a clear title, acceptance
criteria, brief implementation notes, and (when relevant) the GitHub repo/project
context. Use linear_list_issues / linear_search_issues to check what already
exists before creating duplicates. Break a feature into 3-7 issues - not more.
Use linear_create_project_issue when a project key is known (it tags the issue
with the project and repo); use linear_create_issue otherwise.

SAFETY: creating issues is a real write. Create a single issue when the user
clearly asks for one. Before creating MORE than a couple issues at once (e.g. a
whole sprint), first show the user the proposed titles and ask them to confirm.
Reading and searching is always fine. If Linear isn't configured, say which env
var is missing rather than pretending it worked.
""",
        "persona": """
You are Linear, the team's project-management specialist. Voice: crisp, organized,
outcome-focused - you think in scopes and acceptance criteria. You never spam the
board; you confirm before creating a batch. Sign off with "- Linear".
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
        _accrue_cost(FAST_MODEL, getattr(response, "usage", None))
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


def plan_company_goal(goal, available_keys):
    """Turn a Company Mode goal into a tailored work plan instead of the same fixed
    4 tasks every time. Miles reads the goal and returns a list of (owner, title)
    tuples using only the available specialist agents - picking the right ones, in a
    sensible order, however many the goal actually needs. Returns None on any error
    or empty result, so company_mode.assign_goal falls back to its default plan and
    /assign never breaks."""
    menu = "\n".join(
        f"- {key}: {SPECIALISTS[key]['label']}" for key in available_keys if key in SPECIALISTS
    )
    instructions = f"""You are Miles, a startup COO planning the work for a goal the founder
just handed you. Break the goal into a short, tailored work plan and assign each task to the
best-fit teammate. Use ONLY these agents:
{menu}

Rules:
- Pick only the agents the goal actually needs. A simple goal may need just 1-2 tasks; a full
  new product may need 4-5. Do NOT force every agent in.
- Order tasks so earlier results feed later ones (e.g. research -> build -> copy -> checklist).
- Each task is ONE specific, concrete instruction written for that agent.
- Prefer 'code' (Patch) for building the actual downloadable asset/file, 'write' (Quill) for
  long-form copy, 'marketing' (Sway) for positioning/SEO/launch content, 'research' (Scout)
  for validation and current-events angles, 'task' (Sage) for an operational checklist,
  'finance' (Ledger) for a budget or P&L check.
- Unless the goal is trivial, END the plan with ONE 'editor' (Vera) task that reviews the
  finished deliverables against the goal and either approves them or lists required
  revisions - nothing ships unreviewed.

Reply with ONLY a JSON object: {{"tasks": [{{"owner": "<agent key>", "title": "<task>"}}, ...]}}."""

    try:
        openai_client = get_openai_client()
        response = call_with_retries(
            lambda: openai_client.responses.create(
                model=FAST_MODEL,
                instructions=instructions,
                input=[{"role": "user", "content": goal}],
            ),
            label="Company planner call",
        )
        _accrue_cost(FAST_MODEL, getattr(response, "usage", None))
        raw = response.output_text.strip()

        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            raise ValueError(f"no JSON object in planner output: {raw!r}")

        parsed = json.loads(raw[start:end + 1])
        plan = []
        for item in parsed.get("tasks", []):
            owner = item.get("owner")
            title = (item.get("title") or "").strip()
            if owner in available_keys and title:
                plan.append((owner, title))

        if plan:
            return plan
        raise ValueError(f"planner produced no valid tasks: {parsed!r}")

    except Exception as e:
        logger.error(f"Company planner failed, using default plan: {e}")
        return None


NEXT_MOVE_FALLBACK = (
    "Next move: if a product has sales, drive more traffic to it before building "
    "anything new; if nothing's selling yet, ship one more small product for your "
    "strongest audience. - Miles"
)


def recommend_next_move(pnl_summary):
    """Given the company's product P&L, have Miles recommend ONE concrete next move
    (build what, double down, sunset, or drive traffic). Returns a short string; a
    plain heuristic fallback on any error so it never breaks a report."""
    instructions = (
        "You are Miles, a startup COO. Given the company's product P&L below, recommend "
        "ONE concrete next move in 2-3 sentences: what to build, what to double down on, "
        "what to sunset, or whether the priority is driving traffic to a product that "
        "already sells. Be specific and decisive. Sign off '- Miles'."
    )
    try:
        openai_client = get_openai_client()
        response = call_with_retries(
            lambda: openai_client.responses.create(
                model=FAST_MODEL,
                instructions=instructions,
                input=[{"role": "user", "content": pnl_summary}],
            ),
            label="Next-move recommendation call",
        )
        _accrue_cost(FAST_MODEL, getattr(response, "usage", None))
        text = (response.output_text or "").strip()
        if text:
            return text
    except Exception as e:
        logger.error(f"recommend_next_move failed: {e}")
    return NEXT_MOVE_FALLBACK


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

    if command == "/project" or command.startswith("/project "):
        print(handle_project_command(user_prompt[len("/project"):]))
        return True

    if command == "/linear" or command.startswith("/linear "):
        print(handle_linear_command(user_prompt[len("/linear"):]))
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
