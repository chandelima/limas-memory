"""Agente Memory Curator: lê mensagens recentes e mantém a memória via tools.

Não conversa com o usuário: a saída é um relatório curto do que fez.
"""

from dataclasses import dataclass, field
from datetime import datetime

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import config
from curator.tools import default_tools
from llm import get_llm

RECURSION_LIMIT = 40  # teto de passos (modelo <-> tools) por execução


@dataclass
class ChatMessage:
    role: str  # "usuario" ou "assistente"
    text: str
    at: str = ""  # data/hora ISO, se conhecida (ajuda a registrar a origem)


@dataclass
class ToolEvent:
    name: str
    args: dict
    result: str


@dataclass
class CurateResult:
    report: str
    trace: list[ToolEvent] = field(default_factory=list)  # chamadas, na ordem

    @property
    def tool_calls(self) -> list[str]:
        return [e.name for e in self.trace]


def load_system_prompt() -> str:
    return (config.PROMPTS_DIR / "curator.md").read_text(encoding="utf-8")


def build_curator(llm: BaseChatModel | None = None, tools: list | None = None):
    """Monta o agente. Sem argumentos, usa o modelo e a memória configurados por env."""
    # temperature=0: a memória é permanente e versionada; queremos consistência.
    return create_agent(
        model=llm or get_llm(temperature=0),
        tools=tools if tools is not None else default_tools(),
        system_prompt=load_system_prompt(),
        name="memory_curator",
    )


def format_input(
    messages: list[ChatMessage],
    context: str = "",
    now: datetime | None = None,
    channel: str = "desconhecido",
) -> str:
    now = now or datetime.now().astimezone()
    lines = [
        f"DATA E HORA ATUAIS: {now.isoformat(timespec='minutes')}",
        f"CANAL: {channel}",
        "",
    ]
    if context.strip():
        lines += ["CONTEXTO DA CONVERSA:", context.strip(), ""]
    lines.append("MENSAGENS RECENTES:")
    for m in messages:
        stamp = f" ({m.at})" if m.at else ""
        lines.append(f"- [{m.role}{stamp}] {m.text}")
    lines += ["", "Processe as mensagens acima conforme o seu fluxo de trabalho."]
    return "\n".join(lines)


def curate(
    messages: list[ChatMessage],
    context: str = "",
    agent=None,
    now: datetime | None = None,
    channel: str = "desconhecido",
) -> CurateResult:
    if not messages:
        raise ValueError("Nenhuma mensagem para curar")
    agent = agent or build_curator()
    state = agent.invoke(
        {"messages": [HumanMessage(format_input(messages, context, now, channel))]},
        config={"recursion_limit": RECURSION_LIMIT},
    )
    out = state["messages"]
    results = {m.tool_call_id: str(m.content) for m in out if isinstance(m, ToolMessage)}
    trace = [
        ToolEvent(c["name"], c["args"], results.get(c["id"], ""))
        for m in out
        if isinstance(m, AIMessage)
        for c in m.tool_calls
    ]
    report = next(
        (m.text for m in reversed(out) if isinstance(m, AIMessage) and not m.tool_calls), ""
    )
    return CurateResult(report=report.strip(), trace=trace)
