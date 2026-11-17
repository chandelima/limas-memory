"""Resumo da manhã: o bot manda, num horário fixo, o que há para hoje e para os próximos dias.

O texto vem da lista de tarefas (leitura exata do arquivo, sem modelo), então não inventa
nada. Se o bot estava desligado na hora, ele recupera o resumo ao iniciar (até o meio-dia).
Quando não há nada para dizer, não manda nada. O resumo entra no histórico da conversa
como fala do assistente (já marcada como curada), para o agente saber o que foi dito.
"""

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from conversation.log import ConversationLog
from tasks.format import format_agenda
from tasks.store import TaskStore

CATCHUP_UNTIL = time(12, 0)  # depois disso, um resumo perdido não é mais enviado
RETRY_SECONDS = 300
MAX_ATTEMPTS = 6
UNDATED_LIMIT = 5  # tarefas sem data listadas no resumo


def parse_hhmm(value: str) -> time | None:
    """'06:00' -> time(6, 0); vazio, 'off' ou 'desligado' -> None (resumo desligado)."""
    value = (value or "").strip().lower()
    if value in ("", "off", "desligado", "no", "nao", "não"):
        return None
    try:
        hours, minutes = value.split(":")
        return time(int(hours), int(minutes))
    except (ValueError, TypeError):
        raise ValueError(f"horário inválido: '{value}' (use HH:MM, por exemplo 06:00, ou 'off')") from None


_OFFSET = re.compile(r"(?:utc|gmt)?\s*([+-])(\d{1,2})(?::?(\d{2}))?")


def parse_timezone(value: str) -> tzinfo:
    """Aceita nome IANA ('America/Recife'), 'UTC'/'Z' ou deslocamento ('-03:00', '-3', 'UTC-3')."""
    raw = value.strip()
    if raw.lower() in ("utc", "z", "gmt"):
        return timezone.utc
    if match := _OFFSET.fullmatch(raw.lower()):
        sign = -1 if match.group(1) == "-" else 1
        hours, minutes = int(match.group(2)), int(match.group(3) or 0)
        if hours > 14 or minutes > 59:
            raise ValueError(f"deslocamento de fuso inválido: '{raw}'")
        return timezone(sign * timedelta(hours=hours, minutes=minutes))
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(
            f"fuso desconhecido: '{raw}' (use um nome como America/Recife, UTC ou um deslocamento como -03:00)"
        ) from None


@dataclass(frozen=True)
class BriefingTime:
    at: time
    tz: tzinfo | None = None  # None = fuso da máquina

    def describe(self) -> str:
        zone = getattr(self.tz, "key", None) or (str(self.tz) if self.tz is not None else "fuso da máquina")
        return f"{self.at:%H:%M} ({zone})"


def parse_briefing_time(value: str) -> BriefingTime | None:
    """'06:00' (fuso da máquina), '06:00-03' (ISO 8601: horário + deslocamento do UTC, também
    '06:00-03:00', '06:00+05:30', '06:00Z'), '06:00 America/Recife' (nome IANA, acompanha o
    horário de verão), '06:00 UTC' ou '06:00 -03:00'. Vazio ou 'off' -> None (desligado)."""
    value = (value or "").strip()
    if not value or value.lower() in ("off", "desligado", "no", "nao", "não"):
        return None
    if compact := re.fullmatch(r"(\d{1,2}:\d{2})\s*(z|[+-]\d{1,2}(?::?\d{2})?)", value, re.IGNORECASE):
        return BriefingTime(parse_hhmm(compact.group(1)), parse_timezone(compact.group(2)))
    clock, _, zone = value.partition(" ")
    return BriefingTime(parse_hhmm(clock), parse_timezone(zone) if zone.strip() else None)


def build_briefing(tasks: TaskStore, today: date | None = None) -> str | None:
    """Texto do resumo, ou None se não há nada atrasado, de hoje, próximo ou sem data."""
    today = today or tasks.today()
    body = format_agenda(tasks.agenda(today), show_ids=False, max_undated=UNDATED_LIMIT)
    return f"Bom dia!\n\n{body}" if body else None


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
        self.tz = tz  # fuso do horário do resumo; None = fuso da máquina

    def _now(self) -> datetime:
        """Agora, no fuso do resumo: dele vêm a data de 'hoje', a hora de enviar e o dia enviado."""
        now = self.clock()
        return now.astimezone(self.tz) if self.tz is not None else now

    # ---- estado: em que dia o resumo foi enviado pela última vez -------------

    def last_sent(self) -> date | None:
        try:
            return date.fromisoformat(json.loads(self.state_path.read_text(encoding="utf-8"))["last_sent"])
        except (OSError, ValueError, KeyError, TypeError):
            return None  # sem arquivo ou ilegível: vale como "nunca enviado"

    def _mark_sent(self, day: date) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(json.dumps({"last_sent": day.isoformat()}), encoding="utf-8")
        os.replace(tmp, self.state_path)

    # ---- quando enviar ---------------------------------------------------------

    def _target(self, day: date, now: datetime) -> datetime:
        return datetime.combine(day, self.at, tzinfo=now.tzinfo)

    def due_now(self, now: datetime) -> bool:
        """Já passou da hora de hoje, ainda não foi enviado hoje e ainda é de manhã."""
        today = now.date()
        return (now >= self._target(today, now) and self.last_sent() != today
                and now.time() < CATCHUP_UNTIL)

    def seconds_until_next(self, now: datetime) -> float:
        target = self._target(now.date(), now)
        if now >= target:
            target = self._target(now.date() + timedelta(days=1), now)
        # em UTC: a diferença real, mesmo se uma mudança de horário de verão cair no meio
        return (target.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()

    # ---- envio -------------------------------------------------------------------

    async def send(self, send_text: Callable[[str], Awaitable[None]]) -> bool:
        """Monta e envia o resumo de hoje. True se resolvido (enviado ou nada a dizer)."""
        now = self._now()
        text = build_briefing(self.tasks, now.date())
        if text is not None:
            try:
                await send_text(text)
            except Exception as exc:  # noqa: BLE001 - tenta de novo depois
                print(f"[resumo] não consegui enviar o resumo da manhã: {type(exc).__name__}: {exc}",
                      file=sys.stderr, flush=True)
                return False
            if self.log is not None:
                message = self.log.append("assistente", text)
                self.log.mark_curated([message.id])  # o Curator não precisa processar o resumo
            print(f"[resumo] resumo da manhã enviado ({now:%Y-%m-%d %H:%M})", flush=True)
        self._mark_sent(now.date())
        return True

    async def _wait(self, stop: asyncio.Event, seconds: float) -> bool:
        """Espera `seconds` ou até pedirem para parar. True se pediram para parar."""
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def _deliver(self, stop: asyncio.Event, send_text) -> bool:
        """Envia com algumas tentativas. True se pediram para parar no meio."""
        for attempt in range(MAX_ATTEMPTS):
            if await self.send(send_text):
                return False
            if attempt < MAX_ATTEMPTS - 1 and await self._wait(stop, RETRY_SECONDS):
                return True
        return False

    async def run(self, stop: asyncio.Event, send_text: Callable[[str], Awaitable[None]]) -> None:
        """Laço do resumo: recupera um resumo perdido ao iniciar e depois envia todo dia."""
        if self.due_now(self._now()) and await self._deliver(stop, send_text):
            return
        while not stop.is_set():
            if await self._wait(stop, self.seconds_until_next(self._now())):
                return
            if await self._deliver(stop, send_text):
                return
