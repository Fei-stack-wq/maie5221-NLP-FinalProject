"""Small loopback-only HTTP adapter for the local Studio application.

No framework, shell command, public binding, credential endpoint or arbitrary
static-file root. Browser actions require the same origin and a server token.
"""
from __future__ import annotations

import hmac
import json
import re
import secrets
import shutil
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .studio_service import StudioError, StudioService
from .studio_preview import PREVIEW_CSP, html_preview
from .studio_uploads import UploadError, open_upload, save_upload

MAX_BODY_BYTES = 65536
_ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
           "/room-panel.js": ("room-panel.js", "text/javascript; charset=utf-8"),
           "/room-panel.css": ("room-panel.css", "text/css; charset=utf-8"),
           "/directory.js": ("directory.js", "text/javascript; charset=utf-8"),
           "/directory.css": ("directory.css", "text/css; charset=utf-8"),
           "/index.html": ("index.html", "text/html; charset=utf-8"),
           "/superagent.css": ("superagent.css", "text/css; charset=utf-8"),
           "/superagent.js": ("superagent.js", "text/javascript; charset=utf-8"),
           "/activity.js": ("activity.js", "text/javascript; charset=utf-8"),
           "/activity.css": ("activity.css", "text/css; charset=utf-8"),
           "/attachments.js": ("attachments.js", "text/javascript; charset=utf-8"),
           "/markdown-reader.js": ("markdown-reader.js", "text/javascript; charset=utf-8"),
           "/markdown-reader.css": ("markdown-reader.css", "text/css; charset=utf-8"),
           "/markdown-it.min.js": ("vendor/markdown-it.min.js", "text/javascript; charset=utf-8"),
           "/app.js": ("app.js", "text/javascript; charset=utf-8"),
           "/reply-stream.js": ("reply-stream.js", "text/javascript; charset=utf-8"),
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

    def _headers(self, status: int, length: int, content_type: str, *, filename: str | None, csp: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        if filename is not None:
            fallback = re.sub(r"[^A-Za-z0-9._ -]", "_", filename)
            encoded = quote(filename, safe="")
            self.send_header("Content-Disposition", f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", csp)
        self.end_headers()

    def _send(self, status: int, payload: bytes, content_type: str, *, filename: str | None = None,
              csp: str = _CSP):
        self._headers(status, len(payload), content_type, filename=filename, csp=csp)
        if self.command != "HEAD":
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, status: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._send(status, data, "application/json; charset=utf-8")

    def _reply_stream(self, run_id):
        stream = self.server.studio_service.reply_stream(run_id)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        if self.command == "HEAD":
            return
        cursor = None
        try:
            while True:
                cursor, frames = stream.updates(cursor) if stream else (0, [("done", {})])
                if not frames:
                    self.wfile.write(b": heartbeat\n\n")
                for kind, data in frames:
                    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                    self.wfile.write(f"event: {kind}\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                if frames and frames[-1][0] == "done":
                    return
        except OSError:
            # Closing the browser or switching conversations just disconnects
            # this reader. It never cancels the job or retries the model call.
            return

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
        except (StudioError, UploadError) as exc:
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
        if path == "/api/superagent":
            self._json(200, service.superagent_head())
            return
        if path == "/api/profiles":
            self._json(200, {"profiles": service.profiles()})
            return
        if path == "/api/runs":
            self._json(200, service.list_runs())
            return
        if path == "/api/directory/chats":
            self._json(200, service.directory_chats())
            return
        if path == "/api/conversations":
            self._json(200, service.list_conversations())
            return
        parts = path.split("/")
        if parts[:4] == ["", "api", "directory", "channels"] and len(parts) == 6 and parts[5] == "members":
            self._json(200, service.channel_members(parts[4]))
            return
        if parts[:4] == ["", "api", "directory", "roles"] and len(parts) in (4, 5):
            self._json(200, service.role_directory(parts[4] if len(parts) == 5 else None))
            return
        if len(parts) in (5, 7) and parts[:3] == ["", "api", "profiles"] and parts[4] == "knowledge":
            if len(parts) == 5:
                self._json(200, service.role_library(parts[3]))
                return
            if parts[6] == "history":
                self._json(200, service.role_operation(parts[3], "history", {}, parts[5]))
                return
        if len(parts) == 5 and parts[:3] == ["", "api", "runs"] and parts[4] == "reply-stream":
            self._reply_stream(parts[3])
            return
        if len(parts) == 5 and parts[:3] == ["", "api", "runs"] and parts[4] == "activity":
            self._json(200, service.tool_activity(parts[3]))
            return
        if len(parts) == 5 and parts[:3] == ["", "api", "runs"] and parts[4] == "room-answer":
            self._json(200, service.room_answer(parts[3]))
            return
        if len(parts) == 5 and parts[:3] == ["", "api", "runs"] and parts[4] == "room-events":
            params = parse_qs(url.query, keep_blank_values=True)
            if params and (set(params) != {"before"} or len(params["before"]) != 1
                           or not re.fullmatch(r"[0-9]{1,12}", params["before"][0])):
                raise StudioError("消息分页位置无效。")
            rows = service.room_events(parts[3])
            end = min(len(rows), int(params["before"][0])) if params else len(rows)
            start = max(0, end - 24)
            self._json(200, service._redact({"start": start, "events": rows[start:end]}))
            return
        if len(parts) == 5 and parts[:3] == ["", "api", "runs"] and parts[4] == "room.zip":
            self._send(200, service.room_bundle(parts[3]), "application/zip", filename=f"{parts[3]}-room.zip")
            return
        if len(parts) == 5 and parts[:3] == ["", "api", "runs"] and parts[4] in {"artifacts.zip", "project.zip"}:
            bundle = service.artifact_bundle(parts[3], project=parts[4] == "project.zip")
            self._send(200, bundle, "application/zip", filename=f"{parts[3]}-{parts[4]}")
            return
        if len(parts) == 6 and parts[:3] == ["", "api", "runs"] and parts[4] == "attachments":
            meta = service._meta(service._directory(parts[3]))
            from .studio_conversations import turn_attachments
            from .studio_steering import steering_rows
            updates = steering_rows(service.room_events(parts[3])) if meta.get("has_steering") else []
            refs = turn_attachments({"message_attachments": meta.get("attachments", []), "steering": updates})
            if not any(ref["id"] == parts[5] for ref in refs):
                raise StudioError("这条消息没有该附件。", "attachment_not_found", 404)
            ref, stream = open_upload(service.runs_dir, parts[5])
            with stream:
                self._headers(200, ref["size"], "application/octet-stream", filename=ref["name"],
                              csp="default-src 'none'; sandbox")
                if self.command != "HEAD":
                    try:
                        shutil.copyfileobj(stream, self.wfile, length=256 * 1024)
                    except (ConnectionError, TimeoutError):
                        pass
            return
        if len(parts) == 4 and parts[:3] == ["", "api", "runs"]:
            self._json(200, service.detail(parts[3]))
            return
        if len(parts) == 5 and parts[:3] == ["", "api", "runs"] and parts[4] in {"artifact", "preview", "download", "excerpt"}:
            params = parse_qs(url.query, keep_blank_values=True)
            if set(params) != {"path"} or len(params["path"]) != 1:
                raise StudioError("缺少产物路径。")
            if parts[4] == "excerpt":
                self._json(200, service.artifact_excerpt(parts[3], params["path"][0]))
            elif parts[4] == "download":
                name = params["path"][0]
                data = service.download_artifact(parts[3], name)
                self._send(200, data, "application/octet-stream", filename=name.rsplit("/", 1)[-1])
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

    def _request_length(self) -> int:
        if self.headers.get_all("Transfer-Encoding"):
            raise StudioError("不支持分块请求。")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
            raise StudioError("请求长度无效。")
        return int(lengths[0])

    def _post(self):
        url = urlsplit(self.path)
        if url.path == "/api/uploads":
            params = parse_qs(url.query, keep_blank_values=True, strict_parsing=True, errors="strict")
            if set(params) != {"name"} or len(params["name"]) != 1:
                raise StudioError("上传请求需要一个文件名。")
            if self.headers.get_content_type() != "application/octet-stream" or self.headers.get("Content-Encoding"):
                raise StudioError("附件必须以 application/octet-stream 原始字节发送。", "unsupported_media_type", 415)
            ref = save_upload(self.server.studio_service.runs_dir, params["name"][0], self.rfile, self._request_length())
            self._json(201, {"upload": ref})
            return
        parts = unquote(url.path).split("/")
        cancel = len(parts) == 5 and parts[:3] == ["", "api", "runs"] and parts[4] == "cancel"
        steer = len(parts) == 5 and parts[:3] == ["", "api", "runs"] and parts[4] == "steer"
        reply = len(parts) == 6 and parts[:3] == ["", "api", "runs"] and parts[4] == "questions"
        review = len(parts) == 5 and parts[:3] == ["", "api", "runs"] and parts[4] == "review"
        profile_op = parts[:3] == ["", "api", "profiles"] and (
            len(parts) == 5 and parts[4] in {"edit", "knowledge", "bind"} or
            len(parts) == 7 and parts[4] == "knowledge" and parts[6] in {"status", "restore"})
        channel_op = parts[:4] == ["", "api", "directory", "channels"] and len(parts) in (4, 5)
        members_op = parts[:4] == ["", "api", "directory", "channels"] and len(parts) == 6 and parts[5] == "members"
        if (url.path not in ("/api/runs", "/api/profiles", "/api/workspaces/pick", "/api/superagent/messages")
                and not (cancel or steer or reply or review or profile_op or channel_op or members_op)) or url.query:
            raise StudioError("接口不存在。", "not_found", 404)
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise StudioError("请求必须为 JSON。", "unsupported_media_type", 415)
        length = self._request_length()
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
        if steer:
            self._json(202, {"event": self.server.studio_service.steer(parts[3], payload)})
            return
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
        if members_op:
            self._json(200, self.server.studio_service.update_members(parts[4], payload))
            return
        if channel_op:
            service = self.server.studio_service
            channel = service.create_channel(payload) if len(parts) == 4 else service.update_channel(parts[4], payload)
            self._json(200, {"channel": channel})
            return
        if profile_op:
            service = self.server.studio_service
            if parts[4] == "bind":
                result = service.link_profile(parts[3], payload)
            elif parts[4] == "edit":
                result = {"profile": service.edit_profile(parts[3], payload)}
            else:
                result = service.role_operation(parts[3], parts[6] if len(parts) == 7 else "save",
                                                payload, parts[5] if len(parts) == 7 else None)
            self._json(200, result)
            return
        if url.path == "/api/superagent/messages":
            self._json(202, {"run": self.server.studio_service.send_superagent(payload)})
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
