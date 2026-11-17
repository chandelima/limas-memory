"""CLI para testar o Memory Curator com uma conversa de exemplo.

Uso:
    python src/curate.py evals/scenarios/02_fato_novo.txt
    python src/curate.py conversa.txt --context "Papo sobre hábitos"
    type conversa.txt | python src/curate.py -
    python src/curate.py ex.txt --memory-dir D:/tmp/memoria-teste   # não toca na memória real
    python src/curate.py ex.txt --verbose    # mostra cada chamada de tool e o resultado

Formato da conversa: uma mensagem por linha, com prefixo `usuario:` ou `assistente:`.
Linhas sem prefixo continuam a mensagem anterior (ou formam uma mensagem do usuário).
"""

import argparse
import re
import sys
from pathlib import Path

import config
from curator.agent import ChatMessage, build_curator, curate
from curator.tools import build_tools, make_store_and_git

_ROLE = re.compile(r"^\s*(usuario|usuário|user|assistente|assistant)\s*[:>]\s*(.*)$", re.I)
_ROLE_NAMES = {
    "usuario": "usuario", "usuário": "usuario", "user": "usuario",
    "assistente": "assistente", "assistant": "assistente",
}
MAX_DIFF_LINES = 200
MAX_VALUE_CHARS = 80
MAX_RESULT_CHARS = 300


def describe_args(args: dict) -> str:
    parts = []
    for key, value in args.items():
        text = str(value)
        if len(text) > MAX_VALUE_CHARS:
            text = f"{text[:MAX_VALUE_CHARS]}...({len(text)} chars)"
        parts.append(f"{key}={text!r}")
    return ", ".join(parts)


def describe_usage(u) -> str:
    text = (
        f"chamadas ao modelo: {u.llm_calls} | entrada: {u.input_tokens} | saída: {u.output_tokens} | "
        f"cache (leitura): {u.cache_read} | cache (criação): {u.cache_creation} | "
        f"raciocínio: {u.reasoning} | total: {u.total_tokens}"
    )
    text += f" | custo: US$ {u.cost:.6f}" if u.cost_reported else " | custo: n/d"
    if u.unreported_calls:
        text += f" | ATENÇÃO: {u.unreported_calls} chamada(s) sem dados de uso"
    return text


def parse_conversation(text: str) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    current: ChatMessage | None = None
    for line in text.splitlines():
        match = _ROLE.match(line)
        if match:
            current = ChatMessage(_ROLE_NAMES[match.group(1).lower()], match.group(2).strip())
            messages.append(current)
        elif line.strip():
            if current is None:
                current = ChatMessage("usuario", line.strip())
                messages.append(current)
            else:
                current.text += "\n" + line.strip()
    return [m for m in messages if m.text]


def main(argv: list[str] | None = None, agent=None) -> int:
    ap = argparse.ArgumentParser(description="Roda o Memory Curator sobre uma conversa.")
    ap.add_argument("conversa", help="arquivo .txt com a conversa, ou '-' para stdin")
    ap.add_argument("--context", default="", help="contexto adicional da conversa")
    ap.add_argument("--channel", default="teste (CLI)", help="canal informado ao Curator")
    ap.add_argument("--verbose", action="store_true", help="mostra cada tool e seu resultado")
    ap.add_argument("--memory-dir", type=Path, default=None,
                    help="pasta da memória (padrão: MEMORY_DIR / ./memory)")
    args = ap.parse_args(argv)

    text = sys.stdin.read() if args.conversa == "-" else Path(args.conversa).read_text(encoding="utf-8")
    messages = parse_conversation(text)
    if not messages:
        print("Nenhuma mensagem encontrada na conversa.", file=sys.stderr)
        return 2

    memory_dir = args.memory_dir or config.MEMORY_DIR
    store, git = make_store_and_git(memory_dir)
    git.ensure_repo()
    before = git.head()

    if agent is None:
        try:
            agent = build_curator(tools=build_tools(store, git))
        except RuntimeError as exc:
            print(f"Erro: {exc}", file=sys.stderr)
            return 1

    print(f"Memória: {memory_dir}")
    print(f"Mensagens: {len(messages)}\n")
    result = curate(messages, args.context, agent=agent, channel=args.channel)

    print("== Tools chamadas ==")
    print(" -> ".join(result.tool_calls) or "(nenhuma)")
    if args.verbose and result.trace:
        print("\n== Detalhe das tools ==")
        for i, event in enumerate(result.trace, 1):
            print(f"{i}. {event.name}({describe_args(event.args)})")
            shown = event.result.strip().replace("\n", " | ")
            if len(shown) > MAX_RESULT_CHARS:
                shown = shown[:MAX_RESULT_CHARS] + "..."
            print(f"   -> {shown}")
    print("\n== Relatório do Curator ==")
    print(result.report or "(sem relatório)")
    print("\n== Uso de tokens ==")
    print(describe_usage(result.usage))

    after = git.head()
    print("\n== Mudanças na memória ==")
    if after == before:
        print("(nenhum commit novo)")
    else:
        lines = git.diff(before).splitlines()
        print("\n".join(lines[:MAX_DIFF_LINES]))
        if len(lines) > MAX_DIFF_LINES:
            print(f"... ({len(lines) - MAX_DIFF_LINES} linhas omitidas)")
        print("\n== Commits ==")
        print("\n".join(git.log(5)))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):  # evita erro de acentos no console do Windows
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
