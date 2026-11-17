import subprocess

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from curator.agent import ChatMessage, build_curator, curate, format_input, load_system_prompt
from curator.tools import build_tools
from storage.git import MemoryGit
from storage.store import MemoryStore

DOC = "---\ndescription: Preferências de café\n---\n# Café\nGosta de café sem açúcar (fonte: Telegram).\n"


class ScriptedModel(FakeMessagesListChatModel):
    """Modelo falso que devolve respostas pré-definidas e aceita bind_tools."""

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
    assert "Memory Curator" in prompt and "FLUXO DE TRABALHO" in prompt
    assert "commit_memory" in prompt and "description:" in prompt


def test_format_input_includes_context_messages_and_timestamp():
    text = format_input(
        [ChatMessage("usuario", "Prefiro café sem açúcar", at="2026-11-17T10:00")],
        context="Conversa sobre hábitos",
    )
    assert "Conversa sobre hábitos" in text
    assert "- [usuario (2026-11-17T10:00)] Prefiro café sem açúcar" in text


def test_curate_rejects_empty_messages():
    with pytest.raises(ValueError):
        curate([])


def test_agent_runs_tools_writes_commits_and_reports(mem):
    root, tools, git = mem
    script = [
        call("list_memory", {}, "1"),
        call("write_memory", {"path": "preferencias/cafe.md", "content": DOC}, "2"),
        call("update_index", {}, "3"),
        call("commit_memory", {"message": "docs: registra preferência de café"}, "4"),
        AIMessage(content="Criado: preferencias/cafe.md. Ignorado: saudação."),
    ]
    agent = build_curator(llm=ScriptedModel(responses=script), tools=tools)
    result = curate([ChatMessage("usuario", "Prefiro café sem açúcar")], agent=agent)

    assert result.tool_calls == ["list_memory", "write_memory", "update_index", "commit_memory"]
    assert result.report == "Criado: preferencias/cafe.md. Ignorado: saudação."
    assert (root / "preferencias" / "cafe.md").read_text(encoding="utf-8") == DOC
    assert "cafe.md" in (root / "index.md").read_text(encoding="utf-8")
    assert git.log(1)[0].endswith("docs: registra preferência de café")


def test_agent_with_nothing_worth_saving_writes_nothing(mem):
    root, tools, git = mem
    script = [
        call("list_memory", {}, "1"),
        AIMessage(content="Nada com valor futuro; nada gravado."),
    ]
    agent = build_curator(llm=ScriptedModel(responses=script), tools=tools)
    result = curate([ChatMessage("usuario", "oi, tudo bem?")], agent=agent)
    assert result.tool_calls == ["list_memory"]
    assert list(root.rglob("*.md")) == [] and git.log() == []


def test_tool_error_is_visible_to_agent_which_can_retry(mem):
    root, tools, _ = mem
    script = [
        call("write_memory", {"path": "a.md", "content": "sem frontmatter"}, "1"),
        call("write_memory", {"path": "a.md", "content": DOC}, "2"),
        AIMessage(content="Corrigido e gravado."),
    ]
    agent = build_curator(llm=ScriptedModel(responses=script), tools=tools)
    result = curate([ChatMessage("usuario", "x")], agent=agent)
    assert result.tool_calls == ["write_memory", "write_memory"]
    assert (root / "a.md").exists()


def test_format_input_includes_current_datetime():
    from datetime import datetime, timezone

    now = datetime(2026, 11, 17, 9, 30, tzinfo=timezone.utc)
    text = format_input([ChatMessage("usuario", "oi")], now=now)
    assert text.startswith("DATA E HORA ATUAIS: 2026-11-17T09:30+00:00")


def test_prompt_guides_dates_quotes_and_no_process_notes():
    prompt = load_system_prompt()
    assert "datas absolutas" in prompt and "DATA E" in prompt
    assert "entre aspas" in prompt
    assert "processo do Curator" in prompt


def test_format_input_includes_channel_with_default():
    assert "CANAL: desconhecido" in format_input([ChatMessage("usuario", "oi")])
    assert "CANAL: Telegram" in format_input([ChatMessage("usuario", "oi")], channel="Telegram")


def test_prompt_forbids_inventing_channel_and_forcing_links():
    prompt = load_system_prompt()
    assert "Nunca deduza ou\n  invente o canal" in prompt
    assert "Não force links" in prompt
    assert "UMA vez por documento" in prompt


def test_curator_uses_temperature_zero(monkeypatch):
    import curator.agent as agent_mod

    seen = {}

    def fake_get_llm(**kwargs):
        seen.update(kwargs)
        return ScriptedModel(responses=[AIMessage(content="ok")])

    monkeypatch.setattr(agent_mod, "get_llm", fake_get_llm)
    build_curator(tools=[])
    assert seen == {"temperature": 0}


def test_trace_pairs_each_call_with_its_result(mem):
    root, tools, _ = mem
    script = [
        call("write_memory", {"path": "a.md", "content": "sem frontmatter"}, "1"),
        call("write_memory", {"path": "a.md", "content": DOC}, "2"),
        AIMessage(content="ok"),
    ]
    agent = build_curator(llm=ScriptedModel(responses=script), tools=tools)
    result = curate([ChatMessage("usuario", "x")], agent=agent, channel="Telegram")
    assert [e.name for e in result.trace] == ["write_memory", "write_memory"]
    assert result.trace[0].result.startswith("ERRO")
    assert result.trace[1].result.startswith("OK")
    assert result.trace[1].args["path"] == "a.md"
