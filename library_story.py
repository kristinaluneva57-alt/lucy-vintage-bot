"""Статья по теме из книг библиотеки — с картинками со страниц книг.

Участница пишет тему («гранат», «бакелит», «Trifari»), Люся собирает
короткую красивую статью по-русски ТОЛЬКО из книг библиотеки и
показывает иллюстрации со страниц, где об этом говорится.

Как это работает и почему дёшево:
1. Бесплатный поиск (library_search) находит содержательные страницы
   по теме во всех книгах. Служебные страницы (оглавление, указатель,
   выходные данные, OCR-мусор) отсеиваются.
2. В ИИ уходят только фрагменты лучших страниц (до ~14 000 знаков) —
   ОДИН вызов gpt-4.1-mini. Оценка: меньше 1 цента за статью.
3. Готовая статья кэшируется: та же тема при той же библиотеке —
   бесплатно и мгновенно. Когда в библиотеку добавляются страницы,
   статья пересобирается.
4. Картинки — без ИИ: страницы, на которых говорится о теме и которые
   индекс (visual_page_index) считает иллюстрированными. Рендер страницы
   берётся из кэша R2 (page_images), белые поля обрезаются.
5. Книги и страницы источников подставляет бот из базы — ИИ их не пишет.
   Ссылки на несуществующие фрагменты убираются.

Честность: картинка — это страница из книги, где говорится о теме.
Люся не утверждает, что на ней изображено именно то, о чём статья,
если этого не видно из подписи.

Книги, страницы и PDF не меняются. Расход пишется в общий учёт
ai_ledger (kind="library_story"), лимиты — общие с «пересказами по
книгам» (LUCY_LIBRARY_AI_DAILY_LIMIT).

Настройки (переменные Railway, все необязательные):
- LUCY_STORY_ENABLED (1) — выключатель статей
- LUCY_STORY_PICTURES (4) — сколько картинок показывать (0 — без картинок)
"""

import html
import io
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import library_search
import lucy_ai

SEARCH_LIMIT = 40
MAX_PAGES = 10
MAX_PAGES_PER_BOOK = 3
MAX_PASSAGES_PER_PAGE = 2
MAX_FRAGMENT_CHARS = 1300
MAX_CONTEXT_CHARS = 14000
MAX_FRAGMENTS = 14
MAX_TOPIC_CHARS = 120
MAX_ARTICLE_TOKENS = 1600
MIN_PAGE_QUALITY = 0.3
MIN_RELATIVE_SCORE = 0.2
CACHE_DAYS = 30
MIN_ILLUSTRATION = 0.35
MAX_PICTURES_PER_BOOK = 2
TELEGRAM_LIMIT = 3900
PICTURE_DPI = 120
PICTURE_MAX_SIDE = 1600

NO_INFO = "НЕТ СВЕДЕНИЙ"

SYSTEM_PROMPT = """Ты — Люся, исследовательница библиотеки OH MY GOD VINTAGE School. Участница попросила статью на тему. Напиши короткую живую статью по-русски ТОЛЬКО по фрагментам книг, которые тебе дали. Фрагменты пронумерованы: [1], [2] и т.д. Это текст страниц после распознавания (OCR) — он может быть на английском, итальянском, немецком, французском, чешском, польском или русском и с опечатками.

Правила — без исключений:
1. Только сведения из фрагментов. Ничего из своих знаний: ни фактов, ни дат, ни имён, ни фирм, ни клейм, ни атрибуций.
2. После каждого утверждения — номер фрагмента: [1], [2]. Номеров, которых нет во фрагментах, не бывает.
3. Не пиши названия книг, авторов и номера страниц — бот добавит их сам.
4. Своими словами. Не переписывай фрагменты подряд и не выдавай перевод за цитату.
5. Если фрагменты противоречат друг другу или OCR неразборчив — так и скажи.
6. Если сведений мало — напиши короткую статью из того, что есть, и в конце честно скажи, чего в книгах не нашлось. Не догадывайся.
7. Если по теме во фрагментах вообще ничего нет — ответь одной строкой: НЕТ СВЕДЕНИЙ.

Форма:
- первая строка — заголовок, начинается с «# »;
- 1–2 предложения вступления;
- 2–5 разделов, каждый начинается со строки «## Подзаголовок» (по сути, что есть во фрагментах: что это, история, как узнать, где и как использовали, интересные факты);
- в конце раздел «## Чего в книгах не нашлось», только если чего-то важного явно не хватает;
- 250–600 слов, короткие абзацы, на «ты» к читательнице не обращайся — это статья;
- без звёздочек, таблиц и другого Markdown, кроме строк «# » и «## »."""

_CITATION = re.compile(r"\[(\d{1,2})\]")
_CITATION_WITH_SPACE = re.compile(r"\s*\[(\d{1,2})\]")


# --- настройки ---

def _int_env(name, default):
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


def enabled():
    value = os.environ.get("LUCY_STORY_ENABLED", "1").strip().lower()
    return value not in ("0", "false", "no", "off")


def pictures_limit():
    return min(_int_env("LUCY_STORY_PICTURES", 4), 8)


# --- база ---

def _now():
    return datetime.now(timezone.utc)


def init_tables(db_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS library_stories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                story_key TEXT NOT NULL,
                topic TEXT NOT NULL,
                article TEXT NOT NULL,
                fragments_json TEXT NOT NULL,
                pictures_json TEXT NOT NULL DEFAULT '[]',
                user_id INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_library_stories_key ON library_stories (story_key)"
        )
        conn.commit()
    finally:
        conn.close()


def normalize_topic(topic):
    text = re.sub(r"[^\w\s\-]", " ", (topic or "").lower().replace("ё", "е"))
    return " ".join(text.split())[:MAX_TOPIC_CHARS]


def story_key(db_path, topic):
    """Тема + состояние библиотеки: новые страницы — новая статья."""
    conn = sqlite3.connect(db_path)
    try:
        version = conn.execute(
            "SELECT COALESCE(MAX(id), 0), COUNT(*) FROM library_pages"
        ).fetchone()
    finally:
        conn.close()
    return f"{normalize_topic(topic)}|{version[0]}:{version[1]}"


def find_cached(db_path, key):
    since = (_now() - timedelta(days=CACHE_DAYS)).isoformat(timespec="seconds")
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT id, topic, article, fragments_json, pictures_json FROM library_stories
            WHERE story_key = ? AND created_at >= ?
            ORDER BY id DESC LIMIT 1
            """,
            (key, since),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "id": row[0], "topic": row[1], "article": row[2],
        "fragments": json.loads(row[3]), "pictures": json.loads(row[4]),
    }


def save_story(db_path, key, topic, article, fragments, pictures, user_id=None):
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO library_stories
                (story_key, topic, article, fragments_json, pictures_json, user_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key, topic[:MAX_TOPIC_CHARS], article,
                json.dumps(fragments, ensure_ascii=False),
                json.dumps(pictures, ensure_ascii=False),
                user_id, _now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_story(db_path, story_id):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, topic, article, fragments_json, pictures_json FROM library_stories WHERE id = ?",
            (story_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "id": row[0], "topic": row[1], "article": row[2],
        "fragments": json.loads(row[3]), "pictures": json.loads(row[4]),
    }


# --- поиск и фрагменты ---

def find_pages(db_path, topic):
    """Бесплатный поиск. Возвращает (plan, results)."""
    plan = library_search.build_search_plan(topic[:MAX_TOPIC_CHARS])
    if not plan.groups:
        return plan, []
    return plan, library_search.search_pages(db_path, plan, limit=SEARCH_LIMIT)


def content_pages(results):
    """Лучшие содержательные страницы, не больше MAX_PAGES_PER_BOOK с книги."""
    good = [r for r in results if r.get("quality", 1) >= MIN_PAGE_QUALITY]
    pages = good or results
    if pages and pages[0].get("score"):
        best = pages[0]["score"]
        pages = [r for r in pages if r.get("score", best) >= best * MIN_RELATIVE_SCORE]
    chosen, per_book = [], {}
    for result in pages:
        code = result.get("document_code") or result.get("title")
        if per_book.get(code, 0) >= MAX_PAGES_PER_BOOK:
            continue
        per_book[code] = per_book.get(code, 0) + 1
        chosen.append(result)
        if len(chosen) >= MAX_PAGES:
            break
    return chosen


def select_fragments(results):
    fragments, total = [], 0
    for result in content_pages(results):
        for text in library_search.passages(result, max_passages=MAX_PASSAGES_PER_PAGE):
            text = text[:MAX_FRAGMENT_CHARS]
            if len(fragments) >= MAX_FRAGMENTS or total + len(text) > MAX_CONTEXT_CHARS:
                return fragments
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


def clean_article(article, fragments):
    valid = {f["n"] for f in fragments}

    def keep(match):
        return match.group(0) if int(match.group(1)) in valid else ""

    text = _CITATION_WITH_SPACE.sub(keep, article or "")
    # Звёздочки и прочий Markdown, если модель всё-таки их поставила.
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?m)^\s*[*•]\s+", "— ", text)
    return text.strip()


def has_info(article):
    return bool(article) and NO_INFO not in article.upper()[:40]


def cited_numbers(article, fragments):
    valid = {f["n"] for f in fragments}
    cited = []
    for match in _CITATION.finditer(article or ""):
        number = int(match.group(1))
        if number in valid and number not in cited:
            cited.append(number)
    return cited


def write(topic, fragments, user_id=None):
    """Один вызов gpt-4.1-mini. Бросает lucy_ai.AIUnavailable при проблемах."""
    if not enabled():
        raise lucy_ai.AIUnavailable("disabled")
    article = lucy_ai.ask_openai(
        build_messages(topic, fragments),
        max_tokens=MAX_ARTICLE_TOKENS,
        temperature=0.3,
        check_enabled=False,
        timeout=90.0,
        kind="library_story",
        user_id=user_id,
    )
    return clean_article(article, fragments)


# --- картинки ---

def _page_rows(db_path, codes):
    if not codes:
        return {}
    conn = sqlite3.connect(db_path)
    try:
        marks = ",".join("?" for _ in codes)
        rows = conn.execute(
            f"SELECT document_code, id, storage_key FROM library_documents WHERE document_code IN ({marks})",
            list(codes),
        ).fetchall()
    finally:
        conn.close()
    return {code: (doc_id, storage) for code, doc_id, storage in rows}


def _illustration_info(db_path, doc_ids):
    """{(document_id, pdf_page): (illustration_score, captions)} из visual_page_index."""
    if not doc_ids:
        return {}
    conn = sqlite3.connect(db_path)
    try:
        marks = ",".join("?" for _ in doc_ids)
        try:
            rows = conn.execute(
                f"SELECT document_id, pdf_page, illustration_score, captions "
                f"FROM visual_page_index WHERE document_id IN ({marks})",
                list(doc_ids),
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
    finally:
        conn.close()
    info = {}
    for doc_id, page, score, captions in rows:
        try:
            caption_list = json.loads(captions) if captions else []
        except (TypeError, ValueError):
            caption_list = []
        info[(doc_id, page)] = (score or 0.0, caption_list)
    return info


def _caption_for(captions, terms):
    terms = [t.lower() for t in terms if len(t) >= 3]
    for caption in captions or []:
        text = str(caption)
        lower = text.lower()
        if any(term in lower for term in terms):
            return text[:160]
    return None


def pick_pictures(db_path, results, fragments, terms, limit=None):
    """Иллюстрированные страницы, где говорится о теме. Без ИИ.

    Порядок: страницы, на которые ссылается статья, и страницы, где тема
    есть в подписи к иллюстрации, — выше. Не больше 2 картинок с книги.
    """
    limit = pictures_limit() if limit is None else limit
    if limit <= 0 or not results:
        return []
    cited_pages = {(f["document_code"], f["pdf_page"]) for f in fragments}
    codes = {r.get("document_code") for r in results if r.get("document_code")}
    docs = _page_rows(db_path, codes)
    info = _illustration_info(db_path, {doc_id for doc_id, _ in docs.values()})

    scored, seen = [], set()
    for rank, result in enumerate(results):
        code = result.get("document_code")
        if code not in docs or (code, result["pdf_page"]) in seen:
            continue
        seen.add((code, result["pdf_page"]))
        if result.get("quality", 1) < 0.15:
            continue
        doc_id, storage = docs[code]
        if not storage:
            continue
        illustration, captions = info.get((doc_id, result["pdf_page"]), (0.0, []))
        if illustration < MIN_ILLUSTRATION:
            continue
        caption = _caption_for(captions, terms)
        score = illustration
        score += 0.6 if caption else 0.0
        score += 0.4 if (code, result["pdf_page"]) in cited_pages else 0.0
        score += max(0.0, 0.3 - rank * 0.01)
        scored.append((score, {
            "document_code": code,
            "storage_key": storage,
            "title": result["title"],
            "pdf_page": result["pdf_page"],
            "printed_page": result.get("printed_page"),
            "caption": caption,
        }))

    scored.sort(key=lambda item: -item[0])
    chosen, per_book = [], {}
    for _, picture in scored:
        code = picture["document_code"]
        if per_book.get(code, 0) >= MAX_PICTURES_PER_BOOK:
            continue
        per_book[code] = per_book.get(code, 0) + 1
        chosen.append(picture)
        if len(chosen) >= limit:
            break
    return chosen


def trim_margins(data, max_side=PICTURE_MAX_SIDE):
    """Обрезает белые поля страницы и уменьшает до max_side. Без ИИ."""
    from PIL import Image, ImageOps

    try:
        image = Image.open(io.BytesIO(data)).convert("RGB")
        gray = ImageOps.invert(image.convert("L"))
        # Всё светлее ~ 235 считается полем.
        mask = gray.point(lambda value: 255 if value > 20 else 0)
        box = mask.getbbox()
        if box:
            pad = 12
            box = (
                max(0, box[0] - pad), max(0, box[1] - pad),
                min(image.width, box[2] + pad), min(image.height, box[3] + pad),
            )
            if (box[2] - box[0]) * (box[3] - box[1]) > 0.15 * image.width * image.height:
                image = image.crop(box)
        image.thumbnail((max_side, max_side))
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=85)
        return out.getvalue()
    except Exception as error:
        print(f"story picture trim failed: {type(error).__name__}")
        return data


def picture_bytes(picture):
    """JPEG страницы для картинки (кэш R2 → рендер из PDF)."""
    import page_images

    data = page_images.page_image(
        picture["document_code"], picture["storage_key"], picture["pdf_page"], dpi=PICTURE_DPI
    )
    return trim_margins(data)


def page_label(item):
    label = f"PDF-стр. {item['pdf_page']}"
    if item.get("printed_page"):
        label += f" (в книге стр. {item['printed_page']})"
    return label


def picture_caption(picture, topic):
    lines = [f"🖼 {picture['title']} — {page_label(picture)}"]
    if picture.get("caption"):
        lines.append(f"Подпись в книге (OCR, без перевода): {picture['caption']}")
    else:
        lines.append(f"Страница книги, где говорится о теме «{topic[:60]}».")
    return "\n".join(lines)[:1000]


# --- оформление для Telegram (HTML) ---

def to_html(article):
    """# Заголовок → жирный, ## Раздел → жирный с отступом. Остальное экранируется."""
    lines = []
    for raw in (article or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("## "):
            lines.append("")
            lines.append(f"<b>{html.escape(stripped[3:].strip())}</b>")
        elif stripped.startswith("# "):
            lines.append(f"<b>📖 {html.escape(stripped[2:].strip().upper())}</b>")
        else:
            lines.append(html.escape(line))
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def sources_html(article, fragments):
    cited = cited_numbers(article, fragments)
    shown = [f for f in fragments if f["n"] in cited] or fragments
    lines = ["<b>📚 Источники</b>"]
    for f in shown:
        lines.append(html.escape(f"[{f['n']}] {f['title']} — {page_label(f)}"))
    lines.append(
        "\n💬 Статью собрала Люся с помощью ИИ только по фрагментам книг "
        "нашей библиотеки. Точный текст — по кнопкам «📄 Оригинал»."
    )
    return "\n".join(lines), [f["n"] for f in shown]


def split_messages(text, limit=TELEGRAM_LIMIT):
    """Делит по абзацам, не разрывая HTML-теги внутри строки."""
    parts, current = [], ""
    for paragraph in text.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(paragraph) > limit:
            cut = paragraph.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            parts.append(paragraph[:cut])
            paragraph = paragraph[cut:].lstrip()
        current = paragraph
    if current:
        parts.append(current)
    return parts


def format_story(story):
    """Список HTML-сообщений: статья (одно или несколько) + источники."""
    body = to_html(story["article"])
    sources, numbers = sources_html(story["article"], story["fragments"])
    parts = split_messages(body)
    if parts and len(parts[-1]) + len(sources) + 2 <= TELEGRAM_LIMIT:
        parts[-1] = parts[-1] + "\n\n" + sources
    else:
        parts.append(sources)
    return parts, numbers


def get_fragment(db_path, story_id, number):
    story = get_story(db_path, story_id)
    if story is None:
        return None
    for fragment in story["fragments"]:
        if fragment["n"] == number:
            return fragment
    return None
