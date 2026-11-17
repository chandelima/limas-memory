"""Operações sobre a lista de tarefas (memory/todos/todo.md), gravada por código.

A pasta `todos/` é protegida na store: o Curator não escreve nela, só este módulo
(`internal=True`). Assim a estrutura do arquivo nunca depende de um modelo obedecer ao
formato. As mudanças valem na hora e entram no próximo commit do Curator.
"""

import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from typing import Callable

from storage.store import MemoryStore, StoreError, is_protected
from tasks import model
from tasks.model import Completion, Recurrence, Task, TaskError, TaskList

TODO_PATH = "todos/todo.md"  # central: sem data, recorrentes e concluídas recentes
SHARD_DIR = "todos"  # pendentes com data: um arquivo por mês de vencimento (todos/AAAA-MM.md)
SHARD = re.compile(r"todos/\d{4}-\d{2}\.md")
INDEX_PATH = "todos/index.md"  # aponta para o central, os meses e o arquivo das concluídas
ARCHIVE_DIR = "todos/arquivo"  # concluídas antigas, um arquivo por mês (AAAA-MM.md)
ARCHIVE_DAYS = 30  # concluídas mais antigas que isso saem do arquivo principal
ARCHIVE_FILE = re.compile(r"todos/arquivo/\d{4}-\d{2}\.md")


@dataclass
class AgendaItem:
    task: Task
    when: time | None  # hora do dia, se houver
    recurring: bool = False
    done: bool = False  # só para ocorrências de recorrentes já cumpridas no dia


@dataclass
class Agenda:
    today: date
    overdue: list[Task] = field(default_factory=list)  # pendentes com data anterior a hoje
    due_today: list[AgendaItem] = field(default_factory=list)
    upcoming: dict[date, list[AgendaItem]] = field(default_factory=dict)
    undated: list[Task] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.overdue or self.due_today or self.upcoming or self.undated)


def _sort_key(item: AgendaItem):
    return (item.when is None, item.when or time.min)


class TaskStore:
    def __init__(self, store: MemoryStore, today: Callable[[], date] = date.today):
        self.store = store
        self.today = today
        self._lock = threading.RLock()
        self._shard_extras: dict[str, list[str]] = {}  # linhas não reconhecidas, por arquivo mensal

    # ---- leitura e gravação do arquivo ---------------------------------------

    def _shard_paths(self) -> list[str]:
        return sorted(p for p in self.store.list_paths() if SHARD.fullmatch(p))

    def load(self) -> TaskList:
        """Arquivo central (sem data, recorrentes, concluídas recentes) + todos os arquivos
        mensais de pendentes. Uma lista antiga, com tudo no central, também é lida."""
        try:
            tl = model.parse(self.store.read_doc(TODO_PATH))
        except StoreError:  # ainda não existe
            tl = TaskList()
        seen = {t.id for t in tl.pending}
        self._shard_extras = {}
        for path in self._shard_paths():
            shard = model.parse(self.store.read_doc(path))
            self._shard_extras[path] = shard.extras.get(model.SECTION_PENDING, [])
            tl.pending += [t for t in shard.pending if t.id not in seen]
            seen |= {t.id for t in shard.pending}
        return tl

    def _write_if_changed(self, path: str, text: str) -> None:
        try:
            current = self.store.read_doc(path)
        except StoreError:
            current = None
        if current != text:  # só toca nos arquivos que mudaram (diffs pequenos no git)
            self.store.write_doc(path, text, internal=True)

    def _save(self, tl: TaskList) -> None:
        """Grava a lista. Concluídas antigas (mais de ARCHIVE_DAYS dias, ou além do teto
        MAX_DONE) vão para o arquivo mensal ANTES de saírem do arquivo principal, então
        nada se perde, nem se o processo cair no meio."""
        recent = sorted(tl.done, key=lambda c: c.done_on, reverse=True)
        cutoff = self.today() - timedelta(days=ARCHIVE_DAYS)
        old = [c for c in recent if c.done_on < cutoff]
        keep = [c for c in recent if c.done_on >= cutoff]
        old += keep[model.MAX_DONE:]
        if old:
            self._archive(old)
            tl.done = keep[: model.MAX_DONE]
        # pendentes COM data vão para o arquivo do mês de vencimento; o resto fica no central
        dated: dict[str, list[Task]] = {}
        for task in tl.pending:
            if task.due_date:
                dated.setdefault(task.due_date.strftime("%Y-%m"), []).append(task)
        central = TaskList(pending=[t for t in tl.pending if t.due_date is None], recurring=tl.recurring,
                           done=tl.done, extras=tl.extras)
        self._write_if_changed(TODO_PATH, model.render(central))
        for path in sorted(set(self._shard_extras) | {f"{SHARD_DIR}/{m}.md" for m in dated}):
            month = path.removeprefix(f"{SHARD_DIR}/").removesuffix(".md")
            self._write_if_changed(
                path, model.render_month(dated.get(month, []), month, self._shard_extras.get(path, [])))
        self._write_index(central, dated)

    def _write_index(self, central: TaskList, dated: dict[str, list[Task]]) -> None:
        """todos/index.md: aponta para todos os arquivos de tarefas que existem."""
        months = {path.removeprefix(f"{SHARD_DIR}/").removesuffix(".md"): 0 for path in self._shard_paths()}
        months.update({m: len(tasks) for m, tasks in dated.items()})
        archives = {}
        for path in self.store.list_paths():
            if ARCHIVE_FILE.fullmatch(path):
                archives[path.removeprefix(f"{ARCHIVE_DIR}/").removesuffix(".md")] = len(
                    model.parse(self.store.read_doc(path)).done)
        self._write_if_changed(INDEX_PATH, model.render_index(central, months, archives, self.today().strftime("%Y-%m")))

    def _archive(self, done: list[Completion]) -> None:
        by_month: dict[str, list[Completion]] = {}
        for c in done:
            by_month.setdefault(c.done_on.strftime("%Y-%m"), []).append(c)
        for month, items in by_month.items():
            path = f"{ARCHIVE_DIR}/{month}.md"
            try:
                existing = model.parse(self.store.read_doc(path)).done
            except StoreError:
                existing = []
            seen = {(c.id, c.done_on) for c in existing}
            merged = existing + [c for c in items if (c.id, c.done_on) not in seen]
            self.store.write_doc(path, model.render_archive(merged, month), internal=True)

    @staticmethod
    def _new_id(tl: TaskList) -> str:
        taken = tl.ids()
        while (new := secrets.token_hex(2)) in taken:
            pass
        return new

    @staticmethod
    def _find(items: list, task_id: str):
        task_id = task_id.strip().lower().removeprefix("^").removeprefix("[").removesuffix("]")
        return next((i for i in items if i.id == task_id), None)

    # ---- mudanças ------------------------------------------------------------

    def add(self, text: str, due_date: date | None = None, due_time: time | None = None,
            recurrence: Recurrence | None = None, tags=(), ref: str = "") -> Task:
        text, inline = model.split_tags(model.clean_text(text))  # '#etiqueta' no fim do texto também vale
        if not text:
            raise TaskError("a tarefa precisa de um texto")
        tags = tuple(dict.fromkeys((*inline, *model.parse_tags(tags))))
        ref = self._check_ref(ref)
        if recurrence is not None and due_date is not None:
            raise TaskError("tarefa recorrente não tem data; informe só a recorrência (e a hora)")
        if due_time is not None and due_date is None and recurrence is None:
            raise TaskError("uma hora precisa de uma data")
        with self._lock:
            tl = self.load()
            task = Task(self._new_id(tl), text, due_date, due_time, recurrence, tags, ref)
            (tl.recurring if recurrence else tl.pending).append(task)
            self._save(tl)
            return task

    def complete(self, task_id: str, on: date | None = None) -> Completion:
        on = on or self.today()
        with self._lock:
            tl = self.load()
            if task := self._find(tl.pending, task_id):
                tl.pending.remove(task)
                done = Completion(task.id, task.text, task.due_date, task.due_time, on, task.tags, task.ref)
            elif task := self._find(tl.recurring, task_id):
                if not task.recurrence.occurs_on(on):
                    raise TaskError(f"'{task.text}' não ocorre em {on.isoformat()} ({task.recurrence.render()})")
                existing = next((c for c in tl.done if c.id == task.id and c.done_on == on), None)
                if existing:
                    return existing  # já estava cumprida nesse dia
                done = Completion(task.id, task.text, on, task.due_time, on, task.tags, task.ref)
            elif self._find(tl.done, task_id):
                raise TaskError(f"a tarefa {task_id} já está concluída")
            else:
                raise TaskError(f"não existe tarefa com o ID {task_id}")
            tl.done.append(done)
            self._save(tl)
            return done

    def reopen(self, task_id: str, on: date | None = None) -> Task:
        """Desfaz uma conclusão. Tarefa única volta para Pendentes; recorrente perde a
        conclusão do dia `on` (padrão: hoje)."""
        on = on or self.today()
        with self._lock:
            tl = self.load()
            if recurring := self._find(tl.recurring, task_id):
                match = next((c for c in tl.done if c.id == recurring.id and c.done_on == on), None)
                if not match:
                    raise TaskError(f"'{recurring.text}' não estava cumprida em {on.isoformat()}")
                tl.done.remove(match)
                self._save(tl)
                return recurring
            match = self._find(sorted(tl.done, key=lambda c: c.done_on, reverse=True), task_id)
            if not match:
                raise TaskError(f"não existe tarefa concluída com o ID {task_id}")
            tl.done.remove(match)
            task = Task(match.id, match.text, match.due_date, match.due_time, tags=match.tags, ref=match.ref)
            tl.pending.append(task)
            self._save(tl)
            return task

    def reschedule(self, task_id: str, due_date: date, due_time: time | None = None) -> Task:
        with self._lock:
            tl = self.load()
            task = self._find(tl.pending, task_id)
            if task is None:
                if self._find(tl.recurring, task_id):
                    raise TaskError("tarefa recorrente: remova-a e crie outra com a nova recorrência")
                raise TaskError(f"não existe tarefa pendente com o ID {task_id}")
            task.due_date, task.due_time = due_date, due_time
            self._save(tl)
            return task

    def _check_ref(self, ref: str) -> str:
        """Referência válida: caminho de um documento que EXISTE na memória e não é de uma
        pasta gerenciada por código (todos/, anexos/). Vazio = sem referência."""
        ref = model.normalize_ref(ref)
        if not ref:
            return ""
        if is_protected(ref):
            raise TaskError("não referencie arquivos de todos/ ou anexos/; use o documento do assunto")
        try:
            self.store.read_doc(ref)
        except StoreError:
            raise TaskError(f"o documento '{ref}' não existe na memória (confira o caminho com list_memory)") from None
        return ref

    def set_ref(self, task_id: str, ref: str) -> Task:
        """Liga uma tarefa pendente ou recorrente a um documento da memória ('' remove a ligação)."""
        ref = self._check_ref(ref)
        with self._lock:
            tl = self.load()
            task = self._find(tl.pending, task_id) or self._find(tl.recurring, task_id)
            if task is None:
                raise TaskError(f"não existe tarefa pendente ou recorrente com o ID {task_id}")
            task.ref = ref
            self._save(tl)
            return task

    def retag(self, task_id: str, add=(), remove=()) -> Task:
        """Acrescenta e/ou tira etiquetas de uma tarefa pendente ou recorrente."""
        add_tags, remove_tags = model.parse_tags(add), set(model.parse_tags(remove))
        with self._lock:
            tl = self.load()
            task = self._find(tl.pending, task_id) or self._find(tl.recurring, task_id)
            if task is None:
                raise TaskError(f"não existe tarefa pendente ou recorrente com o ID {task_id}")
            task.tags = tuple(t for t in dict.fromkeys((*task.tags, *add_tags)) if t not in remove_tags)
            self._save(tl)
            return task

    def tags(self) -> dict[str, int]:
        """Etiquetas em uso (pendentes e recorrentes) e quantas tarefas tem cada uma."""
        tl = self.load()
        counts: dict[str, int] = {}
        for task in (*tl.pending, *tl.recurring):
            for tag in task.tags:
                counts[tag] = counts.get(tag, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def remove(self, task_id: str) -> Task:
        with self._lock:
            tl = self.load()
            for items in (tl.pending, tl.recurring):
                if task := self._find(items, task_id):
                    items.remove(task)
                    self._save(tl)
                    return task
            raise TaskError(f"não existe tarefa pendente ou recorrente com o ID {task_id}")

    # ---- consultas -----------------------------------------------------------

    def agenda(self, on: date | None = None, horizon_days: int = 3, tag: str = "") -> Agenda:
        """Atrasadas, de hoje (com recorrentes) e dos próximos `horizon_days` dias.
        Com `tag`, só as tarefas com essa etiqueta."""
        on = on or self.today()
        tl = self.load()
        if tag:
            wanted = model.normalize_tag(tag)
            tl.pending = [t for t in tl.pending if wanted in t.tags]
            tl.recurring = [t for t in tl.recurring if wanted in t.tags]
        agenda = Agenda(today=on)
        agenda.overdue = sorted((t for t in tl.pending if t.due_date and t.due_date < on),
                                key=lambda t: (t.due_date, t.due_time or time.min))
        agenda.undated = [t for t in tl.pending if t.due_date is None]
        for offset in range(horizon_days + 1):
            day = on + timedelta(days=offset)
            items = [AgendaItem(t, t.due_time) for t in tl.pending if t.due_date == day]
            for rec in tl.recurring:
                # diárias só em "hoje": repetir em todo dia dos "próximos dias" seria ruído
                if rec.recurrence.occurs_on(day) and (offset == 0 or rec.recurrence.kind != "daily"):
                    cumprida = any(c.id == rec.id and c.done_on == day for c in tl.done)
                    items.append(AgendaItem(rec, rec.due_time, recurring=True, done=cumprida))
            items.sort(key=_sort_key)
            if offset == 0:
                agenda.due_today = items
            elif items:
                agenda.upcoming[day] = items
        return agenda
