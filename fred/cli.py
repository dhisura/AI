"""FRED terminal entry point.

    python -m fred            # interactive chat (text)
    python -m fred --voice    # voice mode: hold F8 to talk, FRED answers aloud
    python -m fred --speakers # one-shot test: speak a sentence and exit
    python -m fred --mic      # one-shot test: record 5s and print the transcript
    python -m fred "hi"       # one-shot question
    python -m fred --reset    # clear conversation history
    python -m fred --reflect  # scan failures, propose lessons (Phase 5)
    python -m fred --launch "app name"  # universal launcher (Win -> type -> Enter)

In voice mode you can still type: /exit quits, /mute silences the voice,
/reset clears the conversation.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

from .brain import Brain, BrainError
from .config import DATA_DIR, load_settings
from .memory import all_lessons, confirm_lesson, retire_lesson

BANNER = "FRED -- local, offline, yours."


# --------------------------------------------------------------------------
# Text mode
# --------------------------------------------------------------------------

def _repl(brain: Brain, tts=None, muted: bool = False) -> None:
    print(BANNER)
    print(f"model: {brain.model}")
    if tts is not None:
        print(f"voice: {tts.name}")
    print()

    # Shared across modes: /mute flips `muted`, and _prewarm_hook needs to see
    # the flip, so it reads a one-element list rather than a bool snapshot.
    state = {"muted": muted}
    hooks = _prewarm_hook(tts, state)
    while True:
        try:
            line = input("\nyou > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not line:
            continue
        if line in {"/exit", "/quit"}:
            break
        if line == "/reset":
            brain.reset()
            print("[conversation cleared]")
            continue
        if line == "/mute":
            state["muted"] = not state["muted"]
            print(f"[voice {'off' if state['muted'] else 'on'}]")
            continue

        line = _dispatch(brain, tts, line, state["muted"], hooks)
        if line is None:  # brain unreachable
            continue
        print(f"\nFRED> {line}")
        _say(tts, line, state["muted"])


def _say(tts, text: str, muted: bool = False) -> None:
    """Speak a reply, never letting audio problems kill the conversation."""
    if tts is None or muted:
        return
    try:
        tts.speak(text)
    except Exception as exc:
        print(f"[tts error] {exc}")


def _prewarm_hook(tts, state: dict) -> dict:
    """Build the hook that warms Piper while the brain is still thinking.

    Runs on a daemon thread: loading the voice model takes ~3 s, and blocking
    the reply on it would defeat the point.

    `state` is the shared muted flag, so /mute turning the voice off actually
    stops pre-warming instead of quietly carrying on.
    """
    done = {"value": False}

    def on_thinking() -> None:
        # prewarm() only exists on PiperEngine, so a swapped-in provider without
        # it must not take the whole reply down.
        warm = getattr(tts, "prewarm", None)
        if tts is None or state.get("muted") or done["value"] or warm is None:
            return
        done["value"] = True
        threading.Thread(target=warm, daemon=True).start()

    return {"on_thinking": on_thinking}


def _dispatch(brain: Brain, tts, line: str, muted: bool,
              hooks: dict | None = None) -> str | None:
    """One conversational turn, with the Phase 4 task loop in front of the brain.

    Order matters: the planner only sees instructions to answer `{"intent":
    "chat"}` for anything it cannot do, so anything it does classify as a task
    must actually be run. The model must not be allowed to talk its way out of
    an action it was asked to take.
    """
    print("[thinking...]")
    if not muted:
        _fire(hooks)

    from .agent import PlanError, plan_task, run_task

    try:
        plan = plan_task(brain, line)
    except PlanError as exc:
        print(f"\n[refused] {exc}")
        # Refusal is a tool result, so the model may report it verbatim.
        try:
            return "".join(brain.chat_stream(
                f"The task runner refused that request: {exc}. "
                f"Tell the user plainly what happened, in one sentence."
            ))
        except BrainError:
            return None
    except Exception as exc:
        print(f"\n[planner error] {type(exc).__name__}: {exc} -- answering as chat")

    if plan is not None:
        # Start the voice engine while the UI work happens, exactly as we do
        # while the LLM thinks -- app automation takes just as long.
        if not muted and hooks:
            _fire(hooks)
        episode = run_task(brain, line, assume_yes=False)
        if episode.intent == "chat":
            pass  # fall through to normal chat
        elif episode.success:
            return f"Done. {episode.actions}."
        elif episode.error:
            return f"I could not finish that. {episode.error}"
        else:
            return "I could not finish that."

    try:
        return "".join(brain.chat_stream(line))
    except BrainError as exc:
        print(f"\n[error] {exc}\n")
        return None


def _fire(hooks: dict | None) -> None:
    if not hooks:
        return
    try:
        hooks["on_thinking"]()
    except Exception as exc:
        print(f"[pre-warm failed] {exc}")


def _once(brain: Brain, question: str) -> int:
    try:
        print("".join(brain.chat_stream(question)).strip())
    except BrainError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------
# Voice mode
# --------------------------------------------------------------------------

def _voice_loop(brain: Brain, ears, tts, vk: int, muted: bool = False) -> None:
    """Non-blocking loop that accepts both speech (hold PTT) and typing.

    Uses msvcrt for typed input (Windows console, no dependency) and
    GetAsyncKeyState for the push-to-talk key, so neither blocks the other.
    """
    import msvcrt

    typed: list[str] = []
    state = {"muted": muted}
    hooks = _prewarm_hook(tts, state)

    print(BANNER)
    print(f"model: {brain.model}")
    print(f"voice: {tts.name if tts else 'off'}")
    print(f"ears:  whisper '{ears.model_size}'")
    print(f"\nHold F{vk - 0x6F} and speak. Release to send. Type /exit to quit.\n")
    print("Note: confirmation prompts are TYPED only. Say nothing at them.\n")

    while True:
        # 1. Push-to-talk has priority.
        if _key_held(vk):
            _handle_speech(brain, ears, tts, vk, state, hooks)
            continue

        # 2. Otherwise drain anything typed.
        if not msvcrt.kbhit():
            time.sleep(0.01)
            continue

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):  # extended key: arrows, etc. -- drop the pair
            msvcrt.getwch()
            continue
        if ch == "\x03":  # Ctrl+C
            raise KeyboardInterrupt
        if ch in ("\r", "\n"):
            line = "".join(typed).strip()
            typed.clear()
            print()
            if line in {"/exit", "/quit"}:
                break
            if line == "/reset":
                brain.reset()
                print("[conversation cleared]")
                continue
            if line == "/mute":
                state["muted"] = not state["muted"]
                print(f"[voice {'off' if state['muted'] else 'on'}]")
                continue
            if line:
                print(f"\nyou> {line}")
                reply = _dispatch(brain, tts, line, state["muted"], hooks)
                if reply is not None:
                    print(f"\nFRED> {reply}")
                    _say(tts, reply, state["muted"])
            continue
        if ch == "\x08":  # backspace
            if typed:
                typed.pop()
                sys.stdout.write("\b \b")
                sys.stdout.flush()
            continue
        typed.append(ch)
        sys.stdout.write(ch)
        sys.stdout.flush()


def _key_held(vk: int) -> bool:
    import ctypes

    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


def _handle_speech(brain: Brain, ears, tts, vk: int, state: dict,
                   hooks: dict | None = None) -> None:
    """Record one utterance, answer it, speak the reply."""
    print("[recording...]")
    try:
        text = ears.capture(vk)
    except Exception as exc:
        print(f"[ears error] {exc}")
        return
    if not text:
        print("[nothing heard]")
        return
    print(f"\nyou (spoken)> {text}")
    reply = _dispatch(brain, tts, text, state["muted"], hooks)
    if reply is None:
        return
    print(f"\nFRED> {reply}")
    _say(tts, reply, state["muted"])


def _answer(brain: Brain, tts, question: str, muted: bool,
            hooks: dict | None = None) -> None:
    """Get a reply, then speak it. Thin wrapper kept for --no-task callers."""
    reply = _dispatch(brain, tts, question, muted, hooks)
    if reply is None:
        return
    if not reply.strip():
        print("[no reply from model -- check the Ollama log]")
        return
    print(f"\nFRED> {reply}")
    _say(tts, reply, muted)


# --------------------------------------------------------------------------
# Phase 2 self-tests
# --------------------------------------------------------------------------

def _test_speakers() -> int:
    from .voice import get_tts

    text = "FRED here. Speech test. If you can hear this, audio output works."
    try:
        tts = get_tts()
    except RuntimeError as exc:
        print(f"[FAIL] {exc}")
        return 1
    ok, why = tts.available()
    print(f"provider: {tts.name} ({why})")
    if not ok:
        print(f"[FAIL] {why}")
        return 1
    print("speaking now...")
    started = time.time()
    try:
        tts.speak(text)
    except Exception as exc:
        print(f"[FAIL] {exc}")
        return 1
    print(f"[ OK ] spoke {len(text)} chars in {time.time() - started:.1f}s")
    return 0


def _test_mic(seconds: float = 5.0) -> int:
    from .ears import Ears, EarsError, list_microphones

    try:
        mics = list_microphones()
    except EarsError as exc:
        print(f"[FAIL] {exc}")
        return 1
    if not mics:
        print("[FAIL] no input devices found")
        return 1
    print("input devices:")
    for idx, name, ch in mics:
        print(f"  [{idx}] {name} ({ch} ch)")
    if load_settings().get("mic_device") is None:
        print("  (using the system default)")

    ears = Ears()
    print(f"\nspeak now -- recording {seconds:.0f}s...")
    try:
        audio = ears.record(seconds)
    except EarsError as exc:
        print(f"[FAIL] {exc}")
        return 1
    peak = float(abs(audio).max())
    print(f"[ OK ] captured {len(audio) / ears.settings['sample_rate']:.1f}s, "
          f"peak level {peak:.3f}")
    if peak < 0.005:
        print("[WARN] level is very low -- check the mic volume in Windows settings")
    try:
        print("transcribing (first run downloads the model)...")
        text = ears.transcribe(audio)
    except EarsError as exc:
        print(f"[FAIL] {exc}")
        return 1
    if text:
        print(f'[ OK ] heard: "{text}"')
        return 0
    print("[WARN] transcribed to nothing -- try again, or use --mic to retry")
    return 1


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Phase 3: hands
# --------------------------------------------------------------------------

def _test_matcher(text: str) -> int:
    from .hands import extract_app

    allowed = ["Notepad.exe", "Calculator.exe", "explorer.exe", "chrome.exe"]
    print(f"heard   : {text!r}")
    match = extract_app(text, allowed)
    if match:
        print(f"resolved: {match.app} (score {match.score:.2f}, via {match.via})")
        return 0
    print("resolved: no match -- FRED would ask instead of guessing")
    return 1


def _run_notepad(text: str, path: str, assume_yes: bool) -> int:
    from .hands import ActionRefused, notepad_write, verify_file

    try:
        ok = notepad_write(text, path, assume_yes=assume_yes)
    except ActionRefused as exc:
        print(f"\n[cancelled] {exc}")
        return 1
    except Exception as exc:
        print(f"\n[error] {type(exc).__name__}: {exc}")
        return 1

    if ok and verify_file(path, text):
        print(f"\n[ OK ] verified on disk: {Path(path).resolve()}")
        return 0
    print(f"\n[FAIL] the file was not written correctly to {path}")
    return 1


# --------------------------------------------------------------------------
# Phase 4: task loop
# --------------------------------------------------------------------------

def _task(brain: Brain, transcript: str, assume_yes: bool) -> str:
    """Plan, act, verify, then answer honestly from the tool result.

    The reply is built from the episode, not from the model's imagination: the
    only claim FRED may make is the one the verifier actually confirmed.
    """
    from .agent import run_task

    episode = run_task(brain, transcript, assume_yes=assume_yes)

    if episode.intent == "chat":
        return ""  # not a task; let the normal brain answer it
    if episode.success:
        return f"Done. {episode.actions}."
    if episode.error:
        return f"I did not manage that. {episode.error}"
    return "I did not manage that."


def _test_plan(text: str) -> int:
    """Show what the planner would decide, without acting. Debugging aid."""
    from .agent import PlanError, plan_task

    brain = Brain()
    try:
        plan = plan_task(brain, text)
    except PlanError as exc:
        print(f"plan REJECTED: {exc}")
        return 1
    if plan is None:
        print("plan: chat (no action)")
        return 0
    print(f"plan: {plan.app}.{plan.action} {plan.args}")
    return 0


def _task_mode(transcript: str, assume_yes: bool = False,
               max_steps: int | None = None) -> int:
    """Run one task end to end and report what the verifier actually found."""
    from .agent import run_task

    brain = Brain()
    episode = run_task(brain, transcript, assume_yes=assume_yes,
                       max_steps=max_steps)
    if episode.intent == "chat":
        print("[plan] not an actionable task -- try chat instead")
        return 1
    print(f"\n[{' OK ' if episode.success else 'FAIL'}] {episode.intent}: "
          f"{episode.actions or episode.error}")
    print("[episode] logged to data/episodes.jsonl")
    return 0 if episode.success else 1


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fred", description="FRED, your local AI agent.")
    parser.add_argument("question", nargs="*", help="ask one question and exit")
    parser.add_argument("--reset", action="store_true", help="clear conversation history and exit")
    parser.add_argument("--model", help="override the model in config/settings.json")
    parser.add_argument("--voice", action="store_true", help="voice mode: hold F8 to talk")
    parser.add_argument("--speakers", action="store_true", help="test speech output and exit")
    parser.add_argument("--mic", action="store_true", help="test the microphone and exit")
    parser.add_argument("--no-tts", action="store_true", help="voice mode without speaking")
    parser.add_argument("--ptt", default="F8", help="push-to-talk key (default F8)")

    # Phase 3: hands
    parser.add_argument("--match", metavar="TEXT",
                        help="show what TEXT resolves to (fuzzy app matching) and exit")
    parser.add_argument("--notepad", metavar="TEXT",
                        help="open Notepad, type TEXT, save and verify it")
    parser.add_argument("--save-to", default=str(DATA_DIR / "notepad" / "fred.txt"),
                        help="where --notepad writes (default data/notepad/fred.txt)")
    parser.add_argument("--yes", action="store_true",
                        help="skip confirmation prompts (testing only)")

    # Phase 4: task loop
    parser.add_argument("--task", metavar="TEXT",
                        help="plan and run TEXT as a task (Phase 4)")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --task, show the plan without acting")
    parser.add_argument("--max-steps", type=int,
                        help="override the hard step cap (default from settings)")

    # Phase 5: lessons & universal launcher
    parser.add_argument("--reflect", action="store_true",
                        help="scan failures and propose lessons (Phase 5)")
    parser.add_argument("--launch", metavar="APP",
                        help="launch any app via Win -> type -> Enter (universal launcher)")
    parser.add_argument("--lesson", nargs=2, metavar=("ACTION", "INDEX"),
                        help="manage lessons: confirm N / retire N")

    args = parser.parse_args(argv)

    settings = load_settings()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if args.speakers:
        return _test_speakers()
    if args.mic:
        return _test_mic()
    if args.match:
        return _test_matcher(args.match)
    if args.notepad:
        return _run_notepad(args.notepad, args.save_to, args.yes)
    if args.dry_run:
        return _test_plan(" ".join(args.task or args.question).strip())

    if args.task:
        return _task_mode(args.task, assume_yes=args.yes,
                          max_steps=args.max_steps)

    # Phase 5: --reflect, --launch, --lesson
    if args.reflect:
        from scripts.reflect import run_reflect
        return run_reflect(dry_run=False)

    if args.launch:
        from .hands import launch_universal
        try:
            ok = launch_universal(args.launch, assume_yes=args.yes)
            if ok:
                print(f"[OK] Launched {args.launch}")
                return 0
            print(f"[FAIL] Could not launch {args.launch}")
            return 1
        except Exception as exc:
            print(f"[FAIL] {exc}")
            return 1

    if args.lesson:
        action, index_str = args.lesson
        try:
            index = int(index_str)
        except ValueError:
            print(f"[FAIL] index must be an integer, got {index_str!r}")
            return 1
        if action == "confirm":
            if confirm_lesson(index):
                print(f"[OK] Lesson {index} marked as confirmed")
                return 0
            print(f"[FAIL] No lesson at index {index}")
            return 1
        elif action == "retire":
            if retire_lesson(index):
                print(f"[OK] Lesson {index} marked as retired")
                return 0
            print(f"[FAIL] No lesson at index {index}")
            return 1
        else:
            print(f"[FAIL] unknown lesson action {action!r} (use confirm or retire)")
            return 1

    if args.reset:
        brain = Brain(model=args.model)
        brain.reset()
        print(f"[conversation cleared; model {brain.model}]")
        return 0

    brain = Brain(model=args.model)
    question = " ".join(args.question).strip()
    if question:
        return _once(brain, question)

    if args.voice:
        return _voice_mode(brain, args)

    _repl(brain)
    return 0


def _voice_mode(brain: Brain, args) -> int:
    """Start voice mode, degrading to text if audio is unavailable."""
    from .ears import Ears, EarsError, VK

    vk = VK.get(args.ptt.upper())
    if vk is None:
        print(f"[FAIL] unknown push-to-talk key '{args.ptt}' (use F1-F10)")
        return 1

    tts = None
    if not args.no_tts:
        from .voice import get_tts

        try:
            tts = get_tts()
        except RuntimeError as exc:
            print(f"[tts unavailable] {exc}")

    try:
        ears = Ears()
        ears.preload()  # load whisper now, not on the first turn
    except EarsError as exc:
        print(f"[ears unavailable] {exc}")
        print("[falling back to text mode]")
        _repl(brain, tts=tts)
        return 0

    try:
        _voice_loop(brain, ears, tts, vk, muted=args.no_tts)
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())