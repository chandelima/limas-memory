import json

import pytest

from conversation.log import ConversationLog


@pytest.fixture
def log(tmp_path):
    return ConversationLog(tmp_path / "data" / "conversa.json")


def fill(log, n):
    return [log.append("usuario" if i % 2 == 0 else "assistente", f"msg {i}") for i in range(n)]


def test_append_assigns_increasing_ids_and_timestamps(log):
    a, b = log.append("usuario", " oi "), log.append("assistente", "olá")
    assert (a.id, b.id) == (1, 2) and a.text == "oi"
    assert a.at and "T" in a.at  # ISO 8601


def test_rejects_bad_role_and_empty_text(log):
    with pytest.raises(ValueError):
        log.append("robo", "x")
    with pytest.raises(ValueError):
        log.append("usuario", "   ")


def test_persists_across_instances_including_curated_marker(tmp_path):
    path = tmp_path / "c.json"
    first = ConversationLog(path)
    fill(first, 4)
    first.mark_curated(2)
    second = ConversationLog(path)
    assert [m.text for m in second.messages()] == ["msg 0", "msg 1", "msg 2", "msg 3"]
    assert second.curated_upto == 2 and [m.id for m in second.pending()] == [3, 4]
    assert second.append("usuario", "nova").id == 5  # ids continuam


def test_pending_recent_and_before(log):
    fill(log, 6)
    log.mark_curated(3)
    assert [m.id for m in log.pending()] == [4, 5, 6]
    assert [m.id for m in log.recent(2)] == [5, 6] and log.recent(0) == []
    assert [m.id for m in log.before(4, 2)] == [2, 3]
    assert log.before(1, 5) == []


def test_mark_curated_never_goes_backwards(log):
    fill(log, 3)
    log.mark_curated(3)
    log.mark_curated(1)
    assert log.curated_upto == 3 and log.pending() == []


def test_prune_keeps_recent_and_all_pending(log):
    fill(log, 10)
    log.mark_curated(6)
    removed = log.prune(keep=3)  # as 3 mais recentes (8, 9, 10) + pendentes (7..10)
    assert [m.id for m in log.messages()] == [7, 8, 9, 10]
    assert removed == 6
    log.mark_curated(10)
    log.prune(keep=2)
    assert [m.id for m in log.messages()] == [9, 10]
    assert log.append("usuario", "x").id == 11  # ids não são reaproveitados


def test_last_message_at(log):
    assert log.last_message_at() is None
    log.append("usuario", "a", at="2026-11-17T10:00:00-03:00")
    log.append("assistente", "b", at="2026-11-17T10:05:00-03:00")
    assert log.last_message_at().isoformat() == "2026-11-17T10:05:00-03:00"


def test_atomic_write_leaves_no_tmp_file(log):
    log.append("usuario", "oi")
    assert not list(log.path.parent.glob("*.tmp"))
    assert json.loads(log.path.read_text(encoding="utf-8"))["messages"][0]["text"] == "oi"


def test_corrupted_file_is_set_aside_and_log_starts_empty(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{ isto não é json", encoding="utf-8")
    log = ConversationLog(path)
    assert log.messages() == [] and log.append("usuario", "oi").id == 1
    assert len(list(tmp_path.glob("c.json.corrupt-*"))) == 1


def test_unicode_is_kept_readable(log):
    log.append("usuario", "café sem açúcar")
    assert "café sem açúcar" in log.path.read_text(encoding="utf-8")
