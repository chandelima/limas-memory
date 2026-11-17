import subprocess

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from curator.agent import ChatMessage, build_curator, curate, format_input, load_system_prompt
from curator.tools import build_tools
from storage.git import MemoryGit
from storage.store import MemoryStore

DOC = "---\ndescription: Coffee preferences\n---\n# Coffee\nLikes coffee without sugar (source: Telegram).\n"


class ScriptedModel(FakeMessagesListChatModel):
    """Fake model that returns predefined replies and accepts bind_tools."""

    def bind_tools(self, tools, **kwargs):
        return self


def call(name, args, id):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": id}])


@pytest.fixture
def mem(tmp_path):
    root = tmp_path / "memory"
    root.mkdir()
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    store, git = MemoryStore(root), MemoryGit(root, remote=str(bare))
    return root, build_tools(store, git), git


def test_system_prompt_has_rules_and_workflow():
    prompt = load_system_prompt()
    assert "Memory Curator" in prompt and "WORKFLOW" in prompt
    assert "commit_memory" in prompt and "description:" in prompt


def test_format_input_includes_context_messages_and_timestamp():
    text = format_input(
        [ChatMessage("user", "I prefer coffee without sugar", at="2026-11-17T10:00")],
        context="Conversation about habits",
    )
    assert "Conversation about habits" in text
    assert "- [user (2026-11-17T10:00)] I prefer coffee without sugar" in text


def test_curate_rejects_empty_messages():
    with pytest.raises(ValueError):
        curate([])


def test_agent_runs_tools_writes_commits_and_reports(mem):
    root, tools, git = mem
    script = [
        call("list_memory", {}, "1"),
        call("write_memory", {"path": "preferences/coffee.md", "content": DOC}, "2"),
        call("update_index", {}, "3"),
        call("commit_memory", {"message": "docs: record coffee preference"}, "4"),
        AIMessage(content="Created: preferences/coffee.md. Ignored: greeting."),
    ]
    agent = build_curator(llm=ScriptedModel(responses=script), tools=tools)
    result = curate([ChatMessage("user", "I prefer coffee without sugar")], agent=agent)

    assert result.tool_calls == ["list_memory", "write_memory", "update_index", "commit_memory"]
    assert result.report == "Created: preferences/coffee.md. Ignored: greeting."
    assert (root / "preferences" / "coffee.md").read_text(encoding="utf-8") == DOC
    assert "coffee.md" in (root / "index.md").read_text(encoding="utf-8")
    assert git.log(1)[0].endswith("docs: record coffee preference")


def test_agent_with_nothing_worth_saving_writes_nothing(mem):
    root, tools, git = mem
    script = [
        call("list_memory", {}, "1"),
        AIMessage(content="Nothing of future value; nothing written."),
    ]
    agent = build_curator(llm=ScriptedModel(responses=script), tools=tools)
    result = curate([ChatMessage("user", "hi, how are you?")], agent=agent)
    assert result.tool_calls == ["list_memory"]
    assert list(root.rglob("*.md")) == [] and git.log() == []


def test_tool_error_is_visible_to_agent_which_can_retry(mem):
    root, tools, _ = mem
    script = [
        call("write_memory", {"path": "a.md", "content": "no frontmatter"}, "1"),
        call("write_memory", {"path": "a.md", "content": DOC}, "2"),
        AIMessage(content="Fixed and written."),
    ]
    agent = build_curator(llm=ScriptedModel(responses=script), tools=tools)
    result = curate([ChatMessage("user", "x")], agent=agent)
    assert result.tool_calls == ["write_memory", "write_memory"]
    assert (root / "a.md").exists()


def test_format_input_includes_current_datetime():
    from datetime import datetime, timezone

    now = datetime(2026, 11, 17, 9, 30, tzinfo=timezone.utc)
    text = format_input([ChatMessage("user", "hi")], now=now)
    assert text.startswith("CURRENT DATE AND TIME: 2026-11-17T09:30+00:00")


def test_prompt_guides_dates_quotes_and_no_process_notes():
    prompt = load_system_prompt()
    assert "absolute dates" in prompt and "CURRENT DATE AND TIME" in prompt
    assert "in double" in prompt and "quotes" in prompt
    assert "Curator's process" in prompt


def test_format_input_includes_channel_with_default():
    assert "CHANNEL: unknown" in format_input([ChatMessage("user", "hi")])
    assert "CHANNEL: Telegram" in format_input([ChatMessage("user", "hi")], channel="Telegram")


def test_prompt_forbids_inventing_channel_and_forcing_links():
    prompt = load_system_prompt()
    assert "Never deduce or invent\n  the channel" in prompt
    assert "do not force connections" in prompt  # link whenever possible, but never by force
    assert "ONCE per\n  document" in prompt


def test_curator_uses_temperature_zero(monkeypatch):
    import curator.agent as agent_mod

    seen = {}

    def fake_get_llm(**kwargs):
        seen.update(kwargs)
        return ScriptedModel(responses=[AIMessage(content="ok")])

    monkeypatch.setattr(agent_mod, "get_llm", fake_get_llm)
    build_curator(tools=[])
    assert seen == {"model": None, "temperature": 0}


def test_trace_pairs_each_call_with_its_result(mem):
    root, tools, _ = mem
    script = [
        call("write_memory", {"path": "a.md", "content": "no frontmatter"}, "1"),
        call("write_memory", {"path": "a.md", "content": DOC}, "2"),
        AIMessage(content="ok"),
    ]
    agent = build_curator(llm=ScriptedModel(responses=script), tools=tools)
    result = curate([ChatMessage("user", "x")], agent=agent, channel="Telegram")
    assert [e.name for e in result.trace] == ["write_memory", "write_memory"]
    assert result.trace[0].result.startswith("ERROR")
    assert result.trace[1].result.startswith("OK")
    assert result.trace[1].args["path"] == "a.md"


def test_prompt_asks_for_english_frontmatter_keys():
    prompt = load_system_prompt()
    assert "frontmatter KEYS are ALWAYS in English" in prompt
    for key in ("`description`", "`origin_date`", "`channel`"):
        assert key in prompt
    assert "`CHANNEL:`" in prompt  # the channel is still provided and recorded


def test_prompt_defines_title_contested_and_hypothesis_conventions():
    prompt = load_system_prompt()
    assert "Start the body of every document with a clear `# `" in prompt
    assert "## Contested" in prompt and "under review" in prompt
    assert "## Hypotheses (unverified)" in prompt


def test_format_input_tells_the_curator_the_users_language_when_known():
    assert "USER LANGUAGE" not in format_input([ChatMessage("user", "hi")])
    text = format_input([ChatMessage("user", "oi")], channel="Telegram", language="Portuguese (Brazil)")
    assert "USER LANGUAGE: Portuguese (Brazil)" in text
    assert "`USER LANGUAGE:`" in load_system_prompt()
