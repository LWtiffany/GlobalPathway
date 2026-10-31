"""Password login, cookie session, CSRF, and lockout."""
import json
import unittest
from datetime import datetime, timedelta, timezone

from agent2 import api
from agent2.accounts import MemoryAccountDirectory
from agent2.knowledge import DEFAULT_FIXTURE, FixtureKnowledgeRead
from agent2.passwords import hash_password, verify_password
from agent2.service import Agent2Service, ServiceError
from agent2.store import Store

NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)


def fixture_records():
    return json.loads(DEFAULT_FIXTURE.read_text(encoding="utf-8"))["records"]


class PasswordHelpers(unittest.TestCase):
    def test_pbkdf2_roundtrip_and_scrypt_verify(self):
        stored = hash_password("DemoPass123!")
        self.assertTrue(stored.startswith("pbkdf2_sha256$"))
        self.assertTrue(verify_password("DemoPass123!", stored))
        self.assertFalse(verify_password("wrong", stored))


class AuthApiCases(unittest.TestCase):
    def setUp(self):
        knowledge = FixtureKnowledgeRead(fixture_records(), "test")
        store = Store()
        self.addCleanup(store.close)
        accounts = MemoryAccountDirectory([{
            "user_id": "user_demo_001",
            "username": "demo_lin",
            "password_hash": hash_password("DemoPass123!"),
            "external_subject": "rds:user_demo_001",
            "status": "active",
            "failed_attempts": 0,
            "locked_until": None,
        }])
        self.service = Agent2Service(
            store, knowledge, clock=lambda: NOW, demo_enabled=False, accounts=accounts)
        self.accounts = accounts

    def dispatch(self, method, path, body=None, headers=None):
        raw = json.dumps(body or {}).encode() if body is not None else b"{}"
        return api.dispatch(self.service, method, path, headers or {}, raw)

    def test_password_login_sets_cookie_and_csrf(self):
        status, payload, headers = self.dispatch(
            "POST", "/v1/login", {"username": "demo_lin", "password": "DemoPass123!"})
        self.assertEqual(status, 200)
        self.assertIn("csrf_token", payload)
        self.assertNotIn("token", payload)
        cookie = dict(headers).get("Set-Cookie", "")
        self.assertIn("gp_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        status, me, _ = self.dispatch(
            "GET", "/v1/me", headers={"Cookie": cookie.split(";", 1)[0]})
        self.assertEqual(status, 200)
        self.assertEqual(me["user_id"], "user_demo_001")
        self.assertEqual(me["csrf_token"], payload["csrf_token"])

    def test_cookie_mutating_requires_csrf_and_origin(self):
        _, login, headers = self.dispatch(
            "POST", "/v1/login", {"username": "demo_lin", "password": "DemoPass123!"})
        cookie = dict(headers)["Set-Cookie"].split(";", 1)[0]
        status, payload, _ = self.dispatch(
            "POST", "/v1/profiles", {"fields": {}, "synthetic": True},
            headers={"Cookie": cookie, "Host": "localhost:8787",
                     "Origin": "http://localhost:8787"})
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "CSRF_DENIED")
        status, payload, _ = self.dispatch(
            "POST", "/v1/profiles", {"fields": {}, "synthetic": True},
            headers={"Cookie": cookie, "Host": "localhost:8787",
                     "Origin": "http://localhost:8787",
                     "X-CSRF-Token": login["csrf_token"]})
        self.assertEqual(status, 201)

    def test_bad_password_and_lockout(self):
        for _ in range(5):
            status, payload, _ = self.dispatch(
                "POST", "/v1/login", {"username": "demo_lin", "password": "WrongPass1"})
            self.assertEqual(status, 401)
            self.assertEqual(payload["code"], "INVALID_CREDENTIALS")
        status, payload, _ = self.dispatch(
            "POST", "/v1/login", {"username": "demo_lin", "password": "DemoPass123!"})
        self.assertEqual(status, 423)
        self.assertEqual(payload["code"], "ACCOUNT_LOCKED")

    def test_password_length_rejected(self):
        status, payload, _ = self.dispatch(
            "POST", "/v1/login", {"username": "demo_lin", "password": "short"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "INVALID_INPUT")
        self.assertIn("8", payload["message"])
        status, payload, _ = self.dispatch(
            "POST", "/v1/login", {"username": "demo_lin", "password": "x" * 17})
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "INVALID_INPUT")

    def test_demo_login_disabled(self):
        status, payload, _ = self.dispatch("POST", "/v1/demo/login", {"user": "alice"})
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "DEMO_DISABLED")


if __name__ == "__main__":
    unittest.main()
