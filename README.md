# Tyler AI Assistant

A beginner-friendly command-line AI assistant built with Python and the OpenAI API.

## Features

- Chat with an AI assistant from the terminal
- Short-term conversation memory during a session
- Long-term memory that survives across restarts, using a local vector database
  (see "How long-term memory works" below)
- Read files from a sandboxed `files/` folder and ask questions about them
- Search the web (via Tavily) and get an AI-summarized answer with sources
- Plain chat can autonomously use tools (read a file, search the web, save/recall a
  memory, write a file) without you needing to type a command — see "How automation
  works" below
- `/help` command
- `/clear` command to reset short-term memory (long-term memory is untouched)
- `/history` command to view current short-term memory
- `/read <filename>` command to print a file from `files/`
- `/askfile <filename> <question>` command to ask a question about a file
- `/search <query>` command to search the web and get a summarized answer
- `/remember <fact>` command to explicitly save something to long-term memory
- `/recall <query>` command to see what long-term memory has stored about a topic
- Four specialist agents, each with its own role and curated tool access — see
  "Specialist agents" below
- `/quit` command to exit
- API keys stored safely in a `.env` file (never committed to git)

## Setup

Create a virtual environment:

```powershell
python -m venv .venv
```

Activate it:

```powershell
.venv\Scripts\Activate.ps1
```

Install dependencies:

```powershell
pip install -r requirements.txt
```

Create a `.env` file in the project root with your API keys:

```
OPENAI_API_KEY=your-openai-key-here
TAVILY_API_KEY=your-tavily-key-here
```

- Get an OpenAI key from https://platform.openai.com
- Get a free Tavily key (used for `/search`) from https://tavily.com

## Usage

Run the assistant:

```powershell
python main.py
```

Type a message and press Enter to chat with the AI, or use one of the commands above.
Files you want to `/read` or `/askfile` must live inside the `files/` folder — paths
outside it are rejected for safety.

## How long-term memory works

Unlike `conversation_history` (a plain in-memory list that's lost when you `/quit`),
long-term memory is stored in a local [Chroma](https://www.trychroma.com/) vector
database in the `memory_db/` folder. It survives across separate runs of the program.

- Every plain chat message and `/remember <fact>` gets converted into an embedding
  (via OpenAI's `text-embedding-3-small` model — no new API key needed, it reuses
  `OPENAI_API_KEY`) and stored.
- Before answering a plain chat message, the assistant automatically searches
  long-term memory for related past memories and includes them as context — so it can
  recall facts from previous sessions without you asking explicitly.
- `/recall <query>` lets you inspect this directly: it shows the raw memories Chroma
  thinks are most similar to your query, along with a "distance" score (lower = more
  similar). There's no relevance cutoff, so even an unrelated memory can show up if
  the store doesn't have anything better — `/recall` exists so you can see that
  happening instead of it being a black box.
- `memory_db/` is gitignored (it's local generated data, like `.venv/`).

## How automation works

Plain chat messages (anything you type that *isn't* a `/command`) let the AI decide
for itself which capability to use, instead of you having to know the right command:

- **Read a file, search the web, save a memory, or recall a memory** — the AI calls
  the same underlying functions the slash commands use, but decides on its own when
  they're needed. For example, "what's in sample.txt?" triggers a file read with no
  `/read` needed.
- **Write a file** — a new capability (`write_file`), sandboxed to the `files/` folder
  just like reading. Because this is the one capability that changes something on
  disk, the assistant always asks for confirmation first: `Allow? (y/n)`. Answering
  anything other than `y` cancels the write.
- Every autonomous tool call prints a `[tool] name(arguments)` line first, so you can
  always see what the AI is doing and why — it's never a silent black box.
- Slash commands (`/read`, `/askfile`, `/search`, `/remember`, `/recall`) still work
  exactly as before — they're deterministic and don't involve the AI deciding anything.
  Use them when you want to do exactly one specific thing; use plain chat when you want
  the assistant to figure out what's needed on its own.

## Specialist agents

Beyond the general assistant, four specialist commands each use the same tool-calling
machinery as plain chat, but with a different system prompt and a **curated subset**
of tools — not every specialist can do everything:

| Command | Specialist | Tools it has |
|---|---|---|
| `/code <task>` | Coding Agent | read file, write file, search the web, recall memory |
| `/research <topic>` | Researcher Agent | search the web, recall memory, remember a fact |
| `/write <prompt>` | Writer Agent | read file, write file, recall memory |
| `/task <request>` | Personal Assistant Agent | remember a fact, recall memory, write file, read file |

This is deliberate: for example, the Researcher Agent *cannot* call `write_file` even
if asked to — it genuinely doesn't have access to that tool, so it'll tell you it can't
and offer the content for you to save yourself, rather than faking it. Scoping each
agent to only the tools its role needs is a basic safety practice (least privilege),
not just an organizational nicety.

All specialists share the same `conversation_history` and long-term memory as plain
chat and each other — there's one memory store for the whole assistant, not one per
agent. A fact saved via `/task` (Personal Assistant) can be recalled later in plain
chat or by any other specialist.
