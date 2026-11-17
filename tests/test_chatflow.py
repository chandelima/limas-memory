import pytest

import chatflow
from chat_agent.agent import ReplyResult
from chatflow import Conversation, describe_outcome, describe_status
from conversation.log import ConversationLog
from curator.agent import CurateResult, ToolEvent
from curator.service import CurationService, Outcome


class FakeCurator:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, messages, context, channel):
        self.calls.append(([m.text for m in messages], context, channel))
        if self.fail:
            raise RuntimeError("no network")
        return CurateResult(report="Wrote 1 document.", trace=[ToolEvent("commit_memory", {}, "OK")])


@pytest.fixture
def seen_histories(monkeypatch):
    """Swap the chat agent for fixed replies and keep the history it received."""
    seen, answers = [], iter(["Noted", "You're welcome", "Sure"])

    def fake_reply(history, agent=None, now=None, language="", on_step=None):
        seen.append([(m.role, m.text) for m in history])
        return ReplyResult(text=next(answers, "ok"))

    monkeypatch.setattr(chatflow, "reply", fake_reply)
    return seen


def make(tmp_path, curator=None, log=None):
    log = log or ConversationLog(tmp_path / "conversation.json")
    service = CurationService(log, curator or FakeCurator(), channel="Telegram", idle_seconds=600)
    return Conversation(log, service, agent=object()), log


def test_respond_logs_both_sides_and_passes_recent_history(tmp_path, seen_histories):
    conv, log = make(tmp_path)
    conv.respond("I drink coffee")
    conv.respond("thanks")
    assert [(m.role, m.text) for m in log.messages()] == [
        ("user", "I drink coffee"), ("assistant", "Noted"), ("user", "thanks"), ("assistant", "You're welcome"),
    ]
    assert seen_histories[1] == [("user", "I drink coffee"), ("assistant", "Noted"), ("user", "thanks")]


def test_history_persists_across_conversations_on_the_same_log(tmp_path, seen_histories):
    path = tmp_path / "conversation.json"
    make(tmp_path, log=ConversationLog(path))[0].respond("first session")
    make(tmp_path, log=ConversationLog(path))[0].respond("second session")
    assert seen_histories[1] == [("user", "first session"), ("assistant", "Noted"), ("user", "second session")]


def test_curate_now_processes_pending_messages(tmp_path, seen_histories):
    curator = FakeCurator()
    conv, log = make(tmp_path, curator)
    conv.respond("I drink coffee")
    out = conv.curate_now()
    assert "[curator] 2 message(s) processed (commit_memory)" in out and "Wrote 1 document." in out
    assert curator.calls == [(["I drink coffee", "Noted"], "", "Telegram")]
    assert log.pending() == []
    assert conv.curate_now() == "[curator] nothing pending."


def test_curate_now_failure_is_reported_and_messages_stay_pending(tmp_path, seen_histories):
    conv, log = make(tmp_path, FakeCurator(fail=True))
    conv.respond("hi")
    out = conv.curate_now()
    assert "[curator] failed" in out and "no network" in out
    assert len(log.pending()) == 2


def test_curate_now_while_another_curation_runs_says_so(tmp_path, seen_histories):
    conv, log = make(tmp_path)
    conv.respond("something pending")
    conv.service._busy.acquire()  # simulates an automatic curation in progress
    try:
        out = conv.curate_now()
    finally:
        conv.service._busy.release()
    assert "a curation is already running" in out and "nothing pending" not in out
    assert len(log.pending()) == 2  # nothing was processed


def test_status_reports_pending_idle_and_last_error(tmp_path, seen_histories):
    conv, _ = make(tmp_path, FakeCurator(fail=True))
    conv.respond("hi")
    assert "pending: 2" in conv.status() and "curation after 600s" in conv.status()
    conv.curate_now()
    assert "last error: RuntimeError: no network" in conv.status()


def test_describe_outcome_variants():
    ok = Outcome(3, result=CurateResult(report="Report", trace=[]))
    assert "3 message(s)" in describe_outcome(ok) and "no tools" in describe_outcome(ok)
    assert "failed" in describe_outcome(Outcome(2, error="boom"))
    assert "a curation is already running" in describe_outcome(Outcome(0, busy=True))


def test_status_mentions_running_curation_only_while_it_runs(tmp_path):
    log = ConversationLog(tmp_path / "c.json")
    log.append("user", "hi")
    seen = {}

    def curator(messages, context, channel):
        seen["during"] = describe_status(seen["service"])
        return CurateResult(report="ok")

    service = CurationService(log, curator, channel="t")
    seen["service"] = service
    service.run_now()
    assert "curation running" in seen["during"]
    assert "curation running" not in describe_status(service)


def test_the_known_language_reaches_the_chat_agent(tmp_path, monkeypatch):
    import i18n

    seen = {}

    def fake_reply(history, agent=None, now=None, language="", on_step=None):
        seen["language"] = language
        return ReplyResult(text="ok")

    monkeypatch.setattr(chatflow, "reply", fake_reply)
    conv, _ = make(tmp_path)
    conv.respond("oi")
    assert seen["language"] == ""  # not set up yet
    current = i18n.current()
    current.language, current.configured = "Portuguese (Brazil)", True
    conv.respond("oi de novo")
    assert seen["language"] == "Portuguese (Brazil)"
