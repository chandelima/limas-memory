"""Task list model and its Markdown format (memory/todos/todo.md).

The file is generated and read by code (never by the Curator), so the format is fixed:

    ## Pending
    - [ ] 2026-11-18 08:00 — Doctor appointment ^a1b2        (date and time)
    - [ ] 2026-11-20 — Call the bank ^c3d4                    (date only)
    - [ ] Buy a gift ^e5f6                                    (no date)

    ## Recurring
    - [ ] daily 08:00 — Take medication ^g7h8
    - [ ] weekly mon,wed 19:00 — Workout ^i9j0
    - [ ] monthly 5 — Pay rent ^k1l2

    ## Done
    - [x] 2026-11-18 08:00 — Doctor appointment (done on 2026-11-18) ^a1b2

The `^xxxx` suffix is the task ID (used by the tools). A recurring task is a definition;
each time it is fulfilled it becomes a line under Done with the same ID.
Lines the code does not recognize (manual edits) are preserved in their own section.
"""

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, time

import yaml

from storage import markdown as md

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SEP = " — "
MAX_DONE = 100  # history of completed tasks kept in the file
MAX_TEXT = 200
EMPTY = "_None._"

SECTION_PENDING, SECTION_RECURRING, SECTION_DONE = "Pending", "Recurring", "Done"
_SECTION_NAMES = {"pending": SECTION_PENDING, "recurring": SECTION_RECURRING, "done": SECTION_DONE}

_ID = re.compile(r"\s\^([0-9a-f]{4})\s*$")
_ITEM = re.compile(r"^- \[( |x)\] (.+)$")
_DATE = r"\d{4}-\d{2}-\d{2}"
_TIME = r"\d{2}:\d{2}"
_PENDING = re.compile(rf"^(?:({_DATE})(?: ({_TIME}))?{re.escape(SEP)})?(.+)$")
_RECURRING = re.compile(rf"^(daily|weekly [a-z,]+|monthly \d{{1,2}})(?: ({_TIME}))?{re.escape(SEP)}(.+)$")
_DONE = re.compile(rf"^(?:({_DATE})(?: ({_TIME}))?|no date){re.escape(SEP)}(.+) \(done on ({_DATE})\)$")


class TaskError(ValueError):
    """Invalid request about tasks; the message is safe to show to the agent."""


# ---- dates and times -----------------------------------------------------------------


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise TaskError(f"invalid date: '{value}' (use YYYY-MM-DD)") from None


def parse_time(value: str) -> time:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if match and int(match.group(1)) < 24 and int(match.group(2)) < 60:
        return time(int(match.group(1)), int(match.group(2)))
    raise TaskError(f"invalid time: '{value}' (use HH:MM)")


def fmt_time(t: time | None) -> str:
    return t.strftime("%H:%M") if t else ""


# ---- recurrence ----------------------------------------------------------------------


@dataclass(frozen=True)
class Recurrence:
    kind: str  # "daily" | "weekly" | "monthly"
    weekdays: tuple[int, ...] = ()  # 0 = Monday
    month_day: int | None = None

    def occurs_on(self, d: date) -> bool:
        if self.kind == "daily":
            return True
        if self.kind == "weekly":
            return d.weekday() in self.weekdays
        last = calendar.monthrange(d.year, d.month)[1]
        return d.day == min(self.month_day or 1, last)  # day 31 falls on the last day of a short month

    def render(self) -> str:
        if self.kind == "daily":
            return "daily"
        if self.kind == "weekly":
            return "weekly " + ",".join(WEEKDAYS[i] for i in self.weekdays)
        return f"monthly {self.month_day}"

    @classmethod
    def parse(cls, text: str) -> "Recurrence":
        raw = " ".join(text.lower().split())
        if raw in ("daily", "every day", "everyday"):
            return cls("daily")
        if raw in ("weekdays", "workdays", "every weekday"):
            return cls("weekly", weekdays=(0, 1, 2, 3, 4))
        if raw.startswith("weekly"):
            names = [n for n in re.split(r"[\s,]+", raw.removeprefix("weekly").strip()) if n]
            days = sorted({WEEKDAYS.index(n[:3]) for n in names if n[:3] in WEEKDAYS})
            if days and len(days) == len(set(n[:3] for n in names)):
                return cls("weekly", weekdays=tuple(days))
        if raw.startswith("monthly"):
            day = raw.removeprefix("monthly").strip().removeprefix("day").strip()
            if day.isdigit() and 1 <= int(day) <= 31:
                return cls("monthly", month_day=int(day))
        raise TaskError(
            f"invalid recurrence: '{text}' (use 'daily', 'weekly mon,wed' or 'monthly 5')"
        )


# ---- tasks ---------------------------------------------------------------------------


@dataclass
class Task:
    id: str
    text: str
    due_date: date | None = None
    due_time: time | None = None
    recurrence: Recurrence | None = None
    tags: tuple[str, ...] = ()
    ref: str = ""  # memory document with the details (e.g. trips/petrolina-2027.md)


@dataclass
class Completion:
    id: str
    text: str
    due_date: date | None
    due_time: time | None
    done_on: date
    tags: tuple[str, ...] = ()
    ref: str = ""


@dataclass
class TaskList:
    pending: list[Task] = field(default_factory=list)
    recurring: list[Task] = field(default_factory=list)
    done: list[Completion] = field(default_factory=list)
    extras: dict[str, list[str]] = field(default_factory=dict)  # unrecognized lines

    def ids(self) -> set[str]:
        return {t.id for t in (*self.pending, *self.recurring)} | {c.id for c in self.done}


# ---- tags (#health) --------------------------------------------------------------------

_TAG = re.compile(r"[^\W\d_][\w-]*")  # starts with a letter: "#1" is not a tag
_TRAILING_TAGS = re.compile(r"(?:^|\s+)#[^\W\d_][\w-]*(?:\s+#[^\W\d_][\w-]*)*\s*$")


def normalize_tag(tag: str) -> str:
    value = tag.strip().lstrip("#").lower().replace(" ", "-")
    if not _TAG.fullmatch(value):
        raise TaskError(f"invalid tag: '{tag}' (letters, digits and hyphen; starts with a letter)")
    return value


def parse_tags(value) -> tuple[str, ...]:
    """'health, home' / '#health #home' / ['health'] -> ('health', 'home'); no repeats."""
    items = re.split(r"[,\s]+", value) if isinstance(value, str) else list(value)
    return tuple(dict.fromkeys(normalize_tag(i) for i in items if str(i).strip()))


def split_tags(text: str) -> tuple[str, tuple[str, ...]]:
    """Split the tags written at the END of the text: 'Checkup #health' -> ('Checkup', ('health',))."""
    match = _TRAILING_TAGS.search(text)
    if not match:
        return text, ()
    return text[: match.start()].rstrip(), parse_tags(match.group(0))


def with_tags(text: str, tags: tuple[str, ...]) -> str:
    return text + "".join(f" #{t}" for t in tags)


# ---- reference to a document (relative Markdown link) ----------------------------------

_REF_LINK = re.compile(r"\s\[[^\]]*\]\(\.\./([^)\s]+\.md)\)\s*$")
_REF_PATH = re.compile(r"[\w./-]+\.md", re.UNICODE)


def normalize_ref(ref: str) -> str:
    """'trips/petrolina-2027.md' (relative to the memory root); '' = no reference."""
    ref = ref.strip().lstrip("/")
    if not ref:
        return ""
    if ".." in ref.split("/") or not _REF_PATH.fullmatch(ref):
        raise TaskError(f"invalid reference: '{ref}' (use a document path, e.g. trips/x.md)")
    return ref


def split_ref(content: str) -> tuple[str, str]:
    """Split the '[details](../trips/x.md)' link off the end of the line."""
    match = _REF_LINK.search(content)
    return (content[: match.start()].rstrip(), match.group(1)) if match else (content, "")


def with_ref(content: str, ref: str) -> str:
    return f"{content} [details](../{ref})" if ref else content


def clean_text(text: str) -> str:
    """A single line, without the '^' that would mark an ID; empty or too long is an error."""
    text = " ".join(text.replace("^", "").split())
    if not text:
        raise TaskError("the task needs a text")
    if len(text) > MAX_TEXT:
        raise TaskError(f"text too long (maximum {MAX_TEXT} characters)")
    return text


# ---- Markdown -> model -----------------------------------------------------------------


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
        content, ref = split_ref(content)
        try:
            if section == SECTION_PENDING and item.group(1) == " " and (m := _PENDING.match(content)):
                due, tm = _parse_when(m.group(1), m.group(2))
                text, tags = split_tags(m.group(3))
                tl.pending.append(Task(task_id, text, due, tm, tags=tags, ref=ref))
            elif section == SECTION_RECURRING and item.group(1) == " " and (m := _RECURRING.match(content)):
                rec = Recurrence.parse(m.group(1))
                text, tags = split_tags(m.group(3))
                tl.recurring.append(Task(task_id, text, due_time=parse_time(m.group(2)) if m.group(2) else None,
                                         recurrence=rec, tags=tags, ref=ref))
            elif section == SECTION_DONE and item.group(1) == "x" and (m := _DONE.match(content)):
                due, tm = _parse_when(m.group(1), m.group(2))
                text, tags = split_tags(m.group(3))
                tl.done.append(Completion(task_id, text, due, tm, date.fromisoformat(m.group(4)), tags, ref))
            else:
                raise ValueError
        except (ValueError, TaskError):
            tl.extras.setdefault(section, []).append(line)
    return tl


# ---- model -> Markdown -----------------------------------------------------------------


def _when(d: date | None, t: time | None) -> str:
    return " ".join(p for p in (d.isoformat() if d else "", fmt_time(t)) if p)


def _pending_line(t: Task) -> str:
    when = _when(t.due_date, t.due_time)
    return f"- [ ] {when + SEP if when else ''}{with_ref(with_tags(t.text, t.tags), t.ref)} ^{t.id}"


def _recurring_line(t: Task) -> str:
    rule = " ".join(p for p in (t.recurrence.render(), fmt_time(t.due_time)) if p)
    return f"- [ ] {rule}{SEP}{with_ref(with_tags(t.text, t.tags), t.ref)} ^{t.id}"


def _done_line(c: Completion) -> str:
    body = f"{with_tags(c.text, c.tags)} (done on {c.done_on.isoformat()})"
    return f"- [x] {_when(c.due_date, c.due_time) or 'no date'}{SEP}{with_ref(body, c.ref)} ^{c.id}"


def _front(description: str) -> str:
    dumped = yaml.safe_dump({"description": description}, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{dumped}\n---"


def render(tl: TaskList) -> str:
    """Central file (todos/todo.md): undated pending tasks, recurring ones and recent completions.
    Pending tasks WITH a date live in monthly files (render_month); callers split them."""
    pending = sorted(tl.pending, key=lambda x: (x.due_date or date.max, x.due_time or time.min))
    recurring = sorted(tl.recurring, key=lambda x: x.due_time or time.min)
    done = sorted(tl.done, key=lambda c: c.done_on, reverse=True)[:MAX_DONE]
    sections = (
        (SECTION_PENDING, [_pending_line(t) for t in pending]),
        (SECTION_RECURRING, [_recurring_line(t) for t in recurring]),
        (SECTION_DONE, [_done_line(c) for c in done]),
    )
    out = [_front("Task list managed by the bot: undated, recurring and recently completed tasks."), "# Tasks", ""]
    for name, lines in sections:
        out += [f"## {name}", ""]
        out += [*lines, *tl.extras.get(name, [])] or [EMPTY]
        out.append("")
    return "\n".join(out)


def render_month(tasks: list[Task], month: str, extras: list[str] | None = None) -> str:
    """Pending tasks due in `month` (todos/YYYY-MM.md), with only the Pending section."""
    lines = [_pending_line(t) for t in sorted(tasks, key=lambda x: (x.due_date or date.max, x.due_time or time.min))]
    lines += list(extras or [])
    return "\n".join([_front(f"Pending tasks due in {month}."),
                      f"# Tasks due in {month}", "", f"## {SECTION_PENDING}", "",
                      *(lines or [EMPTY]), ""])


def _n(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def render_index(central: TaskList, months: dict[str, int], archives: dict[str, int], current_month: str) -> str:
    """Index of the todos/ folder: the central file, the months of pending tasks (future and
    already overdue) and the monthly archive of completed ones. Generated by code on every change."""
    out = [_front("Task index: central list, pending tasks by month and archive of completed ones."),
           "# Task index", "", "Generated automatically on every change; do not edit by hand.", "",
           "## Central list", "",
           "- [Undated, recurring and recently completed](todo.md) — "
           + ", ".join([_n(len(central.pending), "undated task", "undated tasks"),
                        _n(len(central.recurring), "recurring", "recurring"),
                        _n(len(central.done), "recently completed", "recently completed")]), "",
           "## Pending by month", ""]
    if months:
        for month in sorted(months):
            note = ""
            if months[month] and month < current_month:
                note = " (due date has passed)"
            label = _n(months[month], "pending", "pending") if months[month] else "none pending"
            out.append(f"- [{month}]({month}.md) — {label}{note}")
    else:
        out.append(EMPTY)
    out += ["", "## Completed (monthly archive)", ""]
    if archives:
        out += [f"- [{month}](archive/{month}.md) — {_n(archives[month], 'completed', 'completed')}"
                for month in sorted(archives, reverse=True)]
    else:
        out.append(EMPTY)
    return "\n".join([*out, ""])


def render_archive(done: list[Completion], month: str) -> str:
    """Monthly archive of completed tasks (todos/archive/YYYY-MM.md), with only the Done section."""
    lines = [_done_line(c) for c in sorted(done, key=lambda c: c.done_on, reverse=True)]
    return "\n".join([_front(f"Archive of tasks completed in {month}."),
                      f"# Tasks completed in {month}", "", f"## {SECTION_DONE}", "", *lines, ""])
