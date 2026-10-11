"""Бесплатный индекс страниц библиотеки для визуальной атрибуции.

Для каждой страницы каждой книги по уже сохранённому OCR (без ИИ):
- подписи к иллюстрациям (короткие строки с Fig./Plate/No./названиями);
- годы (1850–2029), цены и признаки рекламы/каталога;
- упомянутые марки бижутерии;
- оценка «страница с иллюстрацией» (мало текста или картинки в PDF);
- полнотекстовый индекс SQLite FTS5 для быстрого поиска кандидатов.

Индекс строится для уже загруженных книг без повторной загрузки PDF
(/vindex) и автоматически после распознавания новой книги.
Страницы книг (library_pages) только читаются.
"""

import json
import re
import sqlite3

import library_search

# Марки бижутерии и ювелирных домов — для связи страниц с производителем.
KNOWN_BRANDS = [
    "sarah coventry", "coventry", "trifari", "coro", "corocraft", "napier", "monet",
    "miriam haskell", "haskell", "eisenberg", "weiss", "juliana", "schiaparelli",
    "boucher", "marcel boucher", "hobe", "hobé", "kramer", "lisner", "sherman",
    "regency", "kenneth jay lane", "kjl", "crown trifari", "florenza", "hollycraft",
    "art", "dodds", "emmons", "avon", "vendome", "vendôme", "jomaz", "mazer",
    "jeanne", "har", "selro", "bsk", "danecraft", "beau", "whiting & davis",
    "whiting and davis", "pennino", "reja", "mazer bros", "nettie rosenstein",
    "hattie carnegie", "carnegie", "chanel", "dior", "christian dior", "givenchy",
    "yves saint laurent", "ysl", "lanvin", "swarovski", "gripoix", "henkel & grosse",
    "henkel and grosse", "grosse", "jakob bengel", "bengel", "max neiger", "neiger",
    "jablonec", "gablonz", "bijoux cascio", "cascio", "sharra pagano", "pagano",
    "coppola e toppo", "coppola", "trifari", "boucheron", "cartier", "tiffany",
]
KNOWN_BRANDS = sorted(set(KNOWN_BRANDS), key=len, reverse=True)
# Слишком короткие/общие слова считаются маркой только рядом с ювелирными словами.
WEAK_BRANDS = {"art", "har", "beau", "jeanne", "avon", "coventry"}

AD_MARKERS = [
    "$", "¢", "only", "price", "priced", "order", "available at", "on sale",
    "advertisement", "advertising", "catalog", "catalogue", "style no", "no.",
    "item", "matching", "set includes", "complete set", "gift", "ensemble",
    "parure", "suite", "presents", "new for", "collection", "lire", "dm ",
    "katalog", "anzeige", "pubblicit", "listino", "prix", "publicité", "inzerát",
    "reklama", "cena", "cenník",
]

CAPTION_MARKERS = re.compile(
    r"^(?:fig\.?|figure|plate|pl\.|ill\.|illus|abb\.|tav\.|tavola|obr\.|ryc\.|rys\.|no\.|nr\.|n°|#)\s*\d+",
    re.IGNORECASE,
)

JEWELRY_WORDS = [
    "necklace", "brooch", "pin", "earring", "bracelet", "bangle", "ring", "pendant",
    "parure", "demi-parure", "set", "choker", "lariat", "collar", "clip", "locket",
    "collana", "spilla", "orecchin", "braccial", "anello", "ciondolo",
    "halskette", "kette", "brosche", "ohrring", "ohrclip", "armband", "anhänger",
    "collier", "broche", "boucle", "bracelet", "bague", "pendentif",
    "náhrdelník", "brož", "náušnic", "náramek", "naszyjnik", "broszk", "kolczyk",
    "колье", "брошь", "серьги", "браслет", "кулон",
]

_YEAR = re.compile(r"(?<!\d)(18[5-9]\d|19\d\d|20[0-2]\d)(?!\d)")
_PRICE = re.compile(r"(?:\$\s?\d+(?:[.,]\d\d)?|\d+(?:[.,]\d\d)?\s?(?:¢|cents|lire|dm|mk|kč|zł))", re.IGNORECASE)
_QUOTED = re.compile(r"[\"“”«»„']([A-Z][^\"“”«»„']{2,40})[\"“”«»„']")


def init_tables(db_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS visual_page_index (
                document_id INTEGER NOT NULL,
                pdf_page INTEGER NOT NULL,
                document_code TEXT,
                captions TEXT,
                years TEXT,
                prices INTEGER DEFAULT 0,
                brands TEXT,
                ad_score REAL DEFAULT 0,
                illustration_score REAL DEFAULT 0,
                jewelry_words INTEGER DEFAULT 0,
                indexed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (document_id, pdf_page)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS visual_index_status (
                document_id INTEGER PRIMARY KEY,
                pages INTEGER,
                scanned INTEGER,
                indexed_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS visual_page_fts USING fts5(
                    document_id UNINDEXED, pdf_page UNINDEXED, body,
                    tokenize = "unicode61 remove_diacritics 2"
                )
                """
            )
        except sqlite3.OperationalError as error:
            # Без FTS5 поиск работает медленнее, через LIKE.
            print(f"FTS5 unavailable: {error}")
        conn.commit()
    finally:
        conn.close()


def has_fts(conn):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'visual_page_fts'"
    ).fetchone() is not None


# --- признаки страницы ---

def find_brands(text):
    lower = " " + re.sub(r"\s+", " ", (text or "").lower()) + " "
    found = []
    for brand in KNOWN_BRANDS:
        pattern = r"(?<![\w])" + re.escape(brand) + r"(?![\w])"
        if re.search(pattern, lower):
            if brand in WEAK_BRANDS and not any(word in lower for word in JEWELRY_WORDS):
                continue
            if any(brand in other for other in found):
                continue
            found.append(brand)
    return found


def find_captions(text):
    captions = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or len(line) > 160:
            continue
        lower = line.lower()
        is_caption = bool(CAPTION_MARKERS.match(line)) or bool(_QUOTED.search(line))
        if not is_caption and len(line) <= 100:
            has_jewelry = any(word in lower for word in JEWELRY_WORDS)
            has_detail = bool(_YEAR.search(line)) or bool(find_brands(line))
            is_caption = has_jewelry and has_detail
        if is_caption:
            captions.append(line)
    return captions[:20]


def page_features(text, image_coverage=None, scanned=False):
    """Признаки одной страницы по тексту (и, если есть, по доле картинок в PDF)."""
    text = text or ""
    lower = text.lower()
    words = re.findall(r"[^\W\d_]{2,}", text)
    years = sorted(set(int(y) for y in _YEAR.findall(text)))
    prices = len(_PRICE.findall(text))
    brands = find_brands(text)
    captions = find_captions(text)
    jewelry = sum(lower.count(word) for word in JEWELRY_WORDS)

    ad_hits = sum(1 for marker in AD_MARKERS if marker in lower)
    ad_score = min(1.0, 0.15 * ad_hits + 0.25 * min(prices, 3) + (0.2 if years and brands else 0))

    # Иллюстрация: мало текста на странице или большая доля картинок в PDF.
    text_sparse = 1.0 - min(1.0, len(words) / 350)
    if image_coverage is not None and not scanned:
        illustration = max(min(1.0, image_coverage * 1.2), text_sparse * 0.6)
    else:
        illustration = text_sparse
    if captions:
        illustration = min(1.0, illustration + 0.2)

    return {
        "captions": captions,
        "years": years,
        "prices": prices,
        "brands": brands,
        "ad_score": round(ad_score, 3),
        "illustration_score": round(illustration, 3),
        "jewelry_words": jewelry,
    }


def _image_coverage(pdf_path):
    """Доля площади каждой страницы под картинками (по PDF). None без PDF."""
    try:
        import fitz
    except ImportError:
        return None, False
    coverage = {}
    with fitz.open(pdf_path) as document:
        for index in range(document.page_count):
            page = document.load_page(index)
            area = page.rect.width * page.rect.height or 1
            covered = 0.0
            for info in page.get_image_info():
                x0, y0, x1, y1 = info.get("bbox", (0, 0, 0, 0))
                covered += max(0, x1 - x0) * max(0, y1 - y0)
            coverage[index + 1] = min(1.0, covered / area)
    values = list(coverage.values())
    # Скан: почти каждая страница — одна большая картинка.
    scanned = bool(values) and sum(1 for v in values if v > 0.85) / len(values) > 0.8
    return coverage, scanned


def index_document(db_path, document_id, pdf_path=None):
    """Строит (или перестраивает) индекс одной книги. Возвращает число страниц."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT document_code FROM library_documents WHERE id = ?", (document_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"Документ {document_id} не найден")
        document_code = row[0]
        pages = conn.execute(
            """
            SELECT pdf_page, COALESCE(original_text, ''), COALESCE(cleaned_text, original_text, '')
            FROM library_pages WHERE document_id = ? ORDER BY pdf_page
            """,
            (document_id,),
        ).fetchall()
    finally:
        conn.close()

    coverage, scanned = (None, False)
    if pdf_path:
        try:
            coverage, scanned = _image_coverage(pdf_path)
        except Exception as error:
            # Без доли картинок индекс всё равно строится — по тексту.
            print(f"image coverage failed: {type(error).__name__}")

    conn = sqlite3.connect(db_path)
    try:
        fts = has_fts(conn)
        conn.execute("DELETE FROM visual_page_index WHERE document_id = ?", (document_id,))
        if fts:
            conn.execute("DELETE FROM visual_page_fts WHERE document_id = ?", (document_id,))
        for pdf_page, original, cleaned in pages:
            features = page_features(
                original, (coverage or {}).get(pdf_page), scanned
            )
            conn.execute(
                """
                INSERT INTO visual_page_index (
                    document_id, pdf_page, document_code, captions, years, prices,
                    brands, ad_score, illustration_score, jewelry_words
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id, pdf_page, document_code,
                    json.dumps(features["captions"], ensure_ascii=False),
                    json.dumps(features["years"]),
                    features["prices"],
                    json.dumps(features["brands"], ensure_ascii=False),
                    features["ad_score"],
                    features["illustration_score"],
                    features["jewelry_words"],
                ),
            )
            if fts:
                body = cleaned + "\n" + "\n".join(features["captions"])
                conn.execute(
                    "INSERT INTO visual_page_fts (document_id, pdf_page, body) VALUES (?, ?, ?)",
                    (document_id, pdf_page, body),
                )
        conn.execute(
            """
            INSERT INTO visual_index_status (document_id, pages, scanned, indexed_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(document_id) DO UPDATE SET
                pages = excluded.pages, scanned = excluded.scanned,
                indexed_at = CURRENT_TIMESTAMP
            """,
            (document_id, len(pages), int(scanned)),
        )
        conn.commit()
    finally:
        conn.close()
    return len(pages)


def books_to_index(db_path, only_missing=True):
    """Книги, которым нужен индекс: без индекса или с изменившимся числом страниц."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT d.id, d.document_code,
                   (SELECT COUNT(*) FROM library_pages p WHERE p.document_id = d.id),
                   s.pages
            FROM library_documents d
            LEFT JOIN visual_index_status s ON s.document_id = d.id
            WHERE COALESCE(d.storage_key, '') LIKE 'books/%'
            ORDER BY d.id
            """
        ).fetchall()
    finally:
        conn.close()
    return [
        (doc_id, code)
        for doc_id, code, pages, indexed in rows
        if pages and (not only_missing or indexed != pages)
    ]


def index_stats(db_path):
    conn = sqlite3.connect(db_path)
    try:
        books = conn.execute(
            "SELECT COUNT(*) FROM library_documents WHERE COALESCE(storage_key, '') LIKE 'books/%'"
        ).fetchone()[0]
        indexed = conn.execute("SELECT COUNT(*) FROM visual_index_status").fetchone()[0]
        pages = conn.execute("SELECT COUNT(*) FROM visual_page_index").fetchone()[0]
        ads = conn.execute(
            "SELECT COUNT(*) FROM visual_page_index WHERE ad_score >= 0.5"
        ).fetchone()[0]
        illustrated = conn.execute(
            "SELECT COUNT(*) FROM visual_page_index WHERE illustration_score >= 0.5"
        ).fetchone()[0]
        return {"books": books, "indexed": indexed, "pages": pages, "ads": ads, "illustrated": illustrated}
    finally:
        conn.close()


# --- поиск кандидатов ---

def _fts_query(terms):
    parts = []
    for term in terms:
        term = re.sub(r"[\"*^():]", " ", term).strip()
        if len(term) < 2:
            continue
        # Префиксный поиск: «necklace» найдёт и «necklaces».
        parts.append('"' + term.replace('"', "") + '"*')
    return " OR ".join(parts)


# --- служебные страницы ---

# Строка указателя или оглавления: текст, затем номера страниц (до 3 цифр,
# чтобы подписи с годами «Necklace, 1964» не принимались за указатель).
_NUMBERED_LINE = re.compile(r"^(\D{2,70}?)[,.]?\s+(\d{1,3})(?:\s*[-–,;]\s*\d{1,3})*\.?$")
SERVICE_HEADINGS = (
    "contents", "table of contents", "index", "bibliography", "further reading",
    "acknowledgements", "acknowledgments", "list of illustrations", "glossary",
    "indice", "sommario", "bibliografia", "inhaltsverzeichnis", "register",
    "literatur", "sommaire", "table des matières", "obsah", "rejstřík",
    "spis treści", "содержание", "оглавление", "указатель", "библиография",
)
FRONT_MATTER = ("isbn", "all rights reserved", "printed in", "library of congress",
                "cataloguing in publication", "cataloging-in-publication")


def service_reason(original_text):
    """Почему страница служебная (оглавление, указатель, выходные данные,
    библиография) — или None. Нужен исходный OCR с переносами строк."""
    text = original_text or ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    lower = text.lower()
    head = " ".join(lines[:3]).lower()

    if any(marker in lower for marker in FRONT_MATTER):
        return "выходные данные"
    if len(lines) >= 6:
        # «Fig. 12», «Plate 54», «No. 7» — подписи к иллюстрациям, не указатель.
        numbered = [
            m for m in (
                _NUMBERED_LINE.match(line) for line in lines if not CAPTION_MARKERS.match(line)
            ) if m
        ]
        if len(numbered) / len(lines) >= 0.5:
            letters = [m.group(1).strip()[:1].lower() for m in numbered]
            numbers = [int(m.group(2)) for m in numbered]
            pairs = max(1, len(numbered) - 1)
            alphabetical = sum(1 for a, b in zip(letters, letters[1:]) if a <= b) / pairs
            increasing = sum(1 for a, b in zip(numbers, numbers[1:]) if a <= b) / pairs
            if increasing >= 0.8:
                return "оглавление"
            if alphabetical >= 0.8:
                return "указатель"
    for heading in SERVICE_HEADINGS:
        if head.startswith(heading) or re.match(rf"^\W*{re.escape(heading)}\b", head):
            return {
                "contents": "оглавление", "table of contents": "оглавление",
                "index": "указатель", "bibliography": "библиография",
            }.get(heading, "служебная страница")
    return None


def documents_for_brands(conn, brands):
    """Книги, в названии которых есть марка с клейма (например «Sarah Coventry Jewelry»)."""
    if not brands:
        return {}
    found = {}
    for doc_id, title, file_title in conn.execute(
        "SELECT id, COALESCE(meta_title, ''), COALESCE(title, '') FROM library_documents "
        "WHERE COALESCE(storage_key, '') LIKE 'books/%'"
    ):
        haystack = " " + re.sub(r"[^\w]+", " ", f"{title} {file_title}".lower()) + " "
        for brand in brands:
            key = " " + re.sub(r"[^\w]+", " ", brand.lower()).strip() + " "
            if brand not in WEAK_BRANDS and key in haystack:
                found[doc_id] = brand
                break
    return found


def search_candidates(db_path, terms, brands=(), limit=8, per_document=4, with_excluded=False):
    """Дешёвый (без ИИ) поиск страниц-кандидатов по всей библиотеке.

    1. Книги, в названии которых есть марка с клейма, участвуют целиком:
       их страницы ранжируются по подписям, иллюстрациям, рекламе и словам
       описания, даже если сама марка на странице не напечатана.
    2. Плюс полнотекстовые совпадения (FTS5) по всем книгам.
    3. Служебные страницы (оглавление, указатель, выходные данные,
       библиография) исключаются.

    Возвращает список dict; with_excluded=True — ещё и (номер страницы,
    причина) исключённых служебных страниц, для диагностики.
    """
    terms = [t for t in dict.fromkeys(t.strip().lower() for t in terms if t and t.strip())]
    brands = [b.lower() for b in brands if b]
    excluded = []
    if not terms and not brands:
        return ([], excluded) if with_excluded else []
    query_terms = terms + [b for b in brands if b not in terms]

    conn = sqlite3.connect(db_path)
    try:
        brand_docs = documents_for_brands(conn, brands)

        hits = {}
        if has_fts(conn):
            match = _fts_query(query_terms)
            if match:
                for doc, page, rank in conn.execute(
                    """
                    SELECT f.document_id, f.pdf_page, bm25(visual_page_fts) AS rank
                    FROM visual_page_fts f
                    WHERE visual_page_fts MATCH ?
                    ORDER BY rank
                    LIMIT 600
                    """,
                    (match,),
                ):
                    hits[(doc, page)] = -rank
        else:
            for term in query_terms:
                for doc, page in conn.execute(
                    "SELECT document_id, pdf_page FROM library_pages "
                    "WHERE COALESCE(cleaned_text, original_text) LIKE ? LIMIT 600",
                    (f"%{term}%",),
                ):
                    hits[(doc, page)] = hits.get((doc, page), 0) + 1.0

        # Все страницы книг «по марке» — тоже кандидаты.
        for doc_id in brand_docs:
            for (page,) in conn.execute(
                "SELECT pdf_page FROM visual_page_index WHERE document_id = ?", (doc_id,)
            ):
                hits.setdefault((doc_id, page), 0.0)

        results = []
        for (doc, page), text_score in hits.items():
            row = conn.execute(
                """
                SELECT i.document_code, i.captions, i.years, i.brands, i.ad_score,
                       i.illustration_score,
                       COALESCE(NULLIF(d.meta_title, ''), d.title),
                       COALESCE(p.cleaned_text, p.original_text, ''),
                       COALESCE(p.original_text, '')
                FROM visual_page_index i
                JOIN library_documents d ON d.id = i.document_id
                JOIN library_pages p ON p.document_id = i.document_id AND p.pdf_page = i.pdf_page
                WHERE i.document_id = ? AND i.pdf_page = ?
                """,
                (doc, page),
            ).fetchone()
            if row is None:
                continue
            code, captions, years, page_brands, ad, illustration, title, text, original = row

            reason = service_reason(original)
            if reason:
                excluded.append((code, page, reason))
                continue

            page_brands = json.loads(page_brands or "[]")
            captions = json.loads(captions or "[]")
            lower = text.lower()
            matched_terms = [t for t in terms if t in lower]
            brand_on_page = any(b in page_brands or b in lower for b in brands)
            in_brand_book = doc in brand_docs

            score = (
                min(text_score, 8.0)
                + 0.8 * len(matched_terms)
                + (3.0 if brand_on_page else 0.0)
                + (4.0 if in_brand_book else 0.0)
                + 1.5 * illustration
                + 1.0 * ad
                + (0.7 if captions else 0.0)
            )
            # Страница о ДРУГОЙ марке (на клейме одна, на странице другая) — ниже.
            if brands and page_brands and not brand_on_page:
                score -= 2.5
            # Книга по марке без единой зацепки на странице — ниже страниц с зацепками.
            if in_brand_book and not (matched_terms or brand_on_page or captions or ad >= 0.3):
                score -= 2.0

            why = []
            if in_brand_book:
                why.append(f"книга о марке {brand_docs[doc].title()}")
            if brand_on_page:
                why.append("марка на странице")
            if matched_terms:
                why.append("слова: " + ", ".join(matched_terms[:4]))
            if captions:
                why.append("подпись к иллюстрации")
            if ad >= 0.5:
                why.append("похоже на рекламу/каталог")
            if illustration >= 0.5:
                why.append("страница с иллюстрацией")

            results.append({
                "document_id": doc,
                "document_code": code,
                "title": title,
                "pdf_page": page,
                "score": round(score, 3),
                "captions": captions,
                "years": json.loads(years or "[]"),
                "brands": page_brands,
                "ad_score": ad,
                "illustration_score": illustration,
                "text": text,
                "why": why,
            })
    finally:
        conn.close()

    results.sort(key=lambda r: (-r["score"], r["document_id"], r["pdf_page"]))
    picked, per_doc = [], {}
    for result in results:
        count = per_doc.get(result["document_id"], 0)
        if count >= per_document:
            continue
        per_doc[result["document_id"]] = count + 1
        picked.append(result)
        if len(picked) >= limit:
            break
    return (picked, excluded) if with_excluded else picked
