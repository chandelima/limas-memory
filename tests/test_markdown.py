import pytest

from storage import markdown as md


def test_parse_frontmatter():
    meta, body = md.parse_frontmatter("---\ndescription: hi\ntags: [a, b]\n---\n# T\nbody\n")
    assert meta == {"description": "hi", "tags": ["a", "b"]}
    assert body == "# T\nbody\n"


def test_parse_frontmatter_absent():
    assert md.parse_frontmatter("# T\nbody") == ({}, "# T\nbody")


def test_parse_frontmatter_invalid():
    with pytest.raises(md.MarkdownError):
        md.parse_frontmatter("---\na: [\n---\nbody")
    with pytest.raises(md.MarkdownError):
        md.parse_frontmatter("---\n- list\n---\nbody")


@pytest.mark.parametrize(
    "line,expected",
    [
        ("# A", (1, "A")),
        ("### Long title", (3, "Long title")),
        ("## Closed ##", (2, "Closed")),
        ("####### seven", None),
        ("#no space", None),
        ("text # in the middle", None),
    ],
)
def test_parse_heading(line, expected):
    assert md.parse_heading(line) == expected


def test_headings_ignore_code_fences():
    body = "# A\n```\n# comment\n```\n## B\n"
    assert md.headings(body) == [(1, "A"), (2, "B")]


def test_first_heading():
    assert md.first_heading("text\n## B\n# A\n") == "A"
    assert md.first_heading("text\n## B\n# A\n", level=2) == "B"
    assert md.first_heading("no headings") is None


def test_extract_links_skips_images():
    text = "[a](x.md) ![img](f.png) [b](https://s.com \"t\")"
    assert md.extract_links(text) == ["x.md", "https://s.com"]


def test_is_external():
    assert md.is_external("https://x.com") and md.is_external("mailto:a@b.c")
    assert md.is_external("//cdn.x/y")
    assert not md.is_external("../a.md") and not md.is_external("a.md#x")


def test_invalid_frontmatter_error_hints_at_quotes():
    with pytest.raises(md.MarkdownError, match="double quotes"):
        md.parse_frontmatter("---\ndescription: User - note: something\n---\nbody")
