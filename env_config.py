"""Unified project environment loader.

All runtime code must load secrets from the repository root `.env` only.
Do not read `Binance/.env`, `execution/.env`, or nested module `.env` files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"

_LOADED = False


def load_project_env(*, override: bool = False) -> Path:
    """Load `<repo_root>/.env` into process env. Idempotent unless override=True."""
    global _LOADED
    if _LOADED and not override:
        return ENV_FILE
    load_dotenv(ENV_FILE, override=override)
    _LOADED = True
    return ENV_FILE


def project_root() -> Path:
    return PROJECT_ROOT


def env_file_path() -> Union[str, Path]:
    return ENV_FILE
