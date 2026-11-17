import pytest

from storage.store import MAX_LINES, MemoryStore, StoreError

DOC = "---\ndescription: Coffee preferences\n---\n# Coffee\nLikes coffee without sugar.\n"


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path)


def test_write_and_read_roundtrip(store):
    result = store.write_doc("preferences/coffee.md", DOC)
    assert result.created and result.path == "preferences/coffee.md"
    assert store.read_doc("preferences/coffee.md") == DOC


def test_write_existing_marks_not_created(store):
    store.write_doc("a.md", DOC)
    assert store.write_doc("a.md", DOC).created is False


def test_list_docs_returns_descriptions_and_skips_index(store):
    store.write_doc("a.md", DOC)
    store.write_doc("index.md", "---\ndescription: Index\n---\n# Index\n- a\n")
    docs = store.list_docs()
    assert [(d.path, d.description) for d in docs] == [("a.md", "Coffee preferences")]


@pytest.mark.parametrize(
    "path", ["../outside.md", "/abs/x.md", "C:\\x.md", "a/../../x.md", "note.txt", "script.py", ".git/x.md"]
)
def test_rejects_bad_paths(store, path):
    with pytest.raises(StoreError):
        store.write_doc(path, DOC)


def test_requires_description(store):
    with pytest.raises(StoreError, match="description"):
        store.write_doc("a.md", "# No frontmatter\ntext\n")
    with pytest.raises(StoreError, match="description"):
        store.write_doc("a.md", "---\ntitle: x\n---\ntext\n")


def test_rejects_empty_body(store):
    with pytest.raises(StoreError, match="no content"):
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
    with pytest.raises(StoreError, match="secret"):
        store.write_doc("a.md", f"---\ndescription: x\n---\n{secret}\n")


def test_warns_on_large_doc(store):
    body = "line\n" * MAX_LINES
    result = store.write_doc("large.md", f"---\ndescription: x\n---\n{body}")
    assert result.warnings and "split" in result.warnings[0]


def test_read_missing_raises(store):
    with pytest.raises(StoreError, match="does not exist"):
        store.read_doc("missing.md")


def test_search_finds_lines_case_insensitive(store):
    store.write_doc("a.md", DOC)
    hits = store.search_docs("LIKES COFFEE")
    assert [(h.path, h.line) for h in hits] == [("a.md", 5)]
    assert store.search_docs("nonexistent") == []


def test_search_rejects_empty_query(store):
    with pytest.raises(StoreError):
        store.search_docs("  ")


def _many(store, n, folder=""):
    for i in range(n):
        prefix = f"{folder}/" if folder else ""
        store.write_doc(f"{prefix}d{i:03}.md", f"---\ndescription: doc {i}\n---\nbody {i}\n")


def test_list_docs_paginates(store):
    _many(store, 7)
    page1 = store.list_docs(offset=0, limit=3)
    page3 = store.list_docs(offset=6, limit=3)
    assert [d.path for d in page1] == ["d000.md", "d001.md", "d002.md"]
    assert [d.path for d in page3] == ["d006.md"]
    assert store.count_docs() == 7


def test_list_docs_filters_by_folder(store):
    _many(store, 2)
    _many(store, 3, "people")
    _many(store, 1, "people/friends")
    assert store.count_docs("people") == 4  # includes subfolders
    assert [d.path for d in store.list_docs("people/friends")] == ["people/friends/d000.md"]
    assert [d.path for d in store.list_docs("(root)")] == ["d000.md", "d001.md"]
    assert store.folder_counts() == {"(root)": 2, "people": 4}


def test_write_adds_trailing_newline(store):
    store.write_doc("a.md", "---\ndescription: x\n---\n# A\nno newline at the end")
    assert store.read_doc("a.md").endswith("no newline at the end\n")
    store.write_doc("b.md", DOC)
    assert store.read_doc("b.md") == DOC  # already ended with a newline: no duplicate
