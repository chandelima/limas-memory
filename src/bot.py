"""Bot do Telegram: conversa com o agente de resposta e alimenta o Curator.

Uso:
    python src/bot.py
    python src/bot.py --memory-dir D:/tmp/memoria-teste --log D:/tmp/conversa.json --idle-seconds 30

Só o usuário em TELEGRAM_ALLOWED_USER_ID é atendido (e só em conversa privada). Sem essa
env, o bot recusa todo mundo e imprime o ID de quem escreveu, para você configurá-la.
Cada mensagem vai para o histórico; o agente de resposta lê a memória (somente leitura);
depois de CURATOR_IDLE_SECONDS sem mensagens, o Curator processa o que ficou pendente.
Comandos: /status e /curate (não vão para o modelo).
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction, ChatType
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import config
from chatflow import Conversation, build_service, describe_outcome
from conversation.log import ConversationLog
from responder.agent import build_responder, default_read_tools

TELEGRAM_LIMIT = 4096  # tamanho máximo de uma mensagem do Telegram
WATCH_INTERVAL = 30  # segundos entre verificações de inatividade
TYPING_INTERVAL = 4  # o indicador "digitando..." dura ~5s
SHUTDOWN_WAIT = 30  # espera máxima por uma curadoria em andamento ao encerrar

HELP = "Pode conversar comigo normalmente.\n/status mostra o estado da memória.\n/curate processa agora o que ficou pendente."


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
    def __init__(self, conversation: Conversation, allowed_user_id: int | None):
        self.conversation = conversation
        self.allowed_user_id = allowed_user_id
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
        for chunk in split_message(text) or ["(sem resposta)"]:
            await update.message.reply_text(chunk)

    async def _keep_typing(self, update: Update) -> None:
        while True:
            await update.effective_chat.send_action(ChatAction.TYPING)
            await asyncio.sleep(TYPING_INTERVAL)

    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._authorized(update):
            await update.message.reply_text(f"Olá! {HELP}")

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        text = (update.message.text or "").strip()
        if not text:
            return
        async with self._lock:
            typing = asyncio.create_task(self._keep_typing(update))
            try:
                result = await asyncio.to_thread(self.conversation.respond, text)
            except Exception as exc:  # noqa: BLE001 - nunca expõe detalhes internos no chat
                print(f"[bot] erro ao responder: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                await update.message.reply_text("Tive um problema para responder agora. Tente de novo em instantes.")
                return
            finally:
                typing.cancel()
        await self._reply(update, result.text)

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


async def post_init(app: Application) -> None:
    """Liga o laço que dispara o Curator por inatividade, dentro do loop do bot."""
    stop = asyncio.Event()
    service = app.bot_data["service"]
    app.bot_data["stop"] = stop
    app.bot_data["watcher"] = asyncio.create_task(
        service.watch(stop, WATCH_INTERVAL, on_outcome=lambda o: print(describe_outcome(o), flush=True))
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
    app.add_handler(CommandHandler("start", handlers.on_start))
    app.add_handler(CommandHandler("status", handlers.on_status))
    app.add_handler(CommandHandler("curate", handlers.on_curate))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.on_message))
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
    args = ap.parse_args(argv)

    if not config.TELEGRAM_BOT_TOKEN:
        print("Erro: defina TELEGRAM_BOT_TOKEN no ambiente ou no .env (token do @BotFather)", file=sys.stderr)
        return 1
    if agent is None:
        try:
            agent = build_responder(tools=default_read_tools(args.memory_dir))
        except RuntimeError as exc:
            print(f"Erro: {exc}", file=sys.stderr)
            return 1

    configure_logging()
    log = ConversationLog(args.log or config.CONVERSATION_FILE)
    service = build_service(log, args.memory_dir, args.idle_seconds, channel="Telegram")
    handlers = BotHandlers(Conversation(log, service, agent), config.TELEGRAM_ALLOWED_USER_ID)
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
