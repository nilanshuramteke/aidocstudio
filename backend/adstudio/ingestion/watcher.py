"""Watch folders: import new files from user-chosen folders. Polling reconcile is the source of truth
(works on network drives and after missed events); watchdog only wakes the loop early."""
from __future__ import annotations  # methods named `list` shadow the builtin inside the class body

import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path

from ..core.errors import AppError, NotFound
from ..core.ids import new_id
from ..core.timeutil import now_iso
from ..documents.service import DocumentService
from ..storage.db import Database

log = logging.getLogger("adstudio.watch")
SETTLE_SECONDS = 2.0  # a file modified this recently may still be mid-copy
SKIP_SUFFIXES = (".tmp", ".part", ".crdownload", ".partial", ".swp", ".lnk")
IMPORTED_DIR = "_imported"


def _skip(name: str) -> bool:
    low = name.lower()
    return name.startswith(("~$", ".")) or low.endswith(SKIP_SUFFIXES) or low in ("thumbs.db", "desktop.ini")


class WatchService:
    def __init__(self, db: Database, docs: DocumentService, data_root: Path):
        self.db, self.docs, self.root = db, docs, Path(data_root).resolve()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._observer = None
        self._scan_lock = threading.Lock()
        self.interval_s = 30.0

    # ── CRUD ──────────────────────────────────────────────────
    def _validate_path(self, raw: str) -> Path:
        p = Path(raw)
        if not p.is_absolute():
            raise AppError("Folder path must be absolute", code="invalid_path")
        if not p.is_dir():
            raise AppError("Folder does not exist", code="invalid_path")
        rp = p.resolve()
        for forbidden in (self.root / "files", self.root / "derived", self.root / "db", self.root / "tmp"):
            if rp == forbidden or forbidden in rp.parents:
                raise AppError("That folder is managed by the app and cannot be watched", code="invalid_path")
        return rp

    def create(self, path: str, recursive: bool = True, after_import: str = "leave", enabled: bool = True) -> dict:
        if after_import not in ("leave", "move", "delete"):
            raise AppError("after_import must be leave, move or delete", code="invalid_body")
        rp = self._validate_path(path)
        fid = new_id()
        try:
            with self.db.write() as c:
                c.execute("INSERT INTO watch_folders(id,path,recursive,enabled,after_import) VALUES(?,?,?,?,?)",
                          (fid, str(rp), int(recursive), int(enabled), after_import))
        except Exception as e:  # noqa: BLE001
            if "UNIQUE" in str(e):
                raise AppError("That folder is already watched", code="duplicate", status=409) from e
            raise
        self._rewatch()
        return self.get(fid)

    def get(self, fid: str) -> dict:
        with self.db.read() as c:
            r = c.execute("SELECT * FROM watch_folders WHERE id=?", (fid,)).fetchone()
        if not r:
            raise NotFound("Watch folder not found")
        return {"id": r["id"], "path": r["path"], "recursive": bool(r["recursive"]), "enabled": bool(r["enabled"]),
                "after_import": r["after_import"], "last_scan_at": r["last_scan_at"],
                "last_scan": json.loads(r["last_scan_json"]) if r["last_scan_json"] else None}

    def list(self) -> list[dict]:
        with self.db.read() as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM watch_folders ORDER BY path")]
        return [self.get(i) for i in ids]

    def update(self, fid: str, changes: dict) -> dict:
        cur = self.get(fid)
        after = changes.get("after_import", cur["after_import"])
        if after not in ("leave", "move", "delete"):
            raise AppError("after_import must be leave, move or delete", code="invalid_body")
        with self.db.write() as c:
            c.execute("UPDATE watch_folders SET recursive=?, enabled=?, after_import=? WHERE id=?",
                      (int(changes.get("recursive", cur["recursive"])), int(changes.get("enabled", cur["enabled"])), after, fid))
        self._rewatch()
        return self.get(fid)

    def delete(self, fid: str) -> None:
        with self.db.write() as c:
            if not c.execute("DELETE FROM watch_folders WHERE id=?", (fid,)).rowcount:
                raise NotFound("Watch folder not found")
        self._rewatch()

    # ── scanning ──────────────────────────────────────────────
    def scan(self, fid: str) -> dict:
        with self._scan_lock:
            f = self.get(fid)
            folder = Path(f["path"])
            summary = {"imported": 0, "duplicate": 0, "rejected": 0, "skipped_recent": 0, "errors": [], "files": []}
            if not folder.is_dir():
                summary["errors"].append("folder is missing or unreachable")
            else:
                now = time.time()
                it = folder.rglob("*") if f["recursive"] else folder.glob("*")
                for p in sorted(it):
                    if not p.is_file() or _skip(p.name) or IMPORTED_DIR in p.relative_to(folder).parts[:-1]:
                        continue
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    if now - st.st_mtime < SETTLE_SECONDS:
                        summary["skipped_recent"] += 1
                        continue
                    if self._seen(fid, p, st):
                        continue
                    self._import_file(f, folder, p, st, summary)
            with self.db.write() as c:
                c.execute("UPDATE watch_folders SET last_scan_at=?, last_scan_json=? WHERE id=?",
                          (now_iso(), json.dumps({k: v for k, v in summary.items() if k != "files"}), fid))
            return summary

    def _seen(self, fid: str, p: Path, st: os.stat_result) -> bool:
        with self.db.read() as c:
            r = c.execute("SELECT mtime_ns, size FROM watch_seen WHERE folder_id=? AND path=?", (fid, str(p))).fetchone()
        return bool(r and r["mtime_ns"] == st.st_mtime_ns and r["size"] == st.st_size)

    def _import_file(self, f: dict, folder: Path, p: Path, st: os.stat_result, summary: dict) -> None:
        try:
            with open(p, "rb") as fh:
                res = self.docs.import_stream(p.name, fh, source="watch")
        except OSError as e:  # locked / permission: retry on the next scan (not recorded as seen)
            summary["errors"].append(f"{p.name}: {e}")
            return
        children = [res] + [c for c in res.children]
        statuses = [r.status for r in (res.children or [res])]
        summary["imported"] += statuses.count("created")
        summary["duplicate"] += statuses.count("duplicate")
        summary["rejected"] += statuses.count("rejected")
        summary["files"].append({"name": p.name, "status": res.status})
        with self.db.write() as c:
            c.execute("INSERT INTO watch_seen(folder_id,path,mtime_ns,size,result) VALUES(?,?,?,?,?)"
                      " ON CONFLICT(folder_id,path) DO UPDATE SET mtime_ns=excluded.mtime_ns, size=excluded.size, result=excluded.result",
                      (f["id"], str(p), st.st_mtime_ns, st.st_size, res.status))
        if res.status == "rejected" and not res.children:
            return  # unsupported files stay where they are, untouched
        try:
            if f["after_import"] == "move":
                dest = folder / IMPORTED_DIR / p.relative_to(folder)
                dest.parent.mkdir(parents=True, exist_ok=True)
                n, final = 1, dest
                while final.exists():
                    final = dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
                    n += 1
                shutil.move(str(p), final)
            elif f["after_import"] == "delete":
                p.unlink()
        except OSError as e:
            summary["errors"].append(f"{p.name}: could not {f['after_import']}: {e}")

    def scan_all(self) -> None:
        for f in self.list():
            if f["enabled"]:
                try:
                    self.scan(f["id"])
                except Exception:  # noqa: BLE001
                    log.exception("scan failed for %s", f["path"])

    # ── background loop ───────────────────────────────────────
    def start(self, interval_s: float = 30.0) -> None:
        self.interval_s = interval_s
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="adstudio-watch", daemon=True)
        self._thread.start()
        self._rewatch()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._observer:
            try:
                self._observer.stop()
                self._observer.join(2)
            except Exception:  # noqa: BLE001
                pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.interval_s)
            if self._stop.is_set():
                return
            if self._wake.is_set():
                self._wake.clear()
                self._stop.wait(SETTLE_SECONDS + 0.5)  # let a burst of writes finish
            self.scan_all()

    def _rewatch(self) -> None:
        """(Re)start the watchdog observer so file events wake the loop immediately. Failures are harmless."""
        if self._thread is None:
            return
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
            if self._observer:
                self._observer.stop()
            wake = self._wake

            class _H(FileSystemEventHandler):
                def on_any_event(self, event):
                    wake.set()

            obs = Observer()
            for f in self.list():
                if f["enabled"] and Path(f["path"]).is_dir():
                    obs.schedule(_H(), f["path"], recursive=f["recursive"])
            obs.start()
            self._observer = obs
        except Exception:  # noqa: BLE001
            log.warning("watchdog unavailable; relying on periodic scans")
