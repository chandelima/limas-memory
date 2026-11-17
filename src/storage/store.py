"""Camada de armazenamento da memória: só Markdown, confinado a MEMORY_DIR.

Sem LLM aqui. Este módulo é quem garante as regras mecânicas:
- nada fora da pasta da memória, só arquivos .md;
- todo documento tem frontmatter com `description` (uma linha);
- nenhum segredo (chaves/tokens) é gravado;
- avisa quando um documento cresce demais (a ação certa é dividir, não resumir).
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from storage import markdown as md

MAX_LINES = 300  # acima disso, avisa para dividir o documento
ROOT_FOLDER = "(raiz)"  # nome da "pasta" dos documentos soltos na raiz
# Pastas gerenciadas por código (não pelo Curator): só `write_doc(..., internal=True)` grava
# nelas, e ficam fora de list_docs, search_docs e do índice. Leitura (read_doc) é permitida.
PROTECTED_FOLDERS = ("anexos", "todos")


def is_protected(rel_path: str) -> bool:
    return rel_path.split("/", 1)[0] in PROTECTED_FOLDERS

_SECRET_PATTERNS = {
    "chave sk-": re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    "token GitHub": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    "chave AWS": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "token Slack": re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
    "chave privada": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "atribuição de segredo": re.compile(
        r"(?i)\b(api[_-]?key|secret|token|password|senha)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+=]{12,}"
    ),
}


class StoreError(ValueError):
    """Operação recusada pela camada de armazenamento."""


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

    # ---- caminhos ---------------------------------------------------------

    def _resolve(self, rel_path: str) -> Path:
        if not rel_path or not isinstance(rel_path, str):
            raise StoreError("Caminho vazio")
        rel = Path(rel_path)
        if rel.is_absolute() or rel.drive:
            raise StoreError("Use caminhos relativos à pasta da memória")
        if rel.suffix.lower() != ".md":
            raise StoreError("Só arquivos .md são permitidos")
        if any(part == ".git" for part in rel.parts):
            raise StoreError("Acesso a .git não é permitido")
        full = (self.root / rel).resolve()
        if not full.is_relative_to(self.root):
            raise StoreError("Caminho fora da pasta da memória")
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

    # ---- leitura ----------------------------------------------------------

    def list_paths(self) -> list[str]:
        """Caminhos relativos de todos os .md, incluindo index.md."""
        return [self._rel(p) for p in self._iter_docs()]

    def _doc_paths(self, folder: str = "") -> list[str]:
        """Caminhos dos documentos (sem index.md), opcionalmente de uma pasta."""
        paths = [r for r in self.list_paths() if r != "index.md" and not is_protected(r)]
        folder = folder.strip().strip("/")
        if not folder:
            return paths
        if folder == ROOT_FOLDER:
            return [r for r in paths if "/" not in r]
        return [r for r in paths if r.startswith(folder + "/")]

    def folder_counts(self) -> dict[str, int]:
        """Quantidade de documentos por pasta de primeiro nível (raiz = '(raiz)')."""
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
        """Documentos (exceto index.md) com a `description` de cada um.

        `folder` filtra por pasta ('pessoas', 'pessoas/amigos' ou '(raiz)');
        `offset`/`limit` paginam. Só os documentos da página são lidos do disco.
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
            raise StoreError(f"Documento não existe: {rel_path}")
        return full.read_text(encoding="utf-8")

    def search_docs(self, query: str, limit: int = 20) -> list[SearchHit]:
        """Busca textual simples (sem diferenciar maiúsculas) em todos os documentos."""
        q = query.strip().lower()
        if not q:
            raise StoreError("Consulta vazia")
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

    # ---- escrita ----------------------------------------------------------

    def write_doc(self, rel_path: str, content: str, internal: bool = False) -> WriteResult:
        """Grava um documento. `internal=True` é para código (não o Curator): permite as
        pastas protegidas e dispensa a busca de segredos (metadados mecânicos)."""
        full = self._resolve(rel_path)
        if is_protected(self._rel(full)) and not internal:
            raise StoreError(
                f"A pasta {self._rel(full).split('/', 1)[0]}/ é gerenciada pelo bot: "
                "não crie nem altere documentos nela"
            )

        meta, body = _parse(content)
        description = str(meta.get("description", "")).strip()
        if not description:
            raise StoreError("O frontmatter precisa de `description:` com uma linha")
        if "\n" in description:
            raise StoreError("`description` deve ter uma única linha")
        if not body.strip():
            raise StoreError("Documento sem conteúdo")

        for name, pattern in ({} if internal else _SECRET_PATTERNS).items():
            if pattern.search(content):
                raise StoreError(
                    f"Possível segredo detectado ({name}); a memória vai para o GitHub"
                )

        warnings = []
        n_lines = content.count("\n") + 1
        if n_lines > MAX_LINES:
            warnings.append(
                f"Documento com {n_lines} linhas (limite {MAX_LINES}): "
                "divida em documentos menores ligados por links, sem resumir."
            )

        if not content.endswith("\n"):
            content += "\n"  # arquivos de texto terminam com quebra de linha (o git reclama)

        created = not full.exists()
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8", newline="\n")
        return WriteResult(self._rel(full), created, warnings)
