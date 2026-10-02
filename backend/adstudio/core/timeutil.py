from datetime import datetime, timedelta, timezone

_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime(_FMT)


def iso_in(seconds: float) -> str:
    """ISO timestamp `seconds` from now (negative = past). Lexicographically comparable."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(_FMT)
