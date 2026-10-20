"""Verify the speech-in path without needing anyone to talk.

Synthesises a known sentence with Piper, then pushes that audio through the
exact code path -- resample, silence trim, whisper -- and prints what FRED
heard. If this round-trips, recording and transcription are sound; the only
untested part is the microphone itself.
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fred.ears import Ears, _resample, _trim_silence  # noqa: E402
from fred.voice import PiperEngine  # noqa: E402

CASES = [
    "FRED here. Speech test. If you can hear this, audio output works.",
    "Open notepad and write hello world.",
]


def wav_to_float(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as wf:
        sr = wf.getframerate()
        width = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())
    if width == 2:
        data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 1:
        data = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise SystemExit(f"unsupported sample width {width}")
    return data, sr


def main() -> int:
    piper = PiperEngine()
    ok, why = piper.available()
    if not ok:
        print(f"[SKIP] piper unavailable: {why}")
        return 0

    ears = Ears()
    print(f"whisper model: {ears.model_size}\n")

    passed = 0
    for text in CASES:
        wav = piper._wav_for(text)
        if not Path(wav).exists():
            piper._render(text, wav)

        audio, sr = wav_to_float(wav)
        print(f"said   : {text}")
        print(f"  audio: {len(audio) / sr:.1f}s @ {sr} Hz, peak {np.max(np.abs(audio)):.3f}")

        # Exactly what Ears.transcribe does, given a non-16k input.
        trimmed = _trim_silence(audio, sr)
        heard = ears.transcribe(trimmed, samplerate=sr)
        print(f"  heard : {heard!r}")

        if heard:
            passed += 1
        print()

    print(f"{passed}/{len(CASES)} round-trips produced text")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())