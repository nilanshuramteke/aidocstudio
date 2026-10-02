"""Child process for the kill-mid-job test: runs workers until killed."""
import sys
import time
from pathlib import Path

from adstudio.core.config import Config
from adstudio.core.container import build_container

if __name__ == "__main__":
    cfg = Config.load(Path(sys.argv[1]), workers=1, heartbeat_s=0.2, stale_after_s=0.5)
    build_container(cfg, start_workers=True)
    time.sleep(120)
