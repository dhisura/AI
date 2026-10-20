"""FRED's ears: push-to-talk transcription.

faster-whisper `base`, int8, CPU. The model loads lazily (importing this module
is cheap) and is kept warm between turns because reloading it is the slow part.

Push-to-talk key is polled with the Windows GetAsyncKeyState API through ctypes,
so there is no keyboard-hook dependency and no admin rights needed.
"""

from __future__ import annotations

import ctypes
import gc
import sys
import threading
import time
from typing import Callable

import numpy as np

from .config import load_settings

try:
    import sounddevice as sd
except ImportError:
    sd = None

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None

# Virtual-key codes for the push-to-talk key.
VK = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74,
    "F6": 0x75, "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79,
}
WHISPER_MODELS = ("tiny", "base", "small")


class EarsError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Low-level helpers
# --------------------------------------------------------------------------

def _key_down(vk: int) -> bool:
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


def _resample(audio: np.ndarray, src: int, dst: int = 16000) -> np.ndarray:
    """Linear-interpolation resample.

    faster-whisper assumes any raw array it is handed is 16 kHz, so this is
    mandatory, not cosmetic. Avoids a scipy dependency for one operation.
    """
    if src == dst or len(audio) == 0:
        return audio.astype(np.float32)
    n_out = int(round(len(audio) * dst / float(src)))
    if n_out <= 0:
        return audio.astype(np.float32)
    x_old = np.linspace(0.0, 1.0, num=len(audio), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


def _trim_silence(audio: np.ndarray, sr: int, threshold: float = 0.02,
                  pad_ms: int = 150) -> np.ndarray:
    """Cut leading/trailing quiet stretches using an RMS gate."""
    if len(audio) == 0:
        return audio
    win = max(1, int(sr * 0.02))
    frames = len(audio) // win
    if frames < 2:
        return audio
    trimmed = audio[: frames * win].reshape(frames, win)
    energy = np.sqrt(np.mean(trimmed.astype(np.float32) ** 2, axis=1))
    if energy.max() <= 0:
        return audio
    gate = max(threshold, energy.max() * 0.06)
    loud = np.where(energy > gate)[0]
    if len(loud) == 0:
        return audio
    pad = int(sr * pad_ms / 1000)
    start = max(0, loud[0] * win - pad)
    end = min(len(audio), (loud[-1] + 1) * win + pad)
    return audio[start:end]


# --------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------

def list_microphones() -> list[tuple[int, str, int]]:
    """Return [(index, name, max_input_channels)] for every input device."""
    if sd is None:
        raise EarsError("sounddevice is not installed -- run: pip install sounddevice")
    out = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev.get("max_input_channels", 0) > 0:
            out.append((idx, dev["name"], dev["max_input_channels"]))
    return out


def _resolve_device(index: int | None) -> int | None:
    """None means sounddevice's default input device."""
    if index is not None:
        return index
    settings = load_settings()
    return settings.get("mic_device") or None


# --------------------------------------------------------------------------
# Ears
# --------------------------------------------------------------------------

class Ears:
    """Holds one whisper model; transcribes recorded audio."""

    def __init__(self, model_size: str | None = None, device: str = "cpu",
                 compute_type: str = "int8") -> None:
        settings = load_settings()
        self.settings = settings
        self.model_size = model_size or self._auto_model(settings)
        self.device = device
        self.compute_type = compute_type
        self.min_seconds = float(settings.get("min_speech_s", 0.6))
        self.max_seconds = float(settings.get("max_speech_s", 30.0))
        self._model = None

    @staticmethod
    def _auto_model(settings: dict) -> str:
        """Honour the configured model, but step down to `tiny` if RAM is tight.

        faster-whisper `base` costs roughly 300 MB resident; below ~2.5 GB free
        we take the smaller hit rather than risk swapping.
        """
        wanted = settings.get("whisper_model", "base")
        floor = settings.get("free_ram_floor_mb", 2500)
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            free_mb = stat.ullAvailPhys / (1024 * 1024)
            if free_mb < floor and wanted in ("small", "base"):
                smaller = "tiny" if wanted != "tiny" else "tiny"
                print(f"[ears] only {free_mb:.0f} MB free RAM -- using whisper "
                      f"'{smaller}' instead of '{wanted}'")
                return smaller
        except Exception:
            pass
        return wanted

    # -- model lifecycle ------------------------------------------------

    def preload(self) -> None:
        """Load the model ahead of time so the first turn is not slow."""
        self._ensure_model()

    def _ensure_model(self):
        if self._model is None:
            if WhisperModel is None:
                raise EarsError(
                    "faster-whisper is not installed -- run: pip install faster-whisper"
                )
            print(f"[ears] loading whisper '{self.model_size}' ({self.compute_type}, "
                  f"{self.device})...")
            try:
                self._model = WhisperModel(
                    self.model_size, device=self.device,
                    compute_type=self.compute_type,
                )
            except Exception as exc:
                raise EarsError(
                    f"Could not load whisper '{self.model_size}': {exc}\n"
                    "The model downloads from HuggingFace on first run, so this "
                    "needs internet once. Try again, or set a smaller model in "
                    "config/settings.json."
                ) from exc
        return self._model

    def unload(self) -> None:
        self._model = None
        gc.collect()

    # -- recording ------------------------------------------------------

    def record(self, seconds: float, samplerate: int | None = None,
               device: int | None = None) -> np.ndarray:
        """Record a fixed-length mono float32 clip. Blocking."""
        if sd is None:
            raise EarsError("sounddevice is not installed -- run: pip install sounddevice")
        sr = samplerate or int(self.settings.get("sample_rate", 16000))
        dev = _resolve_device(device)
        chunks: list[np.ndarray] = []
        wanted = int(sr * seconds)

        def callback(indata, frames, time_info, status):  # noqa: ARG001
            chunks.append(indata[:, 0].copy())

        try:
            with sd.InputStream(samplerate=sr, channels=1, dtype="float32",
                                device=dev, callback=callback):
                collected = 0
                deadline = time.time() + seconds + 5.0
                while collected < wanted and time.time() < deadline:
                    time.sleep(0.02)
                    collected = sum(len(c) for c in chunks)
        except Exception as exc:
            raise EarsError(f"Could not open the microphone: {exc}") from exc

        if not chunks:
            raise EarsError("No audio captured -- is the right microphone selected?")
        return np.concatenate(chunks).astype(np.float32)

    def listen(self, ptt_key: str = "F8", max_seconds: float = 30.0,
               min_seconds: float = 0.6, samplerate: int | None = None,
               device: int | None = None,
               ) -> str | None:
        """Block until the push-to-talk key is pressed, then capture.

        Convenient for scripts and the `--mic` test. The interactive voice loop
        instead polls the key itself and calls capture() directly, so typing and
        speech can be handled without blocking each other.
        """
        vk = VK.get(ptt_key.upper())
        if vk is None:
            raise EarsError(f"Unknown push-to-talk key '{ptt_key}' (try F1-F10)")

        # Wait for the key to be released first, so a key held down from a
        # previous turn does not immediately start a new recording.
        while _key_down(vk):
            time.sleep(0.02)

        print(f"  hold {ptt_key.upper()} to talk... (release to send)")
        while not _key_down(vk):
            time.sleep(0.01)

        return self.capture(vk, max_seconds, min_seconds, samplerate, device)

    def capture(self, vk: int, max_seconds: float = 30.0, min_seconds: float = 0.6,
                samplerate: int | None = None, device: int | None = None) -> str | None:
        """Record while `vk` is held, then transcribe. Returns None if silent."""
        return self._capture_and_transcribe(
            vk, max_seconds, min_seconds, samplerate, device
        )

    def _capture_and_transcribe(self, vk: int, max_seconds: float,
                                min_seconds: float, samplerate, device) -> str | None:
        max_seconds = max_seconds or self.max_seconds
        min_seconds = min_seconds or self.min_seconds
        if sd is None:
            raise EarsError("sounddevice is not installed")
        sr = samplerate or int(self.settings.get("sample_rate", 16000))
        dev = _resolve_device(device)

        chunks: list[np.ndarray] = []
        started = time.time()

        def callback(indata, frames, time_info, status):  # noqa: ARG001
            chunks.append(indata[:, 0].copy())

        with sd.InputStream(samplerate=sr, channels=1, dtype="float32",
                            device=dev, callback=callback):
            # Record while held, but keep going briefly after release so the
            # tail of the word is not clipped.
            while _key_down(vk) and time.time() - started < max_seconds:
                time.sleep(0.01)
            tail_until = time.time() + 0.35
            if time.time() - started < min_seconds:
                tail_until = time.time() + (min_seconds - (time.time() - started))
            while time.time() < tail_until:
                time.sleep(0.02)

        if not chunks:
            print("[ears] nothing recorded")
            return None

        audio = np.concatenate(chunks)
        dur = len(audio) / float(sr)
        print(f"[ears] {dur:.1f}s captured")

        peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
        if peak < 0.002:
            print("[ears] heard only silence -- check the mic and try again")
            return None

        audio = _trim_silence(audio, sr)
        text = self.transcribe(audio, samplerate=sr)
        return text or None

    # -- transcription --------------------------------------------------

    def transcribe(self, audio: np.ndarray, samplerate: int | None = None) -> str:
        sr = samplerate or int(self.settings.get("sample_rate", 16000))
        audio_16k = _resample(np.asarray(audio, dtype=np.float32), sr, 16000)
        if len(audio_16k) < 1600:  # under 100 ms
            return ""
        model = self._ensure_model()
        segments, _info = model.transcribe(
            audio_16k,
            language="en",
            beam_size=self.settings.get("beam_size", 1),
            vad_filter=self.settings.get("vad_filter", True),
            condition_on_previous_text=False,  # stops runaway repetition
        )
        return " ".join(s.text.strip() for s in segments).strip()