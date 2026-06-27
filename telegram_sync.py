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
from pyrogram.errors import BotMethodInvalid
from pyrogram import Client, idle

load_dotenv()

from database import init_db, upsert_post


BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = Path(os.getenv("UPLOAD_FOLDER", str(BASE_DIR / "uploads")))
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

DISKWALA_DOMAIN = os.getenv("DISKWALA_DOMAIN", "diskwala.com").lower()
CHANNEL_VALUE = os.getenv("TELEGRAM_CHANNEL", "").strip()
CHANNEL_ID = int(CHANNEL_VALUE) if CHANNEL_VALUE.lstrip("-").isdigit() else None
CHANNEL_USERNAME = CHANNEL_VALUE.lower() if CHANNEL_ID is None else None
IMPORT_LIMIT = int(os.getenv("IMPORT_LIMIT", "0"))

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
SESSION_NAME = os.getenv("PYROGRAM_SESSION", "diskwala_session")

URL_PATTERN = re.compile(r"https?://[^\s<>'\")]+", re.IGNORECASE)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "telegram_sync.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)


if not API_ID or not API_HASH:
    raise RuntimeError("API_ID and API_HASH are required in your .env file.")

if not CHANNEL_VALUE:
    raise RuntimeError("TELEGRAM_CHANNEL is required in your .env file.")


client = Client(
    SESSION_NAME,
    api_id=int(API_ID),
    api_hash=API_HASH,
    bot_token=BOT_TOKEN or None,
)


def call_bot_api(method, params=None):
    query = urllib.parse.urlencode(params or {})
    suffix = f"?{query}" if query else ""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}{suffix}"

    with urllib.request.urlopen(url, timeout=45) as response:
        data = json.loads(response.read().decode("utf-8"))

    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram Bot API error"))

    return data["result"]


def normalize_url(url):
    return (url or "").rstrip(".,;!]")


def is_diskwala_url(url):
    host = urlparse(url).netloc.lower()
    return DISKWALA_DOMAIN in host


def add_link(links, seen, url):
    url = normalize_url(url)
    if url and is_diskwala_url(url) and url not in seen:
        links.append(url)
        seen.add(url)


def extract_links_from_text_and_entities(text, entities):
    links = []
    seen = set()

    for match in URL_PATTERN.findall(text or ""):
        add_link(links, seen, match)

    for entity in entities or []:
        url = getattr(entity, "url", None)
        if isinstance(entity, dict):
            url = entity.get("url")
        if url:
            add_link(links, seen, url)

    return links


def extract_diskwala_links(message):
    """Find Diskwala links inside a caption or text message."""
    text = message.caption or message.text or ""
    entities = message.caption_entities or message.entities or []
    return extract_links_from_text_and_entities(text, entities)


def extract_bot_api_links(message):
    text = message.get("caption") or message.get("text") or ""
    entities = message.get("caption_entities") or message.get("entities") or []
    return extract_links_from_text_and_entities(text, entities)


def chat_matches(message):
    if not message.chat:
        return False

    if CHANNEL_ID is not None:
        return message.chat.id == CHANNEL_ID

    username = (message.chat.username or "").lower()
    configured = CHANNEL_USERNAME.lstrip("@")
    return username == configured


def telegram_date_as_text(message):
    if not message.date:
        return None

    return message.date.strftime("%Y-%m-%d %H:%M:%S")


def bot_api_date_as_text(message):
    timestamp = message.get("date")
    if not timestamp:
        return None

    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


async def download_thumbnail(app_client, message):
    """Store the Telegram file_id so we can proxy it later — no local disk needed."""
    if message.photo:
        return f"tg:{message.photo.file_id}", "image"

    if message.video and message.video.thumbs:
        return f"tg:{message.video.thumbs[-1].file_id}", "video"

    if message.document and message.document.thumbs:
        return f"tg:{message.document.thumbs[-1].file_id}", "document"

    return None, "text"


def download_bot_api_thumbnail(message):
    """Store the Telegram file_id so we can proxy it later — no local disk needed."""
    if message.get("photo"):
        file_id = message["photo"][-1]["file_id"]
        return f"tg:{file_id}", "image"

    if message.get("video", {}).get("thumbnail"):
        file_id = message["video"]["thumbnail"]["file_id"]
        return f"tg:{file_id}", "video"

    if message.get("document", {}).get("thumbnail"):
        file_id = message["document"]["thumbnail"]["file_id"]
        return f"tg:{file_id}", "document"

    return None, "text"


async def save_message(app_client, message):
    caption = message.caption or message.text or ""
    links = extract_diskwala_links(message)

    if not links:
        logging.info(
            "Skipped message %s: no %s links found",
            message.id,
            DISKWALA_DOMAIN,
        )
        return

    try:
        thumbnail_name, media_type = await download_thumbnail(app_client, message)
        post_id = upsert_post(
            telegram_message_id=message.id,
            caption=caption,
            media_type=media_type,
            thumbnail_path=thumbnail_name,
            links=links,
            created_at=telegram_date_as_text(message),
        )
        logging.info("Saved post #%s from Telegram message %s", post_id, message.id)
    except Exception as error:
        logging.exception("Could not save Telegram message %s: %s", message.id, error)


def bot_api_chat_matches(message):
    chat = message.get("chat") or {}

    if CHANNEL_ID is not None:
        return chat.get("id") == CHANNEL_ID

    username = (chat.get("username") or "").lower()
    configured = CHANNEL_USERNAME.lstrip("@")
    return username == configured


def save_bot_api_message(message):
    links = extract_bot_api_links(message)
    message_id = message.get("message_id")

    if not links:
        logging.info(
            "Skipped Bot API message %s: no %s links found",
            message_id,
            DISKWALA_DOMAIN,
        )
        return

    try:
        thumbnail_name, media_type = download_bot_api_thumbnail(message)
        post_id = upsert_post(
            telegram_message_id=message_id,
            caption=message.get("caption") or message.get("text") or "",
            media_type=media_type,
            thumbnail_path=thumbnail_name,
            links=links,
            created_at=bot_api_date_as_text(message),
        )
        logging.info("Saved post #%s from Bot API message %s", post_id, message_id)
    except Exception as error:
        logging.exception("Could not save Bot API message %s: %s", message_id, error)


def run_bot_api_sync():
    logging.info(
        "Bot API sync started. channel=%s domain=%s",
        CHANNEL_VALUE,
        DISKWALA_DOMAIN,
    )
    offset = None

    while True:
        try:
            updates = call_bot_api(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 30,
                    "limit": 50,
                    "allowed_updates": json.dumps(["channel_post"]),
                },
            )

            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("channel_post")

                if not message:
                    continue

                if not bot_api_chat_matches(message):
                    chat = message.get("chat") or {}
                    logging.info(
                        "Ignored Bot API message %s from chat id=%s title=%s",
                        message.get("message_id"),
                        chat.get("id"),
                        chat.get("title"),
                    )
                    continue

                logging.info("Received matching Bot API channel message %s", message["message_id"])
                save_bot_api_message(message)
        except Exception as error:
            logging.exception("Bot API polling error: %s", error)
            time.sleep(5)


@client.on_message()
async def on_new_channel_post(app_client, message):
    if not chat_matches(message):
        if message.chat:
            logging.info(
                "Ignored message %s from chat id=%s username=%s",
                message.id,
                message.chat.id,
                message.chat.username,
            )
        return

    logging.info("Received matching channel message %s", message.id)
    await save_message(app_client, message)


async def import_recent_history(app_client):
    if IMPORT_LIMIT <= 0:
        return

    chat = CHANNEL_ID if CHANNEL_ID is not None else CHANNEL_VALUE
    logging.info("Importing latest %s messages from %s...", IMPORT_LIMIT, chat)

    try:
        async for message in app_client.get_chat_history(chat, limit=IMPORT_LIMIT):
            await save_message(app_client, message)
    except BotMethodInvalid:
        logging.error(
            "IMPORT_LIMIT cannot be used with BOT_TOKEN. "
            "Keep IMPORT_LIMIT=0 for live bot sync, or remove BOT_TOKEN "
            "and login with a Telegram user session to import old posts."
        )


async def main():
    init_db()
    await client.start()
    logging.info(
        "Telegram sync started. channel=%s domain=%s import_limit=%s",
        CHANNEL_VALUE,
        DISKWALA_DOMAIN,
        IMPORT_LIMIT,
    )
    await import_recent_history(client)
    logging.info("Listening for new posts from %s...", CHANNEL_VALUE)
    await idle()
    await client.stop(block=False)


if __name__ == "__main__":
    lock_file = BASE_DIR / "telegram_sync.lock"
    if lock_file.exists():
        try:
            pid = int(lock_file.read_text().strip())
            import psutil
            if psutil.pid_exists(pid):
                logging.error(
                    f"Another instance is already running (PID {pid}). "
                    "Stop it first or delete telegram_sync.lock if it is stale."
                )
                raise SystemExit(1)
        except (ValueError, ImportError):
            pass

    lock_file.write_text(str(os.getpid()))
    try:
        init_db()
        if BOT_TOKEN:
            run_bot_api_sync()
        else:
            asyncio.run(main())
    finally:
        lock_file.unlink(missing_ok=True)
