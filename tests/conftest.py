import pytest

import config


@pytest.fixture(autouse=True)
def isolate_conversation_file(tmp_path, monkeypatch):
    """Nenhum teste pode escrever no histórico real (data/conversa.json)."""
    monkeypatch.setattr(config, "CONVERSATION_FILE", tmp_path / "conversa-teste.json")
