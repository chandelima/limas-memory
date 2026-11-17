"""Tudo o que o bot faz diretamente na API do Telegram, numa classe só.

Os handlers (bot.py) tratam de autorização, fila e conversa; esta classe só sabe enviar
texto, reenviar arquivos, baixar arquivos, mostrar "digitando..." e publicar comandos.
Assim detalhes da biblioteca (ReplyParameters, ChatAction, limites de tamanho, texto
simples) ficam aqui, e os testes dos handlers usam um gateway falso.
"""

import asyncio
import re
import sys
from contextlib import asynccontextmanager, suppress

from telegram import BotCommandScopeChat, ReplyParameters
from telegram.constants import ChatAction

TELEGRAM_LIMIT = 4096  # tamanho máximo de uma mensagem do Telegram
TYPING_INTERVAL = 4  # o indicador "digitando..." dura ~5s


def to_plain(text: str) -> str:
    """O bot envia texto simples (sem parse_mode): tira a marcação Markdown que os
    modelos às vezes usam (**negrito**, __negrito__, `código`, títulos com #)."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"`+([^`\n]+)`+", r"\1", text)
    return re.sub(r"^#{1,6}[ \t]+", "", text, flags=re.MULTILINE)


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Divide em pedaços de até `limit`, preferindo quebrar em parágrafo, linha ou espaço."""
    text = text.strip()
    chunks = []
    while len(text) > limit:
        cut = limit
        for sep in ("\n\n", "\n", " "):
            idx = text.rfind(sep, 0, limit)
            if idx > 0:
                cut = idx
                break
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


class TelegramGateway:
    def __init__(self, bot):
        self.bot = bot

    async def send_text(self, chat_id: int, text: str) -> None:
        """Envia texto simples, dividido no limite do Telegram."""
        for chunk in split_message(to_plain(text)) or ["(sem resposta)"]:
            await self.bot.send_message(chat_id=chat_id, text=chunk)

    async def send_file(self, chat_id: int, attachment) -> None:
        """Reenvia um arquivo pelo file_id, citando a mensagem original quando ela existir."""
        reply_to = ReplyParameters(message_id=attachment.message_id, allow_sending_without_reply=True)
        if attachment.kind == "photo":
            await self.bot.send_photo(chat_id=chat_id, photo=attachment.file_id, reply_parameters=reply_to)
        else:
            await self.bot.send_document(chat_id=chat_id, document=attachment.file_id, reply_parameters=reply_to)

    async def download(self, file_id: str) -> bytes:
        tg_file = await self.bot.get_file(file_id)
        return bytes(await tg_file.download_as_bytearray())

    @asynccontextmanager
    async def typing(self, chat_id: int):
        """Mantém o "digitando..." ligado enquanto o bloco roda (inclusive esperando a vez)."""
        task = asyncio.create_task(self._typing_loop(chat_id))
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _typing_loop(self, chat_id: int) -> None:
        while True:
            try:
                await self.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception:  # noqa: BLE001 - o indicador é um luxo: nunca derruba o fluxo
                pass
            await asyncio.sleep(TYPING_INTERVAL)

    async def register_commands(self, commands: list, allowed_user_id: int | None) -> None:
        """Publica a lista de comandos (menu "/" do Telegram). Com usuário autorizado, só
        ele vê o menu; sem ele, vale o escopo padrão. Falha aqui não impede o bot de rodar."""
        scope = BotCommandScopeChat(chat_id=allowed_user_id) if allowed_user_id else None
        try:
            await self.bot.set_my_commands(commands, scope=scope)
        except Exception as exc:  # noqa: BLE001
            print(f"[bot] não consegui registrar os comandos no Telegram: {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
