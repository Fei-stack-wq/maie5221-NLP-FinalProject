"""Research discovery and traceable source handoff without external network."""
import hashlib
import json
import shutil
import socket
import unittest
import urllib.error
from unittest.mock import patch

from miniagent.config import Limits
from miniagent.research import Page, _public_socket, fetch
from miniagent.sandbox import Sandbox
from miniagent.tools import ToolContext, build_default_registry
from tests.support import cleanup, make_temp_dir


def public_pdf_fixture():
    # A complete PDF with two text pages, an explicit cross-reference
    # table, and no library dependency for creating the test fixture.
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>"]
    for content_id in (6, 7):
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
                       f"/Resources << /Font << /F 5 0 R >> >> /Contents {content_id} 0 R >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for title in ("Source one", "Course catalogue"):
        stream = f"BT /F 12 Tf 10 100 Td ({title}) Tj ET".encode()
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, item in enumerate(objects, 1):
        offsets.append(len(body))
        body.extend(f"{index} 0 obj\n".encode() + item + b"\nendobj\n")
    xref = len(body)
    body.extend(b"xref\n0 8\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        body.extend(f"{offset:010} 00000 n \n".encode())
    body.extend(f"trailer\n<< /Size 8 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return Page("https://campus.example/catalogue", "application/octet-stream", bytes(body), False)


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

    @unittest.skipUnless(shutil.which("pdftotext"), "Poppler is optional")
    def test_public_pdf_is_read_by_page_and_only_text_evidence_is_published(self):
        page = public_pdf_fixture()
        body = page.body
        with patch("miniagent.research.fetch", return_value=page):
            result = self.registry.dispatch("http_get", {"url": page.url, "pdf_page": 2}, self.ctx)
            self.assertTrue(result.ok, result.content)
            evidence = json.loads((self.root / result.meta["path"]).read_text())
            self.assertEqual(evidence["text"], "Course catalogue")
            self.assertEqual(evidence["observed_bytes_sha256"], hashlib.sha256(body).hexdigest())
            self.assertEqual((evidence["pdf_page"], evidence["extractor"], evidence["text_truncated"]),
                             (2, "pdftotext", False))
            self.assertFalse(self.registry.dispatch("http_get", {"url": page.url, "pdf_page": 12}, self.ctx).ok)
        self.assertEqual([item for item in self.root.rglob("*") if item.is_file()],
                         [self.root / result.meta["path"]])

    def test_incomplete_pdf_and_missing_parser_cannot_create_a_source_receipt(self):
        for body, truncated in ((b"%PDF-1.4", True), (b"invalid", False), (b"%PDF-1.4", False)):
            page = Page("https://campus.example/file.pdf", "application/pdf", body, truncated)
            with patch("miniagent.research.fetch", return_value=page):
                result = self.registry.dispatch("http_get", {"url": page.url}, self.ctx)
                self.assertFalse(result.ok)
        with patch("miniagent.research.fetch", return_value=page), patch("miniagent.research.shutil.which", return_value=None):
            self.assertIn("Poppler", self.registry.dispatch("http_get", {"url": page.url}, self.ctx).content)
        self.assertEqual(list(self.root.iterdir()), [])

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
        discovery_page = self.registry.dispatch("read_source", {"path": search.meta["path"], "query": "campus"}, self.ctx)
        self.assertTrue(discovery_page.ok, discovery_page.content)
        self.assertIn("检索线索", discovery_page.content)
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

    def test_read_saved_source_uses_text_offsets_and_finds_exact_passages_offline(self):
        body = "第一段🌿 Economics。第二段 Economic growth。".encode()
        with patch("miniagent.research.fetch", return_value=Page("https://example.org/text", "text/plain", body, False)) as fetch:
            source = self.registry.dispatch("http_get", {"url": "https://example.org/text"}, self.ctx)
            path = source.meta["path"]
            def read(**args):
                return self.registry.dispatch("read_source", {"path": path, **args}, self.ctx)

            page = read(query="ECONOMIC", limit=8)
            self.assertTrue(page.ok, page.content)
            self.assertEqual(page.meta["offset"], body.decode().index("Economic"))
            self.assertTrue(page.content.endswith("Economic"))
            rest = read(offset=page.meta["next_offset"])
            self.assertTrue(rest.content.endswith("s。第二段 Economic growth。"))
            self.assertTrue(rest.meta["eof"])
            self.assertFalse(read(query="缺失").ok)
            self.assertFalse(read(offset=12000).ok)
            self.assertFalse(read(limit=0).ok)
            self.assertFalse(read(path="../other.json").ok)
            (self.root / path).write_bytes(b'{}')
            self.assertFalse(read().ok)
            malformed = b'invalid-json'
            damaged = f"sources/{hashlib.sha256(malformed).hexdigest()}.json"
            (self.root / damaged).write_bytes(malformed)
            self.assertIn("JSON", read(path=damaged).content)
            fetch.assert_called_once()

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
