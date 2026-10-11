"""Settings, loaded from ~/.delegate/config.json and environment variables.

Precedence (highest first): environment variables, config.json, defaults.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_HOME = Path.home() / ".delegate"


@dataclass(frozen=True, slots=True)
class Settings:
    db_path: Path
    lease_minutes: int = 15
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 3140


def _home() -> Path:
    return Path(os.environ.get("DELEGATE_HOME", str(DEFAULT_HOME))).expanduser()


def load_settings() -> Settings:
    """Read settings. A missing config file is fine; a malformed one is an error."""
    home = _home()
    raw: dict[str, Any] = {}
    config_file = home / "config.json"
    if config_file.exists():
        try:
            loaded = json.loads(config_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {config_file}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"{config_file} must contain a JSON object")
        raw = loaded  # pyright: ignore[reportUnknownVariableType]

    db_path = Path(os.environ.get("DELEGATE_DB") or raw.get("db_path") or home / "delegate.db")
    lease = int(os.environ.get("DELEGATE_LEASE_MINUTES") or raw.get("lease_minutes") or 15)
    if lease < 1:
        raise ValueError("lease_minutes must be >= 1")
    port = int(os.environ.get("DELEGATE_PORT") or raw.get("dashboard_port") or 3140)
    host = str(os.environ.get("DELEGATE_HOST") or raw.get("dashboard_host") or "127.0.0.1")
    return Settings(
        db_path=db_path.expanduser(),
        lease_minutes=lease,
        dashboard_host=host,
        dashboard_port=port,
    )
