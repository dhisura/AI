"""FRED's task loop: plan -> act -> verify, with a hard step cap.

The loop is deliberately small and explicit. The planner (a qwen2.5:3b JSON
completion) picks one of three apps; the executor runs it; the verifier checks
the result against the real system state. Every attempt is written to
data/episodes.jsonl before the user is told anything, so Phase 5 can learn from
it.

Two rules are load-bearing:
  * the step cap is enforced here, not in the prompt -- a model that ignores its
    instructions should not be able to run away;
  * confirmation is TYPED only. Spoken confirmation would mean whisper decides
    whether a destructive action proceeds, and "yes" becoming "dress" at a
    confirmation prompt is not a risk worth taking.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .config import DATA_DIR, load_settings
from .memory import Episode, log_episode

# The whole allowed action surface. The executor refuses anything not in here,
# so a hallucinated app or action cannot reach pywinauto.
ALLOWED_ACTIONS = {
    "notepad": {"write"},
    "calculator": {"calculate"},
    "explorer": {"open"},
}

# What each app's args must contain. Extra or missing keys are a rejection, not
# something to shrug at and pass through.
REQUIRED_ARGS = {
    ("notepad", "write"): {"text", "path"},
    ("calculator", "calculate"): {"expression"},
    ("explorer", "open"): {"folder"},
}

BLOCKED_TERMS = (
    "delete", "remove", "erase", "wipe", "format", "uninstall",
    "move", "rename", "cut", "paste", "email", "send", "message", "post",
    "buy", "purchase", "pay", "transfer", "password", "credential", "login",
    "banking", "bank account", "shutdown", "restart",
)

DEFAULT_NOTEPAD_PATH = str(DATA_DIR / "notepad" / "fred.txt")


class PlanError(RuntimeError):
    """The planner produced something FRED will not act on."""


@dataclass
class Plan:
    app: str
    action: str
    args: dict


def _strip_fence(raw: str) -> str:
    """Models like wrapping JSON in ```json ... ``` despite being told not to."""
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL | re.IGNORECASE)
    if fence:
        return fence.group(1).strip()
    return text


def parse_plan(raw: str, transcript: str) -> Plan | None:
    """Turn planner output into a validated Plan, or None to fall through to chat.

    Every rejection is explained on stdout: when a 3B model gets this wrong we
    need to know whether it guessed an app, dropped a key, or tried something
    dangerous -- those need different fixes.
    """
    text = _strip_fence(raw or "")
    if not text:
        print("[plan] empty reply, treating as chat")
        return None

    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        print(f"[plan] no JSON object in: {text[:120]!r}")
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        print(f"[plan] invalid JSON ({exc}): {text[:120]!r}")
        return None

    if not isinstance(data, dict):
        print("[plan] JSON was not an object")
        return None

    intent = str(data.get("intent", "")).strip().lower()
    if intent != "task":
        print(f"[plan] intent={intent!r} -> chat")
        return None

    app = str(data.get("app", "")).strip().lower()
    action = str(data.get("action", "")).strip().lower()
    args = data.get("args")

    if app not in ALLOWED_ACTIONS:
        raise PlanError(f"planner chose '{app}', which is not allowlisted")
    if action not in ALLOWED_ACTIONS[app]:
        raise PlanError(f"'{action}' is not a valid action for {app}")
    if not isinstance(args, dict):
        raise PlanError(f"args for {app}.{action} was not an object")

    required = REQUIRED_ARGS[(app, action)]
    missing = required - set(args)
    if missing:
        raise PlanError(f"{app}.{action} missing args: {sorted(missing)}")

    # An empty arg is a non-answer, not a default. Without this, "open chrome"
    # -- which the planner should refuse -- degrades into explorer.open with
    # folder "", which reads as a confident action and fails somewhere less
    # obvious.
    empties = [k for k, v in args.items() if not str(v).strip()]
    if empties:
        raise PlanError(f"{app}.{action} has empty args: {sorted(empties)}")

    extra = set(args) - required
    if extra:
        # Not fatal -- drop them -- but it is worth seeing, because a model
        # inventing arguments is how a "safe" action turns into something else.
        print(f"[plan] ignoring unexpected args {sorted(extra)}")
        args = {k: args[k] for k in required}

    # Last line of defence, on the raw transcript: a destructive request must
    # never reach an executor even if the planner waves it through.
    lowered = transcript.lower()
    for term in BLOCKED_TERMS:
        if term in lowered:
            raise PlanError(f"transcript contains blocked term '{term}'")

    return Plan(app=app, action=action, args=args)


def plan_task(brain, transcript: str) -> Plan | None:
    """Ask the planner what to do. None means 'just chat about it'."""
    from .brain import PLANNER_PROMPT, _inject_lessons
    from .config import load_settings

    # Inject lessons into planner prompt for relevance to the current task
    settings = load_settings()
    max_lessons = settings.get("lesson_injection_count", 3)
    lessons = _inject_lessons(transcript, max_lessons)

    planner_prompt = PLANNER_PROMPT
    if lessons:
        planner_prompt = PLANNER_PROMPT + lessons

    raw = brain._complete(planner_prompt, transcript)
    return parse_plan(raw, transcript)


# Spoken maths reaches the planner as words ("twelve times seven"), and the
# planner sometimes copies that through verbatim instead of doing the
# translation itself. Normalising here keeps that failure recoverable -- and
# only for these exact words, so "one plus one" can never smuggle a command
# through a calculator that validates plain arithmetic.
_WORD_OPERATORS = (
    ("plus", "+"), ("minus", "-"), ("times", "*"), ("multiplied by", "*"),
    ("divided by", "/"), ("over", "/"), ("to the power of", "^"),
)
# Multi-word numbers must be handled before single words, or "twenty two"
# becomes "20 2" -- which is not an error the calculator will catch.
_MULTI_WORD_NUMBERS = (
    ("one hundred", "100"), ("two hundred", "200"), ("three hundred", "300"),
    ("four hundred", "400"), ("five hundred", "500"), ("six hundred", "600"),
    ("seven hundred", "700"), ("eight hundred", "800"), ("nine hundred", "900"),
    ("twenty one", "21"), ("twenty two", "22"), ("twenty three", "23"),
    ("twenty four", "24"), ("twenty five", "25"), ("twenty six", "26"),
    ("twenty seven", "27"), ("twenty eight", "28"), ("twenty nine", "29"),
    ("thirty one", "31"), ("thirty two", "32"), ("thirty three", "33"),
    ("forty five", "45"),
)
_WORD_NUMBERS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
    "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
    "eighteen": "18", "nineteen": "19", "twenty": "20", "thirty": "30",
    "forty": "40", "fifty": "50", "hundred": "100",
}


def normalise_expression(text: str) -> str:
    """Turn spoken arithmetic into a calculator key sequence.

    Raises ValueError rather than guessing: if anything outside the known words
    and arithmetic characters is present, FRED must not guess at what the user
    meant and press keys on their behalf.

    One left-to-right pass, not a chain of substitutions: rewriting "one" before
    "hundred" is seen turns "one hundred" into "1 100", and the calculator
    would happily return 1100.
    """
    expression = text.strip().lower()
    if not expression:
        raise ValueError("empty expression")

    multi = dict(_MULTI_WORD_NUMBERS)
    operators = dict(_WORD_OPERATORS)

    out: list[str] = []
    i = 0
    words = expression.split()
    n = len(words)
    while i < n:
        matched = False
        # Longest phrase first, so "twenty two" wins over "twenty".
        for size in (3, 2):
            phrase = " ".join(words[i:i + size])
            if size <= n - i and (phrase in multi or phrase in operators):
                out.append(multi.get(phrase) or operators[phrase])
                i += size
                matched = True
                break
        if matched:
            continue
        word = words[i]
        # "two hundred and fifty" is three separate words; "and" is filler and
        # dropping it here is what makes the number phrases readable.
        if word == "and":
            i += 1
            continue
        if word in _WORD_NUMBERS:
            out.append(_WORD_NUMBERS[word])
        elif word in operators:
            out.append(operators[word])
        else:
            out.append(word)
        i += 1

    expression = "".join(out)

    if not re.fullmatch(r"[0-9\s+\-*/.()^]+", expression):
        raise ValueError(f"'{text.strip()}' is not plain arithmetic")
    # Spaces are harmless to a calculator but wrong for verification, so the
    # final form is compacted anyway.
    return expression.replace(" ", "")


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

def run_plan(plan: Plan, assume_yes: bool = False) -> tuple[bool, str]:
    """Carry out one plan. Returns (verified, human-readable description)."""
    from . import hands

    if plan.app == "notepad":
        text = str(plan.args["text"])
        path = str(plan.args.get("path") or DEFAULT_NOTEPAD_PATH)
        ok = hands.notepad_write(text, path, assume_yes=assume_yes)
        return ok, f"wrote {len(text)} chars to {Path(path).resolve()}"

    if plan.app == "calculator":
        expression = str(plan.args["expression"])
        expect = plan.args.get("expect")
        try:
            expression = normalise_expression(expression)
        except ValueError as exc:
            raise PlanError(str(exc)) from exc
        ok = hands.calc(expression, expect=expect, assume_yes=assume_yes)
        return ok, f"calculated {expression!r}, display showed {hands.calculator_display()!r}"

    if plan.app == "explorer":
        folder = str(plan.args["folder"])
        try:
            # Resolve before acting: the window's title is matched on the real
            # folder name, so verification fails if this is left as the bare
            # word the planner produced.
            target = hands.resolve_folder(folder)
        except FileNotFoundError as exc:
            raise PlanError(str(exc)) from exc
        ok = hands.explorer_open(folder, assume_yes=assume_yes)
        names = hands.explorer_listing(folder)
        return ok, f"opened {target} showing {len(names)} item(s)"

    raise PlanError(f"no executor for {plan.app}.{plan.action}")


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------

def run_task(brain, transcript: str, assume_yes: bool = False,
             max_steps: int | None = None) -> Episode:
    """Plan, execute, verify, and log. Always returns an Episode.

    Every exit path -- success, refusal, crash, step cap -- is logged, because
    the failures are the only things Phase 5 gets to learn from.
    """
    settings = load_settings()
    cap = max_steps if max_steps is not None else settings.get("max_steps", 4)

    episode = Episode(transcript=transcript, started_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    steps = 0

    # Set a default intent so failures during planning still log something meaningful
    episode.intent = "planning"

    try:
        plan = plan_task(brain, transcript)
        if plan is None:
            episode.intent = "chat"
            episode.success = False
            episode.error = "not an actionable task"
            return episode

        episode.app = plan.app
        episode.intent = f"{plan.app}.{plan.action}"

        while steps < cap:
            steps += 1
            print(f"[step {steps}/{cap}] {plan.app}.{plan.action} {plan.args}")

            try:
                verified, description = run_plan(plan, assume_yes=assume_yes)
            except hands_refused() as exc:
                episode.record_step(f"{plan.app}.{plan.action}", plan.args,
                                    result="refused", error=str(exc))
                episode.actions = description = str(exc)
                episode.success = False
                episode.error = str(exc)
                print(f"\n[cancelled] {exc}")
                return episode
            except PlanError as exc:
                episode.record_step(f"{plan.app}.{plan.action}", plan.args,
                                    result="rejected", error=str(exc))
                episode.success = False
                episode.error = str(exc)
                print(f"\n[refused] {exc}")
                return episode
            except Exception as exc:
                episode.record_step(f"{plan.app}.{plan.action}", plan.args,
                                    result="error", error=f"{type(exc).__name__}: {exc}")
                episode.success = False
                episode.error = f"{type(exc).__name__}: {exc}"
                print(f"\n[error] {type(exc).__name__}: {exc}")
                return episode

            episode.record_step(f"{plan.app}.{plan.action}", plan.args,
                                result="ok" if verified else "unverified")
            episode.verify = bool(verified)
            episode.actions = description
            episode.success = bool(verified)

            if verified:
                print(f"[verified] {description}")
                break

            print(f"[not verified] {description}")

        if not episode.success and steps >= cap:
            episode.error = f"step cap ({cap}) reached without verification"
            print(f"\n[giving up] {episode.error}")

    except PlanError as exc:
        episode.success = False
        episode.error = str(exc)
        print(f"\n[refused] {exc}")
    except Exception as exc:
        episode.success = False
        episode.error = f"{type(exc).__name__}: {exc}"
        print(f"\n[error] {type(exc).__name__}: {exc}")
    finally:
        log_episode(episode)

    return episode


def hands_refused():
    """hands.ActionRefused, imported lazily to keep this module importable
    without pywinauto installed."""
    from .hands import ActionRefused

    return ActionRefused