import asyncio
from datetime import datetime, timedelta

from chatflow import describe_outcome, describe_status
from conversation.log import ConversationLog
from curator.agent import CurateResult
from curator.service import CurationService, Outcome

T0 = datetime.fromisoformat("2026-11-17T10:00:00-03:00")


def make_service(tmp_path, curator, **kw):
    log = ConversationLog(tmp_path / "c.json")
    log.append("usuario", "oi", at=T0.isoformat())
    return CurationService(log, curator, channel="Telegram", **kw)


def test_tick_returns_none_while_busy_so_nobody_is_notified(tmp_path):
    holder = {}

    def reentrant(messages, context, channel):
        holder["inner_tick"] = holder["service"].tick(T0 + timedelta(hours=2))
        holder["running"] = holder["service"].is_running
        return CurateResult(report="ok")

    service = make_service(tmp_path, reentrant)
    holder["service"] = service
    assert service.run_now().ok
    assert holder["inner_tick"] is None and holder["running"] is True
    assert not service.is_running  # liberado ao terminar


def test_describe_outcome_for_busy():
    assert "já há uma curadoria em andamento" in describe_outcome(Outcome(0, busy=True))


def test_status_mentions_running_curation_only_while_it_runs(tmp_path):
    seen = {}

    def curator(messages, context, channel):
        seen["during"] = describe_status(seen["service"])
        return CurateResult(report="ok")

    service = make_service(tmp_path, curator)
    seen["service"] = service
    service.run_now()
    assert "curadoria em andamento" in seen["during"]
    assert "curadoria em andamento" not in describe_status(service)


def test_watch_awaits_async_callbacks_and_survives_callback_errors(tmp_path, caplog):
    service = make_service(tmp_path, lambda m, c, ch: CurateResult(report="ok"))
    events = []

    async def scenario():
        stop = asyncio.Event()

        async def on_outcome(outcome):
            events.append(outcome)
            stop.set()
            raise RuntimeError("callback com defeito")

        await asyncio.wait_for(service.watch(stop, interval=0.01, on_outcome=on_outcome), timeout=5)

    asyncio.run(scenario())
    assert len(events) == 1  # o laço terminou normalmente apesar do erro no callback
    assert "erro ao tratar o resultado" in caplog.text
