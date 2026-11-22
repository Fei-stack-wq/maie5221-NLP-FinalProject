"""Local HTTP server for the Studio: JSON API + static UI, plus a preview origin.

Two listeners, both bound to 127.0.0.1:

* ``port``      — the Studio UI and ``/api``. Mutating requests must carry the
  ``X-Studio: 1`` header (forces a CORS preflight, so pages from other origins
  — including the preview origin — cannot drive the API).
* ``port + 1``  — serves each group's project files at ``/<group_id>/<path>``
  so generated web apps open with working relative links and ``localStorage``,
  on an origin that has no access to the Studio API.
"""

from __future__ import annotations

import json
import mimetypes
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .studio import Studio, StudioError

WEB = Path(__file__).with_name("web")
TEMPLATES = Path(__file__).resolve().parent.parent / "examples" / "teams"


def templates() -> list[dict]:
    rows = []
    if TEMPLATES.is_dir():
        for path in sorted(TEMPLATES.glob("*.json")):
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    return rows


def _content_type(path: Path) -> str:
    kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if kind.startswith("text/") or kind in {"application/javascript", "application/json", "image/svg+xml"}:
        kind += "; charset=utf-8"
    if path.suffix.lower() in {".md", ".py", ".txt", ".csv", ".log", ".ts", ".jsx", ".tsx", ".yaml", ".yml",
                               ".toml", ".ini", ".sh", ".sql"}:
        kind = "text/plain; charset=utf-8"
    return kind


class _Base(BaseHTTPRequestHandler):
    server_version = "miniagent-studio"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002 - quiet by default
        return

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in {"127.0.0.1", "localhost", "[::1]"}


class StudioHandler(_Base):
    studio: Studio
    preview_port: int

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "forbidden host"})
        url = urlsplit(self.path)
        parts = [unquote(p) for p in url.path.split("/") if p]
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            if not parts or parts == ["index.html"]:
                return self._static("index.html")
            if parts[0] == "static" and len(parts) >= 2:
                return self._static("/".join(parts[1:]))
            if parts[0] != "api":
                return self._json(404, {"error": "not found"})
            return self._api_get(parts[1:], query)
        except StudioError as exc:
            return self._json(exc.status, {"error": str(exc)})

    def _static(self, rel: str):
        path = (WEB / rel).resolve()
        if WEB.resolve() not in path.parents or not path.is_file():
            return self._json(404, {"error": "not found"})
        csp = ("default-src 'self'; img-src 'self' data: http://127.0.0.1:*; frame-src http://127.0.0.1:*; "
               "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'")
        return self._send(200, path.read_bytes(), _content_type(path), {"Content-Security-Policy": csp})

    def _api_get(self, parts: list[str], query: dict):
        studio = self.studio
        if parts == ["config"]:
            s = studio.settings
            return self._json(200, {"model": s.model, "thinking": s.thinking, "has_key": bool(s.api_key),
                                    "preview_port": self.preview_port, "member_max_steps": s.limits.max_steps,
                                    "lead_max_steps": s.team.lead_max_steps, "max_delegations": s.team.max_delegations})
        if parts == ["templates"]:
            return self._json(200, templates())
        if parts == ["groups"]:
            return self._json(200, studio.groups())
        if len(parts) >= 2 and parts[0] == "groups":
            gid = parts[1]
            rest = parts[2:]
            if not rest:
                return self._json(200, studio.state(gid, int(query.get("after", 0) or 0),
                                                    int(query.get("activity_after", 0) or 0)))
            if rest == ["files"]:
                return self._json(200, studio.files(gid))
            if rest[0] == "files" and len(rest) > 1:
                path = studio.read_file(gid, "/".join(rest[1:]))
                kind = _content_type(path)
                # Never execute generated files on the API origin: only raster
                # images keep their type; everything else is plain text here.
                if not kind.startswith(("image/png", "image/jpeg", "image/gif", "image/webp")):
                    kind = "text/plain; charset=utf-8"
                return self._send(200, path.read_bytes(), kind, {"Content-Security-Policy": "sandbox"})
            if rest == ["archive.zip"]:
                name = f"{gid}-project.zip"
                return self._send(200, studio.archive(gid), "application/zip",
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
            if rest == ["tasks"]:
                return self._json(200, studio.tasks(gid))
        return self._json(404, {"error": "not found"})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 2_000_000:
            raise StudioError("request too large", 413)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            raise StudioError("invalid JSON") from None
        if not isinstance(data, dict):
            raise StudioError("expected a JSON object")
        return data

    def _mutating(self, method: str):
        if not self._host_ok() or self.headers.get("X-Studio") != "1":
            return self._json(403, {"error": "missing X-Studio header"})
        parts = [unquote(p) for p in urlsplit(self.path).path.split("/") if p]
        try:
            body = self._body()
            studio = self.studio
            if parts[:1] != ["api"]:
                return self._json(404, {"error": "not found"})
            parts = parts[1:]
            if method == "POST" and parts == ["groups"]:
                return self._json(201, studio.create_group(body.get("name", ""), body.get("members", []),
                                                           body.get("lead", ""), body.get("mode", "interactive")))
            if len(parts) >= 2 and parts[0] == "groups":
                gid, rest = parts[1], parts[2:]
                if method == "POST" and rest == ["messages"]:
                    return self._json(200, studio.post(gid, body.get("text", "")))
                if method == "POST" and rest == ["cancel"]:
                    return self._json(200, studio.cancel(gid))
                if method == "PATCH" and not rest:
                    return self._json(200, studio.update_group(gid, body))
            return self._json(404, {"error": "not found"})
        except StudioError as exc:
            return self._json(exc.status, {"error": str(exc)})

    def do_POST(self):
        self._mutating("POST")

    def do_PATCH(self):
        self._mutating("PATCH")


class PreviewHandler(_Base):
    studio: Studio

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "forbidden host"})
        parts = [unquote(p) for p in urlsplit(self.path).path.split("/") if p]
        if not parts:
            return self._json(404, {"error": "not found"})
        try:
            rel = "/".join(parts[1:]) or "index.html"
            path = self.studio.read_file(parts[0], rel)
        except StudioError as exc:
            if exc.status == 404 and not urlsplit(self.path).path.endswith("/"):
                try:
                    path = self.studio.read_file(parts[0], "/".join(parts[1:] + ["index.html"]))
                except StudioError:
                    return self._json(404, {"error": str(exc)})
            else:
                return self._json(exc.status, {"error": str(exc)})
        content_type = _content_type(path)
        if path.suffix.lower() in {".js", ".mjs"}:
            content_type = "text/javascript; charset=utf-8"
        return self._send(200, path.read_bytes(), content_type)


def serve(studio: Studio, port: int = 8765, *, ready: threading.Event | None = None) -> tuple[ThreadingHTTPServer, ThreadingHTTPServer]:
    """Start both listeners on background threads and return them."""
    preview_handler = type("Preview", (PreviewHandler,), {"studio": studio})
    preview = ThreadingHTTPServer(("127.0.0.1", port + 1 if port else 0), preview_handler)
    handler = type("Handler", (StudioHandler,), {"studio": studio, "preview_port": preview.server_address[1]})
    main = ThreadingHTTPServer(("127.0.0.1", port), handler)
    main.daemon_threads = preview.daemon_threads = True
    for server in (main, preview):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    if ready:
        ready.set()
    return main, preview
