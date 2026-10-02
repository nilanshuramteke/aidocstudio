"""Per-field confidence and review status. Thresholds come from settings, never hard-coded at call sites."""
from dataclasses import dataclass

RULE_CONF = 0.90
RULE_LLM_AGREE_CONF = 0.95
LLM_CONF = 0.75
VALIDATION_FAIL_MULT = 0.70
UNGROUNDED_CAP = 0.50


def base_confidence(extractor_conf: float, ocr_conf: float | None, grounded: bool) -> float:
    """min(OCR confidence of the matched words, extractor confidence); capped if not found in the source."""
    c = extractor_conf if ocr_conf is None else min(ocr_conf, extractor_conf)
    return min(c, UNGROUNDED_CAP) if not grounded else c


def final_confidence(base: float, validation_failed: bool) -> float:
    return base * (VALIDATION_FAIL_MULT if validation_failed else 1.0)


@dataclass
class Thresholds:
    auto_accept: float = 0.90
    soft_review: float = 0.70


def decide(conf: float, th: Thresholds, *, missing_required: bool = False, validation_failed: bool = False,
           disagreement: bool = False) -> tuple[str, str | None]:
    """(status, flag_reason). Any flag forces review regardless of confidence."""
    if missing_required:
        return "needs_review", "missing_required"
    if disagreement:
        return "needs_review", "disagreement"
    if validation_failed:
        return "needs_review", "validation_failed"
    if conf < th.auto_accept:
        return "needs_review", "low_confidence"
    return "auto", None
