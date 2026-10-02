"""Versioned prompt files + tolerant JSON parsing of model replies."""
import json
import re
from pathlib import Path

_DIR = Path(__file__).parent / "prompts"
MAX_PROMPT_CHARS = 12000


def load(name: str) -> str:
    return (_DIR / f"{name}.txt").read_text(encoding="utf-8")


def clip(text: str, limit: int = MAX_PROMPT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "\n[...truncated]"


def parse_json_object(reply: str) -> dict | None:
    """Parse a model reply that should be a JSON object; tolerate code fences and surrounding prose."""
    reply = reply.strip()
    for candidate in (reply, *re.findall(r"\{.*\}", reply, re.DOTALL)):
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict):
            return obj
    return None
