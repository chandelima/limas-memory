import json

import pytest

from conversation.log import (
    ANSWERED, FAILED, READING, READY, RECEIVED, ConversationLog, Message, order_key,
)


@pytest.fixture
def log(tmp_path):
    return ConversationLog(tmp_path / "data" / "conversa.json")


def fill(log, n):
    return [log.append("usuario" if i % 2 == 0 else "assistente", f"msg {i}") for i in range(n)]


# ---- básico e persistência -----------------------------------------------------------


def test_append_assigns_increasing_ids_and_timestamps(log):
    a, b = log.append("usuario", " oi "), log.append("assistente", "olá")
    assert (a.id, b.id) == (1, 2) and a.text == "oi"
    assert a.at and "T" in a.at  # ISO 8601
    assert a.status == ANSWERED and a.reply_to is None and a.curated is False


def test_rejects_bad_role_status_and_empty_text(log):
    with pytest.raises(ValueError):
        log.append("robo", "x")
    with pytest.raises(ValueError):
        log.append("usuario", "   ")
    with pytest.raises(ValueError):
        log.append("usuario", "x", status="inventado")
    with pytest.raises(ValueError):
        log.update(log.append("usuario", "x").id, status="inventado")


def test_persists_status_reply_to_and_curated_across_instances(tmp_path):
    path = tmp_path / "c.json"
    first = ConversationLog(path)
    user = first.append("usuario", "oi", status=RECEIVED)
    first.append("assistente", "olá", reply_to=user.id)
    first.mark_curated([user.id])
    second = ConversationLog(path)
    assert [(m.status, m.reply_to, m.curated) for m in second.messages()] == [
        (RECEIVED, None, True), (ANSWERED, 1, False),
    ]
    assert second.append("usuario", "nova").id == 3  # ids continuam


def test_old_file_format_still_loads_with_curated_watermark(tmp_path):
    old = tmp_path / "old.json"
    old.write_text(json.dumps({
        "messages": [
            {"id": 1, "role": "usuario", "text": "a", "at": "2026-11-17T10:00:00-03:00"},
            {"id": 2, "role": "assistente", "text": "b", "at": "2026-11-17T10:00:01-03:00"},
            {"id": 3, "role": "usuario", "text": "c", "at": "2026-11-17T10:01:00-03:00"},
        ],
        "next_id": 4, "curated_upto": 2,
    }), encoding="utf-8")
    log = ConversationLog(old)
    assert [m.curated for m in log.messages()] == [True, True, False]
    assert all(m.status == ANSWERED for m in log.messages())
    assert [m.id for m in log.pending()] == [3]


def test_atomic_write_leaves_no_tmp_file_and_keeps_unicode(log):
    log.append("usuario", "café sem açúcar")
    assert not list(log.path.parent.glob("*.tmp"))
    assert "café sem açúcar" in log.path.read_text(encoding="utf-8")


def test_corrupted_file_is_set_aside_and_log_starts_empty(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{ isto não é json", encoding="utf-8")
    log = ConversationLog(path)
    assert log.messages() == [] and log.append("usuario", "oi").id == 1
    assert len(list(tmp_path.glob("c.json.corrupt-*"))) == 1


# ---- estados: chegada imediata e atualização -----------------------------------------


def test_message_is_persisted_on_arrival_before_any_processing(tmp_path):
    path = tmp_path / "c.json"
    ConversationLog(path).append("usuario", "[Arquivo enviado: v.pdf (anexo:ab12cd34)]\n(lendo)",
                                 attachments=["ab12cd34"], status=READING)
    reborn = ConversationLog(path)  # como se o bot tivesse caído e reiniciado
    assert [(m.id, m.status) for m in reborn.unfinished()] == [(1, READING)]


def test_update_changes_text_and_status_and_persists(tmp_path):
    path = tmp_path / "c.json"
    log = ConversationLog(path)
    msg = log.append("usuario", "(lendo)", status=READING)
    log.update(msg.id, text="Hotel Sol, check-in 12/03", status=READY)
    assert ConversationLog(path).get(msg.id).text == "Hotel Sol, check-in 12/03"
    assert ConversationLog(path).get(msg.id).status == READY
    log.update(msg.id, status=ANSWERED)  # só o estado: texto intacto
    assert log.get(msg.id).text == "Hotel Sol, check-in 12/03" and log.unfinished() == []
    with pytest.raises(KeyError):
        log.update(999, status=FAILED)
    with pytest.raises(KeyError):
        log.get(999)


def test_unfinished_lists_only_user_messages_in_progress(log):
    a = log.append("usuario", "a", status=RECEIVED)
    log.append("usuario", "b", status=READING)
    log.append("usuario", "c", status=READY)
    log.append("usuario", "d", status=ANSWERED)
    log.append("usuario", "e", status=FAILED)
    log.append("assistente", "resposta", reply_to=a.id)
    assert [m.text for m in log.unfinished()] == ["a", "b", "c"]


# ---- ordem da conversa ---------------------------------------------------------------


def two_quick_messages(log):
    """Usuário manda M1 e M2 em seguida; o assistente responde M1 depois de M2 já gravada."""
    m1 = log.append("usuario", "M1", status=ANSWERED)
    m2 = log.append("usuario", "M2", status=RECEIVED)
    a1 = log.append("assistente", "A1", reply_to=m1.id)
    return m1, m2, a1


def test_replies_stay_next_to_the_message_they_answer(log):
    two_quick_messages(log)
    assert [m.text for m in log.recent(10)] == ["M1", "A1", "M2"]  # ids: 1, 3, 2
    assert [m.id for m in log.messages()] == [1, 2, 3]  # por chegada


def test_order_key_orders_user_then_reply_then_next_user():
    u1 = Message(1, "usuario", "a", "t")
    a1 = Message(5, "assistente", "r", "t", reply_to=1)
    u2 = Message(2, "usuario", "b", "t")
    legacy_reply = Message(7, "assistente", "r", "t")  # sem reply_to: vale pelo próprio id
    assert sorted([u2, a1, legacy_reply, u1], key=order_key) == [u1, a1, u2, legacy_reply]


def test_history_ends_at_the_target_and_skips_later_and_unfinished_messages(log):
    m1 = log.append("usuario", "M1", status=ANSWERED)
    log.append("assistente", "A1", reply_to=m1.id)
    m2 = log.append("usuario", "M2", status=READY)
    log.append("usuario", "M3 chegou depois", status=RECEIVED)
    log.append("usuario", "M0 travada", status=READING)
    assert [m.text for m in log.history(m2.id, 10)] == ["M1", "A1", "M2"]
    assert log.history(m2.id, 2)[0].text == "A1" and log.history(m2.id, 0) == []


def test_history_for_a_second_quick_message_includes_the_first_reply(log):
    m1, m2, a1 = two_quick_messages(log)
    assert [m.text for m in log.history(m2.id, 10)] == ["M1", "A1", "M2"]
    assert [m.text for m in log.history(m1.id, 10)] == ["M1"]  # a resposta A1 ainda não existia


# ---- curadoria ----------------------------------------------------------------------


def test_pending_only_has_final_uncurated_messages_in_conversation_order(log):
    m1, m2, a1 = two_quick_messages(log)  # M2 ainda RECEIVED
    assert [m.text for m in log.pending()] == ["M1", "A1"]  # M2 em andamento fica de fora
    log.update(m2.id, status=ANSWERED)
    assert [m.text for m in log.pending()] == ["M1", "A1", "M2"]
    failed = log.append("usuario", "arquivo ilegível", status=FAILED)
    assert failed.id in [m.id for m in log.pending()]


def test_mark_curated_is_per_message_so_an_unfinished_older_message_is_not_skipped(log):
    m1, m2, a1 = two_quick_messages(log)  # M2 (id 2) em andamento, A1 (id 3) pronta
    log.mark_curated([m.id for m in log.pending()])  # cura M1 e A1 (ids 1 e 3)
    assert [m.curated for m in log.messages()] == [True, False, True]
    log.update(m2.id, status=ANSWERED)
    assert [m.text for m in log.pending()] == ["M2"]  # M2 não foi pulada, apesar de id < 3
    log.mark_curated([m2.id])
    log.mark_curated([m2.id])  # idempotente
    assert log.pending() == []


def test_curated_context_returns_the_latest_curated_in_order(log):
    fill(log, 6)
    log.mark_curated([1, 2, 3, 4])
    assert [m.id for m in log.curated_context(2)] == [3, 4]
    assert log.curated_context(0) == []


def test_prune_keeps_recent_curated_and_every_uncurated_message(log):
    msgs = fill(log, 10)
    log.mark_curated([m.id for m in msgs[:6]])  # 1..6 curadas; 7..10 pendentes
    removed = log.prune(keep=3)  # as 3 mais recentes (8, 9, 10) já estão fora das curadas
    assert removed == 6 and [m.id for m in log.messages()] == [7, 8, 9, 10]
    log.mark_curated([7, 8, 9, 10])
    log.prune(keep=2)
    assert [m.id for m in log.messages()] == [9, 10]
    assert log.append("usuario", "x").id == 11  # ids não são reaproveitados


def test_prune_never_removes_unfinished_messages(log):
    first = log.append("usuario", "antiga", status=READING)
    for _ in range(5):
        log.append("usuario", "curada").text
    log.mark_curated([m.id for m in log.messages() if m.id != first.id])
    log.prune(keep=1)
    assert first.id in [m.id for m in log.messages()]


def test_last_message_at(log):
    assert log.last_message_at() is None
    log.append("usuario", "a", at="2026-11-17T10:00:00-03:00")
    log.append("assistente", "b", at="2026-11-17T10:05:00-03:00")
    assert log.last_message_at().isoformat() == "2026-11-17T10:05:00-03:00"
