"""Standalone HTTP server for the admin console (standard library only).

Serves the SPA frontend from web/ and the /v1/admin/* JSON API. Admin endpoints use
a session token from POST /v1/admin/login (separate from any user token).
"""
import json
import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .console import AdminConsole, ConsoleError

MAX_BODY_BYTES = 64 * 1024
WEB_ROOT = Path(__file__).resolve().parent / "web"
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}
STATIC_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                               "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                               "base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}

ROUTES = []


def route(method, pattern, auth="admin"):
    def register(handler):
        ROUTES.append((method, re.compile(f"^{pattern}$"), handler, auth))
        return handler
    return register


def _json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral() else float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _dump(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=_json_default))


@route("GET", r"/healthz", auth=False)
def healthz(console, actor, body, query):
    return 200, {"status": "ok", "service": "admin"}


@route("POST", r"/v1/admin/login", auth=False)
def admin_login(console, actor, body, query):
    return 200, console.login(body.get("username"), body.get("password"))


@route("GET", r"/v1/admin/reviews")
def admin_reviews(console, actor, body, query):
    return 200, console.list_reviews(query.get("domain") or None)


@route("GET", r"/v1/admin/reviews/(?P<review_id>[\w-]+)")
def admin_review(console, actor, body, query, review_id):
    return 200, console.get_review(review_id)


@route("POST", r"/v1/admin/reviews/(?P<review_id>[\w-]+)/approve")
def admin_approve(console, actor, body, query, review_id):
    return 200, console.approve(actor, review_id, body.get("note"), body.get("selections"))


@route("POST", r"/v1/admin/reviews/(?P<review_id>[\w-]+)/reject")
def admin_reject(console, actor, body, query, review_id):
    return 200, console.reject(actor, review_id, body.get("note"))


def static_file(path):
    relative = path.partition("?")[0].lstrip("/") or "admin.html"
    candidate = (WEB_ROOT / relative).resolve()
    if not candidate.is_relative_to(WEB_ROOT) or not candidate.is_file():
        candidate = WEB_ROOT / "admin.html"
    content_type = STATIC_TYPES.get(candidate.suffix)
    if not content_type or not candidate.is_file():
        return None
    return candidate.read_bytes(), content_type


def dispatch(console, method, path, headers, raw_body):
    trace_id = str(uuid.uuid4())
    path, _, query_string = path.partition("?")
    query = dict(part.split("=", 1) for part in query_string.split("&") if "=" in part)
    try:
        if len(raw_body) > MAX_BODY_BYTES:
            raise ConsoleError("PAYLOAD_TOO_LARGE", "请求体过大", 413)
        try:
            body = json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            raise ConsoleError("INVALID_JSON", "请求体不是合法 JSON", 400)
        if not isinstance(body, dict):
            raise ConsoleError("INVALID_JSON", "请求体必须是 JSON 对象", 400)
        for verb, pattern, handler, auth in ROUTES:
            match = pattern.match(path)
            if match and verb == method:
                actor = None
                if auth != "admin":
                    pass  # public route
                else:
                    token = (headers.get("Authorization") or "").removeprefix("Bearer ").strip()
                    actor = console.actor_for_token(token)
                status, payload = handler(console, actor, body, query, **match.groupdict())
                payload = _dump(payload)
                if isinstance(payload, dict):
                    payload.setdefault("trace_id", trace_id)
                return status, payload
        raise ConsoleError("NOT_FOUND", "接口不存在", 404)
    except ConsoleError as exc:
        return exc.http_status, {"code": exc.code, "message": exc.message,
                                 "retryable": exc.code in ("ADMIN_UNAUTHORIZED",), "trace_id": trace_id}
    except (ValueError, TypeError):
        return 400, {"code": "INVALID_INPUT", "message": "请求参数不合法", "retryable": False,
                     "trace_id": trace_id}
    except Exception:
        return 500, {"code": "INTERNAL", "message": "服务内部错误", "retryable": True,
                     "trace_id": trace_id}


def make_handler(console):
    class Handler(BaseHTTPRequestHandler):
        def _handle(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, MAX_BODY_BYTES + 1)) if length else b""
            status, payload = dispatch(console, method, self.path, self.headers, raw)
            data = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.partition("?")[0]
            # 已注册的 GET API 路由（含 /healthz）优先，避免被静态文件 fallback 吞掉
            for verb, pattern, handler, auth in ROUTES:
                if verb == "GET" and pattern.match(path):
                    return self._handle("GET")
            found = static_file(self.path)
            if not found:
                return self._handle("GET")
            data, content_type = found
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            for name, value in STATIC_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            self._handle("POST")

        def log_message(self, fmt, *args):
            pass

    return Handler


def serve(console=None, host="127.0.0.1", port=8765):
    console = console or AdminConsole()
    server = ThreadingHTTPServer((host, port), make_handler(console))
    print(f"[admin_console] listening on http://{host}:{port}  (数据审核后台)")
    server.serve_forever()
