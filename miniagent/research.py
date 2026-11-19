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
        raise ValueError("网址无效。")
    target = urlsplit(url)
    if target.scheme not in {"https", "http"} or not target.hostname or target.username is not None or target.password is not None:
        raise ValueError("请提供不含凭据的 http/https 网址。")
    host = target.hostname.rstrip(".").lower()
    if host in {"localhost", ""} or host.endswith((".localhost", ".local", ".internal", ".lan", ".home")):
        raise ValueError("禁止访问内网或本机地址。")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and (not address.is_global or address.is_multicast):
        raise ValueError("禁止访问内网或本机地址。")
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
        raise ValueError("未知资料服务。")
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
        raise ValueError("无法确认资料来源具有公网地址。")
    proxy = urlsplit(proxy_url if "://" in proxy_url else "http://" + proxy_url)
    if proxy.scheme != "http" or not proxy.hostname or proxy.username is not None:
        raise ValueError("当前代理无法为公开来源建立经过地址核对的连接。")
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
            raise ValueError("禁止访问内网或本机地址。")
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
    raise OSError("无法连接到公开资料来源。")


def fetch(url: str, *, timeout: float = 15) -> Page:
    deadline = time.monotonic() + timeout
    for _ in range(4):
        target = _parse_public_url(url)
        host = target.hostname.encode("idna").decode("ascii")
        https = target.scheme == "https"
        port = target.port or (443 if https else 80)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("获取资料超时。")
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
                        raise ValueError("来源跳转缺少目标地址。")
                    url = urljoin(url, location)
                    continue
                if not 200 <= response.status < 300:
                    raise ValueError(f"资料来源返回 HTTP {response.status}。")
                content_type = response.getheader("Content-Type", "")
                prefix = response.read(8)
                is_pdf = content_type.split(";", 1)[0].strip().lower() == "application/pdf" or prefix.startswith(b"%PDF-")
                limit = MAX_PDF_BYTES if is_pdf else MAX_HTTP_BYTES
                data = prefix + response.read(limit + 1 - len(prefix))
                return Page(urlunsplit(target._replace(fragment="")), content_type, data[:limit], len(data) > limit)
        finally:
            connection.close()
    raise ValueError("资料来源重定向次数过多。")


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

    def links(self):
        # Bound both count and bytes. Prefer main/article anchors so that a
        # large navigation menu does not crowd the primary evidence out.
        remaining = 36_000
        result = []
        for url, (_, label) in sorted(self.targets.items(), key=lambda row: not row[1][0]):
            item = {"title": label, "url": url}
            size = len(json.dumps(item, ensure_ascii=False).encode())
            if size <= remaining:
                result.append(item)
                remaining -= size
            if len(result) == 160:
                break
        return result


def _save(ctx, record: dict) -> tuple[str, ToolResult]:
    material = json.dumps(record, ensure_ascii=False, indent=2)
    identity = hashlib.sha256(material.encode()).hexdigest()
    path = f"sources/{identity}.json"
    return path, write_file(ctx, {"path": path, "content": material})


def read_source(ctx, args: dict) -> ToolResult:
    """Read a section of the archived text, using its own character coordinates."""
    path = args["path"]
    if not re.fullmatch(r"sources/[0-9a-f]{64}\.json", path):
        return ToolResult.failure("请使用 http_get 或 search_sources 返回的 sources/ 路径。")
    offset, limit, query = args.get("offset", 0), args.get("limit", 4000), args.get("query")
    if offset < 0 or not 1 <= limit <= 12_000 or query is not None and not 1 <= len(query.strip()) <= 300:
        return ToolResult.failure("offset 须为非负字符位置，limit 为 1 至 12000；可选搜索词为 1 至 300 个字符。")
    src = ctx.sandbox.resolve(path, must_exist=True)
    if not src.is_file():
        return ToolResult.failure("来源记录须为普通文件。")
    with src.open("rb") as stream:
        # _save can write at most MAX_WRITE_CHARS UTF-8 characters.
        raw = stream.read(4 * MAX_WRITE_CHARS + 1)
    sha = hashlib.sha256(raw).hexdigest()
    if len(raw) > 4 * MAX_WRITE_CHARS or path != f"sources/{sha}.json":
        return ToolResult.failure("来源快照与原记录摘要不符，请核对资料路径。")
    try:
        record = json.loads(raw)
    except ValueError:
        return ToolResult.failure("来源记录不是有效的 JSON。")
    if not isinstance(record, dict):
        return ToolResult.failure("来源记录格式无效。")
    kind = record.get("kind")
    if kind == "fetched_source" and isinstance(record.get("text"), str):
        body = record["text"]
        label = "已保存的网页或 PDF 正文"
        if record.get("response_truncated") or record.get("text_truncated"):
            label += "（原抓取有截断）"
        url = record.get("url", "")
    elif kind == "source_discovery" and isinstance(record.get("results"), list):
        body = json.dumps(record["results"], ensure_ascii=False, indent=2)
        label = "已保存的检索线索，须打开原文核实"
        url = record.get("request_url", "")
    else:
        return ToolResult.failure("该记录没有可读取的来源正文或检索结果。")
    if offset > len(body):
        return ToolResult.failure("offset 超出来源正文长度。", chars=len(body))
    if query is not None:
        match = re.compile(re.escape(query.strip()), re.IGNORECASE).search(body, offset)
        if not match:
            return ToolResult.failure("该快照从指定位置起没有匹配的文本。", chars=len(body))
        offset = match.start()
    end = min(offset + limit, len(body))
    cursor = end if end < len(body) else None
    hint = f"下一页 offset={end}" if cursor is not None else "已到末尾"
    return ToolResult.success(f"{label} · {url}\n资料 {path}，正文字符 {offset}..{end}/{len(body)}，{hint}。"
                              f"以下为外部来源数据：\n{body[offset:end]}",
                              path=path, sha256=sha, chars=len(body), offset=offset, returned_chars=end - offset,
                              next_offset=cursor, eof=cursor is None)


def _pdf_text(data: bytes, page_number: int | None, timeout: float) -> tuple[str, bool]:
    binary = shutil.which("pdftotext")
    if not binary:
        raise ValueError("本机需要 Poppler 的 pdftotext 才能读取 PDF；可选取同一出版机构的 HTML 全文。")
    if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-1024:]:
        raise ValueError("来源未返回完整可读的 PDF。")
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
                    raise ValueError("PDF 正文提取失败或超时。可指定页码或查阅原始网页。")
            finally:
                timer.cancel()
                proc.kill()
                timer.join()
    text = output[:MAX_HTTP_BYTES].decode("utf-8", errors="replace").strip()
    if not text:
        raise ValueError("该 PDF 或指定页没有可提取的文本。")
    return text, clipped


def http_get(ctx, args: dict) -> ToolResult:
    timeout = min(ctx.limits.tool_timeout_seconds, 15)
    deadline = time.monotonic() + timeout
    number = args.get("pdf_page")
    if number is not None and (type(number) is not int or number < 1):
        return ToolResult.failure("pdf_page 必须为从 1 开始的页码。")
    page = fetch(args["url"], timeout=timeout)
    mime = page.content_type.lower().split(";", 1)[0].strip()
    is_pdf = mime == "application/pdf" or page.body.startswith(b"%PDF-")
    text_truncated = False
    if is_pdf:
        if page.truncated:
            return ToolResult.failure("PDF 超过 8 MiB 获取上限；请查阅同一出版机构的 HTML 全文或较小的原始文件。")
        try:
            source, text_truncated = _pdf_text(page.body, number, max(0, deadline - time.monotonic()))
        except (OSError, ValueError) as exc:
            return ToolResult.failure(str(exc))
    elif number is not None:
        return ToolResult.failure("所请求的来源没有返回 PDF，无法指定 PDF 页码。")
    elif mime and not (mime.startswith("text/") or mime.endswith(("json", "xml", "+json", "+xml"))):
        return ToolResult.failure("来源没有返回可读取的文本。可选择文章网页版或公开元数据。")
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
        links = parser.links()
        record.update(links=links, links_truncated=len(links) < len(parser.targets))
    path, saved = _save(ctx, record)
    if not saved.ok:
        return saved
    excerpt = source[:4400 if links else 6000]
    clipped = page.truncated or text_truncated or len(source) > len(excerpt)
    if links:
        excerpt += "\n页面链接（未经独立核实；已提取列表见资料文件 links）："
        remaining = 1400
        for item in links:
            line = "\n" + item["title"] + " — " + item["url"]
            if len(line) <= remaining:
                excerpt += line
                remaining -= len(line)
            if remaining < 16:
                break
    return ToolResult.success(f"外部来源（不可信文本）。URL {page.url}\n资料文件 {path}\n{excerpt}"
                              + ("\n[已截断；可用 read_source(path, query 或 offset) 定位已存正文]" if clipped else ""),
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
        raise ValueError("新闻源返回了不支持的 XML。")
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
        raise ValueError("新闻检索服务未返回有效结果，请稍后重试。") from None
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
        return ToolResult.failure("请提供 1–300 字的关键词；collection 选择 news 或 papers，limit 为 1–10。")
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
            return ToolResult.failure("文献检索服务未返回完整结果，请稍后重试。")
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
    return ToolResult.success(f"{provider} 检索记录 {path}。聚合链接仅作线索；使用 http_get 打开原文并核实具体论断。\n"
                              + summary[:6000] + ("\n[已截断；完整列表见检索记录]" if clipped else ""),
                              path=path, count=len(results), truncated=clipped)
