"""Application logging layer: console + a Telegram channel.

- Console: every event from LOG_LEVEL up (default INFO), with time, level and origin.
- Telegram channel (LOG_CHANNEL_ID): only what matters (from LOG_CHANNEL_LEVEL up, default
  WARNING, plus lifecycle notices marked with extra={"channel": True}), sent in batches,
  without repeating identical consecutive lines and without exceeding Telegram's message limit.
- Privacy: logs record IDs, counts, durations and the type/message of errors; never the text of
  your messages, the memory content or the Curator's report. Secrets (bot token, API keys,
  git tokens) are masked in every destination.

The logs of the channel sending itself (logger "limas.logship") go only to the console, so that
a failure to send does not generate more logs to send.
"""

import asyncio
import logging
import queue
import re
import sys
import time
from typing import Awaitable, Callable, Iterable

import config

ROOT_NAME = "limas"
SHIP_NAME = f"{ROOT_NAME}.logship"
MAX_BACKOFF = 300  # seconds between attempts while the channel is unreachable
CHANNEL_LIMIT = 3900  # margin under Telegram's 4096-character limit
_HANDLER_MARK = "_limas_console"

_PATTERNS = [
    re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}"),  # Telegram bot token (in the URL it comes glued to "bot")
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),  # sk-... style keys
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\bAuthorization:\s*\S+(?:\s+\S+)?"),
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password|senha)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+=]{12,}"),
]


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{ROOT_NAME}.{name}")


def known_secrets() -> list[str]:
    """Sensitive values of the current configuration (read on the spot, to follow changes)."""
    values = (config.TELEGRAM_BOT_TOKEN, config.OPENROUTER_API_KEY, config.MEMORY_GIT_TOKEN)
    return [v for v in values if v and len(v) >= 8]


def redact(text: str, secrets: Iterable[str] | None = None) -> str:
    for secret in (known_secrets() if secrets is None else secrets):
        text = text.replace(secret, "***")
    for pattern in _PATTERNS:
        text = pattern.sub("***", text)
    return text


class RedactingFormatter(logging.Formatter):
    """Format and mask secrets, including in exception text."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def parse_level(value: str | int, default: int) -> int:
    if isinstance(value, int):
        return value
    level = logging.getLevelName(str(value).strip().upper())
    return level if isinstance(level, int) else default


def parse_channel_id(value: str) -> int | str | None:
    """'-1001234567890' -> int; '@mychannel' -> str; empty -> None (channel disabled)."""
    value = (value or "").strip()
    if not value:
        return None
    if value.lstrip("-").isdigit():
        return int(value)
    if value.startswith("@") and len(value) > 1:
        return value
    raise ValueError(f"invalid LOG_CHANNEL_ID: '{value}' (use the numeric ID, like -1001234567890, or @channelname)")


# ---- Telegram channel: queue on emit, batch sending by a task --------------------------------


class ChannelHandler(logging.Handler):
    """Put in a queue (non-blocking and thread-safe) the records that go to the channel."""

    def __init__(self, level: int = logging.WARNING, maxsize: int = 500):
        super().__init__(logging.DEBUG)
        self.min_level = level
        self.queue: queue.Queue[str] = queue.Queue(maxsize=maxsize)
        self.dropped = 0

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == SHIP_NAME or record.name.startswith(SHIP_NAME + "."):
            return  # the log sending itself never goes back into the queue
        if record.levelno < self.min_level and not getattr(record, "channel", False):
            return
        try:
            line = self.channel_line(record)
            try:
                self.queue.put_nowait(line)
            except queue.Full:  # drop the oldest and count it
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    pass
                self.dropped += 1
                self.queue.put_nowait(line)
        except Exception:  # noqa: BLE001 - logging never takes the program down
            self.handleError(record)

    @staticmethod
    def channel_line(record: logging.LogRecord) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        text = f"{stamp} #{record.levelname} {record.name.removeprefix(ROOT_NAME + '.')}: {record.getMessage()}"
        if record.exc_info and record.exc_info[1] is not None:
            err = record.exc_info[1]
            text += f" [{type(err).__name__}: {err}]"  # only type and message; the traceback stays on the console
        return " ".join(redact(text).split())  # a single line


def pack(lines: list[str], limit: int = CHANNEL_LIMIT) -> list[str]:
    """Join lines into messages of up to `limit` characters (a huge line is cut)."""
    messages, current = [], ""
    for line in lines:
        line = line if len(line) <= limit else line[: limit - 1] + "…"
        if current and len(current) + 1 + len(line) > limit:
            messages.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        messages.append(current)
    return messages


def collapse(lines: list[str]) -> list[str]:
    """Join identical consecutive lines (ignoring the time): 'X' 'X' 'X' -> 'X (x3)'."""
    out: list[tuple[str, int]] = []
    for line in lines:
        key = line.split(" ", 1)[1] if " " in line else line
        if out and out[-1][0].split(" ", 1)[-1] == key:
            out[-1] = (out[-1][0], out[-1][1] + 1)
        else:
            out.append((line, 1))
    return [f"{line} (x{n})" if n > 1 else line for line, n in out]


class LogShipper:
    """Empty the ChannelHandler queue every `flush_seconds` and send it to the channel in a batch."""

    def __init__(
        self,
        handler: ChannelHandler,
        send: Callable[[str], Awaitable[None]],
        *,
        flush_seconds: float = 5,
        min_interval: float = 3,
        header: str = "",
        keep_on_failure: int = 200,
    ):
        self.handler = handler
        self.send = send
        self.flush_seconds = flush_seconds
        self.min_interval = min_interval
        self.header = header
        self.keep_on_failure = keep_on_failure
        self._pending: list[str] = []  # lines that failed to send, to try again
        self._last_sent = 0.0
        self._failures = 0  # consecutive failed sends
        self._retry_at = 0.0  # no attempt before this instant (exponential back-off)
        self._log = logging.getLogger(SHIP_NAME)

    def _drain(self) -> list[str]:
        lines, self._pending = self._pending, []
        while True:
            try:
                lines.append(self.handler.queue.get_nowait())
            except queue.Empty:
                break
        if self.handler.dropped:
            lines.append(f"({self.handler.dropped} log lines dropped due to overflow)")
            self.handler.dropped = 0
        return lines

    async def flush(self, force: bool = False) -> int:
        """Send what is in the queue. Return how many messages were sent. While the channel is
        unreachable, attempts are spaced out (back-off) and the lines keep accumulating."""
        if self._failures and not force and time.monotonic() < self._retry_at:
            return 0
        lines = collapse(self._drain())
        if not lines:
            return 0
        sent = 0
        messages = pack([self.header, *lines] if self.header else lines)
        for i, message in enumerate(messages):
            wait = self.min_interval - (time.monotonic() - self._last_sent)
            if wait > 0 and self._last_sent:
                await asyncio.sleep(wait)
            try:
                await self.send(message)
            except Exception as exc:  # noqa: BLE001 - keep the rest for the next attempt
                self._failures += 1
                self._retry_at = time.monotonic() + min(MAX_BACKOFF, self.flush_seconds * 2 ** self._failures)
                if self._failures == 1:  # one warning per streak, not one per attempt
                    self._log.warning("could not send the log to the channel: %s: %s (retrying with back-off)",
                                      type(exc).__name__, exc)
                rest = [ln for m in messages[i:] for ln in m.split("\n") if not (self.header and ln == self.header)]
                self._pending = rest[-self.keep_on_failure:]
                return sent
            self._last_sent = time.monotonic()
            sent += 1
        if self._failures and sent:
            self._log.info("the log channel is reachable again (after %d failed attempt(s))", self._failures)
            self._failures = 0
        return sent

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.flush_seconds)
            except asyncio.TimeoutError:
                pass
            await self.flush()
        await self.flush(force=True)  # farewell: what is left on shutdown


# ---- configuration -----------------------------------------------------------------------------------


def configure_logging(console_level: int | str = logging.INFO, channel_level: int | str = logging.WARNING,
                      channel: bool = False) -> ChannelHandler | None:
    """Turn on the console (and, if `channel`, the channel queue). Can be called several times.
    Return the ChannelHandler when the channel is on."""
    console_level = parse_level(console_level, logging.INFO)
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, _HANDLER_MARK, False)]:
        root.removeHandler(h)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(console_level)
    console.setFormatter(RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    setattr(console, _HANDLER_MARK, True)
    root.addHandler(console)
    root.setLevel(min(console_level, logging.WARNING))
    # libraries stay at WARNING: httpx logs the URL of every call, and it contains the bot token
    for name in ("httpx", "httpx2", "httpcore", "telegram", "apscheduler"):
        logging.getLogger(name).setLevel(logging.WARNING)
    app = logging.getLogger(ROOT_NAME)
    app.setLevel(logging.DEBUG)
    for h in [h for h in app.handlers if isinstance(h, ChannelHandler)]:
        app.removeHandler(h)
    if not channel:
        return None
    handler = ChannelHandler(parse_level(channel_level, logging.WARNING))
    app.addHandler(handler)
    return handler
