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
class Usage:
    """Consumo de tokens somado sobre todas as chamadas ao modelo de uma execução."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read: int = 0
    cache_creation: int = 0
    reasoning: int = 0
    cost: float = 0.0  # só se o provedor informar (OpenRouter: usage.cost)
    cost_reported: bool = False
    llm_calls: int = 0
    unreported_calls: int = 0  # chamadas sem metadados de uso

    def add(self, other: "Usage") -> None:
        for name in (
            "input_tokens", "output_tokens", "total_tokens", "cache_read",
            "cache_creation", "reasoning", "llm_calls", "unreported_calls",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.cost += other.cost
        self.cost_reported = self.cost_reported or other.cost_reported


def aggregate_usage(messages: list) -> Usage:
    usage = Usage()
    for m in messages:
        if not isinstance(m, AIMessage):
            continue
        usage.llm_calls += 1
        um = m.usage_metadata
        if not um:
            usage.unreported_calls += 1
            continue
        usage.input_tokens += um.get("input_tokens", 0)
        usage.output_tokens += um.get("output_tokens", 0)
        usage.total_tokens += um.get("total_tokens", 0)
        usage.cache_read += (um.get("input_token_details") or {}).get("cache_read", 0)
        usage.cache_creation += (um.get("input_token_details") or {}).get("cache_creation", 0)
        usage.reasoning += (um.get("output_token_details") or {}).get("reasoning", 0)
        cost = (m.response_metadata.get("token_usage") or {}).get("cost")
        if isinstance(cost, (int, float)):
            usage.cost += float(cost)
            usage.cost_reported = True
    return usage


@dataclass
class CurateResult:
    report: str
    trace: list[ToolEvent] = field(default_factory=list)  # chamadas, na ordem
    usage: Usage = field(default_factory=Usage)

    @property
    def tool_calls(self) -> list[str]:
        return [e.name for e in self.trace]


def load_system_prompt() -> str:
    return (config.PROMPTS_DIR / "curator.md").read_text(encoding="utf-8")


def build_curator(
    llm: BaseChatModel | None = None, tools: list | None = None, model: str | None = None
):
    """Monta o agente. Sem argumentos, usa o modelo e a memória configurados por env.

    `model` escolhe o slug do OpenRouter (ignorado se `llm` for passado).
    """
    # temperature=0: a memória é permanente e versionada; queremos consistência.
    return create_agent(
        model=llm or get_llm(model=model, temperature=0),
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
    return CurateResult(report=report.strip(), trace=trace, usage=aggregate_usage(out))
