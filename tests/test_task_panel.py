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
    a = tasks.add("Consulta médica", TODAY, time(8, 0))
    b = tasks.add("Ligar para o banco", TODAY)
    tasks.complete(b.id)
    text, rows = panel.build_panel(tasks)
    assert "marcar ou desmarcar" in text
    assert labels(rows) == ["⭕ [08:00] Consulta médica", "🟢 Ligar para o banco"]  # no mesmo dia: só a hora
    assert panel.decode(rows[0][0][1]) == (a.id, TODAY, "hoje", "")
    assert all(len(data.encode()) <= 64 for _, data in sum(rows, []))


def test_overdue_upcoming_and_undated_appear_with_their_dates(tasks):
    tasks.add("Atrasada", date(2026, 11, 10))
    tasks.add("Amanhã", date(2026, 11, 19), time(9, 30))
    tasks.add("Sem data")
    tasks.add("Longe", date(2026, 12, 25))  # fora do horizonte de "hoje"
    _, rows = panel.build_panel(tasks)
    assert labels(rows) == ["⭕ [10/11] Atrasada", "⭕ [Amanhã 09:30] Amanhã", "⭕ Sem data"]
    _, pend = panel.build_panel(tasks, "pendentes")
    assert "⭕ [25/12] Longe" in labels(pend)


def test_toggle_flips_the_current_state_both_ways(tasks):
    t = tasks.add("Comprar pão", TODAY)
    assert panel.toggle(tasks, t.id, TODAY) == ("Comprar pão", True)
    assert [x.id for x in tasks.load().pending] == []
    assert panel.toggle(tasks, t.id, TODAY) == ("Comprar pão", False)  # segundo toque reabre
    assert [x.id for x in tasks.load().pending] == [t.id]


def test_recurring_task_toggles_only_todays_occurrence(tasks):
    r = tasks.add("Remédio", recurrence=Recurrence.parse("diária"), due_time=time(8, 0))
    assert panel.toggle(tasks, r.id, TODAY)[1] is True
    _, rows = panel.build_panel(tasks)
    assert labels(rows) == ["🟢 [08:00] Remédio"]
    assert panel.toggle(tasks, r.id, TODAY)[1] is False
    assert labels(panel.build_panel(tasks)[1]) == ["⭕ [08:00] Remédio"]


def test_toggle_unknown_task_raises_task_error(tasks):
    with pytest.raises(TaskError):
        panel.toggle(tasks, "zzzz", TODAY)


def test_tag_filter_and_button_limit_and_label_truncation(tasks):
    tasks.add("Dentista #saúde", TODAY)
    tasks.add("Mercado", TODAY)
    _, rows = panel.build_panel(tasks, "hoje", "saúde")
    assert len(rows) == 1 and "Dentista" in rows[0][0][0]
    for i in range(panel.MAX_BUTTONS + 5):
        tasks.add(f"Tarefa {i} " + "x" * 80)
    text, rows = panel.build_panel(tasks, "pendentes")
    assert len(rows) == panel.MAX_BUTTONS and "mostrando 40 de" in text
    assert all(len(r[0][0]) <= panel.LABEL_MAX for r in rows)


def test_empty_panel_has_no_buttons_and_decode_rejects_garbage(tasks):
    text, rows = panel.build_panel(tasks)
    assert rows == [] and "nenhuma tarefa" in text
    assert panel.decode("outra:coisa") is None and panel.decode("tk:a1b2:20261340:h:") is None


def test_multi_day_panel_prefixes_every_dated_task_with_hoje_amanha_or_date(tasks):
    tasks.add("A", TODAY, time(18, 0))
    tasks.add("B", TODAY)
    tasks.add("C", date(2026, 11, 19), time(7, 0))
    tasks.add("D", date(2027, 1, 31), time(15, 0))
    tasks.add("E", date(2026, 11, 17))  # ontem
    _, rows = panel.build_panel(tasks, "pendentes")
    assert labels(rows) == ["⭕ [Ontem] E", "⭕ [Hoje] B", "⭕ [Hoje 18:00] A", "⭕ [Amanhã 07:00] C", "⭕ [31/01 15:00] D"]
