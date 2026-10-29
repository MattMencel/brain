"""User config: ~/.config/brain/brain.toml, or the file BRAIN_CONFIG names. Missing file = all defaults."""

import hashlib
import json
import os
import tomllib
from pathlib import Path

PATH = Path(os.environ.get("BRAIN_CONFIG", Path.home() / ".config" / "brain" / "brain.toml"))


def load() -> dict:
    return tomllib.loads(PATH.read_text()) if PATH.exists() else {}


def connector(name: str) -> dict:
    return load().get("connectors", {}).get(name, {})


def digest(cfg: dict) -> str:
    """Fingerprint of a connector's table, so a run can tell its scope changed since the last success."""
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()


def enabled(names: list[str]) -> list[str]:
    """Connectors with a table in the config, unless it sets enabled = false."""
    tables = load().get("connectors", {})
    return [n for n in names if n in tables and tables[n].get("enabled", True)]
