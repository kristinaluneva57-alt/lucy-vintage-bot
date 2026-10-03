import os

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

MAIN_MENU = [
    ["🔎 Определить украшение", "📚 Найти информацию"],
    ["🆘 Найди мне это", "🎓 Учиться"],
    ["🧭 Разделы школы", "💬 Vintage Club"],
]


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

    if text == "🔎 Определить украшение":
        await update.message.reply_text(
            "🔎 Отлично. Начинаем исследование.\n\n"
            "Пришли мне фотографии украшения:\n"
            "1. 📸 Общий вид\n"
            "2. 🔄 Оборотную сторону\n"
            "3. 🔍 Клеймо крупным планом, если оно есть\n"
            "4. 🔗 Застёжку, крепления и необычные детали\n\n"
            "Не переживай, если не знаешь, что именно фотографировать — "
            "я буду вести тебя по шагам. 😈"
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
    elif text == "🆘 Найди мне это":
        context.user_data["mode"] = "find"
        await update.message.reply_text(
            "🆘 НАЙДИ МНЕ ЭТО\n\n"
            "Ищешь конкретное украшение, бренд, книгу, каталог или что-то ещё?\n\n"
            "Напиши мне, что именно нужно найти. Чем больше деталей — тем лучше. 🔎\n\n"
            "💬 Люся: Можно даже начать с «я не знаю, как эта зараза называется». Разберёмся. 😏"
        )

    elif context.user_data.get("mode") == "find" and text not in sum(MAIN_MENU, []):
        query = text
        context.user_data["mode"] = None
        await update.message.reply_text(
            f"🔎 Приняла запрос:\n\n«{query}»\n\n"
            "Я запомнила, что именно мы ищем. "
            "Сам настоящий поиск по источникам подключим следующим этапом. 😈"
        )
    elif context.user_data.get("mode") == "search" and text not in sum(MAIN_MENU, []):
        query = text
        context.user_data["mode"] = None
        await update.message.reply_text(
            f"🔎 Ищу информацию по запросу: {query}\n\n"
            "Пока я учусь искать по базе школы. 😈"
        )
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    app.run_polling()


if __name__ == "__main__":
    main()
