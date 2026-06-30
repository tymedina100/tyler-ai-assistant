from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI()

conversation_history = []

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
            model="gpt-5.5",
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


def main():
    while True:
        user_prompt = input("\nAsk the AI something, or type 'quit' to exit: ")

        if user_prompt.strip() == "":
            print("Please type something before pressing Enter.")
            continue

        if user_prompt.lower().strip() == "quit":
            print("Goodbye!")
            break

        answer = ask_ai(user_prompt)

        print()
        print("AI response:")
        print(answer)

if __name__ == "__main__":
    main()