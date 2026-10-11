"""Изображения страниц книг: рендер, кэш в R2, вырезки и OCR мелкого текста.

- Страница рендерится из PDF (PyMuPDF) только когда она нужна как кандидат
  или как доказательство, и сохраняется в R2: page-images/DOC-…/p0054-120.jpg.
  Повторно та же страница берётся из R2, PDF не скачивается.
- Мелкий текст (подписи, газетная реклама): нужная область страницы
  вырезается в высоком разрешении и читается бесплатным Tesseract.
- Фото пользовательницы уменьшаются до 1024 px перед отправкой в ИИ.

Исходные PDF и страницы книги не изменяются.
"""

import io
import os
import shutil
import subprocess
import tempfile
import threading

from PIL import Image

PAGE_DPI = 120          # для сравнения с фото и показа пользовательнице
CROP_DPI = 400          # для мелкого текста
MAX_SIDE = 1024         # максимальная сторона изображения для ИИ
GRID = ("A", "B", "C")  # сетка 3×3: столбцы A–C, строки 1–3
CACHE_PREFIX = "page-images"

_pdf_lock = threading.Lock()
_pdf_cache = {}         # document_code -> путь к временно скачанному PDF


# --- R2 ---

def _r2():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def r2_get(key):
    try:
        response = _r2().get_object(Bucket=os.environ["R2_BUCKET_NAME"], Key=key)
        return response["Body"].read()
    except Exception:
        return None


def r2_put(key, data):
    try:
        _r2().put_object(
            Bucket=os.environ["R2_BUCKET_NAME"], Key=key, Body=data,
            ContentType="image/jpeg",
        )
    except Exception as error:
        print(f"page cache upload failed: {type(error).__name__}")


# --- PDF ---

def pdf_path(document_code, storage_key):
    """Скачивает PDF из R2 один раз на время работы процесса (не больше 2 книг)."""
    from library_importer import download_pdf_from_r2

    with _pdf_lock:
        path = _pdf_cache.get(document_code)
        if path and os.path.exists(path):
            return path
        while len(_pdf_cache) >= 2:
            old_code, old_path = next(iter(_pdf_cache.items()))
            shutil.rmtree(os.path.dirname(old_path), ignore_errors=True)
            _pdf_cache.pop(old_code, None)
        folder = tempfile.mkdtemp(prefix="lucy_visual_pdf_")
        path = os.path.join(folder, "book.pdf")
        download_pdf_from_r2(storage_key, path)
        _pdf_cache[document_code] = path
        return path


def render_pdf_page(path, pdf_page, dpi, clip=None):
    """Рендер страницы (или её области clip в долях 0..1) в JPEG."""
    import fitz

    with fitz.open(path) as document:
        page = document.load_page(pdf_page - 1)
        rect = page.rect
        if clip:
            x0, y0, x1, y1 = clip
            rect = fitz.Rect(
                rect.x0 + rect.width * x0, rect.y0 + rect.height * y0,
                rect.x0 + rect.width * x1, rect.y0 + rect.height * y1,
            )
        pixmap = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), clip=rect, alpha=False)
        return pixmap.tobytes("jpeg")


# Подменяется в тестах.
def _render(document_code, storage_key, pdf_page, dpi, clip=None):
    return render_pdf_page(pdf_path(document_code, storage_key), pdf_page, dpi, clip)


def page_image(document_code, storage_key, pdf_page, dpi=PAGE_DPI):
    """JPEG страницы: из кэша R2 или рендер из PDF (и сохранение в кэш)."""
    key = f"{CACHE_PREFIX}/{document_code}/p{pdf_page:04d}-{dpi}.jpg"
    cached = r2_get(key)
    if cached:
        return cached
    data = _render(document_code, storage_key, pdf_page, dpi)
    r2_put(key, data)
    return data


# --- сетка и вырезки ---

def cell_box(cell, margin=0.06):
    """«B2» → область (x0, y0, x1, y1) в долях страницы, с небольшим запасом."""
    cell = (cell or "").strip().upper()
    if len(cell) != 2 or cell[0] not in GRID or cell[1] not in "123":
        raise ValueError(f"Неверная клетка: {cell}")
    column = GRID.index(cell[0])
    row = int(cell[1]) - 1
    x0, y0 = column / 3, row / 3
    return (
        max(0.0, x0 - margin), max(0.0, y0 - margin),
        min(1.0, x0 + 1 / 3 + margin), min(1.0, y0 + 1 / 3 + margin),
    )


def crop_image(document_code, storage_key, pdf_page, cell, dpi=CROP_DPI):
    key = f"{CACHE_PREFIX}/{document_code}/p{pdf_page:04d}-{cell.upper()}-{dpi}.jpg"
    cached = r2_get(key)
    if cached:
        return cached
    data = _render(document_code, storage_key, pdf_page, dpi, clip=cell_box(cell))
    r2_put(key, data)
    return data


# --- изображения для ИИ и Tesseract ---

def shrink_jpeg(data, max_side=MAX_SIDE, quality=85):
    """Уменьшает изображение до max_side по длинной стороне (JPEG)."""
    image = Image.open(io.BytesIO(data))
    image = image.convert("RGB")
    image.thumbnail((max_side, max_side))
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=quality)
    return output.getvalue()


def image_size(data):
    with Image.open(io.BytesIO(data)) as image:
        return image.size


def tesseract_text(data, language="eng"):
    """Бесплатный OCR вырезки через Tesseract. Пустая строка, если не вышло."""
    image = Image.open(io.BytesIO(data)).convert("L")
    # Мелкий шрифт читается лучше после увеличения.
    if max(image.size) < 2000:
        scale = 2000 / max(image.size)
        image = image.resize((int(image.width * scale), int(image.height * scale)))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    try:
        result = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", language or "eng", "--psm", "6"],
            input=buffer.getvalue(), capture_output=True, timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        print(f"tesseract failed: {type(error).__name__}")
        return ""
    return result.stdout.decode("utf-8", "ignore").strip()


def readable(text, minimum_letters=20):
    """Похоже ли на прочитанный текст, а не на мусор."""
    letters = sum(1 for ch in text if ch.isalpha())
    words = [w for w in text.split() if sum(1 for ch in w if ch.isalpha()) >= 3]
    return letters >= minimum_letters and len(words) >= 3
