"""Serve an explicitly opened HTML project without granting it a Studio origin."""
from __future__ import annotations

import base64
from html import escape
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from .studio_service import StudioError, StudioService


# Only the host-written wrapper is top-level. The generated product runs in
# an opaque data document in a sandbox with scripts, but no same-origin,
# navigation of ancestors, popups, forms, downloads, or storage privileges.
_ASSET_POLICY = ("default-src 'none'; script-src 'unsafe-inline' data:; "
                 "style-src 'unsafe-inline' data:; img-src data:; font-src data:; "
                 "media-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'")
PREVIEW_CSP = _ASSET_POLICY + "; frame-src data:; frame-ancestors 'none'"


def _data_url(mime: str, content: str) -> str:
    return "data:" + mime + ";base64," + base64.b64encode(content.encode("utf-8")).decode("ascii")


class _ProjectHTML(HTMLParser):
    """Preserve the authored HTML and embed explicitly linked local JS and CSS."""

    def __init__(self, service: StudioService, run_id: str, document: PurePosixPath):
        super().__init__(convert_charrefs=False)
        self.service, self.run_id, self.document = service, run_id, document
        self.parts: list[str] = []
        self.cache: dict[PurePosixPath, str] = {}
        self.embedded_bytes = 0

    def _asset(self, reference: str, suffix: str) -> str:
        url = urlsplit(reference)
        path = PurePosixPath(unquote(url.path))
        if (url.scheme or url.netloc or path.is_absolute() or ".." in path.parts or
                path.suffix.lower() != suffix or "\\" in str(path)):
            raise StudioError("预览支持内嵌脚本和样式，以及同目录或子目录内的 JS/CSS。可下载完整项目运行其他依赖。",
                              "unsupported_preview", 415)
        target = self.document.parent / path
        if target not in self.cache:
            if len(self.cache) >= 64:
                raise StudioError("预览依赖过多，请下载项目运行。", "preview_too_large", 413)
            item = self.service.artifact(self.run_id, str(target))
            if item["truncated"]:
                raise StudioError("预览依赖超过读取上限，请下载完整项目。", "preview_too_large", 413)
            mime = "text/javascript" if suffix == ".js" else "text/css"
            self.cache[target] = _data_url(mime, item["content"])
        self.embedded_bytes += len(self.cache[target])
        if self.embedded_bytes > 4 * 1024 * 1024:
            raise StudioError("预览资源超过 4 MiB，请下载完整项目。", "preview_too_large", 413)
        return self.cache[target]

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        style = tag == "link" and "stylesheet" in (values.get("rel") or "").lower().split()
        field = "href" if style else "src" if tag == "script" else None
        if field is None or not values.get(field):
            self.parts.append(self.get_starttag_text())
            return
        ref = self._asset(values[field], ".css" if style else ".js")
        rendered = "".join(" " + name + ('="' + escape(ref if name == field else value, quote=True) + '"'
                           if value is not None else "") for name, value in attrs)
        self.parts.append("<" + tag + rendered + ">")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        self.parts.append("</" + tag + ">")

    def handle_data(self, data):
        self.parts.append(data)

    def handle_entityref(self, name):
        self.parts.append("&" + name + ";")

    def handle_charref(self, name):
        self.parts.append("&#" + name + ";")

    def handle_comment(self, data):
        self.parts.append("<!--" + data + "-->")

    def handle_decl(self, decl):
        self.parts.append("<!" + decl + ">")


def html_preview(service: StudioService, run_id: str, path: str) -> bytes:
    document = PurePosixPath(path)
    if document.suffix.lower() != ".html":
        raise StudioError("演示预览仅支持 HTML 产物。", "unsupported_preview", 415)
    item = service.artifact(run_id, path)
    if item["truncated"]:
        raise StudioError("HTML 超过预览读取上限，请下载完整项目。", "preview_too_large", 413)
    parser = _ProjectHTML(service, run_id, document)
    parser.feed(item["content"])
    parser.close()
    product = _data_url("text/html;charset=utf-8", "".join(parser.parts))
    title = escape(document.name, quote=True)
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{title}</title><style>body{{margin:0}}iframe{{display:block;width:100vw;height:100dvh;border:0}}</style>'
        f'<iframe title="{title}" sandbox="allow-scripts" referrerpolicy="no-referrer" src="{product}"></iframe></html>'
    ).encode("utf-8")
