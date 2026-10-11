"""Загрузка книг через /bigbook: дубликаты только по SHA-256 содержимого.

Регрессия: два разных PDF с одинаковым именем («Book 5») и одинаковым
числом страниц (182) не должны считаться дублями. Сообщение о дубле
показывает название из каталога (в том числе исправленное через /settitle)
и безопасную диагностику без ссылок и секретов.

Запуск: python -m unittest discover -s tests
"""

import hashlib
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from helpers import load_bot

load_bot()  # заглушки telegram/fitz/boto3, если их нет
import big_book_upload  # noqa: E402
import library_catalog  # noqa: E402
import library_db  # noqa: E402
import library_importer  # noqa: E402

try:
    import flask  # noqa: F401
    HAS_FLASK = hasattr(flask.Flask("x"), "test_client")
except Exception:
    HAS_FLASK = False

SARAH = b"%PDF-1.4\n% Sarah Coventry Jewelry: An Unauthorized Guide\n" + b"S" * 5000
BAKELITE = b"%PDF-1.4\n% Bakelite Jewelry: Good, Better, Best\n" + b"B" * 7000
BOT_TOKEN = "123456:SECRET-BOT-TOKEN"


class FakeR2:
    """R2 в памяти: ключ → байты."""

    def __init__(self):
        self.objects = {}
        self.urls = {}

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        url = f"https://r2.example/{Params['Key']}?X-Amz-Signature=very-secret-{len(self.urls)}"
        self.urls[url] = Params["Key"]
        return url

    def put(self, url, data):  # то, что делает браузер: PUT по ссылке
        self.objects[self.urls[url]] = data

    def head_object(self, Bucket, Key):
        return {"ContentLength": len(self.objects[Key])}

    def download(self, key, path):
        with open(path, "wb") as file:
            file.write(self.objects[key])
        return path


class SyncThread:
    def __init__(self, target, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class BigBookTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lucy_bigbook_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        path = os.path.join(self.dir, "test.db")
        library_db.DB_PATH = path
        library_db.init_library_db()
        self.r2 = FakeR2()
        self.reports = []
        self.indexed = []
        patches = [
            mock.patch.object(library_importer, "download_pdf_from_r2", self.r2.download),
            # Оба PDF — по 182 страницы: число страниц не должно влиять на дубли.
            mock.patch.object(library_importer, "inspect_pdf", lambda path: {"page_count": 182}),
            mock.patch.object(library_importer, "_ocr_document", lambda *a, **k: None),
            mock.patch.object(library_importer, "_visual_index_safely",
                              lambda doc_id, path: self.indexed.append(doc_id)),
            mock.patch.object(big_book_upload, "notify_admin", self.reports.append),
            mock.patch.object(big_book_upload, "r2_client", lambda: self.r2),
            mock.patch.object(big_book_upload, "bucket", lambda: "bucket"),
            mock.patch.object(big_book_upload.threading, "Thread", SyncThread),
            mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": BOT_TOKEN, "LUCY_ADMIN_ID": "999"}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        big_book_upload.sessions.clear()

    def documents(self):
        conn = sqlite3.connect(library_db.DB_PATH)
        try:
            return conn.execute(
                "SELECT document_code, title, checksum, storage_key, meta_title, meta_author, meta_source "
                "FROM library_documents ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    def import_bytes(self, data, title, key):
        self.r2.objects[key] = data
        return library_importer.import_pdf_with_ocr(storage_key=key, title=title, language="eng")

    # --- дедупликация ---

    def test_different_pdfs_same_name_and_pages_are_not_duplicates(self):
        first = self.import_bytes(SARAH, "Book 5", "books/browser/a.pdf")
        second = self.import_bytes(BAKELITE, "Book 5", "books/browser/b.pdf")
        self.assertTrue(first["created"])
        self.assertTrue(second["created"])
        self.assertNotEqual(first["document_code"], second["document_code"])
        docs = self.documents()
        self.assertEqual(len(docs), 2)
        self.assertEqual(docs[0][2], hashlib.sha256(SARAH).hexdigest())
        self.assertEqual(docs[1][2], hashlib.sha256(BAKELITE).hexdigest())

    def test_same_bytes_under_other_name_is_duplicate(self):
        first = self.import_bytes(SARAH, "Sarah Coventry", "books/browser/a.pdf")
        again = self.import_bytes(SARAH, "Book 5", "books/browser/c.pdf")
        self.assertFalse(again["created"])
        self.assertEqual(again["document_code"], first["document_code"])
        docs = self.documents()
        self.assertEqual(len(docs), 1)
        # Существующий документ не изменён: имя, ключ R2, ID.
        self.assertEqual(docs[0][:4], ("DOC-000001", "Sarah Coventry",
                                       hashlib.sha256(SARAH).hexdigest(), "books/browser/a.pdf"))
        self.assertEqual(again["duplicate"]["file_title"], "Sarah Coventry")
        self.assertEqual(again["upload"]["sha256"], hashlib.sha256(SARAH).hexdigest())
        self.assertEqual(again["upload"]["size"], len(SARAH))

    def test_completed_duplicate_is_not_touched(self):
        self.import_bytes(SARAH, "Sarah", "books/browser/a.pdf")
        library_catalog.set_manual_metadata(
            "DOC-000001", "Sarah Coventry Jewelry: An Unauthorized Guide for Collectors",
            "Monica Lynn Clements, Patricia Rosser Clements",
        )
        before = self.documents()
        self.indexed.clear()
        with mock.patch.object(library_importer, "_detect_metadata_safely") as detect:
            self.import_bytes(SARAH, "Book 5", "books/browser/c.pdf")
            detect.assert_not_called()
        self.assertEqual(self.indexed, [])  # индекс не перестраивается
        self.assertEqual(self.documents(), before)

    # --- сообщение о дубле ---

    def test_duplicate_report_shows_saved_title_and_diagnostics(self):
        self.import_bytes(SARAH, "Sarah", "books/browser/a.pdf")
        library_catalog.set_manual_metadata(
            "DOC-000001", "Sarah Coventry Jewelry: An Unauthorized Guide for Collectors",
            "Monica Lynn Clements, Patricia Rosser Clements",
        )
        result = self.import_bytes(SARAH, "Book 5", "books/browser/c.pdf")
        text = big_book_upload.book_report(result, "Book 5", "eng", "Book 5.pdf", "abcdef0123456789")
        self.assertIn("УЖЕ ЕСТЬ В БИБЛИОТЕКЕ", text)
        self.assertIn("Название: Sarah Coventry Jewelry: An Unauthorized Guide for Collectors", text)
        self.assertIn("Автор: Monica Lynn Clements, Patricia Rosser Clements", text)
        self.assertNotIn("Название не определено", text)
        self.assertIn("Выбранный файл: Book 5.pdf", text)
        self.assertIn(f"SHA-256: {hashlib.sha256(SARAH).hexdigest()}", text)
        self.assertIn("Получено сервером: " + library_importer.format_size(len(SARAH)), text)
        self.assertIn("Сессия загрузки: abcdef01", text)
        self.assertNotIn("abcdef0123456789", text)
        self.assertIn("Дубликат: DOC-000001", text)
        self.assertIn("Исходное имя файла DOC-000001: «Sarah»", text)
        for secret in ("books/browser", "X-Amz", BOT_TOKEN, "token"):
            self.assertNotIn(secret, text)

    def test_new_book_report_has_no_duplicate_line(self):
        result = self.import_bytes(BAKELITE, "Book 5", "books/browser/b.pdf")
        text = big_book_upload.book_report(result, "Book 5", "eng", "Book 5.pdf", "1234567890")
        self.assertIn("✅ КНИГА В БИБЛИОТЕКЕ", text)
        self.assertNotIn("Дубликат", text)
        self.assertIn(f"SHA-256: {hashlib.sha256(BAKELITE).hexdigest()}", text)

    # --- весь путь: страница → /prepare → PUT в R2 → /complete ---

    def browser_upload(self, client, token, name, data):
        prepared = client.post(f"/prepare?token={token}",
                               json={"name": name, "size": len(data), "language": "eng"}).get_json()
        self.r2.put(prepared["upload_url"], data)
        response = client.post(f"/complete?token={token}", json={"upload_id": prepared["upload_id"]})
        return prepared, response

    @unittest.skipUnless(HAS_FLASK, "нужен Flask")
    def test_full_path_two_uploads_named_book5(self):
        client = big_book_upload.web_app.test_client()
        token = big_book_upload.make_token(999)
        first, response = self.browser_upload(client, token, "Book 5.pdf", SARAH)
        self.assertEqual(response.status_code, 200)
        second, response = self.browser_upload(client, token, "Book 5.pdf", BAKELITE)
        self.assertEqual(response.status_code, 200)

        # Каждая загрузка — своя сессия и свой объект в R2.
        self.assertNotEqual(first["upload_id"], second["upload_id"])
        keys = [doc[3] for doc in self.documents()]
        self.assertEqual(len(set(keys)), 2)
        # Сервер получил байты именно второго файла.
        self.assertEqual(self.r2.objects[keys[1]], BAKELITE)
        self.assertEqual(len(self.documents()), 2)
        self.assertIn("✅ КНИГА В БИБЛИОТЕКЕ", self.reports[1])
        self.assertIn(f"Сессия загрузки: {second['upload_id'][:8]}", self.reports[1])
        for report in self.reports:
            self.assertNotIn("X-Amz", report)
            self.assertNotIn(token, report)

    @unittest.skipUnless(HAS_FLASK, "нужен Flask")
    def test_full_path_same_file_twice_is_duplicate(self):
        client = big_book_upload.web_app.test_client()
        token = big_book_upload.make_token(999)
        self.browser_upload(client, token, "Sarah.pdf", SARAH)
        self.browser_upload(client, token, "Book 5.pdf", SARAH)
        self.assertEqual(len(self.documents()), 1)
        self.assertIn("УЖЕ ЕСТЬ В БИБЛИОТЕКЕ", self.reports[1])
        self.assertIn("Выбранный файл: Book 5.pdf", self.reports[1])
        self.assertIn("Дубликат: DOC-000001", self.reports[1])

    @unittest.skipUnless(HAS_FLASK, "нужен Flask")
    def test_upload_session_cannot_be_reused(self):
        client = big_book_upload.web_app.test_client()
        token = big_book_upload.make_token(999)
        prepared, _ = self.browser_upload(client, token, "Sarah.pdf", SARAH)
        again = client.post(f"/complete?token={token}", json={"upload_id": prepared["upload_id"]})
        self.assertEqual(again.status_code, 409)
        self.assertEqual(len(self.reports), 1)

    def test_page_shows_chosen_file_and_resets_selection(self):
        html = big_book_upload.HTML
        self.assertIn("Выбран файл: ", html)
        self.assertIn('document.getElementById("file").value = ""', html)


if __name__ == "__main__":
    unittest.main()
