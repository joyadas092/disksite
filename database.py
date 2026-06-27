import json
import os
import random
import sqlite3
import urllib.request
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", BASE_DIR / "database.db"))
TURSO_URL = os.getenv("TURSO_DATABASE_URL", "").strip()
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "").strip()

if not DATABASE_PATH.is_absolute():
    DATABASE_PATH = BASE_DIR / DATABASE_PATH


# ---------------------------------------------------------------------------
# Turso HTTP client — no extra packages, pure stdlib urllib
# ---------------------------------------------------------------------------

class _TursoRow:
    """sqlite3.Row-compatible wrapper around a Turso result row."""
    def __init__(self, cols, raw_values):
        self._cols = [c["name"] for c in cols]
        self._vals = [self._cast(v) for v in raw_values]

    @staticmethod
    def _cast(v):
        if v is None:
            return None
        if isinstance(v, dict):
            t = v.get("type", "text")
            val = v.get("value")
            if val is None:
                return None
            if t == "integer":
                return int(val)
            if t == "real":
                return float(val)
            return val
        return v

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._vals[key]
        return self._vals[self._cols.index(key)]

    def keys(self):
        return self._cols


class _TursoCursor:
    def __init__(self, rows, lastrowid=None):
        self._rows = rows
        self.lastrowid = lastrowid

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _TursoConnection:
    """Minimal sqlite3-compatible connection that talks to Turso over HTTP."""

    def __init__(self, url, token):
        self._url = url.replace("libsql://", "https://") + "/v2/pipeline"
        self._token = token
        self.row_factory = None  # accepted but ignored

    # -- internal helpers --

    def _http(self, stmts):
        requests = [{"type": "execute", "stmt": s} for s in stmts]
        requests.append({"type": "close"})
        body = json.dumps({"requests": requests}).encode()
        req = urllib.request.Request(
            self._url,
            data=body,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    @staticmethod
    def _encode_param(p):
        if p is None:
            return {"type": "null", "value": None}
        if isinstance(p, bool):
            return {"type": "integer", "value": "1" if p else "0"}
        if isinstance(p, int):
            return {"type": "integer", "value": str(p)}
        if isinstance(p, float):
            return {"type": "real", "value": str(p)}
        return {"type": "text", "value": str(p)}

    def _to_stmt(self, sql, params=()):
        return {"sql": sql, "args": [self._encode_param(p) for p in (params or [])]}

    # -- public API (mirrors sqlite3.Connection) --

    def execute(self, sql, params=()):
        result = self._http([self._to_stmt(sql, params)])
        res = result["results"][0]
        if res["type"] == "error":
            raise Exception(res.get("error", {}).get("message", "Turso error"))
        er = res["response"]["result"]
        cols = er.get("cols", [])
        rows = [_TursoRow(cols, r) for r in er.get("rows", [])]
        lastrowid = er.get("last_insert_rowid")
        return _TursoCursor(rows, int(lastrowid) if lastrowid is not None else None)

    def executescript(self, script):
        """Split a SQL script into individual statements and execute as a batch."""
        stmts = _split_script(script)
        if stmts:
            self._http([{"sql": s, "args": []} for s in stmts])

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def _split_script(script):
    """
    Split a SQL script into individual statements.
    Handles CREATE TRIGGER...END blocks that contain internal semicolons.
    """
    stmts = []
    buf = []
    in_trigger = False

    for line in script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        upper = stripped.upper()
        if upper.startswith("CREATE TRIGGER"):
            in_trigger = True
        buf.append(line)
        if in_trigger:
            if upper == "END;" or upper == "END":
                in_trigger = False
                stmts.append("\n".join(buf).rstrip(";"))
                buf = []
        elif stripped.endswith(";"):
            stmts.append("\n".join(buf).rstrip(";"))
            buf = []

    if buf:
        s = "\n".join(buf).rstrip(";").strip()
        if s:
            stmts.append(s)

    return [s for s in stmts if s.strip()]


# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------

def get_connection():
    if TURSO_URL and TURSO_TOKEN:
        return _TursoConnection(TURSO_URL, TURSO_TOKEN)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


# ---------------------------------------------------------------------------
# Public database functions (unchanged)
# ---------------------------------------------------------------------------

def init_db():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    with get_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_message_id INTEGER UNIQUE,
                caption TEXT NOT NULL DEFAULT '',
                media_type TEXT NOT NULL DEFAULT 'unknown',
                thumbnail_path TEXT,
                links_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS views (
                post_id INTEGER PRIMARY KEY,
                count INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(
                caption,
                content='posts',
                content_rowid='id'
            );

            CREATE TRIGGER IF NOT EXISTS posts_ai AFTER INSERT ON posts BEGIN
                INSERT INTO posts_fts(rowid, caption)
                VALUES (new.id, new.caption);
            END;

            CREATE TRIGGER IF NOT EXISTS posts_ad AFTER DELETE ON posts BEGIN
                INSERT INTO posts_fts(posts_fts, rowid, caption)
                VALUES ('delete', old.id, old.caption);
            END;

            CREATE TRIGGER IF NOT EXISTS posts_au AFTER UPDATE ON posts BEGIN
                INSERT INTO posts_fts(posts_fts, rowid, caption)
                VALUES ('delete', old.id, old.caption);
                INSERT INTO posts_fts(rowid, caption)
                VALUES (new.id, new.caption);
            END;

            CREATE INDEX IF NOT EXISTS idx_posts_created_at
                ON posts(created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_views_count
                ON views(count DESC);
            """
        )


def upsert_post(telegram_message_id, caption, media_type, thumbnail_path, links, created_at=None):
    links_json = json.dumps(links, ensure_ascii=False)

    with get_connection() as connection:
        existing = connection.execute(
            "SELECT id FROM posts WHERE telegram_message_id = ?",
            (telegram_message_id,),
        ).fetchone()

        if existing:
            post_id = existing["id"]
            connection.execute(
                """
                UPDATE posts
                SET caption = ?,
                    media_type = ?,
                    thumbnail_path = ?,
                    links_json = ?,
                    created_at = COALESCE(?, created_at),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (caption, media_type, thumbnail_path, links_json, created_at, post_id),
            )
        else:
            cursor = connection.execute(
                """
                INSERT INTO posts (
                    telegram_message_id, caption, media_type,
                    thumbnail_path, links_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
                """,
                (telegram_message_id, caption, media_type, thumbnail_path, links_json, created_at),
            )
            post_id = cursor.lastrowid
            connection.execute(
                "INSERT OR IGNORE INTO views (post_id, count) VALUES (?, 0)",
                (post_id,),
            )

    return post_id


def increment_views(post_id):
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO views (post_id, count) VALUES (?, 1)
            ON CONFLICT(post_id) DO UPDATE SET count = count + 1
            """,
            (post_id,),
        )


def get_post(post_id):
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT posts.*, COALESCE(views.count, 0) AS views
            FROM posts
            LEFT JOIN views ON views.post_id = posts.id
            WHERE posts.id = ?
            """,
            (post_id,),
        ).fetchone()
    return _format_post(row) if row else None


def get_posts(search="", sort="latest", page=1, limit=20):
    page = max(int(page), 1)
    limit = min(max(int(limit), 1), 50)
    offset = (page - 1) * limit
    params = []
    where_sql = ""
    join_sql = "LEFT JOIN views ON views.post_id = posts.id"

    fts_query = _to_fts_query(search) if search else ""

    if fts_query:
        join_sql += " JOIN posts_fts ON posts_fts.rowid = posts.id"
        where_sql = "WHERE posts_fts MATCH ?"
        params.append(fts_query)

    if sort == "oldest":
        order_sql = "ORDER BY posts.created_at ASC, posts.id ASC"
    elif sort == "most_viewed":
        order_sql = "ORDER BY COALESCE(views.count, 0) DESC, posts.id DESC"
    elif sort == "random" and not search:
        return _get_random_posts(limit)
    else:
        order_sql = "ORDER BY posts.created_at DESC, posts.id DESC"

    query = f"""
        SELECT posts.*, COALESCE(views.count, 0) AS views
        FROM posts
        {join_sql}
        {where_sql}
        {order_sql}
        LIMIT ? OFFSET ?
    """
    params.extend([limit + 1, offset])

    with get_connection() as connection:
        rows = connection.execute(query, params).fetchall()

    has_more = len(rows) > limit
    return [_format_post(row) for row in rows[:limit]], has_more


def _get_random_posts(limit):
    with get_connection() as connection:
        max_id_row = connection.execute("SELECT MAX(id) AS max_id FROM posts").fetchone()
        max_id = max_id_row["max_id"] if max_id_row else None

        if not max_id:
            return [], False

        start_id = random.randint(1, max_id)
        rows = connection.execute(
            """
            SELECT posts.*, COALESCE(views.count, 0) AS views
            FROM posts
            LEFT JOIN views ON views.post_id = posts.id
            WHERE posts.id >= ?
            ORDER BY posts.id ASC
            LIMIT ?
            """,
            (start_id, limit),
        ).fetchall()

        if len(rows) < limit:
            extra = connection.execute(
                """
                SELECT posts.*, COALESCE(views.count, 0) AS views
                FROM posts
                LEFT JOIN views ON views.post_id = posts.id
                WHERE posts.id < ?
                ORDER BY posts.id ASC
                LIMIT ?
                """,
                (start_id, limit - len(rows)),
            ).fetchall()
            rows = list(rows) + list(extra)

    return [_format_post(row) for row in rows], True


def _format_post(row):
    links = json.loads(row["links_json"] or "[]")
    thumbnail_path = row["thumbnail_path"]
    if not thumbnail_path:
        thumbnail_url = None
    elif thumbnail_path.startswith("tg:"):
        thumbnail_url = f"/thumb/{thumbnail_path[3:]}"
    else:
        thumbnail_url = f"/uploads/{thumbnail_path}"

    return {
        "id": row["id"],
        "caption": row["caption"],
        "media_type": row["media_type"],
        "thumbnail_url": thumbnail_url,
        "links": links,
        "views": row["views"],
        "created_at": row["created_at"],
    }


def delete_post(post_id):
    with get_connection() as connection:
        row = connection.execute(
            "SELECT thumbnail_path FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        if not row:
            return False
        connection.execute("DELETE FROM posts WHERE id = ?", (post_id,))

    if row["thumbnail_path"] and not row["thumbnail_path"].startswith("tg:"):
        thumb = BASE_DIR / "uploads" / row["thumbnail_path"]
        thumb.unlink(missing_ok=True)

    return True


def _to_fts_query(value):
    words = []
    for word in value.strip().split():
        cleaned = "".join(c for c in word if c.isalnum())
        if cleaned:
            words.append(f"{cleaned}*")
    return " ".join(words)
