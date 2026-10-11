"""Каталог библиотеки: названия, авторы, дубликаты. Без платного ИИ.

Название и автор определяются по уже сохранённому OCR первых страниц
(обложка, титульный лист, выходные данные). Если уверенности нет —
поле остаётся пустым, и каталог пишет «Название не определено».
Ничего не выдумываем: значение берётся только из текста книги или
вручную от администратора.

Метаданные хранятся в отдельных колонках library_documents
(meta_title, meta_author, meta_source). Исходное поле title (имя файла)
не меняется. Страницы, PDF и документы не удаляются.
"""

import hashlib
import os
import re
import sqlite3
from collections import defaultdict

from contextlib import contextmanager

import library_db


@contextmanager
def get_connection():
    """Соединение с базой: коммит при успехе и обязательное закрытие."""
    conn = library_db.get_connection()
    try:
        with conn:
            yield conn
    finally:
        conn.close()

FIRST_PAGES = 8          # сколько первых страниц смотреть
TITLE_PAGES = 4          # на скольких искать сам заголовок
MAX_TITLE_PAGE_LINES = 25

UNDEFINED_TITLE = "Название не определено"

# Строки, которые не бывают названием книги.
NOT_TITLE_MARKERS = [
    "isbn", "copyright", "©", "(c)", "all rights", "rights reserved", "printed",
    "published", "publisher", "publications", "publishing", "ltd", "limited",
    "edition", "www.", "http", "library of congress", "cataloguing",
    "contents", "index", "introduction", "chapter", "preface", "foreword",
    "illustrations", "acknowledg", "bibliography", "first published",
    "tutti i diritti", "stampato", "editore", "alle rechte", "verlag",
    "gedruckt", "tous droits", "imprimé", "éditions", "wydawnictwo",
    "nakladatel", "vydal", "содержание", "оглавление", "издательство",
]
NOT_TITLE_EXACT = {"contents", "index", "introduction", "preface", "foreword"}

SMALL_WORDS = {
    "and", "of", "the", "in", "on", "for", "to", "a", "an", "at", "by", "with",
    "di", "e", "del", "della", "delle", "dei", "il", "la", "le", "lo", "gli",
    "und", "der", "die", "das", "des", "von", "im", "zur", "zum",
    "et", "du", "de", "les", "aux", "en", "y", "a", "i", "o", "w", "z", "na",
    "и", "в", "на", "о", "с", "по", "для",
}

BAD_TITLE_STARTS = {"and", "of", "und", "e", "et", "ed", "di", "del", "des", "и", "или", "&"}

NAME_PARTICLES = {"de", "di", "da", "van", "von", "del", "della", "le", "la", "du", "der"}

_NAME_WORD = r"[A-ZÀ-ÖØ-ÝА-ЯЁ][A-Za-zÀ-ÖØ-öø-ÿА-Яа-яЁё'’\-]+\.?"
_NAME = rf"{_NAME_WORD}(?:[ \t]+(?:{_NAME_WORD}|{'|'.join(NAME_PARTICLES)})){{1,3}}"
_COPYRIGHT_NAME = re.compile(
    rf"(?i:copyright|©|\(c\))[ \t]*(?i:©|\(c\))?[ \t]*(?:\d{{4}}[ \t,]*)?"
    rf"(?i:by[ \t]+)?({_NAME})"
)
_BY_NAME = re.compile(
    rf"^[ \t]*(?i:by|di|von|par|de|autor:?|author:?|автор:?)[ \t]+({_NAME})[ \t]*$",
    re.MULTILINE,
)

# Издательства и организации — не авторы.
ORGANIZATION_WORDS = [
    "publication", "publishing", "publisher", "press", "ltd", "limited", "inc",
    "company", "books", "museum", "society", "verlag", "editore", "edizioni",
    "éditions", "editions", "wydawnictwo", "nakladatel", "издательство",
]


# --- нормализация ---

def normalize(text):
    text = (text or "").lower().replace("ё", "е")
    text = re.sub(r"[^\w]+", " ", text)
    return " ".join(text.split())


def _letters(text):
    return sum(1 for ch in text if ch.isalpha())


def smart_case(text):
    """«JET JEWELLERY AND ORNAMENTS» → «Jet Jewellery and Ornaments».
    Строки в обычном регистре не трогаем."""
    result = []
    for index, word in enumerate(text.split()):
        letters = [ch for ch in word if ch.isalpha()]
        shouting = len(letters) > 1 and all(ch.isupper() for ch in letters)
        if not shouting:
            result.append(word)
        elif index > 0 and word.lower() in SMALL_WORDS:
            result.append(word.lower())
        else:
            lower = word.lower()
            result.append(lower[:1].upper() + lower[1:])
    return " ".join(result)


def _clean_name(name):
    name = re.sub(r"[\d,;:]+.*$", "", name).strip(" .,-")
    return smart_case(name)


# --- определение названия и автора ---

def _title_like(line):
    lower = line.lower()
    if any(marker in lower for marker in NOT_TITLE_MARKERS):
        return False
    if normalize(line) in NOT_TITLE_EXACT:
        return False
    if _letters(line) < 3:
        return False
    if len(re.findall(r"\d", line)) > 4:
        return False
    tokens = line.split()
    if not tokens or len(tokens) > 12:
        return False
    # Мусор OCR: мало букв относительно длины.
    return _letters(line) / max(1, len(line.replace(" ", ""))) >= 0.75


def _filename_overlap(candidate, filename_title):
    words = {w for w in normalize(filename_title).split() if len(w) >= 3}
    cand = {w for w in normalize(candidate).split() if len(w) >= 3}
    if not words or not cand:
        return 0.0
    return len(words & cand) / len(cand)


def detect_metadata(pages, filename_title=""):
    """Определяет название и автора по первым страницам.

    pages — список (pdf_page, текст) в порядке страниц.
    Возвращает dict: title, author (None, если нет уверенности),
    title_candidate, author_candidate (лучшая догадка для подсказки
    администратору), empty_pages, reason.
    """
    pages = [(number, text or "") for number, text in pages][:FIRST_PAGES]
    texts = [text for _, text in pages]
    normalized = [normalize(text) for text in texts]
    empty_pages = [number for number, text in pages if _letters(text) < 10]

    # --- автор ---
    explicit = []
    for text in texts:
        explicit += [_clean_name(m.group(1)) for m in _COPYRIGHT_NAME.finditer(text)]
        explicit += [_clean_name(m.group(1)) for m in _BY_NAME.finditer(text)]
    explicit = [
        name for name in explicit
        if len(name.split()) >= 2
        and not any(word in name.lower() for word in ORGANIZATION_WORDS)
    ]

    name_lines = []
    for text in texts[:TITLE_PAGES]:
        for line in text.splitlines():
            line = line.strip()
            connectors = SMALL_WORDS - NAME_PARTICLES
            if (
                re.fullmatch(_NAME, line)
                and not any(marker in line.lower() for marker in NOT_TITLE_MARKERS)
                and not any(word.lower() in connectors for word in line.split())
            ):
                name_lines.append(smart_case(line))

    def pages_with(candidate):
        key = normalize(candidate)
        return sum(1 for text in normalized if key and key in text)

    author, author_candidate = None, None
    if explicit:
        author = max(explicit, key=lambda name: (pages_with(name), len(name)))
    # Явный автор (из «© …» или «by …») не склеивается с названием.
    name_keys = {normalize(name) for name in explicit}

    # --- название ---
    candidates = {}
    for index, (number, text) in enumerate(pages[:TITLE_PAGES]):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines or len(lines) > MAX_TITLE_PAGE_LINES:
            continue
        for start in range(len(lines)):
            for size in (1, 2, 3):
                window = lines[start:start + size]
                if len(window) < size or not all(_title_like(line) for line in window):
                    continue
                if any(normalize(line) in name_keys for line in window):
                    continue
                candidate = " ".join(window)
                key = normalize(candidate)
                if len(key) < 6 or len(key.split()) < 2:
                    continue
                # Название не начинается с союза — это обрывок строки.
                if key.split()[0] in BAD_TITLE_STARTS:
                    continue
                if author and normalize(author) in key:
                    continue
                repeats = sum(
                    1 for other, text_norm in enumerate(normalized)
                    if other != index and key in text_norm
                )
                overlap = _filename_overlap(candidate, filename_title)
                score = (repeats, round(overlap, 1), len(key.split()))
                if key not in candidates or score > candidates[key][0]:
                    candidates[key] = (score, candidate)

    title, title_candidate = None, None
    if candidates:
        (repeats, overlap, _), best = max(candidates.values())
        if repeats >= 1 or overlap >= 0.5:
            title = smart_case(best)
        else:
            title_candidate = smart_case(best)

    if not author:
        title_key = normalize(title or "")
        others = [
            name for name in name_lines
            if not title_key or normalize(name) not in title_key
        ]
        repeated = [name for name in others if pages_with(name) >= 2]
        if repeated:
            author = max(repeated, key=pages_with)
        elif others:
            author_candidate = others[0]

    reason = None
    if not title:
        if len(empty_pages) >= min(2, len(pages)) and pages:
            reason = "первые страницы почти без текста — попробуйте /fixmeta с ocr"
        else:
            reason = "название не повторяется на других страницах"

    return {
        "title": title,
        "author": author,
        "title_candidate": title_candidate,
        "author_candidate": author_candidate,
        "empty_pages": empty_pages,
        "reason": reason,
    }


# --- база ---

def first_pages(document_id, limit=FIRST_PAGES):
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT pdf_page, COALESCE(original_text, cleaned_text, '')
            FROM library_pages
            WHERE document_id = ?
            ORDER BY pdf_page
            LIMIT ?
            """,
            (document_id, limit),
        ).fetchall()


def get_document(document_code):
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT id, document_code, title, storage_key, language,
                   meta_title, meta_author, meta_source
            FROM library_documents WHERE document_code = ?
            """,
            (document_code.upper(),),
        ).fetchone()


def save_metadata(document_id, title, author, source):
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE library_documents
            SET meta_title = ?, meta_author = ?, meta_source = ?,
                meta_checked_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (title, author, source, document_id),
        )


def mark_checked(document_id):
    with get_connection() as conn:
        conn.execute(
            "UPDATE library_documents SET meta_checked_at = CURRENT_TIMESTAMP WHERE id = ?",
            (document_id,),
        )


def refresh_document_metadata(document_id, pages=None, force=False):
    """Определяет и сохраняет название/автора одной книги.

    Ручные названия (meta_source='manual') не перезаписываются без force.
    Неуверенный результат не сохраняется — остаётся «не определено».
    Возвращает (detected: dict, saved: bool, skipped_manual: bool).
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT title, meta_source FROM library_documents WHERE id = ?",
            (document_id,),
        ).fetchone()
    if row is None:
        raise LookupError("Документ не найден")
    filename_title, source = row

    if pages is None:
        pages = first_pages(document_id)
    detected = detect_metadata(pages, filename_title)

    if source == "manual" and not force:
        mark_checked(document_id)
        return detected, False, True

    if detected["title"] or detected["author"]:
        save_metadata(document_id, detected["title"], detected["author"], "auto")
        return detected, True, False

    mark_checked(document_id)
    return detected, False, False


def set_manual_metadata(document_code, title, author=None):
    document = get_document(document_code)
    if document is None:
        raise LookupError(f"Документ {document_code.upper()} не найден")
    save_metadata(document[0], title.strip(), (author or "").strip() or None, "manual")
    return document[1]


def ocr_title_pages(document_code, pages=3, dpi=400):
    """Заново распознаёт первые страницы PDF из R2 — только для поиска
    названия. В library_pages ничего не записывается."""
    import shutil
    import tempfile

    from library_importer import download_pdf_from_r2, inspect_pdf, ocr_pdf_page

    document = get_document(document_code)
    if document is None:
        raise LookupError(f"Документ {document_code.upper()} не найден")
    document_id, _, _, storage_key, language = document[:5]
    if not storage_key or not storage_key.startswith("books/"):
        raise LookupError(f"У {document[1]} нет PDF в хранилище")

    temp_dir = tempfile.mkdtemp(prefix="lucy_title_")
    try:
        path = os.path.join(temp_dir, "book.pdf")
        download_pdf_from_r2(storage_key, path)
        total = inspect_pdf(path)["page_count"]
        fresh = []
        for number in range(1, min(pages, total) + 1):
            fresh.append((number, ocr_pdf_page(path, number, language=language or "eng", dpi=dpi)))
        return document_id, fresh
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# --- каталог ---

def catalog():
    """Все документы из базы с метаданными и числом страниц."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                d.id, d.document_code, d.title, d.meta_title, d.meta_author,
                d.meta_source, d.language, d.storage_key, d.checksum,
                COALESCE(j.status, '—'), COALESCE(j.last_completed_page, 0),
                (SELECT COUNT(*) FROM library_pages p WHERE p.document_id = d.id),
                (SELECT COUNT(*) FROM library_pages p WHERE p.document_id = d.id
                   AND COALESCE(p.original_text, '') = '')
            FROM library_documents d
            LEFT JOIN library_import_jobs j ON j.id = (
                SELECT MAX(j2.id) FROM library_import_jobs j2 WHERE j2.document_id = d.id
            )
            ORDER BY d.id
            """
        ).fetchall()
    keys = [
        "id", "code", "file_title", "meta_title", "meta_author", "meta_source",
        "language", "storage_key", "checksum", "status", "last_page", "pages",
        "empty_pages",
    ]
    documents = [dict(zip(keys, row)) for row in rows]
    for document in documents:
        document["is_book"] = (document["storage_key"] or "").startswith("books/")
    return documents


# --- дубликаты ---

def _fingerprint(document_id):
    """Отпечаток текста первых содержательных страниц (для старых записей
    без хеша PDF)."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(cleaned_text, original_text, '')
            FROM library_pages WHERE document_id = ?
            ORDER BY pdf_page LIMIT 12
            """,
            (document_id,),
        ).fetchall()
    text = " ".join(normalize(row[0]) for row in rows if _letters(row[0] or "") >= 40)
    text = text[:4000]
    return hashlib.sha1(text.encode("utf-8")).hexdigest() if len(text) >= 200 else None


def find_duplicates(documents=None):
    """Возвращает (точные, возможные).

    Точные — одинаковый SHA-256 исходного PDF.
    Возможные — одинаковый текст первых страниц, или одинаковое название
    при одинаковом числе страниц. Ничего не удаляется.
    """
    documents = documents if documents is not None else catalog()
    books = [d for d in documents if d["is_book"]]

    by_hash = defaultdict(list)
    for document in books:
        if document["checksum"]:
            by_hash[document["checksum"]].append(document["code"])
    exact = [sorted(codes) for codes in by_hash.values() if len(codes) > 1]
    exact_pairs = {frozenset(group) for group in exact}

    possible = []
    by_print = defaultdict(list)
    by_title = defaultdict(list)
    for document in books:
        fingerprint = _fingerprint(document["id"])
        if fingerprint:
            by_print[fingerprint].append(document["code"])
        title = normalize(document["meta_title"] or "")
        if title and document["pages"]:
            by_title[(title, document["pages"])].append(document["code"])

    seen = set()
    for reason, groups in (
        ("одинаковый текст первых страниц", by_print.values()),
        ("одинаковое название и число страниц", by_title.values()),
    ):
        for codes in groups:
            group = frozenset(codes)
            if len(codes) > 1 and group not in exact_pairs and group not in seen:
                seen.add(group)
                possible.append((sorted(codes), reason))
    return exact, possible


def documents_without_hash(documents=None):
    documents = documents if documents is not None else catalog()
    return [d for d in documents if d["is_book"] and not d["checksum"]]


def fill_missing_checksums():
    """Считает SHA-256 исходных PDF из R2 для записей без хеша и записывает
    его только в пустое поле checksum. Возвращает (заполнено, ошибки)."""
    import shutil
    import tempfile

    from library_importer import calculate_checksum, download_pdf_from_r2

    filled, failed = [], []
    for document in documents_without_hash():
        temp_dir = tempfile.mkdtemp(prefix="lucy_hash_")
        try:
            path = os.path.join(temp_dir, "book.pdf")
            download_pdf_from_r2(document["storage_key"], path)
            checksum = calculate_checksum(path)
            with get_connection() as conn:
                conn.execute(
                    "UPDATE library_documents SET checksum = ? "
                    "WHERE id = ? AND (checksum IS NULL OR checksum = '')",
                    (checksum, document["id"]),
                )
            filled.append(document["code"])
        except Exception as error:
            print(f"checksum failed for {document['code']}: {type(error).__name__}")
            failed.append(document["code"])
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
    return filled, failed
