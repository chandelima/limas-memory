from datetime import date, time

import pytest

from storage.store import MemoryStore
from tasks import model
from tasks.format import describe, format_agenda
from tasks.model import Recurrence, TaskError
from tasks.store import TaskStore
from tasks.tools import build_task_tools

TODAY = date(2026, 11, 18)  # Wednesday


@pytest.fixture
def tasks(tmp_path):
    return TaskStore(MemoryStore(tmp_path / "memory"), today=lambda: TODAY)


@pytest.fixture
def tools(tasks):
    return {t.name: t for t in build_task_tools(tasks)}


def call(tools, name, **kwargs):
    return tools[name].invoke(kwargs)


# ---- tags: model -----------------------------------------------------------------------


def test_normalize_and_parse_tags():
    assert model.normalize_tag("#Health") == "health" and model.normalize_tag("new home") == "new-home"
    assert model.normalize_tag("#Café") == "café"  # non-ASCII letters are fine
    assert model.parse_tags("health, home #home  work") == ("health", "home", "work")
    assert model.parse_tags(["A", "b"]) == ("a", "b") and model.parse_tags("") == ()
    for bad in ("1", "#2024", "a!b", "-x"):
        with pytest.raises(TaskError, match="invalid tag"):
            model.normalize_tag(bad)


def test_split_tags_only_takes_the_trailing_ones_and_not_numbers():
    assert model.split_tags("Checkup #health #home") == ("Checkup", ("health", "home"))
    assert model.split_tags("Pay bill #1") == ("Pay bill #1", ())  # "#1" is not a tag
    assert model.split_tags("Talk about #project tomorrow") == ("Talk about #project tomorrow", ())  # only at the end
    assert model.split_tags("no tag") == ("no tag", ())
    assert model.with_tags("Checkup", ("health", "home")) == "Checkup #health #home"


TAGGED = """## Pending

- [ ] 2026-11-18 08:00 — Doctor appointment #health ^a1b2
- [ ] Buy a gift #home #family ^c3d4

## Recurring

- [ ] daily 07:00 — Take medication #health ^e5f6

## Done

- [x] 2026-11-17 — Dentist #health (done on 2026-11-17) ^0a0a
"""


def test_tags_are_parsed_in_every_section_and_roundtrip():
    tl = model.parse(TAGGED)
    assert [t.tags for t in tl.pending] == [("health",), ("home", "family")]
    assert tl.recurring[0].tags == ("health",) and tl.done[0].tags == ("health",)
    assert tl.pending[0].text == "Doctor appointment" and tl.done[0].text == "Dentist"  # without the tags
    again = model.parse(model.render(tl))
    assert [(t.text, t.tags) for t in again.pending] == [(t.text, t.tags) for t in tl.pending]
    assert again.recurring == tl.recurring and again.done == tl.done


# ---- tags: store -----------------------------------------------------------------------


def test_add_merges_inline_and_explicit_tags_and_rejects_text_only_tags(tasks):
    task = tasks.add("Doctor appointment #health", tags="Home, health")
    assert task.text == "Doctor appointment" and task.tags == ("health", "home")
    with pytest.raises(TaskError, match="needs a text"):
        tasks.add("#health")
    with pytest.raises(TaskError, match="invalid tag"):
        tasks.add("x", tags="1abc")
    assert [t.text for t in tasks.load().pending] == ["Doctor appointment"]


def test_tags_follow_the_task_through_complete_and_reopen(tasks):
    task = tasks.add("Appointment", TODAY, tags="health")
    done = tasks.complete(task.id)
    assert done.tags == ("health",) and tasks.load().done[0].tags == ("health",)
    assert tasks.reopen(task.id).tags == ("health",)
    rec = tasks.add("Medication", recurrence=Recurrence.parse("daily"), tags="health")
    assert tasks.complete(rec.id).tags == ("health",)


def test_retag_adds_and_removes_and_tags_counts_usage(tasks):
    a, b = tasks.add("a", tags="home"), tasks.add("b", tags="home, health")
    rec = tasks.add("r", recurrence=Recurrence.parse("daily"), tags="health")
    assert tasks.tags() == {"home": 2, "health": 2}
    assert tasks.retag(a.id, add="work", remove="home").tags == ("work",)
    assert tasks.retag(rec.id, add="health, habit").tags == ("health", "habit")
    assert tasks.tags() == {"health": 2, "home": 1, "habit": 1, "work": 1}
    with pytest.raises(TaskError, match="no pending or recurring task"):
        tasks.retag("zzzz", add="x")


def test_agenda_can_be_filtered_by_tag(tasks):
    tasks.add("appointment", TODAY, time(8, 0), tags="health")
    tasks.add("meeting", TODAY, time(9, 0), tags="work")
    tasks.add("medication", recurrence=Recurrence.parse("daily"), tags="health")
    agenda = tasks.agenda(tag="Health")
    assert sorted(i.task.text for i in agenda.due_today) == ["appointment", "medication"]
    assert [i.task.text for i in tasks.agenda(tag="work").due_today] == ["meeting"]
    with pytest.raises(TaskError):
        tasks.agenda(tag="1")


# ---- formatting ----------------------------------------------------------------------------


def test_format_agenda_sections_and_empty_case(tasks):
    assert format_agenda(tasks.agenda()) == ""
    tasks.add("overdue", date(2026, 11, 15), tags="home")
    tasks.add("appointment", TODAY, time(8, 0), tags="health")
    tasks.add("medication", recurrence=Recurrence.parse("daily"), due_time=time(7, 0))
    tasks.add("tomorrow", date(2026, 11, 19), time(9, 30))
    tasks.add("no date")
    text = format_agenda(tasks.agenda())
    assert text.split("\n\n") == [
        "Overdue:\n- overdue #home (was 15/11 (Sun))",
        "Today (Wednesday, 18/11):\n- 07:00 medication (daily)\n- 08:00 appointment #health",
        "Upcoming days:\n- 19/11 (Thu) 09:30 tomorrow",
        "No date:\n- no date",
    ]
    assert "[" not in text  # the morning briefing has no IDs
    with_ids = format_agenda(tasks.agenda(), show_ids=True)
    assert f"[{tasks.load().pending[0].id}]" in with_ids


def test_format_agenda_says_nothing_today_when_only_other_days_have_items_and_caps_undated(tasks):
    tasks.add("tomorrow", date(2026, 11, 19))
    for i in range(7):
        tasks.add(f"loose {i}")
    text = format_agenda(tasks.agenda(), max_undated=5)
    assert "Today (Wednesday, 18/11):\n- nothing scheduled" in text and "- (and 2 more)" in text


def test_describe_task_one_liner(tasks):
    assert describe(tasks.add("a", TODAY, time(8, 0), tags="x")) == "a (18/11 (Wed) at 08:00) #x"
    assert describe(tasks.add("b", recurrence=Recurrence.parse("weekly mon"), due_time=time(19, 0))) == "b (weekly mon at 19:00)"
    assert describe(tasks.add("c")) == "c"


# ---- tools ----------------------------------------------------------------------------------


def test_add_task_tool_variants_and_confirmation_text(tools):
    one = call(tools, "add_task", text="Doctor appointment", due_date="2026-11-18", due_time="08:00", tags="health")
    assert one.startswith("OK: created [") and "Doctor appointment (18/11 (Wed) at 08:00) #health" in one
    rec = call(tools, "add_task", text="Workout", recurrence="weekly mon,wed", due_time="19:00")
    assert "(weekly mon,wed at 19:00)" in rec
    assert call(tools, "add_task", text="Buy a gift").endswith("Buy a gift")


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        (dict(text="x", due_date="18/11/2026"), "invalid date"),
        (dict(text="x", due_date="2026-11-18", due_time="8h"), "invalid time"),
        (dict(text="x", recurrence="fortnightly"), "invalid recurrence"),
        (dict(text="x", tags="1a"), "invalid tag"),
        (dict(text="x", due_time="08:00"), "a time needs a date"),
        (dict(text="x", due_date="2026-11-18", recurrence="daily"), "has no date"),
        (dict(text="   "), "needs a text"),
    ],
)
def test_add_task_tool_validation_errors_are_returned_as_text(tools, kwargs, expected):
    out = call(tools, "add_task", **kwargs)
    assert out.startswith("ERROR") and expected in out


def test_list_tasks_today_shows_ids_header_and_tag_summary(tools):
    call(tools, "add_task", text="Appointment", due_date="2026-11-18", due_time="08:00", tags="health")
    call(tools, "add_task", text="Medication", recurrence="daily", due_time="07:00", tags="health")
    out = call(tools, "list_tasks")
    assert out.startswith("Today is 18/11 (Wed), 2026-11-18.")
    assert "Today (Wednesday, 18/11):" in out and "07:00 Medication #health (daily)" in out
    assert "Tags in use: #health (2)" in out
    ids = [w.strip("[]") for w in out.split() if w.startswith("[") and w.endswith("]")]
    assert len(ids) == 2 and all(len(i) == 4 for i in ids)


def test_list_tasks_scopes_and_filters(tools, tasks):
    call(tools, "add_task", text="Far", due_date="2026-11-24", tags="home")
    call(tools, "add_task", text="No date")
    call(tools, "add_task", text="Workout", recurrence="weekly mon", due_time="19:00", tags="home")
    done = tasks.add("Finished", TODAY, tags="home")
    tasks.complete(done.id)
    assert "No tasks for the period." not in call(tools, "list_tasks", scope="week")
    assert "Far" in call(tools, "list_tasks", scope="week") and "Far" not in call(tools, "list_tasks", scope="today")
    pend = call(tools, "list_tasks", scope="pending")
    assert pend.index("Far") < pend.index("No date")  # dated before undated
    assert "weekly mon 19:00 Workout #home" in call(tools, "list_tasks", scope="recurring")
    assert "done on 18/11 (Wed): Finished #home" in call(tools, "list_tasks", scope="done")
    assert "No date" not in call(tools, "list_tasks", scope="pending", tag="home")
    assert "No tasks in this list." in call(tools, "list_tasks", scope="recurring", tag="work")


def test_list_tasks_empty_invalid_scope_and_invalid_tag(tools):
    assert "No tasks for the period." in call(tools, "list_tasks")
    assert call(tools, "list_tasks", scope="all").startswith("ERROR: invalid scope 'all'")
    assert call(tools, "list_tasks", tag="1x").startswith("ERROR")


def test_complete_reschedule_reopen_remove_and_tag_tools(tools, tasks):
    task = tasks.add("Appointment", TODAY, time(8, 0))
    assert call(tools, "reschedule_task", task_id=task.id, due_date="2026-11-25", due_time="10:30") \
        .endswith("Appointment (25/11 (Wed) at 10:30)")
    assert "tags updated" in call(tools, "tag_task", task_id=task.id, add_tags="health")
    assert "#health" in call(tools, "list_tasks", scope="pending")
    out = call(tools, "complete_task", task_id=task.id)
    assert out == f"OK: completed [{task.id}] Appointment (on 18/11 (Wed))"
    assert call(tools, "reopen_task", task_id=task.id).startswith(f"OK: reopened [{task.id}]")
    assert call(tools, "remove_task", task_id=task.id).startswith("OK: removed")
    assert "No tasks" in call(tools, "list_tasks", scope="pending")


def test_recurring_completion_through_the_tool(tools, tasks):
    rec = tasks.add("Medication", recurrence=Recurrence.parse("daily"))
    assert call(tools, "complete_task", task_id=rec.id).startswith("OK: completed")
    assert "(daily, done)" in call(tools, "list_tasks")
    assert call(tools, "reopen_task", task_id=rec.id).startswith("OK: reopened")
    assert "daily, done" not in call(tools, "list_tasks")


@pytest.mark.parametrize(
    "name,kwargs,expected",
    [
        ("complete_task", dict(task_id="zzzz"), "no task with ID"),
        ("complete_task", dict(task_id="zzzz", on_date="tomorrow"), "invalid date"),
        ("reschedule_task", dict(task_id="zzzz", due_date="2026-11-20"), "no pending task"),
        ("reschedule_task", dict(task_id="zzzz", due_date="20/11"), "invalid date"),
        ("reopen_task", dict(task_id="zzzz"), "no completed task"),
        ("remove_task", dict(task_id="zzzz"), "no pending or recurring task"),
        ("tag_task", dict(task_id="zzzz", add_tags="x"), "no pending or recurring task"),
        ("tag_task", dict(task_id="zzzz", add_tags="1x"), "invalid tag"),
    ],
)
def test_other_tools_return_errors_as_text(tools, name, kwargs, expected):
    out = call(tools, name, **kwargs)
    assert out.startswith("ERROR") and expected in out


def test_reschedule_a_recurring_task_explains_what_to_do(tools, tasks):
    rec = tasks.add("Medication", recurrence=Recurrence.parse("daily"))
    assert "remove it and create another" in call(tools, "reschedule_task", task_id=rec.id, due_date="2026-11-20")


def test_the_eight_tools_have_descriptions_for_the_model(tools):
    assert set(tools) == {"list_tasks", "add_task", "complete_task", "reschedule_task", "reopen_task",
                          "remove_task", "tag_task", "link_task"}
    assert all(t.description for t in tools.values())


def test_split_tags_when_the_whole_text_is_only_tags():
    assert model.split_tags("#health") == ("", ("health",))
    assert model.split_tags("#health #home") == ("", ("health", "home"))
    assert model.split_tags("#1") == ("#1", ())


def test_prompts_teach_the_task_conventions_to_both_agents():
    from chat_agent.agent import load_system_prompt as chat_agent_prompt
    from curator.agent import load_system_prompt as curator_prompt

    resp = chat_agent_prompt()
    assert "TASKS:" in resp
    for name in ("list_tasks", "add_task", "complete_task", "reschedule_task", "reopen_task", "remove_task", "tag_task"):
        assert name in resp
    assert "YYYY-MM-DD" in resp and "[now: ...]" in resp
    # travel events: not tasks by default, but an explicit user request prevails
    assert "do not copy them to the\n  list on your own" in resp
    assert "an\n  explicit user request always prevails" in resp and "the tag `travel`" in resp
    assert "are NOT\n  tasks" not in resp  # the absolute rule that made the agent refuse the request
    assert "The only thing you change is the task list" in resp
    cur = curator_prompt()
    assert "TASKS AND COMMITMENTS:" in cur and "`todos/`" in cur and "do not copy travel events" in cur
