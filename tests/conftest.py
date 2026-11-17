import pytest

import config


@pytest.fixture(autouse=True)
def isolate_conversation_file(tmp_path, monkeypatch):
    """Nenhum teste pode escrever no histórico real (data/conversa.json)."""
    monkeypatch.setattr(config, "CONVERSATION_FILE", tmp_path / "conversa-teste.json")


@pytest.fixture(autouse=True)
def isolate_attachments_file(tmp_path, monkeypatch):
    """Nenhum teste pode escrever na tabela real de anexos (data/anexos.json)."""
    monkeypatch.setattr(config, "ATTACHMENTS_FILE", tmp_path / "anexos-teste.json")
