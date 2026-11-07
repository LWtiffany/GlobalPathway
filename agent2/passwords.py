"""Password hashing for Agent2 end-user accounts.

Supports:
  - scrypt$N$r$p$salt_hex$digest_hex  (legacy RDS rows)
  - pbkdf2_sha256$iterations$salt_hex$digest_hex  (preferred new writes)
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

PBKDF2_ITERATIONS = 600_000
MIN_PASSWORD = 8
MAX_PASSWORD = 16
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN = 16384, 8, 1, 32


def password_length_ok(password: str) -> bool:
    return isinstance(password, str) and MIN_PASSWORD <= len(password) <= MAX_PASSWORD


def password_length_error() -> str:
    return f"密码长度须为 {MIN_PASSWORD}–{MAX_PASSWORD} 位"


def hash_password(password: str) -> str:
    if not password_length_ok(password):
        raise ValueError(password_length_error())
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not isinstance(password, str) or not isinstance(stored, str) or not stored:
        return False
    try:
        scheme, *parts = stored.split("$")
        if scheme == "pbkdf2_sha256" and len(parts) == 3:
            iterations, salt_hex, digest_hex = parts
            actual = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
            return hmac.compare_digest(actual.hex(), digest_hex)
        if scheme == "scrypt" and len(parts) == 5:
            n, r, p, salt_hex, digest_hex = parts
            actual = hashlib.scrypt(
                password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                n=int(n), r=int(r), p=int(p), dklen=len(digest_hex) // 2)
            return hmac.compare_digest(actual.hex(), digest_hex)
    except (ValueError, TypeError):
        return False
    return False


def needs_rehash(stored: str) -> bool:
    return isinstance(stored, str) and not stored.startswith("pbkdf2_sha256$")


def dummy_hash() -> str:
    """Stable-cost hash for timing-safe unknown-user checks."""
    return hash_password(secrets.token_hex(8))
