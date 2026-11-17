"""Histórico da conversa em arquivo JSON (fora de memory/, não versionado).

Guarda todas as mensagens, cada uma com um estado, e é gravado de forma atômica a cada
alteração: a mensagem do usuário é persistida assim que chega, antes de qualquer
processamento, então sobrevive a uma queda do bot e pode ser retomada ao reiniciar.

Estados de uma mensagem do usuário:
    received  chegou, ainda sem resposta
    reading   é um arquivo ainda sendo lido
    ready     conteúdo pronto, aguardando a resposta
    answered  respondida (estado final)
    failed    não deu para ler/responder (estado final)
As respostas do assistente nascem `answered` e guardam `reply_to` (a mensagem do usuário a
que respondem), o que mantém a ordem da conversa mesmo com várias mensagens em seguida.
Cada mensagem tem também a marca `curated`: o Curator já processou (ou não).
"""

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

ROLES = ("usuario", "assistente")

RECEIVED, READING, READY, ANSWERED, FAILED = "received", "reading", "ready", "answered", "failed"
IN_PROGRESS = (RECEIVED, READING, READY)
FINAL = (ANSWERED, FAILED)
STATUSES = IN_PROGRESS + FINAL


@dataclass
class Message:
    id: int
    role: str
    text: str
    at: str  # ISO 8601 com fuso
    attachments: list[str] = field(default_factory=list)  # IDs de anexos (anexos/<id>.md)
    status: str = ANSWERED  # arquivos antigos, sem o campo, valem como já respondidos
    reply_to: int | None = None  # assistente: id da mensagem do usuário que ele responde
    curated: bool = False


def order_key(m: Message) -> tuple[int, int]:
    """Ordem da conversa: a resposta vem logo depois da mensagem a que responde."""
    if m.role == "assistente" and m.reply_to is not None:
        return (m.reply_to, 1)
    return (m.id, 0 if m.role == "usuario" else 1)


class ConversationLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._messages: list[Message] = []
        self._next_id = 1
        self._load()

    # ---- persistência -----------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._messages = [Message(**m) for m in data["messages"]]
            self._next_id = int(data["next_id"])
            legacy_upto = data.get("curated_upto")  # formato anterior: marca d'água por id
            if legacy_upto:
                for m in self._messages:
                    if m.id <= int(legacy_upto):
                        m.curated = True
        except (ValueError, KeyError, TypeError):
            # Arquivo corrompido: guarda de lado (para inspeção) e começa limpo.
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.path.replace(self.path.with_name(f"{self.path.name}.corrupt-{stamp}"))
            self._messages, self._next_id = [], 1

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"messages": [asdict(m) for m in self._messages], "next_id": self._next_id}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # troca atômica

    # ---- escrita ----------------------------------------------------------

    def append(
        self,
        role: str,
        text: str,
        at: str | None = None,
        attachments: list[str] | None = None,
        status: str = ANSWERED,
        reply_to: int | None = None,
    ) -> Message:
        if role not in ROLES:
            raise ValueError(f"role deve ser um de {ROLES}")
        if status not in STATUSES:
            raise ValueError(f"status deve ser um de {STATUSES}")
        text = text.strip()
        if not text:
            raise ValueError("Mensagem vazia")
        with self._lock:
            msg = Message(
                id=self._next_id,
                role=role,
                text=text,
                at=at or datetime.now().astimezone().isoformat(timespec="seconds"),
                attachments=list(attachments or []),
                status=status,
                reply_to=reply_to,
            )
            self._next_id += 1
            self._messages.append(msg)
            self._save()
            return msg

    def update(self, msg_id: int, text: str | None = None, status: str | None = None) -> Message:
        """Atualiza o texto e/ou o estado de uma mensagem (KeyError se não existir)."""
        if status is not None and status not in STATUSES:
            raise ValueError(f"status deve ser um de {STATUSES}")
        with self._lock:
            msg = self._find(msg_id)
            if text is not None:
                msg.text = text.strip() or msg.text
            if status is not None:
                msg.status = status
            self._save()
            return msg

    def mark_curated(self, ids) -> None:
        """Marca as mensagens indicadas como já processadas pelo Curator."""
        wanted = set(ids)
        with self._lock:
            changed = False
            for m in self._messages:
                if m.id in wanted and not m.curated:
                    m.curated, changed = True, True
            if changed:
                self._save()

    def prune(self, keep: int) -> int:
        """Descarta mensagens já curadas, mantendo as `keep` mais recentes da conversa.
        Mensagens não curadas (inclusive as em andamento) nunca são removidas."""
        with self._lock:
            ordered = sorted(self._messages, key=order_key)
            keep_ids = {m.id for m in ordered[-keep:]} if keep > 0 else set()
            kept = [m for m in self._messages if not m.curated or m.id in keep_ids]
            removed = len(self._messages) - len(kept)
            if removed:
                self._messages = kept
                self._save()
            return removed

    # ---- leitura ----------------------------------------------------------

    def _find(self, msg_id: int) -> Message:
        for m in self._messages:
            if m.id == msg_id:
                return m
        raise KeyError(f"mensagem {msg_id} não existe")

    def get(self, msg_id: int) -> Message:
        with self._lock:
            return self._find(msg_id)

    def messages(self) -> list[Message]:
        """Em ordem de chegada (por id)."""
        with self._lock:
            return list(self._messages)

    def recent(self, n: int) -> list[Message]:
        """As `n` últimas, na ordem da conversa."""
        with self._lock:
            return sorted(self._messages, key=order_key)[-n:] if n > 0 else []

    def history(self, upto_id: int, n: int) -> list[Message]:
        """Até `n` mensagens, na ordem da conversa, terminando na mensagem `upto_id` do
        usuário. Inclui só o que já está resolvido (respondido/falhou) mais a própria
        `upto_id`; mensagens em andamento e posteriores ficam de fora."""
        with self._lock:
            chosen = []
            for m in self._messages:
                if m.role == "usuario":
                    if m.id == upto_id or (m.id < upto_id and m.status in FINAL):
                        chosen.append(m)
                else:  # resposta do assistente: entra se responde a algo anterior a `upto_id`
                    ref = m.reply_to if m.reply_to is not None else m.id
                    if ref < upto_id:
                        chosen.append(m)
            return sorted(chosen, key=order_key)[-n:] if n > 0 else []

    def pending(self) -> list[Message]:
        """Mensagens prontas para o Curator: ainda não curadas e em estado final."""
        with self._lock:
            return sorted((m for m in self._messages if not m.curated and m.status in FINAL), key=order_key)

    def curated_context(self, n: int) -> list[Message]:
        """As `n` últimas mensagens já curadas (contexto para a próxima curadoria)."""
        with self._lock:
            done = sorted((m for m in self._messages if m.curated), key=order_key)
            return done[-n:] if n > 0 else []

    def unfinished(self) -> list[Message]:
        """Mensagens do usuário que ainda não tiveram resposta (para retomar ao reiniciar)."""
        with self._lock:
            return [m for m in self._messages if m.role == "usuario" and m.status in IN_PROGRESS]

    def last_message_at(self) -> datetime | None:
        with self._lock:
            return datetime.fromisoformat(self._messages[-1].at) if self._messages else None
