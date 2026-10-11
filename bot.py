import os
import asyncio
import sqlite3
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
from library_importer import register_document, save_page, update_import_progress, get_import_progress, download_pdf_from_r2, inspect_pdf, import_pdf_with_ocr

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
    "details_photo",
    "measurements",
    "weight",
    "provenance",
    "seller_story",
    "item_id",
    "last_media_group",
    "album_notice",
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
        ("front_photo", "TEXT"),
        ("back_photo", "TEXT"),
        ("mark_photo", "TEXT"),
        ("details_photo", "TEXT"),
        ("owner_user_id", "INTEGER"),
    ]

    existing_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(items)")
    }

    for column_name, column_type in columns:
        if column_name not in existing_columns:
            conn.execute(
                f"ALTER TABLE items ADD COLUMN {column_name} {column_type}"
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
    owner_user_id
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
        owner_user_id
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        owner_user_id
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
OWNER_FILTER = "(owner_user_id = ? OR (owner_user_id IS NULL AND ?))"


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
               seller_story, attribution_status, front_photo, back_photo, mark_photo, details_photo
        FROM items
        WHERE item_code = ?
          AND {OWNER_FILTER}
        """,
        (item_code, user_id, is_admin(user_id))
    )

    item = cursor.fetchone()
    conn.close()

    return item
async def open_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    item_code = query.data.split(":", 1)[1]
    item = get_item(item_code, update.effective_user.id)

    if not item:
        await query.message.reply_text(
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
        details_photo
    ) = item
    
    if front_photo:
        await query.message.reply_photo(photo=front_photo)
        
    if back_photo:
        await query.message.reply_photo(photo=back_photo)

    card = (
        "✦ OH MY GOD VINTAGE ARCHIVE ✦\n\n"
        f"💎 {item_code}\n"
        "Карточка находки\n\n"
        "─── ПАСПОРТ ПРЕДМЕТА ───\n"
        f"📏 Размер: {measurements}\n"
        f"⚖️ Вес: {weight}\n"
        f"📍 Происхождение: {provenance}\n\n"
        "─── АТРИБУЦИЯ ───\n"
        f"◇ Статус: {attribution_status}\n\n"
        "─── ИСТОРИЯ ПРОДАВЦА ───\n"
        f"{seller_story}"
    )
    details_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🔬 Клеймо и детали",
            callback_data=f"details:{item_code}"
        )]
    ])
    
    await query.message.reply_text(
        card,
        reply_markup=details_keyboard
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
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /start выводит из незаконченной анкеты.
    reset_anketa(context)

    text = (
        "👋 Привет! Я Люся — помощница OH MY GOD VINTAGE School.\n\n"
        "Сейчас я в бета-версии и умею вот что:\n\n"
        f"{BTN_BOX} — твои находки. Здесь можно добавить украшение "
        "(фото, клеймо, размеры, история) и посмотреть сохранённые "
        "карточки. Их видишь только ты.\n"
        f"{BTN_LEARN} — короткие уроки для начинающих.\n"
        f"{BTN_SEARCH} — поиск по книгам нашей библиотеки "
        "(пока по-английски).\n\n"
        "Отвечать на свободные вопросы я пока учусь — "
        "скоро научусь. 🙂\n\n"
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
        context.user_data["step"] = "details_photo"
        await message.reply_text(
            "Фото маркировки получила. 🔎✨\n\n"
            "Сохраняем её как отдельную зацепку — само клеймо "
            "ещё не считаем доказательством производителя.\n\n"
            "Теперь пришли крупное фото застёжки, шарнира, креплений "
            "или другой характерной детали.",
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
    "Я ищу по текстам книг из нашей библиотеки.\n\n"
    "⚠️ Пока это бета, и есть ограничения:\n"
    "• книг в библиотеке пока немного, но их будет больше;\n"
    "• книги на английском, поэтому пиши запрос по-английски: "
    "bakelite, jet, Trifari, rhinestone, brooch;\n"
    "• лучше одно-два слова, а не целый вопрос;\n"
    "• я показываю фрагмент страницы как есть, после распознавания — "
    "в нём могут быть опечатки.\n\n"
    "Напиши, что ищем 🔎"
)

FALLBACK_TEXT = (
    "🙈 Я пока не понимаю свободные вопросы — этому я ещё учусь.\n\n"
    "Сейчас я умею:\n"
    f"{BTN_BOX} — добавить находку или посмотреть свои\n"
    f"{BTN_LEARN} — короткие уроки\n"
    f"{BTN_SEARCH} — поиск по книгам (по-английски)\n\n"
    "Выбери кнопку в меню 👇"
)


def has_cyrillic(text):
    return any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in text)


def search_library(query):
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT d.title, p.pdf_page, p.printed_page,
                    COALESCE(p.cleaned_text, p.original_text)
            FROM library_pages p
            JOIN library_documents d ON d.id = p.document_id
            WHERE COALESCE(p.cleaned_text, p.original_text) LIKE ?
            ORDER BY d.id, p.pdf_page
            LIMIT 3
            """,
            (f"%{query}%",)
        )
        return cursor.fetchall()
    finally:
        conn.close()


async def run_search(update: Update, query: str):
    try:
        results = await asyncio.to_thread(search_library, query)
    except Exception as error:
        print(f"Library search failed: {type(error).__name__}: {error}")
        await update.message.reply_text(
            "😔 Поиск по библиотеке сейчас не сработал. "
            "Попробуй чуть позже.",
            reply_markup=main_keyboard(),
        )
        return

    if not results:
        if has_cyrillic(query):
            hint = (
                "Книги в библиотеке на английском — попробуй написать "
                "запрос по-английски. Например: bakelite, jet, Trifari."
            )
        else:
            hint = (
                "Попробуй другое слово или более короткий запрос. "
                "Книг пока немного, библиотека пополняется. 📚"
            )
        await update.message.reply_text(
            f"🔎 По запросу «{query}» пока ничего не нашла.\n\n{hint}",
            reply_markup=main_keyboard(),
        )
        return

    for title, pdf_page, printed_page, page_text in results:
        page_label = printed_page or f"PDF {pdf_page}"
        position = max(0, page_text.lower().find(query.lower()))
        start = max(0, position - 250)
        end = min(len(page_text), position + len(query) + 500)
        excerpt = page_text[start:end].strip()
        if start > 0:
            excerpt = "…" + excerpt
        if end < len(page_text):
            excerpt = excerpt + "…"

        await update.message.reply_text(
            f"📚 {title}\n"
            f"📄 Страница: {page_label}\n\n"
            f"{excerpt}"
        )

    await update.message.reply_text(
        "Это фрагменты страниц после распознавания — "
        "в тексте возможны опечатки. ✨\n"
        f"Чтобы искать ещё, снова нажми «{BTN_SEARCH}».",
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
                update.effective_user.id
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

    # Всё остальное: Люся не молчит.
    reset_anketa(context)
    await update.message.reply_text(FALLBACK_TEXT, reply_markup=main_keyboard())


async def start_anketa(message, context):
    reset_anketa(context)
    context.user_data["mode"] = "identify"
    context.user_data["step"] = "front_photo"
    await message.reply_text(
        "➕ ДОБАВИТЬ УКРАШЕНИЕ\n\n"
        "Заполним карточку находки — она сохранится в твоей шкатулке.\n\n"
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
            "🗃️ МОЯ ШКАТУЛКА\n\n"
            "Здесь живут твои находки. Их видишь только ты.",
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


def main():
    init_db()
    init_library_db()
    init_knowledge_bridge()
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
    app.add_handler(CallbackQueryHandler(box_callback, pattern=r"^box:"))
    app.add_handler(CallbackQueryHandler(open_item, pattern=r"^item:"))
    app.add_handler(CallbackQueryHandler(open_details, pattern=r"^details:"))
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
