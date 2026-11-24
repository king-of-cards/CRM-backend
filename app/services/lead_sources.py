"""Shared parsing of the optional `sources` query parameter (comma-separated lead sources)."""
from fastapi import HTTPException

LEAD_SOURCES = ("spin", "whatsapp", "wedding", "wedding_planner", "vendor_to_koc", "dhruva", "koc_to_dhruva")


def parse_sources(value: str | None, default: tuple[str, ...] | set[str] | None = None,
                  allowed: tuple[str, ...] = LEAD_SOURCES) -> set[str]:
    """None -> `default` (every allowed source when not given). Otherwise split on commas, trim,
    lowercase; 400 when the list is empty or names a source outside `allowed`."""
    if value is None:
        return set(allowed if default is None else default)
    requested = {part.strip().lower() for part in value.split(",") if part.strip()}
    if not requested:
        raise HTTPException(400, f"sources must be a comma-separated list of: {', '.join(allowed)}")
    unknown = requested - set(allowed)
    if unknown:
        raise HTTPException(400, f"Unknown source(s): {', '.join(sorted(unknown))}. Allowed: {', '.join(allowed)}")
    return requested
