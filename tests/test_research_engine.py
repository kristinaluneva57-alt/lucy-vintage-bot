"""Lucy Research Engine: общий бюджет и проверяемое исследование украшения.

OpenAI и интернет-поиск подменяются поддельными — настоящих платных
запросов нет. Поддельная модель нарочно ошибается (несуществующая
страница, цена не из источника, «продано» с маркетплейса), чтобы
проверить, что код это вычищает.

Запуск: python -m unittest discover -s tests
"""

import io
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock

from helpers import FakeChat, FakeMessage, buttons, load_bot, texts

bot = load_bot()
import ai_ledger  # noqa: E402
import library_db  # noqa: E402
import library_importer  # noqa: E402
import lucy_ai  # noqa: E402
import access  # noqa: E402
import research_engine  # noqa: E402
import web_search  # noqa: E402

openai_module = sys.modules["openai"]
COVENTRY = "Sarah Coventry Jewelry: An Unauthorized Guide for Collectors"


def jpeg_bytes():
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 180, 90)).save(buffer, format="JPEG")
    return buffer.getvalue()


OBSERVATION = {
    "object_type": "брошь",
    "observations": [{"text": "золотистый металл, стразы в лапках", "photo": "front"}],
    "construction": "булавка с безопасным замком",
    "materials_visible": "похоже на позолоту и стекло",
    "mark_seen": "SARAH COV.",
    "cannot_tell": ["проба металла"],
    "library_terms": ["Sarah Coventry", "brooch"],
    "web_queries": ["Sarah Coventry brooch rhinestone", "Sarah Coventry mark"],
}

DRAFT = {
    "identification": {"summary": "Брошь Sarah Coventry", "type": "брошь", "maker": "Sarah Coventry",
                       "period": "1960-е", "materials": "позолота, стекло", "confidence": "high"},
    "basis": [{"text": "клеймо SARAH COV.", "refs": ["CARD"]},
              {"text": "стразы в лапках", "refs": ["PHOTO"]},
              {"text": "выдуманный признак", "refs": ["B99"]}],
    "visual": [{"text": "безопасный замок булавки", "photo": "back"}],
    "book_findings": [{"text": "Клеймо SARAH COV. ставилось на застёжку", "refs": ["B1"]},
                      {"text": "Факт со страницы, которой нет", "refs": ["B99"]}],
    "web_findings": [{"text": "Справочник клейм описывает SARAH COV.", "refs": ["W1"]},
                     {"text": "Утверждение без ссылки", "refs": []}],
    "analogs": [{"text": "Брошь той же линии", "similar": "форма", "different": "цвет камней", "refs": ["W2"]},
                {"text": "Аналог без источника", "refs": []}],
    "prices": [{"amount": "120", "currency": "USD", "kind": "sold", "text": "аукцион", "refs": ["W2"]},
               {"amount": "85", "currency": "USD", "kind": "sold", "text": "Etsy", "refs": ["W3"]},
               {"amount": "999", "currency": "USD", "kind": "sold", "text": "выдумка", "refs": ["W2"]}],
    "confirmed": [{"text": "Производитель Sarah Coventry", "refs": ["CARD", "B1"]},
                  {"text": "Период 1960-е", "refs": []}],
    "assumed": [{"text": "Линия «Royal Bouquet»", "refs": ["PHOTO"]}],
    "unknown": ["точный год"],
    "next_steps": ["сфотографировать застёжку крупно"],
}


class FakeOpenAI:
    calls = []
    error = None
    critic = {"unsupported": [], "overstated": [], "notes": []}

    def __init__(self, *args, **kwargs):
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        FakeOpenAI.calls.append(kwargs)
        if FakeOpenAI.error is not None:
            raise FakeOpenAI.error
        system = kwargs["messages"][0]["content"]
        if system == research_engine.OBSERVE_SYSTEM:
            answer = OBSERVATION
        elif system == research_engine.SYNTH_SYSTEM:
            answer = DRAFT
        elif system == research_engine.CRITIC_SYSTEM:
            answer = FakeOpenAI.critic
        else:
            answer = {"text": "ok"}
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=json.dumps(answer, ensure_ascii=False)))],
            usage=types.SimpleNamespace(prompt_tokens=20000, completion_tokens=2000),
        )


class FakeWeb(web_search.Provider):
    name = "fake"
    connected = True

    def __init__(self):
        self.calls = []

    def credits_for(self, queries, depth):
        return len(queries) * (2 if depth == "advanced" else 1)

    def search(self, queries, depth="basic", per_query=5, include_domains=None):
        self.calls.append((list(queries), depth))
        return [
            {"url": "https://www.illusionjewels.com/marks/sarah", "title": "Sarah Coventry marks",
             "content": "Sarah Coventry used the mark SARAH COV. from the 1950s.", "query": queries[0]},
            {"url": "https://www.liveauctioneers.com/item/1", "title": "Sarah Coventry brooch",
             "content": "Vintage Sarah Coventry rhinestone brooch. Sold for $120 on May 3.", "query": queries[0]},
            {"url": "https://www.etsy.com/listing/2", "title": "Sarah Coventry brooch for sale",
             "content": "Beautiful brooch, price $85, free shipping.", "query": queries[0]},
            {"url": "https://www.etsy.com/listing/2", "title": "duplicate", "content": "dup", "query": queries[0]},
        ]


class ResearchEngineTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        path = os.path.join(tempfile.mkdtemp(prefix="lucy_test_"), "test.db")
        bot.DB_PATH = path
        library_db.DB_PATH = path
        bot.init_db()
        library_db.init_library_db()
        research_engine.init_tables(path)
        access.init_tables(path)
        bot.attribution.init_tables(path)
        bot.visual_ai.init_tables(path)

        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO library_documents (id, document_code, title, storage_key, language) "
            "VALUES (7, 'DOC-000007', ?, 'books/telegram/a.pdf', 'eng')", (COVENTRY,),
        )
        text = ("Sarah Coventry brooches were sold at home parties. The mark SARAH COV. "
                "appears on the clasp of brooches made in the 1960s. ") * 5
        for page in (12, 13):
            conn.execute(
                "INSERT INTO library_pages (document_id, pdf_page, original_text, cleaned_text, "
                "extraction_status) VALUES (7, ?, ?, ?, 'extracted')",
                (page, text, library_importer.clean_ocr_text(text)),
            )
        conn.execute(
            "INSERT INTO items (item_code, owner_user_id, front_photo, back_photo, mark_photo, mark_text, "
            "notes, measurements) VALUES ('OMG-000006', 999, 'F1', 'B1', 'M1', 'SARAH COV.', "
            "'брошь со стразами', '52 × 38 мм')"
        )
        conn.execute(
            "INSERT INTO items (item_code, owner_user_id, front_photo, mark_text) "
            "VALUES ('OMG-000007', 1, 'F2', 'Trifari')"
        )
        conn.commit()
        conn.close()

        self.env = mock.patch.dict(os.environ, {
            "OPENAI_API_KEY": "sk-test",
            "LUCY_RESEARCH_ENABLED": "1",
            "LUCY_RESEARCH_MODEL": "gpt-4.1",
            "LUCY_AI_BUDGET_MONTH_USD": "11",
            "LUCY_PILOT_CODE": "omg2026",
            "LUCY_PILOT_MAX": "10",
            "LUCY_PILOT_RESEARCH_MONTH": "4",
            "LUCY_PILOT_DEEP_MONTH": "1",
        })
        self.env.start()
        self.patches = [
            mock.patch.object(openai_module, "OpenAI", FakeOpenAI, create=True),
            mock.patch.object(bot, "download_photo", mock.AsyncMock(return_value=jpeg_bytes())),
        ]
        self.web = FakeWeb()
        self.patches.append(mock.patch.object(web_search, "get_provider", lambda: self.web))
        for patch in self.patches:
            patch.start()
        FakeOpenAI.calls = []
        FakeOpenAI.error = None
        FakeOpenAI.critic = {"unsupported": [], "overstated": [], "notes": []}
        ai_ledger.notifier = None
        self.admin = FakeChat(bot, 999)
        self.user = FakeChat(bot, 1)

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.env.stop()
        ai_ledger.notifier = None

    def query(self, sql, *args):
        conn = sqlite3.connect(bot.DB_PATH)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def last_research(self):
        return research_engine.get(bot.DB_PATH, self.query("SELECT MAX(id) FROM item_research")[0][0])

    async def run_research(self, code="OMG-000006", mode=""):
        await self.admin.say(f"/research {code} {mode}".strip())
        record = self.last_research()
        await self.admin.press(f"rs:go:{record['id']}")
        return await self.admin.run_tasks()

    # --- общий бюджет ---

    def test_ledger_totals_all_journals_and_blocks_over_budget(self):
        path = bot.DB_PATH
        ai_ledger.record(path, provider="openai", kind="chat", model="gpt-4.1-mini", cost=4.0)
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS visual_ai_calls (status TEXT, cost_usd REAL, reserved_usd REAL, created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO visual_ai_calls (kind, model, status, cost_usd, reserved_usd, created_at) "
            "VALUES ('compare', 'gpt-4.1-mini', 'done', 3.0, 0, ?)", (ai_ledger._stamp(),),
        )
        conn.commit()
        conn.close()
        self.assertAlmostEqual(ai_ledger.month_total(path), 7.0)
        with self.assertRaises(ai_ledger.BudgetExceeded):
            ai_ledger.reserve(path, provider="openai", kind="x", estimate=4.5)
        ai_ledger.reserve(path, provider="openai", kind="x", estimate=3.5)

    def test_budget_alerts_once_per_threshold(self):
        sent = []
        ai_ledger.notifier = sent.append
        for _ in range(3):
            ai_ledger.record(bot.DB_PATH, provider="openai", kind="chat", cost=3.0)
        ai_ledger.record(bot.DB_PATH, provider="openai", kind="chat", cost=0.5)
        self.assertEqual(len(sent), 2)  # 50 % (6 $), затем сразу 80 % (9 $)
        ai_ledger.record(bot.DB_PATH, provider="openai", kind="chat", cost=2.0)
        self.assertEqual(len(sent), 3)
        self.assertIn("100 %", sent[-1])
        self.assertIn("остановлены", sent[-1])

    def test_unknown_model_is_priced_high(self):
        price, known = ai_ledger.model_price("gpt-9-unknown")
        self.assertFalse(known)
        self.assertEqual(price, ai_ledger.UNKNOWN_PRICE)
        with mock.patch.dict(os.environ, {"LUCY_MODEL_PRICES": '{"gpt-9-unknown": [1, 5]}'}):
            self.assertEqual(ai_ledger.model_price("gpt-9-unknown"), ((1.0, 5.0), True))

    @mock.patch.dict(os.environ, {"LUCY_AI_ENABLED": "1"})
    def test_free_chat_records_cost_and_stops_at_budget(self):
        lucy_ai.ask_openai([{"role": "user", "content": "привет"}], kind="chat")
        spent = self.query("SELECT kind, model, cost_usd FROM ai_spend")
        self.assertEqual(spent[0][:2], ("chat", "gpt-4.1-mini"))
        self.assertGreater(spent[0][2], 0)
        with mock.patch.dict(os.environ, {"LUCY_AI_BUDGET_MONTH_USD": "0.001"}):
            with self.assertRaises(lucy_ai.AIUnavailable) as caught:
                lucy_ai.ask_openai([{"role": "user", "content": "ещё"}])
        self.assertEqual(caught.exception.reason, "budget")

    # --- доступ и план ---

    async def test_only_admin_for_now(self):
        replies = await self.user.say("/research OMG-000007")
        self.assertIn("только администратору", texts(replies)[0])
        self.assertEqual(FakeOpenAI.calls, [])

    async def test_plan_shows_cost_and_waits_for_button(self):
        replies = await self.admin.say("/research OMG-000006")
        text = texts(replies)[0]
        record = self.last_research()
        self.assertIn("🔬 Исследование украшения · OMG-000006", text)
        self.assertIn("Стоимость: не больше ≈", text)
        self.assertIn("4 интернет-запросов", text)
        self.assertEqual(buttons(replies), [f"rs:go:{record['id']}", f"rs:no:{record['id']}"])
        await self.admin.run_tasks()
        self.assertEqual(FakeOpenAI.calls, [])
        self.assertEqual(record["status"], "planned")

    # --- исследование ---

    async def test_research_report_is_verified(self):
        before = self.query("SELECT * FROM library_pages ORDER BY id")
        replies = await self.run_research()
        text = "\n".join(texts(replies))
        record = self.last_research()
        self.assertEqual(record["status"], "done")
        self.assertEqual(len(FakeOpenAI.calls), 2)  # наблюдение + синтез
        self.assertEqual(len(self.web.calls), 1)
        self.assertIsNotNone(FakeOpenAI.calls[0]["messages"][1]["content"][2]["image_url"])  # фото ушли модели

        self.assertIn("🧾 Предварительно: Брошь Sarah Coventry", text)
        self.assertIn("📷 Что модель рассмотрела на фото (клеймо, лицо, оборот)", text)
        self.assertIn("Клеймо SARAH COV. ставилось на застёжку [B1]", text)
        self.assertNotIn("B99", text)
        self.assertNotIn("Факт со страницы, которой нет", text)
        self.assertNotIn("Утверждение без ссылки", text)
        self.assertNotIn("Аналог без источника", text)
        self.assertIn("отличается: цвет камней", text)
        self.assertIn("120 USD — продажа на аукционе", text)
        self.assertIn("85 USD — цена объявления", text)  # Etsy — не продажа
        self.assertNotIn("999", text)  # суммы нет в источнике
        self.assertIn("Период 1960-е", text.split("🤔 Предполагается:")[1])  # без источника — предположение
        self.assertIn("— Клеймо со слов владелицы: SARAH COV. (карточка)", text)
        self.assertIn(f"[B1] {COVENTRY} (DOC-000007) — PDF-стр.", text)
        self.assertIn("[W2] Sarah Coventry brooch — https://www.liveauctioneers.com/item/1", text)
        self.assertEqual(text.count("https://www.etsy.com/listing/2"), 1)  # дубль ссылки убран
        self.assertIn("поиска по картинкам нет", text)
        self.assertIn("удалено утверждений без источника: 5, понижено до предположения: 2", text)

        metrics = record["result"]["metrics"]
        self.assertEqual(metrics["removed"], 5)
        self.assertNotIn("  (", text)
        self.assertEqual(self.query("SELECT * FROM library_pages ORDER BY id"), before)
        spend = self.query("SELECT provider, kind, status, ref FROM ai_spend ORDER BY id")
        self.assertEqual([s[1] for s in spend], ["research_observe", "web_search", "research_synthesis"])
        self.assertTrue(all(s[2] == "done" and s[3] == f"research:{record['id']}" for s in spend))
        self.assertGreater(record["cost_usd"], 0)

    async def test_double_tap_runs_once(self):
        await self.admin.say("/research OMG-000006")
        record = self.last_research()
        await self.admin.press(f"rs:go:{record['id']}")
        replies = await self.admin.press(f"rs:go:{record['id']}")
        self.assertIn("повторно не запускаю", texts(replies)[0])
        await self.admin.run_tasks()
        self.assertEqual(len(FakeOpenAI.calls), 2)

    async def test_same_data_is_free_next_time(self):
        await self.run_research()
        calls = len(FakeOpenAI.calls)
        replies = await self.admin.say("/research OMG-000006")
        self.assertIn("уже готово", texts(replies)[0])
        self.assertEqual(len(FakeOpenAI.calls), calls)

    async def test_deep_uses_critic(self):
        FakeOpenAI.critic = {"unsupported": ["c4"], "overstated": ["c5"], "notes": ["сверено"]}
        replies = await self.run_research(mode="deep")
        text = "\n".join(texts(replies))
        self.assertEqual(len(FakeOpenAI.calls), 3)
        self.assertEqual(self.web.calls[0][1], "advanced")
        queries = self.web.calls[0][0]
        self.assertEqual(len(queries), 6)  # 2 из наблюдения + клеймо + 3 целевых; лимит 8 не добивается выдумками
        targeted = [q for q in queries if isinstance(q, dict)]
        self.assertEqual([q["target"] for q in targeted], ["auction", "museum", "reference"])
        self.assertIn("liveauctioneers.com", targeted[0]["include_domains"])
        self.assertEqual(targeted[0]["query"], "SARAH COV. jewelry auction sold")
        self.assertIn("целевые: аукционы, музеи, справочники клейм", text)
        self.assertIn("🧭 Глубокая атрибуция", text)
        self.assertIn("замечания проверки: сверено", text)
        self.assertEqual(self.last_research()["result"]["metrics"]["removed"], 6)  # 5 + критик

    async def test_without_web_provider(self):
        self.web.connected = False
        replies = await self.run_research()
        text = "\n".join(texts(replies))
        self.assertIn("интернет-поиск не подключён", text)
        self.assertNotIn("https://", text)
        self.assertEqual(self.web.calls, [])

    async def test_search_error_is_reported(self):
        def broken(*args, **kwargs):
            raise web_search.SearchUnavailable("auth", "Tavily: неверный ключ (HTTP 401)")

        self.web.search = broken
        text = "\n".join(texts(await self.run_research()))
        self.assertIn("⚠️ интернет-поиск не сработал: Tavily: неверный ключ (HTTP 401)", text)
        self.assertEqual(self.last_research()["status"], "done")  # книги и фото всё равно
        failed = self.query("SELECT status FROM ai_spend WHERE kind = 'web_search'")
        self.assertEqual(failed, [("failed",)])  # неудачный поиск не считается расходом

    async def test_budget_stops_research(self):
        with mock.patch.dict(os.environ, {"LUCY_AI_BUDGET_MONTH_USD": "0.0001"}):
            replies = await self.run_research()
        self.assertIn("исчерпан бюджет ИИ", "\n".join(texts(replies)))
        self.assertEqual(FakeOpenAI.calls, [])
        self.assertEqual(self.last_research()["status"], "error")

    async def test_ai_error_shows_safe_details_to_admin(self):
        error = openai_module.OpenAIError("сбой sk-SECRET-KEY")
        error.code = "model_not_found"
        error.status_code = 404
        FakeOpenAI.error = error
        text = "\n".join(texts(await self.run_research()))
        self.assertIn("ИИ не ответил (OpenAIError · HTTP 404 · code=model_not_found)", text)
        self.assertNotIn("SECRET", text)

    # --- карточка шкатулки ---

    async def test_preview_offers_new_modes_to_allowed_only(self):
        replies = await self.admin.press("vpre:OMG-000006")
        self.assertIn("rs:new:OMG-000006:research", buttons(replies))
        self.assertIn("rs:new:OMG-000006:deep", buttons(replies))
        self.assertIn("vattr:OMG-000006", buttons(replies))  # старое исследование на месте
        self.assertIn("исследование Люси", texts(replies)[0])
        replies = await self.user.press("vpre:OMG-000007")
        self.assertNotIn("rs:new:OMG-000007:research", buttons(replies))
        replies = await self.user.press("rs:new:OMG-000007:research")
        self.assertIn("только в пилоте", texts(replies)[0])

    async def test_card_button_starts_plan(self):
        replies = await self.admin.press("rs:new:OMG-000006:deep")
        self.assertIn("🧭 Глубокая атрибуция · OMG-000006", texts(replies)[0])
        self.assertEqual(FakeOpenAI.calls, [])

    async def test_card_shows_research_and_saves_status(self):
        await self.run_research()
        record = self.last_research()
        replies = await self.admin.press("item:OMG-000006")
        card = "\n".join(texts(replies))
        self.assertIn("🔬 Исследование Люси: Брошь Sarah Coventry (уверенность: высокая)", card)
        self.assertIn(f"rs:open:{record['id']}", buttons(replies))

        replies = await self.admin.press(f"rs:open:{record['id']}")
        self.assertIn("🧾 Предварительно: Брошь Sarah Coventry", texts(replies)[0])
        self.assertIn(f"rs:save:{record['id']}", buttons(replies))

        replies = await self.user.press(f"rs:save:{record['id']}")
        self.assertIn("не твоё", texts(replies)[0])
        replies = await self.admin.press(f"rs:save:{record['id']}")
        self.assertIn("Вывод записан", texts(replies)[0])
        status = self.query("SELECT attribution_status FROM items WHERE item_code = 'OMG-000006'")[0][0]
        self.assertIn("Брошь Sarah Coventry — предварительно, уверенность высокая", status)
        self.assertEqual(len(FakeOpenAI.calls), 2)  # открыть и записать — бесплатно

    # --- пилот и лимиты ---

    async def join(self, chat, code="omg2026"):
        chat.context.args = [f"pilot-{code}"]
        await chat.bot.start(chat._update(message=FakeMessage(chat, text="/start")), chat.context)
        chat.context.args = []
        return texts(chat.sent[-2:])

    async def test_pilot_invite(self):
        self.assertIn("не подходит", (await self.join(self.user, "wrong"))[0])
        self.assertEqual(access.role_of(bot.DB_PATH, 1), "member")
        self.assertIn("Добро пожаловать в пилот", (await self.join(self.user))[0])
        self.assertEqual(access.role_of(bot.DB_PATH, 1), "pilot")
        self.assertIn("уже в пилоте", (await self.join(self.user))[0])
        with mock.patch.dict(os.environ, {"LUCY_PILOT_MAX": "1"}):
            self.assertIn("заняты", (await self.join(FakeChat(bot, 2)))[0])
        with mock.patch.dict(os.environ, {"LUCY_PILOT_CODE": ""}):
            self.assertIn("закрыт", (await self.join(FakeChat(bot, 3)))[0])

    async def test_pilot_can_research_within_limit(self):
        await self.join(self.user)
        replies = await self.user.press("vpre:OMG-000007")
        self.assertIn("rs:new:OMG-000007:research", buttons(replies))
        replies = await self.user.press("rs:new:OMG-000007:research")
        self.assertIn("Осталось в этом месяце: 4 из 4", texts(replies)[0])
        record = self.last_research()
        await self.user.press(f"rs:go:{record['id']}")
        await self.user.run_tasks()
        self.assertEqual(self.last_research()["status"], "done")
        self.assertEqual(access.used_this_month(bot.DB_PATH, 1, "research"), 1)

        # Те же данные — из кэша, лимит не тратится.
        replies = await self.user.press("rs:new:OMG-000007:research")
        self.assertIn("уже готово", texts(replies)[0])
        self.assertEqual(access.used_this_month(bot.DB_PATH, 1, "research"), 1)

    async def test_pilot_limit_blocks(self):
        await self.join(self.user)
        with mock.patch.dict(os.environ, {"LUCY_PILOT_DEEP_MONTH": "1"}):
            await self.user.press("rs:new:OMG-000007:deep")
            first = self.last_research()
            await self.user.press(f"rs:go:{first['id']}")
            await self.user.run_tasks()
            conn = sqlite3.connect(bot.DB_PATH)
            conn.execute("UPDATE items SET notes = 'новое описание' WHERE item_code = 'OMG-000007'")
            conn.commit()
            conn.close()
            calls = len(FakeOpenAI.calls)
            replies = await self.user.press("rs:new:OMG-000007:deep")
            self.assertIn("Лимит на этот месяц исчерпан (1 из 1)", texts(replies)[0])
            self.assertEqual(len(FakeOpenAI.calls), calls)

    async def test_member_and_admin_access(self):
        replies = await self.user.press("rs:new:OMG-000007:research")
        self.assertIn("только в пилоте", texts(replies)[0])
        replies = await self.admin.say("/research OMG-000006")
        self.assertNotIn("Осталось в этом месяце", texts(replies)[0])  # у администратора нет личного лимита

    async def test_pilot_admin_command(self):
        await self.join(self.user)
        text = texts(await self.admin.say("/pilot"))[0]
        self.assertIn("ПИЛОТ ЛЮСИ: 1 из 10", text)
        self.assertIn("?start=pilot-omg2026", text)
        self.assertIn("• 1: исследований 0, глубоких 0", text)
        self.assertIn("только администратору", texts(await self.user.say("/pilot"))[0])
        self.assertIn("выведена", texts(await self.admin.say("/pilot remove 1"))[0])
        self.assertEqual(access.role_of(bot.DB_PATH, 1), "member")

    # --- испытания ---

    async def test_trial_summary_and_sheet(self):
        async def reply_document(message, document, filename=None, **kwargs):
            message.chat.sent.append(("document", (filename, document.getvalue().decode("utf-8")), None))

        await self.run_research()
        calls = len(FakeOpenAI.calls)
        with mock.patch.object(FakeMessage, "reply_document", reply_document, create=True):
            replies = await self.admin.say("/trial OMG-000006 OMG-000007")
        self.assertEqual(len(FakeOpenAI.calls), calls)  # сводка ничего не запускает
        text = "\n".join(texts(replies))
        self.assertIn("💎 OMG-000006", text)
        self.assertIn("• старая: не запускалась", text)
        self.assertIn("• исследование: Брошь Sarah Coventry · уверенность высокая · книг 1, сайтов 3, "
                      "аналогов 1, продаж 1 · удалено без источника 5, понижено 2", text)
        self.assertIn("• глубокая: не запускалась (/research OMG-000006 deep)", text)
        self.assertIn("• исследование: 0.1360 $ (по 1)", text)
        files = [c for k, c, _ in replies if k == "document"]
        name, sheet = files[0]
        self.assertEqual(name, "trial_results.tsv")
        header = sheet.splitlines()[0].split("\t")
        self.assertIn("правильность 0-2", header)
        self.assertEqual(len(sheet.splitlines()), 1 + 2 * 3)
        self.assertIn("только администратору", texts(await self.user.say("/trial OMG-000007"))[0])

    async def test_foreign_card_cannot_be_researched(self):
        replies = await self.admin.say("/research OMG-000007")
        self.assertIn("только свою карточку", texts(replies)[0])


if __name__ == "__main__":
    unittest.main()
