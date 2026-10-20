"""Tests for the spoken -> app fuzzy matcher.

Run:  python tests/test_matching.py

These are the cases that motivated the design: whisper turns "notepad" into
"Note Ed", "explorer" into "exployer", and speech arrives wrapped in filler.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fred.hands import extract_app, is_allowed, normalize, resolve_app  # noqa: E402

ALLOWED = ["Notepad.exe", "Calculator.exe", "explorer.exe", "chrome.exe"]

# Spoken -> expected executable. These are real whisper mishearings plus the
# ways people actually phrase requests.
CASES = [
    # Exact, spelled correctly
    ("notepad", "Notepad.exe"),
    ("open notepad", "Notepad.exe"),
    ("Notepad.exe", "Notepad.exe"),
    ("calculator", "Calculator.exe"),
    ("open the calculator", "Calculator.exe"),
    ("file explorer", "explorer.exe"),
    ("chrome", "chrome.exe"),

    # Whisper mishearings -- the whole reason fuzzy matching exists
    ("Note Ed", "Notepad.exe"),
    ("note ed", "Notepad.exe"),
    ("nodepad", "Notepad.exe"),
    ("not epad", "Notepad.exe"),
    ("exployer", "explorer.exe"),
    ("file exployer", "explorer.exe"),
    ("calculater", "Calculator.exe"),
    ("crome", "chrome.exe"),

    # Filler around the app name
    ("please open notepad for me", "Notepad.exe"),
    ("can you launch the calculator now", "Calculator.exe"),
    ("open notepad please", "Notepad.exe"),

    # Embedded in a sentence
    ("I want to open notepad and write something", "Notepad.exe"),
    ("open file explorer and show my documents", "explorer.exe"),
]

NEGATIVE = [
    "",
    "hello",
    "what is the weather",
    "open photoshop",     # not allowlisted, and must not fuzzy onto something
    # Destructive/external intent must never resolve to an app, even when a
    # word in it resembles one.
    "delete all my files",
    "open notepad and delete everything in it",
    "send an email to fred@example.com",
    "enter my password into chrome",
    "buy something on chrome",
    "empty the recycle bin and remove all files",
]


def main() -> int:
    failures: list[str] = []

    print("normalisation")
    for raw, want in [("Open Notepad!", "open notepad"), ("  Chrome.exe  ", "chrome exe")]:
        got = normalize(raw)
        status = "ok " if got == want else "FAIL"
        if got != want:
            failures.append(f"normalize({raw!r}) = {got!r}, want {want!r}")
        print(f"  [{status}] {raw!r} -> {got!r}")

    print("\nmatching (should resolve)")
    for spoken, want in CASES:
        match = resolve_app(spoken, ALLOWED)
        got = match.app if match else None
        ok = got == want
        if not ok:
            failures.append(f"resolve_app({spoken!r}) = {got!r}, want {want!r}")
        detail = f"{match.score:.2f} via {match.via}" if match else "no match"
        print(f"  [{'ok ' if ok else 'FAIL'}] {spoken!r:45} -> {got or '-':16} ({detail})")

    print("\nrejecting (must not match anything)")
    for spoken in NEGATIVE:
        match = resolve_app(spoken, ALLOWED)
        ok = match is None or match.score < 0.62
        if not ok:
            failures.append(f"resolve_app({spoken!r}) wrongly matched {match.app} "
                            f"at {match.score:.2f}")
        got = f"{match.app} ({match.score:.2f})" if match else "no match"
        print(f"  [{'ok ' if ok else 'FAIL'}] {spoken!r:45} -> {got}")

    print("\nallowlist gate")
    for app, want in [("notepad.exe", True), ("Notepad.exe", True),
                      ("calc.exe", False), ("cmd.exe", False)]:
        got = is_allowed(app)
        ok = got == want
        if not ok:
            failures.append(f"is_allowed({app!r}) = {got}, want {want}")
        print(f"  [{'ok ' if ok else 'FAIL'}] {app:14} allowed={got}")

    total = len(CASES) + len(NEGATIVE) + 6
    print(f"\n{'-' * 50}")
    if failures:
        print(f"{len(failures)} FAILURE(S) out of ~{total} checks:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"All checks passed (~{total}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())