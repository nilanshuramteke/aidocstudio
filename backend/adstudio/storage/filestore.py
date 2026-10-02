"""Content-addressed original store (immutable) + regenerable derived/ folder."""
import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import BinaryIO

CHUNK = 1024 * 1024


class FileStore:
    def __init__(self, data_root: Path):
        self.root = Path(data_root)
        self.files = self.root / "files"
        self.derived = self.root / "derived"
        self.tmp = self.root / "tmp"
        self.tmp.mkdir(parents=True, exist_ok=True)

    def rel_path(self, sha: str, ext: str) -> str:
        return f"files/{sha[:2]}/{sha[2:4]}/{sha}.{ext}"

    def abs(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if not p.is_relative_to(self.root.resolve()):
            raise ValueError("path escapes data root")
        return p

    def spool(self, src: BinaryIO, max_bytes: int) -> tuple[Path, str, int]:
        """Stream src into a temp file while hashing. Raises ValueError if over max_bytes."""
        h, size = hashlib.sha256(), 0
        fd, tmp_name = tempfile.mkstemp(dir=self.tmp)
        try:
            with os.fdopen(fd, "wb") as out:
                while chunk := src.read(CHUNK):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError(f"file exceeds {max_bytes} bytes")
                    h.update(chunk)
                    out.write(chunk)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return Path(tmp_name), h.hexdigest(), size

    def commit(self, tmp: Path, sha: str, ext: str) -> str:
        """Move a spooled file into the store (dedupes by content). Returns the relative path."""
        rel = self.rel_path(sha, ext)
        dest = self.abs(rel)
        if dest.exists():
            tmp.unlink(missing_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp), dest)
        return rel

    def derived_dir(self, doc_id: str, *sub: str) -> Path:
        d = self.derived.joinpath(doc_id, *sub)
        d.mkdir(parents=True, exist_ok=True)
        return d
