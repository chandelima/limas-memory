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
Comandos: /status e /curate (não vão para o modelo).
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

from telegram import BotCommand, Update
from telegram.constants import ChatType
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import config
from attachments import reader
from attachments.registry import AttachmentRegistry
from attachments.tools import build_attachment_tools
from chatflow import Conversation, build_service, describe_outcome
from conversation.log import READING, ConversationLog
from responder.agent import build_responder, default_read_tools
from storage.store import MemoryStore
from telegram_gateway import TELEGRAM_LIMIT, TYPING_INTERVAL, TelegramGateway, split_message, to_plain  # noqa: F401

WATCH_INTERVAL = 30  # segundos entre verificações de inatividade
SHUTDOWN_WAIT = 30  # espera máxima por uma curadoria em andamento ao encerrar
RECOVERY_MAX_AGE = timedelta(hours=24)  # mensagens sem resposta mais antigas que isso são descartadas

HELP = "Pode conversar comigo normalmente.\n/status mostra o estado da memória.\n/curate processa agora o que ficou pendente."
FILE_NOTICE = "Recebi o arquivo, lendo..."
RECOVERY_NOTICE = "Voltei depois de uma reinicialização e vou responder as mensagens que ficaram pendentes."
ERROR_REPLY = "Tive um problema para responder agora. Tente de novo em instantes."

COMMANDS = [
    BotCommand("status", "Mostra o estado da memória e das mensagens pendentes"),
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
    ):
        self.conversation = conversation
        self.allowed_user_id = allowed_user_id
        self.registry = registry  # sem registro, o bot ignora arquivos
        self.read_document = read_document
        self.verbose = verbose  # imprime no console as consultas à memória de cada resposta
        self.gateway_factory = gateway_factory
        self._lock = asyncio.Lock()  # uma mensagem por vez: o histórico é sequencial

    def _authorized(self, update: Update) -> bool:
        user, chat = update.effective_user, update.effective_chat
        if user is None or chat is None or chat.type != ChatType.PRIVATE:
            return False
        if self.allowed_user_id is None:
            print(f"[bot] mensagem recusada do usuário id {user.id}. "
                  f"Para liberar, defina TELEGRAM_ALLOWED_USER_ID={user.id}", flush=True)
            return False
        return user.id == self.allowed_user_id

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
            self.conversation.fail(msg_id, text=reader.format_unreadable(att.ref, att.filename, att.caption))
            await gateway.send_text(chat_id, f"Não consegui ler esse arquivo. {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - nunca expõe detalhes internos no chat
            print(f"[bot] erro ao baixar o arquivo: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
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
            print(f"[bot] erro ao responder: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            self.conversation.fail(msg_id)
            await gateway.send_text(chat_id, ERROR_REPLY)
            return
        if self.verbose:
            self._log_trace(result)
        await gateway.send_text(chat_id, result.text)
        for att in result.files:
            try:
                await gateway.send_file(chat_id, att)
            except Exception as exc:  # noqa: BLE001
                print(f"[bot] erro ao reenviar {att.ref}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
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
        fresh = []
        for msg in self.conversation.unfinished():
            if now - datetime.fromisoformat(msg.at) > RECOVERY_MAX_AGE:
                self.conversation.fail(msg.id)
            else:
                fresh.append(msg)
        if not fresh:
            return
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
    print(f"[bot] erro: {context.error!r}", file=sys.stderr, flush=True)


async def notify_outcome(app: Application, outcome) -> None:
    """Curadoria automática terminou: mostra no console e envia o relatório ao usuário.

    Falhas são avisadas uma vez por sequência (o serviço tenta de novo a cada poucos
    minutos; sem isso seriam várias mensagens iguais). Um sucesso reinicia a contagem.
    """
    text = describe_outcome(outcome)
    print(text, flush=True)
    allowed = app.bot_data.get("allowed_user_id")
    if allowed is None:
        return
    if outcome.error:
        if app.bot_data.get("failure_notified"):
            return
        app.bot_data["failure_notified"] = True
    else:
        app.bot_data["failure_notified"] = False
    try:
        await app.bot_data["gateway"].send_text(allowed, text)
    except Exception as exc:  # noqa: BLE001 - falhar ao avisar não pode derrubar o bot
        print(f"[bot] não consegui enviar o relatório: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


async def _safe_recover(handlers: BotHandlers, gateway) -> None:
    try:
        await handlers.recover(gateway)
    except Exception as exc:  # noqa: BLE001
        print(f"[bot] erro ao retomar mensagens pendentes: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


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


async def post_shutdown(app: Application) -> None:
    recovery = app.bot_data.get("recovery")
    if recovery is not None and not recovery.done():
        recovery.cancel()
    stop, watcher = app.bot_data.get("stop"), app.bot_data.get("watcher")
    if stop is None or watcher is None:
        return
    stop.set()
    try:
        await asyncio.wait_for(watcher, timeout=SHUTDOWN_WAIT)  # deixa uma curadoria terminar
    except asyncio.TimeoutError:
        watcher.cancel()


def build_application(token: str, handlers: BotHandlers, service) -> Application:
    # concurrent_updates: sem isso a biblioteca trata uma mensagem por vez, e um arquivo lento
    # seguraria até a gravação e o "digitando..." da mensagem seguinte.
    app = (Application.builder().token(token).concurrent_updates(True)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    app.bot_data["service"] = service
    app.bot_data["handlers"] = handlers
    app.bot_data["allowed_user_id"] = handlers.allowed_user_id
    app.bot_data["gateway"] = handlers.gateway_factory(app.bot)
    app.add_handler(CommandHandler("start", handlers.on_start))
    app.add_handler(CommandHandler("status", handlers.on_status))
    app.add_handler(CommandHandler("curate", handlers.on_curate))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.on_message))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handlers.on_file))
    app.add_handler(MessageHandler(filters.COMMAND, handlers.on_unknown))  # sempre por último
    app.add_error_handler(on_error)
    return app


def configure_logging() -> None:
    """O httpx loga a URL das requisições em INFO, e ela contém o token do bot."""
    logging.basicConfig(level=logging.WARNING)
    for name in ("httpx", "httpcore", "telegram"):
        logging.getLogger(name).setLevel(logging.WARNING)


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
    registry = AttachmentRegistry(MemoryStore(args.memory_dir or config.MEMORY_DIR))
    outbox: list = []  # arquivos que o agente pede para reenviar na resposta atual
    if agent is None:
        try:
            tools = default_read_tools(args.memory_dir) + build_attachment_tools(registry, outbox)
            agent = build_responder(tools=tools)
        except RuntimeError as exc:
            print(f"Erro: {exc}", file=sys.stderr)
            return 1

    configure_logging()
    log = ConversationLog(args.log or config.CONVERSATION_FILE)
    service = build_service(log, args.memory_dir, args.idle_seconds, channel="Telegram")
    handlers = BotHandlers(Conversation(log, service, agent, outbox), config.TELEGRAM_ALLOWED_USER_ID,
                           registry, verbose=args.verbose)
    app = application_factory(config.TELEGRAM_BOT_TOKEN, handlers, service)

    print(f"Bot iniciado | modelo: {config.OPENROUTER_MODEL} | memória: {args.memory_dir or config.MEMORY_DIR}")
    print(f"Histórico: {log.path} | Curador após {args.idle_seconds:.0f}s de inatividade")
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
