import os
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

MAIN_MENU = [
    ["🔎 Определить украшение", "📚 Найти информацию"],
    ["🆘 Найди мне это", "🎓 Учиться"],
    ["🧭 Разделы школы", "💬 Vintage Club"],
]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = ReplyKeyboardMarkup(
        MAIN_MENU,
        resize_keyboard=True
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

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()
    
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

if __name__ == "__main__":
    main()
