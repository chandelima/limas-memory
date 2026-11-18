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
    assert "Tap a task to open it" in text
    assert labels(rows) == ["⭕ [08:00] Doctor appointment", "🟢 Call the bank"]  # same day: only the time
    assert panel.decode(rows[0][0][1]) == panel.Action("tk", a.id, TODAY, "today", "")
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


LONG = "Medical appointment with Dr. Francisco Patriota at ProntoMED, bring the exams and the insurance card"


def test_the_detail_card_shows_the_full_text_state_when_and_the_buttons(tasks):
    task = tasks.add(LONG + " #health", date(2026, 11, 19), time(8, 0))
    text, rows = panel.build_detail(tasks, task.id, TODAY, "week", "health")
    assert text.splitlines()[0] == LONG + " #health"  # nothing is cut
    assert "⭕ Not done yet" in text and "When: Tomorrow 08:00" in text
    (toggle_label, toggle_data), (remove_label, remove_data), (back_label, back_data) = rows[0][0], rows[1][0], rows[2][0]
    assert toggle_label == "🟢 Mark as done" and remove_label == "🗑️ Remove task" and back_label == "⬅️ Back to the list"
    assert panel.decode(remove_data) == panel.Action("tr", task.id, TODAY, "week", "health")
    assert panel.decode(toggle_data) == panel.Action("tt", task.id, TODAY, "week", "health")
    assert panel.decode(back_data) == panel.Action("tb", "", None, "week", "health")
    assert all(len(d.encode()) <= 64 for row in rows for _, d in row)


def test_the_card_follows_the_state_and_covers_recurring_undated_and_linked_tasks(tasks):
    store = tasks.store
    store.write_doc("trips/x.md", "---\ndescription: x\n---\n# X\nbody\n")
    one = tasks.add("Buy bread")
    tasks.complete(one.id)
    text, rows = panel.build_detail(tasks, one.id, TODAY)
    assert "🟢 Done" in text and "No date" in text and rows[0][0][0] == "⭕ Mark as not done"
    rec = tasks.add("Medication", recurrence=Recurrence.parse("daily"), due_time=time(8, 0), ref="trips/x.md")
    text, rows = panel.build_detail(tasks, rec.id, TODAY)
    assert "Repeats: daily 08:00" in text and "Details: trips/x.md" in text and "⭕ Not done yet" in text
    tasks.complete(rec.id)
    assert "🟢 Done" in panel.build_detail(tasks, rec.id, TODAY)[0]


def test_the_card_of_a_missing_task_is_an_error(tasks):
    with pytest.raises(TaskError):
        panel.build_detail(tasks, "zzzz", TODAY)


def test_back_callback_roundtrip_and_garbage():
    assert panel.decode(panel.encode_back("pending", "")) == panel.Action("tb", "", None, "pending", "")
    assert panel.decode("tb::::x:") is None and panel.decode("tt:a1b2:20261118:q:") is None


def test_a_completed_one_off_task_has_no_remove_button_but_recurring_ones_do(tasks):
    one = tasks.add("Buy bread")
    tasks.complete(one.id)
    assert [r[0][0] for r in panel.build_detail(tasks, one.id, TODAY)[1]] == ["⭕ Mark as not done", "⬅️ Back to the list"]
    rec = tasks.add("Medication", recurrence=Recurrence.parse("daily"))
    assert "🗑️ Remove task" in [r[0][0] for r in panel.build_detail(tasks, rec.id, TODAY)[1]]


def test_removal_asks_for_confirmation_first_and_cancel_goes_back_to_the_card(tasks):
    task = tasks.add(LONG + " #health", TODAY)
    text, rows = panel.build_confirm_remove(tasks, task.id, TODAY, "pending", "health")
    assert text.startswith("Remove this task?") and LONG in text
    assert [r[0][0] for r in rows] == ["🗑️ Yes, remove it", "Cancel"]
    assert panel.decode(rows[0][0][1]).kind == "tx" and panel.decode(rows[1][0][1]).kind == "tk"  # cancel = open the card
    assert [x.id for x in tasks.load().pending] == [task.id]  # asking removes nothing
    assert panel.remove(tasks, task.id) == LONG
    assert tasks.load().pending == []
    with pytest.raises(TaskError):
        panel.build_confirm_remove(tasks, task.id, TODAY)
    with pytest.raises(Exception):
        panel.remove(tasks, task.id)


def test_a_weekly_task_shows_today_and_its_next_occurrence_in_the_list(tasks):
    # today is Wednesday: "Wash clothes" repeats on Wednesdays and Thursdays (the reported case)
    tasks.add("Wash clothes", recurrence=Recurrence.parse("weekly wed,thu"))
    _, rows = panel.build_panel(tasks)
    assert labels(rows) == ["⭕ [Today] Wash clothes", "⭕ [Tomorrow] Wash clothes"]
    today_action, tomorrow_action = (panel.decode(r[0][1]) for r in rows)
    assert today_action.day == TODAY and tomorrow_action.day == date(2026, 11, 19)  # each button knows its day
    _, week = panel.build_panel(tasks, "week")  # 7 days: Wed 18, Thu 19 and Wed 25 (Thu 26 is outside)
    assert [panel.decode(r[0][1]).day for r in week] == [TODAY, date(2026, 11, 19), date(2026, 11, 25)]


def test_a_daily_task_still_shows_only_today(tasks):
    tasks.add("Medication", recurrence=Recurrence.parse("daily"))
    assert len(panel.build_panel(tasks)[1]) == 1


def test_the_card_of_a_future_occurrence_says_the_day_and_has_no_check_button(tasks):
    task = tasks.add("Wash clothes", recurrence=Recurrence.parse("weekly wed,thu"), due_time=time(19, 0))
    tomorrow = date(2026, 11, 19)
    text, rows = panel.build_detail(tasks, task.id, tomorrow)
    assert "Repeats: weekly wed,thu 19:00" in text and "When: Tomorrow 19:00" in text
    assert [r[0][0] for r in rows] == ["🗑️ Remove task", "⬅️ Back to the list"]  # no "mark as done" yet
    today_text, today_rows = panel.build_detail(tasks, task.id, TODAY)
    assert "When:" not in today_text and today_rows[0][0][0] == "🟢 Mark as done"  # today's card is as before


def test_a_stale_tap_cannot_complete_a_future_occurrence(tasks):
    task = tasks.add("Wash clothes", recurrence=Recurrence.parse("weekly wed,thu"))
    with pytest.raises(TaskError, match="still in the future"):
        panel.toggle(tasks, task.id, date(2026, 11, 19))
    assert tasks.load().done == []
    assert panel.toggle(tasks, task.id, TODAY) == ("Wash clothes", True)  # today's occurrence works as always
    _, rows = panel.build_panel(tasks)
    assert labels(rows) == ["🟢 [Today] Wash clothes", "⭕ [Tomorrow] Wash clothes"]  # only today's got checked
