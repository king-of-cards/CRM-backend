"""The 12 King of Cards stores and the pincode -> area table, as the frontend has them.

Ported from CRM-frontend-updated/src/components/leads/stores.js and pincode.js, so exports
show a spin lead's branch and location exactly like the list screen, and uploads can read
those branch names back, even when crm_branches (the Branches page) is empty. Branches set up
on the Branches page take priority over these built-in stores.
"""
import re
from typing import Any

# By the branch id the spin wheel stores on each lead
STORES: dict[int, dict[str, Any]] = {
    1: {"id": 1, "name": "Chamrajpet", "code": "CHA-001", "city": "Bangalore", "vendor": "RJ"},
    2: {"id": 2, "name": "HSR Layout", "code": "HSR 002", "city": "Bangalore", "vendor": "Phodeo"},
    3: {"id": 3, "name": "Hoodi(whitefield)", "code": "Hoodi-003", "city": "Bangalore", "vendor": "Phodeo"},
    4: {"id": 4, "name": "Jayanagar", "code": "JNG-004", "city": "Bangalore", "vendor": "Phodeo"},
    5: {"id": 5, "name": "Bommasandra", "code": "BMSD-005", "city": "Bangalore", "vendor": "Phodeo"},
    6: {"id": 6, "name": "Sahakar Nagar", "code": "SKN-006", "city": "Bangalore", "vendor": "Phodeo"},
    7: {"id": 7, "name": "Hyderabad - Kondhapur", "code": "HYB-007", "city": "Hyderabad", "vendor": "OHP"},
    8: {"id": 8, "name": "Visakhapatnam", "code": "VIZ-008", "city": "Visakhapatnam", "vendor": "OHP"},
    9: {"id": 9, "name": "Hubballi", "code": "HUBL-009", "city": "Hubballi", "vendor": "OHP"},
    10: {"id": 10, "name": "Mysore", "code": "MYS-010", "city": "Mysore", "vendor": "OHP"},
    11: {"id": 11, "name": "Chitradurga", "code": "CTRD-011", "city": "Chitradurga", "vendor": "OHP"},
    12: {"id": 12, "name": "Banjara Hills Hyderabad", "code": "BANJARA-009", "city": "Hyderabad", "vendor": "OHP"},
}

# Rough location from an Indian pincode (its first 3 digits = the sorting district)
PINCODE_AREAS: dict[int, str] = {
    # Karnataka
    560: "Bangalore", 561: "Bangalore Rural", 562: "Bangalore Rural", 563: "Kolar", 570: "Mysore", 571: "Mysore",
    572: "Tumkur", 573: "Hassan", 574: "Dakshina Kannada", 575: "Mangalore", 576: "Udupi", 577: "Shimoga / Chitradurga",
    580: "Hubballi-Dharwad", 581: "Uttara Kannada / Haveri", 582: "Gadag", 583: "Ballari", 584: "Raichur",
    585: "Kalaburagi / Bidar", 586: "Vijayapura", 587: "Bagalkot", 590: "Belagavi", 591: "Belagavi",
    # Telangana
    500: "Hyderabad", 501: "Ranga Reddy", 502: "Sangareddy / Medak", 503: "Nizamabad", 504: "Adilabad",
    505: "Karimnagar", 506: "Warangal", 507: "Khammam", 508: "Nalgonda", 509: "Mahabubnagar",
    # Andhra Pradesh
    515: "Anantapur", 516: "Kadapa", 517: "Chittoor / Tirupati", 518: "Kurnool", 520: "Vijayawada", 521: "Krishna",
    522: "Guntur", 523: "Prakasam", 524: "Nellore", 530: "Visakhapatnam", 531: "Visakhapatnam", 532: "Srikakulam",
    533: "East Godavari", 534: "West Godavari", 535: "Vizianagaram",
    # Tamil Nadu, Kerala and big cities elsewhere
    600: "Chennai", 601: "Chennai region", 603: "Chengalpattu", 625: "Madurai", 641: "Coimbatore", 620: "Tiruchirappalli",
    682: "Kochi", 695: "Thiruvananthapuram", 673: "Kozhikode", 400: "Mumbai", 411: "Pune", 110: "Delhi",
    700: "Kolkata", 380: "Ahmedabad", 302: "Jaipur", 226: "Lucknow", 201: "Noida / Ghaziabad", 122: "Gurugram",
}


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value if value is not None else "").lower())


def pincode_area(pincode: Any) -> str | None:
    digits = re.sub(r"\D", "", str(pincode if pincode is not None else ""))
    return PINCODE_AREAS.get(int(digits[:3])) if len(digits) == 6 else None


def store_for(branch_id: Any, branches: list[dict] | None = None) -> dict | None:
    """{id, name, code, city} for a branch id (or its name or code): the Branches page first,
    then the 12 built-in stores. None when truly unknown (never a made-up "Branch 7")."""
    if branch_id is None or str(branch_id).strip() == "":
        return None
    key = str(branch_id).strip()
    k = _norm(key)
    branches = branches or []
    found = next((b for b in branches if str(b.get("id")) == key), None) or next(
        (b for b in branches if any(x and _norm(x) == k for x in (b.get("slug"), b.get("name"), b.get("code")))), None
    )
    if found:
        return {"id": found.get("id"), "name": found.get("name"), "code": found.get("code"), "city": found.get("city")}
    built_in = STORES.get(int(key)) if key.isdigit() else None
    built_in = built_in or next((s for s in STORES.values() if k in (_norm(s["name"]), _norm(s["code"]))), None)
    if built_in:
        return {f: built_in[f] for f in ("id", "name", "code", "city")}
    return None


def branch_ids_by_name(branches: list[dict] | None = None) -> dict[str, int]:
    """Normalised branch name -> id, for reading uploads: the built-in stores, overridden by the
    Branches page (its names win for its ids)."""
    out = {_norm(s["name"]): s["id"] for s in STORES.values()}
    for b in branches or []:
        if b.get("name"):
            out[_norm(b["name"])] = b["id"]
    return out


def branch_names(branches: list[dict] | None = None) -> list[str]:
    """Every branch name an upload can use: the Branches page's, plus built-in stores whose id
    isn't set up there. Sorted."""
    branches = branches or []
    known = {str(b.get("id")) for b in branches}
    names = [b["name"] for b in branches if b.get("name")]
    names += [s["name"] for s in STORES.values() if str(s["id"]) not in known]
    return sorted(set(names), key=str.lower)
