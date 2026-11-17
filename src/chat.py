"""Chat simples no terminal com LangChain + OpenRouter, mantendo histórico."""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import config
from llm import get_llm


def main() -> None:
    llm = get_llm()
    history = [SystemMessage("Você é um assistente prestativo e conciso.")]

    print(f"Chat iniciado com {config.OPENROUTER_MODEL}. Digite 'sair' para encerrar.\n")
    while True:
        user_input = input("Você: ").strip()
        if user_input.lower() in {"sair", "exit", "quit"}:
            break
        if not user_input:
            continue

        history.append(HumanMessage(user_input))
        print("IA: ", end="", flush=True)
        reply = ""
        for chunk in llm.stream(history):
            print(chunk.text, end="", flush=True)
            reply += chunk.text
        print("\n")
        history.append(AIMessage(reply))


if __name__ == "__main__":
    main()
