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
import base64
import errno
import secrets
import mimetypes
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .studio import Studio, StudioError
from .settings_api import model_settings, save_key, save_models
from .folders import pick
from .evaluation import assessor_export
from .workbench import published_bytes, tool_page

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
        port = self.server.server_address[1]
        return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}



class StudioHandler(_Base):
    studio: Studio
    preview_port: int
    instance_id: str

    def end_headers(self):
        self.send_header("X-Studio-Instance", self.instance_id)
        if self.studio.update_pending:
            self.send_header("X-Studio-Update", "pending")
        super().end_headers()

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
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            return self._json(400, {"error":str(exc)})

    def _static(self, rel: str):
        asset = self.assets.get(rel)
        if asset is None:
            return self._json(404, {"error": "not found"})
        csp = ("default-src 'self'; img-src 'self' data: http://127.0.0.1:*; frame-src http://127.0.0.1:*; "
               "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'")
        return self._send(200, asset, _content_type(Path(rel)), {"Content-Security-Policy": csp})

    def _api_get(self, parts: list[str], query: dict):
        studio = self.studio
        if parts == ["instance"]:
            return self._send(200, json.dumps({"id": self.instance_id}).encode(), "application/json",
                              {"X-Studio-Busy": "1" if studio.busy else "0"})
        if parts == ["assistant"]:
            with studio.writing_request():  # first visit can create the assistant room
                return self._json(200, studio.assistant())
        if parts == ["bootstrap"]:
            return self._json(200, {"token":self.studio_token, "templates":templates(), "has_key":bool(studio.settings.api_key),
                                    **model_settings(studio.settings),
                                    "preview_port":self.preview_port,"workbench_version":1,"chat_protocol":1,"task_pause":True,"member_max_steps":studio.settings.limits.max_steps,
                                    "lead_max_steps":studio.settings.team.lead_max_steps,
                                    "max_delegations":studio.settings.team.max_delegations})
        if parts == ["roles"]:
            return self._json(200, studio.roles.list())
        if len(parts) == 3 and parts[0] == "roles" and parts[2] == "knowledge":
            return self._json(200, studio.roles.history(parts[1]))
        if parts == ["tasks"]:
            groups = [studio.group(query["group"]).group] if query.get("group") else studio.store.conversations()
            rows = []
            for group in groups:
                room = studio.group(group.id)
                label = f"{room.team.name} / {group.name}" if room.parent else group.name
                rows.extend({**task, "group_name": label, "team_id": room.team.id, "session_id": group.id}
                            for task in room.tasks())
            return self._json(200, sorted(rows, key=lambda t:t.get("created_at",0), reverse=True))
        if len(parts) >= 4 and parts[0] == "tasks" and parts[2] == "files":
            store, task = studio.find_task(parts[1])
            name = "/".join(parts[3:])
            data = published_bytes(store, task["id"], name, query.get("sha256"))
            if data is None:
                raise StudioError("此任务发布的文件版本不可用。", 404)
            kind = _content_type(Path(name))
            if not kind.startswith(("image/png", "image/jpeg", "image/gif", "image/webp")):
                kind = "text/plain; charset=utf-8"
            return self._send(200, data, kind, {"Content-Security-Policy": "sandbox"})
        if len(parts) in (2,3) and parts[0] == "tasks":
            if parts[2:] == ["tools"]:
                store, task = studio.find_task(parts[1])
                return self._json(200, tool_page(store, task["id"], int(query.get("cursor", 0))))

            if len(parts) == 2:
                return self._json(200, studio.task_detail(parts[1], include_activity=query.get("view") != "summary"))
            if parts[2] == "artifacts.zip":
                store, task = studio.find_task(parts[1])
                archive = store.task_dir(task["id"])/"artifacts.zip"
                if not archive.is_file():
                    raise StudioError("Artifact snapshot unavailable for this task.",404)
                return self._send(200,archive.read_bytes(),"application/zip")
        if parts == ["config"]:
            s = studio.settings
            return self._json(200, {**model_settings(s), "has_key": bool(s.api_key),
                                    "preview_port": self.preview_port, "member_max_steps": s.limits.max_steps,
                                    "lead_max_steps": s.team.lead_max_steps, "max_delegations": s.team.max_delegations})
        if parts == ["templates"]:
            return self._json(200, templates())
        if parts == ["groups"]:
            rows = studio.conversations() if query.get("view") == "conversations" else studio.groups()
            term = query.get("filter", "").casefold()
            return self._json(200, [r for r in rows if term in r["name"].casefold()])
        if len(parts) >= 2 and parts[0] == "groups":
            gid = parts[1]
            rest = parts[2:]
            if not rest:
                return self._json(200, studio.state(gid, int(query.get("after", 0) or 0),
                                                    int(query.get("activity_after", 0) or 0)))
            if rest == ["sessions"]:
                return self._json(200, studio.sessions(gid))
            if rest == ["chat-stream"]:
                return self._chat_stream(studio.group(gid))
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
            if rest == ["export.zip"]:
                return self._send(200, assessor_export(studio.group(gid)), "application/zip")
            if rest == ["archive.zip"]:
                name = f"{gid}-project.zip"
                return self._send(200, studio.archive(gid), "application/zip",
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
            if rest == ["tasks"]:
                return self._json(200, studio.tasks(gid))
        return self._json(404, {"error": "not found"})

    def _chat_stream(self, store):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command == "HEAD":
            return
        try:
            for frame in store.previews.frames():
                line = ": heartbeat" if frame is None else "data: " + json.dumps(frame, ensure_ascii=False)
                self.wfile.write((line+"\n\n").encode("utf-8"))
                self.wfile.flush()
        except (ConnectionError, OSError):
            return  # A reader leaving the room does not cancel its worker or model request.

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        limit = 28_000_000 if urlsplit(self.path).path.endswith("/uploads") else 2_000_000
        if length < 0 or length > limit:
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
        try:
            with self.studio.writing_request():
                return self._apply_mutation(method)
        except StudioError as exc:
            return self._json(exc.status, {"error": str(exc)})

    def _apply_mutation(self, method: str):
        if (not self._host_ok() or self.headers.get("Origin") != "http://" + self.headers.get("Host", "")
                or self.headers.get("X-Studio-Token") != self.studio_token):
            self.close_connection = True
            return self._json(403, {"error": "invalid Studio origin or token"})
        parts = [unquote(p) for p in urlsplit(self.path).path.split("/") if p]
        try:
            body = self._body()
            studio = self.studio
            if parts[:1] != ["api"]:
                return self._json(404, {"error": "not found"})
            parts = parts[1:]
            if method == "POST" and parts == ["settings","api-key"]:
                return self._json(200, save_key(studio,body.get("api_key")))
            if method == "POST" and parts == ["settings","models"]:
                return self._json(200, save_models(studio, body))
            if studio.settings.api_key and studio.settings.api_key in json.dumps(body,ensure_ascii=False):
                raise StudioError("The payload contains the configured API key.")
            if method == "POST" and parts == ["uploads"]:
                data = base64.b64decode(body["data"], validate=True)
                return self._json(201, studio.uploads.save(body["name"],data,studio.settings.api_key))
            if method == "POST" and parts == ["folders","pick"]:
                return self._json(200, {"path":pick()})
            if method == "POST" and parts == ["roles"]:
                return self._json(201, studio.roles.save(body))
            if method == "PATCH" and len(parts) == 2 and parts[0] == "roles":
                return self._json(200, studio.roles.save({**body,"id":parts[1]}, update=True))
            if method == "POST" and len(parts) == 3 and parts[0] == "roles" and parts[2] == "knowledge":
                studio.roles.get(parts[1])
                entry = studio.roles.propose(parts[1], body["text"], "general", body.get("kind", "memory"))
                return self._json(201, entry)
            if len(parts) == 5 and parts[0] == "roles" and parts[2] == "knowledge" and parts[4] == "status" and method == "POST":
                return self._json(200, studio.roles.revise(parts[1],parts[3],body["status"],text=body.get("text"),
                                                          revision=body.get("revision")))
            if method == "POST" and len(parts)==3 and parts[0]=="tasks":
                if parts[2]=="pause":
                    return self._json(200,studio.pause_task(parts[1]))
                if parts[2]=="resume":
                    return self._json(200,studio.resume_task(parts[1]))
                if parts[2]=="cancel":
                    from .studio_dispatch import cancel_task
                    return self._json(200,cancel_task(studio,parts[1]))
            if method == "POST" and parts == ["groups"]:
                if body.get("kind") == "assistant":
                    raise ValueError("Use /api/assistant")

                return self._json(201, studio.create_group(body.get("name", ""), body.get("members", []),
                                                           body.get("lead", ""), body.get("mode", "interactive"),
                                                           kind=body.get("kind","group"),role_ids=body.get("role_ids",[])))
            if len(parts) >= 2 and parts[0] == "groups":
                gid, rest = parts[1], parts[2:]
                if method == "POST" and rest == ["uploads"]:
                    data = base64.b64decode(body["data"], validate=True)
                    with studio._lock:
                        room = studio.group(gid)
                        studio._require_available(room)
                        upload = studio.uploads.save_in_room(room, body["name"], data, studio.settings.api_key)
                    return self._json(201,upload)
                if method == "POST" and rest == ["sessions"]:
                    return self._json(201, studio.create_session(gid, body.get("name")))
                if method == "POST" and rest == ["messages"]:
                    return self._json(200, studio.post(gid, body.get("text", ""), attachments=body.get("attachments", []),
                                                      options=body.get("options"), queue=body.get("queue") is True,
                                                      client_id=body.get("client_id")))
                if method == "POST" and len(rest) == 2 and rest[0] == "approvals":
                    return self._json(200, studio.decide_approval(gid, rest[1], body.get("decision")))
                if method == "POST" and rest == ["cancel"]:
                    return self._json(200, studio.cancel(gid))
                if method == "DELETE" and not rest:
                    return self._json(200,studio.trash(gid))
                if method == "PATCH" and not rest:
                    return self._json(200, studio.update_group(gid, body))
            return self._json(404, {"error": "not found"})
        except StudioError as exc:
            return self._json(exc.status, {"error": str(exc)})
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            return self._json(400, {"error":str(exc)})

    def do_POST(self):
        try:
            self._mutating("POST")
        except (BrokenPipeError, ConnectionResetError):
            # A page reload can disconnect the composer while a room reply is being committed.
            self.close_connection = True

    def do_DELETE(self):
        self._mutating("DELETE")

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


def serve(studio: Studio, port: int = 8765, *, strict_port: bool = False,
          ready: threading.Event | None = None) -> tuple[ThreadingHTTPServer, ThreadingHTTPServer]:
    """Start both listeners on background threads and return them."""
    preview_handler = type("Preview", (PreviewHandler,), {"studio": studio})
    if not 0 <= port <= 65535:
        raise ValueError("Invalid Studio port")
    # A running process must keep serving the frontend that matches its Python API.
    assets = {p.relative_to(WEB).as_posix(): p.read_bytes() for p in WEB.rglob("*")
              if p.is_file() and WEB.resolve() in p.resolve().parents}
    handler = type("Handler", (StudioHandler,), {"studio": studio, "studio_token": secrets.token_urlsafe(32),
                                                 "assets": assets, "instance_id": secrets.token_hex(16)})
    try:
        main = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as exc:
        if strict_port or exc.errno != errno.EADDRINUSE:
            raise
        main = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    try:
        wanted = port+1 if port and main.server_port == port and port < 65535 else 0
        try:
            preview = ThreadingHTTPServer(("127.0.0.1", wanted), preview_handler)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            preview = ThreadingHTTPServer(("127.0.0.1", 0), preview_handler)
    except BaseException:
        main.server_close()
        raise
    handler.preview_port = preview.server_port
    main.daemon_threads = preview.daemon_threads = True
    for server in (main, preview):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    if ready:
        ready.set()
    return main, preview
