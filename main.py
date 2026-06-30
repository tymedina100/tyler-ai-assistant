from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()

client = OpenAI()

conversation_history = []

MODEL_NAME = "gpt-5.5"

BASE_DIR = Path(__file__).parent
FILES_DIR = BASE_DIR / "files"

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


def ask_ai(prompt):
    conversation_history.append({
        "role": "user",
        "content": prompt
    })

    assistant_response = get_ai_response(conversation_history)

    conversation_history.append({
        "role": "assistant",
        "content": assistant_response
    })

    return assistant_response


def show_help():
    print("""
Available commands:
/help                       - Show this help menu
/clear                      - Clear the current conversation memory
/history                    - Show the current conversation memory
/read <filename>            - Read a file from the files folder
/askfile <filename> <question> - Ask a question about a file
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

        answer = ask_ai(user_prompt)

        print()
        print("AI response:")
        print(answer)


if __name__ == "__main__":
    main()