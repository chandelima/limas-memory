import chat as chat_cli
from conversation.log import ConversationLog
from curator.agent import CurateResult
from curator.service import CurationService


def test_curate_command_while_another_curation_runs_says_so(tmp_path, capsys):
    log = ConversationLog(tmp_path / "conversa.json")
    log.append("usuario", "algo pendente")
    service = CurationService(log, lambda m, c, ch: CurateResult(report="ok"), channel="terminal")
    service._busy.acquire()  # simula uma curadoria automática em andamento
    try:
        inputs = iter(["/curate", "sair"])
        code = chat_cli.main(["--memory-dir", str(tmp_path / "mem")], agent=object(),
                             read=lambda _: next(inputs), service=service, start_watcher=False)
    finally:
        service._busy.release()
    out = capsys.readouterr().out
    assert code == 0
    assert "já há uma curadoria em andamento" in out and "nada pendente" not in out
    assert len(log.pending()) == 1  # nada foi processado
