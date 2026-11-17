import pytest

from storage.store import MAX_LINES, MemoryStore, StoreError

DOC = "---\ndescription: Preferências de café\n---\n# Café\nGosta de café sem açúcar.\n"


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path)


def test_write_and_read_roundtrip(store):
    result = store.write_doc("preferencias/cafe.md", DOC)
    assert result.created and result.path == "preferencias/cafe.md"
    assert store.read_doc("preferencias/cafe.md") == DOC


def test_write_existing_marks_not_created(store):
    store.write_doc("a.md", DOC)
    assert store.write_doc("a.md", DOC).created is False


def test_list_docs_returns_descriptions_and_skips_index(store):
    store.write_doc("a.md", DOC)
    store.write_doc("index.md", "---\ndescription: Índice\n---\n# Índice\n- a\n")
    docs = store.list_docs()
    assert [(d.path, d.description) for d in docs] == [("a.md", "Preferências de café")]


@pytest.mark.parametrize(
    "path", ["../fora.md", "/abs/x.md", "C:\\x.md", "a/../../x.md", "nota.txt", "script.py", ".git/x.md"]
)
def test_rejects_bad_paths(store, path):
    with pytest.raises(StoreError):
        store.write_doc(path, DOC)


def test_requires_description(store):
    with pytest.raises(StoreError, match="description"):
        store.write_doc("a.md", "# Sem frontmatter\ntexto\n")
    with pytest.raises(StoreError, match="description"):
        store.write_doc("a.md", "---\ntitulo: x\n---\ntexto\n")


def test_rejects_empty_body(store):
    with pytest.raises(StoreError, match="sem conteúdo"):
        store.write_doc("a.md", "---\ndescription: x\n---\n\n")


@pytest.mark.parametrize(
    "secret",
    [
        "sk-abcdefghijklmnopqrstuvwxyz123456",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "AKIAABCDEFGHIJKLMNOP",
        "api_key = abcdef1234567890XYZ",
        "-----BEGIN RSA PRIVATE KEY-----",
    ],
)
def test_blocks_secrets(store, secret):
    with pytest.raises(StoreError, match="segredo"):
        store.write_doc("a.md", f"---\ndescription: x\n---\n{secret}\n")


def test_warns_on_large_doc(store):
    body = "linha\n" * MAX_LINES
    result = store.write_doc("grande.md", f"---\ndescription: x\n---\n{body}")
    assert result.warnings and "divida" in result.warnings[0]


def test_read_missing_raises(store):
    with pytest.raises(StoreError, match="não existe"):
        store.read_doc("nao-existe.md")


def test_search_finds_lines_case_insensitive(store):
    store.write_doc("a.md", DOC)
    hits = store.search_docs("CAFÉ SEM")
    assert [(h.path, h.line) for h in hits] == [("a.md", 5)]
    assert store.search_docs("inexistente") == []


def test_search_rejects_empty_query(store):
    with pytest.raises(StoreError):
        store.search_docs("  ")


def _many(store, n, folder=""):
    for i in range(n):
        prefix = f"{folder}/" if folder else ""
        store.write_doc(f"{prefix}d{i:03}.md", f"---\ndescription: doc {i}\n---\ncorpo {i}\n")


def test_list_docs_paginates(store):
    _many(store, 7)
    page1 = store.list_docs(offset=0, limit=3)
    page3 = store.list_docs(offset=6, limit=3)
    assert [d.path for d in page1] == ["d000.md", "d001.md", "d002.md"]
    assert [d.path for d in page3] == ["d006.md"]
    assert store.count_docs() == 7


def test_list_docs_filters_by_folder(store):
    _many(store, 2)
    _many(store, 3, "pessoas")
    _many(store, 1, "pessoas/amigos")
    assert store.count_docs("pessoas") == 4  # inclui subpastas
    assert [d.path for d in store.list_docs("pessoas/amigos")] == ["pessoas/amigos/d000.md"]
    assert [d.path for d in store.list_docs("(raiz)")] == ["d000.md", "d001.md"]
    assert store.folder_counts() == {"(raiz)": 2, "pessoas": 4}
