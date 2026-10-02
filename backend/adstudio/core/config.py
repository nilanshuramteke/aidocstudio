"""Settings from env + optional <data_root>/config.toml. Nothing here is secret."""
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def default_data_root() -> Path:
    env = os.environ.get("ADSTUDIO_DATA_DIR")
    return Path(env) if env else Path.home() / "AI-Document-Studio"


@dataclass(frozen=True)
class Config:
    data_root: Path
    port: int = 0  # 0 = pick a free port
    workers: int = max(1, (os.cpu_count() or 2) - 1)
    heartbeat_s: float = 10.0
    stale_after_s: float = 60.0
    extra: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_root / "db" / "studio.sqlite"

    @classmethod
    def load(cls, data_root: Path | None = None, **overrides) -> "Config":
        root = Path(data_root) if data_root else default_data_root()
        raw: dict = {}
        toml_path = root / "config.toml"
        if toml_path.exists():
            raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        known = {k: raw[k] for k in ("port", "workers", "heartbeat_s", "stale_after_s") if k in raw}
        known.update(overrides)
        return cls(data_root=root, extra=raw, **known)

    def ensure_dirs(self) -> None:
        for sub in ("db", "files", "derived", "backups", "inbox", "models", "logs", "exports"):
            (self.data_root / sub).mkdir(parents=True, exist_ok=True)
