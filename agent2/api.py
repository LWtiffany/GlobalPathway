"""Agent 2 HTTP API（S1 最小集，标准库实现）。

认证：POST /v1/demo/login 得到 token，之后带 `Authorization: Bearer <token>`。
错误统一为 {"code","message","retryable","trace_id"}，不含内部堆栈。
"""
import json
import re
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pydantic import BaseModel

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
    return value


@route("POST", r"/v1/demo/login", auth=False)
def demo_login(service, user_id, body, query):
    return 200, service.demo_login(body.get("user"))


@route("GET", r"/v1/me")
def me(service, user_id, body, query):
    return 200, service.me(user_id)


@route("POST", r"/v1/profiles")
def create_profile(service, user_id, body, query):
    return 201, service.create_profile(user_id, body.get("fields") or {}, synthetic=bool(body.get("synthetic")))


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
    return 200, service.confirm_proposal(user_id, proposal_id, expected_version=body.get("expected_version"))


@route("POST", r"/v1/profile-proposals/(?P<proposal_id>[\w-]+)/dismiss")
def dismiss_proposal(service, user_id, body, query, proposal_id):
    return 200, service.dismiss_proposal(user_id, proposal_id)


@route("POST", r"/v1/conversations")
def create_conversation(service, user_id, body, query):
    return 201, service.create_conversation(user_id, body.get("profile_id"))


@route("GET", r"/v1/conversations")
def list_conversations(service, user_id, body, query):
    return 200, service.list_conversations(user_id)


@route("GET", r"/v1/conversations/(?P<conversation_id>[\w-]+)/messages")
def list_messages(service, user_id, body, query, conversation_id):
    return 200, service.list_messages(user_id, conversation_id, after_seq=int(query.get("after_seq", 0)),
                                      limit=int(query.get("limit", 50)))


@route("POST", r"/v1/conversations/(?P<conversation_id>[\w-]+)/messages")
def post_message(service, user_id, body, query, conversation_id):
    return 200, service.post_message(user_id, conversation_id, body.get("text"), body.get("client_message_id"))


@route("POST", r"/v1/messages/(?P<message_id>[\w-]+)/reaction")
def react(service, user_id, body, query, message_id):
    return 200, service.react(user_id, message_id, body.get("kind"))


@route("GET", r"/v1/profiles/(?P<profile_id>[\w-]+)/advice/(?P<topic>[\w-]+)")
def get_advice(service, user_id, body, query, profile_id, topic):
    return 200, service.get_advice(user_id, profile_id, topic)


@route("GET", r"/v1/knowledge/snapshots/(?P<revision_id>[\w-]+)")
def snapshot(service, user_id, body, query, revision_id):
    return 200, service.knowledge_snapshot(revision_id)


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
        return 500, {"code": "INTERNAL", "message": "服务内部错误", "retryable": True, "trace_id": trace_id}


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        def _handle(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, MAX_BODY_BYTES + 1)) if length else b""
            status, payload = dispatch(service, method, self.path, self.headers, raw)
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PATCH(self):
            self._handle("PATCH")

        def log_message(self, fmt, *args):
            pass

    return Handler


def serve(service, host="127.0.0.1", port=8765):
    server = ThreadingHTTPServer((host, port), make_handler(service))
    print(f"[agent2] listening on http://{host}:{port}  (Demo 模拟身份，资料为合成夹具)")
    server.serve_forever()
