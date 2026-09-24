"""
Loads application configuration from config.yaml and environment variables.
Environment variables override YAML values.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import yaml

_CONFIG: Dict[str, Any] = {}
_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def load() -> Dict[str, Any]:
    global _CONFIG
    if _CONFIG:
        return _CONFIG

    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            _CONFIG = yaml.safe_load(fh) or {}
    else:
        _CONFIG = {}

    return _CONFIG


def get(key_path: str, default: Any = None) -> Any:
    """
    Fetch a config value by dotted path, e.g. 'checks.book_name.allowed_chars'.
    Returns default if not found. Environment variables (UPPER_SNAKE_CASE of the
    dotted key) override YAML values.
    """
    cfg = load()
    env_key = key_path.upper().replace(".", "_")
    env_val = os.environ.get(env_key)
    if env_val is not None:
        return env_val

    parts = key_path.split(".")
    val = cfg
    for part in parts:
        if isinstance(val, dict):
            val = val.get(part)
        else:
            return default
        if val is None:
            return default
    return val


def get_list(key_path: str, default: list | None = None) -> list:
    val = get(key_path, default or [])
    if isinstance(val, str):
        return [v.strip() for v in val.split(",") if v.strip()]
    return val or []
