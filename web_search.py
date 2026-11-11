"""Интернет-поиск для исследования украшений.

Общий интерфейс Provider: search(queries, depth, per_query, include_domains)
→ список источников {url, title, content, domain, query, published}.
Каждый источник — то, что поисковый сервис действительно вернул: ИИ
получает их пронумерованными [W1], [W2]…, а код проверяет, что в ответе
нет других ссылок.

Поставщик — Tavily (TAVILY_API_KEY). Без ключа работает NullProvider:
интернет не используется, и отчёт честно пишет «интернет-поиск не подключён».

Запрос — строка или {"query": ..., "include_domains": [...]} (целевой поиск
по аукционам, музеям, справочникам клейм).
"""

import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlparse


class SearchUnavailable(Exception):
    def __init__(self, reason, details=""):
        super().__init__(reason)
        self.reason = reason
        self.details = details


def domain_of(url):
    host = (urlparse(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


class Provider:
    name = "none"
    connected = False

    def credits_for(self, queries, depth):
        """Сколько единиц (кредитов) спишет поиск — для оценки и учёта."""
        return 0

    def search(self, queries, depth="basic", per_query=5, include_domains=None):
        return []


class NullProvider(Provider):
    """Интернет не подключён."""


def get_provider():
    """Поставщик поиска по настройкам (см. configured_provider ниже)."""
    return configured_provider()


# --- Tavily ---

TAVILY_URL = "https://api.tavily.com/search"
TAVILY_TIMEOUT = 25


def _post_json(url, payload, headers, timeout):
    """HTTP POST с JSON. Возвращает (статус, тело). Подменяется в тестах."""
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


class TavilyProvider(Provider):
    """Tavily Search API: текст найденных страниц, фильтр по доменам.

    Базовый поиск — 1 кредит, расширенный — 2. Ключ — переменная
    TAVILY_API_KEY (в Railway). Ключ и тексты запросов в логи не пишутся.
    """

    name = "tavily"
    connected = True

    def __init__(self, api_key, post=None):
        self.api_key = api_key
        self.post = post or _post_json

    def credits_for(self, queries, depth):
        return len(queries) * (2 if depth == "advanced" else 1)

    def search(self, queries, depth="basic", per_query=5, include_domains=None):
        results, errors = [], []
        for item in queries:
            query = item["query"] if isinstance(item, dict) else item
            domains = (item.get("include_domains") if isinstance(item, dict) else None) or include_domains
            payload = {
                "api_key": self.api_key,
                "query": str(query)[:400],
                "search_depth": "advanced" if depth == "advanced" else "basic",
                "max_results": int(per_query),
                "include_answer": False,
                "include_raw_content": False,
                "include_images": False,
            }
            if domains:
                payload["include_domains"] = sorted(domains)
            try:
                status, body = self.post(
                    TAVILY_URL, payload, {"Authorization": f"Bearer {self.api_key}"}, TAVILY_TIMEOUT
                )
            except Exception as error:  # сеть, таймаут
                errors.append(type(error).__name__)
                continue
            if status == 401:
                raise SearchUnavailable("auth", "Tavily: неверный ключ (HTTP 401)")
            if status in (429, 432, 433):
                raise SearchUnavailable("quota", f"Tavily: закончились кредиты или лимит (HTTP {status})")
            if status != 200:
                errors.append(f"HTTP {status}")
                continue
            try:
                data = json.loads(body)
            except ValueError:
                errors.append("не JSON")
                continue
            for result in data.get("results") or []:
                if isinstance(result, dict) and result.get("url"):
                    results.append({
                        "url": result.get("url"),
                        "title": result.get("title") or "",
                        "content": result.get("content") or "",
                        "query": query,
                        "published": result.get("published_date"),
                    })
        if errors:
            print(f"Tavily: ошибок {len(errors)} из {len(queries)}: {', '.join(errors[:3])}")
        if not results and errors and len(errors) == len(queries):
            raise SearchUnavailable("error", f"Tavily не ответил: {errors[0]}")
        return results


def configured_provider():
    """Tavily — только если поиск ЯВНО включён (LUCY_WEB_SEARCH=tavily) и задан ключ.

    Ноябрь 2026: интернет-поиск заморожен по решению Кристины (дорого для
    пилота). Код остаётся; по умолчанию поиск выключен, даже если в Railway
    лежит TAVILY_API_KEY. Включить: LUCY_WEB_SEARCH=tavily.
    """
    if os.environ.get("LUCY_WEB_SEARCH", "off").strip().lower() not in ("tavily", "on", "1", "yes"):
        return NullProvider()
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        return NullProvider()
    return TavilyProvider(key)
