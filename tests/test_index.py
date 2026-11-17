import pytest

from storage.index import build_index, find_broken_links, update_index
from storage.store import MemoryStore


def doc(desc, body):
    return f"---\ndescription: {desc}\n---\n{body}\n"


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path)


def test_empty_index(store):
    assert "Nenhum documento ainda" in build_index(store)


def test_index_groups_and_uses_description(store):
    store.write_doc("geral.md", doc("Nota geral", "# Nota\ntexto"))
    store.write_doc("pessoas/ana.md", doc("Colega de trabalho", "# Ana\ntexto"))
    store.write_doc("projetos/limas.md", doc("Projeto de memória", "sem titulo"))
    text = build_index(store)
    assert "## Geral" in text and "- [Nota](geral.md) — Nota geral" in text
    assert "- [Ana](pessoas/ana.md) — Colega de trabalho" in text
    assert "- [limas](projetos/limas.md) — Projeto de memória" in text  # título = nome do arquivo
    assert text.index("## Geral") < text.index("## pessoas") < text.index("## projetos")


def test_update_index_writes_valid_doc_and_is_idempotent(store):
    store.write_doc("a.md", doc("A", "# A\nx"))
    assert update_index(store) == "index.md"
    first = store.read_doc("index.md")
    update_index(store)
    assert store.read_doc("index.md") == first
    assert [d.path for d in store.list_docs()] == ["a.md"]  # index fora da lista


def test_index_encodes_spaces_and_links_resolve(store):
    store.write_doc("pessoas/ana maria.md", doc("Ana", "# Ana Maria\nx"))
    update_index(store)
    assert "(pessoas/ana%20maria.md)" in store.read_doc("index.md")
    assert find_broken_links(store) == []


def test_detects_broken_links(store):
    store.write_doc("a.md", doc("A", "# A\n[ok](b.md) [quebrado](nao-existe.md)"))
    store.write_doc("b.md", doc("B", "# B\nx"))
    broken = find_broken_links(store)
    assert [(b.doc, b.target) for b in broken] == [("a.md", "nao-existe.md")]


def test_relative_links_between_folders(store):
    store.write_doc("pessoas/ana.md", doc("Ana", "# Ana\n[proj](../projetos/p.md#meta)"))
    store.write_doc("projetos/p.md", doc("P", "# P\n[ana](../pessoas/ana.md)"))
    assert find_broken_links(store) == []


def test_ignores_external_anchor_and_images(store):
    body = "# A\n[site](https://x.com) [mail](mailto:a@b.c) [topo](#a) ![img](foto.png)"
    store.write_doc("a.md", doc("A", body))
    assert find_broken_links(store) == []
