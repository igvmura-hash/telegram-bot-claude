#!/usr/bin/env python3
"""
Telegram Bot with Claude AI integration via Railway
Receives messages in Telegram, sends to Claude, returns responses
"""

import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
import requests
from flask import Flask, request
from anthropic import Anthropic

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)

# Telegram config
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Notion config: incoming notes go to "📝 Журнал заметок" and are sorted by the
# daily 19:00 summary. Without NOTION_TOKEN the bot works as before (chat only).
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_INBOX_DB = os.getenv("NOTION_INBOX_DB", "42c0163465754a67b42333685f1840bc")
NOTION_VERSION = "2022-06-28"

# Only this Telegram user may save notes and chat with Claude (see /myid).
# If empty, the bot answers everyone (old behaviour) - set it in Railway Variables.
OWNER_ID = os.getenv("OWNER_ID", "").strip()

MSK = timezone(timedelta(hours=3))

# Initialize Anthropic client
client = Anthropic()

# Store conversation history per user (chat_id)
conversation_history = {}
MAX_HISTORY = 10  # Keep last 10 messages per conversation


def get_conversation_history(chat_id: int) -> list:
    """Get conversation history for a user"""
    if chat_id not in conversation_history:
        conversation_history[chat_id] = []
    return conversation_history[chat_id]


def add_to_history(chat_id: int, role: str, content: str):
    """Add message to conversation history"""
    history = get_conversation_history(chat_id)
    history.append({"role": role, "content": content})

    # Keep only last MAX_HISTORY messages
    if len(history) > MAX_HISTORY:
        conversation_history[chat_id] = history[-MAX_HISTORY:]


def send_telegram_message(chat_id: int, text: str):
    """Send a message to a Telegram chat via the raw Bot API"""
    try:
        resp = requests.post(
            f"{TELEGRAM_API_URL}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
        if resp.status_code != 200:
            # Markdown parsing can fail on odd characters - retry as plain text
            logger.warning(f"Markdown send failed ({resp.status_code}), retrying as plain text")
            requests.post(
                f"{TELEGRAM_API_URL}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=15,
            )
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")


def send_typing_action(chat_id: int):
    """Show the 'typing...' indicator in Telegram"""
    try:
        requests.post(
            f"{TELEGRAM_API_URL}/sendChatAction",
            json={"chat_id": chat_id, "action": "typing"},
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"Failed to send typing action: {e}")


def _rich_text(text: str) -> list:
    """Notion rich_text, split into 2000-char pieces (Notion limit per piece)"""
    text = text or ""
    return [{"type": "text", "text": {"content": text[i:i + 2000]}}
            for i in range(0, min(len(text), 40000), 2000)] or []


def save_to_notion(text: str, source: str) -> bool:
    """Save a raw note into the Notion inbox (Журнал заметок, Разобрано = нет)"""
    if not NOTION_TOKEN:
        logger.warning("NOTION_TOKEN is not set - note not saved")
        return False
    now = datetime.now(MSK)
    first_line = " ".join(text.split())[:60]
    try:
        resp = requests.post(
            "https://api.notion.com/v1/pages",
            headers={
                "Authorization": f"Bearer {NOTION_TOKEN}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
            json={
                "parent": {"database_id": NOTION_INBOX_DB},
                "properties": {
                    "Заметка": {"title": [{"type": "text", "text": {"content": f"Telegram: {first_line}"}}]},
                    "Дата": {"date": {"start": now.isoformat(timespec="minutes")}},
                    "Исходный текст": {"rich_text": _rich_text(text)},
                    "Кратко": {"rich_text": _rich_text(f"Источник: {source}")},
                    "Разобрано": {"checkbox": False},
                },
            },
            timeout=20,
        )
        if resp.status_code != 200:
            logger.error(f"Notion error {resp.status_code}: {resp.text[:500]}")
            return False
        return True
    except Exception as e:
        logger.error(f"Failed to save to Notion: {e}")
        return False


def is_owner(user_id) -> bool:
    return not OWNER_ID or str(user_id) == OWNER_ID


def get_claude_response(user_message: str, chat_id: int, user_name: Optional[str] = None) -> str:
    """Send message to Claude and get response"""
    try:
        # Add user message to history
        add_to_history(chat_id, "user", user_message)

        # Get conversation history
        history = get_conversation_history(chat_id)

        # System prompt
        system_prompt = """Ты помощник Igor в его реальном бизнесе недвижимости (moscowestate).

Igor ведёт:
- Проект Рожново (15 домов, жилой комплекс)
- Клиентов и агентов по продаже недвижимости
- Две хоккейные группы (дети Дима и Илья)
- Финансовую аналитику проектов
- Строительные работы (опалубка, кровля, окна, сметы)

Отвечай кратко, по делу. Используй структурированные ответы:
- 🟢 Клиенты
- 🔨 Строительство/Задачи
- 💰 Финансы
- ⚠️ Срочное
- 🏒 Хоккей

Если вопрос про бизнес - помогай с анализом, рекомендациями, проверкой данных.
Если просьба добавить информацию - запомни и используй в следующих ответах.
Язык: русский."""

        # Call Claude API
        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=1024,
            system=system_prompt,
            messages=history,
        )

        # Get response text (claude-sonnet-5 uses adaptive thinking by default,
        # so content[0] can be a ThinkingBlock instead of the text block)
        assistant_message = next(
            (block.text for block in response.content if getattr(block, "type", None) == "text"),
            "",
        )

        # Add response to history
        add_to_history(chat_id, "assistant", assistant_message)

        return assistant_message

    except Exception as e:
        logger.error(f"Error calling Claude API: {e}")
        return f"❌ Ошибка обработки: {str(e)}"


@app.route("/", methods=["GET"])
def home():
    """Health check endpoint"""
    return {"status": "ok", "bot": "Igor's Telegram Bot is running"}, 200


@app.route(f"/webhook/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    """Telegram webhook endpoint"""
    try:
        update = request.get_json(silent=True)

        if not update:
            return {"ok": False}, 400

        message = update.get("message")

        # Handle message updates
        if message and message.get("text"):
            chat = message["chat"]
            chat_id = chat["id"]
            from_user = message.get("from", {}) or {}
            user_id = from_user.get("id")
            user_name = from_user.get("first_name", "")
            user_message = message["text"]

            logger.info(f"Message from {user_name} (ID: {user_id}): {user_message[:200]}")

            # Groups (Рожново, хоккей): silently save every message to the inbox, never reply
            if chat.get("type") in ("group", "supergroup"):
                group_name = chat.get("title", "группа")
                author = " ".join(filter(None, [from_user.get("first_name"), from_user.get("last_name")]))
                save_to_notion(user_message, f"Telegram-группа «{group_name}», автор: {author or 'неизвестно'}")
                return {"ok": True}, 200

            # /myid - shows the Telegram ID to put into OWNER_ID
            if user_message.strip().startswith("/myid"):
                send_telegram_message(chat_id, f"Ваш Telegram ID: {user_id}")
                return {"ok": True}, 200

            if not is_owner(user_id):
                logger.info(f"Ignored message from non-owner {user_id}")
                return {"ok": True}, 200

            # "+ текст" - save to Notion inbox, sorted by the evening summary
            if user_message.lstrip().startswith("+"):
                note = user_message.lstrip()[1:].strip()
                if not note:
                    send_telegram_message(chat_id, "Напишите текст после «+», например: + Клиент Сергей, бюджет 40")
                elif save_to_notion(note, f"Telegram, личное сообщение от {user_name}"):
                    send_telegram_message(chat_id, "✅ Записал в Notion — разберу в вечерней сводке")
                else:
                    send_telegram_message(chat_id, "❌ Не получилось записать в Notion. Проверьте NOTION_TOKEN в Railway.")
                return {"ok": True}, 200

            # Show typing indicator
            send_typing_action(chat_id)

            # Get Claude response
            response_text = get_claude_response(user_message, chat_id, user_name)

            # Send response back to user
            send_telegram_message(chat_id, response_text)

            logger.info(f"Response sent to {user_name}")

        return {"ok": True}, 200

    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return {"ok": False, "error": str(e)}, 500


@app.route("/set_webhook", methods=["GET", "POST"])
def set_webhook():
    """Set the Telegram webhook (call once, or after redeploy, to activate)"""
    try:
        webhook_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN")
        if not webhook_domain:
            return {"error": "RAILWAY_PUBLIC_DOMAIN not set"}, 400

        full_url = f"https://{webhook_domain}/webhook/{TELEGRAM_TOKEN}"

        resp = requests.post(
            f"{TELEGRAM_API_URL}/setWebhook",
            json={"url": full_url},
            timeout=15,
        )
        data = resp.json()

        return {
            "ok": data.get("ok", False),
            "webhook_url": full_url,
            "telegram_response": data,
        }, 200

    except Exception as e:
        logger.error(f"Error setting webhook: {e}")
        return {"ok": False, "error": str(e)}, 400


@app.route("/webhook_info", methods=["GET"])
def webhook_info():
    """Get current webhook info"""
    try:
        resp = requests.get(f"{TELEGRAM_API_URL}/getWebhookInfo", timeout=15)
        return resp.json(), 200
    except Exception as e:
        return {"error": str(e)}, 400


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
