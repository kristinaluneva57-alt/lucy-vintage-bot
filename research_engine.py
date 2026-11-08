"""Lucy Research Engine — исследование украшения из шкатулки.

Режимы:
- research — 🔬 исследование украшения: фото (модель их рассматривает),
  карточка, до 12 страниц книг целиком, 4 интернет-запроса;
- deep — 🧭 глубокая атрибуция: до 25 страниц книг, 8 целевых запросов
  (справочники клейм, музеи, аукционы, патенты) и второй проход
  «критик», который ищет утверждения без опоры на источники.

Как идёт исследование (каждый вызов ИИ — с резервом в общем бюджете
и фактической стоимостью в ai_ledger):
1. Наблюдение: модель описывает только то, что видно на фото, и
   предлагает поисковые термины. Клеймо, которое вписала владелица, —
   главный факт.
2. Книги: текстовый поиск по карточке и терминам → лучшие содержательные
   страницы целиком, пронумерованные [B1]…
3. Интернет: поставщик web_search → реальные найденные страницы [W1]…
4. Синтез: сильная модель (LUCY_RESEARCH_MODEL) возвращает JSON —
   определение, признаки, увиденное на фото, сведения из книг и сети,
   аналоги, цены, подтверждено / предполагается / неизвестно.
5. Критик (только deep): отмечает неподтверждённые и преувеличенные
   утверждения.
6. Проверка кодом: ссылки только на реально переданные источники;
   доказательства без ссылок, аналоги и цены без источника удаляются;
   «продано за» — только если источник аукционный и в тексте есть слова
   о продаже, а сумма есть в тексте источника; без подтверждённых
   источников уверенность не выше «низкой». Число удалённого и
   пониженного сохраняется — это метрика честности для испытаний.

Книги, страницы и карточки не меняются: результат пишется в
item_research.
"""

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import ai_ledger
import attribution
import library_ai
import library_search
import page_images
import visual_ai
import web_search

DB_TIMEOUT = 30
IMAGE_TOKENS = visual_ai.IMAGE_TOKENS_ESTIMATE
CHARS_PER_TOKEN = 3.0
OVERHEAD_TOKENS = 400
AI_TIMEOUT = 180.0
RUNNING_STALE_MINUTES = 30
WEB_CONTENT_CHARS = 1500
OBSERVE_MAX_TOKENS = 1000
CRITIC_MAX_TOKENS = 1200

MODES = {
    "research": {
        "label": "🔬 Исследование украшения",
        "photos": 3,
        "library_pages": 12,
        "page_chars": 3000,
        "web_queries": 4,
        "web_depth": "basic",
        "web_per_query": 4,
        "synth_tokens": 3000,
        "critic": False,
    },
    "deep": {
        "label": "🧭 Глубокая атрибуция",
        "photos": 3,
        "library_pages": 25,
        "page_chars": 3000,
        "web_queries": 8,
        "web_depth": "advanced",
        "web_per_query": 4,
        "synth_tokens": 4000,
        "critic": True,
    },
}

AUCTION_DOMAINS = {
    "liveauctioneers.com", "invaluable.com", "christies.com", "sothebys.com",
    "bonhams.com", "dorotheum.com", "catawiki.com", "the-saleroom.com",
    "lot-tissimo.com", "ha.com", "skinnerinc.com", "doyle.com",
    "hindmanauctions.com", "freemansauction.com", "lempertz.com",
    "nagel-auktionen.de", "kollerauktionen.ch", "drouot.com", "interencheres.com",
    "worthpoint.com", "phillips.com", "artcurial.com", "ader-paris.fr",
}
LISTING_DOMAINS = {
    "ebay.com", "ebay.de", "ebay.co.uk", "ebay.fr", "ebay.it", "etsy.com",
    "rubylane.com", "1stdibs.com", "vinted.com", "vinted.de", "vinted.fr",
    "chairish.com", "poshmark.com", "pamono.com",
}
MUSEUM_DOMAINS = {
    "metmuseum.org", "vam.ac.uk", "britishmuseum.org", "collections.si.edu",
    "cooperhewitt.org", "rijksmuseum.nl", "mfa.org", "artic.edu",
    "collections.mfa.org", "nms.ac.uk", "smb.museum", "europeana.eu",
}
REFERENCE_DOMAINS = {
    "illusionjewels.com", "patents.google.com", "jewelcollect.org",
    "morninggloryjewelry.com", "vintagejewelrylane.com", "jewelrycollectorsweb.com",
}
SOLD_WORDS = (
    "sold for", "sold at", "price realized", "price realised", "realized", "realised",
    "hammer price", "hammer", "winning bid", "sold", "verkauft", "zuschlag",
    "adjugé", "venduto", "aggiudicato", "продано", "продана",
)

OBSERVE_SYSTEM = """You are Lucy, a careful researcher of vintage costume and antique jewelry for OH MY GOD VINTAGE School. You see photos of ONE piece from a collector's box and the owner's card.

Describe ONLY what is actually visible. Do not identify the maker from style alone. The mark text written by the owner is the main fact; if a photo shows the mark too, say whether it is legible.

Return a JSON object:
{
 "object_type": "brooch / necklace / ... (Russian)",
 "observations": [{"text": "visible feature in Russian", "photo": "front|back|mark|details|extra"}],
 "construction": "clasp, pin, hinge, settings, findings, backs — what is visible (Russian)",
 "materials_visible": "what the materials look like — appearance only, not proof (Russian)",
 "mark_seen": "mark text as seen on photos, or null",
 "cannot_tell": ["what cannot be determined from the photos (Russian)"],
 "library_terms": ["up to 8 short English terms to search jewelry books: maker names from the owner's mark, materials, techniques, motif"],
 "web_queries": ["up to 8 English web search queries for this exact piece: mark + type + motif, maker history, mark reference, auction results"]
}"""

SYNTH_SYSTEM = """You are Lucy, a careful researcher of vintage jewelry for OH MY GOD VINTAGE School. Write the result in Russian for a collector.

You get: the owner's card (the owner's mark text is the main fact), observations of the photos, the photos themselves, numbered pages from books of the school library [B1], [B2]… (OCR text, any language, typos possible) and numbered web pages actually returned by a search [W1], [W2]…

Rules — no exceptions:
1. Use only facts from the given sources, the card and what is visible on the photos. Never invent makers, dates, marks, sales, prices, pages or links.
2. Every statement in book_findings, web_findings, analogs, prices and confirmed must have refs: "B3", "W2". Use "PHOTO" only for what you see on the photos, "CARD" only for what the owner wrote.
3. Separate clearly: confirmed (supported by a source or the owner's mark), assumed (likely, but not proven), unknown.
4. A price is "sold" only if the source says the item was sold at auction (realized/hammer/sold for). Asking prices of shops and marketplaces are "asking". Estimates are "estimate". Copy the amount exactly as in the source.
5. For analogs say what is similar and what is different from our piece.
6. Confidence "high" only when a source matches the mark AND the design; otherwise "medium" or "low". If data is insufficient, say so.
7. Do not write titles of books or URLs — only refs.

Return a JSON object:
{
 "identification": {"summary": "...", "type": "...", "maker": "... or null", "period": "... or null", "materials": "... or null", "confidence": "low|medium|high"},
 "basis": [{"text": "feature the identification relies on", "refs": ["PHOTO", "CARD", "B1", "W2"]}],
 "visual": [{"text": "what you actually see on the photos", "photo": "front|back|mark|details|extra"}],
 "book_findings": [{"text": "...", "refs": ["B1"]}],
 "web_findings": [{"text": "...", "refs": ["W1"]}],
 "analogs": [{"text": "which analog", "similar": "...", "different": "...", "refs": ["W3"]}],
 "prices": [{"amount": "120", "currency": "USD", "kind": "sold|asking|estimate", "text": "context", "refs": ["W4"]}],
 "confirmed": [{"text": "...", "refs": ["CARD", "B1"]}],
 "assumed": [{"text": "...", "refs": ["PHOTO", "W2"]}],
 "unknown": ["..."],
 "next_steps": ["what to check or photograph next"]
}"""

CRITIC_SYSTEM = """You check a draft attribution of a vintage jewelry piece against its sources. Each claim has an id like "c7" and refs to sources. For each claim decide: is it actually supported by the referenced source texts (or the photo/card when refs say PHOTO/CARD)? Be strict: names, dates, places and prices must appear in the source.

Return a JSON object: {"unsupported": ["c3"], "overstated": ["c5"], "notes": ["short Russian remarks"]}.
unsupported — the sources do not say this. overstated — the source supports only a weaker version (move to assumptions)."""

CONFIDENCE_LABELS = {"low": "низкая", "medium": "средняя", "high": "высокая"}
PRICE_LABELS = {
    "sold": "продажа на аукционе",
    "asking": "цена объявления",
    "estimate": "оценка",
    "unverified": "тип цены не подтверждён",
}
PHOTO_LABELS = {"front": "лицо", "back": "оборот", "mark": "клеймо", "details": "детали", "extra": "доп. фото"}
SECTIONS = ("basis", "book_findings", "web_findings", "analogs", "prices", "confirmed", "assumed")


class ResearchUnavailable(Exception):
    """reason: disabled | no_key | budget | quota | error | bad_json."""

    def __init__(self, reason, details=""):
        super().__init__(reason)
        self.reason = reason
        self.details = details


class AlreadyRunning(Exception):
    pass


# --- настройки ---

def enabled():
    return os.environ.get("LUCY_RESEARCH_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


def research_model():
    return os.environ.get("LUCY_RESEARCH_MODEL", "gpt-4.1").strip() or "gpt-4.1"


def search_price_per_credit():
    try:
        return max(0.0, float(os.environ.get("LUCY_SEARCH_PRICE_PER_CREDIT", 0.008)))
    except ValueError:
        return 0.008


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
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS item_research (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_code TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                inputs_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                request_json TEXT NOT NULL,
                result_json TEXT,
                estimate_usd REAL DEFAULT 0,
                cost_usd REAL DEFAULT 0,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_item_research_item ON item_research (item_code)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_item_research_hash ON item_research (inputs_hash)")
    finally:
        conn.close()
    ai_ledger.init_tables(db_path)


_COLUMNS = ("id, item_code, user_id, mode, inputs_hash, status, request_json, result_json, "
            "estimate_usd, cost_usd, error, created_at, updated_at")


def _row(row):
    if row is None:
        return None
    keys = [c.strip() for c in _COLUMNS.split(",")]
    record = dict(zip(keys, row))
    record["request"] = json.loads(record.pop("request_json"))
    record["result"] = json.loads(record["result_json"]) if record.get("result_json") else None
    record.pop("result_json")
    return record


def get(db_path, research_id):
    conn = _connect(db_path)
    try:
        return _row(conn.execute(f"SELECT {_COLUMNS} FROM item_research WHERE id = ?", (research_id,)).fetchone())
    finally:
        conn.close()


def latest_for_item(db_path, item_code):
    conn = _connect(db_path)
    try:
        return _row(conn.execute(
            f"SELECT {_COLUMNS} FROM item_research WHERE item_code = ? AND status = 'done' "
            "ORDER BY id DESC LIMIT 1",
            (item_code,),
        ).fetchone())
    finally:
        conn.close()


def done_count(db_path, item_code):
    conn = _connect(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM item_research WHERE item_code = ? AND status = 'done'", (item_code,)
        ).fetchone()[0]
    finally:
        conn.close()


def card_text(card):
    """Карточка для ИИ. Клеймо владелицы — первым и как главный факт."""
    lines = []
    mark = attribution.owner_mark(card)
    if mark:
        lines.append(f"OWNER'S MARK (main fact, written by the owner): {mark}")
    elif card.get("mark_status") == "no":
        lines.append("Owner says: no mark.")
    labels = (
        ("notes", "Owner's description"), ("measurements", "Size"), ("weight", "Weight"),
        ("provenance", "Where found"), ("seller_story", "Seller's story (a clue, not proof)"),
    )
    for key, label in labels:
        value = (card.get(key) or "").strip()
        if value and value.lower() not in ("нет", "не указаны", "не указан", "не указано"):
            lines.append(f"{label}: {value}")
    return "\n".join(lines) or "The card has no text fields."


def inputs_hash(card, mode, photo_ids, version, provider):
    payload = json.dumps(
        {"card": card_text(card), "mode": mode, "photos": list(photo_ids),
         "library": version, "web": provider, "model": research_model()},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def estimate(mode, provider=None):
    """Оценка сверху для плана: все вызовы ИИ + интернет-поиск."""
    config = MODES[mode]
    model = research_model()
    provider = provider or web_search.get_provider()
    observe_in = config["photos"] * IMAGE_TOKENS + 1500 / CHARS_PER_TOKEN + OVERHEAD_TOKENS
    total = ai_ledger.token_cost(model, observe_in, OBSERVE_MAX_TOKENS)
    web_results = config["web_queries"] * config["web_per_query"] if provider.connected else 0
    synth_in = (
        config["library_pages"] * config["page_chars"] / CHARS_PER_TOKEN
        + web_results * WEB_CONTENT_CHARS / CHARS_PER_TOKEN
        + min(2, config["photos"]) * IMAGE_TOKENS + 2500 / CHARS_PER_TOKEN + OVERHEAD_TOKENS
    )
    total += ai_ledger.token_cost(model, synth_in, config["synth_tokens"])
    if config["critic"]:
        total += ai_ledger.token_cost(model, synth_in * 0.6, CRITIC_MAX_TOKENS)
    if provider.connected:
        credits = provider.credits_for([None] * config["web_queries"], config["web_depth"])
        total += credits * search_price_per_credit()
    return total


def library_version(db_path):
    """Состояние библиотеки: новые страницы — новое исследование, а не старый кэш."""
    conn = _connect(db_path)
    try:
        last, count = conn.execute("SELECT COALESCE(MAX(id), 0), COUNT(*) FROM library_pages").fetchone()
    finally:
        conn.close()
    return f"{last}:{count}"


def plan(db_path, item_code, user_id, card, mode, photo_ids):
    """Создаёт план исследования или находит готовое / идущее.

    Возвращает (состояние, запись): cached — готовое исследование тех же
    данных (бесплатно); running — уже идёт; planned — новый план.
    """
    provider = web_search.get_provider()
    digest = inputs_hash(card, mode, photo_ids, library_version(db_path), provider.name)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM item_research WHERE inputs_hash = ? AND status = 'done' "
            "ORDER BY id DESC LIMIT 1",
            (digest,),
        ).fetchone()
        if row:
            return "cached", _row(row)
        running = _running_for(conn, user_id)
        if running:
            return "running", _row(running)
        request = {"card": card, "photo_ids": list(photo_ids), "provider": provider.name}
        cursor = conn.execute(
            """
            INSERT INTO item_research (item_code, user_id, mode, inputs_hash, status, request_json,
                                       estimate_usd, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'planned', ?, ?, ?, ?)
            """,
            (item_code, user_id, mode, digest, json.dumps(request, ensure_ascii=False),
             estimate(mode, provider), _stamp(), _stamp()),
        )
        research_id = cursor.lastrowid
    finally:
        conn.close()
    return "planned", get(db_path, research_id)


def _running_for(conn, user_id):
    since = (_now() - timedelta(minutes=RUNNING_STALE_MINUTES)).isoformat(timespec="seconds")
    return conn.execute(
        f"SELECT {_COLUMNS} FROM item_research WHERE user_id = ? AND status = 'running' "
        "AND updated_at >= ? ORDER BY id DESC LIMIT 1",
        (user_id, since),
    ).fetchone()


def claim(db_path, research_id, user_id):
    """Атомарно переводит план в «идёт». Второе нажатие кнопки ничего не запускает."""
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _running_for(conn, user_id):
                conn.execute("ROLLBACK")
                return False
            cursor = conn.execute(
                "UPDATE item_research SET status = 'running', updated_at = ? "
                "WHERE id = ? AND user_id = ? AND status = 'planned'",
                (_stamp(), research_id, user_id),
            )
            conn.execute("COMMIT")
            return cursor.rowcount == 1
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def cancel(db_path, research_id, user_id):
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            "UPDATE item_research SET status = 'cancelled', updated_at = ? "
            "WHERE id = ? AND user_id = ? AND status = 'planned'",
            (_stamp(), research_id, user_id),
        )
        return cursor.rowcount == 1
    finally:
        conn.close()


def _finish(db_path, research_id, status, result=None, error=None):
    conn = _connect(db_path)
    try:
        cost = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN status IN ('done', 'unknown') THEN cost_usd ELSE 0 END), 0) "
            "FROM ai_spend WHERE ref = ?",
            (f"research:{research_id}",),
        ).fetchone()[0]
        conn.execute(
            "UPDATE item_research SET status = ?, result_json = ?, cost_usd = ?, error = ?, updated_at = ? "
            "WHERE id = ?",
            (status, json.dumps(result, ensure_ascii=False) if result is not None else None,
             cost, error, _stamp(), research_id),
        )
    finally:
        conn.close()


# --- вызов модели ---

def _error_details(error):
    parts = [type(error).__name__]
    status = getattr(error, "status_code", None)
    if status is not None:
        parts.append(f"HTTP {status}")
    code = getattr(error, "code", None)
    if code:
        parts.append(f"code={str(code)[:60]}")
    return " · ".join(parts)


def call_model(db_path, *, kind, system, content, images, max_tokens, user_id, research_id):
    """Один платный вызов с картинками и JSON-ответом. Возвращает dict."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ResearchUnavailable("no_key")
    model = research_model()
    text_chars = len(system) + sum(len(p.get("text", "")) for p in content if p.get("type") == "text")
    estimate_usd = ai_ledger.token_cost(
        model, text_chars / CHARS_PER_TOKEN + images * IMAGE_TOKENS + OVERHEAD_TOKENS, max_tokens
    )
    try:
        call_id = ai_ledger.reserve(
            db_path, provider="openai", kind=kind, model=model, estimate=estimate_usd,
            user_id=user_id, ref=f"research:{research_id}",
        )
    except ai_ledger.BudgetExceeded as error:
        raise ResearchUnavailable("budget", str(error)) from None

    import openai

    no_response = tuple(
        getattr(openai, name) for name in ("APITimeoutError", "APIConnectionError")
        if isinstance(getattr(openai, name, None), type)
    )
    try:
        client = openai.OpenAI(api_key=api_key, timeout=AI_TIMEOUT, max_retries=1)
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
            max_completion_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
    except no_response as error:
        details = _error_details(error) + " · ответ не получен"
        print(f"Research AI no response: {details}")
        ai_ledger.fail(db_path, call_id, details, response_received=False)
        raise ResearchUnavailable("error", details) from None
    except openai.OpenAIError as error:
        details = _error_details(error)
        print(f"Research AI error: {details}")
        ai_ledger.fail(db_path, call_id, details, response_received=True)
        code = getattr(error, "code", None)
        reason = "quota" if code in ("insufficient_quota", "billing_hard_limit_reached") else "error"
        raise ResearchUnavailable(reason, details) from None
    except BaseException as error:
        ai_ledger.fail(db_path, call_id, type(error).__name__, response_received=False)
        raise

    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "prompt_tokens", 0) or 0
    output_tokens = getattr(usage, "completion_tokens", 0) or 0
    ai_ledger.settle(db_path, call_id, ai_ledger.token_cost(model, input_tokens, output_tokens),
                     input_tokens, output_tokens)
    raw = (response.choices[0].message.content or "").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise ResearchUnavailable("bad_json", "ответ модели не JSON") from None
    if not isinstance(data, dict):
        raise ResearchUnavailable("bad_json", "ответ модели не объект JSON")
    return data


def _photo_content(photos, limit):
    """Фото для модели: клеймо и лицо — в первую очередь."""
    order = {"mark": 0, "front": 1, "back": 2, "details": 3, "extra": 4}
    chosen = sorted(photos, key=lambda p: order.get(p[0], 5))[:limit]
    parts = []
    for label, data in chosen:
        parts.append({"type": "text", "text": f"Photo: {label}"})
        parts.append(visual_ai.image_part(page_images.shrink_jpeg(data)))
    return parts, [label for label, _ in chosen]


# --- доказательства ---

def _strings(values, limit):
    return [str(v).strip() for v in (values or []) if str(v).strip()][:limit]


def library_evidence(db_path, card, observation, config):
    """Лучшие содержательные страницы книг по карточке и терминам наблюдения."""
    terms, _ = attribution.card_terms(card)
    terms += _strings(observation.get("library_terms"), 8)
    query = " ".join(dict.fromkeys(t for t in terms if t))
    if not query:
        return [], query
    plan_ = library_search.build_search_plan(query)
    results = library_search.search_pages(db_path, plan_, limit=config["library_pages"] * 3)
    good = [r for r in results if r.get("quality", 1) >= library_ai.MIN_PAGE_QUALITY]
    pages = []
    for number, result in enumerate(good[:config["library_pages"]], start=1):
        text = (result.get("text") or "").strip()
        if len(text) > config["page_chars"]:
            text = "\n…\n".join(library_search.passages(result, max_passages=3, before=500, after=1000))
            text = text[:config["page_chars"]]
        pages.append({
            "ref": f"B{number}",
            "title": result["title"],
            "document_code": result.get("document_code"),
            "pdf_page": result["pdf_page"],
            "printed_page": result.get("printed_page"),
            "language": result.get("language"),
            "text": text,
        })
    return pages, query


def web_queries(card, observation, config):
    queries = _strings(observation.get("web_queries"), config["web_queries"])
    mark = attribution.owner_mark(card)
    if mark and not any(mark.lower() in q.lower() for q in queries):
        queries.insert(0, f"{mark} jewelry mark")
    return queries[:config["web_queries"]]


def web_evidence(db_path, provider, queries, config, user_id, research_id):
    """Найденные страницы + учёт кредитов поиска."""
    if not provider.connected or not queries:
        return []
    credits = provider.credits_for(queries, config["web_depth"])
    estimate_usd = credits * search_price_per_credit()
    try:
        call_id = ai_ledger.reserve(
            db_path, provider=provider.name, kind="web_search", estimate=estimate_usd,
            user_id=user_id, ref=f"research:{research_id}",
        )
    except ai_ledger.BudgetExceeded as error:
        raise ResearchUnavailable("budget", str(error)) from None
    try:
        found = provider.search(queries, depth=config["web_depth"], per_query=config["web_per_query"])
    except web_search.SearchUnavailable as error:
        ai_ledger.fail(db_path, call_id, f"{error.reason}: {error.details}", response_received=True)
        return []
    except Exception as error:
        ai_ledger.fail(db_path, call_id, type(error).__name__, response_received=False)
        return []
    ai_ledger.settle(db_path, call_id, estimate_usd, units=credits)
    sources, seen = [], set()
    for item in found:
        url = (item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        sources.append({
            "ref": f"W{len(sources) + 1}",
            "url": url,
            "title": (item.get("title") or web_search.domain_of(url))[:200],
            "domain": web_search.domain_of(url),
            "content": (item.get("content") or "")[:WEB_CONTENT_CHARS],
            "query": item.get("query"),
        })
    return sources


def _source_blocks(books, web):
    parts = []
    for page in books:
        parts.append(
            f"[{page['ref']}] book page (language: {library_search.language_name(page.get('language'))})\n{page['text']}"
        )
    for source in web:
        parts.append(f"[{source['ref']}] web page {source['domain']}: {source['title']}\n{source['content']}")
    return "\n\n".join(parts) or "No sources were found."


# --- проверка ответа кодом ---

def _clean_refs(refs, valid, allow):
    result = []
    for ref in refs or []:
        ref = str(ref).strip().upper().strip("[]")
        if ref in valid or ref in allow:
            if ref not in result:
                result.append(ref)
    return result


def _domain_kind(domain):
    def match(domains):
        return any(domain == d or domain.endswith("." + d) for d in domains)
    if match(AUCTION_DOMAINS):
        return "auction"
    if match(LISTING_DOMAINS):
        return "listing"
    if match(MUSEUM_DOMAINS):
        return "museum"
    if match(REFERENCE_DOMAINS):
        return "reference"
    return "web"


def _digits(text):
    return re.sub(r"[^\d]", "", text or "")


def validate(draft, books, web, card):
    """Оставляет только подкреплённое. Возвращает (результат, метрики)."""
    valid = {p["ref"] for p in books} | {s["ref"] for s in web}
    by_ref = {s["ref"]: s for s in web}
    metrics = {"removed": 0, "downgraded": 0, "removed_items": []}

    def drop(section, item, why):
        metrics["removed"] += 1
        metrics["removed_items"].append(f"{section}: {str(item.get('text') or item.get('amount'))[:80]} — {why}")

    out = {}
    ident = draft.get("identification") if isinstance(draft.get("identification"), dict) else {}
    out["identification"] = {
        key: (str(ident.get(key)).strip() if ident.get(key) not in (None, "", "null") else None)
        for key in ("summary", "type", "maker", "period", "materials")
    }
    confidence = str(ident.get("confidence") or "low").lower()
    out["identification"]["confidence"] = confidence if confidence in CONFIDENCE_LABELS else "low"

    out["visual"] = [
        {"text": str(v.get("text")).strip(), "photo": v.get("photo") if v.get("photo") in PHOTO_LABELS else None}
        for v in draft.get("visual") or [] if isinstance(v, dict) and str(v.get("text") or "").strip()
    ]

    rules = {
        "basis": ({"PHOTO", "CARD"}, False),
        "book_findings": (set(), True),
        "web_findings": (set(), True),
        "analogs": (set(), True),
        "confirmed": ({"CARD"}, False),
        "assumed": ({"PHOTO", "CARD"}, False),
    }
    for section, (allow, need_source) in rules.items():
        kept = []
        for item in draft.get(section) or []:
            if not isinstance(item, dict) or not str(item.get("text") or "").strip():
                continue
            refs = _clean_refs(item.get("refs"), valid, allow)
            if section == "book_findings":
                refs = [r for r in refs if r.startswith("B")]
            if section == "web_findings":
                refs = [r for r in refs if r.startswith("W")]
            if not refs:
                if section == "confirmed":
                    # «Подтверждено» без источника — только предположение.
                    metrics["downgraded"] += 1
                    out.setdefault("_moved", []).append({"text": str(item["text"]).strip(), "refs": []})
                    continue
                if section == "assumed":
                    kept.append({"text": str(item["text"]).strip(), "refs": []})
                    continue
                drop(section, item, "нет ссылки на источник")
                continue
            entry = {"text": str(item["text"]).strip(), "refs": refs}
            if section == "analogs":
                entry["similar"] = str(item.get("similar") or "").strip()
                entry["different"] = str(item.get("different") or "").strip()
            kept.append(entry)
        out[section] = kept
    out["assumed"] = out.get("_moved", []) + out["assumed"]
    out.pop("_moved", None)

    prices = []
    for item in draft.get("prices") or []:
        if not isinstance(item, dict):
            continue
        refs = [r for r in _clean_refs(item.get("refs"), valid, set()) if r.startswith("W")]
        amount = str(item.get("amount") or "").strip()
        if not refs or not _digits(amount):
            drop("prices", item, "нет интернет-источника или суммы")
            continue
        source = by_ref[refs[0]]
        if _digits(amount) not in _digits(source["content"]):
            drop("prices", item, "суммы нет в тексте источника")
            continue
        kind = str(item.get("kind") or "").lower()
        domain_kind = _domain_kind(source["domain"])
        text_lower = source["content"].lower()
        if kind == "sold" and not (domain_kind == "auction" and any(w in text_lower for w in SOLD_WORDS)):
            kind = "asking" if domain_kind == "listing" else "unverified"
            metrics["downgraded"] += 1
        if kind not in PRICE_LABELS:
            kind = "unverified"
        prices.append({
            "amount": amount, "currency": str(item.get("currency") or "").strip()[:8],
            "kind": kind, "text": str(item.get("text") or "").strip(), "refs": refs,
        })
    out["prices"] = prices

    out["unknown"] = _strings(draft.get("unknown"), 10)
    out["next_steps"] = _strings(draft.get("next_steps"), 8)

    # Клеймо владелицы — всегда первое подтверждённое.
    mark = attribution.owner_mark(card)
    if mark and not any("CARD" in c["refs"] and mark.lower() in c["text"].lower() for c in out["confirmed"]):
        out["confirmed"].insert(0, {"text": f"Клеймо со слов владелицы: {mark}", "refs": ["CARD"]})

    sourced = any(
        any(r.startswith(("B", "W")) for r in item["refs"])
        for section in ("confirmed", "book_findings", "web_findings") for item in out[section]
    )
    if not sourced and out["identification"]["confidence"] != "low":
        out["identification"]["confidence"] = "low"
        metrics["downgraded"] += 1
    return out, metrics


def apply_critic(result, verdict, metrics):
    """Убирает неподтверждённое и переносит преувеличенное в предположения."""
    ids = claim_ids(result)
    unsupported = {str(c) for c in verdict.get("unsupported") or []}
    overstated = {str(c) for c in verdict.get("overstated") or []}
    for claim_id, (section, index) in sorted(ids.items(), key=lambda kv: -kv[1][1]):
        if claim_id in unsupported:
            item = result[section].pop(index)
            metrics["removed"] += 1
            metrics["removed_items"].append(f"{section}: {item['text'][:80]} — критик: нет в источниках")
        elif claim_id in overstated and section in ("confirmed", "book_findings", "web_findings"):
            item = result[section].pop(index)
            result["assumed"].append({"text": item["text"], "refs": item["refs"]})
            metrics["downgraded"] += 1
    result["critic_notes"] = _strings(verdict.get("notes"), 6)
    return result


def claim_ids(result):
    ids, number = {}, 0
    for section in SECTIONS:
        if section == "prices":
            continue
        for index, _ in enumerate(result.get(section) or []):
            number += 1
            ids[f"c{number}"] = (section, index)
    return ids


def _claims_for_critic(result):
    lines = []
    for claim_id, (section, index) in claim_ids(result).items():
        item = result[section][index]
        lines.append(f"{claim_id} [{section}] {item['text']} (refs: {', '.join(item['refs']) or '—'})")
    return "\n".join(lines)


# --- исследование ---

def run(db_path, research_id, photos, provider=None):
    """Синхронное исследование (вызывается в фоне). photos — [(метка, JPEG)]."""
    record = get(db_path, research_id)
    if record is None or record["status"] != "running":
        raise AlreadyRunning("исследование не в статусе «идёт»")
    if not enabled():
        _finish(db_path, research_id, "error", error="disabled")
        raise ResearchUnavailable("disabled")
    provider = provider or web_search.get_provider()
    config = MODES[record["mode"]]
    card = record["request"]["card"]
    user_id = record["user_id"]
    calls = dict(user_id=user_id, research_id=research_id)
    try:
        photo_parts, seen_photos = _photo_content(photos, config["photos"])
        observation = call_model(
            db_path, kind="research_observe", system=OBSERVE_SYSTEM,
            content=[{"type": "text", "text": "Owner's card:\n" + card_text(card)}] + photo_parts,
            images=len(seen_photos), max_tokens=OBSERVE_MAX_TOKENS, **calls,
        )
        books, library_query = library_evidence(db_path, card, observation, config)
        queries = web_queries(card, observation, config)
        web = web_evidence(db_path, provider, queries, config, user_id, research_id)

        synth_photos, synth_seen = _photo_content(photos, min(2, config["photos"]))
        text = (
            "Owner's card:\n" + card_text(card)
            + "\n\nObservations of the photos (JSON):\n" + json.dumps(observation, ensure_ascii=False)
            + "\n\nSources:\n\n" + _source_blocks(books, web)
        )
        draft = call_model(
            db_path, kind="research_synthesis", system=SYNTH_SYSTEM,
            content=[{"type": "text", "text": text}] + synth_photos,
            images=len(synth_seen), max_tokens=config["synth_tokens"], **calls,
        )
        result, metrics = validate(draft, books, web, card)
        if config["critic"]:
            verdict = call_model(
                db_path, kind="research_critic", system=CRITIC_SYSTEM,
                content=[{"type": "text", "text": "Claims:\n" + _claims_for_critic(result)
                          + "\n\nOwner's card:\n" + card_text(card)
                          + "\n\nSources:\n\n" + _source_blocks(books, web)}],
                images=0, max_tokens=CRITIC_MAX_TOKENS, **calls,
            )
            result = apply_critic(result, verdict, metrics)
    except ResearchUnavailable as problem:
        _finish(db_path, research_id, "error", error=f"{problem.reason}: {problem.details}".rstrip(": "))
        raise
    except Exception as error:
        _finish(db_path, research_id, "error", error=type(error).__name__)
        raise

    result.update({
        "mode": record["mode"],
        "item_code": record["item_code"],
        "model": research_model(),
        "photos_seen": seen_photos,
        "observation": {
            "object_type": observation.get("object_type"),
            "construction": observation.get("construction"),
            "materials_visible": observation.get("materials_visible"),
            "mark_seen": observation.get("mark_seen"),
            "cannot_tell": _strings(observation.get("cannot_tell"), 6),
        },
        "books": books,
        "web": web,
        "library_query": library_query,
        "web_queries": queries,
        "web_provider": provider.name if provider.connected else None,
        "metrics": metrics,
    })
    _finish(db_path, research_id, "done", result=result)
    return get(db_path, research_id)


# --- оформление ---

def _refs_text(refs):
    shown = [r for r in refs if r not in ("PHOTO", "CARD")]
    tags = []
    if "PHOTO" in refs:
        tags.append("фото")
    if "CARD" in refs:
        tags.append("карточка")
    parts = [" ".join(f"[{r}]" for r in shown)] if shown else []
    if tags:
        parts.append(f"({', '.join(tags)})")
    return " ".join(parts)


def _line(item):
    refs = _refs_text(item.get("refs") or [])
    return f"— {item['text']}" + (f" {refs}" if refs else "")


def report_lines(record):
    result = record["result"]
    ident = result["identification"]
    mode_label = MODES[result["mode"]]["label"]
    lines = [f"{mode_label} · {result['item_code']}", ""]
    lines.append(f"🧾 Предварительно: {ident.get('summary') or 'определить не удалось'}")
    lines.append(f"Уверенность: {CONFIDENCE_LABELS[ident['confidence']]}")
    for key, label in (("type", "Предмет"), ("maker", "Производитель"),
                       ("period", "Период"), ("materials", "Материалы")):
        if ident.get(key):
            lines.append(f"{label}: {ident[key]}")

    def section(title, items, formatter):
        if items:
            lines.append("")
            lines.append(title)
            lines.extend(formatter(item) for item in items)

    photos = ", ".join(PHOTO_LABELS.get(p, p) for p in result.get("photos_seen") or [])
    section(f"📷 Что модель рассмотрела на фото ({photos or 'фото нет'}):", result["visual"],
            lambda v: f"— {v['text']}" + (f" ({PHOTO_LABELS[v['photo']]})" if v.get("photo") else ""))
    section("🔎 На чём основано:", result["basis"], lambda b: _line(b))
    section("📚 Из книг библиотеки:", result["book_findings"], lambda b: _line(b))
    section("🌐 Из интернета:", result["web_findings"], lambda w: _line(w))
    section("🪞 Аналоги:", result["analogs"], lambda a: (
        _line(a)
        + (f"\n   совпадает: {a['similar']}" if a.get("similar") else "")
        + (f"\n   отличается: {a['different']}" if a.get("different") else "")))
    section("💶 Цены:", result["prices"], lambda p: (
        f"— {p['amount']} {p['currency']} — {PRICE_LABELS[p['kind']]}"
        + (f": {p['text']}" if p.get("text") else "") + f" {_refs_text(p['refs'])}"))
    section("✅ Подтверждено:", result["confirmed"], lambda c: _line(c))
    section("🤔 Предполагается:", result["assumed"], lambda c: _line(c))
    section("❓ Неизвестно:", result["unknown"], lambda u: f"— {u}")
    section("➡️ Что сделать дальше:", result["next_steps"], lambda s: f"— {s}")

    cited = _cited(result)
    books = [p for p in result["books"] if p["ref"] in cited]
    web = [s for s in result["web"] if s["ref"] in cited]
    if books or web:
        lines += ["", "📖 Источники:"]
        lines += [f"[{p['ref']}] {p['title']} ({p.get('document_code') or '—'}) — "
                  f"{library_ai.page_label(p)}" for p in books]
        lines += [f"[{s['ref']}] {s['title']} — {s['url']}" for s in web]

    lines += ["", "ℹ️ Как искала:"]
    lines.append(f"— книги: {len(result['books'])} стр. найдено по тексту карточки и описанию"
                 + (f" (запрос: {result['library_query'][:80]})" if result.get("library_query") else ""))
    if result.get("web_provider"):
        lines.append(f"— интернет: {len(result['web_queries'])} запросов, {len(result['web'])} страниц")
    else:
        lines.append("— интернет-поиск не подключён: только книги и фото")
    lines.append("— фото рассмотрела модель; поиска по картинкам нет — аналоги найдены по тексту")
    metrics = result.get("metrics") or {}
    lines.append(f"— проверка: удалено утверждений без источника: {metrics.get('removed', 0)}, "
                 f"понижено до предположения: {metrics.get('downgraded', 0)}")
    if result.get("critic_notes"):
        lines.append("— замечания проверки: " + "; ".join(result["critic_notes"]))
    lines.append(f"— стоимость: {record.get('cost_usd') or 0:.4f} $ · модель {result.get('model')}")
    lines += ["", "💬 Это исследование, а не экспертиза. Проверь источники по ссылкам."]
    return lines


def _cited(result):
    cited = set()
    for section_name in SECTIONS:
        for item in result.get(section_name) or []:
            cited.update(item.get("refs") or [])
    return cited


def plan_lines(record):
    mode = MODES[record["mode"]]
    provider = record["request"].get("provider")
    lines = [
        f"{mode['label']} · {record['item_code']}",
        "",
        "Что сделаю:",
        f"— рассмотрю до {mode['photos']} фото и карточку (клеймо владелицы — главный факт);",
        f"— прочитаю до {mode['library_pages']} подходящих страниц книг целиком;",
    ]
    if provider and provider != "none":
        lines.append(f"— сделаю {mode['web_queries']} интернет-запросов (справочники, музеи, аукционы);")
    else:
        lines.append("— интернет-поиск пока не подключён — только книги и фото;")
    if mode["critic"]:
        lines.append("— вторым проходом проверю каждое утверждение по источникам;")
    lines += [
        "",
        f"Модель: {research_model()}",
        f"Стоимость: не больше ≈ {record['estimate_usd']:.2f} $ (обычно меньше).",
        "Время: 1–3 мин. Поиска по картинкам нет: аналоги ищутся по тексту.",
    ]
    return lines
