import os
import sqlite3

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
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
    conn.commit()
    conn.close()
    
def save_item(measurements, weight, provenance, seller_story):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO items (
            measurements,
            weight,
            provenance,
            seller_story
        )
        VALUES (?, ?, ?, ?)
        """,
        (measurements, weight, provenance, seller_story)
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
               seller_story, attribution_status
        FROM items
        WHERE item_code = ?
        """,
        (item_code,)
    )

    item = cursor.fetchone()
    conn.close()

    return item
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
                context.user_data["step"] = "back_photo"
                await update.message.reply_text(
                    "Фото лицевой стороны получила. 📸✨\n\n"
                    "Теперь переверни украшение и пришли фото оборота целиком.\n\n"
                    "💬 Люся: Не обрезай застёжку и края — сзади иногда интереснее, чем спереди. 😏"
                )
                return
        if step == "back_photo":
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
            text
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

        message = "🗃 МОЯ ШКАТУЛКА\n\n"

        for item_code, measurements, weight in items:
            message += (
                f"💎 {item_code}\n"
                f"📏 {measurements}\n"
                f"⚖️ {weight}\n\n"
            )

        await update.message.reply_text(message)
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
def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )    
    
    app.add_handler(
        MessageHandler(filters.PHOTO, handle_message)
    )

    app.run_polling()


if __name__ == "__main__":
    main()
