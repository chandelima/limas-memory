"""Fluxo de conversa usado pelo bot do Telegram (bot.py).

Sem nada de interface: recebe texto, registra no histórico, consulta o agente de resposta
e deixa o Curator trabalhar por inatividade. Quem chama cuida de mostrar o resultado.
"""

import time
from dataclasses import replace
from pathlib import Path

from applog import get_logger
from conversation.log import ANSWERED, FAILED, READY, RECEIVED, ConversationLog, Message
from curator.agent import ToolEvent, build_curator, curate
from curator.service import CurationService, Outcome, to_chat
from curator.tools import default_tools, make_store_and_git
from responder.agent import ReplyResult, reply

log = get_logger("responder")

HISTORY_MESSAGES = 30  # quantas mensagens recentes o agente de resposta enxerga


SYNC_MESSAGE = "sync: registra alterações feitas fora do Curator (tarefas, anexos)"


def sync_memory(memory_dir: Path | None) -> ToolEvent | None:
    """Depois de cada curadoria: commita o que mudou fora dela (tarefas e anexos são gravados
    direto pelas tools do agente de resposta) e envia ao remote o que ainda não subiu.
    Sem nada a fazer, devolve None; falha de git nunca derruba a curadoria."""
    try:
        _, git = make_store_and_git(memory_dir)
        result = git.sync(SYNC_MESSAGE)
    except Exception as exc:  # noqa: BLE001
        return ToolEvent("sync_memory", {}, f"ERRO: {type(exc).__name__}: {exc}")
    if result.sha is None and result.pushed and not result.push_error:
        return ToolEvent("sync_memory", {}, "OK: repositório já estava em dia com o remote.")
    if result.pushed:
        return ToolEvent("sync_memory", {}, f"OK: {'commit ' + result.sha + ' e ' if result.sha else ''}enviado ao remote.")
    if result.sha is None and "Sem remote" in result.push_error:
        return None
    return ToolEvent("sync_memory", {}, f"OK: salvo localmente. Push pendente: {result.push_error}")


def build_service(
    log: ConversationLog,
    memory_dir: Path | None,
    idle_seconds: float,
    channel: str = "desconhecido",
) -> CurationService:
    """Serviço de curadoria; o agente do Curator só é criado na primeira vez que roda."""
    cache: dict = {}

    def curate_fn(messages, context, channel):
        if "agent" not in cache:
            cache["agent"] = build_curator(tools=default_tools(memory_dir))
        result = curate(messages, context, agent=cache["agent"], channel=channel)
        sync = sync_memory(memory_dir)
        if sync is not None:
            result.trace.append(sync)
        return result

    return CurationService(log, curate_fn, channel=channel, idle_seconds=idle_seconds)


def describe_outcome(outcome: Outcome) -> str:
    if outcome.busy:
        return "[curador] já há uma curadoria em andamento; aguarde ela terminar."
    if outcome.error:
        return f"[curador] falhou; {outcome.messages} mensagem(ns) seguem pendentes: {outcome.error}"
    tools = " -> ".join(outcome.result.tool_calls) or "nenhuma tool"
    return (
        f"[curador] {outcome.messages} mensagem(ns) processada(s) ({tools})\n"
        f"{outcome.result.report}"
    )


def describe_status(service: CurationService) -> str:
    pending = len(service.log.pending())
    idle = service.seconds_idle()
    parts = [f"pendentes: {pending}"]
    in_progress = len(service.log.unfinished())
    if in_progress:
        parts.append(f"mensagens em processamento: {in_progress}")
    if service.is_running:
        parts.append("curadoria em andamento")
    if idle is not None:
        parts.append(f"inativo há {idle:.0f}s (curadoria após {service.idle_seconds:.0f}s)")
    if service.last_error:
        parts.append(f"último erro: {service.last_error}")
    return "[status] " + " | ".join(parts)


def compact_history(messages: list[Message]) -> list[Message]:
    """Encurta, no histórico do agente de resposta, o conteúdo de arquivos antigos.

    Só encurta o que já foi curado (os dados já estão na memória) e nunca a última
    mensagem. Arquivos ainda pendentes mantêm o texto completo: o Curator só roda depois
    da inatividade e o agente precisa conseguir responder sobre o arquivo nesse meio tempo.
    """
    out = []
    for i, m in enumerate(messages):
        if m.attachments and m.curated and i < len(messages) - 1:
            header = m.text.splitlines()[0]
            omitted = "(conteúdo omitido do histórico; os dados estão na memória)"
            m = replace(m, text=f"{header}\n{omitted}")
        out.append(m)
    return out


class Conversation:
    """Uma conversa: histórico + agente de resposta + serviço de curadoria.

    `outbox` é a lista que a tool `send_attachment` do agente preenche; os arquivos
    pedidos numa resposta saem em `ReplyResult.files`."""

    def __init__(self, log: ConversationLog, service: CurationService, agent, outbox: list | None = None):
        self.log = log
        self.service = service
        self.agent = agent
        self.outbox = outbox if outbox is not None else []

    # ---- 1. chegada: persistir logo, antes de qualquer processamento -------------

    def ingest(self, text: str, attachments: list[str] | None = None, status: str = RECEIVED) -> Message:
        """Grava a fala do usuário assim que ela chega, para não se perder se o bot cair."""
        return self.log.append("usuario", text, attachments=attachments, status=status)

    def set_content(self, msg_id: int, text: str, status: str = READY) -> Message:
        """Preenche o conteúdo de uma mensagem (ex.: texto extraído de um arquivo)."""
        return self.log.update(msg_id, text=text, status=status)

    def fail(self, msg_id: int, text: str | None = None) -> Message:
        """Dá a mensagem como resolvida sem resposta (arquivo ilegível, erro ao responder)."""
        return self.log.update(msg_id, text=text, status=FAILED)

    def unfinished(self) -> list[Message]:
        return self.log.unfinished()

    # ---- 2. resposta -------------------------------------------------------------

    def respond_to(self, msg_id: int) -> ReplyResult:
        """Responde à mensagem `msg_id` (já gravada), lendo a memória, e registra a resposta."""
        history = compact_history(self.log.history(msg_id, HISTORY_MESSAGES))
        self.outbox.clear()
        started = time.monotonic()
        result = reply([to_chat(m) for m in history], agent=self.agent)
        usage = result.usage
        cost = f"US$ {usage.cost:.4f}" if usage.cost_reported else "n/d"
        log.info("resposta à mensagem %s: %d chamada(s) de tool, %d arquivo(s), %d tokens, custo %s, %.1fs",
                 msg_id, len(result.trace), len(self.outbox), usage.total_tokens, cost, time.monotonic() - started)
        result.files = list(self.outbox)
        self.outbox.clear()
        self.log.append("assistente", result.text, reply_to=msg_id)
        self.log.update(msg_id, status=ANSWERED)
        return result

    def respond(self, text: str, attachments: list[str] | None = None) -> ReplyResult:
        """Atalho: grava a fala do usuário e responde na sequência."""
        return self.respond_to(self.ingest(text, attachments=attachments, status=READY).id)

    def curate_now(self) -> str:
        outcome = self.service.run_now()
        return describe_outcome(outcome) if outcome else "[curador] nada pendente."

    def status(self) -> str:
        return describe_status(self.service)
