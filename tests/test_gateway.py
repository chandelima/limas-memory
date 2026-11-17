import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ChatAction

import telegram_gateway as tg
from attachments.registry import Attachment
from telegram_gateway import TelegramGateway, split_message, to_plain


def run(coro):
    return asyncio.run(coro)


def fake_bot(**kw):
    return SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock(), send_document=AsyncMock(),
                           send_chat_action=AsyncMock(), set_my_commands=AsyncMock(), get_file=AsyncMock(), **kw)


def att(kind="document"):
    return Attachment(id="ab12cd34", file_id="FILEID", file_unique_id="u", kind=kind, chat_id=42, message_id=77,
                      filename="v.pdf", mime="application/pdf", size=1, caption="", received_at="2026-11-17T10:00:00-03:00")


# ---- text ----------------------------------------------------------------------------


def test_to_plain_strips_markdown_markers_only():
    text = "## Title\n**Created:** `a.md` and __b__\n- item\n2 * 3 * 4"
    assert to_plain(text) == "Title\nCreated: a.md and b\n- item\n2 * 3 * 4"


def test_split_short_and_empty():
    assert split_message("short") == ["short"] and split_message("   ") == []


def test_split_respects_limit_and_prefers_paragraphs():
    text = ("a" * 40 + "\n\n") + ("b" * 40 + "\n\n") + ("c" * 40)
    chunks = split_message(text, limit=100)
    assert chunks == ["a" * 40 + "\n\n" + "b" * 40, "c" * 40]


def test_split_hard_cut_without_separator_and_keeps_content():
    assert [len(c) for c in split_message("x" * 250, limit=100)] == [100, 100, 50]
    words = " ".join(["word"] * 300)
    pieces = split_message(words, limit=100)
    assert all(len(p) <= 100 for p in pieces) and " ".join(pieces).split() == words.split()


def test_send_text_is_plain_and_split():
    bot = fake_bot()
    run(TelegramGateway(bot).send_text(42, "You drink **tea**. " + "p " * 3000))
    texts = [c.kwargs["text"] for c in bot.send_message.await_args_list]
    assert texts[0].startswith("You drink tea.") and "**" not in texts[0]
    assert len(texts) > 1 and all(len(t) <= tg.TELEGRAM_LIMIT for t in texts)
    assert all(c.kwargs["chat_id"] == 42 for c in bot.send_message.await_args_list)


def test_send_text_with_empty_text_still_sends_something():
    bot = fake_bot()
    run(TelegramGateway(bot).send_text(42, "   "))
    assert bot.send_message.await_args.kwargs["text"] == "(no reply)"


# ---- files ---------------------------------------------------------------------------


def test_send_file_uses_file_id_and_quotes_the_original_message():
    bot = fake_bot()
    gw = TelegramGateway(bot)
    run(gw.send_file(42, att("document")))
    run(gw.send_file(42, att("photo")))
    doc, photo = bot.send_document.await_args.kwargs, bot.send_photo.await_args.kwargs
    assert doc["chat_id"] == 42 and doc["document"] == "FILEID"
    assert doc["reply_parameters"].message_id == 77 and doc["reply_parameters"].allow_sending_without_reply is True
    assert photo["photo"] == "FILEID" and photo["reply_parameters"].message_id == 77


def test_download_returns_bytes():
    bot = fake_bot()
    bot.get_file.return_value = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b"%PDF")))
    assert run(TelegramGateway(bot).download("FILEID")) == b"%PDF"
    bot.get_file.assert_awaited_once_with("FILEID")


# ---- typing --------------------------------------------------------------------------


def test_typing_shows_immediately_repeats_and_stops_on_exit(monkeypatch):
    monkeypatch.setattr(tg, "TYPING_INTERVAL", 0.01)
    bot = fake_bot()

    async def scenario():
        async with TelegramGateway(bot).typing(42):
            await asyncio.sleep(0.06)
        calls_inside = bot.send_chat_action.await_count
        await asyncio.sleep(0.05)
        return calls_inside

    inside = run(scenario())
    assert inside >= 3  # immediate and repeated
    assert bot.send_chat_action.await_count == inside  # it stopped when leaving the block
    assert bot.send_chat_action.await_args.kwargs == {"chat_id": 42, "action": ChatAction.TYPING}


def test_typing_survives_api_errors_and_block_exceptions(monkeypatch):
    monkeypatch.setattr(tg, "TYPING_INTERVAL", 0.01)
    bot = fake_bot()
    bot.send_chat_action.side_effect = RuntimeError("network")

    async def scenario():
        async with TelegramGateway(bot).typing(42):
            await asyncio.sleep(0.03)  # an error in the indicator does not break the block
            raise ValueError("block error")

    with pytest.raises(ValueError, match="block error"):
        run(scenario())
    assert bot.send_chat_action.await_count >= 1


# ---- commands ------------------------------------------------------------------------


def test_register_commands_scope_for_allowed_user_or_default():
    from telegram import BotCommand

    commands = [BotCommand("status", "State"), BotCommand("curate", "Process")]
    bot = fake_bot()
    gw = TelegramGateway(bot)
    run(gw.register_commands(commands, 42))
    assert bot.set_my_commands.await_args.args[0] == commands
    assert bot.set_my_commands.await_args.kwargs["scope"].chat_id == 42
    run(gw.register_commands(commands, None))
    assert bot.set_my_commands.await_args.kwargs["scope"] is None


def test_register_commands_failure_does_not_raise(caplog):
    bot = fake_bot()
    bot.set_my_commands.side_effect = RuntimeError("no network")
    run(TelegramGateway(bot).register_commands([], 42))
    assert "could not register the commands" in caplog.text


def test_buttons_are_sent_edited_and_answered_through_the_bot():
    calls = []

    class B:
        async def send_message(self, **kw):
            calls.append(("send", kw))
            return SimpleNamespace(message_id=77)

        async def edit_message_text(self, **kw):
            calls.append(("edit", kw))

        async def answer_callback_query(self, cid, text=None):
            calls.append(("answer", cid, text))

    gw = TelegramGateway(B())
    rows = [[("⭕ Task", "tk:a1b2:20261118:t:")]]
    assert asyncio.run(gw.send_buttons(1, "Panel", rows)) == 77
    asyncio.run(gw.edit_buttons(1, 77, "Panel", rows))
    asyncio.run(gw.answer_callback("cb", "done"))
    keyboard = calls[0][1]["reply_markup"].inline_keyboard
    assert keyboard[0][0].text == "⭕ Task" and keyboard[0][0].callback_data == "tk:a1b2:20261118:t:"
    assert calls[1][0] == "edit" and calls[2] == ("answer", "cb", "done")
