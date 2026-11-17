import asyncio
from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import bot
import briefing as br
import config
from briefing import BriefingScheduler, build_briefing, parse_hhmm
from conversation.log import ConversationLog
from fakes import ALLOWED, FakeGateway
from storage.store import MemoryStore
from tasks.model import Recurrence
from tasks.store import TaskStore

TZ = timezone(timedelta(hours=-3))


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def clock():
    return Clock(at(18, 5, 0))  # quarta-feira, 05:00


@pytest.fixture
def tasks(tmp_path, clock):
    return TaskStore(MemoryStore(tmp_path / "memory"), today=lambda: clock().date())


@pytest.fixture
def scheduler(tasks, clock, tmp_path):
    return BriefingScheduler(tasks, time(6, 0), tmp_path / "briefing.json", clock=clock)


class Sink:
    """send_text falso: guarda o que foi enviado e pode falhar as primeiras vezes."""

    def __init__(self, fail_times=0):
        self.sent, self.fail_times, self.calls = [], fail_times, 0

    async def __call__(self, text):
        self.calls += 1
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("telegram fora")
        self.sent.append(text)


def fast_waits(scheduler, clock, stop_after):
    """Troca a espera real por avanço do relógio falso; pede para parar após N esperas."""
    waits = []

    async def fake_wait(stop, seconds):
        waits.append(seconds)
        clock.advance(seconds)
        if len(waits) >= stop_after:
            stop.set()
            return True
        return False

    scheduler._wait = fake_wait
    return waits


# ---- configuração e texto ----------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [("06:00", time(6, 0)), ("6:30", time(6, 30)), (" 23:59 ", time(23, 59))])
def test_parse_hhmm(value, expected):
    assert parse_hhmm(value) == expected


@pytest.mark.parametrize("value", ["", "off", "OFF", "desligado", None])
def test_parse_hhmm_off(value):
    assert parse_hhmm(value) is None


@pytest.mark.parametrize("value", ["25:00", "6h", "06-00", "abc", "12:61"])
def test_parse_hhmm_rejects_invalid(value):
    with pytest.raises(ValueError, match="horário inválido"):
        parse_hhmm(value)


def test_build_briefing_is_plain_text_without_ids_and_none_when_empty(tasks):
    assert build_briefing(tasks) is None
    tasks.add("Consulta médica", date(2026, 11, 18), time(8, 0), tags="saúde")
    tasks.add("Remédio", recurrence=Recurrence.parse("diária"), due_time=time(7, 0))
    tasks.add("Ligar", date(2026, 11, 15))
    text = build_briefing(tasks)
    assert text.startswith("Bom dia!\n\nAtrasadas:\n- Ligar (era 15/11 (dom))")
    assert "Hoje (quarta-feira, 18/11):\n- 07:00 Remédio (diária)\n- 08:00 Consulta médica #saúde" in text
    assert "[" not in text and "**" not in text


# ---- quando enviar -------------------------------------------------------------------------


def test_due_now_only_after_the_time_before_noon_and_once_a_day(scheduler, clock):
    assert not scheduler.due_now(at(18, 5, 59))  # ainda não deu a hora
    assert scheduler.due_now(at(18, 6, 0)) and scheduler.due_now(at(18, 11, 59))
    assert not scheduler.due_now(at(18, 12, 0))  # perdeu a manhã: não manda mais
    scheduler._mark_sent(date(2026, 11, 18))
    assert not scheduler.due_now(at(18, 7, 0))
    assert scheduler.due_now(at(19, 7, 0))  # no dia seguinte vale de novo


def test_seconds_until_next(scheduler):
    assert scheduler.seconds_until_next(at(18, 5, 0)) == 3600
    assert scheduler.seconds_until_next(at(18, 6, 0)) == 86400  # exatamente na hora: o próximo é amanhã
    assert scheduler.seconds_until_next(at(18, 20, 0)) == 10 * 3600


def test_last_sent_survives_restarts_and_tolerates_a_damaged_state_file(scheduler, tmp_path):
    assert scheduler.last_sent() is None
    scheduler._mark_sent(date(2026, 11, 18))
    other = BriefingScheduler(scheduler.tasks, time(6, 0), scheduler.state_path)
    assert other.last_sent() == date(2026, 11, 18)
    scheduler.state_path.write_text("{ não é json", encoding="utf-8")
    assert scheduler.last_sent() is None
    assert not list(tmp_path.glob("*.tmp"))


# ---- envio -----------------------------------------------------------------------------------


def test_send_delivers_marks_the_day_and_records_it_as_curated_assistant_message(tasks, clock, tmp_path):
    log = ConversationLog(tmp_path / "c.json")
    sched = BriefingScheduler(tasks, time(6, 0), tmp_path / "b.json", log=log, clock=clock)
    tasks.add("Consulta médica", date(2026, 11, 18), time(8, 0))
    clock.now = at(18, 6, 0)
    sink = Sink()
    assert run(sched.send(sink)) is True
    assert "08:00 Consulta médica" in sink.sent[0]
    assert sched.last_sent() == date(2026, 11, 18)
    msg = log.messages()[0]
    assert (msg.role, msg.curated) == ("assistente", True) and msg.text == sink.sent[0]
    assert log.pending() == []  # o Curator não vai processar o resumo
    assert [m.role for m in log.recent(5)] == ["assistente"]  # mas o agente enxerga o que foi dito


def test_send_with_nothing_to_say_sends_nothing_but_marks_the_day(scheduler, clock):
    clock.now = at(18, 6, 0)
    sink = Sink()
    assert run(scheduler.send(sink)) is True and sink.sent == []
    assert scheduler.last_sent() == date(2026, 11, 18)


def test_send_failure_is_reported_and_not_marked(tasks, scheduler, clock, caplog):
    tasks.add("Consulta", date(2026, 11, 18))
    clock.now = at(18, 6, 0)
    assert run(scheduler.send(Sink(fail_times=1))) is False
    assert scheduler.last_sent() is None
    assert "não consegui enviar o resumo" in caplog.text


# ---- laço ----------------------------------------------------------------------------------------


def test_run_waits_for_the_time_then_sends_every_day(tasks, scheduler, clock):
    tasks.add("Consulta", date(2026, 11, 18), time(8, 0))
    tasks.add("Dentista", date(2026, 11, 19), time(9, 0))
    waits = fast_waits(scheduler, clock, stop_after=3)  # 05:00 -> 06:00 (envia) -> dia 19 06:00 (envia) -> para
    sink = Sink()
    run(scheduler.run(asyncio.Event(), sink))
    assert waits == [3600, 86400, 86400]
    assert len(sink.sent) == 2 and "Consulta" in sink.sent[0] and "Dentista" in sink.sent[1]
    assert scheduler.last_sent() == date(2026, 11, 19)


def test_run_recovers_a_missed_briefing_on_startup_then_waits_for_tomorrow(tasks, scheduler, clock):
    tasks.add("Consulta", date(2026, 11, 18), time(8, 0))
    clock.now = at(18, 6, 40)  # o bot subiu 40 min depois da hora
    waits = fast_waits(scheduler, clock, stop_after=1)
    sink = Sink()
    run(scheduler.run(asyncio.Event(), sink))
    assert len(sink.sent) == 1 and waits == [pytest.approx(23 * 3600 + 20 * 60)]


@pytest.mark.parametrize("hour,already_sent", [(13, False), (7, True), (5, False)])
def test_run_does_not_recover_when_too_late_already_sent_or_too_early(tasks, scheduler, clock, hour, already_sent):
    tasks.add("Consulta", date(2026, 11, 18))
    clock.now = at(18, hour, 0)
    if already_sent:
        scheduler._mark_sent(date(2026, 11, 18))
    fast_waits(scheduler, clock, stop_after=1)
    sink = Sink()
    run(scheduler.run(asyncio.Event(), sink))
    assert sink.sent == []  # a 1ª espera já foi a última; nada enviado antes dela


def test_failures_are_retried_every_five_minutes_until_it_works(tasks, scheduler, clock):
    tasks.add("Consulta", date(2026, 11, 18))
    clock.now = at(18, 6, 0)
    waits = fast_waits(scheduler, clock, stop_after=3)  # 2 esperas de retry + a espera até o dia seguinte
    sink = Sink(fail_times=2)
    run(scheduler.run(asyncio.Event(), sink))  # recuperação ao iniciar: falha, falha, envia
    assert sink.calls == 3 and len(sink.sent) == 1
    assert waits == [br.RETRY_SECONDS, br.RETRY_SECONDS, 86400 - 2 * br.RETRY_SECONDS]  # até 06:00 de amanhã
    assert scheduler.last_sent() == date(2026, 11, 18)


def test_an_overdue_task_keeps_showing_up_in_every_morning_briefing_until_done(tasks, scheduler, clock):
    task = tasks.add("Ligar para o banco", date(2026, 11, 15))
    clock.now = at(18, 5, 0)
    fast_waits(scheduler, clock, stop_after=3)  # envia dias 18 e 19
    sink = Sink()
    run(scheduler.run(asyncio.Event(), sink))
    assert len(sink.sent) == 2 and all("Ligar para o banco" in t and "Atrasadas" in t for t in sink.sent)
    tasks.complete(task.id)
    assert build_briefing(tasks) is None


def test_gives_up_after_max_attempts_and_tries_again_the_next_day(tasks, scheduler, clock):
    tasks.add("Consulta", date(2026, 11, 18))
    tasks.add("Dentista", date(2026, 11, 19))
    clock.now = at(18, 6, 0)
    waits = fast_waits(scheduler, clock, stop_after=br.MAX_ATTEMPTS)  # 5 esperas de retry + 1 para o dia seguinte
    sink = Sink(fail_times=br.MAX_ATTEMPTS)  # as 6 tentativas do dia 18 falham
    run(scheduler.run(asyncio.Event(), sink))
    assert sink.calls == br.MAX_ATTEMPTS and sink.sent == []
    assert waits[: br.MAX_ATTEMPTS - 1] == [br.RETRY_SECONDS] * (br.MAX_ATTEMPTS - 1)
    assert scheduler.last_sent() is None  # nada marcado; o dia seguinte ainda tenta


def test_stop_during_a_retry_wait_ends_the_loop(tasks, scheduler, clock):
    tasks.add("Consulta", date(2026, 11, 18))
    clock.now = at(18, 6, 0)
    fast_waits(scheduler, clock, stop_after=1)  # a 1ª espera (retry) pede para parar
    sink = Sink(fail_times=99)
    run(scheduler.run(asyncio.Event(), sink))
    assert sink.calls == 1


def test_real_wait_returns_true_when_stopped_and_false_on_timeout(scheduler):
    async def scenario():
        stop = asyncio.Event()
        assert await scheduler._wait(stop, 0.01) is False
        stop.set()
        return await scheduler._wait(stop, 5)

    assert run(scenario()) is True


# ---- ligação no bot --------------------------------------------------------------------------------


class FakeBriefing:
    def __init__(self):
        self.started, self.finished = False, False

    async def run(self, stop, send_text):
        self.started = True
        await send_text("Bom dia! Hoje: consulta às 08:00.")
        await stop.wait()
        self.finished = True


class FakeService:
    async def watch(self, stop, interval=30, on_outcome=None):
        await stop.wait()


def make_app(briefing, allowed=ALLOWED):
    handlers = bot.BotHandlers(MagicMock(unfinished=lambda: []), allowed, briefing=briefing)
    app = bot.build_application("123456:fake-token", handlers, FakeService())
    gw = FakeGateway()
    app.bot_data["gateway"] = gw
    return app, gw


def lifecycle(app):
    async def go():
        await bot.post_init(app)
        await asyncio.sleep(0.05)
        await bot.post_shutdown(app)

    run(go())


def test_post_init_starts_the_briefing_loop_and_shutdown_stops_it():
    fake = FakeBriefing()
    app, gw = make_app(fake)
    lifecycle(app)
    assert fake.started and fake.finished
    assert gw.texts == [(ALLOWED, "Bom dia! Hoje: consulta às 08:00.")] or \
        (ALLOWED, "Bom dia! Hoje: consulta às 08:00.") in gw.texts
    assert app.bot_data["briefing"].done()


def test_briefing_is_not_started_without_scheduler_or_allowed_user():
    for briefing, allowed in ((None, ALLOWED), (FakeBriefing(), None)):
        app, gw = make_app(briefing, allowed)
        lifecycle(app)
        assert "briefing" not in app.bot_data
        assert not any("Bom dia" in t for t in gw.sent())


def test_main_wires_the_briefing_from_config(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:fake-token")
    seen = {}
    flags = ["--log", str(tmp_path / "c.json"), "--memory-dir", str(tmp_path / "mem")]
    for value, expected in (("06:00", time(6, 0)), ("7:15", time(7, 15)), ("off", None)):
        monkeypatch.setattr(config, "BRIEFING_TIME", value)
        bot.main(flags, agent=object(), application_factory=lambda t, h, s: seen.update(b=h.briefing) or MagicMock())
        assert (seen["b"].at if seen["b"] else None) == expected
    out = capsys.readouterr().out
    assert "Resumo da manhã: 06:00" in out and "Resumo da manhã: 07:15" in out and "Resumo da manhã: desligado" in out
    monkeypatch.setattr(config, "BRIEFING_TIME", "06:00")
    bot.main(flags, agent=object(), application_factory=lambda t, h, s: seen.update(b=h.briefing) or MagicMock())
    assert seen["b"].state_path == config.BRIEFING_STATE_FILE and seen["b"].log is not None


def test_main_rejects_an_invalid_briefing_time(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:fake-token")
    monkeypatch.setattr(config, "BRIEFING_TIME", "6 da manhã")
    assert bot.main(["--log", str(tmp_path / "c.json")], agent=object(),
                    application_factory=lambda *a: MagicMock()) == 1
    assert "BRIEFING_TIME" in capsys.readouterr().err
