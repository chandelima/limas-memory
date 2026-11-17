"""Memory index (index.md) and relative-link validation.

The index is generated from each document's frontmatter `description`:
the chat agent reads only the index and opens just the document it needs.
"""

from dataclasses import dataclass
from posixpath import dirname, normpath
from urllib.parse import quote, unquote

from storage import markdown as md
from storage.store import MemoryStore, StoreError

INDEX_PATH = "index.md"
ROOT_GROUP = "General"


@dataclass
class BrokenLink:
    doc: str
    target: str


def _title(store: MemoryStore, path: str) -> str:
    try:
        _, body = md.parse_frontmatter(store.read_doc(path))
    except (StoreError, md.MarkdownError):
        body = ""
    return md.first_heading(body) or path.rsplit("/", 1)[-1].removesuffix(".md")


def build_index(store: MemoryStore) -> str:
    groups: dict[str, list[str]] = {}
    for doc in store.list_docs():
        folder = doc.path.split("/", 1)[0] if "/" in doc.path else ROOT_GROUP
        link = f"[{_title(store, doc.path)}]({quote(doc.path)})"
        line = f"- {link} — {doc.description}" if doc.description else f"- {link}"
        groups.setdefault(folder, []).append(line)

    lines = [
        "---",
        "description: Memory index, generated from the description of each document",
        "---",
        "# Index",
        "",
    ]
    if not groups:
        lines.append("_No documents yet._")
    for folder in sorted(groups, key=lambda f: (f != ROOT_GROUP, f.lower())):
        lines += [f"## {folder}", "", *groups[folder], ""]
    return "\n".join(lines).rstrip() + "\n"


def update_index(store: MemoryStore) -> str:
    """Regenerate and write index.md. Return the written path."""
    return store.write_doc(INDEX_PATH, build_index(store)).path


def find_broken_links(store: MemoryStore) -> list[BrokenLink]:
    """Relative Markdown links that point to non-existent documents."""
    existing = set(store.list_paths())
    broken = []
    for path in sorted(existing):
        for raw in md.extract_links(store.read_doc(path)):
            target = unquote(raw.split("#", 1)[0])
            if not target or md.is_external(raw):
                continue  # local anchor or external link
            resolved = normpath(f"{dirname(path)}/{target}" if dirname(path) else target)
            if resolved not in existing:
                broken.append(BrokenLink(path, raw))
    return broken
