import pytest

from storage.index import build_index, find_broken_links, update_index
from storage.store import MemoryStore


def doc(desc, body):
    return f"---\ndescription: {desc}\n---\n{body}\n"


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path)


def test_empty_index(store):
    assert "No documents yet" in build_index(store)


def test_index_groups_and_uses_description(store):
    store.write_doc("general.md", doc("General note", "# Note\ntext"))
    store.write_doc("people/ana.md", doc("Coworker", "# Ana\ntext"))
    store.write_doc("projects/limas.md", doc("Memory project", "no title"))
    text = build_index(store)
    assert "## General" in text and "- [Note](general.md) — General note" in text
    assert "- [Ana](people/ana.md) — Coworker" in text
    assert "- [limas](projects/limas.md) — Memory project" in text  # title = file name
    assert text.index("## General") < text.index("## people") < text.index("## projects")


def test_update_index_writes_valid_doc_and_is_idempotent(store):
    store.write_doc("a.md", doc("A", "# A\nx"))
    assert update_index(store) == "index.md"
    first = store.read_doc("index.md")
    update_index(store)
    assert store.read_doc("index.md") == first
    assert [d.path for d in store.list_docs()] == ["a.md"]  # index is not listed


def test_index_encodes_spaces_and_links_resolve(store):
    store.write_doc("people/ana maria.md", doc("Ana", "# Ana Maria\nx"))
    update_index(store)
    assert "(people/ana%20maria.md)" in store.read_doc("index.md")
    assert find_broken_links(store) == []


def test_detects_broken_links(store):
    store.write_doc("a.md", doc("A", "# A\n[ok](b.md) [broken](missing.md)"))
    store.write_doc("b.md", doc("B", "# B\nx"))
    broken = find_broken_links(store)
    assert [(b.doc, b.target) for b in broken] == [("a.md", "missing.md")]


def test_relative_links_between_folders(store):
    store.write_doc("people/ana.md", doc("Ana", "# Ana\n[proj](../projects/p.md#meta)"))
    store.write_doc("projects/p.md", doc("P", "# P\n[ana](../people/ana.md)"))
    assert find_broken_links(store) == []


def test_ignores_external_anchor_and_images(store):
    body = "# A\n[site](https://x.com) [mail](mailto:a@b.c) [top](#a) ![img](photo.png)"
    store.write_doc("a.md", doc("A", body))
    assert find_broken_links(store) == []
