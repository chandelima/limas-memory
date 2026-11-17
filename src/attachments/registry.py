"""Registro local dos arquivos enviados pelo usuário (fora de memory/, não versionado).

A memória guarda só um ID opaco (`anexo:ab12cd34`); este registro liga o ID ao arquivo no
Telegram (`file_id`) e à mensagem original. O `file_id` é uma chave de acesso ao arquivo
pelo bot, por isso nunca vai para o repositório da memória.
"""

import json
import os
import secrets
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

PREFIX = "anexo:"


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


class AttachmentRegistry:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._items: dict[str, Attachment] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._items = {a["id"]: Attachment(**a) for a in data["attachments"]}
        except (ValueError, KeyError, TypeError):
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.path.replace(self.path.with_name(f"{self.path.name}.corrupt-{stamp}"))
            self._items = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"attachments": [asdict(a) for a in self._items.values()]}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

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
            while new_id in self._items:
                new_id = secrets.token_hex(4)
            item = Attachment(
                id=new_id, file_id=file_id, file_unique_id=file_unique_id, kind=kind,
                chat_id=chat_id, message_id=message_id, filename=filename, mime=mime,
                size=size, caption=caption.strip(),
                received_at=received_at or datetime.now().astimezone().isoformat(timespec="seconds"),
            )
            self._items[new_id] = item
            self._save()
            return item

    def get(self, attachment_id: str) -> Attachment | None:
        with self._lock:
            return self._items.get(normalize_id(attachment_id))

    def all(self) -> list[Attachment]:
        with self._lock:
            return list(self._items.values())
