import pytest

import chatflow
from chatflow import Conversation, describe_status
from conversation.log import ANSWERED, FAILED, READING, READY, RECEIVED, ConversationLog
from curator.agent import CurateResult
from curator.service import CurationService
from responder.agent import ReplyResult


@pytest.fixture
def seen(monkeypatch):
    """Respostas fixas do agente; guarda o histórico que ele recebeu em cada turno."""
    calls, answers = [], iter(["R1", "R2", "R3"])

    def fake_reply(history, agent=None, now=None):
        calls.append([(m.role, m.text) for m in history])
        return ReplyResult(text=next(answers))

    monkeypatch.setattr(chatflow, "reply", fake_reply)
    return calls


@pytest.fixture
def conv(tmp_path):
    log = ConversationLog(tmp_path / "c.json")
    service = CurationService(log, lambda m, c, ch: CurateResult(report="ok"), channel="t")
    return Conversation(log, service, agent=object())


def test_ingest_persists_immediately_with_received_status(conv, tmp_path):
    msg = conv.ingest("tomo café")
    assert msg.status == RECEIVED
    assert ConversationLog(tmp_path / "c.json").get(msg.id).text == "tomo café"  # já no disco
    assert [m.id for m in conv.unfinished()] == [msg.id]


def test_respond_to_answers_marks_the_message_and_links_the_reply(conv, seen):
    msg = conv.ingest("tomo café")
    result = conv.respond_to(msg.id)
    assert result.text == "R1"
    user, reply = conv.log.messages()
    assert user.status == ANSWERED and (reply.role, reply.text, reply.reply_to) == ("assistente", "R1", msg.id)
    assert conv.unfinished() == []


def test_two_quick_messages_keep_conversation_order_in_each_turn(conv, seen):
    m1, m2 = conv.ingest("primeira"), conv.ingest("segunda")  # ambas gravadas antes de qualquer resposta
    conv.respond_to(m1.id)
    conv.respond_to(m2.id)
    assert seen[0] == [("usuario", "primeira")]  # a segunda ainda não existia para o turno 1
    assert seen[1] == [("usuario", "primeira"), ("assistente", "R1"), ("usuario", "segunda")]
    assert [(m.role, m.text) for m in conv.log.recent(10)] == [
        ("usuario", "primeira"), ("assistente", "R1"), ("usuario", "segunda"), ("assistente", "R2"),
    ]


def test_file_message_goes_from_reading_to_ready_with_the_extracted_text(conv, seen):
    msg = conv.ingest("[Arquivo enviado: v.pdf (anexo:ab12cd34)]\n(lendo)", attachments=["ab12cd34"], status=READING)
    assert msg.status == READING
    conv.set_content(msg.id, "[Arquivo enviado: v.pdf (anexo:ab12cd34)]\nHotel Sol, check-in 12/03")
    assert conv.log.get(msg.id).status == READY
    conv.respond_to(msg.id)
    assert "Hotel Sol, check-in 12/03" in seen[0][0][1] and "(lendo)" not in seen[0][0][1]


def test_fail_resolves_the_message_without_a_reply_and_curator_still_sees_it(conv):
    msg = conv.ingest("[Arquivo enviado: v.pdf (anexo:ab12cd34)]\n(lendo)", status=READING)
    conv.fail(msg.id, text="[Arquivo enviado: v.pdf (anexo:ab12cd34)]\n(não foi possível ler o arquivo)")
    assert conv.log.get(msg.id).status == FAILED and conv.unfinished() == []
    assert "não foi possível ler" in conv.log.pending()[0].text


def test_respond_shortcut_still_ingests_and_answers(conv, seen):
    assert conv.respond("oi").text == "R1"
    assert [m.status for m in conv.log.messages()] == [ANSWERED, ANSWERED]


def test_unfinished_older_message_does_not_leak_into_the_history_of_a_later_turn(conv, seen):
    stuck = conv.ingest("travada", status=READING)
    later = conv.ingest("depois")
    conv.respond_to(later.id)
    assert seen[0] == [("usuario", "depois")]
    assert [m.id for m in conv.unfinished()] == [stuck.id]  # continua pendente para recuperação


def test_status_counts_messages_in_progress(conv):
    assert "em processamento" not in describe_status(conv.service)
    conv.ingest("a")
    conv.ingest("b", status=READING)
    assert "mensagens em processamento: 2" in describe_status(conv.service)
