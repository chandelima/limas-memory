"""Chat no terminal com o agente de resposta (lê a memória, não escreve nela).

Uso:
    python src/chat.py
    python src/chat.py --memory-dir D:/tmp/memoria-teste --verbose

Digite 'sair' para encerrar.
"""

import argparse
import sys
from pathlib import Path

import config
from curator.agent import ChatMessage
from responder.agent import build_responder, default_read_tools, reply

EXIT_WORDS = {"sair", "exit", "quit"}


def main(argv: list[str] | None = None, agent=None, read=input) -> int:
    ap = argparse.ArgumentParser(description="Chat com o agente de resposta.")
    ap.add_argument("--memory-dir", type=Path, default=None,
                    help="pasta da memória (padrão: MEMORY_DIR / ./memory)")
    ap.add_argument("--verbose", action="store_true", help="mostra as consultas à memória")
    args = ap.parse_args(argv)

    if agent is None:
        try:
            agent = build_responder(tools=default_read_tools(args.memory_dir))
        except RuntimeError as exc:
            print(f"Erro: {exc}", file=sys.stderr)
            return 1

    memory_dir = args.memory_dir or config.MEMORY_DIR
    print(f"Chat com {config.OPENROUTER_MODEL} (memória: {memory_dir}). Digite 'sair' para encerrar.\n")
    history: list[ChatMessage] = []
    while True:
        try:
            text = read("Você: ").strip()
        except EOFError:
            break
        if text.lower() in EXIT_WORDS:
            break
        if not text:
            continue

        history.append(ChatMessage("usuario", text))
        result = reply(history, agent=agent)
        if args.verbose:
            for event in result.trace:
                print(f"  [memória] {event.name}({', '.join(f'{k}={v!r}' for k, v in event.args.items())})")
        print(f"IA: {result.text}\n")
        history.append(ChatMessage("assistente", result.text))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
