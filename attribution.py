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
3. ИИ ① описывает украшение по фото → поисковые термины. Клеймо, которое
   владелица вписала в карточку (mark_text), — исходный факт: модель его
   не перечитывает, а поиск, выбор книги по марке, сравнение и отчёт
   опираются на него. Чтение клейма ИИ с фото используется для поиска
   только если в карточке клейма нет — и помечается как предположение.
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
Return a single JSON object: {"pages": [one object per page as described above], "summary_ru": "1-3 sentences in Russian"}."""

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


class PartialResult(Exception):
    """Описание получено, но сравнение не выполнено (ошибка ИИ, бюджет,
    не удалось получить изображения страниц)."""

    def __init__(self, result, reason):
        super().__init__(reason)
        self.result = result
        self.reason = reason


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


def owner_mark(card):
    """Клеймо, указанное владелицей в карточке, или None."""
    value = re.sub(r"\s+", " ", (card or {}).get("mark_text") or "").strip()
    if not value or value.lower() in ("не читается", "нет", "клейма нет", "-", "—"):
        return None
    return value[:200]


def _mark_key(text):
    return re.sub(r"[^0-9a-zа-яё]+", "", (text or "").lower())


def same_mark(seen, recorded):
    """Совпадает ли прочитанное с фото с записанным клеймом (без учёта
    регистра, пробелов и знаков; частичное чтение тоже считается)."""
    a, b = _mark_key(seen), _mark_key(recorded)
    if not a or not b:
        return False
    if min(len(a), len(b)) < 4:  # «©», «R», «SC» — только точное совпадение
        return a == b
    return a in b or b in a


def owner_mark_note(mark):
    """Текст для модели: клеймо — данность, повторно не распознавать."""
    return (
        f"OWNER-RECORDED MARK (a fact entered by the owner, already verified by her): «{mark}». "
        "Do NOT re-read or re-interpret the mark and do not use it as your own reading; "
        "do not put brand names into search_terms. Spend your attention on construction, shape, "
        "settings, stones, metal, findings and clasp. Leave \"marks\" empty, unless the characters "
        "visible in the photos clearly differ from the owner's record — then list what you see."
    )


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
    # Кэш учитывает и данные карточки: если владелица вписала или исправила
    # клеймо, старый результат (без него) повторно не выдаётся.
    card_state = json.dumps(
        {"mark": owner_mark(card), "notes": (card or {}).get("notes") or ""},
        ensure_ascii=False, sort_keys=True,
    )
    digest = photos_hash(photos, version + "\n" + card_state)

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
            except PartialResult as partial:
                # Описание и кандидаты сохраняются; попытка не списывается.
                result = partial.result
                result["partial"] = True
                result["partial_reason"] = partial.reason
                mode = "partial"
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
    mark = owner_mark(card)
    priority = visual_index.find_brands(mark) if mark else []
    candidates = visual_index.search_candidates(
        db_path, terms, brands, limit=3, priority_brands=priority,
    ) if (terms or brands) else []
    return {
        "mode": "free",
        "note": note,
        "owner_mark": mark,
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
                "why": c.get("why", []),
            }
            for n, c in enumerate(candidates, 1)
        ],
    }


def _run_ai(db_path, attribution_id, user_id, photos, card):
    calls = dict(db_path=db_path, attribution_id=attribution_id, user_id=user_id)
    mark = owner_mark(card)
    owner_brands = visual_index.find_brands(mark) if mark else []
    if mark:
        # Клеймо уже известно — фото клейма не нужно модели в первую очередь:
        # лицо, оборот и детали важнее для описания конструкции.
        order = {"front": 0, "back": 1, "details": 2, "extra": 3, "mark": 4}
    else:
        order = {"mark": 0, "front": 1, "back": 2}
    chosen = sorted(photos, key=lambda p: order.get(p[0], 5))[:MAX_USER_PHOTOS]
    small = [(label, page_images.shrink_jpeg(data)) for label, data in chosen]

    # ① описание по фото
    intro = "Photos of one piece: " + ", ".join(l for l, _ in small)
    if mark:
        intro += "\n" + owner_mark_note(mark)
    content = [{"type": "text", "text": intro}]
    content += [visual_ai.image_part(data) for _, data in small]
    described = visual_ai.call_json(
        kind="describe", system=DESCRIBE_SYSTEM, content=content,
        images=len(small), max_tokens=700, **calls,
    )

    marks = [m for m in described.get("marks") or [] if isinstance(m, dict) and m.get("text")]
    photo_marks = [str(m["text"])[:120] for m in marks if m.get("legible")]
    if mark:
        # Клеймо владелицы — единственный источник марки. Чтение ИИ его не заменяет.
        brands, brand_source = owner_brands, "owner" if owner_brands else None
        mark_conflict = [text for text in (str(m["text"])[:120] for m in marks) if not same_mark(text, mark)]
    else:
        brands = []
        for text in photo_marks:
            brands += visual_index.find_brands(text)
        brands = list(dict.fromkeys(brands))
        brand_source = "photo" if brands else None
        mark_conflict = []

    ai_terms = [t for t in described.get("search_terms") or [] if isinstance(t, str)]
    if mark:
        # Ни марки, «додуманные» моделью, ни её собственное чтение клейма
        # (например «Sakai Tamaru») не должны уводить поиск от клейма владелицы.
        read_by_ai = [str(m["text"]) for m in marks]
        ai_terms = [
            t for t in ai_terms
            if set(visual_index.find_brands(t)) <= set(owner_brands)
            and not any(same_mark(t, text) for text in read_by_ai)
        ]
    card_terms_list, card_brands = card_terms(card)
    # Сначала данные карточки (клеймо, описание владелицы), потом слова ИИ.
    terms = list(dict.fromkeys(card_terms_list + ai_terms))
    search_brands = list(dict.fromkeys(brands + card_brands))

    # ② бесплатный поиск кандидатов: страницы о марке с клейма владелицы — первыми
    candidates = visual_index.search_candidates(
        db_path, terms, search_brands, limit=MAX_CANDIDATES, priority_brands=owner_brands,
    )
    result = {
        "mode": "ai",
        "owner_mark": mark,
        "description": {
            "type": described.get("type_ru"),
            "text": described.get("description_ru"),
            "marks": marks,
        },
        "mark_conflict": mark_conflict,
        "brands_from_mark": brands,
        "brand_source": brand_source,
        "terms": terms[:15],
        "candidates_found": len(candidates),
        "candidates": [
            {
                "n": n,
                "document_id": c["document_id"],
                "document_code": c["document_code"],
                "title": c["title"],
                "pdf_page": c["pdf_page"],
                "verdict": None,
                "captions": c["captions"][:3],
                "why": c.get("why", []),
                "score": c["score"],
            }
            for n, c in enumerate(candidates[:MAX_COMPARE_PAGES], 1)
        ],
        "evidence": [],
        "summary": None,
    }
    if not candidates:
        return result

    def stop(reason):
        # Честный частичный результат: описание и найденные по тексту
        # кандидаты, без заявлений о совпадении.
        result["evidence"] = [dict(c) for c in result["candidates"]]
        raise PartialResult(result, reason)

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
        stop("не удалось получить изображения страниц для сравнения")

    # ③ сравнение с не более чем MAX_COMPARE_PAGES страницами
    user_intro = "USER photos:"
    if mark:
        user_intro = (
            f"OWNER-RECORDED MARK on the USER piece: «{mark}» (a fact from the owner; a starting point "
            "for research, not proof of the model). Judge the match ONLY by visual features. Do not "
            "claim that a page shows this brand unless it is printed on the page.\nUSER photos:"
        )
    content = [{"type": "text", "text": user_intro}]
    content += [visual_ai.image_part(data) for _, data in small[:2]]
    for index, (candidate, image) in enumerate(pages, 1):
        excerpt = "; ".join(candidate["captions"][:4]) or candidate["text"][:400]
        content.append({
            "type": "text",
            "text": f"P{index}: {candidate['document_code']}, PDF page {candidate['pdf_page']}. "
                    f"OCR excerpt (may contain errors): {excerpt[:500]}",
        })
        content.append(visual_ai.image_part(image))
    try:
        compared = visual_ai.call_json(
            kind="compare", system=COMPARE_SYSTEM, content=content,
            images=min(2, len(small)) + len(pages), max_tokens=1400, **calls,
        )
    except visual_ai.BudgetExceeded as error:
        stop(f"сравнение остановлено: достигнут {error}")
    except visual_ai.VisualAIUnavailable as error:
        stop(
            "сравнение со страницами не выполнено: ошибка OpenAI"
            + (" (закончился баланс)" if error.reason == "quota" else "")
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
                try:
                    crop_text, method, legible = _read_small_text(
                        db_path, candidate, cell,
                        allow_ai=bool(evidence["text_cell"]) and ai_reads < MAX_AI_TEXT_READS,
                        calls=calls,
                    )
                except (visual_ai.BudgetExceeded, visual_ai.VisualAIUnavailable):
                    # Чтение мелкого текста не удалось — сравнение всё равно засчитано,
                    # но текст рекламы остаётся неподтверждённым.
                    crop_text, method, legible = "", "vision", False
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

def verdict_label(verdict):
    return VERDICT_LABELS.get(verdict, "не сравнивалась с фото")


def best_evidence(result):
    good = [e for e in result.get("evidence", []) if e.get("verdict") in ("exact", "probable")]
    return good[0] if good else None


def source_line(evidence):
    return f"{evidence['document_code']} · {evidence['title']} · PDF-стр. {evidence['pdf_page']}"


MARK_DISCLAIMER = (
    "Клеймо — исходный факт для поиска; само по себе оно не доказывает "
    "подлинность украшения и не определяет точную модель."
)


def owner_mark_lines(result):
    mark = result.get("owner_mark")
    if mark:
        return [f"🔖 Клеймо, указанное владелицей: {mark}", MARK_DISCLAIMER]
    return ["🔖 Клеймо в карточке не указано."]


def photo_lines(result, label="📷 Что видно на фотографии"):
    """Описание ИИ по фото. Чтение клейма с фото никогда не подменяет
    клеймо владелицы: при расхождении — отдельная пометка."""
    lines = []
    description = result.get("description") or {}
    if description.get("text"):
        lines.append(f"{label}: {description['text']}")
    marks = [m for m in description.get("marks") or [] if isinstance(m, dict) and m.get("text")]
    if result.get("owner_mark"):
        conflict = result.get("mark_conflict")
        if conflict is None:  # старые результаты
            conflict = [m["text"] for m in marks if not same_mark(m["text"], result["owner_mark"])]
        if conflict:
            lines.append(
                "⚠️ Автоматическое чтение фотографии отличается от указанной надписи: "
                + ", ".join(f"«{text}»" for text in conflict)
                + ". Для поиска использовано клеймо из карточки."
            )
    elif marks:
        lines.append(
            "Надпись, которую ИИ прочитал на фото (предположение, не подтверждено): "
            + ", ".join(f"«{m['text']}»" + ("" if m.get("legible") else " (нечётко)") for m in marks)
            + ". Если она верна — впиши клеймо в карточку."
        )
    return lines


def format_report(result):
    """Исследовательская карточка на русском. Всё неподтверждённое — отдельно."""
    lines = []
    if result.get("mode") == "free":
        lines.append("🔎 ПОИСК ПО КНИГАМ (без ИИ-сравнения)")
        if result.get("note"):
            lines.append(f"ℹ️ {result['note'][:1].upper() + result['note'][1:]}.")
        if result.get("owner_mark"):
            lines += ["", f"🔖 Клеймо, указанное владелицей: {result['owner_mark']} — поиск шёл в первую очередь по нему."]
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

    if result.get("partial"):
        return format_partial(result)

    lines.append("🔬 ИССЛЕДОВАНИЕ ПО БИБЛИОТЕКЕ")
    if result.get("from_cache"):
        lines.append("(готовый результат по тем же фото — без новых расходов)")
    lines += [""] + owner_mark_lines(result)
    photo = photo_lines(result)
    if photo:
        lines += [""] + photo

    best = best_evidence(result)
    unconfirmed = []

    lines += ["", "📖 Что подтверждено историческим источником:"]
    if result.get("candidates_found") == 0:
        lines.append("В библиотеке не нашлось страниц-кандидатов по признакам этого украшения.")
    elif not best:
        lines.append(
            "Точных или вероятных совпадений среди проверенных страниц нет — "
            "атрибуция не выдаётся."
        )
        if result.get("owner_mark") and any(
            "клеймо владелицы" in (c.get("why") or []) for c in result.get("candidates") or []
        ):
            lines.append(f"Проверены страницы, связанные с клеймом «{result['owner_mark']}».")

    if best:
        manufacturer = None
        source = result.get("brand_source") or ("photo" if result.get("brands_from_mark") else None)
        brand = (result.get("brands_from_mark") or [None])[0]
        on_page = bool(brand) and brand in [b.lower() for b in best.get("page_brands") or []]
        if source == "owner":
            manufacturer = f"{brand.title()} — по клейму, указанному владелицей"
            if on_page:
                manufacturer += f"; марка напечатана на странице-источнике [{best['n']}]"
        elif source == "photo":
            manufacturer = f"{brand.title()} — по надписи, прочитанной ИИ на фото (не подтверждено владелицей)"
            unconfirmed.append("клеймо прочитано ИИ с фото, в карточке не указано")
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

        lines.append(f"Уверенность: {verdict_label(best['verdict'])}")
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
            lines.append(f"[{item['n']}] {source_line(item)} — {verdict_label(item['verdict'])}")

    if unconfirmed:
        lines += ["", "❓ Не подтверждено: " + "; ".join(dict.fromkeys(unconfirmed)) + "."]
    lines += ["", "Похожее украшение — ещё не та же модель. Изображения страниц — по кнопкам ниже."]
    return "\n".join(lines)


def format_partial(result):
    """Исследование оборвалось после описания: честно показываем, что есть."""
    reason = result.get("partial_reason") or "сравнение не выполнено"
    lines = [
        "🔬 ИССЛЕДОВАНИЕ НЕ ЗАВЕРШЕНО",
        f"⚠️ {reason[:1].upper() + reason[1:]}.",
        "Бесплатная попытка не списана.",
    ]
    lines += [""] + owner_mark_lines(result)
    photo = photo_lines(result, label="📷 Что видно на фотографии (описание ИИ)")
    if photo:
        lines += [""] + photo
    evidence = result.get("evidence") or []
    if evidence:
        lines += [
            "",
            "Страницы-кандидаты — найдены по тексту книг, с фото НЕ сравнивались. "
            "Это не атрибуция: посмотри их сама по кнопкам ниже.",
        ]
        for item in evidence:
            lines.append(f"[{item['n']}] {source_line(item)}")
            if item.get("why"):
                lines.append("     почему выбрана: " + "; ".join(item["why"][:3]))
    else:
        lines += ["", "Страниц-кандидатов по тексту не нашлось."]
    lines += ["", "Производитель, модель, комплект и датировка не установлены. "
              "Можно попробовать ещё раз позже."]
    return "\n".join(lines)


def candidates_debug(result):
    """Для администратора: какие страницы отобраны и почему."""
    items = result.get("candidates") or result.get("evidence") or []
    if not items:
        return "🗂 Кандидатов не было."
    lines = ["🗂 Кандидаты для сравнения (отбор по тексту):"]
    for item in items:
        why = "; ".join(item.get("why") or []) or "—"
        score = f" · балл {item['score']}" if item.get("score") is not None else ""
        lines.append(f"[{item['n']}] {item['document_code']} PDF {item['pdf_page']}{score} · {why}")
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
    for kind, images, input_tokens, output_tokens, value, ok, state, error, request in rows:
        total += value
        status = status_notes.get(state, "")
        lines.append(
            f"• {names.get(kind, kind)}: картинок {images}, вход {input_tokens}, "
            f"выход {output_tokens} токенов — {value:.5f} ${status}"
        )
        if error:
            lines.append(f"   ошибка: {error}")
        if error and request:
            lines.append(f"   запрос: {request}")
    lines.append(f"Итого: {total:.5f} $ за {len(rows)} вызов(а)")
    return "\n".join(lines)


def summary_for_card(result):
    best = best_evidence(result)
    if not best:
        return None
    parts = []
    # В карточку — только марка с клейма владелицы; чтение ИИ с фото не записываем.
    source = result.get("brand_source") or ("photo" if result.get("brands_from_mark") else None)
    if result.get("brands_from_mark") and source == "owner":
        parts.append(result["brands_from_mark"][0].title())
    for key in ("set_name", "model_name"):
        value = best.get(key)
        if value and value["confirmed"]:
            parts.append(f"«{value['value']}»")
    label = "совпадение" if best["verdict"] == "exact" else "вероятно"
    return f"{label}: {' '.join(parts) or 'см. источник'} — {source_line(best)}"


def save_to_card(db_path, attribution_id, user_id, allow_unowned=False):
    """Сохраняет атрибуцию в карточку владелицы. Возвращает текст или None.
    allow_unowned=True (только администратор) — ещё и старые карточки без
    владелицы, как в «Моей шкатулке»."""
    record = get_attribution(db_path, attribution_id)
    if not record or record["user_id"] != user_id or not record["item_code"] or not record["result"]:
        return None
    summary = summary_for_card(record["result"])
    if not summary:
        return None
    conn = sqlite3.connect(db_path)
    try:
        access = "(owner_user_id = ? OR (owner_user_id IS NULL AND ?)) AND deleted_at IS NULL"
        access_params = (user_id, bool(allow_unowned))
        owner = conn.execute(
            f"SELECT 1 FROM items WHERE item_code = ? AND {access}",
            (record["item_code"], *access_params),
        ).fetchone()
        if not owner:
            return None
        conn.execute(
            "INSERT INTO item_attributions (item_code, user_id, attribution_id, summary) VALUES (?, ?, ?, ?)",
            (record["item_code"], user_id, attribution_id, summary),
        )
        conn.execute(
            "UPDATE items SET attribution_status = ?, updated_at = datetime('now') "
            f"WHERE item_code = ? AND {access}",
            (summary[:300], record["item_code"], *access_params),
        )
        conn.commit()
    finally:
        conn.close()
    return summary
