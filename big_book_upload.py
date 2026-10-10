
"""Private browser upload of large PDFs directly to Cloudflare R2."""

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import uuid
import urllib.parse
import urllib.request

import boto3
from flask import Flask, jsonify, request, render_template_string
from telegram import Update
from telegram.ext import ContextTypes

from library_importer import import_pdf_with_ocr


log = logging.getLogger(__name__)
web_app = Flask(__name__)

MAX_BYTES = 250 * 1024 * 1024
TOKEN_LIFETIME = 3600
sessions = {}
sessions_lock = threading.Lock()
import_lock = threading.Lock()


def secret():
    return os.environ["TELEGRAM_BOT_TOKEN"].encode()


def make_token(user_id):
    timestamp = str(int(time.time()))
    payload = f"{user_id}:{timestamp}"
    signature = hmac.new(
        secret(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()
    return f"{payload}:{signature}"


def verify_token(token):
    try:
        user_id, timestamp, signature = token.split(":")
        payload = f"{user_id}:{timestamp}"

        expected = hmac.new(
            secret(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(signature, expected):
            return None

        age = time.time() - int(timestamp)
        if age < 0 or age > TOKEN_LIFETIME:
            return None

        if user_id != os.environ.get("LUCY_ADMIN_ID", "").strip():
            return None

        return user_id
    except (ValueError, TypeError):
        return None


def r2_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto"
    )


def bucket():
    return os.environ["R2_BUCKET_NAME"]


def notify_admin(text):
    user_id = os.environ.get("LUCY_ADMIN_ID")
    if not user_id:
        return

    url = (
        "https://api.telegram.org/bot"
        + os.environ["TELEGRAM_BOT_TOKEN"]
        + "/sendMessage"
    )

    data = urllib.parse.urlencode({
        "chat_id": user_id,
        "text": text[:3900]
    }).encode()

    try:
        urllib.request.urlopen(
            urllib.request.Request(url, data=data),
            timeout=20
        ).close()
    except Exception:
        log.exception("Could not send import notification")


HTML = """
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport"
      content="width=device-width,initial-scale=1">
<title>Lucy — загрузка книги</title>
<style>
body {
  font-family: system-ui, sans-serif;
  max-width: 580px;
  margin: 35px auto;
  padding: 18px;
  background: #17151e;
  color: white;
}
h1 { color: #eac7ff; }
.panel {
  background: #292433;
  padding: 22px;
  border-radius: 16px;
}
input, button {
  box-sizing: border-box;
  width: 100%;
  margin: 12px 0;
  padding: 14px;
  border-radius: 10px;
}
button {
  background: #bd88f1;
  border: 0;
  font-weight: bold;
  cursor: pointer;
}
button:disabled { opacity: .5; }
#status { white-space: pre-wrap; line-height: 1.6; }
</style>
</head>
<body>
<h1>📚 Lucy Library</h1>
<div class="panel">
<p>Загрузка больших книг напрямую в библиотеку.</p>
<p>Максимальный размер: 250 МБ.</p>

<label for="language">Язык книги:</label>
<select id="language">
  <option value="eng">🇬🇧 Английский</option>
  <option value="ita">🇮🇹 Итальянский</option>
  <option value="deu">🇩🇪 Немецкий</option>
  <option value="fra">🇫🇷 Французский</option>
  <option value="ces">🇨🇿 Чешский</option>
  <option value="pol">🇵🇱 Польский</option>
</select>

<input type="file" id="file" accept=".pdf,application/pdf">
<button id="send" onclick="uploadBook()">Загрузить книгу</button>
<p id="status"></p>
</div>

<script>
const token = new URLSearchParams(location.search).get("token");
const statusBox = document.getElementById("status");
const button = document.getElementById("send");

function status(text) {
  statusBox.textContent = text;
}

async function api(path, data) {
  const response = await fetch(path + "?token=" +
    encodeURIComponent(token), {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(data)
    });

  const result = await response.json();
  if (!response.ok) {
    throw new Error(result.error || "Ошибка сервера");
  }
  return result;
}

async function uploadBook() {
  const file = document.getElementById("file").files[0];

  if (!file) {
    status("Выбери PDF.");
    return;
  }

  if (!file.name.toLowerCase().endsWith(".pdf")) {
    status("Нужен файл PDF.");
    return;
  }

  if (file.size > 250 * 1024 * 1024) {
    status("Файл больше 250 МБ.");
    return;
  }

  button.disabled = true;

  try {
    status("Подготавливаю загрузку...");

    
    const prepared = await api("/prepare", {
      name: file.name,
      size: file.size,
      language: document.getElementById("language").value
    });

      size: file.size
    });

    status("Загружаю PDF напрямую в R2. Не закрывай страницу...");

    const uploaded = await fetch(prepared.upload_url, {
      method: "PUT",
      headers: {"Content-Type": "application/pdf"},
      body: file
    });

    if (!uploaded.ok) {
      throw new Error(
        "R2 отклонил загрузку: " + uploaded.status
      );
    }

    status("PDF загружен. Передаю Люсе на обработку...");

    await api("/complete", {
      upload_id: prepared.upload_id
    });

    status(
      "✅ Книга передана Люсе!\\n" +
      "Результат обработки придёт в Telegram."
    );

  } catch (error) {
    status("❌ " + error.message);
  } finally {
    button.disabled = false;
  }
}
</script>
</body>
</html>
"""


@web_app.get("/")
def homepage():
    if not verify_token(request.args.get("token", "")):
        return "Ссылка недействительна или устарела.", 403

    return render_template_string(HTML)


@web_app.post("/prepare")
def prepare():
    user_id = verify_token(request.args.get("token", ""))

    if not user_id:
        return jsonify(error="Нет доступа"), 403

    data = request.get_json(silent=True) or {}
    name = str(data.get("name", ""))
    size = data.get("size")
    
    language = str(data.get("language", "eng"))

    allowed_languages = {
        "eng", "ita", "deu",
        "fra", "ces", "pol"
    }

    if language not in allowed_languages:
        return jsonify(error="Неизвестный язык книги"), 400


    if not name.lower().endswith(".pdf"):
        return jsonify(error="Нужен PDF"), 400

    if (
        type(size) is not int
        or size < 1
        or size > MAX_BYTES
    ):
        return jsonify(error="Неверный размер PDF"), 400

    upload_id = uuid.uuid4().hex
    key = f"books/browser/{upload_id}.pdf"

    url = r2_client().generate_presigned_url(
        "put_object",
        Params={
            "Bucket": bucket(),
            "Key": key,
            "ContentType": "application/pdf"
        },
        ExpiresIn=TOKEN_LIFETIME
    )

    with sessions_lock:
        sessions[upload_id] = {
            "key": key,
            "title": name[:-4][:200] or "Без названия",
            "size": size,
             "language": language,
            "user_id": user_id,
            "created": time.time(),
            "state": "prepared"
        }

    return jsonify(
        upload_id=upload_id,
        upload_url=url
    )


def process_book(key, title, language):
    try:
        result = import_pdf_with_ocr(
            storage_key=key,
            title=title,
            language=language
        )

        notify_admin(
            "✅ КНИГА В БИБЛИОТЕКЕ\n\n"
            f"📚 {title}\n"
            f"Документ: {result['document_code']}\n"
            f"Страниц: {result['total_pages']}\n"
            f"Обработано: {result['completed_pages']}\n"
            f"Статус: {result['status']}"
        )

    except Exception:
        log.exception("Large book import failed")
        notify_admin(
            f"❌ Ошибка обработки книги «{title}».\n"
            "Подробности находятся в Railway Logs."
        )
    finally:
        import_lock.release()


@web_app.post("/complete")
def complete():
    user_id = verify_token(request.args.get("token", ""))

    if not user_id:
        return jsonify(error="Нет доступа"), 403

    data = request.get_json(silent=True) or {}
    upload_id = data.get("upload_id", "")

    with sessions_lock:
        session = sessions.get(upload_id)

        if not session or session["user_id"] != user_id:
            return jsonify(error="Загрузка не найдена"), 404

        if session["state"] != "prepared":
            return jsonify(error="Уже обрабатывается"), 409

        if time.time() - session["created"] > TOKEN_LIFETIME:
            return jsonify(error="Время загрузки истекло"), 410

        if not import_lock.acquire(blocking=False):
            return jsonify(
                error="Люся уже обрабатывает другую большую книгу"
            ), 409

        session["state"] = "processing"

    try:
        info = r2_client().head_object(
            Bucket=bucket(),
            Key=session["key"]
        )

        if info["ContentLength"] != session["size"]:
            raise ValueError("Размер файла не совпадает")

        
        worker = threading.Thread(
            target=process_book,
            args=(
                session["key"],
                session["title"],
                session["language"]
            ),
            daemon=True
        )

        worker.start()

        return jsonify(status="processing")

    except Exception:
        with sessions_lock:
            session["state"] = "prepared"
        import_lock.release()
        log.exception("Upload verification failed")
        return jsonify(error="Не удалось проверить PDF в R2"), 400


@web_app.get("/health")
def health():
    return jsonify(status="ok")


def start_upload_server():
    port = int(os.environ.get("PORT", "8080"))

    thread = threading.Thread(
        target=lambda: web_app.run(
            host="0.0.0.0",
            port=port,
            debug=False,
            use_reloader=False
        ),
        daemon=True
    )
    thread.start()


async def bigbook_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user
    admin_id = os.environ.get("LUCY_ADMIN_ID", "").strip()

    if not user or str(user.id) != admin_id:
        await update.effective_message.reply_text(
            "🔒 Только для администратора."
        )
        return

    public_url = os.environ.get(
        "LUCY_PUBLIC_URL", ""
    ).rstrip("/")

    if not public_url:
        await update.effective_message.reply_text(
            "⚠️ Не настроен LUCY_PUBLIC_URL."
        )
        return

    token = make_token(user.id)

    link = (
        public_url
        + "/?token="
        + urllib.parse.quote(token, safe="")
    )

    await update.effective_message.reply_text(
        "📚 ЗАГРУЗКА БОЛЬШОЙ КНИГИ\n\n"
        "Открой личную ссылку:\n"
        + link
        + "\n\nСсылка действует 1 час.\n"
        "Максимальный размер: 250 МБ.\n"
        "Никому не пересылай эту ссылку."
    )
