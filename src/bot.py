"""Bot do Telegram: conversa com o agente de resposta e alimenta o Curator.

Uso:
    python src/bot.py
    python src/bot.py --memory-dir D:/tmp/memoria-teste --log D:/tmp/conversa.json --idle-seconds 30
    python src/bot.py --verbose    # mostra no console as consultas à memória de cada resposta

Só o usuário em TELEGRAM_ALLOWED_USER_ID é atendido (e só em conversa privada). Sem essa
env, o bot recusa todo mundo e imprime o ID de quem escreveu, para você configurá-la.

Fluxo de cada mensagem:
  1. chegada: a mensagem é gravada no histórico na hora (estado received/reading) e o
     "digitando..." liga, antes de qualquer processamento;
  2. fila: uma mensagem é processada por vez, na ordem de chegada (o "digitando..." continua
     enquanto ela espera a vez); arquivos são baixados e lidos; o agente de resposta consulta
     a memória (somente leitura) e responde;
  3. depois de CURATOR_IDLE_SECONDS sem mensagens, o Curator processa o que ficou pendente e
     o relatório é enviado a você (uma falha é avisada uma vez por sequência).
Se o bot cair no meio, ao reiniciar ele retoma as mensagens sem resposta das últimas 24 h.
Todo dia, no horário de BRIEFING_TIME (padrão 06:00), manda o resumo da manhã com as tarefas
de hoje, as atrasadas e as dos próximos dias (nada, se não houver nenhuma).
Comandos: /status e /curate (não vão para o modelo).
"""

import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

from telegram import BotCommand, Update
from telegram.constants import ChatType
from telegram.ext import (Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

import config
from applog import LogShipper, configure_logging, get_logger, parse_channel_id
from attachments import reader
from briefing import BriefingScheduler, parse_briefing_time
from attachments.registry import AttachmentRegistry
from attachments.tools import build_attachment_tools
from chatflow import Conversation, build_service, describe_outcome
from conversation.log import READING, ConversationLog
from responder.agent import build_responder, default_read_tools
from storage.store import MemoryStore
from tasks import panel
from tasks.model import TaskError
from tasks.store import TaskStore
from tasks.tools import build_task_tools
from telegram_gateway import TELEGRAM_LIMIT, TYPING_INTERVAL, TelegramGateway, split_message, to_plain  # noqa: F401

log = get_logger("bot")

WATCH_INTERVAL = 30  # segundos entre verificações de inatividade
SHUTDOWN_WAIT = 30  # espera máxima por uma curadoria em andamento ao encerrar
RECOVERY_MAX_AGE = timedelta(hours=24)  # mensagens sem resposta mais antigas que isso são descartadas

HELP = "Pode conversar comigo normalmente.\n/status mostra o estado da memória.\n/curate processa agora o que ficou pendente.\n/todo mostra as tarefas com botões."
FILE_NOTICE = "Recebi o arquivo, lendo..."
RECOVERY_NOTICE = "Voltei depois de uma reinicialização e vou responder as mensagens que ficaram pendentes."
ERROR_REPLY = "Tive um problema para responder agora. Tente de novo em instantes."

COMMANDS = [
    BotCommand("status", "Mostra o estado da memória e das mensagens pendentes"),
    BotCommand("todo", "Lista as tarefas com botões para marcar e desmarcar"),
    BotCommand("curate", "Processa agora as mensagens pendentes na memória"),
]


class BotHandlers:
    def __init__(
        self,
        conversation: Conversation,
        allowed_user_id: int | None,
        registry: AttachmentRegistry | None = None,
        read_document=reader.read_document,
        verbose: bool = False,
        gateway_factory=TelegramGateway,
        briefing: BriefingScheduler | None = None,
        tasks: TaskStore | None = None,
    ):
        self.conversation = conversation
        self.allowed_user_id = allowed_user_id
        self.registry = registry  # sem registro, o bot ignora arquivos
        self.read_document = read_document
        self.verbose = verbose  # imprime no console as consultas à memória de cada resposta
        self.gateway_factory = gateway_factory
        self.briefing = briefing  # resumo da manhã; None = desligado
        self.tasks = tasks  # painel de tarefas com botões; None = desligado
        self._lock = asyncio.Lock()  # uma mensagem por vez: o histórico é sequencial

    def _authorized(self, update: Update) -> bool:
        user, chat = update.effective_user, update.effective_chat
        if user is None or chat is None or chat.type != ChatType.PRIVATE:
            return False
        if self.allowed_user_id is None:
            log.warning("mensagem recusada do usuário id %s; para liberar, defina TELEGRAM_ALLOWED_USER_ID=%s",
                        user.id, user.id)
            return False
        if user.id != self.allowed_user_id:
            log.warning("mensagem de usuário não autorizado id=%s", user.id)
            return False
        return True

    # ---- comandos (não vão para o modelo) ------------------------------------

    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._authorized(update):
            await self.gateway_factory(context.bot).send_text(update.effective_chat.id, f"Olá! {HELP}")

    async def on_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._authorized(update):
            await self.gateway_factory(context.bot).send_text(update.effective_chat.id, self.conversation.status())

    async def on_curate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        gateway, chat_id = self.gateway_factory(context.bot), update.effective_chat.id
        async with gateway.typing(chat_id):
            report = await asyncio.to_thread(self.conversation.curate_now)
        await gateway.send_text(chat_id, report)

    async def on_todo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/todo [hoje|semana|pendentes] [etiqueta]: painel de tarefas com botões."""
        if not self._authorized(update) or self.tasks is None:
            return
        gateway, chat_id = self.gateway_factory(context.bot), update.effective_chat.id
        args = [a.lower() for a in (context.args or [])]
        scope = "hoje"
        if args and args[0] in panel.PANEL_SCOPES:
            scope = args.pop(0)
        await self._send_panel(gateway, chat_id, scope, args[0].lstrip("#") if args else "")

    async def _send_panel(self, gateway, chat_id: int, scope: str, tag: str = "") -> None:
        try:
            text, rows = await asyncio.to_thread(panel.build_panel, self.tasks, scope, tag)
        except Exception:  # noqa: BLE001 - o painel é um extra: nunca derruba a resposta
            log.exception("erro ao montar o painel de tarefas")
            return
        if rows:
            await gateway.send_buttons(chat_id, text, rows)
        else:
            await gateway.send_text(chat_id, text)

    async def on_task_toggle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Toque num botão do painel: inverte o estado da tarefa e redesenha o painel."""
        query = update.callback_query
        if query is None or not self._authorized(update) or self.tasks is None:
            return
        gateway = self.gateway_factory(context.bot)
        parsed = panel.decode(query.data or "")
        if parsed is None:
            await gateway.answer_callback(query.id)
            return
        task_id, on, scope, tag = parsed
        try:
            text, done = await asyncio.to_thread(panel.toggle, self.tasks, task_id, on)
            notice = f"{'✅ Concluída' if done else '🔲 Reaberta'}: {text}"
        except TaskError as exc:
            notice = f"Não deu: {exc}"
        except Exception:  # noqa: BLE001
            log.exception("erro ao alternar a tarefa %s", task_id)
            notice = "Tive um problema ao atualizar a tarefa."
        await gateway.answer_callback(query.id, notice[:190])
        try:
            new_text, rows = await asyncio.to_thread(panel.build_panel, self.tasks, scope, tag)
            await gateway.edit_buttons(update.effective_chat.id, query.message.message_id, new_text, rows)
        except Exception:  # noqa: BLE001 - ex.: "message is not modified"
            log.warning("não consegui atualizar o painel de tarefas", exc_info=True)

    async def on_added_to_chat(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Bot adicionado a um canal ou grupo: mostra o ID no console, para usar em LOG_CHANNEL_ID
        (como acontece com o ID de um usuário novo). Só informa; não responde nada no chat."""
        change = update.my_chat_member
        chat = update.effective_chat
        if change is None or chat is None or chat.type == ChatType.PRIVATE:
            return
        if change.new_chat_member.status not in ("administrator", "member"):
            return  # removido ou banido
        kind = "canal" if chat.type == ChatType.CHANNEL else "grupo"
        log.info("bot adicionado ao %s '%s'; para enviar os logs para lá, defina LOG_CHANNEL_ID=%s%s",
                 kind, chat.title or "?", chat.id,
                 "" if change.new_chat_member.status == "administrator" else " (e torne o bot administrador)")

    async def on_unknown(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._authorized(update):
            await self.gateway_factory(context.bot).send_text(
                update.effective_chat.id, f"Comando desconhecido. {HELP}")

    # ---- mensagens: gravar na chegada, "digitando" já, processar na vez ------

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        text = (update.message.text or "").strip()
        if not text:
            return
        gateway, chat_id = self.gateway_factory(context.bot), update.effective_chat.id
        msg = self.conversation.ingest(text)  # 1. grava antes de qualquer outra coisa
        async with gateway.typing(chat_id):  # 2. "digitando..." já, mesmo esperando a vez
            async with self._lock:  # 3. uma por vez, na ordem de chegada
                await self._answer(gateway, chat_id, msg.id)

    async def on_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Documento ou foto: grava na chegada, avisa que está lendo e processa na vez."""
        if not self._authorized(update) or self.registry is None:
            return
        msg = update.message
        gateway, chat_id = self.gateway_factory(context.bot), update.effective_chat.id
        if msg.document:
            kind, item = "document", msg.document
            filename = item.file_name or "arquivo"
            mime = reader.guess_mime(filename, item.mime_type or "")
        elif msg.photo:
            kind, item = "photo", msg.photo[-1]  # a maior resolução
            filename, mime = f"foto-{item.file_unique_id}.jpg", "image/jpeg"
        else:
            return
        if not reader.is_supported(mime):
            await gateway.send_text(chat_id, "Por enquanto só leio PDF e imagens (JPG, PNG, WebP ou GIF).")
            return
        if (item.file_size or 0) > reader.MAX_FILE_BYTES:
            await gateway.send_text(chat_id, "Esse arquivo passa do limite de 20 MB.")
            return

        att = self.registry.add(  # grava o anexo já, para poder baixar de novo se o bot cair
            file_id=item.file_id, file_unique_id=item.file_unique_id, kind=kind, chat_id=chat_id,
            message_id=msg.message_id, filename=filename, mime=mime, size=item.file_size or 0,
            caption=msg.caption or "",
        )
        stored = self.conversation.ingest(
            reader.format_placeholder(att.ref, filename, att.caption), attachments=[att.id], status=READING)
        async with gateway.typing(chat_id):
            await gateway.send_text(chat_id, FILE_NOTICE)
            async with self._lock:
                await self._read_and_answer(gateway, chat_id, stored.id, att)

    # ---- núcleo (usado ao vivo e na recuperação) -----------------------------

    async def _read_and_answer(self, gateway, chat_id: int, msg_id: int, att) -> None:
        """Baixa e lê o arquivo; com o conteúdo pronto, passa para a resposta."""
        try:
            data = await gateway.download(att.file_id)
            text = await asyncio.to_thread(self.read_document, data, att.filename, att.mime)
        except reader.ReaderError as exc:
            log.warning("arquivo %s não pôde ser lido: %s", att.ref, exc)
            self.conversation.fail(msg_id, text=reader.format_unreadable(att.ref, att.filename, att.caption))
            await gateway.send_text(chat_id, f"Não consegui ler esse arquivo. {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - nunca expõe detalhes internos no chat
            log.exception("erro ao baixar o arquivo %s", att.ref)
            self.conversation.fail(msg_id, text=reader.format_unreadable(att.ref, att.filename, att.caption))
            await gateway.send_text(chat_id, "Não consegui baixar o arquivo agora. Tente de novo em instantes.")
            return
        self.conversation.set_content(msg_id, reader.format_attachment_message(att.ref, att.filename, att.caption, text))
        await self._answer(gateway, chat_id, msg_id)

    async def _answer(self, gateway, chat_id: int, msg_id: int) -> None:
        """Passa a mensagem ao agente de resposta, responde e reenvia os arquivos pedidos."""
        try:
            result = await asyncio.to_thread(self.conversation.respond_to, msg_id)
        except Exception as exc:  # noqa: BLE001 - nunca expõe detalhes internos no chat
            log.exception("erro ao responder à mensagem %s", msg_id)
            self.conversation.fail(msg_id)
            await gateway.send_text(chat_id, ERROR_REPLY)
            return
        if self.verbose:
            self._log_trace(result)
        await gateway.send_text(chat_id, result.text)
        if self.tasks is not None:  # o agente listou tarefas: mostra também o painel com botões
            listing = next((e for e in reversed(result.trace)
                            if e.name == "list_tasks" and not e.result.startswith("ERRO")), None)
            if listing is not None:
                scope = str(listing.args.get("scope") or "hoje").lower()
                if scope in panel.PANEL_SCOPES:
                    await self._send_panel(gateway, chat_id, scope, str(listing.args.get("tag") or ""))
        for att in result.files:
            try:
                await gateway.send_file(chat_id, att)
            except Exception as exc:  # noqa: BLE001
                log.warning("erro ao reenviar %s", att.ref, exc_info=True)
                await gateway.send_text(chat_id, f"Não consegui reenviar o arquivo {att.filename}.")

    @staticmethod
    def _log_trace(result) -> None:
        """Modo --verbose: mostra no console o que o agente consultou para responder."""
        for event in result.trace:
            args = ", ".join(f"{k}={v!r}" for k, v in event.args.items())
            print(f"  [memória] {event.name}({args})", flush=True)
        for att in result.files:
            print(f"  [arquivo] reenviando {att.ref} ({att.filename})", flush=True)

    # ---- recuperação depois de uma queda -------------------------------------

    async def recover(self, gateway, now: datetime | None = None) -> None:
        """Ao iniciar, retoma as mensagens que ficaram sem resposta (o bot caiu no meio).
        As mais antigas que RECOVERY_MAX_AGE são descartadas (viram `failed`)."""
        chat_id = self.allowed_user_id
        if chat_id is None:
            return
        now = now or datetime.now().astimezone()
        fresh, stale = [], 0
        for msg in self.conversation.unfinished():
            if now - datetime.fromisoformat(msg.at) > RECOVERY_MAX_AGE:
                self.conversation.fail(msg.id)
                stale += 1
            else:
                fresh.append(msg)
        if stale:
            log.warning("%d mensagem(ns) sem resposta há mais de 24 h foram descartadas ao reiniciar", stale)
        if not fresh:
            return
        log.warning("reinício com %d mensagem(ns) sem resposta; retomando", len(fresh))
        await gateway.send_text(chat_id, RECOVERY_NOTICE)
        for msg in fresh:
            async with gateway.typing(chat_id):
                async with self._lock:
                    await self._resume(gateway, chat_id, msg)

    async def _resume(self, gateway, chat_id: int, msg) -> None:
        if msg.status != READING:  # received/ready: o conteúdo já está no histórico
            await self._answer(gateway, chat_id, msg.id)
            return
        att = self.registry.get(msg.attachments[0]) if self.registry and msg.attachments else None
        if att is None:
            self.conversation.fail(msg.id)
            await gateway.send_text(chat_id, "Não consegui retomar um arquivo que você enviou antes; envie de novo.")
            return
        await self._read_and_answer(gateway, chat_id, msg.id, att)


# ---- aplicação -------------------------------------------------------------


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("erro no bot", exc_info=context.error if isinstance(context.error, BaseException) else None)


async def notify_outcome(app: Application, outcome) -> None:
    """Curadoria automática terminou: mostra no console e envia o relatório ao canal de logs
    (ou, sem canal configurado, ao usuário). O /curate manual responde ao próprio usuário.

    Falhas são avisadas uma vez por sequência (o serviço tenta de novo a cada poucos
    minutos; sem isso seriam várias mensagens iguais). Um sucesso reinicia a contagem.
    """
    text = describe_outcome(outcome)
    print(text, flush=True)
    log_channel = app.bot_data.get("log_channel_id")
    target = log_channel if log_channel is not None else app.bot_data.get("allowed_user_id")
    if target is None:
        return
    if outcome.error:
        if app.bot_data.get("failure_notified"):
            return
        app.bot_data["failure_notified"] = True
    else:
        app.bot_data["failure_notified"] = False
    try:
        await app.bot_data["gateway"].send_text(target, text, raw=log_channel is not None)
    except Exception as exc:  # noqa: BLE001 - falhar ao avisar não pode derrubar o bot
        log.warning("não consegui enviar o relatório da curadoria (%s)",
                    "canal de logs" if log_channel is not None else "usuário", exc_info=exc)


async def _safe_recover(handlers: BotHandlers, gateway) -> None:
    try:
        await handlers.recover(gateway)
    except Exception as exc:  # noqa: BLE001
        log.error("erro ao retomar mensagens pendentes", exc_info=exc)


async def post_init(app: Application) -> None:
    """Registra os comandos, liga o laço do Curator e retoma mensagens sem resposta."""
    gateway = app.bot_data["gateway"]
    await gateway.register_commands(COMMANDS, app.bot_data.get("allowed_user_id"))
    stop = asyncio.Event()
    app.bot_data["stop"] = stop
    app.bot_data["watcher"] = asyncio.create_task(
        app.bot_data["service"].watch(stop, WATCH_INTERVAL, on_outcome=lambda outcome: notify_outcome(app, outcome))
    )
    app.bot_data["recovery"] = asyncio.create_task(_safe_recover(app.bot_data["handlers"], gateway))
    log.info("bot iniciado (modelo %s)", config.OPENROUTER_MODEL, extra={"channel": True})
    log_handler, log_channel = app.bot_data.get("log_handler"), app.bot_data.get("log_channel_id")
    if log_handler is not None and log_channel is not None:
        shipper = LogShipper(log_handler, lambda text: gateway.send_text(log_channel, text, raw=True),
                             header=config.LOG_CHANNEL_HEADER)
        app.bot_data["log_shipper"] = asyncio.create_task(shipper.run(stop))
    allowed = app.bot_data.get("allowed_user_id")
    briefing = getattr(app.bot_data["handlers"], "briefing", None)
    if briefing is not None and allowed is not None:
        app.bot_data["briefing"] = asyncio.create_task(
            briefing.run(stop, lambda text: gateway.send_text(allowed, text)))


async def post_shutdown(app: Application) -> None:
    recovery = app.bot_data.get("recovery")
    if recovery is not None and not recovery.done():
        recovery.cancel()
    stop, watcher = app.bot_data.get("stop"), app.bot_data.get("watcher")
    if stop is None or watcher is None:
        return
    log.info("bot encerrando", extra={"channel": True})  # entra na fila antes do envio final
    stop.set()
    briefing = app.bot_data.get("briefing")
    if briefing is not None:
        try:
            await asyncio.wait_for(briefing, timeout=5)  # com `stop` ligado, termina na hora
        except asyncio.TimeoutError:
            briefing.cancel()
    try:
        await asyncio.wait_for(watcher, timeout=SHUTDOWN_WAIT)  # deixa uma curadoria terminar
    except asyncio.TimeoutError:
        watcher.cancel()
    shipper = app.bot_data.get("log_shipper")
    if shipper is not None:
        try:
            await asyncio.wait_for(shipper, timeout=10)  # com `stop` ligado, envia o que sobrou e termina
        except asyncio.TimeoutError:
            shipper.cancel()


def build_application(token: str, handlers: BotHandlers, service) -> Application:
    # concurrent_updates: sem isso a biblioteca trata uma mensagem por vez, e um arquivo lento
    # seguraria até a gravação e o "digitando..." da mensagem seguinte.
    app = (Application.builder().token(token).concurrent_updates(True)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    app.bot_data["service"] = service
    app.bot_data["handlers"] = handlers
    app.bot_data["allowed_user_id"] = handlers.allowed_user_id
    app.bot_data["gateway"] = handlers.gateway_factory(app.bot)
    app.add_handler(ChatMemberHandler(handlers.on_added_to_chat, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("start", handlers.on_start))
    app.add_handler(CommandHandler("status", handlers.on_status))
    app.add_handler(CommandHandler("curate", handlers.on_curate))
    app.add_handler(CommandHandler("todo", handlers.on_todo))
    app.add_handler(CallbackQueryHandler(handlers.on_task_toggle, pattern=r"^tk:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.on_message))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handlers.on_file))
    app.add_handler(MessageHandler(filters.COMMAND, handlers.on_unknown))  # sempre por último
    app.add_error_handler(on_error)
    return app


def main(argv=None, agent=None, application_factory=build_application) -> int:
    ap = argparse.ArgumentParser(description="Bot do Telegram com memória.")
    ap.add_argument("--memory-dir", type=Path, default=None,
                    help="pasta da memória (padrão: MEMORY_DIR / ./memory)")
    ap.add_argument("--log", type=Path, default=None,
                    help="arquivo do histórico (padrão: CONVERSATION_FILE / ./data/conversa.json)")
    ap.add_argument("--idle-seconds", type=float, default=config.CURATOR_IDLE_SECONDS,
                    help="inatividade antes de o Curator processar (padrão: CURATOR_IDLE_SECONDS)")
    ap.add_argument("--verbose", action="store_true",
                    help="imprime no console as consultas à memória de cada resposta")
    args = ap.parse_args(argv)

    if not config.TELEGRAM_BOT_TOKEN:
        print("Erro: defina TELEGRAM_BOT_TOKEN no ambiente ou no .env (token do @BotFather)", file=sys.stderr)
        return 1
    try:
        briefing_time = parse_briefing_time(config.BRIEFING_TIME)
    except ValueError as exc:
        print(f"Erro: BRIEFING_TIME: {exc}", file=sys.stderr)
        return 1
    try:
        log_channel = parse_channel_id(config.LOG_CHANNEL_ID)
    except ValueError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    memory_store = MemoryStore(args.memory_dir or config.MEMORY_DIR)
    registry = AttachmentRegistry(memory_store)
    tasks = TaskStore(memory_store)  # lista de tarefas (todos/todo.md), gravada por código
    outbox: list = []  # arquivos que o agente pede para reenviar na resposta atual
    if agent is None:
        try:
            tools = (default_read_tools(args.memory_dir) + build_attachment_tools(registry, outbox)
                     + build_task_tools(tasks))
            agent = build_responder(tools=tools)
        except RuntimeError as exc:
            print(f"Erro: {exc}", file=sys.stderr)
            return 1

    log_handler = configure_logging(config.LOG_LEVEL, config.LOG_CHANNEL_LEVEL, channel=log_channel is not None)
    history = ConversationLog(args.log or config.CONVERSATION_FILE)
    service = build_service(history, args.memory_dir, args.idle_seconds, channel="Telegram")
    briefing = (BriefingScheduler(tasks, briefing_time.at, config.BRIEFING_STATE_FILE, log=history, tz=briefing_time.tz)
                if briefing_time else None)
    handlers = BotHandlers(Conversation(history, service, agent, outbox), config.TELEGRAM_ALLOWED_USER_ID,
                           registry, verbose=args.verbose, briefing=briefing, tasks=tasks)
    app = application_factory(config.TELEGRAM_BOT_TOKEN, handlers, service)
    app.bot_data["log_handler"] = log_handler  # o envio dos logs ao canal começa no post_init
    app.bot_data["log_channel_id"] = log_channel

    print(f"Bot iniciado | modelo: {config.OPENROUTER_MODEL} | memória: {args.memory_dir or config.MEMORY_DIR}")
    print(f"Histórico: {history.path} | Curador após {args.idle_seconds:.0f}s de inatividade")
    print(f"Canal de logs: {log_channel} ({config.LOG_CHANNEL_LEVEL.upper()}+)" if log_channel is not None
          else "Canal de logs: desligado")
    print(f"Resumo da manhã: {briefing_time.describe()}" if briefing_time else "Resumo da manhã: desligado")
    if config.TELEGRAM_ALLOWED_USER_ID is None:
        print("ATENÇÃO: TELEGRAM_ALLOWED_USER_ID não definido. O bot recusará todos e imprimirá "
              "o ID de quem escrever.")
    print("Ctrl+C para parar.\n", flush=True)
    app.run_polling()
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
