from datetime import date, time

import pytest

from storage.store import MemoryStore
from tasks import panel
from tasks.model import Recurrence, TaskError
from tasks.store import TaskStore

TODAY = date(2026, 11, 18)


@pytest.fixture
def tasks(tmp_path):
    return TaskStore(MemoryStore(tmp_path / "memory"), today=lambda: TODAY)


def labels(rows):
    return [row[0][0] for row in rows]


def test_panel_shows_unchecked_and_checked_buttons_with_callback_data(tasks):
    a = tasks.add("Doctor appointment", TODAY, time(8, 0))
    b = tasks.add("Call the bank", TODAY)
    tasks.complete(b.id)
    text, rows = panel.build_panel(tasks)
    assert "check or uncheck" in text
    assert labels(rows) == ["⭕ [08:00] Doctor appointment", "🟢 Call the bank"]  # same day: only the time
    assert panel.decode(rows[0][0][1]) == (a.id, TODAY, "today", "")
    assert all(len(data.encode()) <= 64 for _, data in sum(rows, []))


def test_overdue_upcoming_and_undated_appear_with_their_dates(tasks):
    tasks.add("Overdue", date(2026, 11, 10))
    tasks.add("Tomorrow", date(2026, 11, 19), time(9, 30))
    tasks.add("No date")
    tasks.add("Far away", date(2026, 12, 25))  # outside the "today" horizon
    _, rows = panel.build_panel(tasks)
    assert labels(rows) == ["⭕ [10/11] Overdue", "⭕ [Tomorrow 09:30] Tomorrow", "⭕ No date"]
    _, pend = panel.build_panel(tasks, "pending")
    assert "⭕ [25/12] Far away" in labels(pend)


def test_toggle_flips_the_current_state_both_ways(tasks):
    t = tasks.add("Buy bread", TODAY)
    assert panel.toggle(tasks, t.id, TODAY) == ("Buy bread", True)
    assert [x.id for x in tasks.load().pending] == []
    assert panel.toggle(tasks, t.id, TODAY) == ("Buy bread", False)  # the second tap reopens it
    assert [x.id for x in tasks.load().pending] == [t.id]


def test_recurring_task_toggles_only_todays_occurrence(tasks):
    r = tasks.add("Medication", recurrence=Recurrence.parse("daily"), due_time=time(8, 0))
    assert panel.toggle(tasks, r.id, TODAY)[1] is True
    _, rows = panel.build_panel(tasks)
    assert labels(rows) == ["🟢 [08:00] Medication"]
    assert panel.toggle(tasks, r.id, TODAY)[1] is False
    assert labels(panel.build_panel(tasks)[1]) == ["⭕ [08:00] Medication"]


def test_toggle_unknown_task_raises_task_error(tasks):
    with pytest.raises(TaskError):
        panel.toggle(tasks, "zzzz", TODAY)


def test_tag_filter_and_button_limit_and_label_truncation(tasks):
    tasks.add("Dentist #health", TODAY)
    tasks.add("Market", TODAY)
    _, rows = panel.build_panel(tasks, "today", "health")
    assert len(rows) == 1 and "Dentist" in rows[0][0][0]
    for i in range(panel.MAX_BUTTONS + 5):
        tasks.add(f"Task {i} " + "x" * 80)
    text, rows = panel.build_panel(tasks, "pending")
    assert len(rows) == panel.MAX_BUTTONS and "showing 40 of" in text
    assert all(len(r[0][0]) <= panel.LABEL_MAX for r in rows)


def test_empty_panel_has_no_buttons_and_decode_rejects_garbage(tasks):
    text, rows = panel.build_panel(tasks)
    assert rows == [] and "no tasks" in text
    assert panel.decode("other:thing") is None and panel.decode("tk:a1b2:20261340:t:") is None


def test_multi_day_panel_prefixes_every_dated_task_with_today_tomorrow_or_date(tasks):
    tasks.add("A", TODAY, time(18, 0))
    tasks.add("B", TODAY)
    tasks.add("C", date(2026, 11, 19), time(7, 0))
    tasks.add("D", date(2027, 1, 31), time(15, 0))
    tasks.add("E", date(2026, 11, 17))  # yesterday
    _, rows = panel.build_panel(tasks, "pending")
    assert labels(rows) == ["⭕ [Yesterday] E", "⭕ [Today] B", "⭕ [Today 18:00] A", "⭕ [Tomorrow 07:00] C", "⭕ [31/01 15:00] D"]
