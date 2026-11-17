"""Painel de tarefas com botões no Telegram: ⬜ pendente / ✅ concluída; tocar alterna.

O painel é montado direto do arquivo de tarefas (sem modelo). O estado de cada botão é
decidido na hora do toque (não no texto do botão), então um painel antigo nunca desfaz ou
refaz nada por engano: tocar sempre inverte o estado ATUAL da tarefa.

callback_data: `tk:<id>:<AAAAMMDD>:<escopo>:<etiqueta>` (cabe nos 64 bytes do Telegram).
"""

from dataclasses import dataclass
from datetime import date, time

from tasks import model
from tasks.format import day_short
from tasks.model import TaskError, fmt_time
from tasks.store import TaskStore

PANEL_SCOPES = {"hoje": "h", "semana": "s", "pendentes": "p"}
_SCOPE_NAMES = {v: k for k, v in PANEL_SCOPES.items()}
MAX_BUTTONS = 40
LABEL_MAX = 56
PREFIX = "tk"
TITLE = "Toque numa tarefa para marcar ou desmarcar:"


@dataclass
class PanelItem:
    id: str
    text: str
    done: bool
    on: date  # dia da ocorrência (a de hoje, para recorrentes)
    when: str = ""  # prefixo curto: data/hora

    def label(self) -> str:
        mark = "✅" if self.done else "⬜"
        label = f"{mark} {self.when + ' ' if self.when else ''}{self.text}"
        return label if len(label) <= LABEL_MAX else label[: LABEL_MAX - 1] + "…"


def _when(due: date | None, at, today: date) -> str:
    parts = []
    if due is not None and due != today:
        parts.append(f"{due:%d/%m}")
    if at:
        parts.append(fmt_time(at))
    return " ".join(parts)


def build_items(tasks: TaskStore, scope: str = "hoje", tag: str = "") -> tuple[list[PanelItem], int]:
    """Itens do painel e o total existente (os itens são limitados a MAX_BUTTONS)."""
    if scope not in PANEL_SCOPES:
        raise TaskError(f"escopo '{scope}' não tem painel (use: {', '.join(PANEL_SCOPES)})")
    today = tasks.today()
    wanted = model.normalize_tag(tag) if tag else ""
    items: list[PanelItem] = []
    tl = tasks.load()
    if scope == "pendentes":
        for t in sorted((t for t in tl.pending if not wanted or wanted in t.tags),
                        key=lambda t: (t.due_date is None, t.due_date or date.max, t.due_time or time.min)):
            items.append(PanelItem(t.id, t.text, False, today, _when(t.due_date, t.due_time, today)))
    else:
        agenda = tasks.agenda(today, horizon_days=3 if scope == "hoje" else 7, tag=tag)
        for t in agenda.overdue:
            items.append(PanelItem(t.id, t.text, False, today, _when(t.due_date, t.due_time, today)))
        for i in agenda.due_today:
            items.append(PanelItem(i.task.id, i.task.text, i.done, today, _when(None, i.when, today)))
        for day, day_items in agenda.upcoming.items():
            for i in day_items:
                if not i.recurring:  # ocorrência futura de recorrente não se marca antes do dia
                    items.append(PanelItem(i.task.id, i.task.text, False, today, _when(day, i.when, today)))
        for t in agenda.undated:
            items.append(PanelItem(t.id, t.text, False, today))
        recurring_ids = {r.id for r in tl.recurring}
        for c in tl.done:  # únicas concluídas hoje continuam visíveis, para poder desmarcar
            if c.done_on == today and c.id not in recurring_ids and (not wanted or wanted in c.tags):
                items.append(PanelItem(c.id, c.text, True, today, _when(c.due_date, c.due_time, today)))
    return items[:MAX_BUTTONS], len(items)


def encode(item: PanelItem, scope: str, tag: str = "") -> str:
    tag = model.normalize_tag(tag) if tag else ""
    return f"{PREFIX}:{item.id}:{item.on:%Y%m%d}:{PANEL_SCOPES[scope]}:{tag}"[:64]


def decode(data: str) -> tuple[str, date, str, str] | None:
    """(id, dia, escopo, etiqueta), ou None se não for um callback de tarefa válido."""
    parts = data.split(":", 4)
    if len(parts) != 5 or parts[0] != PREFIX or parts[3] not in _SCOPE_NAMES:
        return None
    try:
        day = date(int(parts[2][:4]), int(parts[2][4:6]), int(parts[2][6:]))
    except ValueError:
        return None
    return parts[1], day, _SCOPE_NAMES[parts[3]], parts[4]


def build_panel(tasks: TaskStore, scope: str = "hoje", tag: str = "") -> tuple[str, list[list[tuple[str, str]]]]:
    """(texto, linhas de botões [(rótulo, callback_data)]). Linhas vazias se não há tarefas."""
    items, total = build_items(tasks, scope, tag)
    today = tasks.today()
    where = {"hoje": "hoje e próximos dias", "semana": "próximos 7 dias", "pendentes": "pendentes"}[scope]
    head = f"Tarefas ({where}, {day_short(today)})" + (f" #{model.normalize_tag(tag)}" if tag else "")
    if not items:
        return f"{head}: nenhuma tarefa.", []
    text = f"{head}\n{TITLE}"
    if total > len(items):
        text += f"\n(mostrando {len(items)} de {total})"
    return text, [[(it.label(), encode(it, scope, tag))] for it in items]


def toggle(tasks: TaskStore, task_id: str, on: date) -> tuple[str, bool]:
    """Inverte o estado atual da tarefa. Devolve (texto, concluída agora?)."""
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
    raise TaskError("essa tarefa não existe mais")
