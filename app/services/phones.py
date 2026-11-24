"""One E.164 form for every phone number (+919845012345), used to compare leads.

Each table keeps its phone as it always has (the display value); the E.164 form is stored next
to it in `phone_e164` and is what duplicate checks compare, within and across sources.

  normalize(raw)                          India by default: 9845012345, 09845012345, +91 98450 12345
  normalize(raw, international=True, country="UAE")
      International numbers must carry their country code (+971..., 00971... or 971...),
      or the Country column must say which country they are from.
"""
import re
from dataclasses import dataclass
from functools import lru_cache

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat

DEFAULT_REGION = "IN"


@dataclass(frozen=True)
class Phone:
    e164: str | None        # "+919845012345", or None when the number can't be read
    error: str | None = None

    @property
    def digits(self) -> str | None:
        """E.164 without the "+": the display form the sheet tables already use."""
        return self.e164[1:] if self.e164 else None

    @property
    def national_in(self) -> str | None:
        """10-digit form for Indian numbers (as the workflow stores spin phones), else E.164."""
        if self.e164 and self.e164.startswith("+91") and len(self.e164) == 13:
            return self.e164[3:]
        return self.e164


def _norm_name(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


@lru_cache(maxsize=1)
def _country_names() -> dict[str, str]:
    """{"india": "IN", "unitedarabemirates": "AE", "uae": "AE", "in": "IN", ...}"""
    from phonenumbers.geodata.locale import LOCALE_DATA

    names: dict[str, str] = {}
    for region, langs in LOCALE_DATA.items():
        if len(region) != 2:
            continue
        name = langs.get("en", "")
        while name.startswith("*"):          # "*aa" = same as the "aa" entry
            name = langs.get(name[1:], "")
        if name:
            names[_norm_name(name)] = region
        names[region.lower()] = region
    names.update({
        "uae": "AE", "usa": "US", "us": "US", "unitedstatesofamerica": "US", "america": "US",
        "uk": "GB", "england": "GB", "scotland": "GB", "wales": "GB", "britain": "GB", "greatbritain": "GB",
        "ksa": "SA", "russia": "RU", "southkorea": "KR", "korea": "KR", "holland": "NL",
    })
    return names


def region_for_country(country: str | None) -> str | None:
    """'India' / 'IN' / 'UAE' / '+971' / '971' -> region code, or None."""
    text = (country or "").strip()
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    if digits and digits == re.sub(r"[\s+()-]", "", text):
        region = phonenumbers.region_code_for_country_code(int(digits))
        return None if region == "ZZ" else region
    return _country_names().get(_norm_name(text))


@lru_cache(maxsize=65536)               # pure: the same numbers come back on every list / upload
def _parse(text: str, region: str | None, lenient: bool) -> str | None:
    try:
        number = phonenumbers.parse(text, region)
    except NumberParseException:
        return None
    ok = phonenumbers.is_valid_number(number) or (lenient and phonenumbers.is_possible_number(number))
    return phonenumbers.format_number(number, PhoneNumberFormat.E164) if ok else None


def _clean(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, float) and raw.is_integer():   # Excel stores long numbers as floats
        raw = int(raw)
    text = str(raw).strip()
    if text.startswith("00"):
        text = "+" + text[2:]
    return text


def normalize(raw, *, international: bool = False, country: str | None = None, lenient: bool = False) -> Phone:
    """lenient=True also accepts numbers that are merely possible (used for sheet rows, which
    must not be dropped); added and imported leads must be valid numbers."""
    text = _clean(raw)
    if not re.sub(r"\D", "", text):
        return Phone(None, "Phone is required")

    if text.startswith("+"):
        e164 = _parse(text, None, lenient)
        return Phone(e164) if e164 else Phone(None, "Not a valid phone number")

    if international:
        region = region_for_country(country)
        if region:
            e164 = _parse(text, region, lenient) or _parse("+" + re.sub(r"\D", "", text), None, lenient)
            return Phone(e164) if e164 else Phone(None, f"Not a valid phone number for {country}")
        # No usable Country: the digits must start with the country code
        e164 = _parse("+" + re.sub(r"\D", "", text), None, lenient)
        if e164:
            return Phone(e164)
        return Phone(None, "International numbers need the country code (e.g. +971...) or a Country")

    e164 = _parse(text, DEFAULT_REGION, lenient)
    return Phone(e164) if e164 else Phone(None, "Not a valid phone number")


def last10(e164: str) -> str:
    return re.sub(r"\D", "", e164)[-10:]
