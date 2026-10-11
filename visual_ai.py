"""Платные вызовы ИИ для визуальной атрибуции — с бюджетом и учётом затрат.

Каждый вызов:
1. до отправки оценивается по размеру (картинки + текст) и сверяется
   с дневным и месячным бюджетом в долларах;
2. после ответа записывается в visual_ai_calls с фактическими токенами
   и стоимостью.

Настройки (переменные Railway):
- LUCY_VISUAL_MODE: off | admin | all (по умолчанию all)
- LUCY_VISUAL_FREE_PER_MONTH: бесплатных ИИ-атрибуций на человека (3)
- LUCY_VISUAL_BUDGET_DAY_USD (1.00), LUCY_VISUAL_BUDGET_MONTH_USD (10.00)
- LUCY_PRICE_INPUT_PER_M (0.40), LUCY_PRICE_OUTPUT_PER_M (1.60) — цены gpt-4.1-mini
"""

import base64
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

MODEL = "gpt-4.1-mini"
IMAGE_TOKENS_ESTIMATE = 1700   # ≈ 1024×1024 у gpt-4.1-mini (1024 патча × 1,62)
CHARS_PER_TOKEN = 3.0


class BudgetExceeded(Exception):
    pass


class VisualAIUnavailable(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# --- настройки ---

def _float_env(name, default):
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except ValueError:
        return default


def _int_env(name, default):
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


def mode():
    value = os.environ.get("LUCY_VISUAL_MODE", "all").strip().lower()
    return value if value in ("off", "admin", "all") else "all"


def free_per_month():
    return _int_env("LUCY_VISUAL_FREE_PER_MONTH", 3)


def budget_day():
    return _float_env("LUCY_VISUAL_BUDGET_DAY_USD", 1.0)


def budget_month():
    return _float_env("LUCY_VISUAL_BUDGET_MONTH_USD", 10.0)


def price_input():
    return _float_env("LUCY_PRICE_INPUT_PER_M", 0.40)


def price_output():
    return _float_env("LUCY_PRICE_OUTPUT_PER_M", 1.60)


def cost(input_tokens, output_tokens):
    return (input_tokens * price_input() + output_tokens * price_output()) / 1_000_000


# --- учёт и резервирование ---
#
# Каждый платный вызов проходит три состояния в visual_ai_calls:
#   reserved — до запроса атомарно зарезервирована оценка стоимости
#              (reserved_usd); проверка бюджета учитывает все такие резервы;
#   done     — ответ получен: резерв заменён фактической стоимостью;
#   failed   — OpenAI вернул ошибку (запрос не выполнен): резерв снят,
#              стоимость 0, текст ошибки сохранён;
#   unknown  — ответа нет (таймаут, обрыв): запрос мог быть оплачен,
#              оценка остаётся в «возможных расходах» и в бюджете.
# Резерв, который висит дольше STALE_RESERVATION_MINUTES (процесс упал),
# переводится в unknown — так он продолжает учитываться в бюджете.

STALE_RESERVATION_MINUTES = 30
DB_TIMEOUT = 30


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=DB_TIMEOUT, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout = {DB_TIMEOUT * 1000}")
    return conn


def init_tables(db_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS visual_ai_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attribution_id INTEGER,
                user_id INTEGER,
                kind TEXT NOT NULL,
                model TEXT NOT NULL,
                images INTEGER DEFAULT 0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0,
                ok INTEGER DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        existing = {row[1] for row in conn.execute("PRAGMA table_info(visual_ai_calls)")}
        for column, definition in (
            ("status", "TEXT DEFAULT 'done'"),
            ("reserved_usd", "REAL DEFAULT 0"),
            ("error", "TEXT"),
            ("finished_at", "TEXT"),
            ("request_info", "TEXT"),
        ):
            if column not in existing:
                conn.execute(f"ALTER TABLE visual_ai_calls ADD COLUMN {column} {definition}")
        conn.commit()
    finally:
        conn.close()


def _now():
    return datetime.now(timezone.utc)


def _period_prefix(period):
    return _now().strftime("%Y-%m-%d" if period == "day" else "%Y-%m")


# Расходы, которые точно или возможно оплачены (done + unknown).
_CHARGED = "COALESCE(SUM(CASE WHEN status IN ('done', 'unknown') THEN cost_usd ELSE 0 END), 0)"
# Ещё не завершённые запросы.
_RESERVED = "COALESCE(SUM(CASE WHEN status = 'reserved' THEN reserved_usd ELSE 0 END), 0)"


def _totals(conn, period):
    return conn.execute(
        f"SELECT {_CHARGED}, {_RESERVED} FROM visual_ai_calls WHERE created_at LIKE ?",
        (_period_prefix(period) + "%",),
    ).fetchone()


def spent(db_path, period):
    """Оплаченные и возможно оплаченные расходы за день ('day') или месяц."""
    conn = _connect(db_path)
    try:
        return _totals(conn, period)[0]
    finally:
        conn.close()


def reserved(db_path, period):
    conn = _connect(db_path)
    try:
        return _totals(conn, period)[1]
    finally:
        conn.close()


def estimate_cost(images, text_chars, max_output_tokens):
    input_tokens = images * IMAGE_TOKENS_ESTIMATE + text_chars / CHARS_PER_TOKEN
    return cost(input_tokens, max_output_tokens)


def _expire_stale(conn):
    limit = (_now() - timedelta(minutes=STALE_RESERVATION_MINUTES)).isoformat(timespec="seconds")
    conn.execute(
        """
        UPDATE visual_ai_calls
        SET status = 'unknown', cost_usd = reserved_usd, reserved_usd = 0,
            error = COALESCE(error, 'резерв не завершён — процесс прервался'),
            finished_at = ?
        WHERE status = 'reserved' AND created_at < ?
        """,
        (_now().isoformat(timespec="seconds"), limit),
    )


def reserve(db_path, *, kind, images, estimate, attribution_id=None, user_id=None, request_info=None):
    """Атомарно резервирует оценку стоимости вызова. Возвращает id резерва.

    BEGIN IMMEDIATE блокирует запись в базу для других соединений на время
    проверки, поэтому два одновременных запроса не могут оба пройти
    проверку и вместе превысить бюджет.
    """
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _expire_stale(conn)
            day_charged, day_reserved = _totals(conn, "day")
            month_charged, month_reserved = _totals(conn, "month")
            if day_charged + day_reserved + estimate > budget_day():
                raise BudgetExceeded(
                    f"дневной бюджет {budget_day():.2f} $ (потрачено {day_charged:.4f} $, "
                    f"в работе {day_reserved:.4f} $)"
                )
            if month_charged + month_reserved + estimate > budget_month():
                raise BudgetExceeded(
                    f"месячный бюджет {budget_month():.2f} $ (потрачено {month_charged:.4f} $, "
                    f"в работе {month_reserved:.4f} $)"
                )
            cursor = conn.execute(
                """
                INSERT INTO visual_ai_calls (
                    attribution_id, user_id, kind, model, images, status,
                    reserved_usd, cost_usd, ok, created_at, request_info
                ) VALUES (?, ?, ?, ?, ?, 'reserved', ?, 0, 0, ?, ?)
                """,
                (attribution_id, user_id, kind, MODEL, images, estimate,
                 _now().isoformat(timespec="seconds"), request_info),
            )
            conn.execute("COMMIT")
            return cursor.lastrowid
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def settle(db_path, call_id, input_tokens, output_tokens):
    """Ответ получен: резерв заменяется фактической стоимостью."""
    value = cost(input_tokens, output_tokens)
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            UPDATE visual_ai_calls
            SET status = 'done', ok = 1, input_tokens = ?, output_tokens = ?,
                cost_usd = ?, reserved_usd = 0, finished_at = ?
            WHERE id = ?
            """,
            (input_tokens, output_tokens, value, _now().isoformat(timespec="seconds"), call_id),
        )
    finally:
        conn.close()
    return value


def release(db_path, call_id, error, response_received):
    """Ошибка: резерв снимается. Если ответа не было (таймаут, обрыв),
    оценка остаётся как возможный расход — запрос мог быть оплачен."""
    conn = _connect(db_path)
    try:
        if response_received:
            conn.execute(
                """
                UPDATE visual_ai_calls
                SET status = 'failed', ok = 0, cost_usd = 0, reserved_usd = 0,
                    error = ?, finished_at = ?
                WHERE id = ?
                """,
                (error[:300], _now().isoformat(timespec="seconds"), call_id),
            )
        else:
            conn.execute(
                """
                UPDATE visual_ai_calls
                SET status = 'unknown', ok = 0, cost_usd = reserved_usd, reserved_usd = 0,
                    error = ?, finished_at = ?
                WHERE id = ?
                """,
                (error[:300], _now().isoformat(timespec="seconds"), call_id),
            )
    finally:
        conn.close()


def check_budget(db_path, estimate):
    """Только проверка (без резерва) — для подсказок. Для вызовов — reserve()."""
    conn = _connect(db_path)
    try:
        day_charged, day_reserved = _totals(conn, "day")
        month_charged, month_reserved = _totals(conn, "month")
    finally:
        conn.close()
    if day_charged + day_reserved + estimate > budget_day():
        raise BudgetExceeded(f"дневной бюджет {budget_day():.2f} $")
    if month_charged + month_reserved + estimate > budget_month():
        raise BudgetExceeded(f"месячный бюджет {budget_month():.2f} $")


def calls_for(db_path, attribution_id):
    conn = _connect(db_path)
    try:
        return conn.execute(
            """
            SELECT kind, images, input_tokens, output_tokens, cost_usd, ok, status,
                   error, request_info
            FROM visual_ai_calls WHERE attribution_id = ? ORDER BY id
            """,
            (attribution_id,),
        ).fetchall()
    finally:
        conn.close()


def stats(db_path):
    """Сводка для администратора: расходы, резервы, средняя стоимость."""
    day, month = _period_prefix("day"), _period_prefix("month")
    conn = _connect(db_path)
    try:
        _expire_stale(conn)

        def totals(prefix):
            return conn.execute(
                f"""
                SELECT
                    COALESCE(SUM(CASE WHEN status = 'done' THEN cost_usd ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN status = 'unknown' THEN cost_usd ELSE 0 END), 0),
                    {_RESERVED},
                    COUNT(*),
                    COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN status = 'unknown' THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN status = 'reserved' THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(input_tokens), 0),
                    COALESCE(SUM(output_tokens), 0)
                FROM visual_ai_calls WHERE created_at LIKE ?
                """,
                (prefix + "%",),
            ).fetchone()

        day_row, month_row = totals(day), totals(month)
        per_attribution = conn.execute(
            """
            SELECT attribution_id,
                   SUM(CASE WHEN status IN ('done', 'unknown') THEN cost_usd ELSE 0 END),
                   COUNT(*)
            FROM visual_ai_calls
            WHERE created_at LIKE ? AND attribution_id IS NOT NULL
              AND status IN ('done', 'unknown', 'failed')
            GROUP BY attribution_id
            HAVING SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) > 0
            """,
            (month + "%",),
        ).fetchall()
        by_kind = conn.execute(
            """
            SELECT kind, COUNT(*),
                   COALESCE(SUM(CASE WHEN status IN ('done', 'unknown') THEN cost_usd ELSE 0 END), 0),
                   COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0)
            FROM visual_ai_calls WHERE created_at LIKE ? AND status != 'reserved'
            GROUP BY kind ORDER BY kind
            """,
            (month + "%",),
        ).fetchall()
    finally:
        conn.close()

    count = len(per_attribution)
    return {
        "day_cost": day_row[0], "day_possible": day_row[1], "day_reserved": day_row[2],
        "day_calls": day_row[3], "day_failed": day_row[4],
        "month_cost": month_row[0], "month_possible": month_row[1], "month_reserved": month_row[2],
        "month_calls": month_row[3], "month_failed": month_row[4], "month_unknown": month_row[5],
        "month_in_flight": month_row[6],
        "month_input": month_row[7], "month_output": month_row[8],
        "attributions": count,
        "avg_cost": (sum(row[1] for row in per_attribution) / count) if count else 0.0,
        "avg_calls": (sum(row[2] for row in per_attribution) / count) if count else 0.0,
        "by_kind": by_kind,
        "budget_day": budget_day(), "budget_month": budget_month(),
    }


# --- вызов модели ---

def image_part(jpeg_bytes, detail="high"):
    encoded = base64.b64encode(jpeg_bytes).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{encoded}", "detail": detail},
    }


_SECRET = re.compile(r"(sk-[A-Za-z0-9_\-]{4,}|data:image/[^\s'\"]{20,}|[A-Za-z0-9+/]{80,}={0,2})")


def scrub(text, limit=240):
    """Убирает из текста ошибки ключи и куски изображений."""
    return _SECRET.sub("[скрыто]", str(text or ""))[:limit]


def describe_error(error):
    """Безопасная диагностика ошибки OpenAI: тип, HTTP-статус, код, параметр."""
    parts = [type(error).__name__]
    status = getattr(error, "status_code", None)
    if status is not None:
        parts.append(f"HTTP {status}")
    for attribute in ("type", "code", "param"):
        value = getattr(error, attribute, None)
        if value:
            parts.append(f"{attribute}={value}")
    message = getattr(error, "message", None) or (error.args[0] if error.args else "")
    if message:
        parts.append(scrub(message))
    return " · ".join(parts)


def request_summary(kind, images, content, system, max_tokens):
    """Размер и формат запроса — без содержимого."""
    payload = len(system) + sum(
        len(p.get("text", "")) + len((p.get("image_url") or {}).get("url", "")) for p in content
    )
    return (
        f"kind={kind} model={MODEL} images={images} "
        f"payload_kb={payload // 1024} max_tokens={max_tokens} format=json_object"
    )


def _no_response_errors(openai):
    """Ошибки, при которых ответа от OpenAI не было (запрос мог быть оплачен)."""
    names = ("APITimeoutError", "APIConnectionError")
    return tuple(getattr(openai, name) for name in names if isinstance(getattr(openai, name, None), type))


def call_json(db_path, *, kind, system, content, images, max_tokens,
              attribution_id=None, user_id=None):
    """Один платный вызов: резерв бюджета → запрос → фактическая стоимость.

    content — список частей сообщения (текст и картинки).
    Возвращает dict из JSON-ответа модели.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise VisualAIUnavailable("no_key")

    # JSON-режим OpenAI требует слово «JSON» в сообщениях, иначе ошибка 400
    # (так упало сравнение в первом тесте на Railway). Проверяем сами.
    texts_lower = (system + " ".join(p.get("text", "") for p in content)).lower()
    if "json" not in texts_lower:
        system = system + "\nRespond with a single JSON object."

    text_chars = len(system) + sum(len(p.get("text", "")) for p in content if p.get("type") == "text")
    estimate = estimate_cost(images, text_chars, max_tokens)
    call_id = reserve(
        db_path, kind=kind, images=images, estimate=estimate,
        attribution_id=attribution_id, user_id=user_id,
        request_info=request_summary(kind, images, content, system, max_tokens),
    )

    import openai

    try:
        client = openai.OpenAI(api_key=api_key, timeout=60.0, max_retries=1)
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            max_completion_tokens=max_tokens,
            temperature=0.1,
            response_format={"type": "json_object"},
        )
    except _no_response_errors(openai) as error:
        details = describe_error(error) + " · ответ не получен"
        print(f"Visual AI no response: {details}")
        release(db_path, call_id, details, response_received=False)
        raise VisualAIUnavailable("error") from None
    except openai.OpenAIError as error:
        code = getattr(error, "code", None)
        details = describe_error(error)
        print(f"Visual AI error: {details}")
        release(db_path, call_id, details, response_received=True)
        raise VisualAIUnavailable("quota" if code == "insufficient_quota" else "error") from None
    except BaseException as error:
        # Неожиданный сбой до ответа — считаем, что ответа не было.
        release(db_path, call_id, f"{type(error).__name__}", response_received=False)
        raise

    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "prompt_tokens", 0) or 0
    output_tokens = getattr(usage, "completion_tokens", 0) or 0
    settle(db_path, call_id, input_tokens, output_tokens)

    raw = (response.choices[0].message.content or "").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("Visual AI returned non-JSON")
        raise VisualAIUnavailable("bad_json") from None
    if not isinstance(data, dict):
        raise VisualAIUnavailable("bad_json")
    return data
