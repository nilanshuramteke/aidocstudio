"""Key/value settings with validated keys and defaults. Secrets never live here (use keyring)."""
import json
import os

from ..core.errors import AppError
from ..core.timeutil import now_iso
from .db import Database

DEFAULTS: dict[str, object] = {
    "review.auto_accept": 0.90,
    "review.soft_review": 0.70,
    "ocr.languages": ["eng"],
    "ocr.engine": "auto",
    "ocr.timeout_s": 120.0,
    "review.ocr_retry_below": 0.75,
    "llm.base_url": os.environ.get("ADSTUDIO_OLLAMA_URL", "http://127.0.0.1:11434"),  # containers point this at the Ollama host
    "llm.model": "llama3.1:8b",
    "embedding.model": "",
    "automation.allow_webhooks": False,
    "watch.scan_interval_s": 30.0,
    "backup.interval_hours": 0.0,
    "backup.keep": 5,
    "security.idle_lock_minutes": 0,
    "ui.theme": "system",
    "ui.density": "comfortable",
}


class SettingsStore:
    def __init__(self, db: Database):
        self.db = db

    def all(self) -> dict:
        out = dict(DEFAULTS)
        with self.db.read() as c:
            for r in c.execute("SELECT key, value_json FROM settings"):
                if r["key"] in DEFAULTS:
                    out[r["key"]] = json.loads(r["value_json"])
        return out

    def update(self, changes: dict) -> dict:
        unknown = [k for k in changes if k not in DEFAULTS]
        if unknown:
            raise AppError(f"Unknown setting(s): {', '.join(sorted(unknown))}", code="unknown_setting")
        for k, v in changes.items():
            if type(v) is not type(DEFAULTS[k]) and not (isinstance(v, (int, float)) and isinstance(DEFAULTS[k], float)):
                raise AppError(f"Setting '{k}' must be {type(DEFAULTS[k]).__name__}", code="invalid_setting")
        merged = {**self.all(), **changes}
        for k in ("review.auto_accept", "review.soft_review", "review.ocr_retry_below"):
            if not 0 <= float(merged[k]) <= 1:
                raise AppError(f"{k} must be between 0 and 1", code="invalid_setting")
        if merged["review.soft_review"] > merged["review.auto_accept"]:
            raise AppError("review.soft_review cannot exceed review.auto_accept", code="invalid_setting")
        before = self.all()
        with self.db.write() as c:
            for k, v in changes.items():
                c.execute(
                    "INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)"
                    " ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                    (k, json.dumps(v), now_iso()))
            c.execute("INSERT INTO audit_log(at,action,target_type,before_json,after_json) VALUES(?,?,?,?,?)",
                      (now_iso(), "settings.update", "settings",
                       json.dumps({k: before[k] for k in changes}), json.dumps(changes)))
        return self.all()
