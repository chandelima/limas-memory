"""Conversation flow used by the Telegram bot (bot.py).

No interface code: takes text, records it in the history, queries the chat agent and
lets the Curator work after inactivity. Whoever calls it takes care of showing the result.
"""

import time
from dataclasses import replace
from pathlib import Path

from applog import get_logger
from chat_agent.agent import ReplyResult, reply
from conversation.log import ANSWERED, FAILED, READY, RECEIVED, ConversationLog, Message
from curator.agent import ToolEvent, build_curator, curate
from curator.service import CurationService, Outcome, to_chat
from curator.tools import default_tools, make_store_and_git
import i18n
from i18n import t

log = get_logger("chat_agent")

HISTORY_MESSAGES = 30  # how many recent messages the chat agent sees


def user_language() -> str:
    """The user's language, once it is known (see i18n); empty before that."""
    current = i18n.current()
    return current.language if current.configured else ""


SYNC_MESSAGE = "sync: record changes made outside the Curator (tasks, attachments)"


def sync_memory(memory_dir: Path | None) -> ToolEvent | None:
    """After each curation: commit what changed outside it (tasks and attachments are written
    directly by the chat agent's tools) and push whatever has not been pushed yet.
    With nothing to do, returns None; a git failure never breaks the curation."""
    try:
        _, git = make_store_and_git(memory_dir)
        result = git.sync(SYNC_MESSAGE)
    except Exception as exc:  # noqa: BLE001
        return ToolEvent("sync_memory", {}, f"ERROR: {type(exc).__name__}: {exc}")
    if result.sha is None and result.pushed and not result.push_error:
        return ToolEvent("sync_memory", {}, "OK: repository was already up to date with the remote.")
    if result.pushed:
        return ToolEvent("sync_memory", {}, f"OK: {'commit ' + result.sha + ' ' if result.sha else ''}sent to the remote.")
    if result.sha is None and "No 'origin' remote" in result.push_error:
        return None
    return ToolEvent("sync_memory", {}, f"OK: saved locally. Push pending: {result.push_error}")


def build_service(
    log: ConversationLog,
    memory_dir: Path | None,
    idle_seconds: float,
    channel: str = "unknown",
) -> CurationService:
    """Curation service; the Curator agent is only created the first time it runs."""
    cache: dict = {}

    def curate_fn(messages, context, channel):
        if "agent" not in cache:
            cache["agent"] = build_curator(tools=default_tools(memory_dir))
        result = curate(messages, context, agent=cache["agent"], channel=channel, language=user_language())
        sync = sync_memory(memory_dir)
        if sync is not None:
            result.trace.append(sync)
        return result

    return CurationService(log, curate_fn, channel=channel, idle_seconds=idle_seconds)


def describe_outcome(outcome: Outcome) -> str:
    if outcome.busy:
        return t("curator_busy")
    if outcome.error:
        return t("curator_failed", count=outcome.messages, error=outcome.error)
    tools = " -> ".join(outcome.result.tool_calls) or t("curator_no_tools")
    return t("curator_done", count=outcome.messages, tools=tools, report=outcome.result.report)


def describe_status(service: CurationService) -> str:
    pending = len(service.log.pending())
    idle = service.seconds_idle()
    parts = [t("status_pending", count=pending)]
    in_progress = len(service.log.unfinished())
    if in_progress:
        parts.append(t("status_in_progress", count=in_progress))
    if service.is_running:
        parts.append(t("status_running"))
    if idle is not None:
        parts.append(t("status_idle", seconds=f"{idle:.0f}", limit=f"{service.idle_seconds:.0f}"))
    if service.last_error:
        parts.append(t("status_error", error=service.last_error))
    return t("status_prefix") + " " + " | ".join(parts)


def compact_history(messages: list[Message]) -> list[Message]:
    """Shorten, in the chat agent's history, the content of old files.

    Only shortens what has already been curated (the data is already in the memory) and
    never the last message. Files still pending keep their full text: the Curator only
    runs after inactivity and the agent must be able to answer about the file meanwhile.
    """
    out = []
    for i, m in enumerate(messages):
        if m.attachments and m.curated and i < len(messages) - 1:
            header = m.text.splitlines()[0]
            omitted = "(content omitted from the history; the data is in the memory)"
            m = replace(m, text=f"{header}\n{omitted}")
        out.append(m)
    return out


class Conversation:
    """One conversation: history + chat agent + curation service.

    `outbox` is the list the agent's `send_attachment` tool fills in; the files
    requested in a reply come out in `ReplyResult.files`."""

    def __init__(self, log: ConversationLog, service: CurationService, agent, outbox: list | None = None):
        self.log = log
        self.service = service
        self.agent = agent
        self.outbox = outbox if outbox is not None else []

    # ---- 1. arrival: persist right away, before any processing -------------------

    def ingest(self, text: str, attachments: list[str] | None = None, status: str = RECEIVED) -> Message:
        """Record the user's message as soon as it arrives, so it is not lost if the bot crashes."""
        return self.log.append("user", text, attachments=attachments, status=status)

    def set_content(self, msg_id: int, text: str, status: str = READY) -> Message:
        """Fill in a message's content (e.g. text extracted from a file)."""
        return self.log.update(msg_id, text=text, status=status)

    def fail(self, msg_id: int, text: str | None = None) -> Message:
        """Mark the message as resolved without a reply (unreadable file, error while answering)."""
        return self.log.update(msg_id, text=text, status=FAILED)

    def unfinished(self) -> list[Message]:
        return self.log.unfinished()

    # ---- 2. reply ----------------------------------------------------------------

    def respond_to(self, msg_id: int, on_step=None) -> ReplyResult:
        """Answer message `msg_id` (already recorded), reading the memory, and record the reply.
        `on_step(tool_name, args)` is called while the agent works (see chat_agent.reply)."""
        history = compact_history(self.log.history(msg_id, HISTORY_MESSAGES))
        self.outbox.clear()
        started = time.monotonic()
        result = reply([to_chat(m) for m in history], agent=self.agent, language=user_language(), on_step=on_step)
        usage = result.usage
        cost = f"US$ {usage.cost:.4f}" if usage.cost_reported else "n/a"
        log.info("reply to message %s: %d tool call(s), %d file(s), %d tokens, cost %s, %.1fs",
                 msg_id, len(result.trace), len(self.outbox), usage.total_tokens, cost, time.monotonic() - started)
        result.files = list(self.outbox)
        self.outbox.clear()
        self.log.append("assistant", result.text, reply_to=msg_id)
        self.log.update(msg_id, status=ANSWERED)
        return result

    def respond(self, text: str, attachments: list[str] | None = None) -> ReplyResult:
        """Shortcut: record the user's message and answer right away."""
        return self.respond_to(self.ingest(text, attachments=attachments, status=READY).id)

    def curate_now(self) -> str:
        outcome = self.service.run_now()
        return describe_outcome(outcome) if outcome else t("curator_nothing")

    def status(self) -> str:
        return describe_status(self.service)
