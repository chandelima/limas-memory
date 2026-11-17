"""Text of the task lists, used by the agent's tools (with IDs) and by the morning briefing."""

from datetime import date, time

from i18n import t as tr
from tasks.model import Completion, Task, fmt_time, with_tags
from tasks.store import Agenda, AgendaItem



def day_long(d: date) -> str:
    return f"{tr('weekday_long_' + str(d.weekday()))}, {d:%d/%m}"


def day_short(d: date) -> str:
    return f"{d:%d/%m} ({tr('weekday_short_' + str(d.weekday()))})"


def _line(text: str, tags=(), *, task_id: str = "", prefix: str = "", suffix: str = "", ref: str = "") -> str:
    parts = [f"[{task_id}]" if task_id else "", prefix, with_tags(text, tags), suffix, tr("fmt_see", ref=ref) if ref else ""]
    return "- " + " ".join(p for p in parts if p)


def _item_line(item: AgendaItem, *, show_ids: bool, day_prefix: str = "") -> str:
    suffix = ""
    if item.recurring:
        suffix = f"({item.task.recurrence.render()}{tr('fmt_done_suffix') if item.done else ''})"
    prefix = " ".join(p for p in (day_prefix, fmt_time(item.when)) if p)
    return _line(item.task.text, item.task.tags, task_id=item.task.id if show_ids else "", prefix=prefix,
                 suffix=suffix, ref=item.task.ref if show_ids else "")


def format_agenda(agenda: Agenda, *, show_ids: bool = False, max_undated: int = 5, max_overdue: int = 10) -> str:
    """Overdue, today, upcoming days and undated. Empty if there is nothing in any section.
    Overdue shows only the `max_overdue` most recent ones (with 2000 appointments, the whole
    list of overdue items would not fit in a message)."""
    if agenda.is_empty():
        return ""
    blocks = []
    if agenda.overdue:
        lines = [tr("fmt_overdue")]
        shown = agenda.overdue[-max_overdue:]
        if len(agenda.overdue) > len(shown):
            lines.append(tr("fmt_older", count=len(agenda.overdue) - len(shown)))
        for t in shown:
            when = f"{day_short(t.due_date)}{' ' + fmt_time(t.due_time) if t.due_time else ''}"
            lines.append(_line(t.text, t.tags, task_id=t.id if show_ids else "", suffix=tr("fmt_was", when=when),
                               ref=t.ref if show_ids else ""))
        blocks.append(lines)
    today = [tr("fmt_today", day=day_long(agenda.today))]
    today += [_item_line(i, show_ids=show_ids) for i in agenda.due_today] or [tr("fmt_nothing")]
    blocks.append(today)
    if agenda.upcoming:
        lines = [tr("fmt_upcoming")]
        for day, items in agenda.upcoming.items():
            lines += [_item_line(i, show_ids=show_ids, day_prefix=day_short(day)) for i in items]
        blocks.append(lines)
    if agenda.undated:
        lines = [tr("fmt_no_date_header")]
        lines += [_line(t.text, t.tags, task_id=t.id if show_ids else "", ref=t.ref if show_ids else "")
                  for t in agenda.undated[:max_undated]]
        if len(agenda.undated) > max_undated:
            lines.append(tr("fmt_more", count=len(agenda.undated) - max_undated))
        blocks.append(lines)
    return "\n\n".join("\n".join(b) for b in blocks)


def format_pending(tasks: list[Task]) -> str:
    lines = []
    for t in sorted(tasks, key=lambda x: (x.due_date is None, x.due_date or date.max, x.due_time or time.min)):
        when = (day_short(t.due_date) + (" " + fmt_time(t.due_time) if t.due_time else "")) if t.due_date else tr("fmt_no_date")
        lines.append(_line(t.text, t.tags, task_id=t.id, prefix=when, ref=t.ref))
    return "\n".join(lines)


def format_recurring(tasks: list[Task]) -> str:
    return "\n".join(
        _line(t.text, t.tags, task_id=t.id, ref=t.ref,
              prefix=" ".join(p for p in (t.recurrence.render(), fmt_time(t.due_time)) if p))
        for t in sorted(tasks, key=lambda x: x.due_time or time.min)
    )


def format_done(done: list[Completion]) -> str:
    return "\n".join(
        _line(c.text, c.tags, task_id=c.id, prefix=tr("fmt_done_on", day=day_short(c.done_on)), ref=c.ref)
        for c in sorted(done, key=lambda x: x.done_on, reverse=True)
    )


def describe(task: Task) -> str:
    """One-line summary of a task, to confirm a change."""
    parts = [task.text]
    if task.recurrence:
        parts.append(f"({task.recurrence.render()}{' ' + tr('fmt_at', time=fmt_time(task.due_time)) if task.due_time else ''})")
    elif task.due_date:
        parts.append(f"({day_short(task.due_date)}{' ' + tr('fmt_at', time=fmt_time(task.due_time)) if task.due_time else ''})")
    return with_tags(" ".join(parts), task.tags) + (" " + tr("fmt_see", ref=task.ref) if task.ref else "")
