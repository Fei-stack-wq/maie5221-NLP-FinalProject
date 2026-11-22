"""Public source discovery and bounded, citable retrieval for project workspaces."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import re
import socket
import shutil
import subprocess
import tempfile
import threading
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

from .file_tools import MAX_WRITE_CHARS, write_file
from .schemas import ToolResult

MAX_HTTP_BYTES = 200_000
MAX_PDF_BYTES = 8 * 1024 * 1024
_NEWS = "https://api.gdeltproject.org/api/v2/doc/doc"
_PAPERS = "https://api.crossref.org/works"
_NEWS_RSS = "https://news.google.com/rss/search"


@dataclass(frozen=True)
class Page:
    url: str
    content_type: str
    body: bytes
    truncated: bool


def _parse_public_url(url: str):
    if (len(url) > 4096 or "\\" in url or any(ord(ch) <= 32 or ord(ch) == 127 for ch in url)):
        raise ValueError("Invalid URL.")
    target = urlsplit(url)
    if target.scheme not in {"https", "http"} or not target.hostname or target.username is not None or target.password is not None:
        raise ValueError("Use an HTTP or HTTPS URL without credentials.")
    host = target.hostname.rstrip(".").lower()
    if host in {"localhost", ""} or host.endswith((".localhost", ".local", ".internal", ".lan", ".home")):
        raise ValueError("Private or local addresses are not permitted.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and (not address.is_global or address.is_multicast):
        raise ValueError("Private or local addresses are not permitted.")
    return target


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _service(url: str, timeout: float) -> Page:
    # Only program-defined HTTPS services get the OS proxy's normal DNS policy.
    # No redirects and no arbitrary path input from a tool caller.
    endpoint = urlsplit(url)
    if (endpoint.scheme, endpoint.netloc, endpoint.path) not in {
            ("https", "api.gdeltproject.org", "/api/v2/doc/doc"),
            ("https", "api.crossref.org", "/works"), ("https", "news.google.com", "/rss/search"),
            ("https", "dns.google", "/resolve")}:
        raise ValueError("Unknown source service.")
    req = urllib.request.Request(url, headers={"User-Agent": "MiniAgentStudio/1.0",
                                              "Accept": "application/json, application/rss+xml, application/xml"})
    try:
        response = urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise
    with response:
        data = response.read(MAX_HTTP_BYTES + 1)
        return Page(url, response.headers.get("Content-Type", ""), data[:MAX_HTTP_BYTES], len(data) > MAX_HTTP_BYTES)


def _proxy_socket(host: str, port: int, proxy_url: str, timeout: float):
    # Some local system proxies return benchmarking addresses for all names.
    # Ask for a public DNS answer and CONNECT to a validated numeric address.
    resolver = _service("https://dns.google/resolve?" + urlencode({"name": host, "type": "A",
                                                                   "edns_client_subnet": "0.0.0.0/0"}), timeout)
    result = json.loads(resolver.body)
    addresses = [ipaddress.ip_address(row["data"]) for row in result.get("Answer", []) if row.get("type") == 1]
    if (resolver.truncated or result.get("Status") != 0 or not addresses
            or any(not ip.is_global or ip.is_multicast for ip in addresses)):
        raise ValueError("Could not verify a public address for the source.")
    proxy = urlsplit(proxy_url if "://" in proxy_url else "http://" + proxy_url)
    if proxy.scheme != "http" or not proxy.hostname or proxy.username is not None:
        raise ValueError("The configured proxy cannot establish an address-verified connection to the source.")
    connection = http.client.HTTPConnection(proxy.hostname, proxy.port or 80, timeout=timeout)
    connection.set_tunnel(str(addresses[0]), port)
    try:
        connection.connect()
        sock = connection.sock
        connection.sock = None
        return sock
    finally:
        connection.close()


def _public_socket(host: str, port: int, timeout: float):
    # Connect using the validated addresses. TLS and the Host header still use
    # the original DNS name. Redirects must pass the same check on every hop.
    started = time.monotonic()
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    synthetic = ipaddress.ip_network("198.18.0.0/15")
    if addresses and all(ipaddress.ip_address(item[4][0]) in synthetic for item in addresses):
        proxy = urllib.request.getproxies().get("https")
        if proxy:
            return _proxy_socket(host, port, proxy, timeout)
    for _, _, _, _, addr in addresses:
        ip = ipaddress.ip_address(addr[0])
        if not ip.is_global or ip.is_multicast:
            raise ValueError("Private or local addresses are not permitted.")
    for family, kind, protocol, _, sockaddr in addresses:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            break
        sock = socket.socket(family, kind, protocol)
        try:
            sock.settimeout(remaining)
            sock.connect(sockaddr)
            return sock
        except OSError:
            sock.close()
    raise OSError("Could not connect to the public source.")


def fetch(url: str, *, timeout: float = 15) -> Page:
    deadline = time.monotonic() + timeout
    for _ in range(4):
        target = _parse_public_url(url)
        host = target.hostname.encode("idna").decode("ascii")
        https = target.scheme == "https"
        port = target.port or (443 if https else 80)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out fetching source.")
        connection = http.client.HTTPConnection(host, port=port, timeout=remaining)
        try:
            connection.sock = _public_socket(host, port, remaining)
            if https:
                connection.sock = ssl.create_default_context().wrap_socket(connection.sock, server_hostname=host)
            resource = urlunsplit(("", "", target.path or "/", target.query, ""))
            host_header = f"[{host}]" if ":" in host else host
            if port != (443 if https else 80):
                host_header += f":{port}"
            connection.request("GET", resource, headers={"Host": host_header, "User-Agent": "MiniAgentStudio/1.0",
                                                         "Accept-Encoding": "identity"})
            with connection.getresponse() as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location:
                        raise ValueError("Source redirect lacks a target URL.")
                    url = urljoin(url, location)
                    continue
                if not 200 <= response.status < 300:
                    raise ValueError(f"Source returned HTTP {response.status}.")
                content_type = response.getheader("Content-Type", "")
                prefix = response.read(8)
                is_pdf = content_type.split(";", 1)[0].strip().lower() == "application/pdf" or prefix.startswith(b"%PDF-")
                limit = MAX_PDF_BYTES if is_pdf else MAX_HTTP_BYTES
                data = prefix + response.read(limit + 1 - len(prefix))
                return Page(urlunsplit(target._replace(fragment="")), content_type, data[:limit], len(data) > limit)
        finally:
            connection.close()
    raise ValueError("Too many redirects.")


def _link_target(base: str, href: str) -> str:
    if not href or href.startswith("#"):
        return ""
    try:
        parsed = _parse_public_url(urljoin(base, href))
        # Reading port enforces urlsplit's validation of the numeric port.
        parsed.port
    except ValueError:
        return ""
    return urlunsplit(parsed._replace(fragment=""))


class _VisibleText(HTMLParser):
    """Keep labeled outbound evidence as well as text; never fetch linked pages here."""

    def __init__(self, page_url):
        super().__init__()
        self.page_url = self.base = page_url
        self.base_seen = False
        self.hidden_depth = self.main_depth = 0
        self.parts = []
        self.targets = {}
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head", "noscript", "template"}:
            self.hidden_depth += 1
        attributes = dict(attrs)
        if tag == "base" and not self.base_seen:
            target = _link_target(self.page_url, attributes.get("href") or "")
            if target:
                self.base, self.base_seen = target, True
        if self.hidden_depth:
            return
        if tag in {"main", "article"}:
            self.main_depth += 1
        elif tag == "a":
            target = _link_target(self.base, attributes.get("href") or "")
            self.anchor = (target, len(self.parts), attributes.get("aria-label") or attributes.get("title") or "")
        elif tag == "img" and self.anchor and attributes.get("alt"):
            target, start, label = self.anchor
            self.anchor = (target, start, label or attributes["alt"])

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head", "noscript", "template"}:
            self.hidden_depth = max(0, self.hidden_depth - 1)
            return
        if self.hidden_depth:
            return
        if tag in {"main", "article"}:
            self.main_depth = max(0, self.main_depth - 1)
        elif tag == "a" and self.anchor:
            target, start, fallback = self.anchor
            self.anchor = None
            label = " ".join((" ".join(self.parts[start:]) or fallback).split())[:200]
            if target and target != self.page_url and label:
                in_main = self.main_depth > 0
                if target not in self.targets or in_main and not self.targets[target][0]:
                    self.targets[target] = (in_main, label)

    def handle_data(self, data):
        if not self.hidden_depth and data.strip():
            self.parts.append(data.strip())

    def links(self, ctx):
        # Bound both count and bytes. Prefer main/article anchors so that a
        # large navigation menu does not crowd the primary evidence out.
        remaining = 36_000
        result = []
        withheld = 0
        for url, (_, label) in sorted(self.targets.items(), key=lambda row: not row[1][0]):
            item = {"title": label, "url": url}
            material = json.dumps(item, ensure_ascii=False)
            if _source_issue(ctx, "link.json", material):
                withheld += 1
                continue
            size = len(material.encode())
            if len(result) < 160 and size <= remaining:
                result.append(item)
                remaining -= size
        return result, withheld


def _source_issue(ctx, path: str, material: str) -> str:
    # Only the configured provider key is a real secret here. Heuristic
    # "credential" scanners rejected ordinary news URLs and blocked handoffs.
    if ctx.contains_configured_secret and ctx.contains_configured_secret(material):
        return "Source contains the configured service credential."
    return ""


def _save(ctx, record: dict) -> tuple[str, ToolResult]:
    material = json.dumps(record, ensure_ascii=False, indent=2)
    identity = hashlib.sha256(material.encode()).hexdigest()
    path = f"sources/{identity}.json"
    issue = _source_issue(ctx, path, material)
    if issue:
        return path, ToolResult.failure("Cannot save source: " + issue)
    return path, write_file(ctx, {"path": path, "content": material})


def read_source(ctx, args: dict) -> ToolResult:
    """Read a section of the archived text, using its own character coordinates."""
    path = args["path"]
    if not re.fullmatch(r"sources/[0-9a-f]{64}\.json", path):
        return ToolResult.failure("Use a sources/ path returned by http_get or search_sources.")
    offset, limit, query = args.get("offset", 0), args.get("limit", 4000), args.get("query")
    if offset < 0 or not 1 <= limit <= 12_000 or query is not None and not 1 <= len(query.strip()) <= 300:
        return ToolResult.failure("offset must be nonnegative, limit 1..12000, and the optional query must contain 1..300 characters.")
    src = ctx.sandbox.resolve(path, must_exist=True)
    if not src.is_file():
        return ToolResult.failure("The source record must be a regular file.")
    with src.open("rb") as stream:
        # _save can write at most MAX_WRITE_CHARS UTF-8 characters.
        raw = stream.read(4 * MAX_WRITE_CHARS + 1)
    sha = hashlib.sha256(raw).hexdigest()
    if len(raw) > 4 * MAX_WRITE_CHARS or path != f"sources/{sha}.json":
        return ToolResult.failure("Source snapshot digest mismatch. Check the source path.")
    try:
        record = json.loads(raw)
    except ValueError:
        return ToolResult.failure("Invalid JSON source record.")
    if not isinstance(record, dict):
        return ToolResult.failure("Invalid source record format.")
    kind = record.get("kind")
    if kind == "fetched_source" and isinstance(record.get("text"), str):
        body = record["text"]
        label = "Saved page or PDF text"
        if record.get("response_truncated") or record.get("text_truncated"):
            label += " (original fetch truncated)"
        url = record.get("url", "")
    elif kind == "source_discovery" and isinstance(record.get("results"), list):
        body = json.dumps(record["results"], ensure_ascii=False, indent=2)
        label = "Saved discovery leads. Verify against the original source."
        url = record.get("request_url", "")
    else:
        return ToolResult.failure("No readable source text or discovery results in this record.")
    if offset > len(body):
        return ToolResult.failure("offset exceeds source length.", chars=len(body))
    if query is not None:
        match = re.compile(re.escape(query.strip()), re.IGNORECASE).search(body, offset)
        if not match:
            return ToolResult.failure("No match from this offset.", chars=len(body))
        offset = match.start()
    end = min(offset + limit, len(body))
    cursor = end if end < len(body) else None
    hint = f"next offset={end}" if cursor is not None else "end"
    return ToolResult.success(f"{label} · {url}\nRecord {path}, characters {offset}..{end}/{len(body)}, {hint}.\n"
                              f"External source data:\n{body[offset:end]}",
                              path=path, sha256=sha, chars=len(body), offset=offset, returned_chars=end - offset,
                              next_offset=cursor, eof=cursor is None)


def _pdf_text(data: bytes, page_number: int | None, timeout: float) -> tuple[str, bool]:
    binary = shutil.which("pdftotext")
    if not binary:
        raise ValueError("Poppler pdftotext is required. The publisher may also provide HTML.")
    if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-1024:]:
        raise ValueError("Incomplete or unreadable PDF.")
    args = [binary, "-enc", "UTF-8"]
    if page_number is not None:
        args.extend(["-f", str(page_number), "-l", str(page_number)])
    # Poppler alone parses the PDF. Keep raw source bytes outside the agent's
    # publishable tree, and keep parser output and runtime bounded.
    with tempfile.TemporaryFile() as original:
        original.write(data)
        original.seek(0)
        with subprocess.Popen([*args, "-", "-"], stdin=original, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, env={}) as proc:
            timer = threading.Timer(timeout, proc.kill)
            timer.start()
            try:
                output = proc.stdout.read(MAX_HTTP_BYTES + 1)
                clipped = len(output) > MAX_HTTP_BYTES
                if clipped:
                    proc.kill()
                if proc.wait() and not clipped:
                    raise ValueError("PDF text extraction failed or timed out. Select a page or use the original web page.")
            finally:
                timer.cancel()
                proc.kill()
                timer.join()
    text = output[:MAX_HTTP_BYTES].decode("utf-8", errors="replace").strip()
    if not text:
        raise ValueError("No extractable text on the selected page or in this PDF.")
    return text, clipped


def http_get(ctx, args: dict) -> ToolResult:
    timeout = min(ctx.limits.tool_timeout_seconds, 15)
    deadline = time.monotonic() + timeout
    number = args.get("pdf_page")
    if number is not None and (type(number) is not int or number < 1):
        return ToolResult.failure("pdf_page starts at 1.")
    issue = _source_issue(ctx, "request-url.txt", args["url"])
    if issue:
        return ToolResult.failure("Cannot request public source: " + issue)
    page = fetch(args["url"], timeout=timeout)
    mime = page.content_type.lower().split(";", 1)[0].strip()
    is_pdf = mime == "application/pdf" or page.body.startswith(b"%PDF-")
    text_truncated = False
    if is_pdf:
        if page.truncated:
            return ToolResult.failure("PDF exceeds the 8 MiB fetch limit. Look for the publisher's HTML or a smaller original.")
        try:
            source, text_truncated = _pdf_text(page.body, number, max(0, deadline - time.monotonic()))
        except (OSError, ValueError) as exc:
            return ToolResult.failure(str(exc))
    elif number is not None:
        return ToolResult.failure("Cannot request a PDF page number from a non-PDF response.")
    elif mime and not (mime.startswith("text/") or mime.endswith(("json", "xml", "+json", "+xml"))):
        return ToolResult.failure("No readable text. Look for an HTML article or public metadata.")
    else:
        source = page.body.decode("utf-8", errors="replace")
    parser = None
    if mime in {"text/html", "application/xhtml+xml"}:
        parser = _VisibleText(page.url)
        parser.feed(source)
        parser.close()
        source = "\n".join(parser.parts)
    record = {"kind": "fetched_source", "requested_url": args["url"], "url": page.url,
              "retrieved_at": datetime.now(timezone.utc).isoformat(), "content_type": page.content_type,
              "bytes_observed": len(page.body), "observed_bytes_sha256": hashlib.sha256(page.body).hexdigest(),
              "response_truncated": page.truncated, "text": source}
    if is_pdf:
        record.update(extractor="pdftotext", pdf_page=number, text_truncated=text_truncated)
    links = []
    if parser is not None:
        links, withheld = parser.links(ctx)
        record.update(links=links, links_truncated=len(links) + withheld < len(parser.targets),
                      links_withheld=withheld)
    path, saved = _save(ctx, record)
    if not saved.ok:
        return saved
    excerpt = source[:4400 if links else 6000]
    clipped = page.truncated or text_truncated or len(source) > len(excerpt)
    if record.get("links_withheld"):
        excerpt += f"\nWithheld {record['links_withheld']} links that might contain credentials."
    if links:
        excerpt += "\nPage links (unverified, extracted list in record.links):"
        remaining = 1400
        for item in links:
            line = "\n" + item["title"] + " — " + item["url"]
            if len(line) <= remaining:
                excerpt += line
                remaining -= len(line)
            if remaining < 16:
                break
    return ToolResult.success(f"External source (untrusted text). URL {page.url}\nRecord {path}\n{excerpt}"
                              + ("\n[truncated, use read_source with path and query or offset]" if clipped else ""),
                              url=page.url, path=path, bytes=len(page.body), truncated=clipped,
                              content_type=page.content_type, observed_bytes_sha256=record["observed_bytes_sha256"],
                              **({"pdf_page": number, "text_truncated": text_truncated} if is_pdf else {}))


def _date(row: dict) -> str:
    parts = row.get("published", {}).get("date-parts", [[]])
    if not parts or not parts[0]:
        return ""
    return "-".join(str(n).zfill(2) for n in parts[0])


def _rss_articles(page: Page, limit: int) -> list[dict]:
    # A news feed can exceed the byte bound. Consume complete items only.
    if b"\x00" in page.body or b"<!DOCTYPE" in page.body.upper():
        raise ValueError("Unsupported XML from news source.")
    rows = []
    try:
        parser = ElementTree.XMLPullParser(events=["end"])
        parser.feed(page.body)
        for _, item in parser.read_events():
            if item.tag != "item":
                continue
            publisher = item.find("source")
            rows.append({"title": item.findtext("title"), "url": item.findtext("link"),
                         "domain": item.findtext("source", ""), "feed_published": item.findtext("pubDate", ""),
                         "publisher_url": publisher.get("url", "") if publisher is not None else ""})
            if len(rows) == limit:
                break
        if not rows:
            parser.close()
    except ElementTree.ParseError:
        raise ValueError("News search returned invalid results. Retry later.") from None
    return rows


def _results(rows: list, provider: str, limit: int) -> list[dict]:
    news = provider != "Crossref"
    results, seen = [], set()
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        link = row.get("url" if news else "URL")
        title = row.get("title")
        if not news and isinstance(title, list):
            title = next(iter(title), "")
        if not isinstance(link, str) or not isinstance(title, str) or link in seen:
            continue
        try:
            _parse_public_url(link)
        except ValueError:
            continue
        seen.add(link)
        result = {"title": title[:350], "url": link, "publisher": str(row.get("domain" if news else "publisher", ""))[:200]}
        if provider == "Google News RSS":
            result["feed_published"] = str(row.get("feed_published", ""))[:80]
            result["publisher_url"] = str(row.get("publisher_url", ""))[:2048]
            result["link_kind"] = "aggregator"
        elif news:
            result["indexed_at"] = str(row.get("seendate", ""))[:50]
        else:
            result["published"] = _date(row)
        results.append(result)
    return results


def search_sources(ctx, args: dict) -> ToolResult:
    query = args["query"].strip()
    collection, limit = args.get("collection", "news"), args.get("limit", 5)
    if not 1 <= len(query) <= 300 or collection not in {"news", "papers"} or not 1 <= limit <= 10:
        return ToolResult.failure("Use a query of 1..300 characters, news or papers collection, and limit 1..10.")
    news = collection == "news"
    if news:
        url = _NEWS + "?" + urlencode({"query": query, "mode": "artlist", "format": "json", "maxrecords": limit})
    else:
        url = _PAPERS + "?" + urlencode({"query.bibliographic": query, "rows": limit,
                                         "select": "URL,title,publisher,published"})
    timeout = min(ctx.limits.tool_timeout_seconds, 15)
    provider = "GDELT DOC" if news else "Crossref"
    fallback_reason = None
    try:
        page = _service(url, timeout=timeout)
        payload = json.loads(page.body)
        rows = payload["articles"] if news else payload["message"]["items"]
        if type(rows) is not list or page.truncated:
            raise ValueError("incomplete search response")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        if not news:
            return ToolResult.failure("Incomplete results from scholarly search. Retry later.")
        fallback_reason = f"HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
        provider = "Google News RSS"
        url = _NEWS_RSS + "?" + urlencode({"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
        page = _service(url, timeout=timeout)
        rows = _rss_articles(page, limit)
    results = _results(rows, provider, limit)
    record = {"kind": "source_discovery", "provider": provider,
              "query": query, "request_url": url, "retrieved_at": datetime.now(timezone.utc).isoformat(),
              "results": results}
    if fallback_reason:
        record["fallback_from"] = {"provider": "GDELT DOC", "reason": fallback_reason}
    path, saved = _save(ctx, record)
    if not saved.ok:
        return saved
    summary = json.dumps(results, ensure_ascii=False)
    clipped = len(summary) > 6000
    return ToolResult.success(f"{provider} discovery record {path}. Open original publications with http_get to verify claims.\n"
                              + summary[:6000] + ("\n[truncated, see saved discovery record for the full list]" if clipped else ""),
                              path=path, count=len(results), truncated=clipped)
