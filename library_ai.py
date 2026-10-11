"""Русскоязычные ответы по книгам библиотеки (gpt-4.1-mini).

Как это работает:
1. Обычный поиск (library_search) находит лучшие содержательные страницы.
2. Из них вырезаются несколько коротких фрагментов — точный текст после OCR.
3. В OpenAI уходят только эти фрагменты (не книга целиком).
4. ИИ пишет ответ по-русски, ссылаясь на фрагменты как [1], [2].
5. Список источников (книга, PDF-страница) бот подставляет сам из базы —
   ИИ не может придумать источник или страницу.
6. Ответ и фрагменты сохраняются в library_ai_answers: оригиналы
   доступны для проверки, одинаковые запросы не оплачиваются повторно.

Это отдельный режим — не обычный чат с Люсей. Свой выключатель
(LUCY_LIBRARY_AI_ENABLED) и свои лимиты. Страницы и PDF не меняются.
"""

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import library_search
import lucy_ai

MAX_PAGES = 3
MAX_PASSAGES_PER_PAGE = 2
MAX_FRAGMENTS = 4
MAX_FRAGMENT_CHARS = 1500
MAX_CONTEXT_CHARS = 6000
MAX_QUERY_CHARS = 200
MAX_ANSWER_TOKENS = 700
CACHE_DAYS = 7
# Страницы с такой оценкой качества (оглавление, копирайт, мусор)
# не отправляются в ИИ, если есть страницы получше.
MIN_PAGE_QUALITY = 0.3
# Страница берётся, только если она хотя бы на треть так же релевантна,
# как лучшая найденная.
MIN_RELATIVE_SCORE = 0.3

SYSTEM_PROMPT = """Ты — Люся, исследовательница библиотеки OH MY GOD VINTAGE School. Ты отвечаешь на вопрос участницы ТОЛЬКО по фрагментам книг, которые тебе дали. Фрагменты пронумерованы: [1], [2] и т.д. Это текст страниц после распознавания (OCR), он может быть на английском, итальянском, немецком, французском, чешском, польском или русском и содержать опечатки.

Правила — без исключений:
1. Используй только сведения из фрагментов. Не добавляй ничего из своих знаний: ни фактов, ни дат, ни имён, ни клейм, ни производителей, ни атрибуций.
2. После каждого утверждения ставь номер фрагмента, на котором оно основано: [1], [2]. Номеров, которых нет во фрагментах, не бывает.
3. Не пиши названия книг, авторов и номера страниц — их бот добавит сам из базы.
4. Не выдавай перевод за цитату. Если приводишь слова книги, пиши: «в книге сказано (перевод): …» и ставь номер фрагмента. Кавычки-цитаты только для коротких фраз на языке оригинала.
5. Если во фрагментах нет ответа или его мало — прямо скажи: «В найденных фрагментах об этом недостаточно сведений» и коротко перескажи, что там всё-таки есть по теме. Не догадывайся.
6. Если фрагменты противоречат друг другу или текст OCR неразборчив — скажи об этом.

Стиль: по-русски, просто и по делу, на «ты». 80–200 слов. Простой текст без Markdown: без звёздочек, решёток и таблиц."""

ANSWER_HEADER = "📚 ОТВЕТ ПО КНИГАМ БИБЛИОТЕКИ\n\n"
ANSWER_NOTE = (
    "\n\n💬 Пересказ и перевод сделал ИИ — только по найденным фрагментам. "
    "Точный текст книги — по кнопкам «📄 Оригинал»."
)
TELEGRAM_LIMIT = 3900

_CITATION = re.compile(r"\[(\d{1,2})\]")
_CITATION_WITH_SPACE = re.compile(r"\s*\[(\d{1,2})\]")


# --- настройки ---

def _int_env(name, default):
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


def enabled():
    value = os.environ.get("LUCY_LIBRARY_AI_ENABLED", "1").strip().lower()
    return value not in ("0", "false", "no", "off")


def daily_limit():
    return _int_env("LUCY_LIBRARY_AI_DAILY_LIMIT", 10)


def daily_total_limit():
    return _int_env("LUCY_LIBRARY_AI_DAILY_TOTAL", 200)


# --- база ---

def init_tables(db_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS library_ai_usage (
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                questions INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, day)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS library_ai_answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query_key TEXT NOT NULL,
                query TEXT NOT NULL,
                answer TEXT NOT NULL,
                fragments_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_library_ai_answers_key "
            "ON library_ai_answers (query_key)"
        )
        conn.commit()
    finally:
        conn.close()


def _today():
    return lucy_ai._today()


def used_today(db_path, user_id):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT questions FROM library_ai_usage WHERE user_id = ? AND day = ?",
            (user_id, _today()),
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def used_today_total(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT COALESCE(SUM(questions), 0) FROM library_ai_usage WHERE day = ?",
            (_today(),),
        ).fetchone()[0]
    finally:
        conn.close()


def record_question(db_path, user_id):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO library_ai_usage (user_id, day, questions)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, day) DO UPDATE SET questions = questions + 1
            """,
            (user_id, _today()),
        )
        conn.commit()
    finally:
        conn.close()


def query_key(db_path, query):
    """Ключ кэша: нормализованный запрос + состояние библиотеки.

    Когда в библиотеку добавляются страницы, ключ меняется — и старый
    ответ не выдаётся вместо нового.
    """
    normalized = re.sub(r"[^\w\s]", " ", query.lower().replace("ё", "е"))
    normalized = " ".join(normalized.split())
    conn = sqlite3.connect(db_path)
    try:
        version = conn.execute(
            "SELECT COALESCE(MAX(id), 0), COUNT(*) FROM library_pages"
        ).fetchone()
    finally:
        conn.close()
    return f"{normalized}|{version[0]}:{version[1]}"


def find_cached(db_path, key):
    since = (datetime.now(timezone.utc) - timedelta(days=CACHE_DAYS)).isoformat(timespec="seconds")
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT id, answer, fragments_json FROM library_ai_answers
            WHERE query_key = ? AND created_at >= ?
            ORDER BY id DESC LIMIT 1
            """,
            (key, since),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return row[0], row[1], json.loads(row[2])


def save_answer(db_path, key, query, answer, fragments):
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO library_ai_answers
                (query_key, query, answer, fragments_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                key,
                query[:MAX_QUERY_CHARS],
                answer,
                json.dumps(fragments, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_fragment(db_path, answer_id, number):
    """Фрагмент номер number из сохранённого ответа (для кнопки «Оригинал»)."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT fragments_json FROM library_ai_answers WHERE id = ?",
            (answer_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    for fragment in json.loads(row[0]):
        if fragment["n"] == number:
            return fragment
    return None


# --- фрагменты и запрос к ИИ ---

def select_fragments(results):
    """Из найденных страниц берёт несколько лучших фрагментов.

    Служебные страницы (оглавление, копирайт, OCR-мусор) пропускаются,
    если есть содержательные. Общий объём ограничен MAX_CONTEXT_CHARS.
    """
    good = [r for r in results if r.get("quality", 1) >= MIN_PAGE_QUALITY]
    pages = (good or results)[:MAX_PAGES]
    # Слабые совпадения рядом с сильными только засоряют ответ.
    if pages and pages[0].get("score"):
        best = pages[0]["score"]
        pages = [r for r in pages if r.get("score", best) >= best * MIN_RELATIVE_SCORE]

    fragments = []
    total = 0
    for result in pages:
        for text in library_search.passages(result, max_passages=MAX_PASSAGES_PER_PAGE):
            text = text[:MAX_FRAGMENT_CHARS]
            if len(fragments) >= MAX_FRAGMENTS or total + len(text) > MAX_CONTEXT_CHARS:
                break
            total += len(text)
            fragments.append({
                "n": len(fragments) + 1,
                "title": result["title"],
                "document_code": result.get("document_code"),
                "language": result.get("language"),
                "pdf_page": result["pdf_page"],
                "printed_page": result.get("printed_page"),
                "text": text,
            })
    return fragments


def build_messages(query, fragments):
    blocks = []
    for fragment in fragments:
        blocks.append(
            f"[{fragment['n']}] (язык: "
            f"{library_search.language_name(fragment.get('language'))})\n"
            f"{fragment['text']}"
        )
    user = (
        f"Вопрос участницы: {query[:MAX_QUERY_CHARS]}\n\n"
        "Фрагменты книг:\n\n" + "\n\n".join(blocks)
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def clean_answer(answer, fragments):
    """Убирает из ответа ссылки на несуществующие фрагменты."""
    valid = {fragment["n"] for fragment in fragments}

    def keep(match):
        return match.group(0) if int(match.group(1)) in valid else ""

    return _CITATION_WITH_SPACE.sub(keep, answer).strip()


def cited_numbers(answer, fragments):
    valid = {fragment["n"] for fragment in fragments}
    cited = []
    for match in _CITATION.finditer(answer):
        number = int(match.group(1))
        if number in valid and number not in cited:
            cited.append(number)
    return cited


def page_label(fragment):
    label = f"PDF-стр. {fragment['pdf_page']}"
    if fragment.get("printed_page"):
        label += f" (в книге стр. {fragment['printed_page']})"
    return label


def format_reply(answer, fragments):
    """Ответ + список источников, собранный из базы, а не из ответа ИИ."""
    cited = cited_numbers(answer, fragments)
    shown = [f for f in fragments if f["n"] in cited] or fragments
    sources = "\n".join(
        f"[{f['n']}] {f['title']} — {page_label(f)}" for f in shown
    )
    tail = "\n\n📖 Источники:\n" + sources + ANSWER_NOTE
    room = TELEGRAM_LIMIT - len(ANSWER_HEADER) - len(tail)
    if len(answer) > room:
        answer = answer[:room].rstrip() + "…"
    return ANSWER_HEADER + answer + tail, [f["n"] for f in shown]


def ask(fragments, query):
    """Синхронный вызов ИИ. Бросает lucy_ai.AIUnavailable при проблемах."""
    if not enabled():
        raise lucy_ai.AIUnavailable("disabled")
    answer = lucy_ai.ask_openai(
        build_messages(query, fragments),
        max_tokens=MAX_ANSWER_TOKENS,
        temperature=0.1,
        check_enabled=False,
    )
    return clean_answer(answer, fragments)
