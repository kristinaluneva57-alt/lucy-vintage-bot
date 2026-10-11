
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
from library_importer import (
    OCR_LANGUAGES,
    diagnostic_lines,
    metadata_lines,
    calculate_checksum,
    import_pdf_with_ocr,
)


logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 20 * 1024 * 1024
_upload_lock = asyncio.Lock()


# Подпись к PDF → код языка OCR. Можно писать код или название языка.
LANGUAGE_ALIASES = {
    "eng": "eng", "en": "eng", "english": "eng", "английский": "eng", "англ": "eng",
    "ita": "ita", "it": "ita", "italian": "ita", "итальянский": "ita", "итал": "ita",
    "deu": "deu", "de": "deu", "ger": "deu", "german": "deu", "немецкий": "deu", "нем": "deu",
    "fra": "fra", "fr": "fra", "french": "fra", "французский": "fra", "франц": "fra",
    "ces": "ces", "cs": "ces", "cze": "ces", "czech": "ces", "чешский": "ces", "чеш": "ces",
    "pol": "pol", "pl": "pol", "polish": "pol", "польский": "pol", "польск": "pol",
    "rus": "rus", "ru": "rus", "russian": "rus", "русский": "rus", "рус": "rus",
}


def language_from_caption(caption):
    """Возвращает (код языка, распознан_ли). По умолчанию английский."""
    for word in (caption or "").lower().replace(",", " ").split():
        word = word.strip(".:;!?#")
        if word in LANGUAGE_ALIASES:
            return LANGUAGE_ALIASES[word], True
    return "eng", False


def find_duplicate(checksum):
    """Check whether this PDF is already registered."""

    with get_connection() as conn:
        return conn.execute(
            """
            SELECT document_code, COALESCE(meta_title, title), meta_author
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

    language, language_given = language_from_caption(message.caption)

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
                    author = f"Автор: {duplicate[2]}\n" if duplicate[2] else ""
                    await message.reply_text(
                        "📚 Этот файл уже есть "
                        "в библиотеке (совпадает SHA-256 — "
                        "файл байт в байт тот же).\n\n"
                        f"Документ: {duplicate[0]}\n"
                        f"Название: {duplicate[1]}\n"
                        + author
                        + f"Файл: {title}\n"
                        f"SHA-256: {checksum}"
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

            language_note = (
                "" if language_given else
                "\n\nЯзык не указан — распознаю как английский. "
                "Для другого языка пришли PDF с подписью, например: ita, "
                "немецкий, rus."
            )
            await message.reply_text(
                "📖 Книга сохранена в R2.\n"
                f"🔤 Язык распознавания: {OCR_LANGUAGES[language]}.\n"
                "Распознаю страницы в фоне — Люся при этом продолжает "
                "отвечать всем. Напишу, когда закончу."
                + language_note
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
            return

    # Распознавание — в фоне, чтобы бот не «замирал» для всех на время OCR.
    context.application.create_task(
        _import_and_report(message, storage_key, title, language)
    )


async def _import_and_report(message, storage_key, title, language):
    try:
        result = await asyncio.to_thread(
            import_pdf_with_ocr,
            storage_key,
            title,
            language,
        )
        await message.reply_text(
            "✅ КНИГА В БИБЛИОТЕКЕ\n\n"
            f"📚 Файл: {title}\n"
            f"Документ: {result['document_code']}\n"
            + metadata_lines(result)
            + f"Язык OCR: {OCR_LANGUAGES.get(language, language)}\n"
            f"PDF-страниц: {result['total_pages']}\n"
            f"Обработано: {result['completed_pages']}\n"
            f"Статус: {result['status']}\n"
            + diagnostic_lines(result, file_name=f"{title}.pdf")
        )
    except Exception:
        logger.exception("PDF import failed")
        await message.reply_text(
            f"❌ Не удалось распознать «{title}».\n"
            "PDF уже сохранён в R2 — можно продолжить командой "
            "/reocr, когда исправим причину. Подробности — в журнале "
            "Railway.\nСуществующие книги не затронуты."
        )
