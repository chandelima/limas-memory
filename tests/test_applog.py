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
    """Cada teste começa sem handlers do app e deixa tudo como encontrou."""
    root, app = logging.getLogger(), logging.getLogger("limas")
    before_root, before_app, level = list(root.handlers), list(app.handlers), root.level
    yield
    root.handlers[:] = before_root
    app.handlers[:] = before_app
    root.setLevel(level)


@pytest.fixture
def secrets(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "or-minha-chave-secreta-123")
    monkeypatch.setattr(config, "MEMORY_GIT_TOKEN", "meu-token-do-git-1234")


def record(msg="evento", level=logging.WARNING, name="limas.bot", exc=None, channel=False):
    rec = logging.LogRecord(name, level, __file__, 1, msg, (), (type(exc), exc, None) if exc else None)
    if channel:
        rec.channel = True
    return rec


# ---- mascaramento de segredos ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        f"erro em https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
        "usando sk-abcdefghijklmnopqrstuvwxyz123456 no header",
        "push com ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "Authorization: Basic eC1hY2Nlc3MtdG9rZW46c2VncmVkbw==",
        "Authorization: Bearer abcdef1234567890",
        "api_key = abcdef1234567890XYZ",
    ],
)
def test_redact_masks_common_secret_shapes(text):
    out = redact(text, secrets=[])
    assert "***" in out and BOT_TOKEN not in out and "abcdefghijklmnop" not in out and "eC1hY2Nl" not in out


def test_redact_masks_the_configured_secrets_wherever_they_appear(secrets):
    text = "falha: or-minha-chave-secreta-123 e meu-token-do-git-1234 em " + BOT_TOKEN
    out = redact(text)
    assert out == "falha: *** e *** em ***"
    assert redact("texto normal sem segredo") == "texto normal sem segredo"
    assert applog.known_secrets() == [BOT_TOKEN, "or-minha-chave-secreta-123", "meu-token-do-git-1234"]


def test_formatter_masks_secrets_inside_exception_tracebacks(secrets):
    try:
        raise RuntimeError(f"URL inválida bot{BOT_TOKEN}")
    except RuntimeError:
        import sys

        rec = logging.LogRecord("limas.x", logging.ERROR, __file__, 1, "falhou", (), sys.exc_info())
    text = RedactingFormatter("%(levelname)s %(message)s").format(rec)
    assert "falhou" in text and "Traceback" in text and BOT_TOKEN not in text and "***" in text


# ---- configuração ----------------------------------------------------------------------------


def test_parse_level_names_numbers_and_fallback():
    assert parse_level("debug", logging.INFO) == logging.DEBUG and parse_level(" Warning ", 0) == logging.WARNING
    assert parse_level(40, 0) == 40 and parse_level("nenhum", logging.INFO) == logging.INFO


@pytest.mark.parametrize("value,expected", [("", None), ("  ", None), ("-1001234567890", -1001234567890),
                                            ("123", 123), ("@meucanal", "@meucanal")])
def test_parse_channel_id(value, expected):
    assert parse_channel_id(value) == expected


@pytest.mark.parametrize("bad", ["meucanal", "@", "-100abc", "https://t.me/x"])
def test_parse_channel_id_rejects_bad_values(bad):
    with pytest.raises(ValueError, match="LOG_CHANNEL_ID inválido"):
        parse_channel_id(bad)


def test_configure_logging_is_idempotent_and_keeps_libraries_quiet(capsys):
    configure_logging(console_level="INFO")
    configure_logging(console_level="DEBUG")
    marked = [h for h in logging.getLogger().handlers if getattr(h, "_limas_console", False)]
    assert len(marked) == 1  # chamar de novo troca o handler em vez de duplicar
    for name in ("httpx", "httpcore", "telegram"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING  # a URL do httpx tem o token
    get_logger("teste").info("olá do app")
    get_logger("teste").debug("detalhe")
    logging.getLogger("httpx").info("POST https://api.telegram.org/botX/getUpdates")
    err = capsys.readouterr().err
    assert "INFO    limas.teste: olá do app" in err and "DETALHE".lower() in err.lower()
    assert "getUpdates" not in err


def test_console_masks_secrets(capsys, secrets):
    configure_logging()
    get_logger("bot").error("falha com %s", BOT_TOKEN)
    err = capsys.readouterr().err
    assert BOT_TOKEN not in err and "***" in err


def test_channel_handler_is_only_created_when_the_channel_is_enabled():
    assert configure_logging(channel=False) is None
    handler = configure_logging(channel=True, channel_level="ERROR")
    assert isinstance(handler, ChannelHandler) and handler.min_level == logging.ERROR
    assert sum(isinstance(h, ChannelHandler) for h in logging.getLogger("limas").handlers) == 1
    assert configure_logging(channel=True) is not handler  # reconfigurar substitui
    assert sum(isinstance(h, ChannelHandler) for h in logging.getLogger("limas").handlers) == 1


# ---- fila do canal -----------------------------------------------------------------------------


def test_handler_queues_warnings_but_not_info_unless_marked_for_the_channel():
    handler = ChannelHandler(level=logging.WARNING)
    for rec in (record("info comum", logging.INFO), record("aviso", logging.WARNING), record("erro", logging.ERROR),
                record("bot iniciado", logging.INFO, channel=True)):
        handler.emit(rec)
    lines = []
    while not handler.queue.empty():
        lines.append(handler.queue.get_nowait())
    assert [ln.split(": ", 1)[1] for ln in lines] == ["aviso", "erro", "bot iniciado"]
    assert lines[0].split()[1] == "#WARNING" and " bot: " in lines[0]  # hora, nível e origem sem o prefixo limas.


def test_channel_line_has_error_type_and_message_but_no_traceback_and_a_single_line(secrets):
    err = ValueError(f"deu ruim\ncom {BOT_TOKEN}")
    line = ChannelHandler.channel_line(record("não consegui responder", logging.ERROR, exc=err))
    assert "não consegui responder [ValueError: deu ruim com ***]" in line
    assert "\n" not in line and "Traceback" not in line and BOT_TOKEN not in line


def test_handler_ignores_the_shippers_own_logs_to_avoid_loops():
    handler = ChannelHandler()
    handler.emit(record("falha ao enviar", logging.WARNING, name=applog.SHIP_NAME))
    handler.emit(record("outra", logging.ERROR, name=applog.SHIP_NAME + ".x"))
    assert handler.queue.empty()


def test_full_queue_drops_the_oldest_and_counts():
    handler = ChannelHandler(maxsize=3)
    for i in range(5):
        handler.emit(record(f"evento {i}"))
    lines = [handler.queue.get_nowait().split(": ", 1)[1] for _ in range(3)]
    assert lines == ["evento 2", "evento 3", "evento 4"] and handler.dropped == 2


# ---- empacotar e juntar linhas ---------------------------------------------------------------------


def test_pack_respects_the_limit_and_cuts_huge_lines():
    assert pack(["a", "b", "c"], limit=100) == ["a\nb\nc"]
    msgs = pack(["x" * 40, "y" * 40, "z" * 40], limit=100)
    assert msgs == ["x" * 40 + "\n" + "y" * 40, "z" * 40]
    huge = pack(["w" * 500], limit=100)
    assert len(huge) == 1 and len(huge[0]) == 100 and huge[0].endswith("…")


def test_collapse_merges_consecutive_equal_lines_ignoring_the_time():
    lines = ["10:00:01 WARNING bot: usuário não autorizado id=9", "10:00:02 WARNING bot: usuário não autorizado id=9",
             "10:00:03 WARNING bot: usuário não autorizado id=9", "10:00:04 ERROR bot: outra coisa",
             "10:00:05 WARNING bot: usuário não autorizado id=9"]
    assert collapse(lines) == ["10:00:01 WARNING bot: usuário não autorizado id=9 (x3)",
                               "10:00:04 ERROR bot: outra coisa", "10:00:05 WARNING bot: usuário não autorizado id=9"]


# ---- envio em lote ----------------------------------------------------------------------------------


class Sink:
    def __init__(self, fail_times=0):
        self.sent, self.fail_times = [], fail_times

    async def __call__(self, text):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("telegram fora")
        self.sent.append(text)


def shipper(handler, sink, **kw):
    kw.setdefault("flush_seconds", 0.01)
    kw.setdefault("min_interval", 0)
    return LogShipper(handler, sink, **kw)


def test_flush_sends_one_batched_message_without_header_by_default_and_collapses_repeats():
    handler, sink = ChannelHandler(), Sink()
    for _ in range(3):
        handler.emit(record("mesma falha"))
    handler.emit(record("outra falha", logging.ERROR))
    assert run(shipper(handler, sink).flush()) == 1
    assert len(sink.sent) == 1 and sink.sent[0].startswith("[limas-memory]\n")
    assert "mesma falha (x3)" in sink.sent[0] and "ERROR bot: outra falha" in sink.sent[0]
    assert run(shipper(handler, sink).flush()) == 0  # fila vazia: nada a enviar


def test_flush_reports_dropped_lines_and_splits_long_batches():
    handler, sink = ChannelHandler(maxsize=2), Sink()
    for i in range(5):
        handler.emit(record(f"evento {i}"))
    assert run(shipper(handler, sink).flush()) == 1
    assert "(3 linhas de log descartadas por excesso)" in sink.sent[0] and handler.dropped == 0
    big, sink2 = ChannelHandler(maxsize=1000), Sink()
    for i in range(300):
        big.emit(record(f"evento número {i} " + "x" * 30))
    assert run(shipper(big, sink2).flush()) >= 2 and all(len(m) <= applog.CHANNEL_LIMIT for m in sink2.sent)


def test_a_failed_send_keeps_the_lines_and_retries_on_the_next_flush(caplog):
    handler, sink = ChannelHandler(), Sink(fail_times=1)
    handler.emit(record("importante"))
    ship = shipper(handler, sink)
    with caplog.at_level(logging.WARNING, logger=applog.SHIP_NAME):
        assert run(ship.flush()) == 0
    assert sink.sent == [] and "não consegui enviar o log ao canal" in caplog.text
    handler.emit(record("depois"))
    assert run(ship.flush()) == 1  # a que falhou vai junto com a nova
    assert "importante" in sink.sent[0] and "depois" in sink.sent[0]


def test_failure_logs_do_not_re_enter_the_queue():
    handler, sink = ChannelHandler(), Sink(fail_times=5)
    logging.getLogger("limas").addHandler(handler)
    try:
        handler.emit(record("importante"))
        ship = shipper(handler, sink)
        run(ship.flush())  # falha e loga em limas.logship, que o handler ignora
        assert handler.queue.empty()
    finally:
        logging.getLogger("limas").removeHandler(handler)


def test_pending_lines_kept_after_failures_are_bounded():
    handler, sink = ChannelHandler(maxsize=1000), Sink(fail_times=99)
    ship = shipper(handler, sink, keep_on_failure=5)
    for i in range(50):
        handler.emit(record(f"evento {i}"))
    run(ship.flush())
    assert len(ship._pending) == 5 and ship._pending[-1].endswith("evento 49")


def test_rate_limit_waits_between_messages(monkeypatch):
    handler, sink = ChannelHandler(maxsize=1000), Sink()
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(applog.asyncio, "sleep", fake_sleep)
    for i in range(300):
        handler.emit(record(f"evento número {i} " + "x" * 30))
    ship = LogShipper(handler, sink, min_interval=3)
    assert run(ship.flush()) >= 2
    assert waits and all(0 < w <= 3 for w in waits)  # respeitou o intervalo entre as mensagens


def test_run_flushes_periodically_and_once_more_on_stop():
    handler, sink = ChannelHandler(), Sink()

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(shipper(handler, sink).run(stop))
        handler.emit(record("primeiro"))
        await asyncio.sleep(0.05)  # o ciclo de flush envia
        handler.emit(record("na despedida", logging.ERROR))
        stop.set()
        await task

    run(scenario())
    assert len(sink.sent) == 2 and "primeiro" in sink.sent[0] and "na despedida" in sink.sent[1]


def test_optional_header_identifies_the_app_in_a_shared_channel():
    handler, sink = ChannelHandler(), Sink()
    handler.emit(record("falha"))
    run(shipper(handler, sink, header="[meu-app]").flush())
    assert sink.sent[0].startswith("[meu-app]" + chr(10))
