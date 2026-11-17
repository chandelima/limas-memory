"""Telegram bot: talks to the chat agent and feeds the Curator.

Usage:
    python src/bot.py
    python src/bot.py --memory-dir D:/tmp/test-memory --log D:/tmp/conversation.json --idle-seconds 30
    python src/bot.py --verbose    # prints the memory queries behind each reply to the console

Only the user in TELEGRAM_ALLOWED_USER_ID is served (and only in a private chat). Without that
env, the bot refuses everyone and prints the ID of whoever wrote, so you can configure it.

Flow of each message:
  1. arrival: the message is written to the history right away (status received/reading) and the
     "typing..." indicator starts, before any processing;
  2. queue: one message is processed at a time, in arrival order ("typing..." stays on while it
     waits its turn); files are downloaded and read; the chat agent queries the memory
     (read-only) and replies;
  3. after CURATOR_IDLE_SECONDS without messages, the Curator processes what is pending and its
     report goes to the log channel (a failure is reported once per streak).
If the bot dies midway, on restart it resumes the messages left unanswered in the last 24 h.
Every day, at BRIEFING_TIME (default 06:00), it sends the morning briefing with today's tasks,
the overdue ones and the ones for the next days (nothing, if there are none).
Commands: /status, /todo and /curate (they do not go to the model).
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
import i18n
from applog import LogShipper, configure_logging, get_logger, parse_channel_id
from attachments import reader
from attachments.registry import AttachmentRegistry
from attachments.tools import build_attachment_tools
from briefing import BriefingScheduler, parse_briefing_time
from chat_agent.agent import build_chat_agent, default_read_tools
from chatflow import Conversation, build_service, describe_outcome
from conversation.log import READING, ConversationLog
from i18n import t
from storage.store import MemoryStore
from tasks import panel
from tasks.model import TaskError
from tasks.store import TaskStore
from tasks.tools import build_task_tools
from telegram_gateway import TELEGRAM_LIMIT, TYPING_INTERVAL, TelegramGateway, split_message, to_plain  # noqa: F401

log = get_logger("bot")

WATCH_INTERVAL = 30  # seconds between inactivity checks
NETWORK_TIMEOUT = 20  # seconds for each call to the Telegram API (the library default is 5)
SHUTDOWN_WAIT = 30  # maximum wait for a running curation on shutdown
RECOVERY_MAX_AGE = timedelta(hours=24)  # unanswered messages older than this are discarded



def commands() -> list[BotCommand]:
    """The menu of commands, in the user's language (see i18n)."""
    return [
        BotCommand("status", t("cmd_status")),
        BotCommand("todo", t("cmd_todo")),
        BotCommand("curate", t("cmd_curate")),
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
        ensure_language=None,
    ):
        self.conversation = conversation
        self.allowed_user_id = allowed_user_id
        self.registry = registry  # without a registry, the bot ignores files
        self.read_document = read_document
        self.verbose = verbose  # print the memory queries behind each reply to the console
        self.gateway_factory = gateway_factory
        self.briefing = briefing  # morning briefing; None = disabled
        self.tasks = tasks  # task panel with buttons; None = disabled
        self._lock = asyncio.Lock()  # one message at a time: the history is sequential
        self.ensure_language = ensure_language  # sets the user's language up from a message (see i18n)
        self._language_task: asyncio.Task | None = None

    def _authorized(self, update: Update) -> bool:
        user, chat = update.effective_user, update.effective_chat
        if user is None or chat is None or chat.type != ChatType.PRIVATE:
            return False
        if self.allowed_user_id is None:
            log.warning("message refused from user id %s; to allow it, set TELEGRAM_ALLOWED_USER_ID=%s",
                        user.id, user.id)
            return False
        if user.id != self.allowed_user_id:
            log.warning("message from unauthorized user id=%s", user.id)
            return False
        return True

    # ---- commands (they do not go to the model) ------------------------------

    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._authorized(update):
            await self.gateway_factory(context.bot).send_text(update.effective_chat.id, t("hello", help=t("help")))

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
        """/todo [today|week|pending] [tag]: task panel with buttons."""
        if not self._authorized(update) or self.tasks is None:
            return
        gateway, chat_id = self.gateway_factory(context.bot), update.effective_chat.id
        args = [a.lower() for a in (context.args or [])]
        scope = "today"
        if args and args[0] in panel.PANEL_SCOPES:
            scope = args.pop(0)
        await self._send_panel(gateway, chat_id, scope, args[0].lstrip("#") if args else "")

    async def _send_panel(self, gateway, chat_id: int, scope: str, tag: str = "") -> None:
        try:
            text, rows = await asyncio.to_thread(panel.build_panel, self.tasks, scope, tag)
        except Exception:  # noqa: BLE001 - the panel is an extra: it never breaks the reply
            log.exception("error building the task panel")
            return
        if rows:
            await gateway.send_buttons(chat_id, text, rows)
        else:
            await gateway.send_text(chat_id, text)

    async def on_task_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """A tap in the task panel: open a task's card, check/uncheck it from the card, or go back."""
        query = update.callback_query
        if query is None or not self._authorized(update) or self.tasks is None:
            return
        gateway = self.gateway_factory(context.bot)
        parsed = panel.decode(query.data or "")
        if parsed is None:
            await gateway.answer_callback(query.id)
            return
        notice = ""
        try:
            if parsed.kind == panel.TOGGLE:
                text, done = await asyncio.to_thread(panel.toggle, self.tasks, parsed.id, parsed.day)
                notice = t("task_completed" if done else "task_reopened", text=text)
            if parsed.kind == panel.BACK:
                new_text, rows = await asyncio.to_thread(panel.build_panel, self.tasks, parsed.scope, parsed.tag)
            else:  # open, or toggle (which stays on the card, now showing the new state)
                new_text, rows = await asyncio.to_thread(
                    panel.build_detail, self.tasks, parsed.id, parsed.day, parsed.scope, parsed.tag)
        except TaskError as exc:
            await gateway.answer_callback(query.id, t("task_failed", error=exc)[:190])
            return
        except Exception:  # noqa: BLE001
            log.exception("error handling a task panel tap (%s)", parsed.kind)
            await gateway.answer_callback(query.id, t("task_update_error"))
            return
        await gateway.answer_callback(query.id, notice[:190])
        try:
            await gateway.edit_buttons(update.effective_chat.id, query.message.message_id, new_text, rows)
        except Exception:  # noqa: BLE001 - e.g. "message is not modified"
            log.warning("could not update the task panel", exc_info=True)

    async def on_added_to_chat(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Bot added to a channel or group: shows the ID in the console, to use as LOG_CHANNEL_ID
        (as happens with the ID of a new user). It only informs; it says nothing in the chat."""
        change = update.my_chat_member
        chat = update.effective_chat
        if change is None or chat is None or chat.type == ChatType.PRIVATE:
            return
        if change.new_chat_member.status not in ("administrator", "member"):
            return  # removed or banned
        kind = "channel" if chat.type == ChatType.CHANNEL else "group"
        log.info("bot added to the %s '%s'; to send the logs there, set LOG_CHANNEL_ID=%s%s",
                 kind, chat.title or "?", chat.id,
                 "" if change.new_chat_member.status == "administrator" else " (and make the bot an administrator)")

    async def on_unknown(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._authorized(update):
            await self.gateway_factory(context.bot).send_text(
                update.effective_chat.id, t("unknown_command", help=t("help")))

    # ---- messages: record on arrival, "typing" right away, process in turn ---

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        text = (update.message.text or "").strip()
        if not text:
            return
        gateway, chat_id = self.gateway_factory(context.bot), update.effective_chat.id
        msg = self.conversation.ingest(text)  # 1. record before anything else
        self._setup_language(text, gateway)
        async with gateway.typing(chat_id):  # 2. "typing..." right away, even while waiting its turn
            async with self._lock:  # 3. one at a time, in arrival order
                await self._answer(gateway, chat_id, msg.id)

    async def on_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Document or photo: record on arrival, say it is being read and process in turn."""
        if not self._authorized(update) or self.registry is None:
            return
        msg = update.message
        gateway, chat_id = self.gateway_factory(context.bot), update.effective_chat.id
        if msg.document:
            kind, item = "document", msg.document
            filename = item.file_name or "file"
            mime = reader.guess_mime(filename, item.mime_type or "")
        elif msg.photo:
            kind, item = "photo", msg.photo[-1]  # the highest resolution
            filename, mime = f"photo-{item.file_unique_id}.jpg", "image/jpeg"
        else:
            return
        if not reader.is_supported(mime):
            await gateway.send_text(chat_id, t("file_unsupported"))
            return
        if (item.file_size or 0) > reader.MAX_FILE_BYTES:
            await gateway.send_text(chat_id, t("file_too_big"))
            return

        att = self.registry.add(  # record the attachment now, so it can be downloaded again if the bot dies
            file_id=item.file_id, file_unique_id=item.file_unique_id, kind=kind, chat_id=chat_id,
            message_id=msg.message_id, filename=filename, mime=mime, size=item.file_size or 0,
            caption=msg.caption or "",
        )
        stored = self.conversation.ingest(
            reader.format_placeholder(att.ref, filename, att.caption), attachments=[att.id], status=READING)
        async with gateway.typing(chat_id):
            await gateway.send_text(chat_id, t("file_notice"))
            async with self._lock:
                await self._read_and_answer(gateway, chat_id, stored.id, att)

    # ---- language of the fixed messages --------------------------------------

    def _setup_language(self, text: str, gateway) -> None:
        """First messages: have the model detect the user's language and translate the fixed
        messages (once; in the background, so the reply is not delayed)."""
        if self.ensure_language is None or i18n.current().configured:
            return
        if self._language_task is not None and not self._language_task.done():
            return
        self._language_task = asyncio.create_task(self._run_language_setup(text, gateway))

    async def _run_language_setup(self, text: str, gateway) -> None:
        try:
            created = await asyncio.to_thread(self.ensure_language, text)
            if created and self.allowed_user_id is not None:
                await gateway.register_commands(commands(), self.allowed_user_id)  # menu in the new language
        except Exception:  # noqa: BLE001 - the language is a nicety: it never breaks the flow
            log.warning("language setup failed", exc_info=True)

    # ---- core (used live and in recovery) ------------------------------------

    async def _read_and_answer(self, gateway, chat_id: int, msg_id: int, att) -> None:
        """Download and read the file; with the content ready, move on to the reply."""
        try:
            data = await gateway.download(att.file_id)
            text = await asyncio.to_thread(self.read_document, data, att.filename, att.mime)
        except reader.ReaderError as exc:
            log.warning("file %s could not be read: %s", att.ref, exc)
            self.conversation.fail(msg_id, text=reader.format_unreadable(att.ref, att.filename, att.caption))
            await gateway.send_text(chat_id, t("file_unreadable", reason=exc))
            return
        except Exception as exc:  # noqa: BLE001 - never exposes internal details in the chat
            log.exception("error downloading file %s", att.ref)
            self.conversation.fail(msg_id, text=reader.format_unreadable(att.ref, att.filename, att.caption))
            await gateway.send_text(chat_id, t("file_download_failed"))
            return
        self.conversation.set_content(msg_id, reader.format_attachment_message(att.ref, att.filename, att.caption, text))
        await self._answer(gateway, chat_id, msg_id)

    async def _answer(self, gateway, chat_id: int, msg_id: int) -> None:
        """Pass the message to the chat agent, reply and resend the requested files."""
        try:
            result = await asyncio.to_thread(self.conversation.respond_to, msg_id)
        except Exception as exc:  # noqa: BLE001 - never exposes internal details in the chat
            log.exception("error replying to message %s", msg_id)
            self.conversation.fail(msg_id)
            await gateway.send_text(chat_id, t("error_reply"))
            return
        if self.verbose:
            self._log_trace(result)
        await gateway.send_text(chat_id, result.text)
        if self.tasks is not None:  # the agent listed tasks: also show the panel with buttons
            listing = next((e for e in reversed(result.trace)
                            if e.name == "list_tasks" and not e.result.startswith("ERROR")), None)
            if listing is not None:
                scope = str(listing.args.get("scope") or "today").lower()
                if scope in panel.PANEL_SCOPES:
                    await self._send_panel(gateway, chat_id, scope, str(listing.args.get("tag") or ""))
        for att in result.files:
            try:
                await gateway.send_file(chat_id, att)
            except Exception as exc:  # noqa: BLE001
                log.warning("error resending %s", att.ref, exc_info=True)
                await gateway.send_text(chat_id, t("file_resend_failed", filename=att.filename))

    @staticmethod
    def _log_trace(result) -> None:
        """--verbose mode: shows on the console what the agent consulted to reply."""
        for event in result.trace:
            args = ", ".join(f"{k}={v!r}" for k, v in event.args.items())
            print(f"  [memory] {event.name}({args})", flush=True)
        for att in result.files:
            print(f"  [file] resending {att.ref} ({att.filename})", flush=True)

    # ---- recovery after a crash ----------------------------------------------

    async def recover(self, gateway, now: datetime | None = None) -> None:
        """On start, resume the messages left unanswered (the bot died midway).
        The ones older than RECOVERY_MAX_AGE are discarded (they become `failed`)."""
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
            log.warning("%d message(s) unanswered for over 24 h were discarded on restart", stale)
        if not fresh:
            return
        log.warning("restart with %d unanswered message(s); resuming", len(fresh))
        await gateway.send_text(chat_id, t("recovery_notice"))
        for msg in fresh:
            async with gateway.typing(chat_id):
                async with self._lock:
                    await self._resume(gateway, chat_id, msg)

    async def _resume(self, gateway, chat_id: int, msg) -> None:
        if msg.status != READING:  # received/ready: the content is already in the history
            await self._answer(gateway, chat_id, msg.id)
            return
        att = self.registry.get(msg.attachments[0]) if self.registry and msg.attachments else None
        if att is None:
            self.conversation.fail(msg.id)
            await gateway.send_text(chat_id, t("file_resume_failed"))
            return
        await self._read_and_answer(gateway, chat_id, msg.id, att)


# ---- application -----------------------------------------------------------


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("bot error", exc_info=context.error if isinstance(context.error, BaseException) else None)


async def notify_outcome(app: Application, outcome) -> None:
    """The automatic curation finished: show it on the console and send the report to the log
    channel (or, with no channel configured, to the user). A manual /curate replies to the user.

    Failures are reported once per streak (the service retries every few minutes;
    otherwise there would be several identical messages). A success resets the count.
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
    except Exception as exc:  # noqa: BLE001 - failing to notify must not take the bot down
        log.warning("could not send the curation report (%s)",
                    "log channel" if log_channel is not None else "user", exc_info=exc)


async def _safe_recover(handlers: BotHandlers, gateway) -> None:
    try:
        await handlers.recover(gateway)
    except Exception as exc:  # noqa: BLE001
        log.error("error resuming pending messages", exc_info=exc)


async def post_init(app: Application) -> None:
    """Register the commands, start the Curator loop and resume unanswered messages."""
    gateway = app.bot_data["gateway"]
    await gateway.register_commands(commands(), app.bot_data.get("allowed_user_id"))
    stop = asyncio.Event()
    app.bot_data["stop"] = stop
    app.bot_data["watcher"] = asyncio.create_task(
        app.bot_data["service"].watch(stop, WATCH_INTERVAL, on_outcome=lambda outcome: notify_outcome(app, outcome))
    )
    app.bot_data["recovery"] = asyncio.create_task(_safe_recover(app.bot_data["handlers"], gateway))
    log.info("bot started (model %s)", config.OPENROUTER_MODEL, extra={"channel": True})
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
    log.info("bot shutting down", extra={"channel": True})  # enters the queue before the final send
    stop.set()
    briefing = app.bot_data.get("briefing")
    if briefing is not None:
        try:
            await asyncio.wait_for(briefing, timeout=5)  # with `stop` set, it ends right away
        except asyncio.TimeoutError:
            briefing.cancel()
    try:
        await asyncio.wait_for(watcher, timeout=SHUTDOWN_WAIT)  # lets a curation finish
    except asyncio.TimeoutError:
        watcher.cancel()
    shipper = app.bot_data.get("log_shipper")
    if shipper is not None:
        try:
            await asyncio.wait_for(shipper, timeout=10)  # with `stop` set, it sends what is left and ends
        except asyncio.TimeoutError:
            shipper.cancel()


def build_application(token: str, handlers: BotHandlers, service) -> Application:
    # concurrent_updates: without it the library handles one message at a time, and a slow
    # file would hold up even the recording and the "typing..." of the next message.
    # Timeouts above the library's 5 s default: on a slow or unstable link (a home server, a VPN)
    # a few seconds of latency should not turn into "TimedOut" for every message and log batch.
    app = (Application.builder().token(token).concurrent_updates(True)
           .connect_timeout(NETWORK_TIMEOUT).read_timeout(NETWORK_TIMEOUT).write_timeout(NETWORK_TIMEOUT)
           .pool_timeout(NETWORK_TIMEOUT)
           .get_updates_connect_timeout(NETWORK_TIMEOUT).get_updates_read_timeout(NETWORK_TIMEOUT + 10)
           .get_updates_write_timeout(NETWORK_TIMEOUT).get_updates_pool_timeout(NETWORK_TIMEOUT)
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
    app.add_handler(CallbackQueryHandler(handlers.on_task_action, pattern=r"^t[ktb]:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.on_message))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handlers.on_file))
    app.add_handler(MessageHandler(filters.COMMAND, handlers.on_unknown))  # always last
    app.add_error_handler(on_error)
    return app


def main(argv=None, agent=None, application_factory=build_application) -> int:
    ap = argparse.ArgumentParser(description="Telegram bot with memory.")
    ap.add_argument("--memory-dir", type=Path, default=None,
                    help="memory folder (default: MEMORY_DIR / ./memory)")
    ap.add_argument("--log", type=Path, default=None,
                    help="history file (default: CONVERSATION_FILE / ./data/conversation.json)")
    ap.add_argument("--idle-seconds", type=float, default=config.CURATOR_IDLE_SECONDS,
                    help="inactivity before the Curator processes (default: CURATOR_IDLE_SECONDS)")
    ap.add_argument("--verbose", action="store_true",
                    help="print the memory queries behind each reply to the console")
    args = ap.parse_args(argv)

    if not config.TELEGRAM_BOT_TOKEN:
        print("Error: set TELEGRAM_BOT_TOKEN in the environment or in .env (token from @BotFather)", file=sys.stderr)
        return 1
    try:
        briefing_time = parse_briefing_time(config.BRIEFING_TIME)
    except ValueError as exc:
        print(f"Error: BRIEFING_TIME: {exc}", file=sys.stderr)
        return 1
    try:
        log_channel = parse_channel_id(config.LOG_CHANNEL_ID)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    i18n.use(i18n.Messages(config.LANGUAGE_FILE, auto_fill=True))  # the user's language, if it is already set up
    memory_store = MemoryStore(args.memory_dir or config.MEMORY_DIR)
    registry = AttachmentRegistry(memory_store)
    tasks = TaskStore(memory_store)  # task list (todos/todo.md), written by code
    outbox: list = []  # files the agent asks to resend in the current reply
    if agent is None:
        try:
            tools = (default_read_tools(args.memory_dir) + build_attachment_tools(registry, outbox)
                     + build_task_tools(tasks))
            agent = build_chat_agent(tools=tools)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    log_handler = configure_logging(config.LOG_LEVEL, config.LOG_CHANNEL_LEVEL, channel=log_channel is not None)
    history = ConversationLog(args.log or config.CONVERSATION_FILE)
    service = build_service(history, args.memory_dir, args.idle_seconds, channel="Telegram")
    briefing = (BriefingScheduler(tasks, briefing_time.at, config.BRIEFING_STATE_FILE, log=history, tz=briefing_time.tz)
                if briefing_time else None)
    handlers = BotHandlers(Conversation(history, service, agent, outbox), config.TELEGRAM_ALLOWED_USER_ID,
                           registry, verbose=args.verbose, briefing=briefing, tasks=tasks,
                           ensure_language=lambda text: i18n.current().ensure(text))
    app = application_factory(config.TELEGRAM_BOT_TOKEN, handlers, service)
    app.bot_data["log_handler"] = log_handler  # sending the logs to the channel starts in post_init
    app.bot_data["log_channel_id"] = log_channel

    print(f"Bot started | model: {config.OPENROUTER_MODEL} | memory: {args.memory_dir or config.MEMORY_DIR}")
    print(f"History: {history.path} | Curator after {args.idle_seconds:.0f}s of inactivity")
    print(f"Log channel: {log_channel} ({config.LOG_CHANNEL_LEVEL.upper()}+)" if log_channel is not None
          else "Log channel: disabled")
    print(f"Morning briefing: {briefing_time.describe()}" if briefing_time else "Morning briefing: disabled")
    if config.TELEGRAM_ALLOWED_USER_ID is None:
        print("WARNING: TELEGRAM_ALLOWED_USER_ID is not set. The bot will refuse everyone and print "
              "the ID of whoever writes.")
    print("Ctrl+C to stop.\n", flush=True)
    app.run_polling()
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
