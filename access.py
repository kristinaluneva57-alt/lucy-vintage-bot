"""Уровни доступа к исследованиям Люси и месячные лимиты.

Уровни:
- администратор (LUCY_ADMIN_ID) — всё, без персональных лимитов,
  но в общем месячном бюджете ИИ (ai_ledger);
- пилот — до LUCY_PILOT_MAX (10) участниц, вход по ссылке-приглашению
  t.me/<бот>?start=pilot-<LUCY_PILOT_CODE>; месячные лимиты на
  исследования;
- участница — всё остальное в Люсе, исследования Люси — после пилота.
Будущие платные пользователи — отдельный уровень с теми же механизмами
(роль в user_roles + свои лимиты), пока не включён.

Лимиты считаются по запущенным исследованиям за календарный месяц
(готовые из кэша и неудачные не считаются):
- LUCY_PILOT_RESEARCH_MONTH (4) — «🔬 Исследование Люси»;
- LUCY_PILOT_DEEP_MONTH (1) — «🧭 Глубокая атрибуция».
"""

import hmac
import os
import sqlite3
from datetime import datetime, timezone

DB_TIMEOUT = 30
ROLE_ADMIN = "admin"
ROLE_PILOT = "pilot"
ROLE_MEMBER = "member"
ROLE_LABELS = {ROLE_ADMIN: "администратор", ROLE_PILOT: "пилот", ROLE_MEMBER: "участница"}


def _int_env(name, default):
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


def pilot_max():
    return _int_env("LUCY_PILOT_MAX", 10)


def pilot_code():
    return os.environ.get("LUCY_PILOT_CODE", "").strip()


def monthly_limit(role, mode):
    if role == ROLE_ADMIN:
        return None  # без персонального лимита
    if role == ROLE_PILOT:
        if mode == "deep":
            return _int_env("LUCY_PILOT_DEEP_MONTH", 1)
        return _int_env("LUCY_PILOT_RESEARCH_MONTH", 4)
    return 0


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=DB_TIMEOUT, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout = {DB_TIMEOUT * 1000}")
    return conn


def init_tables(db_path):
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_roles (
                user_id INTEGER PRIMARY KEY,
                role TEXT NOT NULL,
                created_at TEXT NOT NULL,
                removed_at TEXT
            )
            """
        )
    finally:
        conn.close()


def _is_admin(user_id):
    admin_id = os.environ.get("LUCY_ADMIN_ID", "").strip()
    return bool(admin_id) and str(user_id) == admin_id


def role_of(db_path, user_id):
    if _is_admin(user_id):
        return ROLE_ADMIN
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT role FROM user_roles WHERE user_id = ? AND removed_at IS NULL", (user_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return ROLE_MEMBER
    finally:
        conn.close()
    return row[0] if row else ROLE_MEMBER


def pilots(db_path):
    conn = _connect(db_path)
    try:
        return [row[0] for row in conn.execute(
            "SELECT user_id FROM user_roles WHERE role = ? AND removed_at IS NULL ORDER BY created_at",
            (ROLE_PILOT,),
        )]
    finally:
        conn.close()


def join_pilot(db_path, user_id, code):
    """Вход в пилот по коду приглашения. Возвращает joined | already | full | bad_code | closed."""
    expected = pilot_code()
    if not expected:
        return "closed"
    if not hmac.compare_digest(code or "", expected):
        return "bad_code"
    init_tables(db_path)
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT role FROM user_roles WHERE user_id = ? AND removed_at IS NULL", (user_id,)
            ).fetchone()
            if row and row[0] == ROLE_PILOT:
                conn.execute("ROLLBACK")
                return "already"
            count = conn.execute(
                "SELECT COUNT(*) FROM user_roles WHERE role = ? AND removed_at IS NULL", (ROLE_PILOT,)
            ).fetchone()[0]
            if count >= pilot_max():
                conn.execute("ROLLBACK")
                return "full"
            conn.execute(
                "INSERT OR REPLACE INTO user_roles (user_id, role, created_at, removed_at) "
                "VALUES (?, ?, ?, NULL)",
                (user_id, ROLE_PILOT, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            conn.execute("COMMIT")
            return "joined"
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def remove(db_path, user_id):
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            "UPDATE user_roles SET removed_at = ? WHERE user_id = ? AND removed_at IS NULL",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), user_id),
        )
        return cursor.rowcount == 1
    finally:
        conn.close()


def used_this_month(db_path, user_id, mode):
    """Запущенные исследования за месяц (готовые и идущие; из кэша и с ошибкой — нет)."""
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    conn = _connect(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM item_research WHERE user_id = ? AND mode = ? "
            "AND status IN ('running', 'done') AND created_at LIKE ?",
            (user_id, mode, month + "%"),
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def research_quota(db_path, user_id, mode):
    """(можно ли, осталось или None — без лимита, лимит или None)."""
    role = role_of(db_path, user_id)
    limit = monthly_limit(role, mode)
    if limit is None:
        return True, None, None
    left = max(0, limit - used_this_month(db_path, user_id, mode))
    return left > 0, left, limit
