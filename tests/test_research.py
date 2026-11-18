"""Research discovery and traceable source handoff without external network."""
import hashlib
import json
import socket
import unittest
import urllib.error
from unittest.mock import patch

from miniagent.config import Limits
from miniagent.research import Page, _public_socket, fetch
from miniagent.sandbox import Sandbox
from miniagent.tools import ToolContext, build_default_registry
from tests.support import cleanup, make_temp_dir


class ResearchTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("research")
        self.ctx = ToolContext(sandbox=Sandbox(self.root), limits=Limits())
        self.registry = build_default_registry()

    def tearDown(self):
        cleanup(self.root)

    def test_official_index_preserves_named_public_links_using_final_url_and_base(self):
        page = Page("https://campus.example/final/news/index", "text/html",
                    (b'<head><base href="../press/"></head><a href="/nav">Navigation</a>'
                     b'<article><a href="launch#section">Campus <strong>AI</strong> event</a>'
                     b'<a href="launch#repeat">Same article</a><a href="//partner.example/page">'
                     b'<img alt="Partner evidence"></a></article>'
                     b'<a href="javascript:alert(1)">JS</a><a href="http://127.0.0.1/x">Private</a>'
                     b'<a href="#here">Self</a><a href="https://u:p@example.org/">Credentials</a>'
                     b'<template><a href="dummy">Invisible</a></template>'), False)
        with patch("miniagent.research.fetch", return_value=page):
            result = self.registry.dispatch("http_get", {"url":"https://campus.example/start"}, self.ctx)
        self.assertTrue(result.ok, result.content)
        evidence = json.loads((self.root/result.meta["path"]).read_text())
        self.assertEqual(evidence["links"], [
            {"title":"Campus AI event", "url":"https://campus.example/final/press/launch"},
            {"title":"Partner evidence", "url":"https://partner.example/page"},
            {"title":"Navigation", "url":"https://campus.example/nav"}])
        self.assertFalse(evidence["links_truncated"])
        self.assertIn("https://campus.example/final/press/launch", result.content)
        self.assertEqual(evidence["observed_bytes_sha256"], hashlib.sha256(page.body).hexdigest())

    def test_link_index_is_bounded_and_primary_content_survives_a_long_menu(self):
        html = ''.join(f'<a href="/nav/{i}">Menu {i}</a>' for i in range(450))
        html += '<main><a href="/story">A recent campus announcement</a></main>'
        page = Page("https://campus.example/", "text/html", html.encode(), False)
        with patch("miniagent.research.fetch", return_value=page):
            result = self.registry.dispatch("http_get", {"url":page.url}, self.ctx)
        data = json.loads((self.root/result.meta["path"]).read_text())
        self.assertEqual(data["links"][0]["url"], "https://campus.example/story")
        self.assertTrue(data["links_truncated"])
        self.assertLessEqual(len(data["links"]), 160)
        self.assertLess(len(result.content), 6500)

    def test_news_discovery_fetch_and_handoff(self):
        article = "https://example.org/campus-event"
        news = {"articles": [{"title": "Campus news", "url": article, "seendate":"20261110T120000Z"},
                             {"title": "Unsafe", "url":"http://localhost/credentials"},
                             {"title": "Duplicate", "url": article}]}
        with patch("miniagent.research._service", return_value=Page("https://example.org/", "application/json",
                                                                   json.dumps(news).encode(), False)) as http:
            search = self.registry.dispatch("search_sources", {"query":"HKUST AI", "collection":"news"}, self.ctx)
        self.assertTrue(search.ok, search.content)
        self.assertEqual(search.meta["count"], 1)
        self.assertIn("query=HKUST+AI", http.call_args.args[0])
        record = json.loads((self.root / search.meta["path"]).read_text())
        self.assertEqual(record["results"][0]["indexed_at"], "20261110T120000Z")
        body = b"<html><head><title>Hidden</title></head><body>Evidence <script>hidden()</script> supported.</body></html>"
        with patch("miniagent.research.fetch", return_value=Page(article + "/final", "text/html", body, False)):
            fetched = self.registry.dispatch("http_get", {"url": article}, self.ctx)
        self.assertTrue(fetched.ok, fetched.content)
        record = json.loads((self.root / fetched.meta["path"]).read_text())
        self.assertEqual((record["requested_url"], record["url"]), (article, article+"/final"))
        self.assertIn("retrieved_at", record)
        self.assertEqual(record["observed_bytes_sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(record["text"], "Evidence\nsupported.")
        self.assertIn(fetched.meta["path"], fetched.content)

    def test_paper_metadata_and_bad_input(self):
        item = {"title":["Detector limitations"], "URL":"https://doi.org/10.1111/test", "published":{"date-parts":[[2025,6]]}}
        with patch("miniagent.research._service", return_value=Page("https://example.org", "application/json",
                     json.dumps({"message":{"items":[item]}}).encode(), False)) as http:
            result = self.registry.dispatch("search_sources", {"query":"detectors", "collection":"papers"}, self.ctx)
            self.assertTrue(result.ok)
            self.assertIn("2025-06", result.content)
            self.assertIn("api.crossref.org", http.call_args.args[0])
            for invalid in ({"query": " "}, {"query": "x","limit":11}, {"query":"x","collection":"private"}):
                self.assertFalse(self.registry.dispatch("search_sources",invalid,self.ctx).ok)
            http.assert_called_once()

    def test_rate_limit_falls_back_and_labels_aggregator_result(self):
        feed = (b'<rss><channel><item><title>Science</title><link>https://news.google.com/a</link>'
                b'<source url="https://example.org">Publisher</source></item></channel></rss>')
        limited = urllib.error.HTTPError("https://example.test", 429, "limited", {}, None)
        self.addCleanup(limited.close)
        with patch("miniagent.research._service", side_effect=[limited, Page("", "application/xml", feed, False)]):
            response = self.registry.dispatch("search_sources", {"query": "mathematics"}, self.ctx)
        self.assertTrue(response.ok, response.content)
        record = json.loads((self.root / response.meta["path"]).read_text())
        self.assertEqual(record["provider"], "Google News RSS")
        self.assertEqual(record["fallback_from"]["reason"], "HTTP 429")
        self.assertEqual(record["results"][0]["link_kind"], "aggregator")
        self.assertEqual(record["results"][0]["publisher_url"], "https://example.org")

    def test_proxy_synthetic_dns_validates_public_answer_before_connect(self):
        synthetic = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.1", 443))]
        for ip in ("10.10.10.10", "8.8.8.8"):
            resolver = Page("https://dns.google", "application/json",
                            json.dumps({"Status": 0, "Answer": [{"type": 1, "data": ip}]}).encode(), False)
            with patch("socket.getaddrinfo", return_value=synthetic), \
                    patch("urllib.request.getproxies", return_value={"https":"http://localhost:9090"}), \
                    patch("miniagent.research._service", return_value=resolver), \
                    patch("miniagent.research.http.client.HTTPConnection") as connector:
                if ip == "10.10.10.10":
                    with self.assertRaisesRegex(ValueError, "公网地址"):
                        _public_socket("public.example", 443, 1)
                    connector.assert_not_called()
                else:
                    _public_socket("public.example", 443, 1)
                    connector.return_value.set_tunnel.assert_called_once_with("8.8.8.8", 443)

    def test_incomplete_or_failed_search_does_not_invent_sources(self):
        for data, truncated in ((b"not a result set", False), (b'{"articles": []}',True)):
            with patch("miniagent.research._service", return_value=Page("https://example.org", "text/plain",data,truncated)):
                self.assertFalse(self.registry.dispatch("search_sources", {"query": "campus"}, self.ctx).ok)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_public_endpoint_and_redirect_validation(self):
        with self.assertRaisesRegex(ValueError, "禁止访问内网"):
            fetch("http://127.0.0.1/admin")
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.15",443))]
        with patch("socket.getaddrinfo", return_value=private), patch("socket.socket") as sock:
            with self.assertRaisesRegex(ValueError, "禁止访问内网"):
                _public_socket("public-looking.example",443,1)
            sock.assert_not_called()
        with patch("miniagent.research.http.client.HTTPConnection") as connection, patch("miniagent.research._public_socket") as sock:
            redirect = connection.return_value.getresponse.return_value.__enter__.return_value
            redirect.status=302
            redirect.getheader.return_value="http://localhost:8080/target"
            with self.assertRaisesRegex(ValueError,"禁止访问内网"):
                fetch("http://external.example/link")
            sock.assert_called_once()
