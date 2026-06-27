import asyncio
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

from database import init_db, upsert_post


BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

DISKWALA_DOMAIN = os.getenv("DISKWALA_DOMAIN", "diskwala.com").lower()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Optional: restrict to a specific Telegram user ID (your own).
# Leave blank to accept from any private chat.
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID", "").strip()

URL_PATTERN = re.compile(r"https?://[^\s<>'\")]+", re.IGNORECASE)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "telegram_sync.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)


# ── Bot API helpers ───────────────────────────────────────────────────────────

def call_bot_api(method, params=None):
    """GET-based Bot API call (for getUpdates, getFile, etc.)."""
    query = urllib.parse.urlencode(params or {})
    suffix = f"?{query}" if query else ""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}{suffix}"
    with urllib.request.urlopen(url, timeout=45) as r:
        data = json.loads(r.read().decode())
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram Bot API error"))
    return data["result"]


def call_bot_api_post(method, payload):
    """POST-based Bot API call (for sendMessage, etc.)."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode())
    if not data.get("ok"):
        logging.warning("Bot API %s failed: %s", method, data)
    return data.get("result")


def send_reply(chat_id, reply_to_message_id, text):
    try:
        call_bot_api_post("sendMessage", {
            "chat_id": chat_id,
            "reply_to_message_id": reply_to_message_id,
            "text": text,
            "parse_mode": "HTML",
        })
    except Exception as exc:
        logging.warning("Could not send reply: %s", exc)


# ── Link extraction ───────────────────────────────────────────────────────────

def normalize_url(url):
    return (url or "").rstrip(".,;!]")


def is_diskwala_url(url):
    host = urlparse(url).netloc.lower()
    return DISKWALA_DOMAIN in host


def extract_links(text, entities):
    links, seen = [], set()
    for match in URL_PATTERN.findall(text or ""):
        url = normalize_url(match)
        if url and is_diskwala_url(url) and url not in seen:
            links.append(url)
            seen.add(url)
    for entity in (entities or []):
        url = entity.get("url") if isinstance(entity, dict) else getattr(entity, "url", None)
        if url:
            url = normalize_url(url)
            if url and is_diskwala_url(url) and url not in seen:
                links.append(url)
                seen.add(url)
    return links


def extract_bot_api_links(message):
    text = message.get("caption") or message.get("text") or ""
    entities = message.get("caption_entities") or message.get("entities") or []
    return extract_links(text, entities)


# ── Thumbnail (file_id stored, proxied on demand) ─────────────────────────────

def get_bot_api_thumbnail(message):
    if message.get("photo"):
        return f"tg:{message['photo'][-1]['file_id']}", "image"
    if message.get("video", {}).get("thumbnail"):
        return f"tg:{message['video']['thumbnail']['file_id']}", "video"
    if message.get("document", {}).get("thumbnail"):
        return f"tg:{message['document']['thumbnail']['file_id']}", "document"
    return None, "text"


def bot_api_date_as_text(message):
    ts = message.get("date")
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else None


# ── Message handling ──────────────────────────────────────────────────────────

def is_allowed_sender(message):
    """Accept messages only from the owner (if OWNER_CHAT_ID is set)."""
    if not OWNER_CHAT_ID:
        return True  # open to anyone who chats the bot
    chat_id = str(message.get("chat", {}).get("id", ""))
    return chat_id == OWNER_CHAT_ID


def handle_bot_api_message(message):
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    text = message.get("text") or message.get("caption") or ""

    # /start command
    if text.strip().startswith("/start"):
        send_reply(chat_id, message_id,
            "👋 <b>Diskwala Bot</b>\n\n"
            "Send me any message with a <b>Diskwala link</b> and I'll post it to the website.\n\n"
            f"🆔 Your chat ID: <code>{chat_id}</code>\n"
            f"🌐 Website: https://diskwala.fun"
        )
        return

    if not is_allowed_sender(message):
        logging.info("Ignored message from unauthorized chat id=%s", chat_id)
        return

    links = extract_bot_api_links(message)

    if not links:
        send_reply(chat_id, message_id,
            f"❌ No <b>{DISKWALA_DOMAIN}</b> links found in this message.\n"
            "Please send a message containing a valid Diskwala link."
        )
        return

    try:
        thumbnail, media_type = get_bot_api_thumbnail(message)
        post_id = upsert_post(
            telegram_message_id=message_id,
            caption=text,
            media_type=media_type,
            thumbnail_path=thumbnail,
            links=links,
            created_at=bot_api_date_as_text(message),
        )
        send_reply(chat_id, message_id,
            f"✅ <b>Posted to website!</b>\n"
            f"🆔 Post #{post_id} | 🔗 {len(links)} link(s)\n"
            f"🌐 <a href='https://diskwala.fun'>diskwala.fun</a>"
        )
        logging.info("Saved post #%s from message %s", post_id, message_id)
    except Exception as error:
        logging.exception("Could not save message %s: %s", message_id, error)
        send_reply(chat_id, message_id, f"❌ Failed to save: {error}")


# ── Polling loop (local development) ─────────────────────────────────────────

def run_bot_api_sync():
    logging.info("Bot API sync started (DM mode). domain=%s", DISKWALA_DOMAIN)
    offset = None

    while True:
        try:
            updates = call_bot_api("getUpdates", {
                "offset": offset,
                "timeout": 30,
                "limit": 50,
                "allowed_updates": json.dumps(["message"]),
            })

            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message")
                if not message:
                    continue
                logging.info("Received message %s from chat %s",
                             message.get("message_id"),
                             message.get("chat", {}).get("id"))
                handle_bot_api_message(message)

        except Exception as error:
            logging.exception("Bot API polling error: %s", error)
            time.sleep(5)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required in your .env file.")

    lock_file = BASE_DIR / "telegram_sync.lock"
    if lock_file.exists():
        try:
            pid = int(lock_file.read_text().strip())
            import psutil
            if psutil.pid_exists(pid):
                logging.error("Another instance is already running (PID %s). "
                              "Stop it or delete telegram_sync.lock.", pid)
                raise SystemExit(1)
        except (ValueError, ImportError):
            pass

    lock_file.write_text(str(os.getpid()))
    try:
        init_db()
        run_bot_api_sync()
    finally:
        lock_file.unlink(missing_ok=True)
