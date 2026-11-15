"""Очередь распознавания книг, которая хранится в базе и переживает перезапуск.

Раньше книга, загруженная через /bigbook, пока Люся читала другую, получала
отказ «Люся уже обрабатывает другую книгу». PDF уже лежал в R2, но читать его
никто не начинал. Ещё при каждом перезапуске Railway пропадала фоновая задача.
Отсюда «потерявшиеся» книги в /lost.

Теперь каждая книга встаёт в очередь (таблица library_import_queue в той же
базе /data). Люся берёт книги строго по одной, от старой к новой. Если Railway
перезапустил бота посреди книги, при старте она снова встаёт в очередь
и продолжается с последней готовой страницы.

Этот модуль работает только с базой. Сам разбор очереди — в big_book_upload.
"""

from library_db import get_connection

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
ERROR = "error"


def ensure_table():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS library_import_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                storage_key TEXT NOT NULL,
                title TEXT NOT NULL,
                language TEXT NOT NULL,
                file_name TEXT,
                upload_id TEXT,
                state TEXT NOT NULL DEFAULT 'queued',
                error TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                finished_at TEXT
            )
        """)


_KEYS = ["id", "storage_key", "title", "language", "file_name", "upload_id", "state", "error"]


def _row(row):
    return dict(zip(_KEYS, row)) if row else None


def enqueue(storage_key, title, language, file_name=None, upload_id=None):
    """Ставит книгу в очередь. Если она уже ждёт или читается — второй раз не ставит.

    Возвращает (id записи, добавлена ли сейчас).
    """
    ensure_table()
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM library_import_queue WHERE storage_key = ? AND state IN (?, ?)",
            (storage_key, QUEUED, RUNNING),
        ).fetchone()
        if existing:
            return existing[0], False
        cursor = conn.execute(
            "INSERT INTO library_import_queue (storage_key, title, language, file_name, upload_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (storage_key, title, language, file_name, upload_id),
        )
        return cursor.lastrowid, True


def position(item_id):
    """Сколько книг перед этой: читается сейчас + ждут раньше неё."""
    ensure_table()
    with get_connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM library_import_queue "
            "WHERE id != ? AND (state = ? OR (state = ? AND id < ?))",
            (item_id, RUNNING, QUEUED, item_id),
        ).fetchone()[0]


def claim_next():
    """Берёт самую старую ждущую книгу и помечает «читается». Нет книг — None."""
    ensure_table()
    with get_connection() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_KEYS)} FROM library_import_queue "
            "WHERE state = ? ORDER BY id LIMIT 1",
            (QUEUED,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE library_import_queue SET state = ?, started_at = CURRENT_TIMESTAMP WHERE id = ?",
            (RUNNING, row[0]),
        )
    item = _row(row)
    item["state"] = RUNNING
    return item


def finish(item_id, error=None):
    ensure_table()
    with get_connection() as conn:
        conn.execute(
            "UPDATE library_import_queue SET state = ?, error = ?, "
            "finished_at = CURRENT_TIMESTAMP WHERE id = ?",
            (ERROR if error else DONE, (error or None) and str(error)[:500], item_id),
        )


def requeue_interrupted():
    """При старте бота: книги, которые читались в момент перезапуска, — снова в очередь."""
    ensure_table()
    with get_connection() as conn:
        return conn.execute(
            "UPDATE library_import_queue SET state = ? WHERE state = ?",
            (QUEUED, RUNNING),
        ).rowcount


def pending():
    """Книги, которые читаются или ждут, по порядку."""
    ensure_table()
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(_KEYS)} FROM library_import_queue "
            "WHERE state IN (?, ?) ORDER BY CASE state WHEN ? THEN 0 ELSE 1 END, id",
            (RUNNING, QUEUED, RUNNING),
        ).fetchall()
    return [_row(row) for row in rows]
