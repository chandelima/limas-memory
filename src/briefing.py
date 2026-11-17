"""Morning briefing: at a fixed time the bot sends what is on for today and the next days.

The text comes from the task list (an exact read of the file, no model), so it does not
make anything up. If the bot was off at that time, it catches up on start (until noon).
When there is nothing to say, it sends nothing. The briefing enters the conversation
history as an assistant turn (already marked as curated), so the agent knows what was said.
"""

import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from applog import get_logger
from conversation.log import ConversationLog
from i18n import t
from tasks.format import format_agenda
from tasks.store import TaskStore

log = get_logger("briefing")

CATCHUP_UNTIL = time(12, 0)  # after this, a missed briefing is no longer sent
RETRY_SECONDS = 300
MAX_ATTEMPTS = 6
UNDATED_LIMIT = 5  # undated tasks listed in the briefing


def parse_hhmm(value: str) -> time | None:
    """'06:00' -> time(6, 0); empty, 'off' or 'disabled' -> None (briefing disabled)."""
    value = (value or "").strip().lower()
    if value in ("", "off", "disabled", "no"):
        return None
    try:
        hours, minutes = value.split(":")
        return time(int(hours), int(minutes))
    except (ValueError, TypeError):
        raise ValueError(f"invalid time: '{value}' (use HH:MM, e.g. 06:00, or 'off')") from None


_OFFSET = re.compile(r"(?:utc|gmt)?\s*([+-])(\d{1,2})(?::?(\d{2}))?")


def parse_timezone(value: str) -> tzinfo:
    """Accept an IANA name ('America/Recife'), 'UTC'/'Z' or an offset ('-03:00', '-3', 'UTC-3')."""
    raw = value.strip()
    if raw.lower() in ("utc", "z", "gmt"):
        return timezone.utc
    if match := _OFFSET.fullmatch(raw.lower()):
        sign = -1 if match.group(1) == "-" else 1
        hours, minutes = int(match.group(2)), int(match.group(3) or 0)
        if hours > 14 or minutes > 59:
            raise ValueError(f"invalid time zone offset: '{raw}'")
        return timezone(sign * timedelta(hours=hours, minutes=minutes))
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(
            f"unknown time zone: '{raw}' (use a name like America/Recife, UTC or an offset like -03:00)"
        ) from None


@dataclass(frozen=True)
class BriefingTime:
    at: time
    tz: tzinfo | None = None  # None = machine time zone

    def describe(self) -> str:
        zone = getattr(self.tz, "key", None) or (str(self.tz) if self.tz is not None else "machine time zone")
        return f"{self.at:%H:%M} ({zone})"


def parse_briefing_time(value: str) -> BriefingTime | None:
    """'06:00' (machine time zone), '06:00-03' (ISO 8601: time + UTC offset, also
    '06:00-03:00', '06:00+05:30', '06:00Z'), '06:00 America/Recife' (IANA name, follows
    daylight saving), '06:00 UTC' or '06:00 -03:00'. Empty or 'off' -> None (disabled)."""
    value = (value or "").strip()
    if not value or value.lower() in ("off", "disabled", "no"):
        return None
    if compact := re.fullmatch(r"(\d{1,2}:\d{2})\s*(z|[+-]\d{1,2}(?::?\d{2})?)", value, re.IGNORECASE):
        return BriefingTime(parse_hhmm(compact.group(1)), parse_timezone(compact.group(2)))
    clock, _, zone = value.partition(" ")
    return BriefingTime(parse_hhmm(clock), parse_timezone(zone) if zone.strip() else None)


def build_briefing(tasks: TaskStore, today: date | None = None) -> str | None:
    """Text of the briefing, or None if nothing is overdue, due today, upcoming or undated."""
    today = today or tasks.today()
    body = format_agenda(tasks.agenda(today), show_ids=False, max_undated=UNDATED_LIMIT)
    return f"{t('briefing_greeting')}\n\n{body}" if body else None


class BriefingScheduler:
    def __init__(
        self,
        tasks: TaskStore,
        at: time,
        state_path: Path,
        log: ConversationLog | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        tz: tzinfo | None = None,
    ):
        self.tasks = tasks
        self.at = at
        self.state_path = Path(state_path)
        self.log = log
        self.clock = clock
        self.tz = tz  # time zone of the briefing time; None = machine time zone

    def _now(self) -> datetime:
        """Now, in the briefing's time zone: it gives 'today's' date, the send time and the day sent."""
        now = self.clock()
        return now.astimezone(self.tz) if self.tz is not None else now

    # ---- state: the day the briefing was last sent ------------------------------

    def last_sent(self) -> date | None:
        try:
            return date.fromisoformat(json.loads(self.state_path.read_text(encoding="utf-8"))["last_sent"])
        except (OSError, ValueError, KeyError, TypeError):
            return None  # no file or unreadable: counts as "never sent"

    def _mark_sent(self, day: date) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(json.dumps({"last_sent": day.isoformat()}), encoding="utf-8")
        os.replace(tmp, self.state_path)

    # ---- when to send --------------------------------------------------------

    def _target(self, day: date, now: datetime) -> datetime:
        return datetime.combine(day, self.at, tzinfo=now.tzinfo)

    def due_now(self, now: datetime) -> bool:
        """Today's time has passed, it has not been sent today and it is still morning."""
        today = now.date()
        return (now >= self._target(today, now) and self.last_sent() != today
                and now.time() < CATCHUP_UNTIL)

    def seconds_until_next(self, now: datetime) -> float:
        target = self._target(now.date(), now)
        if now >= target:
            target = self._target(now.date() + timedelta(days=1), now)
        # in UTC: the real difference, even if a daylight-saving change falls in between
        return (target.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()

    # ---- sending ---------------------------------------------------------------

    async def send(self, send_text: Callable[[str], Awaitable[None]]) -> bool:
        """Build and send today's briefing. True if resolved (sent or nothing to say)."""
        now = self._now()
        text = build_briefing(self.tasks, now.date())
        if text is not None:
            try:
                await send_text(text)
            except Exception as exc:  # noqa: BLE001 - try again later
                log.warning("could not send the morning briefing", exc_info=exc)
                return False
            if self.log is not None:
                message = self.log.append("assistant", text)
                self.log.mark_curated([message.id])  # the Curator does not need to process the briefing
            log.info("morning briefing sent (%s)", f"{now:%Y-%m-%d %H:%M}")
        self._mark_sent(now.date())
        return True

    async def _wait(self, stop: asyncio.Event, seconds: float) -> bool:
        """Wait `seconds` or until asked to stop. True if asked to stop."""
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def _deliver(self, stop: asyncio.Event, send_text) -> bool:
        """Send with a few attempts. True if asked to stop in the middle."""
        for attempt in range(MAX_ATTEMPTS):
            if await self.send(send_text):
                return False
            if attempt < MAX_ATTEMPTS - 1 and await self._wait(stop, RETRY_SECONDS):
                return True
        return False

    async def run(self, stop: asyncio.Event, send_text: Callable[[str], Awaitable[None]]) -> None:
        """Briefing loop: catches up on a missed briefing at start and then sends every day."""
        if self.due_now(self._now()) and await self._deliver(stop, send_text):
            return
        while not stop.is_set():
            if await self._wait(stop, self.seconds_until_next(self._now())):
                return
            if await self._deliver(stop, send_text):
                return
