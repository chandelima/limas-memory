import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot
from curator.agent import CurateResult, ToolEvent
from curator.service import Outcome
from responder.agent import ReplyResult

ALLOWED = 42


@pytest.fixture(autouse=True)
def no_telegram_calls(monkeypatch):
    monkeypatch.setattr(bot, "register_commands", AsyncMock())


@pytest.fixture
def send(monkeypatch):
    from telegram.ext import ExtBot

    mock = AsyncMock()
    monkeypatch.setattr(ExtBot, "send_message", mock)
    return mock


class FakeConversation:
    def __init__(self, answer="resposta"):
        self.answer = answer

    def respond(self, text, attachments=None):
        return ReplyResult(text=self.answer)

    def status(self):
        return "[status]"

    def curate_now(self):
        return "[curador] já há uma curadoria em andamento; aguarde ela terminar."


class FakeService:
    def __init__(self):
        self.started = threading.Event()

    async def watch(self, stop, interval=30, on_outcome=None):
        self.started.set()
        await stop.wait()


def run(coro):
    return asyncio.run(coro)


def make_update(text="oi"):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=ALLOWED),
        effective_chat=SimpleNamespace(type="private", send_action=AsyncMock()),
        message=SimpleNamespace(text=text, reply_text=AsyncMock()),
    )


def ok_outcome(report="Criei 1 documento.", messages=2):
    result = CurateResult(report=report, trace=[ToolEvent("commit_memory", {}, "OK")])
    return Outcome(messages, result=result)


def make_app(allowed=ALLOWED, service=None):
    handlers = bot.BotHandlers(FakeConversation(), allowed)
    app = bot.build_application("123456:fake-token", handlers, service or FakeService())
    return app


def sent_texts(send):
    return [c.kwargs["text"] for c in send.await_args_list]


def test_to_plain_strips_markdown_markers_only():
    text = "## Título\n**Criados:** `a.md` e __b__\n- item\n2 * 3 * 4"
    assert bot.to_plain(text) == "Título\nCriados: a.md e b\n- item\n2 * 3 * 4"


def test_replies_are_sent_as_plain_text():
    update = make_update()
    run(bot.BotHandlers(FakeConversation("Você toma **chá**."), ALLOWED).on_message(update, None))
    assert [c.args[0] for c in update.message.reply_text.await_args_list] == ["Você toma chá."]


def test_automatic_report_is_sent_to_the_allowed_user(send, capsys):
    app = make_app()
    run(bot.notify_outcome(app, ok_outcome("**Criados:** 1 doc")))
    assert send.await_args.kwargs["chat_id"] == ALLOWED
    text = sent_texts(send)[0]
    assert text.startswith("[curador] 2 mensagem(ns) processada(s) (commit_memory)")
    assert "Criados: 1 doc" in text and "**" not in text
    assert "[curador] 2 mensagem(ns)" in capsys.readouterr().out  # e também no console


def test_long_report_is_split_in_several_messages(send):
    app = make_app()
    run(bot.notify_outcome(app, ok_outcome("palavra " * 2000)))
    assert send.await_count > 1
    assert all(len(t) <= bot.TELEGRAM_LIMIT for t in sent_texts(send))


def test_report_is_console_only_without_allowed_user(send, capsys):
    app = make_app(allowed=None)
    run(bot.notify_outcome(app, ok_outcome()))
    assert send.await_count == 0 and "[curador]" in capsys.readouterr().out


def test_failure_is_notified_once_per_streak_and_success_resets_it(send):
    app = make_app()
    fail = Outcome(2, error="RuntimeError: sem rede")
    run(bot.notify_outcome(app, fail))
    run(bot.notify_outcome(app, fail))  # nova tentativa falha: não repete o aviso
    assert send.await_count == 1
    assert "falhou" in sent_texts(send)[0] and "sem rede" in sent_texts(send)[0]
    run(bot.notify_outcome(app, ok_outcome()))  # sucesso: relatório normal e reinicia a contagem
    run(bot.notify_outcome(app, fail))
    assert send.await_count == 3


def test_failure_to_send_report_does_not_raise(send, capsys):
    app = make_app()
    send.side_effect = RuntimeError("telegram fora")
    run(bot.notify_outcome(app, ok_outcome()))  # não levanta
    assert "não consegui enviar o relatório" in capsys.readouterr().err


def test_watcher_callback_sends_report_through_the_bot(send):
    class ReportingService(FakeService):
        async def watch(self, stop, interval=30, on_outcome=None):
            self.started.set()
            await on_outcome(ok_outcome("Feito"))  # o bot passa um callback assíncrono
            await stop.wait()

    app = make_app(service=ReportingService())

    async def lifecycle():
        await bot.post_init(app)
        await asyncio.sleep(0.05)
        await bot.post_shutdown(app)

    run(lifecycle())
    assert "Feito" in sent_texts(send)[0]


def test_curate_command_reports_busy_state_to_the_user():
    update = make_update()
    run(bot.BotHandlers(FakeConversation(), ALLOWED).on_curate(update, None))
    assert "já há uma curadoria em andamento" in update.message.reply_text.await_args.args[0]
