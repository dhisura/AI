"""Shared paths and settings loading for FRED.

Kept separate from brain.py so hands.py / memory.py / scripts can resolve the
same paths without importing the model layer.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
ALLOWLIST_FILE = CONFIG_DIR / "allowlist.txt"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
LESSONS_FILE = DATA_DIR / "lessons.md"

DEFAULTS = {
    "ollama_host": "http://127.0.0.1:11434",
    "model": "qwen2.5:3b",
    "temperature": 0.7,
    # 4096 keeps the KV cache small enough for a 12 GB machine. The model
    # advertises 32k, but raising this is the first thing to OOM on this box.
    "num_ctx": 4096,
    "keep_last_turns": 20,
    "request_timeout_s": 300,
    # Phase 5: lesson injection
    "lesson_injection_count": 3,
}


def load_settings() -> dict:
    settings = dict(DEFAULTS)
    if SETTINGS_FILE.exists():
        with SETTINGS_FILE.open(encoding="utf-8") as fh:
            settings.update(json.load(fh))
    return settings


def data_path(name: str) -> Path:
    """Return a path inside data/, creating the directory if needed."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / name


def allowlist() -> list[str]:
    if not ALLOWLIST_FILE.exists():
        return []
    lines = ALLOWLIST_FILE.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def lessons_path() -> Path:
    """Path to the lessons markdown file."""
    return LESSONS_FILE


def lessons_text() -> str:
    """Lessons written by scripts/reflect.py.

    Returns "" until reflect.py has produced real content, so the system
    prompt stays clean in the meantime.
    """
    path = LESSONS_FILE
    if not path.exists():
        return ""
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    # A lone heading counts as empty.
    if len(lines) < 2:
        return ""
    return "\n".join(lines).strip()