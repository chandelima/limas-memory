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


def _report(on_step, name: str, args: dict) -> None:
    """Progress is a nicety: a failing callback never breaks the reply."""
    try:
        on_step(name, args)
    except Exception:  # noqa: BLE001
        pass


def reply(history: list[ChatMessage], agent=None, now: datetime | None = None, language: str = "",
          on_step=None) -> ReplyResult:
    """Answer the user's last message (which must be the last one in the history).

    `on_step(tool_name, args)`, if given, is called as soon as the agent asks for a tool (while it is
    still working), so the caller can show progress. It runs in the calling thread."""
    if not history or history[-1].role != "user":
        raise ValueError("The history must end with a user message")
    agent = agent or build_chat_agent()
    now = now or datetime.now().astimezone()
    request = {"messages": _to_messages(history, now, language)}
    config_ = {"recursion_limit": RECURSION_LIMIT}
    if on_step is None or not hasattr(agent, "stream"):
        state = agent.invoke(request, config=config_)
    else:
        state, seen = None, 0
        for state in agent.stream(request, config=config_, stream_mode="values"):  # the whole state after each step
            messages = state["messages"]
            for message in messages[seen:]:
                if isinstance(message, AIMessage):
                    for call in message.tool_calls:
                        _report(on_step, call["name"], call["args"])
            seen = len(messages)
        if state is None:
            raise RuntimeError("the agent returned nothing")
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
