"""Excel (.xlsx, the main format) and CSV: reading uploads, writing templates and exports.

Safety:
  - uploads are capped (MAX_UPLOAD_BYTES, MAX_UPLOAD_ROWS, MAX_COLUMNS); openpyxl parses XML
    through defusedxml and runs in read-only mode, and formulas are read as their saved values;
  - exports are formula-safe: in .xlsx every text value is written as a text cell (never a
    formula), in .csv text starting with = + - @ tab or CR gets a leading apostrophe;
  - values look as on the list screens: phones as text (+919845012345), dates "13 Dec 2026",
    date-times "14 Nov 2026, 10:30 am" (India time), money as rupees with a ₹ format.
"""
import csv
import io
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from app.models.vendor_to_koc import STATUS_LABELS as UPDATE_LABELS
from app.services.crm_leads.fields import (
    FIELDS, READ_ONLY_COLUMNS, SOURCE_LABELS, UPLOAD_CRM_FIELDS, display_date, display_datetime, upload_crm_fields,
)

IST = timezone(timedelta(hours=5, minutes=30))
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_UPLOAD_ROWS = 5_000
MAX_COLUMNS = 200
EXPORT_MAX_ROWS = 50_000
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CSV_TYPE = "text/csv; charset=utf-8"
_CSV_DANGEROUS = ("=", "+", "-", "@", "\t", "\r")


class UploadError(ValueError):
    pass


# ── Reading uploads ────────────────────────────────────────────────────
def read_upload(filename: str, content: bytes, sheet_name: str | None = None) -> tuple[list[str], list[tuple[int, list[Any]]]]:
    """(headers, [(row number as shown in Excel, values)]). Empty rows are skipped.
    sheet_name (Excel only): the sheet to read; default "Leads", else the first sheet."""
    if len(content) > MAX_UPLOAD_BYTES:
        raise UploadError("The file is larger than 5 MB.")
    if not content:
        raise UploadError("The file is empty.")
    name = (filename or "").lower()
    if content[:2] == b"PK" or name.endswith(".xlsx"):
        rows = _xlsx_rows(content, sheet_name)
    elif name.endswith((".csv", ".txt")) or not name:
        rows = _csv_rows(content)
    else:
        raise UploadError("Upload an Excel (.xlsx) or CSV file.")

    header_index = next((i for i, (_, values) in enumerate(rows) if any(_filled(v) for v in values)), None)
    if header_index is None:
        raise UploadError("The file has no header row.")
    headers = [str(v).strip() if v is not None else "" for v in rows[header_index][1]][:MAX_COLUMNS]
    data = []
    for number, values in rows[header_index + 1:]:
        values = list(values[:len(headers)])
        if not any(_filled(v) for v in values):
            continue
        data.append((number, values + [None] * (len(headers) - len(values))))
        if len(data) > MAX_UPLOAD_ROWS:
            raise UploadError(f"The file has more than {MAX_UPLOAD_ROWS:,} rows. Split it into smaller files.")
    return headers, data


def _filled(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def _pick_sheet(wb, sheet_name: str | None):
    """The requested sheet (exact name, else the only case-insensitive match), or by default
    "Leads", else the first sheet. Unknown name: UploadError listing the sheets."""
    wanted = (sheet_name or "").strip()
    if not wanted:
        return wb["Leads"] if "Leads" in wb.sheetnames else wb.worksheets[0]
    if wanted in wb.sheetnames:
        return wb[wanted]
    matches = [n for n in wb.sheetnames if n.strip().casefold() == wanted.casefold()]
    if len(matches) == 1:
        return wb[matches[0]]
    available = ", ".join(f'"{n}"' for n in wb.sheetnames)
    raise UploadError(f'There is no sheet named "{wanted}" in this file. Sheets in the file: {available}.')


def _xlsx_rows(content: bytes, sheet_name: str | None = None) -> list[tuple[int, list[Any]]]:
    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001  (bad zip, not a workbook, encrypted, ...)
        raise UploadError("This is not a readable Excel (.xlsx) file.") from exc
    try:
        ws = _pick_sheet(wb, sheet_name)
        rows = []                                   # only rows with something in them
        for number, values in enumerate(ws.iter_rows(values_only=True, max_col=MAX_COLUMNS), start=1):
            if any(_filled(v) for v in values):
                rows.append((number, list(values)))
                if len(rows) > MAX_UPLOAD_ROWS + 1:    # + the header row
                    raise UploadError(f"The file has more than {MAX_UPLOAD_ROWS:,} rows. Split it into smaller files.")
        return rows
    finally:
        wb.close()


def _csv_rows(content: bytes) -> list[tuple[int, list[Any]]]:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover
        raise UploadError("The CSV file's text encoding can't be read (save it as UTF-8).")
    first_line = text.split("\n", 1)[0]
    delimiter = max((",", ";", "\t"), key=first_line.count)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    return [(number, row[:MAX_COLUMNS]) for number, row in enumerate(reader, start=1)]


# ── Writing ────────────────────────────────────────────────────────────
# Display formats, as the list screens show values
XLSX_DATE_FORMAT = "dd mmm yyyy"                       # 13 Dec 2026
XLSX_DATETIME_FORMAT = "d mmm yyyy, hh:mm am/pm"       # 14 Nov 2026, 10:30 am (India time)
XLSX_INR_FORMAT = '[>=10000000]"₹"##\\,##\\,##\\,##0;[>=100000]"₹"##\\,##\\,##0;"₹"##,##0'   # ₹1,50,000


def _text_cell_value(value: str) -> str:
    return ILLEGAL_CHARACTERS_RE.sub("", value)


def _ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST).replace(tzinfo=None)


def _set_cell(cell, value: Any, kind: str) -> None:
    """One xlsx cell (normal or write-only) with the column's display format."""
    if value is None or value == "":
        cell.value = None
        return
    if kind == "yesno":
        value = "Yes" if value else "No"
    if isinstance(value, datetime):
        cell.value = _ist(value)
        cell.number_format = XLSX_DATETIME_FORMAT
    elif isinstance(value, date):
        cell.value = value
        cell.number_format = XLSX_DATE_FORMAT
    elif isinstance(value, (int, float)) and not isinstance(value, bool) and kind in ("money", "int", "value"):
        cell.value = value
        if kind == "money":
            cell.number_format = XLSX_INR_FORMAT
    else:
        cell.value = _text_cell_value(str(value))
        cell.data_type = "s"                     # text, never a formula
        cell.number_format = "@"


def write_xlsx(title: str, headers: list[str], rows: list[list[Any]], kinds: list[str]) -> bytes:
    wb = Workbook(write_only=True)
    ws = wb.create_sheet(title[:31])
    ws.freeze_panes = "A2"
    bold = Font(bold=True)
    header_cells = []
    for h in headers:
        cell = WriteOnlyCell(ws, value=_text_cell_value(h))
        cell.data_type = "s"
        cell.font = bold
        header_cells.append(cell)
    ws.append(header_cells)
    for row in rows:
        cells = []
        for value, kind in zip(row, kinds):
            cell = WriteOnlyCell(ws)
            _set_cell(cell, value, kind)
            cells.append(cell)
        ws.append(cells)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def csv_text(value: Any, kind: str = "text") -> str:
    """The same text the xlsx cell shows; formula-safe."""
    if value is None or value == "":
        return ""
    if kind == "yesno" or isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, datetime):
        return display_datetime(value)
    if isinstance(value, date):
        return display_date(value)
    text = str(value)
    if isinstance(value, str) and text.startswith(_CSV_DANGEROUS):
        return "'" + text
    return text


def write_csv(headers: list[str], rows: list[list[Any]], kinds: list[str]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([csv_text(h) for h in headers])
    for row in rows:
        writer.writerow([csv_text(v, k) for v, k in zip(row, kinds)])
    return buf.getvalue().encode("utf-8-sig")      # BOM: Excel opens UTF-8 correctly


def write_table(fmt: str, title: str, headers: list[str], rows: list[list[Any]], kinds: list[str]):
    """-> (bytes, media type, file extension)."""
    if fmt == "csv":
        return write_csv(headers, rows, kinds), CSV_TYPE, "csv"
    return write_xlsx(title, headers, rows, kinds), XLSX_TYPE, "xlsx"


def filename(stem: str, ext: str) -> str:
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return re.sub(r"[^a-z0-9_-]+", "-", stem.lower()) + f"-{today}.{ext}"


def content_disposition(name: str) -> str:
    return f'attachment; filename="{name}"'


# ── Template ───────────────────────────────────────────────────────────
# Template columns formatted as Text (besides phones), so Excel keeps what is typed
TEXT_COLUMNS: dict[str, tuple[str, ...]] = {
    "koc_to_dhruva": ("Estimated budget per card", "Quantity required"),
}


def template(source: str, fmt: str, branch_names: list[str], vendor_names: list[str]) -> tuple[bytes, str, str]:
    """The export's columns, with one example row in the screen's formats. Read-only columns
    (Quoted date, Branch code, Estimate…) are kept so files match the export; they are ignored
    on upload. Update, Quoted amount and Conversion amount are read for new leads."""
    from app.services.crm_leads.export import headers as layout_headers
    from app.services.crm_leads.export import kinds as layout_kinds

    headers = layout_headers(source)
    kinds = layout_kinds(source)
    example = [EXAMPLES[source].get(h) for h in headers]
    if fmt == "csv":
        return write_csv(headers, [example], kinds), CSV_TYPE, "csv"

    read_only = set(READ_ONLY_COLUMNS[source])
    defs = {f.label: f for f in (*FIELDS[source], *upload_crm_fields(source))}
    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"
    ws.append(headers)
    for i, (value, kind) in enumerate(zip(example, kinds), start=1):
        _set_cell(ws.cell(row=2, column=i), value, kind)
    ws.freeze_panes = "A2"
    for i, h in enumerate(headers, start=1):
        letter = get_column_letter(i)
        required = defs.get(h) is not None and defs[h].required
        color = "C00000" if required else ("808080" if h in read_only else None)
        ws.cell(row=1, column=i).font = Font(bold=True, color=color)
        ws.column_dimensions[letter].width = max(14, len(h) + 4)
        if kinds[i - 1] == "phone" or h in TEXT_COLUMNS.get(source, ()):
            # Text cells: keep + signs, and ranges such as 1-10 (Excel turns them into dates)
            for r in range(2, 2002):
                ws.cell(row=r, column=i).number_format = "@"

    info = wb.create_sheet("Instructions")
    info.append([f"{SOURCE_LABELS[source]}: one lead per row. Only the red column (phone) is required."])
    info.append(["Dates: 13 Dec 2026 or 13-12-2026. Date-times: 14 Nov 2026, 10:30 am (India time); time optional."])
    info.append(["Grey columns are filled in by the CRM and ignored on upload. Delete the example row before uploading."])
    info.append([])
    info.append(["Column", "Required", "How to fill it in"])
    for h in headers:
        f = defs.get(h)
        if h in read_only:
            info.append([h, "", "Shown in exports; ignored on upload."])
        else:
            info.append([h, "Yes" if f and f.required else "", f.help if f else ""])
    for cell in info[5]:
        cell.font = Font(bold=True)
    info.column_dimensions["A"].width = 24
    info.column_dimensions["C"].width = 100

    lists = wb.create_sheet("Lists")
    choices = {
        "Branch": branch_names if source in ("spin", "koc_to_dhruva") else [],
        # "<vendor> · auto" isn't offered (Automatic covers it) but uploads still accept it
        "Assigned to": ["Automatic", "Unassigned"] + vendor_names,
        "Update": [label for label in UPDATE_LABELS.values()],          # Pending first
    }
    col = 0
    for header, values in choices.items():
        if not values or header not in headers:
            continue
        col += 1
        letter = get_column_letter(col)
        lists.cell(row=1, column=col, value=header).font = Font(bold=True)
        for r, value in enumerate(values, start=2):
            lists.cell(row=r, column=col, value=value)
        target = get_column_letter(headers.index(header) + 1)
        dv = DataValidation(type="list", formula1=f"=Lists!${letter}$2:${letter}${len(values) + 1}", allow_blank=True,
                            showErrorMessage=True, error=f"Pick a value from the list ({header}).")
        ws.add_data_validation(dv)
        dv.add(f"{target}2:{target}5001")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), XLSX_TYPE, "xlsx"


_D = date(2026, 12, 20)
_T = datetime(2026, 11, 14, 5, 0, tzinfo=timezone.utc)        # 14 Nov 2026, 10:30 am IST
EXAMPLES: dict[str, dict[str, Any]] = {
    "spin": {"Lead": "Asha Rao", "Email": "asha@example.com", "Contact": "+919845012345", "Branch": "Jayanagar",
             "Branch code": "JNG-004 · Bangalore", "Location": "Bangalore · 560011", "Occasion": "Wedding", "Event date": _D,
             "Budget": "₹10–20", "Other wedding services": "Photography & Videography", "Quantity required": 300,
             "Assigned to": "Phodeo", "Created": _T},
    "whatsapp": {"Lead": "+971501234567", "Country": "United Arab Emirates", "Event date": _D,
                 "Assigned to": "Automatic", "Submitted": _T},
    "wedding": {"Lead": "Ravi Kumar", "Email": "ravi@example.com", "Contact": "+919845010003", "Occasion": "Wedding",
                "Event date": _D, "City": "Bangalore", "Budget": 150000, "Assigned to": "Phodeo", "Submitted": _T},
    "koc_to_dhruva": {"Name": "Asha Rao", "Phone": "+919845012345", "Email": "asha@example.com",
                      "Occasion": "Wedding", "Event date": _D, "Branch": "Jayanagar", "Branch code": "JNG-004 · Bangalore",
                      "Estimated budget per card": "70-100", "Quantity required": "500", "Received": _T,
                      "Update": "Interested", "Additional comments": "Call after 6 pm", "Quoted amount": 45000,
                      "Follow-up date": date(2026, 11, 25), "Source": "Instagram", "Relation": "Sister"},
    "wedding_planner": {"Lead": "Meera", "Role": "Bride", "Email": "meera@example.com", "Contact": "+919845010004",
                        "Wedding date": date(2027, 12, 20), "Wedding city": "Udaipur", "Guests": 300,
                        "Guests staying": 120, "Budget": 2500000, "Budget range": "20L–30L", "Assigned to": "",
                        "Updated": _T},
}
