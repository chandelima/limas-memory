import json
import time

import pytest
from langchain_core.messages import AIMessage

import i18n
from i18n import CATALOG, Messages, validate

PT = {
    "help": "Pode conversar comigo normalmente.",
    "hello": "Olá! {help}",
    "file_notice": "Recebi o arquivo, lendo...",
    "day_today": "Hoje",
    "briefing_greeting": "Bom dia!",
    "status_pending": "pendentes: {count}",
}


def full_pt():
    """A translation of every key: the Portuguese samples, the rest 'pt: <english>'."""
    return {k: PT.get(k, f"pt: {v}") for k, v in CATALOG.items()}


class FakeLLM:
    """Answers the language detection and the translations, and records the calls."""

    def __init__(self, language="Portuguese (Brazil)", translation=None, fail=False):
        self.language, self.translation, self.fail, self.calls = language, translation, fail, []

    def invoke(self, messages):
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("model is down")
        system, user = messages[0][1], messages[1][1]
        if system.startswith("Identify the language"):
            return AIMessage(content=json.dumps({"language": self.language}))
        asked = json.loads(user)
        if self.translation is not None:  # exactly what the test gives, nothing more
            out = {k: self.translation[k] for k in asked if k in self.translation}
        else:
            out = {k: full_pt()[k] for k in asked}
        return AIMessage(content="```json\n" + json.dumps(out) + "\n```")


@pytest.fixture
def path(tmp_path):
    return tmp_path / "data" / "language.json"


def test_without_a_language_file_everything_is_english_and_placeholders_work(path):
    m = Messages(path)
    assert not m.configured
    assert m.get("hello", help="X") == "Hello! X" and m.get("no_reply") == "(no reply)"


def test_catalog_texts_are_consistent_and_translation_prompt_formats():
    assert all(isinstance(v, str) and v for v in CATALOG.values())
    assert "Portuguese" in i18n.TRANSLATE_PROMPT.format(language="Portuguese")
    assert len([k for k in CATALOG if k.startswith("weekday_long_")]) == 7


def test_ensure_detects_a_non_english_language_translates_and_persists(path):
    llm, m = FakeLLM(), Messages(path)
    assert m.ensure("oi, tudo bem? preciso de ajuda", llm=llm) is True
    assert m.configured and m.language == "Portuguese (Brazil)" and len(llm.calls) == 2
    assert m.get("hello", help="ajuda") == "Olá! ajuda" and m.get("day_today") == "Hoje"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["language"] == "Portuguese (Brazil)" and saved["messages"]["briefing_greeting"] == "Bom dia!"
    assert Messages(path).get("file_notice") == "Recebi o arquivo, lendo..."  # a new process reads the file
    assert m.ensure("another message", llm=llm) is False and len(llm.calls) == 2  # only once


def test_english_users_get_a_file_without_translations_and_one_model_call(path):
    llm, m = FakeLLM(language="English"), Messages(path)
    assert m.ensure("hello there", llm=llm) is True and len(llm.calls) == 1
    assert json.loads(path.read_text(encoding="utf-8")) == {"language": "English", "messages": {}}
    assert m.missing() == [] and m.get("no_reply") == "(no reply)"


def test_translations_that_break_placeholders_are_dropped_and_poor_ones_are_not_saved(path):
    broken = full_pt() | {"hello": "Olá! {ajuda}", "status_pending": "pendentes: %d"}
    assert "hello" not in validate(broken) and "status_pending" not in validate(broken)
    assert validate({"help": "", "nonexistent": "x", "no_reply": 5}) == {}
    m = Messages(path)
    bad = FakeLLM(translation={"help": "só um"})  # almost nothing usable
    assert m.ensure("olá", llm=bad) is False and not path.exists() and not m.configured


def test_failure_keeps_english_and_waits_before_trying_again(path):
    m, llm = Messages(path), FakeLLM(fail=True)
    assert m.ensure("olá", llm=llm) is False and not m.configured and len(llm.calls) == 1
    assert m.ensure("olá de novo", llm=llm) is False and len(llm.calls) == 1  # back-off
    m._failed_at = time.monotonic() - i18n.RETRY_AFTER_FAILURE - 1
    assert m.ensure("olá", llm=FakeLLM()) is True and m.configured


def test_a_broken_translation_never_breaks_the_bot(path):
    m = Messages(path)
    m.texts, m.configured, m.language = {"hello": "Olá! {ajuda}"}, True, "Portuguese"
    assert m.get("hello", help="x") == "Hello! x"  # falls back to English on a bad template


def test_corrupt_language_file_falls_back_to_english(path):
    path.parent.mkdir(parents=True)
    path.write_text("{ not json", encoding="utf-8")
    m = Messages(path)
    assert not m.configured and m.get("no_reply") == "(no reply)"


def test_keys_added_by_a_new_version_are_translated_on_demand_and_saved(path, monkeypatch):
    # a file written by an older version: only a few keys
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"language": "Portuguese (Brazil)", "messages": {"hello": "Olá! {help}"}}), encoding="utf-8")
    m = Messages(path)
    assert m.configured and "no_reply" in m.missing() and "hello" not in m.missing()
    llm = FakeLLM(translation=full_pt() | {"no_reply": "(sem resposta)"})
    assert m.fill_missing(llm=llm) == len(CATALOG) - 1  # every missing key asked in one call
    asked = json.loads(llm.calls[0][1][1])
    assert "hello" not in asked and "no_reply" in asked
    assert m.get("no_reply") == "(sem resposta)" and m.get("hello", help="a") == "Olá! a"
    assert json.loads(path.read_text(encoding="utf-8"))["messages"]["no_reply"] == "(sem resposta)"
    assert m.missing() == [] and m.fill_missing(llm=llm) == 0  # nothing left to translate


def test_a_missing_key_is_english_right_away_and_translated_in_the_background(path):
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"language": "Portuguese (Brazil)", "messages": {"hello": "Olá! {help}"}}), encoding="utf-8")
    llm = FakeLLM(translation=full_pt() | {"no_reply": "(sem resposta)"})
    m = Messages(path, auto_fill=True, llm_factory=lambda: llm)
    assert m.get("no_reply") in ("(no reply)", "(sem resposta)")  # English if the thread has not finished
    for _ in range(200):  # wait for the background translation to be saved
        if "no_reply" in json.loads(path.read_text(encoding="utf-8"))["messages"]:
            break
        time.sleep(0.02)
    assert m.get("no_reply") == "(sem resposta)" and m.missing() == []
    assert json.loads(path.read_text(encoding="utf-8"))["messages"]["no_reply"] == "(sem resposta)"


def test_failed_fill_keeps_english_and_does_not_loop(path):
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"language": "Portuguese", "messages": {}}), encoding="utf-8")
    m = Messages(path)
    llm = FakeLLM(fail=True)
    assert m.fill_missing(llm=llm) == 0 and len(llm.calls) == 1
    assert m.fill_missing(llm=llm) == 0 and len(llm.calls) == 1  # back-off
    assert m.get("no_reply") == "(no reply)"


def test_use_switches_the_language_of_t(path):
    m = Messages(path)
    m.ensure("oi", llm=FakeLLM())
    previous = i18n.current()
    i18n.use(m)
    try:
        assert i18n.t("briefing_greeting") == "Bom dia!"
    finally:
        i18n.use(previous)
    assert i18n.t("briefing_greeting") == "Good morning!"
