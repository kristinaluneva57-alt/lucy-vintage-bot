"""Досье для статьи по книгам библиотеки (/article, только администратор).

Как это работает:
1. Обычный поиск (library_search) находит страницы по теме во всех книгах.
2. Отбираются содержательные страницы — не больше MAX_PAGES_PER_BOOK
   из одной книги, чтобы одна толстая книга не забивала остальные.
3. Из страниц вырезаются фрагменты — точный текст после OCR.
4. ИИ (gpt-4.1-mini) пересказывает их по-русски своими словами в виде
   досье по разделам и после каждого факта ставит номер фрагмента [n].
5. Названия книг и страницы подставляет бот из базы — ИИ их не пишет
   и не может придумать.
6. Досье и фрагменты сохраняются в library_articles: повторный запрос
   той же темы не оплачивается, пока в библиотеку не добавили страницы.

Досье — черновик для Кристины, не пост: факты из книг, которые потом
дополняются и проверяются. Страницы и PDF не меняются.
"""

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import library_ai
import library_search
import lucy_ai

SEARCH_LIMIT = 80
MAX_PAGES_PER_BOOK = 5
MAX_PASSAGES_PER_PAGE = 2
MAX_FRAGMENTS = 24
MAX_FRAGMENT_CHARS = 1500
MAX_CONTEXT_CHARS = 30000
MAX_TOPIC_CHARS = 120
MAX_DOSSIER_TOKENS = 2500
AI_TIMEOUT = 120.0
CACHE_DAYS = 30
# Страница берётся, если она хотя бы так релевантна относительно лучшей.
# Ниже, чем в обычном ответе: для досье нужны и второстепенные упоминания.
MIN_RELATIVE_SCORE = 0.1

SYSTEM_PROMPT = """Ты — Люся, исследовательница библиотеки OH MY GOD VINTAGE School. Кристина, хозяйка школы, готовит статью для группы. Тебе дали фрагменты из разных книг её библиотеки по одной теме. Фрагменты пронумерованы: [1], [2] и т.д. Это текст страниц после распознавания (OCR), он может быть на английском, итальянском, немецком, французском, чешском, польском или русском и содержать опечатки.

Собери ДОСЬЕ: всё важное, что эти книги говорят по теме. Это черновик для автора, а не готовый пост.

Правила — без исключений:
1. Только сведения из фрагментов. Ничего не добавляй из своих знаний: ни фактов, ни дат, ни имён, ни клейм, ни атрибуций.
2. После каждого утверждения ставь номер фрагмента: [1], [2]. Номеров, которых нет во фрагментах, не бывает.
3. Не пиши названия книг, авторов книг и номера страниц — бот добавит их сам.
4. Пиши своими словами. Не переписывай фрагменты подряд: статья пойдёт в группу, и длинные куски чужого текста нельзя. Короткая фраза на языке оригинала — можно, в кавычках и с номером.
5. Если книги противоречат друг другу или OCR неразборчив — скажи об этом в разделе «СОМНЕНИЯ».
6. Не догадывайся. Мало сведений — так и напиши.

Разделы (заголовки заглавными буквами, без Markdown — без звёздочек, решёток и таблиц). Раздел, по которому во фрагментах ничего нет, пропусти:
КОРОТКО — 2–3 предложения о сути темы.
ГЛАВНЫЕ ФАКТЫ — список, каждая строка начинается с «— ».
ДАТЫ И ПЕРИОДЫ
ЛЮДИ, ФИРМЫ, МАРКИ, КЛЕЙМА
МАТЕРИАЛЫ И ТЕХНИКИ
КАК УЗНАТЬ — признаки, на что смотреть
ЖИВЫЕ ДЕТАЛИ ДЛЯ ПОСТА — 3–5 интересных подробностей
СОМНЕНИЯ — противоречия и неразборчивые места
ЧЕГО В КНИГАХ НЕТ — что стоит поискать и проверить дополнительно

По-русски, просто и точно."""

HEADER = "📝 ДОСЬЕ ДЛЯ СТАТЬИ\n"
NOTE = (
    "💬 Пересказ сделал ИИ только по найденным фрагментам книг. "
    "Это черновик: факты проверь по оригиналу в файле."
)

_CITATION = re.compile(r"\[(\d{1,2})\]")
_CITATION_WITH_SPACE = re.compile(r"\s*\[(\d{1,2})\]")


# --- настройки ---

def daily_limit():
    try:
        return max(0, int(os.environ.get("LUCY_ARTICLE_DAILY_LIMIT", 20)))
    except ValueError:
        return 20


# --- база ---

def init_tables(db_path):
    conn = sqlite3.connect(db_path)
    try:
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
            "CREATE INDEX IF NOT EXISTS idx_library_articles_key "
            "ON library_articles (topic_key)"
        )
        conn.commit()
    finally:
        conn.close()


def topic_key(db_path, topic):
    # Тот же ключ, что у ответов по библиотеке: тема + состояние библиотеки.
    return library_ai.query_key(db_path, topic)


def find_cached(db_path, key):
    since = (datetime.now(timezone.utc) - timedelta(days=CACHE_DAYS)).isoformat(timespec="seconds")
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT dossier, fragments_json FROM library_articles
            WHERE topic_key = ? AND created_at >= ?
            ORDER BY id DESC LIMIT 1
            """,
            (key, since),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return row[0], json.loads(row[1])


def save(db_path, key, topic, dossier, fragments):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO library_articles
                (topic_key, topic, dossier, fragments_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                key,
                topic[:MAX_TOPIC_CHARS],
                dossier,
                json.dumps(fragments, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def made_today(db_path):
    """Сколько досье оплачено сегодня (кэш не считается)."""
    start = datetime.combine(
        datetime.now(lucy_ai.TIMEZONE).date(), datetime.min.time(), lucy_ai.TIMEZONE
    ).astimezone(timezone.utc).isoformat(timespec="seconds")
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM library_articles WHERE created_at >= ?", (start,)
        ).fetchone()[0]
    finally:
        conn.close()


# --- фрагменты ---

def select_fragments(results):
    """Фрагменты из многих книг: не больше MAX_PAGES_PER_BOOK страниц на книгу.

    Служебные страницы (оглавление, копирайт, указатель, OCR-мусор)
    пропускаются, если есть содержательные. Итог отсортирован по книге
    и странице, чтобы досье и файл читались по порядку.
    """
    good = [r for r in results if r.get("quality", 1) >= library_ai.MIN_PAGE_QUALITY]
    pages = good or results
    if pages and pages[0].get("score"):
        best = pages[0]["score"]
        pages = [r for r in pages if r.get("score", best) >= best * MIN_RELATIVE_SCORE]

    per_book = {}
    chosen = []
    for result in pages:
        book = result.get("document_code") or result["title"]
        if per_book.get(book, 0) >= MAX_PAGES_PER_BOOK:
            continue
        per_book[book] = per_book.get(book, 0) + 1
        chosen.append(result)

    texts = []
    total = 0
    for result in chosen:
        for text in library_search.passages(result, max_passages=MAX_PASSAGES_PER_PAGE):
            text = text[:MAX_FRAGMENT_CHARS]
            if len(texts) >= MAX_FRAGMENTS or total + len(text) > MAX_CONTEXT_CHARS:
                break
            total += len(text)
            texts.append((result, text))

    texts.sort(key=lambda item: (item[0].get("document_code") or "", item[0]["pdf_page"]))
    return [
        {
            "n": number,
            "title": result["title"],
            "document_code": result.get("document_code"),
            "language": result.get("language"),
            "pdf_page": result["pdf_page"],
            "printed_page": result.get("printed_page"),
            "text": text,
        }
        for number, (result, text) in enumerate(texts, start=1)
    ]


def book_count(fragments):
    return len({f.get("document_code") or f["title"] for f in fragments})


# --- запрос к ИИ ---

def build_messages(topic, fragments):
    blocks = [
        f"[{f['n']}] (язык: {library_search.language_name(f.get('language'))})\n{f['text']}"
        for f in fragments
    ]
    user = (
        f"Тема статьи: {topic[:MAX_TOPIC_CHARS]}\n\n"
        "Фрагменты книг:\n\n" + "\n\n".join(blocks)
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def clean_dossier(dossier, fragments):
    """Убирает Markdown и ссылки на несуществующие фрагменты."""
    valid = {f["n"] for f in fragments}

    def keep(match):
        return match.group(0) if int(match.group(1)) in valid else ""

    dossier = _CITATION_WITH_SPACE.sub(keep, dossier)
    dossier = re.sub(r"^\s*#+\s*", "", dossier, flags=re.MULTILINE)
    dossier = dossier.replace("**", "")
    return dossier.strip()


def ask(fragments, topic):
    """Синхронный вызов ИИ. Бросает lucy_ai.AIUnavailable при проблемах."""
    if not library_ai.enabled():
        raise lucy_ai.AIUnavailable("disabled")
    dossier = lucy_ai.ask_openai(
        build_messages(topic, fragments),
        max_tokens=MAX_DOSSIER_TOKENS,
        temperature=0.2,
        check_enabled=False,
        timeout=AI_TIMEOUT,
    )
    return clean_dossier(dossier, fragments)


# --- оформление ---

def cited_numbers(dossier, fragments):
    valid = {f["n"] for f in fragments}
    cited = []
    for match in _CITATION.finditer(dossier):
        number = int(match.group(1))
        if number in valid and number not in cited:
            cited.append(number)
    return sorted(cited)


def source_lines(dossier, fragments):
    cited = set(cited_numbers(dossier, fragments))
    shown = [f for f in fragments if f["n"] in cited] or fragments
    return [
        f"[{f['n']}] {f['title']} ({f.get('document_code') or '—'}) — {library_ai.page_label(f)}"
        for f in shown
    ]


def message_lines(topic, dossier, fragments):
    """Строки для Telegram: заголовок, досье, источники, пометка."""
    lines = [
        HEADER + f"Тема: {topic}",
        f"По {len(fragments)} фрагментам из {book_count(fragments)} книг(и).",
        "",
    ]
    lines += dossier.splitlines()
    lines += ["", "📖 Источники:"] + source_lines(dossier, fragments)
    lines += ["", NOTE, "Полный текст и оригиналы фрагментов — в файле ниже."]
    return lines


def file_text(topic, dossier, fragments):
    """Файл для работы: досье, источники и точный текст каждого фрагмента."""
    parts = [
        f"ДОСЬЕ ДЛЯ СТАТЬИ: {topic}",
        f"Собрано {datetime.now(lucy_ai.TIMEZONE):%d.%m.%Y} по {len(fragments)} фрагментам "
        f"из {book_count(fragments)} книг(и) библиотеки OH MY GOD VINTAGE.",
        "",
        dossier,
        "",
        "ИСТОЧНИКИ",
        *[
            f"[{f['n']}] {f['title']} ({f.get('document_code') or '—'}) — {library_ai.page_label(f)}"
            for f in fragments
        ],
        "",
        "ОРИГИНАЛЫ ФРАГМЕНТОВ",
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
