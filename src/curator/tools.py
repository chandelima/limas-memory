"""Tools LangChain do Memory Curator.

Camada fina: cada tool só chama o `store`/`git`/`index` e devolve texto. Erros de
validação viram uma mensagem "ERRO: ..." que o agente lê e corrige; as regras de
verdade (caminho, description, segredos) continuam vivendo em `storage/`.
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
    """Tools SOMENTE de leitura (usadas pelo Curator e pelo agente de resposta)."""

    @tool
    def list_memory(folder: str = "", page: int = 1) -> str:
        """Lista documentos da memória (caminho e description), 50 por página.
        Sem `folder`, mostra também as pastas com a contagem de documentos; use
        `folder` (ex.: 'pessoas' ou '(raiz)') para filtrar e `page` para continuar.
        Use primeiro, para saber o que existe antes de buscar ou ler."""
        if page < 1:
            return "ERRO: page deve ser >= 1"
        total = store.count_docs(folder)
        if total == 0:
            if not folder:
                return "A memória está vazia."
            return f"Nenhum documento na pasta '{folder}'."
        pages = -(-total // PAGE_SIZE)
        if page > pages:
            return f"ERRO: a página {page} não existe (total: {pages})"
        docs = store.list_docs(folder, offset=(page - 1) * PAGE_SIZE, limit=PAGE_SIZE)
        lines = []
        if not folder:
            counts = ", ".join(f"{name} ({n})" for name, n in store.folder_counts().items())
            lines.append(f"Pastas: {counts}")
        lines += [f"- {d.path} — {d.description or '(sem description)'}" for d in docs]
        footer = f"Página {page}/{pages} ({total} documentos)."
        if page < pages:
            footer += f" Use page={page + 1} para continuar."
        lines.append(footer)
        return "\n".join(lines)

    @tool
    def search_memory(query: str) -> str:
        """Busca um texto (sem diferenciar maiúsculas) em todos os documentos.
        Devolve caminho, linha e trecho. Use para checar se um assunto existe."""
        try:
            hits = store.search_docs(query, limit=SEARCH_LIMIT + 1)
        except StoreError as exc:
            return f"ERRO: {exc}"
        if not hits:
            return "Nenhum resultado."
        lines = [f"{h.path}:{h.line}: {h.text}" for h in hits[:SEARCH_LIMIT]]
        if len(hits) > SEARCH_LIMIT:
            lines.append(f"(mostrando {SEARCH_LIMIT} resultados; há mais, refine a busca)")
        return "\n".join(lines)

    @tool
    def read_memory(path: str) -> str:
        """Lê um documento da memória pelo caminho relativo (ex.: 'pessoas/ana.md')."""
        try:
            return store.read_doc(path)
        except StoreError as exc:
            return f"ERRO: {exc}"

    return [list_memory, search_memory, read_memory]


def build_tools(store: MemoryStore, git: MemoryGit) -> list[BaseTool]:
    """Todas as tools do Curator: leitura + escrita, índice e commit."""

    @tool
    def write_memory(path: str, content: str) -> str:
        """Cria ou SUBSTITUI um documento inteiro. Para atualizar, leia o atual antes
        e envie o conteúdo completo já ajustado. O conteúdo precisa começar com
        frontmatter contendo `description:` de uma linha. Só caminhos .md relativos."""
        try:
            result = store.write_doc(path, content)
        except StoreError as exc:
            return f"ERRO: {exc}"
        status = "criado" if result.created else "atualizado"
        msg = f"OK: {result.path} {status}."
        if result.warnings:
            msg += " AVISO: " + " ".join(result.warnings)
        return msg

    @tool
    def update_index() -> str:
        """Regenera o index.md a partir da description de cada documento e aponta
        links quebrados. Chame depois de criar ou reorganizar documentos."""
        try:
            path = idx.update_index(store)
        except StoreError as exc:
            return f"ERRO: {exc}"
        broken = idx.find_broken_links(store)
        if not broken:
            return f"OK: {path} atualizado. Nenhum link quebrado."
        lines = "\n".join(f"- {b.doc} -> {b.target}" for b in broken)
        return f"OK: {path} atualizado. ATENÇÃO, links quebrados:\n{lines}"

    @tool
    def commit_memory(message: str) -> str:
        """Faz commit de tudo que mudou na memória e tenta o push. Use UMA vez, ao
        final, com mensagem descritiva do que mudou e por quê (ex.: 'docs: registra
        preferência de café em preferencias/cafe.md')."""
        try:
            result = git.commit_and_push(message)
        except GitError as exc:
            return f"ERRO: {exc}"
        if result.sha is None:
            return "Nada para commitar."
        if result.pushed:
            return f"OK: commit {result.sha} enviado ao remote."
        return f"OK: commit {result.sha} salvo localmente. Push pendente: {result.push_error}"

    return [*build_read_tools(store), write_memory, update_index, commit_memory]


def make_store_and_git(
    memory_dir: Path | None = None, push: bool | None = None
) -> tuple[MemoryStore, MemoryGit]:
    """Store e git da pasta indicada (padrão: MEMORY_DIR), com identidade por env.

    `push` controla remote e token: por padrão só a memória REAL (sem `memory_dir`)
    os recebe. Uma pasta indicada explicitamente (teste, comparação) nunca faz
    push, para não enviar dados de teste ao GitHub da memória.
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
    """Tools ligadas à memória e ao git configurados por env."""
    return build_tools(*make_store_and_git(memory_dir))
