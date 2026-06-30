from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI()
conversation_history = []


def ask_ai(prompt):
    conversation_history.append({
        "role": "user",
        "content": prompt
    })

    response = client.responses.create(
        model="gpt-5.5",
        instructions="""
        You are Tyler's beginner-friendly AI assistant.
        Be honest about uncertainty.
        If the user asks for current, live, recent, or real-time information,
        and you do not have a tool for it, say that you cannot verify it yet.
        Keep explanations clear and concise.
        """,
        input=conversation_history
    )

    conversation_history.append({
        "role": "assistant",
        "content": response.output_text
    })

    return response.output_text

while True:
    user_prompt = input("\nAsk the AI something, or type 'quit' to exit: ")

    if user_prompt == "quit":
        print("Goodbye!")
        break

    answer = ask_ai(user_prompt)

    print()
    print("AI response:")
    print(answer)