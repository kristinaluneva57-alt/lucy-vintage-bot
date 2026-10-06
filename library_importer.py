import hashlib
import os
import fitz

from library_db import get_connection


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
                russian_text,
                extraction_status
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_id, pdf_page)
            DO UPDATE SET
                printed_page = excluded.printed_page,
                original_text = excluded.original_text,
                russian_text = excluded.russian_text,
                extraction_status = excluded.extraction_status
            """,
            (
                document_id,
                pdf_page,
                printed_page,
                original_text,
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