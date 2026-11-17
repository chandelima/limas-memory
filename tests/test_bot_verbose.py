import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot
import config
from attachments.registry import Attachment
from curator.agent import ToolEvent
from responder.agent import ReplyResult

ALLOWED = 42


@pytest.fixture(autouse=True)
def no_telegram_calls(monkeypatch):
    monkeypatch.setattr(bot, "register_commands", AsyncMock())


class FakeConversation:
    def __init__(self, result):
        self.result = result

    def respond(self, text, attachments=None):
        return self.result


def update_with(text="o que bebo?"):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=ALLOWED),
        effective_chat=SimpleNamespace(id=ALLOWED, type="private", send_action=AsyncMock()),
        message=SimpleNamespace(text=text, reply_text=AsyncMock()),
    )


def run_message(handlers, update):
    ctx = SimpleNamespace(bot=SimpleNamespace(send_document=AsyncMock(), send_photo=AsyncMock()))
    asyncio.run(handlers.on_message(update, ctx))


def att():
    return Attachment(id="ab12cd34", file_id="F", file_unique_id="u", kind="document", chat_id=ALLOWED,
                      message_id=1, filename="voucher.pdf", mime="application/pdf", size=1, caption="",
                      received_at="2026-11-17T10:00:00-03:00")


def test_verbose_prints_memory_queries_and_resent_files(capsys):
    result = ReplyResult(
        text="Você toma chá.",
        trace=[ToolEvent("list_memory", {}, "..."), ToolEvent("read_memory", {"path": "preferencias/bebidas.md"}, "...")],
        files=[att()],
    )
    run_message(bot.BotHandlers(FakeConversation(result), ALLOWED, verbose=True), update_with())
    out = capsys.readouterr().out
    assert "[memória] list_memory()" in out
    assert "[memória] read_memory(path='preferencias/bebidas.md')" in out
    assert "[arquivo] reenviando anexo:ab12cd34 (voucher.pdf)" in out


def test_default_mode_prints_nothing_about_tools(capsys):
    result = ReplyResult(text="ok", trace=[ToolEvent("list_memory", {}, "...")])
    run_message(bot.BotHandlers(FakeConversation(result), ALLOWED), update_with())
    assert "[memória]" not in capsys.readouterr().out


def test_main_passes_verbose_flag_to_handlers(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:fake-token")
    seen = {}

    def factory(token, handlers, service):
        seen["verbose"] = handlers.verbose
        return MagicMock()

    for flags, expected in (([], False), (["--verbose"], True)):
        bot.main(["--log", str(tmp_path / "c.json"), "--memory-dir", str(tmp_path / "mem"), *flags],
                 agent=object(), application_factory=factory)
        assert seen["verbose"] is expected
