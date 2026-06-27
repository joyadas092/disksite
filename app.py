import os
import threading
import urllib.request

from dotenv import load_dotenv
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_from_directory

load_dotenv()

from database import delete_post, get_post, get_posts, increment_views, init_db


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", os.path.join(BASE_DIR, "uploads"))

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
init_db()


@app.route("/")
def index():
    return render_template(
        "index.html",
        android_url=os.getenv("DISKWALA_ANDROID_URL", "https://play.google.com/store"),
        ios_url=os.getenv("DISKWALA_IOS_URL", "https://www.apple.com/app-store/"),
        desktop_url=os.getenv("DISKWALA_DESKTOP_URL", "https://diskwala.com/download"),
    )


@app.route("/api/posts")
def api_posts():
    search = request.args.get("q", "").strip()
    sort = request.args.get("sort", "latest")
    page = request.args.get("page", 1, type=int)
    limit = request.args.get("limit", 20, type=int)

    if sort not in {"latest", "oldest", "random", "most_viewed"}:
        sort = "latest"

    posts, has_more = get_posts(search=search, sort=sort, page=page, limit=limit)
    return jsonify(
        {
            "posts": posts,
            "has_more": has_more,
            "next_page": page + 1,
        }
    )


@app.route("/go/<int:post_id>/<int:link_number>")
def go_to_diskwala(post_id, link_number):
    post = get_post(post_id)
    if not post:
        abort(404)

    links = post["links"]
    if link_number < 1 or link_number > len(links):
        abort(404)

    increment_views(post_id)
    return redirect(links[link_number - 1])


@app.route("/install")
def install_app():
    user_agent = request.headers.get("User-Agent", "").lower()

    if "android" in user_agent:
        return redirect(os.getenv("DISKWALA_ANDROID_URL", "https://play.google.com/store"))

    if "iphone" in user_agent or "ipad" in user_agent or "ipod" in user_agent:
        return redirect(os.getenv("DISKWALA_IOS_URL", "https://www.apple.com/app-store/"))

    return redirect(os.getenv("DISKWALA_DESKTOP_URL", "https://diskwala.com/download"))


@app.route("/admin/delete/<int:post_id>", methods=["POST"])
def admin_delete_post(post_id):
    admin_key = os.getenv("ADMIN_KEY", "")
    if not admin_key or request.headers.get("X-Admin-Key") != admin_key:
        abort(403)
    deleted = delete_post(post_id)
    return jsonify({"deleted": deleted, "id": post_id})


@app.route("/sitemap.xml")
def sitemap():
    base = os.getenv("SITE_URL", "https://diskwala.fun").rstrip("/")
    posts, _ = get_posts(sort="latest", page=1, limit=50)
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
             f'  <url><loc>{base}/</loc><changefreq>hourly</changefreq><priority>1.0</priority></url>']
    for p in posts:
        lines.append(f'  <url><loc>{base}/go/{p["id"]}/1</loc><lastmod>{p["created_at"][:10]}</lastmod><priority>0.8</priority></url>')
    lines.append("</urlset>")
    return Response("\n".join(lines), mimetype="application/xml")


@app.route("/robots.txt")
def robots():
    base = os.getenv("SITE_URL", "https://diskwala.fun")
    body = f"User-agent: *\nAllow: /\nDisallow: /admin/\nDisallow: /api/\nSitemap: {base}/sitemap.xml\n"
    return Response(body, mimetype="text/plain")


_thumb_cache = {}  # file_id -> (content_type, bytes)

@app.route("/thumb/<path:file_id>")
def proxy_thumbnail(file_id):
    if file_id in _thumb_cache:
        content_type, data = _thumb_cache[file_id]
        return Response(data, mimetype=content_type)

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        abort(404)
    try:
        info_url = f"https://api.telegram.org/bot{bot_token}/getFile?file_id={file_id}"
        with urllib.request.urlopen(info_url, timeout=10) as r:
            import json as _json
            info = _json.loads(r.read())
        file_path = info["result"]["file_path"]
        img_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
        with urllib.request.urlopen(img_url, timeout=10) as r:
            data = r.read()
            content_type = r.headers.get("Content-Type", "image/jpeg")
        if len(_thumb_cache) < 500:
            _thumb_cache[file_id] = (content_type, data)
        return Response(data, mimetype=content_type)
    except Exception:
        abort(404)


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


def _start_telegram_sync():
    """Launch telegram_sync in a background daemon thread (bot-API mode only).
    Only runs in the actual worker process, not the Flask reloader parent."""
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        return
    # When Flask debug reloader is active it spawns a child with WERKZEUG_RUN_MAIN=true.
    # We must skip the parent process so only one poller runs.
    debug_mode = os.getenv("FLASK_DEBUG") == "1"
    if debug_mode and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return
    try:
        from telegram_sync import run_bot_api_sync
        t = threading.Thread(target=run_bot_api_sync, daemon=True, name="telegram-sync")
        t.start()
    except Exception as exc:
        import logging
        logging.error("Could not start telegram sync thread: %s", exc)


if __name__ == "__main__":
    init_db()
    _start_telegram_sync()
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG") == "1")
