import asyncio
from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

import bot
import config
from briefing import BriefingScheduler, parse_briefing_time, parse_timezone
from storage.store import MemoryStore
from tasks.store import TaskStore

UTC = timezone.utc
MINUS3 = timezone(timedelta(hours=-3))


def run(coro):
    return asyncio.run(coro)


# ---- fuso -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,offset",
    [("UTC", 0), ("utc", 0), ("Z", 0), ("-03:00", -3), ("-03", -3), ("-3", -3), ("-0300", -3), ("UTC-3", -3),
     ("GMT+5:30", 5.5), ("+0530", 5.5), ("+14", 14)],
)
def test_parse_timezone_offsets(text, offset):
    assert parse_timezone(text).utcoffset(None) == timedelta(hours=offset)


def test_parse_timezone_iana_name_keeps_daylight_saving():
    ny = parse_timezone("America/New_York")
    assert datetime(2026, 1, 15, 12, tzinfo=ny).utcoffset() == timedelta(hours=-5)
    assert datetime(2026, 7, 15, 12, tzinfo=ny).utcoffset() == timedelta(hours=-4)
    assert parse_timezone("America/Recife").key == "America/Recife"


@pytest.mark.parametrize("bad", ["Marte/Fobos", "-25", "+15", "-03:99", "abc", "America/"])
def test_parse_timezone_rejects_unknown_or_impossible_values(bad):
    with pytest.raises(ValueError, match="fuso desconhecido|deslocamento"):
        parse_timezone(bad)


# ---- BRIEFING_TIME -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,at,offset",
    [
        ("06:00", time(6, 0), None),
        ("06:00-03", time(6, 0), -3),  # o formato pedido: horário colado ao deslocamento (ISO 8601)
        ("6:30-03:00", time(6, 30), -3),
        ("06:00-0300", time(6, 0), -3),
        ("06:00+05:30", time(6, 0), 5.5),
        ("06:00Z", time(6, 0), 0),
        ("06:00 UTC", time(6, 0), 0),
        ("06:00 -03:00", time(6, 0), -3),
        ("06:00 UTC-3", time(6, 0), -3),
        ("  07:15   -03  ", time(7, 15), -3),
    ],
)
def test_parse_briefing_time_accepts_the_compact_iso_form_and_the_spaced_forms(value, at, offset):
    bt = parse_briefing_time(value)
    assert bt.at == at
    if offset is None:
        assert bt.tz is None
    else:
        assert bt.tz.utcoffset(None) == timedelta(hours=offset)


def test_parse_briefing_time_with_a_named_zone_and_off_values():
    bt = parse_briefing_time("06:00 America/Recife")
    assert bt.at == time(6, 0) and bt.tz.key == "America/Recife"
    for off in ("", "off", "OFF", "desligado", None):
        assert parse_briefing_time(off) is None


@pytest.mark.parametrize("bad", ["06:00 Marte/Fobos", "06:00-25", "06:00 -03:99", "25:00-03", "06-03", "6 da manhã", "06:00 -"])
def test_parse_briefing_time_rejects_invalid_values(bad):
    with pytest.raises(ValueError):
        parse_briefing_time(bad)


def test_describe_shows_the_zone():
    assert parse_briefing_time("06:00-03").describe() == "06:00 (UTC-03:00)"
    assert parse_briefing_time("06:00 America/Recife").describe() == "06:00 (America/Recife)"
    assert parse_briefing_time("06:00").describe() == "06:00 (fuso da máquina)"
    assert parse_briefing_time("06:00Z").describe() == "06:00 (UTC)"


# ---- agendador no fuso configurado --------------------------------------------------------


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def tasks(tmp_path):
    return TaskStore(MemoryStore(tmp_path / "memory"))


def scheduler(tasks, tmp_path, clock, tz):
    return BriefingScheduler(tasks, time(6, 0), tmp_path / "b.json", clock=clock, tz=tz)


def test_the_time_is_evaluated_in_the_configured_zone_not_the_clock_zone(tasks, tmp_path):
    clock = Clock(datetime(2026, 11, 18, 8, 59, tzinfo=UTC))  # 05:59 em UTC-3
    sched = scheduler(tasks, tmp_path, clock, MINUS3)
    assert sched._now().hour == 5 and not sched.due_now(sched._now())
    assert sched.seconds_until_next(sched._now()) == 60
    clock.now = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)  # 06:00 em UTC-3
    assert sched.due_now(sched._now())
    assert sched.seconds_until_next(sched._now()) == 86400


def test_without_a_zone_the_machine_clock_is_used_unchanged(tasks, tmp_path):
    now = datetime(2026, 11, 18, 5, 0, tzinfo=timezone(timedelta(hours=-2)))
    sched = scheduler(tasks, tmp_path, Clock(now), None)
    assert sched._now() == now and sched.seconds_until_next(sched._now()) == 3600


def test_the_briefing_day_follows_the_configured_zone_around_midnight_utc(tasks, tmp_path):
    tasks.add("Consulta", date(2026, 11, 18), time(8, 0))
    clock = Clock(datetime(2026, 11, 19, 1, 30, tzinfo=UTC))  # ainda é 18/11 22:30 em UTC-3
    sched = scheduler(tasks, tmp_path, clock, MINUS3)
    sent = []

    async def sink(text):
        sent.append(text)

    assert run(sched.send(sink)) is True
    assert "Hoje (quarta-feira, 18/11)" in sent[0]  # e não quinta, 19/11
    assert sched.last_sent() == date(2026, 11, 18)


def test_next_run_accounts_for_daylight_saving_changes(tasks, tmp_path):
    ny = ZoneInfo("America/New_York")
    clock = Clock(datetime(2026, 3, 7, 7, 0, tzinfo=ny))  # véspera da mudança para o horário de verão
    sched = scheduler(tasks, tmp_path, clock, ny)
    # de 07:00 (EST) até 06:00 do dia seguinte (EDT) passam 22 horas, não 23
    assert sched.seconds_until_next(sched._now()) == 22 * 3600


# ---- ligação no bot --------------------------------------------------------------------------


def start(monkeypatch, tmp_path, value):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:fake-token")
    monkeypatch.setattr(config, "BRIEFING_TIME", value)
    seen = {}
    code = bot.main(["--log", str(tmp_path / "c.json"), "--memory-dir", str(tmp_path / "mem")], agent=object(),
                    application_factory=lambda t, h, s: seen.update(b=h.briefing) or MagicMock())
    return code, seen.get("b")


def test_main_passes_the_zone_to_the_scheduler_and_prints_it(monkeypatch, tmp_path, capsys):
    code, briefing = start(monkeypatch, tmp_path, "06:00-03")
    assert code == 0 and briefing.at == time(6, 0) and briefing.tz.utcoffset(None) == timedelta(hours=-3)
    assert "Resumo da manhã: 06:00 (UTC-03:00)" in capsys.readouterr().out
    code, briefing = start(monkeypatch, tmp_path, "06:00 America/Recife")
    assert briefing.tz.key == "America/Recife"
    assert "Resumo da manhã: 06:00 (America/Recife)" in capsys.readouterr().out
    code, briefing = start(monkeypatch, tmp_path, "06:00")
    assert briefing.tz is None


@pytest.mark.parametrize("bad", ["06:00 Marte/Fobos", "06:00-25", "6 da manhã"])
def test_main_rejects_an_invalid_briefing_time_or_zone(monkeypatch, tmp_path, capsys, bad):
    code, briefing = start(monkeypatch, tmp_path, bad)
    assert code == 1 and briefing is None
    assert "BRIEFING_TIME" in capsys.readouterr().err
