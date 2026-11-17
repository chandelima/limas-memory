"""Registry of the files sent by the user, as Markdown documents in memory/attachments/.

Each attachment is an `.md` (`attachments/<id>.md`) with the data in the frontmatter. The memory
cites the file by a short ID (`attachment:ab12cd34`); this registry links the ID to the
Telegram `file_id` and to the original message. The bot's CODE writes it, at the moment the
file arrives, not the Curator: the `attachments/` folder is protected in the store (the
Curator cannot change it, which prevents an ID being swapped by mistake) and stays out of
list/search/index. It goes to the remote along with the memory, in the Curator's next commit.
"""

import re
import secrets
import threading
from dataclasses import asdict, dataclass, fields
from datetime import datetime

import yaml

from storage import markdown as md
from storage.store import MemoryStore, StoreError

PREFIX = "attachment:"
FOLDER = "attachments"
_ID = re.compile(r"^[0-9a-f]{4,32}$")


@dataclass
class Attachment:
    id: str  # short and opaque, e.g. "ab12cd34"
    file_id: str  # Telegram: sends the file again without downloading it
    file_unique_id: str
    kind: str  # "document" or "photo"
    chat_id: int
    message_id: int  # original message in the chat (to reply quoting it)
    filename: str
    mime: str
    size: int
    caption: str
    received_at: str  # ISO 8601 with time zone

    @property
    def ref(self) -> str:
        return f"{PREFIX}{self.id}"


def normalize_id(value: str) -> str:
    """Accept 'ab12cd34', 'attachment:ab12cd34' and case/whitespace variations."""
    value = value.strip().lower()
    return value.removeprefix(PREFIX).strip()


def _path(attachment_id: str) -> str:
    return f"{FOLDER}/{attachment_id}.md"


def _render(item: Attachment) -> str:
    description = " ".join(f"File {item.filename} ({item.mime}), sent on {item.received_at}".split())
    meta = {"description": description, "attachment_id": item.id, **{k: v for k, v in asdict(item).items() if k != "id"}}
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=False).strip()
    lines = [
        f"# Attachment {item.id}",
        "",
        f"- File: {' '.join(item.filename.split())}",
        f"- Type: {item.mime}",
        f"- Sent on: {item.received_at}",
    ]
    if item.caption:
        lines.append(f"- Caption: {' '.join(item.caption.split())}")
    lines += ["", f"Reference: {item.ref}"]
    return f"---\n{front}\n---\n" + "\n".join(lines) + "\n"


def _parse(text: str) -> Attachment | None:
    try:
        meta, _ = md.parse_frontmatter(text)
        names = {f.name for f in fields(Attachment)}
        values = {k: meta[k] for k in names if k != "id"}
        return Attachment(id=str(meta["attachment_id"]), **values)
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
            return self.put(item)

    def put(self, item: Attachment) -> Attachment:
        """Write an attachment with its ID already set (used when migrating the old registry)."""
        if not _ID.match(item.id):
            raise ValueError(f"invalid attachment ID: {item.id!r}")
        with self._lock:
            self.store.write_doc(_path(item.id), _render(item), internal=True)
            return item

    def get(self, attachment_id: str) -> Attachment | None:
        attachment_id = normalize_id(attachment_id)
        if not _ID.match(attachment_id):  # no paths: hexadecimal IDs only
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
