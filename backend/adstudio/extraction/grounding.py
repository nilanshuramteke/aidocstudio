"""Grounding: locate an extracted value in the source words to get page + bbox + OCR confidence.

A value that cannot be found is a hallucination suspect and is forced to review by the caller.
OCR engines sometimes drop spaces, so all comparisons use a space/punctuation-free lowercase key.
"""
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from ..core.interfaces import OCRWord, PageText

FUZZY_THRESHOLD = 0.85
CONTAINMENT_MIN_RATIO = 0.15  # target must make up this much of a (merged) OCR word to count as found


@dataclass
class Grounding:
    page_no: int
    bbox: list[float] | None  # [x, y, w, h] normalized; None for native text
    ocr_conf: float
    exact: bool


def key(s: str) -> str:
    return re.sub(r"[^0-9a-z]", "", s.lower())


def _union(words: list[OCRWord]) -> list[float]:
    x0 = min(w.x for w in words)
    y0 = min(w.y for w in words)
    x1 = max(w.x + w.w for w in words)
    y1 = max(w.y + w.h for w in words)
    return [round(x0, 5), round(y0, 5), round(x1 - x0, 5), round(y1 - y0, 5)]


def _match_words(target: str, words: list[OCRWord]) -> tuple[list[OCRWord], float] | None:
    keys = [key(w.text) for w in words]
    n_tok = max(1, len(target.split()))
    best: tuple[float, int, int] | None = None
    # pass 1: exact / containment on windows of the right size (+-1 token for OCR splits/merges)
    for size in sorted({max(1, n_tok - 1), n_tok, n_tok + 1, n_tok + 2}):
        for i in range(0, len(words) - size + 1):
            joined = "".join(keys[i:i + size])
            if not joined:
                continue
            if joined == target:
                return words[i:i + size], 1.0
            if target in joined and len(target) / len(joined) >= CONTAINMENT_MIN_RATIO:
                score = 0.95 * len(target) / len(joined) ** 0.5 / len(target) ** 0.5  # prefer tighter windows
                if best is None or score > best[0]:
                    best = (score, i, size)
    if best:
        window = words[best[1]:best[1] + best[2]]
        if len(window) == 1:  # value sits inside one merged OCR word: narrow the box to its character span
            w, joined = window[0], keys[best[1]]
            idx = joined.find(target)
            window = [OCRWord(w.text, w.x + w.w * idx / len(joined), w.y, w.w * len(target) / len(joined), w.h,
                              w.conf, w.line, w.block)]
        return window, 0.9
    # pass 2: fuzzy (OCR character errors)
    fuzzy: tuple[float, int, int] | None = None
    for size in sorted({max(1, n_tok - 1), n_tok, n_tok + 1}):
        for i in range(0, len(words) - size + 1):
            joined = "".join(keys[i:i + size])
            if not joined or abs(len(joined) - len(target)) > max(2, len(target) // 4):
                continue
            sm = SequenceMatcher(None, joined, target)
            if sm.real_quick_ratio() < FUZZY_THRESHOLD or sm.quick_ratio() < FUZZY_THRESHOLD:
                continue
            r = sm.ratio()
            if r >= FUZZY_THRESHOLD and (fuzzy is None or r > fuzzy[0]):
                fuzzy = (r, i, size)
    if fuzzy:
        return words[fuzzy[1]:fuzzy[1] + fuzzy[2]], fuzzy[0]
    return None


def ground(value: str, pages: list[PageText]) -> Grounding | None:
    target = key(value)
    if not target:
        return None
    best: Grounding | None = None
    for p in pages:
        if p.words:
            hit = _match_words(target, p.words)
            if hit:
                words, sim = hit
                conf = (sum(w.conf for w in words) / len(words)) * sim
                g = Grounding(p.page_no, _union(words), round(conf, 4), exact=sim >= 1.0)
                if best is None or g.ocr_conf > best.ocr_conf:
                    best = g
        elif target in key(p.text):  # native text: no geometry, but the value is verifiably present
            return Grounding(p.page_no, None, 1.0, True)
    return best
