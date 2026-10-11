"""Бесплатные ответы Люси на вопросы о винтажных украшениях.

Использует уже подключённый OpenAI (переменная OPENAI_API_KEY).
Защита расходов:
- дневной лимит вопросов на одного пользователя (LUCY_AI_DAILY_LIMIT, по умолчанию 15);
- общий дневной лимит на весь бот (LUCY_AI_DAILY_TOTAL, по умолчанию 300);
- короткая история диалога и ограничение длины ответа;
- выключатель LUCY_AI_ENABLED=0.

Счётчики лежат в отдельной таблице lucy_ai_usage. Существующие таблицы
(карточки, книги, библиотека) не трогаются.
"""

import os
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

AI_MODEL = "gpt-4.1-mini"
MAX_ANSWER_TOKENS = 600
MAX_QUESTION_CHARS = 800
HISTORY_EXCHANGES = 3
HISTORY_ITEM_CHARS = 1200
TELEGRAM_LIMIT = 3900
TIMEZONE = ZoneInfo("Europe/Berlin")

AI_LABEL = (
    "\n\n💬 Это ответ ИИ — общие сведения, а не цитата из книг "
    "нашей библиотеки. Клейма и атрибуцию всегда перепроверяй."
)

SYSTEM_PROMPT = """Ты — Люся, помощница OH MY GOD VINTAGE School, школы о винтажных и антикварных украшениях. Отвечаешь участницам в Telegram.

О чём отвечаешь: винтажные и антикварные украшения и бижутерия — материалы (бакелит, гагат, целлулоид, стразы, позолота, серебро и т.п.), эпохи и стили, техники изготовления, конструкции (застёжки, булавки, крепления), бренды и дизайнеры, как в целом читать клейма, как ухаживать и хранить, как фотографировать находки.
Если вопрос не про украшения — вежливо и коротко скажи, что ты помогаешь только с винтажными украшениями.

Главное правило — честность:
- Не выдумывай факты, даты, клейма, производителей, номера патентов, названия книг, страницы, ссылки и цитаты.
- Различай: «известно» (общепринятые сведения), «вероятно / часто встречается» (предположение) и «неизвестно».
- Если не уверена — так и скажи. Лучше «не знаю точно», чем красивая выдумка.
- Не определяй производителя, возраст или подлинность конкретного украшения по описанию. Можешь объяснить, на какие признаки смотреть, но вывод — только после проверки по источникам.
- Не называй цены и не оценивай стоимость конкретных предметов.
- Ты не видишь фотографии в этом режиме и не ищешь по книгам библиотеки. Не делай вид, что ты что-то проверила по книгам или в интернете.
- Если вопрос про конкретную находку, предложи сохранить её в «🗃️ Моя шкатулка» и обсудить с девочками в группе школы.

Стиль: только русский язык, на «ты», тепло и с лёгким юмором, но по делу. Коротко: обычно до 150–200 слов. Простой текст без Markdown: без звёздочек, решёток и таблиц. Можно эмодзи, в меру."""

_RATE_LIMIT_CODES = {"insufficient_quota", "billing_hard_limit_reached"}


class AIUnavailable(Exception):
    """OpenAI не ответил. reason: 'disabled', 'no_key', 'quota', 'error'."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _today():
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d")


def init_ai_usage_table(db_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lucy_ai_usage (
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                questions INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, day)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def daily_limit():
    return _int_env("LUCY_AI_DAILY_LIMIT", 15)


def daily_total_limit():
    return _int_env("LUCY_AI_DAILY_TOTAL", 300)


def _int_env(name, default):
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


def ai_enabled():
    return os.environ.get("LUCY_AI_ENABLED", "1").strip() not in ("0", "false", "no", "off")


def used_today(db_path, user_id):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT questions FROM lucy_ai_usage WHERE user_id = ? AND day = ?",
            (user_id, _today()),
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def used_today_total(db_path):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(questions), 0) FROM lucy_ai_usage WHERE day = ?",
            (_today(),),
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def record_question(db_path, user_id):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO lucy_ai_usage (user_id, day, questions)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, day) DO UPDATE SET questions = questions + 1
            """,
            (user_id, _today()),
        )
        conn.commit()
    finally:
        conn.close()


def build_messages(history, question):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for item in history[-HISTORY_EXCHANGES * 2:]:
        messages.append({
            "role": item["role"],
            "content": item["content"][:HISTORY_ITEM_CHARS],
        })
    messages.append({"role": "user", "content": question[:MAX_QUESTION_CHARS]})
    return messages


def ask_openai(messages):
    """Синхронный вызов OpenAI. Возвращает текст ответа или бросает AIUnavailable."""
    if not ai_enabled():
        raise AIUnavailable("disabled")

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise AIUnavailable("no_key")

    import openai

    client = openai.OpenAI(api_key=api_key, timeout=30.0, max_retries=1)

    started = time.monotonic()
    try:
        response = client.chat.completions.create(
            model=AI_MODEL,
            messages=messages,
            max_completion_tokens=MAX_ANSWER_TOKENS,
            temperature=0.3,
        )
    except openai.OpenAIError as error:
        code = getattr(error, "code", None)
        # В логи — только тип и код ошибки, без ключей и текста запроса.
        print(f"Lucy AI error: {type(error).__name__} code={code}")
        if code in _RATE_LIMIT_CODES:
            raise AIUnavailable("quota") from None
        raise AIUnavailable("error") from None

    answer = (response.choices[0].message.content or "").strip()
    usage = getattr(response, "usage", None)
    if usage is not None:
        print(
            f"Lucy AI ok: {time.monotonic() - started:.1f}s "
            f"in={usage.prompt_tokens} out={usage.completion_tokens}"
        )
    if not answer:
        raise AIUnavailable("error")
    return answer


def format_answer(answer):
    room = TELEGRAM_LIMIT - len(AI_LABEL)
    if len(answer) > room:
        answer = answer[:room].rstrip() + "…"
    return answer + AI_LABEL
