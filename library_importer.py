import hashlib
import os
import re
import shutil
import tempfile
import threading
import fitz
import urllib.request
import boto3

from library_db import get_connection


# Языки OCR (коды Tesseract), которые установлены на сервере (railpack.json).
OCR_LANGUAGES = {
    "eng": "английский",
    "ita": "итальянский",
    "deu": "немецкий",
    "fra": "французский",
    "ces": "чешский",
    "pol": "польский",
    "rus": "русский",
}

# Одна книга распознаётся за раз, откуда бы её ни загрузили
# (Telegram, /bigbook, /reocr). Остальные ждут своей очереди.
_import_lock = threading.Lock()


def calculate_checksum(file_path):
    sha256 = hashlib.sha256()

    with open(file_path, "rb") as file:
        while True:
            chunk = file.read(1024 * 1024)

            if not chunk:
                break

            sha256.update(chunk)

    return sha256.hexdigest()


def register_document(
    title,
    storage_key,
    language=None,
    document_type="book",
    checksum=None,
):
    with get_connection() as conn:
        cursor = conn.cursor()

        if checksum:
            cursor.execute(
                """
                SELECT id, document_code
                FROM library_documents
                WHERE checksum = ?
                """,
                (checksum,),
            )

            existing = cursor.fetchone()

            if existing:
                return existing[0], existing[1], False

        cursor.execute(
            """
            INSERT INTO library_documents (
                document_type,
                title,
                storage_key,
                checksum,
                language
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                document_type,
                title,
                storage_key,
                checksum,
                language,
            ),
        )

        document_id = cursor.lastrowid
        document_code = f"DOC-{document_id:06d}"

        cursor.execute(
            """
            UPDATE library_documents
            SET document_code = ?
            WHERE id = ?
            """,
            (document_code, document_id),
        )

        cursor.execute(
            """
            INSERT INTO library_import_jobs (
                document_id,
                status
            )
            VALUES (?, 'pending')
            """,
            (document_id,),
        )

        return document_id, document_code, True
        
        
        
def save_page(
    document_id,
    pdf_page,
    original_text=None,
    cleaned_text=None,
    printed_page=None,
    russian_text=None,
    extraction_status="extracted",
):
    with get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO library_pages (
                document_id,
                pdf_page,
                printed_page,
                original_text,
                cleaned_text,
                russian_text,
                extraction_status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_id, pdf_page)
            DO UPDATE SET
                printed_page = COALESCE(
                    excluded.printed_page, library_pages.printed_page
                ),
                original_text = excluded.original_text,
                cleaned_text = excluded.cleaned_text,
                russian_text = COALESCE(
                    excluded.russian_text, library_pages.russian_text
                ),
                extraction_status = excluded.extraction_status
            """,
            (
                document_id,
                pdf_page,
                printed_page,
                original_text,
                cleaned_text,
                russian_text,
                extraction_status,
            ),
        )



def update_import_progress(
    document_id,
    last_completed_page,
    status="processing",
    error_message=None,
):
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE library_import_jobs
            SET
                status = ?,
                last_completed_page = ?,
                error_message = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE document_id = ?
            """,
            (
                status,
                last_completed_page,
                error_message,
                document_id,
            ),
        )
        
        
def get_import_progress(document_id):
    with get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT
                status,
                last_completed_page,
                error_message
            FROM library_import_jobs
            WHERE document_id = ?
            """,
            (document_id,),
        )

        row = cursor.fetchone()

        if not row:
            return "pending", 0, None

        return row[0], row[1], row[2]

        
def inspect_pdf(file_path):
    document = fitz.open(file_path)

    pages = []

    for page_number in range(document.page_count):
        page = document.load_page(page_number)
        text = page.get_text("text").strip()

        pages.append({
            "pdf_page": page_number + 1,
            "has_text": bool(text),
            "text_length": len(text),
        })

    result = {
        "page_count": document.page_count,
        "pages_with_text": sum(1 for page in pages if page["has_text"]),
        "pages_without_text": sum(1 for page in pages if not page["has_text"]),
        "pages": pages,
    }

    document.close()
    return result
    
def download_pdf(url, destination_path):
    urllib.request.urlretrieve(url, destination_path)
    return destination_path
def download_pdf_from_r2(storage_key, destination_path):
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )

    bucket_name = os.environ["R2_BUCKET_NAME"]

    s3.download_file(
        bucket_name,
        storage_key,
        destination_path,
    )

    return destination_path
def clean_ocr_text(text):
    if not text:
        return ""

    cleaned = text

    # Склеиваем слова, разорванные переносом:
    # "manu-\nfacture" -> "manufacture"
    cleaned = re.sub(
        r"([A-Za-z])-\s*\n\s*([a-z])",
        r"\1\2",
        cleaned,
    )

    # Обычные переносы строк внутри абзаца превращаем в пробелы.
    # Пустая строка остаётся границей абзаца.
    cleaned = re.sub(
        r"(?<!\n)\n(?!\n)",
        " ",
        cleaned,
    )

    # Убираем лишние пробелы и табы.
    cleaned = re.sub(
        r"[ \t]+",
        " ",
        cleaned,
    )

    # Приводим большие разрывы между абзацами к двум переносам.
    cleaned = re.sub(
        r"\n{3,}",
        "\n\n",
        cleaned,
    )

    return cleaned.strip()

def ocr_pdf_page(file_path, pdf_page, language="eng", dpi=300):
    """
    Распознаёт скан PDF с указанным языком.
    Поддерживает развороты из двух страниц.
    """

    document = fitz.open(file_path)

    try:
        page = document.load_page(pdf_page - 1)

        width = page.rect.width
        height = page.rect.height

        # Широкие развороты делим на две половины.
        # Обычную страницу распознаём целиком.
        if width > height * 1.15:
            middle = width / 2

            regions = [
                fitz.Rect(0, 0, middle, height),
                fitz.Rect(middle, 0, width, height),
            ]
        else:
            regions = [page.rect]

        texts = []

        for region in regions:
            pix = page.get_pixmap(
                matrix=fitz.Matrix(dpi / 72, dpi / 72),
                clip=region,
                alpha=False,
            )

            ocr_pdf = fitz.open(
                "pdf",
                pix.pdfocr_tobytes(
                    language=language,
                ),
            )

            try:
                text = ocr_pdf[0].get_text(
                    "text",
                    sort=True,
                ).strip()

                if text:
                    texts.append(text)

            finally:
                ocr_pdf.close()

        return "\n\n".join(texts).strip()

    finally:
        document.close()


        
def get_page_text(document_id, pdf_page):
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT original_text
            FROM library_pages
            WHERE document_id = ? AND pdf_page = ?
            """,
            (document_id, pdf_page),
        ).fetchone()
    return (row[0] or "") if row else ""


def _ocr_document(
    document_id,
    local_path,
    language,
    start_page,
    total_pages,
    force_ocr=False,
):
    """Распознаёт страницы start_page..total_pages и сохраняет их.

    Уже распознанный текст не затирается пустым: если новое
    распознавание ничего не дало, старый текст страницы остаётся.
    """
    for pdf_page in range(start_page, total_pages + 1):
        text = ""

        if not force_ocr:
            document = fitz.open(local_path)
            try:
                page = document.load_page(pdf_page - 1)
                text = page.get_text("text").strip()
            finally:
                document.close()

        if not text:
            text = ocr_pdf_page(
                local_path,
                pdf_page,
                language=language,
            )

        if text or not get_page_text(document_id, pdf_page):
            save_page(
                document_id=document_id,
                pdf_page=pdf_page,
                original_text=text,
                cleaned_text=clean_ocr_text(text),
                extraction_status="extracted" if text else "empty",
            )

        update_import_progress(
            document_id,
            pdf_page,
            status="processing",
            error_message=None,
        )


def _run_import(document_id, document_code, local_path, language, restart, force_ocr):
    if restart:
        update_import_progress(
            document_id, 0, status="pending", error_message=None
        )

    _, last_completed_page, _ = get_import_progress(document_id)

    total_pages = inspect_pdf(local_path)["page_count"]
    start_page = last_completed_page + 1

    try:
        if start_page <= total_pages:
            _ocr_document(
                document_id,
                local_path,
                language,
                start_page,
                total_pages,
                force_ocr=force_ocr,
            )
    except Exception as error:
        _, saved_page, _ = get_import_progress(document_id)
        update_import_progress(
            document_id,
            max(start_page - 1, saved_page, 0),
            status="error",
            error_message=f"{type(error).__name__}: {error}"[:500],
        )
        raise

    update_import_progress(
        document_id,
        total_pages,
        status="completed",
        error_message=None,
    )

    return {
        "document_id": document_id,
        "document_code": document_code,
        "total_pages": total_pages,
        "completed_pages": total_pages,
        "status": "completed",
    }


def import_pdf_with_ocr(
    storage_key,
    title,
    language="eng",
):
    """Скачивает PDF из R2, регистрирует книгу и распознаёт её.

    Повторная загрузка того же файла не создаёт дубль (сверка по
    контрольной сумме): незаконченный импорт продолжается с места
    остановки, законченный — сразу возвращает результат.
    """
    with _import_lock:
        temp_dir = tempfile.mkdtemp(prefix="lucy_import_")
        try:
            local_path = os.path.join(temp_dir, "book.pdf")
            download_pdf_from_r2(storage_key, local_path)

            # SHA-256 и размер — от байтов, которые реально лежат в R2
            # под ключом ЭТОЙ загрузки (а не от имени файла или числа страниц).
            size = os.path.getsize(local_path)
            checksum = calculate_checksum(local_path)

            document_id, document_code, created = register_document(
                title=title,
                storage_key=storage_key,
                language=language,
                checksum=checksum,
            )
            before = None if created else stored_document_info(document_id)
            already_done = bool(before) and before["status"] == "completed"

            result = _run_import(
                document_id,
                document_code,
                local_path,
                language,
                restart=False,
                force_ocr=False,
            )
            result["created"] = created
            # Готовый документ-дубль не трогаем: ни метаданные, ни индекс.
            if not already_done:
                _detect_metadata_safely(document_id)
                _visual_index_safely(document_id, local_path)

            # В сообщение — то, что реально сохранено в каталоге (в том числе
            # название, исправленное вручную через /settitle).
            info = stored_document_info(document_id)
            result["meta_title"] = info["meta_title"]
            result["meta_author"] = info["meta_author"]
            result["meta_manual"] = info["meta_source"] == "manual"
            result["upload"] = {
                "size": size,
                "sha256": checksum,
                "storage_key": storage_key,
            }
            if not created:
                result["duplicate"] = {
                    "document_code": document_code,
                    "file_title": before["title"],
                    "created_at": before["created_at"],
                    "pages": before["last_completed_page"],
                    "status": before["status"],
                    "storage_key": before["storage_key"],
                }
            print(
                "book import: "
                f"key={storage_key} size={size} sha256={checksum} "
                f"document={document_code} created={created}"
            )
            return result
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


def stored_document_info(document_id):
    """Что сохранено в каталоге о документе (для сообщений и диагностики)."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT d.document_code, d.title, d.meta_title, d.meta_author,
                   d.meta_source, d.created_at, d.checksum, d.storage_key,
                   (SELECT status FROM library_import_jobs j
                     WHERE j.document_id = d.id ORDER BY j.id DESC LIMIT 1),
                   (SELECT last_completed_page FROM library_import_jobs j
                     WHERE j.document_id = d.id ORDER BY j.id DESC LIMIT 1)
            FROM library_documents d WHERE d.id = ?
            """,
            (document_id,),
        ).fetchone()
    if row is None:
        return None
    keys = ("document_code", "title", "meta_title", "meta_author", "meta_source",
            "created_at", "checksum", "storage_key", "status", "last_completed_page")
    return dict(zip(keys, row))


def format_size(size):
    return f"{size:,} байт ({size / 1024 / 1024:.1f} МБ)".replace(",", " ")


def diagnostic_lines(result, file_name=None, session_id=None):
    """Безопасная диагностика загрузки: без ссылок, токенов и ключей доступа."""
    upload = result.get("upload") or {}
    lines = ["", "🔎 ПРОВЕРКА ФАЙЛА"]
    if file_name:
        lines.append(f"Выбранный файл: {file_name}")
    if upload.get("size") is not None:
        lines.append(f"Получено сервером: {format_size(upload['size'])}")
    if upload.get("sha256"):
        lines.append(f"SHA-256: {upload['sha256']}")
    if session_id:
        lines.append(f"Сессия загрузки: {session_id[:8]}")
    duplicate = result.get("duplicate")
    if duplicate:
        lines += [
            f"Дубликат: {duplicate['document_code']} — SHA-256 совпал полностью, "
            "то есть файл байт в байт тот же, что уже загружен.",
            f"Исходное имя файла {duplicate['document_code']}: «{duplicate['file_title']}»",
            f"Добавлен: {duplicate['created_at'] or 'неизвестно'} · распознано страниц: "
            f"{duplicate['pages'] if duplicate['pages'] is not None else '?'}",
        ]
    return "\n".join(lines) + "\n"


def metadata_lines(result):
    """Строки для сообщения о готовой книге: название и автор."""
    title = result.get("meta_title") or "Название не определено"
    lines = f"Название: {title}\n"
    if result.get("meta_author"):
        lines += f"Автор: {result['meta_author']}\n"
    if not result.get("meta_title") and not result.get("meta_manual"):
        lines += (
            f"Задать вручную: /settitle {result['document_code']} Название | Автор\n"
        )
    return lines


def _visual_index_safely(document_id, pdf_path):
    """После OCR строит бесплатный индекс страниц для визуальной атрибуции.
    PDF уже скачан для импорта — повторно ничего не загружается."""
    try:
        import library_db
        import visual_index

        visual_index.init_tables(library_db.DB_PATH)
        visual_index.index_document(library_db.DB_PATH, document_id, pdf_path)
    except Exception as error:
        print(f"visual index failed: {type(error).__name__}: {error}")


def _detect_metadata_safely(document_id):
    """После импорта определяет название и автора по сохранённому OCR.
    Ошибка здесь не должна ломать импорт книги."""
    try:
        import library_catalog

        detected, saved, manual = library_catalog.refresh_document_metadata(document_id)
        return {
            "meta_title": detected["title"],
            "meta_author": detected["author"],
            "meta_manual": manual,
        }
    except Exception as error:
        print(f"metadata detection failed: {type(error).__name__}: {error}")
        return {"meta_title": None, "meta_author": None, "meta_manual": False}


def get_document(document_code):
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT id, document_code, title, storage_key, language
            FROM library_documents
            WHERE document_code = ?
            """,
            (document_code,),
        ).fetchone()


def reocr_document(document_code, language, force_ocr=False):
    """Повторно распознаёт уже загруженную книгу из R2 без новой загрузки.

    Страницы перезаписываются по мере распознавания, ничего не удаляется.
    force_ocr=True распознаёт даже страницы, где в PDF уже есть
    текстовый слой (например, плохое OCR сканера).
    """
    if language not in OCR_LANGUAGES:
        raise ValueError(f"Неизвестный язык OCR: {language}")

    with _import_lock:
        document = get_document(document_code)
        if document is None:
            raise LookupError(f"Документ {document_code} не найден")

        document_id, document_code, title, storage_key, _ = document
        if not storage_key or not storage_key.startswith("books/"):
            raise LookupError(f"У {document_code} нет PDF в хранилище")

        with get_connection() as conn:
            conn.execute(
                "UPDATE library_documents SET language = ? WHERE id = ?",
                (language, document_id),
            )
            exists = conn.execute(
                "SELECT 1 FROM library_import_jobs WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO library_import_jobs (document_id, status) "
                    "VALUES (?, 'pending')",
                    (document_id,),
                )

        temp_dir = tempfile.mkdtemp(prefix="lucy_reocr_")
        try:
            local_path = os.path.join(temp_dir, "book.pdf")
            download_pdf_from_r2(storage_key, local_path)
            result = _run_import(
                document_id,
                document_code,
                local_path,
                language,
                restart=True,
                force_ocr=force_ocr,
            )
            result["title"] = title
            result.update(_detect_metadata_safely(document_id))
            _visual_index_safely(document_id, local_path)
            return result
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


def list_documents():
    """Каталог библиотеки: книги, язык, статус импорта и страницы."""
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT
                d.document_code,
                d.title,
                d.language,
                COALESCE(j.status, '—'),
                COALESCE(j.last_completed_page, 0),
                (SELECT COUNT(*) FROM library_pages p
                 WHERE p.document_id = d.id),
                (SELECT COUNT(*) FROM library_pages p
                 WHERE p.document_id = d.id
                   AND COALESCE(p.original_text, '') = ''),
                d.storage_key
            FROM library_documents d
            LEFT JOIN library_import_jobs j ON j.document_id = d.id
            ORDER BY d.id
            """
        ).fetchall()
