"""Histórico da conversa em arquivo JSON (fora de memory/, não versionado).

Guarda todas as mensagens e um marcador `curated_upto`: o id da última mensagem que o
Curator já processou. O que vem depois é "pendente" e será curado quando houver
inatividade. O arquivo é gravado de forma atômica a cada alteração, então sobrevive a
reinícios do bot sem perder mensagens ainda não curadas.
"""

import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

ROLES = ("usuario", "assistente")


@dataclass
class Message:
    id: int
    role: str
    text: str
    at: str  # ISO 8601 com fuso


class ConversationLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._messages: list[Message] = []
        self._next_id = 1
        self._curated_upto = 0
        self._load()

    # ---- persistência -----------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._messages = [Message(**m) for m in data["messages"]]
            self._next_id = int(data["next_id"])
            self._curated_upto = int(data["curated_upto"])
        except (ValueError, KeyError, TypeError):
            # Arquivo corrompido: guarda de lado (para inspeção) e começa limpo.
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.path.replace(self.path.with_name(f"{self.path.name}.corrupt-{stamp}"))
            self._messages, self._next_id, self._curated_upto = [], 1, 0

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "messages": [asdict(m) for m in self._messages],
            "next_id": self._next_id,
            "curated_upto": self._curated_upto,
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # troca atômica

    # ---- escrita ----------------------------------------------------------

    def append(self, role: str, text: str, at: str | None = None) -> Message:
        if role not in ROLES:
            raise ValueError(f"role deve ser um de {ROLES}")
        text = text.strip()
        if not text:
            raise ValueError("Mensagem vazia")
        with self._lock:
            msg = Message(
                id=self._next_id,
                role=role,
                text=text,
                at=at or datetime.now().astimezone().isoformat(timespec="seconds"),
            )
            self._next_id += 1
            self._messages.append(msg)
            self._save()
            return msg

    def mark_curated(self, upto_id: int) -> None:
        """Marca como curadas todas as mensagens até `upto_id` (nunca retrocede)."""
        with self._lock:
            if upto_id > self._curated_upto:
                self._curated_upto = upto_id
                self._save()

    def prune(self, keep: int) -> int:
        """Descarta mensagens já curadas, mantendo as `keep` mais recentes do total.
        Mensagens pendentes nunca são removidas. Devolve quantas saíram."""
        with self._lock:
            keep_ids = {m.id for m in self._messages[-keep:]} if keep > 0 else set()
            kept = [
                m for m in self._messages
                if m.id > self._curated_upto or m.id in keep_ids
            ]
            removed = len(self._messages) - len(kept)
            if removed:
                self._messages = kept
                self._save()
            return removed

    # ---- leitura ----------------------------------------------------------

    @property
    def curated_upto(self) -> int:
        return self._curated_upto

    def messages(self) -> list[Message]:
        with self._lock:
            return list(self._messages)

    def recent(self, n: int) -> list[Message]:
        with self._lock:
            return list(self._messages[-n:]) if n > 0 else []

    def pending(self) -> list[Message]:
        with self._lock:
            return [m for m in self._messages if m.id > self._curated_upto]

    def before(self, msg_id: int, n: int) -> list[Message]:
        """Até `n` mensagens imediatamente anteriores a `msg_id` (para contexto)."""
        with self._lock:
            older = [m for m in self._messages if m.id < msg_id]
            return older[-n:] if n > 0 else []

    def last_message_at(self) -> datetime | None:
        with self._lock:
            return datetime.fromisoformat(self._messages[-1].at) if self._messages else None
