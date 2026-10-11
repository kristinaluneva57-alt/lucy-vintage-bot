"""Визуальная атрибуция украшения по библиотеке (первая версия).

Важно: это НЕ визуальный поиск по всем изображениям библиотеки.
Страницы-кандидаты отбираются по тексту (OCR, подписи, марки), и только
они сравниваются с фото по изображению. Иллюстрация без текста рядом
может не попасть в кандидаты.

Поток одной атрибуции (запускается в фоне):
1. Кэш: те же фото при том же состоянии индекса → готовый результат, 0 $.
2. Права: режим LUCY_VISUAL_MODE, 3 бесплатные ИИ-атрибуции в месяц
   (администратор без личного лимита), бюджет в $ на день и месяц —
   проверяется ДО каждого платного вызова (visual_ai.call_json).
3. ИИ ① описывает украшение по фото и читает клеймо → поисковые термины.
4. Бесплатный поиск кандидатов по индексу всей библиотеки (visual_index).
5. ИИ ② сравнивает фото с не более чем MAX_COMPARE_PAGES изображениями
   страниц: точное совпадение / вероятное / только стиль / нет.
6. Мелкий текст: область подписи или рекламы вырезается в 400 dpi →
   бесплатный Tesseract → при неудаче ИИ ③ читает вырезку (до 2 раз).
7. Проверка кодом: название комплекта или модели и годы засчитываются,
   только если они есть в тексте страницы или в прочитанной вырезке.
8. Отчёт на русском с DOC-ID, книгой и PDF-страницей; изображения
   страниц и вырезок доступны кнопками.

Без ИИ (лимит, бюджет, выключатель) работает бесплатный режим:
кандидаты по тексту карточки и изображения страниц для сравнения глазами.
"""

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import library_search
import page_images
import visual_ai
import visual_index

MAX_USER_PHOTOS = 3
MAX_COMPARE_PAGES = 4
MAX_CANDIDATES = 8
MAX_AI_TEXT_READS = 2
CACHE_DAYS = 30
RUNNING_TIMEOUT = timedelta(minutes=15)

VERDICT_LABELS = {
    "exact": "✅ точное совпадение модели",
    "probable": "🟡 вероятное совпадение",
    "stylistic": "⚪ только стилистическое сходство",
    "none": "— не совпадает",
}
VERDICT_ORDER = {"exact": 0, "probable": 1, "stylistic": 2, "none": 3}
DATE_KIND_LABELS = {
    "advertisement": "год рекламы",
    "catalog": "год каталога",
    "production": "год производства",
    "unknown": "год (тип не ясен)",
}
MATERIAL_WORDS = [
    "rhodium", "silver-tone", "silvertone", "silver tone", "gold-tone", "goldtone",
    "gold tone", "sterling", "gold plated", "gold-plated", "gold filled", "rhinestone",
    "rhinestones", "glass", "cabochon", "lucite", "plastic", "enamel", "faux pearl",
    "simulated pearl", "crystal", "pot metal", "brass", "copper", "aurora borealis",
    "argento", "dorato", "strass", "vetro", "silber", "vergoldet", "glas", "argenté",
    "doré", "verre",
]

DESCRIBE_SYSTEM = """You are a vintage costume jewelry researcher. Look ONLY at the photos.
Return JSON with keys:
"type_ru": jewelry type in Russian (e.g. "колье", "брошь"),
"description_ru": 2-4 sentences in Russian: shape, construction, settings, stones (count, shape, colour), metal tone, findings/clasp,
"stones": [{"count": int|null, "shape": str, "colour": str}],
"metal_tone": str,
"construction": [short English phrases],
"marks": [{"text": exact characters you can read, "legible": true|false}],
"search_terms": 8-15 short search phrases for finding this piece in old catalogues and books: English first, plus Italian, German, French equivalents for the type (e.g. "necklace", "Y necklace", "lariat", "red cabochon", "silver tone", "collana", "Halskette", "collier").
Rules: never guess a brand, model or date. A brand may appear only inside "marks" and only as characters actually visible. If a mark is blurry, set legible=false and write what is uncertain."""

COMPARE_SYSTEM = """You compare a real jewelry piece (USER photos) with pages from historical books, catalogues and advertisements (P1, P2, …).
Each page is divided into a 3x3 grid: columns A,B,C (left→right), rows 1,2,3 (top→bottom).
For EVERY page return an object in "pages" with keys:
"label": "P1" etc.,
"verdict": "exact" (same model: same construction, same number/shape/arrangement of stones, same details), "probable" (very close, but a detail cannot be checked), "stylistic" (only similar style/type), or "none",
"matching_features": [concrete visual features that match, in Russian],
"differences": [differences or things that cannot be checked, in Russian],
"piece_cell": grid cell of the matching piece or null,
"text_cell": grid cell of the caption or advertisement text about that piece, or null,
"is_advertisement": true|false,
"visible_text": text near the piece that you can actually read, copied exactly, or "",
"text_legible": true|false,
"set_name": set/collection name ONLY if printed on the page and legible, else null,
"model_name": model/style name or number ONLY if printed and legible, else null,
"set_items": other pieces of the same set shown or named on the page,
"dates": [{"year": int, "kind": "advertisement"|"catalog"|"production"|"unknown", "basis": what on the page shows it}],
"confidence": 0.0-1.0.
Rules: never invent names, dates, materials or quotes. A similar piece is NOT the same model — use "stylistic". The year of an advertisement or catalogue is NOT the production year unless the page says so. If text is too small or blurry, set text_legible=false.
Also return "summary_ru": 1-3 sentences in Russian."""

READ_SYSTEM = """You read small printed text in a cropped area of a historical page (advertisement, catalogue or caption).
Return JSON: "text": the text exactly as printed (keep original language and spelling), "legible": true|false,
"set_name": set/collection name if printed, else null, "years": [years printed], "unclear": what could not be read.
Never guess missing words: replace unreadable parts with [?]. If nothing is readable, legible=false and text=""."""


# --- база ---

def init_tables(db_path):
    visual_index.init_tables(db_path)
    visual_ai.init_tables(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS visual_attributions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                item_code TEXT,
                photos_hash TEXT,
                mode TEXT,
                status TEXT NOT NULL,
                paid INTEGER DEFAULT 0,
                cached_from INTEGER,
                result_json TEXT,
                created_at TEXT NOT NULL,
                finished_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS visual_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                pdf_page INTEGER NOT NULL,
                cell TEXT,
                method TEXT NOT NULL,
                text TEXT,
                legible INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS item_attributions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_code TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                attribution_id INTEGER NOT NULL,
                summary TEXT NOT NULL,
                saved_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _now():
    return datetime.now(timezone.utc)


def library_version(db_path):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(pages), 0), COALESCE(MAX(indexed_at), '') FROM visual_index_status"
        ).fetchone()
        return f"{row[0]}:{row[1]}:{row[2]}"
    finally:
        conn.close()


def photos_hash(photos, version):
    digest = hashlib.sha256()
    for label, data in photos:
        digest.update(label.encode())
        digest.update(hashlib.sha256(data).digest())
    digest.update(version.encode())
    return digest.hexdigest()


class AlreadyRunning(Exception):
    pass


_USED_SLOTS = """
    SELECT COUNT(*) FROM visual_attributions
    WHERE user_id = ? AND created_at LIKE ?
      AND (paid = 1 OR (status = 'running' AND mode = 'ai' AND created_at >= ?))
"""


def _used_slots(conn, user_id):
    month = _now().strftime("%Y-%m")
    fresh = (_now() - RUNNING_TIMEOUT).isoformat(timespec="seconds")
    return conn.execute(_USED_SLOTS, (user_id, month + "%", fresh)).fetchone()[0]


def paid_this_month(db_path, user_id):
    """Использованные ИИ-попытки месяца: завершённые с оплатой + идущие сейчас."""
    conn = sqlite3.connect(db_path, timeout=visual_ai.DB_TIMEOUT)
    try:
        return _used_slots(conn, user_id)
    finally:
        conn.close()


def running_for(db_path, user_id):
    since = (_now() - RUNNING_TIMEOUT).isoformat(timespec="seconds")
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT id FROM visual_attributions WHERE user_id = ? AND status = 'running' AND created_at >= ?",
            (user_id, since),
        ).fetchone() is not None
    finally:
        conn.close()


def find_cached(db_path, photos_digest):
    since = (_now() - timedelta(days=CACHE_DAYS)).isoformat(timespec="seconds")
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT id, result_json FROM visual_attributions
            WHERE photos_hash = ? AND status = 'done' AND mode = 'ai' AND created_at >= ?
            ORDER BY id DESC LIMIT 1
            """,
            (photos_digest, since),
        ).fetchone()
    finally:
        conn.close()
    return (row[0], json.loads(row[1])) if row else None


def _insert(db_path, user_id, item_code, digest, mode, cached_from=None, result=None, status="running"):
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO visual_attributions
                (user_id, item_code, photos_hash, mode, status, cached_from, result_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, item_code, digest, mode, status, cached_from,
             json.dumps(result, ensure_ascii=False) if result is not None else None,
             _now().isoformat(timespec="seconds")),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def _finish(db_path, attribution_id, result, mode, paid, status="done"):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            UPDATE visual_attributions
            SET result_json = ?, mode = ?, paid = ?, status = ?, finished_at = ?
            WHERE id = ?
            """,
            (json.dumps(result, ensure_ascii=False), mode, int(paid), status,
             _now().isoformat(timespec="seconds"), attribution_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_attribution(db_path, attribution_id):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT user_id, item_code, mode, status, result_json, cached_from FROM visual_attributions WHERE id = ?",
            (attribution_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    user_id, item_code, mode, status, result_json, cached_from = row
    return {
        "id": attribution_id, "user_id": user_id, "item_code": item_code, "mode": mode,
        "status": status, "cached_from": cached_from,
        "result": json.loads(result_json) if result_json else None,
    }


def _save_evidence(db_path, document_id, pdf_page, cell, method, text, legible):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO visual_evidence (document_id, pdf_page, cell, method, text, legible) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (document_id, pdf_page, cell, method, text, int(legible)),
        )
        conn.commit()
    finally:
        conn.close()


def _cached_evidence(db_path, document_id, pdf_page, cell):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            """
            SELECT method, text, legible FROM visual_evidence
            WHERE document_id = ? AND pdf_page = ? AND cell = ? AND legible = 1
            ORDER BY id DESC LIMIT 1
            """,
            (document_id, pdf_page, cell),
        ).fetchone()
    finally:
        conn.close()


def _storage(db_path, document_id):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT storage_key, language FROM library_documents WHERE id = ?", (document_id,)
        ).fetchone()
        return row if row else (None, None)
    finally:
        conn.close()


# --- проверка утверждений кодом ---

def _norm(text):
    return " ".join(re.sub(r"[^\w]+", " ", (text or "").lower()).split())


def appears_in(value, *texts):
    key = _norm(value)
    return bool(key) and any(key in _norm(text) for text in texts if text)


def materials_in(*texts):
    joined = " ".join(t.lower() for t in texts if t)
    return [word for word in MATERIAL_WORDS if word in joined]


# --- основной поток ---

def _mode_for(used, is_admin):
    current = visual_ai.mode()
    if current == "off":
        return "free", "ИИ-сравнение сейчас выключено"
    if current == "admin" and not is_admin:
        return "free", "ИИ-сравнение пока доступно только в тестовом режиме"
    if not is_admin and used >= visual_ai.free_per_month():
        return "free", (
            f"бесплатные ИИ-исследования на этот месяц закончились "
            f"({visual_ai.free_per_month()} в месяц)"
        )
    return "ai", None


def start_slot(db_path, user_id, item_code, digest, is_admin):
    """Атомарно занимает слот исследования: проверка «уже идёт» и месячного
    лимита и вставка записи — в одной транзакции BEGIN IMMEDIATE.
    Два одновременных запуска не могут оба получить последнюю попытку.
    Возвращает (attribution_id, mode, reason)."""
    conn = visual_ai._connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            fresh = (_now() - RUNNING_TIMEOUT).isoformat(timespec="seconds")
            if conn.execute(
                "SELECT 1 FROM visual_attributions "
                "WHERE user_id = ? AND status = 'running' AND created_at >= ?",
                (user_id, fresh),
            ).fetchone():
                raise AlreadyRunning()
            mode, reason = _mode_for(_used_slots(conn, user_id), is_admin)
            cursor = conn.execute(
                """
                INSERT INTO visual_attributions
                    (user_id, item_code, photos_hash, mode, status, created_at)
                VALUES (?, ?, ?, ?, 'running', ?)
                """,
                (user_id, item_code, digest, mode, _now().isoformat(timespec="seconds")),
            )
            conn.execute("COMMIT")
            return cursor.lastrowid, mode, reason
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def decide_mode(db_path, user_id, is_admin):
    """('ai', None) или ('free', причина) — для подсказки до запуска."""
    return _mode_for(paid_this_month(db_path, user_id), is_admin)


def card_terms(card):
    """Поисковые термины из карточки: клеймо и описание (через словарь)."""
    terms, brands = [], []
    for field in ("mark_text", "notes"):
        value = card.get(field) or ""
        if not value or value == "не читается":
            continue
        plan = library_search.build_search_plan(value)
        for _, group_terms, _ in plan.groups:
            terms += [t.rstrip("=") for t in group_terms if not re.search("[а-я]", t)]
        brands += visual_index.find_brands(value)
    return list(dict.fromkeys(terms)), list(dict.fromkeys(brands))


def run(db_path, user_id, item_code, photos, card, is_admin=False):
    """Синхронная атрибуция (вызывается в фоне). Возвращает (attribution_id, result).

    photos — список (метка, JPEG-байты): front, back, mark, details, extra…
    card — поля карточки: mark_text, notes.
    """
    version = library_version(db_path)
    digest = photos_hash(photos, version)

    # Готовый результат по тем же фото — бесплатно, попытку не списывает.
    allowed = visual_ai.mode() == "all" or (visual_ai.mode() == "admin" and is_admin)
    cached = find_cached(db_path, digest) if allowed else None
    if cached:
        source_id, result = cached
        result = dict(result, from_cache=True)
        attribution_id = _insert(db_path, user_id, item_code, digest, "ai",
                                 cached_from=source_id, result=result, status="done")
        return attribution_id, result

    attribution_id, mode, reason = start_slot(db_path, user_id, item_code, digest, is_admin)
    paid = False
    try:
        if mode == "ai":
            try:
                result = _run_ai(db_path, attribution_id, user_id, photos, card)
                paid = True
            except visual_ai.BudgetExceeded as error:
                # Незавершённое исследование не списывает бесплатную попытку.
                result = _run_free(db_path, card, note=f"достигнут {error} — платные операции остановлены")
                mode = "free"
            except visual_ai.VisualAIUnavailable as error:
                result = _run_free(db_path, card, note="ИИ сейчас недоступен" + (
                    " (закончился баланс OpenAI)" if error.reason == "quota" else ""))
                result["ai_problem"] = error.reason
                mode = "free"
        else:
            result = _run_free(db_path, card, note=reason)
    except Exception:
        _finish(db_path, attribution_id, {"error": True}, mode, paid, status="error")
        raise

    result["attribution_id"] = attribution_id
    _finish(db_path, attribution_id, result, mode, paid)
    return attribution_id, result


def _run_free(db_path, card, note=None):
    terms, brands = card_terms(card)
    candidates = visual_index.search_candidates(db_path, terms, brands, limit=3) if (terms or brands) else []
    return {
        "mode": "free",
        "note": note,
        "terms": terms[:12],
        "evidence": [
            {
                "n": n,
                "document_id": c["document_id"],
                "document_code": c["document_code"],
                "title": c["title"],
                "pdf_page": c["pdf_page"],
                "verdict": None,
                "captions": c["captions"][:3],
            }
            for n, c in enumerate(candidates, 1)
        ],
    }


def _run_ai(db_path, attribution_id, user_id, photos, card):
    calls = dict(db_path=db_path, attribution_id=attribution_id, user_id=user_id)
    order = {"mark": 0, "front": 1, "back": 2}
    chosen = sorted(photos, key=lambda p: order.get(p[0], 5))[:MAX_USER_PHOTOS]
    small = [(label, page_images.shrink_jpeg(data)) for label, data in chosen]

    # ① описание и клеймо
    content = [{"type": "text", "text": "Photos of one piece: " + ", ".join(l for l, _ in small)}]
    content += [visual_ai.image_part(data) for _, data in small]
    described = visual_ai.call_json(
        kind="describe", system=DESCRIBE_SYSTEM, content=content,
        images=len(small), max_tokens=700, **calls,
    )

    marks = [m for m in described.get("marks") or [] if isinstance(m, dict)]
    legible_marks = [m.get("text", "") for m in marks if m.get("legible") and m.get("text")]
    if card.get("mark_text") and card["mark_text"] != "не читается":
        legible_marks.append(card["mark_text"])
    brands = []
    for text in legible_marks:
        brands += visual_index.find_brands(text)
    brands = list(dict.fromkeys(brands))

    terms = [t for t in described.get("search_terms") or [] if isinstance(t, str)]
    extra_terms, extra_brands = card_terms(card)
    terms = list(dict.fromkeys(terms + extra_terms))
    brands = list(dict.fromkeys(brands + extra_brands))

    # ② бесплатный поиск кандидатов
    candidates = visual_index.search_candidates(db_path, terms, brands, limit=MAX_CANDIDATES)
    result = {
        "mode": "ai",
        "description": {
            "type": described.get("type_ru"),
            "text": described.get("description_ru"),
            "marks": marks,
        },
        "brands_from_mark": brands,
        "terms": terms[:15],
        "candidates_found": len(candidates),
        "evidence": [],
        "summary": None,
    }
    if not candidates:
        return result

    pages = []
    for candidate in candidates[:MAX_COMPARE_PAGES]:
        storage_key, _ = _storage(db_path, candidate["document_id"])
        try:
            image = page_images.page_image(candidate["document_code"], storage_key, candidate["pdf_page"])
        except Exception as error:
            print(f"page render failed: {type(error).__name__}")
            continue
        pages.append((candidate, page_images.shrink_jpeg(image)))
    if not pages:
        result["render_failed"] = True
        return result

    # ③ сравнение с не более чем MAX_COMPARE_PAGES страницами
    content = [{"type": "text", "text": "USER photos:"}]
    content += [visual_ai.image_part(data) for _, data in small[:2]]
    for index, (candidate, image) in enumerate(pages, 1):
        excerpt = "; ".join(candidate["captions"][:4]) or candidate["text"][:400]
        content.append({
            "type": "text",
            "text": f"P{index}: {candidate['document_code']}, PDF page {candidate['pdf_page']}. "
                    f"OCR excerpt (may contain errors): {excerpt[:500]}",
        })
        content.append(visual_ai.image_part(image))
    compared = visual_ai.call_json(
        kind="compare", system=COMPARE_SYSTEM, content=content,
        images=min(2, len(small)) + len(pages), max_tokens=1400, **calls,
    )
    result["summary"] = compared.get("summary_ru")

    by_label = {
        p.get("label"): p for p in compared.get("pages") or [] if isinstance(p, dict)
    }
    ai_reads = 0
    for index, (candidate, _) in enumerate(pages, 1):
        page = by_label.get(f"P{index}") or {}
        verdict = page.get("verdict") if page.get("verdict") in VERDICT_ORDER else "none"
        evidence = {
            "n": index,
            "document_id": candidate["document_id"],
            "document_code": candidate["document_code"],
            "title": candidate["title"],
            "pdf_page": candidate["pdf_page"],
            "verdict": verdict,
            "confidence": page.get("confidence"),
            "matching": [str(x) for x in page.get("matching_features") or []][:6],
            "differences": [str(x) for x in page.get("differences") or []][:6],
            "piece_cell": _valid_cell(page.get("piece_cell")),
            "text_cell": _valid_cell(page.get("text_cell")),
            "is_ad": bool(page.get("is_advertisement")) or candidate["ad_score"] >= 0.5,
            "read_text": None,
            "read_method": None,
            "text_unreadable": False,
        }

        crop_text = ""
        if verdict in ("exact", "probable"):
            cell = evidence["text_cell"] or evidence["piece_cell"]
            needs_reading = cell and (
                evidence["is_ad"] or not page.get("text_legible")
                or page.get("set_name") or page.get("model_name")
            )
            if needs_reading:
                # Платное чтение ИИ — только для области текста (подпись/реклама);
                # область самого украшения читаем лишь бесплатным Tesseract.
                crop_text, method, legible = _read_small_text(
                    db_path, candidate, cell,
                    allow_ai=bool(evidence["text_cell"]) and ai_reads < MAX_AI_TEXT_READS,
                    calls=calls,
                )
                if method == "vision":
                    ai_reads += 1
                evidence["read_text"] = crop_text[:800] if legible else None
                evidence["read_method"] = method if legible else None
                evidence["text_unreadable"] = not legible
                evidence["crop_cell"] = cell

        sources = (candidate["text"], crop_text, page.get("visible_text") if page.get("text_legible") else "")
        evidence["set_name"] = _checked(page.get("set_name"), sources)
        evidence["model_name"] = _checked(page.get("model_name"), sources)
        evidence["set_items"] = [str(x) for x in page.get("set_items") or []][:8]
        evidence["dates"] = _checked_dates(page.get("dates"), sources)
        evidence["materials"] = materials_in(candidate["text"], crop_text) if verdict in ("exact", "probable") else []
        evidence["page_brands"] = candidate["brands"]
        result["evidence"].append(evidence)

    result["evidence"].sort(key=lambda e: (VERDICT_ORDER[e["verdict"]], -(e.get("confidence") or 0)))
    for n, evidence in enumerate(result["evidence"], 1):
        evidence["n"] = n
    return result


def _valid_cell(value):
    try:
        page_images.cell_box(value)
        return value.strip().upper()
    except (ValueError, AttributeError):
        return None


def _checked(value, sources):
    """Название засчитывается, только если оно есть в тексте источника."""
    if not value or not isinstance(value, str):
        return None
    return {"value": value.strip(), "confirmed": appears_in(value, *sources)}


def _checked_dates(dates, sources):
    checked = []
    for item in dates or []:
        if not isinstance(item, dict):
            continue
        try:
            year = int(item.get("year"))
        except (TypeError, ValueError):
            continue
        if not 1800 <= year <= 2030:
            continue
        kind = item.get("kind") if item.get("kind") in DATE_KIND_LABELS else "unknown"
        in_text = any(str(year) in (text or "") for text in sources)
        checked.append({
            "year": year,
            "kind": kind,
            "basis": str(item.get("basis") or "")[:200],
            "in_source_text": in_text,
        })
    return checked


def _read_small_text(db_path, candidate, cell, allow_ai, calls):
    """Мелкий текст области: кэш → Tesseract (бесплатно) → ИИ-чтение вырезки."""
    cached = _cached_evidence(db_path, candidate["document_id"], candidate["pdf_page"], cell)
    if cached:
        method, text, legible = cached
        return text, method, bool(legible)

    storage_key, language = _storage(db_path, candidate["document_id"])
    try:
        crop = page_images.crop_image(candidate["document_code"], storage_key, candidate["pdf_page"], cell)
    except Exception as error:
        print(f"crop failed: {type(error).__name__}")
        return "", None, False

    text = page_images.tesseract_text(crop, language or "eng")
    if page_images.readable(text):
        _save_evidence(db_path, candidate["document_id"], candidate["pdf_page"], cell, "tesseract", text, True)
        return text, "tesseract", True

    if not allow_ai:
        _save_evidence(db_path, candidate["document_id"], candidate["pdf_page"], cell, "tesseract", text, False)
        return text, "tesseract", False

    data = visual_ai.call_json(
        kind="read_text", system=READ_SYSTEM,
        content=[{"type": "text", "text": "Cropped area:"},
                 visual_ai.image_part(page_images.shrink_jpeg(crop, max_side=1536))],
        images=2, max_tokens=500, **calls,
    )
    legible = bool(data.get("legible")) and page_images.readable(data.get("text") or "", 8)
    text = (data.get("text") or "").strip()
    _save_evidence(db_path, candidate["document_id"], candidate["pdf_page"], cell, "vision", text, legible)
    return text, "vision", legible


# --- отчёт ---

def best_evidence(result):
    good = [e for e in result.get("evidence", []) if e.get("verdict") in ("exact", "probable")]
    return good[0] if good else None


def source_line(evidence):
    return f"{evidence['document_code']} · {evidence['title']} · PDF-стр. {evidence['pdf_page']}"


def format_report(result):
    """Исследовательская карточка на русском. Всё неподтверждённое — отдельно."""
    lines = []
    if result.get("mode") == "free":
        lines.append("🔎 ПОИСК ПО КНИГАМ (без ИИ-сравнения)")
        if result.get("note"):
            lines.append(f"ℹ️ {result['note'][:1].upper() + result['note'][1:]}.")
        if not result.get("evidence"):
            lines += ["", "По надписи клейма и описанию карточки подходящих страниц не нашлось.",
                      "Добавь в карточку текст клейма и описание — по ним идёт поиск."]
            return "\n".join(lines)
        lines += ["", "Страницы, где встречаются слова из твоей карточки. Сравни украшение сама — "
                  "изображения страниц по кнопкам ниже:"]
        for evidence in result["evidence"]:
            lines.append(f"[{evidence['n']}] {source_line(evidence)}")
            for caption in evidence.get("captions") or []:
                lines.append(f"     «{caption[:120]}»")
        return "\n".join(lines)

    lines.append("🔬 ИССЛЕДОВАНИЕ ПО БИБЛИОТЕКЕ")
    if result.get("from_cache"):
        lines.append("(готовый результат по тем же фото — без новых расходов)")
    description = result.get("description") or {}
    if description.get("text"):
        lines += ["", f"Что на фото: {description['text']}"]
    marks = description.get("marks") or []
    if marks:
        shown = [f"«{m.get('text')}»" + ("" if m.get("legible") else " (нечётко)") for m in marks if m.get("text")]
        if shown:
            lines.append("Клеймо на фото: " + ", ".join(shown))

    best = best_evidence(result)
    unconfirmed = []

    lines.append("")
    if result.get("candidates_found") == 0:
        lines.append("В библиотеке не нашлось страниц-кандидатов по признакам этого украшения.")
    elif not best:
        lines.append("Точных или вероятных совпадений среди проверенных страниц нет.")

    if best:
        manufacturer = None
        if result.get("brands_from_mark"):
            manufacturer = f"{result['brands_from_mark'][0].title()} (по клейму на украшении)"
        elif best.get("page_brands"):
            manufacturer = f"{best['page_brands'][0].title()} — упомянут на странице-источнике, клеймо не подтверждает"
            unconfirmed.append("производитель не подтверждён клеймом")
        lines.append(f"Производитель: {manufacturer or 'не установлен'}")

        for key, label in (("model_name", "Модель"), ("set_name", "Комплект")):
            value = best.get(key)
            if value and value["confirmed"]:
                lines.append(f"{label}: «{value['value']}» — напечатано в источнике [{best['n']}]")
            elif value:
                unconfirmed.append(f"{label.lower()} «{value['value']}» — не найдено в тексте страницы")
            else:
                lines.append(f"{label}: не установлено")

        dates = best.get("dates") or []
        if dates:
            parts = []
            for date in dates:
                label = DATE_KIND_LABELS[date["kind"]]
                text = f"{date['year']} — {label}"
                if not date["in_source_text"]:
                    text += " (год не найден в тексте страницы — не подтверждён)"
                parts.append(text)
            lines.append("Датировка: " + "; ".join(parts))
            if not any(d["kind"] == "production" and d["in_source_text"] for d in dates):
                unconfirmed.append("год производства (год рекламы или каталога — не то же самое)")
        else:
            lines.append("Датировка: в источнике не указана")
            unconfirmed.append("датировка")

        if best.get("materials"):
            lines.append("Материалы (упомянуты в тексте источника): " + ", ".join(best["materials"]))
        else:
            unconfirmed.append("материалы — в источнике не названы")

        if best.get("set_items"):
            lines.append("В комплект, по источнику, входят: " + ", ".join(best["set_items"]))

        lines.append(f"Уверенность: {VERDICT_LABELS[best['verdict']]}")
        if best.get("matching"):
            lines.append("Почему совпадает: " + "; ".join(best["matching"]))
        if best.get("differences"):
            lines.append("Что не удалось сверить: " + "; ".join(best["differences"]))
        if best.get("text_unreadable"):
            unconfirmed.append("мелкий текст рекламы/подписи прочитать не удалось")
        elif best.get("read_text"):
            method = "Tesseract" if best.get("read_method") == "tesseract" else "ИИ"
            lines.append(f"Текст рядом с украшением (прочитан: {method}): «{best['read_text'][:300]}»")

    if result.get("summary"):
        lines += ["", f"Кратко: {result['summary']}"]

    evidence = result.get("evidence") or []
    if evidence:
        lines += ["", "📖 Проверенные источники:"]
        for item in evidence:
            lines.append(f"[{item['n']}] {source_line(item)} — {VERDICT_LABELS[item['verdict']]}")

    if unconfirmed:
        lines += ["", "❓ Не подтверждено: " + "; ".join(dict.fromkeys(unconfirmed)) + "."]
    lines += ["", "Похожее украшение — ещё не та же модель. Изображения страниц — по кнопкам ниже."]
    return "\n".join(lines)


def cost_breakdown(db_path, attribution_id):
    rows = visual_ai.calls_for(db_path, attribution_id)
    if not rows:
        return "💵 Платных вызовов не было: 0 $."
    names = {"describe": "описание фото", "compare": "сравнение со страницами", "read_text": "чтение мелкого текста"}
    lines = ["💵 Расходы этой атрибуции:"]
    total = 0.0
    status_notes = {
        "done": "",
        "failed": " (ошибка OpenAI — не оплачено)",
        "unknown": " (ответ не получен — возможно оплачено, учтено в бюджете)",
        "reserved": " (ещё выполняется — зарезервировано)",
    }
    for kind, images, input_tokens, output_tokens, value, ok, state in rows:
        total += value
        status = status_notes.get(state, "")
        lines.append(
            f"• {names.get(kind, kind)}: картинок {images}, вход {input_tokens}, "
            f"выход {output_tokens} токенов — {value:.5f} ${status}"
        )
    lines.append(f"Итого: {total:.5f} $ за {len(rows)} вызов(а)")
    return "\n".join(lines)


def summary_for_card(result):
    best = best_evidence(result)
    if not best:
        return None
    parts = []
    if result.get("brands_from_mark"):
        parts.append(result["brands_from_mark"][0].title())
    for key in ("set_name", "model_name"):
        value = best.get(key)
        if value and value["confirmed"]:
            parts.append(f"«{value['value']}»")
    label = "совпадение" if best["verdict"] == "exact" else "вероятно"
    return f"{label}: {' '.join(parts) or 'см. источник'} — {source_line(best)}"


def save_to_card(db_path, attribution_id, user_id):
    """Сохраняет атрибуцию в карточку владелицы. Возвращает текст или None."""
    record = get_attribution(db_path, attribution_id)
    if not record or record["user_id"] != user_id or not record["item_code"] or not record["result"]:
        return None
    summary = summary_for_card(record["result"])
    if not summary:
        return None
    conn = sqlite3.connect(db_path)
    try:
        owner = conn.execute(
            "SELECT 1 FROM items WHERE item_code = ? AND owner_user_id = ? AND deleted_at IS NULL",
            (record["item_code"], user_id),
        ).fetchone()
        if not owner:
            return None
        conn.execute(
            "INSERT INTO item_attributions (item_code, user_id, attribution_id, summary) VALUES (?, ?, ?, ?)",
            (record["item_code"], user_id, attribution_id, summary),
        )
        conn.execute(
            "UPDATE items SET attribution_status = ?, updated_at = datetime('now') "
            "WHERE item_code = ? AND owner_user_id = ?",
            (summary[:300], record["item_code"], user_id),
        )
        conn.commit()
    finally:
        conn.close()
    return summary
