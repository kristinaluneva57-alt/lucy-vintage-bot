"""Общий учёт фактических расходов на ИИ и поиск и общий месячный бюджет.

Все платные вызовы Люси попадают в общий месячный бюджет
LUCY_AI_BUDGET_MONTH_USD (по умолчанию 11 $ ≈ €10):

- новые вызовы (исследование украшения, интернет-поиск, свободные
  вопросы, ответы по книгам) пишутся в таблицу ai_spend;
- у визуальной атрибуции (visual_ai_calls) и /article
  (library_article_calls) свои журналы — их расходы тоже учитываются.

Каждый крупный вызов: резерв оценки → запрос → фактическая стоимость
по токенам и единицам из ответа. Оценка — не гарантия: «вторая стена» —
предоплаченный баланс OpenAI без автопополнения и бесплатный план
поиска (см. docs/RESEARCH_ENGINE.md).

При 50 %, 80 % и 100 % месячного бюджета администратору уходит одно
сообщение (notifier задаёт бот при старте). После 100 % платные вызовы
останавливаются для всех, включая администратора.

Настройки:
- LUCY_AI_BUDGET_MONTH_USD (11.0)
- LUCY_MODEL_PRICES — JSON {"модель": [вход, выход]} в $ за 1 млн токенов,
  дополняет и переопределяет встроенные цены
- LUCY_PRICE_INPUT_PER_M / LUCY_PRICE_OUTPUT_PER_M — цена gpt-4.1-mini
  (как в visual_ai)
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

DB_TIMEOUT = 30
STALE_RESERVATION_MINUTES = 30
ALERT_THRESHOLDS = (50, 80, 100)
# Цена неизвестной модели — нарочно завышенная: лучше остановиться раньше.
UNKNOWN_PRICE = (10.0, 40.0)
DEFAULT_PRICES = {
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1": (2.00, 8.00),
}

# Функция notifier(text) отправляет сообщение администратору.
# Задаётся ботом при старте; в тестах — None (предупреждения только в базе).
notifier = None


class BudgetExceeded(Exception):
    pass


def _float_env(name, default):
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except ValueError:
        return default


def budget_month():
    return _float_env("LUCY_AI_BUDGET_MONTH_USD", 11.0)


def model_price(model):
    """(вход, выход) в $ за 1 млн токенов и признак «цена известна»."""
    prices = dict(DEFAULT_PRICES)
    prices["gpt-4.1-mini"] = (
        _float_env("LUCY_PRICE_INPUT_PER_M", 0.40),
        _float_env("LUCY_PRICE_OUTPUT_PER_M", 1.60),
    )
    try:
        extra = json.loads(os.environ.get("LUCY_MODEL_PRICES", "") or "{}")
        for name, pair in extra.items():
            prices[str(name)] = (float(pair[0]), float(pair[1]))
    except (ValueError, TypeError, IndexError, AttributeError):
        print("LUCY_MODEL_PRICES: не удалось прочитать — использую встроенные цены")
    if model in prices:
        return prices[model], True
    return UNKNOWN_PRICE, False


def token_cost(model, input_tokens, output_tokens):
    (price_in, price_out), _ = model_price(model)
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def _now():
    return datetime.now(timezone.utc)


def _stamp():
    return _now().isoformat(timespec="seconds")


def _month():
    return _now().strftime("%Y-%m")


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=DB_TIMEOUT, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout = {DB_TIMEOUT * 1000}")
    return conn


def init_tables(db_path):
    conn = _connect(db_path)
    try:
        _create(conn)
    finally:
        conn.close()


def _create(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_spend (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            kind TEXT NOT NULL,
            model TEXT,
            user_id INTEGER,
            ref TEXT,
            status TEXT NOT NULL,
            reserved_usd REAL DEFAULT 0,
            cost_usd REAL DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            units INTEGER DEFAULT 0,
            error TEXT,
            created_at TEXT NOT NULL,
            finished_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_budget_alerts (
            month TEXT NOT NULL,
            threshold INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (month, threshold)
        )
        """
    )


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _expire_stale(conn):
    limit = (_now() - timedelta(minutes=STALE_RESERVATION_MINUTES)).isoformat(timespec="seconds")
    conn.execute(
        """
        UPDATE ai_spend
        SET status = 'unknown', cost_usd = reserved_usd, reserved_usd = 0,
            error = COALESCE(error, 'резерв не завершён — процесс прервался'), finished_at = ?
        WHERE status = 'reserved' AND created_at < ?
        """,
        (_stamp(), limit),
    )


_SPENT_SQL = (
    "SELECT COALESCE(SUM(CASE WHEN status IN ('done', 'unknown') THEN cost_usd ELSE 0 END), 0) "
    "+ COALESCE(SUM(CASE WHEN status = 'reserved' THEN reserved_usd ELSE 0 END), 0) "
    "FROM {table} WHERE created_at LIKE ?"
)


def _month_by_source(conn):
    prefix = _month() + "%"
    sources = {}
    for name, table in (("ИИ и поиск", "ai_spend"),
                        ("атрибуция по фото", "visual_ai_calls"),
                        ("досье /article", "library_article_calls")):
        if _table_exists(conn, table):
            sources[name] = conn.execute(_SPENT_SQL.format(table=table), (prefix,)).fetchone()[0]
    return sources


def month_by_source(db_path):
    """Расходы за месяц по журналам (оплачено + возможно оплачено + в работе)."""
    # Только чтение: вызывается и внутри чужих транзакций (резерв атрибуции).
    conn = _connect(db_path)
    try:
        return _month_by_source(conn)
    finally:
        conn.close()


def month_total(db_path):
    return sum(month_by_source(db_path).values())


def check(db_path, estimate=0.0):
    """Бросает BudgetExceeded, если оценка не помещается в общий бюджет."""
    total = month_total(db_path)
    if total + estimate > budget_month():
        raise BudgetExceeded(
            f"общий месячный бюджет ИИ {budget_month():.2f} $ (потрачено {total:.2f} $)"
        )


def reserve(db_path, *, provider, kind, model=None, estimate, user_id=None, ref=None):
    """Атомарно проверяет общий бюджет и резервирует оценку. Возвращает id."""
    conn = _connect(db_path)
    try:
        _create(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            _expire_stale(conn)
            total = sum(_month_by_source(conn).values())
            if total + estimate > budget_month():
                raise BudgetExceeded(
                    f"общий месячный бюджет ИИ {budget_month():.2f} $ (потрачено {total:.2f} $)"
                )
            cursor = conn.execute(
                """
                INSERT INTO ai_spend (provider, kind, model, user_id, ref, status,
                                      reserved_usd, created_at)
                VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?)
                """,
                (provider, kind, model, user_id, ref, estimate, _stamp()),
            )
            conn.execute("COMMIT")
            return cursor.lastrowid
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def settle(db_path, call_id, cost, input_tokens=0, output_tokens=0, units=0):
    """Ответ получен: резерв заменяется фактической стоимостью."""
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            UPDATE ai_spend
            SET status = 'done', cost_usd = ?, reserved_usd = 0, input_tokens = ?,
                output_tokens = ?, units = ?, finished_at = ?
            WHERE id = ?
            """,
            (cost, input_tokens, output_tokens, units, _stamp(), call_id),
        )
    finally:
        conn.close()
    _alert_if_needed(db_path)
    return cost


def fail(db_path, call_id, error, response_received):
    """Ошибка. Без ответа (таймаут, обрыв) оценка остаётся возможным расходом."""
    conn = _connect(db_path)
    try:
        if response_received:
            conn.execute(
                "UPDATE ai_spend SET status = 'failed', reserved_usd = 0, cost_usd = 0, "
                "error = ?, finished_at = ? WHERE id = ?",
                (str(error)[:300], _stamp(), call_id),
            )
        else:
            conn.execute(
                "UPDATE ai_spend SET status = 'unknown', cost_usd = reserved_usd, reserved_usd = 0, "
                "error = ?, finished_at = ? WHERE id = ?",
                (str(error)[:300], _stamp(), call_id),
            )
    finally:
        conn.close()
    _alert_if_needed(db_path)


def record(db_path, *, provider, kind, model=None, cost, input_tokens=0, output_tokens=0,
           units=0, user_id=None, ref=None):
    """Запись уже состоявшегося вызова (короткие ответы без резерва)."""
    conn = _connect(db_path)
    try:
        _create(conn)
        conn.execute(
            """
            INSERT INTO ai_spend (provider, kind, model, user_id, ref, status, cost_usd,
                                  input_tokens, output_tokens, units, created_at, finished_at)
            VALUES (?, ?, ?, ?, ?, 'done', ?, ?, ?, ?, ?, ?)
            """,
            (provider, kind, model, user_id, ref, cost, input_tokens, output_tokens,
             units, _stamp(), _stamp()),
        )
    finally:
        conn.close()
    _alert_if_needed(db_path)


def _alert_if_needed(db_path):
    """Одно сообщение на каждый порог 50/80/100 % за месяц."""
    try:
        budget = budget_month()
        if budget <= 0:
            return
        total = month_total(db_path)
        percent = total / budget * 100
        conn = _connect(db_path)
        try:
            new = []
            for threshold in ALERT_THRESHOLDS:
                if percent < threshold:
                    continue
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO ai_budget_alerts (month, threshold, created_at) "
                    "VALUES (?, ?, ?)",
                    (_month(), threshold, _stamp()),
                )
                if cursor.rowcount:
                    new.append(threshold)
        finally:
            conn.close()
        if new and notifier is not None:
            top = max(new)
            text = (
                f"💵 Люся: потрачено {total:.2f} $ из {budget:.2f} $ месячного бюджета ИИ "
                f"({top} %)."
            )
            if top >= 100:
                text += " Платные запросы остановлены до следующего месяца."
            notifier(text)
    except Exception as error:  # предупреждение не должно ломать ответ
        print(f"Budget alert failed: {type(error).__name__}: {error}")


def summary_lines(db_path):
    """Строки для /vstats: общий бюджет и расходы по журналам."""
    sources = month_by_source(db_path)
    total = sum(sources.values())
    lines = [f"💵 Общий бюджет ИИ за месяц: {total:.4f} $ из {budget_month():.2f} $"]
    for name, value in sources.items():
        lines.append(f"• {name}: {value:.4f} $")
    return lines
