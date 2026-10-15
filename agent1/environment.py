"""Data locations. Test storage is process-owned and never points at live data."""
import atexit
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory


ENVIRONMENTS = ("development", "production", "test")
_test_directory = None


@dataclass(frozen=True)
class EnvironmentPaths:
    environment: str
    data: Path
    database: Path
    admin_database: Path
    snapshots: Path
    legacy_database: Path
    legacy_snapshots: Path
    legacy_gate: Path

    def validate(self):
        # Reject junctions/symlinks that redirect an environment into another one.
        if self.data.resolve() != self.data.absolute():
            raise ValueError(f"环境目录不能通过链接重定向：{self.data}")
        for path in (self.database, self.admin_database, self.snapshots, self.legacy_database,
                     self.legacy_snapshots, self.legacy_gate):
            if path.resolve() != path.absolute() or not path.is_relative_to(self.data):
                raise ValueError(f"数据路径越过环境边界：{path}")


def cleanup_test_storage():
    global _test_directory
    if _test_directory is not None:
        _test_directory.cleanup()
        _test_directory = None


atexit.register(cleanup_test_storage)


def paths_for(root, environment):
    global _test_directory
    if environment not in ENVIRONMENTS:
        raise ValueError(f"未知环境 {environment!r}，可选：{', '.join(ENVIRONMENTS)}")
    if environment == "test":
        if _test_directory is None:
            _test_directory = TemporaryDirectory(prefix="globalpathway-agent1-test-")
        data = Path(_test_directory.name).resolve()
    else:
        data = Path(root).resolve() / "data" / environment
    paths = EnvironmentPaths(
        environment=environment, data=data, database=data / "agent1.sqlite3",
        admin_database=data / "admin.sqlite3", snapshots=data / "snapshots", legacy_database=data / "legacy" / "agent1.sqlite3",
        legacy_snapshots=data / "legacy" / "snapshots", legacy_gate=data / "legacy" / "gate",
    )
    paths.validate()
    return paths
