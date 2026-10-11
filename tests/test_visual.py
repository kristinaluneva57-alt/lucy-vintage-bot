"""Визуальная атрибуция по всей библиотеке.

Модель библиотеки: DOC-000007 «Sarah Coventry Jewelry» (182 страницы) с
каталожной таблицей (PDF 54) и газетной рекламой (PDF 120), книга о Trifari
и книга Jet. Изображения страниц рисуются программно (Pillow); мелкий текст
рекламы читается НАСТОЯЩИМ Tesseract из вырезки 400 dpi.
OpenAI подменён поддельным клиентом с заданным расходом токенов —
настоящих платных запросов нет.

Запуск: python -m unittest discover -s tests
"""

import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock

from PIL import Image, ImageDraw, ImageFont

from helpers import FakeChat, buttons, load_bot, photos, texts

bot = load_bot()
import attribution  # noqa: E402
import library_db  # noqa: E402
import library_importer  # noqa: E402
import page_images  # noqa: E402
import visual_ai  # noqa: E402
import visual_index  # noqa: E402

openai_module = sys.modules["openai"]
HAS_TESSERACT = shutil.which("tesseract") is not None

OWNER, STRANGER, ADMIN = 1, 2, 999

AD_LINES = [
    "THE CRIMSON CASCADE NECKLACE",
    "BY SARAH COVENTRY  1965",
    "SILVERTONE WITH RED STONES",
    "ONLY $6.00 AT YOUR PARTY",
]

# OCR страниц в том виде, в каком его сохранил обычный импорт (с ошибками).
SC_PAGES = {
    54: "Plate 54\nSarah Coventry necklace with matching earrings,\nfrom the 1964 catalogue.\nThe Y-shaped necklace has five red stones.",
    120: "Sarah Coventry advertisement\nTHE CR1MS0N CASC4DE NECKLACE\nBY SARAH COVENTRY 1985,\nSRVERTONE WITH RED STONES\nONLY $6.00 AT YOURPARTY",
    3: "Sarah Coventry Jewelry\nA collector's guide\nCopyright 2005. All rights reserved. ISBN 0-000-00000-0",
}
TRIFARI_PAGES = {
    12: "Fig. 12 Trifari necklace with red glass cabochons, 1958.",
}
JET_PAGES = {
    5: "Jet is a fossilised wood. Whitby jet brooches and necklaces were popular mourning jewellery.",
}

# Высокое разрешение (400 dpi), из которого «рендерятся» страницы.
HIRES = (2400, 3200)


def draw_necklace(draw, x, y, scale=1.0):
    """Y-образное колье с пятью красными овалами."""
    draw.line((x, y, x + 300 * scale, y + 250 * scale), fill="gray", width=8)
    draw.line((x + 600 * scale, y, x + 300 * scale, y + 250 * scale), fill="gray", width=8)
    draw.line((x + 300 * scale, y + 250 * scale, x + 300 * scale, y + 650 * scale), fill="gray", width=8)
    for i in range(5):
        cy = y + (260 + i * 90) * scale
        draw.ellipse((x + 270 * scale, cy, x + 330 * scale, cy + 70 * scale), fill=(128, 0, 32))


def make_page(document_code, pdf_page, readable_ad=True):
    image = Image.new("RGB", HIRES, "white")
    draw = ImageDraw.Draw(image)
    if (document_code, pdf_page) == ("DOC-000007", 120):
        draw_necklace(draw, 900, 400)  # клетки B1–B2
        if readable_ad:
            font = ImageFont.load_default(size=20)
            for i, line in enumerate(AD_LINES):
                draw.text((900, 2300 + i * 32), line, fill="black", font=font)  # клетка B3
    elif (document_code, pdf_page) == ("DOC-000007", 54):
        draw_necklace(draw, 200, 300, 0.8)
        draw.ellipse((1500, 400, 1600, 520), fill=(128, 0, 32))
    elif (document_code, pdf_page) == ("DOC-000010", 12):
        draw.ellipse((800, 800, 1600, 1200), outline="gold", width=12)
        for i in range(3):
            draw.ellipse((900 + i * 250, 1100, 1000 + i * 250, 1200), fill=(150, 0, 0))
    return image


def jpeg(image, quality=85):
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def user_photo(kind):
    image = Image.new("RGB", (1600, 1200), (235, 230, 225))
    draw = ImageDraw.Draw(image)
    if kind == "mark":
        draw.text((500, 550), "SARAH COVENTRY", fill="black", font=ImageFont.load_default(size=60))
    else:
        draw_necklace(draw, 500, 150, 1.4 if kind == "front" else 1.2)
    return jpeg(image)


class JsonModeError(openai_module.OpenAIError):
    """Ошибка, которую OpenAI возвращает при JSON-режиме без слова JSON."""

    status_code = 400
    type = "invalid_request_error"
    code = None
    param = "messages"

    def __init__(self):
        Exception.__init__(self)
        self.message = (
            "'messages' must contain the word 'json' in some form, to use "
            "'response_format' of type 'json_object'."
        )


class FakeOpenAI:
    """Поддельный OpenAI: отвечает по типу запроса, считает токены."""

    calls = []
    error = None
    fail_kind = None
    compare_override = None
    read_answer = {"text": "", "legible": False, "set_name": None, "years": [], "unclear": "всё"}
    USAGE = {"describe": (5200, 420), "compare": (11800, 900), "read_text": (3100, 150)}

    def __init__(self, *args, **kwargs):
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    @staticmethod
    def kind(system):
        if system == attribution.DESCRIBE_SYSTEM:
            return "describe"
        if system == attribution.COMPARE_SYSTEM:
            return "compare"
        return "read_text"

    def _create(self, **kwargs):
        system = kwargs["messages"][0]["content"]
        content = kwargs["messages"][1]["content"]
        kind = self.kind(system)
        FakeOpenAI.calls.append({"kind": kind, "content": content, **kwargs})
        # Как настоящий OpenAI: JSON-режим без слова «JSON» в сообщениях — ошибка 400.
        if (kwargs.get("response_format") or {}).get("type") == "json_object":
            texts_lower = (system + " ".join(p.get("text", "") for p in content)).lower()
            if "json" not in texts_lower:
                raise JsonModeError()
        if FakeOpenAI.fail_kind == kind:
            raise JsonModeError()
        if FakeOpenAI.error is not None:
            raise FakeOpenAI.error

        if kind == "describe":
            answer = {
                "type_ru": "колье",
                "description_ru": "Серебристое Y-образное колье с пятью бордово-красными овальными вставками.",
                "stones": [{"count": 5, "shape": "oval", "colour": "dark red"}],
                "metal_tone": "silver tone",
                "construction": ["Y-shaped drop"],
                "marks": [{"text": "SARAH COVENTRY", "legible": True}],
                "search_terms": ["necklace", "Y necklace", "red stones", "silvertone", "collana"],
            }
        elif kind == "compare":
            labels = {}
            for part in content:
                if part.get("type") == "text":
                    match = re.match(r"(P\d+): (DOC-\d+), PDF page (\d+)", part["text"])
                    if match:
                        labels[(match.group(2), int(match.group(3)))] = match.group(1)
            pages = []
            for (code, page), label in labels.items():
                if (code, page) == ("DOC-000007", 120):
                    pages.append({
                        "label": label, "verdict": "exact",
                        "matching_features": ["Y-образная конструкция", "пять овальных красных вставок"],
                        "differences": [], "piece_cell": "B2", "text_cell": "B3",
                        "is_advertisement": True, "visible_text": "", "text_legible": False,
                        "set_name": "Crimson Cascade", "model_name": None,
                        "set_items": ["серьги"],
                        "dates": [{"year": 1965, "kind": "advertisement", "basis": "дата в рекламе"}],
                        "confidence": 0.9,
                    })
                elif (code, page) == ("DOC-000007", 54):
                    pages.append({
                        "label": label, "verdict": "probable",
                        "matching_features": ["Y-образная форма"], "differences": ["число камней плохо видно"],
                        "piece_cell": "A1", "text_cell": None, "is_advertisement": False,
                        "visible_text": "", "text_legible": True,
                        "set_name": "Ruby Dream",  # выдумка: такого текста на странице нет
                        "model_name": None, "set_items": [],
                        "dates": [{"year": 1964, "kind": "catalog", "basis": "подпись"}],
                        "confidence": 0.6,
                    })
                else:
                    pages.append({"label": label, "verdict": "stylistic", "matching_features": ["красные камни"],
                                  "differences": ["другая форма"], "confidence": 0.2})
            answer = {"pages": pages, "summary_ru": "Найдена реклама с тем же колье."}
            if FakeOpenAI.compare_override is not None:
                answer = FakeOpenAI.compare_override(labels)
        else:
            answer = FakeOpenAI.read_answer

        tokens_in, tokens_out = self.USAGE[kind]
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=json.dumps(answer)))],
            usage=types.SimpleNamespace(prompt_tokens=tokens_in, completion_tokens=tokens_out),
        )


def add_book(conn, doc_id, title, pages, total_pages=None, language="eng"):
    code = f"DOC-{doc_id:06d}"
    conn.execute(
        "INSERT INTO library_documents (id, document_code, title, meta_title, storage_key, language, checksum) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (doc_id, code, f"file{doc_id}", title, f"books/browser/{doc_id}.pdf", language, f"h{doc_id}"),
    )
    for number in range(1, (total_pages or max(pages)) + 1):
        text = pages.get(number, f"Page {number}. General text about costume jewellery history and collecting.")
        conn.execute(
            "INSERT INTO library_pages (document_id, pdf_page, original_text, cleaned_text) VALUES (?, ?, ?, ?)",
            (doc_id, number, text, library_importer.clean_ocr_text(text)),
        )
    conn.execute(
        "INSERT INTO library_import_jobs (document_id, status, last_completed_page) VALUES (?, 'completed', ?)",
        (doc_id, total_pages or max(pages)),
    )


class VisualAttributionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        path = os.path.join(tempfile.mkdtemp(prefix="lucy_test_"), "test.db")
        bot.DB_PATH = path
        library_db.DB_PATH = path
        bot.init_db()
        library_db.init_library_db()
        attribution.init_tables(path)

        conn = sqlite3.connect(path)
        add_book(conn, 7, "Sarah Coventry Jewelry", SC_PAGES, total_pages=182)
        add_book(conn, 10, "Trifari: A Collector's Guide", TRIFARI_PAGES, total_pages=40)
        add_book(conn, 2, "Jet Jewellery and Ornaments", JET_PAGES, total_pages=30)
        conn.execute(
            "INSERT INTO items (item_code, measurements, weight, provenance, seller_story, "
            "front_photo, back_photo, mark_photo, owner_user_id, mark_text, notes) VALUES "
            "('OMG-000001', '45 см', '30 г', 'Flohmarkt', 'нет', 'F1', 'B1', 'M1', ?, 'SARAH COVENTRY', "
            "'Y-образное колье, пять красных овальных вставок')",
            (OWNER,),
        )
        conn.execute(
            "INSERT INTO items (item_code, front_photo, mark_photo, owner_user_id, mark_text) VALUES "
            "('OMG-000002', 'F9', 'M9', ?, 'SARAH COVENTRY')",
            (ADMIN,),
        )
        conn.commit()
        conn.close()
        for doc_id in (7, 10, 2):
            visual_index.index_document(path, doc_id)

        # Хранилище R2 в памяти и рендер нарисованных страниц.
        self.r2 = {}
        self.renders = []
        self.readable_ad = True
        patches = [
            mock.patch.object(page_images, "r2_get", lambda key: self.r2.get(key)),
            mock.patch.object(page_images, "r2_put", lambda key, data: self.r2.__setitem__(key, data)),
            mock.patch.object(page_images, "_render", self.fake_render),
            mock.patch.object(openai_module, "OpenAI", FakeOpenAI, create=True),
            mock.patch.dict(os.environ, {
                "OPENAI_API_KEY": "sk-test", "LUCY_VISUAL_MODE": "all",
                "LUCY_VISUAL_FREE_PER_MONTH": "3", "LUCY_VISUAL_BUDGET_DAY_USD": "1",
                "LUCY_VISUAL_BUDGET_MONTH_USD": "10",
            }),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        FakeOpenAI.calls = []
        FakeOpenAI.error = None
        FakeOpenAI.fail_kind = None
        FakeOpenAI.compare_override = None
        FakeOpenAI.read_answer = {"text": "", "legible": False, "set_name": None, "years": [], "unclear": "всё"}

        self.owner = self.chat(OWNER)
        self.admin = self.chat(ADMIN)
        self.stranger = self.chat(STRANGER)

    def fake_render(self, document_code, storage_key, pdf_page, dpi, clip=None):
        self.renders.append((document_code, pdf_page, dpi, clip))
        image = make_page(document_code, pdf_page, self.readable_ad)
        if clip:
            x0, y0, x1, y1 = clip
            image = image.crop((int(x0 * HIRES[0]), int(y0 * HIRES[1]), int(x1 * HIRES[0]), int(y1 * HIRES[1])))
        scale = dpi / 400
        image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))))
        return jpeg(image)

    def chat(self, user_id):
        chat = FakeChat(bot, user_id)

        async def get_file(file_id):
            kind = {"F": "front", "B": "back", "M": "mark"}.get(file_id[0], "front")

            async def download_as_bytearray():
                return bytearray(user_photo(kind) + file_id.encode())

            return types.SimpleNamespace(download_as_bytearray=download_as_bytearray)

        chat.context.bot.get_file = get_file
        return chat

    def library_snapshot(self):
        conn = sqlite3.connect(bot.DB_PATH)
        try:
            return conn.execute(
                "SELECT document_id, pdf_page, original_text, cleaned_text FROM library_pages ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    async def research(self, chat=None, code="OMG-000001"):
        chat = chat or self.owner
        intro = await chat.press(f"vattr:{code}")
        result = await chat.run_tasks()
        return intro, result

    # --- индекс ---

    def test_index_features(self):
        ad = visual_index.page_features(SC_PAGES[120])
        self.assertGreaterEqual(ad["ad_score"], 0.5)
        self.assertIn("sarah coventry", ad["brands"])
        plate = visual_index.page_features(SC_PAGES[54])
        self.assertIn(1964, plate["years"])
        self.assertTrue(plate["captions"])
        stats = visual_index.index_stats(bot.DB_PATH)
        self.assertEqual(stats["indexed"], 3)
        self.assertEqual(stats["pages"], 182 + 40 + 30)

    def test_candidates_come_from_whole_library(self):
        results = visual_index.search_candidates(
            bot.DB_PATH, ["necklace", "red stones"], ["sarah coventry"], limit=8
        )
        top = [(r["document_code"], r["pdf_page"]) for r in results[:2]]
        self.assertEqual(set(top), {("DOC-000007", 120), ("DOC-000007", 54)})
        self.assertIn("DOC-000010", {r["document_code"] for r in results})  # другая книга
        self.assertIn("DOC-000002", {r["document_code"] for r in results})
        self.assertNotIn(3, [r["pdf_page"] for r in results if r["document_code"] == "DOC-000007"][:2])

    def test_books_to_index_and_reindex(self):
        self.assertEqual(visual_index.books_to_index(bot.DB_PATH), [])
        conn = sqlite3.connect(bot.DB_PATH)
        conn.execute("INSERT INTO library_pages (document_id, pdf_page, original_text) VALUES (2, 31, 'new page')")
        conn.commit()
        conn.close()
        self.assertEqual(visual_index.books_to_index(bot.DB_PATH), [(2, "DOC-000002")])

    def test_new_book_is_indexed_after_ocr(self):
        def fake_download(key, path):
            with open(path, "wb") as file:
                file.write(b"another pdf")

        document = types.SimpleNamespace(
            load_page=lambda i: types.SimpleNamespace(get_text=lambda *a, **k: "Sarah Coventry necklace ad 1966 $5"),
            close=lambda: None,
        )
        with mock.patch.object(library_importer, "download_pdf_from_r2", fake_download), \
                mock.patch.object(library_importer, "inspect_pdf", lambda p: {"page_count": 2}), \
                mock.patch.object(library_importer.fitz, "open", lambda *a, **k: document, create=True), \
                mock.patch.object(visual_index, "_image_coverage", lambda p: (None, False)):
            result = library_importer.import_pdf_with_ocr("books/telegram/n.pdf", "new", "eng")
        conn = sqlite3.connect(bot.DB_PATH)
        rows = conn.execute(
            "SELECT COUNT(*) FROM visual_page_index WHERE document_id = ?", (result["document_id"],)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(rows, 2)

    # --- изображения и мелкий текст ---

    @unittest.skipUnless(HAS_TESSERACT, "нужен tesseract")
    def test_crop_reads_small_ad_text_that_page_ocr_garbles(self):
        page = page_images.page_image("DOC-000007", "k", 120, dpi=120)
        crop = page_images.crop_image("DOC-000007", "k", 120, "B3")
        self.assertGreater(page_images.image_size(crop)[0], 900)
        page_text = page_images.tesseract_text(page)
        crop_text = page_images.tesseract_text(crop)
        self.assertIn("CRIMSON CASCADE", crop_text)
        self.assertIn("1965", crop_text)
        self.assertNotIn("1965", page_text)  # на целой странице год читается с ошибкой
        # повтор — из кэша R2, без нового рендера
        renders = len(self.renders)
        page_images.crop_image("DOC-000007", "k", 120, "B3")
        self.assertEqual(len(self.renders), renders)

    def test_cell_box(self):
        self.assertEqual(page_images.cell_box("A1", margin=0), (0.0, 0.0, 1 / 3, 1 / 3))
        with self.assertRaises(ValueError):
            page_images.cell_box("D4")

    # --- полное исследование ---

    @unittest.skipUnless(HAS_TESSERACT, "нужен tesseract")
    async def test_full_research_sarah_coventry(self):
        before = self.library_snapshot()
        intro, replies = await self.research()
        self.assertIn("Начинаю исследование", texts(intro)[0])
        report = texts(replies)[0]

        self.assertIn("Серебристое Y-образное колье", report)
        self.assertIn("Производитель: Sarah Coventry (по клейму на украшении)", report)
        self.assertIn("Комплект: «Crimson Cascade» — напечатано в источнике [1]", report)
        self.assertIn("1965 — год рекламы", report)
        self.assertIn("✅ точное совпадение модели", report)
        self.assertIn("[1] DOC-000007 · Sarah Coventry Jewelry · PDF-стр. 120", report)
        self.assertIn("прочитан: Tesseract", report)
        self.assertIn("год производства", report.split("Не подтверждено")[1])
        self.assertIn("Материалы (упомянуты в тексте источника): silvertone", report)
        self.assertIn("В комплект, по источнику, входят: серьги", report)

        # Выдуманное название на другой странице не попало в подтверждённые.
        record = attribution.get_attribution(bot.DB_PATH, 1)
        plate = next(e for e in record["result"]["evidence"] if e["pdf_page"] == 54)
        self.assertEqual(plate["set_name"], {"value": "Ruby Dream", "confirmed": False})
        self.assertNotIn("Ruby Dream", report)

        # Модели ушло не больше 4 страниц и 2 фото украшения, а не вся библиотека.
        compare = next(c for c in FakeOpenAI.calls if c["kind"] == "compare")
        images = [p for p in compare["content"] if p["type"] == "image_url"]
        self.assertLessEqual(len(images), 2 + attribution.MAX_COMPARE_PAGES)
        self.assertEqual([c["kind"] for c in FakeOpenAI.calls], ["describe", "compare"])  # Tesseract справился

        self.assertEqual(self.library_snapshot(), before)
        self.assertIn("vsave:1", buttons(replies))
        self.assertIn("vsrc:1:1", buttons(replies))

    @unittest.skipUnless(HAS_TESSERACT, "нужен tesseract")
    async def test_costs_recorded_and_stats(self):
        await self.research(self.admin, "OMG-000002")
        rows = visual_ai.calls_for(bot.DB_PATH, 1)
        self.assertEqual([r[0] for r in rows], ["describe", "compare"])
        expected = (5200 * 0.40 + 420 * 1.60 + 11800 * 0.40 + 900 * 1.60) / 1e6
        self.assertAlmostEqual(sum(r[4] for r in rows), expected, places=8)

        breakdown = texts(self.admin.sent)[-1]
        self.assertIn("Расходы этой атрибуции", breakdown)
        self.assertIn("вход 5200", breakdown)
        self.assertIn(f"Итого: {expected:.5f} $ за 2 вызов(а)", breakdown)

        stats = texts(await self.admin.say("/vstats"))[0]
        self.assertIn(f"Месяц: {expected:.4f} $ оплачено + 0.0000 $ возможно + 0.0000 $ в работе", stats)
        self.assertIn("Атрибуций с ИИ за месяц: 1", stats)
        self.assertIn(f"средняя стоимость: {expected:.4f} $", stats)
        self.assertIn("запросов на атрибуцию: 2.0", stats)

    @unittest.skipUnless(HAS_TESSERACT, "нужен tesseract")
    async def test_unreadable_ad_uses_ai_read_and_says_so(self):
        self.readable_ad = False
        _, replies = await self.research()
        report = texts(replies)[0]
        kinds = [c["kind"] for c in FakeOpenAI.calls]
        self.assertEqual(kinds.count("read_text"), 1)
        self.assertIn("мелкий текст рекламы/подписи прочитать не удалось", report)
        self.assertIn("«Crimson Cascade» — не найдено в тексте страницы", report)
        self.assertNotIn("Комплект: «Crimson Cascade» — напечатано", report)

    async def test_cache_reuses_result_for_free(self):
        await self.research()
        calls = len(FakeOpenAI.calls)
        _, replies = await self.research()
        self.assertEqual(len(FakeOpenAI.calls), calls)
        self.assertIn("без новых расходов", texts(replies)[0])
        self.assertEqual(attribution.paid_this_month(bot.DB_PATH, OWNER), 1)

    async def test_monthly_free_quota_then_free_search(self):
        os.environ["LUCY_VISUAL_FREE_PER_MONTH"] = "1"
        await self.research()
        conn = sqlite3.connect(bot.DB_PATH)
        conn.execute("UPDATE items SET front_photo = 'F2' WHERE item_code = 'OMG-000001'")  # новые фото — не кэш
        conn.commit()
        conn.close()
        calls = len(FakeOpenAI.calls)
        intro, replies = await self.research()
        self.assertIn("закончились", texts(intro)[0])
        self.assertEqual(len(FakeOpenAI.calls), calls)
        report = texts(replies)[0]
        self.assertIn("ПОИСК ПО КНИГАМ (без ИИ-сравнения)", report)
        self.assertIn("DOC-000007 · Sarah Coventry Jewelry", report)
        self.assertIn("vsrc:2:1", buttons(replies))

    async def test_admin_has_no_quota_but_budget(self):
        os.environ["LUCY_VISUAL_FREE_PER_MONTH"] = "0"
        _, replies = await self.research(self.admin, "OMG-000002")
        self.assertIn("ИССЛЕДОВАНИЕ ПО БИБЛИОТЕКЕ", texts(replies)[0])

    async def test_budget_checked_before_any_paid_call(self):
        os.environ["LUCY_VISUAL_BUDGET_DAY_USD"] = "0.001"
        _, replies = await self.research(self.admin, "OMG-000002")
        self.assertEqual(FakeOpenAI.calls, [])
        report = texts(replies)[0]
        self.assertIn("дневной бюджет", report)
        self.assertIn("ПОИСК ПО КНИГАМ", report)

    async def test_budget_stops_midway(self):
        # хватает на описание (~0.0034 $), но не на сравнение (~0.0089 $ по оценке)
        os.environ["LUCY_VISUAL_BUDGET_DAY_USD"] = "0.006"
        _, replies = await self.research()
        self.assertEqual([c["kind"] for c in FakeOpenAI.calls], ["describe"])
        report = texts(replies)[0]
        self.assertIn("ИССЛЕДОВАНИЕ НЕ ЗАВЕРШЕНО", report)
        self.assertIn("достигнут дневной бюджет", report)
        self.assertIn("Серебристое Y-образное колье", report)  # описание сохранено
        self.assertEqual(attribution.paid_this_month(bot.DB_PATH, OWNER), 0)  # попытка не списана

    async def test_api_error_falls_back_without_secrets(self):
        error = openai_module.OpenAIError("sk-SECRET")
        error.code = None
        FakeOpenAI.error = error
        _, replies = await self.research()
        report = texts(replies)[0]
        self.assertIn("ИИ сейчас недоступен", report)
        self.assertNotIn("SECRET", report)
        rows = visual_ai.calls_for(bot.DB_PATH, 1)
        self.assertEqual(rows[0][4], 0)  # неудачный вызов не стоит денег
        self.assertEqual(rows[0][5], 0)

    async def test_mode_switch(self):
        os.environ["LUCY_VISUAL_MODE"] = "off"
        intro, _ = await self.research()
        self.assertIn("выключено", texts(intro)[0])
        self.assertEqual(FakeOpenAI.calls, [])
        os.environ["LUCY_VISUAL_MODE"] = "admin"
        intro, _ = await self.research()
        self.assertIn("тестовом режиме", texts(intro)[0])
        _, replies = await self.research(self.admin, "OMG-000002")
        self.assertIn("ИССЛЕДОВАНИЕ", texts(replies)[0])

    async def test_only_style_match_is_not_attribution(self):
        FakeOpenAI.compare_override = lambda labels: {
            "pages": [{"label": label, "verdict": "stylistic", "matching_features": ["красные камни"]}
                      for label in labels.values()],
            "summary_ru": "Только похожие по стилю украшения.",
        }
        _, replies = await self.research()
        report = texts(replies)[0]
        self.assertIn("Точных или вероятных совпадений среди проверенных страниц нет", report)
        self.assertNotIn("Производитель:", report)
        self.assertNotIn("vsave:1", buttons(replies))

    # --- просмотр доказательств и сохранение ---

    @unittest.skipUnless(HAS_TESSERACT, "нужен tesseract")
    async def test_source_button_sends_page_and_crop(self):
        await self.research()
        replies = await self.owner.press("vsrc:1:1")
        kinds = [kind for kind, _, _ in replies]
        self.assertEqual(kinds, ["photo", "photo"])
        page, crop = photos(replies)
        self.assertEqual(page_images.image_size(page), (720, 960))  # страница 120 dpi
        self.assertIn("DOC-000007 · Sarah Coventry Jewelry · PDF-стр. 120", replies[0][2])
        self.assertIn("клетка B2", replies[0][2])
        self.assertIn("B3", replies[1][2])

        denied = await self.stranger.press("vsrc:1:1")
        self.assertIn("Не нашла", texts(denied)[0])

    @unittest.skipUnless(HAS_TESSERACT, "нужен tesseract")
    async def test_save_to_card(self):
        await self.research()
        denied = await self.stranger.press("vsave:1")
        self.assertIn("Сохранить нельзя", texts(denied)[0])
        replies = await self.owner.press("vsave:1")
        self.assertIn("Сохранила в карточку", texts(replies)[0])
        conn = sqlite3.connect(bot.DB_PATH)
        status = conn.execute("SELECT attribution_status FROM items WHERE item_code = 'OMG-000001'").fetchone()[0]
        conn.close()
        self.assertIn("Sarah Coventry", status)
        self.assertIn("«Crimson Cascade»", status)
        self.assertIn("PDF-стр. 120", status)
        card = texts(await self.owner.press("item:OMG-000001"))[-1]
        self.assertIn("Crimson Cascade", card)

    async def test_card_has_research_button_and_owner_only(self):
        replies = await self.owner.press("item:OMG-000001")
        self.assertIn("vattr:OMG-000001", buttons(replies))
        intro, _ = await self.research(self.stranger, "OMG-000001")
        self.assertIn("только свою карточку", texts(intro)[0])
        self.assertEqual(FakeOpenAI.calls, [])

    async def test_vindex_command(self):
        replies = await self.admin.say("/vindex")
        self.assertIn("Страниц в индексе: 252", texts(replies)[0])
        await self.admin.say("/vindex DOC-000007")
        replies = await self.admin.run_tasks()
        self.assertIn("DOC-000007 (182 стр.)", texts(replies)[0])
        self.assertIn("только администратору", texts(await self.owner.say("/vindex"))[0])



class SlowOpenAI(FakeOpenAI):
    """Как FakeOpenAI, но каждый ответ «думает» 0,2 с — для гонок."""

    def _create(self, **kwargs):
        import time

        time.sleep(0.2)
        return super()._create(**kwargs)


class NoResponseError(openai_module.APITimeoutError):
    def __init__(self):
        Exception.__init__(self, "timeout sk-SECRET")


class BudgetConcurrencyTest(unittest.IsolatedAsyncioTestCase):
    """Бюджет и месячный лимит при одновременных запросах."""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(prefix="lucy_test_"), "test.db")
        visual_ai.init_tables(self.path)
        patches = [
            mock.patch.object(openai_module, "OpenAI", SlowOpenAI, create=True),
            mock.patch.dict(os.environ, {
                "OPENAI_API_KEY": "sk-test",
                "LUCY_VISUAL_BUDGET_DAY_USD": "1",
                "LUCY_VISUAL_BUDGET_MONTH_USD": "10",
                "LUCY_VISUAL_MODE": "all",
                "LUCY_VISUAL_FREE_PER_MONTH": "1",
            }),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        FakeOpenAI.calls = []
        FakeOpenAI.error = None
        FakeOpenAI.fail_kind = None
        FakeOpenAI.read_answer = {"text": "x", "legible": False, "set_name": None, "years": [], "unclear": ""}

    def rows(self):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(
                "SELECT status, cost_usd, reserved_usd, input_tokens, output_tokens, error "
                "FROM visual_ai_calls ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    def read_call(self):
        """Платный вызов «чтение текста»: 2 картинки, ответ до 500 токенов."""
        return visual_ai.call_json(
            self.path, kind="read_text", system=attribution.READ_SYSTEM,
            content=[{"type": "text", "text": "Cropped area:"}], images=2, max_tokens=500,
        )

    def estimate(self):
        text = len(attribution.READ_SYSTEM) + len("Cropped area:")
        return visual_ai.estimate_cost(2, text, 500)

    def run_threads(self, target, count):
        import threading

        barrier = threading.Barrier(count)
        outcomes = []
        lock = threading.Lock()

        def worker():
            barrier.wait()
            try:
                value = target()
                outcome = ("ok", value)
            except Exception as error:  # noqa: BLE001
                outcome = (type(error).__name__, None)
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        return outcomes

    # --- резервирование ---

    def test_concurrent_reservations_never_exceed_budget(self):
        os.environ["LUCY_VISUAL_BUDGET_DAY_USD"] = "0.01"
        outcomes = self.run_threads(
            lambda: visual_ai.reserve(self.path, kind="compare", images=4, estimate=0.003), 20
        )
        self.assertEqual(sum(1 for kind, _ in outcomes if kind == "ok"), 3)
        self.assertEqual(sum(1 for kind, _ in outcomes if kind == "BudgetExceeded"), 17)
        self.assertLessEqual(visual_ai.reserved(self.path, "day"), 0.01)

    def test_concurrent_api_calls_respect_budget(self):
        estimate = self.estimate()
        os.environ["LUCY_VISUAL_BUDGET_DAY_USD"] = f"{estimate * 2 + estimate / 2:.8f}"
        outcomes = self.run_threads(self.read_call, 8)
        self.assertEqual(sum(1 for kind, _ in outcomes if kind == "ok"), 2)
        self.assertEqual(len(FakeOpenAI.calls), 2)  # остальные остановлены ДО запроса
        self.assertLessEqual(visual_ai.spent(self.path, "day"), visual_ai.budget_day())
        self.assertEqual(visual_ai.reserved(self.path, "day"), 0)

    def test_settle_replaces_reserve_with_actual_cost(self):
        self.read_call()
        status, cost_usd, reserved_usd, tokens_in, tokens_out, _ = self.rows()[0]
        self.assertEqual(status, "done")
        self.assertEqual(reserved_usd, 0)
        self.assertEqual((tokens_in, tokens_out), (3100, 150))
        self.assertAlmostEqual(cost_usd, (3100 * 0.40 + 150 * 1.60) / 1e6, places=10)

    def test_error_with_response_releases_reserve(self):
        error = openai_module.OpenAIError("bad request sk-SECRET")
        error.code = "invalid_request"
        FakeOpenAI.error = error
        with self.assertRaises(visual_ai.VisualAIUnavailable):
            self.read_call()
        status, cost_usd, reserved_usd, _, _, saved_error = self.rows()[0]
        self.assertEqual((status, cost_usd, reserved_usd), ("failed", 0, 0))
        self.assertIn("invalid_request", saved_error)
        self.assertNotIn("SECRET", saved_error)
        self.assertEqual(visual_ai.spent(self.path, "day"), 0)

    def test_no_response_counts_possible_cost(self):
        FakeOpenAI.error = NoResponseError()
        with self.assertRaises(visual_ai.VisualAIUnavailable):
            self.read_call()
        status, cost_usd, reserved_usd, _, _, saved_error = self.rows()[0]
        self.assertEqual(status, "unknown")
        self.assertAlmostEqual(cost_usd, self.estimate(), places=10)
        self.assertEqual(reserved_usd, 0)
        self.assertIn("ответ не получен", saved_error)
        self.assertNotIn("SECRET", saved_error)
        # Возможный расход учитывается в бюджете.
        self.assertAlmostEqual(visual_ai.spent(self.path, "day"), self.estimate(), places=10)
        os.environ["LUCY_VISUAL_BUDGET_DAY_USD"] = f"{self.estimate() * 1.5:.8f}"
        FakeOpenAI.error = None
        with self.assertRaises(visual_ai.BudgetExceeded):
            self.read_call()

    def test_stale_reservation_still_counts(self):
        call_id = visual_ai.reserve(self.path, kind="compare", images=4, estimate=0.004)
        conn = sqlite3.connect(self.path)
        conn.execute("UPDATE visual_ai_calls SET created_at = ? WHERE id = ?",
                     ((visual_ai._now() - __import__("datetime").timedelta(hours=1))
                      .isoformat(timespec="seconds"), call_id))
        conn.commit()
        conn.close()
        # Переход через полночь UTC не важен для теста: считаем месяц.
        stats = visual_ai.stats(self.path)
        self.assertEqual(self.rows()[0][0], "unknown")
        self.assertAlmostEqual(stats["month_possible"], 0.004)
        self.assertEqual(stats["month_reserved"], 0)

    async def test_vstats_shows_spent_possible_and_reserved(self):
        bot.DB_PATH = self.path
        attribution.init_tables(self.path)
        self.read_call()                                  # оплачено
        FakeOpenAI.error = NoResponseError()
        with self.assertRaises(visual_ai.VisualAIUnavailable):
            self.read_call()                              # возможно оплачено
        FakeOpenAI.error = None
        visual_ai.reserve(self.path, kind="compare", images=4, estimate=0.0123)  # в работе

        chat = FakeChat(bot, ADMIN)
        text = texts(await chat.say("/vstats"))[0]
        paid = (3100 * 0.40 + 150 * 1.60) / 1e6
        self.assertIn(f"Месяц: {paid:.4f} $ оплачено + {self.estimate():.4f} $ возможно + 0.0123 $ в работе", text)
        self.assertIn("без ответа (учтены как возможные расходы): 1", text)
        self.assertIn("выполняются сейчас: 1", text)
        self.assertIn("запросов: 3", text)

    # --- месячный лимит ---

    def test_parallel_starts_get_one_slot(self):
        attribution.init_tables(self.path)
        outcomes = self.run_threads(
            lambda: attribution.start_slot(self.path, OWNER, "OMG-000001", "h", False), 10
        )
        started = [value for kind, value in outcomes if kind == "ok"]
        self.assertEqual(len(started), 1)
        self.assertEqual(sum(1 for kind, _ in outcomes if kind == "AlreadyRunning"), 9)
        self.assertEqual(started[0][1], "ai")

    def test_running_slot_counts_toward_quota(self):
        attribution.init_tables(self.path)
        first_id, mode, _ = attribution.start_slot(self.path, OWNER, "OMG-000001", "h1", False)
        self.assertEqual(mode, "ai")
        # Первое исследование ещё идёт — лимит 1 уже занят.
        self.assertEqual(attribution.paid_this_month(self.path, OWNER), 1)
        attribution._finish(self.path, first_id, {}, "ai", paid=True)
        second_id, mode, reason = attribution.start_slot(self.path, OWNER, "OMG-000002", "h2", False)
        self.assertEqual(mode, "free")
        self.assertIn("закончились", reason)

    def test_aborted_attempt_returns_slot(self):
        attribution.init_tables(self.path)
        first_id, _, _ = attribution.start_slot(self.path, OWNER, "OMG-000001", "h1", False)
        attribution._finish(self.path, first_id, {}, "free", paid=False)  # бюджет оборвал
        _, mode, _ = attribution.start_slot(self.path, OWNER, "OMG-000002", "h2", False)
        self.assertEqual(mode, "ai")

    def test_admin_parallel_runs_also_serialized(self):
        attribution.init_tables(self.path)
        outcomes = self.run_threads(
            lambda: attribution.start_slot(self.path, ADMIN, "OMG-000002", "h", True), 5
        )
        self.assertEqual(sum(1 for kind, _ in outcomes if kind == "ok"), 1)


class ConcurrentBotRunsTest(unittest.IsolatedAsyncioTestCase):
    """Два нажатия «Исследовать» подряд: второе не обходит лимит."""

    setUp = VisualAttributionTest.setUp
    fake_render = VisualAttributionTest.fake_render
    chat = VisualAttributionTest.chat

    async def test_double_press_runs_once(self):
        os.environ["LUCY_VISUAL_FREE_PER_MONTH"] = "1"
        with mock.patch.object(openai_module, "OpenAI", SlowOpenAI, create=True):
            await self.owner.press("vattr:OMG-000001")
            await self.owner.press("vattr:OMG-000001")
            tasks = list(self.owner.tasks)
            self.owner.tasks.clear()
            start = len(self.owner.sent)
            import asyncio

            await asyncio.gather(*tasks)
        replies = texts(self.owner.sent[start:])
        self.assertEqual(sum(1 for r in replies if "уже идёт" in r), 1)
        self.assertEqual(sum(1 for r in replies if "ИССЛЕДОВАНИЕ ПО БИБЛИОТЕКЕ" in r), 1)
        self.assertEqual([c["kind"] for c in FakeOpenAI.calls].count("describe"), 1)
        self.assertEqual(attribution.paid_this_month(bot.DB_PATH, OWNER), 1)


if __name__ == "__main__":
    unittest.main()
