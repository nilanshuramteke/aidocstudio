"""Citation post-check: every [S#] must exist, and each cited sentence must be supported by the cited text.

Deliberately cheap and deterministic (token overlap, no second LLM): it catches fabricated citation numbers and
sentences whose numbers/names do not appear in the source. It does not prove entailment; evals sample that manually.
"""
import re
from dataclasses import dataclass, field

_CITE = re.compile(r"\[\s*S(\d+(?:\s*[,;]\s*S?\d+)*)\s*\]", re.IGNORECASE)
_SENT = re.compile(r"(?<=[.!?])\s+|\n+")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-/.,]*[A-Za-z0-9]|[A-Za-z0-9]")
_STOP = {"this", "that", "with", "from", "have", "been", "were", "their", "there", "which", "would", "could", "should",
         "about", "into", "also", "than", "then", "them", "they", "will", "shall", "must", "such", "each", "other",
         "document", "documents", "according", "states", "stated", "section", "mentioned", "provided", "specified"}
SUPPORT_RATIO = 0.6
NOT_FOUND_TEXT = "Not found in your documents."


@dataclass
class Checked:
    answer: str
    used: list[int]
    warnings: list[dict] = field(default_factory=list)
    not_found: bool = False

    @property
    def grounded(self) -> bool:
        return bool(self.used) and not any(w["reason"] in ("unsupported", "invalid_citation") for w in self.warnings)


def content_tokens(text: str) -> set[str]:
    out = set()
    for w in _WORD.findall(text.lower()):
        w = w.strip(".,-/")
        if not w:
            continue
        if any(ch.isdigit() for ch in w):
            out.add("#" + w.replace(",", ""))  # numbers/IDs must match exactly (commas ignored)
        elif len(w) >= 4 and w not in _STOP:
            out.add(w[:5])  # prefix match tolerates plurals/inflection (terminate ~ termination)
    return out


def _cited_numbers(sentence: str) -> list[int]:
    nums: list[int] = []
    for m in _CITE.finditer(sentence):
        nums += [int(x) for x in re.findall(r"\d+", m.group(1))]
    return nums


def strip_citations(s: str) -> str:
    return re.sub(r"\s{2,}", " ", _CITE.sub("", s)).strip()


def _sentences(text: str) -> list[str]:
    """Split into sentences, attaching a citation-only fragment ("... notice. [S1]") to the sentence before it."""
    out: list[str] = []
    for part in filter(None, (p.strip() for p in _SENT.split(text))):
        if out and not strip_citations(part):
            out[-1] += " " + part
        else:
            out.append(part)
    return out


def check_answer(answer: str, sources: dict[int, str]) -> Checked:
    """`sources` maps S-number -> chunk text. Returns the cleaned answer, used source numbers, and warnings."""
    text = answer.strip()
    if text.upper().startswith("NOT_FOUND"):
        return Checked(NOT_FOUND_TEXT, [], not_found=True)
    used: list[int] = []
    warnings: list[dict] = []
    kept: list[str] = []
    for raw in _sentences(text):
        nums = _cited_numbers(raw)
        bad = [n for n in nums if n not in sources]
        good = [n for n in nums if n in sources]
        plain = strip_citations(raw)
        toks = content_tokens(plain)
        if bad:
            warnings.append({"sentence": plain, "reason": "invalid_citation", "detail": f"S{bad[0]} does not exist"})
        if good:
            pool = set().union(*(content_tokens(sources[n]) for n in good))
            digits = {t for t in toks if t.startswith("#")}
            words = toks - digits
            missing_digits = digits - pool
            supported_words = len(words & pool) / len(words) if words else 1.0
            if missing_digits or supported_words < SUPPORT_RATIO:
                warnings.append({"sentence": plain, "reason": "unsupported",
                                 "detail": ("numbers not in source: " + ", ".join(sorted(t[1:] for t in missing_digits)))
                                 if missing_digits else "wording not found in the cited passage"})
            for n in good:
                if n not in used:
                    used.append(n)
        elif toks and not bad:
            warnings.append({"sentence": plain, "reason": "uncited", "detail": "no citation"})
        kept.append(_CITE.sub(lambda m: "".join(f"[S{n}]" for n in _cited_numbers(m.group(0)) if n in sources), raw))
    if not used and not warnings:
        warnings.append({"sentence": "", "reason": "uncited", "detail": "the answer has no citations"})
    return Checked("\n".join(kept).strip(), used, warnings)


def best_quote(chunk: str, sentence: str, limit: int = 240) -> str:
    """The sentence of the chunk that overlaps most with the claim: shown on the citation chip."""
    want = content_tokens(sentence)
    best, score = chunk[:limit], -1
    for s in _SENT.split(chunk):
        sc = len(content_tokens(s) & want)
        if s.strip() and sc > score:
            best, score = s.strip(), sc
    return best[:limit]
