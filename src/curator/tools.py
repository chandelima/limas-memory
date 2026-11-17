"""LangChain tools of the Memory Curator.

Thin layer: each tool only calls `store`/`git`/`index` and returns text. Validation
errors become an "ERROR: ..." message that the agent reads and fixes; the real rules
(path, description, secrets) keep living in `storage/`.
"""

from pathlib import Path

from langchain_core.tools import BaseTool, tool

import config
from storage import index as idx
from storage.git import GitError, MemoryGit
from storage.store import MemoryStore, StoreError

PAGE_SIZE = 50
SEARCH_LIMIT = 20


def build_read_tools(store: MemoryStore) -> list[BaseTool]:
    """READ-ONLY tools (used by the Curator and by the chat agent)."""

    @tool
    def list_memory(folder: str = "", page: int = 1) -> str:
        """List memory documents (path and description), 50 per page.
        Without `folder`, it also shows the folders with their document counts; use
        `folder` (e.g. 'people' or '(root)') to filter and `page` to continue.
        Use it first, to know what exists before searching or reading."""
        if page < 1:
            return "ERROR: page must be >= 1"
        total = store.count_docs(folder)
        if total == 0:
            if not folder:
                return "The memory is empty."
            return f"No documents in folder '{folder}'."
        pages = -(-total // PAGE_SIZE)
        if page > pages:
            return f"ERROR: page {page} does not exist (total: {pages})"
        docs = store.list_docs(folder, offset=(page - 1) * PAGE_SIZE, limit=PAGE_SIZE)
        lines = []
        if not folder:
            counts = ", ".join(f"{name} ({n})" for name, n in store.folder_counts().items())
            lines.append(f"Folders: {counts}")
        lines += [f"- {d.path} — {d.description or '(no description)'}" for d in docs]
        footer = f"Page {page}/{pages} ({total} documents)."
        if page < pages:
            footer += f" Use page={page + 1} to continue."
        lines.append(footer)
        return "\n".join(lines)

    @tool
    def search_memory(query: str) -> str:
        """Search for text (case-insensitive) across all documents.
        Returns path, line and snippet. Use it to check whether a subject exists."""
        try:
            hits = store.search_docs(query, limit=SEARCH_LIMIT + 1)
        except StoreError as exc:
            return f"ERROR: {exc}"
        if not hits:
            return "No results."
        lines = [f"{h.path}:{h.line}: {h.text}" for h in hits[:SEARCH_LIMIT]]
        if len(hits) > SEARCH_LIMIT:
            lines.append(f"(showing {SEARCH_LIMIT} results; there are more, refine the search)")
        return "\n".join(lines)

    @tool
    def read_memory(path: str) -> str:
        """Read a memory document by its relative path (e.g. 'people/ana.md')."""
        try:
            return store.read_doc(path)
        except StoreError as exc:
            return f"ERROR: {exc}"

    return [list_memory, search_memory, read_memory]


def build_tools(store: MemoryStore, git: MemoryGit) -> list[BaseTool]:
    """All the Curator's tools: read + write, index and commit."""

    @tool
    def write_memory(path: str, content: str) -> str:
        """Create or REPLACE a whole document. To update, read the current one first
        and send the full content already adjusted. The content must start with
        frontmatter containing a one-line `description:`. Relative .md paths only."""
        try:
            result = store.write_doc(path, content)
        except StoreError as exc:
            return f"ERROR: {exc}"
        status = "created" if result.created else "updated"
        msg = f"OK: {result.path} {status}."
        if result.warnings:
            msg += " WARNING: " + " ".join(result.warnings)
        return msg

    @tool
    def update_index() -> str:
        """Regenerate index.md from each document's description and report broken
        links. Call it after creating or reorganizing documents."""
        try:
            path = idx.update_index(store)
        except StoreError as exc:
            return f"ERROR: {exc}"
        broken = idx.find_broken_links(store)
        if not broken:
            return f"OK: {path} updated. No broken links."
        lines = "\n".join(f"- {b.doc} -> {b.target}" for b in broken)
        return f"OK: {path} updated. ATTENTION, broken links:\n{lines}"

    @tool
    def commit_memory(message: str) -> str:
        """Commit everything that changed in the memory and try to push. Use it ONCE, at
        the end, with a descriptive message of what changed and why (e.g. 'docs: record
        coffee preference in preferences/coffee.md')."""
        try:
            result = git.commit_and_push(message)
        except GitError as exc:
            return f"ERROR: {exc}"
        if result.sha is None:
            return "Nothing to commit."
        if result.pushed:
            return f"OK: commit {result.sha} sent to the remote."
        return f"OK: commit {result.sha} saved locally. Push pending: {result.push_error}"

    return [*build_read_tools(store), write_memory, update_index, commit_memory]


def make_store_and_git(
    memory_dir: Path | None = None, push: bool | None = None
) -> tuple[MemoryStore, MemoryGit]:
    """Store and git for the given folder (default: MEMORY_DIR), with identity from env.

    `push` controls the remote and token: by default only the REAL memory (no
    `memory_dir`) gets them. An explicitly given folder (test, comparison) never
    pushes, so test data is not sent to the memory's remote.
    """
    root = Path(memory_dir) if memory_dir else config.MEMORY_DIR
    if push is None:
        push = memory_dir is None
    git = MemoryGit(
        root,
        remote=config.MEMORY_GIT_REMOTE if push else "",
        user_name=config.MEMORY_GIT_USER_NAME,
        user_email=config.MEMORY_GIT_USER_EMAIL,
        token=config.MEMORY_GIT_TOKEN if push else "",
    )
    return MemoryStore(root), git


def default_tools(memory_dir: Path | None = None) -> list[BaseTool]:
    """Tools bound to the memory and git configured by env."""
    return build_tools(*make_store_and_git(memory_dir))
