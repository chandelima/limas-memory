from datetime import date, time

import pytest

from curator.tools import build_read_tools, build_tools
from storage import index as idx
from storage import markdown as md
from storage.git import MemoryGit
from storage.store import MemoryStore, StoreError
from tasks import model
from tasks.model import Completion, Recurrence, Task, TaskError
from tasks.store import TODO_PATH, TaskStore

TODAY = date(2026, 11, 18)  # Wednesday


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory")


@pytest.fixture
def tasks(store):
    return TaskStore(store, today=lambda: TODAY)


# ---- dates, times and recurrence --------------------------------------------------------


def test_parse_date_and_time_validate_input():
    assert model.parse_date(" 2026-11-18 ") == TODAY
    assert model.parse_time("8:05") == time(8, 5) and model.parse_time("23:59") == time(23, 59)
    for bad in ("18/11/2026", "2026-13-01", "tomorrow", ""):
        with pytest.raises(TaskError, match="invalid date"):
            model.parse_date(bad)
    for bad in ("24:00", "8h", "08:60", "noon"):
        with pytest.raises(TaskError, match="invalid time"):
            model.parse_time(bad)


@pytest.mark.parametrize(
    "text,rendered",
    [("daily", "daily"), ("Daily", "daily"), ("every day", "daily"), ("weekly mon,wed", "weekly mon,wed"),
     ("weekly wednesday, monday", "weekly mon,wed"), ("weekdays", "weekly mon,tue,wed,thu,fri"),
     ("monthly 5", "monthly 5"), ("monthly day 31", "monthly 31")],
)
def test_recurrence_parse_and_render(text, rendered):
    assert Recurrence.parse(text).render() == rendered


@pytest.mark.parametrize("bad", ["", "fortnightly", "weekly", "weekly xyz", "weekly mon,xyz", "monthly 40", "monthly"])
def test_recurrence_rejects_invalid_values(bad):
    with pytest.raises(TaskError, match="invalid recurrence"):
        Recurrence.parse(bad)


def test_recurrence_occurs_on():
    assert Recurrence.parse("daily").occurs_on(TODAY)
    weekly = Recurrence.parse("weekly wed,fri")
    assert weekly.occurs_on(TODAY) and not weekly.occurs_on(date(2026, 11, 19))
    monthly = Recurrence.parse("monthly 31")
    assert monthly.occurs_on(date(2026, 12, 31)) and monthly.occurs_on(date(2026, 2, 28))  # day 31 falls on the last day
    assert not monthly.occurs_on(date(2026, 2, 27))


def test_clean_text_makes_one_line_without_carets_and_rejects_empty_or_huge():
    assert model.clean_text("  call   the\nbank ^ab12 ") == "call the bank ab12"
    with pytest.raises(TaskError):
        model.clean_text(" \n ")
    with pytest.raises(TaskError, match="too long"):
        model.clean_text("x" * 201)


# ---- Markdown <-> model ----------------------------------------------------------------


SAMPLE = """---
description: x
---
# Tasks

## Pending

- [ ] 2026-11-18 08:00 — Doctor appointment ^a1b2
- [ ] 2026-11-20 — Call the bank — urgent ^c3d4
- [ ] Buy a gift ^e5f6

## Recurring

- [ ] daily 08:00 — Take medication ^0a0a
- [ ] weekly mon,wed 19:00 — Workout ^0b0b
- [ ] monthly 5 — Pay rent ^0c0c

## Done

- [x] 2026-11-17 09:00 — Dentist (done on 2026-11-17) ^d1d1
- [x] no date — Something without a deadline (done on 2026-11-16) ^d2d2
"""


def test_parse_reads_every_kind_of_line():
    tl = model.parse(SAMPLE)
    assert [(t.id, t.text, t.due_date, t.due_time) for t in tl.pending] == [
        ("a1b2", "Doctor appointment", date(2026, 11, 18), time(8, 0)),
        ("c3d4", "Call the bank — urgent", date(2026, 11, 20), None),
        ("e5f6", "Buy a gift", None, None),
    ]
    assert [(t.id, t.recurrence.render(), t.due_time) for t in tl.recurring] == [
        ("0a0a", "daily", time(8, 0)), ("0b0b", "weekly mon,wed", time(19, 0)), ("0c0c", "monthly 5", None)]
    assert [(c.id, c.text, c.due_date, c.done_on) for c in tl.done] == [
        ("d1d1", "Dentist", date(2026, 11, 17), date(2026, 11, 17)),
        ("d2d2", "Something without a deadline", None, date(2026, 11, 16))]
    assert tl.extras == {}


def test_render_then_parse_is_lossless():
    tl = model.parse(SAMPLE)
    again = model.parse(model.render(tl))  # render sorts; the content is the same
    by_id = lambda items: sorted(items, key=lambda x: x.id)  # noqa: E731
    assert by_id(again.pending) == by_id(tl.pending)
    assert by_id(again.recurring) == by_id(tl.recurring) and by_id(again.done) == by_id(tl.done)


def test_render_sorts_and_has_valid_frontmatter_and_empty_markers():
    tl = model.TaskList(pending=[Task("b", "later", date(2026, 12, 1)), Task("a", "no date"),
                                 Task("c", "before", date(2026, 11, 1), time(9, 0)), Task("d", "before early", date(2026, 11, 1), time(7, 0))])
    text = model.render(tl)
    meta, _ = md.parse_frontmatter(text)
    assert meta["description"].startswith("Task list")
    order = [line for line in text.splitlines() if line.startswith("- [ ]")]
    assert [o.split(" — ")[-1].split(" ^")[0] if " — " in o else o for o in order] == [
        "before early", "before", "later", "- [ ] no date ^a"]
    assert text.count(model.EMPTY) == 2  # Recurring and Done are empty


def test_done_history_is_capped_keeping_the_newest():
    done = [Completion(f"{i:04x}", f"t{i}", None, None, date(2026, 1, 1) + __import__("datetime").timedelta(days=i))
            for i in range(model.MAX_DONE + 20)]
    out = model.parse(model.render(model.TaskList(done=done)))
    assert len(out.done) == model.MAX_DONE and out.done[0].text == f"t{model.MAX_DONE + 19}"


def test_unrecognized_lines_are_preserved_in_their_section():
    text = SAMPLE.replace("- [ ] Buy a gift ^e5f6", "- [ ] Buy a gift ^e5f6\na loose note\n- [ ] line without id")
    tl = model.parse(text)
    assert tl.extras == {"Pending": ["a loose note", "- [ ] line without id"]}
    again = model.render(tl)
    assert "a loose note" in again and "- [ ] line without id" in again


def test_invalid_date_or_wrong_checkbox_goes_to_extras_not_lost():
    bad = "## Pending\n\n- [ ] 2026-13-45 — impossible date ^aaaa\n\n## Done\n\n- [ ] 2026-11-17 — unchecked (done on 2026-11-17) ^bbbb\n"
    tl = model.parse(bad)
    assert tl.pending == [] and tl.done == []
    assert len(tl.extras["Pending"]) == 1 and len(tl.extras["Done"]) == 1


def test_parse_ignores_text_before_the_first_section_and_unknown_sections():
    tl = model.parse("# Tasks\n\nintroduction\n\n## Something else\n\n- [ ] x ^aaaa\n\n## Pending\n\n- [ ] y ^bbbb\n")
    assert [t.text for t in tl.pending] == ["y"]


# ---- operations ------------------------------------------------------------------------


def test_add_writes_the_file_in_the_protected_folder_and_persists(tasks, store):
    task = tasks.add("Doctor appointment", date(2026, 11, 18), time(8, 0))
    assert len(task.id) == 4
    text = store.read_doc("todos/2026-11.md")  # dated pending task: file of the due month
    assert f"- [ ] 2026-11-18 08:00 — Doctor appointment ^{task.id}" in text
    assert text.endswith("\n") and "Doctor appointment" not in store.read_doc(TODO_PATH)
    reopened = TaskStore(MemoryStore(store.root))
    assert reopened.load().pending == [task]


def test_add_validations(tasks):
    with pytest.raises(TaskError, match="a time needs a date"):
        tasks.add("x", due_time=time(8, 0))
    with pytest.raises(TaskError, match="has no date"):
        tasks.add("x", due_date=TODAY, recurrence=Recurrence.parse("daily"))
    with pytest.raises(TaskError):
        tasks.add("   ")
    assert tasks.load().pending == []


def test_ids_are_unique_and_lookup_accepts_decorated_ids(tasks):
    ids = {tasks.add(f"t{i}").id for i in range(60)}
    assert len(ids) == 60
    first = tasks.load().pending[0]
    assert tasks.complete(f"[{first.id.upper()}]").id == first.id


def test_complete_moves_a_task_to_done_with_the_date(tasks):
    task = tasks.add("Doctor appointment", date(2026, 11, 18), time(8, 0))
    done = tasks.complete(task.id)
    assert (done.done_on, done.due_date, done.due_time) == (TODAY, date(2026, 11, 18), time(8, 0))
    tl = tasks.load()
    assert tl.pending == [] and [c.id for c in tl.done] == [task.id]
    with pytest.raises(TaskError, match="is already completed"):
        tasks.complete(task.id)
    assert tasks.complete(tasks.add("another").id, on=date(2026, 11, 20)).done_on == date(2026, 11, 20)


def test_complete_unknown_id_is_an_error(tasks):
    with pytest.raises(TaskError, match="no task with ID"):
        tasks.complete("zzzz")


def test_recurring_completion_is_per_day_idempotent_and_checks_the_recurrence(tasks):
    task = tasks.add("Workout", recurrence=Recurrence.parse("weekly mon,wed"), due_time=time(19, 0))
    first = tasks.complete(task.id)  # today is Wednesday
    assert tasks.complete(task.id) == first and len(tasks.load().done) == 1
    assert [t.id for t in tasks.load().recurring] == [task.id]  # the definition stays
    with pytest.raises(TaskError, match="does not occur on 2026-11-19"):
        tasks.complete(task.id, on=date(2026, 11, 19))
    tasks.complete(task.id, on=date(2026, 11, 23))  # the following Monday
    assert len(tasks.load().done) == 2


def test_reopen_one_off_returns_it_to_pending(tasks):
    task = tasks.add("Appointment", date(2026, 11, 18), time(8, 0))
    tasks.complete(task.id)
    back = tasks.reopen(task.id)
    tl = tasks.load()
    assert (back.due_date, back.due_time) == (date(2026, 11, 18), time(8, 0))
    assert [t.id for t in tl.pending] == [task.id] and tl.done == []
    with pytest.raises(TaskError):
        tasks.reopen("zzzz")


def test_reopen_recurring_removes_only_that_days_completion(tasks):
    task = tasks.add("Medication", recurrence=Recurrence.parse("daily"))
    tasks.complete(task.id, on=TODAY)
    tasks.complete(task.id, on=date(2026, 11, 17))
    tasks.reopen(task.id)
    assert [c.done_on for c in tasks.load().done] == [date(2026, 11, 17)]
    with pytest.raises(TaskError, match="was not done"):
        tasks.reopen(task.id)


def test_reschedule_one_off_only(tasks):
    task = tasks.add("Call", date(2026, 11, 18))
    moved = tasks.reschedule(task.id, date(2026, 11, 25), time(10, 30))
    assert (moved.due_date, moved.due_time) == (date(2026, 11, 25), time(10, 30))
    assert tasks.load().pending[0].due_date == date(2026, 11, 25)
    tasks.reschedule(task.id, date(2026, 11, 26))  # no time: the previous time is cleared
    assert tasks.load().pending[0].due_time is None
    rec = tasks.add("Medication", recurrence=Recurrence.parse("daily"))
    with pytest.raises(TaskError, match="recurring"):
        tasks.reschedule(rec.id, TODAY)
    with pytest.raises(TaskError, match="no pending task"):
        tasks.reschedule("zzzz", TODAY)


def test_remove_pending_and_recurring_but_not_unknown(tasks):
    one, rec = tasks.add("one-off"), tasks.add("always", recurrence=Recurrence.parse("daily"))
    assert tasks.remove(one.id).text == "one-off" and tasks.remove(rec.id).text == "always"
    tl = tasks.load()
    assert tl.pending == [] and tl.recurring == []
    with pytest.raises(TaskError):
        tasks.remove("zzzz")


def test_missing_file_means_empty_list_and_manual_edits_survive_operations(tasks, store):
    assert tasks.load().pending == []
    store.write_doc(TODO_PATH, SAMPLE + "\nmanual note outside a section\n", internal=True)
    tasks.add("new")
    assert "new" in store.read_doc(TODO_PATH)  # no date: stays in the central file
    assert "Doctor appointment" in store.read_doc("todos/2026-11.md")  # the old list was redistributed by month
    assert {t.text for t in tasks.load().pending} >= {"new", "Doctor appointment", "Buy a gift"}


# ---- agenda ----------------------------------------------------------------------------


def test_agenda_groups_overdue_today_recurring_upcoming_and_undated(tasks):
    tasks.add("overdue", date(2026, 11, 15))
    tasks.add("appointment", TODAY, time(8, 0))
    tasks.add("today no time", TODAY)
    tasks.add("tomorrow", date(2026, 11, 19), time(9, 0))
    tasks.add("in 3 days", date(2026, 11, 21))
    tasks.add("too far", date(2026, 11, 30))
    tasks.add("no date")
    tasks.add("medication", recurrence=Recurrence.parse("daily"), due_time=time(7, 0))
    tasks.add("workout", recurrence=Recurrence.parse("weekly mon"), due_time=time(19, 0))
    agenda = tasks.agenda()
    assert agenda.today == TODAY and [t.text for t in agenda.overdue] == ["overdue"]
    assert [i.task.text for i in agenda.due_today] == ["medication", "appointment", "today no time"]  # by time; untimed last
    assert [i.recurring for i in agenda.due_today] == [True, False, False]
    # the daily task only shows under "today"; in the upcoming days, only what is specific to the day
    assert list(agenda.upcoming) == [date(2026, 11, 19), date(2026, 11, 21)]
    assert [i.task.text for i in agenda.upcoming[date(2026, 11, 21)]] == ["in 3 days"]
    assert [i.task.text for i in agenda.upcoming[date(2026, 11, 19)]] == ["tomorrow"]
    assert "too far" not in str(agenda.upcoming) and [t.text for t in agenda.undated] == ["no date"]
    assert not agenda.is_empty()


def test_agenda_marks_recurring_occurrences_already_done_today_and_is_empty_when_nothing(tasks):
    assert tasks.agenda().is_empty()
    rec = tasks.add("medication", recurrence=Recurrence.parse("daily"))
    tasks.complete(rec.id)
    items = tasks.agenda().due_today
    assert [(i.task.text, i.done) for i in items] == [("medication", True)]
    assert tasks.agenda(on=date(2026, 11, 19)).due_today[0].done is False


def test_agenda_horizon_and_reference_date(tasks):
    tasks.add("in 5 days", date(2026, 11, 23))
    assert tasks.agenda(horizon_days=3).upcoming == {}
    assert list(tasks.agenda(horizon_days=5).upcoming) == [date(2026, 11, 23)]
    assert [t.text for t in tasks.agenda(on=date(2026, 11, 24)).overdue] == ["in 5 days"]


# ---- protection: only code writes, the Curator does not --------------------------------


def test_curator_cannot_write_the_task_file_but_can_read_nothing_odd(tmp_path, store, tasks):
    tasks.add("Doctor appointment", TODAY, time(8, 0))
    doc = "---\ndescription: x\n---\n# X\nbody\n"
    with pytest.raises(StoreError, match="managed by the bot"):
        store.write_doc(TODO_PATH, doc)
    tools = {t.name: t for t in build_tools(store, MemoryGit(store.root))}
    out = tools["write_memory"].invoke({"path": TODO_PATH, "content": doc})
    assert out.startswith("ERROR") and "managed by the bot" in out
    shard = tools["write_memory"].invoke({"path": "todos/2026-11.md", "content": doc})
    assert shard.startswith("ERROR") and "managed by the bot" in shard
    assert "Doctor appointment" in store.read_doc("todos/2026-11.md")  # intact


def test_task_file_is_hidden_from_list_search_and_index(tasks, store):
    tasks.add("Doctor appointment", TODAY, time(8, 0))
    store.write_doc("trips/x.md", "---\ndescription: Trip\n---\n# Trip\nroutine checkup\n")
    assert [d.path for d in store.list_docs()] == ["trips/x.md"]
    assert [h.path for h in store.search_docs("Doctor appointment")] == []
    assert [h.path for h in store.search_docs("routine checkup")] == ["trips/x.md"]
    idx.update_index(store)
    assert "todos" not in store.read_doc("index.md")
    assert build_read_tools(store)[0].invoke({}) .count("todos") == 0


def test_task_file_goes_into_the_curators_next_commit(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    TaskStore(store, today=lambda: TODAY).add("Doctor appointment", TODAY)
    git = MemoryGit(store.root)
    assert git.commit_and_push("docs: x").sha
    assert TODO_PATH in git._git_ok("ls-files")


# ---- monthly archive of completed tasks ------------------------------------------------


def done_on(tasks, text, on):
    task = tasks.add(text)
    tasks.complete(task.id, on=on)
    return task


def test_old_completions_move_to_a_monthly_archive_and_leave_the_main_file(tasks, store):
    old = done_on(tasks, "old one from October", date(2026, 10, 5))  # > 30 days before 18/11
    recent = done_on(tasks, "recent", date(2026, 11, 10))
    main = tasks.load()
    assert [c.text for c in main.done] == ["recent"]
    archive = model.parse(store.read_doc("todos/archive/2026-10.md"))
    assert [(c.id, c.text, c.done_on) for c in archive.done] == [(old.id, "old one from October", date(2026, 10, 5))]
    meta, body = md.parse_frontmatter(store.read_doc("todos/archive/2026-10.md"))
    assert meta["description"] == "Archive of tasks completed in 2026-10."
    assert "# Tasks completed in 2026-10" in body and recent.id not in store.read_doc("todos/archive/2026-10.md")


def test_archive_is_grouped_by_month_deduplicated_and_appended(tasks, store):
    done_on(tasks, "sep 1", date(2026, 9, 1))
    done_on(tasks, "sep 2", date(2026, 9, 20))
    done_on(tasks, "oct 1", date(2026, 10, 2))
    assert [c.text for c in model.parse(store.read_doc("todos/archive/2026-09.md")).done] == ["sep 2", "sep 1"]
    assert [c.text for c in model.parse(store.read_doc("todos/archive/2026-10.md")).done] == ["oct 1"]
    done_on(tasks, "sep 3", date(2026, 9, 30))  # the existing month receives the new one
    assert [c.text for c in model.parse(store.read_doc("todos/archive/2026-09.md")).done] == ["sep 3", "sep 2", "sep 1"]
    tasks._archive(model.parse(store.read_doc("todos/archive/2026-09.md")).done)  # resending does not duplicate
    assert len(model.parse(store.read_doc("todos/archive/2026-09.md")).done) == 3


def test_overflow_beyond_the_cap_is_archived_instead_of_dropped(tasks, store, monkeypatch):
    monkeypatch.setattr(model, "MAX_DONE", 3)
    for i in range(5):
        done_on(tasks, f"t{i}", date(2026, 11, 10 + i))
    assert [c.text for c in tasks.load().done] == ["t4", "t3", "t2"]
    assert [c.text for c in model.parse(store.read_doc("todos/archive/2026-11.md")).done] == ["t1", "t0"]


def test_archive_is_protected_and_hidden_like_the_main_file(tasks, store):
    done_on(tasks, "old", date(2026, 10, 5))
    with pytest.raises(StoreError, match="managed by the bot"):
        store.write_doc("todos/archive/2026-10.md", "---\ndescription: x\n---\n# X\nbody\n")
    assert store.list_docs() == [] and store.search_docs("old") == []
    assert "todos/archive/2026-10.md" in store.list_paths()  # exists and can be read
    assert "old" in store.read_doc("todos/archive/2026-10.md")


def test_a_recent_completion_never_touches_the_archive(tasks, store):
    done_on(tasks, "today", TODAY)
    assert not (store.root / "todos" / "archive").exists()
