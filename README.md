# limas-memory

A personal memory assistant you talk to on Telegram. It answers using what it remembers about you and, in the
background, turns what you tell it into structured, versioned Markdown notes.

It is built from LangChain agents running on any model available through [OpenRouter](https://openrouter.ai).

## How it works

Two agents with strictly separated jobs:

| Agent | Job | Memory access |
|---|---|---|
| **Chat Agent** | Talks to you, reads the memory, manages your tasks | Read-only (plus the task and attachment tools) |
| **Memory Curator** | Decides what has lasting value and writes it down | The **only** writer of `memory/` |

```
Telegram ──► bot ──► Chat Agent ──► reads memory/ ──► reply (+ task buttons)
               │
               └─► conversation history ──(after N s of silence, or /curate)──► Curator ──► memory/ ──► git commit + push
```

- **Memory** is a plain folder of Markdown files (`memory/`) with an `index.md`, one small document per piece of
  knowledge, English frontmatter (`description`, `origin_date`, `channel`) and relative links between related
  documents. It is its own git repository: the Curator commits (and pushes) every change with a descriptive message.
  `memory/` contains only Markdown; all code lives in `src/`.
- **Curator** gets the recent messages and the existing documents, checks what is already stored, and then creates,
  updates or ignores. It records contradictions with historical context, marks hypotheses as hypotheses, and never
  summarizes away information.
- **Everything is persisted on arrival.** Each message is written to the history before any processing, the typing
  indicator starts immediately, and messages are answered one at a time, in order. After a crash, the bot resumes
  unanswered messages from the last 24 hours.

## Features

- **Chat with memory**: ask about your trips, preferences, projects, people. The chat agent consults the memory
  before answering.
- **Attachments**: send a PDF or an image (a trip voucher, a receipt). It is read by a model, the full content is
  kept in the memory (by design: you decide what is stored), and the original file can be sent back on request.
- **Tasks and reminders** (`todos/`): a central list plus one file per month, with tags (`#health`), recurrence
  (daily, weekly, monthly), links to the related memory document, and an index. Completed tasks older than 30 days
  are archived.
- **Web search**: the chat agent has a `web_search` tool and uses it only when the question needs current or external
  information (news, prices, exchange rates, weather). It runs OpenRouter's `web` plugin with the Parallel engine, the
  cheapest one (about US$ 0.001 per search in `turbo` mode, plus the model tokens), in a separate short call, so the
  normal conversation never pays for a search. Results come with source links and are treated as untrusted text
  (never as instructions). `WEB_SEARCH=off` removes the tool.
- **Task buttons**: `/todo` (or any request to list tasks) shows a panel where each task is a button, `⭕`
  pending / `🟢` done; tapping one opens its card (full text, when, tags, linked document) with buttons to
  check/uncheck it and to go back to the list, so long tasks are never cut off. Multi-day panels prefix each task with `[Today 18:00]`, `[Tomorrow 07:00]`,
  `[31/01 15:00]`.
- **Lists** (shopping, movies, books…): loose items without a date are kept by the Curator in `lists/<name>.md`
  rather than becoming tasks.
- **Morning briefing**: every day at `BRIEFING_TIME` the bot sends overdue, today's and upcoming tasks (nothing if
  there is nothing to say). If the bot was down at that time, it catches up until noon.
- **Your language**: the code only knows English messages. On your first message the bot asks the model which
  language you write in and, if it is not English, to translate every fixed message (help, errors, task panel,
  briefing, weekday names…) into it, as JSON. The result is saved in `data/language.json`. Messages added by newer
  versions of the app are translated on demand: English is shown right away and the missing keys are translated in
  the background and saved. To change the language, delete the file; it is rebuilt from the next message.
- **Logging**: console logs plus an optional Telegram channel that receives warnings, errors, and start/stop
  notices, and the Curator's reports for automatic runs. Secrets (bot token, API keys, git token) are masked in all
  destinations, and logs never contain the text of your messages or of the memory.
- **Memory backup**: after every curation the memory repository is committed and pushed to your private remote
  (GitHub, Gitea…), including changes made outside the Curator (tasks, attachments).

### Bot commands

| Command | What it does |
|---|---|
| `/status` | Pending messages, curation state, last error |
| `/curate` | Runs the Curator now on whatever is pending and replies with its report |
| `/todo [today\|week\|pending] [tag]` | Task panel with buttons: today and the next days (default), the next 7 days, or all pending; the optional tag filters |

Only the user in `TELEGRAM_ALLOWED_USER_ID` is served, and only in a private chat.

## Quick start

Requirements: Python 3.12+, `git`, an OpenRouter API key, and a Telegram bot token from
[@BotFather](https://t.me/BotFather).

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip
cp .env.example .env                            # then fill it in (see below)
.venv/bin/python src/bot.py                     # Windows: .venv\Scripts\python
```

The first message from an unauthorized user makes the bot print their numeric ID: use it as
`TELEGRAM_ALLOWED_USER_ID`. Adding the bot to a channel or group prints that chat's ID, which is what
`LOG_CHANNEL_ID` needs.

Useful flags: `--memory-dir`, `--log` (conversation file), `--idle-seconds`, `--verbose` (prints which memory
documents each answer consulted).

## Configuration

All settings are environment variables (`.env` is loaded automatically). See `.env.example` for comments.

**Required**

| Variable | Purpose |
|---|---|
| `OPENROUTER_API_KEY` | OpenRouter key |
| `TELEGRAM_BOT_TOKEN` | Bot token |
| `TELEGRAM_ALLOWED_USER_ID` | Your numeric Telegram user ID |

**Memory backup (recommended)**

| Variable | Purpose |
|---|---|
| `MEMORY_GIT_REMOTE` | HTTPS URL of a **private** repository for the memory |
| `MEMORY_GIT_TOKEN` | Access token with write access to that repository only (sent as an HTTP header, never stored in `.git/config`) |
| `MEMORY_GIT_USER_NAME`, `MEMORY_GIT_USER_EMAIL` | Author of the Curator's commits |

**Optional**

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_MODEL` | `openai/gpt-6-luna` | Model for both agents |
| `DOCUMENT_MODEL` | `OPENROUTER_MODEL` | Model that reads PDFs and images |
| `PDF_ENGINE` | automatic | OpenRouter PDF engine (`cloudflare-ai`, `mistral-ocr`, `native`) |
| `CURATOR_IDLE_SECONDS` | `600` | Silence before the Curator runs automatically |
| `BRIEFING_TIME` | `06:00` | `HH:MM`, optionally with a zone: `06:00-03`, `06:00 America/Recife`, or `off` |
| `LOG_LEVEL` | `INFO` | Console level |
| `LOG_CHANNEL_ID` | off | Telegram channel for logs (`-100…` or `@name`); the bot must be a channel **admin** that can post |
| `LOG_CHANNEL_LEVEL` | `WARNING` | Minimum level sent to the channel |
| `LOG_CHANNEL_HEADER` | empty | Text on top of each batch, for shared channels |
| `WEB_SEARCH`, `WEB_SEARCH_ENGINE`, `WEB_SEARCH_MODE`, `WEB_SEARCH_MAX_RESULTS`, `WEB_SEARCH_MODEL` | `on`, `parallel`, `turbo`, `3`, `OPENROUTER_MODEL` | Web search tool |
| `MEMORY_DIR`, `CONVERSATION_FILE`, `BRIEFING_STATE_FILE`, `LANGUAGE_FILE` | `./memory`, `./data/conversation.json`, `./data/briefing.json`, `./data/language.json` | Locations |

## Docker

```bash
cp .env.example .env     # fill it in (Compose reads it for the ${...} values)
docker compose up -d --build
docker compose logs -f
```

- The image contains only the code. Secrets come from the environment of the stack. Locally that is the `.env`
  next to `docker-compose.yml`; in **Portainer** (stack from a Git repository) add each variable under
  *Environment variables* (the repository has no `.env`). `OPENROUTER_API_KEY` and `TELEGRAM_BOT_TOKEN` are required.
- State lives in two named volumes: `memory` (the memory git repository) and `data` (conversation history and
  briefing state). To start from an existing memory, copy it into the `memory` volume, for example
  `docker cp ./memory/. <container>:/memory` followed by `docker compose restart`.
  The container runs as uid 1000, so fix the ownership if needed: `docker exec -u 0 <container> chown -R 1000 /memory /data`.
- `TZ` (default `America/Recife`) defines what "today" means for tasks. `BRIEFING_TIME` may carry its own zone.
- On shutdown the bot waits up to 30 s for a running curation to finish.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Layout:

```
src/
  bot.py, telegram_gateway.py   Telegram handlers and the thin wrapper around the Telegram API
  chatflow.py                   conversation flow: ingest → respond → curate → sync
  i18n.py                       fixed messages: English catalog + the user's language (data/language.json)
  chat_agent/, curator/         the two agents (prompts in src/prompts/*.md)
  storage/                      MemoryStore (path-confined, Markdown only), index, git
  tasks/                        task store, tools, button panel
  attachments/                  file reading, registry, resend tool
  conversation/                 persisted conversation history
  briefing.py, applog.py        morning briefing, logging layer
evals/                          model comparison on scenarios (compare.py)
tests/
```

Try the Curator on a sample conversation without touching your real memory:

```bash
python src/curate.py evals/scenarios/02_new_fact.txt --memory-dir /tmp/memory-test --verbose
```

Compare models on the Curator scenarios (writes `compare-report.md` and `compare-results.json`; each run uses a
fresh temporary memory and never pushes):

```bash
python evals/compare.py --models openai/gpt-6-luna,anthropic/claude-haiku-5.5 --runs 2
```

## Privacy notes

- The memory can hold sensitive data (vouchers, reservation codes) because you asked it to keep everything. Use a
  **private** remote.
- `data/conversation.json` keeps the last 50 curated messages in plain text on disk (they give the chat agent its
  conversational context). It is git-ignored.
- Prompts and the memory are sent to the model provider you configure on OpenRouter.
