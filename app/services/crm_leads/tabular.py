"""Excel (.xlsx, the main format) and CSV: reading uploads, writing templates and exports.

Safety:
  - uploads are capped (MAX_UPLOAD_BYTES, MAX_UPLOAD_ROWS, MAX_COLUMNS); openpyxl parses XML
    through defusedxml and runs in read-only mode, and formulas are read as their saved values;
  - exports are formula-safe: in .xlsx every text value is written as a text cell (never a
    formula), in .csv text starting with = + - @ tab or CR gets a leading apostrophe;
  - phone numbers are always text; dates are DD-MM-YYYY and times are India time (IST).
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

from app.services.crm_leads.fields import FIELDS, SOURCE_LABELS

IST = timezone(timedelta(hours=5, minutes=30))
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_UPLOAD_ROWS = 5_000
MAX_COLUMNS = 200
EXPORT_MAX_ROWS = 50_000
DATE_FORMAT = "DD-MM-YYYY"
DATETIME_FORMAT = "DD-MM-YYYY HH:MM"
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
def _text_cell_value(value: str) -> str:
    return ILLEGAL_CHARACTERS_RE.sub("", value)


def _ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST).replace(tzinfo=None)


def write_xlsx(title: str, headers: list[str], rows: list[list[Any]], text_columns: set[int] = frozenset()) -> bytes:
    """Write-only workbook. text_columns: indexes always written as text (e.g. phone)."""
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
        for i, value in enumerate(row):
            if value is not None and i in text_columns and not isinstance(value, str):
                value = str(value)
            if isinstance(value, datetime):
                cell = WriteOnlyCell(ws, value=_ist(value))
                cell.number_format = DATETIME_FORMAT
            elif isinstance(value, date):
                cell = WriteOnlyCell(ws, value=value)
                cell.number_format = DATE_FORMAT
            elif isinstance(value, bool):
                cell = WriteOnlyCell(ws, value="Yes" if value else "No")
                cell.data_type = "s"
            elif isinstance(value, (int, float)):
                cell = WriteOnlyCell(ws, value=value)
            elif value is None:
                cell = WriteOnlyCell(ws, value=None)
            else:
                cell = WriteOnlyCell(ws, value=_text_cell_value(str(value)))
                cell.data_type = "s"                 # text, never a formula
                cell.number_format = "@"
            cells.append(cell)
        ws.append(cells)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def csv_safe(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, datetime):
        return _ist(value).strftime("%d-%m-%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d-%m-%Y")
    text = str(value)
    if isinstance(value, str) and text.startswith(_CSV_DANGEROUS):
        return "'" + text
    return text


def write_csv(headers: list[str], rows: list[list[Any]], text_columns: set[int] = frozenset()) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([csv_safe(h) for h in headers])
    for row in rows:
        writer.writerow([csv_safe(str(v) if v is not None and i in text_columns else v) for i, v in enumerate(row)])
    return buf.getvalue().encode("utf-8-sig")      # BOM: Excel opens UTF-8 correctly


def write_table(fmt: str, title: str, headers: list[str], rows: list[list[Any]], text_columns: set[int] = frozenset()):
    """-> (bytes, media type, file extension)."""
    if fmt == "csv":
        return write_csv(headers, rows, text_columns), CSV_TYPE, "csv"
    return write_xlsx(title, headers, rows, text_columns), XLSX_TYPE, "xlsx"


def filename(stem: str, ext: str) -> str:
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return re.sub(r"[^a-z0-9_-]+", "-", stem.lower()) + f"-{today}.{ext}"


def content_disposition(name: str) -> str:
    return f'attachment; filename="{name}"'


# ── Template ───────────────────────────────────────────────────────────
def template(source: str, fmt: str, branch_names: list[str]) -> tuple[bytes, str, str]:
    fields = FIELDS[source]
    headers = [f.label for f in fields]
    example = [f.example if f.example is not None else "" for f in fields]
    if fmt == "csv":
        return write_csv(headers, [example], {i for i, f in enumerate(fields) if f.type == "phone"}), CSV_TYPE, "csv"

    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"
    ws.append(headers)
    ws.append([str(v) if fields[i].type == "phone" and v != "" else v for i, v in enumerate(example)])
    ws.freeze_panes = "A2"
    for i, f in enumerate(fields, start=1):
        letter = get_column_letter(i)
        ws.cell(row=1, column=i).font = Font(bold=True, color="C00000" if f.required else None)
        ws.column_dimensions[letter].width = max(14, len(f.label) + 4)
        if f.type in ("phone", "pincode"):
            for r in range(2, 2002):                     # keep leading zeros and + signs
                ws.cell(row=r, column=i).number_format = "@"

    info = wb.create_sheet("Instructions")
    info.append([f"{SOURCE_LABELS[source]}: fill in the Leads sheet, one lead per row. Red headers are required."])
    info.append(["Dates: DD-MM-YYYY. Times: India time. Delete the example row before uploading."])
    info.append([])
    info.append(["Column", "Required", "How to fill it in"])
    for f in fields:
        info.append([f.label, "Yes" if f.required else "", f.help])
    for cell in info[4]:
        cell.font = Font(bold=True)
    info.column_dimensions["A"].width = 32
    info.column_dimensions["C"].width = 80

    branch_col = next((i for i, f in enumerate(fields, start=1) if f.type == "branch"), None)
    if branch_col and branch_names:
        lists = wb.create_sheet("Branches")
        lists.append(["Branch"])
        for name in branch_names:
            lists.append([name])
        letter = get_column_letter(branch_col)
        dv = DataValidation(type="list", formula1=f"=Branches!$A$2:$A${len(branch_names) + 1}", allow_blank=True,
                            showErrorMessage=True, error="Pick a branch from the list (Branches sheet).")
        ws.add_data_validation(dv)
        dv.add(f"{letter}2:{letter}5001")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), XLSX_TYPE, "xlsx"
