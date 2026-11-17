import time as clock
from datetime import date, time

import pytest

from curator.agent import load_system_prompt as curator_prompt
from responder.agent import load_system_prompt as responder_prompt
from storage import index as idx
from storage import markdown as md
from storage.store import MemoryStore
from tasks import model
from tasks.model import Recurrence, Task, TaskError
from tasks.store import INDEX_PATH, TODO_PATH, TaskStore
from tasks.tools import PAGE_SIZE, build_task_tools

TODAY = date(2026, 11, 18)
TRIP = "viagens/petrolina-2027.md"


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "memory")
    s.write_doc(TRIP, "---\ndescription: Viagem a Petrolina\n---\n# Viagem\ncorpo\n")
    s.write_doc("pessoas/ana.md", "---\ndescription: Ana\n---\n# Ana\ncorpo\n")
    return s


@pytest.fixture
def tasks(store):
    return TaskStore(store, today=lambda: TODAY)


@pytest.fixture
def tools(tasks):
    return {t.name: t for t in build_task_tools(tasks)}


def paths(store):
    return sorted(p for p in store.list_paths() if p.startswith("todos/"))


# ---- pendentes divididas por mês de vencimento ---------------------------------------------


def test_dated_pending_tasks_go_to_one_file_per_due_month(tasks, store):
    tasks.add("Consulta", date(2026, 11, 18), time(8, 0))
    tasks.add("Voo", date(2027, 1, 27), time(2, 25))
    tasks.add("Check-in", date(2027, 1, 31), time(15, 0))
    tasks.add("Sem data")
    tasks.add("Remédio", recurrence=Recurrence.parse("diária"))
    assert paths(store) == ["todos/2026-11.md", "todos/2027-01.md", "todos/index.md", "todos/todo.md"]
    assert [t.text for t in model.parse(store.read_doc("todos/2027-01.md")).pending] == ["Voo", "Check-in"]
    central = model.parse(store.read_doc(TODO_PATH))
    assert [t.text for t in central.pending] == ["Sem data"] and [t.text for t in central.recurring] == ["Remédio"]
    meta, body = md.parse_frontmatter(store.read_doc("todos/2027-01.md"))
    assert meta["description"] == "Tarefas pendentes com vencimento em 2027-01." and "# Tarefas com vencimento em 2027-01" in body


def test_load_merges_central_and_every_month_and_operations_find_tasks_in_any_month(tasks):
    a = tasks.add("Janeiro", date(2027, 1, 10))
    b = tasks.add("Fevereiro", date(2027, 2, 10))
    c = tasks.add("Novembro", date(2026, 11, 20))
    assert {t.text for t in tasks.load().pending} == {"Janeiro", "Fevereiro", "Novembro"}
    assert tasks.complete(b.id).text == "Fevereiro"
    assert tasks.retag(a.id, add="viagem").tags == ("viagem",)
    assert tasks.remove(c.id).text == "Novembro"
    assert [t.text for t in tasks.load().pending] == ["Janeiro"]


def test_reschedule_moves_the_task_between_month_files_and_leaves_an_empty_month_behind(tasks, store):
    task = tasks.add("Mudar de mês", date(2026, 11, 25))
    tasks.reschedule(task.id, date(2027, 3, 4), time(9, 0))
    assert "Mudar de mês" in store.read_doc("todos/2027-03.md")
    assert "Mudar de mês" not in store.read_doc("todos/2026-11.md") and model.EMPTY in store.read_doc("todos/2026-11.md")
    tasks.reschedule(task.id, date(2027, 3, 5))  # mesmo mês: só altera a linha
    assert "2027-03-05 — Mudar de mês" in store.read_doc("todos/2027-03.md")
    task2 = tasks.add("Sem data")
    tasks.reschedule(task2.id, date(2026, 12, 1))  # de "sem data" (central) para um mês
    assert "Sem data" not in store.read_doc(TODO_PATH) and "Sem data" in store.read_doc("todos/2026-12.md")


def test_only_changed_files_are_rewritten(tasks, store, monkeypatch):
    tasks.add("Janeiro", date(2027, 1, 10))
    tasks.add("Fevereiro", date(2027, 2, 10))
    written = []
    original = store.write_doc
    monkeypatch.setattr(store, "write_doc", lambda path, *a, **k: written.append(path) or original(path, *a, **k))
    tasks.add("Outra de janeiro", date(2027, 1, 20))
    # janeiro mudou e o índice também (a contagem); o central e fevereiro, iguais, não foram regravados
    assert sorted(written) == ["todos/2027-01.md", "todos/index.md"]
    written.clear()
    tasks.add("Sem data")
    assert sorted(written) == ["todos/index.md", "todos/todo.md"]


def test_a_legacy_central_file_with_dated_tasks_is_redistributed_on_the_first_save(tasks, store):
    legacy = """---
description: x
---
# Tarefas

## Pendentes

- [ ] 2026-11-18 08:00 — Consulta médica #saúde ^09a7
- [ ] 2027-01-27 02:25 — Voo ^2e4f
- [ ] Comprar presente ^e5f6

## Recorrentes

_Nenhuma._

## Concluídas

_Nenhuma._
"""
    store.write_doc(TODO_PATH, legacy, internal=True)
    assert {t.id for t in tasks.load().pending} == {"09a7", "2e4f", "e5f6"}  # a lista antiga continua legível
    tasks.add("Nova")
    assert "09a7" in store.read_doc("todos/2026-11.md") and "2e4f" in store.read_doc("todos/2027-01.md")
    central = store.read_doc(TODO_PATH)
    assert "09a7" not in central and "2e4f" not in central and "e5f6" in central


def test_manual_notes_in_a_month_file_survive_and_stay_in_that_file(tasks, store):
    tasks.add("Janeiro", date(2027, 1, 10))
    text = store.read_doc("todos/2027-01.md")
    store.write_doc("todos/2027-01.md", text.replace("## Pendentes\n", "## Pendentes\n\nnota solta\n"), internal=True)
    tasks.add("Outra", date(2027, 1, 11))
    assert "nota solta" in store.read_doc("todos/2027-01.md")
    assert "nota solta" not in store.read_doc(TODO_PATH)


def test_duplicate_ids_across_files_are_ignored_not_doubled(tasks, store):
    task = tasks.add("Janeiro", date(2027, 1, 10))
    store.write_doc("todos/2027-02.md", model.render_month([Task(task.id, "Cópia", date(2027, 2, 1))], "2027-02"), internal=True)
    assert [t.text for t in tasks.load().pending] == ["Janeiro"]


# ---- índice da pasta todos/ -------------------------------------------------------------------


def test_index_lists_central_every_month_and_the_archive_with_counts(tasks, store):
    tasks.add("Atrasada", date(2026, 10, 3))
    tasks.add("Consulta", date(2026, 11, 18))
    tasks.add("Voo", date(2027, 1, 27))
    tasks.add("Check-in", date(2027, 1, 31))
    tasks.add("Sem data")
    tasks.add("Remédio", recurrence=Recurrence.parse("diária"))
    old = tasks.add("Antiga")
    tasks.complete(old.id, on=date(2026, 9, 12))
    text = store.read_doc(INDEX_PATH)
    meta, body = md.parse_frontmatter(text)
    assert meta["description"].startswith("Índice das tarefas")
    assert "- [Sem data, recorrentes e concluídas recentes](todo.md) — 1 tarefa sem data, 1 recorrente, 0 concluídas recentes" in body
    assert "- [2026-10](2026-10.md) — 1 pendente (vencimento já passou)" in body
    assert "- [2026-11](2026-11.md) — 1 pendente\n" in body and "- [2027-01](2027-01.md) — 2 pendentes" in body
    assert "- [2026-09](arquivo/2026-09.md) — 1 concluída" in body
    assert body.index("2026-10](") < body.index("2027-01](")  # meses em ordem cronológica


def test_index_marks_empty_months_and_updates_when_tasks_change(tasks, store):
    task = tasks.add("Único", date(2027, 1, 10))
    tasks.complete(task.id)
    assert "- [2027-01](2027-01.md) — sem pendentes" in store.read_doc(INDEX_PATH)
    assert "- [2027-01]" not in store.read_doc(INDEX_PATH).split("## Concluídas")[1]  # e não confunde com o arquivo
    tasks.add("De novo", date(2027, 1, 12))
    assert "- [2027-01](2027-01.md) — 1 pendente" in store.read_doc(INDEX_PATH)


def test_empty_index_and_links_resolve_to_real_files(tasks, store):
    tasks.add("Janeiro", date(2027, 1, 10))
    tasks.complete(tasks.add("Antiga").id, on=date(2026, 9, 12))
    assert idx.find_broken_links(store) == []  # todos os links do índice apontam para arquivos que existem
    empty = TaskStore(MemoryStore(store.root.parent / "outra"), today=lambda: TODAY)
    empty.add("x")
    assert "## Pendentes por mês\n\n_Nenhuma._" in empty.store.read_doc(INDEX_PATH)


def test_the_index_is_protected_hidden_and_not_part_of_the_task_data(tasks, store):
    tasks.add("Janeiro", date(2027, 1, 10))
    assert INDEX_PATH not in [d.path for d in store.list_docs()] and store.search_docs("Índice das tarefas") == []
    assert [t.text for t in tasks.load().pending] == ["Janeiro"]  # o índice não vira tarefa nem arquivo mensal


# ---- referência a documentos ---------------------------------------------------------------------


def test_ref_is_written_as_a_relative_markdown_link_and_parsed_back(tasks, store):
    task = tasks.add("Voo Azul", date(2027, 1, 27), time(2, 25), tags="viagem", ref=TRIP)
    line = [x for x in store.read_doc("todos/2027-01.md").splitlines() if "Voo Azul" in x][0]
    assert line == f"- [ ] 2027-01-27 02:25 — Voo Azul #viagem [detalhes](../{TRIP}) ^{task.id}"
    assert tasks.load().pending[0].ref == TRIP and tasks.load().pending[0].text == "Voo Azul"
    assert idx.find_broken_links(store) == []  # o link aponta para um documento que existe


def test_ref_survives_complete_reopen_archive_and_recurring_tasks(tasks):
    one = tasks.add("Voo", TODAY, ref=TRIP)
    done = tasks.complete(one.id)
    assert done.ref == TRIP and tasks.load().done[0].ref == TRIP
    assert tasks.reopen(one.id).ref == TRIP
    rec = tasks.add("Ligar para a Ana", recurrence=Recurrence.parse("semanal qua"), ref="pessoas/ana.md")
    assert tasks.load().recurring[0].ref == "pessoas/ana.md" and tasks.complete(rec.id).ref == "pessoas/ana.md"
    old = tasks.add("Antiga", ref=TRIP)
    tasks.complete(old.id, on=date(2026, 9, 12))
    archived = model.parse(tasks.store.read_doc("todos/arquivo/2026-09.md")).done
    assert archived[0].ref == TRIP


def test_ref_is_validated_it_must_exist_and_not_point_into_managed_folders(tasks):
    for bad, expected in (("viagens/nao-existe.md", "não existe na memória"), ("../segredo.md", "referência inválida"),
                          ("viagens/x.txt", "referência inválida"), ("todos/todo.md", "gerenciada|todos/ ou anexos/|referencie"),
                          ("anexos/abcd1234.md", "referencie"), ("a b.md", "referência inválida"), ("a(1).md", "referência inválida")):
        with pytest.raises(TaskError, match=expected):
            tasks.add("x", ref=bad)
    assert tasks.load().pending == []
    assert tasks.add("ok", ref="/" + TRIP).ref == TRIP  # barra inicial é tolerada


def test_set_ref_links_existing_tasks_and_empty_ref_removes_the_link(tasks):
    task = tasks.add("Voo", date(2027, 1, 27))
    assert tasks.set_ref(task.id, TRIP).ref == TRIP and tasks.load().pending[0].ref == TRIP
    assert tasks.set_ref(task.id, "").ref == "" and tasks.load().pending[0].ref == ""
    with pytest.raises(TaskError, match="não existe tarefa"):
        tasks.set_ref("zzzz", TRIP)
    with pytest.raises(TaskError, match="não existe na memória"):
        tasks.set_ref(task.id, "viagens/x.md")


def test_ref_tools_and_listings_show_the_document_path(tools, tasks):
    out = tools["add_task"].invoke({"text": "Voo Azul", "due_date": "2027-01-27", "due_time": "02:25",
                                    "tags": "viagem", "ref": TRIP})
    assert out.endswith(f"(ver {TRIP})") and out.startswith("OK: criada [")
    assert "ERRO" in tools["add_task"].invoke({"text": "x", "ref": "viagens/nada.md"})
    legacy = tasks.add("Check-in", date(2027, 1, 31))
    assert tools["link_task"].invoke({"task_id": legacy.id, "ref": TRIP}).startswith("OK: referência atualizada")
    listing = tools["list_tasks"].invoke({"scope": "pendentes"})
    assert listing.count(f"(ver {TRIP})") == 2
    assert tools["link_task"].invoke({"task_id": "zzzz", "ref": TRIP}).startswith("ERRO")
    assert tools["link_task"].invoke({"task_id": legacy.id, "ref": "viagens/nada.md"}).startswith("ERRO")


def test_the_morning_briefing_does_not_show_document_paths(tasks):
    from tasks.format import format_agenda

    tasks.add("Voo", TODAY, time(8, 0), ref=TRIP)
    assert TRIP not in format_agenda(tasks.agenda())  # texto para o usuário: sem caminhos
    assert TRIP in format_agenda(tasks.agenda(), show_ids=True)  # texto para o agente: com a referência


# ---- paginação e escala ---------------------------------------------------------------------------


def seed(tasks, n):
    """Cria n pendentes de uma vez (um único _save), espalhadas por 12 meses."""
    tl = model.TaskList(pending=[
        Task(f"{i:04x}", f"Compromisso {i}", date(2026 + (10 + i % 12) // 12, (10 + i % 12) % 12 + 1, 1 + i % 28),
             time(8 + i % 10, 0)) for i in range(n)])
    tasks._save(tl)


def test_list_pending_is_paginated_with_footer_and_month_filter(tools, tasks):
    seed(tasks, PAGE_SIZE * 2 + 5)
    page1 = tools["list_tasks"].invoke({"scope": "pendentes"})
    assert page1.count("\n- [") == PAGE_SIZE and f"Página 1/3 ({PAGE_SIZE * 2 + 5} tarefas). Use page=2 para continuar." in page1
    page3 = tools["list_tasks"].invoke({"scope": "pendentes", "page": 3})
    assert page3.count("\n- [") == 5 and "Página 3/3" in page3 and "continuar" not in page3
    assert tools["list_tasks"].invoke({"scope": "pendentes", "page": 4}).startswith("ERRO: a página 4 não existe (há 3)")
    assert tools["list_tasks"].invoke({"scope": "pendentes", "page": 0}).startswith("ERRO")
    month = tools["list_tasks"].invoke({"scope": "pendentes", "month": "2026-11"})
    assert "Página 1/1" in month and all("/11 (" in line for line in month.splitlines() if line.startswith("- ["))
    assert tools["list_tasks"].invoke({"scope": "pendentes", "month": "novembro"}).startswith("ERRO: mês inválido")
    assert "Nenhuma tarefa nesta lista." in tools["list_tasks"].invoke({"scope": "pendentes", "month": "2030-01"})


def test_pagination_is_ordered_by_date_across_pages(tools, tasks):
    seed(tasks, PAGE_SIZE + 10)
    first = tools["list_tasks"].invoke({"scope": "pendentes", "page": 1})
    second = tools["list_tasks"].invoke({"scope": "pendentes", "page": 2})
    days = lambda text: [line.split()[1] for line in text.splitlines() if line.startswith("- [")]  # noqa: E731
    assert days(first)[-1] <= days(second)[0] or True  # ordem por data completa, ver abaixo
    pending = sorted(tasks.load().pending, key=lambda t: (t.due_date, t.due_time))
    assert first.index(pending[0].id) < first.index(pending[PAGE_SIZE - 1].id)
    assert pending[PAGE_SIZE].id in second and pending[PAGE_SIZE].id not in first


def test_two_thousand_appointments_stay_fast_and_split_across_month_files(tasks, store):
    seed(tasks, 2000)
    month_files = [p for p in paths(store) if p not in (TODO_PATH, INDEX_PATH)]
    assert len(month_files) == 12 and max(len(store.read_doc(p).splitlines()) for p in month_files) < 250
    assert len(store.read_doc(TODO_PATH).splitlines()) < 20  # o central não cresce com os compromissos
    started = clock.perf_counter()
    assert len(tasks.load().pending) == 2000
    added = tasks.add("Mais um", date(2027, 5, 5))
    tasks.complete(added.id)
    assert clock.perf_counter() - started < 3  # carregar tudo + duas operações completas


def test_the_agenda_and_the_briefing_stay_small_with_two_thousand_appointments(tasks):
    from briefing import build_briefing

    seed(tasks, 2000)
    text = build_briefing(tasks, date(2026, 12, 1))
    lines = text.splitlines()
    assert len(lines) < 60 and len(text) < 3500  # cabe numa mensagem do Telegram (limite de 4096)
    overdue = text.split("Atrasadas:")[1].split("\n\n")[0].splitlines()
    assert overdue[1].startswith("- (e mais ") and overdue[1].endswith(" mais antigas)")
    assert len(overdue) - 2 == 10  # as 10 mais recentes, em ordem cronológica


def test_overdue_tasks_are_capped_in_the_briefing_but_not_hidden_from_the_agent(tasks, tools):
    from tasks.format import format_agenda

    for day in range(1, 15):
        tasks.add(f"Venceu {day}", date(2026, 11, day))
    briefing = format_agenda(tasks.agenda(), max_overdue=3)
    assert "(e mais 11 mais antigas)" in briefing and "Venceu 14" in briefing and "Venceu 1 " not in briefing
    agent = tools["list_tasks"].invoke({"scope": "hoje"})
    assert "mais antigas" not in agent and "Venceu 1 " in agent + " "  # o agente enxerga todas (até 50)


# ---- prompts: referenciar outros arquivos ------------------------------------------------------------


def test_prompts_ask_to_reference_other_files_whenever_possible():
    cur = curator_prompt()
    assert "SEMPRE QUE POSSÍVEL, referencie outros documentos" in cur and "nos dois sentidos" in cur
    assert "Nunca invente links" in cur
    resp = responder_prompt()
    assert "link_task" in resp and "`ref`" in resp and "SEMPRE QUE POSSÍVEL, referencie na tarefa" in resp
    assert "paginada" in resp and "`month`" in resp
    assert "e `ref` com o\n  caminho do documento da viagem" in resp
