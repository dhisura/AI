"""FRED's lesson extractor: scan failures, extract specific lessons, write proposed entries.

Run:  python scripts/reflect.py

This script reads the episode log, finds failures without existing lessons,
asks the local LLM to extract ONE specific lesson per failure, and writes
them to data/lessons.md as [proposed] entries.

Only [confirmed] lessons are injected into the system prompt (see brain.py).
The user confirms lessons by editing lessons.md or using:
    python -m fred --lesson confirm N
    python -m fred --lesson retire N

Lessons are SPECIFIC, not generic:
  GOOD: "when opening Notepad via file path, verify the window title matches the filename exactly"
  BAD:  "be careful with Notepad"
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fred.config import load_settings  # noqa: E402
from fred.memory import failures, propose_lesson, all_lessons, confirm_lesson, retire_lesson  # noqa: E402


def _build_prompt(episode: dict) -> str:
    """Build a focused prompt for the LLM to extract ONE specific lesson."""
    transcript = episode.get("transcript", "")
    intent = episode.get("intent") or ""
    error = episode.get("error", "")
    steps = episode.get("steps", [])
    actions = episode.get("actions", "")

    step_summary = ""
    for s in steps:
        step_summary += f"  Step {s.get('step')}: {s.get('action')} {s.get('args', {})} -> {s.get('result')}"
        if s.get("error"):
            step_summary += f" (error: {s['error']})"
        step_summary += "\n"

    # Extract the app name from intent
    app_name = intent.split(".")[0] if "." in intent else "the app"

    return f"""You are FRED's reflection engine. A task failed. Extract ONE specific lesson
that would prevent this exact failure next time. No generic advice.

Failed task: {transcript!r}
Intent: {intent}
Error: {error}
Actions taken: {actions}
Steps:
{step_summary}

Rules for the lesson:
- One sentence, specific to this failure
- MUST mention the app name: {app_name}
- Name the app, the action, and what went wrong
- Say what to do differently next time (check, verify, wait, resolve differently)
- NO platitudes ("be careful", "verify things", "handle errors")
- NO suggestions to use a different app or tool (Notepad++, VSCode, etc.)
- The lesson must be about how to use {app_name} correctly, not to avoid it
- Must be actionable by a future FRED instance reading it

Examples of GOOD lessons:
- "when opening Notepad via file path, verify the window title matches the filename exactly before typing"
- "when Calculator's display stays empty after pressing Enter, re-read the display before assuming success"
- "when resolving a folder name, always map known Windows folders (Documents, Desktop) to Path.home() first"

Examples of BAD lessons (will be rejected):
- "be more careful with Notepad"
- "verify things before assuming they work"
- "handle errors better"
- "use Notepad++ instead of Notepad"
- "switch to a different editor"

Output exactly ONE line, the lesson text only, no formatting, no quotes."""


def _call_llm(prompt: str) -> str | None:
    """Call the local Ollama model for lesson extraction."""
    settings = load_settings()
    host = settings["ollama_host"].rstrip("/")
    model = settings["model"]

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You extract specific, actionable lessons from failures. One line only."},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 128},
    }

    req = urllib.request.Request(
        f"{host}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data.get("message", {}).get("content", "").strip()
        # Take only the first line, strip markdown
        for line in content.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line.strip("- ").strip()
    except Exception as exc:
        print(f"[reflect] LLM call failed: {exc}")
    return None


def _has_lesson_for(episode: dict, existing_lessons: list) -> bool:
    """Check if any existing lesson covers this episode.

    Heuristic: if the episode's intent keywords appear in a confirmed lesson,
    we consider it covered. Proposed lessons don't count as coverage yet.
    """
    intent = episode.get("intent") or ""
    transcript = episode.get("transcript") or ""
    intent = intent.lower()
    transcript = transcript.lower()

    for lesson in existing_lessons:
        if lesson.state != "confirmed":
            continue
        lesson_words = set(lesson.text.lower().split())
        intent_words = set(intent.split())
        transcript_words = set(transcript.split())
        # Check for meaningful overlap
        if lesson_words & intent_words or lesson_words & transcript_words:
            if len(lesson_words & (intent_words | transcript_words)) >= 2:
                return True
    return False


def run_reflect(dry_run: bool = False) -> int:
    """Scan failures, extract lessons, write proposed entries."""
    print("FRED reflect\n" + "=" * 50)

    # Load existing lessons
    existing = all_lessons()
    confirmed = [l for l in existing if l.state == "confirmed"]
    proposed = [l for l in existing if l.state == "proposed"]

    print(f"Existing lessons: {len(confirmed)} confirmed, {len(proposed)} proposed")

    # Get all failures
    fails = failures()
    print(f"Total failures in log: {len(fails)}")

    # Filter: only failures without a covering confirmed lesson
    uncovered = [e for e in fails if not _has_lesson_for(e, existing)]
    print(f"Failures without covering lesson: {len(uncovered)}")

    if not uncovered:
        print("Nothing new to reflect on.")
        return 0

    created = 0
    for ep in uncovered:
        transcript = ep.get("transcript", "")[:80]
        print(f"\nReflecting on: {transcript!r}...")

        prompt = _build_prompt(ep)
        lesson_text = _call_llm(prompt)

        if not lesson_text:
            print("  [skip] LLM returned nothing")
            continue

        # Validate: must be specific, not generic
        generic_phrases = [
            "be careful", "be more careful", "verify things", "verify everything",
            "handle errors", "handle error", "check first", "always check",
            "make sure", "ensure that", "should check", "should verify",
            "best practice", "good practice", "always verify",
        ]
        # Also reject lessons that suggest using a different app or tool
        bad_suggestions = [
            "use notepad++", "use vscode", "use another", "use different",
            "switch to", "instead of notepad", "instead of calculator",
            "instead of explorer", "use a different", "try another",
            "recommend", "suggest", "better to use",
        ]
        lowered = lesson_text.lower()
        if any(g in lowered for g in generic_phrases):
            print(f"  [reject] generic lesson: {lesson_text!r}")
            continue
        if any(b in lowered for b in bad_suggestions):
            print(f"  [reject] bad suggestion (use different tool): {lesson_text!r}")
            continue

        if len(lesson_text) < 30:
            print(f"  [reject] too short: {lesson_text!r}")
            continue

        # Must mention the app name from the failure (only for actual app failures)
        intent = (ep.get("intent") or "").lower()
        if intent and intent != "chat":
            app_name = intent.split(".")[0] if "." in intent else intent
            if app_name and app_name not in lowered:
                # Check if any app name is mentioned
                known_apps = ["notepad", "calculator", "explorer", "chrome"]
                if not any(app in lowered for app in known_apps):
                    print(f"  [reject] doesn't mention app: {lesson_text!r}")
                    continue

        print(f"  [proposed] {lesson_text!r}")

        if not dry_run:
            propose_lesson(lesson_text, source_episode=transcript)
            created += 1
            print("  -> written to lessons.md as [proposed]")
        else:
            created += 1
            print("  -> would write (dry run)")

    print("\n" + "=" * 50)
    if dry_run:
        print(f"Dry run: {created} lesson(s) would be proposed.")
    else:
        print(f"Done. {created} new lesson(s) proposed.")
        print("Review them in data/lessons.md and confirm with:")
        print("  python -m fred --lesson confirm N")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract lessons from FRED's failures.")
    parser.add_argument("--dry-run", action="store_true", help="show what would be written without writing")
    args = parser.parse_args()
    return run_reflect(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())