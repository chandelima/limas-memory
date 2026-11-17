"""Fixed user-facing messages, in the user's language.

The code only knows the English messages (CATALOG). The user's language lives in a JSON file in
the data folder (LANGUAGE_FILE, default data/language.json):

    {"language": "Portuguese (Brazil)", "messages": {"hello": "Olá! {help}", ...}}

When the file does not exist yet, `Messages.ensure(sample)` asks the model which language the
user writes in and, if it is not English, asks it to translate every fixed message, returning a
JSON object. The result is validated (same keys, same {placeholders}) and saved; anything missing
or invalid falls back to English. To change the language, delete the file: it is rebuilt from the
next message. Logs and technical output stay in English.

When a new version of the app adds messages that the file does not have yet, they are translated
on demand: the English text is used right away and the missing keys are translated in the
background (and saved), so the next time they appear in the user's language.
"""

import json
import re
import threading
import time
from pathlib import Path

from applog import get_logger

log = get_logger("i18n")

RETRY_AFTER_FAILURE = 300  # seconds before trying again after a failed setup

CATALOG: dict[str, str] = {
    # ---- bot: commands, notices, errors ----
    "help": "You can just talk to me.\n/status shows the memory state.\n/curate processes what is pending now.\n/todo shows the tasks with buttons.",
    "hello": "Hello! {help}",
    "unknown_command": "Unknown command. {help}",
    "file_notice": "Got the file, reading...",
    "recovery_notice": "I'm back after a restart and will answer the messages that were left pending.",
    "error_reply": "I had a problem answering right now. Try again in a moment.",
    "cmd_status": "Show the memory state and the pending messages",
    "cmd_todo": "List the tasks with buttons to check and uncheck them",
    "cmd_curate": "Process the pending messages into the memory now",
    "task_completed": "🟢 Completed: {text}",
    "task_reopened": "⭕ Reopened: {text}",
    "task_failed": "Could not: {error}",
    "task_gone": "that task no longer exists",
    "task_update_error": "I had a problem updating the task.",
    "file_unsupported": "For now I only read PDFs and images (JPG, PNG, WebP or GIF).",
    "file_too_big": "That file exceeds the 20 MB limit.",
    "file_unreadable": "I could not read that file. {reason}",
    "file_download_failed": "I could not download the file right now. Try again in a moment.",
    "file_resend_failed": "I could not resend the file {filename}.",
    "file_resume_failed": "I could not resume a file you sent earlier; please send it again.",
    # ---- document reader errors ----
    "reader_unsupported": "I only read PDFs and images (JPG, PNG, WebP or GIF).",
    "reader_empty": "The file is empty.",
    "reader_too_big": "The file exceeds the 20 MB limit.",
    "reader_failed": "I could not read the file ({error_type}).",
    "reader_vision_hint": " The model may not accept images: set DOCUMENT_MODEL to a vision model.",
    "reader_no_text": "The model returned no text for this file.",
    # ---- curation report and status ----
    "curator_busy": "[curator] a curation is already running; wait for it to finish.",
    "curator_failed": "[curator] failed; {count} message(s) stay pending: {error}",
    "curator_done": "[curator] {count} message(s) processed ({tools})\n{report}",
    "curator_no_tools": "no tools",
    "curator_nothing": "[curator] nothing pending.",
    "status_prefix": "[status]",
    "status_pending": "pending: {count}",
    "status_in_progress": "messages in progress: {count}",
    "status_running": "curation running",
    "status_idle": "idle for {seconds}s (curation after {limit}s)",
    "status_error": "last error: {error}",
    # ---- task panel ----
    "panel_hint": "Tap a task to open it:",
    "panel_head": "Tasks ({where}, {day})",
    "panel_where_today": "today and the next days",
    "panel_where_week": "next 7 days",
    "panel_where_pending": "pending",
    "panel_none": "{head}: no tasks.",
    "panel_showing": "(showing {shown} of {total})",
    "detail_pending": "⭕ Not done yet",
    "detail_done": "🟢 Done",
    "detail_when": "When: {when}",
    "detail_no_date": "No date",
    "detail_repeats": "Repeats: {rule}",
    "detail_see": "Details: {ref}",
    "btn_complete": "🟢 Mark as done",
    "btn_reopen": "⭕ Mark as not done",
    "btn_back": "⬅️ Back to the list",
    "day_today": "Today",
    "day_tomorrow": "Tomorrow",
    "day_yesterday": "Yesterday",
    # ---- task lists and the morning briefing ----
    "briefing_greeting": "Good morning!",
    "fmt_overdue": "Overdue:",
    "fmt_older": "- (and {count} older ones)",
    "fmt_was": "(was {when})",
    "fmt_today": "Today ({day}):",
    "fmt_nothing": "- nothing scheduled",
    "fmt_upcoming": "Upcoming days:",
    "fmt_no_date_header": "No date:",
    "fmt_more": "- (and {count} more)",
    "fmt_no_date": "no date",
    "fmt_see": "(see {ref})",
    "fmt_done_suffix": ", done",
    "fmt_done_on": "done on {day}:",
    "fmt_at": "at {time}",
    "weekday_long_0": "Monday", "weekday_long_1": "Tuesday", "weekday_long_2": "Wednesday",
    "weekday_long_3": "Thursday", "weekday_long_4": "Friday", "weekday_long_5": "Saturday",
    "weekday_long_6": "Sunday",
    "weekday_short_0": "Mon", "weekday_short_1": "Tue", "weekday_short_2": "Wed",
    "weekday_short_3": "Thu", "weekday_short_4": "Fri", "weekday_short_5": "Sat",
    "weekday_short_6": "Sun",
    # ---- gateway ----
    "no_reply": "(no reply)",
}

_PLACEHOLDER = re.compile(r"\{(\w+)\}")

DETECT_PROMPT = (
    "Identify the language the user writes in. Reply with a JSON object only, in the form "
    '{"language": "<English name of the language, with the regional variant when it is clear, '
    'e.g. English, Portuguese (Brazil), Spanish (Mexico)>"}.'
)
TRANSLATE_PROMPT = (
    "You translate the fixed messages of a chat bot into {language}. You receive a JSON object "
    "that maps message keys to English texts. Reply with a JSON object only, with EXACTLY the same "
    "keys and the translated texts as values. Rules: keep every {{placeholder}} unchanged and "
    "untranslated (e.g. {{count}}, {{day}}); keep newlines, emojis, punctuation structure and "
    "leading/trailing spaces; do not translate command names (/status, /curate, /todo) or "
    "UPPER_CASE identifiers (DOCUMENT_MODEL); the bracketed labels such as [curator] and [status] "
    "may be translated; use short, natural, friendly wording in the "
    "target language; weekday names must be the real names of the days in {language}."
)


def _placeholders(text: str) -> set[str]:
    return set(_PLACEHOLDER.findall(text))


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("the model did not return a JSON object")
    return data


def _ask_json(llm, system: str, user: str) -> dict:
    """Ask the model for a JSON object. Prefers JSON mode; falls back to a plain call."""
    messages = [("system", system), ("user", user)]
    try:
        reply = llm.bind(response_format={"type": "json_object"}).invoke(messages)
    except Exception:  # noqa: BLE001 - the provider may not support JSON mode
        reply = llm.invoke(messages)
    return _parse_json(reply.text)


def validate(translated: dict) -> dict[str, str]:
    """Keep only the translations that are strings and keep the same {placeholders}."""
    good = {}
    for key, text in translated.items():
        base = CATALOG.get(key)
        if base is None or not isinstance(text, str) or not text.strip():
            continue
        if _placeholders(text) == _placeholders(base):
            good[key] = text
    return good


def _translate_prompt(language: str) -> str:
    return TRANSLATE_PROMPT.format(language=language)


class Messages:
    def __init__(self, path: Path | None = None, *, auto_fill: bool = False, llm_factory=None):
        self.path = Path(path) if path else None
        self.language = "English"
        self.texts: dict[str, str] = {}
        self.configured = False
        self.auto_fill = auto_fill  # translate missing keys in the background when they are needed
        self._llm_factory = llm_factory
        self._lock = threading.Lock()
        self._failed_at = 0.0
        self._filling = False
        self.load()
        if self.auto_fill and self.missing():
            self._request_fill()  # keys added by a newer version of the app

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.language = str(data.get("language") or "English")
            self.texts = validate(data.get("messages") or {})
            self.configured = True
        except (OSError, ValueError, AttributeError):
            log.warning("language file %s is unreadable; using English until it is rebuilt", self.path)

    def _llm(self):
        if self._llm_factory is not None:
            return self._llm_factory()
        from llm import get_llm

        return get_llm(temperature=0)

    @property
    def is_english(self) -> bool:
        return self.language.lower().startswith("english")

    def missing(self) -> list[str]:
        """Catalog keys with no translation yet (none for English or before the setup)."""
        if not self.configured or self.is_english:
            return []
        return [key for key in CATALOG if key not in self.texts]

    def _request_fill(self) -> None:
        if self._filling:
            return
        self._filling = True

        def work():
            try:
                self.fill_missing()
            finally:
                self._filling = False

        threading.Thread(target=work, name="i18n-fill", daemon=True).start()

    def fill_missing(self, llm=None) -> int:
        """Translate the keys the language file does not have yet and save them. Return how many
        were added (0 when there is nothing to do or the attempt failed; the next need retries)."""
        with self._lock:
            keys = self.missing()
            recently_failed = self._failed_at and time.monotonic() - self._failed_at < RETRY_AFTER_FAILURE
            if not keys or recently_failed:
                return 0
            try:
                payload = {key: CATALOG[key] for key in keys}
                translated = validate(_ask_json(llm or self._llm(), _translate_prompt(self.language),
                                                json.dumps(payload, ensure_ascii=False, indent=1)))
                added = {k: v for k, v in translated.items() if k in payload}
                if not added:
                    raise ValueError("no usable translation returned")
                self.texts.update(added)
                self._save()
                log.info("translated %d missing message(s) to %s", len(added), self.language)
                return len(added)
            except Exception as exc:  # noqa: BLE001 - English is used until a later attempt works
                self._failed_at = time.monotonic()
                log.warning("could not translate the missing messages", exc_info=exc)
                return 0

    def get(self, key: str, **kwargs) -> str:
        if self.auto_fill and key not in self.texts and key in CATALOG and self.missing():
            self._request_fill()  # on demand: English now, the translation for the next time
        text = self.texts.get(key) or CATALOG[key]
        if not kwargs:
            return text
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return CATALOG[key].format(**kwargs)  # a broken translation never breaks the bot

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"language": self.language, "messages": self.texts}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def ensure(self, sample: str, llm=None) -> bool:
        """Set the language up from a sample of the user's text, if it is not set up yet.
        Return True when the language file was just created (the caller may refresh menus)."""
        if self.configured or not sample.strip():
            return False
        with self._lock:
            recently_failed = self._failed_at and time.monotonic() - self._failed_at < RETRY_AFTER_FAILURE
            if self.configured or recently_failed:
                return False
            try:
                llm = llm or self._llm()
                language = str(_ask_json(llm, DETECT_PROMPT, sample[:1000]).get("language") or "").strip()
                if not language:
                    raise ValueError("no language detected")
                texts: dict[str, str] = {}
                if not language.lower().startswith("english"):
                    translated = _ask_json(llm, _translate_prompt(language),
                                           json.dumps(CATALOG, ensure_ascii=False, indent=1))
                    texts = validate(translated)
                    if len(texts) < len(CATALOG) * 0.8:
                        raise ValueError(f"only {len(texts)} of {len(CATALOG)} messages were translated correctly")
                self.language, self.texts, self.configured = language, texts, True
                self._save()
                log.info("user language set up: %s (%d translated messages)", language, len(texts))
                return True
            except Exception as exc:  # noqa: BLE001 - stay in English and try again later
                self._failed_at = time.monotonic()
                log.warning("could not set up the user language", exc_info=exc)
                return False


_current = Messages()  # English only, until main() points it to the language file


def use(messages: Messages) -> Messages:
    global _current
    _current = messages
    return messages


def current() -> Messages:
    return _current


def t(key: str, **kwargs) -> str:
    return _current.get(key, **kwargs)
