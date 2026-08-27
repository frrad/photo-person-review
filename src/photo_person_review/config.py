"""Configuration for private catalog state.

The source photo tree is never copied into the catalog workspace.  Only its
stable identity and an observed source path are recorded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from platformdirs import user_data_dir
except ImportError:  # pragma: no cover - useful for a source checkout without dependencies
    user_data_dir = None  # type: ignore[assignment]


APP_NAME = "photo-person-review"


def default_workspace() -> Path:
    """Return the platform-appropriate private state directory."""

    configured = os.environ.get("PPR_WORKSPACE")
    if configured:
        return Path(configured).expanduser()
    if user_data_dir is not None:
        return Path(user_data_dir(APP_NAME))
    return Path.home() / ".local" / "share" / APP_NAME


@dataclass(frozen=True, slots=True)
class CatalogConfig:
    """Paths belonging to one append-only catalog."""

    workspace: Path

    @classmethod
    def from_workspace(cls, workspace: str | Path | None = None) -> "CatalogConfig":
        return cls(Path(workspace).expanduser() if workspace is not None else default_workspace())

    @property
    def database_path(self) -> Path:
        return self.workspace / "catalog.sqlite3"

    @property
    def temporary_root(self) -> Path:
        """Root for disposable review packets; it is intentionally not in SQLite."""

        return self.workspace / "tmp"

    @property
    def models_dir(self) -> Path:
        """Pinned executable model artifacts; these never contain photo data."""

        return self.workspace / "models"

    def ensure_workspace(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
