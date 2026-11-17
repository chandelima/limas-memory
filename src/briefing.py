"""Resumo da manhã: o bot manda, num horário fixo, o que há para hoje e para os próximos dias.

O texto vem da lista de tarefas (leitura exata do arquivo, sem modelo), então não inventa
nada. Se o bot estava desligado na hora, ele recupera o resumo ao iniciar (até o meio-dia).
Quando não há nada para dizer, não manda nada. O resumo entra no histórico da conversa
como fala do assistente (já marcada como curada), para o agente saber o que foi dito.
"""

import asyncio
import json
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Awaitable, Callable

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
    ):
        self.tasks = tasks
        self.at = at
        self.state_path = Path(state_path)
        self.log = log
        self.clock = clock

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
        return (target - now).total_seconds()

    # ---- envio -------------------------------------------------------------------

    async def send(self, send_text: Callable[[str], Awaitable[None]]) -> bool:
        """Monta e envia o resumo de hoje. True se resolvido (enviado ou nada a dizer)."""
        now = self.clock()
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
        if self.due_now(self.clock()) and await self._deliver(stop, send_text):
            return
        while not stop.is_set():
            if await self._wait(stop, self.seconds_until_next(self.clock())):
                return
            if await self._deliver(stop, send_text):
                return
