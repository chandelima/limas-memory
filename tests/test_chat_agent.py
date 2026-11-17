from datetime import datetime, timezone
from typing import ClassVar

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from chat_agent.agent import _to_messages, build_chat_agent, load_system_prompt, reply
from curator.agent import ChatMessage
from curator.tools import build_read_tools, build_tools
from storage.git import MemoryGit
from storage.store import MemoryStore

DOC = "---\ndescription: Coffee preferences\n---\n# Coffee\nNo sugar.\n"


class ScriptedModel(FakeMessagesListChatModel):
    bound_calls: ClassVar[list] = []  # names of the tools passed to bind_tools

    def bind_tools(self, tools, **kwargs):
        type(self).bound_calls.append([t.name for t in tools])
        return self


def tool_call(name, args, id):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": id}])


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "memory")
    s.write_doc("preferences/coffee.md", DOC)
    return s


def test_read_tools_are_read_only(store, tmp_path):
    names = {t.name for t in build_read_tools(store)}
    assert names == {"list_memory", "search_memory", "read_memory"}
    full = {t.name for t in build_tools(store, MemoryGit(tmp_path / "memory"))}
    assert names < full and {"write_memory", "update_index", "commit_memory"} <= full


def test_chat_agent_only_gets_read_tools(store):
    ScriptedModel.bound_calls.clear()
    model = ScriptedModel(responses=[AIMessage(content="hi")])
    agent = build_chat_agent(llm=model, tools=build_read_tools(store))
    reply([ChatMessage("user", "hi")], agent=agent)  # bind_tools happens on invoke
    assert set(ScriptedModel.bound_calls[-1]) == {"list_memory", "search_memory", "read_memory"}


def test_prompt_is_read_only_and_does_not_promise_saving():
    prompt = load_system_prompt()
    assert "READ-ONLY" in prompt
    assert "do NOT write" in prompt and "was\n  saved" in prompt
    assert "contested" in prompt  # asks for clarification on conflicts


def test_history_conversion_adds_now_only_to_last_user_message():
    now = datetime(2026, 11, 17, 9, 30, tzinfo=timezone.utc)
    history = [
        ChatMessage("user", "hi"),
        ChatMessage("assistant", "hello"),
        ChatMessage("user", "what do I drink?"),
    ]
    msgs = _to_messages(history, now)
    assert [type(m).__name__ for m in msgs] == ["HumanMessage", "AIMessage", "HumanMessage"]
    assert msgs[0].content == "hi"
    assert msgs[2].content == "[now: 2026-11-17T09:30+00:00]\nwhat do I drink?"


def test_reply_requires_history_ending_with_user():
    with pytest.raises(ValueError):
        reply([])
    with pytest.raises(ValueError):
        reply([ChatMessage("assistant", "hi")])


def test_reply_consults_memory_and_returns_text_and_trace(store):
    script = [
        tool_call("list_memory", {}, "1"),
        tool_call("read_memory", {"path": "preferences/coffee.md"}, "2"),
        AIMessage(content="You drink coffee without sugar."),
    ]
    agent = build_chat_agent(llm=ScriptedModel(responses=script), tools=build_read_tools(store))
    result = reply([ChatMessage("user", "How do I take my coffee?")], agent=agent)
    assert result.text == "You drink coffee without sugar."
    assert [e.name for e in result.trace] == ["list_memory", "read_memory"]
    assert "preferences/coffee.md" in result.trace[0].result
    assert "No sugar." in result.trace[1].result


def test_reply_cannot_write_even_if_model_tries(store):
    script = [
        tool_call("write_memory", {"path": "x.md", "content": DOC}, "1"),
        AIMessage(content="I could not write it."),
    ]
    agent = build_chat_agent(llm=ScriptedModel(responses=script), tools=build_read_tools(store))
    try:
        reply([ChatMessage("user", "write this down")], agent=agent)
    except Exception:
        pass  # nonexistent tool: the agent may refuse the call
    assert not (store.root / "x.md").exists()


def test_prompt_asks_reading_documents_before_answering_and_has_no_style_section():
    prompt = load_system_prompt()
    assert "READ it with" in prompt
    assert "STYLE" not in prompt  # plain text is the gateway's job (to_plain)
