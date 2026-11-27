"""The six KPIs shown on every lead screen and the dashboards, computed the same way everywhere.

    total              leads counted
    interested         Update is "interested"
    not_interested     Update is "not_interested"
    quoted_count       leads with a quoted amount, and quoted_total: their sum (rupees)
    converted          Update is "converted", and converted_pct: of total, 1 decimal (null with 0 leads)
    conversion_count   leads with a conversion amount, and conversion_total: their sum (rupees)
    payout_count       leads with a payout (commission), and payout_total: their sum (rupees)

The 4 sources read Update and the amounts from crm_lead_meta (a lead without a row is Pending,
without amounts); Vendor -> KOC from its own columns. Anything with update_status,
quoted_amount and conversion_amount attributes (or None for "nothing set") can be counted.
"""
from collections.abc import Iterable
from typing import Any

from sqlmodel import Session, select

from app.models.lead_meta import LeadMeta

KEYS = ("total", "interested", "not_interested", "quoted_count", "quoted_total",
        "converted", "converted_pct", "conversion_count", "conversion_total", "payout_count", "payout_total")


def compute(records: Iterable[Any], payout: bool = True) -> dict:
    """records: one per lead; None = no CRM details (Pending, no amounts).
    payout=False: without payout_count / payout_total (the Dhruva sections have no payout yet)."""
    total = interested = not_interested = converted = 0
    quoted: list[int] = []
    conversion: list[int] = []
    payouts: list[int] = []
    for r in records:
        total += 1
        if r is None:
            continue
        status = r.update_status
        interested += status == "interested"
        not_interested += status == "not_interested"
        converted += status == "converted"
        if r.quoted_amount is not None:
            quoted.append(r.quoted_amount)
        if r.conversion_amount is not None:
            conversion.append(r.conversion_amount)
        # Payout: only where the record has one (the Dhruva sections don't)
        if getattr(r, "payout_amount", None) is not None:
            payouts.append(r.payout_amount)
    return {
        "total": total,
        "interested": interested,
        "not_interested": not_interested,
        "quoted_count": len(quoted),
        "quoted_total": sum(quoted),
        "converted": converted,
        "converted_pct": round(converted / total * 100, 1) if total else None,
        "conversion_count": len(conversion),
        "conversion_total": sum(conversion),
        **({"payout_count": len(payouts), "payout_total": sum(payouts)} if payout else {}),
    }


def metas_of(db: Session, source: str) -> dict[str, LeadMeta]:
    """Every CRM detail row of a source, by lead key (the table is small: one row per touched lead)."""
    return {m.lead_key: m for m in db.exec(select(LeadMeta).where(LeadMeta.source == source))}


def for_keys(db: Session, source: str, keys: Iterable[Any], vendor_id: int | None = None,
             payout: bool = True) -> dict:
    """The KPIs over these leads of one of the 4 sources: each lead once, with its overall values
    (every vendor copy merged: lead_crm.merge_copies, e.g. converted if any vendor converted, amounts
    summed); vendor_id: that vendor's copy only (a vendor login's numbers)."""
    from app.services import lead_crm

    values = lead_crm.records(db, source, vendor_id)
    return compute((values.get(str(k)) for k in keys), payout=payout)
