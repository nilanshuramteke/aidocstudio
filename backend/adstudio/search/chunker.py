"""Page-bounded chunking (~350 tokens, 50 overlap) with an approximate source box per chunk."""
import math
import re
from dataclasses import dataclass

from ..core.interfaces import OCRWord

TARGET_WORDS = 260  # ~350 tokens
OVERLAP_WORDS = 40  # ~50 tokens
_TOKEN = re.compile(r"\S+")


@dataclass
class Chunk:
    page_no: int
    ord: int
    text: str
    char_start: int
    char_end: int
    bbox: list[float] | None


def _union(words: list[OCRWord]) -> list[float] | None:
    if not words:
        return None
    x0, y0 = min(w.x for w in words), min(w.y for w in words)
    x1, y1 = max(w.x + w.w for w in words), max(w.y + w.h for w in words)
    return [round(x0, 5), round(y0, 5), round(x1 - x0, 5), round(y1 - y0, 5)]


def chunk_page(page_no: int, text: str, words: list[OCRWord] | None, *, start_ord: int = 0,
               target: int = TARGET_WORDS, overlap: int = OVERLAP_WORDS) -> list[Chunk]:
    """Chunks never cross a page boundary. bbox is approximate: OCR words and text tokens are aligned proportionally."""
    toks = [(m.start(), m.end()) for m in _TOKEN.finditer(text)]
    if not toks:
        return []
    step = max(1, target - overlap)
    out: list[Chunk] = []
    i, ord_ = 0, start_ord
    while i < len(toks):
        j = min(i + target, len(toks))
        bbox = None
        if words:
            a = int(i * len(words) / len(toks))
            b = max(a + 1, math.ceil(j * len(words) / len(toks)))
            bbox = _union(words[a:b])
        s, e = toks[i][0], toks[j - 1][1]
        out.append(Chunk(page_no, ord_, text[s:e], s, e, bbox))
        ord_ += 1
        if j == len(toks):
            break
        i += step
    return out
