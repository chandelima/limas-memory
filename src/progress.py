"""Live progress of a reply: one Telegram message that shows what the agent is doing and, in the
end, is EDITED into the final answer (nothing is deleted).

    thinking...            (sent as soon as the bot starts answering)
    ↓ edited at every step of the agent, at most once per EDIT_INTERVAL seconds
    steps so far...
    ↓ edited one last time
    the final answer

The agent runs in a worker thread and reports each step through `push` (thread-safe); a task on
the event loop does the edits. The gateway needs `send_placeholder`, `edit_text` and `replace_text`.
Progress is a nicety: if any call fails it is dropped and the answer is sent the normal way.
"""

import asyncio
import contextlib

from applog import get_logger
from i18n import t

log = get_logger("progress")

EDIT_INTERVAL = 1.2  # Telegram allows about one edit per second per chat
MAX_LINES = 8  # steps shown at once (the last ones)
QUERY_CHARS = 40  # how much of a search query or path is shown

_LABELS = {
    "list_memory": ("progress_memory_list", None),
    "search_memory": ("progress_memory_search", "query"),
    "read_memory": ("progress_memory_read", "path"),
    "send_attachment": ("progress_file", None),
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


class Progress:
    def __init__(self, gateway, chat_id: int, loop: asyncio.AbstractEventLoop | None = None,
                 edit_interval: float | None = None):
        self.gateway = gateway
        self.chat_id = chat_id
        self.loop = loop or asyncio.get_running_loop()
        self.edit_interval = EDIT_INTERVAL if edit_interval is None else edit_interval
        self.message_id: int | None = None
        self._lines: list[str] = []
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Send the "thinking..." message and start following the steps."""
        try:
            self.message_id = await self.gateway.send_placeholder(self.chat_id, t("progress_thinking"))
        except Exception:  # noqa: BLE001 - without it the answer just arrives the normal way
            log.warning("could not send the progress message", exc_info=True)
            return
        self._task = asyncio.create_task(self._follow())

    def push(self, name: str, args: dict | None = None) -> None:
        """Report a step. Safe to call from any thread."""
        if self.message_id is None:
            return
        try:
            self.loop.call_soon_threadsafe(self._queue.put_nowait, step_label(name, args))
        except RuntimeError:  # the loop is closed (shutting down)
            pass

    async def _follow(self) -> None:
        while True:
            line = await self._queue.get()
            new = [line]
            while not self._queue.empty():
                new.append(self._queue.get_nowait())
            changed = False
            for item in new:
                if not self._lines or self._lines[-1] != item:  # no repeated lines in a row
                    self._lines.append(item)
                    changed = True
            if changed:
                try:
                    await self.gateway.edit_text(self.chat_id, self.message_id, "\n".join(self._lines[-MAX_LINES:]))
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
        """Turn the progress message into the final text (or send it normally if there is none)."""
        await self._stop()  # no step edit can land after (and overwrite) the final text
        if self.message_id is None:
            await self.gateway.send_text(self.chat_id, text)
            return
        await self.gateway.replace_text(self.chat_id, self.message_id, text)
