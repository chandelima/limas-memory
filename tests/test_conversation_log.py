import pytest

from conversation.log import (
    ANSWERED, FAILED, READING, READY, RECEIVED, ConversationLog, Message, order_key,
)


@pytest.fixture
def log(tmp_path):
    return ConversationLog(tmp_path / "data" / "conversation.json")


def fill(log, n):
    return [log.append("user" if i % 2 == 0 else "assistant", f"msg {i}") for i in range(n)]


# ---- basics and persistence ----------------------------------------------------------


def test_append_assigns_increasing_ids_and_timestamps(log):
    a, b = log.append("user", " hi "), log.append("assistant", "hello")
    assert (a.id, b.id) == (1, 2) and a.text == "hi"
    assert a.at and "T" in a.at  # ISO 8601
    assert a.status == ANSWERED and a.reply_to is None and a.curated is False


def test_rejects_bad_role_status_and_empty_text(log):
    with pytest.raises(ValueError):
        log.append("robot", "x")
    with pytest.raises(ValueError):
        log.append("user", "   ")
    with pytest.raises(ValueError):
        log.append("user", "x", status="made-up")
    with pytest.raises(ValueError):
        log.update(log.append("user", "x").id, status="made-up")


def test_persists_status_reply_to_and_curated_across_instances(tmp_path):
    path = tmp_path / "c.json"
    first = ConversationLog(path)
    user = first.append("user", "hi", status=RECEIVED)
    first.append("assistant", "hello", reply_to=user.id)
    first.mark_curated([user.id])
    second = ConversationLog(path)
    assert [(m.status, m.reply_to, m.curated) for m in second.messages()] == [
        (RECEIVED, None, True), (ANSWERED, 1, False),
    ]
    assert second.append("user", "new").id == 3  # ids continue


def test_atomic_write_leaves_no_tmp_file_and_keeps_unicode(log):
    log.append("user", "café sem açúcar")
    assert not list(log.path.parent.glob("*.tmp"))
    assert "café sem açúcar" in log.path.read_text(encoding="utf-8")


def test_corrupted_file_is_set_aside_and_log_starts_empty(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{ this is not json", encoding="utf-8")
    log = ConversationLog(path)
    assert log.messages() == [] and log.append("user", "hi").id == 1
    assert len(list(tmp_path.glob("c.json.corrupt-*"))) == 1


# ---- statuses: immediate arrival and update ------------------------------------------


def test_message_is_persisted_on_arrival_before_any_processing(tmp_path):
    path = tmp_path / "c.json"
    ConversationLog(path).append("user", "[File sent: v.pdf (attachment:ab12cd34)]\n(reading)",
                                 attachments=["ab12cd34"], status=READING)
    reborn = ConversationLog(path)  # as if the bot had crashed and restarted
    assert [(m.id, m.status) for m in reborn.unfinished()] == [(1, READING)]


def test_update_changes_text_and_status_and_persists(tmp_path):
    path = tmp_path / "c.json"
    log = ConversationLog(path)
    msg = log.append("user", "(reading)", status=READING)
    log.update(msg.id, text="Hotel Sol, check-in 12/03", status=READY)
    assert ConversationLog(path).get(msg.id).text == "Hotel Sol, check-in 12/03"
    assert ConversationLog(path).get(msg.id).status == READY
    log.update(msg.id, status=ANSWERED)  # status only: text intact
    assert log.get(msg.id).text == "Hotel Sol, check-in 12/03" and log.unfinished() == []
    with pytest.raises(KeyError):
        log.update(999, status=FAILED)
    with pytest.raises(KeyError):
        log.get(999)


def test_unfinished_lists_only_user_messages_in_progress(log):
    a = log.append("user", "a", status=RECEIVED)
    log.append("user", "b", status=READING)
    log.append("user", "c", status=READY)
    log.append("user", "d", status=ANSWERED)
    log.append("user", "e", status=FAILED)
    log.append("assistant", "reply", reply_to=a.id)
    assert [m.text for m in log.unfinished()] == ["a", "b", "c"]


# ---- conversation order --------------------------------------------------------------


def two_quick_messages(log):
    """The user sends M1 and M2 back to back; the assistant answers M1 after M2 is already recorded."""
    m1 = log.append("user", "M1", status=ANSWERED)
    m2 = log.append("user", "M2", status=RECEIVED)
    a1 = log.append("assistant", "A1", reply_to=m1.id)
    return m1, m2, a1


def test_replies_stay_next_to_the_message_they_answer(log):
    two_quick_messages(log)
    assert [m.text for m in log.recent(10)] == ["M1", "A1", "M2"]  # ids: 1, 3, 2
    assert [m.id for m in log.messages()] == [1, 2, 3]  # by arrival


def test_order_key_orders_user_then_reply_then_next_user():
    u1 = Message(1, "user", "a", "t")
    a1 = Message(5, "assistant", "r", "t", reply_to=1)
    u2 = Message(2, "user", "b", "t")
    legacy_reply = Message(7, "assistant", "r", "t")  # no reply_to: its own id counts
    assert sorted([u2, a1, legacy_reply, u1], key=order_key) == [u1, a1, u2, legacy_reply]


def test_history_ends_at_the_target_and_skips_later_and_unfinished_messages(log):
    m1 = log.append("user", "M1", status=ANSWERED)
    log.append("assistant", "A1", reply_to=m1.id)
    m2 = log.append("user", "M2", status=READY)
    log.append("user", "M3 arrived later", status=RECEIVED)
    log.append("user", "M0 stuck", status=READING)
    assert [m.text for m in log.history(m2.id, 10)] == ["M1", "A1", "M2"]
    assert log.history(m2.id, 2)[0].text == "A1" and log.history(m2.id, 0) == []


def test_history_for_a_second_quick_message_includes_the_first_reply(log):
    m1, m2, a1 = two_quick_messages(log)
    assert [m.text for m in log.history(m2.id, 10)] == ["M1", "A1", "M2"]
    assert [m.text for m in log.history(m1.id, 10)] == ["M1"]  # reply A1 did not exist yet


# ---- curation ------------------------------------------------------------------------


def test_pending_only_has_final_uncurated_messages_in_conversation_order(log):
    m1, m2, a1 = two_quick_messages(log)  # M2 still RECEIVED
    assert [m.text for m in log.pending()] == ["M1", "A1"]  # M2 in progress is left out
    log.update(m2.id, status=ANSWERED)
    assert [m.text for m in log.pending()] == ["M1", "A1", "M2"]
    failed = log.append("user", "unreadable file", status=FAILED)
    assert failed.id in [m.id for m in log.pending()]


def test_mark_curated_is_per_message_so_an_unfinished_older_message_is_not_skipped(log):
    m1, m2, a1 = two_quick_messages(log)  # M2 (id 2) in progress, A1 (id 3) ready
    log.mark_curated([m.id for m in log.pending()])  # curates M1 and A1 (ids 1 and 3)
    assert [m.curated for m in log.messages()] == [True, False, True]
    log.update(m2.id, status=ANSWERED)
    assert [m.text for m in log.pending()] == ["M2"]  # M2 was not skipped, despite id < 3
    log.mark_curated([m2.id])
    log.mark_curated([m2.id])  # idempotent
    assert log.pending() == []


def test_curated_context_returns_the_latest_curated_in_order(log):
    fill(log, 6)
    log.mark_curated([1, 2, 3, 4])
    assert [m.id for m in log.curated_context(2)] == [3, 4]
    assert log.curated_context(0) == []


def test_prune_keeps_recent_curated_and_every_uncurated_message(log):
    msgs = fill(log, 10)
    log.mark_curated([m.id for m in msgs[:6]])  # 1..6 curated; 7..10 pending
    removed = log.prune(keep=3)  # the 3 most recent (8, 9, 10) are already outside the curated ones
    assert removed == 6 and [m.id for m in log.messages()] == [7, 8, 9, 10]
    log.mark_curated([7, 8, 9, 10])
    log.prune(keep=2)
    assert [m.id for m in log.messages()] == [9, 10]
    assert log.append("user", "x").id == 11  # ids are not reused


def test_prune_never_removes_unfinished_messages(log):
    first = log.append("user", "old", status=READING)
    for _ in range(5):
        log.append("user", "curated").text
    log.mark_curated([m.id for m in log.messages() if m.id != first.id])
    log.prune(keep=1)
    assert first.id in [m.id for m in log.messages()]


def test_last_message_at(log):
    assert log.last_message_at() is None
    log.append("user", "a", at="2026-11-17T10:00:00-03:00")
    log.append("assistant", "b", at="2026-11-17T10:05:00-03:00")
    assert log.last_message_at().isoformat() == "2026-11-17T10:05:00-03:00"


def test_history_only_includes_answered_user_messages_besides_the_target(log):
    answered = log.append("user", "answered", status=ANSWERED)
    log.append("assistant", "reply", reply_to=answered.id)
    log.append("user", "failed", status=FAILED)
    log.append("user", "in progress", status=READING)
    target = log.append("user", "target", status=READY)
    assert [m.text for m in log.history(target.id, 10)] == ["answered", "reply", "target"]
