from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI()

conversation_history = []

MODEL_NAME = "gpt-5.5"

ASSISTANT_INSTRUCTIONS = """
You are Tyler's beginner-friendly AI assistant.
Be honest about uncertainty.
If the user asks for current, live, recent, or real-time information,
and you do not have a tool for it, say that you cannot verify it yet.
Keep explanations clear and concise.
"""


def ask_ai(prompt):
    conversation_history.append({
        "role": "user",
        "content": prompt
    })

    try:
        response = client.responses.create(
            model=MODEL_NAME,
            instructions=ASSISTANT_INSTRUCTIONS,
            input=conversation_history
        )

        assistant_response = response.output_text

        conversation_history.append({
            "role": "assistant",
            "content": assistant_response
        })

        return assistant_response

    except Exception:
        return "Sorry, something went wrong while contacting the AI service. Check your API key, internet connection, or account billing."


def show_help():
    print("""
Available commands:
/help   - Show this help menu
/clear  - Clear the current conversation memory
/quit   - Exit the assistant

Anything else will be sent to the AI.
""")
    
    

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

        answer = ask_ai(user_prompt)

        print()
        print("AI response:")
        print(answer)



if __name__ == "__main__":
    main()