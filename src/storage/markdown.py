"""Funções puras para *ler* Markdown (sem estado, sem acesso a disco).

Quem escreve os documentos é o LLM; aqui só interpretamos o texto:
frontmatter, títulos e links.
"""

import re

import yaml

_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_EXTERNAL = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.\-]*:|//)")


class MarkdownError(ValueError):
    """Markdown mal formado (ex.: frontmatter inválido)."""


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Devolve (metadados, corpo). Metadados vazios se não houver frontmatter."""
    match = _FRONTMATTER.match(content)
    if not match:
        return {}, content
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise MarkdownError(
            "Frontmatter inválido (se a description tiver ':' ou '#', "
            f"coloque-a entre aspas duplas): {exc}"
        ) from exc
    if not isinstance(meta, dict):
        raise MarkdownError("Frontmatter deve ser um mapa chave: valor")
    return meta, content[match.end():]


def parse_heading(line: str) -> tuple[int, str] | None:
    """('## Título' -> (2, 'Título')); None se a linha não é um título."""
    match = _HEADING.match(line)
    return (len(match.group(1)), match.group(2)) if match else None


def headings(body: str) -> list[tuple[int, str]]:
    """Todos os títulos (nível, texto), ignorando blocos de código."""
    found = []
    in_fence = False
    for line in body.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence and (h := parse_heading(line)):
            found.append(h)
    return found


def first_heading(body: str, level: int = 1) -> str | None:
    """Texto do primeiro título do nível indicado (padrão: `# `)."""
    return next((text for lvl, text in headings(body) if lvl == level), None)


def extract_links(text: str) -> list[str]:
    """Destinos de todos os links Markdown (imagens não contam)."""
    return _LINK.findall(text)


def is_external(target: str) -> bool:
    """Link com esquema (https:, mailto:...) ou protocolo relativo (//host)."""
    return bool(_EXTERNAL.match(target))
