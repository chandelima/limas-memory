"""Gatilho do Curator: roda sobre as mensagens pendentes depois de um período de
inatividade (ou sob demanda). Não conhece Telegram nem terminal: quem usa chama `tick()`
periodicamente (ou `watch()`, em asyncio) e `run_now()` para forçar.
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
CONTEXT_MESSAGES = 6  # mensagens já curadas, mandadas só como contexto
KEEP_MESSAGES = 50  # histórico mantido no arquivo após curar


@dataclass
class Outcome:
    messages: int  # quantas mensagens pendentes foram enviadas ao Curator
    result: CurateResult | None = None
    error: str = ""
    busy: bool = False  # já havia uma curadoria em andamento; nada foi feito agora

    @property
    def ok(self) -> bool:
        return not self.error


CurateFn = Callable[[list[ChatMessage], str, str], CurateResult]  # (mensagens, contexto, canal)


def to_chat(m: Message) -> ChatMessage:
    return ChatMessage(m.role, m.text, m.at)


def format_context(older: list[Message]) -> str:
    if not older:
        return ""
    lines = ["Mensagens anteriores, JÁ processadas (só contexto; não grave de novo):"]
    lines += [f"- [{m.role} ({m.at})] {m.text}" for m in older]
    return "\n".join(lines)


class CurationService:
    def __init__(
        self,
        log: ConversationLog,
        curate_fn: CurateFn,
        *,
        channel: str = "desconhecido",
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
        self._failure_logged = False  # só a 1ª falha de uma sequência vai como ERROR
        self._last_push_warning = ""  # o aviso de push pendente repete só quando o motivo muda

    # ---- estado -----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._busy.locked()

    def seconds_idle(self, now: datetime | None = None) -> float | None:
        last = self.log.last_message_at()
        if last is None:
            return None
        return ((now or datetime.now().astimezone()) - last).total_seconds()

    def due(self, now: datetime | None = None) -> bool:
        """Há pendentes, a conversa ficou inativa o suficiente e não estamos em espera
        após uma falha."""
        now = now or datetime.now().astimezone()
        if not self.log.pending():
            return False
        idle = self.seconds_idle(now)
        if idle is None or idle < self.idle_seconds:
            return False
        if self._failed_at and (now - self._failed_at).total_seconds() < self.retry_seconds:
            return False
        return True

    # ---- execução ---------------------------------------------------------

    def tick(self, now: datetime | None = None) -> Outcome | None:
        """Chame periodicamente. Roda o Curator se estiver na hora; senão, None."""
        now = now or datetime.now().astimezone()
        if not self.due(now):
            return None
        outcome = self.run_now(now)
        return None if outcome is not None and outcome.busy else outcome

    def run_now(self, now: datetime | None = None) -> Outcome | None:
        """Cura as mensagens pendentes agora, sem esperar a inatividade.
        None se não há nada pendente; Outcome(busy=True) se já há uma curadoria em
        andamento (nada é feito)."""
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
            except Exception as exc:  # noqa: BLE001 - falha vira Outcome; mensagens ficam pendentes
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

    # ---- logs (só contagens e metadados; nunca o conteúdo das mensagens ou da memória) ----

    def _log_failure(self, count: int, exc: Exception) -> None:
        if not self._failure_logged:
            self._failure_logged = True
            log.error("curadoria falhou (%d mensagem(ns) seguem pendentes; nova tentativa em %.0fs)",
                      count, self.retry_seconds, exc_info=exc)
        else:  # as tentativas seguintes ficam só no console, para não repetir o aviso no canal
            log.info("curadoria ainda falhando: %s", self.last_error)

    def _log_success(self, count: int, result: CurateResult, seconds: float) -> None:
        usage = result.usage
        cost = f"US$ {usage.cost:.4f}" if usage.cost_reported else "n/d"
        log.info("curadoria concluída: %d mensagem(ns), %d chamada(s) de tool, %d tokens, custo %s, %.1fs",
                 count, len(result.trace), usage.total_tokens, cost, seconds)
        commits = [e.result for e in result.trace if e.name in ("commit_memory", "sync_memory")]
        pending = next((r for r in commits if "Push pendente" in r), None)
        if pending is not None:
            reason = pending.split("Push pendente:", 1)[-1].strip()[:200]
            if reason != self._last_push_warning:  # avisa quando é novo, não a cada curadoria
                self._last_push_warning = reason
                log.warning("push da memória pendente: %s", reason)
            else:
                log.debug("push da memória ainda pendente: %s", reason)
        elif any("enviado ao remote" in r or "em dia com o remote" in r for r in commits):
            self._last_push_warning = ""

    async def watch(
        self,
        stop: asyncio.Event,
        interval: float = 30,
        on_outcome: Callable[[Outcome], object] | None = None,
    ) -> None:
        """Laço asyncio: verifica a cada `interval` s, sem bloquear o loop de eventos."""
        while not stop.is_set():
            outcome = await asyncio.to_thread(self.tick)
            if outcome and on_outcome:
                try:
                    result = on_outcome(outcome)
                    if inspect.isawaitable(result):  # o callback pode ser assíncrono
                        await result
                except Exception as exc:  # noqa: BLE001 - um callback com defeito não derruba o laço
                    log.error("erro ao tratar o resultado da curadoria", exc_info=exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
