# CRM leads API: add, import, export, edit, archive, and who sees what

Endpoints for leads that the team adds or imports in the CRM, for all four lead sources, the
per-vendor access rules, and the CRM details endpoint `PUT /leads/{source}/{lead_key}/crm`.

- Base path: `/api/v1`
- Auth: every endpoint needs an access token (`Authorization: Bearer <token>`). Missing or
  invalid token: `401`. Admin-only endpoints answer `403 {"detail": "Admin access required"}` to
  vendor logins. A vendor login that isn't linked to a vendor gets
  `403 {"detail": "This login isn't linked to a vendor."}` on every lead endpoint.
- `{source}` is one of `spin`, `whatsapp`, `wedding`, `wedding_planner`. Any other value: `422`.

## Contents

1. [Concepts](#1-concepts)
2. [Fields per source](#2-fields-per-source)
3. [Endpoints](#3-endpoints)
   - [GET fields](#get-crm-leadssourcefields) · [GET template](#get-crm-leadssourcetemplate) ·
     [POST import/preview](#post-crm-leadssourceimportpreview) · [POST import](#post-crm-leadssourceimport) ·
     [GET export](#get-crm-leadssourceexport) · [POST add](#post-crm-leadssource) ·
     [GET lead](#get-crm-leadssourcelead_key) · [PATCH lead](#patch-crm-leadssourcelead_key) ·
     [POST archive / unarchive](#post-crm-leadssourcelead_keyarchive-and-unarchive)
4. [Duplicates](#4-duplicates)
5. [Access: who sees and edits what](#5-access-who-sees-and-edits-what)
6. [Changes to the existing endpoints](#6-changes-to-the-existing-endpoints)
7. [Limits](#7-limits)
8. [File downloads: filenames and CORS](#8-file-downloads-filenames-and-cors)
9. [Dashboard: date filter](#9-dashboard-date-filter)
10. [Lead list filters](#10-lead-list-filters)

---

## 1. Concepts

**Origin.** Every lead has an `origin`:

| origin | Meaning |
|---|---|
| `sheet` | Copied from the Google Sheet by the sync (whatsapp, wedding, wedding_planner). Read-only here. |
| `workflow` | A spin lead from the workflow database. Read-only here. Appears only in the spin list and export. |
| `added` | Created with `POST /crm-leads/{source}`. |
| `imported` | Created with `POST /crm-leads/{source}/import`. |

Only `added` and `imported` leads can be read, edited or archived through `/crm-leads`. The sheet
sync never changes them.

**Lead key.** Each lead has a `lead_key`. It identifies the lead everywhere, including the CRM
details endpoint (`PUT /leads/{source}/{lead_key}/crm`).

| Source | Added/imported key | Example |
|---|---|---|
| `spin` | `crm-<number>` | `crm-12` |
| `whatsapp`, `wedding`, `wedding_planner` | `crm-<32 hex characters>` | `crm-3f9a0c…` |

Workflow spin leads keep their numeric id (`101`) and sheet leads keep their sheet key, so a
`crm-` key never clashes with them.

**CRM fields are separate.** Assigned vendor, interest, converted, quoted date/amount and notes
are **not** lead fields. They are stored in `crm_lead_meta` and edited only through
`PUT /leads/{source}/{lead_key}/crm` (see [Access](#5-access-who-sees-and-edits-what) for who may
change which field).

**Assignment.** A lead's vendor is, in this order: the vendor an admin picked (manual); the
sheet's "leads shared" column; the vendor of the lead's branch (spin); the vendors' area rules
(pincode, city, country). It is worked out each time a list loads and shown as the "Assigned to"
chip: `Phodeo` (manual, sheet or branch), `Phodeo · auto` (area rules) or `Unassigned`. A lead
uploaded with an empty "Assigned to" gets no automatic assignment (`crm_lead_meta.auto_assign = 0`)
and stays unassigned until an admin picks a vendor.

**Phone numbers.** A phone is required in every source. Each number is converted to one
E.164 form (`+919845012345`) for duplicate checks across all sources:

- `spin`, `wedding`, `wedding_planner`: India by default. `9845012345`, `09845012345` and
  `+91 98450 12345` are the same number. Other countries need their country code
  (`+44…` or `0044…`).
- `whatsapp` (international): the number must carry its country code (`+971…`, `00971…` or
  `971…`), or the `country` field must say which country it is from (a name such as
  `United Arab Emirates` or `UAE`, an ISO code such as `AE`, or a dial code such as `+971`).
- Numbers that aren't valid for their country are rejected.

**Archived.** Archiving hides a lead from lists, counts, statistics and exports (and from
vendor users entirely, so they get `404` for it). Its phone still counts in duplicate checks, and
its CRM details are kept. It can be unarchived.

**Dates and times.**

- **Uploads** (preview and import) accept only the formats the screens and exports use, or
  `DD-MM-YYYY`; the time is optional:
  - dates: `13 Dec 2026`, `13 December 2026`, `13-12-2026`;
  - date-times: `14 Nov 2026, 10:30 am`, `14 Nov 2026 10:30`, `14-11-2026 10:30`;
  - real Excel date/date-time cells.

  Anything else (`20/12/2026`, `2026-12-20`) is a row error:
  `Use a date like 13 Dec 2026 or 13-12-2026`. This applies to Submitted, Created, spin's
  Event date and other date columns.
- **Event dates** (International and wedding Event date, planner Wedding date) behave like the
  sheet sync instead: any text is accepted and kept as written. A full date is also stored as
  the date (`13 Dec 2026`, `13-12-2026`, `20/12/2026`), while partial dates such as `April 2027`,
  `November 16` or `16th Nov` stay text. For the planner, a full date is the first day and anything
  else becomes the wedding month's text. The only error is text longer than the column (100
  characters; planner wedding month 50).
- **JSON requests** (single add and edit) accept the same, plus ISO (`2026-12-20`,
  `2026-11-14T05:00:00Z`).
- A time without a time zone is read as **India time (IST)**. Times are stored in UTC.

---

## 2. Fields per source

Each source has the columns of its list screen (the same columns as the export and template,
see [export](#get-crm-leadssourceexport)), plus optional extra columns. In uploads a column is
recognised by its **label**, key or an alias; case, spaces and punctuation don't matter. In JSON
requests use the **key**. **Only the phone is required**, in every source.
`GET /crm-leads/{source}/fields` returns the same definitions.

Export columns that are shown but never read back are listed under each source as
**read-only**: in a preview they are reported in `read_only_columns` and ignored.

**Field types**

| type | Accepts | Stored as |
|---|---|---|
| `text` | Any text up to `max_length` | Text |
| `email` | A valid email address | Lower case |
| `phone` | See [Phone numbers](#1-concepts) | As the sync stores it, plus the E.164 form |
| `country` | Country name, ISO code or dial code | Text |
| `date` | See dates above | Date |
| `event_date` | Any text, like the sheet sync (see Event dates above) | The text as written, plus the date when it is one |
| `datetime` | See date-times above. Empty means now. | UTC date-time |
| `month` | `December 2027`, `Dec 2027`, `12/2027`, `2027-12` | `December 2027` |
| `int` | Whole number, commas allowed | Number |
| `money` | `2500000`, `25,00,000`, `₹25,00,000`, `25L`, `1.5 lakh` | Rupees |
| `bool` | `Yes`/`No`, `Y`/`N`, `true`/`false`, `1`/`0` | Boolean |
| `range` | `₹10–20`, `10-20`, `10_20` (₹ and dashes are normalised) | Text such as `10-20` |
| `budget` | `150000`, `₹1,50,000`, `50k`, `1.5 lakh` | The text, plus the amount in rupees when readable |
| `pincode` | 6 digits | Digits |
| `branch` | Spin: a branch **name** as on the Branches page (in JSON also its numeric id) | Branch id |
| `assigned` | `Phodeo` (manual), `Automatic` / `Auto` / `Phodeo · auto` (automatic), `Unassigned` or empty | In `crm_lead_meta` (see below) |
| `location` | Spin: `Bangalore · 560011`; the 6-digit pincode in it is saved | The pincode |
| `wedding_when` | Planner: a date (fills the first day), or a month or any other text (fills the wedding month) | First day or wedding month |

**"Assigned to"** (`assigned_to`) is not stored on the lead:

| Value | Uploads | Single add |
|---|---|---|
| A vendor name, e.g. `Phodeo` | Manual assignment to that vendor | Same |
| `Automatic` or `Auto` (any case), or `Phodeo · auto` | Automatic (as if empty in a single add) | Automatic |
| `Unassigned` | Unassigned: no automatic assignment | Same |
| empty | **Unassigned**: no automatic assignment | **Automatic** |
| Unknown or inactive vendor | Row error `Unknown vendor 'X'` | `422` |

### spin

| Key | Label (column) | Type | Max | Upload aliases |
|---|---|---|---|---|
| `name` | Lead | text | 150 | name, full name |
| `email` | Email | email | 150 | email address |
| `phone` | **Contact** (required) | phone | 20 | phone, mobile, phone number, contact number, whatsapp number |
| `branch` | Branch | branch | | store, branch name |
| `location` | Location | location | | |
| `occasionType` | Occasion | text | 100 | occasion type |
| `occasionDate` | Event date | date | | occasion date |
| `estimatedBudgetPerCard` | Budget | range | 100 | budget per card |
| `assigned_to` | Assigned to | assigned | | assigned to, vendor |
| `created_at` | Created | datetime | | created at |

Read-only: Branch code, Interest, Quoted amount, Quoted date, Converted.
Extra columns: Pincode (`pincode`), Lead occasion (`occasion`), Bride/Groom name, Bride/Groom
phone, Event venue, Coverage days, Quantity required, Service, Service types, Service budget,
Service quantity, Other wedding services. Indian numbers are stored in the workflow's 10-digit
form; `occasionDate` as `YYYY-MM-DD`.

### whatsapp (International)

| Key | Label (column) | Type | Max | Upload aliases |
|---|---|---|---|---|
| `phone` | **Lead** (required) | phone | 32 | phone, contact, whatsapp, whatsapp number, mobile, number |
| `country` | Country | country | 100 | |
| `event_date` | Event date | event_date | 100 | wedding date |
| `assigned_to` | Assigned to | assigned | | |
| `submitted_at` | Submitted | datetime | | submitted at, submission time, timestamp |

Read-only: Interest, Quoted amount, Quoted date, Converted.

### wedding (Wedding & event photography)

| Key | Label (column) | Type | Max | Upload aliases |
|---|---|---|---|---|
| `name` | Lead | text | 200 | name, full name |
| `email` | Email | email | 254 | email address |
| `phone` | **Contact** (required) | phone | 50 | phone, contact number, mobile |
| `occasion` | Occasion | text | 100 | what is the occasion |
| `event_date` | Event date | event_date | 100 | |
| `city` | City | text | 200 | city event location, location |
| `budget` | Budget | budget | 100 | photography budget |
| `assigned_to` | Assigned to | assigned | | |
| `submitted_at` | Submitted | datetime | | timestamp, submitted at |

Read-only: Interest, Quoted amount, Quoted date, Converted. `is_repeat` is set automatically
when the phone already exists in this source.

### wedding_planner

| Key | Label (column) | Type | Max | Upload aliases |
|---|---|---|---|---|
| `name` | Lead | text | 200 | name, full name |
| `role` | Role | text | 100 | you are the |
| `email` | Email | email | 254 | email address |
| `phone` | **Contact** (required) | phone | 32 | phone, whatsapp number, contact number |
| `wedding_when` | Wedding date | wedding_when | | |
| `city` | Wedding city | text | 200 | wedding city destination, city |
| `total_guests` | Guests | int | | total guests |
| `guests_staying` | Guests staying | int | | |
| `total_budget` | Budget | money | | total budget |
| `budget_range` | Budget range | text | 100 | |
| `assigned_to` | Assigned to | assigned | | |
| `updated` | Updated | datetime | | last updated |

Read-only: Estimate, Fits budget, Interest, Quoted amount, Quoted date, Converted, Last step.
Extra columns: Wedding month, First day of celebrations, Wedding type, Family home city, Kind of
place, Traditions, Food, Alcohol, Budget includes jewellery and outfits, Jewellery and outfits,
Priority 1–3, Shortlisted vendors, Consent: share plan, Consent: offers on WhatsApp.

**Never taken from uploads or add/edit:** interest, quote, converted, notes (set with
`PUT /leads/…/crm`), "leads shared", and fields only the planner tool or the workflow fill in.
Unknown keys in `values` are ignored.

---

## 3. Endpoints

### Lead object

`get`, `add`, `edit`, `archive` and `unarchive` return a lead in this shape:

```json
{
  "source": "spin",
  "lead_key": "crm-12",
  "origin": "added",
  "archived": false,
  "created_at": "2026-11-14T05:00:00+00:00",
  "updated_at": "2026-11-14T05:00:00+00:00",
  "values": {
    "phone": "9845012345",
    "name": "Asha Rao",
    "branch": 4,
    "pincode": "560011",
    "occasionDate": "2026-12-20"
  }
}
```

`values` holds every field of the source (`null` when empty). In this object, dates are
ISO (`YYYY-MM-DD`), date-times are ISO 8601 in UTC, and `branch` is the branch id.
`phone` comes back as stored:

| Source | `values.phone` |
|---|---|
| `spin` | `9845012345` (Indian numbers) or `+971…` |
| `whatsapp`, `wedding_planner` | `+919845012345` |
| `wedding` | As typed |

### Error format

Validation errors (`422`) from these endpoints use this body:

```json
{
  "detail": {
    "message": "Please fix the highlighted fields.",
    "errors": [
      { "field": "phone", "message": "Phone is required" },
      { "field": "email", "message": "Not a valid email address" }
    ]
  }
}
```

A `422` with FastAPI's standard list-shaped `detail` means the request itself is malformed (for
example an unknown `{source}`, or a body that isn't JSON).

Every other error has `{"detail": "<message>"}`, except `409` duplicates (see
[POST add](#post-crm-leadssource)).

`503 {"detail": "Duplicate check against the workflow leads isn't possible right now. Try again shortly."}`
can come from any endpoint that checks duplicates (preview, import, add, edit) when the
workflow database is configured but can't be reached.

---

### `GET /crm-leads/{source}/fields`

Field definitions for building forms and the import column mapping.

**Response `200`**

```json
{
  "source": "spin",
  "label": "Spin leads",
  "fields": [
    {
      "key": "phone",
      "label": "Contact",
      "type": "phone",
      "required": true,
      "max_length": 20,
      "help": "Indian numbers can be written without +91; other countries need their country code."
    }
  ],
  "branches": [ { "id": 4, "name": "Jayanagar", "city": "Bangalore" } ]
}
```

`branches` is only present for `spin`, sorted by name.

---

### `GET /crm-leads/{source}/template`

Admin only. Downloads an import file with **the same columns as the export**, so an exported
file can be uploaded back.

| Query | Values | Default |
|---|---|---|
| `format` | `xlsx`, `csv` | `xlsx` |

**Response `200`:** a file with `Content-Disposition: attachment; filename="<source>-leads-template-YYYY-MM-DD.<ext>"`.

- **Excel** has these sheets:
  - **Leads:** the export's header row and one example row in the screen's formats. The phone
    column is red (required) and read-only columns are grey.
  - **Instructions:** how to fill in each column; read-only columns are marked "ignored on upload".
  - **Lists:** the values for the dropdowns on the Leads sheet: branch names (spin) and every
    "Assigned to" choice (`Automatic`, `Unassigned`, then each active vendor; uploads
    still accept `<vendor> · auto` from exported files).
- **CSV** (UTF-8 with BOM): the header row and the example row.

Delete the example row before uploading, or it will be imported as a lead.

---

### `POST /crm-leads/{source}/import/preview`

Checks an uploaded file and returns every row with its errors and duplicate status.
**Nothing is saved.**

**Request:** `multipart/form-data` with these fields:

| Field | Required | Meaning |
|---|---|---|
| `file` | ✔ | The `.xlsx` or `.csv` file. |
| `sheet_name` | | Excel only: the sheet to read, up to 31 characters (Excel's limit). If it's missing or empty, the sheet named `Leads` is read, or else the first sheet. An exact name wins; otherwise a single match ignoring case and surrounding spaces is used. Ignored for CSV. |

- **Excel `.xlsx`** (main format; detected by content or name). Formulas are read as their
  last saved value.
- **CSV** (`.csv`, `.txt`, or no file name). UTF-8 (with or without BOM) or Windows-1252.
  The delimiter (comma, semicolon or tab) is detected from the first line.
- The first non-empty row is the header. Empty rows are skipped. Row numbers in the
  response are the row numbers as shown in Excel.
- Columns are matched to fields by label, key or alias. If two columns match the same
  field, the first one wins. Read-only export columns (Interest, quote, converted, …) are
  listed in `read_only_columns`; other columns that match no field in `ignored_columns`.
- Admin only.

**Response `200`**

```json
{
  "source": "spin",
  "columns": { "Phone": "phone", "Name": "name", "Branch": "branch" },
  "ignored_columns": ["Notes from sales"],
  "read_only_columns": ["Interest", "Quoted amount", "Quoted date", "Converted"],
  "rows": [
    {
      "row": 2,
      "status": "ok",
      "values": { "phone": "9845020001", "name": "Asha", "branch": 4, "occasionDate": "20 Dec 2026", "assigned_to": "Unassigned" },
      "errors": [],
      "duplicate": null,
      "warnings": [ { "type": "other_source", "sources": ["wedding"] } ],
      "phone_e164": "+919845020001"
    },
    {
      "row": 3,
      "status": "error",
      "values": { "phone": "9845020002", "email": "not-an-email" },
      "errors": [ { "field": "email", "message": "Not a valid email address" } ],
      "duplicate": null,
      "warnings": []
    },
    {
      "row": 5,
      "status": "duplicate",
      "values": { "phone": "+91 98450 20001" },
      "errors": [],
      "duplicate": { "reason": "in_file", "duplicate_of_row": 2 },
      "warnings": [],
      "phone_e164": "+919845020001"
    }
  ],
  "counts": {
    "total": 8,
    "ok": 2,
    "errors": 3,
    "duplicates": 3,
    "duplicates_by_reason": {
      "in_file": 1,
      "existing_sheet": 0,
      "existing_workflow": 1,
      "existing_added": 1
    },
    "other_source_warnings": 1
  }
}
```

**Row fields**

| Field | Meaning |
|---|---|
| `row` | Row number in the file. |
| `status` | `duplicate` (the phone matches an existing lead or an earlier row of the file; skipped), else `error` (no usable phone, or field errors), else `ok` (will be imported). A duplicate stays a duplicate even if other fields have errors; those errors are still listed, for information. |
| `values` | Every field of the source (abbreviated above). Values are cleaned, in upload formats: dates `20 Dec 2026`, date-times `14 Nov 2026, 10:30 am` (IST), numbers, booleans, branch as id, `assigned_to` as `Phodeo` / `Phodeo · auto` / `Automatic` / `Unassigned`. A field that has an error keeps the text as written. Send these to `POST /import` unchanged, or after the user fixes them. |
| `errors` | `[{field, message}]`, empty when the row is valid. |
| `duplicate` | `null`, or a duplicate object. See [Duplicates](#4-duplicates). |
| `warnings` | Non-blocking. `{"type": "other_source", "sources": [...]}` lists the other sources where the phone exists. |
| `phone_e164` | The normalised phone. Present only for rows without errors. |

**Counts:** `total = ok + errors + duplicates`. `duplicates_by_reason` always contains all
four reasons. `other_source_warnings` counts rows that have an `other_source` warning (rows
with errors are never checked against other sources).

**Errors**

| Status | `detail` |
|---|---|
| 400 | `The file is larger than 5 MB.` |
| 400 | `The file is empty.` |
| 400 | `Upload an Excel (.xlsx) or CSV file.` |
| 400 | `This is not a readable Excel (.xlsx) file.` |
| 400 | `There is no sheet named "April" in this file. Sheets in the file: "Instructions", "Leads", "March batch".` |
| 400 | `The file has no header row.` |
| 400 | `The file has more than 5,000 rows. Split it into smaller files.` |
| 400 | `The file has no Contact (phone) column. Use the template's column names.` (the label of the source's phone column) |
| 400 | `The file has no leads (only a header row).` |
| 422 | `sheet_name` longer than 31 characters (FastAPI validation format) |
| 503 | Workflow duplicate check unavailable (see [Error format](#error-format)). |

---

### `POST /crm-leads/{source}/import`

Saves rows as leads with `origin = imported`. Every row is validated and checked for
duplicates again on the server, whatever the preview said. Rows that pass are saved
together in one transaction. Rows with errors and **all duplicates are skipped**: there is
no override for imports.

**Request (JSON)**

```json
{
  "rows": [
    { "row": 2, "values": { "phone": "9845020001", "name": "Asha", "branch": 4 } },
    { "row": 3, "values": { "phone": "9845020002", "email": "not-an-email" } }
  ]
}
```

- `row`: an integer ≥ 1, echoed back in the results. Use the preview's row number.
- `values`: field key to value, as in [Fields per source](#2-fields-per-source).
- At most 5,000 rows.
- Rows within the request are checked against each other (`in_file`), in the order sent.

**Response `200`:** the same shape as the preview, with these differences:

- Saved rows have `status: "imported"` and a `lead_key`.
- `counts` has two more numbers: `imported` and `skipped` (`skipped = total - imported`).

```json
{
  "source": "spin",
  "rows": [
    {
      "row": 2,
      "status": "imported",
      "lead_key": "crm-13",
      "values": { "phone": "9845020001", "name": "Asha", "branch": 4 },
      "errors": [],
      "duplicate": null,
      "warnings": [],
      "phone_e164": "+919845020001"
    },
    {
      "row": 3,
      "status": "error",
      "values": { "phone": "9845020002", "email": "not-an-email" },
      "errors": [ { "field": "email", "message": "Not a valid email address" } ],
      "duplicate": null,
      "warnings": []
    }
  ],
  "counts": {
    "total": 2,
    "ok": 1,
    "errors": 1,
    "duplicates": 0,
    "duplicates_by_reason": { "in_file": 0, "existing_sheet": 0, "existing_workflow": 0, "existing_added": 0 },
    "other_source_warnings": 0,
    "imported": 1,
    "skipped": 1
  }
}
```

Each saved row's "Assigned to" is stored in `crm_lead_meta`: a vendor name = manual,
`Automatic`, `Auto` or `<vendor> · auto` = automatic, **empty or `Unassigned` = unassigned** (no automatic assignment).

Importing the same file twice is safe: the second time every row is reported as a duplicate.
The same goes for an exported file: its rows show as already existing (`existing_sheet`,
`existing_workflow` or `existing_added`). Admin only.

**Errors**

| Status | Cause |
|---|---|
| 400 | `No rows to import.` (empty `rows`) |
| 422 | More than 5,000 rows, or a malformed body (FastAPI validation format) |
| 503 | Workflow duplicate check unavailable |

---

### `GET /crm-leads/{source}/export`

Downloads **exactly what the requesting user's list shows** for the source: the same leads
(vendor users: only their own), filters, sort, columns, headers, order and display formats,
across all pages. **Archived leads, and sheet rows removed from the sheet, are never exported.**
Admins and vendor users.

There is one route per source, because each list has its own filters. `format` is
`xlsx` (default) or `csv` for all of them.

| Route | Query parameters |
|---|---|
| `GET /crm-leads/spin/export` | The `GET /spin-leads` filters (see [Lead list filters](#10-lead-list-filters)), `sort`, `order` (default `asc`) |
| `GET /crm-leads/whatsapp/export` | The `GET /whatsapp-leads` filters, `sort` (`submitted_at` default, `event_date`, `country`, `phone`, `first_seen_at`), `order` (default `desc`) |
| `GET /crm-leads/wedding/export` | The `GET /wedding-leads` filters, `sort` (`submitted_at` default, `event_date`, `budget_amount`, `name`, `city`), `order` (default `desc`) |
| `GET /crm-leads/wedding_planner/export` | The `GET /wedding-plans` filters, `sort` (`updated` default, `wedding_date`, `total_budget`, `total_guests`, `plan_estimate`, `name`), `order` (default `desc`) |

Each export uses exactly the same filter code as its list, so it always contains the list's leads
in the list's order. The validation errors are the same too.

**Response `200`:** a file with `Content-Disposition: attachment; filename="<source>-leads-YYYY-MM-DD.<ext>"`.

**Columns** (each list screen flattened: Lead and its subtitle, Contact, the screen's columns in
order, the CRM columns, then the trailing column), with an example row:

**spin**

| Lead | Email | Contact | Branch | Branch code | Location | Occasion | Event date | Budget | Assigned to | Interest | Quoted amount | Quoted date | Converted | Created |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Asha Rao | asha@example.com | +919845012345 | Jayanagar | JNG-004 · Bangalore | Bangalore · 560011 | Wedding | 20 Dec 2026 | ₹10–20 | Phodeo | Interested | ₹45,000 | 20 Nov 2026 | No | 14 Nov 2026, 10:30 am |

**whatsapp**

| Lead | Country | Event date | Assigned to | Interest | Quoted amount | Quoted date | Converted | Submitted |
|---|---|---|---|---|---|---|---|---|
| +971501234567 | United Arab Emirates | 20 Dec 2026 | OHP · auto | Others | | | No | 14 Nov 2026, 10:30 am |

**wedding**

| Lead | Email | Contact | Occasion | Event date | City | Budget | Assigned to | Interest | Quoted amount | Quoted date | Converted | Submitted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Ravi Kumar | ravi@example.com | +919845010003 | Wedding | 20 Dec 2026 | Bangalore | ₹1,50,000 | Phodeo · auto | Interested | ₹60,000 | 20 Nov 2026 | Yes | 14 Nov 2026, 10:30 am |

**wedding_planner**

| Lead | Role | Email | Contact | Wedding date | Wedding city | Guests | Guests staying | Budget | Budget range | Estimate | Fits budget | Assigned to | Interest | Quoted amount | Quoted date | Converted | Updated | Last step |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Meera | Bride | meera@example.com | +919845010004 | 20 Dec 2027 | Udaipur | 300 | 120 | ₹25,00,000 | 20L–30L | ₹23,10,000 | Yes | OHP · auto | Interested | ₹24,00,000 | 20 Nov 2026 | No | 14 Nov 2026, 10:30 am | pdf |

Notes:
- Spin: Branch is the branch name (`Hyderabad - Kondhapur`), Branch code is code and city
  (`HYB-007 · Hyderabad`), and Location is the area (the lead's city, else the area of its
  pincode, else the branch's city) and the pincode (`Hyderabad · 500084`). Branches come from the
  Branches page, else the 12 built-in stores the screen also uses; an unknown branch id leaves
  these cells empty. Occasion, Event date and Budget come from the same fields the screen uses
  (occasion type, occasion date, budget per card). Uploads accept the same branch names.
- Planner: Wedding date is the first day of celebrations, or the wedding month as text
  (`December 2027`) when there's no date.
- Lead is the name only (empty when there is none), so a re-upload never saves a phone or Plan ID
  as a name. Countdown text ("in 36 days") isn't exported.
- Interest values: `Interested`, `Not interested`, `Others`, `Follow up` (older leads).

**Formatting**

| | Excel (.xlsx) | CSV |
|---|---|---|
| Dates | Date cells, display format `dd mmm yyyy` → `20 Dec 2026` | `20 Dec 2026` |
| Date-times | Date-time cells in IST, format `d mmm yyyy, hh:mm am/pm` → `14 Nov 2026, 10:30 am` | `14 Nov 2026, 10:30 am` |
| Phone (Lead/Contact) | Text cells, with country code: `+919845012345` | Text, formula-safe: `'+919845012345` |
| Money | Number cells (full rupees) with a ₹ format and Indian grouping → `₹1,50,000` | Plain number: `150000` |
| Spin budget ranges | Text `₹10–20` | `₹10–20` |
| Assigned to | The chip text: `Phodeo`, `Phodeo · auto` or `Unassigned` | Same |
| Converted / Fits budget | `Yes` / `No` | `Yes` / `No` |
| Other text | Always a text cell, never a formula | Values starting with `=`, `+`, `-`, `@`, tab or CR get a leading `'` |
| Other | Header row bold and frozen | UTF-8 with BOM, comma-separated, CRLF line endings |

The upload parser accepts all of these back (including `₹` amounts, `₹10–20`, `Phodeo · auto`
and the date/date-time texts), plus `DD-MM-YYYY`.

**Errors**

| Status | `detail` |
|---|---|
| 400 | `This export has 61,234 leads; the limit is 50,000. Narrow the filters and try again.` |
| 400 | `Invalid sort column` (spin) |
| 403 | Vendor login without a vendor |
| 503 | `Read database is not configured` / `Read database unavailable` / `Read database query timed out` (spin: workflow database) |

---

### `POST /crm-leads/{source}`

Adds one lead (`origin = added`). Not for spin: `POST /crm-leads/spin` answers
`405 {"detail": "Spin leads can only be added by bulk upload"}`.

**Request (JSON)**

```json
{
  "values": { "phone": "98450 10001", "name": "Asha", "branch": "Jayanagar", "occasionDate": "20-12-2026" },
  "allow_duplicate": false
}
```

- `values`: field key to value. Strings, numbers and booleans are accepted. For spin,
  `branch` can be the branch name or its numeric id.
- `allow_duplicate` (default `false`): save even if the phone already exists in this
  source. Available only on this endpoint.
- `values.assigned_to` (optional): a vendor name = manual, `Unassigned` = unassigned, empty,
  `Automatic`, `Auto` or `<vendor> · auto` = automatic.
- Admin only, like every `/crm-leads` endpoint except export.

**Response `201`**

```json
{
  "lead": { "source": "spin", "lead_key": "crm-12", "origin": "added", "archived": false, "...": "..." },
  "warnings": [
    { "type": "other_source", "sources": ["wedding"] },
    { "type": "duplicate_allowed", "duplicate": { "reason": "existing_added", "lead_key": "crm-7", "created_at": "…", "archived": false } }
  ]
}
```

`warnings` is empty in the usual case. `duplicate_allowed` appears only when
`allow_duplicate` was used and a duplicate existed.

**Errors**

| Status | Body |
|---|---|
| 409 | Duplicate (below) |
| 422 | Field errors (see [Error format](#error-format)) |
| 503 | Workflow duplicate check unavailable |

**`409` duplicate body**

```json
{
  "detail": {
    "message": "A lead with this phone number already exists.",
    "duplicate": { "reason": "existing_sheet", "lead_key": "5f1c…" },
    "warnings": [ { "type": "other_source", "sources": ["whatsapp"] } ]
  }
}
```

The `duplicate` object follows [Duplicates](#4-duplicates). It is never `in_file` here.

---

### `GET /crm-leads/{source}/{lead_key}`

Returns one added/imported lead (archived ones too), for example to fill the edit form.

**Response `200`:** `{ ...lead object }` (the object itself, not wrapped in `lead`).

**Errors**

| Status | `detail` |
|---|---|
| 404 | `Lead not found` |
| 409 | `This lead comes from the Google Sheet and can't be changed here.` (spin: `…from the workflow (spin wheel)…`) |

---

### `PATCH /crm-leads/{source}/{lead_key}`

Edits an added/imported lead (archived ones too). Only the fields sent are changed.

**Request (JSON)**

```json
{ "values": { "name": "Asha R", "email": null } }
```

- Send `null` (or `""`) to clear a field. `phone` can't be cleared.
- Unknown keys, and `assigned_to`, are ignored (change the assignment with
  `PUT /leads/{source}/{lead_key}/crm`). If no known key is sent:
  `422 {"detail": {"message": "Nothing to change.", "errors": []}}`.
- `whatsapp`: the stored phone already includes its country code, so changing `country` alone
  doesn't change the number. To fix a number read with the wrong country, send `phone`
  (with its country code) together with `country`.
- Changing `phone` re-runs the duplicate check, ignoring the lead itself. There is no
  `allow_duplicate` here: a duplicate is always refused with `409`.

**Response `200`:** `{ "lead": { ...lead object } }`

**Errors:** `404` / `409` (sheet or workflow lead) as in GET, `409` duplicate (same body as add),
`422` field errors, `503` workflow duplicate check unavailable.

---

### `POST /crm-leads/{source}/{lead_key}/archive` and `/unarchive`

`archive` hides the lead from lists, counts and exports. The phone still counts in duplicate
checks and the CRM details are kept. `unarchive` shows it again. No request body. Calling
either one twice is harmless.

**Response `200`:** `{ "lead": { ...lead object, "archived": true } }`

**Errors:** `404` / `409` as in GET.

---

## 4. Duplicates

Duplicates are found on the normalised phone, within the same source. Each row (or add) gets
at most one `duplicate` object. When several apply, the first match in this order wins:
`existing_sheet`/`existing_workflow`, then `existing_added`, then `in_file`. In uploads a
duplicate wins over field errors: order **existing > in_file > errors** (a row with a usable phone
that repeats an existing lead or an earlier row is counted as a duplicate even if other fields
have errors).

| `reason` | When | Extra fields |
|---|---|---|
| `existing_sheet` | A sheet row in this source has the phone, including rows since removed from the sheet. Not used for spin. | `lead_key` |
| `existing_workflow` | Spin only: a workflow spin lead has the phone. | `lead_key` (workflow lead id) |
| `existing_added` | An added/imported lead in this source has the phone, including archived ones. | `lead_key`, `created_at` (ISO 8601 UTC), `archived` |
| `in_file` | Upload/import only: an earlier valid row in the same file or request has the phone. | `duplicate_of_row` |

```json
{ "reason": "in_file", "duplicate_of_row": 2 }
{ "reason": "existing_sheet", "lead_key": "5f1c0e…" }
{ "reason": "existing_workflow", "lead_key": "101" }
{ "reason": "existing_added", "lead_key": "crm-7", "created_at": "2026-11-14T05:00:00+00:00", "archived": false }
```

**Across sources (not blocking).** When the phone exists in *other* sources, the row or add
gets `{"type": "other_source", "sources": [...]}` with the sorted source names (for example
`["spin", "wedding"]`). Workflow spin leads count as `spin`. These rows are still imported.

| Endpoint | Duplicate in this source |
|---|---|
| Single add | `409`, unless `allow_duplicate: true` |
| Edit (phone change) | Always `409` |
| Import | Skipped, `status: "duplicate"` |

---

## 5. Access: who sees and edits what

**Logins.** An admin (`crm_users.role = admin`) sees every lead. A vendor user
(`role = vendor`) is linked to one vendor in `crm_vendor_users` and sees **only the leads whose
assignment is that vendor** (manual, sheet, branch or area rules, the same as the "Assigned to"
chip), in every source. Unassigned leads are visible to admins only.

For a vendor user, other vendors' leads and unassigned leads are never returned: not in lists,
totals, statistics, analytics, filter options or exports. A direct request for one answers
**`404 {"detail": "Lead not found"}`**. Archived leads are hidden the same way.

**`GET /leads/{source}/{lead_key}/crm`:** the lead's CRM details. `404` for a lead the user
can't see.

**`PUT /leads/{source}/{lead_key}/crm`:** send only the fields that change. Per field:

| Field | Admin | Vendor user of the assigned vendor | Other vendor users |
|---|---|---|---|
| `interest` (`interested`, `not_interested`, `other`, `follow_up`), `quoted_amount`, `quoted_date`, `converted` | **403** `Only the assigned vendor can change interest, quote and converted.` | ✔ | **404** |
| `vendor_id` (a vendor = manual; `null` = automatic) | ✔ | **403** `Only an admin can assign leads.` | **404** |
| `notes` | ✔ | ✔ | **404** |

- `updated_by` records the login that made the change.
- Setting `vendor_id` to `null` also switches a lead uploaded as unassigned to automatic
  assignment.
- A `crm-` key with no matching added/imported lead in `{source}` answers `404` (all sources).
  Workflow ids and sheet keys need no lookup.
- The CRM details of every lead are stored in
  `crm_lead_meta(source, lead_key = "crm-…" | workflow id | sheet key)`, never on the lead.

---

## 6. Changes to the existing endpoints

**Opened to vendor users, limited to their own leads:**

| Endpoint | Vendor users get |
|---|---|
| `GET /spin-leads` | Their spin leads (workflow and CRM-added). `vendor_id` is ignored. |
| `GET /spin-leads/filter-options`, `/whatsapp-leads/filter-options`, `/wedding-leads/filter-options`, `/wedding-plans/filter-options` | Dropdown values counted over their leads; no vendors (see [Lead list filters](#10-lead-list-filters)). |
| `GET /whatsapp-leads`, `/wedding-leads`, `/wedding-plans` | Their leads. |
| `GET /whatsapp-leads/stats`, `/wedding-leads/stats`, `/wedding-plans/stats` | Numbers over their leads. |
| `GET /analytics/overview` | Built from their leads only, for the period asked (see [Dashboard](#9-dashboard-date-filter)). |
| `GET /leads/{source}/{lead_key}/crm`, `PUT …/crm` | See [Access](#5-access-who-sees-and-edits-what). |
| `GET /crm-leads/{source}/export` | Their list. |
| `GET /vendors` | Only their own vendor. |
| `GET /branches` | `{id, name, code, city, is_active}` per branch: no vendors, no lead counts. |
| `GET /auth/me` | Adds `"vendor": {"id": 2, "name": "Phodeo"}` (`null` for admins). |

**Admin only (`403` for vendor users):** every other `/crm-leads` endpoint (fields, template,
preview, import, add, get, edit, archive, unarchive), `POST`/`PATCH /vendors`,
`GET /vendors/stats`, `POST`/`PATCH /branches`, `POST /branches/import`, `/sheets/*`,
`/read-db/*`, `/users`.

**List rows:** whatsapp, wedding and wedding planner items have `origin` (`sheet`, `added`,
`imported`); spin items have `origin` (`workflow`, `added`, `imported`). Added/imported spin leads
(not archived) are listed first, then the workflow leads; paging and `total` cover both. Each
item's `crm` object has `auto_assign` (`false` for leads uploaded unassigned).

---

## 7. Limits

| What | Limit | When exceeded |
|---|---|---|
| Upload file size | 5 MB | `400` |
| Rows per upload (non-empty rows after the header) | 5,000 | `400` |
| Columns read per upload | 200 (extra columns ignored) | |
| `sheet_name` | 31 characters | `422` |
| Rows per import request | 5,000 | `422` |
| Rows per export | 50,000 | `400` with the actual count |
| Text fields | `max_length` per field (see [Fields per source](#2-fields-per-source)) | Row/field error |

---

## 8. File downloads: filenames and CORS

`template` and `export` send the file name in `Content-Disposition`:

```
Content-Disposition: attachment; filename="wedding_planner-leads-2026-11-14.xlsx"
```

- Names are `<source>-leads-YYYY-MM-DD.<ext>` (export) and
  `<source>-leads-template-YYYY-MM-DD.<ext>` (template), using **today's date in IST**.
- The API sends `Access-Control-Expose-Headers: Content-Disposition`, so browser code on an
  allowed origin (`CORS_ORIGINS` in the backend settings) can read it.
- Requests need the bearer token, so download with `fetch`, not a plain link:

```js
const res = await fetch(`${API}/api/v1/crm-leads/wedding/export?format=xlsx&search=${q}`, {
  headers: { Authorization: `Bearer ${token}` },
});
if (!res.ok) throw new Error((await res.json()).detail);       // e.g. the 50,000-row limit
const disposition = res.headers.get("Content-Disposition") ?? "";
const name = /filename="([^"]+)"/.exec(disposition)?.[1] ?? "export.xlsx";
const url = URL.createObjectURL(await res.blob());
Object.assign(document.createElement("a"), { href: url, download: name }).click();
URL.revokeObjectURL(url);
```

---

## 9. Dashboard: date filter

`GET /analytics/overview` takes a period. So do the lists the Dashboard also calls
(`GET /spin-leads`, `/whatsapp-leads`, `/wedding-leads`, `/wedding-plans`), but for those it's
optional. Admins and vendor users; vendor users only ever get their own leads.

### Parameters

| Query | Format | Meaning |
|---|---|---|
| `date_from` | `YYYY-MM-DD` | First day of the period (India calendar day), included |
| `date_to` | `YYYY-MM-DD` | Last day, included |
| `period` | `all` | Dashboard only: **all time**, instead of `date_from` / `date_to` (see below) |

- **Defaults:** with neither date, `/analytics/overview` covers the **last 30 days including
  today**; the four lists apply **no filter** (the list pages are unchanged).
- **One date** (only `date_from` or only `date_to`): that single day. For a single day you can also
  send the same date twice.
- **IST days:** a day runs from 00:00 IST, which is 18:30 UTC the day before. A lead at 23:30 IST
  on 15 Nov counts on the 15th; one at 00:30 IST on 16 Nov (19:00 UTC on the 15th) counts on the 16th.
- **400 errors**, with the reason in `detail`:

| Case | `detail` |
|---|---|
| Not a valid date | `date_from must be a date like 2026-11-16 (YYYY-MM-DD)` |
| `date_from` after `date_to` | `date_from must be on or before date_to` |
| More than 366 days, both ends counted | `The period can be at most 366 days (this one has 367)` |
| A date after today (IST) | `Dates can't be in the future (today is 2026-11-16 in India)` |
| `period=all` with `date_from` or `date_to` | `Use either period=all or date_from / date_to, not both` |
| `period` other than `all` | `period must be "all" (or leave it out and send date_from / date_to)` |

### All time (`period=all`)

- **What's counted:** every number covers all the leads the viewer can see, including leads with
  no arrival date.
- **`period`:** `date_from` is the earliest arrival date among those leads and `date_to` is today.
  `days` counts both ends, `all_time` is `true`, `previous` is `null`, and `granularity` is `"month"`.
- **No comparison:** `previous_period`, `change_pct` and `average_per_day.previous` are `null`.
- **`average_per_day.period`:** leads per day over `days`, from the earliest arrival to today.
- **`trend`:** one point per calendar month, from the first lead's month to the current month
  (`{date: YYYY-MM-01, end: last day of the month, or today}`).
- **Limits:** the 366-day limit doesn't apply. Results are cached per viewer like any other period.
- **Lists:** they need no parameter for all time. Without `date_from` / `date_to`, the lists,
  exports and stats already cover every lead, and their totals equal the all-time `totals`.

### When a lead "arrives"

| Source | Arrival date |
|---|---|
| Spin, workflow | `spin_lead.created_at` |
| Spin, added/imported | `crm_spin_leads.created_at`: the entry time, or the uploaded "Created" |
| International, Wedding | `submitted_at`: the sheet's time; for added/imported leads the entry time or the uploaded "Submitted" |
| Wedding planner | `updated_in_sheet_at`: the sheet's "Last updated"; for added/imported plans the entry time or the uploaded "Updated" |

A planner visitor who continues a plan later moves it to that later day; the sheet has no
separate "submitted" time. Leads with no arrival time are never in a period, so the dashboard doesn't count them.

### What each number covers (`/analytics/overview`)

**Every number follows the period:** it counts only leads that arrived in it. The only exception is
`this_week` / `last_week`, kept with their original meaning for the current screen.

| Field | Screen | Covers |
|---|---|---|
| `totals` (per source, `all`) | Total leads, tile counts | Leads that arrived in the period (the same as `new_in_period`) |
| `new_in_period` | New in period, tiles' "+N" | Leads that arrived in the period |
| `previous_period` | Comparison | The same number of days right before `date_from` |
| `change_pct` | % change | `new_in_period` vs `previous_period`, rounded; `null` when the previous period has 0 |
| `average_per_day` | Average per day | `{period, previous}`, each per source plus `all`: leads per day, 1 decimal (e.g. 6 leads in 7 days = `0.9`) |
| `source_mix` | Source mix, tiles' % bar | `[{source, count, pct}]` of `new_in_period` |
| `trend` | Lead inflow | `[{date, end, <source counts>}]`: one point per day; for periods over 90 days, 7-day buckets starting at `date_from` (the last may be shorter). `period.granularity` says which |
| `wedding_budget`, `photography_budget`, `guests`, `budget_fit`, `repeats` | Wedding budgets, Budgets & size | Leads that arrived in the period |
| `sharing` | Shared with partners, Partners callout | Wedding and planner leads that arrived in the period |
| `top_cities`, `top_countries`, `occasions`, `partners` | Lists | Leads that arrived in the period |
| `upcoming_30_days` | Events in 30 days | Leads that arrived in the period whose event is from today to today + 30 |
| `upcoming_by_month` | Upcoming events by month | Leads that arrived in the period, by event month (from this month) |
| `needs_attention` | Needs attention | `{not_shared, over_budget, total}` among the leads that arrived in the period |
| `this_week`, `last_week` | (current screen) | **Original meaning**, not the period: the last 7 days vs the 7 before. Kept until the screen uses `new_in_period` / `previous_period`, then removed |

Also in the response:
- `period`: `{date_from, date_to, days, all_time, previous: {date_from, date_to} | null, granularity: "day" | "week" | "month"}`
- `not_filtered`: `[]` (kept for compatibility; every number follows the period)

**Caching:** results are cached per viewer and period. Vendor logins of the same vendor share
their vendor's entry, and `refresh=true` clears the cache.

### The lists (`/spin-leads`, `/whatsapp-leads`, `/wedding-leads`, `/wedding-plans`)

With `date_from` / `date_to`, only leads that arrived in the period are listed and counted, using
the same arrival dates, IST days and validation as above. Without them, nothing changes. The
exports and stats take the same parameters (see [Lead list filters](#10-lead-list-filters)).

---

## 10. Lead list filters

`GET /spin-leads`, `/whatsapp-leads`, `/wedding-leads` and `/wedding-plans` take the parameters
below. The same parameters work on:
- each source's export (`GET /crm-leads/{source}/export`);
- the stats endpoints (`GET /whatsapp-leads/stats`, `/wedding-leads/stats`, `/wedding-plans/stats`),
  whose numbers are all computed over the filtered leads.

A list, its export and its stats share one filter definition, so for the same parameters the list
`total`, the export's rows and the stats `total` are always equal. Spin has no stats endpoint; use
the list's `total`.

**Combining:** different parameters combine with AND; repeated values of one parameter
(`?interest=interested&interest=not_set`) combine with OR. Vendor users only ever get their own
leads, whatever they send.

### Parameters

✔ = available for that source.

| Parameter | Spin | International | Wedding | Planner | Meaning |
|---|---|---|---|---|---|
| `search` | ✔ | ✔ | ✔ | ✔ | Text match. Name and email where the source has them, and the phone by its digits (`+91 98450` finds `919845…`). Spin: every visible text field. International: phone, country and event-date text. Wedding: name, email, city, occasion, leads shared. Planner: name, email, city, plan ID, traditions, leads shared |
| `date_from`, `date_to` | ✔ | ✔ | ✔ | ✔ | Arrival date, IST days, inclusive (see [Dashboard](#9-dashboard-date-filter)) |
| `event_from`, `event_to` | ✔ | ✔ | ✔ | ✔ | Event date, `YYYY-MM-DD`, inclusive. Spin: occasion date. International and Wedding: the event date when it reads as a date (text such as "April 2027" never matches). Planner: wedding date (first day, else the 1st of the month) |
| `upcoming_days` | ✔ | ✔ | ✔ | ✔ | Event from today (IST) to today + N, N from 1 to 366 |
| `vendor_id` (repeatable) | ✔ | ✔ | ✔ | ✔ | Assigned to, exactly as the "Assigned to" chip. **Admins only**; ignored for vendor users |
| `unassigned=true` | ✔ | ✔ | ✔ | ✔ | No vendor. Admins only. With `vendor_id`: either |
| `interest` (repeatable) | ✔ | ✔ | ✔ | ✔ | `interested`, `not_interested`, `other`, `follow_up`, `not_set` |
| `converted` | ✔ | ✔ | ✔ | ✔ | `yes` / `no` |
| `quote` | ✔ | ✔ | ✔ | ✔ | `has`, `none`, `gt:<amount>` (more than), `lt:<amount>` (less than), in rupees |
| `origin` (repeatable) | `workflow`, `added`, `imported` | `sheet`, `added`, `imported` | same | same | Where the lead came from |
| `branch` | ✔ | | | | Branch id |
| `pincode` | ✔ | | | | Pincode starts with |
| `budget` | ✔ | | | | Exact budget value, e.g. `10-20` |
| `country` | | ✔ | | | Exact country |
| `occasion` | | | ✔ | | Exact occasion |
| `shared_with` | | | ✔ | ✔ | Partner name, or `__none__` for not shared |
| `hide_repeats=true` | | | ✔ | | Leave out repeat submissions |
| `fits_budget` | | | | ✔ | `true` / `false` |
| `last_step` | | | | ✔ | The planner step, e.g. `pdf` |
| `city` | | | ✔ | ✔ | City contains, ignoring case (`Hyderabad` finds "Shamirpet, Hyderabad"). Planner: wedding city |
| `budget_min`, `budget_max` | | | ✔ | ✔ | Inclusive, rupees. Wedding: the budget amount; Planner: the total budget |
| `guests_min`, `guests_max` | | | | ✔ | Inclusive total guests |
| `page`, `page_size`, `sort`, `order` | ✔ | ✔ | ✔ | ✔ | Paging and sort (lists and exports) |

**Older spin parameters**, still accepted until the frontend switches, then removed:
`interest=none` (= `not_set`), and `quoted_op` (`set`, `unset`, `eq`, `ne`, `gt`, `gte`, `lt`,
`lte`) with `quoted_value`. A `quote` parameter wins over them.

### Errors (400)

| Case | `detail` |
|---|---|
| Bad `quote` | `quote must be has, none, gt:<amount> or lt:<amount> (e.g. gt:50000)` |
| `event_from` after `event_to` | `event_from must be on or before event_to` |
| `upcoming_days` with `event_from` / `event_to` | `Use either upcoming_days or event_from / event_to, not both` |
| `upcoming_days` outside 1–366 | `upcoming_days must be between 1 and 366` |
| Event date not `YYYY-MM-DD` | `event_from must be a date like 2026-12-20 (YYYY-MM-DD)` |
| `origin` not available for the source | `origin 'workflow' doesn't exist for International leads; use sheet, added, imported` |
| Unknown `interest` | `interest must be one of: interested, not_interested, other, follow_up, not_set` |
| A minimum above its maximum | `budget_min must not be more than budget_max` (also guests) |
| Arrival dates | As in [Dashboard](#9-dashboard-date-filter) |

### Filter options (dropdown values)

| Endpoint | Returns |
|---|---|
| `GET /spin-leads/filter-options` | `budgets`, `pincodes`, `has_branch` / `has_pincode` / `has_budget` (as before), plus `branches` (`[{id, name, count}]`), `origins`, `interests`, `vendors` |
| `GET /whatsapp-leads/filter-options` | `countries`, `origins`, `interests`, `vendors` |
| `GET /wedding-leads/filter-options` | `occasions`, `cities`, `shared_with` (including `__none__`), `origins`, `interests`, `vendors` |
| `GET /wedding-plans/filter-options` | `cities`, `last_steps`, `shared_with` (including `__none__`), `origins`, `interests`, `vendors` |

- **Format:** values are `[{value, count}]`, most common first. Counts are over all the leads the
  viewer may see (vendor users: their own), not narrowed by other selected filters.
- **Cities:** cleaned like the dashboard's Top cities ("Shamirpet, Hyderabad" becomes "Hyderabad"),
  which suits the `city` "contains" filter.
- **`interests`:** includes `not_set`.
- **`vendors`:** `[{id, name}]` of the active vendors for admins; empty for vendor users, since
  vendor filtering is admin-only.
