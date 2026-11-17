"""Tools LangChain do agente de resposta para a lista de tarefas (todos/todo.md).

Camada fina sobre o TaskStore: erros de validação viram "ERRO: ..." para o agente corrigir.
As mudanças valem na hora (o arquivo é gravado por código) e entram no próximo commit.
"""

import unicodedata

from langchain_core.tools import BaseTool, tool

from tasks import model
from tasks.format import (
    day_short, describe, format_agenda, format_done, format_pending, format_recurring,
)
from tasks.model import Recurrence, TaskError
from tasks.store import TaskStore

SCOPES = ("hoje", "semana", "pendentes", "recorrentes", "concluidas")
DONE_LIMIT = 20


def _norm(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text.strip().lower()) if unicodedata.category(c) != "Mn")


def _date_or_none(value: str):
    return model.parse_date(value) if value.strip() else None


def _time_or_none(value: str):
    return model.parse_time(value) if value.strip() else None


def build_task_tools(tasks: TaskStore) -> list[BaseTool]:
    @tool
    def list_tasks(scope: str = "hoje", tag: str = "") -> str:
        """Lista tarefas COM O ID de cada uma (use o ID para concluir, reagendar, etc.).
        `scope`: 'hoje' (atrasadas, hoje com as recorrentes e os próximos 3 dias), 'semana'
        (próximos 7 dias), 'pendentes' (todas as abertas), 'recorrentes' ou 'concluidas'
        (as últimas 20). `tag` filtra por etiqueta (ex.: 'saúde'). Chame antes de responder
        perguntas sobre tarefas e antes de concluir ou alterar uma."""
        try:
            wanted = _norm(scope) or "hoje"
            if wanted not in SCOPES:
                return f"ERRO: escopo '{scope}' inválido (use: {', '.join(SCOPES)})"
            today = tasks.today()
            header = f"Hoje é {day_short(today)}, {today:%Y-%m-%d}."
            if wanted in ("hoje", "semana"):
                agenda = tasks.agenda(horizon_days=3 if wanted == "hoje" else 7, tag=tag)
                body = format_agenda(agenda, show_ids=True, max_undated=50) or "Nenhuma tarefa para o período."
            else:
                tl = tasks.load()
                keep = (lambda items: [i for i in items if model.normalize_tag(tag) in i.tags]) if tag else (lambda items: items)
                if wanted == "pendentes":
                    body = format_pending(keep(tl.pending))
                elif wanted == "recorrentes":
                    body = format_recurring(keep(tl.recurring))
                else:
                    body = format_done(keep(sorted(tl.done, key=lambda c: c.done_on, reverse=True))[:DONE_LIMIT])
                body = body or "Nenhuma tarefa nesta lista."
            used = tasks.tags()
            footer = ("\nEtiquetas em uso: " + ", ".join(f"#{t} ({n})" for t, n in used.items())) if used else ""
            return f"{header}\n\n{body}{footer}"
        except TaskError as exc:
            return f"ERRO: {exc}"

    @tool
    def add_task(text: str, due_date: str = "", due_time: str = "", recurrence: str = "", tags: str = "") -> str:
        """Cria uma tarefa. `text`: o que fazer (sem a data nem a etiqueta). `due_date`:
        AAAA-MM-DD (converta 'amanhã', 'sexta' etc. usando a data de agora); vazio = sem
        data. `due_time`: HH:MM (só com data, ou numa recorrente). `recurrence`: 'diária',
        'semanal seg,qua' ou 'mensal 5' (tarefa recorrente não tem data). `tags`: etiquetas
        separadas por vírgula, ex.: 'saúde, casa'."""
        try:
            task = tasks.add(text, _date_or_none(due_date), _time_or_none(due_time),
                             Recurrence.parse(recurrence) if recurrence.strip() else None, tags)
            return f"OK: criada [{task.id}] {describe(task)}"
        except TaskError as exc:
            return f"ERRO: {exc}"

    @tool
    def complete_task(task_id: str, on_date: str = "") -> str:
        """Marca uma tarefa como concluída (a data padrão é hoje; `on_date` AAAA-MM-DD só se
        foi feita em outro dia). Numa recorrente, vale só para aquele dia."""
        try:
            done = tasks.complete(task_id, _date_or_none(on_date))
            return f"OK: concluída [{done.id}] {done.text} (em {day_short(done.done_on)})"
        except TaskError as exc:
            return f"ERRO: {exc}"

    @tool
    def reschedule_task(task_id: str, due_date: str, due_time: str = "") -> str:
        """Muda a data (AAAA-MM-DD) e, se houver, a hora (HH:MM) de uma tarefa pendente.
        Sem `due_time`, a hora anterior é removida. Não serve para recorrentes."""
        try:
            task = tasks.reschedule(task_id, model.parse_date(due_date), _time_or_none(due_time))
            return f"OK: reagendada [{task.id}] {describe(task)}"
        except TaskError as exc:
            return f"ERRO: {exc}"

    @tool
    def reopen_task(task_id: str, on_date: str = "") -> str:
        """Desfaz a conclusão de uma tarefa (ela volta para as pendentes). Numa recorrente,
        desfaz a conclusão de hoje (ou de `on_date`). Só vale para concluídas dos últimos
        30 dias."""
        try:
            task = tasks.reopen(task_id, _date_or_none(on_date))
            return f"OK: reaberta [{task.id}] {describe(task)}"
        except TaskError as exc:
            return f"ERRO: {exc}"

    @tool
    def remove_task(task_id: str) -> str:
        """Remove uma tarefa pendente ou recorrente (use quando o usuário desistiu dela)."""
        try:
            task = tasks.remove(task_id)
            return f"OK: removida [{task.id}] {describe(task)}"
        except TaskError as exc:
            return f"ERRO: {exc}"

    @tool
    def tag_task(task_id: str, add_tags: str = "", remove_tags: str = "") -> str:
        """Acrescenta e/ou tira etiquetas de uma tarefa pendente ou recorrente (listas
        separadas por vírgula, ex.: add_tags='saúde')."""
        try:
            task = tasks.retag(task_id, add_tags, remove_tags)
            return f"OK: etiquetas atualizadas [{task.id}] {describe(task)}"
        except TaskError as exc:
            return f"ERRO: {exc}"

    return [list_tasks, add_task, complete_task, reschedule_task, reopen_task, remove_task, tag_task]
