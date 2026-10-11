"""Миниатюры первых страниц книг для визуального каталога (/books, /bookcheck).

- Миниатюра делается из PDF, привязанного к конкретному DOC-ID
  (storage_key в library_documents), — не по названию и не из интернета.
- Один проход: PDF скачивается из R2 один раз, рендерятся первые
  PREVIEW_PAGES страниц (локально, PyMuPDF, без OpenAI), и вместе с
  короткой сводкой (число страниц PDF, первые строки текста, «пустая ли
  страница») всё сохраняется в R2:
      thumbnails/DOC-000007/<id-pdf>/p0001.jpg
      thumbnails/DOC-000007/<id-pdf>/info.json
  <id-pdf> — короткий хеш storage_key: если у документа когда-нибудь
  окажется другой PDF, старая миниатюра к нему не подставится.
- Повторный просмотр берёт миниатюру и сводку из R2: большой PDF не
  скачивается и не рендерится заново.
- Ничего в базе не меняется: ни названия, ни OCR, ни индексы.
"""

import hashlib
import io
import json
import re
import threading

from PIL import Image

import page_images

PREFIX = "thumbnails"
PREVIEW_PAGES = 3        # стр. 1–3: если первая пустая или только иллюстрация
THUMB_SIDE = 640         # длинная сторона миниатюры, px
RENDER_DPI = 80
BLANK_INK = 0.004        # меньше 0.4 % «чернил» — страница пустая
TEXT_LINES = 6

_build_lock = threading.Lock()


class PreviewUnavailable(Exception):
    """PDF недоступен (R2, повреждённый файл) — показываем карточку без миниатюры."""


def pdf_id(storage_key):
    return hashlib.sha1((storage_key or "").encode()).hexdigest()[:12]


def base_key(document_code, storage_key):
    return f"{PREFIX}/{document_code}/{pdf_id(storage_key)}"


# --- R2 (подменяется в тестах) ---

def r2_get(key):
    return page_images.r2_get(key)


def r2_put(key, data, content_type):
    try:
        import os

        page_images._r2().put_object(
            Bucket=os.environ["R2_BUCKET_NAME"], Key=key, Body=data, ContentType=content_type,
        )
        return True
    except Exception as error:
        print(f"thumbnail cache upload failed: {type(error).__name__}")
        return False


# --- рендер (подменяется в тестах: fitz на сервере есть, локально может не быть) ---

def render_pages(document_code, storage_key, pages=PREVIEW_PAGES):
    """Скачивает PDF документа и рендерит первые страницы.
    Возвращает (число страниц PDF, [(номер, JPEG, текст страницы)])."""
    import fitz

    path = page_images.pdf_path(document_code, storage_key)
    rendered = []
    with fitz.open(path) as pdf:
        total = pdf.page_count
        for number in range(1, min(pages, total) + 1):
            page = pdf.load_page(number - 1)
            zoom = RENDER_DPI / 72
            pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            rendered.append((number, pixmap.tobytes("jpeg"), page.get_text("text") or ""))
    return total, rendered


# --- обработка ---

def shrink(data):
    image = Image.open(io.BytesIO(data)).convert("RGB")
    image.thumbnail((THUMB_SIDE, THUMB_SIDE))
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=80)
    return output.getvalue()


def is_blank(data):
    """Почти белая страница (нет ни текста, ни картинки)."""
    image = Image.open(io.BytesIO(data)).convert("L")
    image.thumbnail((200, 200))
    pixels = list(image.getdata())
    ink = sum(1 for value in pixels if value < 200)
    return ink / max(1, len(pixels)) < BLANK_INK


def first_lines(text, limit=TEXT_LINES):
    lines = []
    for line in (text or "").splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if sum(1 for ch in line if ch.isalpha()) >= 2:
            lines.append(line[:120])
        if len(lines) >= limit:
            break
    return lines


def build_preview(document_code, storage_key):
    """Один проход по PDF: миниатюры стр. 1–3 и сводка → R2. Возвращает info."""
    if not storage_key:
        raise PreviewUnavailable("у документа нет PDF в хранилище")
    with _build_lock:
        # Пока ждали, другой запрос мог уже всё сделать.
        cached = load_info(document_code, storage_key)
        if cached:
            return cached
        try:
            total, rendered = render_pages(document_code, storage_key)
        except Exception as error:
            print(f"thumbnail render failed for {document_code}: {type(error).__name__}")
            raise PreviewUnavailable("не удалось прочитать PDF из хранилища") from error

        base = base_key(document_code, storage_key)
        info = {"document_code": document_code, "pdf_pages": total, "pages": {}, "cached": True}
        images = {}
        for number, data, text in rendered:
            thumb = shrink(data)
            images[number] = thumb
            lines = first_lines(text)
            info["pages"][str(number)] = {
                "blank": is_blank(thumb),
                "has_text_layer": bool(lines),
                "lines": lines,
            }
            if not r2_put(f"{base}/p{number:04d}.jpg", thumb, "image/jpeg"):
                info["cached"] = False
        if info["cached"]:
            # Сводка пишется последней: есть info.json — есть и миниатюры.
            if not r2_put(f"{base}/info.json", json.dumps(info, ensure_ascii=False).encode(), "application/json"):
                info["cached"] = False
        info["_images"] = images  # только в памяти, для этого показа
        return info


def load_info(document_code, storage_key):
    data = r2_get(f"{base_key(document_code, storage_key)}/info.json")
    if not data:
        return None
    try:
        return json.loads(data)
    except ValueError:
        return None


def get_preview(document_code, storage_key, pdf_page=1):
    """(JPEG миниатюры, info, источник: 'cache'|'rendered').
    Бросает PreviewUnavailable, если PDF прочитать нельзя."""
    if pdf_page < 1 or pdf_page > PREVIEW_PAGES:
        raise ValueError("Миниатюры есть только для первых страниц")
    info = load_info(document_code, storage_key)
    if info:
        if str(pdf_page) not in info["pages"]:
            return None, info, "cache"
        image = r2_get(f"{base_key(document_code, storage_key)}/p{pdf_page:04d}.jpg")
        if image:
            return image, info, "cache"
    info = build_preview(document_code, storage_key)
    image = (info.get("_images") or {}).get(pdf_page)
    if image is None and str(pdf_page) in info["pages"]:
        image = r2_get(f"{base_key(document_code, storage_key)}/p{pdf_page:04d}.jpg")
    return image, info, "rendered" if info.get("_images") else "cache"


def page_is_cover_like(info, pdf_page=1):
    page = (info or {}).get("pages", {}).get(str(pdf_page)) or {}
    return not page.get("blank") and bool(page.get("lines"))


# Слова, которые есть почти в каждой книге о бижутерии: по ним одним
# нельзя считать, что название совпало с обложкой.
GENERIC_TITLE_WORDS = {
    "jewelry", "jewellery", "jewels", "jewel", "bijoux", "schmuck", "gioielli",
    "guide", "collector", "collectors", "collecting", "book", "books", "with",
    "price", "prices", "values", "identification", "edition", "vintage", "from",
    "unauthorized", "complete", "encyclopedia", "history",
}


def title_found(title, *texts):
    """Есть ли значимые слова названия из каталога в тексте первых страниц.
    None — проверить нечем (в названии нет значимых слов)."""
    words = [w for w in re.findall(r"[a-zа-яё0-9]+", (title or "").lower()) if len(w) >= 4]
    significant = [w for w in words if w not in GENERIC_TITLE_WORDS] or words
    if not significant:
        return None
    haystack = " ".join(t or "" for t in texts).lower()
    found = sum(1 for w in significant if w in haystack)
    return found / len(significant) >= 0.5
