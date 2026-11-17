"""Texto das listas de tarefas, usado pelas tools do agente (com IDs) e pelo resumo da manhã."""

from datetime import date, time

from tasks.model import Completion, Task, fmt_time, with_tags
from tasks.store import Agenda, AgendaItem

WEEKDAYS_LONG = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo")
WEEKDAYS_SHORT = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")


def day_long(d: date) -> str:
    return f"{WEEKDAYS_LONG[d.weekday()]}, {d:%d/%m}"


def day_short(d: date) -> str:
    return f"{d:%d/%m} ({WEEKDAYS_SHORT[d.weekday()]})"


def _line(text: str, tags=(), *, task_id: str = "", prefix: str = "", suffix: str = "") -> str:
    parts = [f"[{task_id}]" if task_id else "", prefix, with_tags(text, tags), suffix]
    return "- " + " ".join(p for p in parts if p)


def _item_line(item: AgendaItem, *, show_ids: bool, day_prefix: str = "") -> str:
    suffix = ""
    if item.recurring:
        suffix = f"({item.task.recurrence.render()}{', cumprida' if item.done else ''})"
    prefix = " ".join(p for p in (day_prefix, fmt_time(item.when)) if p)
    return _line(item.task.text, item.task.tags, task_id=item.task.id if show_ids else "", prefix=prefix, suffix=suffix)


def format_agenda(agenda: Agenda, *, show_ids: bool = False, max_undated: int = 5) -> str:
    """Atrasadas, hoje, próximos dias e sem data. Vazio se não há nada em nenhuma seção."""
    if agenda.is_empty():
        return ""
    blocks = []
    if agenda.overdue:
        lines = ["Atrasadas:"]
        for t in agenda.overdue:
            when = f"{day_short(t.due_date)}{' ' + fmt_time(t.due_time) if t.due_time else ''}"
            lines.append(_line(t.text, t.tags, task_id=t.id if show_ids else "", suffix=f"(era {when})"))
        blocks.append(lines)
    today = [f"Hoje ({day_long(agenda.today)}):"]
    today += [_item_line(i, show_ids=show_ids) for i in agenda.due_today] or ["- nada marcado"]
    blocks.append(today)
    if agenda.upcoming:
        lines = ["Próximos dias:"]
        for day, items in agenda.upcoming.items():
            lines += [_item_line(i, show_ids=show_ids, day_prefix=day_short(day)) for i in items]
        blocks.append(lines)
    if agenda.undated:
        lines = ["Sem data:"]
        lines += [_line(t.text, t.tags, task_id=t.id if show_ids else "") for t in agenda.undated[:max_undated]]
        if len(agenda.undated) > max_undated:
            lines.append(f"- (e mais {len(agenda.undated) - max_undated})")
        blocks.append(lines)
    return "\n\n".join("\n".join(b) for b in blocks)


def format_pending(tasks: list[Task]) -> str:
    lines = []
    for t in sorted(tasks, key=lambda x: (x.due_date is None, x.due_date or date.max, x.due_time or time.min)):
        when = (day_short(t.due_date) + (" " + fmt_time(t.due_time) if t.due_time else "")) if t.due_date else "sem data"
        lines.append(_line(t.text, t.tags, task_id=t.id, prefix=when))
    return "\n".join(lines)


def format_recurring(tasks: list[Task]) -> str:
    return "\n".join(
        _line(t.text, t.tags, task_id=t.id,
              prefix=" ".join(p for p in (t.recurrence.render(), fmt_time(t.due_time)) if p))
        for t in sorted(tasks, key=lambda x: x.due_time or time.min)
    )


def format_done(done: list[Completion]) -> str:
    return "\n".join(
        _line(c.text, c.tags, task_id=c.id, prefix=f"feita em {day_short(c.done_on)}:")
        for c in sorted(done, key=lambda x: x.done_on, reverse=True)
    )


def describe(task: Task) -> str:
    """Resumo de uma linha de uma tarefa, para confirmar uma mudança."""
    parts = [task.text]
    if task.recurrence:
        parts.append(f"({task.recurrence.render()}{' às ' + fmt_time(task.due_time) if task.due_time else ''})")
    elif task.due_date:
        parts.append(f"({day_short(task.due_date)}{' às ' + fmt_time(task.due_time) if task.due_time else ''})")
    return with_tags(" ".join(parts), task.tags)
