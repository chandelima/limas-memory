"""Memory storage layer: Markdown only, confined to MEMORY_DIR.

No LLM here. This module enforces the mechanical rules:
- nothing outside the memory folder, only .md files;
- every document has frontmatter with a one-line `description`;
- no secrets (keys/tokens) are written;
- warns when a document grows too large (the right fix is to split, not summarize).
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from storage import markdown as md

MAX_LINES = 300  # above this, warn to split the document
ROOT_FOLDER = "(root)"  # name of the "folder" for documents sitting at the root
# Folders managed by code (not by the Curator): only `write_doc(..., internal=True)` writes
# to them, and they stay out of list_docs, search_docs and the index. Reading (read_doc) is allowed.
PROTECTED_FOLDERS = ("attachments", "todos")


def is_protected(rel_path: str) -> bool:
    return rel_path.split("/", 1)[0] in PROTECTED_FOLDERS

_SECRET_PATTERNS = {
    "sk- key": re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    "AWS key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "secret assignment": re.compile(
        r"(?i)\b(api[_-]?key|secret|token|password|senha)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+=]{12,}"
    ),
}


class StoreError(ValueError):
    """Operation refused by the storage layer."""


@dataclass
class Doc:
    path: str
    description: str


@dataclass
class SearchHit:
    path: str
    line: int
    text: str


@dataclass
class WriteResult:
    path: str
    created: bool
    warnings: list[str] = field(default_factory=list)


def _parse(content: str) -> tuple[dict, str]:
    try:
        return md.parse_frontmatter(content)
    except md.MarkdownError as exc:
        raise StoreError(str(exc)) from exc


class MemoryStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    # ---- paths ---------------------------------------------------------

    def _resolve(self, rel_path: str) -> Path:
        if not rel_path or not isinstance(rel_path, str):
            raise StoreError("Empty path")
        rel = Path(rel_path)
        if rel.is_absolute() or rel.drive:
            raise StoreError("Use paths relative to the memory folder")
        if rel.suffix.lower() != ".md":
            raise StoreError("Only .md files are allowed")
        if any(part == ".git" for part in rel.parts):
            raise StoreError("Access to .git is not allowed")
        full = (self.root / rel).resolve()
        if not full.is_relative_to(self.root):
            raise StoreError("Path outside the memory folder")
        return full

    def _rel(self, full: Path) -> str:
        return full.relative_to(self.root).as_posix()

    def _iter_docs(self):
        if not self.root.exists():
            return
        for p in sorted(self.root.rglob("*.md")):
            if ".git" in p.relative_to(self.root).parts:
                continue
            yield p

    # ---- reading ----------------------------------------------------------

    def list_paths(self) -> list[str]:
        """Relative paths of all .md files, including index.md."""
        return [self._rel(p) for p in self._iter_docs()]

    def _doc_paths(self, folder: str = "") -> list[str]:
        """Document paths (without index.md), optionally for one folder."""
        paths = [r for r in self.list_paths() if r != "index.md" and not is_protected(r)]
        folder = folder.strip().strip("/")
        if not folder:
            return paths
        if folder == ROOT_FOLDER:
            return [r for r in paths if "/" not in r]
        return [r for r in paths if r.startswith(folder + "/")]

    def folder_counts(self) -> dict[str, int]:
        """Number of documents per top-level folder (root = '(root)')."""
        counts: dict[str, int] = {}
        for r in self._doc_paths():
            top = r.split("/", 1)[0] if "/" in r else ROOT_FOLDER
            counts[top] = counts.get(top, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (kv[0] != ROOT_FOLDER, kv[0].lower())))

    def count_docs(self, folder: str = "") -> int:
        return len(self._doc_paths(folder))

    def list_docs(
        self, folder: str = "", offset: int = 0, limit: int | None = None
    ) -> list[Doc]:
        """Documents (except index.md) with each one's `description`.

        `folder` filters by folder ('people', 'people/friends' or '(root)');
        `offset`/`limit` paginate. Only the documents on the page are read from disk.
        """
        paths = self._doc_paths(folder)
        paths = paths[offset : None if limit is None else offset + limit]
        docs = []
        for rel in paths:
            try:
                meta, _ = _parse((self.root / rel).read_text(encoding="utf-8"))
            except StoreError:
                meta = {}
            docs.append(Doc(rel, str(meta.get("description", "")).strip()))
        return docs

    def read_doc(self, rel_path: str) -> str:
        full = self._resolve(rel_path)
        if not full.is_file():
            raise StoreError(f"Document does not exist: {rel_path}")
        return full.read_text(encoding="utf-8")

    def search_docs(self, query: str, limit: int = 20) -> list[SearchHit]:
        """Simple case-insensitive text search across all documents."""
        q = query.strip().lower()
        if not q:
            raise StoreError("Empty query")
        hits: list[SearchHit] = []
        for p in self._iter_docs():
            if is_protected(self._rel(p)):
                continue
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if q in line.lower():
                    hits.append(SearchHit(self._rel(p), n, line.strip()))
                    if len(hits) >= limit:
                        return hits
        return hits

    # ---- writing ----------------------------------------------------------

    def write_doc(self, rel_path: str, content: str, internal: bool = False) -> WriteResult:
        """Write a document. `internal=True` is for code (not the Curator): it allows the
        protected folders and skips the secret scan (mechanical metadata)."""
        full = self._resolve(rel_path)
        if is_protected(self._rel(full)) and not internal:
            raise StoreError(
                f"The {self._rel(full).split('/', 1)[0]}/ folder is managed by the bot: "
                "do not create or change documents in it"
            )

        meta, body = _parse(content)
        description = str(meta.get("description", "")).strip()
        if not description:
            raise StoreError("The frontmatter needs a one-line `description:`")
        if "\n" in description:
            raise StoreError("`description` must be a single line")
        if not body.strip():
            raise StoreError("Document has no content")

        for name, pattern in ({} if internal else _SECRET_PATTERNS).items():
            if pattern.search(content):
                raise StoreError(
                    f"Possible secret detected ({name}); the memory is pushed to a remote"
                )

        warnings = []
        n_lines = content.count("\n") + 1
        if n_lines > MAX_LINES:
            warnings.append(
                f"Document has {n_lines} lines (limit {MAX_LINES}): "
                "split it into smaller documents linked together, without summarizing."
            )

        if not content.endswith("\n"):
            content += "\n"  # text files end with a newline (git complains otherwise)

        created = not full.exists()
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8", newline="\n")
        return WriteResult(self._rel(full), created, warnings)
