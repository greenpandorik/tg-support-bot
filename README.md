# Support Bot

A Telegram bot that bridges users in private chat with a support team via a **forum supergroup**.  
Each user gets their own forum topic where agents can read and reply.

---

## Table of Contents

- [How It Works](#how-it-works)
- [Features](#features)
- [Project Structure](#project-structure)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [Support Commands](#support-commands)
- [Configuration Reference](#configuration-reference)

---

## How It Works

```
User (private chat) ──► Bot ──► Forum topic in support group
Support agent ──► Replies in topic ──► Bot ──► User (private chat)
```

1. A user sends `/start` and writes their question.
2. The bot creates (or reuses) a dedicated forum topic in the support supergroup.
3. The first message in the topic is a pinned info card with the user's name, Telegram ID, and commands.
4. Support agents reply inside the topic — the bot forwards every message to the user.
5. The topic icon (or title prefix) updates automatically to show whether the last message was from the user or an agent.

---

## Features

- **Forum topics per user** — one topic per Telegram user, auto-created on first message.
- **Silent mode** — mute delivery to the user without closing the topic (`/silent`).
- **Ban / Unban** — block a user from messaging support (`/ban`).
- **Dialogue archive** — export the full conversation as a `.txt` file (`/archive`).
- **Topic status icons** — configurable custom emojis or title prefixes to show topic state (new / user replied / agent replied / banned).
- **Auto cleanup** — background job removes stale topics for banned or inactive users.
- **Multi-language** — Russian and English out of the box; easily extensible.
- **Newsletter** — broadcast a message to all users (developer only, `/newsletter`).
- **Persistent storage** — SQLite for dialogue archive and user state; Redis for real-time state and locks.

---

## Project Structure

```
tg-support-bot/
├── app/
│   ├── __main__.py              # Entry point: bot, dispatcher, scheduler setup
│   ├── config.py                # Pydantic-less config loaded from .env
│   ├── logger.py                # Logging configuration
│   └── bot/
│       ├── commands.py          # Bot command registration (BotCommand scopes)
│       ├── manager.py           # Manager helper (send/delete messages, i18n)
│       ├── db/
│       │   ├── dialogue_store.py    # SQLite: stores all messages for /archive
│       │   ├── user_state_store.py  # SQLite: persists user/topic state to disk
│       │   └── cleanup_stats_store.py
│       ├── handlers/
│       │   ├── group/
│       │   │   ├── command.py   # /ban /silent /close /info /archive /help /id
│       │   │   └── message.py   # Forwards agent replies to users; updates topic icons
│       │   └── private/
│       │       ├── command.py   # /start /language /source /newsletter
│       │       ├── message.py   # Forwards user messages to the forum topic
│       │       ├── callback_query.py  # Language selection
│       │       ├── windows.py   # UI "screens": main menu, language picker
│       │       └── my_chat_member.py  # Handles bot block/unblock events
│       ├── jobs/
│       │   └── cleanup.py       # Periodic cleanup of old/blocked topics
│       ├── middlewares/
│       │   ├── manager.py       # Injects Manager into every handler
│       │   ├── redis.py         # Injects RedisStorage + UserData
│       │   ├── album.py         # Groups media albums into a single Album object
│       │   └── throttling.py    # Basic flood protection
│       ├── types/
│       │   └── album.py         # Album dataclass for grouped media
│       └── utils/
│           ├── create_forum_topic.py  # Topic creation, icon/title updates, locks
│           ├── archive_topic.py       # Ensures "archive" meta-topic exists
│           ├── reactions.py           # Pin messages, ack reactions
│           ├── exceptions.py          # Custom exceptions
│           ├── texts.py               # All i18n strings (ru / en)
│           └── redis/
│               ├── redis.py           # RedisStorage wrapper
│               └── models.py          # UserData dataclass
├── .env.example                 # Environment variable template
├── docker-compose.yml           # Bot + Redis services
├── Dockerfile
├── requirements.txt
└── setup.sh                     # One-command setup & deploy script
```

---

## Requirements

- Python 3.10+
- Redis
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- A Telegram **supergroup** with **Topics** enabled (the support group)

---

## Quick Start

### Using `setup.sh` (recommended)

```bash
git clone <repo-url> tg-support-bot
cd tg-support-bot
cp .env.example .env
# Edit .env with your values
./setup.sh
```

The script detects your OS, installs Docker if needed, creates the `.data` directory, and brings up the containers.

### Manual (Docker Compose)

```bash
cp .env.example .env
# Fill in .env
mkdir -p .data redis/data
docker compose up -d --build
```

### Without Docker (local)

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Fill in .env, set REDIS_HOST=localhost
python -m app
```

---

## Support Commands

Commands available to users in `SUPPORT_ADMIN_IDS`:

| Command    | Description |
|------------|-------------|
| `/ban`     | Block / Unblock the user associated with the current topic |
| `/silent`  | Toggle silent mode — messages are not forwarded to the user |
| `/close`   | Mark the topic as answered by manager (updates icon/title) |
| `/info`    | Show user info card (ID, name, username, language, ban status) |
| `/archive` | Export the last 200 dialogue messages as a `.txt` file |
| `/help`    | List all available commands |
| `/id`      | Show the current chat ID (useful during initial setup) |

---

## Configuration Reference

See [`.env.example`](.env.example) for all available variables with descriptions.

Key variables:

| Variable | Description |
|----------|-------------|
| `BOT_TOKEN` | Telegram bot token |
| `BOT_DEV_ID` | Your Telegram ID (developer/admin) |
| `BOT_GROUP_ID` | Forum supergroup ID |
| `SUPPORT_ADMIN_IDS` | Comma-separated IDs allowed to run support commands |
| `BOT_EMOJI_ID` | Custom emoji ID for the default topic icon |
| `TOPIC_STATUS_IN_TITLE` | `true` = status in topic title; `false` = status as emoji icon |
| `MULTI_LANGUAGE` | Enable Russian + English UI |
| `DEFAULT_LANGUAGE` | `ru` or `en` |
| `REDIS_HOST` | Redis hostname (default: `redis` inside Docker) |


---

## Support the Project

If this project was useful to you and you want to say thanks, you can support the developer with a small coffee donation.

**USDT TRC20 wallet:** `TCKRKJHE5Yewk8o3sTGB5J6YMcVBxivoG1`

Thank you for your support and interest in the project.

---

# Поддержка-бот

Telegram-бот, который связывает пользователей в личных чатах с командой поддержки через **форум-супергруппу**.  
Для каждого пользователя создаётся отдельная тема (топик), где агенты могут читать и отвечать.

---

## Как это работает

```
Пользователь (личный чат) ──► Бот ──► Топик в форум-группе поддержки
Агент поддержки ──► Отвечает в топике ──► Бот ──► Пользователь (личный чат)
```

1. Пользователь отправляет `/start` и пишет вопрос.
2. Бот создаёт (или переиспользует) отдельный топик в форум-супергруппе.
3. Первое сообщение в топике — закреплённая карточка с именем, Telegram ID пользователя и списком команд.
4. Агенты отвечают внутри топика — бот пересылает каждое сообщение пользователю.
5. Иконка топика (или префикс заголовка) обновляется: показывает, от кого последнее сообщение.

---

## Возможности

- **Топики per-user** — один топик на пользователя, создаётся автоматически.
- **Тихий режим** — отключить доставку сообщений пользователю без закрытия топика (`/silent`).
- **Бан / Разбан** — заблокировать пользователя от обращений в поддержку (`/ban`).
- **Архив диалога** — экспортировать переписку в `.txt` (`/archive`).
- **Статусные иконки** — настраиваемые emoji или текстовые префиксы для состояний топика.
- **Автоочистка** — фоновая задача удаляет устаревшие топики забаненных или неактивных пользователей.
- **Мультиязычность** — русский и английский из коробки.
- **Рассылка** — отправить сообщение всем пользователям (только для разработчика, `/newsletter`).
- **Постоянное хранение** — SQLite для архива диалогов и состояний; Redis для live-состояния и блокировок.

---

## Быстрый старт

### Через `setup.sh` (рекомендуется)

```bash
git clone <repo-url> tg-support-bot
cd tg-support-bot
cp .env.example .env
# Заполните .env нужными значениями
./setup.sh
```

Скрипт определяет ОС, при необходимости устанавливает Docker, создаёт папку `.data` и поднимает контейнеры.

### Вручную (Docker Compose)

```bash
cp .env.example .env
# Заполните .env
mkdir -p .data redis/data
docker compose up -d --build
```

### Без Docker (локально)

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Заполните .env, укажите REDIS_HOST=localhost
python -m app
```

---

## Команды поддержки

Команды доступны пользователям из `SUPPORT_ADMIN_IDS`:

| Команда    | Описание |
|------------|----------|
| `/ban`     | Заблокировать / разблокировать пользователя текущего топика |
| `/silent`  | Тихий режим — сообщения не пересылаются пользователю |
| `/close`   | Отметить топик как отвеченный менеджером |
| `/info`    | Карточка пользователя (ID, имя, юзернейм, язык, статус бана) |
| `/archive` | Экспорт последних 200 сообщений диалога в `.txt` |
| `/help`    | Список всех доступных команд |
| `/id`      | Показать ID текущего чата (для первоначальной настройки) |


---

## Поддержать проект

Если проект оказался полезным и вы хотите сказать спасибо, можно угостить разработчика чашкой кофе.

**USDT TRC20 кошелёк:** `TCKRKJHE5Yewk8o3sTGB5J6YMcVBxivoG1`

Спасибо за поддержку и интерес к проекту.
