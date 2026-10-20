"""Tests for the Phase 4 planner's parser and guardrails.

These are pure-function tests: no model, no UI. The planner's real output comes
from qwen2.5:3b, which is not deterministic enough to assert on directly -- but
the parser that decides what FRED is *allowed* to do with that output must be,
and that is what this file pins down.

Run:  python tests/test_planner.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fred.agent import PlanError, normalise_expression, parse_plan  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  [ok ] {label}")
    else:
        print(f"  [FAIL] {label}\n         got:  {got!r}\n         want: {want!r}")
        failures.append(label)


# -- Valid plans the parser must accept ------------------------------------
GOOD = [
    (
        'plain json',
        '{"intent": "task", "app": "notepad", "action": "write",'
        ' "args": {"text": "hello", "path": "notes.txt"}}',
        ("notepad", "write", {"text": "hello", "path": "notes.txt"}),
    ),
    (
        'fenced in a markdown block, as models often do',
        '```json\n{"intent": "task", "app": "calculator", "action": "calculate",'
        ' "args": {"expression": "12*7"}}\n```',
        ("calculator", "calculate", {"expression": "12*7"}),
    ),
    (
        'chat decision',
        '{"intent": "chat"}',
        None,
    ),
    (
        'prose around the json',
        'Sure! Here is the plan:\n{"intent": "task", "app": "explorer",'
        ' "action": "open", "args": {"folder": "Documents"}}\nHope that helps.',
        ("explorer", "open", {"folder": "Documents"}),
    ),
]

print("-- valid plans --")
for label, raw, want in GOOD:
    try:
        plan = parse_plan(raw, "open notepad and write hello")
        got = None if plan is None else (plan.app, plan.action, plan.args)
        check(label, got, want)
    except Exception as exc:
        check(label, f"raised {type(exc).__name__}: {exc}", want)

# -- Plans the parser must REFUSE ------------------------------------------
# Each of these is a case where doing the wrong thing is worse than doing
# nothing, so a refusal is the correct answer, not an error to route around.
BAD = [
    (
        'app not on the allowlist',
        '{"intent": "task", "app": "chrome", "action": "open",'
        ' "args": {"folder": "C:/"}}',
    ),
    (
        'action not allowed for that app',
        '{"intent": "task", "app": "notepad", "action": "delete",'
        ' "args": {"path": "x.txt"}}',
    ),
    (
        'missing a required arg',
        '{"intent": "task", "app": "notepad", "action": "write",'
        ' "args": {"text": "hi"}}',
    ),
    (
        'args is not an object',
        '{"intent": "task", "app": "notepad", "action": "write", "args": "hello"}',
    ),
    (
        'destructive request smuggled past the planner',
        '{"intent": "task", "app": "notepad", "action": "write",'
        ' "args": {"text": "x", "path": "y.txt"}}',
    ),
    (
        'empty arg instead of an honest refusal (the "open chrome" misroute)',
        '{"intent": "task", "app": "explorer", "action": "open",'
        ' "args": {"folder": ""}}',
    ),
]

print("\n-- refused plans --")
for label, raw in BAD:
    transcript = ("delete all my files" if "smuggled" in label
                  else "open notepad and write something")
    try:
        plan = parse_plan(raw, transcript)
        check(f"{label} (should raise)", f"accepted: {plan}", "PlanError")
    except PlanError:
        print(f"  [ok ] {label}")
    except Exception as exc:
        check(label, f"raised {type(exc).__name__}, want PlanError", "PlanError")

# -- Extra args are dropped, not passed through ---------------------------
print("\n-- unexpected args --")
try:
    plan = parse_plan(
        '{"intent": "task", "app": "calculator", "action": "calculate",'
        ' "args": {"expression": "2+2", "shell_command": "rm -rf /"}}',
        "what is two plus two",
    )
    check("dangerous extra arg stripped", plan.args, {"expression": "2+2"})
except Exception as exc:
    check("dangerous extra arg stripped", f"raised {exc}", {"expression": "2+2"})

# -- Unparseable output falls back to chat rather than erroring ------------
print("\n-- malformed output --")
for label, raw in [("no json at all", "I think you should open notepad"),
                   ("empty string", ""),
                   ("truncated json", '{"intent": "task", "app": "note')]:
    try:
        check(label, parse_plan(raw, "hello"), None)
    except Exception as exc:
        check(label, f"raised {type(exc).__name__}: {exc}", None)

# -- Blocked terms are checked on the raw transcript -----------------------
print("\n-- blocked terms --")
PLAN = ('{"intent": "task", "app": "notepad", "action": "write",'
        ' "args": {"text": "x", "path": "y.txt"}}')
for term in ["delete", "move my files", "send an email", "buy something",
             "show my password"]:
    try:
        check(f"'{term}' refused", parse_plan(PLAN, term), "PlanError")
    except PlanError:
        print(f"  [ok ] '{term}' refused")
    except Exception as exc:
        check(f"'{term}' refused", f"raised {type(exc).__name__}", "PlanError")

# -- A harmless transcript must NOT be blocked -----------------------------
print("\n-- no false positives --")
for phrase in ["open notepad and write hello", "what is 2+2",
               "open file explorer on documents", "tell me a joke"]:
    try:
        parse_plan(PLAN, phrase)
        print(f"  [ok ] '{phrase}' allowed")
    except PlanError as exc:
        check(f"'{phrase}' allowed", f"blocked: {exc}", "allowed")

# -- Spoken maths has to become key presses -------------------------------
# "12 times 7" is what the planner hands back when someone says "twelve times
# seven". Without this the calculator refuses and the task simply fails.
print("\n-- spoken arithmetic --")
SPOKEN = [
    ("12 times 7", "12*7"),
    ("twelve times seven", "12*7"),
    ("one plus one", "1+1"),
    ("100 divided by 4", "100/4"),
    ("2 + 2", "2+2"),
    ("five minus nine", "5-9"),
    ("twenty plus twenty two", "20+22"),
    ("twenty two", "22"),
    ("one hundred", "100"),
    ("one hundred plus one", "100+1"),
    ("(2+2)*3", "(2+2)*3"),
]
for raw, want in SPOKEN:
    try:
        check(f"{raw!r} -> {want!r}", normalise_expression(raw), want)
    except ValueError as exc:
        check(f"{raw!r} -> {want!r}", f"raised {exc}", want)

print("\n-- arithmetic that must be refused --")
for raw in ["rm -rf /", "import os", "calc.exe", "", "delete everything",
            "overwrite the file", "1;2;3"]:
    try:
        got = normalise_expression(raw)
        check(f"{raw!r} refused", f"accepted as {got!r}", "ValueError")
    except ValueError:
        print(f"  [ok ] {raw!r} refused")

# -- Folder names have to become real paths --------------------------------
# The planner says "Documents"; the shell is sitting in the repo. If FRED
# resolves that against the working directory it opens the wrong thing, or
# fails -- so known folder names must come from the OS.
print("\n-- folder resolution --")
from pathlib import Path  # noqa: E402

from fred.hands import resolve_folder  # noqa: E402

home = Path.home()
FOLDERS = [
    ("Documents", home / "Documents"),
    ("documents", home / "Documents"),
    ("Desktop", home / "Desktop"),
    ("Downloads", home / "Downloads"),
    ("my documents folder", home / "Documents"),
    ('"Documents"', home / "Documents"),
    (str(home / "Documents"), home / "Documents"),
]
for said, want in FOLDERS:
    try:
        check(f"{said!r} -> {want}", resolve_folder(said), want)
    except FileNotFoundError as exc:
        check(f"{said!r} -> {want}", f"raised {exc}", want)

print("\n-- folder resolution must refuse, not guess --")
for said in ["definitely not a folder", "", "   ", "C:/NoSuchPlace"]:
    try:
        got = resolve_folder(said)
        check(f"{said!r} refused", f"resolved to {got}", "FileNotFoundError")
    except FileNotFoundError:
        print(f"  [ok ] {said!r} refused")

print("\n" + "-" * 50)
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("All planner checks passed.")