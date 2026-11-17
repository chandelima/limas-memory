"""Fluxo de conversa compartilhado pelo terminal (chat.py) e pelo Telegram (bot.py).

Sem nada de interface: recebe texto, registra no histórico, consulta o agente de resposta
e deixa o Curator trabalhar por inatividade. Quem chama cuida de mostrar o resultado.
"""

from dataclasses import replace
from pathlib import Path

from conversation.log import ConversationLog, Message
from curator.agent import build_curator, curate
from curator.service import CurationService, Outcome, to_chat
from curator.tools import default_tools
from responder.agent import ReplyResult, reply

HISTORY_MESSAGES = 30  # quantas mensagens recentes o agente de resposta enxerga


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
        return curate(messages, context, agent=cache["agent"], channel=channel)

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
    if service.is_running:
        parts.append("curadoria em andamento")
    if idle is not None:
        parts.append(f"inativo há {idle:.0f}s (curadoria após {service.idle_seconds:.0f}s)")
    if service.last_error:
        parts.append(f"último erro: {service.last_error}")
    return "[status] " + " | ".join(parts)


def compact_history(messages: list[Message], curated_upto: int) -> list[Message]:
    """Encurta, no histórico do agente de resposta, o conteúdo de arquivos antigos.

    Só encurta o que já foi curado (os dados já estão na memória) e nunca a última
    mensagem. Arquivos ainda pendentes mantêm o texto completo: o Curator só roda depois
    da inatividade e o agente precisa conseguir responder sobre o arquivo nesse meio tempo.
    """
    out = []
    for i, m in enumerate(messages):
        if m.attachments and m.id <= curated_upto and i < len(messages) - 1:
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

    def respond(self, text: str, attachments: list[str] | None = None) -> ReplyResult:
        """Registra a fala do usuário, responde (lendo a memória) e registra a resposta."""
        self.log.append("usuario", text, attachments=attachments)
        recent = compact_history(self.log.recent(HISTORY_MESSAGES), self.log.curated_upto)
        history = [to_chat(m) for m in recent]
        self.outbox.clear()
        result = reply(history, agent=self.agent)
        result.files = list(self.outbox)
        self.outbox.clear()
        self.log.append("assistente", result.text)
        return result

    def curate_now(self) -> str:
        outcome = self.service.run_now()
        return describe_outcome(outcome) if outcome else "[curador] nada pendente."

    def status(self) -> str:
        return describe_status(self.service)
