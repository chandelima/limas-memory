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
    return Clock(at(18, 5, 0))  # Wednesday, 05:00


@pytest.fixture
def tasks(tmp_path, clock):
    return TaskStore(MemoryStore(tmp_path / "memory"), today=lambda: clock().date())


@pytest.fixture
def scheduler(tasks, clock, tmp_path):
    return BriefingScheduler(tasks, time(6, 0), tmp_path / "briefing.json", clock=clock)


class Sink:
    """Fake send_text: keeps what was sent and can fail the first few times."""

    def __init__(self, fail_times=0):
        self.sent, self.fail_times, self.calls = [], fail_times, 0

    async def __call__(self, text):
        self.calls += 1
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("telegram is down")
        self.sent.append(text)


def fast_waits(scheduler, clock, stop_after):
    """Replace the real wait with a fake clock advance; ask to stop after N waits."""
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


# ---- configuration and text --------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [("06:00", time(6, 0)), ("6:30", time(6, 30)), (" 23:59 ", time(23, 59))])
def test_parse_hhmm(value, expected):
    assert parse_hhmm(value) == expected


@pytest.mark.parametrize("value", ["", "off", "OFF", "disabled", None])
def test_parse_hhmm_off(value):
    assert parse_hhmm(value) is None


@pytest.mark.parametrize("value", ["25:00", "6h", "06-00", "abc", "12:61"])
def test_parse_hhmm_rejects_invalid(value):
    with pytest.raises(ValueError, match="invalid time"):
        parse_hhmm(value)


def test_build_briefing_is_plain_text_without_ids_and_none_when_empty(tasks):
    assert build_briefing(tasks) is None
    tasks.add("Doctor appointment", date(2026, 11, 18), time(8, 0), tags="health")
    tasks.add("Medication", recurrence=Recurrence.parse("daily"), due_time=time(7, 0))
    tasks.add("Call", date(2026, 11, 15))
    text = build_briefing(tasks)
    assert text.startswith("Good morning!\n\nOverdue:\n- Call (was 15/11 (Sun))")
    assert "Today (Wednesday, 18/11):\n- 07:00 Medication (daily)\n- 08:00 Doctor appointment #health" in text
    assert "[" not in text and "**" not in text


# ---- when to send ---------------------------------------------------------------------------


def test_due_now_only_after_the_time_before_noon_and_once_a_day(scheduler, clock):
    assert not scheduler.due_now(at(18, 5, 59))  # not time yet
    assert scheduler.due_now(at(18, 6, 0)) and scheduler.due_now(at(18, 11, 59))
    assert not scheduler.due_now(at(18, 12, 0))  # missed the morning: no longer sent
    scheduler._mark_sent(date(2026, 11, 18))
    assert not scheduler.due_now(at(18, 7, 0))
    assert scheduler.due_now(at(19, 7, 0))  # the next day it counts again


def test_seconds_until_next(scheduler):
    assert scheduler.seconds_until_next(at(18, 5, 0)) == 3600
    assert scheduler.seconds_until_next(at(18, 6, 0)) == 86400  # exactly on time: the next one is tomorrow
    assert scheduler.seconds_until_next(at(18, 20, 0)) == 10 * 3600


def test_last_sent_survives_restarts_and_tolerates_a_damaged_state_file(scheduler, tmp_path):
    assert scheduler.last_sent() is None
    scheduler._mark_sent(date(2026, 11, 18))
    other = BriefingScheduler(scheduler.tasks, time(6, 0), scheduler.state_path)
    assert other.last_sent() == date(2026, 11, 18)
    scheduler.state_path.write_text("{ not json", encoding="utf-8")
    assert scheduler.last_sent() is None
    assert not list(tmp_path.glob("*.tmp"))


# ---- sending --------------------------------------------------------------------------------


def test_send_delivers_marks_the_day_and_records_it_as_curated_assistant_message(tasks, clock, tmp_path):
    log = ConversationLog(tmp_path / "c.json")
    sched = BriefingScheduler(tasks, time(6, 0), tmp_path / "b.json", log=log, clock=clock)
    tasks.add("Doctor appointment", date(2026, 11, 18), time(8, 0))
    clock.now = at(18, 6, 0)
    sink = Sink()
    assert run(sched.send(sink)) is True
    assert "08:00 Doctor appointment" in sink.sent[0]
    assert sched.last_sent() == date(2026, 11, 18)
    msg = log.messages()[0]
    assert (msg.role, msg.curated) == ("assistant", True) and msg.text == sink.sent[0]
    assert log.pending() == []  # the Curator will not process the briefing
    assert [m.role for m in log.recent(5)] == ["assistant"]  # but the agent sees what was said


def test_send_with_nothing_to_say_sends_nothing_but_marks_the_day(scheduler, clock):
    clock.now = at(18, 6, 0)
    sink = Sink()
    assert run(scheduler.send(sink)) is True and sink.sent == []
    assert scheduler.last_sent() == date(2026, 11, 18)


def test_send_failure_is_reported_and_not_marked(tasks, scheduler, clock, caplog):
    tasks.add("Appointment", date(2026, 11, 18))
    clock.now = at(18, 6, 0)
    assert run(scheduler.send(Sink(fail_times=1))) is False
    assert scheduler.last_sent() is None
    assert "could not send the morning briefing" in caplog.text


# ---- loop -----------------------------------------------------------------------------------


def test_run_waits_for_the_time_then_sends_every_day(tasks, scheduler, clock):
    tasks.add("Appointment", date(2026, 11, 18), time(8, 0))
    tasks.add("Dentist", date(2026, 11, 19), time(9, 0))
    waits = fast_waits(scheduler, clock, stop_after=3)  # 05:00 -> 06:00 (sends) -> day 19 06:00 (sends) -> stops
    sink = Sink()
    run(scheduler.run(asyncio.Event(), sink))
    assert waits == [3600, 86400, 86400]
    assert len(sink.sent) == 2 and "Appointment" in sink.sent[0] and "Dentist" in sink.sent[1]
    assert scheduler.last_sent() == date(2026, 11, 19)


def test_run_recovers_a_missed_briefing_on_startup_then_waits_for_tomorrow(tasks, scheduler, clock):
    tasks.add("Appointment", date(2026, 11, 18), time(8, 0))
    clock.now = at(18, 6, 40)  # the bot came up 40 min after the time
    waits = fast_waits(scheduler, clock, stop_after=1)
    sink = Sink()
    run(scheduler.run(asyncio.Event(), sink))
    assert len(sink.sent) == 1 and waits == [pytest.approx(23 * 3600 + 20 * 60)]


@pytest.mark.parametrize("hour,already_sent", [(13, False), (7, True), (5, False)])
def test_run_does_not_recover_when_too_late_already_sent_or_too_early(tasks, scheduler, clock, hour, already_sent):
    tasks.add("Appointment", date(2026, 11, 18))
    clock.now = at(18, hour, 0)
    if already_sent:
        scheduler._mark_sent(date(2026, 11, 18))
    fast_waits(scheduler, clock, stop_after=1)
    sink = Sink()
    run(scheduler.run(asyncio.Event(), sink))
    assert sink.sent == []  # the 1st wait was already the last; nothing sent before it


def test_failures_are_retried_every_five_minutes_until_it_works(tasks, scheduler, clock):
    tasks.add("Appointment", date(2026, 11, 18))
    clock.now = at(18, 6, 0)
    waits = fast_waits(scheduler, clock, stop_after=3)  # 2 retry waits + the wait until the next day
    sink = Sink(fail_times=2)
    run(scheduler.run(asyncio.Event(), sink))  # recovery on start: fails, fails, sends
    assert sink.calls == 3 and len(sink.sent) == 1
    assert waits == [br.RETRY_SECONDS, br.RETRY_SECONDS, 86400 - 2 * br.RETRY_SECONDS]  # until 06:00 tomorrow
    assert scheduler.last_sent() == date(2026, 11, 18)


def test_an_overdue_task_keeps_showing_up_in_every_morning_briefing_until_done(tasks, scheduler, clock):
    task = tasks.add("Call the bank", date(2026, 11, 15))
    clock.now = at(18, 5, 0)
    fast_waits(scheduler, clock, stop_after=3)  # sends on days 18 and 19
    sink = Sink()
    run(scheduler.run(asyncio.Event(), sink))
    assert len(sink.sent) == 2 and all("Call the bank" in t and "Overdue" in t for t in sink.sent)
    tasks.complete(task.id)
    assert build_briefing(tasks) is None


def test_gives_up_after_max_attempts_and_tries_again_the_next_day(tasks, scheduler, clock):
    tasks.add("Appointment", date(2026, 11, 18))
    tasks.add("Dentist", date(2026, 11, 19))
    clock.now = at(18, 6, 0)
    waits = fast_waits(scheduler, clock, stop_after=br.MAX_ATTEMPTS)  # 5 retry waits + 1 for the next day
    sink = Sink(fail_times=br.MAX_ATTEMPTS)  # all 6 attempts on day 18 fail
    run(scheduler.run(asyncio.Event(), sink))
    assert sink.calls == br.MAX_ATTEMPTS and sink.sent == []
    assert waits[: br.MAX_ATTEMPTS - 1] == [br.RETRY_SECONDS] * (br.MAX_ATTEMPTS - 1)
    assert scheduler.last_sent() is None  # nothing marked; the next day still tries


def test_stop_during_a_retry_wait_ends_the_loop(tasks, scheduler, clock):
    tasks.add("Appointment", date(2026, 11, 18))
    clock.now = at(18, 6, 0)
    fast_waits(scheduler, clock, stop_after=1)  # the 1st wait (retry) asks to stop
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


# ---- wiring in the bot -------------------------------------------------------------------------


class FakeBriefing:
    def __init__(self):
        self.started, self.finished = False, False

    async def run(self, stop, send_text):
        self.started = True
        await send_text("Good morning! Today: appointment at 08:00.")
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
    assert (ALLOWED, "Good morning! Today: appointment at 08:00.") in gw.texts
    assert app.bot_data["briefing"].done()


def test_briefing_is_not_started_without_scheduler_or_allowed_user():
    for briefing, allowed in ((None, ALLOWED), (FakeBriefing(), None)):
        app, gw = make_app(briefing, allowed)
        lifecycle(app)
        assert "briefing" not in app.bot_data
        assert not any("Good morning" in t for t in gw.sent())


def test_main_wires_the_briefing_from_config(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:fake-token")
    seen = {}
    flags = ["--log", str(tmp_path / "c.json"), "--memory-dir", str(tmp_path / "mem")]
    for value, expected in (("06:00", time(6, 0)), ("7:15", time(7, 15)), ("off", None)):
        monkeypatch.setattr(config, "BRIEFING_TIME", value)
        bot.main(flags, agent=object(), application_factory=lambda t, h, s: seen.update(b=h.briefing) or MagicMock())
        assert (seen["b"].at if seen["b"] else None) == expected
    out = capsys.readouterr().out
    assert "Morning briefing: 06:00" in out and "Morning briefing: 07:15" in out and "Morning briefing: disabled" in out
    monkeypatch.setattr(config, "BRIEFING_TIME", "06:00")
    bot.main(flags, agent=object(), application_factory=lambda t, h, s: seen.update(b=h.briefing) or MagicMock())
    assert seen["b"].state_path == config.BRIEFING_STATE_FILE and seen["b"].log is not None


def test_main_rejects_an_invalid_briefing_time(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:fake-token")
    monkeypatch.setattr(config, "BRIEFING_TIME", "6 in the morning")
    assert bot.main(["--log", str(tmp_path / "c.json")], agent=object(),
                    application_factory=lambda *a: MagicMock()) == 1
    assert "BRIEFING_TIME" in capsys.readouterr().err
