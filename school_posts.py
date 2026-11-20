"""Редакция школы: статья из досье → черновик → правка Кристины → публикация в тему группы.

Как это работает:
1. Досье по теме собирает /article (как раньше).
2. Кнопка «✍️ Сделать статью» — один недорогой вызов ИИ превращает досье в статью для
   девочек: заголовок, вступление, разделы. Только факты из досье, с номерами фрагментов —
   по ним бот сам добавляет источники (книга, страница). Номера в тексте статьи не показываются.
3. Кристина читает черновик в личке с Люсей: может заменить текст целиком, дописать своё,
   поставить обложку, выбрать тему группы.
4. «Опубликовать» — Люся отправляет статью в выбранную тему группы, а в теме обновляет
   закреплённое оглавление (все статьи темы со ссылками).
5. Статью можно поправить и после публикации: «Обновить в группе» меняет те же сообщения.

Темы группы Люся узнаёт так: Кристина пишет /tema в нужной теме и выбирает её название кнопкой.

Таблицы:
- school_topics   — тема группы: где она (chat_id, thread_id) и сообщение-оглавление;
- school_articles — статьи: текст, тема, обложка, опубликованные сообщения.
"""

import html
import json
import re
import sqlite3
from datetime import datetime, timezone

# Темы группы (схема из 13 тем, согласована с Кристиной). slug — короткое имя для кнопок.
TOPICS = [
    ("start", "Старт"),
    ("club", "Vintage Club"),
    ("opredelitel", "Определитель"),
    ("klejma", "Архив клейм"),
    ("vremya", "Машина времени"),
    ("lab", "Лаборатория материалов"),
    ("anatomy", "Анатомия украшения"),
    ("moda", "Дом моды"),
    ("crime", "Vintage Crime"),
    ("ohota", "Охота и продажа"),
    ("library", "Секретная библиотека"),
    ("rassled", "Наши расследования"),
    ("private", "Private Room"),
]
TOPIC_NAMES = dict(TOPICS)
# В какие темы идут статьи (остальные — для общения и служебные).
ARTICLE_TOPICS = ["vremya", "lab", "anatomy", "moda", "crime", "klejma", "ohota", "library"]

MAX_MESSAGE = 3900      # запас до лимита Telegram 4096
MAX_CAPTION = 1000      # подпись к фото — до 1024
POST_MAX_TOKENS = 2200

POST_PROMPT = """Ты — Люся, редактор OH MY GOD VINTAGE School, школы винтажных украшений для девочек-реселлеров. Кристина, хозяйка школы, попросила превратить досье в статью для группы в Telegram. Досье собрано по книгам её библиотеки; после каждого факта стоит номер фрагмента: [12].

Напиши статью по-русски: живо, тепло и по делу, как опытная подруга объясняет новичкам. Без воды, без пафоса, без канцелярита. Обращайся на «ты».

Правила — без исключений:
1. Только факты из досье. Ничего не добавляй из своих знаний: ни дат, ни имён, ни фирм, ни клейм.
2. После каждого факта оставь его номер, как в досье: [12]. Новых номеров не придумывай.
3. Раздел «СОМНЕНИЯ» и «ЧЕГО В КНИГАХ НЕТ» из досье в статью не переноси — вместо этого, если важно, одна честная фраза в конце: что ещё стоит проверить.
4. Не пиши названия книг и страницы — бот добавит источники сам.
5. Без Markdown: без звёздочек, решёток, таблиц.

Формат ответа строго такой:
ЗАГОЛОВОК: короткий цепляющий заголовок (до 70 знаков)
ВСТУПЛЕНИЕ: 2–3 предложения, зачем это знать
РАЗДЕЛ: название раздела
текст раздела, абзацы через пустую строку; списки — строки, начинающиеся с «— »
РАЗДЕЛ: название следующего раздела
...

3–6 разделов. Обязательно раздел «Как узнать» или «На что смотреть», если в досье есть признаки. Общая длина — до 3500 знаков."""

_CITE = re.compile(r"\s*\[(\d{1,4})\]")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables(db_path):
    conn = connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS school_topics (
                slug TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                thread_id INTEGER,
                toc_message_id INTEGER,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS school_articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER,
                theme TEXT NOT NULL,
                topic_slug TEXT,
                title TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                sources_json TEXT NOT NULL DEFAULT '[]',
                cover_file_id TEXT,
                status TEXT NOT NULL DEFAULT 'draft',
                messages_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                published_at TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


# ───────────── Темы группы ─────────────

def save_topic(db_path, slug, chat_id, thread_id):
    if slug not in TOPIC_NAMES:
        raise ValueError(slug)
    conn = connect(db_path)
    try:
        old = conn.execute("SELECT chat_id, thread_id FROM school_topics WHERE slug = ?", (slug,)).fetchone()
        # Тему перенесли в другое место — старое оглавление больше не наше.
        keep_toc = old is not None and old["chat_id"] == chat_id and old["thread_id"] == thread_id
        conn.execute(
            """
            INSERT INTO school_topics (slug, chat_id, thread_id, toc_message_id, updated_at)
            VALUES (?, ?, ?, NULL, ?)
            ON CONFLICT(slug) DO UPDATE SET chat_id = excluded.chat_id, thread_id = excluded.thread_id,
              toc_message_id = CASE WHEN ? THEN school_topics.toc_message_id ELSE NULL END,
              updated_at = excluded.updated_at
            """,
            (slug, chat_id, thread_id, _now(), 1 if keep_toc else 0),
        )
        conn.commit()
    finally:
        conn.close()


def get_topic(db_path, slug):
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT * FROM school_topics WHERE slug = ?", (slug,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def topics_status(db_path):
    """[(slug, name, привязана ли)] — для подсказки «где какая тема»."""
    conn = connect(db_path)
    try:
        known = {r["slug"] for r in conn.execute("SELECT slug FROM school_topics")}
    finally:
        conn.close()
    return [(slug, name, slug in known) for slug, name in TOPICS]


def set_toc_message(db_path, slug, message_id):
    conn = connect(db_path)
    try:
        conn.execute("UPDATE school_topics SET toc_message_id = ?, updated_at = ? WHERE slug = ?", (message_id, _now(), slug))
        conn.commit()
    finally:
        conn.close()


# ───────────── Статьи ─────────────

def _row(row):
    if row is None:
        return None
    d = dict(row)
    d["sources"] = json.loads(d.pop("sources_json") or "[]")
    d["messages"] = json.loads(d.pop("messages_json") or "[]")
    return d


def create_article(db_path, job_id, theme, title, body, sources):
    conn = connect(db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO school_articles (job_id, theme, title, body, sources_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, theme, title, body, json.dumps(sources, ensure_ascii=False), _now(), _now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_article(db_path, article_id):
    conn = connect(db_path)
    try:
        return _row(conn.execute("SELECT * FROM school_articles WHERE id = ?", (article_id,)).fetchone())
    finally:
        conn.close()


def update_article(db_path, article_id, **fields):
    allowed = {"title", "body", "topic_slug", "cover_file_id", "status", "published_at"}
    sets, values = [], []
    for key, value in fields.items():
        if key == "messages":
            sets.append("messages_json = ?")
            values.append(json.dumps(value))
        elif key in allowed:
            sets.append(f"{key} = ?")
            values.append(value)
        else:
            raise ValueError(key)
    sets.append("updated_at = ?")
    values.append(_now())
    conn = connect(db_path)
    try:
        conn.execute(f"UPDATE school_articles SET {', '.join(sets)} WHERE id = ?", (*values, article_id))
        conn.commit()
    finally:
        conn.close()


def delete_article(db_path, article_id):
    conn = connect(db_path)
    try:
        conn.execute("DELETE FROM school_articles WHERE id = ? AND status = 'draft'", (article_id,))
        conn.commit()
    finally:
        conn.close()


def published_in_topic(db_path, slug):
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM school_articles WHERE topic_slug = ? AND status = 'published' ORDER BY published_at",
            (slug,),
        ).fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


def drafts(db_path, limit=10):
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM school_articles ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


# ───────────── Из досье в статью ─────────────

def post_messages(theme, dossier):
    return [
        {"role": "system", "content": POST_PROMPT},
        {"role": "user", "content": f"Тема: {theme}\n\nДосье:\n\n{dossier}"},
    ]


def parse_post(text):
    """Ответ ИИ → (заголовок, тело). Тело — простой текст: вступление и разделы «РАЗДЕЛ: …»."""
    title, lines = "", []
    for raw in (text or "").replace("\r", "").split("\n"):
        line = raw.strip().replace("**", "")
        line = re.sub(r"^#+\s*", "", line)
        if line.upper().startswith("ЗАГОЛОВОК:"):
            title = line.split(":", 1)[1].strip()
        elif line.upper().startswith("ВСТУПЛЕНИЕ:"):
            lines.append(line.split(":", 1)[1].strip())
            lines.append("")
        else:
            lines.append(line)
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return title.strip(" «»\""), body


def sources_for(text, fragments, source_line):
    """Источники, на которые есть ссылки в тексте: [n] → «Книга — стр.» (без повторов, по порядку)."""
    valid = {f["n"]: f for f in fragments}
    seen, out = set(), []
    for m in _CITE.finditer(text or ""):
        n = int(m.group(1))
        if n in valid and n not in seen:
            seen.add(n)
            # «[3] Книга (DOC-…) — стр. 5» → «Книга — стр. 5»: номер и код документа девочкам не нужны.
            line = re.sub(r"^\[\d+\]\s*", "", source_line(valid[n]))
            line = re.sub(r"\s*\((DOC-[^)]*|—)\)", "", line)
            if line not in out:
                out.append(line)
    return out


def strip_cites(text):
    return _CITE.sub("", text or "")


# ───────────── Как статья выглядит в Telegram ─────────────

def _esc(s):
    return html.escape(s, quote=False)


def render_body(body):
    """Тело статьи → HTML для Telegram: «РАЗДЕЛ: X» становится жирным заголовком, «— » — пунктом."""
    out = []
    for line in strip_cites(body).split("\n"):
        s = line.strip()
        if s.upper().startswith("РАЗДЕЛ:"):
            out.append(f"\n<b>{_esc(s.split(':', 1)[1].strip())}</b>")
        elif s.startswith("— ") or s.startswith("- "):
            out.append("▫️ " + _esc(s[2:].strip()))
        else:
            out.append(_esc(s))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def render_parts(article, body_html=None, with_title=True):
    """Готовые к отправке куски (HTML), каждый ≤ MAX_MESSAGE.
    Первый кусок — заголовок и начало; последний заканчивается источниками и подписью школы.
    body_html — уже готовое тело (текст, который Кристина прислала сама); иначе собираем из article["body"]."""
    head = f"<b>{_esc(article['title'] or article['theme'])}</b>"
    body = render_body(article["body"]) if body_html is None else body_html
    tail = ""
    if article.get("sources"):
        tail = "\n\n<i>📖 По книгам библиотеки школы:</i>\n" + "\n".join(f"<i>· {_esc(s)}</i>" for s in article["sources"])
    tail += "\n\n✦ OH MY GOD VINTAGE School"
    paragraphs = ([head] if with_title else []) + [p for p in body.split("\n\n") if p.strip()] + [tail.strip()]
    parts, cur = [], ""
    for p in paragraphs:
        chunk = p if not cur else cur + "\n\n" + p
        if len(chunk) <= MAX_MESSAGE:
            cur = chunk
            continue
        if cur:
            parts.append(cur)
        while len(p) > MAX_MESSAGE:  # очень длинный абзац — режем по строкам
            cut = p.rfind("\n", 0, MAX_MESSAGE)
            cut = cut if cut > 0 else MAX_MESSAGE
            parts.append(p[:cut])
            p = p[cut:].lstrip("\n")
        cur = p
    if cur:
        parts.append(cur)
    return parts


def cover_caption(article):
    """Подпись к обложке: заголовок + первые строки (≤ MAX_CAPTION)."""
    title = f"<b>{_esc(article['title'] or article['theme'])}</b>"
    first = render_body(article["body"]).split("\n\n")[0]
    cap = f"{title}\n\n{first}"
    return cap if len(cap) <= MAX_CAPTION else title


def message_link(chat_id, message_id, thread_id=None):
    """Ссылка на сообщение закрытой группы: -1001234567890 → t.me/c/1234567890/…"""
    internal = str(chat_id)
    internal = internal[4:] if internal.startswith("-100") else internal.lstrip("-")
    if thread_id:
        return f"https://t.me/c/{internal}/{thread_id}/{message_id}"
    return f"https://t.me/c/{internal}/{message_id}"


def toc_text(slug, articles, chat_id, thread_id):
    """Оглавление темы: все опубликованные статьи со ссылками (HTML)."""
    lines = [f"📚 <b>Статьи темы «{_esc(TOPIC_NAMES.get(slug, slug))}»</b>", ""]
    if not articles:
        lines.append("Скоро здесь появятся первые статьи ✨")
    for i, a in enumerate(articles, 1):
        first = (a.get("messages") or [None])[0]
        title = _esc(a["title"] or a["theme"])
        lines.append(f"{i}. <a href=\"{message_link(chat_id, first, thread_id)}\">{title}</a>" if first else f"{i}. {title}")
    lines += ["", "<i>Оглавление обновляется само, когда выходит новая статья.</i>"]
    return "\n".join(lines)
