"""Curator trigger: runs over the pending messages after a period of inactivity (or on
demand). It knows nothing about Telegram or the terminal: callers invoke `tick()`
periodically (or `watch()`, in asyncio) and `run_now()` to force a run.
"""

import asyncio
import inspect
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from applog import get_logger
from conversation.log import ConversationLog, Message
from curator.agent import ChatMessage, CurateResult

log = get_logger("curator")

DEFAULT_IDLE_SECONDS = 600
DEFAULT_RETRY_SECONDS = 300
CONTEXT_MESSAGES = 6  # already-curated messages, sent as context only
KEEP_MESSAGES = 50  # history kept in the file after curating


@dataclass
class Outcome:
    messages: int  # how many pending messages were sent to the Curator
    result: CurateResult | None = None
    error: str = ""
    busy: bool = False  # a curation was already running; nothing was done now

    @property
    def ok(self) -> bool:
        return not self.error


CurateFn = Callable[[list[ChatMessage], str, str], CurateResult]  # (messages, context, channel)


def to_chat(m: Message) -> ChatMessage:
    return ChatMessage(m.role, m.text, m.at)


def format_context(older: list[Message]) -> str:
    if not older:
        return ""
    lines = ["Earlier messages, ALREADY processed (context only; do not record again):"]
    lines += [f"- [{m.role} ({m.at})] {m.text}" for m in older]
    return "\n".join(lines)


class CurationService:
    def __init__(
        self,
        log: ConversationLog,
        curate_fn: CurateFn,
        *,
        channel: str = "unknown",
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        retry_seconds: float = DEFAULT_RETRY_SECONDS,
    ):
        self.log = log
        self.curate_fn = curate_fn
        self.channel = channel
        self.idle_seconds = idle_seconds
        self.retry_seconds = retry_seconds
        self.last_error = ""
        self._failed_at: datetime | None = None
        self._busy = threading.Lock()
        self._failure_logged = False  # only the 1st failure of a streak is logged as ERROR
        self._last_push_warning = ""  # the pending-push warning repeats only when the reason changes

    # ---- state ------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._busy.locked()

    def seconds_idle(self, now: datetime | None = None) -> float | None:
        last = self.log.last_message_at()
        if last is None:
            return None
        return ((now or datetime.now().astimezone()) - last).total_seconds()

    def due(self, now: datetime | None = None) -> bool:
        """There are pending messages, the conversation has been idle long enough and we
        are not in a back-off after a failure."""
        now = now or datetime.now().astimezone()
        if not self.log.pending():
            return False
        idle = self.seconds_idle(now)
        if idle is None or idle < self.idle_seconds:
            return False
        if self._failed_at and (now - self._failed_at).total_seconds() < self.retry_seconds:
            return False
        return True

    # ---- execution --------------------------------------------------------

    def tick(self, now: datetime | None = None) -> Outcome | None:
        """Call periodically. Runs the Curator if it is time; otherwise None."""
        now = now or datetime.now().astimezone()
        if not self.due(now):
            return None
        outcome = self.run_now(now)
        return None if outcome is not None and outcome.busy else outcome

    def run_now(self, now: datetime | None = None) -> Outcome | None:
        """Curate the pending messages now, without waiting for inactivity.
        None if nothing is pending; Outcome(busy=True) if a curation is already
        running (nothing is done)."""
        if not self._busy.acquire(blocking=False):
            return Outcome(0, busy=True)
        try:
            pending = self.log.pending()
            if not pending:
                return None
            context = format_context(self.log.curated_context(CONTEXT_MESSAGES))
            started = time.monotonic()
            try:
                result = self.curate_fn([to_chat(m) for m in pending], context, self.channel)
            except Exception as exc:  # noqa: BLE001 - failure becomes an Outcome; messages stay pending
                self.last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
                self._failed_at = now or datetime.now().astimezone()
                self._log_failure(len(pending), exc)
                return Outcome(len(pending), error=self.last_error)
            self.last_error, self._failed_at, self._failure_logged = "", None, False
            self.log.mark_curated([m.id for m in pending])
            self.log.prune(KEEP_MESSAGES)
            self._log_success(len(pending), result, time.monotonic() - started)
            return Outcome(len(pending), result=result)
        finally:
            self._busy.release()

    # ---- logs (counts and metadata only; never message or memory content) ----

    def _log_failure(self, count: int, exc: Exception) -> None:
        if not self._failure_logged:
            self._failure_logged = True
            log.error("curation failed (%d message(s) stay pending; retrying in %.0fs)",
                      count, self.retry_seconds, exc_info=exc)
        else:  # later attempts stay on the console only, to avoid repeating the alert in the channel
            log.info("curation still failing: %s", self.last_error)

    def _log_success(self, count: int, result: CurateResult, seconds: float) -> None:
        usage = result.usage
        cost = f"US$ {usage.cost:.4f}" if usage.cost_reported else "n/a"
        log.info("curation finished: %d message(s), %d tool call(s), %d tokens, cost %s, %.1fs",
                 count, len(result.trace), usage.total_tokens, cost, seconds)
        commits = [e.result for e in result.trace if e.name in ("commit_memory", "sync_memory")]
        pending = next((r for r in commits if "Push pending" in r), None)
        if pending is not None:
            reason = pending.split("Push pending:", 1)[-1].strip()[:200]
            if reason != self._last_push_warning:  # warn when it is new, not on every curation
                self._last_push_warning = reason
                log.warning("memory push pending: %s", reason)
            else:
                log.debug("memory push still pending: %s", reason)
        elif any("sent to the remote" in r or "up to date with the remote" in r for r in commits):
            self._last_push_warning = ""

    async def watch(
        self,
        stop: asyncio.Event,
        interval: float = 30,
        on_outcome: Callable[[Outcome], object] | None = None,
    ) -> None:
        """asyncio loop: checks every `interval` s, without blocking the event loop."""
        while not stop.is_set():
            outcome = await asyncio.to_thread(self.tick)
            if outcome and on_outcome:
                try:
                    result = on_outcome(outcome)
                    if inspect.isawaitable(result):  # the callback may be async
                        await result
                except Exception as exc:  # noqa: BLE001 - a faulty callback does not take the loop down
                    log.error("error handling the curation result", exc_info=exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
