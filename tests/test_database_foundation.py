"""Offline foundation tests. Every write is confined to temporary directories."""
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agent1 import config, db, storage
from agent1.environment import cleanup_test_storage, paths_for


class DatabaseFoundationTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="agent1-foundation-suite-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.paths = paths_for(self.root, "development")

    def test_status_does_not_create_database_or_directories(self):
        self.assertFalse(storage.status(self.paths)["initialized"])
        self.assertFalse(self.paths.data.exists())

    def test_init_is_idempotent_and_has_no_legacy_tables(self):
        first = storage.initialize(self.paths)
        second = storage.initialize(self.paths)
        self.assertEqual(first["migrations"], second["migrations"])
        self.assertIn("visa_policy_revision", first["tables"])
        self.assertIn("fx_policy", first["tables"])
        self.assertIn("tuition", first["tables"])
        self.assertNotIn("knowledge_versions", first["tables"])
        self.assertNotIn("runs", first["tables"])
        with storage.session(self.paths) as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM sources").fetchone()[0], 0)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_development_and_production_are_separate(self):
        production = paths_for(self.root, "production")
        storage.initialize(self.paths)
        self.assertNotEqual(self.paths.database, production.database)
        self.assertFalse(production.database.exists())
        self.assertNotEqual(self.paths.database, self.paths.legacy_database)

    def test_test_environment_is_temporary(self):
        paths = paths_for(self.root, "test")
        self.addCleanup(cleanup_test_storage)
        self.assertFalse(paths.data.is_relative_to(self.root))
        storage.initialize(paths)
        cleanup_test_storage()
        self.assertFalse(paths.data.exists())

    def test_wrong_environment_marker_is_rejected(self):
        storage.initialize(self.paths)
        with closing(sqlite3.connect(self.paths.database)) as connection:
            connection.execute("UPDATE database_identity SET environment='production'")
            connection.commit()
        with self.assertRaises(RuntimeError):
            storage.initialize(self.paths)
        with self.assertRaises(RuntimeError):
            storage.status(self.paths)

    def test_existing_mixed_database_is_not_upgraded(self):
        self.paths.data.mkdir(parents=True)
        with closing(sqlite3.connect(self.paths.database)) as connection:
            connection.execute("CREATE TABLE runs(id INTEGER PRIMARY KEY)")
            connection.execute("INSERT INTO runs VALUES(1)")
            connection.commit()
        with self.assertRaises(RuntimeError):
            storage.initialize(self.paths)
        with closing(sqlite3.connect(self.paths.database)) as connection:
            self.assertEqual(connection.execute("SELECT * FROM runs").fetchall(), [(1,)])
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("runs",)])

    def test_legacy_session_cannot_write_production(self):
        # Patch only the environment guard; no global path points at a real production DB.
        with patch.object(config, "ENVIRONMENT", "production"):
            with self.assertRaises(RuntimeError):
                with db.session():
                    self.fail("production must reject legacy connections")

    def test_readonly_connection_rejects_writes(self):
        storage.initialize(self.paths)
        with storage.session(self.paths) as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM database_identity")

    def test_partial_migration_is_rolled_back(self):
        migrations = self.root / "migrations"
        migrations.mkdir()
        (migrations / "001_broken.sql").write_text(
            "CREATE TABLE half_done(id INTEGER);\nINVALID SQL;\n", encoding="utf-8")
        with patch.object(storage, "MIGRATIONS", migrations):
            with self.assertRaises(sqlite3.Error):
                storage.initialize(self.paths)
        with closing(sqlite3.connect(self.paths.database)) as connection:
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])

    def test_dangling_review_is_rejected(self):
        storage.initialize(self.paths)
        with storage.session(self.paths, readonly=False) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("""INSERT INTO review_tasks
                    (review_id,candidate_id,reason,status,created_at)
                    VALUES('review','missing','test','OPEN','2026-10-13T00:00:00+00:00')""")

    def test_mutated_applied_migration_is_rejected(self):
        storage.initialize(self.paths)
        migrations = self.root / "changed-migrations"
        migrations.mkdir()
        for original in storage.MIGRATIONS.glob("*.sql"):
            (migrations / original.name).write_text(original.read_text(encoding="utf-8") + "\n-- changed\n", encoding="utf-8")
        with patch.object(storage, "MIGRATIONS", migrations):
            with self.assertRaises(RuntimeError):
                storage.initialize(self.paths)

    def test_audit_is_append_only(self):
        storage.initialize(self.paths)
        with storage.session(self.paths, readonly=False) as connection:
            connection.execute("""INSERT INTO audit_events
                (event_id,transaction_id,actor,action,reason,detail_json,occurred_at)
                VALUES('event','tx','tester','test','constraint check','{}','2026-10-13T00:00:00+00:00')""")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE audit_events SET reason='changed'")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM audit_events")


if __name__ == "__main__":
    unittest.main()
