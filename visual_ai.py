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
import sqlite3
from datetime import datetime, timezone

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


# --- учёт ---

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
        conn.commit()
    finally:
        conn.close()


def _now():
    return datetime.now(timezone.utc)


def spent(db_path, period):
    """Расходы за текущий день ('day') или месяц ('month') по UTC."""
    prefix = _now().strftime("%Y-%m-%d" if period == "day" else "%Y-%m")
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM visual_ai_calls WHERE created_at LIKE ?",
            (prefix + "%",),
        ).fetchone()[0]
    finally:
        conn.close()


def estimate_cost(images, text_chars, max_output_tokens):
    input_tokens = images * IMAGE_TOKENS_ESTIMATE + text_chars / CHARS_PER_TOKEN
    return cost(input_tokens, max_output_tokens)


def check_budget(db_path, estimate):
    """Бросает BudgetExceeded, если вызов не помещается в бюджет."""
    day, month = spent(db_path, "day"), spent(db_path, "month")
    if day + estimate > budget_day():
        raise BudgetExceeded(f"дневной бюджет {budget_day():.2f} $ (потрачено {day:.4f} $)")
    if month + estimate > budget_month():
        raise BudgetExceeded(f"месячный бюджет {budget_month():.2f} $ (потрачено {month:.4f} $)")


def record_call(db_path, attribution_id, user_id, kind, images, input_tokens, output_tokens, ok=True):
    value = cost(input_tokens, output_tokens)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO visual_ai_calls (
                attribution_id, user_id, kind, model, images,
                input_tokens, output_tokens, cost_usd, ok, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (attribution_id, user_id, kind, MODEL, images, input_tokens,
             output_tokens, value, int(ok), _now().isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()
    return value


def calls_for(db_path, attribution_id):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            """
            SELECT kind, images, input_tokens, output_tokens, cost_usd, ok
            FROM visual_ai_calls WHERE attribution_id = ? ORDER BY id
            """,
            (attribution_id,),
        ).fetchall()
    finally:
        conn.close()


def stats(db_path):
    """Сводка для администратора: расходы за день/месяц, средняя стоимость."""
    now = _now()
    day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    conn = sqlite3.connect(db_path)
    try:
        def one(sql, params):
            return conn.execute(sql, params).fetchone()

        day_cost, day_calls = one(
            "SELECT COALESCE(SUM(cost_usd),0), COUNT(*) FROM visual_ai_calls WHERE created_at LIKE ?",
            (day + "%",),
        )
        month_cost, month_calls, month_in, month_out = one(
            "SELECT COALESCE(SUM(cost_usd),0), COUNT(*), COALESCE(SUM(input_tokens),0), "
            "COALESCE(SUM(output_tokens),0) FROM visual_ai_calls WHERE created_at LIKE ?",
            (month + "%",),
        )
        per_attribution = conn.execute(
            """
            SELECT attribution_id, SUM(cost_usd), COUNT(*)
            FROM visual_ai_calls
            WHERE created_at LIKE ? AND attribution_id IS NOT NULL
            GROUP BY attribution_id
            """,
            (month + "%",),
        ).fetchall()
        by_kind = conn.execute(
            """
            SELECT kind, COUNT(*), COALESCE(SUM(cost_usd),0),
                   COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0)
            FROM visual_ai_calls WHERE created_at LIKE ?
            GROUP BY kind ORDER BY kind
            """,
            (month + "%",),
        ).fetchall()
    finally:
        conn.close()
    count = len(per_attribution)
    return {
        "day_cost": day_cost, "day_calls": day_calls,
        "month_cost": month_cost, "month_calls": month_calls,
        "month_input": month_in, "month_output": month_out,
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


def call_json(db_path, *, kind, system, content, images, max_tokens,
              attribution_id=None, user_id=None):
    """Один платный вызов с проверкой бюджета до и записью затрат после.

    content — список частей сообщения (текст и картинки).
    Возвращает dict из JSON-ответа модели.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise VisualAIUnavailable("no_key")

    text_chars = len(system) + sum(len(p.get("text", "")) for p in content if p.get("type") == "text")
    check_budget(db_path, estimate_cost(images, text_chars, max_tokens))

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
    except openai.OpenAIError as error:
        code = getattr(error, "code", None)
        print(f"Visual AI error: {type(error).__name__} code={code}")
        record_call(db_path, attribution_id, user_id, kind, images, 0, 0, ok=False)
        raise VisualAIUnavailable("quota" if code == "insufficient_quota" else "error") from None

    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "prompt_tokens", 0) or 0
    output_tokens = getattr(usage, "completion_tokens", 0) or 0
    record_call(db_path, attribution_id, user_id, kind, images, input_tokens, output_tokens)

    raw = (response.choices[0].message.content or "").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("Visual AI returned non-JSON")
        raise VisualAIUnavailable("bad_json") from None
    if not isinstance(data, dict):
        raise VisualAIUnavailable("bad_json")
    return data
