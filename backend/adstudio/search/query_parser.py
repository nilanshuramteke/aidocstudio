"""Deterministic query understanding. Never executes anything: it only produces a filter dict + remaining text.

Parsed filters are shown to the user as removable chips, so every rule here must be explainable in one line.
"""
import re
from calendar import monthrange

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
     "december"], 1)}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})
_MULT = {"k": 1_000, "thousand": 1_000, "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "crore": 10_000_000,
         "cr": 10_000_000, "m": 1_000_000, "million": 1_000_000}
_CUR = r"(?:rs\.?|inr|₹|\$|usd|eur|€)?\s*"
_NUM = r"([\d][\d,]*(?:\.\d+)?)\s*(k|thousand|lakhs?|lac|crore|cr|million|m)?\b"
_ABOVE = re.compile(r"\b(?:above|over|more than|greater than|at least|>=?)\s*" + _CUR + _NUM, re.I)
_BELOW = re.compile(r"\b(?:below|under|less than|at most|<=?)\s*" + _CUR + _NUM, re.I)
_TYPE = re.compile(r"\btype:([\w\- ]+?)(?=\s+\w+:|\s*$|\s+(?:in|from|above|below|over|under|before|after)\b)", re.I)
_BEFORE = re.compile(r"\bbefore:(\d{4}-\d{2}-\d{2})", re.I)
_AFTER = re.compile(r"\bafter:(\d{4}-\d{2}-\d{2})", re.I)
_TAG = re.compile(r"\btag:([\w\-]+)", re.I)
_REVIEW = re.compile(r"\breview:(\w+)", re.I)
_STATE = re.compile(r"\bstate:(\w+)", re.I)
_MONTH_YEAR = re.compile(r"\bin\s+(" + "|".join(_MONTHS) + r")\s+(\d{4})\b", re.I)
_YEAR = re.compile(r"\bin\s+(\d{4})\b", re.I)
_FROM = re.compile(r"\bfrom\s+([A-Za-z0-9][\w&.'\- ]*?)(?=\s+(?:in|above|over|below|under|before|after|type:|state:|review:)\b|\s*$)", re.I)


def _amount(num: str, mult: str | None) -> float:
    return float(num.replace(",", "")) * (_MULT.get((mult or "").lower(), 1))


def parse_query(q: str) -> tuple[dict, str]:
    """Return (filters, remaining free text). Filter keys: type, date_from, date_to, amount_min, amount_max,
    entity, review, state."""
    filters: dict = {}
    rest = q

    def take(rx: re.Pattern, fn) -> None:
        nonlocal rest
        m = rx.search(rest)
        if m:
            fn(m)
            rest = (rest[:m.start()] + " " + rest[m.end():])

    take(_TYPE, lambda m: filters.__setitem__("type", m.group(1).strip().lower()))
    take(_TAG, lambda m: filters.__setitem__("tag", m.group(1)))
    take(_BEFORE, lambda m: filters.__setitem__("date_to", m.group(1)))
    take(_AFTER, lambda m: filters.__setitem__("date_from", m.group(1)))
    take(_REVIEW, lambda m: filters.__setitem__("review", "needs_review" if m.group(1).lower().startswith("need") else m.group(1).lower()))
    take(_STATE, lambda m: filters.__setitem__("state", m.group(1).lower()))

    def month_year(m):
        mo, y = _MONTHS[m.group(1).lower()], int(m.group(2))
        filters["date_from"], filters["date_to"] = f"{y}-{mo:02d}-01", f"{y}-{mo:02d}-{monthrange(y, mo)[1]:02d}"

    take(_MONTH_YEAR, month_year)
    if "date_from" not in filters and "date_to" not in filters:
        take(_YEAR, lambda m: filters.update(date_from=f"{m.group(1)}-01-01", date_to=f"{m.group(1)}-12-31"))
    take(_ABOVE, lambda m: filters.__setitem__("amount_min", _amount(m.group(1), m.group(2))))
    take(_BELOW, lambda m: filters.__setitem__("amount_max", _amount(m.group(1), m.group(2))))
    take(_FROM, lambda m: filters.__setitem__("entity", m.group(1).strip()))
    return filters, re.sub(r"\s+", " ", rest).strip()
