"""Registro dos arquivos enviados pelo usuário, como documentos Markdown em memory/anexos/.

Cada anexo é um `.md` (`anexos/<id>.md`) com os dados no frontmatter. A memória cita o
arquivo por um ID curto (`anexo:ab12cd34`); este registro liga o ID ao `file_id` do
Telegram e à mensagem original. Quem grava é o CÓDIGO do bot, no momento em que o arquivo
chega, e não o Curator: a pasta `anexos/` é protegida na store (o Curator não consegue
alterá-la, o que evitaria um ID trocado por engano) e fica fora de list/search/índice.
Vai para o GitHub junto com a memória, no commit seguinte do Curator.
"""

import re
import secrets
import threading
from dataclasses import asdict, dataclass, fields
from datetime import datetime

import yaml

from storage import markdown as md
from storage.store import MemoryStore, StoreError

PREFIX = "anexo:"
FOLDER = "anexos"
_ID = re.compile(r"^[0-9a-f]{4,32}$")


@dataclass
class Attachment:
    id: str  # curto e opaco, ex.: "ab12cd34"
    file_id: str  # Telegram: reenvia o arquivo sem baixar de novo
    file_unique_id: str
    kind: str  # "document" ou "photo"
    chat_id: int
    message_id: int  # mensagem original no chat (para responder citando-a)
    filename: str
    mime: str
    size: int
    caption: str
    received_at: str  # ISO 8601 com fuso

    @property
    def ref(self) -> str:
        return f"{PREFIX}{self.id}"


def normalize_id(value: str) -> str:
    """Aceita 'ab12cd34', 'anexo:ab12cd34' e variações de caixa/espaço."""
    value = value.strip().lower()
    return value.removeprefix(PREFIX).strip()


def _path(attachment_id: str) -> str:
    return f"{FOLDER}/{attachment_id}.md"


def _render(item: Attachment) -> str:
    description = " ".join(f"Arquivo {item.filename} ({item.mime}), enviado em {item.received_at}".split())
    meta = {"description": description, "anexo_id": item.id, **{k: v for k, v in asdict(item).items() if k != "id"}}
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=False).strip()
    lines = [
        f"# Anexo {item.id}",
        "",
        f"- Arquivo: {' '.join(item.filename.split())}",
        f"- Tipo: {item.mime}",
        f"- Enviado em: {item.received_at}",
    ]
    if item.caption:
        lines.append(f"- Legenda: {' '.join(item.caption.split())}")
    lines += ["", f"Referência: {item.ref}"]
    return f"---\n{front}\n---\n" + "\n".join(lines) + "\n"


def _parse(text: str) -> Attachment | None:
    try:
        meta, _ = md.parse_frontmatter(text)
        names = {f.name for f in fields(Attachment)}
        values = {k: meta[k] for k in names if k != "id"}
        return Attachment(id=str(meta["anexo_id"]), **values)
    except (md.MarkdownError, KeyError, TypeError):
        return None


class AttachmentRegistry:
    def __init__(self, store: MemoryStore):
        self.store = store
        self._lock = threading.RLock()

    def add(
        self,
        *,
        file_id: str,
        file_unique_id: str,
        kind: str,
        chat_id: int,
        message_id: int,
        filename: str,
        mime: str,
        size: int = 0,
        caption: str = "",
        received_at: str | None = None,
    ) -> Attachment:
        with self._lock:
            new_id = secrets.token_hex(4)
            while (self.store.root / _path(new_id)).exists():
                new_id = secrets.token_hex(4)
            item = Attachment(
                id=new_id, file_id=file_id, file_unique_id=file_unique_id, kind=kind,
                chat_id=chat_id, message_id=message_id, filename=filename, mime=mime,
                size=size, caption=caption.strip(),
                received_at=received_at or datetime.now().astimezone().isoformat(timespec="seconds"),
            )
            self.store.write_doc(_path(new_id), _render(item), internal=True)
            return item

    def get(self, attachment_id: str) -> Attachment | None:
        attachment_id = normalize_id(attachment_id)
        if not _ID.match(attachment_id):  # nada de caminhos: só IDs hexadecimais
            return None
        try:
            return _parse(self.store.read_doc(_path(attachment_id)))
        except StoreError:
            return None

    def all(self) -> list[Attachment]:
        with self._lock:
            items = []
            for path in self.store.list_paths():
                if path.startswith(f"{FOLDER}/"):
                    item = _parse(self.store.read_doc(path))
                    if item:
                        items.append(item)
            return sorted(items, key=lambda a: a.received_at)
