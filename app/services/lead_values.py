"""Shared value rules for every lead upload and single add (all sections).

  is_na()     N/A-like values ("", "N/A", "NA", "-", "--", "nil", "none", "null", ".", any case and
              spacing) mean empty: never a value, never an error.
  dup_norm()  a value as the duplicate check compares it: trimmed, spaces collapsed, any case,
              dates as ISO (YYYY-MM-DD), numbers without ".0", ranges with "-", N/A-like = "".

Duplicates are "every template field matches" (phones as E.164, compared by the caller); the
fields compared are each section's own lead fields, without read-only, CRM and arrival columns.
"""
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

NA_VALUES = frozenset({"", "n/a", "na", "-", "--", "nil", "nill", "none", "null", "."})
_SPACES = re.compile(r"\s+")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_DATE_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y")


def is_na(value: Any) -> bool:
    """None, blank, or an N/A-like text (any case, spaces ignored)."""
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    return _SPACES.sub("", value).casefold() in NA_VALUES


def clean(value: Any) -> Any:
    """None for N/A-like values; anything else unchanged."""
    return None if is_na(value) else value


def _number(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    number = float(value)
    return str(int(number)) if number == int(number) else repr(number)


def to_iso_date(value: Any) -> str | None:
    """A date, datetime or date text (ISO, 13-12-2026, 13/12/2026, 13 Dec 2026, …) as YYYY-MM-DD."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = _SPACES.sub(" ", str(value)).strip().replace(",", "")
    m = _ISO.match(text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def dup_norm(value: Any, kind: str = "text") -> str:
    """The comparable form of a value. kind: the field type (date / event_date / int / money /
    amount / range / bool / anything else = text)."""
    if is_na(value):
        return ""
    if isinstance(value, (datetime, date)):
        return to_iso_date(value)
    if isinstance(value, bool) or (kind == "bool" and isinstance(value, (int, float))):
        return "yes" if value else "no"
    if isinstance(value, (int, float, Decimal)):
        return _number(value)
    text = _SPACES.sub(" ", str(value)).strip().casefold()
    if kind in ("date", "event_date"):
        return to_iso_date(text) or text
    if kind in ("int", "money", "amount"):
        digits = re.sub(r"[^\d.]", "", text)
        try:
            return _number(digits) if digits else text
        except ValueError:
            return text
    if kind == "range":
        return re.sub(r"\s*[-–—_]\s*", "-", text.replace("₹", "")).strip()
    if kind == "bool":
        return "yes" if text in ("yes", "y", "true", "1") else "no" if text in ("no", "n", "false", "0") else text
    return text
