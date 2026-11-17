"""Bot do Telegram: conversa com o agente de resposta e alimenta o Curator.

Uso:
    python src/bot.py
    python src/bot.py --memory-dir D:/tmp/memoria-teste --log D:/tmp/conversa.json --idle-seconds 30
    python src/bot.py --verbose    # mostra no console as consultas à memória de cada resposta

Só o usuário em TELEGRAM_ALLOWED_USER_ID é atendido (e só em conversa privada). Sem essa
env, o bot recusa todo mundo e imprime o ID de quem escreveu, para você configurá-la.
Cada mensagem vai para o histórico; o agente de resposta lê a memória (somente leitura);
depois de CURATOR_IDLE_SECONDS sem mensagens, o Curator processa o que ficou pendente e o
relatório é enviado a você (uma falha é avisada uma vez por sequência).
Comandos: /status e /curate (não vão para o modelo).
"""

import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path

from telegram import BotCommand, BotCommandScopeChat, ReplyParameters, Update
from telegram.constants import ChatAction, ChatType
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import config
from attachments import reader
from attachments.registry import AttachmentRegistry
from attachments.tools import build_attachment_tools
from chatflow import Conversation, build_service, describe_outcome
from conversation.log import ConversationLog
from responder.agent import build_responder, default_read_tools

TELEGRAM_LIMIT = 4096  # tamanho máximo de uma mensagem do Telegram
WATCH_INTERVAL = 30  # segundos entre verificações de inatividade
TYPING_INTERVAL = 4  # o indicador "digitando..." dura ~5s
SHUTDOWN_WAIT = 30  # espera máxima por uma curadoria em andamento ao encerrar

HELP = "Pode conversar comigo normalmente.\n/status mostra o estado da memória.\n/curate processa agora o que ficou pendente."


def to_plain(text: str) -> str:
    """O bot envia texto simples (sem parse_mode): tira a marcação Markdown que os
    modelos às vezes usam (**negrito**, __negrito__, `código`, títulos com #)."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"`+([^`\n]+)`+", r"\1", text)
    return re.sub(r"^#{1,6}[ \t]+", "", text, flags=re.MULTILINE)


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Divide em pedaços de até `limit`, preferindo quebrar em parágrafo, linha ou espaço."""
    text = text.strip()
    chunks = []
    while len(text) > limit:
        cut = limit
        for sep in ("\n\n", "\n", " "):
            idx = text.rfind(sep, 0, limit)
            if idx > 0:
                cut = idx
                break
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


class BotHandlers:
    def __init__(
        self,
        conversation: Conversation,
        allowed_user_id: int | None,
        registry: AttachmentRegistry | None = None,
        read_document=reader.read_document,
        verbose: bool = False,
    ):
        self.conversation = conversation
        self.allowed_user_id = allowed_user_id
        self.registry = registry  # sem registro, o bot ignora arquivos
        self.read_document = read_document
        self.verbose = verbose  # imprime no console as consultas à memória de cada resposta
        self._lock = asyncio.Lock()  # uma conversa por vez: o histórico é sequencial

    def _authorized(self, update: Update) -> bool:
        user, chat = update.effective_user, update.effective_chat
        if user is None or chat is None or chat.type != ChatType.PRIVATE:
            return False
        if self.allowed_user_id is None:
            print(f"[bot] mensagem recusada do usuário id {user.id}. "
                  f"Para liberar, defina TELEGRAM_ALLOWED_USER_ID={user.id}", flush=True)
            return False
        return user.id == self.allowed_user_id

    async def _reply(self, update: Update, text: str) -> None:
        for chunk in split_message(to_plain(text)) or ["(sem resposta)"]:
            await update.message.reply_text(chunk)

    async def _keep_typing(self, update: Update) -> None:
        while True:
            await update.effective_chat.send_action(ChatAction.TYPING)
            await asyncio.sleep(TYPING_INTERVAL)

    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._authorized(update):
            await update.message.reply_text(f"Olá! {HELP}")

    async def _respond(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, attachments: list[str] | None = None
    ) -> None:
        """Passa o texto ao agente de resposta, responde e envia os arquivos que ele pediu."""
        async with self._lock:
            typing = asyncio.create_task(self._keep_typing(update))
            try:
                result = await asyncio.to_thread(self.conversation.respond, text, attachments)
            except Exception as exc:  # noqa: BLE001 - nunca expõe detalhes internos no chat
                print(f"[bot] erro ao responder: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                await update.message.reply_text("Tive um problema para responder agora. Tente de novo em instantes.")
                return
            finally:
                typing.cancel()
        if self.verbose:
            self._log_trace(result)
        await self._reply(update, result.text)
        await self._send_files(update, context, result.files)

    @staticmethod
    def _log_trace(result) -> None:
        """Modo --verbose: mostra no console o que o agente consultou para responder."""
        for event in result.trace:
            args = ", ".join(f"{k}={v!r}" for k, v in event.args.items())
            print(f"  [memória] {event.name}({args})", flush=True)
        for att in result.files:
            print(f"  [arquivo] reenviando {att.ref} ({att.filename})", flush=True)

    async def _send_files(self, update: Update, context: ContextTypes.DEFAULT_TYPE, files: list) -> None:
        """Reenvia os arquivos pelo file_id, citando a mensagem original quando ela existir."""
        for att in files:
            reply_to = ReplyParameters(message_id=att.message_id, allow_sending_without_reply=True)
            send = context.bot.send_photo if att.kind == "photo" else context.bot.send_document
            kwargs = {"photo": att.file_id} if att.kind == "photo" else {"document": att.file_id}
            try:
                await send(chat_id=update.effective_chat.id, reply_parameters=reply_to, **kwargs)
            except Exception as exc:  # noqa: BLE001
                print(f"[bot] erro ao reenviar {att.ref}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                await update.message.reply_text(f"Não consegui reenviar o arquivo {att.filename}.")

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        text = (update.message.text or "").strip()
        if text:
            await self._respond(update, context, text)

    async def on_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Documento ou foto: baixa, lê o conteúdo, registra o anexo e passa ao agente."""
        if not self._authorized(update) or self.registry is None:
            return
        msg = update.message
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
            await msg.reply_text("Por enquanto só leio PDF e imagens (JPG, PNG, WebP ou GIF).")
            return
        if (item.file_size or 0) > reader.MAX_FILE_BYTES:
            await msg.reply_text("Esse arquivo passa do limite de 20 MB.")
            return

        typing = asyncio.create_task(self._keep_typing(update))
        try:
            tg_file = await context.bot.get_file(item.file_id)
            data = bytes(await tg_file.download_as_bytearray())
            text = await asyncio.to_thread(self.read_document, data, filename, mime)
        except reader.ReaderError as exc:
            await msg.reply_text(f"Não consegui ler esse arquivo. {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            print(f"[bot] erro ao baixar o arquivo: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            await msg.reply_text("Não consegui baixar o arquivo agora. Tente de novo em instantes.")
            return
        finally:
            typing.cancel()

        att = self.registry.add(
            file_id=item.file_id, file_unique_id=item.file_unique_id, kind=kind,
            chat_id=update.effective_chat.id, message_id=msg.message_id, filename=filename,
            mime=mime, size=item.file_size or len(data), caption=msg.caption or "",
        )
        message = reader.format_attachment_message(att.ref, filename, msg.caption or "", text)
        await self._respond(update, context, message, [att.id])

    async def on_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._authorized(update):
            await self._reply(update, self.conversation.status())

    async def on_curate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await update.effective_chat.send_action(ChatAction.TYPING)
        await self._reply(update, await asyncio.to_thread(self.conversation.curate_now))

    async def on_unknown(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._authorized(update):
            await update.message.reply_text(f"Comando desconhecido. {HELP}")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print(f"[bot] erro: {context.error!r}", file=sys.stderr, flush=True)


COMMANDS = [
    BotCommand("status", "Mostra o estado da memória e das mensagens pendentes"),
    BotCommand("curate", "Processa agora as mensagens pendentes na memória"),
]


async def register_commands(bot, allowed_user_id: int | None) -> None:
    """Publica a lista de comandos (menu "/" do Telegram). Com usuário autorizado, só
    ele vê o menu; sem ele, vale o escopo padrão. Falha aqui não impede o bot de rodar."""
    scope = BotCommandScopeChat(chat_id=allowed_user_id) if allowed_user_id else None
    try:
        await bot.set_my_commands(COMMANDS, scope=scope)
    except Exception as exc:  # noqa: BLE001
        print(f"[bot] não consegui registrar os comandos no Telegram: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)


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
        for chunk in split_message(to_plain(text)):
            await app.bot.send_message(chat_id=allowed, text=chunk)
    except Exception as exc:  # noqa: BLE001 - falhar ao avisar não pode derrubar o bot
        print(f"[bot] não consegui enviar o relatório: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


async def post_init(app: Application) -> None:
    """Registra os comandos e liga o laço que dispara o Curator por inatividade."""
    await register_commands(app.bot, app.bot_data.get("allowed_user_id"))
    stop = asyncio.Event()
    service = app.bot_data["service"]
    app.bot_data["stop"] = stop
    app.bot_data["watcher"] = asyncio.create_task(
        service.watch(stop, WATCH_INTERVAL, on_outcome=lambda outcome: notify_outcome(app, outcome))
    )


async def post_shutdown(app: Application) -> None:
    stop, watcher = app.bot_data.get("stop"), app.bot_data.get("watcher")
    if stop is None or watcher is None:
        return
    stop.set()
    try:
        await asyncio.wait_for(watcher, timeout=SHUTDOWN_WAIT)  # deixa uma curadoria terminar
    except asyncio.TimeoutError:
        watcher.cancel()


def build_application(token: str, handlers: BotHandlers, service) -> Application:
    app = Application.builder().token(token).post_init(post_init).post_shutdown(post_shutdown).build()
    app.bot_data["service"] = service
    app.bot_data["allowed_user_id"] = handlers.allowed_user_id
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
    ap.add_argument("--attachments", type=Path, default=None,
                    help="tabela de anexos (padrão: ATTACHMENTS_FILE / ./data/anexos.json)")
    ap.add_argument("--verbose", action="store_true",
                    help="imprime no console as consultas à memória de cada resposta")
    args = ap.parse_args(argv)

    if not config.TELEGRAM_BOT_TOKEN:
        print("Erro: defina TELEGRAM_BOT_TOKEN no ambiente ou no .env (token do @BotFather)", file=sys.stderr)
        return 1
    registry = AttachmentRegistry(args.attachments or config.ATTACHMENTS_FILE)
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
