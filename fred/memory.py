"""FRED's memory: an append-only episode log plus a SQLite table over it.

Phase 4 starts writing `data/episodes.jsonl` from the very first task so that
Phase 5's reflect.py has real history to learn from. Logging retroactively is
worthless -- a lesson extracted from a task FRED never saw is a lesson it
cannot be tested against.

The JSONL file is the source of truth (append-only, one line per attempt, easy
to read by hand and to diff). SQLite mirrors it for querying.

One `Episode` == one attempt at one task, not one conversation. A retried
Notepad write that fails twice and succeeds once is a single episode with three
steps and a final result.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import DATA_DIR, LESSONS_FILE

EPISODES = DATA_DIR / "episodes.jsonl"
DB_FILE = DATA_DIR / "fred.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    transcript  TEXT NOT NULL,
    app         TEXT,
    intent      TEXT,
    actions     TEXT,
    verify      TEXT,
    success     INTEGER NOT NULL,
    steps       INTEGER,
    error       TEXT
);
"""


@dataclass
class Episode:
    """One attempt at one task.

    `steps` is a list of dicts describing what was actually done:
    {"step": 1, "action": "notepad_write", "args": {...}, "result": "ok"}
    """

    transcript: str = ""
    app: str | None = None
    intent: str | None = None
    actions: str | None = None
    verify: bool | None = None
    success: bool = False
    steps: list[dict] = field(default_factory=list)
    error: str | None = None
    started_at: str = ""

    def record_step(self, action: str, args: dict | None = None,
                    result: str = "ok", **extra) -> None:
        """Log one action actually performed."""
        step = {"step": len(self.steps) + 1, "action": action, "result": result}
        if args:
            step["args"] = args
        step.update(extra)
        self.steps.append(step)

    def to_dict(self) -> dict:
        d = asdict(self)
        if not self.started_at:
            d["started_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return d


def log_episode(ep: Episode) -> Episode:
    """Append one episode to episodes.jsonl and mirror it into SQLite.

    Logging must never take down the agent, so a write failure is reported on
    stdout rather than raised -- losing one episode is bad, losing the task the
    user asked for because the log failed is worse.
    """
    payload = ep.to_dict()
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with EPISODES.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"[memory] could not append to {EPISODES.name}: {exc}")
        return payload
    _mirror_sqlite(payload)
    return payload


def _mirror_sqlite(row: dict) -> None:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT INTO episodes (started_at, transcript, app, intent, actions,"
                " verify, success, steps, error) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    row.get("started_at", ""),
                    row.get("transcript", ""),
                    row.get("app"),
                    row.get("intent"),
                    row.get("actions"),
                    None if row.get("verify") is None else int(bool(row["verify"])),
                    int(bool(row.get("success"))),
                    len(row.get("steps") or []),
                    row.get("error"),
                ),
            )
    except Exception as exc:
        print(f"[memory] sqlite mirror failed (jsonl is still written): {exc}")


def recent_episodes(limit: int = 20) -> list[dict]:
    """Read the tail of the log, newest last."""
    if not EPISODES.exists():
        return []
    out: list[dict] = []
    for line in EPISODES.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a partial line from a killed run
    return out[-limit:]


def success_rate() -> tuple[int, int]:
    """(succeeded, total) over the whole log."""
    eps = recent_episodes(limit=10 ** 9)
    return sum(1 for e in eps if e.get("success")), len(eps)


# --------------------------------------------------------------------------
# Phase 5 will build reflect.py on these hooks
# --------------------------------------------------------------------------

def failures() -> list[dict]:
    """Every episode that did not succeed -- the raw material for lessons."""
    return [e for e in recent_episodes(limit=10 ** 9) if not e.get("success")]


def record_to_sqlite(record: dict) -> None:
    """Public entry point for importing an external episode into the DB."""
    _mirror_sqlite(record)


# --------------------------------------------------------------------------
# Phase 5: Lesson lifecycle
# --------------------------------------------------------------------------
#
# Lessons are stored in data/lessons.md as markdown with state tags:
#   - [proposed] Lesson text
#   - [confirmed] Lesson text
#   - [retired] Lesson text
#
# Only confirmed lessons are injected into the system prompt.
# The user can edit lessons.md directly or use CLI commands to change states.

@dataclass
class Lesson:
    """One extracted lesson."""
    text: str
    state: str  # "proposed", "confirmed", "retired"
    created_at: str
    source_episode: str | None = None  # transcript snippet for traceability

    def to_line(self) -> str:
        return f"- [{self.state}] {self.text}"

    @classmethod
    def from_line(cls, line: str) -> "Lesson | None":
        """Parse a lesson line from lessons.md.

        Expected format: - [state] lesson text
        """
        line = line.strip()
        if not line.startswith("- ["):
            return None
        start = line.find("[")
        end = line.find("]")
        if start == -1 or end == -1 or end <= start:
            return None
        state = line[start + 1:end].strip()
        if state not in ("proposed", "confirmed", "retired"):
            return None
        text = line[end + 1:].strip()
        if not text:
            return None
        return cls(text=text, state=state, created_at="")


def _read_lessons_file() -> list[Lesson]:
    """Read all lessons from lessons.md."""
    path = LESSONS_FILE
    if not path.exists():
        return []
    lessons = []
    for line in path.read_text(encoding="utf-8").splitlines():
        lesson = Lesson.from_line(line)
        if lesson:
            lessons.append(lesson)
    return lessons


def _write_lessons_file(lessons: list[Lesson]) -> None:
    """Write all lessons back to lessons.md, preserving order and unknown lines."""
    path = LESSONS_FILE
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Read the original file to preserve comments and unknown lines
    if path.exists():
        original_lines = path.read_text(encoding="utf-8").splitlines()
    else:
        original_lines = []

    # Track which lesson lines we've handled
    lesson_lines_written = set()
    new_lines = []

    # First pass: update existing lesson lines in place
    lesson_idx = 0
    for line in original_lines:
        lesson = Lesson.from_line(line)
        if lesson and lesson_idx < len(lessons):
            # Replace with the updated lesson
            new_lines.append(lessons[lesson_idx].to_line())
            lesson_lines_written.add(lesson_idx)
            lesson_idx += 1
        else:
            # Keep non-lesson lines (comments, headings, etc.)
            new_lines.append(line)

    # Append any remaining new lessons
    while lesson_idx < len(lessons):
        new_lines.append(lessons[lesson_idx].to_line())
        lesson_idx += 1

    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def propose_lesson(text: str, source_episode: str | None = None) -> Lesson:
    """Add a new proposed lesson."""
    lessons = _read_lessons_file()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lesson = Lesson(text=text, state="proposed", created_at=now, source_episode=source_episode)
    lessons.append(lesson)
    _write_lessons_file(lessons)
    return lesson


def confirm_lesson(index: int) -> bool:
    """Mark a lesson as confirmed by its index in the full list (0-based)."""
    lessons = _read_lessons_file()
    if 0 <= index < len(lessons):
        lessons[index].state = "confirmed"
        _write_lessons_file(lessons)
        return True
    return False


def retire_lesson(index: int) -> bool:
    """Mark a lesson as retired by its index in the full list (0-based)."""
    lessons = _read_lessons_file()
    if 0 <= index < len(lessons):
        lessons[index].state = "retired"
        _write_lessons_file(lessons)
        return True
    return False


def confirmed_lessons() -> list[str]:
    """Return only confirmed lesson texts, for injection into the system prompt."""
    lessons = _read_lessons_file()
    return [l.text for l in lessons if l.state == "confirmed"]


def all_lessons() -> list[Lesson]:
    """Return all lessons with their states."""
    return _read_lessons_file()