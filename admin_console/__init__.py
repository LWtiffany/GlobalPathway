"""Standalone data-review console for Agent1（管理端审核后台）.

Self-contained package: it connects to the shared PostgreSQL database (team03)
through the SSH tunnel started by ``scripts/start_team03_pg_tunnel.ps1`` and does
NOT import ``agent1``/``agent2`` — the approve/reject logic is ported here.

Run::

    python -m admin_console admin add <用户名>   # create an admin account
    python -m admin_console serve                # HTTP console at 127.0.0.1:8765
"""

__version__ = "1.0.0"
