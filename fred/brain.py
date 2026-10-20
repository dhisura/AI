"""FRED's brain: local chat against Ollama.

Standard library only (urllib) -- no HTTP client dependency for Phase 1.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Iterator

from .config import lessons_path, lessons_text, load_settings
from .memory import confirmed_lessons

SYSTEM_PROMPT = """You are FRED, a personal assistant running entirely on the \
user's own Windows laptop. No internet, no cloud, no accounts.

Voice: direct and plain. Short paragraphs or bullets. A little dry humour is \
fine; padding is not. Never open with "Great question" or similar filler.

Honesty rules, which matter more than anything else:
- If you do not know, say so. Do not invent facts, versions, or file paths.
- Never claim to have performed an action. You can only describe actions that \
appear verbatim in a tool result given to you. Without one, you did not do it.
- When a task has been run for you, its tool result is the only thing you may \
report. If the result says it failed, say it failed.

Your hands: a separate task runner handles "open X" and "write Y" requests, \
and hands you the verified result. By the time you speak, the action has \
already happened or already failed -- so never say you are about to do it, \
and never re-describe it as a plan. What you can still not do: anything \
outside Notepad, Calculator and File Explorer, anything destructive, and \
anything on the internet.

Answer in English."""


def _inject_lessons(transcript: str, max_lessons: int = 3) -> str:
    """Select top-N confirmed lessons by keyword overlap with the transcript.

    Simple TF-like scoring: count how many distinct lesson words appear in
    the transcript. Returns formatted lesson block or empty string.
    """
    lessons = confirmed_lessons()
    if not lessons or max_lessons <= 0:
        return ""

    # Stop words to ignore in scoring - keep app names and key action words
    STOP_WORDS = {
        "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
        "being", "have", "has", "had", "do", "does", "did", "will", "would",
        "could", "should", "may", "might", "must", "can", "when", "what",
        "where", "who", "why", "how", "if", "then", "than", "that", "this",
        "these", "those", "it", "its", "as", "so", "not", "no", "yes",
        "via", "always", "never", "exactly", "before", "after", "using", "use", "used",
        "check", "checking", "verify", "verifying", "wait", "waiting", "send",
        "sending", "focus", "focusing", "match", "matches", "matching",
        "title", "titles", "name", "names",
    }
    # Keep app names - these are meaningful for lesson matching
    APP_NAMES = {"notepad", "calculator", "explorer", "file", "folder", "open", "write", "launch", "close", "calculate", "navigate", "list", "save", "type", "enter", "press", "key"}
    STOP_WORDS = STOP_WORDS - APP_NAMES

    import re
    # Helper to extract words, stripping punctuation
    def extract_words(text: str) -> set:
        # Replace punctuation with spaces, then split
        cleaned = re.sub(r"[^\w\s]", " ", text.lower())
        return {w for w in cleaned.split() if w not in STOP_WORDS and len(w) > 2}

    # Normalise transcript words (filter stop words, but keep words > 2 chars)
    trans_words = extract_words(transcript)

    scored: list[tuple[int, str]] = []
    for lesson in lessons:
        # Also filter stop words from lesson
        lesson_words = extract_words(lesson)
        # Count overlap
        overlap = len(trans_words & lesson_words)
        if overlap > 0:
            scored.append((overlap, lesson))

    if not scored:
        return ""

    # Sort by overlap descending, take top N
    scored.sort(reverse=True, key=lambda x: x[0])
    top = [l for _, l in scored[:max_lessons]]

    return "\n\nLessons from your own past failures. Obey them:\n\n" + "\n".join(f"- {l}" for l in top)


class BrainError(RuntimeError):
    """Raised when the model cannot be reached or returns an error."""


# A SEPARATE prompt for planning, not an extension of SYSTEM_PROMPT. If a task
# turns out not to be actionable, the planner must be able to say "chat" and
# fall through to the normal conversational prompt -- which currently tells the
# model it cannot open apps. That line is only honest while nothing is wired
# up, so it gets updated in the same commit that lets the agent act.
PLANNER_PROMPT = """You are FRED's task planner. You do not do the task yourself; \
you choose which app performs it.

Reply with ONE JSON object and nothing else. No prose, no markdown fence.

If the user is asking a question, chatting, or anything you cannot do with the \
apps listed below, reply exactly:
{"intent": "chat"}

Otherwise reply with:
{"intent": "task", "app": "<app>", "action": "<action>", "args": {...}}

Apps you may choose -- and nothing else is allowed:
- "notepad"   action "write"   args {"text": "...", "path": "..."}
- "calculator" action "calculate" args {"expression": "2+2"}
- "explorer"  action "open"    args {"folder": "..."}

Rules:
- "app" MUST be one of those three exact lowercase names.
- "action" MUST be one of the actions listed for that app.
- "args" must contain exactly the keys listed for that action.
- Only plain arithmetic goes to the calculator: digits, spaces, + - * / . ( ).
- Put a default path in "path" when the user does not give one, for example \
"data/notepad/fred.txt".
- Put a sensible folder in "folder" when the user does not give one, for \
example "Documents".
- Never write JSON for anything destructive. There is no delete, move, or \
rename action. If the user asks for one, reply {"intent": "chat"}.
- Never claim the task succeeded. Reporting results is not your job.

Example:
User: "open notepad and write hello world to notes.txt"
{"intent": "task", "app": "notepad", "action": "write", "args": {"text": "hello world", "path": "notes.txt"}}"""


class Brain:
    """A conversation with one Ollama model, held in memory for the session."""

    def __init__(self, model: str | None = None, host: str | None = None) -> None:
        settings = load_settings()
        self.host = (host or settings["ollama_host"]).rstrip("/")
        self.model = model or settings["model"]
        self.temperature = settings["temperature"]
        self.num_ctx = settings["num_ctx"]
        self.keep_last_turns = settings["keep_last_turns"]
        self.timeout = settings["request_timeout_s"]
        self.messages: list[dict] = [{"role": "system", "content": self._system_prompt()}]

    def _system_prompt(self, transcript: str = "") -> str:
        """Build the system prompt with lesson injection.

        Args:
            transcript: Current user transcript for relevance scoring.
        """
        settings = load_settings()
        max_lessons = settings.get("lesson_injection_count", 3)

        lessons = _inject_lessons(transcript, max_lessons)
        if not lessons:
            return SYSTEM_PROMPT
        return SYSTEM_PROMPT + lessons

    def reset(self) -> None:
        """Start a fresh conversation, re-reading lessons.md."""
        self.messages = [{"role": "system", "content": self._system_prompt()}]

    # -- Ollama plumbing -------------------------------------------------

    def available_models(self) -> list[str]:
        with urllib.request.urlopen(f"{self.host}/api/tags", timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", [])]

    def _post(self, payload: dict, stream: bool) -> urllib.request.Request:
        return urllib.request.Request(
            f"{self.host}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

    def _trimmed(self) -> list[dict]:
        """System prompt plus the most recent turns, to bound context growth."""
        if self.keep_last_turns > 0 and len(self.messages) > 1:
            return [self.messages[0]] + self.messages[-self.keep_last_turns:]
        return list(self.messages)

    # -- Talking ---------------------------------------------------------

    def chat(self, user_text: str) -> str:
        """Send a message and return the full reply. No streaming."""
        self.messages.append({"role": "user", "content": user_text})
        payload = {
            "model": self.model,
            "messages": self._trimmed(),
            "stream": False,
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx},
        }
        try:
            with urllib.request.urlopen(self._post(payload, False), timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            self.messages.pop()
            raise self._explain(exc) from exc

        reply = data.get("message", {}).get("content", "")
        self.messages.append({"role": "assistant", "content": reply})
        return reply

    def _complete(self, system: str, user_text: str) -> str:
        """One stateless completion under a different system prompt.

        Used for planning, which must not pollute the conversation history --
        a planning scratchpad leaking into the chat transcript would confuse
        both the model and the user.
        """
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
            "stream": False,
            # Planning wants determinism, not the conversational 0.7.
            "options": {"temperature": 0.1, "num_ctx": self.num_ctx},
        }
        try:
            with urllib.request.urlopen(self._post(payload, False), timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise self._explain(exc) from exc
        return data.get("message", {}).get("content", "")

    def chat_stream(self, user_text: str) -> Iterator[str]:
        """Send a message and yield reply text as it arrives.

        The exchange is only recorded once the stream finishes, so an
        interrupted reply does not pollute the conversation history.
        """
        self.messages.append({"role": "user", "content": user_text})
        payload = {
            "model": self.model,
            "messages": self._trimmed(),
            "stream": True,
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx},
        }
        try:
            resp = urllib.request.urlopen(self._post(payload, True), timeout=self.timeout)
        except Exception as exc:
            self.messages.pop()
            raise self._explain(exc) from exc

        def chunks() -> Iterator[str]:
            parts: list[str] = []
            try:
                with resp:
                    for raw in resp:
                        line = raw.decode("utf-8").strip()
                        if not line:
                            continue
                        event = json.loads(line)
                        piece = event.get("message", {}).get("content", "")
                        if piece:
                            parts.append(piece)
                            yield piece
                        if event.get("done"):
                            break
            except Exception as exc:
                self.messages.pop()
                raise self._explain(exc) from exc
            self.messages.append({"role": "assistant", "content": "".join(parts)})

        return chunks()

    # -- Errors ----------------------------------------------------------

    def _explain(self, exc: Exception) -> BrainError:
        if isinstance(exc, urllib.error.HTTPError):
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                detail = ""
            return BrainError(f"Ollama returned HTTP {exc.code}. {detail}")
        reason = getattr(exc, "reason", exc)
        return BrainError(
            f"Cannot reach Ollama at {self.host} ({reason}).\n"
            "Is Ollama running? Try:  ollama serve"
        )