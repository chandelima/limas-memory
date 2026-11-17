import asyncio
import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.constants import ChatAction

import bot
import config
from responder.agent import ReplyResult

ALLOWED = 42


class FakeConversation:
    def __init__(self, answer="resposta", fail=False):
        self.answer, self.fail = answer, fail
        self.received = []
        self.active = 0
        self.max_active = 0

    def respond(self, text):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        time.sleep(0.03)
        self.active -= 1
        if self.fail:
            raise RuntimeError("segredo interno: api key abc")
        self.received.append(text)
        return ReplyResult(text=self.answer)

    def status(self):
        return "[status] pendentes: 2"

    def curate_now(self):
        return "[curador] 2 mensagem(ns) processada(s)"


def make_update(user_id=ALLOWED, chat_type="private", text="oi"):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(type=chat_type, send_action=AsyncMock()),
        message=SimpleNamespace(text=text, reply_text=AsyncMock()),
    )


def run(coro):
    return asyncio.run(coro)


def replies(update):
    return [c.args[0] for c in update.message.reply_text.await_args_list]


# ---- split_message -----------------------------------------------------------------


def test_split_short_and_empty():
    assert bot.split_message("curta") == ["curta"]
    assert bot.split_message("   ") == []


def test_split_respects_limit_and_prefers_paragraphs():
    text = ("a" * 40 + "\n\n") + ("b" * 40 + "\n\n") + ("c" * 40)
    chunks = bot.split_message(text, limit=100)
    assert chunks == ["a" * 40 + "\n\n" + "b" * 40, "c" * 40]
    assert all(len(c) <= 100 for c in chunks)


def test_split_hard_cut_when_no_separator_and_keeps_all_content():
    chunks = bot.split_message("x" * 250, limit=100)
    assert [len(c) for c in chunks] == [100, 100, 50]
    words = " ".join(["palavra"] * 300)
    pieces = bot.split_message(words, limit=100)
    assert all(len(p) <= 100 for p in pieces) and " ".join(pieces).split() == words.split()


# ---- handlers ----------------------------------------------------------------------


def test_authorized_message_is_answered_with_typing_indicator():
    conv = FakeConversation("Você toma chá.")
    update = make_update(text="o que bebo?")
    run(bot.BotHandlers(conv, ALLOWED).on_message(update, None))
    assert conv.received == ["o que bebo?"]
    assert replies(update) == ["Você toma chá."]
    update.effective_chat.send_action.assert_awaited_with(ChatAction.TYPING)


def test_long_answer_is_split_into_several_messages():
    update = make_update()
    run(bot.BotHandlers(FakeConversation("p " * 3000), ALLOWED).on_message(update, None))
    assert len(replies(update)) > 1 and all(len(r) <= bot.TELEGRAM_LIMIT for r in replies(update))


def test_other_users_and_groups_are_ignored():
    conv = FakeConversation()
    handlers = bot.BotHandlers(conv, ALLOWED)
    stranger = make_update(user_id=999)
    group = make_update(chat_type="group")
    run(handlers.on_message(stranger, None))
    run(handlers.on_message(group, None))
    run(handlers.on_status(stranger, None))
    run(handlers.on_curate(stranger, None))
    run(handlers.on_start(stranger, None))
    run(handlers.on_unknown(stranger, None))
    assert conv.received == []
    assert stranger.message.reply_text.await_count == 0 and group.message.reply_text.await_count == 0


def test_without_allowed_id_refuses_everyone_and_prints_sender_id(capsys):
    conv = FakeConversation()
    update = make_update(user_id=777)
    run(bot.BotHandlers(conv, None).on_message(update, None))
    assert conv.received == [] and update.message.reply_text.await_count == 0
    assert "TELEGRAM_ALLOWED_USER_ID=777" in capsys.readouterr().out


def test_errors_reply_generically_without_leaking_details(capsys):
    update = make_update()
    run(bot.BotHandlers(FakeConversation(fail=True), ALLOWED).on_message(update, None))
    assert replies(update) == ["Tive um problema para responder agora. Tente de novo em instantes."]
    assert "abc" not in replies(update)[0]
    assert "api key abc" in capsys.readouterr().err  # detalhe só no console


def test_commands_do_not_reach_the_responder():
    conv = FakeConversation()
    handlers = bot.BotHandlers(conv, ALLOWED)
    for method in (handlers.on_status, handlers.on_curate, handlers.on_start, handlers.on_unknown):
        run(method(make_update(text="/algo"), None))
    assert conv.received == []


def test_status_curate_start_and_unknown_texts():
    handlers = bot.BotHandlers(FakeConversation(), ALLOWED)
    for method, expected in (
        (handlers.on_status, "[status] pendentes: 2"),
        (handlers.on_curate, "[curador] 2 mensagem(ns) processada(s)"),
    ):
        update = make_update()
        run(method(update, None))
        assert replies(update) == [expected]
    start, unknown = make_update(), make_update()
    run(handlers.on_start(start, None))
    run(handlers.on_unknown(unknown, None))
    assert replies(start)[0].startswith("Olá!") and "/curate" in replies(start)[0]
    assert replies(unknown)[0].startswith("Comando desconhecido")


def test_empty_text_is_ignored():
    conv = FakeConversation()
    run(bot.BotHandlers(conv, ALLOWED).on_message(make_update(text="   "), None))
    assert conv.received == []


def test_concurrent_messages_are_handled_one_at_a_time():
    conv = FakeConversation()
    handlers = bot.BotHandlers(conv, ALLOWED)

    async def both():
        await asyncio.gather(
            handlers.on_message(make_update(text="a"), None),
            handlers.on_message(make_update(text="b"), None),
        )

    run(both())
    assert sorted(conv.received) == ["a", "b"] and conv.max_active == 1


# ---- aplicação, watcher e main -----------------------------------------------------


class FakeService:
    def __init__(self):
        self.started = threading.Event()
        self.finished = False

    async def watch(self, stop, interval=30, on_outcome=None):
        self.started.set()
        await stop.wait()
        self.finished = True


def test_application_registers_handlers_in_order_with_command_fallback_last():
    handlers = bot.BotHandlers(FakeConversation(), ALLOWED)
    app = bot.build_application("123456:fake-token", handlers, FakeService())
    registered = app.handlers[0]
    assert len(registered) == 5
    assert registered[-1].callback == handlers.on_unknown
    assert registered[-2].callback == handlers.on_message


def test_watcher_starts_with_the_bot_and_stops_on_shutdown():
    service = FakeService()
    app = bot.build_application("123456:fake-token", bot.BotHandlers(FakeConversation(), ALLOWED), service)

    async def lifecycle():
        await bot.post_init(app)
        await asyncio.sleep(0.05)
        assert service.started.is_set() and not service.finished
        await bot.post_shutdown(app)

    run(lifecycle())
    assert service.finished


def test_shutdown_without_init_is_harmless():
    app = bot.build_application("123456:fake-token", bot.BotHandlers(FakeConversation(), ALLOWED), FakeService())
    run(bot.post_shutdown(app))


def test_logging_hides_httpx_requests_because_urls_carry_the_token():
    bot.configure_logging()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


def test_main_requires_token(monkeypatch, capsys):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    assert bot.main([]) == 1
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err


def test_main_requires_openrouter_key(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:fake-token")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    assert bot.main(["--memory-dir", str(tmp_path / "m")]) == 1
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err


def test_main_wires_everything_and_starts_polling(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:fake-token")
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USER_ID", ALLOWED)
    fake_app = MagicMock()
    seen = {}

    def factory(token, handlers, service):
        seen.update(token=token, handlers=handlers, service=service)
        return fake_app

    code = bot.main(["--log", str(tmp_path / "c.json"), "--idle-seconds", "5"],
                    agent=object(), application_factory=factory)
    assert code == 0 and fake_app.run_polling.called
    assert seen["token"] == "123456:fake-token"
    assert seen["handlers"].allowed_user_id == ALLOWED
    assert seen["service"].idle_seconds == 5 and seen["service"].channel == "Telegram"
    out = capsys.readouterr().out
    assert "Bot iniciado" in out and "123456:fake-token" not in out  # o token nunca é impresso


def test_main_warns_when_allowed_id_is_missing(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:fake-token")
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USER_ID", None)
    bot.main(["--log", str(tmp_path / "c.json")], agent=object(),
             application_factory=lambda *a: MagicMock())
    assert "TELEGRAM_ALLOWED_USER_ID não definido" in capsys.readouterr().out
