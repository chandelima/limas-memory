"""Chat no terminal: agente de resposta + histórico em arquivo + Curator por inatividade.

Simula, no terminal, o que o bot do Telegram fará: cada mensagem vai para o histórico
(JSON), o agente de resposta consulta a memória (só leitura) e, depois de um tempo sem
mensagens, o Curator processa o que ficou pendente e atualiza a memória.

Uso:
    python src/chat.py
    python src/chat.py --memory-dir D:/tmp/memoria-teste --verbose
    python src/chat.py --memory-dir D:/tmp/m --log D:/tmp/conversa.json --idle-seconds 30

Comandos: /curar (cura as pendentes agora), /status, sair.
"""

import argparse
import sys
import threading
from pathlib import Path

import config
from conversation.log import ConversationLog
from curator.agent import build_curator, curate
from curator.service import CurationService, Outcome, to_chat
from curator.tools import default_tools
from responder.agent import build_responder, default_read_tools, reply

EXIT_WORDS = {"sair", "exit", "quit"}
HISTORY_MESSAGES = 30  # quantas mensagens recentes o agente de resposta enxerga
WATCH_INTERVAL = 5  # segundos entre verificações de inatividade


def build_service(log: ConversationLog, memory_dir: Path | None, idle_seconds: float) -> CurationService:
    """Serviço de curadoria; o agente do Curator só é criado na primeira vez que roda."""
    cache: dict = {}

    def curate_fn(messages, context, channel):
        if "agent" not in cache:
            cache["agent"] = build_curator(tools=default_tools(memory_dir))
        return curate(messages, context, agent=cache["agent"], channel=channel)

    return CurationService(log, curate_fn, channel="terminal", idle_seconds=idle_seconds)


def describe_outcome(outcome: Outcome) -> str:
    if outcome.error:
        return f"[curador] falhou; {outcome.messages} mensagem(ns) seguem pendentes: {outcome.error}"
    tools = " -> ".join(outcome.result.tool_calls) or "nenhuma tool"
    return (
        f"[curador] {outcome.messages} mensagem(ns) processada(s) ({tools})\n"
        f"{outcome.result.report}"
    )


def describe_status(service: CurationService) -> str:
    pending = len(service.log.pending())
    idle = service.seconds_idle()
    parts = [f"pendentes: {pending}"]
    if idle is not None:
        parts.append(f"inativo há {idle:.0f}s (curadoria após {service.idle_seconds:.0f}s)")
    if service.last_error:
        parts.append(f"último erro: {service.last_error}")
    return "[status] " + " | ".join(parts)


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
    service = service or build_service(log, args.memory_dir, args.idle_seconds)
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
          f"Comandos: /curar, /status\n")
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
                print(describe_status(service) + "\n")
                continue
            if text == "/curar":
                outcome = service.run_now()
                print((describe_outcome(outcome) if outcome else "[curador] nada pendente.") + "\n")
                continue

            log.append("usuario", text)
            result = reply([to_chat(m) for m in log.recent(HISTORY_MESSAGES)], agent=agent)
            if args.verbose:
                for event in result.trace:
                    print(f"  [memória] {event.name}({', '.join(f'{k}={v!r}' for k, v in event.args.items())})")
            print(f"IA: {result.text}\n")
            log.append("assistente", result.text)
    finally:
        stop.set()

    pending = len(log.pending())
    if pending:
        print(f"{pending} mensagem(ns) ainda não curada(s); ficam salvas em {log.path} "
              f"e serão curadas após {service.idle_seconds:.0f}s de inatividade ou com /curar.")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
