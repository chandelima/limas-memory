"""Duplas de teste compartilhadas pelos testes do bot (sem rede, sem Telegram)."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

ALLOWED = 42


class FakeGateway:
    """Imita TelegramGateway e registra, em ordem, tudo o que o bot fez."""

    def __init__(self):
        self.events = []  # ("typing_on"|"typing_off"|"text"|"file", valor), na ordem
        self.texts, self.files = [], []
        self.data = b"%PDF-1.4"
        self.download_error = None
        self.send_file_error = None
        self.registered = None

    async def send_text(self, chat_id, text):
        self.texts.append((chat_id, text))
        self.events.append(("text", text))

    async def send_file(self, chat_id, attachment):
        if self.send_file_error:
            raise self.send_file_error
        self.files.append((chat_id, attachment))
        self.events.append(("file", attachment.id))

    async def download(self, file_id):
        self.events.append(("download", file_id))
        if self.download_error:
            raise self.download_error
        return self.data

    @asynccontextmanager
    async def typing(self, chat_id):
        self.events.append(("typing_on", chat_id))
        try:
            yield
        finally:
            self.events.append(("typing_off", chat_id))

    async def register_commands(self, commands, allowed_user_id):
        self.registered = ([c.command for c in commands], allowed_user_id)

    # conveniências para as asserções
    def sent(self):
        return [t for _, t in self.texts]

    def index(self, kind, value=None):
        for i, (k, v) in enumerate(self.events):
            if k == kind and (value is None or v == value or (isinstance(v, str) and value in v)):
                return i
        raise AssertionError(f"evento {kind!r}/{value!r} não encontrado em {self.events}")


def make_update(*, text=None, document=None, photo=None, caption="", user_id=ALLOWED, chat_type="private",
                message_id=555):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id, type=chat_type),
        message=SimpleNamespace(text=text, document=document, photo=photo, caption=caption,
                                message_id=message_id, reply_text=AsyncMock()),
    )


def make_context():
    return SimpleNamespace(bot=object())  # o gateway falso ignora o bot


def pdf_doc(name="voucher.pdf", mime="application/pdf", size=2000, file_id="TGFILE-A"):
    return SimpleNamespace(file_id=file_id, file_unique_id="uniqA", file_name=name, mime_type=mime, file_size=size)
