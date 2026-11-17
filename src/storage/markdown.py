"""Pure functions for *reading* Markdown (stateless, no disk access).

The LLM writes the documents; here we only interpret the text:
frontmatter, headings and links.
"""

import re

import yaml

_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_EXTERNAL = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.\-]*:|//)")


class MarkdownError(ValueError):
    """Malformed Markdown (e.g. invalid frontmatter)."""


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Return (metadata, body). Metadata is empty when there is no frontmatter."""
    match = _FRONTMATTER.match(content)
    if not match:
        return {}, content
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise MarkdownError(
            "Invalid frontmatter (if the description contains ':' or '#', "
            f"wrap it in double quotes): {exc}"
        ) from exc
    if not isinstance(meta, dict):
        raise MarkdownError("Frontmatter must be a key: value map")
    return meta, content[match.end():]


def parse_heading(line: str) -> tuple[int, str] | None:
    """('## Title' -> (2, 'Title')); None if the line is not a heading."""
    match = _HEADING.match(line)
    return (len(match.group(1)), match.group(2)) if match else None


def headings(body: str) -> list[tuple[int, str]]:
    """All headings (level, text), ignoring code blocks."""
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
    """Text of the first heading at the given level (default: `# `)."""
    return next((text for lvl, text in headings(body) if lvl == level), None)


def extract_links(text: str) -> list[str]:
    """Targets of all Markdown links (images do not count)."""
    return _LINK.findall(text)


def is_external(target: str) -> bool:
    """Link with a scheme (https:, mailto:...) or a protocol-relative one (//host)."""
    return bool(_EXTERNAL.match(target))
