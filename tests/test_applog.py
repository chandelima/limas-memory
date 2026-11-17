import asyncio
import logging

import pytest

import applog
import config
from applog import (
    ChannelHandler, LogShipper, RedactingFormatter, collapse, configure_logging, get_logger, pack,
    parse_channel_id, parse_level, redact,
)

BOT_TOKEN = "123456789:AAH-abcdefghijklmnopqrstuvwxyz012345"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def clean_logging():
    """Each test starts without app handlers and leaves everything as it found it."""
    root, app = logging.getLogger(), logging.getLogger("limas")
    before_root, before_app, level = list(root.handlers), list(app.handlers), root.level
    yield
    root.handlers[:] = before_root
    app.handlers[:] = before_app
    root.setLevel(level)


@pytest.fixture
def secrets(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "or-my-secret-key-123")
    monkeypatch.setattr(config, "MEMORY_GIT_TOKEN", "my-git-token-1234")


def record(msg="event", level=logging.WARNING, name="limas.bot", exc=None, channel=False):
    rec = logging.LogRecord(name, level, __file__, 1, msg, (), (type(exc), exc, None) if exc else None)
    if channel:
        rec.channel = True
    return rec


# ---- masking secrets ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        f"error at https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
        "using sk-abcdefghijklmnopqrstuvwxyz123456 in the header",
        "push with ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "Authorization: Basic eC1hY2Nlc3MtdG9rZW46c2VncmVkbw==",
        "Authorization: Bearer abcdef1234567890",
        "api_key = abcdef1234567890XYZ",
    ],
)
def test_redact_masks_common_secret_shapes(text):
    out = redact(text, secrets=[])
    assert "***" in out and BOT_TOKEN not in out and "abcdefghijklmnop" not in out and "eC1hY2Nl" not in out


def test_redact_masks_the_configured_secrets_wherever_they_appear(secrets):
    text = "failure: or-my-secret-key-123 and my-git-token-1234 in " + BOT_TOKEN
    out = redact(text)
    assert out == "failure: *** and *** in ***"
    assert redact("normal text without a secret") == "normal text without a secret"
    assert applog.known_secrets() == [BOT_TOKEN, "or-my-secret-key-123", "my-git-token-1234"]


def test_formatter_masks_secrets_inside_exception_tracebacks(secrets):
    try:
        raise RuntimeError(f"Invalid URL bot{BOT_TOKEN}")
    except RuntimeError:
        import sys

        rec = logging.LogRecord("limas.x", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    text = RedactingFormatter("%(levelname)s %(message)s").format(rec)
    assert "failed" in text and "Traceback" in text and BOT_TOKEN not in text and "***" in text


# ---- configuration -------------------------------------------------------------------------------


def test_parse_level_names_numbers_and_fallback():
    assert parse_level("debug", logging.INFO) == logging.DEBUG and parse_level(" Warning ", 0) == logging.WARNING
    assert parse_level(40, 0) == 40 and parse_level("none", logging.INFO) == logging.INFO


@pytest.mark.parametrize("value,expected", [("", None), ("  ", None), ("-1001234567890", -1001234567890),
                                            ("123", 123), ("@mychannel", "@mychannel")])
def test_parse_channel_id(value, expected):
    assert parse_channel_id(value) == expected


@pytest.mark.parametrize("bad", ["mychannel", "@", "-100abc", "https://t.me/x"])
def test_parse_channel_id_rejects_bad_values(bad):
    with pytest.raises(ValueError, match="invalid LOG_CHANNEL_ID"):
        parse_channel_id(bad)


def test_configure_logging_is_idempotent_and_keeps_libraries_quiet(capsys):
    configure_logging(console_level="INFO")
    configure_logging(console_level="DEBUG")
    marked = [h for h in logging.getLogger().handlers if getattr(h, "_limas_console", False)]
    assert len(marked) == 1  # calling again replaces the handler instead of duplicating it
    for name in ("httpx", "httpcore", "telegram"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING  # the httpx URL carries the token
    get_logger("test").info("hello from the app")
    get_logger("test").debug("detail")
    logging.getLogger("httpx").info("POST https://api.telegram.org/botX/getUpdates")
    err = capsys.readouterr().err
    assert "INFO    limas.test: hello from the app" in err and "detail" in err.lower()
    assert "getUpdates" not in err


def test_console_masks_secrets(capsys, secrets):
    configure_logging()
    get_logger("bot").error("failure with %s", BOT_TOKEN)
    err = capsys.readouterr().err
    assert BOT_TOKEN not in err and "***" in err


def test_channel_handler_is_only_created_when_the_channel_is_enabled():
    assert configure_logging(channel=False) is None
    handler = configure_logging(channel=True, channel_level="ERROR")
    assert isinstance(handler, ChannelHandler) and handler.min_level == logging.ERROR
    assert sum(isinstance(h, ChannelHandler) for h in logging.getLogger("limas").handlers) == 1
    assert configure_logging(channel=True) is not handler  # reconfiguring replaces it
    assert sum(isinstance(h, ChannelHandler) for h in logging.getLogger("limas").handlers) == 1


# ---- channel queue --------------------------------------------------------------------------------


def test_handler_queues_warnings_but_not_info_unless_marked_for_the_channel():
    handler = ChannelHandler(level=logging.WARNING)
    for rec in (record("plain info", logging.INFO), record("warning", logging.WARNING), record("error", logging.ERROR),
                record("bot started", logging.INFO, channel=True)):
        handler.emit(rec)
    lines = []
    while not handler.queue.empty():
        lines.append(handler.queue.get_nowait())
    assert [ln.split(": ", 1)[1] for ln in lines] == ["warning", "error", "bot started"]
    assert lines[0].split()[1] == "#WARNING" and " bot: " in lines[0]  # time, level and origin without the limas. prefix


def test_channel_line_has_error_type_and_message_but_no_traceback_and_a_single_line(secrets):
    err = ValueError(f"went wrong\nwith {BOT_TOKEN}")
    line = ChannelHandler.channel_line(record("could not answer", logging.ERROR, exc=err))
    assert "could not answer [ValueError: went wrong with ***]" in line
    assert "\n" not in line and "Traceback" not in line and BOT_TOKEN not in line


def test_handler_ignores_the_shippers_own_logs_to_avoid_loops():
    handler = ChannelHandler()
    handler.emit(record("failed to send", logging.WARNING, name=applog.SHIP_NAME))
    handler.emit(record("another", logging.ERROR, name=applog.SHIP_NAME + ".x"))
    assert handler.queue.empty()


def test_full_queue_drops_the_oldest_and_counts():
    handler = ChannelHandler(maxsize=3)
    for i in range(5):
        handler.emit(record(f"event {i}"))
    lines = [handler.queue.get_nowait().split(": ", 1)[1] for _ in range(3)]
    assert lines == ["event 2", "event 3", "event 4"] and handler.dropped == 2


# ---- packing and joining lines ------------------------------------------------------------------------


def test_pack_respects_the_limit_and_cuts_huge_lines():
    assert pack(["a", "b", "c"], limit=100) == ["a\nb\nc"]
    msgs = pack(["x" * 40, "y" * 40, "z" * 40], limit=100)
    assert msgs == ["x" * 40 + "\n" + "y" * 40, "z" * 40]
    huge = pack(["w" * 500], limit=100)
    assert len(huge) == 1 and len(huge[0]) == 100 and huge[0].endswith("…")


def test_collapse_merges_consecutive_equal_lines_ignoring_the_time():
    lines = ["10:00:01 WARNING bot: unauthorized user id=9", "10:00:02 WARNING bot: unauthorized user id=9",
             "10:00:03 WARNING bot: unauthorized user id=9", "10:00:04 ERROR bot: something else",
             "10:00:05 WARNING bot: unauthorized user id=9"]
    assert collapse(lines) == ["10:00:01 WARNING bot: unauthorized user id=9 (x3)",
                               "10:00:04 ERROR bot: something else", "10:00:05 WARNING bot: unauthorized user id=9"]


# ---- batch sending -----------------------------------------------------------------------------------------


class Sink:
    def __init__(self, fail_times=0):
        self.sent, self.fail_times = [], fail_times

    async def __call__(self, text):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("telegram is down")
        self.sent.append(text)


def shipper(handler, sink, **kw):
    kw.setdefault("flush_seconds", 0.01)
    kw.setdefault("min_interval", 0)
    return LogShipper(handler, sink, **kw)


def test_flush_sends_one_batched_message_without_header_by_default_and_collapses_repeats():
    handler, sink = ChannelHandler(), Sink()
    for _ in range(3):
        handler.emit(record("same failure"))
    handler.emit(record("another failure", logging.ERROR))
    assert run(shipper(handler, sink).flush()) == 1
    assert len(sink.sent) == 1 and sink.sent[0].split()[0][0].isdigit()  # no header: starts with the time
    assert "same failure (x3)" in sink.sent[0] and "#ERROR bot: another failure" in sink.sent[0]
    assert run(shipper(handler, sink).flush()) == 0  # empty queue: nothing to send


def test_flush_reports_dropped_lines_and_splits_long_batches():
    handler, sink = ChannelHandler(maxsize=2), Sink()
    for i in range(5):
        handler.emit(record(f"event {i}"))
    assert run(shipper(handler, sink).flush()) == 1
    assert "(3 log lines dropped due to overflow)" in sink.sent[0] and handler.dropped == 0
    big, sink2 = ChannelHandler(maxsize=1000), Sink()
    for i in range(300):
        big.emit(record(f"event number {i} " + "x" * 30))
    assert run(shipper(big, sink2).flush()) >= 2 and all(len(m) <= applog.CHANNEL_LIMIT for m in sink2.sent)


def test_a_failed_send_keeps_the_lines_and_retries_on_the_next_flush(caplog):
    handler, sink = ChannelHandler(), Sink(fail_times=1)
    handler.emit(record("important"))
    ship = shipper(handler, sink)
    with caplog.at_level(logging.WARNING, logger=applog.SHIP_NAME):
        assert run(ship.flush()) == 0
    assert sink.sent == [] and "could not send the log to the channel" in caplog.text
    handler.emit(record("later"))
    ship._retry_at = 0  # (skip the back-off wait)
    assert run(ship.flush()) == 1  # the one that failed goes along with the new one
    assert "important" in sink.sent[0] and "later" in sink.sent[0]


def test_failure_logs_do_not_re_enter_the_queue():
    handler, sink = ChannelHandler(), Sink(fail_times=5)
    logging.getLogger("limas").addHandler(handler)
    try:
        handler.emit(record("important"))
        ship = shipper(handler, sink)
        run(ship.flush())  # fails and logs to limas.logship, which the handler ignores
        assert handler.queue.empty()
    finally:
        logging.getLogger("limas").removeHandler(handler)


def test_pending_lines_kept_after_failures_are_bounded():
    handler, sink = ChannelHandler(maxsize=1000), Sink(fail_times=99)
    ship = shipper(handler, sink, keep_on_failure=5)
    for i in range(50):
        handler.emit(record(f"event {i}"))
    run(ship.flush())
    assert len(ship._pending) == 5 and ship._pending[-1].endswith("event 49")


def test_rate_limit_waits_between_messages(monkeypatch):
    handler, sink = ChannelHandler(maxsize=1000), Sink()
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(applog.asyncio, "sleep", fake_sleep)
    for i in range(300):
        handler.emit(record(f"event number {i} " + "x" * 30))
    ship = LogShipper(handler, sink, min_interval=3)
    assert run(ship.flush()) >= 2
    assert waits and all(0 < w <= 3 for w in waits)  # it respected the interval between messages


def test_run_flushes_periodically_and_once_more_on_stop():
    handler, sink = ChannelHandler(), Sink()

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(shipper(handler, sink).run(stop))
        handler.emit(record("first"))
        await asyncio.sleep(0.05)  # the flush cycle sends
        handler.emit(record("at farewell", logging.ERROR))
        stop.set()
        await task

    run(scenario())
    assert len(sink.sent) == 2 and "first" in sink.sent[0] and "at farewell" in sink.sent[1]


def test_optional_header_identifies_the_app_in_a_shared_channel():
    handler, sink = ChannelHandler(), Sink()
    handler.emit(record("failure"))
    run(shipper(handler, sink, header="[my-app]").flush())
    assert sink.sent[0].startswith("[my-app]" + chr(10))


def test_repeated_failures_warn_once_and_back_off_instead_of_retrying_every_flush(caplog):
    handler, sink = ChannelHandler(), Sink(fail_times=99)
    ship = shipper(handler, sink, flush_seconds=60)  # back-off: 120 s after the first failure
    handler.emit(record("important"))
    with caplog.at_level(logging.INFO, logger=applog.SHIP_NAME):
        run(ship.flush())
        calls_after_first = sink.fail_times
        run(ship.flush())  # inside the back-off window: it does not even try
        run(ship.flush())
    assert sink.fail_times == calls_after_first
    assert caplog.text.count("could not send the log to the channel") == 1
    assert "retrying with back-off" in caplog.text


def test_recovery_after_failures_is_logged_and_resets_the_back_off(caplog):
    handler, sink = ChannelHandler(), Sink(fail_times=2)
    ship = shipper(handler, sink, flush_seconds=0.001)
    handler.emit(record("important"))
    with caplog.at_level(logging.INFO, logger=applog.SHIP_NAME):
        run(ship.flush())
        ship._retry_at = 0
        run(ship.flush())
        ship._retry_at = 0
        assert run(ship.flush()) == 1  # the third attempt works
    assert "reachable again (after 2 failed attempt(s))" in caplog.text
    assert ship._failures == 0 and "important" in sink.sent[0]


def test_shutdown_flush_ignores_the_back_off():
    handler, sink = ChannelHandler(), Sink(fail_times=1)
    ship = shipper(handler, sink, flush_seconds=60)
    handler.emit(record("first"))
    run(ship.flush())  # fails: now backing off for 2 minutes
    handler.emit(record("farewell", logging.ERROR))
    assert run(ship.flush()) == 0 and sink.sent == []
    assert run(ship.flush(force=True)) == 1 and "farewell" in sink.sent[0]
