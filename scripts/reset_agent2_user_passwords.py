#!/usr/bin/env python3
"""Reset all RDS userinformation.users passwords (destructive).

Usage:
  AGENT1_DB_PASSWORD=... .venv/bin/python scripts/reset_agent2_user_passwords.py 'DemoPass123!'
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main(argv=None):
    argv = list(argv or sys.argv[1:])
    if not argv:
        print("usage: reset_agent2_user_passwords.py <new-password>", file=sys.stderr)
        return 2
    password = argv[0]
    from agent2.passwords import MAX_PASSWORD, MIN_PASSWORD, password_length_ok
    if not password_length_ok(password):
        print(f"password length must be {MIN_PASSWORD}–{MAX_PASSWORD}", file=sys.stderr)
        return 2
    if not os.environ.get("AGENT1_DB_PASSWORD"):
        try:
            from local_secrets import AGENT1_DB_PASSWORD  # type: ignore
            if AGENT1_DB_PASSWORD:
                os.environ["AGENT1_DB_PASSWORD"] = AGENT1_DB_PASSWORD
        except ImportError:
            pass
    from agent1 import config
    if not config.DB_PASSWORD and os.environ.get("AGENT1_DB_PASSWORD"):
        config.DB_PASSWORD = os.environ["AGENT1_DB_PASSWORD"]
    from agent2.accounts import reset_all_passwords
    n = reset_all_passwords(password)
    print(f"reset {n} user password(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
