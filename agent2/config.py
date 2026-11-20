"""Agent2 runtime configuration.

Customer state is stored in a dedicated PostgreSQL schema.  There is no local
database fallback: a missing password or an unreachable RDS endpoint must fail
startup instead of silently creating a second source of truth.
"""
import os

from agent1 import config as agent1_config


DB_HOST = os.getenv("AGENT2_DB_HOST", agent1_config.DB_HOST)
DB_PORT = int(os.getenv("AGENT2_DB_PORT", str(agent1_config.DB_PORT)))
DB_NAME = os.getenv("AGENT2_DB_NAME", agent1_config.DB_NAME)
DB_USER = os.getenv("AGENT2_DB_USER", agent1_config.DB_USER)
DB_PASSWORD = os.getenv("AGENT2_DB_PASSWORD", agent1_config.DB_PASSWORD)
DB_SCHEMA = os.getenv("AGENT2_DB_SCHEMA", "agent2")
USER_SCHEMA = os.getenv("AGENT2_USER_SCHEMA", "userinformation")
DB_CONNECT_TIMEOUT = int(os.getenv("AGENT2_DB_CONNECT_TIMEOUT", "10"))


def connection_kwargs():
    if not DB_PASSWORD:
        raise RuntimeError(
            "AGENT2_DB_PASSWORD 未配置；Agent2 不再回退到本地 SQLite。"
        )
    return {
        "host": DB_HOST,
        "port": DB_PORT,
        "dbname": DB_NAME,
        "user": DB_USER,
        "password": DB_PASSWORD,
        "connect_timeout": DB_CONNECT_TIMEOUT,
        # TCP keepalives so idle NAT/firewall drops are noticed; the store
        # still reconnects if the server has already closed the session.
        "keepalives": 1,
        "keepalives_idle": int(os.getenv("AGENT2_DB_KEEPALIVES_IDLE", "30")),
        "keepalives_interval": int(os.getenv("AGENT2_DB_KEEPALIVES_INTERVAL", "10")),
        "keepalives_count": int(os.getenv("AGENT2_DB_KEEPALIVES_COUNT", "3")),
    }
