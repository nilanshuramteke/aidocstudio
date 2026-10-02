import subprocess
import sys
import time
from pathlib import Path

from adstudio.core.config import Config
from adstudio.core.container import build_container

from .test_queue import wait_for


def test_job_survives_process_kill(tmp_path):
    data = tmp_path / "data"
    cfg = Config.load(data, workers=1, heartbeat_s=0.2, stale_after_s=0.5)
    marker = tmp_path / "marker.txt"

    parent = build_container(cfg, start_workers=False)
    jid = parent.queue.enqueue("debug:sleep", {"seconds": 60, "slow_first": True, "marker": str(marker)})

    child = subprocess.Popen([sys.executable, str(Path(__file__).parent / "_crash_worker.py"), str(data)],
                             cwd=Path(__file__).parent.parent)
    try:
        assert wait_for(lambda: parent.queue.get(jid)["status"] == "running", timeout=30), "child never claimed job"
        child.kill()  # hard kill mid-job
        child.wait(10)
    finally:
        if child.poll() is None:
            child.kill()
    assert parent.queue.get(jid)["status"] == "running"  # orphaned, as after a crash

    time.sleep(0.8)  # let the heartbeat go stale
    parent.start()  # startup recovery requeues, then workers finish the job
    try:
        assert wait_for(lambda: parent.queue.get(jid)["status"] == "done", timeout=15)
        assert marker.read_text().splitlines() == [jid]  # ran to completion exactly once
        assert parent.queue.get(jid)["attempts"] == 2
    finally:
        parent.stop()
