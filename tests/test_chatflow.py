import pytest

import chatflow
from chatflow import Conversation, describe_outcome, describe_status
from conversation.log import ConversationLog
from curator.agent import CurateResult, ToolEvent
from curator.service import CurationService, Outcome
from responder.agent import ReplyResult


class FakeCurator:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, messages, context, channel):
        self.calls.append(([m.text for m in messages], context, channel))
        if self.fail:
            raise RuntimeError("sem rede")
        return CurateResult(report="Gravei 1 documento.", trace=[ToolEvent("commit_memory", {}, "OK")])


@pytest.fixture
def seen_histories(monkeypatch):
    """Troca o agente de resposta por respostas fixas e guarda o histórico recebido."""
    seen, answers = [], iter(["Anotado", "De nada", "Claro"])

    def fake_reply(history, agent=None, now=None):
        seen.append([(m.role, m.text) for m in history])
        return ReplyResult(text=next(answers, "ok"))

    monkeypatch.setattr(chatflow, "reply", fake_reply)
    return seen


def make(tmp_path, curator=None, log=None):
    log = log or ConversationLog(tmp_path / "conversa.json")
    service = CurationService(log, curator or FakeCurator(), channel="Telegram", idle_seconds=600)
    return Conversation(log, service, agent=object()), log


def test_respond_logs_both_sides_and_passes_recent_history(tmp_path, seen_histories):
    conv, log = make(tmp_path)
    conv.respond("tomo café")
    conv.respond("valeu")
    assert [(m.role, m.text) for m in log.messages()] == [
        ("usuario", "tomo café"), ("assistente", "Anotado"), ("usuario", "valeu"), ("assistente", "De nada"),
    ]
    assert seen_histories[1] == [("usuario", "tomo café"), ("assistente", "Anotado"), ("usuario", "valeu")]


def test_history_persists_across_conversations_on_the_same_log(tmp_path, seen_histories):
    path = tmp_path / "conversa.json"
    make(tmp_path, log=ConversationLog(path))[0].respond("primeira sessão")
    make(tmp_path, log=ConversationLog(path))[0].respond("segunda sessão")
    assert seen_histories[1] == [("usuario", "primeira sessão"), ("assistente", "Anotado"), ("usuario", "segunda sessão")]


def test_curate_now_processes_pending_messages(tmp_path, seen_histories):
    curator = FakeCurator()
    conv, log = make(tmp_path, curator)
    conv.respond("tomo café")
    out = conv.curate_now()
    assert "[curador] 2 mensagem(ns) processada(s) (commit_memory)" in out and "Gravei 1 documento." in out
    assert curator.calls == [(["tomo café", "Anotado"], "", "Telegram")]
    assert log.pending() == []
    assert conv.curate_now() == "[curador] nada pendente."


def test_curate_now_failure_is_reported_and_messages_stay_pending(tmp_path, seen_histories):
    conv, log = make(tmp_path, FakeCurator(fail=True))
    conv.respond("oi")
    out = conv.curate_now()
    assert "[curador] falhou" in out and "sem rede" in out
    assert len(log.pending()) == 2


def test_curate_now_while_another_curation_runs_says_so(tmp_path, seen_histories):
    conv, log = make(tmp_path)
    conv.respond("algo pendente")
    conv.service._busy.acquire()  # simula uma curadoria automática em andamento
    try:
        out = conv.curate_now()
    finally:
        conv.service._busy.release()
    assert "já há uma curadoria em andamento" in out and "nada pendente" not in out
    assert len(log.pending()) == 2  # nada foi processado


def test_status_reports_pending_idle_and_last_error(tmp_path, seen_histories):
    conv, _ = make(tmp_path, FakeCurator(fail=True))
    conv.respond("oi")
    assert "pendentes: 2" in conv.status() and "curadoria após 600s" in conv.status()
    conv.curate_now()
    assert "último erro: RuntimeError: sem rede" in conv.status()


def test_describe_outcome_variants():
    ok = Outcome(3, result=CurateResult(report="Relatório", trace=[]))
    assert "3 mensagem(ns)" in describe_outcome(ok) and "nenhuma tool" in describe_outcome(ok)
    assert "falhou" in describe_outcome(Outcome(2, error="boom"))
    assert "já há uma curadoria em andamento" in describe_outcome(Outcome(0, busy=True))


def test_status_mentions_running_curation_only_while_it_runs(tmp_path):
    log = ConversationLog(tmp_path / "c.json")
    log.append("usuario", "oi")
    seen = {}

    def curator(messages, context, channel):
        seen["during"] = describe_status(seen["service"])
        return CurateResult(report="ok")

    service = CurationService(log, curator, channel="t")
    seen["service"] = service
    service.run_now()
    assert "curadoria em andamento" in seen["during"]
    assert "curadoria em andamento" not in describe_status(service)
