"""Полное исследование темы по всей библиотеке (/article, только администратор).

Как это работает:
1. Поиск (library_search) находит ВСЕ страницы с темой во всех книгах —
   без ограничения числа страниц на книгу.
2. Служебные страницы (оглавление, указатель, выходные данные, OCR-мусор)
   в ИИ не уходят, но перечисляются в отчёте о полноте.
3. Перед запуском Люся показывает план: сколько страниц, этапов, сколько
   это стоит и сколько займёт. Дороже LUCY_ARTICLE_CONFIRM_USD — только
   по кнопке «Запустить». Дороже LUCY_ARTICLE_MAX_USD — не запускается.
4. Страницы обрабатываются этапами по ~40 тыс. знаков: на каждом этапе
   gpt-4.1-mini выписывает факты по теме с номерами фрагментов [n].
   Каждый этап сохраняется в базу сразу — если Railway перезапустится,
   исследование продолжается с первого несделанного этапа (по кнопке,
   автоматически деньги не тратятся).
5. Итоговый вызов собирает из выписок досье по разделам.
6. Книги и страницы источников подставляет бот из базы — ИИ их не пишет
   и не может придумать. Ссылки на несуществующие фрагменты убираются.
7. Каждый платный вызов: резерв оценки в бюджете → запрос → фактическая
   стоимость (library_article_calls). Свой дневной и месячный бюджет.

Книги, страницы, OCR и DOC-ID не меняются.

Настройки (переменные Railway):
- LUCY_ARTICLE_MAX_PAGES (400) — сколько содержательных страниц читает ИИ
- LUCY_ARTICLE_CONFIRM_USD (0.02) — дороже этого — только по кнопке
- LUCY_ARTICLE_MAX_USD (1.00) — дороже этого исследование не запускается
- LUCY_ARTICLE_BUDGET_DAY_USD (1.00), LUCY_ARTICLE_BUDGET_MONTH_USD (5.00)
- LUCY_PRICE_INPUT_PER_M / LUCY_PRICE_OUTPUT_PER_M — общие цены gpt-4.1-mini
- LUCY_LIBRARY_AI_ENABLED — общий выключатель пересказов по книгам
"""

import asyncio
import json
import math
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import library_ai
import library_search
import lucy_ai
import visual_ai

MODEL = lucy_ai.AI_MODEL
SEARCH_LIMIT = 100000          # все найденные страницы, без отсечения по числу
MAX_CANDIDATES = 8000          # страниц-кандидатов из базы за один поиск
PAGE_CHARS = 4000              # текст одной страницы в фрагменте
PART_CHARS = 40000             # объём фрагментов на один этап
PART_MAX_TOKENS = 1500
FINAL_MAX_TOKENS = 4000
CHARS_PER_TOKEN = visual_ai.CHARS_PER_TOKEN
OVERHEAD_TOKENS = 600          # служебные токены запроса — с запасом
SECONDS_PER_CALL = 25          # для оценки времени
AI_TIMEOUT = 120.0
MAX_TOPIC_CHARS = 120
PROGRESS_EVERY = 5
STALE_RESERVATION_MINUTES = 30
DB_TIMEOUT = 30
MAX_LISTED_SERVICE_PAGES = 20

# Задачи, которые выполняются в этом процессе прямо сейчас.
# После перезапуска Railway список пуст: задачи в базе со статусом
# «running» считаются прерванными и продолжаются только по кнопке.
RUNNING = set()

PART_PROMPT = """Ты — Люся, исследовательница библиотеки OH MY GOD VINTAGE School. Это один этап большого исследования темы по книгам. Тебе дали фрагменты страниц из книг — текст после распознавания (OCR), он может быть на разных языках и с опечатками. Фрагменты пронумерованы: [12], [13] и т.д.

Выпиши по-русски ВСЕ сведения по теме исследования, которые есть в этих фрагментах: факты, даты, имена, фирмы, линии и коллекции, клейма, материалы, техники, признаки, цены, интересные подробности.

Правила — без исключений:
1. Только то, что написано во фрагментах. Ничего от себя.
2. Каждая строка начинается с «— » и заканчивается номером фрагмента: [12]. Номеров, которых нет во фрагментах, не бывает.
3. Своими словами, коротко. Не переписывай фрагменты подряд.
4. Не пиши названия книг и номера страниц.
5. Если OCR неразборчив или фрагменты противоречат друг другу — так и отметь.
6. Если по теме во фрагментах ничего нет — ответь одной строкой: НЕТ СВЕДЕНИЙ.

Без Markdown: без звёздочек, решёток и таблиц."""

DOSSIER_RULES = """Правила — без исключений:
1. Только сведения из материала, который тебе дали. Ничего не добавляй из своих знаний: ни фактов, ни дат, ни имён, ни клейм, ни атрибуций.
2. После каждого утверждения ставь номер фрагмента в квадратных скобках, как в материале: [1], [27]. Номеров, которых нет в материале, не бывает.
3. Не пиши названия книг, авторов книг и номера страниц — бот добавит их сам.
4. Пиши своими словами: статья пойдёт в группу, длинные куски чужого текста нельзя. Короткая фраза на языке оригинала — можно, в кавычках и с номером.
5. Если источники противоречат друг другу или OCR неразборчив — скажи об этом в разделе «СОМНЕНИЯ».
6. Не догадывайся. Мало сведений — так и напиши.

Разделы (заголовки заглавными буквами, без Markdown — без звёздочек, решёток и таблиц). Раздел, по которому ничего нет, пропусти:
КОРОТКО — 2–3 предложения о сути темы.
ГЛАВНЫЕ ФАКТЫ — список, каждая строка начинается с «— ».
ДАТЫ И ПЕРИОДЫ
ЛЮДИ, ФИРМЫ, МАРКИ, КЛЕЙМА
МАТЕРИАЛЫ И ТЕХНИКИ
КАК УЗНАТЬ — признаки, на что смотреть
ЖИВЫЕ ДЕТАЛИ ДЛЯ ПОСТА — 3–7 интересных подробностей
СОМНЕНИЯ — противоречия и неразборчивые места
ЧЕГО В КНИГАХ НЕТ — что стоит поискать и проверить дополнительно

По-русски, просто и точно. Ничего важного из материала не теряй."""

DIRECT_PROMPT = (
    "Ты — Люся, исследовательница библиотеки OH MY GOD VINTAGE School. Кристина, хозяйка школы, "
    "готовит статью для группы. Тебе дали фрагменты из книг её библиотеки по одной теме — текст "
    "после распознавания (OCR), на разных языках, возможны опечатки. Фрагменты пронумерованы: [1], [2].\n\n"
    "Собери ДОСЬЕ: всё важное, что эти книги говорят по теме. Это черновик для автора, а не готовый пост.\n\n"
    + DOSSIER_RULES
)

FINAL_PROMPT = (
    "Ты — Люся, исследовательница библиотеки OH MY GOD VINTAGE School. Кристина, хозяйка школы, "
    "готовит статью для группы. Все страницы библиотеки по теме уже прочитаны по этапам, и по каждому "
    "этапу выписаны факты с номерами фрагментов, например [27]. Тебе дали все эти выписки.\n\n"
    "Собери из них одно ДОСЬЕ: объедини повторы, сохрани все разные факты и их номера.\n\n"
    + DOSSIER_RULES
)

HEADER = "📝 ДОСЬЕ ДЛЯ СТАТЬИ"
NOTE = (
    "💬 Пересказ сделал ИИ только по найденным страницам книг. "
    "Это черновик: факты проверь по оригиналу в файле."
)
NO_INFO = "НЕТ СВЕДЕНИЙ"

_CITATION = re.compile(r"\[(\d{1,4})\]")
_CITATION_WITH_SPACE = re.compile(r"\s*\[(\d{1,4})\]")


class ArticleUnavailable(Exception):
    """ИИ недоступен: reason = disabled | no_key | quota | error | budget."""

    def __init__(self, reason, details=""):
        super().__init__(reason)
        self.reason = reason
        self.details = details


# --- настройки ---

def _float_env(name, default):
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except ValueError:
        return default


def _int_env(name, default):
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


def max_pages():
    return _int_env("LUCY_ARTICLE_MAX_PAGES", 400)


def confirm_usd():
    return _float_env("LUCY_ARTICLE_CONFIRM_USD", 0.02)


def max_usd():
    return _float_env("LUCY_ARTICLE_MAX_USD", 1.0)


def budget_day():
    return _float_env("LUCY_ARTICLE_BUDGET_DAY_USD", 1.0)


def budget_month():
    return _float_env("LUCY_ARTICLE_BUDGET_MONTH_USD", 5.0)


def _now():
    return datetime.now(timezone.utc)


def _stamp():
    return _now().isoformat(timespec="seconds")


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=DB_TIMEOUT, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout = {DB_TIMEOUT * 1000}")
    return conn


# --- база ---

def init_tables(db_path):
    conn = sqlite3.connect(db_path)
    try:
        # Досье первой версии /article (PR #23). Таблица остаётся как есть.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS library_articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_key TEXT NOT NULL,
                topic TEXT NOT NULL,
                dossier TEXT NOT NULL,
                fragments_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS library_article_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_key TEXT NOT NULL,
                topic TEXT NOT NULL,
                user_id INTEGER,
                status TEXT NOT NULL,
                research_json TEXT NOT NULL,
                parts_total INTEGER NOT NULL,
                estimate_usd REAL NOT NULL,
                dossier TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_library_article_jobs_key "
            "ON library_article_jobs (topic_key)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS library_article_parts (
                job_id INTEGER NOT NULL,
                part_no INTEGER NOT NULL,
                notes TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (job_id, part_no)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS library_article_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER,
                kind TEXT NOT NULL,
                model TEXT NOT NULL,
                status TEXT NOT NULL,
                reserved_usd REAL DEFAULT 0,
                cost_usd REAL DEFAULT 0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                error TEXT,
                created_at TEXT NOT NULL,
                finished_at TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def topic_key(db_path, topic):
    # Тема + состояние библиотеки: новые страницы — новое исследование.
    return library_ai.query_key(db_path, topic)


# --- поиск и план исследования ---

def _library_overview(db_path):
    """Все книги и книги, которые ещё не распознаны до конца."""
    conn = sqlite3.connect(db_path)
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM library_documents WHERE COALESCE(storage_key, '') LIKE 'books/%'"
        ).fetchone()[0]
        unfinished = conn.execute(
            """
            SELECT d.document_code, COALESCE(NULLIF(d.meta_title, ''), d.title),
                   j.status, j.last_completed_page
            FROM library_documents d
            JOIN library_import_jobs j ON j.id = (
                SELECT MAX(j2.id) FROM library_import_jobs j2 WHERE j2.document_id = d.id
            )
            WHERE COALESCE(d.storage_key, '') LIKE 'books/%'
              AND j.status != 'completed'
            ORDER BY d.id
            """
        ).fetchall()
    finally:
        conn.close()
    return total, [list(row) for row in unfinished]


def _count_candidates(db_path, plan):
    terms = plan.all_terms
    if not terms:
        return 0
    where = " OR ".join("COALESCE(p.cleaned_text, p.original_text) LIKE ?" for _ in terms)
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            f"""
            SELECT COUNT(*) FROM library_pages p
            JOIN library_documents d ON d.id = p.document_id
            WHERE COALESCE(d.storage_key, '') LIKE 'books/%' AND ({where})
            """,
            [library_search._like_value(term) for term in terms],
        ).fetchone()[0]
    finally:
        conn.close()


def page_fragment(result):
    """Текст страницы для ИИ: вся страница или, если она длинная, окна вокруг совпадений."""
    text = (result.get("text") or "").strip()
    if len(text) <= PAGE_CHARS:
        return text
    windows = library_search.passages(result, max_passages=3, before=600, after=1200)
    return "\n…\n".join(windows)[:PAGE_CHARS]


def split_parts(fragments):
    """Номера фрагментов по этапам: не больше PART_CHARS знаков на этап."""
    parts, current, size = [], [], 0
    for fragment in fragments:
        length = len(fragment["text"])
        if current and size + length > PART_CHARS:
            parts.append(current)
            current, size = [], 0
        current.append(fragment["n"])
        size += length
    if current:
        parts.append(current)
    return parts


def _call_estimate(prompt_chars, max_tokens):
    return visual_ai.cost(prompt_chars / CHARS_PER_TOKEN + OVERHEAD_TOKENS, max_tokens)


def estimate_cost(fragments, parts):
    """Оценка сверху: полный объём входа и максимальный объём ответа каждого этапа."""
    by_n = {f["n"]: f for f in fragments}
    if len(parts) <= 1:
        chars = len(DIRECT_PROMPT) + sum(len(f["text"]) + 40 for f in fragments)
        return _call_estimate(chars, FINAL_MAX_TOKENS)
    total = 0.0
    for part in parts:
        chars = len(PART_PROMPT) + sum(len(by_n[n]["text"]) + 40 for n in part)
        total += _call_estimate(chars, PART_MAX_TOKENS)
    notes_chars = len(parts) * PART_MAX_TOKENS * CHARS_PER_TOKEN
    total += _call_estimate(len(FINAL_PROMPT) + notes_chars, FINAL_MAX_TOKENS)
    return total


def calls_count(parts_total):
    return 1 if parts_total <= 1 else parts_total + 1


def minutes_estimate(parts_total):
    return max(1, math.ceil(calls_count(parts_total) * SECONDS_PER_CALL / 60))


def build_research(db_path, topic):
    """Поиск по всей библиотеке и план исследования (без платных вызовов)."""
    plan = library_search.build_search_plan(topic)
    books_total, unfinished = _library_overview(db_path)
    research = {
        "topic": topic,
        "terms": plan.all_terms,
        "unknown_words": plan.unknown_words,
        "fragments": [],
        "parts": [],
        "estimate": 0.0,
        "report": {
            "books_total": books_total,
            "unfinished": unfinished,
            "candidates": 0,
            "candidates_cap": MAX_CANDIDATES,
            "pages_found": 0,
            "content_pages": 0,
            "processed_pages": 0,
            "skipped_by_limit": 0,
            "max_pages": max_pages(),
            "service_pages": [],
            "books": {},
        },
    }
    if not plan.all_terms:
        return research

    results = library_search.search_pages(
        db_path, plan, limit=SEARCH_LIMIT, max_candidates=MAX_CANDIDATES
    )
    report = research["report"]
    report["candidates"] = _count_candidates(db_path, plan)
    report["pages_found"] = len(results)

    content = [r for r in results if r.get("quality", 1) >= library_ai.MIN_PAGE_QUALITY]
    service = [r for r in results if r.get("quality", 1) < library_ai.MIN_PAGE_QUALITY]
    processed = content[:max_pages()]  # уже по убыванию релевантности
    report["content_pages"] = len(content)
    report["processed_pages"] = len(processed)
    report["skipped_by_limit"] = len(content) - len(processed)
    report["service_pages"] = [
        [r.get("document_code") or "—", r["title"], r["pdf_page"]]
        for r in sorted(service, key=lambda r: (r.get("document_code") or "", r["pdf_page"]))
    ]

    books = report["books"]
    for result in results:
        code = result.get("document_code") or result["title"]
        entry = books.setdefault(code, {"title": result["title"], "found": 0, "processed": 0})
        entry["found"] += 1
    for result in processed:
        books[result.get("document_code") or result["title"]]["processed"] += 1

    processed.sort(key=lambda r: (r.get("document_code") or "", r["pdf_page"]))
    research["fragments"] = [
        {
            "n": number,
            "title": result["title"],
            "document_code": result.get("document_code"),
            "language": result.get("language"),
            "pdf_page": result["pdf_page"],
            "printed_page": result.get("printed_page"),
            "text": page_fragment(result),
        }
        for number, result in enumerate(processed, start=1)
    ]
    research["parts"] = split_parts(research["fragments"])
    research["estimate"] = estimate_cost(research["fragments"], research["parts"])
    return research


# --- задачи исследования ---

def create_job(db_path, key, topic, user_id, research):
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO library_article_jobs (
                topic_key, topic, user_id, status, research_json, parts_total,
                estimate_usd, created_at, updated_at
            ) VALUES (?, ?, ?, 'planned', ?, ?, ?, ?, ?)
            """,
            (
                key, topic[:MAX_TOPIC_CHARS], user_id,
                json.dumps(research, ensure_ascii=False),
                len(research["parts"]), research["estimate"], _stamp(), _stamp(),
            ),
        )
        return cursor.lastrowid
    finally:
        conn.close()


def _job_from_row(row):
    if row is None:
        return None
    keys = ("id", "topic_key", "topic", "user_id", "status", "research_json",
            "parts_total", "estimate_usd", "dossier", "error", "created_at", "updated_at")
    job = dict(zip(keys, row))
    job["research"] = json.loads(job.pop("research_json"))
    return job


_JOB_COLUMNS = (
    "id, topic_key, topic, user_id, status, research_json, parts_total, "
    "estimate_usd, dossier, error, created_at, updated_at"
)


def get_job(db_path, job_id):
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"SELECT {_JOB_COLUMNS} FROM library_article_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    finally:
        conn.close()
    return _job_from_row(row)


def find_job(db_path, key):
    """Последнее не отменённое исследование этой темы при этом состоянии библиотеки."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"""
            SELECT {_JOB_COLUMNS} FROM library_article_jobs
            WHERE topic_key = ? AND status != 'cancelled'
            ORDER BY id DESC LIMIT 1
            """,
            (key,),
        ).fetchone()
    finally:
        conn.close()
    return _job_from_row(row)


def set_status(db_path, job_id, status, error=None, dossier=None):
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            UPDATE library_article_jobs
            SET status = ?, error = ?, dossier = COALESCE(?, dossier), updated_at = ?
            WHERE id = ?
            """,
            (status, error, dossier, _stamp(), job_id),
        )
    finally:
        conn.close()


def saved_parts(db_path, job_id):
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT part_no, notes FROM library_article_parts WHERE job_id = ? ORDER BY part_no",
            (job_id,),
        ).fetchall()
    finally:
        conn.close()
    return dict(rows)


def save_part(db_path, job_id, part_no, notes):
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO library_article_parts (job_id, part_no, notes, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (job_id, part_no, notes, _stamp()),
        )
    finally:
        conn.close()


def remaining_estimate(db_path, job):
    """Оценка оставшейся стоимости: несделанные этапы + итог."""
    research = job["research"]
    parts = research["parts"]
    done = saved_parts(db_path, job["id"])
    left = [part for number, part in enumerate(parts, start=1) if number not in done]
    if len(parts) <= 1:
        return research["estimate"] if not job.get("dossier") else 0.0
    by_n = {f["n"]: f for f in research["fragments"]}
    total = 0.0
    for part in left:
        chars = len(PART_PROMPT) + sum(len(by_n[n]["text"]) + 40 for n in part)
        total += _call_estimate(chars, PART_MAX_TOKENS)
    notes_chars = len(parts) * PART_MAX_TOKENS * CHARS_PER_TOKEN
    total += _call_estimate(len(FINAL_PROMPT) + notes_chars, FINAL_MAX_TOKENS)
    return total


def is_running(job_id):
    return job_id in RUNNING


def any_running():
    return bool(RUNNING)


# --- бюджет и платные вызовы ---

def _expire_stale(conn):
    limit = (_now() - timedelta(minutes=STALE_RESERVATION_MINUTES)).isoformat(timespec="seconds")
    conn.execute(
        """
        UPDATE library_article_calls
        SET status = 'unknown', cost_usd = reserved_usd, reserved_usd = 0,
            error = COALESCE(error, 'резерв не завершён — процесс прервался'), finished_at = ?
        WHERE status = 'reserved' AND created_at < ?
        """,
        (_stamp(), limit),
    )


def _period_prefix(period):
    return _now().strftime("%Y-%m-%d" if period == "day" else "%Y-%m")


def _totals(conn, period):
    return conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN status IN ('done', 'unknown') THEN cost_usd ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN status = 'reserved' THEN reserved_usd ELSE 0 END), 0)
        FROM library_article_calls WHERE created_at LIKE ?
        """,
        (_period_prefix(period) + "%",),
    ).fetchone()


def spent(db_path, period="day"):
    """Оплаченные, возможно оплаченные и зарезервированные расходы за день или месяц."""
    conn = _connect(db_path)
    try:
        charged, held = _totals(conn, period)
    finally:
        conn.close()
    return charged + held


def job_cost(db_path, job_id):
    conn = _connect(db_path)
    try:
        return conn.execute(
            """
            SELECT COALESCE(SUM(CASE WHEN status IN ('done', 'unknown') THEN cost_usd ELSE 0 END), 0)
            FROM library_article_calls WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()[0]
    finally:
        conn.close()


def budget_problem(db_path, estimate):
    """Текст причины, если оценка не помещается в бюджет; иначе None."""
    if estimate > max_usd():
        return (
            f"оценка {estimate:.2f} $ больше лимита одного исследования {max_usd():.2f} $ "
            "(LUCY_ARTICLE_MAX_USD)"
        )
    day, month = spent(db_path, "day"), spent(db_path, "month")
    if day + estimate > budget_day():
        return f"дневной бюджет досье {budget_day():.2f} $ (уже {day:.2f} $)"
    if month + estimate > budget_month():
        return f"месячный бюджет досье {budget_month():.2f} $ (уже {month:.2f} $)"
    return None


def _reserve(db_path, job_id, kind, estimate):
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _expire_stale(conn)
            day = sum(_totals(conn, "day"))
            month = sum(_totals(conn, "month"))
            if day + estimate > budget_day():
                raise ArticleUnavailable("budget", f"дневной бюджет досье {budget_day():.2f} $")
            if month + estimate > budget_month():
                raise ArticleUnavailable("budget", f"месячный бюджет досье {budget_month():.2f} $")
            cursor = conn.execute(
                """
                INSERT INTO library_article_calls (job_id, kind, model, status, reserved_usd, created_at)
                VALUES (?, ?, ?, 'reserved', ?, ?)
                """,
                (job_id, kind, MODEL, estimate, _stamp()),
            )
            conn.execute("COMMIT")
            return cursor.lastrowid
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _finish_call(db_path, call_id, status, cost=0.0, input_tokens=0, output_tokens=0, error=None):
    conn = _connect(db_path)
    try:
        if status == "unknown":
            conn.execute(
                """
                UPDATE library_article_calls
                SET status = 'unknown', cost_usd = reserved_usd, reserved_usd = 0,
                    error = ?, finished_at = ? WHERE id = ?
                """,
                (error, _stamp(), call_id),
            )
        else:
            conn.execute(
                """
                UPDATE library_article_calls
                SET status = ?, cost_usd = ?, reserved_usd = 0, input_tokens = ?,
                    output_tokens = ?, error = ?, finished_at = ? WHERE id = ?
                """,
                (status, cost, input_tokens, output_tokens, error, _stamp(), call_id),
            )
    finally:
        conn.close()


def call_ai(db_path, job_id, kind, messages, max_tokens):
    """Один платный вызов: резерв → запрос → фактическая стоимость. Синхронно."""
    if not library_ai.enabled():
        raise ArticleUnavailable("disabled")
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ArticleUnavailable("no_key")

    chars = sum(len(m["content"]) for m in messages)
    call_id = _reserve(db_path, job_id, kind, _call_estimate(chars, max_tokens))

    import openai

    no_response = tuple(
        getattr(openai, name) for name in ("APITimeoutError", "APIConnectionError")
        if isinstance(getattr(openai, name, None), type)
    )
    try:
        client = openai.OpenAI(api_key=api_key, timeout=AI_TIMEOUT, max_retries=1)
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            max_completion_tokens=max_tokens,
            temperature=0.2,
        )
    except no_response as error:
        print(f"Article AI no response: {type(error).__name__}")
        _finish_call(db_path, call_id, "unknown", error=f"{type(error).__name__} · ответ не получен")
        raise ArticleUnavailable("error") from None
    except openai.OpenAIError as error:
        code = getattr(error, "code", None)
        # В логи — только тип и код ошибки, без ключей и текста запроса.
        print(f"Article AI error: {type(error).__name__} code={code}")
        _finish_call(db_path, call_id, "failed", error=f"{type(error).__name__} code={code}")
        raise ArticleUnavailable("quota" if code in ("insufficient_quota", "billing_hard_limit_reached") else "error") from None
    except BaseException as error:
        _finish_call(db_path, call_id, "unknown", error=type(error).__name__)
        raise

    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "prompt_tokens", 0) or 0
    output_tokens = getattr(usage, "completion_tokens", 0) or 0
    value = visual_ai.cost(input_tokens, output_tokens)
    _finish_call(db_path, call_id, "done", value, input_tokens, output_tokens)
    text = (response.choices[0].message.content or "").strip()
    if not text:
        raise ArticleUnavailable("error")
    return text


def _fragment_blocks(fragments):
    return "\n\n".join(
        f"[{f['n']}] (язык: {library_search.language_name(f.get('language'))})\n{f['text']}"
        for f in fragments
    )


def part_messages(topic, fragments):
    return [
        {"role": "system", "content": PART_PROMPT},
        {"role": "user", "content": f"Тема исследования: {topic}\n\nФрагменты:\n\n{_fragment_blocks(fragments)}"},
    ]


def direct_messages(topic, fragments):
    return [
        {"role": "system", "content": DIRECT_PROMPT},
        {"role": "user", "content": f"Тема статьи: {topic}\n\nФрагменты книг:\n\n{_fragment_blocks(fragments)}"},
    ]


def final_messages(topic, notes):
    return [
        {"role": "system", "content": FINAL_PROMPT},
        {"role": "user", "content": f"Тема статьи: {topic}\n\nВыписки по этапам:\n\n{notes}"},
    ]


def clean_text(text, fragments):
    """Убирает Markdown и ссылки на несуществующие фрагменты."""
    valid = {f["n"] for f in fragments}

    def keep(match):
        return match.group(0) if int(match.group(1)) in valid else ""

    text = _CITATION_WITH_SPACE.sub(keep, text)
    text = re.sub(r"^\s*#+\s*", "", text, flags=re.MULTILINE)
    return text.replace("**", "").strip()


async def run_job(db_path, job_id, notify):
    """Выполняет исследование по этапам. notify(text) — сообщения о ходе работы.

    Возвращает статус: done | paused | cancelled. Каждый этап сохраняется
    сразу; повторный запуск начинает с первого несделанного этапа.
    """
    if job_id in RUNNING:
        return "running"
    RUNNING.add(job_id)
    try:
        job = await asyncio.to_thread(get_job, db_path, job_id)
        research = job["research"]
        topic, fragments, parts = job["topic"], research["fragments"], research["parts"]
        by_n = {f["n"]: f for f in fragments}
        await asyncio.to_thread(set_status, db_path, job_id, "running")

        try:
            if len(parts) <= 1:
                answer = await _with_retry(
                    call_ai, db_path, job_id, "article_direct",
                    direct_messages(topic, fragments), FINAL_MAX_TOKENS,
                )
            else:
                done = await asyncio.to_thread(saved_parts, db_path, job_id)
                for number, part in enumerate(parts, start=1):
                    if number in done:
                        continue
                    batch = [by_n[n] for n in part]
                    notes = await _with_retry(
                        call_ai, db_path, job_id, "article_part",
                        part_messages(topic, batch), PART_MAX_TOKENS,
                    )
                    notes = clean_text(notes, batch)
                    await asyncio.to_thread(save_part, db_path, job_id, number, notes)
                    done[number] = notes
                    if number % PROGRESS_EVERY == 0 and number < len(parts):
                        await notify(f"⏳ «{topic}»: прочитан этап {number} из {len(parts)}.")
                joined = "\n\n".join(
                    f"ЭТАП {number}\n{done[number]}" for number in sorted(done)
                    if done[number].strip() and done[number].strip().upper() != NO_INFO
                )
                if not joined:
                    joined = NO_INFO
                answer = await _with_retry(
                    call_ai, db_path, job_id, "article_final",
                    final_messages(topic, joined), FINAL_MAX_TOKENS,
                )
        except ArticleUnavailable as problem:
            reason = problem.details or problem.reason
            await asyncio.to_thread(set_status, db_path, job_id, "paused", reason)
            return "paused"

        dossier = clean_text(answer, fragments)
        await asyncio.to_thread(set_status, db_path, job_id, "done", None, dossier)
        return "done"
    finally:
        RUNNING.discard(job_id)


async def _with_retry(function, *args):
    """Один повтор при сбое OpenAI. Бюджет и выключатель не повторяются."""
    try:
        return await asyncio.to_thread(function, *args)
    except ArticleUnavailable as problem:
        if problem.reason != "error":
            raise
    return await asyncio.to_thread(function, *args)


# --- оформление ---

def book_count(research):
    return len(research["report"]["books"])


def cited_numbers(dossier, fragments):
    valid = {f["n"] for f in fragments}
    cited = []
    for match in _CITATION.finditer(dossier or ""):
        number = int(match.group(1))
        if number in valid and number not in cited:
            cited.append(number)
    return sorted(cited)


def source_line(fragment):
    return (
        f"[{fragment['n']}] {fragment['title']} ({fragment.get('document_code') or '—'}) — "
        f"{library_ai.page_label(fragment)}"
    )


def source_lines(dossier, fragments):
    cited = set(cited_numbers(dossier, fragments))
    return [source_line(f) for f in fragments if f["n"] in cited]


def plan_lines(db_path, job):
    """План перед запуском: что найдено, сколько этапов, сколько стоит."""
    research = job["research"]
    report = research["report"]
    parts_total = len(research["parts"])
    done = len(saved_parts(db_path, job["id"]))
    estimate = remaining_estimate(db_path, job)
    lines = [
        f"📝 ПЛАН ИССЛЕДОВАНИЯ: {job['topic']}",
        "",
        f"Нашла страниц с темой: {report['pages_found']} "
        f"в {book_count(research)} из {report['books_total']} книг.",
        f"ИИ прочитает содержательных страниц: {report['processed_pages']}"
        + (f" из {report['content_pages']}" if report["skipped_by_limit"] else "") + ".",
        f"Служебных страниц (оглавления, указатели, выходные данные) — в отчёт, без ИИ: "
        f"{len(report['service_pages'])}.",
        f"Этапов: {parts_total}" + (" + итоговое досье" if parts_total > 1 else "")
        + (f" · уже сделано: {done}" if done else "") + ".",
        f"Стоимость: не больше ≈ {estimate:.2f} $ (обычно меньше).",
        f"Время: около {minutes_estimate(parts_total - done)} мин.",
        f"Потрачено на досье сегодня: {spent(db_path, 'day'):.2f} $ из {budget_day():.2f} $.",
    ]
    if report["skipped_by_limit"]:
        lines.append(
            f"⚠️ {report['skipped_by_limit']} менее подходящих страниц сверх лимита "
            f"{report['max_pages']} (LUCY_ARTICLE_MAX_PAGES) не будут прочитаны."
        )
    lines.append("Результат придёт сюда — чат можно закрыть.")
    return lines


def report_lines(db_path, job):
    """Отчёт о полноте: что найдено, что прочитано, что пропущено и почему."""
    research = job["research"]
    report = research["report"]
    lines = [
        "📊 ПОЛНОТА ИССЛЕДОВАНИЯ",
        f"Книг в библиотеке: {report['books_total']} · с упоминанием темы: {book_count(research)}",
        f"Страниц с совпадениями: {report['pages_found']}",
        f"— прочитано ИИ: {report['processed_pages']}"
        + (" (все содержательные)" if not report["skipped_by_limit"] else
           f" из {report['content_pages']} — {report['skipped_by_limit']} менее подходящих "
           f"сверх лимита {report['max_pages']}"),
        f"— служебные страницы без ИИ: {len(report['service_pages'])}",
    ]
    parts_total = len(research["parts"])
    done = len(saved_parts(db_path, job["id"])) if parts_total > 1 else (1 if job.get("dossier") else 0)
    lines.append(
        f"Этапов выполнено: {done} из {parts_total} · стоимость: {job_cost(db_path, job['id']):.4f} $"
    )
    if report["books"]:
        lines.append("По книгам:")
        for code, entry in sorted(report["books"].items()):
            lines.append(
                f"• {code} · {entry['title']} — найдено {entry['found']}, прочитано {entry['processed']}"
            )
    service = report["service_pages"]
    if service:
        shown = ", ".join(f"{code} стр. {page}" for code, _, page in service[:MAX_LISTED_SERVICE_PAGES])
        more = f" и ещё {len(service) - MAX_LISTED_SERVICE_PAGES}" if len(service) > MAX_LISTED_SERVICE_PAGES else ""
        lines.append(f"Упоминания в оглавлениях и указателях: {shown}{more}")
    for code, title, status, last_page in report["unfinished"]:
        lines.append(
            f"⚠️ {code} · {title} ещё не распознана до конца ({status}, до стр. {last_page or 0}) — "
            "искала только по готовым страницам."
        )
    if report["candidates"] > report["candidates_cap"]:
        lines.append(
            f"⚠️ Совпадений больше {report['candidates_cap']} — просмотрены не все страницы. "
            "Сделай тему конкретнее."
        )
    if research["unknown_words"]:
        lines.append(
            "Не искала по словам (нет в словаре): " + ", ".join(research["unknown_words"][:8])
            + ". Имена и фирмы пиши латиницей."
        )
    lines.append(
        "Полноту не гарантируют: опечатки OCR, синонимы не из словаря, "
        "картинки без подписи."
    )
    return lines


def result_lines(db_path, job):
    research = job["research"]
    fragments = research["fragments"]
    lines = [
        HEADER,
        f"Тема: {job['topic']}",
        f"По {len(fragments)} страницам из {book_count(research)} книг(и).",
        "",
    ]
    lines += (job.get("dossier") or "").splitlines()
    sources = source_lines(job.get("dossier"), fragments)
    if sources:
        lines += ["", "📖 Источники:"] + sources
    lines += [""] + report_lines(db_path, job)
    lines += ["", NOTE, "Полный текст и оригиналы страниц — в файле ниже."]
    return lines


def file_text(db_path, job):
    """Файл для работы: досье, отчёт, все источники и точный текст каждой страницы."""
    research = job["research"]
    fragments = research["fragments"]
    parts = [
        f"ДОСЬЕ ДЛЯ СТАТЬИ: {job['topic']}",
        f"Собрано {datetime.now(lucy_ai.TIMEZONE):%d.%m.%Y} по {len(fragments)} страницам "
        f"из {book_count(research)} книг(и) библиотеки OH MY GOD VINTAGE.",
        "",
        job.get("dossier") or "",
        "",
        *report_lines(db_path, job),
        "",
        "ИСТОЧНИКИ",
        *[source_line(f) for f in fragments],
        "",
        "ОРИГИНАЛЫ СТРАНИЦ",
        "Точный текст страниц после распознавания (OCR), без перевода, возможны опечатки.",
    ]
    for f in fragments:
        parts += [
            "",
            f"[{f['n']}] {f['title']} — {library_ai.page_label(f)} · "
            f"язык: {library_search.language_name(f.get('language'))}",
            f["text"],
        ]
    parts += ["", NOTE]
    return "\n".join(parts) + "\n"


def file_name(topic):
    slug = re.sub(r"[^\w]+", "_", topic.lower()).strip("_")[:40] or "tema"
    return f"dossier_{slug}.txt"
