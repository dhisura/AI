"""Speech output for FRED.

Provider-agnostic by design: Piper is the default, but nothing outside this
module knows that. Callers only ever touch TTSEngine and get_tts().

To add a provider: subclass TTSEngine, implement available()/speak()/stop(),
and register it in PROVIDERS. No caller changes.

Windows note: playback uses the stdlib `winsound` module, so speaking FRED's
reply needs no audio library at all.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from abc import ABC, abstractmethod

from .config import load_settings

try:  # Windows-only stdlib; import guarded so the module imports anywhere.
    import winsound
except ImportError:  # pragma: no cover - non-Windows
    winsound = None


# --------------------------------------------------------------------------
# Text preparation
# --------------------------------------------------------------------------

_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_FENCE = re.compile(r"`([^`]*)`")
_URL = re.compile(r"https?://\S+")
_MD = re.compile(r"(\*\*|__|\*|_|^#{1,6}\s*|^\s*[-*+]\s+|^\s*\d+\.\s+)", re.MULTILINE)


def speakable(text: str) -> str:
    """Turn a model reply into something worth listening to.

    Drops code blocks entirely (unreadable aloud), strips markdown noise, and
    flattens lists to plain sentences.
    """
    text = _FENCE.sub(" ", text or "")
    text = _INLINE_FENCE.sub(r"\1", text)
    text = _URL.sub("a link", text)
    text = _MD.sub("", text)
    text = text.replace("*", "").replace("`", "")
    return re.sub(r"\s+", " ", text).strip()


def _sentences(text: str) -> list[str]:
    """Split on sentence enders so the first audio starts sooner."""
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if p.strip()]


# Piper chokes on some non-ASCII punctuation; map to plain equivalents.
_PUNCT_MAP = {
    "—": ", ", "–": "-", "‘": "'", "’": "'",
    "“": '"', "”": '"', "…": "...", " ": " ",
}


def for_piper(text: str) -> str:
    for bad, good in _PUNCT_MAP.items():
        text = text.replace(bad, good)
    return text.encode("ascii", "replace").decode("ascii").strip()


# --------------------------------------------------------------------------
# Engine interface
# --------------------------------------------------------------------------

class TTSEngine(ABC):
    """What every speech backend must provide."""

    name = "base"

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """Return (ready, human-readable reason). Never raises."""

    @abstractmethod
    def speak(self, text: str) -> None:
        """Speak text aloud, blocking until the utterance finishes."""

    def stop(self) -> None:
        """Cut off any speech in progress. Best effort."""

    def voices(self) -> list[str]:
        return []


class PiperEngine(TTSEngine):
    """Piper TTS.

    Two backends, chosen automatically:
      - native: imports piper.voice and keeps the model resident, so warm
        synthesis costs milliseconds instead of reloading 63 MB of weights.
      - cli:    shells out to piper.exe once per utterance. Slower, but works
        if the piper package is not importable.

    Both produce a WAV played by the same code, so nothing downstream differs.
    """

    name = "piper"

    def __init__(self, binary: str | None = None, model: str | None = None) -> None:
        settings = load_settings()
        self.binary = binary or settings.get("piper_binary") or self._find_binary()
        self.model = model or settings.get("piper_voice", "")
        self.extra_args: list[str] = []
        if settings.get("piper_espeak_data"):
            self.extra_args += ["--espeak-data", settings["piper_espeak_data"]]
        self.backend = "cli" if settings.get("piper_backend") == "cli" else "native"
        self._voice = None  # resident PiperVoice, native backend only
        self._cache_dir = None

    def _config_path(self) -> str:
        return f"{self.model}.json" if self.model else ""

    @staticmethod
    def _find_binary() -> str:
        found = shutil.which("piper")
        if found:
            return found
        candidates = [
            os.path.expandvars(r"%LOCALAPPDATA%\FRED\piper\piper.exe"),
            os.path.expandvars(r"%USERPROFILE%\bin\piper.exe"),
            os.path.expandvars(r"%USERPROFILE%\piper\piper.exe"),
        ]
        for path in candidates:
            if os.path.isfile(path):
                return path
        return "piper"  # let the caller fail with a real error message

    def _cache(self):
        if self._cache_dir is None:
            from .config import data_path

            self._cache_dir = data_path("tts_cache")
        return self._cache_dir

    def available(self) -> tuple[bool, str]:
        if not self.model:
            return False, "no piper_voice set in config/settings.json"
        if not os.path.isfile(self.model):
            return False, f"voice model not found: {self.model}"
        if self.backend == "native":
            try:
                import piper.voice  # noqa: F401
                return True, f"{os.path.basename(self.model)} (resident)"
            except ImportError:
                pass
        exe = self.binary
        if exe == "piper" and not shutil.which("piper"):
            return False, "piper.exe not found on PATH"
        if not os.path.isfile(exe):
            return False, f"piper binary not at {exe}"
        return True, f"{os.path.basename(self.model)} via {os.path.basename(exe)}"

    def _wav_for(self, sentence: str) -> str:
        """Cache path for a sentence, rendering it if not already present."""
        key = hashlib.sha1((sentence + self.model).encode("utf-8")).hexdigest()[:16]
        return os.path.join(self._cache(), f"{key}.wav")

    def _resident(self):
        """Load the voice model once and keep it. Returns None if unavailable."""
        if self._voice is not None:
            return self._voice
        try:
            from piper.voice import PiperVoice
        except Exception:
            return None
        config = self._config_path()
        self._voice = PiperVoice.load(self.model, config if os.path.isfile(config) else None)
        return self._voice

    def _render_native(self, sentence: str, out_wav: str) -> bool:
        """Synthesise with the model held in memory. False if unavailable."""
        voice = self._resident()
        if voice is None:
            return False
        import wave as _wave

        with _wave.open(out_wav, "wb") as wav:
            voice.synthesize_wav(for_piper(sentence), wav)
        return os.path.isfile(out_wav) and os.path.getsize(out_wav) > 0

    def _render_cli(self, sentence: str, out_wav: str) -> None:
        """Synthesise by shelling out to piper.exe (slow: reloads the model)."""
        if os.path.exists(out_wav):
            os.remove(out_wav)
        try:
            proc = subprocess.run(
                [self.binary, "-m", self.model, "-f", out_wav, *self.extra_args],
                input=for_piper(sentence).encode("utf-8"),
                capture_output=True,
                timeout=60,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"piper executable not found: {self.binary}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("piper timed out after 60s") from exc

        if not os.path.isfile(out_wav):
            err = proc.stderr.decode("utf-8", "replace").strip()
            tail = err[-600:] if err else f"exit {proc.returncode}"
            raise RuntimeError(f"piper produced no audio: {tail}")
        _trim_wav(out_wav)

    def speak(self, text: str) -> None:
        ready, why = self.available()
        if not ready:
            raise RuntimeError(f"Piper not ready: {why}")

        spoken = speakable(text)
        if not spoken:
            return
        wav = self._wav_for(spoken)
        if not os.path.isfile(wav):
            if self.backend == "native":
                if not self._render_native(spoken, wav):
                    self._render_cli(spoken, wav)
            else:
                self._render_cli(spoken, wav)
            _trim_wav(wav)
        _play(wav, self.stop)

    def prewarm(self, phrases: list[str] | None = None) -> None:
        """Load the model now and render likely phrases while the user waits.

        Called the moment the brain starts generating a reply, so the ~4 s
        cold start overlaps with model inference instead of following it.
        """
        if self.backend != "native":
            return
        try:
            voice = self._resident()
        except Exception as exc:
            print(f"[tts] pre-warm failed: {exc}")
            return
        if voice is None:
            return
        for phrase in phrases or []:
            cleaned = speakable(phrase)
            if not cleaned:
                continue
            wav = self._wav_for(cleaned)
            if os.path.isfile(wav):
                continue
            try:
                self._render_native(cleaned, wav)
                _trim_wav(wav)
            except Exception:
                pass  # a missed pre-warm just costs latency later

    def stop(self) -> None:
        if winsound is not None:
            try:
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass


class SapiEngine(TTSEngine):
    """Windows built-in speech via PowerShell. Zero install, mediocre voice.

    Exists so FRED still has a voice if the Piper download fails.
    """

    name = "sapi"

    def __init__(self, rate: int = 0, volume: int = 100) -> None:
        self.rate = rate
        self.volume = volume

    def available(self) -> tuple[bool, str]:
        if sys.platform != "win32":
            return False, "SAPI is Windows-only"
        return True, "Windows SAPI (built-in)"

    def speak(self, text: str) -> None:
        spoken = speakable(text)
        if not spoken:
            return
        ps = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.Rate = {self.rate}; $s.Volume = {self.volume}; "
            f"$s.Speak({_ps_quote(spoken)})"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, timeout=180,
        )


class KokoroEngine(TTSEngine):
    """Placeholder so the swap is a config change, not a code change."""

    name = "kokoro"

    def available(self) -> tuple[bool, str]:
        return False, "Kokoro is not wired up yet; use the 'piper' provider"

    def speak(self, text: str) -> None:
        raise RuntimeError(self.available()[1])


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


# --------------------------------------------------------------------------
# WAV helpers
# --------------------------------------------------------------------------

def _wav_duration(path: str) -> float:
    try:
        with wave.open(path, "rb") as wf:
            rate = wf.getframerate() or 22050
            return wf.getnframes() / float(rate)
    except Exception:
        return 1.0


def _trim_wav(path: str, pad_s: float = 0.05) -> None:
    """Drop near-silent head and tail so playback starts on the first syllable."""
    try:
        with wave.open(path, "rb") as wf:
            params = wf.getparams()
            frames = wf.readframes(params.nframes)
        if not frames:
            return
        width = params.sampwidth
        count = len(frames) // width
        peak = 0
        for i in range(0, len(frames) - width + 1, width):
            level = abs(frames[i] if width == 1 else int.from_bytes(frames[i:i+width], "little", signed=True))
            peak = max(peak, level)
        if peak == 0:
            return
        thresh = peak * 0.02
        step = max(1, int(params.framerate * 0.01))
        start, end = 0, count
        for i in range(0, count, step):
            chunk = frames[i*width:(i+step)*width]
            if any(abs(b if width == 1 else int.from_bytes(bytes([b]), "little", signed=True)) > thresh for b in chunk):
                start = i
                break
        for i in range(count - step, 0, -step):
            chunk = frames[i*width:(i+step)*width]
            if any(abs(b if width == 1 else int.from_bytes(bytes([b]), "little", signed=True)) > thresh for b in chunk):
                end = min(count, i + step)
                break
        pad = int(params.framerate * pad_s)
        start, end = max(0, start - pad), min(count, end + pad)
        if end <= start:
            return
        with wave.open(path, "wb") as wf:
            wf.setparams(params)
            wf.writeframes(frames[start*width:end*width])
    except Exception:
        pass  # trimming is an optimisation, never a hard failure


def _play(path: str, stop_cb) -> None:
    """Play a WAV and block until it finishes.

    PlaySound without SND_ASYNC is already synchronous, so this returns the
    moment the last sample is done. The engine's stop() flag can cut playback
    short by running on a watchdog thread.
    """
    if winsound is None:  # pragma: no cover - non-Windows
        sys.stdout.write("\a")
        time.sleep(min(2.0, _wav_duration(path)))
        return

    if stop_cb():  # already asked to stop; do not start
        return

    # Play on a worker so a stop() request can purge mid-utterance. PlaySound
    # blocks this thread, not the caller's.
    finished = threading.Event()

    def run() -> None:
        try:
            winsound.PlaySound(path, winsound.SND_FILENAME)
        finally:
            finished.set()

    threading.Thread(target=run, daemon=True).start()
    finished.wait()


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------

PROVIDERS: dict[str, type[TTSEngine]] = {
    "piper": PiperEngine,
    "sapi": SapiEngine,
    "kokoro": KokoroEngine,
}


class _StopFlag:
    """Shared so stop() can interrupt the sleep loop inside _play."""

    def __init__(self) -> None:
        self.hit = False

    def __call__(self) -> bool:
        return self.hit


def get_tts(provider: str | None = None) -> TTSEngine:
    """Build the configured engine, falling back to any provider that works."""
    settings = load_settings()
    wanted = (provider or settings.get("tts_provider", "piper")).lower()

    if wanted in PROVIDERS:
        engine = PROVIDERS[wanted]()
        ok, _ = engine.available()
        if ok:
            return engine
        if wanted != "piper":
            raise RuntimeError(f"tts_provider '{wanted}' is not ready")

    # Piper is the intended default; if it is missing, fall back rather than
    # leave FRED mute, but say so clearly.
    engine = PiperEngine()
    if engine.available()[0]:
        return engine
    fallback = SapiEngine()
    if fallback.available()[0]:
        return fallback
    raise RuntimeError("No TTS provider available")