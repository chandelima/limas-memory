import time as clock
from datetime import date, time

import pytest

from chat_agent.agent import load_system_prompt as chat_agent_prompt
from curator.agent import load_system_prompt as curator_prompt
from storage import index as idx
from storage import markdown as md
from storage.store import MemoryStore
from tasks import model
from tasks.model import Recurrence, Task, TaskError
from tasks.store import INDEX_PATH, TODO_PATH, TaskStore
from tasks.tools import PAGE_SIZE, build_task_tools

TODAY = date(2026, 11, 18)
TRIP = "trips/petrolina-2027.md"


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "memory")
    s.write_doc(TRIP, "---\ndescription: Trip to Petrolina\n---\n# Trip\nbody\n")
    s.write_doc("people/ana.md", "---\ndescription: Ana\n---\n# Ana\nbody\n")
    return s


@pytest.fixture
def tasks(store):
    return TaskStore(store, today=lambda: TODAY)


@pytest.fixture
def tools(tasks):
    return {t.name: t for t in build_task_tools(tasks)}


def paths(store):
    return sorted(p for p in store.list_paths() if p.startswith("todos/"))


# ---- pending tasks split by due month ------------------------------------------------------


def test_dated_pending_tasks_go_to_one_file_per_due_month(tasks, store):
    tasks.add("Appointment", date(2026, 11, 18), time(8, 0))
    tasks.add("Flight", date(2027, 1, 27), time(2, 25))
    tasks.add("Check-in", date(2027, 1, 31), time(15, 0))
    tasks.add("No date")
    tasks.add("Medication", recurrence=Recurrence.parse("daily"))
    assert paths(store) == ["todos/2026-11.md", "todos/2027-01.md", "todos/index.md", "todos/todo.md"]
    assert [t.text for t in model.parse(store.read_doc("todos/2027-01.md")).pending] == ["Flight", "Check-in"]
    central = model.parse(store.read_doc(TODO_PATH))
    assert [t.text for t in central.pending] == ["No date"] and [t.text for t in central.recurring] == ["Medication"]
    meta, body = md.parse_frontmatter(store.read_doc("todos/2027-01.md"))
    assert meta["description"] == "Pending tasks due in 2027-01." and "# Tasks due in 2027-01" in body


def test_load_merges_central_and_every_month_and_operations_find_tasks_in_any_month(tasks):
    a = tasks.add("January", date(2027, 1, 10))
    b = tasks.add("February", date(2027, 2, 10))
    c = tasks.add("November", date(2026, 11, 20))
    assert {t.text for t in tasks.load().pending} == {"January", "February", "November"}
    assert tasks.complete(b.id).text == "February"
    assert tasks.retag(a.id, add="travel").tags == ("travel",)
    assert tasks.remove(c.id).text == "November"
    assert [t.text for t in tasks.load().pending] == ["January"]


def test_reschedule_moves_the_task_between_month_files_and_leaves_an_empty_month_behind(tasks, store):
    task = tasks.add("Change month", date(2026, 11, 25))
    tasks.reschedule(task.id, date(2027, 3, 4), time(9, 0))
    assert "Change month" in store.read_doc("todos/2027-03.md")
    assert "Change month" not in store.read_doc("todos/2026-11.md") and model.EMPTY in store.read_doc("todos/2026-11.md")
    tasks.reschedule(task.id, date(2027, 3, 5))  # same month: only the line changes
    assert "2027-03-05 — Change month" in store.read_doc("todos/2027-03.md")
    task2 = tasks.add("No date")
    tasks.reschedule(task2.id, date(2026, 12, 1))  # from "no date" (central) to a month
    assert "No date" not in store.read_doc(TODO_PATH) and "No date" in store.read_doc("todos/2026-12.md")


def test_only_changed_files_are_rewritten(tasks, store, monkeypatch):
    tasks.add("January", date(2027, 1, 10))
    tasks.add("February", date(2027, 2, 10))
    written = []
    original = store.write_doc
    monkeypatch.setattr(store, "write_doc", lambda path, *a, **k: written.append(path) or original(path, *a, **k))
    tasks.add("Another January one", date(2027, 1, 20))
    # January changed and so did the index (the count); the central file and February, unchanged, were not rewritten
    assert sorted(written) == ["todos/2027-01.md", "todos/index.md"]
    written.clear()
    tasks.add("No date")
    assert sorted(written) == ["todos/index.md", "todos/todo.md"]


def test_a_legacy_central_file_with_dated_tasks_is_redistributed_on_the_first_save(tasks, store):
    legacy = """---
description: x
---
# Tasks

## Pending

- [ ] 2026-11-18 08:00 — Doctor appointment #health ^09a7
- [ ] 2027-01-27 02:25 — Flight ^2e4f
- [ ] Buy a gift ^e5f6

## Recurring

_None._

## Done

_None._
"""
    store.write_doc(TODO_PATH, legacy, internal=True)
    assert {t.id for t in tasks.load().pending} == {"09a7", "2e4f", "e5f6"}  # the old list is still readable
    tasks.add("New")
    assert "09a7" in store.read_doc("todos/2026-11.md") and "2e4f" in store.read_doc("todos/2027-01.md")
    central = store.read_doc(TODO_PATH)
    assert "09a7" not in central and "2e4f" not in central and "e5f6" in central


def test_manual_notes_in_a_month_file_survive_and_stay_in_that_file(tasks, store):
    tasks.add("January", date(2027, 1, 10))
    text = store.read_doc("todos/2027-01.md")
    store.write_doc("todos/2027-01.md", text.replace("## Pending\n", "## Pending\n\nloose note\n"), internal=True)
    tasks.add("Other", date(2027, 1, 11))
    assert "loose note" in store.read_doc("todos/2027-01.md")
    assert "loose note" not in store.read_doc(TODO_PATH)


def test_duplicate_ids_across_files_are_ignored_not_doubled(tasks, store):
    task = tasks.add("January", date(2027, 1, 10))
    store.write_doc("todos/2027-02.md", model.render_month([Task(task.id, "Copy", date(2027, 2, 1))], "2027-02"), internal=True)
    assert [t.text for t in tasks.load().pending] == ["January"]


# ---- index of the todos/ folder -------------------------------------------------------------------


def test_index_lists_central_every_month_and_the_archive_with_counts(tasks, store):
    tasks.add("Overdue", date(2026, 10, 3))
    tasks.add("Appointment", date(2026, 11, 18))
    tasks.add("Flight", date(2027, 1, 27))
    tasks.add("Check-in", date(2027, 1, 31))
    tasks.add("No date")
    tasks.add("Medication", recurrence=Recurrence.parse("daily"))
    old = tasks.add("Old")
    tasks.complete(old.id, on=date(2026, 9, 12))
    text = store.read_doc(INDEX_PATH)
    meta, body = md.parse_frontmatter(text)
    assert meta["description"].startswith("Task index")
    assert "- [Undated, recurring and recently completed](todo.md) — 1 undated task, 1 recurring, 0 recently completed" in body
    assert "- [2026-10](2026-10.md) — 1 pending (due date has passed)" in body
    assert "- [2026-11](2026-11.md) — 1 pending\n" in body and "- [2027-01](2027-01.md) — 2 pending" in body
    assert "- [2026-09](archive/2026-09.md) — 1 completed" in body
    assert body.index("2026-10](") < body.index("2027-01](")  # months in chronological order


def test_index_marks_empty_months_and_updates_when_tasks_change(tasks, store):
    task = tasks.add("Only one", date(2027, 1, 10))
    tasks.complete(task.id)
    assert "- [2027-01](2027-01.md) — none pending" in store.read_doc(INDEX_PATH)
    assert "- [2027-01]" not in store.read_doc(INDEX_PATH).split("## Completed")[1]  # and does not get mixed up with the archive
    tasks.add("Again", date(2027, 1, 12))
    assert "- [2027-01](2027-01.md) — 1 pending" in store.read_doc(INDEX_PATH)


def test_empty_index_and_links_resolve_to_real_files(tasks, store):
    tasks.add("January", date(2027, 1, 10))
    tasks.complete(tasks.add("Old").id, on=date(2026, 9, 12))
    assert idx.find_broken_links(store) == []  # every index link points to files that exist
    empty = TaskStore(MemoryStore(store.root.parent / "other"), today=lambda: TODAY)
    empty.add("x")
    assert "## Pending by month\n\n_None._" in empty.store.read_doc(INDEX_PATH)


def test_the_index_is_protected_hidden_and_not_part_of_the_task_data(tasks, store):
    tasks.add("January", date(2027, 1, 10))
    assert INDEX_PATH not in [d.path for d in store.list_docs()] and store.search_docs("Task index") == []
    assert [t.text for t in tasks.load().pending] == ["January"]  # the index is neither a task nor a monthly file


# ---- reference to documents ----------------------------------------------------------------------


def test_ref_is_written_as_a_relative_markdown_link_and_parsed_back(tasks, store):
    task = tasks.add("Azul flight", date(2027, 1, 27), time(2, 25), tags="travel", ref=TRIP)
    line = [x for x in store.read_doc("todos/2027-01.md").splitlines() if "Azul flight" in x][0]
    assert line == f"- [ ] 2027-01-27 02:25 — Azul flight #travel [details](../{TRIP}) ^{task.id}"
    assert tasks.load().pending[0].ref == TRIP and tasks.load().pending[0].text == "Azul flight"
    assert idx.find_broken_links(store) == []  # the link points to a document that exists


def test_ref_survives_complete_reopen_archive_and_recurring_tasks(tasks):
    one = tasks.add("Flight", TODAY, ref=TRIP)
    done = tasks.complete(one.id)
    assert done.ref == TRIP and tasks.load().done[0].ref == TRIP
    assert tasks.reopen(one.id).ref == TRIP
    rec = tasks.add("Call Ana", recurrence=Recurrence.parse("weekly wed"), ref="people/ana.md")
    assert tasks.load().recurring[0].ref == "people/ana.md" and tasks.complete(rec.id).ref == "people/ana.md"
    old = tasks.add("Old", ref=TRIP)
    tasks.complete(old.id, on=date(2026, 9, 12))
    archived = model.parse(tasks.store.read_doc("todos/archive/2026-09.md")).done
    assert archived[0].ref == TRIP


def test_ref_is_validated_it_must_exist_and_not_point_into_managed_folders(tasks):
    for bad, expected in (("trips/missing.md", "does not exist in the memory"), ("../secret.md", "invalid reference"),
                          ("trips/x.txt", "invalid reference"), ("todos/todo.md", "todos/ or attachments/"),
                          ("attachments/abcd1234.md", "do not reference"), ("a b.md", "invalid reference"), ("a(1).md", "invalid reference")):
        with pytest.raises(TaskError, match=expected):
            tasks.add("x", ref=bad)
    assert tasks.load().pending == []
    assert tasks.add("ok", ref="/" + TRIP).ref == TRIP  # a leading slash is tolerated


def test_set_ref_links_existing_tasks_and_empty_ref_removes_the_link(tasks):
    task = tasks.add("Flight", date(2027, 1, 27))
    assert tasks.set_ref(task.id, TRIP).ref == TRIP and tasks.load().pending[0].ref == TRIP
    assert tasks.set_ref(task.id, "").ref == "" and tasks.load().pending[0].ref == ""
    with pytest.raises(TaskError, match="no pending or recurring task"):
        tasks.set_ref("zzzz", TRIP)
    with pytest.raises(TaskError, match="does not exist in the memory"):
        tasks.set_ref(task.id, "trips/x.md")


def test_ref_tools_and_listings_show_the_document_path(tools, tasks):
    out = tools["add_task"].invoke({"text": "Azul flight", "due_date": "2027-01-27", "due_time": "02:25",
                                    "tags": "travel", "ref": TRIP})
    assert out.endswith(f"(see {TRIP})") and out.startswith("OK: created [")
    assert "ERROR" in tools["add_task"].invoke({"text": "x", "ref": "trips/nothing.md"})
    legacy = tasks.add("Check-in", date(2027, 1, 31))
    assert tools["link_task"].invoke({"task_id": legacy.id, "ref": TRIP}).startswith("OK: reference updated")
    listing = tools["list_tasks"].invoke({"scope": "pending"})
    assert listing.count(f"(see {TRIP})") == 2
    assert tools["link_task"].invoke({"task_id": "zzzz", "ref": TRIP}).startswith("ERROR")
    assert tools["link_task"].invoke({"task_id": legacy.id, "ref": "trips/nothing.md"}).startswith("ERROR")


def test_the_morning_briefing_does_not_show_document_paths(tasks):
    from tasks.format import format_agenda

    tasks.add("Flight", TODAY, time(8, 0), ref=TRIP)
    assert TRIP not in format_agenda(tasks.agenda())  # text for the user: no paths
    assert TRIP in format_agenda(tasks.agenda(), show_ids=True)  # text for the agent: with the reference


# ---- pagination and scale ------------------------------------------------------------------------


def seed(tasks, n):
    """Create n pending tasks at once (a single _save), spread over 12 months."""
    tl = model.TaskList(pending=[
        Task(f"{i:04x}", f"Appointment {i}", date(2026 + (10 + i % 12) // 12, (10 + i % 12) % 12 + 1, 1 + i % 28),
             time(8 + i % 10, 0)) for i in range(n)])
    tasks._save(tl)


def test_list_pending_is_paginated_with_footer_and_month_filter(tools, tasks):
    seed(tasks, PAGE_SIZE * 2 + 5)
    page1 = tools["list_tasks"].invoke({"scope": "pending"})
    assert page1.count("\n- [") == PAGE_SIZE and f"Page 1/3 ({PAGE_SIZE * 2 + 5} tasks). Use page=2 to continue." in page1
    page3 = tools["list_tasks"].invoke({"scope": "pending", "page": 3})
    assert page3.count("\n- [") == 5 and "Page 3/3" in page3 and "continue" not in page3
    assert tools["list_tasks"].invoke({"scope": "pending", "page": 4}).startswith("ERROR: page 4 does not exist (there are 3)")
    assert tools["list_tasks"].invoke({"scope": "pending", "page": 0}).startswith("ERROR")
    month = tools["list_tasks"].invoke({"scope": "pending", "month": "2026-11"})
    assert "Page 1/1" in month and all("/11 (" in line for line in month.splitlines() if line.startswith("- ["))
    assert tools["list_tasks"].invoke({"scope": "pending", "month": "november"}).startswith("ERROR: invalid month")
    assert "No tasks in this list." in tools["list_tasks"].invoke({"scope": "pending", "month": "2030-01"})


def test_pagination_is_ordered_by_date_across_pages(tools, tasks):
    seed(tasks, PAGE_SIZE + 10)
    first = tools["list_tasks"].invoke({"scope": "pending", "page": 1})
    second = tools["list_tasks"].invoke({"scope": "pending", "page": 2})
    pending = sorted(tasks.load().pending, key=lambda t: (t.due_date, t.due_time))
    assert first.index(pending[0].id) < first.index(pending[PAGE_SIZE - 1].id)
    assert pending[PAGE_SIZE].id in second and pending[PAGE_SIZE].id not in first


def test_two_thousand_appointments_stay_fast_and_split_across_month_files(tasks, store):
    seed(tasks, 2000)
    month_files = [p for p in paths(store) if p not in (TODO_PATH, INDEX_PATH)]
    assert len(month_files) == 12 and max(len(store.read_doc(p).splitlines()) for p in month_files) < 250
    assert len(store.read_doc(TODO_PATH).splitlines()) < 20  # the central file does not grow with the appointments
    started = clock.perf_counter()
    assert len(tasks.load().pending) == 2000
    added = tasks.add("One more", date(2027, 5, 5))
    tasks.complete(added.id)
    assert clock.perf_counter() - started < 3  # load everything + two complete operations


def test_the_agenda_and_the_briefing_stay_small_with_two_thousand_appointments(tasks):
    from briefing import build_briefing

    seed(tasks, 2000)
    text = build_briefing(tasks, date(2026, 12, 1))
    lines = text.splitlines()
    assert len(lines) < 60 and len(text) < 3500  # fits in a Telegram message (limit 4096)
    overdue = text.split("Overdue:")[1].split("\n\n")[0].splitlines()
    assert overdue[1].startswith("- (and ") and overdue[1].endswith(" older ones)")
    assert len(overdue) - 2 == 10  # the 10 most recent, in chronological order


def test_overdue_tasks_are_capped_in_the_briefing_but_not_hidden_from_the_agent(tasks, tools):
    from tasks.format import format_agenda

    for day in range(1, 15):
        tasks.add(f"Expired {day}", date(2026, 11, day))
    briefing = format_agenda(tasks.agenda(), max_overdue=3)
    assert "(and 11 older ones)" in briefing and "Expired 14" in briefing and "Expired 1 " not in briefing
    agent = tools["list_tasks"].invoke({"scope": "today"})
    assert "older ones" not in agent and "Expired 1 " in agent + " "  # the agent sees all of them (up to 50)


# ---- prompts: reference other files ---------------------------------------------------------------


def test_prompts_ask_to_reference_other_files_whenever_possible():
    cur = curator_prompt()
    assert "WHENEVER POSSIBLE, reference other memory documents" in cur and "in both directions" in cur
    assert "Never invent links" in cur
    resp = chat_agent_prompt()
    assert "link_task" in resp and "`ref`" in resp and "WHENEVER POSSIBLE, reference in the task" in resp
    assert "paginated" in resp and "`month`" in resp
    assert "and `ref` with\n  the path of the trip document" in resp
