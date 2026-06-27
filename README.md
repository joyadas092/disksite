# Diskwala Gallery

A simple Flask website that imports Diskwala links from a Telegram channel and shows them as responsive cards.

## What This Project Uses

- Python for all backend code
- Flask for the website
- SQLite for the database
- Pyrogram for Telegram automation
- Bootstrap 5 and vanilla JavaScript for the UI

There is no React, Node, WordPress, login system, or CMS.

## Folder Structure

```text
DiskwalaWeb/
  app.py
  telegram_sync.py
  database.py
  requirements.txt
  render.yaml
  .env.example
  templates/
    index.html
  static/
    css/style.css
    js/app.js
  uploads/
```

`database.db` is created automatically after the first run.

## Run Locally

Open PowerShell in this folder:

```powershell
cd D:\python\Telegrambots\DiskwalaWeb
```

Create a virtual environment:

```powershell
python -m venv .venv
```

Activate it:

```powershell
.\.venv\Scripts\Activate.ps1
```

Install dependencies:

```powershell
pip install -r requirements.txt
```

Create your local environment file:

```powershell
copy .env.example .env
```

Start the website:

```powershell
python app.py
```

Open this URL in your browser:

```text
http://127.0.0.1:5000
```

## Configure Telegram Sync

Edit `.env` and fill these values:

```text
API_ID=your_telegram_api_id
API_HASH=your_telegram_api_hash
BOT_TOKEN=optional_bot_token
TELEGRAM_CHANNEL=@your_channel_username
DISKWALA_DOMAIN=diskwala.com
```

Get `API_ID` and `API_HASH` from:

```text
https://my.telegram.org
```

If you use `BOT_TOKEN`, the bot must be able to read the channel. Usually that means adding the bot to the channel as an admin.

Important: Telegram bots cannot import old channel history with `get_chat_history`.

Use one of these modes:

- Live bot sync: keep `BOT_TOKEN` filled and keep `IMPORT_LIMIT=0`.
- Old post import: remove `BOT_TOKEN`, run `telegram_sync.py`, and login as a Telegram user session when Pyrogram asks.

Run the Telegram importer in a second PowerShell window:

```powershell
cd D:\python\Telegrambots\DiskwalaWeb
.\.venv\Scripts\Activate.ps1
python telegram_sync.py
```

To import older Telegram posts, set this in `.env` before running `telegram_sync.py`:

```text
IMPORT_LIMIT=500
```

Set it back to `0` after the first import if you only want new posts.

## How It Works

`telegram_sync.py` watches your Telegram channel. When a post has a Diskwala link, it saves the caption, thumbnail, links, and media type into SQLite.

Sync logs are written here:

```text
logs/telegram_sync.log
```

`app.py` serves the homepage and API. The homepage loads 20 cards first. When you scroll down, JavaScript asks the API for the next 20.

Clicking a Watch button opens `/go/<post_id>/<link_number>`. That route increases the view counter and immediately redirects to the Diskwala link.

## Search And Sorting

The search box looks through captions.

Available sorting:

- Latest
- Oldest
- Random
- Most Viewed

The Random button does not shuffle the whole database. It picks a random starting id and loads a small set, which is much faster for large databases.

## Render Deployment

1. Push this `DiskwalaWeb` folder to GitHub.
2. Create a new Render Web Service.
3. Connect your GitHub repo.
4. Use these settings:

```text
Environment: Python
Build Command: pip install -r requirements.txt
Start Command: python app.py
```

5. Add environment variables in Render:

```text
FLASK_DEBUG=0
DISKWALA_ANDROID_URL=your_android_app_link
DISKWALA_IOS_URL=your_ios_app_link
DISKWALA_DESKTOP_URL=your_desktop_download_link
```

For Telegram sync on Render, create a separate Background Worker with:

```text
python telegram_sync.py
```

Add the Telegram environment variables to that worker too.

## Quick Test Without Telegram

You can create the database by running:

```powershell
python -c "from database import init_db; init_db()"
```

Then start the website:

```powershell
python app.py
```

The page will open with an empty state until Telegram posts are imported.
