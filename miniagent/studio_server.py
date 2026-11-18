"""Small loopback-only HTTP adapter for the local Studio application.

No framework, shell command, public binding, credential endpoint or arbitrary
static-file root. Browser actions require the same origin and a server token.
"""
from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .studio_service import StudioError, StudioService
from .studio_preview import PREVIEW_CSP, html_preview

MAX_BODY_BYTES = 65536
_ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
           "/index.html": ("index.html", "text/html; charset=utf-8"),
           "/app.js": ("app.js", "text/javascript; charset=utf-8"),
           "/style.css": ("style.css", "text/css; charset=utf-8")}
_CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"


class StudioHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, service: StudioService):
        self.studio_service = service
        self.studio_token = secrets.token_urlsafe(32)
        super().__init__(address, handler)


class StudioHandler(BaseHTTPRequestHandler):
    server_version = "MiniAgentStudio"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        # URLs and request bodies can contain user input. No request echo.
        pass

    def _send(self, status: int, payload: bytes, content_type: str, *, filename: str | None = None,
              csp: str = _CSP):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        if filename is not None:
            self.send_header("Content-Disposition", f"attachment; filename=\"{filename}\"; filename*=UTF-8''{filename}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", csp)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, status: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._send(status, data, "application/json; charset=utf-8")

    def _guard(self, *, mutation: bool = False):
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0] not in allowed:
            raise StudioError("仅允许本机访问。", "forbidden", 403)
        origins = self.headers.get_all("Origin", [])
        if origins and (len(origins) != 1 or origins[0] != "http://" + hosts[0]):
            raise StudioError("请求来源无效。", "forbidden", 403)
        if mutation:
            if len(origins) != 1:
                raise StudioError("缺少同源请求信息。", "forbidden", 403)
            tokens = self.headers.get_all("X-Studio-Token", [])
            if len(tokens) != 1 or not hmac.compare_digest(tokens[0].encode("utf-8"), self.server.studio_token.encode("ascii")):
                raise StudioError("页面会话已失效，请刷新后重试。", "forbidden", 403)

    def _handle(self, *, mutation: bool = False):
        try:
            self._guard(mutation=mutation)
            if mutation:
                self._post()
            else:
                self._get()
        except StudioError as exc:
            self._json(exc.status, {"error": str(exc), "code": exc.code})
        except (ValueError, UnicodeError, json.JSONDecodeError):
            self._json(400, {"error": "请求格式无效。", "code": "invalid_request"})
        except (OSError, RuntimeError, TypeError, KeyError, sqlite3.Error):
            self._json(500, {"error": "本地服务暂时无法完成请求，原有任务不会被自动重试。", "code": "internal_error"})

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle(mutation=True)

    def do_HEAD(self):
        self._handle()

    def do_OPTIONS(self):
        self._json(405, {"error": "不支持跨站请求。", "code": "method_not_allowed"})

    def do_PUT(self):
        self._json(405, {"error": "不支持此操作。", "code": "method_not_allowed"})

    do_DELETE = do_PUT
    do_PATCH = do_PUT

    def _get(self):
        url = urlsplit(self.path)
        path = unquote(url.path)
        if path in _ASSETS:
            name, content_type = _ASSETS[path]
            asset = Path(__file__).resolve().parent / "web" / name
            if not asset.is_file() or asset.is_symlink():
                raise StudioError("界面资源不存在。", "not_found", 404)
            self._send(200, asset.read_bytes(), content_type)
            return
        service = self.server.studio_service
        if path == "/api/bootstrap":
            self._json(200, {**service.bootstrap(), "csrf_token": self.server.studio_token})
            return
        if path == "/api/runs":
            self._json(200, service.list_runs())
            return
        if path == "/api/conversations":
            self._json(200, service.list_conversations())
            return
        parts = path.split("/")
        if len(parts) == 5 and parts[:3] == ["", "api", "runs"] and parts[4] == "room.zip":
            self._send(200, service.room_bundle(parts[3]), "application/zip", filename=f"{parts[3]}-room.zip")
            return
        if len(parts) == 5 and parts[:3] == ["", "api", "runs"] and parts[4] == "artifacts.zip":
            bundle = service.artifact_bundle(parts[3])
            self._send(200, bundle, "application/zip", filename=f"{parts[3]}-artifacts.zip")
            return
        if len(parts) == 4 and parts[:3] == ["", "api", "runs"]:
            self._json(200, service.detail(parts[3]))
            return
        if len(parts) == 5 and parts[:3] == ["", "api", "runs"] and parts[4] in {"artifact", "preview", "download"}:
            params = parse_qs(url.query, keep_blank_values=True)
            if set(params) != {"path"} or len(params["path"]) != 1:
                raise StudioError("缺少产物路径。")
            if parts[4] == "download":
                from urllib.parse import quote
                name = params["path"][0]
                data = service.download_artifact(parts[3], name)
                self._send(200, data, "application/octet-stream", filename=quote(name.rsplit("/", 1)[-1], safe=""))
            elif parts[4] == "preview":
                name = params["path"][0]
                if name.lower().endswith(".png"):
                    self._send(200, service.download_artifact(parts[3], name), "image/png", csp="default-src 'none'")
                else:
                    self._send(200, html_preview(service, parts[3], name), "text/html; charset=utf-8", csp=PREVIEW_CSP)
            else:
                self._json(200, service.artifact(parts[3], params["path"][0]))
            return
        raise StudioError("页面不存在。", "not_found", 404)

    def _post(self):
        url = urlsplit(self.path)
        parts = unquote(url.path).split("/")
        cancel = len(parts) == 5 and parts[:3] == ["", "api", "runs"] and parts[4] == "cancel"
        reply = len(parts) == 6 and parts[:3] == ["", "api", "runs"] and parts[4] == "questions"
        review = len(parts) == 5 and parts[:3] == ["", "api", "runs"] and parts[4] == "review"
        if (url.path not in ("/api/runs", "/api/profiles", "/api/workspaces/pick") and not (cancel or reply or review)) or url.query:
            raise StudioError("接口不存在。", "not_found", 404)
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise StudioError("请求必须为 JSON。", "unsupported_media_type", 415)
        if self.headers.get("Transfer-Encoding"):
            raise StudioError("不支持分块请求。")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isdigit():
            raise StudioError("请求长度无效。")
        length = int(lengths[0])
        if length > MAX_BODY_BYTES:
            raise StudioError("任务描述过长。", "body_too_large", 413)
        if length == 0:
            raise StudioError("任务内容不能为空。")
        body = self.rfile.read(length)
        if len(body) != length:
            raise StudioError("请求内容不完整。")
        def pairs(items):
            data = {}
            for key, value in items:
                if key in data:
                    raise ValueError("duplicate key")
                data[key] = value
            return data
        def constant(value):
            raise ValueError("non-finite JSON")
        payload = json.loads(body, object_pairs_hook=pairs, parse_constant=constant)
        if url.path == "/api/workspaces/pick":
            from .studio_workspace import WorkspaceError, pick_directory
            if payload != {}:
                raise StudioError("目录选择请求必须为空对象。")
            try:
                selected = pick_directory()
            except (WorkspaceError, TimeoutError) as exc:
                raise StudioError(str(exc), "picker_unavailable", 409) from None
            self._json(200, {"path": selected})
            return
        if url.path == "/api/profiles":
            self._json(200, {"profile": self.server.studio_service.add_profile(payload)})
            return
        if reply or review:
            required = {"answer"} if reply else {"verdict", "note"}
            if type(payload) is not dict or set(payload) != required:
                raise StudioError("答复或评审参数无效。")
            service = self.server.studio_service
            row = (service.reply_human(parts[3], parts[5], payload["answer"]) if reply else
                   service.review_delivery(parts[3], payload["verdict"], payload["note"]))
            self._json(200, {"event": row})
            return
        if cancel:
            if payload != {}:
                raise StudioError("停止请求必须是空 JSON 对象。")
            self._json(202, {"run": self.server.studio_service.cancel(parts[3])})
            return
        self._json(202, {"run": self.server.studio_service.start(payload)})


def create_server(service: StudioService, host: str = "127.0.0.1", port: int = 8765) -> StudioHTTPServer:
    if host != "127.0.0.1":
        raise ValueError("Studio only binds to 127.0.0.1")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    return StudioHTTPServer((host, port), StudioHandler, service)


def serve(service: StudioService, host: str = "127.0.0.1", port: int = 8765, *, open_browser: bool = False) -> None:
    server = create_server(service, host=host, port=port)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"Agent Studio 已启动：{url}", flush=True)
    print("按 Ctrl+C 关闭；历史任务保留，未完成任务不会自动重放。", flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()
