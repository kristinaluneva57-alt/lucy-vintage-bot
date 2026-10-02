import os
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

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
    app.run_polling()

if __name__ == "__main__":
    main()
