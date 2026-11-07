"""Apply or verify the reviewed HSBC deposit-account tables in shared RDS.

The connection uses agent1.config, which defaults locally to the SSH tunnel at
127.0.0.1:15432 and reads the password from local_secrets.py when no environment
variable is set. No credential is printed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent1 import config, storage  # noqa: E402

MIGRATIONS = ROOT / "agent1" / "migrations"
TABLES = ("ACCOUNT_PRODUCT", "ACCOUNT_OPENING_ROUTE", "ACCOUNT_ELIGIBILITY_RULE",
          "ACCOUNT_REQUIRED_DOCUMENT", "ACCOUNT_FEE")


def inspect(connection) -> dict[str, int]:
    missing = [table for table in TABLES if not connection.execute(
        "SELECT to_regclass(%s) AS name", (f"public.{table.lower()}",)
    ).fetchone()["name"]]
    if missing:
        raise RuntimeError("missing RDS tables: " + ", ".join(missing))
    return {table: int(connection.execute(
        f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]) for table in TABLES}


def main() -> None:
    parser = argparse.ArgumentParser(description="同步或检查汇丰普通账户 RDS 数据")
    parser.add_argument("--apply", action="store_true",
                        help="幂等应用 016 schema 和 seed；省略时只读检查")
    parser.add_argument("--describe", action="store_true",
                        help="只读显示现有开户表字段，用于兼容诊断")
    parser.add_argument("--data", action="store_true",
                        help="只读显示现有开户产品、渠道、规则和费用")
    args = parser.parse_args()
    connection = storage._connect()
    try:
        if args.data:
            for table in ("ACCOUNT_PRODUCT", "ACCOUNT_OPENING_ROUTE",
                          "ACCOUNT_ELIGIBILITY_RULE", "ACCOUNT_FEE"):
                for row in connection.execute(f"SELECT * FROM {table} ORDER BY account_id"):
                    print(f"{table.lower()}:", dict(row))
            return
        if args.describe:
            account_tables = connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                "AND tablename LIKE 'account%' ORDER BY tablename").fetchall()
            print("account tables:", ", ".join(row["tablename"] for row in account_tables))
            constraints = connection.execute(
                """SELECT conname, pg_get_constraintdef(oid) AS definition
                   FROM pg_constraint
                   WHERE conrelid IN ('account_product'::regclass,
                                      'account_opening_route'::regclass,
                                      'account_eligibility_rule'::regclass,
                                      'account_required_document'::regclass,
                                      'account_fee'::regclass)
                   ORDER BY conname""").fetchall()
            for row in constraints:
                print(f"constraint {row['conname']}: {row['definition']}")
            rows = connection.execute(
                """SELECT table_name, column_name, data_type, is_nullable
                   FROM information_schema.columns
                   WHERE table_schema='public' AND table_name = ANY(%s)
                   ORDER BY table_name, ordinal_position""",
                ([table.lower() for table in TABLES],),
            ).fetchall()
            for row in rows:
                print(f"{row['table_name']}.{row['column_name']} "
                      f"{row['data_type']} nullable={row['is_nullable']}")
            rule_columns = connection.execute(
                """SELECT column_name, data_type, is_nullable
                   FROM information_schema.columns
                   WHERE table_schema='public' AND table_name='account_eligibility_rule'
                   ORDER BY ordinal_position""").fetchall()
            for row in rule_columns:
                print(f"account_eligibility_rule.{row['column_name']} "
                      f"{row['data_type']} nullable={row['is_nullable']}")
            if rule_columns:
                sample_rules = connection.execute(
                    "SELECT * FROM account_eligibility_rule ORDER BY account_id, rule_id LIMIT 30"
                ).fetchall()
                for rule in sample_rules:
                    print("rule:", dict(rule))
            for table in TABLES:
                exists = connection.execute("SELECT to_regclass(%s) AS name",
                                            (f"public.{table.lower()}",)).fetchone()["name"]
                if exists:
                    count = connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                    print(f"{table}: {count} rows")
            if connection.execute("SELECT to_regclass('public.account_product') AS name").fetchone()["name"]:
                products = connection.execute(
                    "SELECT account_id, product_name, market, product_status FROM ACCOUNT_PRODUCT "
                    "ORDER BY account_id").fetchall()
                for product in products:
                    print("product:", dict(product))
                for table in ("ACCOUNT_OPENING_ROUTE", "ACCOUNT_FEE"):
                    for row in connection.execute(f"SELECT * FROM {table} ORDER BY account_id"):
                        print(f"{table.lower()}:", dict(row))
            return
        if args.apply:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (16016,))
            existing_schema = connection.execute(
                """SELECT EXISTS (
                       SELECT 1 FROM information_schema.columns
                       WHERE table_schema='public' AND table_name='account_product'
                         AND column_name='product_code') AS present""").fetchone()["present"]
            if existing_schema:
                migration = (MIGRATIONS / "017_hsbc_hk_children_savings_rds.sql").read_text(
                    encoding="utf-8")
                connection.execute(migration)
            else:
                schema = (MIGRATIONS / "016_hsbc_deposit_accounts.sql").read_text(encoding="utf-8")
                seed = (MIGRATIONS / "016_hsbc_deposit_accounts_seed.sql").read_text(encoding="utf-8")
                connection.execute(schema)
                connection.execute(seed)
            connection.commit()
        counts = inspect(connection)
        print(f"RDS connected: {config.DB_HOST}:{config.DB_PORT}/{config.DB_NAME}")
        print("mode:", "applied and verified" if args.apply else "read-only check")
        for table, count in counts.items():
            print(f"{table}: {count}")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    main()
