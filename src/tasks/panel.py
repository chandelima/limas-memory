"""Task panel with buttons on Telegram: ⭕ pending / 🟢 done; tapping toggles.

The panel is built straight from the task file (no model). Each button's state is
decided at tap time (not from the button text), so an old panel never undoes or
redoes anything by mistake: a tap always flips the task's CURRENT state.

callback_data: `tk:<id>:<YYYYMMDD>:<scope>:<tag>` (fits in Telegram's 64 bytes).
"""

from dataclasses import dataclass
from datetime import date, time

from i18n import t
from tasks import model
from tasks.format import day_short
from tasks.model import TaskError, fmt_time
from tasks.store import TaskStore

PANEL_SCOPES = {"today": "t", "week": "w", "pending": "p"}
_SCOPE_NAMES = {v: k for k, v in PANEL_SCOPES.items()}
MAX_BUTTONS = 40
LABEL_MAX = 56
PREFIX = "tk"


@dataclass
class PanelItem:
    id: str
    text: str
    done: bool
    on: date  # day of the occurrence (today's, for recurring tasks)
    due: date | None = None  # day of the appointment (None = no date)
    at: time | None = None  # time of the appointment
    when: str = ""  # button prefix, e.g. "[Tomorrow 07:00]" (built in build_items)

    def label(self) -> str:
        mark = "🟢" if self.done else "⭕"
        label = f"{mark} {self.when + ' ' if self.when else ''}{self.text}"
        return label if len(label) <= LABEL_MAX else label[: LABEL_MAX - 1] + "…"


def day_name(day: date, today: date) -> str:
    delta = (day - today).days
    return {0: t("day_today"), 1: t("day_tomorrow"), -1: t("day_yesterday")}.get(delta) or f"{day:%d/%m}"


def _prefix(item: PanelItem, today: date, multi_day: bool) -> str:
    """"[Today 18:00]", "[Tomorrow 07:00]", "[31/01 15:00]". With everything on the same day, only the time."""
    if item.due is None and item.at is None:
        return ""
    parts = []
    if multi_day and item.due is not None:
        parts.append(day_name(item.due, today))
    if item.at:
        parts.append(fmt_time(item.at))
    return f"[{' '.join(parts)}]" if parts else ""


def build_items(tasks: TaskStore, scope: str = "today", tag: str = "") -> tuple[list[PanelItem], int]:
    """Panel items and the total that exists (items are limited to MAX_BUTTONS)."""
    if scope not in PANEL_SCOPES:
        raise TaskError(f"scope '{scope}' has no panel (use: {', '.join(PANEL_SCOPES)})")
    today = tasks.today()
    wanted = model.normalize_tag(tag) if tag else ""
    items: list[PanelItem] = []
    tl = tasks.load()
    if scope == "pending":
        for t in sorted((t for t in tl.pending if not wanted or wanted in t.tags),
                        key=lambda t: (t.due_date is None, t.due_date or date.max, t.due_time or time.min)):
            items.append(PanelItem(t.id, t.text, False, today, t.due_date, t.due_time))
    else:
        agenda = tasks.agenda(today, horizon_days=3 if scope == "today" else 7, tag=tag)
        for t in agenda.overdue:
            items.append(PanelItem(t.id, t.text, False, today, t.due_date, t.due_time))
        for i in agenda.due_today:
            items.append(PanelItem(i.task.id, i.task.text, i.done, today, today, i.when))
        for day, day_items in agenda.upcoming.items():
            for i in day_items:
                if not i.recurring:  # a future occurrence of a recurring task cannot be checked before its day
                    items.append(PanelItem(i.task.id, i.task.text, False, today, day, i.when))
        for t in agenda.undated:
            items.append(PanelItem(t.id, t.text, False, today))
        recurring_ids = {r.id for r in tl.recurring}
        for c in tl.done:  # one-off tasks completed today stay visible, so they can be unchecked
            if c.done_on == today and c.id not in recurring_ids and (not wanted or wanted in c.tags):
                items.append(PanelItem(c.id, c.text, True, today, c.due_date, c.due_time))
    multi_day = any(i.due is not None and i.due != today for i in items)
    for i in items:
        i.when = _prefix(i, today, multi_day)
    return items[:MAX_BUTTONS], len(items)


def encode(item: PanelItem, scope: str, tag: str = "") -> str:
    tag = model.normalize_tag(tag) if tag else ""
    return f"{PREFIX}:{item.id}:{item.on:%Y%m%d}:{PANEL_SCOPES[scope]}:{tag}"[:64]


def decode(data: str) -> tuple[str, date, str, str] | None:
    """(id, day, scope, tag), or None if it is not a valid task callback."""
    parts = data.split(":", 4)
    if len(parts) != 5 or parts[0] != PREFIX or parts[3] not in _SCOPE_NAMES:
        return None
    try:
        day = date(int(parts[2][:4]), int(parts[2][4:6]), int(parts[2][6:]))
    except ValueError:
        return None
    return parts[1], day, _SCOPE_NAMES[parts[3]], parts[4]


def build_panel(tasks: TaskStore, scope: str = "today", tag: str = "") -> tuple[str, list[list[tuple[str, str]]]]:
    """(text, button rows [(label, callback_data)]). Empty rows if there are no tasks."""
    items, total = build_items(tasks, scope, tag)
    today = tasks.today()
    where = t(f"panel_where_{scope}")
    head = t("panel_head", where=where, day=day_short(today)) + (f" #{model.normalize_tag(tag)}" if tag else "")
    if not items:
        return t("panel_none", head=head), []
    text = f"{head}\n{t('panel_title')}"
    if total > len(items):
        text += "\n" + t("panel_showing", shown=len(items), total=total)
    return text, [[(it.label(), encode(it, scope, tag))] for it in items]


def toggle(tasks: TaskStore, task_id: str, on: date) -> tuple[str, bool]:
    """Flip the task's current state. Return (text, completed now?)."""
    tl = tasks.load()
    if rec := next((t for t in tl.recurring if t.id == task_id), None):
        if any(c.id == task_id and c.done_on == on for c in tl.done):
            tasks.reopen(task_id, on)
            return rec.text, False
        tasks.complete(task_id, on)
        return rec.text, True
    if pending := next((t for t in tl.pending if t.id == task_id), None):
        tasks.complete(task_id, tasks.today())
        return pending.text, True
    if done := next((c for c in tl.done if c.id == task_id), None):
        tasks.reopen(task_id)
        return done.text, False
    raise TaskError(t("task_gone"))
