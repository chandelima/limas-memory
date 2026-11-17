import asyncio
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import bot
import chatflow
import config
from attachments import reader
from attachments.reader import ReaderError
from attachments.registry import AttachmentRegistry
from chatflow import Conversation
from conversation.log import ANSWERED, FAILED, READING, READY, RECEIVED, ConversationLog
from curator.agent import CurateResult, ToolEvent
from curator.service import CurationService, Outcome
from fakes import ALLOWED, FakeGateway, make_context, make_update, pdf_doc
from responder.agent import ReplyResult
from storage.store import MemoryStore


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Bot completo com Conversation, histórico e registro reais; só o modelo e o Telegram são falsos."""
    log = ConversationLog(tmp_path / "c.json")
    service = CurationService(log, lambda m, c, ch: CurateResult(report="Gravei 1 documento."), channel="Telegram")
    conv = Conversation(log, service, agent=object())
    registry = AttachmentRegistry(MemoryStore(tmp_path / "memory"))
    gw = FakeGateway()
    state = SimpleNamespace(replies=iter([]), calls=[], delay=0.0, fail=None, trace=[], send_files=[],
                            hook=None, active=0, max_active=0,
                            read=lambda data, filename, mime: "Hotel Sol, check-in 12/03")

    def fake_reply(history, agent=None, now=None):
        gw.events.append(("agent", history[-1].text[:30]))
        state.calls.append([(m.role, m.text) for m in history])
        state.active += 1
        state.max_active = max(state.max_active, state.active)
        try:
            if state.hook:
                state.hook()
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
    """O que está no ARQUIVO do histórico agora (como se outro processo o lesse)."""
    return ConversationLog(env.log.path).get(msg_id)


# ---- texto: gravar na chegada, "digitando" já, responder na vez -----------------------


def test_message_is_persisted_and_typing_is_on_before_the_agent_runs(env):
    seen = {}

    def hook():
        seen["status"] = stored(env).status  # já no disco, ainda sem resposta
        seen["typing"] = ("typing_on", ALLOWED) in env.gw.events

    env.state.hook = hook
    run(env.handlers.on_message(make_update(text="oi"), env.ctx))
    assert seen == {"status": RECEIVED, "typing": True}
    order = [k for k, _ in env.gw.events]
    assert order == ["typing_on", "agent", "text", "typing_off"]


def test_answer_is_sent_and_the_message_is_marked_answered(env):
    env.state.replies = iter(["Você toma chá."])
    run(env.handlers.on_message(make_update(text="o que bebo?"), env.ctx))
    assert env.gw.texts == [(ALLOWED, "Você toma chá.")]
    user, reply = env.log.messages()
    assert (user.status, reply.role, reply.reply_to) == (ANSWERED, "assistente", user.id)


def test_other_users_groups_and_empty_text_are_ignored_and_not_persisted(env):
    for update in (make_update(text="oi", user_id=999), make_update(text="oi", chat_type="group"),
                   make_update(text="   ")):
        run(env.handlers.on_message(update, env.ctx))
    assert env.log.messages() == [] and env.gw.events == [] and env.state.calls == []


def test_without_allowed_id_refuses_everyone_and_prints_sender_id(env, capsys):
    run(env.make_handlers(allowed=None).on_message(make_update(text="oi", user_id=777), env.ctx))
    assert env.log.messages() == [] and env.gw.events == []
    assert "TELEGRAM_ALLOWED_USER_ID=777" in capsys.readouterr().out


def test_agent_error_replies_generically_and_marks_the_message_failed(env, capsys):
    env.state.fail = RuntimeError("segredo interno: api key abc")
    run(env.handlers.on_message(make_update(text="oi"), env.ctx))
    assert env.gw.sent() == [bot.ERROR_REPLY] and "abc" not in env.gw.sent()[0]
    assert stored(env).status == FAILED and env.log.unfinished() == []
    assert "api key abc" in capsys.readouterr().err  # detalhe só no console


def test_two_quick_messages_are_persisted_with_typing_first_then_answered_in_order(env):
    env.state.delay = 0.05
    env.state.replies = iter(["R1", "R2"])

    async def both():
        await asyncio.gather(env.handlers.on_message(make_update(text="M1"), env.ctx),
                             env.handlers.on_message(make_update(text="M2"), env.ctx))

    run(both())
    assert [m.text for m in env.log.recent(10)] == ["M1", "R1", "M2", "R2"]
    assert env.state.max_active == 1  # uma por vez
    assert env.state.calls[1] == [("usuario", "M1"), ("assistente", "R1"), ("usuario", "M2")]
    typing_on = [i for i, (k, _) in enumerate(env.gw.events) if k == "typing_on"]
    assert len(typing_on) == 2 and typing_on[1] < env.gw.index("text", "R1")  # a 2ª já "digitava" esperando


def test_a_slow_message_does_not_block_the_next_one_from_being_persisted(env):
    persisted = {}
    env.state.delay = 0.1

    async def scenario():
        slow = asyncio.create_task(env.handlers.on_message(make_update(text="lenta"), env.ctx))
        await asyncio.sleep(0.03)  # a primeira está processando
        second = asyncio.create_task(env.handlers.on_message(make_update(text="rápida"), env.ctx))
        await asyncio.sleep(0.02)
        persisted["texts"] = [m.text for m in ConversationLog(env.log.path).messages()]
        await asyncio.gather(slow, second)

    run(scenario())
    assert persisted["texts"] == ["lenta", "rápida"]  # a 2ª já estava no disco durante a 1ª


# ---- comandos (não vão para o modelo) -------------------------------------------------


def test_commands_answer_through_the_gateway_without_calling_the_agent(env):
    for method in (env.handlers.on_status, env.handlers.on_curate, env.handlers.on_start, env.handlers.on_unknown):
        run(method(make_update(text="/algo"), env.ctx))
    texts = env.gw.sent()
    assert texts[0].startswith("[status] pendentes: 0") and texts[1] == "[curador] nada pendente."
    assert texts[2].startswith("Olá!") and "/curate" in texts[2] and texts[3].startswith("Comando desconhecido")
    assert env.state.calls == [] and env.log.messages() == []


def test_curate_command_processes_pending_messages_with_typing(env):
    run(env.handlers.on_message(make_update(text="tomo café"), env.ctx))
    env.gw.events.clear(), env.gw.texts.clear()
    run(env.handlers.on_curate(make_update(text="/curate"), env.ctx))
    assert "[curador] 2 mensagem(ns) processada(s)" in env.gw.sent()[0] and "Gravei 1 documento." in env.gw.sent()[0]
    assert env.gw.events[0][0] == "typing_on"


def test_commands_from_strangers_are_ignored(env):
    for method in (env.handlers.on_status, env.handlers.on_curate, env.handlers.on_start, env.handlers.on_unknown):
        run(method(make_update(text="/x", user_id=999), env.ctx))
    assert env.gw.events == []


def test_curate_command_while_another_curation_runs_says_so(env):
    run(env.handlers.on_message(make_update(text="algo pendente"), env.ctx))
    env.gw.texts.clear()
    env.conv.service._busy.acquire()  # simula curadoria automática em andamento
    try:
        run(env.handlers.on_curate(make_update(text="/curate"), env.ctx))
    finally:
        env.conv.service._busy.release()
    assert "já há uma curadoria em andamento" in env.gw.sent()[0] and "nada pendente" not in env.gw.sent()[0]


# ---- --verbose e reenvio de arquivos --------------------------------------------------


def test_verbose_prints_memory_queries_and_default_mode_does_not(env, capsys):
    env.state.trace = [ToolEvent("list_memory", {}, "..."), ToolEvent("read_memory", {"path": "viagens/x.md"}, "...")]
    run(env.make_handlers(verbose=True).on_message(make_update(text="oi"), env.ctx))
    out = capsys.readouterr().out
    assert "[memória] list_memory()" in out and "[memória] read_memory(path='viagens/x.md')" in out
    run(env.handlers.on_message(make_update(text="oi de novo"), env.ctx))
    assert "[memória]" not in capsys.readouterr().out


def test_files_requested_by_the_agent_are_resent_after_the_text(env, capsys):
    att = env.registry.add(file_id="F1", file_unique_id="u", kind="document", chat_id=ALLOWED, message_id=77,
                           filename="voucher.pdf", mime="application/pdf")
    env.state.send_files = [att]
    run(env.make_handlers(verbose=True).on_message(make_update(text="me manda o voucher"), env.ctx))
    assert env.gw.files == [(ALLOWED, att)]
    assert env.gw.index("text") < env.gw.index("file")
    assert f"[arquivo] reenviando {att.ref} (voucher.pdf)" in capsys.readouterr().out


def test_resend_failure_is_reported_without_breaking_the_reply(env, capsys):
    att = env.registry.add(file_id="F1", file_unique_id="u", kind="document", chat_id=ALLOWED, message_id=77,
                           filename="voucher.pdf", mime="application/pdf")
    env.state.send_files = [att]
    env.gw.send_file_error = RuntimeError("file_id inválido")
    run(env.handlers.on_message(make_update(text="me manda o voucher"), env.ctx))
    assert env.gw.sent() == ["ok", "Não consegui reenviar o arquivo voucher.pdf."]
    assert "file_id inválido" in capsys.readouterr().err


# ---- arquivos: gravar na chegada, avisar, ler, responder ------------------------------


def test_pdf_is_registered_and_stored_on_arrival_then_read_and_answered(env):
    seen = {}

    def read(data, filename, mime):
        msg = stored(env)
        seen.update(status=msg.status, text=msg.text, notice=bot.FILE_NOTICE in env.gw.sent(),
                    typing=("typing_on", ALLOWED) in env.gw.events, registered=len(env.registry.all()),
                    download=("download", "TGFILE-A") in env.gw.events)
        return "Hotel Sol, check-in 12/03"

    env.state.read = read
    env.state.replies = iter(["Entendi: hotel Sol."])
    run(env.handlers.on_file(make_update(document=pdf_doc(), caption="viagem a Lisboa"), env.ctx))

    assert seen["status"] == READING and "(lendo o arquivo...)" in seen["text"]
    assert seen["notice"] and seen["typing"] and seen["registered"] == 1 and seen["download"]
    att = env.registry.all()[0]
    assert (att.kind, att.filename, att.mime, att.chat_id, att.message_id) == ("document", "voucher.pdf", "application/pdf", ALLOWED, 555)
    msg = env.log.messages()[0]
    assert msg.attachments == [att.id] and msg.status == ANSWERED
    assert "Hotel Sol, check-in 12/03" in msg.text and "Legenda: viagem a Lisboa" in msg.text
    assert "(lendo o arquivo...)" not in msg.text
    assert env.gw.sent() == [bot.FILE_NOTICE, "Entendi: hotel Sol."]
    assert "Hotel Sol, check-in 12/03" in env.state.calls[0][-1][1]  # o agente viu o conteúdo


def test_photo_uses_the_largest_size_and_jpeg_mime(env):
    seen = {}
    env.state.read = lambda d, f, m: seen.update(filename=f, mime=m) or "texto da foto"
    small = SimpleNamespace(file_id="small", file_unique_id="u-small", file_size=100)
    big = SimpleNamespace(file_id="big", file_unique_id="u-big", file_size=900)
    run(env.handlers.on_file(make_update(photo=[small, big]), env.ctx))
    att = env.registry.all()[0]
    assert att.kind == "photo" and att.file_id == "big" and att.size == 900
    assert seen == {"filename": "foto-u-big.jpg", "mime": "image/jpeg"}


def test_pdf_sent_as_octet_stream_is_accepted_by_extension(env):
    seen = {}
    env.state.read = lambda d, f, m: seen.update(mime=m) or "ok"
    run(env.handlers.on_file(make_update(document=pdf_doc(mime="application/octet-stream")), env.ctx))
    assert seen["mime"] == "application/pdf" and len(env.registry.all()) == 1


def test_unsupported_and_oversized_files_are_refused_before_anything_is_stored(env):
    for doc, expected in ((pdf_doc("planilha.xlsx", "application/vnd.ms-excel"), "só leio PDF e imagens"),
                          (pdf_doc(size=reader.MAX_FILE_BYTES + 1), "20 MB")):
        run(env.handlers.on_file(make_update(document=doc), env.ctx))
        assert expected in env.gw.sent()[-1]
    assert env.log.messages() == [] and env.registry.all() == []
    assert not [e for e in env.gw.events if e[0] == "download"]


def test_unreadable_file_is_explained_and_marked_failed_but_the_attachment_is_kept(env):
    def read(d, f, m):
        raise ReaderError("O modelo pode não aceitar imagem: defina DOCUMENT_MODEL.")

    env.state.read = read
    run(env.handlers.on_file(make_update(document=pdf_doc()), env.ctx))
    assert env.gw.sent()[-1] == "Não consegui ler esse arquivo. O modelo pode não aceitar imagem: defina DOCUMENT_MODEL."
    msg = env.log.messages()[0]
    assert msg.status == FAILED and "(não foi possível ler o arquivo)" in msg.text and env.state.calls == []
    assert len(env.registry.all()) == 1 and env.log.unfinished() == []


def test_download_failure_is_generic_and_does_not_leak_details(env, capsys):
    env.gw.download_error = RuntimeError("token 123:abc inválido")
    run(env.handlers.on_file(make_update(document=pdf_doc()), env.ctx))
    assert env.gw.sent()[-1] == "Não consegui baixar o arquivo agora. Tente de novo em instantes."
    assert env.log.messages()[0].status == FAILED
    assert "123:abc" in capsys.readouterr().err


def test_files_from_strangers_or_without_registry_are_ignored(env):
    run(env.handlers.on_file(make_update(document=pdf_doc(), user_id=999), env.ctx))
    run(env.make_handlers(use_registry=False).on_file(make_update(document=pdf_doc()), env.ctx))
    assert env.log.messages() == [] and env.gw.events == [] and env.registry.all() == []


def test_a_text_message_sent_while_a_file_is_being_read_waits_its_turn_with_typing(env):
    env.state.read = lambda d, f, m: time.sleep(0.1) or "Hotel Sol"
    env.state.replies = iter(["R-arquivo", "R-texto"])

    async def scenario():
        file_task = asyncio.create_task(env.handlers.on_file(make_update(document=pdf_doc()), env.ctx))
        await asyncio.sleep(0.03)
        await env.handlers.on_message(make_update(text="e aí?"), env.ctx)
        await file_task

    run(scenario())
    assert env.gw.sent()[1:] == ["R-arquivo", "R-texto"]  # o arquivo termina antes; a pergunta vê o conteúdo
    assert "Hotel Sol" in env.state.calls[1][0][1]
    typing_on = [i for i, (k, _) in enumerate(env.gw.events) if k == "typing_on"]
    assert len(typing_on) == 2 and typing_on[1] < env.gw.index("text", "R-arquivo")


# ---- recuperação depois de uma queda ---------------------------------------------------


def make_attachment(env, filename="v.pdf"):
    return env.registry.add(file_id="TGFILE-A", file_unique_id="u", kind="document", chat_id=ALLOWED,
                            message_id=9, filename=filename, mime="application/pdf")


def test_message_persisted_before_a_crash_is_answered_after_restart(env):
    env.conv.ingest("sobrevive à queda")  # o bot gravou e caiu antes de responder
    reborn_log = ConversationLog(env.log.path)  # processo novo, lendo o arquivo
    reborn = Conversation(reborn_log, CurationService(reborn_log, lambda m, c, ch: CurateResult(report="ok"), channel="T"), object())
    env.state.replies = iter(["Voltei!"])
    run(env.make_handlers(conversation=reborn).recover(env.gw))
    assert env.gw.sent() == [bot.RECOVERY_NOTICE, "Voltei!"]
    assert reborn_log.unfinished() == [] and reborn_log.get(1).status == ANSWERED


def test_recover_resumes_received_ready_and_reading_messages_in_order(env):
    env.conv.ingest("recebida")
    env.conv.ingest("pronta", status=READY)
    att = make_attachment(env)
    reading = env.conv.ingest(reader.format_placeholder(att.ref, "v.pdf", ""), attachments=[att.id], status=READING)
    env.state.replies = iter(["R1", "R2", "R3"])
    run(env.handlers.recover(env.gw))
    assert env.gw.sent() == [bot.RECOVERY_NOTICE, "R1", "R2", "R3"]
    assert env.log.unfinished() == [] and ("download", "TGFILE-A") in env.gw.events
    assert "Hotel Sol" in env.log.get(reading.id).text  # o arquivo foi baixado e lido de novo
    assert [e[0] for e in env.gw.events].count("typing_on") == 3


def test_recover_drops_messages_older_than_a_day_and_stays_quiet(env):
    old = (datetime.now().astimezone() - timedelta(hours=30)).isoformat(timespec="seconds")
    env.log.append("usuario", "muito antiga", at=old, status=RECEIVED)
    run(env.handlers.recover(env.gw))
    assert env.gw.events == [] and env.state.calls == []
    assert env.log.get(1).status == FAILED


def test_recover_mixes_fresh_and_stale(env):
    old = (datetime.now().astimezone() - timedelta(hours=30)).isoformat(timespec="seconds")
    env.log.append("usuario", "antiga", at=old, status=RECEIVED)
    env.conv.ingest("recente")
    run(env.handlers.recover(env.gw))
    assert [m.status for m in env.log.messages() if m.role == "usuario"] == [FAILED, ANSWERED]
    assert env.state.calls[0] == [("usuario", "recente")]


def test_recover_reports_a_file_whose_attachment_is_missing(env):
    env.conv.ingest("[Arquivo enviado: v.pdf (anexo:deadbeef)]\n(lendo o arquivo...)", attachments=["deadbeef"], status=READING)
    run(env.handlers.recover(env.gw))
    assert env.gw.sent() == [bot.RECOVERY_NOTICE, "Não consegui retomar um arquivo que você enviou antes; envie de novo."]
    assert env.log.get(1).status == FAILED


def test_recover_is_a_noop_without_allowed_user_or_pending_messages(env):
    env.conv.ingest("pendente")
    run(env.make_handlers(allowed=None).recover(env.gw))
    assert env.gw.events == []
    env.log.update(1, status=ANSWERED)
    run(env.handlers.recover(env.gw))
    assert env.gw.events == []


# ---- aplicação, watcher e relatório automático -----------------------------------------


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
    app.bot_data["gateway"] = env.gw  # nada de rede nos testes
    return app


def test_application_wiring(env):
    handlers = bot.BotHandlers(env.conv, ALLOWED, env.registry)  # gateway real (padrão)
    app = bot.build_application("123456:fake-token", handlers, FakeService())
    assert app.concurrent_updates > 1  # sem isso, um arquivo lento seguraria as mensagens seguintes
    registered = app.handlers[0]
    assert len(registered) == 6 and registered[-1].callback == handlers.on_unknown
    callbacks = [h.callback for h in registered]
    assert handlers.on_message in callbacks and handlers.on_file in callbacks
    handled = {c for h in registered if hasattr(h, "commands") for c in h.commands}
    assert {c.command for c in bot.COMMANDS} <= handled
    assert app.bot_data["handlers"] is handlers and app.bot_data["allowed_user_id"] == ALLOWED
    assert isinstance(app.bot_data["gateway"], bot.TelegramGateway)
    assert app.bot_data["gateway"].bot is app.bot


def test_post_init_registers_commands_starts_watcher_and_recovers_pending_messages(env):
    env.conv.ingest("ficou pendente")
    service = FakeService()
    app = make_app(env, service)

    async def lifecycle():
        await bot.post_init(app)
        await asyncio.sleep(0.1)
        await bot.post_shutdown(app)

    run(lifecycle())
    assert env.gw.registered == (["status", "curate"], ALLOWED)
    assert service.started and service.finished
    assert env.gw.sent()[0] == bot.RECOVERY_NOTICE and env.log.unfinished() == []


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
    run(bot.post_shutdown(make_app(env)))  # sem init: não quebra


def test_recovery_errors_do_not_crash_the_bot(env, capsys):
    class Broken:
        async def recover(self, gateway):
            raise RuntimeError("falha ao retomar")

    run(bot._safe_recover(Broken(), env.gw))
    assert "erro ao retomar mensagens pendentes" in capsys.readouterr().err


def ok_outcome(report="Criei 1 documento.", messages=2):
    return Outcome(messages, result=CurateResult(report=report, trace=[ToolEvent("commit_memory", {}, "OK")]))


def fake_app(gw, allowed=ALLOWED):
    return SimpleNamespace(bot_data={"allowed_user_id": allowed, "gateway": gw})


def test_automatic_report_is_sent_to_the_allowed_user_and_printed(capsys):
    gw = FakeGateway()
    run(bot.notify_outcome(fake_app(gw), ok_outcome("Criados: 1 doc")))
    assert gw.texts[0][0] == ALLOWED
    assert gw.sent()[0].startswith("[curador] 2 mensagem(ns) processada(s) (commit_memory)")
    assert "[curador] 2 mensagem(ns)" in capsys.readouterr().out


def test_report_is_console_only_without_allowed_user(capsys):
    gw = FakeGateway()
    run(bot.notify_outcome(fake_app(gw, allowed=None), ok_outcome()))
    assert gw.events == [] and "[curador]" in capsys.readouterr().out


def test_failure_is_notified_once_per_streak_and_success_resets_it():
    gw, app = FakeGateway(), None
    app = fake_app(gw)
    fail = Outcome(2, error="RuntimeError: sem rede")
    run(bot.notify_outcome(app, fail))
    run(bot.notify_outcome(app, fail))  # nova tentativa falha: não repete o aviso
    assert len(gw.texts) == 1 and "falhou" in gw.sent()[0] and "sem rede" in gw.sent()[0]
    run(bot.notify_outcome(app, ok_outcome()))
    run(bot.notify_outcome(app, fail))
    assert len(gw.texts) == 3


def test_failure_to_send_the_report_does_not_raise(capsys):
    class Down(FakeGateway):
        async def send_text(self, chat_id, text):
            raise RuntimeError("telegram fora")

    run(bot.notify_outcome(fake_app(Down()), ok_outcome()))
    assert "não consegui enviar o relatório" in capsys.readouterr().err


def test_watcher_callback_sends_the_report_through_the_gateway(env):
    class ReportingService(FakeService):
        async def watch(self, stop, interval=30, on_outcome=None):
            self.started = True
            await on_outcome(ok_outcome("Feito"))  # o bot passa um callback assíncrono
            await stop.wait()

    app = make_app(env, ReportingService())

    async def lifecycle():
        await bot.post_init(app)
        await asyncio.sleep(0.05)
        await bot.post_shutdown(app)

    run(lifecycle())
    assert any("Feito" in t for t in env.gw.sent())


def test_logging_hides_httpx_requests_because_urls_carry_the_token():
    import logging

    bot.configure_logging()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


# ---- main ------------------------------------------------------------------------------


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
    assert "TELEGRAM_ALLOWED_USER_ID não definido" in capsys.readouterr().out


def test_main_gives_the_responder_the_send_attachment_tool(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:fake-token")
    seen = {}
    monkeypatch.setattr(bot, "build_responder", lambda tools: seen.update(names=[t.name for t in tools]) or object())
    code = bot.main(["--memory-dir", str(tmp_path / "m"), "--log", str(tmp_path / "c.json")],
                    application_factory=lambda *a: MagicMock())
    assert code == 0
    assert seen["names"] == ["list_memory", "search_memory", "read_memory", "send_attachment"]
