import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

import bot
import chatflow
import config
from attachments import reader
from attachments.reader import ReaderError
from attachments.registry import AttachmentRegistry
from attachments.tools import build_attachment_tools
from chatflow import Conversation, compact_history
from conversation.log import ConversationLog, Message
from curator.agent import CurateResult
from curator.service import CurationService
from curator.tools import build_read_tools
from responder.agent import ReplyResult, build_responder, load_system_prompt
from storage.store import MemoryStore

ALLOWED = 42


@pytest.fixture(autouse=True)
def no_telegram_calls(monkeypatch):
    monkeypatch.setattr(bot, "register_commands", AsyncMock())


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def registry(tmp_path):
    return AttachmentRegistry(MemoryStore(tmp_path / "memoria-anexos"))


def add_att(registry, **kw):
    base = dict(file_id="FILEID1", file_unique_id="uniq1", kind="document", chat_id=ALLOWED,
                message_id=77, filename="voucher.pdf", mime="application/pdf", size=10)
    base.update(kw)
    return registry.add(**base)


class FakeConversation:
    def __init__(self, files=None, answer="Entendi o voucher."):
        self.files, self.answer, self.received = files or [], answer, []

    def respond(self, text, attachments=None):
        self.received.append((text, attachments))
        return ReplyResult(text=self.answer, files=list(self.files))


def make_context(data=b"%PDF-1.4", get_file_error=None):
    tg_file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(data)))
    bot_mock = SimpleNamespace(
        get_file=AsyncMock(return_value=tg_file, side_effect=get_file_error),
        send_document=AsyncMock(), send_photo=AsyncMock(),
    )
    return SimpleNamespace(bot=bot_mock)


def file_update(*, document=None, photo=None, caption="", user_id=ALLOWED):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id, type="private", send_action=AsyncMock()),
        message=SimpleNamespace(document=document, photo=photo, caption=caption, message_id=555,
                                text=None, reply_text=AsyncMock()),
    )


def pdf_doc(name="voucher.pdf", mime="application/pdf", size=2000):
    return SimpleNamespace(file_id="TGFILE-A", file_unique_id="uniqA", file_name=name,
                           mime_type=mime, file_size=size)


def handlers_for(conv, registry, read=None):
    return bot.BotHandlers(conv, ALLOWED, registry, read_document=read or (lambda d, f, m: "Hotel Sol, check-in 12/03"))


def replies(update):
    return [c.args[0] for c in update.message.reply_text.await_args_list]


# ---- receber arquivos ----------------------------------------------------------------


def test_pdf_is_downloaded_read_registered_and_handed_to_the_agent(registry):
    conv = FakeConversation()
    update, ctx = file_update(document=pdf_doc(), caption="viagem a Lisboa"), make_context()
    run(handlers_for(conv, registry).on_file(update, ctx))

    att = registry.all()[0]
    assert (att.kind, att.filename, att.mime, att.chat_id, att.message_id) == ("document", "voucher.pdf", "application/pdf", ALLOWED, 555)
    assert att.file_id == "TGFILE-A" and att.caption == "viagem a Lisboa"
    text, attachments = conv.received[0]
    assert text.startswith(f"[Arquivo enviado: voucher.pdf ({att.ref})]")
    assert "Legenda: viagem a Lisboa" in text and "Hotel Sol, check-in 12/03" in text
    assert attachments == [att.id]
    assert replies(update) == ["Entendi o voucher."]
    ctx.bot.get_file.assert_awaited_once_with("TGFILE-A")


def test_photo_uses_the_largest_size_and_jpeg_mime(registry):
    small = SimpleNamespace(file_id="small", file_unique_id="u-small", file_size=100)
    big = SimpleNamespace(file_id="big", file_unique_id="u-big", file_size=900)
    seen = {}

    def read(data, filename, mime):
        seen.update(filename=filename, mime=mime)
        return "texto da foto"

    update, ctx = file_update(photo=[small, big]), make_context(data=b"\xff\xd8")
    run(handlers_for(FakeConversation(), registry, read).on_file(update, ctx))
    att = registry.all()[0]
    assert att.kind == "photo" and att.file_id == "big"
    assert seen == {"filename": "foto-u-big.jpg", "mime": "image/jpeg"}


def test_pdf_sent_as_octet_stream_is_accepted_by_extension(registry):
    seen = {}
    read = lambda d, f, m: seen.update(mime=m) or "ok"  # noqa: E731
    doc = pdf_doc(mime="application/octet-stream")
    run(handlers_for(FakeConversation(), registry, read).on_file(file_update(document=doc), make_context()))
    assert seen["mime"] == "application/pdf" and len(registry.all()) == 1


def test_unsupported_and_oversized_files_are_refused_before_downloading(registry):
    conv, ctx = FakeConversation(), make_context()
    for doc, expected in (
        (pdf_doc("planilha.xlsx", "application/vnd.ms-excel"), "só leio PDF e imagens"),
        (pdf_doc(size=reader.MAX_FILE_BYTES + 1), "20 MB"),
    ):
        update = file_update(document=doc)
        run(handlers_for(conv, registry).on_file(update, ctx))
        assert expected in replies(update)[0]
    ctx.bot.get_file.assert_not_awaited()
    assert conv.received == [] and registry.all() == []


def test_reader_error_is_explained_and_nothing_is_registered(registry):
    def read(data, filename, mime):
        raise ReaderError("O modelo pode não aceitar imagem: defina DOCUMENT_MODEL.")

    conv, update = FakeConversation(), file_update(document=pdf_doc())
    run(handlers_for(conv, registry, read).on_file(update, make_context()))
    assert "defina DOCUMENT_MODEL" in replies(update)[0]
    assert conv.received == [] and registry.all() == []


def test_download_failure_is_generic_and_does_not_leak_details(registry, capsys):
    ctx = make_context(get_file_error=RuntimeError("token 123:abc inválido"))
    update = file_update(document=pdf_doc())
    run(handlers_for(FakeConversation(), registry).on_file(update, ctx))
    assert replies(update) == ["Não consegui baixar o arquivo agora. Tente de novo em instantes."]
    assert "123:abc" in capsys.readouterr().err  # detalhe só no console


def test_files_from_strangers_or_without_registry_are_ignored(registry):
    conv, ctx = FakeConversation(), make_context()
    stranger = file_update(document=pdf_doc(), user_id=999)
    run(handlers_for(conv, registry).on_file(stranger, ctx))
    no_registry = file_update(document=pdf_doc())
    run(bot.BotHandlers(conv, ALLOWED, None).on_file(no_registry, ctx))
    assert conv.received == [] and ctx.bot.get_file.await_count == 0
    assert stranger.message.reply_text.await_count == 0 and no_registry.message.reply_text.await_count == 0


# ---- reenviar arquivos ---------------------------------------------------------------


def test_requested_files_are_resent_by_file_id_quoting_the_original_message(registry):
    doc, photo = add_att(registry), add_att(registry, kind="photo", file_id="PHOTOID", message_id=88, filename="foto.jpg")
    conv = FakeConversation(files=[doc, photo])
    update, ctx = SimpleNamespace(**vars(file_update())), make_context()
    update.message.text = "me manda o voucher"
    run(handlers_for(conv, registry).on_message(update, ctx))

    sent = ctx.bot.send_document.await_args.kwargs
    assert sent["chat_id"] == ALLOWED and sent["document"] == "FILEID1"
    assert sent["reply_parameters"].message_id == 77
    assert sent["reply_parameters"].allow_sending_without_reply is True
    photo_sent = ctx.bot.send_photo.await_args.kwargs
    assert photo_sent["photo"] == "PHOTOID" and photo_sent["reply_parameters"].message_id == 88


def test_resend_failure_is_reported_without_breaking_the_reply(registry, capsys):
    att = add_att(registry)
    update, ctx = file_update(), make_context()
    update.message.text = "me manda o voucher"
    ctx.bot.send_document = AsyncMock(side_effect=RuntimeError("file_id inválido"))
    run(handlers_for(FakeConversation(files=[att]), registry).on_message(update, ctx))
    assert replies(update) == ["Entendi o voucher.", "Não consegui reenviar o arquivo voucher.pdf."]


# ---- tool, outbox e histórico --------------------------------------------------------


def test_send_attachment_tool_validates_ids_and_queues_once(registry):
    outbox = []
    tool = build_attachment_tools(registry, outbox)[0]
    att = add_att(registry)
    assert tool.invoke({"attachment_id": f"anexo:{att.id}"}).startswith("OK")
    assert tool.invoke({"attachment_id": att.id}).endswith("já será enviado.")
    assert [a.id for a in outbox] == [att.id]
    assert tool.invoke({"attachment_id": "naoexiste"}).startswith("ERRO")


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def make_conversation(tmp_path, registry, script):
    log = ConversationLog(tmp_path / "c.json")
    outbox = []
    store = MemoryStore(tmp_path / "mem")
    tools = build_read_tools(store) + build_attachment_tools(registry, outbox)
    agent = build_responder(llm=ScriptedModel(responses=script), tools=tools)
    service = CurationService(log, lambda m, c, ch: CurateResult(report="ok"), channel="t")
    return Conversation(log, service, agent, outbox), log, outbox


def test_conversation_collects_files_the_agent_requested(tmp_path, registry):
    att = add_att(registry)
    script = [
        AIMessage(content="", tool_calls=[{"name": "send_attachment", "args": {"attachment_id": att.id}, "id": "1"}]),
        AIMessage(content="Aqui está o voucher."),
    ]
    conv, log, outbox = make_conversation(tmp_path, registry, script)
    result = conv.respond("me manda o voucher")
    assert [f.id for f in result.files] == [att.id] and outbox == []
    assert result.text == "Aqui está o voucher."
    assert [m.role for m in log.messages()] == ["usuario", "assistente"]


def test_conversation_logs_attachment_ids_and_clears_stale_outbox(tmp_path, registry):
    att = add_att(registry)
    conv, log, outbox = make_conversation(tmp_path, registry, [AIMessage(content="ok")])
    outbox.append(att)  # sobra de uma resposta anterior que falhou
    result = conv.respond("[Arquivo enviado: v.pdf (anexo:x)]\nHotel", attachments=[att.id])
    assert result.files == [] and log.messages()[0].attachments == [att.id]


def msg(i, attachments=None, text=None):
    return Message(id=i, role="usuario", text=text or f"[Arquivo enviado: v{i}.pdf (anexo:a{i})]\nconteúdo {i}",
                   at="2026-11-17T10:00:00-03:00", attachments=attachments or [])


def test_compact_history_only_shrinks_curated_attachments_never_the_last_or_pending():
    messages = [msg(1, ["a1"]), msg(2, ["a2"]), msg(3, ["a3"]), msg(4, text="oi")]
    out = compact_history(messages, curated_upto=2)
    assert "conteúdo omitido" in out[0].text and out[0].text.startswith("[Arquivo enviado: v1.pdf (anexo:a1)]")
    assert "conteúdo 1" not in out[0].text
    assert "conteúdo 2" not in out[1].text  # curada: encurtada
    assert out[2].text == messages[2].text  # pendente: completa (o Curator ainda não rodou)
    assert out[3].text == "oi"
    last_is_file = compact_history([msg(1, ["a1"]), msg(2, ["a2"])], curated_upto=5)
    assert "conteúdo 2" in last_is_file[1].text  # a última nunca é encurtada
    assert compact_history([msg(1, text="sem anexo")], curated_upto=9)[0].text == "sem anexo"


def test_guess_mime():
    assert reader.guess_mime("a.pdf", "application/octet-stream") == "application/pdf"
    assert reader.guess_mime("FOTO.JPG", "") == "image/jpeg"
    assert reader.guess_mime("x.png", "image/png") == "image/png"
    assert reader.guess_mime("x.xlsx", "application/vnd.ms-excel") == "application/vnd.ms-excel"


# ---- prompts e montagem --------------------------------------------------------------


def test_prompts_describe_the_attachment_conventions():
    from curator.agent import load_system_prompt as curator_prompt

    cur = curator_prompt()
    assert "[Arquivo enviado: nome (anexo:ID)]" in cur and "guardar TUDO" in cur
    assert "Fonte: anexo:ab12cd34" in cur
    resp = load_system_prompt()
    assert "send_attachment" in resp and "anexo:ID" in resp


def test_main_gives_the_responder_the_send_attachment_tool(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:fake-token")
    seen = {}
    monkeypatch.setattr(bot, "build_responder", lambda tools: seen.update(names=[t.name for t in tools]) or object())
    code = bot.main(["--memory-dir", str(tmp_path / "m"), "--log", str(tmp_path / "c.json")],
                    application_factory=lambda *a: MagicMock())
    assert code == 0
    assert seen["names"] == ["list_memory", "search_memory", "read_memory", "send_attachment"]
