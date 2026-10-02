"""Tiny periodic scheduler for automatic backups (interval from settings; 0 = off)."""
import logging
import threading
from datetime import datetime, timedelta

from ..jobs.queue import JobQueue
from ..storage.settings import SettingsStore
from .backup import list_backups

log = logging.getLogger("adstudio.scheduler")


class BackupScheduler:
    def __init__(self, data_root, settings: SettingsStore, queue: JobQueue, tick_s: float = 300.0):
        self.root, self.settings, self.queue, self.tick_s = data_root, settings, queue, tick_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def due(self, now: datetime | None = None) -> bool:
        hours = float(self.settings.all()["backup.interval_hours"])
        if hours <= 0:
            return False
        autos = [b for b in list_backups(self.root) if b["auto"]]
        if not autos:
            return True
        last = datetime.fromisoformat(autos[0]["modified"])
        return (now or datetime.now()) - last >= timedelta(hours=hours)

    def tick(self) -> bool:
        if self.due() and not self.queue.list("queued") + [j for j in self.queue.list("running") if j["kind"] == "backup"]:
            self.queue.enqueue("backup", {"auto": True}, priority=9)
            return True
        return False

    def start(self) -> None:
        if self._thread:
            return
        def loop():
            while not self._stop.wait(self.tick_s):
                try:
                    self.tick()
                except Exception:  # noqa: BLE001
                    log.exception("backup scheduler tick failed")
        self._thread = threading.Thread(target=loop, name="adstudio-backup-sched", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
