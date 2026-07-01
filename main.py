import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import chromadb
import requests
from dotenv import load_dotenv
from openai import OpenAI
from tavily import TavilyClient


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

client = OpenAI()
tavily_client = TavilyClient()

TODOIST_API_TOKEN = os.environ["TODOIST_API_TOKEN"]
TODOIST_HEADERS = {"Authorization": f"Bearer {TODOIST_API_TOKEN}"}

OPENWEATHER_API_KEY = os.environ["OPENWEATHER_API_KEY"]

conversation_history = []

MODEL_NAME = "gpt-5.5"
EMBEDDING_MODEL_NAME = "text-embedding-3-small"
MAX_TOOL_ITERATIONS = 5
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
]

DELEGATION_TOOLS = [
    {"type": "function", "name": "delegate_to_coding_agent", "strict": False,
     "description": "Delegate a programming, code-writing, code-reading, or debugging task to the Coding Agent.",
     "parameters": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}},
    {"type": "function", "name": "delegate_to_research_agent", "strict": False,
     "description": "Delegate a research or information-lookup request to the Researcher Agent.",
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
    {"type": "function", "name": "delegate_to_general_assistant", "strict": False,
     "description": "Delegate anything that doesn't clearly fit a specialist to the General Assistant.",
     "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]}},
]

FILES_DIR = BASE_DIR / "files"

MEMORY_DIR = BASE_DIR / "memory_db"
chroma_client = chromadb.PersistentClient(path=str(MEMORY_DIR))
memory_collection = chroma_client.get_or_create_collection(name="long_term_memory")

ASSISTANT_INSTRUCTIONS = """
You are Tyler's beginner-friendly AI assistant.
Be honest about uncertainty.
If the user asks for current, live, recent, or real-time information,
and you do not have a tool for it, say that you cannot verify it yet.
Keep explanations clear and concise.
"""

MANAGER_INSTRUCTIONS = """
You are a manager agent. Your job is to read the user's request and get it done
by delegating to one or more of the following agents using tool calls - never
answer the user directly yourself:
- delegate_to_coding_agent: programming, code-writing, code-reading, or debugging
- delegate_to_research_agent: looking up information, facts, or current events
- delegate_to_writer_agent: drafting, editing, or improving written content
- delegate_to_personal_assistant: remembering personal facts, preferences, and
  reminders in long-term memory - NOT a real task-tracking app
- delegate_to_tasks_agent: creating or checking actual to-do items in the user's
  real Todoist account - not general reminders or preferences (that's
  delegate_to_personal_assistant)
- delegate_to_weather_agent: current weather for a location
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
relay it, with at most one short framing sentence.
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


def get_ai_response(input_messages):
    try:
        response = call_with_retries(
            lambda: client.responses.create(
                model=MODEL_NAME,
                instructions=ASSISTANT_INSTRUCTIONS,
                input=input_messages
            ),
            label="OpenAI chat call"
        )

        return response.output_text

    except Exception:
        return "Sorry, something went wrong while contacting the AI service. Check your API key, internet connection, or account billing."


def get_embedding(text):
    try:
        response = call_with_retries(
            lambda: client.embeddings.create(model=EMBEDDING_MODEL_NAME, input=text),
            label="OpenAI embedding call"
        )
        return response.data[0].embedding

    except Exception:
        print("Sorry, something went wrong while embedding text for memory.")
        return None


def build_augmented_prompt(prompt):
    memories = recall_memories(prompt, n_results=3)

    if len(memories) == 0:
        return prompt

    memory_text = "\n".join(f"- {memory['text']}" for memory in memories)
    return f"""
Relevant memories from earlier conversations:
{memory_text}

Current message:
{prompt}
"""


def run_with_tools(instructions, input_items, tools, max_iterations=MAX_TOOL_ITERATIONS):
    assistant_response = "Sorry, I tried too many tool calls without finishing. Please try rephrasing."

    try:
        for _ in range(max_iterations):
            response = call_with_retries(
                lambda: client.responses.create(
                    model=MODEL_NAME,
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
        # Real history keeps the plain prompt; only this one-off call sees the
        # memory-augmented version, so /history never shows the injected memories.
        conversation_history.append({
            "role": "user",
            "content": prompt
        })

        input_items = conversation_history[:-1] + [{
            "role": "user",
            "content": augmented_prompt
        }]
    else:
        # Called as a Manager delegation target - the Manager logs the real
        # conversation_history entry itself, using the user's verbatim message.
        input_items = [{"role": "user", "content": augmented_prompt}]

    assistant_response = run_with_tools(ASSISTANT_INSTRUCTIONS, input_items, TOOLS)

    if record_history:
        conversation_history.append({
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
    if len(conversation_history) == 0:
        print("Conversation history is empty.")
        return

    print("\nConversation history:")

    for message in conversation_history:
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

    content = file_path.read_text(encoding="utf-8")

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
    return f"Saved to {filename}."


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

    content = file_path.read_text(encoding="utf-8")

    prompt = f"""
Use the file content below to answer the user's question.

File name: {filename}

File content:
{content}

User question:
{question}
"""

    temporary_history = conversation_history + [{
        "role": "user",
        "content": prompt
    }]

    answer = get_ai_response(temporary_history)

    conversation_history.append({
        "role": "user",
        "content": f"Asked about file {filename}: {question}"
    })

    conversation_history.append({
        "role": "assistant",
        "content": answer
    })

    print()
    print("AI response:")
    print(answer)


def create_task(content):
    try:
        # Todoist's REST API v2 (/rest/v2/...) is deprecated as of this writing -
        # confirmed directly against the real API, not assumed from documentation,
        # since it returned a 410 Gone pointing to the newer /api/v1/ endpoints.
        response = call_with_retries(
            lambda: requests.post(
                "https://api.todoist.com/api/v1/tasks",
                headers=TODOIST_HEADERS,
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
        return "Sorry, something went wrong while creating the task."


def list_tasks():
    try:
        response = call_with_retries(
            lambda: requests.get(
                "https://api.todoist.com/api/v1/tasks",
                headers=TODOIST_HEADERS,
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
        return "Sorry, something went wrong while listing tasks."


def get_weather(location):
    try:
        response = call_with_retries(
            lambda: requests.get(
                "https://api.openweathermap.org/data/2.5/weather",
                params={"q": location, "appid": OPENWEATHER_API_KEY, "units": "imperial"},
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
        return f"Sorry, something went wrong while getting the weather for {location}."


def search_web(query):
    try:
        response = call_with_retries(
            lambda: tavily_client.search(query, max_results=5),
            label="Tavily search call"
        )
        return response["results"]

    except Exception:
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

    temporary_history = conversation_history + [{
        "role": "user",
        "content": prompt
    }]

    answer = get_ai_response(temporary_history)

    conversation_history.append({
        "role": "user",
        "content": f"Searched the web for: {query}"
    })

    conversation_history.append({
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


# Controls how the write_file tool gets approved, since different interfaces have
# different ways (or no way) to ask for confirmation. One of:
#   "enabled"               - ask via input() and write immediately (CLI default)
#   "disabled"               - refuse all writes outright
#   "requires_confirmation"  - no real terminal to read input() from (e.g. the
#                              Telegram bot) - stage the write in `pending_write`
#                              instead and tell the caller how to confirm it later
WRITE_FILE_MODE = "enabled"

# Holds {"filename": ..., "content": ...} for at most one write awaiting
# out-of-band confirmation (used by WRITE_FILE_MODE == "requires_confirmation"),
# else None. A single global is fine here - this is a personal, single-user app.
pending_write = None

# Set by group_bot.py to a function (specialist_key, request_text, answer_text) -> None.
# Called only from execute_tool's delegate_to_* branches (genuine Manager-mediated
# delegation), never from ask_specialist/ask_ai themselves - direct @mention calls in
# the group bypass the Manager entirely and shouldn't trigger a fake "delegating..."
# announcement. None (the default) is a no-op for the CLI and the single-bot bot.py.
on_delegation = None


def execute_tool(name, arguments):
    print(f"\n[tool] {name}({arguments})")
    logger.info(f"Tool call: {name}({arguments})")

    try:
        if name == "read_file":
            file_path = get_safe_file_path(arguments["filename"])

            if file_path is None:
                return "Access denied. You can only read files inside the files folder."

            if not file_path.exists() or not file_path.is_file():
                return f"File not found: {arguments['filename']}"

            return file_path.read_text(encoding="utf-8")

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

        if name == "write_file":
            if WRITE_FILE_MODE == "disabled":
                return "File writing is disabled in this interface."

            file_path = get_safe_file_path(arguments["filename"])
            file_exists = file_path is not None and file_path.exists()
            overwrite_note = f" This will OVERWRITE the existing files/{arguments['filename']}." if file_exists else ""

            if WRITE_FILE_MODE == "requires_confirmation":
                global pending_write
                pending_write = {"filename": arguments["filename"], "content": arguments["content"]}
                logger.info(f"Staged write to {arguments['filename']}, awaiting out-of-band confirmation")
                return (
                    f"Write to files/{arguments['filename']} is staged and waiting for your "
                    f"confirmation.{overwrite_note} Reply /confirm to write it, or anything "
                    f"else to cancel it."
                )

            # WRITE_FILE_MODE == "enabled" - CLI path, unchanged from before
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


SPECIALISTS = {
    "code": {
        "label": "Coding Agent",
        "tool_names": ["read_file", "write_file", "search_the_web", "recall_memories"],
        "instructions": """
You are a careful coding assistant. Help the user write, read, and debug code.
Use write_file to save code you're asked to create or change, and read_file to
check existing files before editing them. Use search_the_web if you need to look
up an error or current documentation. Explain your reasoning briefly and prefer
simple, correct solutions over clever ones.
"""
    },
    "research": {
        "label": "Researcher Agent",
        "tool_names": ["search_the_web", "recall_memories", "remember_fact"],
        "instructions": """
You are a thorough research assistant. Use search_the_web to find current,
accurate information and cite your sources. Use remember_fact to save important
findings for later, and recall_memories to check what's already been researched
before searching again. Be clear about what is verified fact versus speculation.
"""
    },
    "write": {
        "label": "Writer Agent",
        "tool_names": ["read_file", "write_file", "recall_memories"],
        "instructions": """
You are a skilled writing assistant. Help the user draft, edit, and improve
written content. Use read_file to review an existing draft before editing it,
and write_file to save a finished draft when asked. Match the tone the user
requests and keep writing clear.
"""
    },
    "task": {
        "label": "Personal Assistant Agent",
        "tool_names": ["remember_fact", "recall_memories", "write_file", "read_file"],
        "instructions": """
You are an organized personal assistant. Use remember_fact to save important
personal information, reminders, and preferences, and recall_memories to recall
them later. Use write_file to maintain simple notes when asked. You are NOT a
real task-tracking app - for actual to-do items, that's the Tasks Agent's job,
not yours. Be concise and proactive.
"""
    },
    "tasks": {
        "label": "Tasks Agent",
        "tool_names": ["create_task", "list_tasks"],
        "instructions": """
You are a task management assistant connected to the user's real Todoist
account. Use create_task to add new tasks and list_tasks to see what's
currently open before adding duplicates or when asked what's on the list.
You manage a real external to-do list - this is different from the Personal
Assistant Agent, which only saves general facts/reminders to memory. Be concise.
"""
    },
    "weather": {
        "label": "Weather Agent",
        "tool_names": ["get_weather"],
        "instructions": """
You are a weather assistant. Use get_weather to look up current conditions
for a location the user asks about. Report temperature and conditions
briefly and clearly. If the location is ambiguous (e.g. multiple cities
share a name), ask which one or state your assumption.
"""
    },
}


def ask_specialist(specialist_key, prompt, record_history=True):
    profile = SPECIALISTS[specialist_key]
    specialist_tools = [tool for tool in TOOLS if tool["name"] in profile["tool_names"]]

    augmented_prompt = build_augmented_prompt(prompt)
    input_items = [{"role": "user", "content": augmented_prompt}]

    answer = run_with_tools(profile["instructions"], input_items, specialist_tools)

    if record_history:
        conversation_history.append({
            "role": "user",
            "content": f"Asked {profile['label']}: {prompt}"
        })

        conversation_history.append({
            "role": "assistant",
            "content": answer
        })

        store_memory(f"Asked {profile['label']}: {prompt}\n{profile['label']} replied: {answer[:200]}", source="chat")

    print()
    print(f"{profile['label']} response:")
    print(answer)

    return answer


def ask_manager(prompt):
    input_items = [{"role": "user", "content": prompt}]
    answer = run_with_tools(MANAGER_INSTRUCTIONS, input_items, DELEGATION_TOOLS, max_iterations=MAX_MANAGER_TOOL_ITERATIONS)

    # The Manager owns the conversation_history record (using the user's literal
    # message), not the delegated specialist/general assistant, so /history always
    # reflects what the user actually typed rather than the Manager's tool-call
    # phrasing of the delegated task.
    conversation_history.append({"role": "user", "content": prompt})
    conversation_history.append({"role": "assistant", "content": answer})
    store_memory(f"User said: {prompt}\nAssistant replied: {answer[:200]}", source="chat")

    print()
    print("Manager response:")
    print(answer)

    return answer


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
        conversation_history.clear()
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