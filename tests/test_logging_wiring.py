"""Application logs wired into the components: curation, gateway and bot lifecycle."""

import asyncio
import logging
from datetime import datetime
from unittest.mock import MagicMock

import bot
import config
from conversation.log import ConversationLog
from curator.agent import CurateResult, ToolEvent
from curator.service import CurationService
from telegram_gateway import TelegramGateway

T0 = datetime.fromisoformat("2026-11-17T10:00:00-03:00")


def make_service(tmp_path, fn):
    history = ConversationLog(tmp_path / "c.json")
    for i in range(6):
        history.append("user", f"secret-{i}", at=T0.isoformat())
    return CurationService(history, fn, channel="Telegram", idle_seconds=60, retry_seconds=300)


def result_with(commit_text):
    return CurateResult(report="ok", trace=[ToolEvent("commit_memory", {}, commit_text)])


def test_successful_curation_logs_counts_but_never_content(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    service = make_service(tmp_path, lambda m, c, ch: result_with("OK"))
    service.run_now()
    assert "curation finished: 6 message(s)" in caplog.text
    assert "secret-" not in caplog.text


def test_failure_is_an_error_once_per_streak_then_info(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    state = {"fail": True}

    def fn(m, c, ch):
        if state["fail"]:
            raise RuntimeError("no network")
        return result_with("OK")

    service = make_service(tmp_path, fn)
    service.run_now(); service.run_now()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "curation failed" in errors[0].getMessage()
    state["fail"] = False
    service.run_now()  # success ends the streak
    assert service._failure_logged is False


def test_pending_push_warns_once_per_reason(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    reasons = iter(["Push pending: no remote", "Push pending: no remote", "Push pending: rejected"])
    service = make_service(tmp_path, lambda m, c, ch: result_with(next(reasons)))
    for _ in range(3):
        service.log.append("user", "new", at=T0.isoformat())
        service.run_now()
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warns == ["memory push pending: no remote", "memory push pending: rejected"]


def test_gateway_raw_text_is_not_stripped_of_markdown():
    sent = []

    class B:
        async def send_message(self, chat_id, text):
            sent.append(text)

    asyncio.run(TelegramGateway(B()).send_text(1, "**a** `b`", raw=True))
    asyncio.run(TelegramGateway(B()).send_text(1, "**a** `b`"))
    assert sent == ["**a** `b`", "a b"]


def test_main_rejects_an_invalid_log_channel(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:fake-token")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "k" * 12)
    monkeypatch.setattr(config, "LOG_CHANNEL_ID", "channel without at sign")
    code = bot.main(["--log", str(tmp_path / "c.json"), "--memory-dir", str(tmp_path / "m")],
                    agent=object(), application_factory=lambda *a: MagicMock())
    assert code == 1 and "LOG_CHANNEL_ID" in capsys.readouterr().err
