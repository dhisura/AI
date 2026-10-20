"""Phase 0 environment check.

Run:  python scripts/doctor.py

Checks the things FRED needs to actually work on this machine: Python, Ollama
reachable, the model pulled, disk space, and (where the optional deps are
installed) audio devices. Exits non-zero if anything blocking is wrong, so it
can be used as a gate before later phases.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fred.config import DATA_DIR, load_settings  # noqa: E402

OK, WARN, BAD = "[ OK ]", "[WARN]", "[FAIL]"
results: list[tuple[str, str, str]] = []


def record(level: str, name: str, detail: str = "") -> None:
    results.append((level, name, detail))
    print(f"{level} {name}" + (f" -- {detail}" if detail else ""))


def check_python() -> None:
    ver = ".".join(str(v) for v in sys.version_info[:3])
    if sys.version_info >= (3, 11):
        record(OK, "Python", ver)
    else:
        record(BAD, "Python", f"{ver} -- need 3.11 or newer")


def check_ollama() -> bool:
    exe = shutil.which("ollama")
    if not exe:
        record(BAD, "Ollama binary", "not on PATH -- install from https://ollama.com/download")
        return False
    try:
        out = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=15
        ).stdout.strip()
        record(OK, "Ollama binary", out or exe)
    except Exception as exc:
        record(WARN, "Ollama binary", f"found at {exe} but --version failed: {exc}")
    return True


def check_server(host: str) -> list[str]:
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=8) as resp:
            import json

            data = json.loads(resp.read().decode("utf-8"))
        models = [m.get("name", "") for m in data.get("models", [])]
        record(OK, "Ollama server", f"{host} -- {len(models)} model(s)")
        return models
    except urllib.error.URLError as exc:
        record(BAD, "Ollama server", f"{host} unreachable ({exc.reason}). Start it: ollama serve")
    except Exception as exc:
        record(BAD, "Ollama server", f"{host} error: {exc}")
    return []


def check_model(models: list[str], wanted: str) -> None:
    if not models:
        record(BAD, "Model", f"{wanted} -- cannot verify, server unreachable")
        return
    if wanted in models:
        record(OK, "Model", wanted)
    else:
        record(BAD, "Model", f"{wanted} not pulled. Run: ollama pull {wanted}")
        if models:
            record(WARN, "Models present", ", ".join(models))


def check_inference(model: str, host: str) -> None:
    """A real completion -- proves the model loads and fits in memory."""
    payload = (
        '{"model":"%s","messages":[{"role":"user","content":"Reply with exactly: OK"}],'
        '"stream":false,"options":{"num_predict":8}}' % model
    )
    req = urllib.request.Request(
        f"{host}/api/chat", data=payload.encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            import json

            reply = json.loads(resp.read().decode("utf-8")).get("message", {}).get("content", "")
        if reply.strip():
            record(OK, "Inference", f"{model} replied: {reply.strip()[:60]!r}")
        else:
            record(WARN, "Inference", f"{model} returned an empty reply")
    except Exception as exc:
        record(BAD, "Inference", f"{model} failed to answer: {exc}")


def check_disk() -> None:
    try:
        usage = shutil.disk_usage(Path.home().anchor)
        free_gb = usage.free / 1e9
        if free_gb >= 10:
            record(OK, "Disk free", f"{free_gb:.0f} GB on {Path.home().anchor}")
        else:
            record(WARN, "Disk free", f"only {free_gb:.0f} GB on {Path.home().anchor}")
    except Exception as exc:
        record(WARN, "Disk free", f"could not measure: {exc}")


def check_voice() -> None:
    """Phase 2 readiness: ears and voice, if installed."""
    missing = [m for m in ("faster_whisper", "sounddevice")
               if importlib.util.find_spec(m) is None]
    if missing:
        record(WARN, "Ears", f"missing {', '.join(missing)} -- run: pip install -r requirements.txt")
        return
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from fred.ears import Ears, list_microphones

        mics = list_microphones()
        record(OK, "Microphone", f"{len(mics)} input device(s): "
                                 f"{mics[0][1] if mics else 'none'}")
        ears = Ears()
        record(OK, "Whisper", f"'{ears.model_size}' (int8, cpu)")
    except Exception as exc:
        record(WARN, "Ears", str(exc))

    try:
        from fred.voice import get_tts

        tts = get_tts()
        ok, why = tts.available()
        if ok:
            record(OK, "Voice", f"{tts.name}: {why}")
        else:
            record(WARN, "Voice", why)
    except Exception as exc:
        record(WARN, "Voice", str(exc))


def check_optional_deps() -> None:
    groups = {
        "Hands (Phase 3)": ["pywinauto", "pyautogui"],
    }
    for phase, mods in groups.items():
        missing = [m for m in mods if importlib.util.find_spec(m) is None]
        if missing:
            record(WARN, phase, "not installed yet -- expected before that phase")
        else:
            record(OK, phase, "installed")


def check_agent() -> None:
    """Phase 4: the task loop needs its own modules and a writable log."""
    try:
        from fred.agent import ALLOWED_ACTIONS, run_task  # noqa: F401
        from fred.memory import EPISODES

        record(OK, "Agent loop (Phase 4)",
               f"{len(ALLOWED_ACTIONS)} apps: {', '.join(sorted(ALLOWED_ACTIONS))}")
    except Exception as exc:
        record(BAD, "Agent loop (Phase 4)", str(exc))
        return

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = DATA_DIR / ".doctor-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        where = f"exists ({len(EPISODES.read_text(encoding='utf-8').splitlines())} episodes)" \
            if EPISODES.exists() else "ready, empty"
        record(OK, "Episode log", f"data/episodes.jsonl {where}")
    except Exception as exc:
        record(BAD, "Episode log", str(exc))


def check_lessons() -> None:
    """Phase 5: lesson infrastructure."""
    try:
        from fred.memory import confirmed_lessons, propose_lesson, all_lessons
        from fred.config import lessons_path

        lessons = confirmed_lessons()
        all_l = all_lessons()
        record(OK, "Lesson infrastructure (Phase 5)",
               f"{len(lessons)} confirmed, {len(all_l)} total in {lessons_path().name}")
    except Exception as exc:
        record(BAD, "Lesson infrastructure (Phase 5)", str(exc))

    try:
        from scripts.reflect import run_reflect
        record(OK, "reflect.py", "importable")
    except Exception as exc:
        record(WARN, "reflect.py", f"not importable: {exc}")


def main() -> int:
    settings = load_settings()
    print("FRED doctor\n" + "=" * 46)

    check_python()
    have_exe = check_ollama()
    models = check_server(settings["ollama_host"]) if have_exe else []
    check_model(models, settings["model"])
    if settings["model"] in models:
        check_inference(settings["model"], settings["ollama_host"])
    check_disk()
    check_voice()
    check_optional_deps()
    check_agent()
    check_lessons()

    print("=" * 46)
    failures = [r for r in results if r[0] == BAD]
    if failures:
        print(f"\n{len(failures)} blocking problem(s):")
        for _, name, detail in failures:
            print(f"  - {name}: {detail}")
        return 1
    print("\nAll blocking checks passed. Phases 0-4 are ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())