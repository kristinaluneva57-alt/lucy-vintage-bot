"""Потерявшиеся книги: PDF лежит в R2, а в каталоге (library_documents) его нет.

Так бывает, когда книга уже сохранена в R2, но до распознавания очередь
не дошла: Railway перезапустил бота (деплой, рестарт), и фоновая задача
пропала без отчёта. Этот модуль:

- сравнивает список PDF в R2 (books/…) с storage_key в базе;
- рисует обложку (первую страницу) потерявшегося PDF и кладёт её в кеш R2
  (thumbnails/lost/<hash>.jpg), чтобы повторный просмотр не качал PDF;
- по короткому id (хеш ключа) находит PDF, чтобы поставить его в очередь.

Ничего в базе не меняет — только читает. Импорт делает обычный
library_importer.import_pdf_with_ocr.
"""

import hashlib
import io
import os
import shutil
import tempfile

from library_db import get_connection

BOOK_PREFIX = "books/"
CACHE_PREFIX = "thumbnails/lost"
COVER_SIDE = 640
COVER_DPI = 80
PER_PAGE = 5


def short_id(storage_key):
    """Короткий id для кнопок Telegram (callback_data — до 64 байт)."""
    return hashlib.sha1(storage_key.encode("utf-8")).hexdigest()[:12]


# --- R2 (подменяется в тестах) ---

def _r2():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def list_r2_pdfs():
    """Все PDF под books/ в R2: [{key, size, uploaded}] (uploaded — datetime)."""
    client = _r2()
    bucket = os.environ["R2_BUCKET_NAME"]
    found = []
    token = None
    while True:
        params = {"Bucket": bucket, "Prefix": BOOK_PREFIX}
        if token:
            params["ContinuationToken"] = token
        response = client.list_objects_v2(**params)
        for item in response.get("Contents", []):
            if item["Key"].lower().endswith(".pdf"):
                found.append({
                    "key": item["Key"],
                    "size": item.get("Size") or 0,
                    "uploaded": item.get("LastModified"),
                })
        if not response.get("IsTruncated"):
            break
        token = response.get("NextContinuationToken")
    return found


def known_storage_keys():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT storage_key FROM library_documents WHERE storage_key IS NOT NULL"
        ).fetchall()
    return {row[0] for row in rows}


def find_lost(r2_items=None, known=None):
    """PDF из R2, которых нет в каталоге. Свежие — первыми."""
    r2_items = list_r2_pdfs() if r2_items is None else r2_items
    known = known_storage_keys() if known is None else known
    lost = [dict(item, id=short_id(item["key"])) for item in r2_items if item["key"] not in known]
    lost.sort(key=lambda item: str(item.get("uploaded") or ""), reverse=True)
    return {"r2_total": len(r2_items), "lost": lost}


def lost_by_id(lost_id, r2_items=None, known=None):
    for item in find_lost(r2_items, known)["lost"]:
        if item["id"] == lost_id:
            return item
    return None


# --- обложка ---

def _cache_get(key):
    try:
        response = _r2().get_object(Bucket=os.environ["R2_BUCKET_NAME"], Key=key)
        return response["Body"].read()
    except Exception:
        return None


def _cache_put(key, data):
    try:
        _r2().put_object(
            Bucket=os.environ["R2_BUCKET_NAME"], Key=key, Body=data, ContentType="image/jpeg",
        )
    except Exception as error:
        print(f"lost cover cache upload failed: {type(error).__name__}")


def render_cover(storage_key):
    """(JPEG первой страницы, число страниц PDF, первые строки текста)."""
    import fitz
    from PIL import Image

    from library_importer import download_pdf_from_r2

    folder = tempfile.mkdtemp(prefix="lucy_lost_")
    try:
        path = os.path.join(folder, "book.pdf")
        download_pdf_from_r2(storage_key, path)
        with fitz.open(path) as pdf:
            pages = pdf.page_count
            page = pdf.load_page(0)
            zoom = COVER_DPI / 72
            raw = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False).tobytes("jpeg")
            text = page.get_text("text") or ""
        image = Image.open(io.BytesIO(raw)).convert("RGB")
        image.thumbnail((COVER_SIDE, COVER_SIDE))
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=80)
        lines = [line.strip() for line in text.splitlines() if line.strip()][:3]
        return out.getvalue(), pages, lines
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def cover(storage_key):
    """Обложка из кеша R2 или свежий рендер. (jpeg|None, pages|None, lines)."""
    cache_key = f"{CACHE_PREFIX}/{short_id(storage_key)}.jpg"
    cached = _cache_get(cache_key)
    if cached:
        return cached, None, []
    image, pages, lines = render_cover(storage_key)
    _cache_put(cache_key, image)
    return image, pages, lines


def describe(item):
    size_mb = (item.get("size") or 0) / (1024 * 1024)
    uploaded = item.get("uploaded")
    when = uploaded.strftime("%d.%m.%Y %H:%M") if hasattr(uploaded, "strftime") else "—"
    source = "через /bigbook" if item["key"].startswith("books/browser/") else "через чат"
    return f"Загружена: {when} UTC · {size_mb:.1f} МБ · {source}"
