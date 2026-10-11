"""Общие заготовки для тестов бота.

Тесты запускаются без Telegram, Railway и внешних сервисов:
    python -m unittest discover -s tests

Если какой-то библиотеки нет на компьютере (python-telegram-bot, PyMuPDF,
boto3, openai, flask), вместо неё подставляется простая заглушка.
Если библиотека установлена — используется настоящая.
"""

import importlib
import os
import sqlite3
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


class _Filter:
    def __and__(self, other):
        return self

    def __or__(self, other):
        return self

    def __invert__(self):
        return self

    def __getattr__(self, name):
        return _Filter()


class _Plain:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class _InlineKeyboardButton:
    def __init__(self, text, callback_data=None, url=None, **kwargs):
        self.text = text
        self.callback_data = callback_data
        self.url = url


class _InlineKeyboardMarkup:
    def __init__(self, inline_keyboard, **kwargs):
        self.inline_keyboard = inline_keyboard


class _ReplyKeyboardMarkup:
    def __init__(self, keyboard, **kwargs):
        self.keyboard = keyboard


class _Flask:
    def __init__(self, *args, **kwargs):
        pass

    def _decorator(self, *args, **kwargs):
        return lambda function: function

    get = post = route = _decorator

    def run(self, *args, **kwargs):
        pass


def _stub_missing_modules():
    def missing(name):
        try:
            importlib.import_module(name)
            return False
        except ImportError:
            return True

    if missing("telegram"):
        telegram = types.ModuleType("telegram")
        telegram.Update = _Plain
        telegram.InlineKeyboardButton = _InlineKeyboardButton
        telegram.InlineKeyboardMarkup = _InlineKeyboardMarkup
        telegram.ReplyKeyboardMarkup = _ReplyKeyboardMarkup
        ext = types.ModuleType("telegram.ext")
        for name in ("Application", "CommandHandler", "CallbackQueryHandler", "MessageHandler"):
            setattr(ext, name, _Plain)
        ext.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object)
        ext.filters = _Filter()
        sys.modules["telegram"] = telegram
        sys.modules["telegram.ext"] = ext

    if missing("fitz"):
        sys.modules["fitz"] = types.ModuleType("fitz")
    if missing("boto3"):
        sys.modules["boto3"] = types.ModuleType("boto3")
    if missing("openai"):
        openai = types.ModuleType("openai")

        class OpenAIError(Exception):
            pass

        class OpenAI:
            """Заглушка: тесты подменяют её своим поддельным клиентом."""

            def __init__(self, *args, **kwargs):
                raise OpenAIError("openai не установлен")

        class APIConnectionError(OpenAIError):
            pass

        class APITimeoutError(APIConnectionError):
            pass

        openai.OpenAIError = OpenAIError
        openai.APIConnectionError = APIConnectionError
        openai.APITimeoutError = APITimeoutError
        openai.OpenAI = OpenAI
        sys.modules["openai"] = openai
    if missing("flask"):
        flask = types.ModuleType("flask")
        flask.Flask = _Flask
        flask.jsonify = lambda *args, **kwargs: None
        flask.request = None
        flask.render_template_string = lambda *args, **kwargs: ""
        sys.modules["flask"] = flask


def load_bot():
    """Импортирует bot.py с временной базой данных."""
    _stub_missing_modules()
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
    os.environ["LUCY_ADMIN_ID"] = "999"
    os.environ["LUCY_AI_ENABLED"] = "0"
    temp_dir = tempfile.mkdtemp(prefix="lucy_test_")
    os.environ["DB_PATH"] = os.path.join(temp_dir, "test.db")

    bot = importlib.import_module("bot")
    bot.DB_PATH = os.environ["DB_PATH"]
    bot.init_db()
    return bot


def fresh_database(bot):
    temp_dir = tempfile.mkdtemp(prefix="lucy_test_")
    bot.DB_PATH = os.path.join(temp_dir, "test.db")
    bot.init_db()
    return bot.DB_PATH


def create_old_schema_item(db_path):
    """Карточка в старой базе — до появления новых колонок."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_code TEXT UNIQUE,
            measurements TEXT,
            weight TEXT,
            provenance TEXT,
            seller_story TEXT,
            attribution_status TEXT DEFAULT 'не определено',
            front_photo TEXT,
            back_photo TEXT,
            mark_photo TEXT,
            details_photo TEXT,
            owner_user_id INTEGER
        )
        """
    )
    conn.execute(
        """
        INSERT INTO items (item_code, measurements, weight, provenance,
                           seller_story, front_photo, mark_photo, owner_user_id)
        VALUES ('OMG-000001', '40 мм', '12 г', 'Vinted', 'бабушкина',
                'FRONT-OLD', 'MARK-OLD', 1)
        """
    )
    conn.commit()
    conn.close()


class FakeMessage:
    def __init__(self, chat, text=None, photo_id=None, media_group_id=None):
        self.chat = chat
        self.text = text
        self.photo = [types.SimpleNamespace(file_id=photo_id)] if photo_id else None
        self.media_group_id = media_group_id
        self.caption = None

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.chat.sent.append(("text", text, reply_markup))

    async def reply_photo(self, photo, caption=None, reply_markup=None, **kwargs):
        self.chat.sent.append(("photo", photo, caption))
        if reply_markup is not None:
            # Подпись и кнопки под фото — отдельной записью, чтобы работали texts()/buttons().
            self.chat.sent.append(("text", caption, reply_markup))

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.chat.sent.append(("text", text, reply_markup))


class FakeChat:
    """Личный чат одной пользовательницы с ботом."""

    def __init__(self, bot, user_id):
        self.bot = bot
        self.user_id = user_id
        self.sent = []

        async def _noop(*args, **kwargs):
            return None

        self.tasks = []
        self.context = types.SimpleNamespace(
            user_data={},
            bot_data={},
            args=[],
            bot=types.SimpleNamespace(send_chat_action=_noop, send_message=_noop),
            # Фоновые задачи складываются сюда; тест запускает их сам.
            application=types.SimpleNamespace(create_task=self.tasks.append),
        )

    async def run_tasks(self):
        start = len(self.sent)
        while self.tasks:
            await self.tasks.pop(0)
        return self.sent[start:]

    def _update(self, message=None, callback_query=None):
        return types.SimpleNamespace(
            message=message,
            callback_query=callback_query,
            effective_message=message,
            effective_user=types.SimpleNamespace(id=self.user_id),
            effective_chat=types.SimpleNamespace(id=self.user_id),
        )

    async def say(self, text):
        start = len(self.sent)
        await self.bot.handle_message(
            self._update(message=FakeMessage(self, text=text)), self.context
        )
        return self.sent[start:]

    async def photo(self, file_id, media_group_id=None):
        start = len(self.sent)
        await self.bot.handle_message(
            self._update(message=FakeMessage(self, photo_id=file_id, media_group_id=media_group_id)),
            self.context,
        )
        return self.sent[start:]

    async def press(self, data):
        start = len(self.sent)

        async def answer(*args, **kwargs):
            return None

        query = types.SimpleNamespace(data=data, answer=answer, message=FakeMessage(self))
        update = self._update(callback_query=query)
        prefix = data.split(":", 1)[0]
        if prefix == "item":
            await self.bot.open_item(update, self.context)
        elif prefix == "details":
            await self.bot.open_details(update, self.context)
        elif prefix in ("edit", "edone", "ef", "ep"):
            await self.bot.edit_callback(update, self.context)
        elif prefix in ("del", "delok", "delno"):
            await self.bot.delete_callback(update, self.context)
        elif prefix == "box":
            await self.bot.box_callback(update, self.context)
        elif prefix in ("qskip", "qmark", "qmok", "qmtype", "qmskip", "qmore"):
            await self.bot.quick_callback(update, self.context)
        elif prefix in ("vpre", "vattr", "vsrc", "vsave"):
            await self.bot.attribution_callback(update, self.context)
        elif prefix == "bk":
            await self.bot.books_callback(update, self.context)
        elif prefix == "lorig":
            await self.bot.library_original_callback(update, self.context)
        else:
            raise ValueError(data)
        return self.sent[start:]


def texts(replies):
    return [content for kind, content, _ in replies if kind == "text"]


def photos(replies):
    return [content for kind, content, _ in replies if kind == "photo"]


def buttons(replies):
    """callback_data всех inline-кнопок в последнем ответе с клавиатурой."""
    for kind, _, markup in reversed(replies):
        if markup is not None and hasattr(markup, "inline_keyboard"):
            return [button.callback_data for row in markup.inline_keyboard for button in row]
    return []
