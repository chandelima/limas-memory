"""Anexos fora do bot: tool de reenvio, saída de arquivos da Conversation, histórico compacto
e convenções dos prompts. O fluxo pelo Telegram está em test_bot.py."""

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from attachments import reader
from attachments.registry import AttachmentRegistry
from attachments.tools import build_attachment_tools
from chatflow import Conversation, compact_history
from conversation.log import ConversationLog, Message
from curator.agent import CurateResult
from curator.agent import load_system_prompt as curator_prompt
from curator.service import CurationService
from curator.tools import build_read_tools
from responder.agent import build_responder, load_system_prompt
from storage.store import MemoryStore

ALLOWED = 42


@pytest.fixture
def registry(tmp_path):
    return AttachmentRegistry(MemoryStore(tmp_path / "memoria-anexos"))


def add_att(registry, **kw):
    base = dict(file_id="FILEID1", file_unique_id="uniq1", kind="document", chat_id=ALLOWED,
                message_id=77, filename="voucher.pdf", mime="application/pdf", size=10)
    base.update(kw)
    return registry.add(**base)


# ---- tool, outbox --------------------------------------------------------------------


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


# ---- histórico compacto --------------------------------------------------------------


def msg(i, attachments=None, text=None, curated=False):
    return Message(id=i, role="usuario", text=text or f"[Arquivo enviado: v{i}.pdf (anexo:a{i})]\nconteúdo {i}",
                   at="2026-11-17T10:00:00-03:00", attachments=attachments or [], curated=curated)


def test_compact_history_only_shrinks_curated_attachments_never_the_last_or_pending():
    messages = [msg(1, ["a1"], curated=True), msg(2, ["a2"], curated=True), msg(3, ["a3"]), msg(4, text="oi")]
    out = compact_history(messages)
    assert "conteúdo omitido" in out[0].text and out[0].text.startswith("[Arquivo enviado: v1.pdf (anexo:a1)]")
    assert "conteúdo 1" not in out[0].text
    assert "conteúdo 2" not in out[1].text  # curada: encurtada
    assert out[2].text == messages[2].text  # pendente: completa (o Curator ainda não rodou)
    assert out[3].text == "oi"
    last_is_file = compact_history([msg(1, ["a1"], curated=True), msg(2, ["a2"], curated=True)])
    assert "conteúdo 2" in last_is_file[1].text  # a última nunca é encurtada
    assert compact_history([msg(1, text="sem anexo", curated=True)])[0].text == "sem anexo"


# ---- leitor e prompts ----------------------------------------------------------------


def test_guess_mime():
    assert reader.guess_mime("a.pdf", "application/octet-stream") == "application/pdf"
    assert reader.guess_mime("FOTO.JPG", "") == "image/jpeg"
    assert reader.guess_mime("x.png", "image/png") == "image/png"
    assert reader.guess_mime("x.xlsx", "application/vnd.ms-excel") == "application/vnd.ms-excel"


def test_reader_placeholder_and_unreadable_texts_share_the_header():
    placeholder = reader.format_placeholder("anexo:ab12cd34", "v.pdf", "viagem")
    unreadable = reader.format_unreadable("anexo:ab12cd34", "v.pdf", "viagem")
    assert placeholder.splitlines()[:2] == unreadable.splitlines()[:2] == ["[Arquivo enviado: v.pdf (anexo:ab12cd34)]", "Legenda: viagem"]
    assert placeholder.endswith("(lendo o arquivo...)") and unreadable.endswith("(não foi possível ler o arquivo)")


def test_prompts_describe_the_attachment_conventions():
    cur = curator_prompt()
    assert "[Arquivo enviado: nome (anexo:ID)]" in cur and "guardar TUDO" in cur
    assert "Fonte: anexo:ab12cd34" in cur and "anexos/" in cur
    resp = load_system_prompt()
    assert "send_attachment" in resp and "anexo:ID" in resp
