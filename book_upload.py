
"""Admin-only Telegram PDF upload into Lucy's private R2 library."""

import asyncio
import logging
import os
import tempfile
import uuid
from pathlib import Path

import boto3
import fitz
from telegram import Update
from telegram.ext import ContextTypes

from library_db import get_connection
from library_importer import import_pdf_with_ocr, calculate_checksum


logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 20 * 1024 * 1024
_upload_lock = asyncio.Lock()


def find_duplicate(checksum):
    """Check whether this PDF is already registered."""

    with get_connection() as conn:
        return conn.execute(
            """
            SELECT document_code, title
            FROM library_documents
            WHERE checksum = ?
            LIMIT 1
            """,
            (checksum,),
        ).fetchone()


def upload_to_r2(local_path, storage_key):
    """Save the original PDF in private Cloudflare R2."""

    client = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )

    client.upload_file(
        local_path,
        os.environ["R2_BUCKET_NAME"],
        storage_key,
        ExtraArgs={
            "ContentType": "application/pdf"
        },
    )


async def receive_book_pdf(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Receive, store and import a PDF sent by the owner."""

    message = update.effective_message
    user = update.effective_user

    if message is None:
        return

    admin_id = os.environ.get(
        "LUCY_ADMIN_ID", ""
    ).strip()

    # Only the owner can upload books.
    if (
        user is None
        or not admin_id
        or str(user.id) != admin_id
    ):
        await message.reply_text(
            "🔒 Загрузка книг доступна только администратору."
        )
        return

    document = message.document

    if document is None:
        return

    filename = document.file_name or "book.pdf"

    # Accept PDF only.
    if Path(filename).suffix.lower() != ".pdf":
        await message.reply_text(
            "📚 Отправь книгу файлом в формате PDF."
        )
        return

    # Telegram Bot API download limit.
    if (
        document.file_size is None
        or document.file_size > MAX_FILE_BYTES
    ):
        await message.reply_text(
            "⚠️ Пока принимаю PDF размером до 20 МБ."
        )
        return

    # Avoid simultaneous imports.
    if _upload_lock.locked():
        await message.reply_text(
            "⏳ Другая книга ещё обрабатывается. "
            "Попробуй позже."
        )
        return

    async with _upload_lock:

        title = (
            Path(filename).stem.strip()[:200]
            or "Без названия"
        )

        await message.reply_text(
            f"📥 Получила «{title}». Проверяю PDF…"
        )

        try:
            with tempfile.TemporaryDirectory(
                prefix="lucy_book_"
            ) as temp_dir:

                local_path = str(
                    Path(temp_dir) / "book.pdf"
                )

                # Download from Telegram.
                telegram_file = await document.get_file()

                await telegram_file.download_to_drive(
                    custom_path=local_path
                )

                # Check the real PDF contents.
                with fitz.open(local_path) as pdf:
                    if (
                        not pdf.is_pdf
                        or pdf.page_count < 1
                    ):
                        raise ValueError(
                            "Некорректный PDF"
                        )

                # Detect duplicates by file hash.
                checksum = await asyncio.to_thread(
                    calculate_checksum,
                    local_path,
                )

                duplicate = await asyncio.to_thread(
                    find_duplicate,
                    checksum,
                )

                if duplicate:
                    await message.reply_text(
                        "📚 Эта книга уже есть "
                        "в библиотеке.\n\n"
                        f"Название: {duplicate[1]}\n"
                        f"Документ: {duplicate[0]}"
                    )
                    return

                # Upload original to R2.
                storage_key = (
                    "books/telegram/"
                    f"{uuid.uuid4().hex}.pdf"
                )

                await asyncio.to_thread(
                    upload_to_r2,
                    local_path,
                    storage_key,
                )

            await message.reply_text(
                "📖 Книга сохранена в R2.\n"
                "Начинаю распознавание страниц…"
            )

            # Reuse our existing importer.
            result = await asyncio.to_thread(
                import_pdf_with_ocr,
                storage_key,
                title,
                "eng",
            )

            await message.reply_text(
                "✅ КНИГА В БИБЛИОТЕКЕ\n\n"
                f"📚 {title}\n"
                f"Документ: {result['document_code']}\n"
                f"PDF-страниц: {result['total_pages']}\n"
                f"Обработано: {result['completed_pages']}\n"
                f"Статус: {result['status']}\n\n"
                "OCR сканов пока работает "
                "на английском языке."
            )

        except Exception:
            logger.exception(
                "PDF import failed"
            )

            await message.reply_text(
                "❌ Не удалось обработать PDF.\n"
                "Подробности ошибки — "
                "в журнале Railway.\n"
                "Существующие книги не затронуты."
            )
