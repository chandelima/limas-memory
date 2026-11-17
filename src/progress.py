"""Live reply on Telegram: ONE message that shows the agent thinking and then the answer as it is written.

    <i>💭 Thinking…</i>                       sent as soon as the bot starts answering
    <i>🗂️ Looking at what I remember…</i>      the agent's steps, in italics, "typed" in as they happen
    <i>📖 Reading trips/x.md…</i>

    The answer grows here as the model          ← the thinking is ERASED when the answer starts; the model's
    writes it, in normal text.                    text is streamed for real, token by token

* Thinking (steps and thoughts) is in italics (Telegram HTML), so it is told apart from the answer. It
  has a FAKE typing effect: each new line is revealed in blocks of words at THINKING_CPS characters per
  second, by editing the message. The answer is held back until the thinking has finished typing.
* The answer is REAL streaming: the model's text is shown as it arrives. If the model writes some text
  and then asks for a tool ("let me check..."), that text is moved up as an italic thought.
* When the answer starts, the thinking is erased from the message: the final message is just the answer.
  The message itself is never deleted: the same one is edited from the "thinking" line to the answer.

Telegram allows about one edit per second per chat, so the message is refreshed every EDIT_INTERVAL
seconds at most, with everything received/typed so far. The agent runs in a worker thread and reports
through `push` (a tool step) and `push_text` (a piece of text); both are thread-safe. The gateway needs
`send_placeholder`, `edit_text` (both with `html=True`), `write_answer`, `replace_text` and `send_text`.
Everything here is a nicety: if a call fails, the answer is still delivered the normal way.
"""

import asyncio
import contextlib
import html
import math

from applog import get_logger
from i18n import t
from telegram_gateway import split_message, to_plain

log = get_logger("progress")

EDIT_INTERVAL = 0.8  # between edits (Telegram allows about one edit per second per chat)
THINKING_CPS = 200  # fake typing speed of the thinking lines, in characters per second (a fast LLM); 0 = at once
FIRST_CHUNK = 3000  # the first part of the answer shares the message with the thinking (limit 4096)
MAX_LINES = 8  # thinking lines shown at once (the last ones)
THOUGHT_CHARS = 200  # how much of an interim text is kept as a thought
QUERY_CHARS = 40  # how much of a search query or path is shown
CURSOR = " ▍"  # shown while something is still being typed or written

_LABELS = {
    "list_memory": ("progress_memory_list", None),
    "search_memory": ("progress_memory_search", "query"),
    "read_memory": ("progress_memory_read", "path"),
    "send_attachment": ("progress_file", None),
    "web_search": ("progress_web_search", "query"),
    "list_tasks": ("progress_tasks_list", None),
    "add_task": ("progress_tasks_add", None),
    "complete_task": ("progress_tasks_update", None),
    "reschedule_task": ("progress_tasks_update", None),
    "reopen_task": ("progress_tasks_update", None),
    "remove_task": ("progress_tasks_update", None),
    "tag_task": ("progress_tasks_update", None),
    "link_task": ("progress_tasks_update", None),
}


def step_label(name: str, args: dict | None) -> str:
    """The line shown for a tool call, in the user's language."""
    key, arg = _LABELS.get(name, ("progress_other", None))
    if arg is None:
        return t(key)
    value = " ".join(str((args or {}).get(arg, "")).split())
    if len(value) > QUERY_CHARS:
        value = value[: QUERY_CHARS - 1] + "…"
    return t(key, **{arg: value})


def italics(lines: list[str]) -> str:
    """Thinking lines as Telegram HTML, in italics."""
    return "\n".join(f"<i>{html.escape(line)}</i>" for line in lines)


def _next_boundary(text: str, position: int) -> int:
    """The next whitespace at or after `position` (so the typing grows by whole words)."""
    if position >= len(text):
        return len(text)
    for i in range(position, len(text)):
        if text[i].isspace():
            return i
    return len(text)


class Progress:
    def __init__(self, gateway, chat_id: int, loop: asyncio.AbstractEventLoop | None = None,
                 edit_interval: float | None = None, thinking_cps: float | None = None):
        self.gateway = gateway
        self.chat_id = chat_id
        self.loop = loop or asyncio.get_running_loop()
        self.edit_interval = EDIT_INTERVAL if edit_interval is None else edit_interval
        self.thinking_cps = THINKING_CPS if thinking_cps is None else thinking_cps
        self.message_id: int | None = None
        self._lines: list[str] = []  # steps and interim thoughts (full text), shown in italics
        self._typed = 0  # characters of "\n".join(self._lines) revealed so far (the fake typing)
        self._answer = ""  # the text the model is writing now (real streaming)
        self._shown = ""  # what the message shows right now
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Send the "thinking..." message and start following the agent."""
        self._shown = italics([t("progress_thinking")])
        try:
            self.message_id = await self.gateway.send_placeholder(self.chat_id, self._shown, html=True)
        except Exception:  # noqa: BLE001 - without it the answer just arrives the normal way
            log.warning("could not send the progress message", exc_info=True)
            return
        self._task = asyncio.create_task(self._follow())

    # ---- called from the agent's thread ---------------------------------------------------

    def _post(self, kind: str, value: str) -> None:
        if self.message_id is None:
            return
        try:
            self.loop.call_soon_threadsafe(self._queue.put_nowait, (kind, value))
        except RuntimeError:  # the loop is closed (shutting down)
            pass

    def push(self, name: str, args: dict | None = None) -> None:
        """The agent asked for a tool. Safe to call from any thread."""
        self._post("step", step_label(name, args))

    def push_text(self, delta: str) -> None:
        """A piece of the text the model is writing. Safe to call from any thread."""
        if delta:
            self._post("text", delta)

    # ---- the message -------------------------------------------------------------------------

    def _apply(self, kind: str, value: str) -> None:
        if kind == "text":
            self._answer += value
            return
        thought = " ".join(self._answer.split())  # text before a tool call was a thought, not the answer
        if thought:
            self._lines.append(thought if len(thought) <= THOUGHT_CHARS else thought[: THOUGHT_CHARS - 1] + "…")
            self._answer = ""
        if not self._lines or self._lines[-1] != value:  # no repeated lines in a row
            self._lines.append(value)

    def _advance_typing(self) -> None:
        """One tick of the fake typing: reveal a few more words of the thinking."""
        full = "\n".join(self._lines)
        if self.thinking_cps <= 0:
            self._typed = len(full)
        elif self._typed < len(full):
            step = max(math.ceil(self.thinking_cps * self.edit_interval), 1)
            self._typed = _next_boundary(full, self._typed + step)

    def _render(self) -> str:
        full = "\n".join(self._lines)
        typing = self._typed < len(full)
        lines = full[: self._typed].split("\n") if self._typed else []
        if typing and lines:
            lines[-1] += CURSOR
        if self._answer.strip() and not typing:  # the answer started (after the thinking finished typing):
            return html.escape(self._answer[:FIRST_CHUNK]) + CURSOR  # ... the thinking is erased
        header = italics(lines[-MAX_LINES:]) if lines else ""
        return header or italics([t("progress_thinking")])

    async def _follow(self) -> None:
        while True:
            try:  # wake up for each new event, or at the next tick while the thinking is still typing
                kind, value = await asyncio.wait_for(self._queue.get(), timeout=self.edit_interval)
                self._apply(kind, value)
            except asyncio.TimeoutError:
                pass
            while not self._queue.empty():
                self._apply(*self._queue.get_nowait())
            self._advance_typing()
            text = self._render()
            if text != self._shown:
                try:
                    await self.gateway.edit_text(self.chat_id, self.message_id, text, html=True)
                    self._shown = text
                except Exception:  # noqa: BLE001 - e.g. "message is not modified" or a rate limit
                    log.debug("could not update the progress message", exc_info=True)
            await asyncio.sleep(self.edit_interval)

    async def _stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def finish(self, text: str) -> None:
        """Turn the message into the final answer (the thinking is erased) or, if there is no progress
        message, send it the normal way. The answer is never lost."""
        await self._stop()  # no step edit can land after (and overwrite) the final text
        if self.message_id is None:
            await self.gateway.send_text(self.chat_id, text)
            return
        chunks = split_message(to_plain(text), FIRST_CHUNK) or [t("no_reply")]
        await self.gateway.write_answer(self.chat_id, self.message_id, html.escape(chunks[0]), chunks[0])  # only the answer
        for chunk in chunks[1:]:  # an answer too long for one message continues in new ones
            await self.gateway.send_text(self.chat_id, chunk, raw=True)
