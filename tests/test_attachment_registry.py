import pytest

from attachments.registry import AttachmentRegistry, normalize_id
from curator.tools import build_read_tools, build_tools
from storage import index as idx
from storage import markdown as md
from storage.git import MemoryGit
from storage.store import MemoryStore, StoreError, is_protected

# um file_id real do Telegram é base64url: pode ter '-' e '_' e padrões que parecem segredo
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
                caption="viagem a Lisboa")
    base.update(kw)
    return registry.add(**base)


def test_add_writes_a_markdown_document_in_anexos(registry, store):
    item = make(registry)
    path = f"anexos/{item.id}.md"
    assert (store.root / path).is_file() and path in store.list_paths()
    text = store.read_doc(path)
    meta, body = md.parse_frontmatter(text)
    assert meta["description"] == "Arquivo voucher.pdf (application/pdf), enviado em " + item.received_at
    assert meta["anexo_id"] == item.id and meta["file_id"] == "AgACAg-xyz" and meta["message_id"] == 7
    assert f"# Anexo {item.id}" in body and f"Referência: anexo:{item.id}" in body
    assert "Legenda: viagem a Lisboa" in body
    assert text.endswith("\n")


def test_roundtrip_and_new_instance_reads_the_files(registry, store):
    item = make(registry)
    again = AttachmentRegistry(MemoryStore(store.root))
    got = again.get(item.id)
    assert got == item and again.all() == [item]
    assert len(item.id) == 8 and item.ref == f"anexo:{item.id}"


def test_special_characters_survive_the_roundtrip(registry):
    item = make(registry, filename="voucher: hotel #1 'Sol'\ndia 2.pdf", caption="legenda: com dois-pontos\n e quebra",
                file_id="12345e6")  # parece número em YAML
    got = registry.get(item.id)
    assert got.filename == item.filename and got.caption == "legenda: com dois-pontos\n e quebra".strip()
    assert got.file_id == "12345e6" and isinstance(got.file_id, str)
    assert "\n" not in md.parse_frontmatter(registry.store.read_doc(f"anexos/{item.id}.md"))[0]["description"]


def test_file_ids_that_look_like_secrets_are_not_rejected(registry):
    item = make(registry, file_id=TRICKY_FILE_ID)
    assert registry.get(item.id).file_id == TRICKY_FILE_ID


def test_get_accepts_prefix_and_case_and_rejects_paths(registry):
    item = make(registry)
    assert registry.get(f"anexo:{item.id}").id == item.id
    assert registry.get(f" ANEXO:{item.id.upper()} ").id == item.id
    assert registry.get("naoexiste") is None and registry.get("") is None
    for bad in ("../index", "..", "a/b", "anexos/" + item.id, "xyz!"):
        assert registry.get(bad) is None
    assert normalize_id("Anexo: AB12") == "ab12"


def test_ids_are_unique(registry):
    assert len({make(registry, file_id=f"f{i}").id for i in range(50)}) == 50


def test_all_is_sorted_and_skips_damaged_documents(registry, store):
    first = make(registry, received_at="2026-11-17T09:00:00-03:00")
    second = make(registry, received_at="2026-11-17T10:00:00-03:00")
    store.write_doc("anexos/zzzz9999.md", "---\ndescription: quebrado\n---\n# sem campos\n", internal=True)
    assert [a.id for a in registry.all()] == [first.id, second.id]
    assert registry.get("zzzz9999") is None


# ---- pasta protegida -----------------------------------------------------------------


def test_curator_cannot_write_in_anexos(store):
    doc = "---\ndescription: x\n---\n# X\ncorpo\n"
    for path in ("anexos/novo.md", "anexos/sub/novo.md"):
        with pytest.raises(StoreError, match="gerenciada pelo bot"):
            store.write_doc(path, doc)
    store.write_doc("anexos/novo.md", doc, internal=True)  # o código do bot pode
    with pytest.raises(StoreError, match="gerenciada pelo bot"):
        store.write_doc("anexos/novo.md", doc)  # e o Curator não sobrescreve o que existe
    assert is_protected("anexos/a.md") and not is_protected("viagens/anexos.md")


def test_curator_tool_cannot_touch_anexos_but_can_read_them(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    item = make(AttachmentRegistry(store))
    tools = {t.name: t for t in build_tools(store, MemoryGit(store.root))}
    doc = "---\ndescription: x\n---\n# X\ncorpo\n"
    out = tools["write_memory"].invoke({"path": f"anexos/{item.id}.md", "content": doc})
    assert out.startswith("ERRO") and "gerenciada pelo bot" in out
    assert "file_id" in tools["read_memory"].invoke({"path": f"anexos/{item.id}.md"})
    assert f"Referência: anexo:{item.id}" in store.read_doc(f"anexos/{item.id}.md")  # intacto


def test_anexos_are_hidden_from_list_search_count_and_index(registry, store):
    item = make(registry, filename="reserva-hotel.pdf")
    store.write_doc("viagens/lisboa.md", f"---\ndescription: Viagem a Lisboa\n---\n# Lisboa\nFonte: anexo:{item.id}\n")
    assert [d.path for d in store.list_docs()] == ["viagens/lisboa.md"]
    assert store.count_docs() == 1 and store.folder_counts() == {"viagens": 1}
    assert store.list_docs("anexos") == []
    assert [h.path for h in store.search_docs("reserva-hotel")] == []  # só o registro tem esse nome
    assert [h.path for h in store.search_docs(item.id)] == ["viagens/lisboa.md"]
    idx.update_index(store)
    assert "anexos" not in store.read_doc("index.md") and "viagens/lisboa.md" in store.read_doc("index.md")
    assert idx.find_broken_links(store) == []  # os documentos de anexos continuam existindo p/ links


def test_responder_read_tools_do_not_list_anexos(registry, store):
    make(registry)
    tools = {t.name: t for t in build_read_tools(store)}
    assert tools["list_memory"].invoke({}) == "A memória está vazia."


def test_git_commit_includes_anexos_with_the_next_curator_commit(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    git = MemoryGit(store.root)
    item = make(AttachmentRegistry(store))
    result = git.commit_and_push("docs: registra viagem")
    assert result.sha
    assert f"anexos/{item.id}.md" in git._git_ok("ls-files")
