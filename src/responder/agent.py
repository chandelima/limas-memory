"""Agente de resposta: conversa com o usuário e consulta a memória (somente leitura).

Nunca escreve na memória: só recebe as tools de leitura. Quem escreve é o Curator.
"""

from dataclasses import dataclass, field
from datetime import datetime

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import config
from curator.agent import ChatMessage, ToolEvent
from curator.tools import build_read_tools
from llm import get_llm
from storage.store import MemoryStore

RECURSION_LIMIT = 25


@dataclass
class ReplyResult:
    text: str
    trace: list[ToolEvent] = field(default_factory=list)
    files: list = field(default_factory=list)  # anexos a enviar junto com a resposta


def load_system_prompt() -> str:
    return (config.PROMPTS_DIR / "responder.md").read_text(encoding="utf-8")


def default_read_tools(memory_dir=None) -> list:
    return build_read_tools(MemoryStore(memory_dir or config.MEMORY_DIR))


def build_responder(llm: BaseChatModel | None = None, tools: list | None = None):
    """Monta o agente. Sem argumentos, usa o modelo e a memória configurados por env."""
    return create_agent(
        model=llm or get_llm(),
        tools=tools if tools is not None else default_read_tools(),
        system_prompt=load_system_prompt(),
        name="responder",
    )


def _to_messages(history: list[ChatMessage], now: datetime):
    """Histórico -> mensagens do LangChain. A data atual vai na última fala do usuário."""
    out = []
    for i, m in enumerate(history):
        if m.role == "assistente":
            out.append(AIMessage(m.text))
            continue
        text = m.text
        if i == len(history) - 1:
            text = f"[agora: {now.isoformat(timespec='minutes')}]\n{text}"
        out.append(HumanMessage(text))
    return out


def reply(history: list[ChatMessage], agent=None, now: datetime | None = None) -> ReplyResult:
    """Responde à última mensagem do usuário (que deve ser a última do histórico)."""
    if not history or history[-1].role != "usuario":
        raise ValueError("O histórico deve terminar com uma mensagem do usuário")
    agent = agent or build_responder()
    now = now or datetime.now().astimezone()
    state = agent.invoke(
        {"messages": _to_messages(history, now)},
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
    text = next(
        (m.text for m in reversed(out) if isinstance(m, AIMessage) and not m.tool_calls), ""
    )
    return ReplyResult(text=text.strip(), trace=trace)
