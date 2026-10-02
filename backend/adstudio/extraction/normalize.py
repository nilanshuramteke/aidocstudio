"""Raw string -> normalized value per field kind. Returns None when it cannot be parsed."""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

_DATE_FORMATS = ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%Y-%m-%d", "%Y/%m/%d",
                 "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%B-%Y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y"]
_ORDINAL = re.compile(r"(?<=\d)(st|nd|rd|th)\b", re.I)
_MERGED_DMY = re.compile(r"^(\d{1,2})([A-Za-z]{3,9})(\d{4})$")  # OCR that dropped spaces: 12March2026


def parse_date(raw: str) -> str | None:
    s = _ORDINAL.sub("", raw.strip().strip(".,")).replace("  ", " ")
    m = _MERGED_DMY.match(s)
    if m:
        s = f"{m.group(1)} {m.group(2)} {m.group(3)}"
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_amount(raw: str) -> str | None:
    s = re.sub(r"(?i)(rs\.?|inr|usd|eur|rupees|[\u20b9$\u20ac\u00a3])", "", raw).strip().replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s and s.rfind(",") > s.rfind("."):  # European 1.234,56
        s = s.replace(".", "").replace(",", ".")
    elif "," in s and "." not in s and re.search(r",\d{2}$", s) and s.count(",") == 1:
        s = s.replace(",", ".")  # 54,30 -> 54.30
    else:
        s = s.replace(",", "")
    try:
        return f"{Decimal(s):.2f}"
    except InvalidOperation:
        return None


def normalize(kind: str, raw: str | None) -> str | None:
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if kind == "date":
        return parse_date(raw)
    if kind == "amount":
        return parse_amount(raw)
    if kind == "id":
        return re.sub(r"\s+", "", raw).upper()
    return re.sub(r"\s+", " ", raw)
