import os
import asyncio
import sqlite3
import time

import library_search
from functools import wraps
from book_upload import receive_book_pdf, _upload_lock
from big_book_upload import bigbook_command, start_upload_server

from library_db import init_library_db
from knowledge_bridge import (
    init_knowledge_bridge,
    save_library_fact,
    get_library_fact,
)
from fact_extractor import extract_page_facts
from ai_fact_extractor import extract_ai_facts, save_ai_facts
import lucy_ai
from library_importer import register_document, save_page, update_import_progress, get_import_progress, download_pdf_from_r2, inspect_pdf, import_pdf_with_ocr

from library_importer import OCR_LANGUAGES, reocr_document, list_documents

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
DB_PATH = "/data/lucy_vintage.db"

BTN_BOX = "🗃️ Моя шкатулка"
BTN_LEARN = "🎓 Учиться"
BTN_SEARCH = "📚 Найти информацию"
BTN_NO_MARK = "🚫 Клейма нет"

MAIN_MENU = [
    [BTN_BOX],
    [BTN_LEARN, BTN_SEARCH],
]

# Кнопки внутри шкатулки (под сообщением).
BOX_ADD_TEXT = "➕ Добавить украшение"
BOX_LIST_TEXT = "💎 Мои украшения"
BOX_PATH_ADD = f"«{BTN_BOX}» → «{BOX_ADD_TEXT}»"
BOX_PATH_LIST = f"«{BTN_BOX}» → «{BOX_LIST_TEXT}»"

# Кнопки старого меню. Из главного меню скрыты, но у кого-то
# в чате ещё может висеть старая клавиатура — обработчики работают.
LEGACY_BUTTONS = [
    "🔎 Определить украшение",
    "🔎 Добавить украшение",
    "🗃 Моя шкатулка",
    "🆘 Найди мне это",
    "🧭 Разделы школы",
    "💬 Vintage Club",
]

ADD_ITEM_BUTTONS = {"🔎 Определить украшение", "🔎 Добавить украшение"}
BOX_BUTTONS = {BTN_BOX, "🗃 Моя шкатулка"}

MENU_BUTTONS = set(sum(MAIN_MENU, [])) | set(LEGACY_BUTTONS)

ANKETA_KEYS = [
    "front_photo",
    "back_photo",
    "mark_photo",
    "mark_text",
    "details_photo",
    "measurements",
    "weight",
    "provenance",
    "seller_story",
    "item_id",
    "last_media_group",
    "album_notice",
    "edit_code",
    "edit_field",
    "edit_kind",
    "edit_started",
    "edit_photo_count",
]


def main_keyboard():
    return ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True)


def reset_anketa(context):
    """Полностью выходит из анкеты и забывает её черновик."""
    context.user_data["mode"] = None
    context.user_data["step"] = None
    for key in ANKETA_KEYS:
        context.user_data.pop(key, None)


def anketa_in_progress(context):
    return (
        context.user_data.get("mode") == "identify"
        and context.user_data.get("step") is not None
    )

def init_db():
    
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_code TEXT UNIQUE,
            measurements TEXT,
            weight TEXT,
            provenance TEXT,
            seller_story TEXT,
            attribution_status TEXT DEFAULT 'не определено'
        )
        """
    )
    columns = [
        ("deleted_at", "TEXT"),
        ("front_photo", "TEXT"),
        ("back_photo", "TEXT"),
        ("mark_photo", "TEXT"),
        ("details_photo", "TEXT"),
        ("owner_user_id", "INTEGER"),
        ("mark_text", "TEXT"),
        ("notes", "TEXT"),
        ("updated_at", "TEXT"),
    ]

    existing_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(items)")
    }

    for column_name, column_type in columns:
        if column_name not in existing_columns:
            conn.execute(
                f"ALTER TABLE items ADD COLUMN {column_name} {column_type}"
            )
    # Дополнительные фото к карточке (добавляются при редактировании).
    # Основные фото анкеты остаются в items и не перезаписываются.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS item_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_code TEXT NOT NULL,
            kind TEXT NOT NULL,
            file_id TEXT NOT NULL,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_code TEXT UNIQUE,
            title TEXT NOT NULL,
            author TEXT,
            publication_year INTEGER,
            publisher TEXT,
            source_type TEXT DEFAULT 'book',
            review_status TEXT DEFAULT 'needs_review'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            fact TEXT NOT NULL,
            status TEXT DEFAULT 'extracted',
            notes TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            knowledge_id INTEGER NOT NULL,
            book_id INTEGER NOT NULL,
            page_number INTEGER,
            source_note TEXT,
            FOREIGN KEY (knowledge_id) REFERENCES knowledge(id),
            FOREIGN KEY (book_id) REFERENCES books(id)
        )
        """
    )
    
    columns = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(knowledge_sources)"
        ).fetchall()
    ]

    if "library_page_id" not in columns:
        conn.execute(
            "ALTER TABLE knowledge_sources "
            "ADD COLUMN library_page_id INTEGER "
            "REFERENCES library_pages(id)"
        )

    conn.commit()
    conn.close()
def save_book(title, author, publication_year, publisher):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO books (
            title,
            author,
            publication_year,
            publisher
        )
        VALUES (?, ?, ?, ?)
        """,
        (title, author, publication_year, publisher)
    )

    book_id = cursor.lastrowid
    book_code = f"BOOK-{book_id:06d}"

    cursor.execute(
        "UPDATE books SET book_code = ? WHERE id = ?",
        (book_code, book_id)
    )

    conn.commit()
    conn.close()

    return book_code
def save_knowledge(topic, fact, book_code, page_number, status="extracted", notes=None):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute(
        "SELECT id FROM books WHERE book_code = ?",
        (book_code,)
        )
        book = cursor.fetchone()

        if not book:
            conn.close()
            return None
            
        cursor.execute(
        """
        SELECT k.id
        FROM knowledge k
        JOIN knowledge_sources ks ON ks.knowledge_id = k.id
        WHERE k.fact = ? AND ks.book_id = ? AND ks.page_number = ?
        """,
        (fact, book[0], page_number)
        )

        existing_knowledge = cursor.fetchone()

        if existing_knowledge:
            conn.close()
            return existing_knowledge[0]
            
        cursor.execute(
        """
        INSERT INTO knowledge (
            topic,
            fact,
            status,
            notes
        )
        VALUES (?, ?, ?, ?)
        """,
        (topic, fact, status, notes)
        )

        knowledge_id = cursor.lastrowid

        cursor.execute(
        """
        INSERT INTO knowledge_sources (
            knowledge_id,
            book_id,
            page_number
        )
        VALUES (?, ?, ?)
        """,
        (knowledge_id, book[0], page_number)
        )

        conn.commit()
        conn.close()

        return knowledge_id
def seed_books():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id FROM books WHERE title = ? AND author = ?",
        ("Jet Jewellery and Ornaments", "Helen Muller")
    )

    existing_book = cursor.fetchone()
    conn.close()

    if not existing_book:
        save_book(
            "Jet Jewellery and Ornaments",
            "Helen Muller",
            1980,
            "Shire Publications Ltd"
        )
def seed_knowledge():
        save_knowledge(
        "Материалы / Jet",
        "Jet is a type of brown coal, a fossilised wood of an ancient tree similar to the present-day Araucaria or monkey puzzle tree.",
        "BOOK-000001",
        3,
        status="extracted",
        notes="Introduction. Требует проверки перед переводом в verified."
    )
def save_item(
    measurements,
    weight,
    provenance,
    seller_story,
    front_photo,
    back_photo,
    mark_photo,
    details_photo,
    owner_user_id,
    mark_text=None
):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
    """
    INSERT INTO items (
        measurements,
        weight,
        provenance,
        seller_story,
        front_photo,
        back_photo,
        mark_photo,
        details_photo,
        owner_user_id,
        mark_text
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    (
        measurements,
        weight,
        provenance,
        seller_story,
        front_photo,
        back_photo,
        mark_photo,
        details_photo,
        owner_user_id,
        mark_text
    )
    )

    item_id = cursor.lastrowid
    item_code = f"OMG-{item_id:06d}"

    cursor.execute(
        "UPDATE items SET item_code = ? WHERE id = ?",
        (item_code, item_id)
    )

    conn.commit()
    conn.close()

    return item_code
def is_admin(user_id):
    admin_id = os.environ.get("LUCY_ADMIN_ID", "").strip()
    return bool(admin_id) and str(user_id) == admin_id


def admin_only(handler):
    """Пускает в служебную команду только администратора (LUCY_ADMIN_ID)."""

    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user

        if user is None or not is_admin(user.id):
            if update.effective_message:
                await update.effective_message.reply_text(
                    "🔒 Команда доступна только администратору."
                )
            return

        return await handler(update, context)

    return wrapper


# Старые находки без владельца видит только администратор.
# Удалённые пользователем карточки (deleted_at) не показываются,
# но остаются в базе — администратор может их восстановить.
OWNER_FILTER = (
    "(owner_user_id = ? OR (owner_user_id IS NULL AND ?))"
    " AND deleted_at IS NULL"
)


def delete_item(item_code, user_id):
    """Прячет карточку владелицы. Чужие и старые карточки без владельца
    удалить нельзя. Возвращает True, если карточка удалена."""
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.execute(
            """
            UPDATE items
            SET deleted_at = datetime('now')
            WHERE item_code = ?
              AND owner_user_id = ?
              AND deleted_at IS NULL
            """,
            (item_code, user_id),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


# Поля карточки, которые владелица может менять:
# поле → (название, подсказка, максимальная длина).
EDITABLE_FIELDS = {
    "notes": (
        "📝 Описание",
        "Опиши украшение своими словами: что это, как выглядит, "
        "что уже удалось выяснить.",
        3000,
    ),
    "mark_text": (
        "🔖 Надпись на клейме",
        "Перепиши, что написано на клейме: буквы, цифры, значки. "
        "Например: TRIFARI © PAT. PEND.",
        200,
    ),
    "measurements": (
        "📏 Размеры",
        "Напиши размеры, например: 52 × 38 мм.",
        200,
    ),
    "weight": (
        "⚖️ Вес",
        "Напиши вес, например: 24.6 г.",
        100,
    ),
    "provenance": (
        "📍 Происхождение",
        "Где ты нашла или купила украшение? Город, страна, рынок, магазин.",
        500,
    ),
    "seller_story": (
        "💬 История продавца",
        "Что рассказывал продавец об этом украшении?",
        1500,
    ),
}

PHOTO_KINDS = {
    "extra": "📸 Добавить фото",
    "mark": "🔖 Фото клейма",
}

EDIT_TIMEOUT_SECONDS = 30 * 60


def owns_item(item_code, user_id):
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            """
            SELECT 1 FROM items
            WHERE item_code = ? AND owner_user_id = ? AND deleted_at IS NULL
            """,
            (item_code, user_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_item_field(item_code, user_id, field):
    if field not in EDITABLE_FIELDS:
        raise ValueError(f"Поле нельзя редактировать: {field}")
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            f"""
            SELECT {field} FROM items
            WHERE item_code = ? AND owner_user_id = ? AND deleted_at IS NULL
            """,
            (item_code, user_id),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def update_item_field(item_code, user_id, field, value, append=False):
    """Меняет одно поле своей карточки. Остальные поля не трогаются.
    append=True дописывает текст к существующему. Возвращает True,
    если карточка найдена и обновлена."""
    if field not in EDITABLE_FIELDS:
        raise ValueError(f"Поле нельзя редактировать: {field}")
    if append:
        new_value = (
            f"CASE WHEN COALESCE({field}, '') = '' THEN ? "
            f"ELSE {field} || char(10) || char(10) || ? END"
        )
        params = (value, value, item_code, user_id)
    else:
        new_value = "?"
        params = (value, item_code, user_id)
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.execute(
            f"""
            UPDATE items
            SET {field} = {new_value}, updated_at = datetime('now')
            WHERE item_code = ? AND owner_user_id = ? AND deleted_at IS NULL
            """,
            params,
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def add_item_photo(item_code, user_id, kind, file_id):
    """Добавляет фото к своей карточке. Старые фото не заменяются."""
    if kind not in PHOTO_KINDS:
        raise ValueError(f"Неизвестный тип фото: {kind}")
    if not owns_item(item_code, user_id):
        return False
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO item_photos (item_code, kind, file_id) VALUES (?, ?, ?)",
            (item_code, kind, file_id),
        )
        conn.execute(
            "UPDATE items SET updated_at = datetime('now') WHERE item_code = ?",
            (item_code,),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def get_item_photos(item_code):
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(
            """
            SELECT kind, file_id FROM item_photos
            WHERE item_code = ?
            ORDER BY CASE kind WHEN 'mark' THEN 0 ELSE 1 END, id
            """,
            (item_code,),
        ).fetchall()
    finally:
        conn.close()


def get_items(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        f"""
        SELECT item_code, measurements, weight
        FROM items
        WHERE {OWNER_FILTER}
        ORDER BY id DESC
        """,
        (user_id, is_admin(user_id))
    )

    items = cursor.fetchall()
    conn.close()

    return items
    
def get_item(item_code, user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        f"""
        SELECT item_code, measurements, weight, provenance,
               seller_story, attribution_status, front_photo, back_photo, mark_photo, details_photo,
               mark_text, notes
        FROM items
        WHERE item_code = ?
          AND {OWNER_FILTER}
        """,
        (item_code, user_id, is_admin(user_id))
    )

    item = cursor.fetchone()
    conn.close()

    return item
def mark_text_label(mark_text, mark_photo=None):
    if mark_text:
        return mark_text
    if mark_photo:
        return "не записана"
    return "клейма нет"


async def open_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    item_code = query.data.split(":", 1)[1]
    await show_item_card(query.message, item_code, update.effective_user.id)


async def show_item_card(message, item_code, user_id):
    item = get_item(item_code, user_id)

    if not item:
        await message.reply_text(
            "Не смогла найти эту карточку в шкатулке. 🔎"
        )
        return

    (
        item_code,
        measurements,
        weight,
        provenance,
        seller_story,
        attribution_status,
        front_photo,
        back_photo,
        mark_photo,
        details_photo,
        mark_text,
        notes,
    ) = item

    if front_photo:
        await message.reply_photo(photo=front_photo)

    if back_photo:
        await message.reply_photo(photo=back_photo)

    extra_count = len(get_item_photos(item_code))
    notes_block = f"─── ОПИСАНИЕ ───\n{notes}\n\n" if notes else ""
    extra_line = (
        f"📸 Дополнительных фото: {extra_count}\n" if extra_count else ""
    )

    card = (
        "✦ OH MY GOD VINTAGE ARCHIVE ✦\n\n"
        f"💎 {item_code}\n"
        "Карточка находки\n\n"
        "─── ПАСПОРТ ПРЕДМЕТА ───\n"
        f"📏 Размер: {measurements}\n"
        f"⚖️ Вес: {weight}\n"
        f"📍 Происхождение: {provenance}\n"
        f"🔖 Надпись на клейме: {mark_text_label(mark_text, mark_photo)}\n"
        f"{extra_line}\n"
        f"{notes_block}"
        "─── АТРИБУЦИЯ ───\n"
        f"◇ Статус: {attribution_status}\n\n"
        "─── ИСТОРИЯ ПРОДАВЦА ───\n"
        f"{seller_story}"
    )
    details_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🔬 Клеймо и детали",
            callback_data=f"details:{item_code}"
        )],
        [InlineKeyboardButton(
            "✏️ Редактировать",
            callback_data=f"edit:{item_code}"
        )],
        [InlineKeyboardButton(
            "🗑 Удалить карточку",
            callback_data=f"del:{item_code}"
        )],
    ])

    await message.reply_text(
        card[:4000],
        reply_markup=details_keyboard
    )


async def delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, item_code = query.data.split(":", 1)
    user_id = update.effective_user.id

    if action == "del":
        if not get_item(item_code, user_id):
            await query.message.reply_text("Не смогла найти эту карточку. 🔎")
            return
        await query.message.reply_text(
            f"🗑 Удалить карточку {item_code} из шкатулки?\n\n"
            "Фото и все записи этой находки пропадут из шкатулки.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Да, удалить", callback_data=f"delok:{item_code}")],
                [InlineKeyboardButton("Нет, оставить", callback_data=f"delno:{item_code}")],
            ]),
        )
        return

    if action == "delno":
        await query.message.reply_text(
            f"Хорошо, {item_code} остаётся в шкатулке. 💎",
            reply_markup=main_keyboard(),
        )
        return

    if action == "delok":
        try:
            deleted = delete_item(item_code, user_id)
        except Exception as error:
            print(f"delete_item failed: {type(error).__name__}: {error}")
            await query.message.reply_text(
                "😔 Не получилось удалить карточку. Попробуй чуть позже.",
                reply_markup=main_keyboard(),
            )
            return
        if deleted:
            text = f"🗑 Карточка {item_code} удалена из шкатулки."
        else:
            text = "Эту карточку удалить нельзя — она уже удалена или не твоя. 🔎"
        await query.message.reply_text(text, reply_markup=main_keyboard())


def edit_menu_keyboard(item_code):
    def field_button(field):
        return InlineKeyboardButton(
            EDITABLE_FIELDS[field][0], callback_data=f"ef:{item_code}:{field}"
        )

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(PHOTO_KINDS["extra"], callback_data=f"ep:{item_code}:extra"),
            InlineKeyboardButton(PHOTO_KINDS["mark"], callback_data=f"ep:{item_code}:mark"),
        ],
        [field_button("notes"), field_button("mark_text")],
        [field_button("measurements"), field_button("weight")],
        [field_button("provenance"), field_button("seller_story")],
        [InlineKeyboardButton("✅ Готово — показать карточку", callback_data=f"edone:{item_code}")],
    ])


async def send_edit_menu(message, item_code, intro=None):
    await message.reply_text(
        (intro + "\n\n" if intro else "")
        + f"✏️ РЕДАКТИРОВАНИЕ {item_code}\n\n"
        "Что меняем? Всё остальное в карточке останется как есть.",
        reply_markup=edit_menu_keyboard(item_code),
    )


def clear_edit_state(context):
    for key in ("edit_code", "edit_field", "edit_kind", "edit_started", "edit_photo_count"):
        context.user_data.pop(key, None)
    if context.user_data.get("mode") in ("edit", "edit_photo"):
        context.user_data["mode"] = None


def edit_state_fresh(context):
    started = context.user_data.get("edit_started")
    return started is not None and time.time() - started < EDIT_TIMEOUT_SECONDS


async def edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    action, item_code = parts[0], parts[1]
    arg = parts[2] if len(parts) > 2 else None
    user_id = update.effective_user.id

    if not owns_item(item_code, user_id):
        clear_edit_state(context)
        await query.message.reply_text(
            "Эту карточку редактировать нельзя — она удалена или не твоя. 🔎",
            reply_markup=main_keyboard(),
        )
        return

    if action == "edit":
        reset_anketa(context)
        await send_edit_menu(query.message, item_code)
        return

    if action == "edone":
        reset_anketa(context)
        await show_item_card(query.message, item_code, user_id)
        return

    if action == "ef":
        field = arg[:-4] if arg and arg.endswith("_add") else arg
        if field not in EDITABLE_FIELDS:
            return
        append = arg.endswith("_add")
        reset_anketa(context)
        context.user_data.update({
            "mode": "edit",
            "edit_code": item_code,
            "edit_field": field,
            "edit_kind": "append" if append else "replace",
            "edit_started": time.time(),
        })
        label, hint, _ = EDITABLE_FIELDS[field]
        current = get_item_field(item_code, user_id, field)
        current_text = current if current else "пока не указано"
        how = (
            "Пришли текст — я допишу его в конец описания."
            if append else
            "Пришли новый текст — он заменит текущий."
        )
        keyboard = None
        if field == "notes" and not append and current:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "➕ Лучше дописать к описанию",
                    callback_data=f"ef:{item_code}:notes_add",
                )
            ]])
        await query.message.reply_text(
            f"{label} — {item_code}\n\n"
            f"Сейчас:\n{current_text}\n\n"
            f"{hint}\n{how}\n\n"
            "Передумала — напиши «отмена».",
            reply_markup=keyboard,
        )
        return

    if action == "ep" and arg in PHOTO_KINDS:
        reset_anketa(context)
        context.user_data.update({
            "mode": "edit_photo",
            "edit_code": item_code,
            "edit_field": arg,
            "edit_started": time.time(),
            "edit_photo_count": 0,
        })
        what = (
            "фото клейма крупным планом" if arg == "mark"
            else "новые фото украшения (можно сразу несколько)"
        )
        await query.message.reply_text(
            f"📸 Пришли {what}.\n\n"
            "Старые фото карточки останутся на месте — новые добавятся к ним.\n"
            "Когда закончишь, нажми «✅ Готово».",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Готово", callback_data=f"edit:{item_code}")
            ]]),
        )
        return


async def handle_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Текст, присланный в режиме редактирования поля."""
    data = context.user_data
    item_code = data.get("edit_code")
    field = data.get("edit_field")
    user_id = update.effective_user.id

    if data.get("mode") == "edit_photo":
        await update.message.reply_text(
            "📸 Сейчас жду фото. Когда закончишь — нажми «✅ Готово» "
            "или напиши «отмена».",
        )
        return

    if field not in EDITABLE_FIELDS:
        clear_edit_state(context)
        return

    label, _, max_length = EDITABLE_FIELDS[field]

    if len(text) > max_length:
        await update.message.reply_text(
            f"Слишком длинно для поля «{label}»: {len(text)} символов, "
            f"можно до {max_length}. Сократи, пожалуйста, и пришли ещё раз."
        )
        return

    append = data.get("edit_kind") == "append"
    try:
        saved = update_item_field(item_code, user_id, field, text, append=append)
    except Exception as error:
        print(f"update_item_field failed: {type(error).__name__}: {error}")
        await update.message.reply_text(
            "😔 Не получилось сохранить. Попробуй чуть позже — "
            "карточка не изменилась.",
            reply_markup=main_keyboard(),
        )
        clear_edit_state(context)
        return

    clear_edit_state(context)
    if not saved:
        await update.message.reply_text(
            "Эту карточку редактировать нельзя — она удалена или не твоя. 🔎",
            reply_markup=main_keyboard(),
        )
        return

    action_word = "Дописала" if append else "Сохранила"
    await send_edit_menu(
        update.message,
        item_code,
        intro=f"✅ {action_word}: {label}.",
    )


async def handle_edit_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data
    message = update.message
    item_code = data.get("edit_code")
    kind = data.get("edit_field")

    try:
        saved = add_item_photo(
            item_code, update.effective_user.id, kind, message.photo[-1].file_id
        )
    except Exception as error:
        print(f"add_item_photo failed: {type(error).__name__}: {error}")
        await message.reply_text("😔 Не получилось сохранить фото. Попробуй ещё раз.")
        return

    if not saved:
        clear_edit_state(context)
        await message.reply_text(
            "Эту карточку редактировать нельзя — она удалена или не твоя. 🔎",
            reply_markup=main_keyboard(),
        )
        return

    data["edit_photo_count"] = data.get("edit_photo_count", 0) + 1
    data["edit_started"] = time.time()

    # Из альбома отвечаем один раз, а не на каждое фото.
    group_id = message.media_group_id
    if group_id and group_id == data.get("last_media_group"):
        return
    data["last_media_group"] = group_id

    await message.reply_text(
        "📸 Фото добавлено в карточку. Можно прислать ещё.\n"
        "Когда закончишь, нажми «✅ Готово».",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Готово", callback_data=f"edit:{item_code}")
        ]]),
    )


async def open_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    item_code = query.data.split(":", 1)[1]
    item = get_item(item_code, update.effective_user.id)

    if not item:
        await query.message.reply_text("Не смогла найти эту карточку. 🔎")
        return

    mark_photo = item[8]
    details_photo = item[9]

    if mark_photo:
        await query.message.reply_photo(photo=mark_photo)

    if details_photo:
        await query.message.reply_photo(photo=details_photo)

    for kind, file_id in get_item_photos(item_code)[:20]:
        await query.message.reply_photo(photo=file_id)

    if not (mark_photo or details_photo or get_item_photos(item_code)):
        await query.message.reply_text("Фото клейма и деталей пока нет. 📸")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /start выводит из незаконченной анкеты.
    reset_anketa(context)
    context.user_data.pop("ai_history", None)

    text = (
        "👋 Привет! Я Люся — помощница OH MY GOD VINTAGE School.\n\n"
        "Сейчас я в бета-версии и умею вот что:\n\n"
        f"{BTN_BOX} — твоя личная картотека находок для исследования: "
        "фото, клеймо, размеры и история каждого украшения в одной "
        "карточке. Карточки видишь только ты.\n"
        f"{BTN_LEARN} — короткие уроки для начинающих.\n"
        f"{BTN_SEARCH} — поиск по книгам нашей библиотеки.\n\n"
        "💬 А ещё можно просто написать мне вопрос о винтажных "
        "украшениях — например, «Что такое бакелит?». "
        f"До {lucy_ai.daily_limit()} вопросов в день бесплатно.\n\n"
        "Выбирай 👇"
    )

    await update.message.reply_text(text, reply_markup=main_keyboard())


PHOTO_STEP_HINTS = {
    "front_photo": "📸 Сейчас жду фото лицевой стороны украшения целиком.",
    "back_photo": "📸 Сейчас жду фото оборота украшения целиком.",
    "mark_photo": (
        "📸 Сейчас жду фото клейма или надписи крупным планом.\n"
        "Если клейма нет — нажми «🚫 Клейма нет»."
    ),
    "details_photo": (
        "📸 Сейчас жду крупное фото застёжки, шарнира, "
        "креплений или другой детали."
    ),
}

TEXT_STEP_HINTS = {
    "mark_text": (
        "✍️ Сейчас жду текст, а не фото: перепиши, что написано "
        "на клейме — буквы, цифры, значки.\n"
        "Например: TRIFARI © PAT. PEND.\n"
        "Если не разобрать — напиши «не читается»."
    ),
    "measurements": (
        "Фото больше не нужно — все снимки уже есть. 📸✅\n\n"
        "Сейчас напиши размеры текстом, например: 52 × 38 мм."
    ),
    "weight": (
        "Фото больше не нужно — все снимки уже есть. 📸✅\n\n"
        "Сейчас напиши вес в граммах, например: 24.6 г.\n"
        "Если весов нет — просто напиши «нет»."
    ),
    "provenance": (
        "Фото больше не нужно — все снимки уже есть. 📸✅\n\n"
        "Сейчас напиши текстом, где ты нашла или купила украшение."
    ),
    "seller_story": (
        "Фото больше не нужно — все снимки уже есть. 📸✅\n\n"
        "Сейчас напиши текстом, что рассказывал продавец.\n"
        "Если ничего — просто напиши «нет»."
    ),
}


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    step = context.user_data.get("step")

    if context.user_data.get("mode") in ("edit", "edit_photo"):
        if not edit_state_fresh(context):
            clear_edit_state(context)
        elif context.user_data.get("mode") == "edit_photo":
            await handle_edit_photo(update, context)
            return
        else:
            await message.reply_text(
                "✍️ Сейчас жду текст, а не фото. "
                "Передумала — напиши «отмена»."
            )
            return

    if context.user_data.get("mode") != "identify" or (
        step not in PHOTO_STEP_HINTS and step not in TEXT_STEP_HINTS
    ):
        await message.reply_text(
            "📸 Фото получила, но сейчас мы не заполняем карточку.\n\n"
            "Чтобы сохранить украшение в шкатулку, нажми "
            f"{BOX_PATH_ADD} — и я попрошу фото по шагам.",
            reply_markup=main_keyboard(),
        )
        return

    # Альбом приходит отдельными сообщениями с общим media_group_id.
    # Берём только первое фото альбома, иначе остальные
    # разлетятся по следующим шагам анкеты.
    group_id = message.media_group_id
    if group_id and group_id == context.user_data.get("last_media_group"):
        if context.user_data.get("album_notice") != group_id:
            context.user_data["album_notice"] = group_id
            await message.reply_text(
                "Из альбома взяла только первое фото. 🙂\n"
                "Остальные присылай по одному — на каждый шаг своё."
            )
        return
    context.user_data["last_media_group"] = group_id

    if step in TEXT_STEP_HINTS:
        await message.reply_text(TEXT_STEP_HINTS[step])
        return

    if step == "front_photo":
        context.user_data["front_photo"] = message.photo[-1].file_id
        context.user_data["step"] = "back_photo"
        await message.reply_text(
            "Фото лицевой стороны получила. 📸✨\n\n"
            "Теперь переверни украшение и пришли фото оборота целиком.\n\n"
            "💬 Люся: Не обрезай застёжку и края — сзади иногда интереснее, чем спереди. 😏"
        )
        return
    if step == "back_photo":
        context.user_data["back_photo"] = message.photo[-1].file_id
        context.user_data["step"] = "mark_photo"
        mark_keyboard = ReplyKeyboardMarkup(
            [[BTN_NO_MARK]] + MAIN_MENU,
            resize_keyboard=True
        )
        await message.reply_text(
            "Оборот получила. 🔄✨\n\n"
            "Теперь посмотрим маркировку.\n"
            "Если есть клеймо, подпись, буквы, цифры или странный значок — "
            "пришли его отдельным фото крупным планом.\n\n"
            "💬 Люся: Если камера не фокусируется, немного отодвинь телефон. "
            "Резкое фото издалека полезнее размытого макро. 🔎",
             reply_markup=mark_keyboard
        )
        return
    if step == "mark_photo":
        context.user_data["mark_photo"] = message.photo[-1].file_id
        context.user_data["step"] = "mark_text"
        await message.reply_text(
            "Фото маркировки получила. 🔎✨\n\n"
            "Сохраняем её как отдельную зацепку — само клеймо "
            "ещё не считаем доказательством производителя.\n\n"
            "✍️ Теперь перепиши текстом, что написано на клейме: "
            "буквы, цифры, значки — как видишь.\n"
            "Например: TRIFARI © PAT. PEND. или STERLING 925.\n\n"
            "Если не разобрать — напиши «не читается».\n\n"
            "💬 Люся: Текст клейма — это то, по чему я потом смогу найти "
            "твою находку, когда в библиотеке появятся новые сведения. 🔎",
            reply_markup=main_keyboard(),
        )
        return
    if step == "details_photo":
        context.user_data["details_photo"] = message.photo[-1].file_id
        context.user_data["step"] = "measurements"
        await message.reply_text(
            "Деталь получила. 🔎✨\n\n"
            "Теперь запишем размеры украшения.\n\n"
            "📏 Измерь максимальную высоту и ширину и напиши их в миллиметрах.\n"
            "Например: 52 × 38 мм.\n\n"
            "💬 Люся: Если линейки сейчас нет — ничего страшного, этот шаг можно будет пропустить."
        )
        return


SEARCH_PROMPT = (
    "📚 НАЙТИ ИНФОРМАЦИЮ\n\n"
    "Я ищу по текстам книг нашей библиотеки — на английском, "
    "итальянском, немецком, французском, чешском, польском и русском.\n\n"
    "Пиши запрос по-русски, например:\n"
    "• бакелит\n"
    "• чешское стекло\n"
    "• клейма Trifari\n\n"
    "⚠️ Это бета:\n"
    "• книг пока немного, библиотека пополняется;\n"
    "• русские слова я перевожу по своему словарю ювелирных терминов — "
    "если слова там нет, скажу об этом. Марки и клейма можно писать "
    "латиницей: Trifari, Coro, Napier;\n"
    "• показываю оригинальный текст страницы после распознавания — "
    "без перевода, в нём бывают опечатки.\n\n"
    "Напиши, что ищем 🔎"
)

FALLBACK_TEXT = (
    "🙈 Я пока не понимаю свободные вопросы — этому я ещё учусь.\n\n"
    "Сейчас я умею:\n"
    f"{BTN_BOX} — добавить находку или посмотреть свои\n"
    f"{BTN_LEARN} — короткие уроки\n"
    f"{BTN_SEARCH} — поиск по книгам библиотеки\n\n"
    "Выбери кнопку в меню 👇"
)


def search_library(query):
    plan = library_search.build_search_plan(query)
    return plan, library_search.search_pages(DB_PATH, plan)


def _short_terms(terms, limit=8):
    shown = ", ".join(terms[:limit])
    return shown + (" …" if len(terms) > limit else "")


async def run_search(update: Update, query: str):
    try:
        plan, results = await asyncio.to_thread(search_library, query)
    except Exception as error:
        print(f"Library search failed: {type(error).__name__}: {error}")
        await update.message.reply_text(
            "😔 Поиск по библиотеке сейчас не сработал. "
            "Попробуй чуть позже.",
            reply_markup=main_keyboard(),
        )
        return

    unknown_note = ""
    if plan.unknown_words:
        unknown_note = (
            "\n\n🤔 Этих слов нет в моём словаре, я их не перевела: "
            + ", ".join(f"«{word}»" for word in plan.unknown_words)
            + ". Попробуй синоним или напиши термин латиницей."
        )

    if not plan.groups:
        await update.message.reply_text(
            "🔎 Не поняла, что искать. Напиши материал, вид украшения, "
            "технику или марку — например: бакелит, брошь, клейма Trifari."
            + unknown_note,
            reply_markup=main_keyboard(),
        )
        return

    if not results:
        await update.message.reply_text(
            f"🔎 По запросу «{query}» пока ничего не нашла.\n\n"
            f"Искала: {_short_terms(plan.all_terms)}.\n"
            "Книг пока немного — библиотека пополняется. 📚"
            + unknown_note,
            reply_markup=main_keyboard(),
        )
        return

    for result in results:
        page_label = result["printed_page"] or f"PDF {result['pdf_page']}"
        fragment = library_search.excerpt(result["text"], result["position"])
        await update.message.reply_text(
            f"📚 {result['title']}\n"
            f"🌐 Язык книги: {library_search.language_name(result['language'])}\n"
            f"📄 Страница: {page_label}\n"
            f"🔎 Совпадения: {_short_terms(result['matched'], 5)}\n\n"
            "Оригинальный текст (после распознавания, без перевода):\n"
            f"{fragment}"
        )

    await update.message.reply_text(
        "Это точные фрагменты страниц после распознавания — не перевод "
        "и не пересказ. В тексте возможны опечатки распознавания. ✨\n"
        f"Чтобы искать ещё, снова нажми «{BTN_SEARCH}»."
        + unknown_note,
        reply_markup=main_keyboard(),
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return

    if update.message.photo:
        await handle_photo(update, context)
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    mode = context.user_data.get("mode")
    step = context.user_data.get("step")

    # Любая кнопка меню выводит из анкеты и поиска,
    # чтобы её текст не записался как размер, вес и т.д.
    if text in MENU_BUTTONS:
        if anketa_in_progress(context) and text not in ADD_ITEM_BUTTONS:
            await update.message.reply_text(
                "Анкету прервала — эта находка не сохранилась. "
                f"Начать заново: {BOX_PATH_ADD}."
            )
        reset_anketa(context)
        await handle_menu_button(update, context, text)
        return

    if mode in ("edit", "edit_photo"):
        if not edit_state_fresh(context):
            clear_edit_state(context)
            mode = None
        elif text.lower() in ("отмена", "отменить", "cancel"):
            item_code = context.user_data.get("edit_code")
            clear_edit_state(context)
            await send_edit_menu(
                update.message, item_code, intro="Хорошо, ничего не меняю. 🙂"
            )
            return
        else:
            await handle_edit_text(update, context, text)
            return

    if mode == "identify" and step == "mark_text":
        unreadable = {"не читается", "нечитаемо", "не разобрать", "-", "—"}
        if text == BTN_NO_MARK:
            context.user_data["mark_text"] = None
            note = "Поняла — без надписи. 👍"
        elif text.lower() in unreadable:
            context.user_data["mark_text"] = "не читается"
            note = "Записала: надпись не читается. Это тоже информация. 👍"
        else:
            context.user_data["mark_text"] = text[:200]
            note = f"Записала надпись на клейме: {text[:200]} 🔖✨"
        context.user_data["step"] = "details_photo"
        await update.message.reply_text(
            f"{note}\n\n"
            "Теперь пришли крупное фото застёжки, шарнира, креплений "
            "или другой характерной детали. 📸"
        )
        return

    if mode == "identify" and text == BTN_NO_MARK and step != "mark_photo":
        if step in PHOTO_STEP_HINTS:
            hint = PHOTO_STEP_HINTS[step]
        else:
            hint = TEXT_STEP_HINTS.get(step, "")
        await update.message.reply_text(
            "Шаг с клеймом уже позади. 🙂\n\n" + hint
        )
        return

    if mode == "identify" and step == "measurements":
        context.user_data["measurements"] = text
        context.user_data["step"] = "weight"

        await update.message.reply_text(
            f"Размеры записала: {text} 📏✨\n\n"
            "Теперь вес.\n"
            "Если есть весы — напиши вес украшения в граммах.\n"
            "Например: 24.6 г.\n\n"
            "Если весов нет — просто напиши «нет»."
        )
        return

    if mode == "identify" and step == "weight":
        context.user_data["weight"] = text
        context.user_data["step"] = "provenance"

        await update.message.reply_text(
            f"Вес записала: {text} ⚖️✨\n\n"
            "Теперь немного истории.\n"
            "Напиши, где ты нашла или купила это украшение.\n\n"
            "Например: блошиный рынок в Германии, антикварный магазин, "
            "Vinted, наследство или что-то другое.\n\n"
            "Если знаешь город или страну — тоже напиши. 📍"
        )
        return

    if mode == "identify" and step == "provenance":
        context.user_data["provenance"] = text
        context.user_data["step"] = "seller_story"

        await update.message.reply_text(
            f"Записала происхождение: {text} 📍✨\n\n"
            "А теперь — история продавца.\n"
            "Что тебе рассказывали об этом украшении?\n\n"
            "Например: «принадлежало бабушке», «привезено из Франции», "
            "«это точно 1930-е» и так далее.\n\n"
            "Если ничего не рассказывали — просто напиши «нет».\n\n"
            "💬 Люся: Историю продавца сохраняем отдельно. "
            "Это зацепка, а не доказательство. 🔎"
        )
        return

    if mode == "identify" and step == "seller_story":
        data = context.user_data
        measurements = data.get("measurements", "не указаны")
        weight = data.get("weight", "не указан")
        provenance = data.get("provenance", "не указано")
        mark_label = mark_text_label(data.get("mark_text"), data.get("mark_photo"))

        try:
            item_code = save_item(
                measurements,
                weight,
                provenance,
                text,
                data.get("front_photo"),
                data.get("back_photo"),
                data.get("mark_photo"),
                data.get("details_photo"),
                update.effective_user.id,
                data.get("mark_text"),
            )
        except Exception as error:
            print(f"save_item failed: {type(error).__name__}: {error}")
            await update.message.reply_text(
                "😔 Не получилось сохранить карточку. "
                "Ответы пока помню — пришли историю продавца ещё раз "
                "через минуту.",
                reply_markup=main_keyboard(),
            )
            return

        # Анкета закончена: полностью выходим из неё.
        reset_anketa(context)

        await update.message.reply_text(
            f"🗂 КАРТОЧКА НАХОДКИ — {item_code}\n\n"
            "Я собрала основные данные об украшении:\n\n"
            f"📏 Размеры: {measurements}\n"
            f"⚖️ Вес: {weight}\n"
            f"📍 Происхождение: {provenance}\n"
            f"🔖 Надпись на клейме: {mark_label}\n"
            f"💬 Со слов продавца: {text}\n\n"
            "🔎 Статус атрибуции: не определено\n\n"
            f"Карточка и фото сохранены: {BOX_PATH_LIST}. 🗂✨\n\n"
            "Что дальше? Выбирай в меню 👇",
            reply_markup=main_keyboard(),
        )
        return

    if (
        mode == "identify"
        and step == "mark_photo"
        and text.lower() in [BTN_NO_MARK.lower(), "клейма нет", "нет клейма", "нет"]
    ):
        context.user_data["step"] = "details_photo"
        await update.message.reply_text(
            "Поняла — клейма нет. Это нормально. 👍\n\n"
            "Отсутствие клейма ещё ничего не говорит о возрасте "
            "или происхождении украшения.\n\n"
            "Теперь пришли крупное фото застёжки, шарнира, креплений "
            "или других необычных деталей. 🔎",
            reply_markup=main_keyboard(),
        )
        return

    if mode == "identify" and step in PHOTO_STEP_HINTS:
        await update.message.reply_text(
            PHOTO_STEP_HINTS[step]
            + "\n\nЧтобы выйти из анкеты, нажми /start."
        )
        return

    if mode == "find":
        reset_anketa(context)
        await update.message.reply_text(
            f"🔎 Приняла запрос:\n\n«{text}»\n\n"
            "Я запомнила, что именно мы ищем. "
            "Я посмотрю, что можно найти по этому запросу. 😈",
            reply_markup=main_keyboard(),
        )
        return

    if mode == "search":
        reset_anketa(context)
        await run_search(update, text)
        return

    # Всё остальное — свободный вопрос. Люся не молчит.
    reset_anketa(context)
    await answer_free_question(update, context, text)


AI_UNAVAILABLE_TEXT = (
    "😔 Сейчас не могу ответить на вопрос — я немного перегрелась. "
    "Попробуй, пожалуйста, чуть позже.\n\n"
    "А пока можно заглянуть в меню 👇"
)

AI_LIMIT_TEXT = (
    "🙈 На сегодня бесплатные вопросы закончились — "
    "лимит {limit} в день. Завтра снова смогу ответить!\n\n"
    "А пока можно заглянуть в шкатулку или в уроки 👇"
)

AI_TOTAL_LIMIT_TEXT = (
    "🙈 Сегодня мне задали очень много вопросов, и я устала. "
    "Завтра снова смогу ответить!\n\n"
    "А пока можно заглянуть в шкатулку или в уроки 👇"
)


async def notify_admin_ai_problem(context, reason):
    admin_id = os.environ.get("LUCY_ADMIN_ID", "").strip()
    if not admin_id:
        return
    last = context.bot_data.get("ai_alert_at")
    now = asyncio.get_running_loop().time()
    if last is not None and now - last < 3600:
        return
    context.bot_data["ai_alert_at"] = now
    if reason == "quota":
        text = (
            "⚠️ Люся: OpenAI сообщает, что баланс закончился. "
            "Свободные ответы не работают, пока баланс не пополнят."
        )
    elif reason == "no_key":
        text = "⚠️ Люся: не задан OPENAI_API_KEY — свободные ответы не работают."
    else:
        text = "⚠️ Люся: OpenAI не отвечает. Подробности — в логах Railway."
    try:
        await context.bot.send_message(chat_id=int(admin_id), text=text)
    except Exception as error:
        print(f"Admin alert failed: {type(error).__name__}")


async def answer_free_question(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    if not lucy_ai.ai_enabled():
        await update.message.reply_text(FALLBACK_TEXT, reply_markup=main_keyboard())
        return

    user_id = update.effective_user.id
    unlimited = is_admin(user_id)

    try:
        if not unlimited:
            if lucy_ai.used_today_total(DB_PATH) >= lucy_ai.daily_total_limit():
                await update.message.reply_text(
                    AI_TOTAL_LIMIT_TEXT, reply_markup=main_keyboard()
                )
                return
            if lucy_ai.used_today(DB_PATH, user_id) >= lucy_ai.daily_limit():
                await update.message.reply_text(
                    AI_LIMIT_TEXT.format(limit=lucy_ai.daily_limit()),
                    reply_markup=main_keyboard(),
                )
                return
    except Exception as error:
        print(f"AI usage check failed: {type(error).__name__}: {error}")
        await update.message.reply_text(AI_UNAVAILABLE_TEXT, reply_markup=main_keyboard())
        return

    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action="typing"
        )
    except Exception:
        pass

    history = context.user_data.get("ai_history", [])
    messages = lucy_ai.build_messages(history, text)

    try:
        answer = await asyncio.to_thread(lucy_ai.ask_openai, messages)
    except lucy_ai.AIUnavailable as problem:
        if problem.reason == "disabled":
            await update.message.reply_text(FALLBACK_TEXT, reply_markup=main_keyboard())
            return
        await notify_admin_ai_problem(context, problem.reason)
        await update.message.reply_text(AI_UNAVAILABLE_TEXT, reply_markup=main_keyboard())
        return
    except Exception as error:
        print(f"AI answer failed: {type(error).__name__}")
        await update.message.reply_text(AI_UNAVAILABLE_TEXT, reply_markup=main_keyboard())
        return

    try:
        if not unlimited:
            lucy_ai.record_question(DB_PATH, user_id)
    except Exception as error:
        print(f"AI usage record failed: {type(error).__name__}: {error}")

    history = history + [
        {"role": "user", "content": text[:lucy_ai.MAX_QUESTION_CHARS]},
        {"role": "assistant", "content": answer},
    ]
    context.user_data["ai_history"] = history[-lucy_ai.HISTORY_EXCHANGES * 2:]

    reply = lucy_ai.format_answer(answer)
    if not unlimited:
        left = lucy_ai.daily_limit() - lucy_ai.used_today(DB_PATH, user_id)
        if 0 <= left <= 3:
            reply += f"\n\nОсталось вопросов на сегодня: {left}."

    await update.message.reply_text(reply, reply_markup=main_keyboard())


BOX_INTRO_TEXT = (
    "🗃️ МОЯ ШКАТУЛКА\n\n"
    "💎 У каждого украшения есть своя история. "
    "Давай попробуем её раскрыть!\n\n"
    "Добавляй сюда свои находки: фотографии, клейма, размеры и всё, "
    "что тебе о них известно.\n\n"
    "🔎 Твои украшения сохранятся в личной шкатулке, и ты всегда сможешь "
    "вернуться к ним, дополнить информацию или продолжить исследование.\n\n"
    "📚 А самое интересное — впереди!\n\n"
    "Наша историческая библиотека постоянно пополняется. И в будущем Люся "
    "сможет возвращаться к твоим находкам, сравнивать их с материалами "
    "старинных каталогов, книг и архивов и помогать тебе устанавливать их "
    "происхождение, возраст и производителя — там, где сохранились "
    "достаточные доказательства.\n\n"
    "✨ Возможно, украшение, о котором сегодня почти ничего не известно, "
    "однажды раскроет свою историю!\n\n"
    "Ведь в нашей школе мы не гадаем — мы собираем доказательства. 🔍"
)


async def start_anketa(message, context):
    reset_anketa(context)
    context.user_data["mode"] = "identify"
    context.user_data["step"] = "front_photo"
    await message.reply_text(
        "➕ ДОБАВИТЬ УКРАШЕНИЕ\n\n"
        "Заведём карточку находки в твоей картотеке. Чем точнее фото "
        "и записи, тем проще потом исследовать украшение.\n\n"
        "Начинаем с лицевой стороны.\n\n"
        "📸 Пришли одно фото украшения целиком.\n\n"
        "💬 Люся: Маленький секрет хорошей фотографии — "
        "протри камеру телефона. Серьёзно. 😂\n"
        "Лучше всего снимать при дневном свете возле окна, без вспышки."
    )


async def show_my_items(message, user_id):
    items = get_items(user_id)

    if not items:
        await message.reply_text(
            "💎 Твоя шкатулка пока пустая.\n\n"
            f"Добавь первую находку: {BOX_PATH_ADD}.",
            reply_markup=main_keyboard(),
        )
        return

    keyboard = [
        [InlineKeyboardButton(f"💎 {item_code}", callback_data=f"item:{item_code}")]
        for item_code, measurements, weight in items
    ]

    await message.reply_text(
        "💎 МОИ УКРАШЕНИЯ\n\n"
        "Выбери находку, чтобы открыть её карточку:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def box_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "box:add":
        await start_anketa(query.message, context)
        return

    if query.data == "box:list":
        if anketa_in_progress(context):
            await query.message.reply_text(
                "Анкету прервала — эта находка не сохранилась. "
                f"Начать заново: {BOX_PATH_ADD}."
            )
        reset_anketa(context)
        await show_my_items(query.message, update.effective_user.id)


async def handle_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    if text in ADD_ITEM_BUTTONS:
        await start_anketa(update.message, context)
    elif text == BTN_SEARCH:
        context.user_data["mode"] = "search"
        await update.message.reply_text(SEARCH_PROMPT)
    elif text == BTN_LEARN:
        await update.message.reply_text(
            "🎓 OH MY GOD VINTAGE SCHOOL\n\n"
            "Здесь не будет лекции на два часа и экзамена в пятницу. 😏\n"
            "Учимся короткими интерактивными уроками на реальных украшениях.\n\n"
            "🔍 УРОВЕНЬ 1 — НАУЧИСЬ ВИДЕТЬ\n"
            "Урок 1 из 10\n"
            "«Не хватай сразу 😂»\n\n"
            "⏱ 2–3 минуты\n\n"
            "▶️ НАЧАТЬ УРОК:\n"
            "https://kristinaluneva57-alt.github.io/oh-my-god-vintage/"
        )
    elif text == "🧭 Разделы школы":
        await update.message.reply_text(
            "🏛 OH MY GOD VINTAGE SCHOOL\n\n"
            "Добро пожаловать в энциклопедию винтажных украшений. ✨\n"
            "Здесь мы собираем всё — от эпох и материалов до клейм, "
            "подделок, дизайнеров и реальных расследований.\n\n"
            "🕰 МАШИНА ВРЕМЕНИ\n"
            "💎 ЛАБОРАТОРИЯ МАТЕРИАЛОВ\n"
            "👑 ДОМ МОДЫ — БРЕНДЫ И ДИЗАЙНЕРЫ\n"
            "🔎 АРХИВ КЛЕЙМ\n"
            "🧷 АНАТОМИЯ УКРАШЕНИЯ\n"
            "🕵️ VINTAGE CRIME — ПОДДЕЛКИ\n"
            "🏺 МАСТЕРСКАЯ — ТЕХНИКИ\n"
            "📚 СЕКРЕТНАЯ БИБЛИОТЕКА\n"
            "💰 СКОЛЬКО ЭТО СТОИТ?\n"
            "🛒 ОХОТА — ГДЕ И КАК ПОКУПАТЬ\n"
            "💸 ОТ НАХОДКИ ДО ПРОДАЖИ\n"
            "🔬 VINTAGE LAB — ПРАКТИКУМ\n"
            "🔥 НАШИ РАССЛЕДОВАНИЯ\n"
            "🧭 ОПРЕДЕЛИТЕЛЬ\n\n"
            "💬 Люся: Выбирай, куда полезем. Я бы полезла сразу во всё, "
            "но у меня работа такая. 😏",
            reply_markup=main_keyboard(),
        )
    elif text in BOX_BUTTONS:
        await update.message.reply_text(
            BOX_INTRO_TEXT,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(BOX_ADD_TEXT, callback_data="box:add")],
                [InlineKeyboardButton(BOX_LIST_TEXT, callback_data="box:list")],
            ]),
        )
    elif text == "🆘 Найди мне это":
        context.user_data["mode"] = "find"
        await update.message.reply_text(
            "🆘 НАЙДИ МНЕ ЭТО\n\n"
            "Ищешь конкретное украшение, бренд, книгу, каталог или что-то ещё?\n\n"
            "Напиши мне, что именно нужно найти. Чем больше деталей — тем лучше. 🔎\n\n"
            "💬 Люся: Можно даже начать с «я не знаю, как эта зараза называется». Разберёмся. 😏"
        )
    elif text == "💬 Vintage Club":
        await update.message.reply_text(
            "💬 VINTAGE CLUB\n\n"
            "Место, где можно обсуждать находки, показывать покупки, "
            "спрашивать мнение и разбирать винтаж вместе. ✨\n\n"
            "Здесь будут:\n"
            "💎 находки участников\n"
            "🔎 помощь с атрибуцией\n"
            "💰 «А вы бы купили за эту цену?»\n"
            "🤦‍♀️ наши ошибки и сомнительные покупки\n"
            "🏆 находка месяца и маленькие челленджи\n\n"
            "💬 Люся: Иногда лучший способ понять, что ты купила, — "
            "показать это людям и коллективно удивиться. 😏",
            reply_markup=main_keyboard(),
        )


@admin_only
async def test_knowledge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT b.book_code, b.title, k.fact, ks.page_number
        FROM knowledge k
        JOIN knowledge_sources ks ON ks.knowledge_id = k.id
        JOIN books b ON b.id = ks.book_id
        ORDER BY k.id
        LIMIT 1
        """
    )

    row = cursor.fetchone()
    conn.close()

    if row:
        book_code, title, fact, page = row
        await update.message.reply_text(
            f"📚 {book_code}\n"
            f"{title}\n"
            f"Страница: {page}\n\n"
            f"🧠 {fact}"
        )
    else:
        await update.message.reply_text("❌ В базе знаний пока пусто.")
@admin_only
async def test_bridge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        fact_id = save_library_fact(
            document_code="DOC-000002",
            pdf_page=12,
            topic="Jet — проверка связи",
            fact="Тестовая запись связи факта со страницей PDF.",
            notes="Технический тест, не исторический факт.",
        )

        row = get_library_fact(fact_id)

        if row is None:
            raise ValueError("Факт не найден после сохранения")

        topic, fact, status, title, code, pdf_page, printed_page = row

        await update.message.reply_text(
            f"✅ МОСТ РАБОТАЕТ\n"
            f"ID факта: {fact_id}\n"
            f"Книга: {title}\n"
            f"Документ: {code}\n"
            f"PDF-страница: {pdf_page}\n"
            f"Статус: {status}\n"
            f"Текст: {fact}"
        )

    except Exception as error:
        await update.message.reply_text(
            f"❌ Ошибка моста: {type(error).__name__}: {error}"
        )

@admin_only
async def extract_jet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        candidates = extract_page_facts(
            document_code="DOC-000002",
            pdf_page=12,
            limit=5,
        )

        if not candidates:
            await update.message.reply_text(
                "На странице 12 кандидаты не найдены."
            )
            return

        lines = [
            "🔎 JET — КАНДИДАТЫ НА ФАКТЫ",
            "Документ: DOC-000002",
            "PDF-страница: 12",
            "",
        ]

        for number, sentence in enumerate(candidates, start=1):
            lines.append(f"{number}. {sentence}\n")

        lines.append("⚠️ Это фрагменты OCR, ещё не проверенные.")

        await update.message.reply_text("\n".join(lines))

    except Exception as error:
        await update.message.reply_text(
            f"❌ Ошибка извлечения: {type(error).__name__}: {error}"
        )



@admin_only
async def test_importer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    test_checksum = "OMG-IMPORTER-TEST-V1"

    document_id, document_code, created_first = register_document(
        title="TEST IMPORTER DOCUMENT",
        storage_key="test://library-importer",
        language="en",
        checksum=test_checksum,
    )

    _, second_code, created_second = register_document(
        title="TEST IMPORTER DOCUMENT",
        storage_key="test://library-importer",
        language="en",
        checksum=test_checksum,
    )

    save_page(
        document_id=document_id,
        pdf_page=1,
        printed_page="1",
        original_text="Original test text",
        russian_text="Тестовый перевод",
    )

    update_import_progress(
        document_id=document_id,
        last_completed_page=7,
        status="processing",
    )

    status, last_page, error = get_import_progress(document_id)

    await update.message.reply_text(
        "🧪 ТЕСТ ИМПОРТЕРА\n\n"
        f"Документ: {document_code}\n"
        f"Первое создание: {created_first}\n"
        f"Повторное создание: {created_second}\n"
        f"Код при повторе: {second_code}\n"
        f"Статус: {status}\n"
        f"Последняя страница: {last_page}\n"
        f"Ошибка: {error}"
    )
@admin_only
async def test_env(update: Update, context: ContextTypes.DEFAULT_TYPE):
    names = [
        "R2_ENDPOINT_URL",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET_NAME",
    ]

    result = "\n".join(
        f"{name}: {'YES' if name in os.environ else 'NO'}"
        for name in names
    )

    await update.message.reply_text(
        "🔧 ENV TEST\n\n" + result
    )
@admin_only
async def import_jet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Тот же замок, что у загрузки PDF через чат: обе обработки
    # пишут во временный файл /tmp/library_import.pdf.
    if _upload_lock.locked():
        await update.message.reply_text(
            "⏳ Другая книга ещё обрабатывается. Попробуй позже."
        )
        return

    async with _upload_lock:
        await _import_jet(update)


async def _import_jet(update: Update):
    await update.message.reply_text(
        "📚 Начинаю импорт Jet.\n"
        "OCR обрабатывает всю книгу. Это может занять некоторое время."
    )

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute(
            """
            UPDATE library_import_jobs
            SET status = 'pending',
                last_completed_page = 0,
                error_message = NULL
            WHERE document_id = (
                SELECT id
                FROM library_documents
                WHERE document_code = 'DOC-000002'
            )
            """
        )

        conn.commit()
        conn.close()
        result = await asyncio.to_thread(
            import_pdf_with_ocr,
            storage_key="books/BOOK-000001/Book 1.pdf",
            title="Jet Jewellery and Ornaments",
            language="eng",
        )

        await update.message.reply_text(
            "✅ JET ИМПОРТИРОВАН\n\n"
            f"Документ: {result['document_code']}\n"
            f"PDF-страниц: {result['total_pages']}\n"
            f"Обработано: {result['completed_pages']}\n"
            f"Статус: {result['status']}"
        )

    except Exception as error:
        await update.message.reply_text(
            "❌ ИМПОРТ JET ОСТАНОВЛЕН\n\n"
            f"{type(error).__name__}: {error}\n\n"
            "Уже сохранённые страницы не потеряны."
        )
@admin_only
async def test_r2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    storage_key = "books/BOOK-000001/Book 1.pdf"
    local_path = "/tmp/book_000001.pdf"

    try:
        await asyncio.to_thread(download_pdf_from_r2, storage_key, local_path)
        result = await asyncio.to_thread(inspect_pdf, local_path)

        await update.message.reply_text(
            "☁️ R2 ТЕСТ УСПЕШЕН\n\n"
            f"Файл: {storage_key}\n"
            f"PDF-страниц: {result['page_count']}\n"
            f"Страниц с текстом: {result['pages_with_text']}\n"
            f"Страниц без текста: {result['pages_without_text']}"
        )
    except Exception as error:
        await update.message.reply_text(
            f"❌ R2 ТЕСТ НЕ ПРОШЁЛ\n\n{type(error).__name__}: {error}"
        )

async def ai_jet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = os.environ.get("LUCY_ADMIN_ID")

    if not admin_id or str(update.effective_user.id) != admin_id:
        await update.message.reply_text(
            "🔒 Команда доступна только администратору."
        )
        return
    await update.message.reply_text(
        "🧠 Люся анализирует страницу 12 книги Jet..."
    )

    try:
        facts = await asyncio.to_thread(
            extract_ai_facts,
            "DOC-000002",
            12,
        )

        if not facts:
            await update.message.reply_text(
                "AI не нашёл фактов с подтверждёнными OCR-цитатами."
            )
            return

        lines = ["📚 AI-АНАЛИЗ JET — PDF-страница 12"]

        for number, item in enumerate(facts, 1):
            lines.append(
                f"\n{number}. {item['topic']}\n"
                f"Факт: {item['fact']}\n"
                f"Цитата: {item['quote']}"
            )

        result = "\n".join(lines)
        await update.message.reply_text(result[:3900])

    except Exception as error:
        await update.message.reply_text(
            f"❌ AI-тест остановлен: {type(error).__name__}: {error}"
        )



async def save_jet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = os.environ.get("LUCY_ADMIN_ID")

    if not admin_id or str(update.effective_user.id) != admin_id:
        await update.message.reply_text(
            "🔒 Команда доступна только администратору."
        )
        return

    await update.message.reply_text(
        "📚 Люся извлекает и сохраняет факты из Jet..."
    )

    try:
        facts = await asyncio.to_thread(
            extract_ai_facts, "DOC-000002", 12
        )

        if not facts:
            await update.message.reply_text(
                "На странице нет подтверждённых фактов."
            )
            return

        ids = await asyncio.to_thread(
            save_ai_facts, "DOC-000002", 12, facts
        )

        await update.message.reply_text(
            f"✅ Сохранено или найдено в базе: {len(ids)} фактов.\n"
            f"ID записей: {ids}\n"
            "Источник: DOC-000002, PDF-страница 12."
        )

    except Exception as error:
        await update.message.reply_text(
            f"❌ Ошибка сохранения: {type(error).__name__}: {error}"
        )


LANGUAGE_CODES_HELP = ", ".join(
    f"{code} ({name})" for code, name in OCR_LANGUAGES.items()
)


@admin_only
async def reocr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = [arg.strip() for arg in (context.args or [])]
    if len(args) < 2 or args[1].lower() not in OCR_LANGUAGES:
        await update.message.reply_text(
            "🔁 ПОВТОРНОЕ РАСПОЗНАВАНИЕ\n\n"
            "Формат: /reocr DOC-000003 ita\n"
            "С принудительным OCR всех страниц (если в PDF плохой "
            "текстовый слой): /reocr DOC-000003 ita force\n\n"
            f"Языки: {LANGUAGE_CODES_HELP}\n\n"
            "Коды книг — в /books."
        )
        return

    document_code = args[0].upper()
    language = args[1].lower()
    force_ocr = len(args) > 2 and args[2].lower() == "force"

    await update.message.reply_text(
        f"🔁 Ставлю {document_code} на повторное распознавание "
        f"({OCR_LANGUAGES[language]}"
        + (", все страницы через OCR" if force_ocr else "")
        + ").\nPDF возьму из R2, заново загружать не нужно. "
        "Существующие страницы не удаляются. Напишу, когда закончу."
    )

    context.application.create_task(
        _reocr_and_report(update.message, document_code, language, force_ocr)
    )


async def _reocr_and_report(message, document_code, language, force_ocr):
    try:
        result = await asyncio.to_thread(
            reocr_document, document_code, language, force_ocr
        )
        await message.reply_text(
            "✅ ПОВТОРНОЕ РАСПОЗНАВАНИЕ ГОТОВО\n\n"
            f"📚 {result['title']}\n"
            f"Документ: {result['document_code']}\n"
            f"Язык OCR: {OCR_LANGUAGES[language]}\n"
            f"Страниц: {result['total_pages']}"
        )
    except LookupError as error:
        await message.reply_text(f"❌ {error}")
    except Exception as error:
        print(f"reocr failed: {type(error).__name__}: {error}")
        await message.reply_text(
            f"❌ Повторное распознавание {document_code} остановлено.\n"
            "Уже распознанные страницы сохранены. Подробности — в журнале "
            "Railway. Можно запустить команду ещё раз."
        )


@admin_only
async def books_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rows = await asyncio.to_thread(list_documents)
    except Exception as error:
        print(f"list_documents failed: {type(error).__name__}: {error}")
        await update.message.reply_text("❌ Не получилось прочитать каталог.")
        return

    books = [row for row in rows if (row[7] or "").startswith("books/")]
    if not books:
        await update.message.reply_text("📚 В библиотеке пока нет книг.")
        return

    status_icons = {
        "completed": "✅", "processing": "⏳",
        "pending": "🕓", "error": "❌",
    }
    lines = [f"📚 КАТАЛОГ БИБЛИОТЕКИ — {len(books)} книг\n"]
    for code, title, language, status, last_page, pages, empty, _ in books:
        icon = status_icons.get(status, "•")
        lines.append(
            f"{icon} {code} · {title[:60]}\n"
            f"   язык: {OCR_LANGUAGES.get(language or '', language or '—')} · "
            f"страниц: {pages} (пустых: {empty}) · статус: {status}"
            + (f" (до стр. {last_page})" if status in ("processing", "error") else "")
        )

    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 3800:
            await update.message.reply_text(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await update.message.reply_text(chunk)


def main():
    init_db()
    init_library_db()
    init_knowledge_bridge()
    lucy_ai.init_ai_usage_table(DB_PATH)
    seed_books()
    seed_knowledge()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("testknowledge", test_knowledge))
    app.add_handler(CommandHandler("testbridge", test_bridge))
    app.add_handler(CommandHandler("extractjet", extract_jet))
    app.add_handler(CommandHandler("aijet", ai_jet))
    app.add_handler(CommandHandler("savejet", save_jet))
    app.add_handler(CommandHandler("testimporter", test_importer))
    app.add_handler(CommandHandler("testr2", test_r2))
    app.add_handler(CommandHandler("importjet", import_jet))
    app.add_handler(CommandHandler("bigbook", bigbook_command))
    app.add_handler(CommandHandler("testenv", test_env))
    app.add_handler(CommandHandler("reocr", reocr_command))
    app.add_handler(CommandHandler("books", books_command))
    app.add_handler(CallbackQueryHandler(box_callback, pattern=r"^box:"))
    app.add_handler(CallbackQueryHandler(open_item, pattern=r"^item:"))
    app.add_handler(CallbackQueryHandler(delete_callback, pattern=r"^del(ok|no)?:"))
    app.add_handler(CallbackQueryHandler(open_details, pattern=r"^details:"))
    app.add_handler(CallbackQueryHandler(edit_callback, pattern=r"^(edit|edone|ef|ep):"))
    app.add_handler(
        MessageHandler(
            filters.Document.ALL,
            receive_book_pdf
        )
    )
    # Пока Люся работает только в личных сообщениях:
    # в группе она не должна отвечать на каждое сообщение и фото.
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.PHOTO,
            handle_message,
        )
    )
    
    start_upload_server()
    app.run_polling()


if __name__ == "__main__":
    main()
