import chat as chat_cli
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


def run_chat(tmp_path, inputs, monkeypatch, replies=None, service=None, extra=None):
    """Roda o chat com respostas falsas; devolve (log, histórico visto pelo agente)."""
    seen = []
    texts = iter(replies or [])

    def fake_reply(history, agent=None, now=None):
        seen.append([(m.role, m.text) for m in history])
        return ReplyResult(text=next(texts, "ok"))

    monkeypatch.setattr(chat_cli, "reply", fake_reply)
    it = iter(inputs)
    log_path = tmp_path / "conversa.json"
    args = ["--log", str(log_path), "--memory-dir", str(tmp_path / "mem")] + (extra or [])
    code = chat_cli.main(args, agent=object(), read=lambda _: next(it),
                         service=service, start_watcher=False)
    assert code == 0
    return ConversationLog(log_path), seen


def test_each_turn_is_logged_for_the_curator(tmp_path, monkeypatch, capsys):
    log, _ = run_chat(tmp_path, ["tomo café", "valeu", "sair"], monkeypatch, replies=["Anotado", "De nada"])
    assert [(m.role, m.text) for m in log.messages()] == [
        ("usuario", "tomo café"), ("assistente", "Anotado"),
        ("usuario", "valeu"), ("assistente", "De nada"),
    ]
    out = capsys.readouterr().out
    assert "IA: Anotado" in out and "4 mensagem(ns) ainda não curada(s)" in out


def test_history_persists_across_sessions(tmp_path, monkeypatch):
    run_chat(tmp_path, ["primeira sessão", "sair"], monkeypatch, replies=["ok1"])
    _, seen = run_chat(tmp_path, ["segunda sessão", "sair"], monkeypatch, replies=["ok2"])
    assert seen[0] == [("usuario", "primeira sessão"), ("assistente", "ok1"), ("usuario", "segunda sessão")]


def test_blank_input_and_commands_are_not_logged(tmp_path, monkeypatch):
    log, seen = run_chat(tmp_path, ["", "/status", "oi", "sair"], monkeypatch)
    assert [m.text for m in log.messages()] == ["oi", "ok"] and len(seen) == 1


def test_curar_command_runs_curator_on_pending_messages(tmp_path, monkeypatch, capsys):
    curator = FakeCurator()
    log = ConversationLog(tmp_path / "conversa.json")
    service = CurationService(log, curator, channel="terminal", idle_seconds=600)
    log_after, _ = run_chat(tmp_path, ["tomo café", "/curar", "sair"], monkeypatch,
                            replies=["Anotado"], service=service)
    out = capsys.readouterr().out
    assert "[curador] 2 mensagem(ns) processada(s) (commit_memory)" in out and "Gravei 1 documento." in out
    assert curator.calls == [(["tomo café", "Anotado"], "", "terminal")]
    assert log_after.pending() == []
    assert "ainda não curada" not in out


def test_curar_with_nothing_pending(tmp_path, monkeypatch, capsys):
    run_chat(tmp_path, ["/curar", "sair"], monkeypatch)
    assert "[curador] nada pendente." in capsys.readouterr().out


def test_status_command(tmp_path, monkeypatch, capsys):
    run_chat(tmp_path, ["oi", "/status", "sair"], monkeypatch, extra=["--idle-seconds", "30"])
    out = capsys.readouterr().out
    assert "[status] pendentes: 2" in out and "curadoria após 30s" in out


def test_failed_curation_is_reported_and_messages_stay_pending(tmp_path, monkeypatch, capsys):
    log = ConversationLog(tmp_path / "conversa.json")
    service = CurationService(log, FakeCurator(fail=True), channel="terminal")
    log_after, _ = run_chat(tmp_path, ["oi", "/curar", "sair"], monkeypatch, service=service)
    out = capsys.readouterr().out
    assert "[curador] falhou" in out and "sem rede" in out
    assert len(log_after.pending()) == 2


def test_describe_outcome_variants():
    ok = Outcome(3, result=CurateResult(report="Relatório", trace=[]))
    assert "3 mensagem(ns)" in chat_cli.describe_outcome(ok) and "nenhuma tool" in chat_cli.describe_outcome(ok)
    assert "falhou" in chat_cli.describe_outcome(Outcome(2, error="boom"))


def test_real_conversation_file_is_never_touched(tmp_path, monkeypatch):
    import config

    run_chat(tmp_path, ["oi", "sair"], monkeypatch)
    assert not config.CONVERSATION_FILE.exists()  # a fixture aponta para um temporário
