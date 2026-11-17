import pytest

import config
import i18n


@pytest.fixture(autouse=True)
def isolate_conversation_file(tmp_path, monkeypatch):
    """No test may write to the real history (data/conversation.json)."""
    monkeypatch.setattr(config, "CONVERSATION_FILE", tmp_path / "test-conversation.json")


@pytest.fixture(autouse=True)
def isolate_briefing_state_file(tmp_path, monkeypatch):
    """No test may write to the real morning-briefing state (data/briefing.json)."""
    monkeypatch.setattr(config, "BRIEFING_STATE_FILE", tmp_path / "test-briefing.json")


@pytest.fixture(autouse=True)
def isolate_language(tmp_path, monkeypatch):
    """Tests run in English, with no language file of the real data folder."""
    monkeypatch.setattr(config, "LANGUAGE_FILE", tmp_path / "test-language.json")
    previous = i18n.current()
    i18n.use(i18n.Messages(None))
    yield
    i18n.use(previous)
