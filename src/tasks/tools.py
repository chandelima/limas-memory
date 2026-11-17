"""LangChain tools of the chat agent for the task list (todos/todo.md).

Thin layer over the TaskStore: validation errors become "ERROR: ..." for the agent to fix.
Changes take effect immediately (the file is written by code) and go into the next commit.
"""

import math
import re
from datetime import date, time

from langchain_core.tools import BaseTool, tool

from tasks import model
from tasks.format import (
    day_short, describe, format_agenda, format_done, format_pending, format_recurring,
)
from tasks.model import Recurrence, TaskError
from tasks.store import TaskStore

SCOPES = ("today", "week", "pending", "recurring", "done")
DONE_LIMIT = 20
PAGE_SIZE = 30  # tasks per page in list_tasks("pending")


def _date_or_none(value: str):
    return model.parse_date(value) if value.strip() else None


def _time_or_none(value: str):
    return model.parse_time(value) if value.strip() else None


def build_task_tools(tasks: TaskStore) -> list[BaseTool]:
    @tool
    def list_tasks(scope: str = "today", tag: str = "", month: str = "", page: int = 1) -> str:
        """List tasks WITH each one's ID (use the ID to complete, reschedule, etc.).
        `scope`: 'today' (overdue, today with the recurring ones and the next 3 days), 'week'
        (next 7 days), 'pending' (all open ones), 'recurring' or 'done' (the last 20).
        `tag` filters by tag (e.g. 'health'). With 'pending', `month` (YYYY-MM) filters by
        due month and `page` paginates (30 per page; the footer says how many there are).
        Call it before answering questions about tasks and before completing or changing one."""
        try:
            wanted = scope.strip().lower() or "today"
            if wanted not in SCOPES:
                return f"ERROR: invalid scope '{scope}' (use: {', '.join(SCOPES)})"
            today = tasks.today()
            header = f"Today is {day_short(today)}, {today:%Y-%m-%d}."
            if wanted in ("today", "week"):
                agenda = tasks.agenda(horizon_days=3 if wanted == "today" else 7, tag=tag)
                body = (format_agenda(agenda, show_ids=True, max_undated=50, max_overdue=50)
                        or "No tasks for the period.")
            else:
                tl = tasks.load()
                keep = (lambda items: [i for i in items if model.normalize_tag(tag) in i.tags]) if tag else (lambda items: items)
                if wanted == "pending":
                    pending = keep(tl.pending)
                    if month:
                        if not re.fullmatch(r"\d{4}-\d{2}", month.strip()):
                            return f"ERROR: invalid month: '{month}' (use YYYY-MM)"
                        pending = [t for t in pending if t.due_date and t.due_date.strftime("%Y-%m") == month.strip()]
                    pending.sort(key=lambda t: (t.due_date is None, t.due_date or date.max, t.due_time or time.min))
                    pages = max(1, math.ceil(len(pending) / PAGE_SIZE))
                    if not 1 <= page <= pages:
                        return f"ERROR: page {page} does not exist (there are {pages})"
                    chunk = pending[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
                    body = format_pending(chunk)
                    if body:
                        body += f"\n\nPage {page}/{pages} ({len(pending)} tasks)."
                        if page < pages:
                            body += f" Use page={page + 1} to continue."
                elif wanted == "recurring":
                    body = format_recurring(keep(tl.recurring))
                else:
                    body = format_done(keep(sorted(tl.done, key=lambda c: c.done_on, reverse=True))[:DONE_LIMIT])
                body = body or "No tasks in this list."
            used = tasks.tags()
            footer = ("\nTags in use: " + ", ".join(f"#{t} ({n})" for t, n in used.items())) if used else ""
            return f"{header}\n\n{body}{footer}"
        except TaskError as exc:
            return f"ERROR: {exc}"

    @tool
    def add_task(text: str, due_date: str = "", due_time: str = "", recurrence: str = "", tags: str = "",
                 ref: str = "") -> str:
        """Create a task. `text`: what to do (without the date or the tag). `due_date`:
        YYYY-MM-DD (convert 'tomorrow', 'Friday' etc. using the current date); empty = no
        date. `due_time`: HH:MM (only with a date, or on a recurring task). `recurrence`:
        'daily', 'weekly mon,wed' or 'monthly 5' (a recurring task has no date). `tags`:
        comma-separated tags, e.g. 'health, home'. `ref`: path of the memory document with the
        details (e.g. 'trips/petrolina-2027.md'); ALWAYS fill it in when a related document
        exists (check with list_memory or search_memory); the document must exist."""
        try:
            task = tasks.add(text, _date_or_none(due_date), _time_or_none(due_time),
                             Recurrence.parse(recurrence) if recurrence.strip() else None, tags, ref)
            return f"OK: created [{task.id}] {describe(task)}"
        except TaskError as exc:
            return f"ERROR: {exc}"

    @tool
    def complete_task(task_id: str, on_date: str = "") -> str:
        """Mark a task as completed (the default date is today; `on_date` YYYY-MM-DD only if it
        was done on another day). On a recurring task, it counts for that day only."""
        try:
            done = tasks.complete(task_id, _date_or_none(on_date))
            return f"OK: completed [{done.id}] {done.text} (on {day_short(done.done_on)})"
        except TaskError as exc:
            return f"ERROR: {exc}"

    @tool
    def reschedule_task(task_id: str, due_date: str, due_time: str = "") -> str:
        """Change the date (YYYY-MM-DD) and, if given, the time (HH:MM) of a pending task.
        Without `due_time`, the previous time is removed. Not for recurring tasks."""
        try:
            task = tasks.reschedule(task_id, model.parse_date(due_date), _time_or_none(due_time))
            return f"OK: rescheduled [{task.id}] {describe(task)}"
        except TaskError as exc:
            return f"ERROR: {exc}"

    @tool
    def reopen_task(task_id: str, on_date: str = "") -> str:
        """Undo the completion of a task (it goes back to the pending ones). On a recurring
        task, it undoes today's completion (or the one of `on_date`). Only works for tasks
        completed in the last 30 days."""
        try:
            task = tasks.reopen(task_id, _date_or_none(on_date))
            return f"OK: reopened [{task.id}] {describe(task)}"
        except TaskError as exc:
            return f"ERROR: {exc}"

    @tool
    def remove_task(task_id: str) -> str:
        """Remove a pending or recurring task (use it when the user gave up on it)."""
        try:
            task = tasks.remove(task_id)
            return f"OK: removed [{task.id}] {describe(task)}"
        except TaskError as exc:
            return f"ERROR: {exc}"

    @tool
    def tag_task(task_id: str, add_tags: str = "", remove_tags: str = "") -> str:
        """Add and/or remove tags on a pending or recurring task (comma-separated lists,
        e.g. add_tags='health')."""
        try:
            task = tasks.retag(task_id, add_tags, remove_tags)
            return f"OK: tags updated [{task.id}] {describe(task)}"
        except TaskError as exc:
            return f"ERROR: {exc}"

    @tool
    def link_task(task_id: str, ref: str = "") -> str:
        """Link a pending or recurring task to the memory document that has the details
        (e.g. ref='trips/petrolina-2027.md'). An empty `ref` removes the link. The document
        must exist (check the path with list_memory or search_memory)."""
        try:
            task = tasks.set_ref(task_id, ref)
            return f"OK: reference updated [{task.id}] {describe(task)}"
        except TaskError as exc:
            return f"ERROR: {exc}"

    return [list_tasks, add_task, complete_task, reschedule_task, reopen_task, remove_task, tag_task, link_task]
