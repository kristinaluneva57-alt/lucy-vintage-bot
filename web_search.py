"""Интернет-поиск для исследования украшений.

Общий интерфейс Provider: search(queries, depth, per_query, include_domains)
→ список источников {url, title, content, domain, query, published}.
Каждый источник — то, что поисковый сервис действительно вернул: ИИ
получает их пронумерованными [W1], [W2]…, а код проверяет, что в ответе
нет других ссылок.

Без ключа поиска работает NullProvider: интернет не используется, и отчёт
честно пишет «интернет-поиск не подключён».
"""

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
    """Поставщик поиска по настройкам. В этой версии — только NullProvider."""
    return NullProvider()
