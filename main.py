import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

import chromadb
from dotenv import load_dotenv
from openai import OpenAI
from tavily import TavilyClient


# Windows consoles default to a limited encoding (cp1252) that can't print
# every Unicode character (e.g. em dashes, curly quotes) - web search results
# and AI output can easily contain these, so force UTF-8 output to avoid crashes.
sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

client = OpenAI()
tavily_client = TavilyClient()

conversation_history = []

MODEL_NAME = "gpt-5.5"
EMBEDDING_MODEL_NAME = "text-embedding-3-small"
MAX_TOOL_ITERATIONS = 5

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
]

BASE_DIR = Path(__file__).parent
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


def get_ai_response(input_messages):
    try:
        response = client.responses.create(
            model=MODEL_NAME,
            instructions=ASSISTANT_INSTRUCTIONS,
            input=input_messages
        )

        return response.output_text

    except Exception:
        return "Sorry, something went wrong while contacting the AI service. Check your API key, internet connection, or account billing."


def get_embedding(text):
    try:
        response = client.embeddings.create(model=EMBEDDING_MODEL_NAME, input=text)
        return response.data[0].embedding

    except Exception:
        print("Sorry, something went wrong while embedding text for memory.")
        return None


def ask_ai(prompt):
    memories = recall_memories(prompt, n_results=3)

    if len(memories) > 0:
        memory_text = "\n".join(f"- {memory['text']}" for memory in memories)
        augmented_prompt = f"""
Relevant memories from earlier conversations:
{memory_text}

Current message:
{prompt}
"""
    else:
        augmented_prompt = prompt

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

    assistant_response = "Sorry, I tried too many tool calls without finishing. Please try rephrasing."

    try:
        for _ in range(MAX_TOOL_ITERATIONS):
            response = client.responses.create(
                model=MODEL_NAME,
                instructions=ASSISTANT_INSTRUCTIONS,
                input=input_items,
                tools=TOOLS
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
/quit                       - Exit the assistant

Anything else will be sent to the AI.
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


def write_file(filename, content):
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


def search_web(query):
    try:
        response = tavily_client.search(query, max_results=5)
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


def execute_tool(name, arguments):
    print(f"\n[tool] {name}({arguments})")

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

    if name == "write_file":
        answer = input(f"The AI wants to write to files/{arguments['filename']}. Allow? (y/n): ")

        if answer.strip().lower() != "y":
            return "The user denied permission to write this file."

        return write_file(arguments["filename"], arguments["content"])

    return f"Unknown tool: {name}"


def main():
    while True:
        user_prompt = input("\nAsk the AI something, or type 'quit' to exit: ")

        if user_prompt.strip() == "":
            print("Please type something before pressing Enter.")
            continue

        command = user_prompt.lower().strip()

        if command == "/help":
            show_help()
            continue

        if command == "/clear":
            conversation_history.clear()
            print("Conversation memory cleared.")
            continue

        if command in ["quit", "/quit"]:
            print("Goodbye!")
            break

        if command == "/history":
            show_history()
            continue

        if command.startswith("/read "):
            filename = user_prompt[6:].strip()
            read_file(filename)
            continue

        if command.startswith("/askfile "):
            parts = user_prompt.split(" ", 2)

            if len(parts) < 3:
                print("Usage: /askfile <filename> <question>")
                continue

            filename = parts[1]
            question = parts[2]

            ask_file(filename, question)
            continue

        if command.startswith("/search "):
            query = user_prompt[8:].strip()

            if query == "":
                print("Usage: /search <query>")
                continue

            ask_search(query)
            continue

        if command.startswith("/remember "):
            fact = user_prompt[10:].strip()

            if fact == "":
                print("Usage: /remember <fact>")
                continue

            remember_fact(fact)
            continue

        if command.startswith("/recall "):
            query = user_prompt[8:].strip()

            if query == "":
                print("Usage: /recall <query>")
                continue

            show_recall(query)
            continue

        answer = ask_ai(user_prompt)

        print()
        print("AI response:")
        print(answer)


if __name__ == "__main__":
    main()