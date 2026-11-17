"""Chat no terminal: agente de resposta + histórico em arquivo + Curator por inatividade.

Ferramenta de desenvolvimento: simula no terminal o mesmo fluxo do bot do Telegram
(src/bot.py), útil para testar prompts e memória sem o Telegram.

Uso:
    python src/chat.py
    python src/chat.py --memory-dir D:/tmp/memoria-teste --verbose
    python src/chat.py --memory-dir D:/tmp/m --log D:/tmp/conversa.json --idle-seconds 30

Comandos: /curate (cura as pendentes agora), /status, sair.
"""

import argparse
import sys
import threading
from pathlib import Path

import config
from chatflow import Conversation, build_service, describe_outcome, describe_status  # noqa: F401
from conversation.log import ConversationLog
from responder.agent import build_responder, default_read_tools

EXIT_WORDS = {"sair", "exit", "quit"}
WATCH_INTERVAL = 5  # segundos entre verificações de inatividade


def main(argv=None, agent=None, read=input, service=None, start_watcher=True) -> int:
    ap = argparse.ArgumentParser(description="Chat com o agente de resposta.")
    ap.add_argument("--memory-dir", type=Path, default=None,
                    help="pasta da memória (padrão: MEMORY_DIR / ./memory)")
    ap.add_argument("--log", type=Path, default=None,
                    help="arquivo do histórico (padrão: CONVERSATION_FILE / ./data/conversa.json)")
    ap.add_argument("--idle-seconds", type=float, default=config.CURATOR_IDLE_SECONDS,
                    help="inatividade antes de o Curator processar (padrão: CURATOR_IDLE_SECONDS)")
    ap.add_argument("--verbose", action="store_true", help="mostra as consultas à memória")
    args = ap.parse_args(argv)

    if agent is None:
        try:
            agent = build_responder(tools=default_read_tools(args.memory_dir))
        except RuntimeError as exc:
            print(f"Erro: {exc}", file=sys.stderr)
            return 1

    # Um serviço recebido já traz o seu histórico; usamos o mesmo objeto para não divergir.
    log = service.log if service else ConversationLog(args.log or config.CONVERSATION_FILE)
    service = service or build_service(log, args.memory_dir, args.idle_seconds, channel="terminal")
    conversation = Conversation(log, service, agent)
    memory_dir = args.memory_dir or config.MEMORY_DIR

    stop = threading.Event()
    if start_watcher:
        def watch():
            while not stop.wait(WATCH_INTERVAL):
                outcome = service.tick()
                if outcome:
                    print("\n" + describe_outcome(outcome), flush=True)

        threading.Thread(target=watch, daemon=True).start()

    print(f"Chat com {config.OPENROUTER_MODEL} (memória: {memory_dir}). Digite 'sair' para encerrar.")
    print(f"Histórico: {log.path} | Curador após {service.idle_seconds:.0f}s de inatividade. "
          f"Comandos: /curate, /status\n")
    try:
        while True:
            try:
                text = read("Você: ").strip()
            except EOFError:
                break
            if text.lower() in EXIT_WORDS:
                break
            if not text:
                continue
            if text == "/status":
                print(conversation.status() + "\n")
                continue
            if text == "/curate":
                print(conversation.curate_now() + "\n")
                continue

            result = conversation.respond(text)
            if args.verbose:
                for event in result.trace:
                    print(f"  [memória] {event.name}({', '.join(f'{k}={v!r}' for k, v in event.args.items())})")
            print(f"IA: {result.text}\n")
    finally:
        stop.set()

    pending = len(log.pending())
    if pending:
        print(f"{pending} mensagem(ns) ainda não curada(s); ficam salvas em {log.path} "
              f"e serão curadas após {service.idle_seconds:.0f}s de inatividade ou com /curate.")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
