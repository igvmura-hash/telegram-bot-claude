#!/usr/bin/env python3
"""
Telegram Bot with Claude AI integration via Railway
Receives messages in Telegram, sends to Claude, returns responses
"""

import os
import json
import logging
from typing import Optional
from flask import Flask, request
from telegram import Bot, Update
from telegram.error import TelegramError
from anthropic import Anthropic

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)

# Initialize Telegram bot
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
bot = Bot(token=TELEGRAM_TOKEN)

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

def get_claude_response(user_message: str, chat_id: int, user_name: Optional[str] = None) -> str:
    """Send message to Claude and get response"""
    try:
        # Add user message to history
        add_to_history(chat_id, "user", user_message)

        # Get conversation history
        history = get_conversation_history(chat_id)

        # System prompt
        system_prompt = f"""Ты помощник Igor в его реальном бизнесе недвижимости (moscowestate).

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
            model="claude-3-5-sonnet-20241022",
            max_tokens=1024,
            system=system_prompt,
            messages=history
        )

        # Get response text
        assistant_message = response.content[0].text

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
        update_data = request.get_json()
        update = Update.de_json(update_data, bot)

        if not update:
            return {"ok": False}, 400

        # Handle message updates
        if update.message and update.message.text:
            chat_id = update.message.chat_id
            user_id = update.message.from_user.id
            user_name = update.message.from_user.first_name
            user_message = update.message.text

            logger.info(f"Message from {user_name} (ID: {user_id}): {user_message}")

            # Show typing indicator
            bot.send_chat_action(chat_id, "typing")

            # Get Claude response
            response = get_claude_response(user_message, chat_id, user_name)

            # Send response back to user
            bot.send_message(
                chat_id=chat_id,
                text=response,
                parse_mode="Markdown"
            )

            logger.info(f"Response sent to {user_name}")

        return {"ok": True}, 200

    except TelegramError as e:
        logger.error(f"Telegram error: {e}")
        return {"ok": False, "error": str(e)}, 400
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return {"ok": False, "error": str(e)}, 500

@app.route("/set_webhook", methods=["POST"])
def set_webhook():
    """Manually set webhook (call once to activate)"""
    try:
        webhook_url = os.getenv("RAILWAY_PUBLIC_DOMAIN")
        if not webhook_url:
            return {"error": "RAILWAY_PUBLIC_DOMAIN not set"}, 400

        # Format webhook URL
        full_url = f"https://{webhook_url}/webhook/{TELEGRAM_TOKEN}"

        # Set webhook
        bot.set_webhook(url=full_url)

        return {
            "ok": True,
            "webhook_url": full_url,
            "message": "Webhook set successfully"
        }, 200

    except Exception as e:
        logger.error(f"Error setting webhook: {e}")
        return {"ok": False, "error": str(e)}, 400

@app.route("/webhook_info", methods=["GET"])
def webhook_info():
    """Get current webhook info"""
    try:
        info = bot.get_webhook_info()
        return {
            "url": info.url,
            "has_custom_certificate": info.has_custom_certificate,
            "pending_update_count": info.pending_update_count,
            "last_error_date": info.last_error_date,
            "last_error_message": info.last_error_message,
        }, 200
    except Exception as e:
        return {"error": str(e)}, 400

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
