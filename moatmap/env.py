"""Tiny .env loader (no python-dotenv dependency).

`load_env()` reads ROOT/.env (KEY=VALUE lines) into os.environ. Variables that are
already set in the real environment are never overridden. Blank values are ignored,
so an untouched `.env` copied from `.env.example` behaves like "not set".
"""
import os
from pathlib import Path

from .config import ROOT


def parse_env(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        elif " #" in val:  # trailing comment on an unquoted value
            val = val.split(" #", 1)[0].rstrip()
        if key:
            out[key] = val
    return out


def load_env(path: Path = None) -> dict:
    """Load .env into os.environ (without overriding). Returns the keys it set."""
    path = Path(path) if path else ROOT / ".env"
    if not path.is_file():
        return {}
    applied = {}
    for key, val in parse_env(path.read_text(encoding="utf-8-sig")).items():
        if val == "" or os.environ.get(key):
            continue
        os.environ[key] = val
        applied[key] = val
    return applied


def get(name: str, default: str = "") -> str:
    """os.environ lookup after loading .env; whitespace-only counts as unset."""
    load_env()
    return (os.environ.get(name) or default).strip()
