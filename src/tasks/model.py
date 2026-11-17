"""Modelo da lista de tarefas e o seu formato em Markdown (memory/todos/todo.md).

O arquivo é gerado e lido por código (nunca pelo Curator), então o formato é fixo:

    ## Pendentes
    - [ ] 2026-11-18 08:00 — Consulta médica ^a1b2        (data e hora)
    - [ ] 2026-11-20 — Ligar para o banco ^c3d4           (só data)
    - [ ] Comprar presente ^e5f6                          (sem data)

    ## Recorrentes
    - [ ] diária 08:00 — Tomar remédio ^g7h8
    - [ ] semanal seg,qua 19:00 — Treino ^i9j0
    - [ ] mensal 5 — Pagar aluguel ^k1l2

    ## Concluídas
    - [x] 2026-11-18 08:00 — Consulta médica (feita em 2026-11-18) ^a1b2

O sufixo `^xxxx` é o ID da tarefa (usado pelas tools). Uma tarefa recorrente é uma
definição; cada vez que é cumprida vira uma linha em Concluídas com o mesmo ID.
Linhas que o código não reconhece (edição manual) são preservadas na própria seção.
"""

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, time

import yaml

from storage import markdown as md

WEEKDAYS = ("seg", "ter", "qua", "qui", "sex", "sab", "dom")
SEP = " — "
MAX_DONE = 100  # histórico de concluídas mantido no arquivo
MAX_TEXT = 200
EMPTY = "_Nenhuma._"

SECTION_PENDING, SECTION_RECURRING, SECTION_DONE = "Pendentes", "Recorrentes", "Concluídas"
_SECTION_NAMES = {"pendentes": SECTION_PENDING, "recorrentes": SECTION_RECURRING,
                  "concluidas": SECTION_DONE, "concluídas": SECTION_DONE}

_ID = re.compile(r"\s\^([0-9a-f]{4})\s*$")
_ITEM = re.compile(r"^- \[( |x)\] (.+)$")
_DATE = r"\d{4}-\d{2}-\d{2}"
_TIME = r"\d{2}:\d{2}"
_PENDING = re.compile(rf"^(?:({_DATE})(?: ({_TIME}))?{re.escape(SEP)})?(.+)$")
_RECURRING = re.compile(rf"^(diária|semanal [a-z,]+|mensal \d{{1,2}})(?: ({_TIME}))?{re.escape(SEP)}(.+)$")
_DONE = re.compile(rf"^(?:({_DATE})(?: ({_TIME}))?|sem data){re.escape(SEP)}(.+) \(feita em ({_DATE})\)$")


class TaskError(ValueError):
    """Pedido inválido sobre tarefas; a mensagem é segura para mostrar ao agente."""


# ---- datas e horas -------------------------------------------------------------------


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise TaskError(f"data inválida: '{value}' (use AAAA-MM-DD)") from None


def parse_time(value: str) -> time:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if match and int(match.group(1)) < 24 and int(match.group(2)) < 60:
        return time(int(match.group(1)), int(match.group(2)))
    raise TaskError(f"hora inválida: '{value}' (use HH:MM)")


def fmt_time(t: time | None) -> str:
    return t.strftime("%H:%M") if t else ""


# ---- recorrência ---------------------------------------------------------------------


@dataclass(frozen=True)
class Recurrence:
    kind: str  # "daily" | "weekly" | "monthly"
    weekdays: tuple[int, ...] = ()  # 0 = segunda
    month_day: int | None = None

    def occurs_on(self, d: date) -> bool:
        if self.kind == "daily":
            return True
        if self.kind == "weekly":
            return d.weekday() in self.weekdays
        last = calendar.monthrange(d.year, d.month)[1]
        return d.day == min(self.month_day or 1, last)  # dia 31 vale no último dia do mês curto

    def render(self) -> str:
        if self.kind == "daily":
            return "diária"
        if self.kind == "weekly":
            return "semanal " + ",".join(WEEKDAYS[i] for i in self.weekdays)
        return f"mensal {self.month_day}"

    @classmethod
    def parse(cls, text: str) -> "Recurrence":
        raw = " ".join(text.lower().replace("ú", "u").replace("á", "a").split())
        if raw in ("diaria", "todo dia", "todos os dias", "diario", "daily"):
            return cls("daily")
        if raw in ("dias uteis", "dia util"):
            return cls("weekly", weekdays=(0, 1, 2, 3, 4))
        if raw.startswith("semanal"):
            names = [n for n in re.split(r"[\s,]+", raw.removeprefix("semanal").strip()) if n]
            days = sorted({WEEKDAYS.index(n[:3]) for n in names if n[:3] in WEEKDAYS})
            if days and len(days) == len(set(n[:3] for n in names)):
                return cls("weekly", weekdays=tuple(days))
        if raw.startswith("mensal"):
            day = raw.removeprefix("mensal").strip().removeprefix("dia").strip()
            if day.isdigit() and 1 <= int(day) <= 31:
                return cls("monthly", month_day=int(day))
        raise TaskError(
            f"recorrência inválida: '{text}' (use 'diária', 'semanal seg,qua' ou 'mensal 5')"
        )


# ---- tarefas -------------------------------------------------------------------------


@dataclass
class Task:
    id: str
    text: str
    due_date: date | None = None
    due_time: time | None = None
    recurrence: Recurrence | None = None
    tags: tuple[str, ...] = ()


@dataclass
class Completion:
    id: str
    text: str
    due_date: date | None
    due_time: time | None
    done_on: date
    tags: tuple[str, ...] = ()


@dataclass
class TaskList:
    pending: list[Task] = field(default_factory=list)
    recurring: list[Task] = field(default_factory=list)
    done: list[Completion] = field(default_factory=list)
    extras: dict[str, list[str]] = field(default_factory=dict)  # linhas não reconhecidas

    def ids(self) -> set[str]:
        return {t.id for t in (*self.pending, *self.recurring)} | {c.id for c in self.done}


# ---- etiquetas (#saúde) ----------------------------------------------------------------

_TAG = re.compile(r"[^\W\d_][\w-]*")  # começa com letra: "#1" não é etiqueta
_TRAILING_TAGS = re.compile(r"(?:^|\s+)#[^\W\d_][\w-]*(?:\s+#[^\W\d_][\w-]*)*\s*$")


def normalize_tag(tag: str) -> str:
    value = tag.strip().lstrip("#").lower().replace(" ", "-")
    if not _TAG.fullmatch(value):
        raise TaskError(f"etiqueta inválida: '{tag}' (letras, números e hífen; começa com letra)")
    return value


def parse_tags(value) -> tuple[str, ...]:
    """'saúde, casa' / '#saúde #casa' / ['saúde'] -> ('saúde', 'casa'); sem repetidas."""
    items = re.split(r"[,\s]+", value) if isinstance(value, str) else list(value)
    return tuple(dict.fromkeys(normalize_tag(i) for i in items if str(i).strip()))


def split_tags(text: str) -> tuple[str, tuple[str, ...]]:
    """Separa as etiquetas escritas no FIM do texto: 'Consulta #saúde' -> ('Consulta', ('saúde',))."""
    match = _TRAILING_TAGS.search(text)
    if not match:
        return text, ()
    return text[: match.start()].rstrip(), parse_tags(match.group(0))


def with_tags(text: str, tags: tuple[str, ...]) -> str:
    return text + "".join(f" #{t}" for t in tags)


def clean_text(text: str) -> str:
    """Uma linha só, sem o '^' que marcaria um ID; vazio ou longo demais é erro."""
    text = " ".join(text.replace("^", "").split())
    if not text:
        raise TaskError("a tarefa precisa de um texto")
    if len(text) > MAX_TEXT:
        raise TaskError(f"texto longo demais (máximo {MAX_TEXT} caracteres)")
    return text


# ---- Markdown -> modelo --------------------------------------------------------------


def _parse_when(d: str | None, t: str | None) -> tuple[date | None, time | None]:
    return (date.fromisoformat(d) if d else None, parse_time(t) if t else None)


def parse(text: str) -> TaskList:
    try:
        _, body = md.parse_frontmatter(text)
    except md.MarkdownError:
        body = text
    tl, section = TaskList(), None
    for line in body.splitlines():
        heading = md.parse_heading(line)
        if heading and heading[0] == 2:
            section = _SECTION_NAMES.get(heading[1].strip().lower())
            continue
        if section is None or not line.strip() or line.strip() == EMPTY:
            continue
        item = _ITEM.match(line)
        id_match = _ID.search(item.group(2)) if item else None
        if not item or not id_match:
            tl.extras.setdefault(section, []).append(line)
            continue
        content, task_id = item.group(2)[: id_match.start()].strip(), id_match.group(1)
        try:
            if section == SECTION_PENDING and item.group(1) == " " and (m := _PENDING.match(content)):
                due, tm = _parse_when(m.group(1), m.group(2))
                text, tags = split_tags(m.group(3))
                tl.pending.append(Task(task_id, text, due, tm, tags=tags))
            elif section == SECTION_RECURRING and item.group(1) == " " and (m := _RECURRING.match(content)):
                rec = Recurrence.parse(m.group(1))
                text, tags = split_tags(m.group(3))
                tl.recurring.append(Task(task_id, text, due_time=parse_time(m.group(2)) if m.group(2) else None,
                                         recurrence=rec, tags=tags))
            elif section == SECTION_DONE and item.group(1) == "x" and (m := _DONE.match(content)):
                due, tm = _parse_when(m.group(1), m.group(2))
                text, tags = split_tags(m.group(3))
                tl.done.append(Completion(task_id, text, due, tm, date.fromisoformat(m.group(4)), tags))
            else:
                raise ValueError
        except (ValueError, TaskError):
            tl.extras.setdefault(section, []).append(line)
    return tl


# ---- modelo -> Markdown --------------------------------------------------------------


def _when(d: date | None, t: time | None) -> str:
    return " ".join(p for p in (d.isoformat() if d else "", fmt_time(t)) if p)


def render(tl: TaskList) -> str:
    pending = sorted(tl.pending, key=lambda x: (x.due_date or date.max, x.due_time or time.min))
    recurring = sorted(tl.recurring, key=lambda x: x.due_time or time.min)
    done = sorted(tl.done, key=lambda c: c.done_on, reverse=True)[:MAX_DONE]

    def pending_line(t: Task) -> str:
        when = _when(t.due_date, t.due_time)
        return f"- [ ] {when + SEP if when else ''}{with_tags(t.text, t.tags)} ^{t.id}"

    def recurring_line(t: Task) -> str:
        return f"- [ ] {' '.join(p for p in (t.recurrence.render(), fmt_time(t.due_time)) if p)}{SEP}{with_tags(t.text, t.tags)} ^{t.id}"

    def done_line(c: Completion) -> str:
        return f"- [x] {_when(c.due_date, c.due_time) or 'sem data'}{SEP}{with_tags(c.text, c.tags)} (feita em {c.done_on.isoformat()}) ^{c.id}"

    sections = (
        (SECTION_PENDING, [pending_line(t) for t in pending]),
        (SECTION_RECURRING, [recurring_line(t) for t in recurring]),
        (SECTION_DONE, [done_line(c) for c in done]),
    )
    front = yaml.safe_dump(
        {"description": "Lista de tarefas gerenciada pelo bot: pendentes, recorrentes e concluídas."},
        allow_unicode=True, sort_keys=False,
    ).strip()
    out = [f"---\n{front}\n---", "# Tarefas", ""]
    for name, lines in sections:
        out += [f"## {name}", ""]
        out += [*lines, *tl.extras.get(name, [])] or [EMPTY]
        out.append("")
    return "\n".join(out)


def render_archive(done: list[Completion], month: str) -> str:
    """Arquivo mensal de concluídas (todos/arquivo/AAAA-MM.md), só com a seção Concluídas."""
    front = yaml.safe_dump({"description": f"Arquivo de tarefas concluídas em {month}."},
                           allow_unicode=True, sort_keys=False).strip()
    lines = [f"- [x] {_when(c.due_date, c.due_time) or 'sem data'}{SEP}{with_tags(c.text, c.tags)} (feita em {c.done_on.isoformat()}) ^{c.id}"
             for c in sorted(done, key=lambda c: c.done_on, reverse=True)]
    return "\n".join([f"---\n{front}\n---", f"# Tarefas concluídas em {month}", "",
                      f"## {SECTION_DONE}", "", *lines, ""])
