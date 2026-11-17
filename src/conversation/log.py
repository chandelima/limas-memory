"""Conversation history in a JSON file (outside memory/, not versioned).

Keeps every message, each with a status, and is written atomically on every change:
the user's message is persisted as soon as it arrives, before any processing, so it
survives a bot crash and can be resumed on restart.

Statuses of a user message:
    received  arrived, no answer yet
    reading   a file that is still being read
    ready     content ready, waiting for the answer
    answered  answered (final state)
    failed    could not be read/answered (final state)
Assistant replies are born `answered` and keep `reply_to` (the user message they answer),
which preserves the conversation order even with several messages in a row.
Each message also has the `curated` flag: whether the Curator has processed it.
"""

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

ROLES = ("user", "assistant")

RECEIVED, READING, READY, ANSWERED, FAILED = "received", "reading", "ready", "answered", "failed"
IN_PROGRESS = (RECEIVED, READING, READY)
FINAL = (ANSWERED, FAILED)
STATUSES = IN_PROGRESS + FINAL


@dataclass
class Message:
    id: int
    role: str
    text: str
    at: str  # ISO 8601 with time zone
    attachments: list[str] = field(default_factory=list)  # attachment IDs (attachments/<id>.md)
    status: str = ANSWERED  # old files without the field count as already answered
    reply_to: int | None = None  # assistant: id of the user message it answers
    curated: bool = False


def order_key(m: Message) -> tuple[int, int]:
    """Conversation order: a reply comes right after the message it answers."""
    if m.role == "assistant" and m.reply_to is not None:
        return (m.reply_to, 1)
    return (m.id, 0 if m.role == "user" else 1)


class ConversationLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._messages: list[Message] = []
        self._next_id = 1
        self._load()

    # ---- persistence ------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._messages = [Message(**m) for m in data["messages"]]
            self._next_id = int(data["next_id"])
        except (ValueError, KeyError, TypeError):
            # Corrupt file: set it aside (for inspection) and start clean.
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.path.replace(self.path.with_name(f"{self.path.name}.corrupt-{stamp}"))
            self._messages, self._next_id = [], 1

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"messages": [asdict(m) for m in self._messages], "next_id": self._next_id}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic swap

    # ---- writing ----------------------------------------------------------

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
            raise ValueError(f"role must be one of {ROLES}")
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        text = text.strip()
        if not text:
            raise ValueError("Empty message")
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
        """Update a message's text and/or status (KeyError if it does not exist)."""
        if status is not None and status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        with self._lock:
            msg = self._find(msg_id)
            if text is not None:
                msg.text = text.strip() or msg.text
            if status is not None:
                msg.status = status
            self._save()
            return msg

    def mark_curated(self, ids) -> None:
        """Mark the given messages as already processed by the Curator."""
        wanted = set(ids)
        with self._lock:
            changed = False
            for m in self._messages:
                if m.id in wanted and not m.curated:
                    m.curated, changed = True, True
            if changed:
                self._save()

    def prune(self, keep: int) -> int:
        """Discard already-curated messages, keeping the `keep` most recent of the conversation.
        Messages not yet curated (including those in progress) are never removed."""
        with self._lock:
            ordered = sorted(self._messages, key=order_key)
            keep_ids = {m.id for m in ordered[-keep:]} if keep > 0 else set()
            kept = [m for m in self._messages if not m.curated or m.id in keep_ids]
            removed = len(self._messages) - len(kept)
            if removed:
                self._messages = kept
                self._save()
            return removed

    # ---- reading ----------------------------------------------------------

    def _find(self, msg_id: int) -> Message:
        for m in self._messages:
            if m.id == msg_id:
                return m
        raise KeyError(f"message {msg_id} does not exist")

    def get(self, msg_id: int) -> Message:
        with self._lock:
            return self._find(msg_id)

    def messages(self) -> list[Message]:
        """In arrival order (by id)."""
        with self._lock:
            return list(self._messages)

    def recent(self, n: int) -> list[Message]:
        """The last `n`, in conversation order."""
        with self._lock:
            return sorted(self._messages, key=order_key)[-n:] if n > 0 else []

    def history(self, upto_id: int, n: int) -> list[Message]:
        """Up to `n` messages, in conversation order, ending at the user message `upto_id`.
        Includes only already-answered turns plus `upto_id` itself: messages in progress,
        discarded or failed (with no reply) and later ones are left out."""
        with self._lock:
            chosen = []
            for m in self._messages:
                if m.role == "user":
                    if m.id == upto_id or (m.id < upto_id and m.status == ANSWERED):
                        chosen.append(m)
                else:  # assistant reply: included if it answers something before `upto_id`
                    ref = m.reply_to if m.reply_to is not None else m.id
                    if ref < upto_id:
                        chosen.append(m)
            return sorted(chosen, key=order_key)[-n:] if n > 0 else []

    def pending(self) -> list[Message]:
        """Messages ready for the Curator: not yet curated and in a final state."""
        with self._lock:
            return sorted((m for m in self._messages if not m.curated and m.status in FINAL), key=order_key)

    def curated_context(self, n: int) -> list[Message]:
        """The last `n` already-curated messages (context for the next curation)."""
        with self._lock:
            done = sorted((m for m in self._messages if m.curated), key=order_key)
            return done[-n:] if n > 0 else []

    def unfinished(self) -> list[Message]:
        """User messages that have not been answered yet (to resume after a restart)."""
        with self._lock:
            return [m for m in self._messages if m.role == "user" and m.status in IN_PROGRESS]

    def last_message_at(self) -> datetime | None:
        with self._lock:
            return datetime.fromisoformat(self._messages[-1].at) if self._messages else None
