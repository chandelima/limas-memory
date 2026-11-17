import json

import pytest

import migrate_anexos
from attachments.registry import AttachmentRegistry
from storage.store import MemoryStore


def legacy(item_id, **kw):
    base = dict(id=item_id, file_id=f"file-{item_id}", file_unique_id=f"u-{item_id}", kind="document",
                chat_id=42, message_id=7, filename="voucher.pdf", mime="application/pdf", size=10,
                caption="viagem", received_at="2026-11-17T09:00:00-03:00")
    base.update(kw)
    return base


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "anexos.json"
    path.write_text(json.dumps({"attachments": [legacy("5efcd308"), legacy("70b7c649", kind="photo")]}), encoding="utf-8")
    return path, MemoryStore(tmp_path / "memory")


def test_migrates_keeping_the_original_ids(setup):
    path, store = setup
    imported, existing = migrate_anexos.migrate(path, AttachmentRegistry(store))
    assert (imported, existing) == (2, 0)
    registry = AttachmentRegistry(store)
    assert registry.get("anexo:5efcd308").file_id == "file-5efcd308"
    assert registry.get("70b7c649").kind == "photo"
    assert (store.root / "anexos" / "5efcd308.md").is_file()


def test_is_idempotent_and_never_overwrites(setup):
    path, store = setup
    registry = AttachmentRegistry(store)
    migrate_anexos.migrate(path, registry)
    before = store.read_doc("anexos/5efcd308.md")
    assert migrate_anexos.migrate(path, registry) == (0, 2)
    assert store.read_doc("anexos/5efcd308.md") == before


def test_main_keeps_the_old_json_and_reports(setup, capsys):
    path, store = setup
    assert migrate_anexos.main(["--json", str(path), "--memory-dir", str(store.root)]) == 0
    assert path.exists()
    assert "2 anexo(s) importado(s), 0 já existiam. O JSON antigo foi mantido." in capsys.readouterr().out


def test_main_without_json_is_a_noop(tmp_path, capsys):
    assert migrate_anexos.main(["--json", str(tmp_path / "nao.json"), "--memory-dir", str(tmp_path / "m")]) == 0
    assert "Nada a migrar" in capsys.readouterr().out
    assert not (tmp_path / "m").exists()


def test_main_reports_a_broken_json(tmp_path, capsys):
    bad = tmp_path / "anexos.json"
    bad.write_text('{"attachments": [{"id": "ab12cd34"}]}', encoding="utf-8")
    assert migrate_anexos.main(["--json", str(bad), "--memory-dir", str(tmp_path / "m")]) == 1
    assert "Erro ao ler" in capsys.readouterr().err


def test_rejects_invalid_ids(tmp_path):
    registry = AttachmentRegistry(MemoryStore(tmp_path / "m"))
    from attachments.registry import Attachment

    with pytest.raises(ValueError, match="inválido"):
        registry.put(Attachment(**legacy("../x")))
