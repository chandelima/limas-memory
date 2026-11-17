import pytest

import config


@pytest.fixture(autouse=True)
def isolate_conversation_file(tmp_path, monkeypatch):
    """No test may write to the real history (data/conversation.json)."""
    monkeypatch.setattr(config, "CONVERSATION_FILE", tmp_path / "test-conversation.json")


@pytest.fixture(autouse=True)
def isolate_briefing_state_file(tmp_path, monkeypatch):
    """No test may write to the real morning-briefing state (data/briefing.json)."""
    monkeypatch.setattr(config, "BRIEFING_STATE_FILE", tmp_path / "test-briefing.json")
