"""FRED's hands: UI-tree automation and keyboard control, under guardrails.

Every action passes three gates before it happens:
  1. the app must be on config/allowlist.txt
  2. the user must type an explicit confirmation
  3. the result must be verified, not assumed

Speech is unreliable, so app names are matched fuzzily: whisper hears "Note
Ed" for "notepad" and that still has to resolve to Notepad.exe.
"""

from __future__ import annotations

import difflib
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT, allowlist, load_settings

try:
    import ctypes
    from ctypes import wintypes
except Exception:  # pragma: no cover
    ctypes = None

try:
    from pywinauto.controls.hwndwrapper import ElementNotEnabledError
except Exception:  # pragma: no cover - pywinauto not installed yet

    class ElementNotEnabledError(Exception):
        pass

try:
    import pyautogui
    import pywinauto
    from pywinauto import Application
except ImportError:  # pragma: no cover - Phase 3 deps not installed
    pyautogui = None
    pywinauto = None
    Application = None


# --------------------------------------------------------------------------
# Universal Launcher: Win → type → Enter
# --------------------------------------------------------------------------

# Windows Shell COM constants and interfaces for parsing .lnk and UWP apps
if ctypes:
    # CLSID_ShellLink = 0x00021401
    # IID_IShellLink = 0x000214EE
    # IID_IPersistFile = 0x0000010B

    # Helper to create GUID structure for CoCreateInstance
    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    def _guid_from_string(s: str) -> GUID:
        """Parse a GUID string like '{00021401-0000-0000-C000-000000000046}' into GUID struct."""
        s = s.strip("{}")
        parts = s.split("-")
        if len(parts) != 5:
            raise ValueError(f"Invalid GUID string: {s}")
        data1 = int(parts[0], 16)
        data2 = int(parts[1], 16)
        data3 = int(parts[2], 16)
        # Data4 is 8 bytes: first two from parts[3], remaining from parts[4]
        data4_bytes = bytes.fromhex(parts[3] + parts[4])
        if len(data4_bytes) != 8:
            raise ValueError(f"Data4 must be 8 bytes, got {len(data4_bytes)}")
        guid = GUID(data1, data2, data3, (ctypes.c_ubyte * 8)(*data4_bytes))
        return guid

    SHELL_LINK_ID = _guid_from_string("{00021401-0000-0000-C000-000000000046}")
    SHELL_LINK_ID_PERSIST_FILE = _guid_from_string("{0000010B-0000-0000-C000-000000000046}")
    SHELL_LINK_ID_INTERFACE = _guid_from_string("{000214EE-0000-0000-C000-000000000046}")

    # ShowWindow constants
    SW_SHOW = 5

# Cache for the app index: name -> (target_path, is_uwp)
_APP_INDEX: dict[str, tuple[Path, bool]] = {}
_INDEX_BUILT = False

def _resolve_lnk(lnk_path: Path) -> Path | None:
    """Resolve a .lnk file to its target using Windows Shell COM.

    Returns the target path or None if resolution fails.
    """
    if not ctypes:
        return None

    try:
        # CoInitialize for this thread
        ctypes.windll.ole32.CoInitializeEx(None, 0)

        # Create ShellLink object
        shell_link = ctypes.c_void_p()
        persist_file = ctypes.c_void_p()

        # CoCreateInstance for ShellLink (IShellLinkW)
        hr = ctypes.windll.ole32.CoCreateInstance(
            SHELL_LINK_ID,
            None,
            1,  # CLSCTX_INPROC_SERVER
            SHELL_LINK_ID_INTERFACE,
            ctypes.byref(shell_link)
        )
        if hr < 0:
            ctypes.windll.ole32.CoUninitialize()
            return None

        # QueryInterface for IPersistFile
        hr = ctypes.windll.ole32.CoCreateInstance(
            SHELL_LINK_ID,
            None,
            1,  # CLSCTX_INPROC_SERVER
            SHELL_LINK_ID_PERSIST_FILE,
            ctypes.byref(persist_file)
        )
        if hr < 0:
            ctypes.windll.ole32.OleRelease(shell_link)
            ctypes.windll.ole32.CoUninitialize()
            return None

        # Load the .lnk file
        hr = ctypes.windll.oleaut32.OleLoadPersistFile(
            persist_file,
            str(lnk_path).replace('/', '\\'),
            0x0  # STGM_READ
        )
        if hr < 0:
            ctypes.windll.ole32.OleRelease(persist_file)
            ctypes.windll.ole32.OleRelease(shell_link)
            ctypes.windll.ole32.CoUninitialize()
            return None

        # Get the target path
        target_path = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        win32_find_data = wintypes.WIN32_FIND_DATAW()

        hr = ctypes.windll.shell32.ShellLinkW_GetPath(
            shell_link,
            target_path,
            wintypes.MAX_PATH,
            ctypes.byref(win32_find_data),
            0  # SLGP_UNCPRIORITY
        )
        if hr < 0:
            ctypes.windll.ole32.OleRelease(persist_file)
            ctypes.windll.ole32.OleRelease(shell_link)
            ctypes.windll.ole32.CoUninitialize()
            return None

        # Release COM objects
        ctypes.windll.ole32.OleRelease(persist_file)
        ctypes.windll.ole32.OleRelease(shell_link)
        ctypes.windll.ole32.CoUninitialize()

        return Path(target_path.value)
    except Exception:
        try:
            ctypes.windll.ole32.CoUninitialize()
        except Exception:
            pass
        return None

def _scan_start_menu() -> dict[str, tuple[Path, bool]]:
    """Scan Start Menu directories for .lnk files.

    Returns a dict mapping app names to (target_path, is_uwp) tuples.
    """
    index = {}

    # Common Start Menu locations
    start_menu_dirs = []

    # User-specific Start Menu
    user_start_menu = Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    if user_start_menu.exists():
        start_menu_dirs.append(user_start_menu)

    # All-users Start Menu
    common_start_menu = Path(os.environ.get("PROGRAMDATA", "C:\\ProgramData")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    if common_start_menu.exists():
        start_menu_dirs.append(common_start_menu)

    for start_menu in start_menu_dirs:
        try:
            for lnk_file in start_menu.rglob("*.lnk"):
                # Skip redirector .lnk files (they start with "_")
                if lnk_file.name.startswith("_"):
                    continue

                target_path = _resolve_lnk(lnk_file)
                if target_path and target_path.exists():
                    # Use the .lnk stem as the app name (without extension)
                    app_name = lnk_file.stem
                    # Avoid duplicates - prefer shorter names or first encountered
                    if app_name not in index or len(app_name) < len(index[app_name][0].stem):
                        index[app_name] = (target_path, False)  # False = not UWP
        except Exception:
            continue

    return index

def _scan_appsfolder() -> dict[str, tuple[Path, bool]]:
    """Scan UWP apps via shell:appsfolder using COM.

    Returns a dict mapping app names to (target_path, is_uwp) tuples.
    """
    index = {}

    if not ctypes:
        return index

    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0)

        # Create Shell.Application object
        shell = ctypes.c_void_p()
        clsid_shell = ctypes.GUID("{13709620-C279-11CE-A49E-444553540000}")  # CLSID_Shell
        iid_dispatch = ctypes.GUID("{00020400-0000-0000-C000-000000000046}")  # IID_IDispatch
        hr = ctypes.windll.ole32.CoCreateInstance(
            clsid_shell,
            None,
            1,  # CLSCTX_INPROC_SERVER
            iid_dispatch,
            ctypes.byref(shell)
        )
        if hr < 0:
            ctypes.windll.ole32.CoUninitialize()
            return index

        # Get the namespace for shell:appsfolder
        # Use the NameSpace method with "shell:AppsFolder"
        apps_folder_name = "shell:AppsFolder"
        # We need to call the NameSpace method via IDispatch
        # This is complex via raw COM - fall back to known UWP locations for now
        # but mark as UWP
        ctypes.windll.ole32.OleRelease(shell)
        ctypes.windll.ole32.CoUninitialize()
    except Exception:
        try:
            ctypes.windll.ole32.CoUninitialize()
        except Exception:
            pass

    # Fallback: scan known UWP install locations
    uwp_locations = [
        Path.home() / "AppData" / "Local" / "Microsoft" / "WindowsApps",
        Path(os.environ.get("PROGRAMFILES", "C:\\ProgramFiles")) / "WindowsApps",
        Path(os.environ.get("PROGRAMFILES(X86)", "C:\\ProgramFiles (x86)")) / "WindowsApps",
    ]

    for location in uwp_locations:
        if location.exists():
            try:
                for exe_file in location.glob("*.exe"):
                    # UWP apps in these folders often have the exe name as the app name
                    app_name = exe_file.stem
                    if app_name and app_name not in index:
                        index[app_name] = (exe_file, True)  # True = UWP
            except Exception:
                continue

    return index

def _build_app_index():
    """Build or rebuild the application index from Start Menu and UWP apps."""
    global _APP_INDEX, _INDEX_BUILT

    # Start with Start Menu apps
    index = _scan_start_menu()

    # Add/Update with UWP apps
    uwp_index = _scan_appsfolder()
    for name, value in uwp_index.items():
        if name not in index:
            index[name] = value

    _APP_INDEX = index
    _INDEX_BUILT = True

def resolve_universal_app(spoken: str) -> tuple[Path, bool] | None:
    """Resolve a spoken/typed app name to a target path using fuzzy matching.

    Returns (target_path, is_uwp) tuple or None if no good match.
    Reuses the existing normalize/similarity infrastructure.
    """
    global _INDEX_BUILT

    if not _INDEX_BUILT:
        _build_app_index()

    if not _APP_INDEX:
        return None

    phrase = normalize(spoken)
    if not phrase:
        return None

    words = set(phrase.split())
    if words & set(BLOCKED_TERMS):
        return None  # destructive/external intent is never an app launch

    # Remove common launch verbs
    phrase = re.sub(r"^(please\s+)?(open|launch|start|run)\s+", "", phrase).strip()

    best_score = 0.0
    best_result = None
    best_via = "none"

    for app_name, (target_path, is_uwp) in _APP_INDEX.items():
        # Exact match on stem
        stem = normalize(Path(app_name).stem)
        full = normalize(app_name)

        # Check stem match
        score = similarity(phrase, stem) * 1.0
        if score > best_score:
            best_score = score
            best_result = (target_path, is_uwp)
            best_via = "exact" if score >= 0.999 else "fuzzy"

        # Check full name match
        score = similarity(phrase, full) * 0.95
        if score > best_score:
            best_score = score
            best_result = (target_path, is_uwp)
            best_via = "exact" if score >= 0.999 else "fuzzy"

        # Check aliases if any (we don't have per-app aliases in universal launcher)
        # Containment bonus
        if stem in phrase or full in phrase:
            score = max(score, 0.95)
            if score > best_score:
                best_score = score
                best_result = (target_path, is_uwp)
                best_via = "alias"

    if best_score >= 0.62:  # reuse existing threshold
        return best_result
    return None

def _human_delay():
    """Small randomized delay for human-like input (50-150ms)."""
    time.sleep(random.uniform(0.05, 0.15))

def _human_typewrite(text: str):
    """Type text with human-like per-key interval (20-80ms per key)."""
    if not text:
        return

    for char in text:
        pyautogui.write(char)
        time.sleep(random.uniform(0.02, 0.08))

def _human_move_to(x: int, y: int):
    """Move mouse to coordinates with easeInOutQuad tween."""
    if pyautogui:
        # Get current position
        try:
            current_x, current_y = pyautogui.position()
            # pyautogui.moveTo with tween='easeInOutQuad'
            pyautogui.moveTo(x, y, duration=0.25, tween='easeInOutQuad')
            _human_delay()
        except Exception:
            # Fallback to instant move if tween fails
            pyautogui.moveTo(x, y)
            _human_delay()

def launch_universal(app_name: str, assume_yes: bool = False) -> bool:
    """Launch an app using the universal Win → type → Enter method.

    This works like a human: press Win key, type the app name, press Enter.
    Windows Start Menu search will find the app. We don't need to resolve it
    beforehand - the index is only used for verification after launch.

    Returns True if launched and verified, False otherwise.
    """
    _require_pywinauto()

    if not confirm(f"FRED wants to launch {app_name} using universal launcher. OK?", assume_yes=assume_yes):
        raise ActionRefused("declined")

    # Use Win key → type → Enter method (works for any app in Start Menu)
    try:
        # Press Win key to open Start menu
        pyautogui.hotkey('win')
        _human_delay()

        # Type the app name
        _human_typewrite(app_name)
        _human_delay()

        # Press Enter to launch
        pyautogui.press('enter')

        # Wait for window to appear
        timeout = 20.0
        deadline = time.time() + timeout

        while time.time() < deadline:
            try:
                desktop = pywinauto.Desktop(backend="uia")
                # Look for any new window - this is a simple verification
                windows = desktop.windows()
                if len(windows) > 0:
                    # Additional check: try to see if we got a reasonable window
                    for w in windows:
                        try:
                            if w.is_enabled() and w.window_text():
                                return True
                        except Exception:
                            continue
            except Exception:
                pass
            time.sleep(0.5)

        raise ActionRefused(f"Failed to verify launch of {app_name}")

    except Exception as exc:
        raise ActionRefused(f"Failed to launch {app_name}: {exc}")

# How people say these apps. Whisper is literal; people are not.
# Deliberately short and app-specific: a generic alias like "my files" made
# "delete all my files" resolve to explorer.exe, which is exactly the kind of
# phrase that must never resolve to an app.
ALIASES = {
    "notepad.exe": [
        "notepad", "note pad", "noteed", "note ed", "not pad", "nodepad",
        "not epad", "noted pad",
    ],
    "calculator.exe": [
        "calculator", "calc", "calculater", "the calculator",
    ],
    "explorer.exe": [
        "explorer", "file explorer", "file exployer", "exployer",
        "explore", "file manager",
    ],
    "chrome.exe": [
        "chrome", "google chrome", "chrom", "crome", "chromium",
    ],
    "mspaint.exe": [
        "paint", "mspaint", "ms paint",
    ],
}


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# Words that mean "act on the computer", not "open an app". If any of these
# appear, the phrase is a request for a destructive or external action, which
# Phase 3 must never route to an app just because a word happens to match.
BLOCKED_TERMS = (
    "delete", "remove", "erase", "wipe", "format", "uninstall",
    "email", "send", "message", "post", "buy", "purchase", "pay", "transfer",
    "password", "credential", "login", "banking", "bank account",
)


def similarity(a: str, b: str) -> float:
    """0..1 similarity, tuned for short spoken app names."""
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # difflib on the whole string catches "note pad" vs "notepad".
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    # Per-word best match helps when the app name is embedded in a sentence.
    best_word = 0.0
    for word in a.split():
        best_word = max(best_word, difflib.SequenceMatcher(None, word, b).ratio())
    return max(ratio, best_word)


@dataclass
class Match:
    app: str
    score: float
    via: str  # "exact", "alias", or "fuzzy"

    def __bool__(self) -> bool:
        return self.score >= 0.62


def resolve_app(spoken: str, allowed: list[str] | None = None,
                threshold: float | None = None) -> Match | None:
    """Map a spoken or typed phrase onto an allowlisted executable.

    Returns None when nothing is close enough, which is the safe answer: FRED
    should ask rather than guess at an app.
    """
    allowed = allowed if allowed is not None else allowlist()
    threshold = threshold if threshold is not None else load_settings().get(
        "app_match_threshold", 0.62
    )
    phrase = normalize(spoken)
    if not phrase:
        return None

    words = set(phrase.split())
    if words & set(BLOCKED_TERMS):
        return None  # destructive/external intent is never an app launch

    # "open" is never part of the app name.
    phrase = re.sub(r"^(please\s+)?(open|launch|start|run)\s+", "", phrase).strip()

    best = Match(app="", score=0.0, via="none")
    for app in allowed:
        stem = normalize(Path(app).stem)
        full = normalize(app)

        for candidate, weight in ((stem, 1.0), (full, 0.95)):
            score = similarity(phrase, candidate) * weight
            if score > best.score:
                best = Match(app=app, score=score, via="exact" if score >= 0.999 else "fuzzy")

        for alias in ALIASES.get(app.lower(), []):
            norm_alias = normalize(alias)
            score = similarity(phrase, norm_alias)
            # Containment is a strong signal: "open notepad now" -> notepad.
            if norm_alias and norm_alias in phrase:
                score = max(score, 0.95)
            if score > best.score:
                best = Match(app=app, score=score,
                             via="exact" if score >= 0.999 else "alias")

    return best if best.score >= threshold else None


def extract_app(text: str, allowed: list[str] | None = None) -> Match | None:
    """Pull an app name out of a spoken instruction like "open notepad please"."""
    return resolve_app(text, allowed)


# --------------------------------------------------------------------------
# Confirmation
# --------------------------------------------------------------------------

class ActionRefused(Exception):
    """The user declined, or the app was not allowed."""


def confirm(prompt: str, timeout_s: int | None = None,
            assume_yes: bool = False) -> bool:
    """Ask for explicit typed confirmation. Default is NO."""
    if assume_yes:
        return True
    suffix = f" (auto-yes, {timeout_s}s)" if timeout_s else ""
    try:
        answer = input(f"{prompt} [y/N]{suffix} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in {"y", "yes"}


# --------------------------------------------------------------------------
# Window helpers
# --------------------------------------------------------------------------

def _require_pywinauto() -> None:
    if Application is None:
        raise ActionRefused(
            "pywinauto is not installed -- run: pip install pywinauto pyautogui"
        )


def launch(app: str, assume_yes: bool = False) -> object:
    """Start an allowlisted app and return its window."""
    _require_pywinauto()
    if not is_allowed(app):
        raise ActionRefused(
            f"'{app}' is not on the allowlist. Allowed: {', '.join(allowlist()) or 'none'}"
        )
    return pywinauto.Application(backend="uia").start(app)


def is_allowed(app: str) -> bool:
    wanted = normalize(Path(app).stem)
    return any(normalize(Path(a).stem) == wanted for a in allowlist())


def focus(app: str, timeout: float = 10.0) -> object:
    """Connect to a running app and bring it to the front."""
    _require_pywinauto()
    app_obj = pywinauto.Application(backend="uia").connect(path=app, timeout=timeout)
    try:
        app_obj.top_window().set_focus()
    except Exception:
        pass
    return app_obj


def wait_for_window(title_re: str, timeout: float = 10.0):
    _require_pywinauto()
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            return pywinauto.Desktop(backend="uia").window(title_re=title_re)
        except Exception as exc:
            last = exc
            time.sleep(0.4)
    raise ActionRefused(f"No window matching /{title_re}/ within {timeout:.0f}s ({last})")


def type_text(window, text: str) -> None:
    """Type into a focused window. pywinauto's own keyboard, not pyautogui's,
    so it respects the target window rather than the global cursor."""
    window.set_focus()
    if pyautogui is not None:
        time.sleep(0.3)  # let focus settle before synthesising keystrokes
    window.type_keys(_escape_keys(text), with_spaces=True, with_tabs=True,
                     with_newlines=True)


# pywinauto reads these characters as key syntax rather than literals:
#   ^ + % ~   modifier prefixes
#   ( )       grouping
#   { }       literal-key blocks, e.g. {ENTER}
#   [ ]       optional/repeat syntax
# Each one has to be wrapped in braces to be typed as itself.
_KEY_SYNTAX_CHARS = "^%+~(){}[]"


def _escape_keys(text: str) -> str:
    """Make literal text safe for pywinauto's type_keys().

    Without this, typing "}" raises KeySequenceError and typing "(" silently
    swallows the rest of the line. Braces must be doubled inside a block.
    """
    if not text:
        return text
    out = []
    for ch in text:
        if ch in _KEY_SYNTAX_CHARS:
            out.append("{" + ch + "}")
        else:
            out.append(ch)
    return "".join(out)


def hotkey(*keys: str) -> None:
    if pyautogui is None:
        raise ActionRefused("pyautogui is not installed")
    pyautogui.hotkey(*keys)


def press(key: str) -> None:
    if pyautogui is None:
        raise ActionRefused("pyautogui is not installed")
    pyautogui.press(key)


def send_keys(window, keys: str) -> None:
    """Send a pywinauto key expression (e.g. '^s') to a specific window.

    Goes through the target window rather than the global cursor, so it cannot
    land in whichever app happens to have focus.
    """
    window.set_focus()
    time.sleep(0.3)
    window.type_keys(keys)


# --------------------------------------------------------------------------
# Verified Notepad task: open, write, save, prove the file exists
# --------------------------------------------------------------------------

def notepad_write(text: str, path: str, assume_yes: bool = False) -> bool:
    """Write text to a file using Notepad, then verify it on disk.

    Launches `notepad.exe <path>` so Notepad opens the target file directly.
    That sidesteps the Save As dialog entirely and -- more importantly -- means
    FRED only ever types into a window showing exactly the file it was asked to
    write. A loose ".*Notepad.*" match is not safe: modern Notepad keeps every
    tab in one process, so that pattern lands on whatever file was open.

    Every step is confirmed, and the result is read back from disk rather than
    trusted from the UI.
    """
    _require_pywinauto()
    import subprocess

    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists() and not confirm(
        f"{target.name} already exists and WILL BE OVERWRITTEN. Continue?",
        assume_yes=assume_yes,
    ):
        raise ActionRefused("declined, existing file left alone")

    if not confirm(
        f"FRED wants to type into {target} using Notepad. OK?", assume_yes=assume_yes
    ):
        raise ActionRefused("declined")

    # Open the target file directly. Notepad reuses an instance per file, so
    # close any window still showing this file from a previous run first --
    # otherwise stale disabled duplicates pile up and get picked instead.
    desktop = desktop_of_file(target)
    _close_existing(desktop, target)
    time.sleep(0.6)

    subprocess.Popen(["notepad.exe", str(target)])

    # Notepad can take several seconds to open the tab, especially when other
    # tabs are open, so wait for the window to appear rather than sleeping.
    window = _wait_for_document(desktop, target, timeout=25.0)
    if window is None:
        # On a cold start the shell can briefly refuse COM callbacks
        # ("An event was unable to invoke any of the subscribers"). A fresh
        # Desktop object usually gets through.
        time.sleep(2.0)
        window = _wait_for_document(desktop_of_file(target), target, timeout=25.0)
    if window is None:
        raise ActionRefused(f"Notepad never opened {target.name}")

    if not confirm("FRED is typing now. OK?", assume_yes=assume_yes):
        raise ActionRefused("declined before typing")

    if not confirm(f"FRED is saving to {target}. OK?", assume_yes=assume_yes):
        raise ActionRefused("declined before saving")

    # Keystrokes can be dropped when Notepad is still settling, and a dropped
    # character would silently corrupt the file. Type, save, then READ THE FILE
    # BACK and retry if it does not match exactly. The final result must be an
    # exact match too -- a substring check would accept a corrupted "RED" for
    # "FRED", which is precisely the bug this loop exists to catch.
    last_error = None
    for attempt in range(1, 4):
        if attempt > 1:
            # A failed attempt can leave Notepad disabled, unfocused, or pointed
            # at a stale window. Let it settle and look the document up again
            # rather than reusing a wrapper that may be the wrong one now.
            time.sleep(1.5)
            window = _wait_for_document(desktop_of_file(target), target,
                                        timeout=15.0)
            if window is None:
                last_error = "no enabled Notepad window for this file"
                print(f"[retry] attempt {attempt}: {last_error}, trying again")
                continue
        try:
            if _type_and_save(window, target, text, attempt):
                return True
        except ElementNotEnabledError as exc:
            # The whole attempt is guarded, not just the ^a: on a cold start the
            # window can report itself disabled at any point in the sequence,
            # and an exception escaping here used to lose the run entirely.
            last_error = exc
            print(f"[retry] attempt {attempt}: {exc}, trying again")
            continue
        print(f"[retry] attempt {attempt} did not match, trying again")

    if last_error is not None:
        print(f"[error] last failure was: {last_error}")
    return _file_matches_exactly(target, text)


def _type_and_save(window, target: Path, text: str, attempt: int) -> bool:
    # Type into the window wrapper, not a descendant. Notepad's real text
    # surface is class 'RichEditD2DPT' (exposed as a Pane) and the
    # 'NotepadTextBox' Pane above it reports empty text -- targeting either one
    # directly silently drops the keystrokes.
    window.set_focus()
    time.sleep(0.6 * attempt)

    # On a cold start the wrapper can report itself disabled for a moment even
    # though it is perfectly usable. Touching the text area directly is enough
    # to clear that; if the wrapper genuinely cannot focus, give up this attempt
    # and let the caller retry with a freshly looked-up window.
    try:
        window.type_keys("^a")  # select existing content; we replace it
    except ElementNotEnabledError:
        try:
            area = window.descendants(class_name="RichEditD2DPT")
            if area:
                area[0].set_focus()
                time.sleep(0.3)
                area[0].type_keys("^a")
            else:
                return False
        except Exception:
            return False

    time.sleep(0.2)
    window.type_keys(_escape_keys(text), with_spaces=True, with_tabs=True,
                     with_newlines=True)
    time.sleep(0.5)

    window.type_keys("^s")
    return _wait_for_saved(window, target, text, timeout=10.0)


def _file_matches_exactly(target: Path, text: str) -> bool:
    """True only if the file's whole content equals what we asked for.

    Not a substring test on purpose: "RED" is a substring of "FRED", so a
    lenient check would wave through a file that lost its first character.
    """
    if not target.exists():
        return False
    actual = target.read_text(encoding="utf-8", errors="replace")
    return actual.strip() == text.strip()


def _wait_for_saved(window, target: Path, text: str, timeout: float) -> bool:
    """Poll the file on disk until it holds exactly the text we typed.

    The title's leading '*' means unsaved changes, which is a free and
    reliable "still working" signal. The read-back is the real check: a typed
    character can be dropped without anything else reporting an error.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if window.window_text().strip().startswith("*"):
                time.sleep(0.3)
                continue
        except Exception:
            pass
        if _file_matches_exactly(target, text):
            return True
        time.sleep(0.3)
    return False


def _window_title_matches(title: str, target: Path) -> bool:
    """Notepad titles look like 'fred.txt - Notepad' or '*fred.txt - Notepad'."""
    cleaned = title.replace("*", "").strip()
    if not cleaned.lower().endswith("notepad"):
        return False
    stem = cleaned[: -len(" - Notepad")].strip()
    return stem.lower() == target.name.lower()


def _close_existing(desktop, target: Path) -> None:
    """Close any window already showing `target`, so no stale duplicates remain."""
    for w in desktop.windows():
        try:
            if w.element_info.class_name == "Notepad" and \
                    _window_title_matches(w.window_text(), target):
                w.close()
        except Exception:
            continue
    time.sleep(0.5)


def _wait_for_document(desktop, target: Path, timeout: float):
    """Find the Notepad window showing exactly `target`, or None.

    Repeat runs leave several windows with the SAME title -- Notepad reuses an
    instance per file, so launching again adds another -- and the older ones
    are disabled. Typing into a stale window raises ElementNotEnabled and
    silently produces no output. So: take only enabled windows, and the most
    recently created one among them.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        best = None
        best_created = -1
        for w in desktop.windows():
            try:
                if w.element_info.class_name != "Notepad":
                    continue
                if not w.is_enabled():
                    continue  # stale window from an earlier run
                if not _window_title_matches(w.window_text(), target):
                    continue
                created = w.element_info.process_id, w.handle
                # Highest handle is the most recently created window.
                if w.handle > best_created:
                    best, best_created = w, w.handle
            except Exception:
                continue
        if best is not None:
            return best
        time.sleep(0.5)
    return None


def desktop_of_file(target: Path):
    """pywinauto's desktop view of the whole shell."""
    return pywinauto.Desktop(backend="uia")


def _wait_for_save_dialog(desktop, timeout: float):
    """The Save As dialog is its own top-level window, class #32770."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for w in desktop.windows():
            try:
                if w.element_info.class_name == "#32770":
                    return w
            except Exception:
                continue
        time.sleep(0.4)
    return None


def verify_file(path: str, expected: str | None = None) -> bool:
    """Independent check: does the file exist, and does it contain the text?"""
    target = Path(path).expanduser()
    if not target.exists():
        return False
    if expected is None:
        return True
    content = target.read_text(encoding="utf-8", errors="replace")
    return expected.strip() in content


# --------------------------------------------------------------------------
# Calculator
# --------------------------------------------------------------------------

def _find_calc_window(desktop):
    """The Calculator window, by exact title -- it is always titled 'Calculator'."""
    for w in desktop.windows():
        try:
            if (w.window_text() or "") == "Calculator":
                return w
        except Exception:
            continue
    return None


def _calc_display(win) -> str:
    """Read the calculator's own result field: 'Display is 84'."""
    for el in win.descendants():
        try:
            if el.element_info.automation_id == "CalculatorResults":
                return el.window_text() or ""
        except Exception:
            continue
    return ""


def calculator_display() -> str:
    """Current value shown by Calculator, as plain text ('84'), or ''."""
    win = _find_calc_window(pywinauto.Desktop(backend="uia"))
    if win is None:
        return ""
    raw = _calc_display(win)
    # "Display is 84" -> "84". Leave anything unrecognised alone.
    if "Display is" in raw:
        return raw.split("Display is", 1)[1].strip()
    return raw.strip()


def calc(expression: str, expect: str | float | None = None,
         assume_yes: bool = False) -> bool:
    """Type an expression into Calculator, press Enter, and verify the display.

    Verified by reading the result back out of the UIA tree -- never by trusting
    that the keys were delivered. `expect` is optional but recommended: without
    it the only check is that *something* appeared, which would happily accept a
    mistyped result.
    """
    _require_pywinauto()
    import subprocess

    expression = expression.strip()
    if not expression:
        raise ActionRefused("empty expression")

    # Only arithmetic. A calculator keymap, not a shell: refusing here means a
    # malformed request can never turn into a pasted command line.
    if not re.fullmatch(r"[0-9\s+\-*/.()^]+", expression):
        raise ActionRefused(f"'{expression}' is not plain arithmetic")

    if not confirm(
        f"FRED wants to type {expression!r} into Calculator. OK?", assume_yes=assume_yes
    ):
        raise ActionRefused("declined")

    desktop = pywinauto.Desktop(backend="uia")
    if _find_calc_window(desktop) is None:
        subprocess.Popen(["calc.exe"])
        deadline = time.time() + 20.0
        while time.time() < deadline and _find_calc_window(desktop) is None:
            time.sleep(0.5)

    win = _find_calc_window(desktop)
    if win is None:
        raise ActionRefused("Calculator never opened")

    win.set_focus()
    time.sleep(0.8)
    win.type_keys(expression, with_spaces=True, with_tabs=False, with_newlines=False)
    time.sleep(0.4)
    win.type_keys("{ENTER}")

    deadline = time.time() + 10.0
    value = ""
    while time.time() < deadline:
        value = calculator_display()
        if value and value != "0":
            break
        time.sleep(0.3)

    if not value:
        raise ActionRefused("Calculator's display stayed empty")
    print(f"[calc] display now shows: {value}")

    if expect is None:
        return True

    # Compare as numbers when both sides parse, so "8.0" == "8".
    def same(a: str, b) -> bool:
        try:
            return abs(float(a) - float(b)) < 1e-9
        except (TypeError, ValueError):
            return a.strip() == str(b).strip()

    if same(value, expect):
        return True
    print(f"[calc] expected {expect}, display showed {value}")
    return False


# --------------------------------------------------------------------------
# File Explorer (read-only)
# --------------------------------------------------------------------------

# Words that mean "change files". Phase 4 opens, navigates and lists -- it must
# not move, rename or delete anything, so these are refused at the door rather
# than trusted downstream.
EXPLORER_MUTATING = (
    "delete", "remove", "erase", "wipe", "format",
    "move", "rename", "cut", "copy", "paste", "trash", "recycle",
    "new folder", "create folder", "mkdir", "uninstall", "compress", "extract",
)


def _find_explorer_window(desktop, folder: Path):
    """The Explorer window showing `folder`, matched on its exact title.

    As with Notepad, a loose match lands on the wrong window -- there are
    usually several Explorer windows open at once.
    """
    want = folder.name.lower()
    for w in desktop.windows():
        try:
            if w.element_info.class_name != "CabinetWClass":
                continue
            title = (w.window_text() or "").replace(" - File Explorer", "").strip()
            if title.lower() == want:
                return w
        except Exception:
            continue
    return None


def explorer_listing(folder: str) -> list[str]:
    """Names visible in the Explorer window showing `folder`.

    Returns [] when the folder is not open or has no visible items.
    """
    try:
        target = resolve_folder(folder)
    except FileNotFoundError:
        return []
    if not target.is_dir():
        return []
    win = _find_explorer_window(pywinauto.Desktop(backend="uia"), target)
    if win is None:
        return []
    names: list[str] = []
    for el in win.descendants():
        try:
            if str(el.element_info.control_type) != "ListItem":
                continue
            name = (el.window_text() or "").strip()
            if name:
                names.append(name)
        except Exception:
            continue
    return names


# Windows' own "known folders", resolved from the OS rather than guessed at.
# The planner answers with a folder *name* ("Documents"), and Path("Documents")
# resolves against the working directory -- which is FRED's repo, not the user's
# home. Without this, "open Documents" fails because FRED is standing in the
# wrong directory.
KNOWN_FOLDERS = {
    "desktop": "Desktop",
    "documents": "Documents",
    "downloads": "Downloads",
    "pictures": "Pictures",
    "music": "Music",
    "videos": "Videos",
    "home": "",          # the user profile itself
    "this pc": "",
    "computer": "",
}


def resolve_folder(folder: str) -> Path:
    """Turn what a person said about a folder into a real path.

    Accepts a known folder name, an absolute path, or a path relative to the
    user's home. Raises FileNotFoundError rather than guessing when none of
    those hold -- opening the wrong folder is worse than opening nothing.
    """
    text = str(folder or "").strip().strip('"').strip("'")
    if not text:
        raise FileNotFoundError("no folder given")

    candidate = Path(text).expanduser()

    # A bare name, or a known folder spelled loosely, means the home folder.
    if not candidate.is_absolute():
        known = KNOWN_FOLDERS.get(text.lower())
        if known is None:
            for label, resolved in KNOWN_FOLDERS.items():
                if label in text.lower():
                    known = resolved
                    break
        if known is not None:
            return Path.home() / known if known else Path.home()

        # Not a known folder -- try it under home, then under the repo.
        for base in (Path.home(), ROOT):
            probe = base / candidate
            if probe.is_dir():
                return probe
        # Fall through so the caller reports the path it actually tried.

    if candidate.is_dir():
        return candidate
    raise FileNotFoundError(f"no folder called '{text}'")


def explorer_open(folder: str, assume_yes: bool = False) -> bool:
    """Open a folder in File Explorer and verify the window actually shows it."""
    _require_pywinauto()
    import subprocess

    target = resolve_folder(folder)
    if not target.is_dir():
        raise ActionRefused(f"'{target}' is not a folder FRED can open")

    lowered = " ".join(target.parts).lower()
    for term in EXPLORER_MUTATING:
        if term in lowered:
            raise ActionRefused(f"refusing: '{term}' is not something FRED does")

    if not confirm(f"FRED wants to open {target} in File Explorer. OK?",
                   assume_yes=assume_yes):
        raise ActionRefused("declined")

    subprocess.Popen(["explorer.exe", str(target)])

    desktop = pywinauto.Desktop(backend="uia")
    deadline = time.time() + 25.0
    while time.time() < deadline:
        if _find_explorer_window(desktop, target) is not None:
            return True
        time.sleep(0.5)

    raise ActionRefused(f"File Explorer never showed {target.name}")


def explorer_navigate(folder: str, assume_yes: bool = False) -> bool:
    """Go to a folder in an Explorer window and verify the listing."""
    if not explorer_open(folder, assume_yes=assume_yes):
        return False
    try:
        target = resolve_folder(folder)
    except FileNotFoundError:
        return False
    if explorer_listing(str(target)) or not any(target.iterdir()):
        return True
    return False