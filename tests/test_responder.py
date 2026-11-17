from datetime import datetime, timezone
from typing import ClassVar

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

import chat as chat_cli
from curator.agent import ChatMessage
from curator.tools import build_read_tools, build_tools
from responder.agent import _to_messages, build_responder, load_system_prompt, reply
from storage.git import MemoryGit
from storage.store import MemoryStore

DOC = "---\ndescription: Preferências de café\n---\n# Café\nSem açúcar.\n"


class ScriptedModel(FakeMessagesListChatModel):
    bound_calls: ClassVar[list] = []  # nomes das tools passadas a bind_tools

    def bind_tools(self, tools, **kwargs):
        type(self).bound_calls.append([t.name for t in tools])
        return self


def tool_call(name, args, id):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": id}])


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "memory")
    s.write_doc("preferencias/cafe.md", DOC)
    return s


def test_read_tools_are_read_only(store, tmp_path):
    names = {t.name for t in build_read_tools(store)}
    assert names == {"list_memory", "search_memory", "read_memory"}
    full = {t.name for t in build_tools(store, MemoryGit(tmp_path / "memory"))}
    assert names < full and {"write_memory", "update_index", "commit_memory"} <= full


def test_responder_agent_only_gets_read_tools(store):
    ScriptedModel.bound_calls.clear()
    model = ScriptedModel(responses=[AIMessage(content="oi")])
    agent = build_responder(llm=model, tools=build_read_tools(store))
    reply([ChatMessage("usuario", "oi")], agent=agent)  # o bind_tools ocorre ao invocar
    assert set(ScriptedModel.bound_calls[-1]) == {"list_memory", "search_memory", "read_memory"}


def test_prompt_is_read_only_and_does_not_promise_saving():
    prompt = load_system_prompt()
    assert "SOMENTE DE LEITURA" in prompt
    assert "NÃO escreve" in prompt and "foi salvo" in prompt
    assert "contestada" in prompt  # pede esclarecimento sobre conflitos


def test_history_conversion_adds_now_only_to_last_user_message():
    now = datetime(2026, 11, 17, 9, 30, tzinfo=timezone.utc)
    history = [
        ChatMessage("usuario", "oi"),
        ChatMessage("assistente", "olá"),
        ChatMessage("usuario", "o que eu bebo?"),
    ]
    msgs = _to_messages(history, now)
    assert [type(m).__name__ for m in msgs] == ["HumanMessage", "AIMessage", "HumanMessage"]
    assert msgs[0].content == "oi"
    assert msgs[2].content == "[agora: 2026-11-17T09:30+00:00]\no que eu bebo?"


def test_reply_requires_history_ending_with_user():
    with pytest.raises(ValueError):
        reply([])
    with pytest.raises(ValueError):
        reply([ChatMessage("assistente", "oi")])


def test_reply_consults_memory_and_returns_text_and_trace(store):
    script = [
        tool_call("list_memory", {}, "1"),
        tool_call("read_memory", {"path": "preferencias/cafe.md"}, "2"),
        AIMessage(content="Você toma café sem açúcar."),
    ]
    agent = build_responder(llm=ScriptedModel(responses=script), tools=build_read_tools(store))
    result = reply([ChatMessage("usuario", "Como eu tomo café?")], agent=agent)
    assert result.text == "Você toma café sem açúcar."
    assert [e.name for e in result.trace] == ["list_memory", "read_memory"]
    assert "preferencias/cafe.md" in result.trace[0].result
    assert "Sem açúcar." in result.trace[1].result


def test_reply_cannot_write_even_if_model_tries(store):
    script = [
        tool_call("write_memory", {"path": "x.md", "content": DOC}, "1"),
        AIMessage(content="Não consegui gravar."),
    ]
    agent = build_responder(llm=ScriptedModel(responses=script), tools=build_read_tools(store))
    try:
        reply([ChatMessage("usuario", "grave isso")], agent=agent)
    except Exception:
        pass  # tool inexistente: o agente pode recusar a chamada
    assert not (store.root / "x.md").exists()


def test_chat_cli_loop_keeps_history_and_exits(store, capsys):
    script = [AIMessage(content="Oi!"), AIMessage(content="Tchau!")]
    model = ScriptedModel(responses=script)
    agent = build_responder(llm=model, tools=build_read_tools(store))
    inputs = iter(["olá", "", "valeu", "sair"])
    code = chat_cli.main([], agent=agent, read=lambda _: next(inputs))
    out = capsys.readouterr().out
    assert code == 0
    assert "IA: Oi!" in out and "IA: Tchau!" in out


def test_chat_cli_verbose_shows_memory_calls(store, capsys):
    script = [tool_call("list_memory", {}, "1"), AIMessage(content="Achei.")]
    agent = build_responder(llm=ScriptedModel(responses=script), tools=build_read_tools(store))
    inputs = iter(["o que sei de café?", "sair"])
    chat_cli.main(["--verbose"], agent=agent, read=lambda _: next(inputs))
    assert "[memória] list_memory()" in capsys.readouterr().out


def test_chat_cli_without_api_key_returns_1(monkeypatch, capsys, tmp_path):
    import config

    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    assert chat_cli.main(["--memory-dir", str(tmp_path / "m")]) == 1
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err
