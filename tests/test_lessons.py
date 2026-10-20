"""Tests for Phase 5 lesson infrastructure.

Run:  python tests/test_lessons.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fred.memory import (  # noqa: E402
    Lesson,
    _read_lessons_file,
    _write_lessons_file,
    all_lessons,
    confirm_lesson,
    confirmed_lessons,
    propose_lesson,
    retire_lesson,
)
from fred.config import lessons_path  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  [ok ] {label}")
    else:
        print(f"  [FAIL] {label}\n         got:  {got!r}\n         want: {want!r}")
        failures.append(label)


def check_raises(label: str, fn, exc_type: type) -> None:
    try:
        fn()
        check(label, "no exception", f"{exc_type.__name__}")
    except exc_type:
        print(f"  [ok ] {label}")
    except Exception as exc:
        check(label, f"raised {type(exc).__name__}", f"{exc_type.__name__}")


# Clean up any existing test lessons file
test_lessons = lessons_path()
if test_lessons.exists():
    test_lessons.unlink()

print("-- Lesson parsing --")

# Test parsing valid lesson lines
test_lines = [
    "- [proposed] when opening Notepad, verify the window title matches the filename",
    "- [confirmed] when resolving a folder name, always map known Windows folders to Path.home()",
    "- [retired] old lesson about calculator display",
    "# This is a comment",
    "Some other text",
    "- [proposed] another proposed lesson",
]

_write_lessons_file([
    Lesson(text="when opening Notepad, verify the window title matches the filename", state="proposed", created_at=""),
    Lesson(text="when resolving a folder name, always map known Windows folders to Path.home()", state="confirmed", created_at=""),
    Lesson(text="old lesson about calculator display", state="retired", created_at=""),
])

lessons = _read_lessons_file()
check("parse 3 valid lessons", len(lessons), 3)
check("first lesson state", lessons[0].state, "proposed")
check("second lesson state", lessons[1].state, "confirmed")
check("third lesson state", lessons[2].state, "retired")
check("first lesson text", lessons[0].text, "when opening Notepad, verify the window title matches the filename")

# Test from_line edge cases
check("from_line rejects no bracket", Lesson.from_line("not a lesson"), None)
check("from_line rejects bad state", Lesson.from_line("- [bad] lesson text"), None)
check("from_line rejects empty text", Lesson.from_line("- [confirmed] "), None)

print("\n-- Lesson lifecycle --")

# Test propose
proposed_lesson = propose_lesson("test lesson one", source_episode="test")
check("propose returns lesson with source", proposed_lesson.source_episode, "test")
all_l = all_lessons()
check("propose adds lesson", len(all_l), 4)
check("new lesson is proposed", all_l[-1].state, "proposed")
# source_episode is not persisted in lessons.md (by design), so we don't check it after reading

# Test confirm
confirm_lesson(3)  # index 3 is the newly added one
all_l = all_lessons()
check("confirm changes state", all_l[3].state, "confirmed")

# Test retire
retire_lesson(3)
all_l = all_lessons()
check("retire changes state", all_l[3].state, "retired")

# Test confirmed_lessons returns only confirmed
confirmed = confirmed_lessons()
check("confirmed_lessons count", len(confirmed), 1)
check("confirmed lesson text", confirmed[0], "when resolving a folder name, always map known Windows folders to Path.home()")

# Test out of bounds
check("confirm out of bounds returns False", confirm_lesson(999), False)
check("retire out of bounds returns False", retire_lesson(999), False)

print("\n-- Lesson injection scoring --")

from fred.brain import _inject_lessons  # noqa: E402

# Set up a known confirmed lesson
propose_lesson("when opening Notepad via file path, verify the window title matches the filename exactly")
confirm_lesson(len(all_lessons()) - 1)

# Test relevance scoring
transcript = "open notepad and write hello"
injected = _inject_lessons(transcript, max_lessons=3)
check("relevant lesson injected", "Notepad" in injected and "verify" in injected, True)

# Irrelevant transcript
transcript2 = "what is the weather today"
injected2 = _inject_lessons(transcript2, max_lessons=3)
check("irrelevant transcript gets no injection", injected2, "")

# Max lessons limit
# Add lessons that share words with "open notepad and write test"
# Using "open" and "write" and "notepad" in the lesson text
propose_lesson("when open notepad, verify the window title matches the filename before write")
confirm_lesson(len(all_lessons()) - 1)
propose_lesson("when open notepad write text, always select all first")
confirm_lesson(len(all_lessons()) - 1)

# Ask for top 2
injected3 = _inject_lessons("open notepad and write test", max_lessons=2)
lines = [l for l in injected3.split("\n") if l.startswith("- ")]
check("max_lessons respected", len(lines), 2)

print("\n-- File persistence (preserves unknown lines) --")

# Write a file with comments and other content
test_content = """# FRED Lessons

# This is a comment that should be preserved

- [confirmed] when opening Notepad via file path, verify the window title matches the filename exactly
- [proposed] test lesson from earlier

Some free-form text here that should also stay.

- [retired] old calculator lesson
"""

test_lessons.write_text(test_content, encoding="utf-8")
lessons = _read_lessons_file()
check("reads lessons with comments", len(lessons), 3)

# Add a new lesson and verify comments persist
propose_lesson("new lesson that should be appended")
# Re-read raw file
raw = test_lessons.read_text(encoding="utf-8")
check("preserves comment lines", "# This is a comment" in raw, True)
check("preserves free text", "Some free-form text" in raw, True)
check("preserves headings", "# FRED Lessons" in raw, True)
check("new lesson appended", "new lesson that should be appended" in raw, True)

print("\n" + "-" * 50)
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("All lesson tests passed.")