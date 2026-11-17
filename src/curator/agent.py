"""Memory Curator agent: reads recent messages and maintains the memory through tools.

It does not talk to the user: its output is a short report of what it did.
"""

from dataclasses import dataclass, field
from datetime import datetime

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import config
from curator.tools import default_tools
from llm import get_llm

RECURSION_LIMIT = 40  # cap on steps (model <-> tools) per run


@dataclass
class ChatMessage:
    role: str  # "user" or "assistant"
    text: str
    at: str = ""  # ISO date/time, if known (helps record the origin)


@dataclass
class ToolEvent:
    name: str
    args: dict
    result: str


@dataclass
class Usage:
    """Token usage summed over all model calls of a run."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read: int = 0
    cache_creation: int = 0
    reasoning: int = 0
    cost: float = 0.0  # only if the provider reports it (OpenRouter: usage.cost)
    cost_reported: bool = False
    llm_calls: int = 0
    unreported_calls: int = 0  # calls without usage metadata

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
    trace: list[ToolEvent] = field(default_factory=list)  # calls, in order
    usage: Usage = field(default_factory=Usage)

    @property
    def tool_calls(self) -> list[str]:
        return [e.name for e in self.trace]


def load_system_prompt() -> str:
    return (config.PROMPTS_DIR / "curator.md").read_text(encoding="utf-8")


def build_curator(
    llm: BaseChatModel | None = None, tools: list | None = None, model: str | None = None
):
    """Build the agent. With no arguments, uses the model and memory configured by env.

    `model` picks the OpenRouter slug (ignored if `llm` is given).
    """
    # temperature=0: the memory is permanent and versioned; we want consistency.
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
    channel: str = "unknown",
    language: str = "",
) -> str:
    now = now or datetime.now().astimezone()
    lines = [
        f"CURRENT DATE AND TIME: {now.isoformat(timespec='minutes')}",
        f"CHANNEL: {channel}",
    ]
    if language:
        lines.append(f"USER LANGUAGE: {language}")
    lines.append("")
    if context.strip():
        lines += ["CONVERSATION CONTEXT:", context.strip(), ""]
    lines.append("RECENT MESSAGES:")
    for m in messages:
        stamp = f" ({m.at})" if m.at else ""
        lines.append(f"- [{m.role}{stamp}] {m.text}")
    lines += ["", "Process the messages above following your workflow."]
    return "\n".join(lines)


def curate(
    messages: list[ChatMessage],
    context: str = "",
    agent=None,
    now: datetime | None = None,
    channel: str = "unknown",
    language: str = "",
) -> CurateResult:
    if not messages:
        raise ValueError("No messages to curate")
    agent = agent or build_curator()
    state = agent.invoke(
        {"messages": [HumanMessage(format_input(messages, context, now, channel, language))]},
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
