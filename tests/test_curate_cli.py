import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

import curate as cli
from curator.agent import build_curator
from curator.tools import build_tools
from storage.git import MemoryGit
from storage.store import MemoryStore

DOC = "---\ndescription: Coffee preferences\n---\n# Coffee\nNo sugar.\n"


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def tool_call(name, args, id):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": id}])


def test_parse_conversation_roles_and_continuations():
    text = "user: hi\ncontinues here\nassistant: hello\nUSER: bye\n"
    msgs = cli.parse_conversation(text)
    assert [(m.role, m.text) for m in msgs] == [
        ("user", "hi\ncontinues here"),
        ("assistant", "hello"),
        ("user", "bye"),
    ]


def test_parse_conversation_without_prefixes_is_one_user_message():
    msgs = cli.parse_conversation("I prefer coffee\nwithout sugar\n\n")
    assert [(m.role, m.text) for m in msgs] == [("user", "I prefer coffee\nwithout sugar")]


def test_parse_conversation_empty():
    assert cli.parse_conversation("\n  \n") == []


def test_example_scenarios_are_parseable():
    from pathlib import Path

    files = sorted(Path(__file__).parent.parent.joinpath("evals", "scenarios").glob("*.txt"))
    assert len(files) == 5
    for f in files:
        assert cli.parse_conversation(f.read_text(encoding="utf-8")), f.name


def test_main_runs_and_prints_report_and_diff(tmp_path, capsys):
    mem = tmp_path / "memory"
    conv = tmp_path / "conv.txt"
    conv.write_text("user: I drink coffee without sugar\n", encoding="utf-8")
    store, git = MemoryStore(mem), MemoryGit(mem)
    script = [
        tool_call("write_memory", {"path": "preferences/coffee.md", "content": DOC}, "1"),
        tool_call("update_index", {}, "2"),
        tool_call("commit_memory", {"message": "docs: record coffee"}, "3"),
        AIMessage(content="Created: preferences/coffee.md."),
    ]
    agent = build_curator(llm=ScriptedModel(responses=script), tools=build_tools(store, git))

    code = cli.main([str(conv), "--memory-dir", str(mem)], agent=agent)
    out = capsys.readouterr().out

    assert code == 0
    assert "write_memory -> update_index -> commit_memory" in out
    assert "Created: preferences/coffee.md." in out
    assert "+++ b/preferences/coffee.md" in out and "docs: record coffee" in out


def test_main_nothing_saved_reports_no_commit(tmp_path, capsys):
    mem = tmp_path / "memory"
    conv = tmp_path / "conv.txt"
    conv.write_text("user: good morning\n", encoding="utf-8")
    store, git = MemoryStore(mem), MemoryGit(mem)
    agent = build_curator(
        llm=ScriptedModel(responses=[AIMessage(content="Nothing to write.")]),
        tools=build_tools(store, git),
    )
    assert cli.main([str(conv), "--memory-dir", str(mem)], agent=agent) == 0
    assert "(no new commit)" in capsys.readouterr().out


def test_main_empty_conversation_returns_2(tmp_path, capsys):
    conv = tmp_path / "empty.txt"
    conv.write_text("\n", encoding="utf-8")
    assert cli.main([str(conv), "--memory-dir", str(tmp_path / "m")]) == 2


def test_main_without_api_key_returns_1(tmp_path, monkeypatch, capsys):
    import config

    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    conv = tmp_path / "conv.txt"
    conv.write_text("user: hi\n", encoding="utf-8")
    assert cli.main([str(conv), "--memory-dir", str(tmp_path / "m")]) == 1
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err


def test_describe_args_truncates_long_values():
    out = cli.describe_args({"path": "a.md", "content": "x" * 200})
    assert "path='a.md'" in out and "(200 chars)" in out and "x" * 100 not in out


def test_verbose_shows_each_tool_and_result(tmp_path, capsys):
    mem = tmp_path / "memory"
    conv = tmp_path / "conv.txt"
    conv.write_text("user: hi\n", encoding="utf-8")
    store, git = MemoryStore(mem), MemoryGit(mem)
    script = [
        tool_call("write_memory", {"path": "a.md", "content": "no frontmatter"}, "1"),
        AIMessage(content="Failed."),
    ]
    agent = build_curator(llm=ScriptedModel(responses=script), tools=build_tools(store, git))
    cli.main([str(conv), "--memory-dir", str(mem), "--verbose"], agent=agent)
    out = capsys.readouterr().out
    assert "== Tool details ==" in out
    assert "1. write_memory(path='a.md'" in out and "-> ERROR" in out


def test_default_output_has_no_tool_detail(tmp_path, capsys):
    mem = tmp_path / "memory"
    conv = tmp_path / "conv.txt"
    conv.write_text("user: hi\n", encoding="utf-8")
    store, git = MemoryStore(mem), MemoryGit(mem)
    agent = build_curator(
        llm=ScriptedModel(responses=[AIMessage(content="Nothing.")]), tools=build_tools(store, git)
    )
    cli.main([str(conv), "--memory-dir", str(mem)], agent=agent)
    assert "Tool details" not in capsys.readouterr().out
