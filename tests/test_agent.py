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
