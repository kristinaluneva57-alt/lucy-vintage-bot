"""Интернет-поиск: Tavily через поддельный HTTP (настоящих запросов нет).

Запуск: python -m unittest discover -s tests
"""

import json
import os
import unittest
from unittest import mock

from helpers import load_bot

load_bot()
import web_search  # noqa: E402


class FakePost:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, payload, headers, timeout):
        self.calls.append((url, payload, headers))
        status, body = self.responses.pop(0)
        if isinstance(status, Exception):
            raise status
        return status, json.dumps(body) if not isinstance(body, str) else body


RESULTS = {"results": [
    {"title": "Sarah Coventry marks", "url": "https://www.illusionjewels.com/sc", "content": "SARAH COV. mark", "score": 0.9},
    {"title": "no url"},
]}


class TavilyTest(unittest.TestCase):
    def test_request_and_parsing(self):
        post = FakePost([(200, RESULTS), (200, RESULTS)])
        provider = web_search.TavilyProvider("tvly-test", post=post)
        found = provider.search(
            ["Sarah Coventry brooch", {"query": "Sarah Coventry auction sold", "include_domains": ["invaluable.com"]}],
            depth="advanced", per_query=4,
        )
        self.assertEqual(len(found), 2)  # результат без url пропущен
        self.assertEqual(found[0]["url"], "https://www.illusionjewels.com/sc")
        self.assertEqual(found[1]["query"], "Sarah Coventry auction sold")
        url, payload, headers = post.calls[0]
        self.assertEqual(url, "https://api.tavily.com/search")
        self.assertEqual(headers["Authorization"], "Bearer tvly-test")
        self.assertEqual(payload["search_depth"], "advanced")
        self.assertEqual(payload["max_results"], 4)
        self.assertNotIn("include_domains", payload)
        self.assertEqual(post.calls[1][1]["include_domains"], ["invaluable.com"])
        self.assertEqual(provider.credits_for(["a", "b"], "advanced"), 4)
        self.assertEqual(provider.credits_for(["a", "b"], "basic"), 2)

    def test_bad_key_and_quota(self):
        provider = web_search.TavilyProvider("bad", post=FakePost([(401, {"detail": "x"})]))
        with self.assertRaises(web_search.SearchUnavailable) as caught:
            provider.search(["q"])
        self.assertEqual(caught.exception.reason, "auth")
        provider = web_search.TavilyProvider("k", post=FakePost([(432, {"detail": "x"})]))
        with self.assertRaises(web_search.SearchUnavailable) as caught:
            provider.search(["q"])
        self.assertEqual(caught.exception.reason, "quota")

    def test_partial_failures_keep_results(self):
        post = FakePost([(500, "oops"), (TimeoutError(), None), (200, RESULTS)])
        found = web_search.TavilyProvider("k", post=post).search(["a", "b", "c"])
        self.assertEqual(len(found), 1)
        post = FakePost([(500, "oops"), (500, "oops")])
        with self.assertRaises(web_search.SearchUnavailable):
            web_search.TavilyProvider("k", post=post).search(["a", "b"])

    def test_configuration(self):
        with mock.patch.dict(os.environ, {"TAVILY_API_KEY": "", "LUCY_WEB_SEARCH": "tavily"}):
            self.assertFalse(web_search.configured_provider().connected)
        with mock.patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-x", "LUCY_WEB_SEARCH": "tavily"}):
            self.assertEqual(web_search.configured_provider().name, "tavily")
        with mock.patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-x", "LUCY_WEB_SEARCH": "off"}):
            self.assertFalse(web_search.configured_provider().connected)

    def test_domain(self):
        self.assertEqual(web_search.domain_of("https://www.Etsy.com/listing/1"), "etsy.com")


if __name__ == "__main__":
    unittest.main()
