"""Everything the bot does directly on the Telegram API, in a single class.

The handlers (bot.py) deal with authorization, queueing and conversation; this class only knows
how to send text, resend files, download files, show "typing..." and publish commands.
That keeps library details (ReplyParameters, ChatAction, size limits, plain text) here,
and the handlers' tests use a fake gateway.
"""

import asyncio
import re
from contextlib import asynccontextmanager, suppress

from telegram import BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters
from telegram.constants import ChatAction, ParseMode

from applog import get_logger
from i18n import t

log = get_logger("telegram")

TELEGRAM_LIMIT = 4096  # maximum size of a Telegram message
TYPING_INTERVAL = 4  # the "typing..." indicator lasts ~5s


def to_plain(text: str) -> str:
    """The bot sends plain text (no parse_mode): strips the Markdown markup that models
    sometimes use (**bold**, __bold__, `code`, headings with #)."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"`+([^`\n]+)`+", r"\1", text)
    return re.sub(r"^#{1,6}[ \t]+", "", text, flags=re.MULTILINE)


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split into chunks of up to `limit`, preferring to break at a paragraph, line or space."""
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


class TelegramGateway:
    def __init__(self, bot):
        self.bot = bot

    async def send_text(self, chat_id: int | str, text: str, *, raw: bool = False) -> None:
        """Send plain text, split at the Telegram limit. `raw=True` leaves the text untouched
        (used for logs, where `**` or `#` may be part of the content)."""
        for chunk in split_message(text if raw else to_plain(text)) or [t("no_reply")]:
            await self.bot.send_message(chat_id=chat_id, text=chunk)

    async def send_placeholder(self, chat_id: int, text: str, *, html: bool = False) -> int:
        """A message that will be edited later (see progress.py). Return its message_id.
        `html=True`: the text is Telegram HTML (e.g. <i>italics</i>); the caller escapes it."""
        message = await self.bot.send_message(chat_id=chat_id, text=text, **({"parse_mode": ParseMode.HTML} if html else {}))
        return message.message_id

    async def edit_text(self, chat_id: int, message_id: int, text: str, *, html: bool = False) -> None:
        extra = {"parse_mode": ParseMode.HTML} if html else {}
        await self.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, **extra)

    async def write_answer(self, chat_id: int, message_id: int, html_text: str, plain_text: str) -> None:
        """Write the final answer into the progress message: `html_text` has the thinking in italics on
        top; if Telegram refuses it, `plain_text` replaces the message (or goes as a new one)."""
        try:
            await self.edit_text(chat_id, message_id, html_text, html=True)
        except Exception as exc:  # noqa: BLE001
            if "not modified" not in str(exc).lower():
                log.warning("could not write the answer with formatting; falling back to plain text", exc_info=exc)
                await self.replace_text(chat_id, message_id, plain_text, raw=True)

    async def replace_text(self, chat_id: int, message_id: int, text: str, *, raw: bool = False) -> None:
        """Turn an existing message into `text` (plain, split at the Telegram limit): the first chunk
        edits the message and the rest, if any, goes as new messages. If the edit fails, the text is
        sent as a new message instead, so the answer is never lost."""
        chunks = split_message(text if raw else to_plain(text)) or [t("no_reply")]
        try:
            await self.edit_text(chat_id, message_id, chunks[0])
        except Exception as exc:  # noqa: BLE001
            if "not modified" not in str(exc).lower():  # the same text is already there: nothing to do
                log.warning("could not edit the message into the answer; sending it as a new one", exc_info=exc)
                await self.bot.send_message(chat_id=chat_id, text=chunks[0])
        for chunk in chunks[1:]:
            await self.bot.send_message(chat_id=chat_id, text=chunk)

    @staticmethod
    def _keyboard(rows) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data) for label, data in row]
                                     for row in rows])

    async def send_buttons(self, chat_id: int, text: str, rows: list) -> int:
        """Message with buttons (list of rows of (label, callback_data)). Return the message_id."""
        message = await self.bot.send_message(chat_id=chat_id, text=text, reply_markup=self._keyboard(rows))
        return message.message_id

    async def edit_buttons(self, chat_id: int, message_id: int, text: str, rows: list) -> None:
        await self.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text,
                                         reply_markup=self._keyboard(rows) if rows else None)

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        """Answer the tap (the button's "loading" goes away; the text becomes a quick notice)."""
        await self.bot.answer_callback_query(callback_id, text=text or None)

    async def send_file(self, chat_id: int, attachment) -> None:
        """Resend a file by its file_id, quoting the original message when it exists."""
        reply_to = ReplyParameters(message_id=attachment.message_id, allow_sending_without_reply=True)
        if attachment.kind == "photo":
            await self.bot.send_photo(chat_id=chat_id, photo=attachment.file_id, reply_parameters=reply_to)
        else:
            await self.bot.send_document(chat_id=chat_id, document=attachment.file_id, reply_parameters=reply_to)

    async def download(self, file_id: str) -> bytes:
        tg_file = await self.bot.get_file(file_id)
        return bytes(await tg_file.download_as_bytearray())

    @asynccontextmanager
    async def typing(self, chat_id: int):
        """Keep "typing..." on while the block runs (including while waiting for its turn)."""
        task = asyncio.create_task(self._typing_loop(chat_id))
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _typing_loop(self, chat_id: int) -> None:
        while True:
            try:
                await self.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception:  # noqa: BLE001 - the indicator is a luxury: it never breaks the flow
                pass
            await asyncio.sleep(TYPING_INTERVAL)

    async def register_commands(self, commands: list, allowed_user_id: int | None) -> None:
        """Publish the command list (Telegram's "/" menu). With an authorized user, only they
        see the menu; without one, the default scope applies. A failure here does not stop the bot."""
        scope = BotCommandScopeChat(chat_id=allowed_user_id) if allowed_user_id else None
        try:
            await self.bot.set_my_commands(commands, scope=scope)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not register the commands on Telegram", exc_info=exc)
