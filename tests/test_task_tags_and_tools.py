from datetime import date, time

import pytest

from storage.store import MemoryStore
from tasks import model
from tasks.format import describe, format_agenda
from tasks.model import Recurrence, TaskError
from tasks.store import TaskStore
from tasks.tools import build_task_tools

TODAY = date(2026, 11, 18)  # quarta-feira


@pytest.fixture
def tasks(tmp_path):
    return TaskStore(MemoryStore(tmp_path / "memory"), today=lambda: TODAY)


@pytest.fixture
def tools(tasks):
    return {t.name: t for t in build_task_tools(tasks)}


def call(tools, name, **kwargs):
    return tools[name].invoke(kwargs)


# ---- etiquetas: modelo -----------------------------------------------------------------


def test_normalize_and_parse_tags():
    assert model.normalize_tag("#Saúde") == "saúde" and model.normalize_tag("casa nova") == "casa-nova"
    assert model.parse_tags("saúde, casa #casa  trabalho") == ("saúde", "casa", "trabalho")
    assert model.parse_tags(["A", "b"]) == ("a", "b") and model.parse_tags("") == ()
    for bad in ("1", "#2024", "a!b", "-x"):
        with pytest.raises(TaskError, match="etiqueta inválida"):
            model.normalize_tag(bad)


def test_split_tags_only_takes_the_trailing_ones_and_not_numbers():
    assert model.split_tags("Consulta #saúde #casa") == ("Consulta", ("saúde", "casa"))
    assert model.split_tags("Pagar conta #1") == ("Pagar conta #1", ())  # "#1" não é etiqueta
    assert model.split_tags("Falar do #projeto amanhã") == ("Falar do #projeto amanhã", ())  # só no fim
    assert model.split_tags("sem etiqueta") == ("sem etiqueta", ())
    assert model.with_tags("Consulta", ("saúde", "casa")) == "Consulta #saúde #casa"


TAGGED = """## Pendentes

- [ ] 2026-11-18 08:00 — Consulta médica #saúde ^a1b2
- [ ] Comprar presente #casa #família ^c3d4

## Recorrentes

- [ ] diária 07:00 — Tomar remédio #saúde ^e5f6

## Concluídas

- [x] 2026-11-17 — Dentista #saúde (feita em 2026-11-17) ^0a0a
"""


def test_tags_are_parsed_in_every_section_and_roundtrip():
    tl = model.parse(TAGGED)
    assert [t.tags for t in tl.pending] == [("saúde",), ("casa", "família")]
    assert tl.recurring[0].tags == ("saúde",) and tl.done[0].tags == ("saúde",)
    assert tl.pending[0].text == "Consulta médica" and tl.done[0].text == "Dentista"  # sem as etiquetas
    again = model.parse(model.render(tl))
    assert [(t.text, t.tags) for t in again.pending] == [(t.text, t.tags) for t in tl.pending]
    assert again.recurring == tl.recurring and again.done == tl.done


# ---- etiquetas: store ------------------------------------------------------------------


def test_add_merges_inline_and_explicit_tags_and_rejects_text_only_tags(tasks):
    task = tasks.add("Consulta médica #saúde", tags="Casa, saúde")
    assert task.text == "Consulta médica" and task.tags == ("saúde", "casa")
    with pytest.raises(TaskError, match="precisa de um texto"):
        tasks.add("#saúde")
    with pytest.raises(TaskError, match="etiqueta inválida"):
        tasks.add("x", tags="1abc")
    assert [t.text for t in tasks.load().pending] == ["Consulta médica"]


def test_tags_follow_the_task_through_complete_and_reopen(tasks):
    task = tasks.add("Consulta", TODAY, tags="saúde")
    done = tasks.complete(task.id)
    assert done.tags == ("saúde",) and tasks.load().done[0].tags == ("saúde",)
    assert tasks.reopen(task.id).tags == ("saúde",)
    rec = tasks.add("Remédio", recurrence=Recurrence.parse("diária"), tags="saúde")
    assert tasks.complete(rec.id).tags == ("saúde",)


def test_retag_adds_and_removes_and_tags_counts_usage(tasks):
    a, b = tasks.add("a", tags="casa"), tasks.add("b", tags="casa, saúde")
    rec = tasks.add("r", recurrence=Recurrence.parse("diária"), tags="saúde")
    assert tasks.tags() == {"casa": 2, "saúde": 2}
    assert tasks.retag(a.id, add="trabalho", remove="casa").tags == ("trabalho",)
    assert tasks.retag(rec.id, add="saúde, hábito").tags == ("saúde", "hábito")
    assert tasks.tags() == {"saúde": 2, "casa": 1, "hábito": 1, "trabalho": 1}
    with pytest.raises(TaskError, match="não existe tarefa"):
        tasks.retag("zzzz", add="x")


def test_agenda_can_be_filtered_by_tag(tasks):
    tasks.add("consulta", TODAY, time(8, 0), tags="saúde")
    tasks.add("reunião", TODAY, time(9, 0), tags="trabalho")
    tasks.add("remédio", recurrence=Recurrence.parse("diária"), tags="saúde")
    agenda = tasks.agenda(tag="Saúde")
    assert sorted(i.task.text for i in agenda.due_today) == ["consulta", "remédio"]
    assert [i.task.text for i in tasks.agenda(tag="trabalho").due_today] == ["reunião"]
    with pytest.raises(TaskError):
        tasks.agenda(tag="1")


# ---- formatação --------------------------------------------------------------------------


def test_format_agenda_sections_and_empty_case(tasks):
    assert format_agenda(tasks.agenda()) == ""
    tasks.add("atrasada", date(2026, 11, 15), tags="casa")
    tasks.add("consulta", TODAY, time(8, 0), tags="saúde")
    tasks.add("remédio", recurrence=Recurrence.parse("diária"), due_time=time(7, 0))
    tasks.add("amanhã", date(2026, 11, 19), time(9, 30))
    tasks.add("sem data")
    text = format_agenda(tasks.agenda())
    assert text.split("\n\n") == [
        "Atrasadas:\n- atrasada #casa (era 15/11 (dom))",
        "Hoje (quarta-feira, 18/11):\n- 07:00 remédio (diária)\n- 08:00 consulta #saúde",
        "Próximos dias:\n- 19/11 (qui) 09:30 amanhã",
        "Sem data:\n- sem data",
    ]
    assert "[" not in text  # no resumo da manhã não há IDs
    with_ids = format_agenda(tasks.agenda(), show_ids=True)
    assert f"[{tasks.load().pending[0].id}]" in with_ids


def test_format_agenda_says_nothing_today_when_only_other_days_have_items_and_caps_undated(tasks):
    tasks.add("amanhã", date(2026, 11, 19))
    for i in range(7):
        tasks.add(f"solta {i}")
    text = format_agenda(tasks.agenda(), max_undated=5)
    assert "Hoje (quarta-feira, 18/11):\n- nada marcado" in text and "- (e mais 2)" in text


def test_describe_task_one_liner(tasks):
    assert describe(tasks.add("a", TODAY, time(8, 0), tags="x")) == "a (18/11 (qua) às 08:00) #x"
    assert describe(tasks.add("b", recurrence=Recurrence.parse("semanal seg"), due_time=time(19, 0))) == "b (semanal seg às 19:00)"
    assert describe(tasks.add("c")) == "c"


# ---- tools ----------------------------------------------------------------------------------


def test_add_task_tool_variants_and_confirmation_text(tools):
    one = call(tools, "add_task", text="Consulta médica", due_date="2026-11-18", due_time="08:00", tags="saúde")
    assert one.startswith("OK: criada [") and "Consulta médica (18/11 (qua) às 08:00) #saúde" in one
    rec = call(tools, "add_task", text="Treino", recurrence="semanal seg,qua", due_time="19:00")
    assert "(semanal seg,qua às 19:00)" in rec
    assert call(tools, "add_task", text="Comprar presente").endswith("Comprar presente")


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        (dict(text="x", due_date="18/11/2026"), "data inválida"),
        (dict(text="x", due_date="2026-11-18", due_time="8h"), "hora inválida"),
        (dict(text="x", recurrence="quinzenal"), "recorrência inválida"),
        (dict(text="x", tags="1a"), "etiqueta inválida"),
        (dict(text="x", due_time="08:00"), "uma hora precisa de uma data"),
        (dict(text="x", due_date="2026-11-18", recurrence="diária"), "não tem data"),
        (dict(text="   "), "precisa de um texto"),
    ],
)
def test_add_task_tool_validation_errors_are_returned_as_text(tools, kwargs, expected):
    out = call(tools, "add_task", **kwargs)
    assert out.startswith("ERRO") and expected in out


def test_list_tasks_today_shows_ids_header_and_tag_summary(tools):
    call(tools, "add_task", text="Consulta", due_date="2026-11-18", due_time="08:00", tags="saúde")
    call(tools, "add_task", text="Remédio", recurrence="diária", due_time="07:00", tags="saúde")
    out = call(tools, "list_tasks")
    assert out.startswith("Hoje é 18/11 (qua), 2026-11-18.")
    assert "Hoje (quarta-feira, 18/11):" in out and "07:00 Remédio #saúde (diária)" in out
    assert "Etiquetas em uso: #saúde (2)" in out
    ids = [w.strip("[]") for w in out.split() if w.startswith("[") and w.endswith("]")]
    assert len(ids) == 2 and all(len(i) == 4 for i in ids)


def test_list_tasks_scopes_and_filters(tools, tasks):
    call(tools, "add_task", text="Longe", due_date="2026-11-24", tags="casa")
    call(tools, "add_task", text="Sem data")
    call(tools, "add_task", text="Treino", recurrence="semanal seg", due_time="19:00", tags="casa")
    done = tasks.add("Feita", TODAY, tags="casa")
    tasks.complete(done.id)
    assert "Nenhuma tarefa para o período." not in call(tools, "list_tasks", scope="semana")
    assert "Longe" in call(tools, "list_tasks", scope="semana") and "Longe" not in call(tools, "list_tasks", scope="hoje")
    pend = call(tools, "list_tasks", scope="pendentes")
    assert pend.index("Longe") < pend.index("Sem data")  # com data antes de sem data
    assert "semanal seg 19:00 Treino #casa" in call(tools, "list_tasks", scope="recorrentes")
    assert "feita em 18/11 (qua): Feita #casa" in call(tools, "list_tasks", scope="concluídas")  # aceita acento
    assert "Sem data" not in call(tools, "list_tasks", scope="pendentes", tag="casa")
    assert "Nenhuma tarefa nesta lista." in call(tools, "list_tasks", scope="recorrentes", tag="trabalho")


def test_list_tasks_empty_invalid_scope_and_invalid_tag(tools):
    assert "Nenhuma tarefa para o período." in call(tools, "list_tasks")
    assert call(tools, "list_tasks", scope="tudo").startswith("ERRO: escopo 'tudo' inválido")
    assert call(tools, "list_tasks", tag="1x").startswith("ERRO")


def test_complete_reschedule_reopen_remove_and_tag_tools(tools, tasks):
    task = tasks.add("Consulta", TODAY, time(8, 0))
    assert call(tools, "reschedule_task", task_id=task.id, due_date="2026-11-25", due_time="10:30") \
        .endswith("Consulta (25/11 (qua) às 10:30)")
    assert "etiquetas atualizadas" in call(tools, "tag_task", task_id=task.id, add_tags="saúde")
    assert "#saúde" in call(tools, "list_tasks", scope="pendentes")
    out = call(tools, "complete_task", task_id=task.id)
    assert out == f"OK: concluída [{task.id}] Consulta (em 18/11 (qua))"
    assert call(tools, "reopen_task", task_id=task.id).startswith(f"OK: reaberta [{task.id}]")
    assert call(tools, "remove_task", task_id=task.id).startswith("OK: removida")
    assert "Nenhuma tarefa" in call(tools, "list_tasks", scope="pendentes")


def test_recurring_completion_through_the_tool(tools, tasks):
    rec = tasks.add("Remédio", recurrence=Recurrence.parse("diária"))
    assert call(tools, "complete_task", task_id=rec.id).startswith("OK: concluída")
    assert "(diária, cumprida)" in call(tools, "list_tasks")
    assert call(tools, "reopen_task", task_id=rec.id).startswith("OK: reaberta")
    assert "cumprida" not in call(tools, "list_tasks")


@pytest.mark.parametrize(
    "name,kwargs,expected",
    [
        ("complete_task", dict(task_id="zzzz"), "não existe tarefa"),
        ("complete_task", dict(task_id="zzzz", on_date="amanhã"), "data inválida"),
        ("reschedule_task", dict(task_id="zzzz", due_date="2026-11-20"), "não existe tarefa pendente"),
        ("reschedule_task", dict(task_id="zzzz", due_date="20/11"), "data inválida"),
        ("reopen_task", dict(task_id="zzzz"), "não existe tarefa concluída"),
        ("remove_task", dict(task_id="zzzz"), "não existe tarefa"),
        ("tag_task", dict(task_id="zzzz", add_tags="x"), "não existe tarefa"),
        ("tag_task", dict(task_id="zzzz", add_tags="1x"), "etiqueta inválida"),
    ],
)
def test_other_tools_return_errors_as_text(tools, name, kwargs, expected):
    out = call(tools, name, **kwargs)
    assert out.startswith("ERRO") and expected in out


def test_reschedule_a_recurring_task_explains_what_to_do(tools, tasks):
    rec = tasks.add("Remédio", recurrence=Recurrence.parse("diária"))
    assert "remova-a e crie outra" in call(tools, "reschedule_task", task_id=rec.id, due_date="2026-11-20")


def test_the_seven_tools_have_descriptions_for_the_model(tools):
    assert set(tools) == {"list_tasks", "add_task", "complete_task", "reschedule_task", "reopen_task",
                          "remove_task", "tag_task"}
    assert all(t.description for t in tools.values())


def test_split_tags_when_the_whole_text_is_only_tags():
    assert model.split_tags("#saúde") == ("", ("saúde",))
    assert model.split_tags("#saúde #casa") == ("", ("saúde", "casa"))
    assert model.split_tags("#1") == ("#1", ())


def test_prompts_teach_the_task_conventions_to_both_agents():
    from curator.agent import load_system_prompt as curator_prompt
    from responder.agent import load_system_prompt as responder_prompt

    resp = responder_prompt()
    assert "TAREFAS:" in resp
    for name in ("list_tasks", "add_task", "complete_task", "reschedule_task", "reopen_task", "remove_task", "tag_task"):
        assert name in resp
    assert "AAAA-MM-DD" in resp and "[agora: ...]" in resp
    # eventos de viagem: por padrão não viram tarefa, mas um pedido explícito do usuário prevalece
    assert "não os copie para a\n  lista por conta própria" in resp
    assert "pedido explícito do usuário sempre\n  prevalece" in resp and "etiqueta `viagem`" in resp
    assert "NÃO são\n  tarefas" not in resp  # a regra absoluta que fez o agente recusar o pedido
    assert "A única coisa que você altera é a lista de tarefas" in resp
    cur = curator_prompt()
    assert "TAREFAS E COMPROMISSOS:" in cur and "`todos/`" in cur and "não copie eventos de viagem" in cur
