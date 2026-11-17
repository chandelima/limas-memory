import pytest

from attachments.registry import AttachmentRegistry, normalize_id
from curator.tools import build_read_tools, build_tools
from storage import index as idx
from storage import markdown as md
from storage.git import MemoryGit
from storage.store import MemoryStore, StoreError, is_protected

# a real Telegram file_id is base64url: it can have '-' and '_' and patterns that look like secrets
TRICKY_FILE_ID = "BQACAgEAAxkBAAIB-sk-AAAAAAAAAAAAAAAAAAAAAAAA_ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAA"


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory")


@pytest.fixture
def registry(store):
    return AttachmentRegistry(store)


def make(registry, **kw):
    base = dict(file_id="AgACAg-xyz", file_unique_id="u1", kind="document", chat_id=42,
                message_id=7, filename="voucher.pdf", mime="application/pdf", size=1234,
                caption="trip to Lisbon")
    base.update(kw)
    return registry.add(**base)


def test_add_writes_a_markdown_document_in_attachments(registry, store):
    item = make(registry)
    path = f"attachments/{item.id}.md"
    assert (store.root / path).is_file() and path in store.list_paths()
    text = store.read_doc(path)
    meta, body = md.parse_frontmatter(text)
    assert meta["description"] == "File voucher.pdf (application/pdf), sent on " + item.received_at
    assert meta["attachment_id"] == item.id and meta["file_id"] == "AgACAg-xyz" and meta["message_id"] == 7
    assert f"# Attachment {item.id}" in body and f"Reference: attachment:{item.id}" in body
    assert "Caption: trip to Lisbon" in body
    assert text.endswith("\n")


def test_roundtrip_and_new_instance_reads_the_files(registry, store):
    item = make(registry)
    again = AttachmentRegistry(MemoryStore(store.root))
    got = again.get(item.id)
    assert got == item and again.all() == [item]
    assert len(item.id) == 8 and item.ref == f"attachment:{item.id}"


def test_special_characters_survive_the_roundtrip(registry):
    item = make(registry, filename="voucher: hotel #1 'Sol'\nday 2.pdf", caption="caption: with a colon\n and a line break",
                file_id="12345e6")  # looks like a number in YAML
    got = registry.get(item.id)
    assert got.filename == item.filename and got.caption == "caption: with a colon\n and a line break".strip()
    assert got.file_id == "12345e6" and isinstance(got.file_id, str)
    assert "\n" not in md.parse_frontmatter(registry.store.read_doc(f"attachments/{item.id}.md"))[0]["description"]


def test_file_ids_that_look_like_secrets_are_not_rejected(registry):
    item = make(registry, file_id=TRICKY_FILE_ID)
    assert registry.get(item.id).file_id == TRICKY_FILE_ID


def test_get_accepts_prefix_and_case_and_rejects_paths(registry):
    item = make(registry)
    assert registry.get(f"attachment:{item.id}").id == item.id
    assert registry.get(f" ATTACHMENT:{item.id.upper()} ").id == item.id
    assert registry.get("nonexistent") is None and registry.get("") is None
    for bad in ("../index", "..", "a/b", "attachments/" + item.id, "xyz!"):
        assert registry.get(bad) is None
    assert normalize_id("Attachment: AB12") == "ab12"


def test_ids_are_unique(registry):
    assert len({make(registry, file_id=f"f{i}").id for i in range(50)}) == 50


def test_all_is_sorted_and_skips_damaged_documents(registry, store):
    first = make(registry, received_at="2026-11-17T09:00:00-03:00")
    second = make(registry, received_at="2026-11-17T10:00:00-03:00")
    store.write_doc("attachments/zzzz9999.md", "---\ndescription: broken\n---\n# no fields\n", internal=True)
    assert [a.id for a in registry.all()] == [first.id, second.id]
    assert registry.get("zzzz9999") is None


# ---- protected folder -----------------------------------------------------------------------


def test_curator_cannot_write_in_attachments(store):
    doc = "---\ndescription: x\n---\n# X\nbody\n"
    for path in ("attachments/new.md", "attachments/sub/new.md"):
        with pytest.raises(StoreError, match="managed by the bot"):
            store.write_doc(path, doc)
    store.write_doc("attachments/new.md", doc, internal=True)  # the bot's code can
    with pytest.raises(StoreError, match="managed by the bot"):
        store.write_doc("attachments/new.md", doc)  # and the Curator does not overwrite what exists
    assert is_protected("attachments/a.md") and not is_protected("trips/attachments.md")


def test_curator_tool_cannot_touch_attachments_but_can_read_them(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    item = make(AttachmentRegistry(store))
    tools = {t.name: t for t in build_tools(store, MemoryGit(store.root))}
    doc = "---\ndescription: x\n---\n# X\nbody\n"
    out = tools["write_memory"].invoke({"path": f"attachments/{item.id}.md", "content": doc})
    assert out.startswith("ERROR") and "managed by the bot" in out
    assert "file_id" in tools["read_memory"].invoke({"path": f"attachments/{item.id}.md"})
    assert f"Reference: attachment:{item.id}" in store.read_doc(f"attachments/{item.id}.md")  # intact


def test_attachments_are_hidden_from_list_search_count_and_index(registry, store):
    item = make(registry, filename="hotel-booking.pdf")
    store.write_doc("trips/lisbon.md", f"---\ndescription: Trip to Lisbon\n---\n# Lisbon\nSource: attachment:{item.id}\n")
    assert [d.path for d in store.list_docs()] == ["trips/lisbon.md"]
    assert store.count_docs() == 1 and store.folder_counts() == {"trips": 1}
    assert store.list_docs("attachments") == []
    assert [h.path for h in store.search_docs("hotel-booking")] == []  # only the registry has this name
    assert [h.path for h in store.search_docs(item.id)] == ["trips/lisbon.md"]
    idx.update_index(store)
    assert "attachments" not in store.read_doc("index.md") and "trips/lisbon.md" in store.read_doc("index.md")
    assert idx.find_broken_links(store) == []  # the attachment documents still exist for links


def test_chat_agent_read_tools_do_not_list_attachments(registry, store):
    make(registry)
    tools = {t.name: t for t in build_read_tools(store)}
    assert tools["list_memory"].invoke({}) == "The memory is empty."


def test_git_commit_includes_attachments_with_the_next_curator_commit(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    git = MemoryGit(store.root)
    item = make(AttachmentRegistry(store))
    result = git.commit_and_push("docs: record trip")
    assert result.sha
    assert f"attachments/{item.id}.md" in git._git_ok("ls-files")
