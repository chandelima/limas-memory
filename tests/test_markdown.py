import pytest

from storage import markdown as md


def test_parse_frontmatter():
    meta, body = md.parse_frontmatter("---\ndescription: oi\ntags: [a, b]\n---\n# T\ncorpo\n")
    assert meta == {"description": "oi", "tags": ["a", "b"]}
    assert body == "# T\ncorpo\n"


def test_parse_frontmatter_absent():
    assert md.parse_frontmatter("# T\ncorpo") == ({}, "# T\ncorpo")


def test_parse_frontmatter_invalid():
    with pytest.raises(md.MarkdownError):
        md.parse_frontmatter("---\na: [\n---\ncorpo")
    with pytest.raises(md.MarkdownError):
        md.parse_frontmatter("---\n- lista\n---\ncorpo")


@pytest.mark.parametrize(
    "line,expected",
    [
        ("# A", (1, "A")),
        ("### Título longo", (3, "Título longo")),
        ("## Fechado ##", (2, "Fechado")),
        ("####### sete", None),
        ("#sem espaço", None),
        ("texto # no meio", None),
    ],
)
def test_parse_heading(line, expected):
    assert md.parse_heading(line) == expected


def test_headings_ignore_code_fences():
    body = "# A\n```\n# comentario\n```\n## B\n"
    assert md.headings(body) == [(1, "A"), (2, "B")]


def test_first_heading():
    assert md.first_heading("texto\n## B\n# A\n") == "A"
    assert md.first_heading("texto\n## B\n# A\n", level=2) == "B"
    assert md.first_heading("sem títulos") is None


def test_extract_links_skips_images():
    text = "[a](x.md) ![img](f.png) [b](https://s.com \"t\")"
    assert md.extract_links(text) == ["x.md", "https://s.com"]


def test_is_external():
    assert md.is_external("https://x.com") and md.is_external("mailto:a@b.c")
    assert md.is_external("//cdn.x/y")
    assert not md.is_external("../a.md") and not md.is_external("a.md#x")
