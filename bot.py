import os
import sqlite3

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

MAIN_MENU = [
    ["🔎 Определить украшение", "📚 Найти информацию"],
    ["🆘 Найди мне это", "🎓 Учиться"],
    ["🧭 Разделы школы", "🗃 Моя шкатулка"],
    ["💬 Vintage Club"],
]

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
    details_photo
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
        details_photo
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """,
    (
        measurements,
        weight,
        provenance,
        seller_story,
        front_photo,
        back_photo,
        mark_photo,
        details_photo
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
def get_items():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT item_code, measurements, weight FROM items ORDER BY id DESC"
    )

    items = cursor.fetchall()
    conn.close()

    return items
    
def get_item(item_code):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT item_code, measurements, weight, provenance,
               seller_story, attribution_status, front_photo, back_photo, mark_photo, details_photo
        FROM items
        WHERE item_code = ?
        """,
        (item_code,)
    )

    item = cursor.fetchone()
    conn.close()

    return item
async def open_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    item_code = query.data.split(":", 1)[1]
    item = get_item(item_code)

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
    item = get_item(item_code)

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
    keyboard = ReplyKeyboardMarkup(
        MAIN_MENU,
        resize_keyboard=True,
    )

    text = (
        "👋 Привет! Я Люся — помощница OH MY GOD VINTAGE School.\n\n"
        "Помогу разобраться с украшением, найти нужную информацию "
        "в школе, начать собственное расследование или выбрать, "
        "что изучать дальше.\n\n"
        "Не знаешь, с чего начать? Просто выбери 👇\n\n"
        "Или напиши мне свой вопрос."
    )

    await update.message.reply_text(text, reply_markup=keyboard)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if update.message.photo:
        if context.user_data.get("mode") == "identify":
            step = context.user_data.get("step")

            if step == "front_photo":
                context.user_data["front_photo"] = update.message.photo[-1].file_id
                context.user_data["step"] = "back_photo"
                await update.message.reply_text(
                    "Фото лицевой стороны получила. 📸✨\n\n"
                    "Теперь переверни украшение и пришли фото оборота целиком.\n\n"
                    "💬 Люся: Не обрезай застёжку и края — сзади иногда интереснее, чем спереди. 😏"
                )
                return
        if step == "back_photo":
            context.user_data["back_photo"] = update.message.photo[-1].file_id
            context.user_data["step"] = "mark_photo"
            mark_keyboard = ReplyKeyboardMarkup(
                [["🚫 Клейма нет"]],
                resize_keyboard=True
            )
            await update.message.reply_text(
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
            context.user_data["mark_photo"] = update.message.photo[-1].file_id
            context.user_data["step"] = "details_photo"
            await update.message.reply_text(
                "Фото маркировки получила. 🔎✨\n\n"
                "Сохраняем её как отдельную зацепку — само клеймо "
                "ещё не считаем доказательством производителя.\n\n"
                "Теперь пришли крупное фото застёжки, шарнира, креплений "
                "или другой характерной детали."
            )
            return
        if step == "details_photo":
            context.user_data["details_photo"] = update.message.photo[-1].file_id
            context.user_data["step"] = "measurements"
            await update.message.reply_text(
                "Деталь получила. 🔎✨\n\n"
                "Теперь запишем размеры украшения.\n\n"
                "📏 Измерь максимальную высоту и ширину и напиши их в миллиметрах.\n"
                "Например: 52 × 38 мм.\n\n"
                "💬 Люся: Если линейки сейчас нет — ничего страшного, этот шаг можно будет пропустить."
            )
            return
    if (
        context.user_data.get("mode") == "identify"
        and context.user_data.get("step") == "measurements"
    ):
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
    if (
        context.user_data.get("mode") == "identify"
        and context.user_data.get("step") == "weight"
    ):
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
    if (
        context.user_data.get("mode") == "identify"
        and context.user_data.get("step") == "provenance"
    ):
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
    if (
        context.user_data.get("mode") == "identify"
        and context.user_data.get("step") == "seller_story"
    ):
        context.user_data["seller_story"] = text
        context.user_data["item_id"] = save_item(
            context.user_data.get("measurements", "не указаны"),
            context.user_data.get("weight", "не указан"),
            context.user_data.get("provenance", "не указано"),
            text,
            context.user_data.get("front_photo"),
            context.user_data.get("back_photo"),
            context.user_data.get("mark_photo"),
            context.user_data.get("details_photo")
        )
        context.user_data["step"] = "complete"

        await update.message.reply_text(
            f"🗂 КАРТОЧКА НАХОДКИ — {context.user_data['item_id']}\n\n"
            "Я собрала основные данные об украшении:\n\n"
            f"📏 Размеры: {context.user_data.get('measurements', 'не указаны')}\n"
            f"⚖️ Вес: {context.user_data.get('weight', 'не указан')}\n"
            f"📍 Происхождение: {context.user_data.get('provenance', 'не указано')}\n"
            f"💬 Со слов продавца: {text}\n\n"
            "🔎 Статус атрибуции: не определено\n\n"
            "Фотографии и все зацепки тоже прошли по этапам.\n"
            "Карточка сохранена в базе Люси. 🗂✨"
        )
        return
    if (
        context.user_data.get("mode") == "identify"
        and context.user_data.get("step") == "mark_photo"
        and text and text.strip().lower() in ["🚫 клейма нет", "клейма нет", "нет клейма", "нет"]
     ):
        context.user_data["step"] = "details_photo"
        await update.message.reply_text(
                "Поняла — клейма нет. Это нормально. 👍\n\n"
                "Отсутствие клейма ещё ничего не говорит о возрасте "
                "или происхождении украшения.\n\n"
                "Теперь пришли крупное фото застёжки, шарнира, креплений "
                "или других необычных деталей. 🔎"
        )
        return
    if text == "🔎 Определить украшение":
        context.user_data["mode"] = "identify"
        context.user_data["step"] = "front_photo"
        await update.message.reply_text(
            "🔎 ОПРЕДЕЛИТЬ УКРАШЕНИЕ\n\n"
            "Начинаем с лицевой стороны.\n\n"
            "📸 Пришли одно фото украшения целиком.\n\n"
            "💬 Люся: Маленький секрет хорошей фотографии — "
            "протри камеру телефона. Серьёзно. 😂\n"
            "Лучше всего снимать при дневном свете возле окна, без вспышки."
        )
    elif text == "📚 Найти информацию":
        context.user_data["mode"] = "search"
        await update.message.reply_text(
            "📚 Что хочешь найти?\n\n"
            "Напиши тему, материал, бренд, клеймо или технику — "
            "а я попробую привести тебя к нужному разделу школы. 🔎"
        )
    elif text == "🎓 Учиться":
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
            "но у меня работа такая. 😏"
         )
    elif text == "🗃 Моя шкатулка":
        items = get_items()

        if not items:
            await update.message.reply_text(
                "🗃 Твоя шкатулка пока пустая.\n\n"
                "Добавь первую находку через «🔎 Определить украшение»."
            )
            return

        keyboard = []

        for item_code, measurements, weight in items:
            keyboard.append([
            InlineKeyboardButton(
            f"💎 {item_code}",
            callback_data=f"item:{item_code}"
        )
            ])

        await update.message.reply_text(
    "🗃 МОЯ ШКАТУЛКА\n\n"
    "Выбери находку, чтобы открыть её карточку:",
    reply_markup=InlineKeyboardMarkup(keyboard)
)
        return
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
            "показать это людям и коллективно удивиться. 😏"
        )
    elif context.user_data.get("mode") == "find" and text not in sum(MAIN_MENU, []):
        query = text
        context.user_data["mode"] = None
        await update.message.reply_text(
            f"🔎 Приняла запрос:\n\n«{query}»\n\n"
            "Я запомнила, что именно мы ищем. "
            "Я посмотрю, что можно найти по этому запросу. 😈"
        )
    elif context.user_data.get("mode") == "search" and text not in sum(MAIN_MENU, []):
        query = text
        context.user_data["mode"] = None
        await update.message.reply_text(
            f"🔎 Ищу информацию по запросу: {query}\n\n"
            "Пока я учусь искать по базе школы. 😈"
        )
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
def main():
    init_db()
    seed_books()
    seed_knowledge()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("testknowledge", test_knowledge))
    app.add_handler(CallbackQueryHandler(open_item, pattern=r"^item:"))
    app.add_handler(CallbackQueryHandler(open_details, pattern=r"^details:"))
    app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )    
    
    app.add_handler(
        MessageHandler(filters.PHOTO, handle_message)
    )

    app.run_polling()


if __name__ == "__main__":
    main()
