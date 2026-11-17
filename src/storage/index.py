"""Índice da memória (index.md) e validação de links relativos.

O índice é gerado a partir da `description` do frontmatter de cada documento:
o agente de resposta lê só o índice e abre apenas o documento de que precisa.
"""

from dataclasses import dataclass
from posixpath import dirname, normpath
from urllib.parse import quote, unquote

from storage import markdown as md
from storage.store import MemoryStore, StoreError

INDEX_PATH = "index.md"
ROOT_GROUP = "Geral"


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
        "description: Índice da memória, gerado a partir da description de cada documento",
        "---",
        "# Índice",
        "",
    ]
    if not groups:
        lines.append("_Nenhum documento ainda._")
    for folder in sorted(groups, key=lambda f: (f != ROOT_GROUP, f.lower())):
        lines += [f"## {folder}", "", *groups[folder], ""]
    return "\n".join(lines).rstrip() + "\n"


def update_index(store: MemoryStore) -> str:
    """Regenera e grava index.md. Devolve o caminho gravado."""
    return store.write_doc(INDEX_PATH, build_index(store)).path


def find_broken_links(store: MemoryStore) -> list[BrokenLink]:
    """Links Markdown relativos que apontam para documentos inexistentes."""
    existing = set(store.list_paths())
    broken = []
    for path in sorted(existing):
        for raw in md.extract_links(store.read_doc(path)):
            target = unquote(raw.split("#", 1)[0])
            if not target or md.is_external(raw):
                continue  # âncora local ou link externo
            resolved = normpath(f"{dirname(path)}/{target}" if dirname(path) else target)
            if resolved not in existing:
                broken.append(BrokenLink(path, raw))
    return broken
