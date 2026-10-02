"""Deterministic validators (no LLM). Each takes the normalized value; returns (ok, message)."""
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

_GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_PAN_RE = re.compile(r"^[A-Z]{5}\d{4}[A-Z]$")
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def gstin_check_char(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        v = _ALPHABET.index(ch) * (1 if i % 2 == 0 else 2)
        total += v // 36 + v % 36
    return _ALPHABET[(36 - total % 36) % 36]


def validate_gstin(v: str) -> tuple[bool, str]:
    v = v.strip().upper()
    if not _GSTIN_RE.match(v):
        return False, "GSTIN format is invalid"
    if gstin_check_char(v[:14]) != v[14]:
        return False, "GSTIN checksum fails"
    return True, ""


def validate_pan(v: str) -> tuple[bool, str]:
    return (True, "") if _PAN_RE.match(v.strip().upper()) else (False, "PAN format is invalid")


def validate_date(v: str) -> tuple[bool, str]:
    try:
        d = date.fromisoformat(v)
    except ValueError:
        return False, "Not a valid date"
    if d > date.today() + timedelta(days=366 * 5) or d.year < 1950:
        return False, "Date is implausible"
    return True, ""


def validate_amount(v: str) -> tuple[bool, str]:
    try:
        d = Decimal(v)
    except InvalidOperation:
        return False, "Not a valid amount"
    return (True, "") if d >= 0 else (False, "Amount is negative")


BUILTIN = {"gstin": validate_gstin, "pan": validate_pan, "date": validate_date, "amount": validate_amount}
KIND_DEFAULTS = {"date": ["date"], "amount": ["amount"]}  # validators applied implicitly by field kind


def validators_for(field: dict) -> list[str]:
    names = list(KIND_DEFAULTS.get(field.get("kind", ""), []))
    for n in field.get("validators", []):
        if n not in names:
            names.append(n)
    return names


def validate_field(field: dict, value: str | None) -> list[tuple[str, bool, str]]:
    """[(rule_id, passed, message)] for one field's value."""
    if value in (None, ""):
        return []
    out = []
    for name in validators_for(field):
        fn = BUILTIN.get(name)
        if fn:
            ok, msg = fn(value)
            out.append((f"builtin:{name}", ok, msg))
    return out


def cross_field(doc_type_name: str, values: dict[str, str | None]) -> list[tuple[str, list[str], bool, str]]:
    """[(rule_id, involved_field_keys, passed, message)] for rules that span fields."""
    out = []
    if doc_type_name == "Invoice":
        try:
            sub, tax, tot = (Decimal(values[k]) for k in ("subtotal", "tax", "total") if values.get(k))
            if len([k for k in ("subtotal", "tax", "total") if values.get(k)]) == 3:
                ok = abs(sub + tax - tot) <= max(Decimal("0.02"), tot * Decimal("0.001"))
                out.append(("builtin:total_matches", ["subtotal", "tax", "total"], ok,
                            "" if ok else f"Subtotal + tax ({sub + tax}) does not equal total ({tot})"))
        except (ValueError, InvalidOperation):
            pass
        inv, due = values.get("invoice_date"), values.get("due_date")
        if inv and due:
            try:
                ok = date.fromisoformat(due) >= date.fromisoformat(inv)
                out.append(("builtin:due_after_invoice", ["invoice_date", "due_date"], ok,
                            "" if ok else "Due date is before the invoice date"))
            except ValueError:
                pass
    return out
