"""Download a Piper voice model into the FRED data directory.

    python scripts/fetch_voice.py                # default: en_US-amy-medium
    python scripts/fetch_voice.py en_GB-alba-medium
    python scripts/fetch_voice.py en_US-ryan-high --list

Run once. After that FRED is fully offline for speech.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fred.config import data_path  # noqa: E402

VOICE_ROOT = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

CATALOG = """
en_US-amy-low       en_US-amy-medium     en_US-amy-high
en_US-ryan-low      en_US-ryan-medium    en_US-ryan-high
en_US-lessac-medium en_US-lessac-high
en_US-kusal-low     en_US-kusal-medium   en_US-kusal-high
en_GB-alba-medium   en_GB-alba-high
en_GB-jenny_dioco-low en_GB-jenny_dioco-medium
en_GB-northern_english_male-medium
"""


def list_voices() -> None:
    print("Available voices (name -> config/settings.json piper_voice):\n")
    print(CATALOG.strip())


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  fetching {url.rsplit('/', 1)[-1]} ...", flush=True)
    with urllib.request.urlopen(url, timeout=300) as resp, tmp.open("wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        while chunk := resp.read(1 << 16):
            fh.write(chunk)
            got += len(chunk)
            if total:
                pct = 100 * got / total
                print(f"\r  {got / 1e6:6.1f} / {total / 1e6:.1f} MB ({pct:5.1f}%)",
                      end="", flush=True)
    print()
    tmp.replace(dest)


def fetch(name: str) -> int:
    # Voices are stored as en/<locale>/<voice>/<quality>/<name>.onnx,
    # e.g. en/en_US/amy/medium/en_US-amy-medium.onnx
    parts = name.split("-")
    if len(parts) != 3:
        print(f"[FAIL] '{name}' is not in locale-voice-quality form "
              "(for example en_US-amy-medium). See --list.")
        return 1
    locale, voice, quality = parts
    base = f"en/{locale}/{voice}/{quality}"

    model_name = f"{name}.onnx"
    dest_dir = data_path("piper")
    model_path = dest_dir / model_name
    config_path = dest_dir / f"{name}.onnx.json"

    if model_path.exists() and config_path.exists():
        print(f"already present: {model_path}")
    else:
        _download(f"{VOICE_ROOT}/{base}/{model_name}", model_path)
        _download(f"{VOICE_ROOT}/{base}/{model_name}.json", config_path)

    with config_path.open(encoding="utf-8") as fh:
        cfg = json.load(fh)
    print(f"\nvoice: {name}")
    print(f"  sample rate : {cfg.get('audio', {}).get('sample_rate')} Hz")
    print(f"  quality     : {cfg.get('audio', {}).get('quality')}")
    print(f"  size        : {model_path.stat().st_size / 1e6:.1f} MB")
    print("\nNow set this in config/settings.json:")
    print('  "piper_binary": "C:\\\\Users\\\\FRED\\\\AppData\\\\Roaming\\\\Python\\\\Python314\\\\Scripts\\\\piper.exe"')
    print(f'  "piper_voice": "{model_path}"')
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--list" in sys.argv:
        list_voices()
        return 0
    name = args[0] if args else "en_US-amy-medium"
    try:
        return fetch(name)
    except KeyboardInterrupt:
        print("\ncancelled")
        return 1
    except Exception as exc:
        print(f"[FAIL] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())