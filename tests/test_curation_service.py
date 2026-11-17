import asyncio
from datetime import datetime, timedelta

import pytest

from conversation.log import ConversationLog
from curator.agent import CurateResult, ToolEvent
from curator.service import CurationService, format_context

T0 = datetime.fromisoformat("2026-11-17T10:00:00-03:00")


def at(minutes):
    return (T0 + timedelta(minutes=minutes)).isoformat()


class FakeCurator:
    def __init__(self, fail_times=0):
        self.calls = []  # (mensagens, contexto, canal)
        self.fail_times = fail_times

    def __call__(self, messages, context, channel):
        self.calls.append((messages, context, channel))
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("API fora do ar")
        return CurateResult(report="ok", trace=[ToolEvent("commit_memory", {}, "OK")])


@pytest.fixture
def log(tmp_path):
    return ConversationLog(tmp_path / "c.json")


def service(log, curator, **kw):
    kw.setdefault("idle_seconds", 600)
    kw.setdefault("retry_seconds", 300)
    return CurationService(log, curator, channel="Telegram", **kw)


def test_does_not_run_before_idle_period(log):
    curator = FakeCurator()
    log.append("usuario", "oi", at=at(0))
    s = service(log, curator)
    assert s.tick(T0 + timedelta(minutes=9, seconds=59)) is None
    assert curator.calls == []


def test_runs_after_idle_period_and_marks_curated(log):
    curator = FakeCurator()
    log.append("usuario", "tomo café sem açúcar", at=at(0))
    log.append("assistente", "anotado", at=at(1))
    s = service(log, curator)
    assert s.tick(T0 + timedelta(minutes=10)) is None  # só 9 min desde a última (min 1)
    outcome = s.tick(T0 + timedelta(minutes=11))
    assert outcome.ok and outcome.messages == 2 and outcome.result.report == "ok"
    messages, context, channel = curator.calls[0]
    assert [(m.role, m.text, m.at) for m in messages] == [
        ("usuario", "tomo café sem açúcar", at(0)), ("assistente", "anotado", at(1)),
    ]
    assert channel == "Telegram" and context == ""
    assert log.pending() == []
    assert s.tick(T0 + timedelta(hours=1)) is None  # nada novo: não roda de novo


def test_new_message_resets_the_idle_timer(log):
    curator = FakeCurator()
    log.append("usuario", "a", at=at(0))
    s = service(log, curator)
    log.append("usuario", "b", at=at(8))
    assert s.tick(T0 + timedelta(minutes=12)) is None  # 4 min desde a última
    assert s.tick(T0 + timedelta(minutes=18)).messages == 2


def test_context_has_previous_curated_messages_only(log):
    curator = FakeCurator()
    for i in range(3):
        log.append("usuario", f"antiga {i}", at=at(i))
    log.mark_curated(3)
    log.append("usuario", "nova", at=at(5))
    service(log, curator).tick(T0 + timedelta(minutes=30))
    messages, context, _ = curator.calls[0]
    assert [m.text for m in messages] == ["nova"]
    assert "JÁ processadas" in context and "antiga 0" in context and "antiga 2" in context
    assert "nova" not in context


def test_failure_keeps_messages_pending_and_waits_before_retry(log):
    curator = FakeCurator(fail_times=1)
    log.append("usuario", "oi", at=at(0))
    s = service(log, curator)
    first = s.tick(T0 + timedelta(minutes=11))
    assert not first.ok and "API fora do ar" in first.error and s.last_error
    assert len(log.pending()) == 1
    assert s.tick(T0 + timedelta(minutes=13)) is None  # dentro da espera de 5 min
    assert len(curator.calls) == 1
    retry = s.tick(T0 + timedelta(minutes=17))
    assert retry.ok and log.pending() == [] and s.last_error == ""


def test_messages_arriving_during_curation_stay_pending(log):
    log.append("usuario", "a", at=at(0))

    def curator(messages, context, channel):
        log.append("usuario", "chegou durante", at=at(11))
        return CurateResult(report="ok")

    s = service(log, curator)
    assert s.tick(T0 + timedelta(minutes=11)).messages == 1
    assert [m.text for m in log.pending()] == ["chegou durante"]


def test_run_now_ignores_idle_and_returns_none_when_nothing_pending(log):
    curator = FakeCurator()
    s = service(log, curator)
    assert s.run_now() is None
    log.append("usuario", "oi", at=at(0))
    assert s.run_now(T0 + timedelta(seconds=5)).ok  # sem esperar a inatividade


def test_run_now_is_ignored_while_another_curation_is_running(log):
    log.append("usuario", "oi", at=at(0))
    holder = {}

    def reentrant(messages, context, channel):
        holder["inner"] = holder["service"].run_now()  # tenta rodar durante a curadoria
        return CurateResult(report="ok")

    s = service(log, reentrant)
    holder["service"] = s
    assert s.run_now().ok
    assert holder["inner"].busy and holder["inner"].messages == 0  # avisa que está ocupado


def test_seconds_idle_and_due(log):
    s = service(log, FakeCurator())
    assert s.seconds_idle() is None and not s.due()
    log.append("usuario", "oi", at=at(0))
    assert s.seconds_idle(T0 + timedelta(seconds=90)) == 90
    assert s.due(T0 + timedelta(minutes=10)) and not s.due(T0 + timedelta(minutes=9))


def test_format_context_empty():
    assert format_context([]) == ""


def test_watch_runs_tick_until_stopped(log):
    curator = FakeCurator()
    log.append("usuario", "oi", at=at(0))  # muito antiga: já inativa
    s = service(log, curator)
    seen = []

    async def scenario():
        stop = asyncio.Event()

        def on_outcome(o):
            seen.append(o)
            stop.set()

        await asyncio.wait_for(s.watch(stop, interval=0.01, on_outcome=on_outcome), timeout=5)

    asyncio.run(scenario())
    assert len(seen) == 1 and seen[0].ok and len(curator.calls) == 1
