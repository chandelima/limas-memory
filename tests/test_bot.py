import asyncio
import logging
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import applog
import bot
import chatflow
import i18n
import config
from attachments import reader
from attachments.reader import ReaderError
from attachments.registry import AttachmentRegistry
from chat_agent.agent import ReplyResult
from chatflow import Conversation
from conversation.log import ANSWERED, FAILED, READING, READY, RECEIVED, ConversationLog
from curator.agent import CurateResult, ToolEvent
from curator.service import CurationService, Outcome
from fakes import ALLOWED, FakeGateway, make_context, make_update, pdf_doc
from storage.store import MemoryStore


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Complete bot with real Conversation, history and registry; only the model and Telegram are fake."""
    log = ConversationLog(tmp_path / "c.json")
    service = CurationService(log, lambda m, c, ch: CurateResult(report="Wrote 1 document."), channel="Telegram")
    conv = Conversation(log, service, agent=object())
    registry = AttachmentRegistry(MemoryStore(tmp_path / "memory"))
    gw = FakeGateway()
    state = SimpleNamespace(replies=iter([]), calls=[], delay=0.0, fail=None, trace=[], send_files=[],
                            hook=None, active=0, max_active=0,
                            read=lambda data, filename, mime: "Hotel Sol, check-in 12/03")

    def fake_reply(history, agent=None, now=None, language="", on_step=None, on_text=None):
        gw.events.append(("agent", history[-1].text[:30]))
        state.calls.append([(m.role, m.text) for m in history])
        state.active += 1
        state.max_active = max(state.max_active, state.active)
        try:
            if state.hook:
                state.hook()
            for event in state.trace:  # the real agent reports each tool call as it happens
                if on_step:
                    on_step(event.name, event.args)
            if state.delay:
                time.sleep(state.delay)
            if state.fail:
                raise state.fail
            conv.outbox.extend(state.send_files)
            return ReplyResult(text=next(state.replies, "ok"), trace=state.trace)
        finally:
            state.active -= 1

    monkeypatch.setattr(chatflow, "reply", fake_reply)

    def make_handlers(allowed=ALLOWED, verbose=False, use_registry=True, conversation=None):
        return bot.BotHandlers(conversation or conv, allowed, registry if use_registry else None,
                               read_document=lambda d, f, m: state.read(d, f, m), verbose=verbose,
                               gateway_factory=lambda _bot: gw)

    return SimpleNamespace(log=log, conv=conv, registry=registry, gw=gw, state=state,
                           handlers=make_handlers(), make_handlers=make_handlers, tmp_path=tmp_path,
                           ctx=make_context())


def stored(env, msg_id=1):
    """What is in the history FILE right now (as if another process read it)."""
    return ConversationLog(env.log.path).get(msg_id)


# ---- text: record on arrival, "typing" right away, reply in turn --------------------------


def test_message_is_persisted_and_typing_is_on_before_the_agent_runs(env):
    seen = {}

    def hook():
        seen["status"] = stored(env).status  # already on disk, still unanswered
        seen["typing"] = ("typing_on", ALLOWED) in env.gw.events

    env.state.hook = hook
    run(env.handlers.on_message(make_update(text="hi"), env.ctx))
    assert seen == {"status": RECEIVED, "typing": True}
    order = [k for k, _ in env.gw.events]
    assert order == ["typing_on", "placeholder", "agent", "text", "typing_off"]  # the progress message exists before the agent runs


def test_answer_is_sent_and_the_message_is_marked_answered(env):
    env.state.replies = iter(["You drink tea."])
    run(env.handlers.on_message(make_update(text="what do I drink?"), env.ctx))
    assert env.gw.texts == [(ALLOWED, "You drink tea.")]
    user, reply = env.log.messages()
    assert (user.status, reply.role, reply.reply_to) == (ANSWERED, "assistant", user.id)


def test_other_users_groups_and_empty_text_are_ignored_and_not_persisted(env):
    for update in (make_update(text="hi", user_id=999), make_update(text="hi", chat_type="group"),
                   make_update(text="   ")):
        run(env.handlers.on_message(update, env.ctx))
    assert env.log.messages() == [] and env.gw.events == [] and env.state.calls == []


def test_without_allowed_id_refuses_everyone_and_logs_sender_id(env, caplog):
    run(env.make_handlers(allowed=None).on_message(make_update(text="hi", user_id=777), env.ctx))
    assert env.log.messages() == [] and env.gw.events == []
    assert "TELEGRAM_ALLOWED_USER_ID=777" in caplog.text


def test_agent_error_replies_generically_and_marks_the_message_failed(env, caplog):
    env.state.fail = RuntimeError("internal secret: api key abc")
    run(env.handlers.on_message(make_update(text="hi"), env.ctx))
    assert env.gw.sent() == [i18n.t("error_reply")] and "abc" not in env.gw.sent()[0]
    assert stored(env).status == FAILED and env.log.unfinished() == []
    assert "api key abc" in caplog.text  # detail only in the local log (the channel only gets type and message)


def test_two_quick_messages_are_persisted_with_typing_first_then_answered_in_order(env):
    env.state.delay = 0.05
    env.state.replies = iter(["R1", "R2"])

    async def both():
        await asyncio.gather(env.handlers.on_message(make_update(text="M1"), env.ctx),
                             env.handlers.on_message(make_update(text="M2"), env.ctx))

    run(both())
    assert [m.text for m in env.log.recent(10)] == ["M1", "R1", "M2", "R2"]
    assert env.state.max_active == 1  # one at a time
    assert env.state.calls[1] == [("user", "M1"), ("assistant", "R1"), ("user", "M2")]
    typing_on = [i for i, (k, _) in enumerate(env.gw.events) if k == "typing_on"]
    assert len(typing_on) == 2 and typing_on[1] < env.gw.index("text", "R1")  # the 2nd was already "typing" while waiting


def test_a_slow_message_does_not_block_the_next_one_from_being_persisted(env):
    persisted = {}
    env.state.delay = 0.1

    async def scenario():
        slow = asyncio.create_task(env.handlers.on_message(make_update(text="slow"), env.ctx))
        await asyncio.sleep(0.03)  # the first one is processing
        second = asyncio.create_task(env.handlers.on_message(make_update(text="fast"), env.ctx))
        await asyncio.sleep(0.02)
        persisted["texts"] = [m.text for m in ConversationLog(env.log.path).messages()]
        await asyncio.gather(slow, second)

    run(scenario())
    assert persisted["texts"] == ["slow", "fast"]  # the 2nd was already on disk during the 1st


# ---- commands (they do not go to the model) ------------------------------------------------


def test_commands_answer_through_the_gateway_without_calling_the_agent(env):
    for method in (env.handlers.on_status, env.handlers.on_curate, env.handlers.on_start, env.handlers.on_unknown):
        run(method(make_update(text="/something"), env.ctx))
    texts = env.gw.sent()
    assert texts[0].startswith("[status] pending: 0") and texts[1] == "[curator] nothing pending."
    assert texts[2].startswith("Hello!") and "/curate" in texts[2] and texts[3].startswith("Unknown command")
    assert env.state.calls == [] and env.log.messages() == []


def test_curate_command_processes_pending_messages_with_typing(env):
    run(env.handlers.on_message(make_update(text="I drink coffee"), env.ctx))
    env.gw.events.clear(), env.gw.texts.clear()
    run(env.handlers.on_curate(make_update(text="/curate"), env.ctx))
    assert "[curator] 2 message(s) processed" in env.gw.sent()[0] and "Wrote 1 document." in env.gw.sent()[0]
    assert env.gw.events[0][0] == "typing_on"


def test_commands_from_strangers_are_ignored(env):
    for method in (env.handlers.on_status, env.handlers.on_curate, env.handlers.on_start, env.handlers.on_unknown):
        run(method(make_update(text="/x", user_id=999), env.ctx))
    assert env.gw.events == []


def test_curate_command_while_another_curation_runs_says_so(env):
    run(env.handlers.on_message(make_update(text="something pending"), env.ctx))
    env.gw.texts.clear()
    env.conv.service._busy.acquire()  # simulates an automatic curation in progress
    try:
        run(env.handlers.on_curate(make_update(text="/curate"), env.ctx))
    finally:
        env.conv.service._busy.release()
    assert "a curation is already running" in env.gw.sent()[0] and "nothing pending" not in env.gw.sent()[0]


# ---- --verbose and resending files ---------------------------------------------------------


def test_verbose_prints_memory_queries_and_default_mode_does_not(env, capsys):
    env.state.trace = [ToolEvent("list_memory", {}, "..."), ToolEvent("read_memory", {"path": "trips/x.md"}, "...")]
    run(env.make_handlers(verbose=True).on_message(make_update(text="hi"), env.ctx))
    out = capsys.readouterr().out
    assert "[memory] list_memory()" in out and "[memory] read_memory(path='trips/x.md')" in out
    run(env.handlers.on_message(make_update(text="hi again"), env.ctx))
    assert "[memory]" not in capsys.readouterr().out


def test_files_requested_by_the_agent_are_resent_after_the_text(env, capsys):
    att = env.registry.add(file_id="F1", file_unique_id="u", kind="document", chat_id=ALLOWED, message_id=77,
                           filename="voucher.pdf", mime="application/pdf")
    env.state.send_files = [att]
    run(env.make_handlers(verbose=True).on_message(make_update(text="send me the voucher"), env.ctx))
    assert env.gw.files == [(ALLOWED, att)]
    assert env.gw.index("text") < env.gw.index("file")
    assert f"[file] resending {att.ref} (voucher.pdf)" in capsys.readouterr().out


def test_resend_failure_is_reported_without_breaking_the_reply(env, caplog):
    att = env.registry.add(file_id="F1", file_unique_id="u", kind="document", chat_id=ALLOWED, message_id=77,
                           filename="voucher.pdf", mime="application/pdf")
    env.state.send_files = [att]
    env.gw.send_file_error = RuntimeError("invalid file_id")
    run(env.handlers.on_message(make_update(text="send me the voucher"), env.ctx))
    assert env.gw.sent() == ["ok", "I could not resend the file voucher.pdf."]
    assert "invalid file_id" in caplog.text


# ---- files: record, notify, read, reply ----------------------------------------------------


def test_pdf_is_registered_and_stored_on_arrival_then_read_and_answered(env):
    seen = {}

    def read(data, filename, mime):
        msg = stored(env)
        seen.update(status=msg.status, text=msg.text, notice=i18n.t("file_notice") in env.gw.sent(),
                    typing=("typing_on", ALLOWED) in env.gw.events, registered=len(env.registry.all()),
                    download=("download", "TGFILE-A") in env.gw.events)
        return "Hotel Sol, check-in 12/03"

    env.state.read = read
    env.state.replies = iter(["Got it: Hotel Sol."])
    run(env.handlers.on_file(make_update(document=pdf_doc(), caption="trip to Lisbon"), env.ctx))

    assert seen["status"] == READING and "(reading the file...)" in seen["text"]
    assert seen["notice"] and seen["typing"] and seen["registered"] == 1 and seen["download"]
    att = env.registry.all()[0]
    assert (att.kind, att.filename, att.mime, att.chat_id, att.message_id) == ("document", "voucher.pdf", "application/pdf", ALLOWED, 555)
    msg = env.log.messages()[0]
    assert msg.attachments == [att.id] and msg.status == ANSWERED
    assert "Hotel Sol, check-in 12/03" in msg.text and "Caption: trip to Lisbon" in msg.text
    assert "(reading the file...)" not in msg.text
    assert env.gw.sent() == [i18n.t("file_notice"), "Got it: Hotel Sol."]
    assert "Hotel Sol, check-in 12/03" in env.state.calls[0][-1][1]  # the agent saw the content


def test_photo_uses_the_largest_size_and_jpeg_mime(env):
    seen = {}
    env.state.read = lambda d, f, m: seen.update(filename=f, mime=m) or "photo text"
    small = SimpleNamespace(file_id="small", file_unique_id="u-small", file_size=100)
    big = SimpleNamespace(file_id="big", file_unique_id="u-big", file_size=900)
    run(env.handlers.on_file(make_update(photo=[small, big]), env.ctx))
    att = env.registry.all()[0]
    assert att.kind == "photo" and att.file_id == "big" and att.size == 900
    assert seen == {"filename": "photo-u-big.jpg", "mime": "image/jpeg"}


def test_pdf_sent_as_octet_stream_is_accepted_by_extension(env):
    seen = {}
    env.state.read = lambda d, f, m: seen.update(mime=m) or "ok"
    run(env.handlers.on_file(make_update(document=pdf_doc(mime="application/octet-stream")), env.ctx))
    assert seen["mime"] == "application/pdf" and len(env.registry.all()) == 1


def test_unsupported_and_oversized_files_are_refused_before_anything_is_stored(env):
    for doc, expected in ((pdf_doc("sheet.xlsx", "application/vnd.ms-excel"), "only read PDFs and images"),
                          (pdf_doc(size=reader.MAX_FILE_BYTES + 1), "20 MB")):
        run(env.handlers.on_file(make_update(document=doc), env.ctx))
        assert expected in env.gw.sent()[-1]
    assert env.log.messages() == [] and env.registry.all() == []
    assert not [e for e in env.gw.events if e[0] == "download"]


def test_unreadable_file_is_explained_and_marked_failed_but_the_attachment_is_kept(env):
    def read(d, f, m):
        raise ReaderError("The model may not accept images: set DOCUMENT_MODEL.")

    env.state.read = read
    run(env.handlers.on_file(make_update(document=pdf_doc()), env.ctx))
    assert env.gw.sent()[-1] == "I could not read that file. The model may not accept images: set DOCUMENT_MODEL."
    msg = env.log.messages()[0]
    assert msg.status == FAILED and "(could not read the file)" in msg.text and env.state.calls == []
    assert len(env.registry.all()) == 1 and env.log.unfinished() == []


def test_download_failure_is_generic_and_does_not_leak_details(env, caplog):
    env.gw.download_error = RuntimeError("token 123:abc invalid")
    run(env.handlers.on_file(make_update(document=pdf_doc()), env.ctx))
    assert env.gw.sent()[-1] == "I could not download the file right now. Try again in a moment."
    assert env.log.messages()[0].status == FAILED
    assert "123:abc" in caplog.text


def test_files_from_strangers_or_without_registry_are_ignored(env):
    run(env.handlers.on_file(make_update(document=pdf_doc(), user_id=999), env.ctx))
    run(env.make_handlers(use_registry=False).on_file(make_update(document=pdf_doc()), env.ctx))
    assert env.log.messages() == [] and env.gw.events == [] and env.registry.all() == []


def test_a_text_message_sent_while_a_file_is_being_read_waits_its_turn_with_typing(env):
    env.state.read = lambda d, f, m: time.sleep(0.1) or "Hotel Sol"
    env.state.replies = iter(["R-file", "R-text"])

    async def scenario():
        file_task = asyncio.create_task(env.handlers.on_file(make_update(document=pdf_doc()), env.ctx))
        await asyncio.sleep(0.03)
        await env.handlers.on_message(make_update(text="and then?"), env.ctx)
        await file_task

    run(scenario())
    assert env.gw.sent()[1:] == ["R-file", "R-text"]  # the file finishes first; the question sees the content
    assert "Hotel Sol" in env.state.calls[1][0][1]
    typing_on = [i for i, (k, _) in enumerate(env.gw.events) if k == "typing_on"]
    assert len(typing_on) == 2 and typing_on[1] < env.gw.index("text", "R-file")


# ---- recovery after a crash --------------------------------------------------------------------


def make_attachment(env, filename="v.pdf"):
    return env.registry.add(file_id="TGFILE-A", file_unique_id="u", kind="document", chat_id=ALLOWED,
                            message_id=9, filename=filename, mime="application/pdf")


def test_message_persisted_before_a_crash_is_answered_after_restart(env):
    env.conv.ingest("survives the crash")  # the bot recorded it and died before answering
    reborn_log = ConversationLog(env.log.path)  # new process, reading the file
    reborn = Conversation(reborn_log, CurationService(reborn_log, lambda m, c, ch: CurateResult(report="ok"), channel="T"), object())
    env.state.replies = iter(["I'm back!"])
    run(env.make_handlers(conversation=reborn).recover(env.gw))
    assert env.gw.sent() == [i18n.t("recovery_notice"), "I'm back!"]
    assert reborn_log.unfinished() == [] and reborn_log.get(1).status == ANSWERED


def test_recover_resumes_received_ready_and_reading_messages_in_order(env):
    env.conv.ingest("received")
    env.conv.ingest("ready", status=READY)
    att = make_attachment(env)
    reading = env.conv.ingest(reader.format_placeholder(att.ref, "v.pdf", ""), attachments=[att.id], status=READING)
    env.state.replies = iter(["R1", "R2", "R3"])
    run(env.handlers.recover(env.gw))
    assert env.gw.sent() == [i18n.t("recovery_notice"), "R1", "R2", "R3"]
    assert env.log.unfinished() == [] and ("download", "TGFILE-A") in env.gw.events
    assert "Hotel Sol" in env.log.get(reading.id).text  # the file was downloaded and read again
    assert [e[0] for e in env.gw.events].count("typing_on") == 3


def test_recover_drops_messages_older_than_a_day_and_stays_quiet(env):
    old = (datetime.now().astimezone() - timedelta(hours=30)).isoformat(timespec="seconds")
    env.log.append("user", "very old", at=old, status=RECEIVED)
    run(env.handlers.recover(env.gw))
    assert env.gw.events == [] and env.state.calls == []
    assert env.log.get(1).status == FAILED


def test_recover_mixes_fresh_and_stale(env):
    old = (datetime.now().astimezone() - timedelta(hours=30)).isoformat(timespec="seconds")
    env.log.append("user", "old", at=old, status=RECEIVED)
    env.conv.ingest("recent")
    run(env.handlers.recover(env.gw))
    assert [m.status for m in env.log.messages() if m.role == "user"] == [FAILED, ANSWERED]
    assert env.state.calls[0] == [("user", "recent")]


def test_recover_reports_a_file_whose_attachment_is_missing(env):
    env.conv.ingest("[File sent: v.pdf (attachment:deadbeef)]\n(reading the file...)", attachments=["deadbeef"], status=READING)
    run(env.handlers.recover(env.gw))
    assert env.gw.sent() == [i18n.t("recovery_notice"), "I could not resume a file you sent earlier; please send it again."]
    assert env.log.get(1).status == FAILED


def test_recover_is_a_noop_without_allowed_user_or_pending_messages(env):
    env.conv.ingest("pending")
    run(env.make_handlers(allowed=None).recover(env.gw))
    assert env.gw.events == []
    env.log.update(1, status=ANSWERED)
    run(env.handlers.recover(env.gw))
    assert env.gw.events == []


# ---- application, watcher and automatic report -------------------------------------------------


class FakeService:
    def __init__(self):
        self.started = False
        self.finished = False

    async def watch(self, stop, interval=30, on_outcome=None):
        self.started = True
        await stop.wait()
        self.finished = True


def make_app(env, service=None):
    app = bot.build_application("123456:fake-token", env.handlers, service or FakeService())
    app.bot_data["gateway"] = env.gw  # no network in tests
    return app


def test_application_wiring(env):
    handlers = bot.BotHandlers(env.conv, ALLOWED, env.registry)  # real gateway (default)
    app = bot.build_application("123456:fake-token", handlers, FakeService())
    assert app.concurrent_updates > 1  # without it, a slow file would hold up the following messages
    registered = app.handlers[0]
    assert len(registered) == 9 and registered[-1].callback == handlers.on_unknown
    callbacks = [h.callback for h in registered]
    assert handlers.on_message in callbacks and handlers.on_file in callbacks
    handled = {c for h in registered if hasattr(h, "commands") for c in h.commands}
    assert {c.command for c in bot.commands()} <= handled
    assert app.bot_data["handlers"] is handlers and app.bot_data["allowed_user_id"] == ALLOWED
    assert isinstance(app.bot_data["gateway"], bot.TelegramGateway)
    assert app.bot_data["gateway"].bot is app.bot


def test_post_init_registers_commands_starts_watcher_and_recovers_pending_messages(env):
    env.conv.ingest("stayed pending")
    service = FakeService()
    app = make_app(env, service)

    async def lifecycle():
        await bot.post_init(app)
        await asyncio.sleep(0.1)
        await bot.post_shutdown(app)

    run(lifecycle())
    assert env.gw.registered == (["status", "todo", "curate"], ALLOWED)
    assert service.started and service.finished
    assert env.gw.sent()[0] == i18n.t("recovery_notice") and env.log.unfinished() == []


def test_shutdown_cancels_a_recovery_still_in_progress_and_survives_without_init(env):
    app = make_app(env)

    async def slow_recover(gateway):
        await asyncio.sleep(30)

    app.bot_data["handlers"] = SimpleNamespace(recover=slow_recover)

    async def lifecycle():
        await bot.post_init(app)
        await asyncio.sleep(0.02)
        await bot.post_shutdown(app)

    run(lifecycle())
    assert app.bot_data["recovery"].cancelled()
    run(bot.post_shutdown(make_app(env)))  # without init: does not break


def test_recovery_errors_do_not_crash_the_bot(env, caplog):
    class Broken:
        async def recover(self, gateway):
            raise RuntimeError("failed to resume")

    run(bot._safe_recover(Broken(), env.gw))
    assert "error resuming pending messages" in caplog.text


def ok_outcome(report="Created 1 document.", messages=2):
    return Outcome(messages, result=CurateResult(report=report, trace=[ToolEvent("commit_memory", {}, "OK")]))


def fake_app(gw, allowed=ALLOWED):
    return SimpleNamespace(bot_data={"allowed_user_id": allowed, "gateway": gw})


def test_automatic_report_is_sent_to_the_allowed_user_and_printed(capsys):
    gw = FakeGateway()
    run(bot.notify_outcome(fake_app(gw), ok_outcome("Created: 1 doc")))
    assert gw.texts[0][0] == ALLOWED
    assert gw.sent()[0].startswith("[curator] 2 message(s) processed (commit_memory)")
    assert "[curator] 2 message(s)" in capsys.readouterr().out


def test_report_is_console_only_without_allowed_user(capsys):
    gw = FakeGateway()
    run(bot.notify_outcome(fake_app(gw, allowed=None), ok_outcome()))
    assert gw.events == [] and "[curator]" in capsys.readouterr().out


def test_failure_is_notified_once_per_streak_and_success_resets_it():
    gw, app = FakeGateway(), None
    app = fake_app(gw)
    fail = Outcome(2, error="RuntimeError: no network")
    run(bot.notify_outcome(app, fail))
    run(bot.notify_outcome(app, fail))  # new attempt fails: does not repeat the alert
    assert len(gw.texts) == 1 and "failed" in gw.sent()[0] and "no network" in gw.sent()[0]
    run(bot.notify_outcome(app, ok_outcome()))
    run(bot.notify_outcome(app, fail))
    assert len(gw.texts) == 3


def test_failure_to_send_the_report_does_not_raise(caplog):
    class Down(FakeGateway):
        async def send_text(self, chat_id, text, *, raw=False):
            raise RuntimeError("telegram is down")

    run(bot.notify_outcome(fake_app(Down()), ok_outcome()))
    assert "could not send the curation report" in caplog.text


def test_watcher_callback_sends_the_report_through_the_gateway(env):
    class ReportingService(FakeService):
        async def watch(self, stop, interval=30, on_outcome=None):
            self.started = True
            await on_outcome(ok_outcome("Done"))  # the bot passes an async callback
            await stop.wait()

    app = make_app(env, ReportingService())

    async def lifecycle():
        await bot.post_init(app)
        await asyncio.sleep(0.05)
        await bot.post_shutdown(app)

    run(lifecycle())
    assert any("Done" in t for t in env.gw.sent())


def test_logging_hides_httpx_requests_because_urls_carry_the_token():
    bot.configure_logging()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


# ---- main ----------------------------------------------------------------------------------------


def test_main_requires_token_and_openrouter_key(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    assert bot.main([]) == 1 and "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:fake-token")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    assert bot.main(["--memory-dir", str(tmp_path / "m")]) == 1
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err


def test_main_wires_everything_and_never_prints_the_token(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:fake-token")
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USER_ID", ALLOWED)
    fake_app = MagicMock()
    seen = {}

    def factory(token, handlers, service):
        seen.update(token=token, handlers=handlers, service=service)
        return fake_app

    code = bot.main(["--log", str(tmp_path / "c.json"), "--memory-dir", str(tmp_path / "mem"),
                     "--idle-seconds", "5", "--verbose"], agent=object(), application_factory=factory)
    assert code == 0 and fake_app.run_polling.called
    assert seen["token"] == "123456:fake-token"
    assert seen["handlers"].allowed_user_id == ALLOWED and seen["handlers"].verbose is True
    assert seen["service"].idle_seconds == 5 and seen["service"].channel == "Telegram"
    assert "123456:fake-token" not in capsys.readouterr().out


def test_main_verbose_is_off_by_default_and_warns_without_allowed_id(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:fake-token")
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USER_ID", None)
    seen = {}
    bot.main(["--log", str(tmp_path / "c.json"), "--memory-dir", str(tmp_path / "mem")], agent=object(),
             application_factory=lambda t, h, s: seen.update(verbose=h.verbose) or MagicMock())
    assert seen["verbose"] is False
    assert "TELEGRAM_ALLOWED_USER_ID is not set" in capsys.readouterr().out


def test_main_gives_the_chat_agent_the_send_attachment_tool(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:fake-token")
    seen = {}
    monkeypatch.setattr(bot, "build_chat_agent", lambda tools: seen.update(names=[t.name for t in tools]) or object())
    code = bot.main(["--memory-dir", str(tmp_path / "m"), "--log", str(tmp_path / "c.json")],
                    application_factory=lambda *a: MagicMock())
    assert code == 0
    assert seen["names"][:4] == ["list_memory", "search_memory", "read_memory", "send_attachment"]
    assert seen["names"][4:] == ["list_tasks", "add_task", "complete_task", "reschedule_task",
                                 "reopen_task", "remove_task", "tag_task", "link_task", "web_search"]


@pytest.fixture
def clean_logging():
    yield
    applog.configure_logging()  # no channel; removes the test handler
    logging.getLogger().handlers = [h for h in logging.getLogger().handlers if not getattr(h, "_limas_console", False)]


def test_post_init_ships_lifecycle_lines_to_the_channel_and_shutdown_flushes(env, clean_logging):
    handler = applog.configure_logging("INFO", "WARNING", channel=True)
    app = make_app(env)
    app.bot_data["log_handler"], app.bot_data["log_channel_id"] = handler, -100123

    async def lifecycle():
        await bot.post_init(app)
        await asyncio.sleep(0.05)
        await bot.post_shutdown(app)

    asyncio.run(lifecycle())
    sent = " ".join(t for cid, t in env.gw.texts if cid == -100123)
    assert "bot started" in sent and "bot shutting down" in sent


def member_update(chat_type, status, chat_id=-1001234567890, title="Logs"):
    return SimpleNamespace(
        my_chat_member=SimpleNamespace(new_chat_member=SimpleNamespace(status=status)),
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type, title=title),
    )


def test_adding_the_bot_to_a_channel_logs_its_id_for_log_channel_id(env, caplog):
    caplog.set_level(logging.INFO)
    run(env.handlers.on_added_to_chat(member_update("channel", "administrator"), env.ctx))
    assert "LOG_CHANNEL_ID=-1001234567890" in caplog.text and "channel 'Logs'" in caplog.text
    assert "administrator" not in caplog.text and env.gw.events == []  # says nothing in the chat


def test_added_as_plain_member_asks_for_admin_and_removal_or_private_chats_are_ignored(env, caplog):
    caplog.set_level(logging.INFO)
    run(env.handlers.on_added_to_chat(member_update("supergroup", "member"), env.ctx))
    assert "group" in caplog.text and "administrator" in caplog.text
    caplog.clear()
    run(env.handlers.on_added_to_chat(member_update("channel", "left"), env.ctx))
    run(env.handlers.on_added_to_chat(member_update("private", "member"), env.ctx))
    assert caplog.text == ""


def test_automatic_report_goes_to_the_log_channel_when_configured():
    gw = FakeGateway()
    app = fake_app(gw)
    app.bot_data["log_channel_id"] = -100123
    run(bot.notify_outcome(app, ok_outcome("Created: 1 doc")))
    assert [cid for cid, _ in gw.texts] == [-100123]  # the user gets no copy
    assert "Created: 1 doc" in gw.texts[0][1]


# ---- task panel with buttons ---------------------------------------------------------------------


def make_query(data, user_id=ALLOWED, message_id=900):
    return SimpleNamespace(
        callback_query=SimpleNamespace(id="cb1", data=data, message=SimpleNamespace(message_id=message_id)),
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id, type="private"),
    )


@pytest.fixture
def todo_env(env):
    from datetime import date as _date

    from tasks.store import TaskStore
    tasks = TaskStore(MemoryStore(env.tmp_path / "memory"), today=lambda: _date(2026, 11, 18))
    handlers = bot.BotHandlers(env.conv, ALLOWED, env.registry, gateway_factory=lambda _b: env.gw, tasks=tasks)
    return SimpleNamespace(env=env, tasks=tasks, handlers=handlers)


def test_todo_command_sends_the_panel_a_tap_opens_the_card_and_the_card_toggles_and_goes_back(todo_env):
    from datetime import date as _date
    long_text = "Replace the IoT module of the laundry room and test the new firmware with the supplier"
    task = todo_env.tasks.add(long_text, _date(2026, 11, 18))
    env = todo_env.env
    ctx = SimpleNamespace(bot=object(), args=[])
    run(todo_env.handlers.on_todo(make_update(text="/todo"), ctx))
    chat, text, rows = env.gw.panels[0]
    assert chat == ALLOWED and rows[0][0][0].endswith("…")  # the list button is cut ...
    run(todo_env.handlers.on_task_action(make_query(rows[0][0][1]), env.ctx))  # ... tapping it opens the card
    card, card_rows = env.gw.edits[-1][2], env.gw.edits[-1][3]
    assert env.gw.edits[-1][1] == 900 and card.startswith(long_text) and "Not done yet" in card  # full text
    assert [r[0][0] for r in card_rows] == ["🟢 Mark as done", "🗑️ Remove task", "⬅️ Back to the list"]
    assert [t.id for t in todo_env.tasks.load().pending] == [task.id]  # opening changes nothing
    run(todo_env.handlers.on_task_action(make_query(card_rows[0][0][1]), env.ctx))  # check it off
    assert env.gw.answers[-1][1].startswith("🟢 Completed")
    card, card_rows = env.gw.edits[-1][2], env.gw.edits[-1][3]
    assert "🟢 Done" in card and card_rows[0][0][0] == "⭕ Mark as not done"  # stays on the card
    run(todo_env.handlers.on_task_action(make_query(card_rows[0][0][1]), env.ctx))  # the same button undoes it
    assert env.gw.answers[-1][1].startswith("⭕ Reopened") and "Not done yet" in env.gw.edits[-1][2]
    assert [t.id for t in todo_env.tasks.load().pending] == [task.id]
    run(todo_env.handlers.on_task_action(make_query(env.gw.edits[-1][3][2][0][1]), env.ctx))  # back to the list (3rd button)
    assert "Tap a task to open it" in env.gw.edits[-1][2] and env.gw.edits[-1][3][0][0][0].startswith("⭕")


def test_tap_from_a_stranger_or_garbage_or_missing_task_does_nothing_harmful(todo_env):
    env = todo_env.env
    run(todo_env.handlers.on_task_action(make_query("tk:a1b2:20261118:t:", user_id=999), env.ctx))
    assert env.gw.answers == [] and env.gw.edits == []
    run(todo_env.handlers.on_task_action(make_query("garbage"), env.ctx))
    assert env.gw.answers == [("cb1", "")] and env.gw.edits == []
    run(todo_env.handlers.on_task_action(make_query("tk:zzzz:20261118:t:"), env.ctx))
    assert "no longer exists" in env.gw.answers[-1][1] and env.gw.edits == []
    run(todo_env.handlers.on_task_action(make_query("tt:zzzz:20261118:t:"), env.ctx))
    assert "no longer exists" in env.gw.answers[-1][1] and env.gw.edits == []


def test_panel_follows_the_agent_reply_when_it_listed_tasks(todo_env):
    from datetime import date as _date
    todo_env.tasks.add("Dentist", _date(2026, 11, 18))
    env = todo_env.env
    env.state.trace = [ToolEvent("list_tasks", {"scope": "today"}, "Today is ...\n- [a1b2] Dentist")]
    run(todo_env.handlers.on_message(make_update(text="what do I have today?"), env.ctx))
    assert env.gw.index("text") < env.gw.index("buttons") and len(env.gw.panels) == 1
    env.state.trace = [ToolEvent("list_tasks", {"scope": "done"}, "...")]  # no panel for that scope
    run(todo_env.handlers.on_message(make_update(text="and the finished ones?"), env.ctx))
    assert len(env.gw.panels) == 1


# ---- language of the fixed messages -------------------------------------------------------------


def test_first_message_sets_the_language_up_in_the_background_and_refreshes_the_menu(env):
    calls = []

    def ensure(text):
        calls.append(text)
        return True  # the language file was just created

    handlers = bot.BotHandlers(env.conv, ALLOWED, env.registry, gateway_factory=lambda _b: env.gw, ensure_language=ensure)

    async def scenario():
        await handlers.on_message(make_update(text="oi, tudo bem?"), env.ctx)
        await handlers._language_task

    run(scenario())
    assert calls == ["oi, tudo bem?"] and env.gw.sent() == ["ok"]  # the reply is not held up by the setup
    assert env.gw.registered == (["status", "todo", "curate"], ALLOWED)  # menu re-registered


def test_language_setup_is_skipped_once_configured_and_its_failure_is_harmless(env):
    calls = []
    handlers = bot.BotHandlers(env.conv, ALLOWED, env.registry, gateway_factory=lambda _b: env.gw,
                               ensure_language=lambda text: calls.append(text) or 1 / 0)

    async def scenario():
        await handlers.on_message(make_update(text="oi"), env.ctx)
        await handlers._language_task  # the ZeroDivisionError is swallowed (and logged)
        i18n.current().configured = True
        await handlers.on_message(make_update(text="de novo"), env.ctx)

    run(scenario())
    assert calls == ["oi"]  # not asked again once configured


def test_telegram_requests_use_generous_timeouts(env):
    app = bot.build_application("123456:fake-token", env.handlers, FakeService())
    request = app.bot.request
    timeouts = request._client.timeout  # httpx.Timeout
    assert timeouts.connect == bot.NETWORK_TIMEOUT and timeouts.read == bot.NETWORK_TIMEOUT
    assert bot.NETWORK_TIMEOUT > 5  # the library default


def test_removing_a_task_from_its_card_asks_first_and_only_the_confirmation_removes_it(todo_env):
    from datetime import date as _date
    task = todo_env.tasks.add("Replace the IoT module", _date(2026, 11, 18))
    other = todo_env.tasks.add("Buy bread", _date(2026, 11, 18))
    env = todo_env.env
    run(todo_env.handlers.on_task_action(make_query(f"tk:{task.id}:20261118:t:"), env.ctx))
    remove_data = env.gw.edits[-1][3][1][0][1]  # the "Remove task" button
    run(todo_env.handlers.on_task_action(make_query(remove_data), env.ctx))  # asks first
    ask_text, ask_rows = env.gw.edits[-1][2], env.gw.edits[-1][3]
    assert ask_text.startswith("Remove this task?") and "Replace the IoT module" in ask_text
    assert {t.id for t in todo_env.tasks.load().pending} == {task.id, other.id}  # nothing removed yet
    run(todo_env.handlers.on_task_action(make_query(ask_rows[1][0][1]), env.ctx))  # Cancel: back to the card
    assert "Not done yet" in env.gw.edits[-1][2] and {t.id for t in todo_env.tasks.load().pending} == {task.id, other.id}
    run(todo_env.handlers.on_task_action(make_query(remove_data), env.ctx))
    run(todo_env.handlers.on_task_action(make_query(env.gw.edits[-1][3][0][0][1]), env.ctx))  # Yes, remove it
    assert env.gw.answers[-1][1].startswith("🗑️ Removed: Replace the IoT module")
    assert [t.id for t in todo_env.tasks.load().pending] == [other.id]
    assert "Tap a task to open it" in env.gw.edits[-1][2]  # back on the (now shorter) list
    run(todo_env.handlers.on_task_action(make_query(f"tx:{task.id}:20261118:t:"), env.ctx))  # again: already gone
    assert "Could not" in env.gw.answers[-1][1] and [t.id for t in todo_env.tasks.load().pending] == [other.id]


# ---- live progress ---------------------------------------------------------------------------------


def test_the_progress_message_shows_the_agent_steps_and_becomes_the_answer(env, monkeypatch):
    import progress

    monkeypatch.setattr(progress, "EDIT_INTERVAL", 0.01)
    monkeypatch.setattr(progress, "THINKING_CPS", 0)  # no fake typing here: lines appear at once
    env.state.trace = [ToolEvent("list_memory", {}, "..."), ToolEvent("read_memory", {"path": "trips/x.md"}, "...")]
    env.state.replies = iter(["Here is the answer."])
    env.state.delay = 0.1  # leave time for the edits
    run(env.handlers.on_message(make_update(text="tell me about the trip"), env.ctx))
    assert env.gw.progress_edits[0] == "<i>💭 Thinking…</i>"  # italics: thinking is told apart from the answer
    assert any("Reading trips/x.md" in text for text in env.gw.progress_edits)
    kinds = [k for k, _ in env.gw.events]
    assert kinds.index("placeholder") < kinds.index("agent") < kinds.index("text")
    assert env.gw.sent() == ["Here is the answer."]  # the answer replaced the progress message (no extra message)


def test_an_error_replaces_the_progress_message_with_the_generic_reply(env):
    env.state.fail = RuntimeError("boom")
    run(env.handlers.on_message(make_update(text="hi"), env.ctx))
    assert env.gw.sent() == [i18n.t("error_reply")] and "placeholder" in [k for k, _ in env.gw.events]
