"""Camada de log da aplicação: console + um canal do Telegram.

- Console: todos os eventos a partir de LOG_LEVEL (padrão INFO), com hora, nível e origem.
- Canal do Telegram (LOG_CHANNEL_ID): só o que importa (a partir de LOG_CHANNEL_LEVEL, padrão
  WARNING, mais os avisos de ciclo de vida marcados com extra={"channel": True}), enviado em
  lote, sem repetir linhas iguais seguidas e sem estourar o limite de mensagens do Telegram.
- Privacidade: os logs registram IDs, contagens, durações e o tipo/mensagem de erros; nunca o
  texto das suas mensagens, o conteúdo da memória nem o relatório do Curator. Segredos
  (token do bot, chaves de API, tokens do git) são mascarados em todos os destinos.

Os logs do próprio envio ao canal (logger "limas.logship") vão só para o console, para que uma
falha ao enviar não gere mais logs a enviar.
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
CHANNEL_LIMIT = 3900  # margem sob o limite de 4096 caracteres do Telegram
_HANDLER_MARK = "_limas_console"

_PATTERNS = [
    re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}"),  # token de bot do Telegram (na URL vem colado em "bot")
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),  # chaves tipo sk-...
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),  # tokens do GitHub
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\bAuthorization:\s*\S+(?:\s+\S+)?"),
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password|senha)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+=]{12,}"),
]


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{ROOT_NAME}.{name}")


def known_secrets() -> list[str]:
    """Valores sensíveis da configuração atual (lidos na hora, para acompanhar mudanças)."""
    values = (config.TELEGRAM_BOT_TOKEN, config.OPENROUTER_API_KEY, config.MEMORY_GIT_TOKEN)
    return [v for v in values if v and len(v) >= 8]


def redact(text: str, secrets: Iterable[str] | None = None) -> str:
    for secret in (known_secrets() if secrets is None else secrets):
        text = text.replace(secret, "***")
    for pattern in _PATTERNS:
        text = pattern.sub("***", text)
    return text


class RedactingFormatter(logging.Formatter):
    """Formata e mascara segredos, inclusive no texto de exceções."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def parse_level(value: str | int, default: int) -> int:
    if isinstance(value, int):
        return value
    level = logging.getLevelName(str(value).strip().upper())
    return level if isinstance(level, int) else default


def parse_channel_id(value: str) -> int | str | None:
    """'-1001234567890' -> int; '@meucanal' -> str; vazio -> None (canal desligado)."""
    value = (value or "").strip()
    if not value:
        return None
    if value.lstrip("-").isdigit():
        return int(value)
    if value.startswith("@") and len(value) > 1:
        return value
    raise ValueError(f"LOG_CHANNEL_ID inválido: '{value}' (use o ID numérico, como -1001234567890, ou @nomedocanal)")


# ---- canal do Telegram: fila na emissão, envio em lote por uma tarefa -----------------------


class ChannelHandler(logging.Handler):
    """Coloca numa fila (sem bloquear e seguro entre threads) os registros que vão ao canal."""

    def __init__(self, level: int = logging.WARNING, maxsize: int = 500):
        super().__init__(logging.DEBUG)
        self.min_level = level
        self.queue: queue.Queue[str] = queue.Queue(maxsize=maxsize)
        self.dropped = 0

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == SHIP_NAME or record.name.startswith(SHIP_NAME + "."):
            return  # o envio do próprio log nunca volta para a fila
        if record.levelno < self.min_level and not getattr(record, "channel", False):
            return
        try:
            line = self.channel_line(record)
            try:
                self.queue.put_nowait(line)
            except queue.Full:  # descarta a mais antiga e conta
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    pass
                self.dropped += 1
                self.queue.put_nowait(line)
        except Exception:  # noqa: BLE001 - log nunca derruba o programa
            self.handleError(record)

    @staticmethod
    def channel_line(record: logging.LogRecord) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        text = f"{stamp} #{record.levelname} {record.name.removeprefix(ROOT_NAME + '.')}: {record.getMessage()}"
        if record.exc_info and record.exc_info[1] is not None:
            err = record.exc_info[1]
            text += f" [{type(err).__name__}: {err}]"  # só tipo e mensagem; o traceback fica no console
        return " ".join(redact(text).split())  # uma linha só


def pack(lines: list[str], limit: int = CHANNEL_LIMIT) -> list[str]:
    """Junta linhas em mensagens de até `limit` caracteres (linha enorme é cortada)."""
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
    """Junta linhas iguais seguidas (ignorando a hora): 'X' 'X' 'X' -> 'X (x3)'."""
    out: list[tuple[str, int]] = []
    for line in lines:
        key = line.split(" ", 1)[1] if " " in line else line
        if out and out[-1][0].split(" ", 1)[-1] == key:
            out[-1] = (out[-1][0], out[-1][1] + 1)
        else:
            out.append((line, 1))
    return [f"{line} (x{n})" if n > 1 else line for line, n in out]


class LogShipper:
    """Esvazia a fila do ChannelHandler a cada `flush_seconds` e envia em lote ao canal."""

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
        self._pending: list[str] = []  # linhas que falharam ao enviar, para tentar de novo
        self._last_sent = 0.0
        self._log = logging.getLogger(SHIP_NAME)

    def _drain(self) -> list[str]:
        lines, self._pending = self._pending, []
        while True:
            try:
                lines.append(self.handler.queue.get_nowait())
            except queue.Empty:
                break
        if self.handler.dropped:
            lines.append(f"({self.handler.dropped} linhas de log descartadas por excesso)")
            self.handler.dropped = 0
        return lines

    async def flush(self) -> int:
        """Envia o que há na fila. Devolve quantas mensagens foram enviadas."""
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
            except Exception as exc:  # noqa: BLE001 - guarda o resto para a próxima tentativa
                self._log.warning("não consegui enviar o log ao canal: %s: %s", type(exc).__name__, exc)
                rest = [ln for m in messages[i:] for ln in m.split("\n") if not (self.header and ln == self.header)]
                self._pending = rest[-self.keep_on_failure:]
                return sent
            self._last_sent = time.monotonic()
            sent += 1
        return sent

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.flush_seconds)
            except asyncio.TimeoutError:
                pass
            await self.flush()
        await self.flush()  # despedida: o que sobrou ao encerrar


# ---- configuração ------------------------------------------------------------------------------------


def configure_logging(console_level: int | str = logging.INFO, channel_level: int | str = logging.WARNING,
                      channel: bool = False) -> ChannelHandler | None:
    """Liga o console (e, se `channel`, a fila do canal). Pode ser chamada várias vezes.
    Devolve o ChannelHandler quando o canal está ligado."""
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
    # bibliotecas ficam em WARNING: o httpx loga a URL de cada chamada, e ela contém o token do bot
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
