"""Agent 2 HTTP API（S1 最小集，标准库实现）。

认证：POST /v1/demo/login 得到 token，之后带 `Authorization: Bearer <token>`。
错误统一为 {"code","message","retryable","trace_id"}，不含内部堆栈。
"""
import json
import re
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from . import clarify
from .service import ServiceError

MAX_BODY_BYTES = 64 * 1024

ROUTES = []


def route(method, pattern, auth=True):
    def register(handler):
        ROUTES.append((method, re.compile(f"^{pattern}$"), handler, auth))
        return handler
    return register


def _dump(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_dump(v) for v in value]
    if isinstance(value, dict):
        return {k: _dump(v) for k, v in value.items()}
    # psycopg returns native datetime/date/Decimal/UUID objects while the old
    # SQLite adapter mostly returned strings and floats.  Normalize every leaf
    # at the HTTP boundary so one raw RDS column cannot abort the connection.
    return to_jsonable_python(value)


@route("GET", r"/healthz", auth=False)
def healthz(service, user_id, body, query):
    """Process-level health check; it deliberately does not expose user data."""
    return 200, {"status": "ok", "service": "user"}


@route("GET", r"/readyz", auth=False)
def readyz(service, user_id, body, query):
    """Readiness requires the configured RDS state store to be reachable."""
    service.store.one("SELECT 1 AS ok")
    return 200, {"status": "ready", "service": "user", "storage": "rds"}


@route("POST", r"/v1/demo/login", auth=False)
def demo_login(service, user_id, body, query):
    return 200, service.demo_login(body.get("user"))


@route("GET", r"/v1/me")
def me(service, user_id, body, query):
    return 200, service.me(user_id)


@route("POST", r"/v1/profiles")
def create_profile(service, user_id, body, query):
    return 201, service.create_profile(user_id, body.get("fields") or {},
                                       synthetic=bool(body.get("synthetic")),
                                       display_name=body.get("display_name"))


@route("GET", r"/v1/profiles/(?P<profile_id>[\w-]+)")
def get_profile(service, user_id, body, query, profile_id):
    return 200, service.get_profile(user_id, profile_id)


@route("PATCH", r"/v1/profiles/(?P<profile_id>[\w-]+)")
def patch_profile(service, user_id, body, query, profile_id):
    return 200, service.patch_profile(user_id, profile_id, expected_version=body.get("expected_version"),
                                      set_fields=body.get("fields") or {},
                                      clear_fields=body.get("clear_fields") or [])


@route("GET", r"/v1/profiles/(?P<profile_id>[\w-]+)/proposals")
def list_proposals(service, user_id, body, query, profile_id):
    return 200, service.list_proposals(user_id, profile_id)


@route("POST", r"/v1/profile-proposals/(?P<proposal_id>[\w-]+)/confirm")
def confirm_proposal(service, user_id, body, query, proposal_id):
    return 200, service.confirm_proposal(
        user_id, proposal_id, expected_version=body.get("expected_version"),
        refresh_topics=body.get("refresh_topics") or [])


@route("POST", r"/v1/profile-proposals/(?P<proposal_id>[\w-]+)/dismiss")
def dismiss_proposal(service, user_id, body, query, proposal_id):
    return 200, service.dismiss_proposal(user_id, proposal_id)


@route("POST", r"/v1/profile-proposals/confirm-batch")
def confirm_proposals_batch(service, user_id, body, query):
    return 200, service.confirm_proposals(
        user_id, body.get("proposal_ids") or [],
        expected_version=body.get("expected_version"),
        refresh_topics=body.get("refresh_topics") or [])


@route("GET", r"/v1/profiles/(?P<profile_id>[\w-]+)/bank-relationships")
def list_bank_relationships(service, user_id, body, query, profile_id):
    return 200, service.list_bank_relationships(user_id, profile_id)


@route("GET", r"/v1/profiles/(?P<profile_id>[\w-]+)/bank-relationship-proposals")
def list_bank_relationship_proposals(service, user_id, body, query, profile_id):
    return 200, service.list_bank_relationship_proposals(user_id, profile_id)


@route("POST", r"/v1/bank-relationship-proposals/(?P<proposal_id>[\w-]+)/confirm")
def confirm_bank_relationship_proposal(service, user_id, body, query, proposal_id):
    return 200, service.confirm_bank_relationship_proposal(
        user_id, proposal_id, expected_version=body.get("expected_version"),
        proposed_status=body.get("status"))


@route("POST", r"/v1/bank-relationship-proposals/(?P<proposal_id>[\w-]+)/dismiss")
def dismiss_bank_relationship_proposal(service, user_id, body, query, proposal_id):
    return 200, service.dismiss_bank_relationship_proposal(user_id, proposal_id)


@route("POST", r"/v1/conversations")  # compatibility alias
@route("POST", r"/v1/sessions")
def create_conversation(service, user_id, body, query):
    return 201, service.create_conversation(user_id, body.get("profile_id"))


@route("GET", r"/v1/conversations")  # compatibility alias
@route("GET", r"/v1/sessions")
def list_conversations(service, user_id, body, query):
    return 200, service.list_conversations(user_id)


@route("GET", r"/v1/conversations/(?P<conversation_id>[\w-]+)/messages")  # compatibility alias
@route("GET", r"/v1/sessions/(?P<conversation_id>[\w-]+)/messages")
def list_messages(service, user_id, body, query, conversation_id):
    return 200, service.list_messages(user_id, conversation_id, after_seq=int(query.get("after_seq", 0)),
                                      limit=int(query.get("limit", 50)))


@route("POST", r"/v1/conversations/(?P<conversation_id>[\w-]+)/messages")  # compatibility alias
@route("POST", r"/v1/sessions/(?P<conversation_id>[\w-]+)/messages")
def post_message(service, user_id, body, query, conversation_id):
    return 200, service.post_message(
        user_id, conversation_id, body.get("text"), body.get("client_message_id"),
        confirmed_fields=body.get("confirmed_fields") or {},
        session_fields=body.get("session_fields") or {},
        expected_profile_version=body.get("expected_profile_version"),
        cleared_fields=body.get("cleared_fields") or [])


@route("POST", r"/v1/messages/(?P<message_id>[\w-]+)/reaction")
def react(service, user_id, body, query, message_id):
    return 200, service.react(user_id, message_id, body.get("kind"))


@route("GET", r"/v1/profiles/(?P<profile_id>[\w-]+)/advice/(?P<topic>[\w-]+)")
def get_advice(service, user_id, body, query, profile_id, topic):
    return 200, service.get_advice(user_id, profile_id, topic)


@route("POST", r"/v1/profiles/(?P<profile_id>[\w-]+)/advice/(?P<topic>[\w-]+)/refresh")
def refresh_advice(service, user_id, body, query, profile_id, topic):
    return 200, service.refresh_advice(user_id, profile_id, topic)


@route("GET", r"/v1/profiles/(?P<profile_id>[\w-]+)/advice-refresh-jobs")
def list_advice_refresh_jobs(service, user_id, body, query, profile_id):
    return 200, service.list_refresh_jobs(user_id, profile_id, query.get("status"))


@route("GET", r"/v1/knowledge/snapshots/(?P<revision_id>[\w-]+)")
def snapshot(service, user_id, body, query, revision_id):
    return 200, service.knowledge_snapshot(revision_id)


@route("GET", r"/v1/fields", auth=False)
def fields(service, user_id, body, query):
    # Return the persistence policy together with the input schema so the UI
    # can distinguish durable Profile facts from turn/session-only choices.
    return 200, {"fields": clarify.field_definitions()}


@route("GET", r"/v1/schools", auth=False)
def list_schools(service, user_id, body, query):
    from . import catalog
    country = (query.get("country") or "").strip() or None
    return 200, {"schools": catalog.list_schools(country=country)}


@route("GET", r"/v1/schools/(?P<school_id>[\w-]+)/programs", auth=False)
def list_programs(service, user_id, body, query, school_id):
    from . import catalog
    return 200, {"programs": catalog.list_programs(school_id)}


@route("GET", r"/v1/profiles")
def list_profiles(service, user_id, body, query):
    return 200, service.list_profiles(user_id)


@route("POST", r"/v1/profiles/(?P<profile_id>[\w-]+)/conversation")  # compatibility alias
@route("POST", r"/v1/profiles/(?P<profile_id>[\w-]+)/session")
def profile_conversation(service, user_id, body, query, profile_id):
    return 200, service.profile_conversation(user_id, profile_id)


@route("GET", r"/v1/profiles/(?P<profile_id>[\w-]+)/advice")
def list_advice(service, user_id, body, query, profile_id):
    return 200, service.list_advice(user_id, profile_id)


@route("POST", r"/v1/messages/(?P<message_id>[\w-]+)/feedback")
def submit_feedback(service, user_id, body, query, message_id):
    return 200, service.submit_feedback(user_id, message_id, body.get("kind"), body.get("note") or "")


@route("GET", r"/v1/feedback")
def list_feedback(service, user_id, body, query):
    return 200, service.list_feedback(user_id)


@route("GET", r"/v1/profiles/(?P<profile_id>[\w-]+)/plan")
def list_plan(service, user_id, body, query, profile_id):
    return 200, service.list_plan(user_id, profile_id)


@route("POST", r"/v1/profiles/(?P<profile_id>[\w-]+)/plan")
def add_plan_item(service, user_id, body, query, profile_id):
    return 201, service.add_plan_item(user_id, profile_id, body.get("text"))


@route("PATCH", r"/v1/plan-items/(?P<item_id>[\w-]+)")
def update_plan_item(service, user_id, body, query, item_id):
    return 200, service.update_plan_item(user_id, item_id, text=body.get("text"), done=body.get("done"))


@route("DELETE", r"/v1/plan-items/(?P<item_id>[\w-]+)")
def delete_plan_item(service, user_id, body, query, item_id):
    return 200, service.delete_plan_item(user_id, item_id)


WEB_ROOT = Path(__file__).resolve().parent / "web"
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}
STATIC_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}


def static_file(path):
    """Resolve a GET path to a file under agent2/web (SPA: unknown paths fall back to index.html)."""
    relative = path.partition("?")[0].lstrip("/") or "index.html"
    candidate = (WEB_ROOT / relative).resolve()
    if not candidate.is_relative_to(WEB_ROOT) or not candidate.is_file():
        candidate = WEB_ROOT / "index.html"
    content_type = STATIC_TYPES.get(candidate.suffix)
    if not content_type or not candidate.is_file():
        return None
    return candidate.read_bytes(), content_type


def dispatch(service, method, path, headers, raw_body):
    """Pure request handler (also used by tests): returns (status, payload dict)."""
    trace_id = str(uuid.uuid4())
    path, _, query_string = path.partition("?")
    query = dict(part.split("=", 1) for part in query_string.split("&") if "=" in part)
    try:
        if len(raw_body) > MAX_BODY_BYTES:
            raise ServiceError("PAYLOAD_TOO_LARGE", "请求体过大", 413)
        try:
            body = json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            raise ServiceError("INVALID_JSON", "请求体不是合法 JSON", 400)
        if not isinstance(body, dict):
            raise ServiceError("INVALID_JSON", "请求体必须是 JSON 对象", 400)
        for verb, pattern, handler, auth in ROUTES:
            match = pattern.match(path)
            if match and verb == method:
                user_id = None
                if auth:
                    token = (headers.get("Authorization") or "").removeprefix("Bearer ").strip()
                    user_id = service.user_for_token(token)
                status, payload = handler(service, user_id, body, query, **match.groupdict())
                payload = _dump(payload)
                if isinstance(payload, dict):
                    payload.setdefault("trace_id", trace_id)
                else:
                    payload = {"items": payload, "trace_id": trace_id}
                return status, payload
        raise ServiceError("NOT_FOUND", "接口不存在", 404)
    except ServiceError as exc:
        return exc.http_status, {"code": exc.code, "message": exc.message, "retryable": exc.retryable,
                                 "trace_id": trace_id}
    except (ValueError, TypeError):
        return 400, {"code": "INVALID_INPUT", "message": "请求参数不合法", "retryable": False, "trace_id": trace_id}
    except Exception:
        # The stack must stay out of the HTTP response, but a silent 500 is
        # otherwise impossible to diagnose from the dev console.
        traceback.print_exc()
        return 500, {"code": "INTERNAL", "message": "服务内部错误", "retryable": True, "trace_id": trace_id}


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        def _respond(self, status, data, content_type, extra_headers=()):
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                for name, value in extra_headers:
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # Client aborted (refresh / fetch timeout); nothing left to deliver.

        def _handle(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, MAX_BODY_BYTES + 1)) if length else b""
            status, payload = dispatch(service, method, self.path, self.headers, raw)
            # Keep a final serialization guard even though dispatch already
            # normalizes payloads.  This protects responses containing a nested
            # value returned by a custom mapping/model implementation.
            data = json.dumps(
                payload, ensure_ascii=False, default=to_jsonable_python
            ).encode("utf-8")
            self._respond(status, data, "application/json; charset=utf-8")

        def do_GET(self):
            path = self.path.partition("?")[0]
            # Registered API routes (including /healthz) must win over the SPA fallback.
            if path.startswith("/v1/") or any(
                    verb == "GET" and pattern.match(path)
                    for verb, pattern, handler, auth in ROUTES):
                return self._handle("GET")
            found = static_file(self.path)
            if not found:
                return self._handle("GET")
            data, content_type = found
            self._respond(200, data, content_type, STATIC_HEADERS.items())

        def do_POST(self):
            self._handle("POST")

        def do_PATCH(self):
            self._handle("PATCH")

        def do_DELETE(self):
            self._handle("DELETE")

        def log_message(self, fmt, *args):
            pass

    return Handler


def serve(service, host="127.0.0.1", port=8765):
    server = ThreadingHTTPServer((host, port), make_handler(service))
    print(f"[agent2] listening on http://{host}:{port}  (Demo 模拟身份；前端页面同址打开)")
    server.serve_forever()
