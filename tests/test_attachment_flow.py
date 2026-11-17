"""Attachments outside the bot: resend tool, file output of Conversation, compact history
and prompt conventions. The flow through Telegram is in test_bot.py."""

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from attachments import reader
from attachments.registry import AttachmentRegistry
from attachments.tools import build_attachment_tools
from chat_agent.agent import build_chat_agent, load_system_prompt
from chatflow import Conversation, compact_history
from conversation.log import ConversationLog, Message
from curator.agent import CurateResult
from curator.agent import load_system_prompt as curator_prompt
from curator.service import CurationService
from curator.tools import build_read_tools
from storage.store import MemoryStore

ALLOWED = 42


@pytest.fixture
def registry(tmp_path):
    return AttachmentRegistry(MemoryStore(tmp_path / "attachments-memory"))


def add_att(registry, **kw):
    base = dict(file_id="FILEID1", file_unique_id="uniq1", kind="document", chat_id=ALLOWED,
                message_id=77, filename="voucher.pdf", mime="application/pdf", size=10)
    base.update(kw)
    return registry.add(**base)


# ---- tool, outbox ----------------------------------------------------------------------------


def test_send_attachment_tool_validates_ids_and_queues_once(registry):
    outbox = []
    tool = build_attachment_tools(registry, outbox)[0]
    att = add_att(registry)
    assert tool.invoke({"attachment_id": f"attachment:{att.id}"}).startswith("OK")
    assert tool.invoke({"attachment_id": att.id}).endswith("will already be sent.")
    assert [a.id for a in outbox] == [att.id]
    assert tool.invoke({"attachment_id": "nonexistent"}).startswith("ERROR")


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def make_conversation(tmp_path, registry, script):
    log = ConversationLog(tmp_path / "c.json")
    outbox = []
    store = MemoryStore(tmp_path / "mem")
    tools = build_read_tools(store) + build_attachment_tools(registry, outbox)
    agent = build_chat_agent(llm=ScriptedModel(responses=script), tools=tools)
    service = CurationService(log, lambda m, c, ch: CurateResult(report="ok"), channel="t")
    return Conversation(log, service, agent, outbox), log, outbox


def test_conversation_collects_files_the_agent_requested(tmp_path, registry):
    att = add_att(registry)
    script = [
        AIMessage(content="", tool_calls=[{"name": "send_attachment", "args": {"attachment_id": att.id}, "id": "1"}]),
        AIMessage(content="Here is the voucher."),
    ]
    conv, log, outbox = make_conversation(tmp_path, registry, script)
    result = conv.respond("send me the voucher")
    assert [f.id for f in result.files] == [att.id] and outbox == []
    assert result.text == "Here is the voucher."
    assert [m.role for m in log.messages()] == ["user", "assistant"]


def test_conversation_logs_attachment_ids_and_clears_stale_outbox(tmp_path, registry):
    att = add_att(registry)
    conv, log, outbox = make_conversation(tmp_path, registry, [AIMessage(content="ok")])
    outbox.append(att)  # leftover from a previous reply that failed
    result = conv.respond("[File sent: v.pdf (attachment:x)]\nHotel", attachments=[att.id])
    assert result.files == [] and log.messages()[0].attachments == [att.id]


# ---- compact history ---------------------------------------------------------------------------


def msg(i, attachments=None, text=None, curated=False):
    return Message(id=i, role="user", text=text or f"[File sent: v{i}.pdf (attachment:a{i})]\ncontent {i}",
                   at="2026-11-17T10:00:00-03:00", attachments=attachments or [], curated=curated)


def test_compact_history_only_shrinks_curated_attachments_never_the_last_or_pending():
    messages = [msg(1, ["a1"], curated=True), msg(2, ["a2"], curated=True), msg(3, ["a3"]), msg(4, text="hi")]
    out = compact_history(messages)
    assert "content omitted" in out[0].text and out[0].text.startswith("[File sent: v1.pdf (attachment:a1)]")
    assert "content 1" not in out[0].text
    assert "content 2" not in out[1].text  # curated: shortened
    assert out[2].text == messages[2].text  # pending: complete (the Curator has not run yet)
    assert out[3].text == "hi"
    last_is_file = compact_history([msg(1, ["a1"], curated=True), msg(2, ["a2"], curated=True)])
    assert "content 2" in last_is_file[1].text  # the last one is never shortened
    assert compact_history([msg(1, text="no attachment", curated=True)])[0].text == "no attachment"


# ---- reader and prompts ----------------------------------------------------------------------


def test_guess_mime():
    assert reader.guess_mime("a.pdf", "application/octet-stream") == "application/pdf"
    assert reader.guess_mime("PHOTO.JPG", "") == "image/jpeg"
    assert reader.guess_mime("x.png", "image/png") == "image/png"
    assert reader.guess_mime("x.xlsx", "application/vnd.ms-excel") == "application/vnd.ms-excel"


def test_reader_placeholder_and_unreadable_texts_share_the_header():
    placeholder = reader.format_placeholder("attachment:ab12cd34", "v.pdf", "trip")
    unreadable = reader.format_unreadable("attachment:ab12cd34", "v.pdf", "trip")
    assert placeholder.splitlines()[:2] == unreadable.splitlines()[:2] == ["[File sent: v.pdf (attachment:ab12cd34)]", "Caption: trip"]
    assert placeholder.endswith("(reading the file...)") and unreadable.endswith("(could not read the file)")


def test_prompts_describe_the_attachment_conventions():
    cur = curator_prompt()
    assert "[File sent: name (attachment:ID)]" in cur and "storing EVERYTHING" in cur
    assert "Source: attachment:ab12cd34" in cur and "attachments/" in cur
    resp = load_system_prompt()
    assert "send_attachment" in resp and "attachment:ID" in resp
