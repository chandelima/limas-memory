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

TODAY = date(2026, 11, 18)  # quarta-feira


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory")


@pytest.fixture
def tasks(store):
    return TaskStore(store, today=lambda: TODAY)


# ---- datas, horas e recorrência -------------------------------------------------------


def test_parse_date_and_time_validate_input():
    assert model.parse_date(" 2026-11-18 ") == TODAY
    assert model.parse_time("8:05") == time(8, 5) and model.parse_time("23:59") == time(23, 59)
    for bad in ("18/11/2026", "2026-13-01", "amanhã", ""):
        with pytest.raises(TaskError, match="data inválida"):
            model.parse_date(bad)
    for bad in ("24:00", "8h", "08:60", "meio-dia"):
        with pytest.raises(TaskError, match="hora inválida"):
            model.parse_time(bad)


@pytest.mark.parametrize(
    "text,rendered",
    [("diária", "diária"), ("Diaria", "diária"), ("todo dia", "diária"), ("semanal seg,qua", "semanal seg,qua"),
     ("semanal quarta, segunda", "semanal seg,qua"), ("dias úteis", "semanal seg,ter,qua,qui,sex"),
     ("mensal 5", "mensal 5"), ("mensal dia 31", "mensal 31")],
)
def test_recurrence_parse_and_render(text, rendered):
    assert Recurrence.parse(text).render() == rendered


@pytest.mark.parametrize("bad", ["", "quinzenal", "semanal", "semanal xyz", "semanal seg,xyz", "mensal 40", "mensal"])
def test_recurrence_rejects_invalid_values(bad):
    with pytest.raises(TaskError, match="recorrência inválida"):
        Recurrence.parse(bad)


def test_recurrence_occurs_on():
    assert Recurrence.parse("diária").occurs_on(TODAY)
    weekly = Recurrence.parse("semanal qua,sex")
    assert weekly.occurs_on(TODAY) and not weekly.occurs_on(date(2026, 11, 19))
    monthly = Recurrence.parse("mensal 31")
    assert monthly.occurs_on(date(2026, 12, 31)) and monthly.occurs_on(date(2026, 2, 28))  # dia 31 vale no último dia
    assert not monthly.occurs_on(date(2026, 2, 27))


def test_clean_text_makes_one_line_without_carets_and_rejects_empty_or_huge():
    assert model.clean_text("  ligar   para\no banco ^ab12 ") == "ligar para o banco ab12"
    with pytest.raises(TaskError):
        model.clean_text(" \n ")
    with pytest.raises(TaskError, match="longo demais"):
        model.clean_text("x" * 201)


# ---- Markdown <-> modelo ---------------------------------------------------------------


SAMPLE = """---
description: x
---
# Tarefas

## Pendentes

- [ ] 2026-11-18 08:00 — Consulta médica ^a1b2
- [ ] 2026-11-20 — Ligar para o banco — urgente ^c3d4
- [ ] Comprar presente ^e5f6

## Recorrentes

- [ ] diária 08:00 — Tomar remédio ^0a0a
- [ ] semanal seg,qua 19:00 — Treino ^0b0b
- [ ] mensal 5 — Pagar aluguel ^0c0c

## Concluídas

- [x] 2026-11-17 09:00 — Dentista (feita em 2026-11-17) ^d1d1
- [x] sem data — Algo sem prazo (feita em 2026-11-16) ^d2d2
"""


def test_parse_reads_every_kind_of_line():
    tl = model.parse(SAMPLE)
    assert [(t.id, t.text, t.due_date, t.due_time) for t in tl.pending] == [
        ("a1b2", "Consulta médica", date(2026, 11, 18), time(8, 0)),
        ("c3d4", "Ligar para o banco — urgente", date(2026, 11, 20), None),
        ("e5f6", "Comprar presente", None, None),
    ]
    assert [(t.id, t.recurrence.render(), t.due_time) for t in tl.recurring] == [
        ("0a0a", "diária", time(8, 0)), ("0b0b", "semanal seg,qua", time(19, 0)), ("0c0c", "mensal 5", None)]
    assert [(c.id, c.text, c.due_date, c.done_on) for c in tl.done] == [
        ("d1d1", "Dentista", date(2026, 11, 17), date(2026, 11, 17)),
        ("d2d2", "Algo sem prazo", None, date(2026, 11, 16))]
    assert tl.extras == {}


def test_render_then_parse_is_lossless():
    tl = model.parse(SAMPLE)
    again = model.parse(model.render(tl))  # o render ordena; o conteúdo é o mesmo
    by_id = lambda items: sorted(items, key=lambda x: x.id)  # noqa: E731
    assert by_id(again.pending) == by_id(tl.pending)
    assert by_id(again.recurring) == by_id(tl.recurring) and by_id(again.done) == by_id(tl.done)


def test_render_sorts_and_has_valid_frontmatter_and_empty_markers():
    tl = model.TaskList(pending=[Task("b", "depois", date(2026, 12, 1)), Task("a", "sem data"),
                                 Task("c", "antes", date(2026, 11, 1), time(9, 0)), Task("d", "antes cedo", date(2026, 11, 1), time(7, 0))])
    text = model.render(tl)
    meta, _ = md.parse_frontmatter(text)
    assert meta["description"].startswith("Lista de tarefas")
    order = [line for line in text.splitlines() if line.startswith("- [ ]")]
    assert [o.split(" — ")[-1].split(" ^")[0] if " — " in o else o for o in order] == [
        "antes cedo", "antes", "depois", "- [ ] sem data ^a"]
    assert text.count(model.EMPTY) == 2  # Recorrentes e Concluídas vazias


def test_done_history_is_capped_keeping_the_newest():
    done = [Completion(f"{i:04x}", f"t{i}", None, None, date(2026, 1, 1) + __import__("datetime").timedelta(days=i))
            for i in range(model.MAX_DONE + 20)]
    out = model.parse(model.render(model.TaskList(done=done)))
    assert len(out.done) == model.MAX_DONE and out.done[0].text == f"t{model.MAX_DONE + 19}"


def test_unrecognized_lines_are_preserved_in_their_section():
    text = SAMPLE.replace("- [ ] Comprar presente ^e5f6", "- [ ] Comprar presente ^e5f6\numa nota solta\n- [ ] linha sem id")
    tl = model.parse(text)
    assert tl.extras == {"Pendentes": ["uma nota solta", "- [ ] linha sem id"]}
    again = model.render(tl)
    assert "uma nota solta" in again and "- [ ] linha sem id" in again


def test_invalid_date_or_wrong_checkbox_goes_to_extras_not_lost():
    bad = "## Pendentes\n\n- [ ] 2026-13-45 — data impossível ^aaaa\n\n## Concluídas\n\n- [ ] 2026-11-17 — não marcada (feita em 2026-11-17) ^bbbb\n"
    tl = model.parse(bad)
    assert tl.pending == [] and tl.done == []
    assert len(tl.extras["Pendentes"]) == 1 and len(tl.extras["Concluídas"]) == 1


def test_parse_ignores_text_before_the_first_section_and_unknown_sections():
    tl = model.parse("# Tarefas\n\nintrodução\n\n## Outra coisa\n\n- [ ] x ^aaaa\n\n## Pendentes\n\n- [ ] y ^bbbb\n")
    assert [t.text for t in tl.pending] == ["y"]


# ---- operações -------------------------------------------------------------------------


def test_add_writes_the_file_in_the_protected_folder_and_persists(tasks, store):
    task = tasks.add("Consulta médica", date(2026, 11, 18), time(8, 0))
    assert len(task.id) == 4
    text = store.read_doc(TODO_PATH)
    assert f"- [ ] 2026-11-18 08:00 — Consulta médica ^{task.id}" in text
    assert text.endswith("\n")
    reopened = TaskStore(MemoryStore(store.root))
    assert reopened.load().pending == [task]


def test_add_validations(tasks):
    with pytest.raises(TaskError, match="uma hora precisa de uma data"):
        tasks.add("x", due_time=time(8, 0))
    with pytest.raises(TaskError, match="não tem data"):
        tasks.add("x", due_date=TODAY, recurrence=Recurrence.parse("diária"))
    with pytest.raises(TaskError):
        tasks.add("   ")
    assert tasks.load().pending == []


def test_ids_are_unique_and_lookup_accepts_decorated_ids(tasks):
    ids = {tasks.add(f"t{i}").id for i in range(60)}
    assert len(ids) == 60
    first = tasks.load().pending[0]
    assert tasks.complete(f"[{first.id.upper()}]").id == first.id


def test_complete_moves_a_task_to_done_with_the_date(tasks):
    task = tasks.add("Consulta médica", date(2026, 11, 18), time(8, 0))
    done = tasks.complete(task.id)
    assert (done.done_on, done.due_date, done.due_time) == (TODAY, date(2026, 11, 18), time(8, 0))
    tl = tasks.load()
    assert tl.pending == [] and [c.id for c in tl.done] == [task.id]
    with pytest.raises(TaskError, match="já está concluída"):
        tasks.complete(task.id)
    assert tasks.complete(tasks.add("outra").id, on=date(2026, 11, 20)).done_on == date(2026, 11, 20)


def test_complete_unknown_id_is_an_error(tasks):
    with pytest.raises(TaskError, match="não existe tarefa"):
        tasks.complete("zzzz")


def test_recurring_completion_is_per_day_idempotent_and_checks_the_recurrence(tasks):
    task = tasks.add("Treino", recurrence=Recurrence.parse("semanal seg,qua"), due_time=time(19, 0))
    first = tasks.complete(task.id)  # hoje é quarta
    assert tasks.complete(task.id) == first and len(tasks.load().done) == 1
    assert [t.id for t in tasks.load().recurring] == [task.id]  # a definição continua
    with pytest.raises(TaskError, match="não ocorre em 2026-11-19"):
        tasks.complete(task.id, on=date(2026, 11, 19))
    tasks.complete(task.id, on=date(2026, 11, 23))  # segunda seguinte
    assert len(tasks.load().done) == 2


def test_reopen_one_off_returns_it_to_pending(tasks):
    task = tasks.add("Consulta", date(2026, 11, 18), time(8, 0))
    tasks.complete(task.id)
    back = tasks.reopen(task.id)
    tl = tasks.load()
    assert (back.due_date, back.due_time) == (date(2026, 11, 18), time(8, 0))
    assert [t.id for t in tl.pending] == [task.id] and tl.done == []
    with pytest.raises(TaskError):
        tasks.reopen("zzzz")


def test_reopen_recurring_removes_only_that_days_completion(tasks):
    task = tasks.add("Remédio", recurrence=Recurrence.parse("diária"))
    tasks.complete(task.id, on=TODAY)
    tasks.complete(task.id, on=date(2026, 11, 17))
    tasks.reopen(task.id)
    assert [c.done_on for c in tasks.load().done] == [date(2026, 11, 17)]
    with pytest.raises(TaskError, match="não estava cumprida"):
        tasks.reopen(task.id)


def test_reschedule_one_off_only(tasks):
    task = tasks.add("Ligar", date(2026, 11, 18))
    moved = tasks.reschedule(task.id, date(2026, 11, 25), time(10, 30))
    assert (moved.due_date, moved.due_time) == (date(2026, 11, 25), time(10, 30))
    assert tasks.load().pending[0].due_date == date(2026, 11, 25)
    tasks.reschedule(task.id, date(2026, 11, 26))  # sem hora: a hora anterior é limpa
    assert tasks.load().pending[0].due_time is None
    rec = tasks.add("Remédio", recurrence=Recurrence.parse("diária"))
    with pytest.raises(TaskError, match="recorrente"):
        tasks.reschedule(rec.id, TODAY)
    with pytest.raises(TaskError, match="não existe tarefa pendente"):
        tasks.reschedule("zzzz", TODAY)


def test_remove_pending_and_recurring_but_not_unknown(tasks):
    one, rec = tasks.add("única"), tasks.add("sempre", recurrence=Recurrence.parse("diária"))
    assert tasks.remove(one.id).text == "única" and tasks.remove(rec.id).text == "sempre"
    tl = tasks.load()
    assert tl.pending == [] and tl.recurring == []
    with pytest.raises(TaskError):
        tasks.remove("zzzz")


def test_missing_file_means_empty_list_and_manual_edits_survive_operations(tasks, store):
    assert tasks.load().pending == []
    store.write_doc(TODO_PATH, SAMPLE + "\nnota manual fora de seção\n", internal=True)
    tasks.add("nova")
    assert "nova" in store.read_doc(TODO_PATH) and "Consulta médica" in store.read_doc(TODO_PATH)


# ---- agenda ----------------------------------------------------------------------------


def test_agenda_groups_overdue_today_recurring_upcoming_and_undated(tasks):
    tasks.add("atrasada", date(2026, 11, 15))
    tasks.add("consulta", TODAY, time(8, 0))
    tasks.add("hoje sem hora", TODAY)
    tasks.add("amanhã", date(2026, 11, 19), time(9, 0))
    tasks.add("daqui a 3 dias", date(2026, 11, 21))
    tasks.add("longe demais", date(2026, 11, 30))
    tasks.add("sem data")
    tasks.add("remédio", recurrence=Recurrence.parse("diária"), due_time=time(7, 0))
    tasks.add("treino", recurrence=Recurrence.parse("semanal seg"), due_time=time(19, 0))
    agenda = tasks.agenda()
    assert agenda.today == TODAY and [t.text for t in agenda.overdue] == ["atrasada"]
    assert [i.task.text for i in agenda.due_today] == ["remédio", "consulta", "hoje sem hora"]  # por hora; sem hora no fim
    assert [i.recurring for i in agenda.due_today] == [True, False, False]
    # o remédio diário aparece em todos os dias do horizonte (19, 20 e 21)
    assert list(agenda.upcoming) == [date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 21)]
    assert [i.task.text for i in agenda.upcoming[date(2026, 11, 21)]] == ["remédio", "daqui a 3 dias"]
    assert [i.task.text for i in agenda.upcoming[date(2026, 11, 19)]] == ["remédio", "amanhã"]
    assert "longe demais" not in str(agenda.upcoming) and [t.text for t in agenda.undated] == ["sem data"]
    assert not agenda.is_empty()


def test_agenda_marks_recurring_occurrences_already_done_today_and_is_empty_when_nothing(tasks):
    assert tasks.agenda().is_empty()
    rec = tasks.add("remédio", recurrence=Recurrence.parse("diária"))
    tasks.complete(rec.id)
    items = tasks.agenda().due_today
    assert [(i.task.text, i.done) for i in items] == [("remédio", True)]
    assert tasks.agenda(on=date(2026, 11, 19)).due_today[0].done is False


def test_agenda_horizon_and_reference_date(tasks):
    tasks.add("em 5 dias", date(2026, 11, 23))
    assert tasks.agenda(horizon_days=3).upcoming == {}
    assert list(tasks.agenda(horizon_days=5).upcoming) == [date(2026, 11, 23)]
    assert [t.text for t in tasks.agenda(on=date(2026, 11, 24)).overdue] == ["em 5 dias"]


# ---- proteção: só o código escreve, o Curator não ---------------------------------------


def test_curator_cannot_write_the_task_file_but_can_read_nothing_odd(tmp_path, store, tasks):
    tasks.add("Consulta médica", TODAY, time(8, 0))
    doc = "---\ndescription: x\n---\n# X\ncorpo\n"
    with pytest.raises(StoreError, match="gerenciada pelo bot"):
        store.write_doc(TODO_PATH, doc)
    tools = {t.name: t for t in build_tools(store, MemoryGit(store.root))}
    out = tools["write_memory"].invoke({"path": TODO_PATH, "content": doc})
    assert out.startswith("ERRO") and "gerenciada pelo bot" in out
    assert "Consulta médica" in store.read_doc(TODO_PATH)  # intacto


def test_task_file_is_hidden_from_list_search_and_index(tasks, store):
    tasks.add("Consulta médica", TODAY, time(8, 0))
    store.write_doc("viagens/x.md", "---\ndescription: Viagem\n---\n# Viagem\nconsulta de rotina\n")
    assert [d.path for d in store.list_docs()] == ["viagens/x.md"]
    assert [h.path for h in store.search_docs("Consulta médica")] == []
    assert [h.path for h in store.search_docs("consulta de rotina")] == ["viagens/x.md"]
    idx.update_index(store)
    assert "todos" not in store.read_doc("index.md")
    assert build_read_tools(store)[0].invoke({}) .count("todos") == 0


def test_task_file_goes_into_the_curators_next_commit(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    TaskStore(store, today=lambda: TODAY).add("Consulta médica", TODAY)
    git = MemoryGit(store.root)
    assert git.commit_and_push("docs: x").sha
    assert TODO_PATH in git._git_ok("ls-files")


# ---- arquivo mensal das concluídas ------------------------------------------------------


def done_on(tasks, text, on):
    task = tasks.add(text)
    tasks.complete(task.id, on=on)
    return task


def test_old_completions_move_to_a_monthly_archive_and_leave_the_main_file(tasks, store):
    old = done_on(tasks, "antiga de outubro", date(2026, 10, 5))  # > 30 dias antes de 18/11
    recent = done_on(tasks, "recente", date(2026, 11, 10))
    main = tasks.load()
    assert [c.text for c in main.done] == ["recente"]
    archive = model.parse(store.read_doc("todos/arquivo/2026-10.md"))
    assert [(c.id, c.text, c.done_on) for c in archive.done] == [(old.id, "antiga de outubro", date(2026, 10, 5))]
    meta, body = md.parse_frontmatter(store.read_doc("todos/arquivo/2026-10.md"))
    assert meta["description"] == "Arquivo de tarefas concluídas em 2026-10."
    assert "# Tarefas concluídas em 2026-10" in body and recent.id not in store.read_doc("todos/arquivo/2026-10.md")


def test_archive_is_grouped_by_month_deduplicated_and_appended(tasks, store):
    done_on(tasks, "set 1", date(2026, 9, 1))
    done_on(tasks, "set 2", date(2026, 9, 20))
    done_on(tasks, "out 1", date(2026, 10, 2))
    assert [c.text for c in model.parse(store.read_doc("todos/arquivo/2026-09.md")).done] == ["set 2", "set 1"]
    assert [c.text for c in model.parse(store.read_doc("todos/arquivo/2026-10.md")).done] == ["out 1"]
    done_on(tasks, "set 3", date(2026, 9, 30))  # o mês já existente recebe a nova
    assert [c.text for c in model.parse(store.read_doc("todos/arquivo/2026-09.md")).done] == ["set 3", "set 2", "set 1"]
    tasks._archive(model.parse(store.read_doc("todos/arquivo/2026-09.md")).done)  # reenviar não duplica
    assert len(model.parse(store.read_doc("todos/arquivo/2026-09.md")).done) == 3


def test_overflow_beyond_the_cap_is_archived_instead_of_dropped(tasks, store, monkeypatch):
    monkeypatch.setattr(model, "MAX_DONE", 3)
    for i in range(5):
        done_on(tasks, f"t{i}", date(2026, 11, 10 + i))
    assert [c.text for c in tasks.load().done] == ["t4", "t3", "t2"]
    assert [c.text for c in model.parse(store.read_doc("todos/arquivo/2026-11.md")).done] == ["t1", "t0"]


def test_archive_is_protected_and_hidden_like_the_main_file(tasks, store):
    done_on(tasks, "antiga", date(2026, 10, 5))
    with pytest.raises(StoreError, match="gerenciada pelo bot"):
        store.write_doc("todos/arquivo/2026-10.md", "---\ndescription: x\n---\n# X\ncorpo\n")
    assert store.list_docs() == [] and store.search_docs("antiga") == []
    assert "todos/arquivo/2026-10.md" in store.list_paths()  # existe e pode ser lido
    assert "antiga" in store.read_doc("todos/arquivo/2026-10.md")


def test_a_recent_completion_never_touches_the_archive(tasks, store):
    done_on(tasks, "hoje", TODAY)
    assert not (store.root / "todos" / "arquivo").exists()
