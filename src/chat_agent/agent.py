"""Chat agent: talks to the user and consults the memory (read-only).

It never writes to the memory: it only receives the read tools. The Curator writes.
"""

from dataclasses import dataclass, field
from datetime import datetime

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import config
from curator.agent import ChatMessage, ToolEvent, Usage, aggregate_usage
from curator.tools import build_read_tools
from llm import get_llm
from storage.store import MemoryStore

RECURSION_LIMIT = 25


@dataclass
class ReplyResult:
    text: str
    trace: list[ToolEvent] = field(default_factory=list)
    files: list = field(default_factory=list)  # attachments to send along with the reply
    usage: Usage = field(default_factory=Usage)


def load_system_prompt() -> str:
    return (config.PROMPTS_DIR / "chat_agent.md").read_text(encoding="utf-8")


def default_read_tools(memory_dir=None) -> list:
    return build_read_tools(MemoryStore(memory_dir or config.MEMORY_DIR))


def build_chat_agent(llm: BaseChatModel | None = None, tools: list | None = None):
    """Build the agent. With no arguments, uses the model and memory configured by env."""
    return create_agent(
        model=llm or get_llm(),
        tools=tools if tools is not None else default_read_tools(),
        system_prompt=load_system_prompt(),
        name="chat_agent",
    )


def _to_messages(history: list[ChatMessage], now: datetime, language: str = ""):
    """History -> LangChain messages. The current date (and the user's language, when known)
    goes on the last user message."""
    out = []
    for i, m in enumerate(history):
        if m.role == "assistant":
            out.append(AIMessage(m.text))
            continue
        text = m.text
        if i == len(history) - 1:
            note = f"[now: {now.isoformat(timespec='minutes')}{'; reply in: ' + language if language else ''}]"
            text = f"{note}\n{text}"
        out.append(HumanMessage(text))
    return out


def reply(history: list[ChatMessage], agent=None, now: datetime | None = None, language: str = "") -> ReplyResult:
    """Answer the user's last message (which must be the last one in the history)."""
    if not history or history[-1].role != "user":
        raise ValueError("The history must end with a user message")
    agent = agent or build_chat_agent()
    now = now or datetime.now().astimezone()
    state = agent.invoke(
        {"messages": _to_messages(history, now, language)},
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
    return ReplyResult(text=text.strip(), trace=trace, usage=aggregate_usage(out))
