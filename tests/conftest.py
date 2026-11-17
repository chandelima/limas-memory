import pytest

import config


@pytest.fixture(autouse=True)
def isolate_conversation_file(tmp_path, monkeypatch):
    """Nenhum teste pode escrever no histórico real (data/conversa.json)."""
    monkeypatch.setattr(config, "CONVERSATION_FILE", tmp_path / "conversa-teste.json")


@pytest.fixture(autouse=True)
def isolate_briefing_state_file(tmp_path, monkeypatch):
    """Nenhum teste pode escrever no estado real do resumo da manhã (data/briefing.json)."""
    monkeypatch.setattr(config, "BRIEFING_STATE_FILE", tmp_path / "briefing-teste.json")
