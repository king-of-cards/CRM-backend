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
11. [Vendor → KOC](#11-vendor--koc)
12. [Users and vendor logins](#12-users-and-vendor-logins)
13. [Dhruva leads](#13-dhruva-leads)
14. [Notifications and hot leads](#14-notifications-and-hot-leads)
15. [Activity and follow-up reminders](#15-activity-and-follow-up-reminders)
16. [View-only role](#16-view-only-role)
17. [Spin: cut-off, gift leads and history upload](#17-spin-cut-off-gift-leads-and-history-upload)
18. [KOC → Dhruva](#18-koc--dhruva)

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

**CRM fields are separate.** Assigned vendor, Update, quoted date/amount, conversion amount, follow-up date and notes
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
| `estimatedBudgetPerCard` | Budget | range | 100 | budget per card, estimated budget per card |
| `otherWeddingServices` | Other wedding services | text | 500 | |
| `quantityRequired` | Quantity required | int | | |
| `assigned_to` | Assigned to | assigned | | assigned to, vendor |
| `created_at` | Created | datetime | | created at |

Other wedding services naming `Wedding Gifts & Return Gifts` makes a gift lead (automatic vendor
Kraftyz, see [section 17](#17-spin-cut-off-gift-leads-and-history-upload)).
Read-only: Branch code, Quoted date. Uploads only: Update, Quoted amount, Conversion amount (see below).
Extra columns: Pincode (`pincode`), Lead occasion (`occasion`), Bride/Groom name, Bride/Groom
phone, Event venue, Coverage days, Service, Service types, Service budget, Service quantity. Indian numbers are stored in the workflow's 10-digit
form; `occasionDate` as `YYYY-MM-DD`.

### whatsapp (International)

| Key | Label (column) | Type | Max | Upload aliases |
|---|---|---|---|---|
| `phone` | **Lead** (required) | phone | 32 | phone, contact, whatsapp, whatsapp number, mobile, number |
| `country` | Country | country | 100 | |
| `event_date` | Event date | event_date | 100 | wedding date |
| `assigned_to` | Assigned to | assigned | | |
| `submitted_at` | Submitted | datetime | | submitted at, submission time, timestamp |

Read-only: Quoted date. Uploads only: Update, Quoted amount, Conversion amount (see below).

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

Read-only: Quoted date. Uploads only: Update, Quoted amount, Conversion amount (see below). `is_repeat` is set automatically
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

Read-only: Estimate, Fits budget, Quoted date, Last step. Uploads only: Update, Quoted amount, Conversion amount (see below).
Extra columns: Wedding month, First day of celebrations, Wedding type, Family home city, Kind of
place, Traditions, Food, Alcohol, Budget includes jewellery and outfits, Jewellery and outfits,
Priority 1–3, Shortlisted vendors, Consent: share plan, Consent: offers on WhatsApp.

**Uploads only (new leads):** three CRM columns are read from bulk uploads and saved in
`crm_lead_meta` with the new lead (like Assigned to). They aren't add/edit form fields; change
them later with `PUT /leads/…/crm`. Each row's `values` carries them under these keys:

| Key | Label | Accepted | Rules |
|---|---|---|---|
| `update_status` | Update | a code (`will_call_back`) or its label (`Will call back`), any case; empty or `Pending` = Pending | Unknown value: row error |
| `quoted_amount` | Quoted amount | rupees: `150000`, `1,50,000`, `₹1,50,000`, `1.5 lakh` | 0 to 10,00,00,000, else a row error |
| `conversion_amount` | Conversion amount | same | same |

Duplicates are skipped as before, so an existing lead's CRM details never change through an
upload. A new lead that is automatic, Pending and without amounts gets no `crm_lead_meta` row.

**Never taken from uploads or add/edit:** quoted date, notes (set with `PUT /leads/…/crm`),
"leads shared", and fields only the planner tool or the workflow fill in. Unknown keys in
`values` are ignored.

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

Admin only. Downloads an import file with the uploadable columns. Exports add read-only columns
such as `Hot`; those are recognized during preview and are not imported.

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
  field, the first one wins. Read-only export columns (Quoted date, Branch code, …) are
  listed in `read_only_columns`; other columns that match no field in `ignored_columns`.
  Update, Quoted amount and Conversion amount are read (see [Uploads only](#2-fields-per-source)).
- Admin only.

**Response `200`**

```json
{
  "source": "spin",
  "columns": { "Phone": "phone", "Name": "name", "Branch": "branch" },
  "ignored_columns": ["Notes from sales"],
  "read_only_columns": ["Branch code", "Quoted date"],
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
order, the CRM columns, then the trailing column), with an example row. Vendor logins get the
same columns **without Assigned to**:

**spin**

| Lead | Email | Contact | Branch | Branch code | Location | Occasion | Event date | Budget | Assigned to | Update | Quoted amount | Quoted date | Conversion amount | Created |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Asha Rao | asha@example.com | +919845012345 | Jayanagar | JNG-004 · Bangalore | Bangalore · 560011 | Wedding | 20 Dec 2026 | ₹10–20 | Phodeo | Interested | ₹45,000 | 20 Nov 2026 | | 14 Nov 2026, 10:30 am |

**whatsapp**

| Lead | Country | Event date | Assigned to | Update | Quoted amount | Quoted date | Conversion amount | Submitted |
|---|---|---|---|---|---|---|---|---|
| +971501234567 | United Arab Emirates | 20 Dec 2026 | OHP · auto | Pending | | | | 14 Nov 2026, 10:30 am |

**wedding**

| Lead | Email | Contact | Occasion | Event date | City | Budget | Assigned to | Update | Quoted amount | Quoted date | Conversion amount | Submitted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Ravi Kumar | ravi@example.com | +919845010003 | Wedding | 20 Dec 2026 | Bangalore | ₹1,50,000 | Phodeo · auto | Converted | ₹60,000 | 20 Nov 2026 | ₹55,000 | 14 Nov 2026, 10:30 am |

**wedding_planner**

| Lead | Role | Email | Contact | Wedding date | Wedding city | Guests | Guests staying | Budget | Budget range | Estimate | Fits budget | Assigned to | Update | Quoted amount | Quoted date | Conversion amount | Updated | Last step |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Meera | Bride | meera@example.com | +919845010004 | 20 Dec 2027 | Udaipur | 300 | 120 | ₹25,00,000 | 20L–30L | ₹23,10,000 | Yes | OHP · auto | Will call back | ₹24,00,000 | 20 Nov 2026 | | 14 Nov 2026, 10:30 am | pdf |

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
- Update values: `Pending` (empty), `Converted`, `Not converted`, `Already purchased somewhere else`,
  `Already purchased with us`, `Will call back`, `Interested`, `Not interested` (the Vendor → KOC labels).
  They upload back, as do the amounts.

**Formatting**

| | Excel (.xlsx) | CSV |
|---|---|---|
| Dates | Date cells, display format `dd mmm yyyy` → `20 Dec 2026` | `20 Dec 2026` |
| Date-times | Date-time cells in IST, format `d mmm yyyy, hh:mm am/pm` → `14 Nov 2026, 10:30 am` | `14 Nov 2026, 10:30 am` |
| Phone (Lead/Contact) | Text cells, with country code: `+919845012345` | Text, formula-safe: `'+919845012345` |
| Money | Number cells (full rupees) with a ₹ format and Indian grouping → `₹1,50,000` | Plain number: `150000` |
| Spin budget ranges | Text `₹10–20` | `₹10–20` |
| Assigned to | The chip text: `Phodeo`, `Phodeo · auto` or `Unassigned`. **Admins only:** vendor logins' exports leave this column out | Same |
| Fits budget | `Yes` / `No` | `Yes` / `No` |
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
| `existing_workflow` | Spin only: a workflow spin row created on or after the spin cut-off (listed or not) has the phone; older rows are ignored ([section 17](#17-spin-cut-off-gift-leads-and-history-upload)). | `lead_key` (workflow lead id) |
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
`["spin", "wedding"]`). Workflow spin rows on or after the spin cut-off count as `spin`. These rows are still imported.

| Endpoint | Duplicate in this source |
|---|---|
| Single add | `409`, unless `allow_duplicate: true` |
| Edit (phone change) | Always `409` |
| Import | Skipped, `status: "duplicate"` |

---

## 5. Access: who sees and edits what

**Logins.** (Dhruva logins never reach these endpoints: see [Users](#12-users-and-vendor-logins).) An admin (`crm_users.role = admin`) sees every lead. A view-only login (`role = viewer`) sees exactly what an admin sees and changes nothing (see [View-only role](#16-view-only-role)). A vendor user
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
| `update_status` (`converted`, `not_converted`, `purchased_elsewhere`, `purchased_with_us`, `will_call_back`, `interested`, `not_interested`; `null` = Pending), `quoted_amount`, `quoted_date`, `conversion_amount` (rupees, 0–100,000,000) | ✔ | ✔ | **404** |
| `vendor_id` (a vendor = manual; `null` = automatic) | ✔ | **403** `Only an admin can assign leads.` | **404** |
| `notes` | ✔ | ✔ | **404** |
| `follow_up_date` (`YYYY-MM-DD` or `null`) | ✔ | ✔ | **404** |

- `updated_by` records the login that made the last change (admin or vendor).
- `interest` and `converted` were replaced by `update_status`: sending either answers **422**
  (`interest was replaced by update_status (…)`), and nothing is saved.
- The answer (and every list row's `crm` object) has `update_status` and `conversion_amount`;
  `interest`, `converted` and `converted_at` are gone.
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
| `GET /spin-leads` | Their spin leads (live workflow rows and CRM-added). `vendor_id` is ignored. |
| `GET /spin-leads/filter-options`, `/whatsapp-leads/filter-options`, `/wedding-leads/filter-options`, `/wedding-plans/filter-options` | Dropdown values counted over their leads; no vendors (see [Lead list filters](#10-lead-list-filters)). |
| `GET /whatsapp-leads`, `/wedding-leads`, `/wedding-plans` | Their leads. |
| `GET /spin-leads/stats`, `/whatsapp-leads/stats`, `/wedding-leads/stats`, `/wedding-plans/stats` | Numbers over their leads. |
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

**`GET /vendors/stats` (admin):** per vendor `leads`, `pending` and a count for each of the 7
update values (`converted`, `not_converted`, `purchased_elsewhere`, `purchased_with_us`,
`will_call_back`, `interested`, `not_interested`), `quoted` / `quoted_value` (leads with a quote and
their sum), `converted_value` (the sum of conversion amounts), `conversion_rate` (`converted` of
`leads`, %) and `by_source`. The old `other` and `follow_up` counts are gone.

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
| `sources` | comma-separated | Dashboard only: the sources **Top cities** is computed over (see [Top cities sources](#top-cities-sources-sources)) |

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
- **No comparison:** `previous_period` and `change_pct` are `null`.
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

### KPIs: the same six everywhere

Every lead screen's stats and dashboards return a `kpis` object with the same core keys, computed
the same way over the leads they cover:

```json
{"total": 120, "interested": 14, "not_interested": 9, "quoted_count": 11, "quoted_total": 1650000,
 "converted": 6, "converted_pct": 5.0, "conversion_count": 5, "conversion_total": 720000}
```

| Key | Meaning |
|---|---|
| `total` | Leads counted |
| `interested`, `not_interested` | Update is "interested" / "not_interested" |
| `quoted_count`, `quoted_total` | Leads with a quoted amount, and their sum (rupees) |
| `converted`, `converted_pct` | Update is "converted"; % of `total`, 1 decimal, `null` with no leads |
| `conversion_count`, `conversion_total` | Leads with a conversion amount, and their sum (rupees) |

Dashboard `kpis` and overview outcomes also include `conversion_amount_count` and
`conversion_amount_total`: converted leads with a conversion amount and the sum for those same
leads, within the period. Existing `conversion_count` / `conversion_total` retain their meaning.

The 4 sources read Update and the amounts from `crm_lead_meta` (a lead without CRM details is
Pending, without amounts); Vendor → KOC from its own columns.

| Where | Covers |
|---|---|
| `GET /spin-leads/stats`, `/whatsapp-leads/stats`, `/wedding-leads/stats`, `/wedding-plans/stats` | The list's leads for the same filters (vendor users: their own) |
| `GET /vendor-to-koc/leads/stats` | The same filtered Vendor → KOC leads (vendors: their own) |
| `GET /analytics/overview` | `kpis`: every source combined, the period's leads. Dashboard-only `quotes_count` is amount-only and `hot_count` counts hot leads; `outcomes.<source>` has those counts per source, all following the date period. `conversion_amount_count` / `conversion_amount_total` count and sum amounts only for converted leads with an amount. Wedding + Wedding planner subtotal can be summed from those two source outcomes |
| `GET /analytics/vendor-to-koc` | Period's Vendor → KOC leads (admins: all; vendors: their own); dashboard `kpis` adds amount-only `quotes_count`, date-filtered `hot_count`, and the converted-with-amount count and matching amount total |
| `GET /analytics/dhruva` | Dhruva leads received in the period; dashboard `kpis` includes the converted-with-amount count and matching amount total |

### Top cities sources (`sources`)

`GET /analytics/overview` and `GET /analytics/vendor-to-koc` take an optional `sources`: a
comma-separated list of `spin`, `whatsapp`, `wedding`, `wedding_planner`, `vendor_to_koc`, `dhruva`,
`koc_to_dhruva`
(the same parsing and validation as `GET /reminders/follow-ups`: spaces and capitals are ignored).
Only the Top cities fields (`top_cities`, `top_cities_sources`, `top_cities_no_city`,
`top_cities_no_city_by_source`) follow it; every other number is unchanged.

| Endpoint | Default (no `sources`) |
|---|---|
| `/analytics/overview` | `spin,wedding,wedding_planner` |
| `/analytics/vendor-to-koc` | `vendor_to_koc` |

- `whatsapp` (country only) and `dhruva` (venue only) are accepted but have no city, so they never
  appear in `top_cities_sources`; `?sources=whatsapp` gives `top_cities: []`, `top_cities_sources: []`.
- `top_cities_sources` lists the included sources in the order `spin`, `wedding`, `wedding_planner`,
  `vendor_to_koc`. Spin is left out when the workflow database can't be read.
- Vendor logins: on the overview every source covers only their own leads (`vendor_to_koc`: their
  vendor's leads). On `/analytics/vendor-to-koc` they still get no Top cities fields; `sources` is
  validated but changes nothing for them.
- Results are cached per viewer, period **and** source selection (`spin,wedding` and `wedding,spin`
  share one entry).
- **400 errors:** `sources must be a comma-separated list of: spin, whatsapp, wedding, wedding_planner, vendor_to_koc, dhruva, koc_to_dhruva`
  (empty list such as `?sources=,`), or `Unknown source(s): <names>. Allowed: spin, whatsapp, wedding, wedding_planner, vendor_to_koc, dhruva, koc_to_dhruva`.

### What each number covers (`/analytics/overview`)

**Every number follows the period:** it counts only leads that arrived in it. The only exception is
`this_week` / `last_week`, kept with their original meaning for the current screen.

| Field | Screen | Covers |
|---|---|---|
| `totals` (per source, `all`) | Total leads, tile counts | Leads that arrived in the period (the same as `new_in_period`) |
| `new_in_period` | New in period, tiles' "+N" | Leads that arrived in the period |
| `previous_period` | Comparison | The same number of days right before `date_from` |
| `change_pct` | % change | `new_in_period` vs `previous_period`, rounded; `null` when the previous period has 0 |
| `kpis` | KPI tiles | [The six KPIs](#kpis-the-same-six-everywhere) over every source's leads of the period (spin left out when the workflow database can't be read) |
| `source_mix` | Source mix, tiles' % bar | `[{source, count, pct}]` of `new_in_period` |
| `trend` | Lead inflow | `[{date, end, <source counts>}]`: one point per day; for periods over 90 days, 7-day buckets starting at `date_from` (the last may be shorter). `period.granularity` says which |
| `photography_budget`, `guests`, `budget_fit`, `repeats` | Budgets & size | Leads that arrived in the period |
| `sharing` | Shared with partners, Partners callout | Wedding and planner leads that arrived in the period. `by_source`: `{wedding, wedding_planner}`, each `{shareable, shared, not_shared}` (they add up to the totals) |
| `top_cities` | Top 10 `[{city, count, by_source}]` | Leads of the [selected sources](#top-cities-sources-sources) (default Spin, Wedding and Wedding planner; vendors: their own leads) that arrived in the period; Spin uses lead city/location or pincode area, never branch fallback. `top_cities_sources` lists the sources actually included. `top_cities_no_city` and `top_cities_no_city_by_source` count blank locations. International country and Dhruva venue are never counted |
| City alias normalization | Top cities | Bangalore/Bengaluru, Hyderabad/Hydrabad/Hyderbad, Gurgaon/Gurugram, Bombay/Mumbai, Madras/Chennai, Calcutta/Kolkata, Mysore/Mysuru, Trivandrum/Thiruvananthapuram, Cochin/Kochi, Pune/Poona; observed `Banglore` and `@Bengaluru` also normalize to Bengaluru |
| City aliases |  | Bangalore/Bengaluru, Hyderabad/Hydrabad/Hyderbad, Gurgaon/Gurugram, Bombay/Mumbai, Madras/Chennai, Calcutta/Kolkata, Mysore/Mysuru, Trivandrum/Thiruvananthapuram, Cochin/Kochi, Pune/Poona; observed `Banglore` and `@Bengaluru` also normalize to Bengaluru |
| `top_countries`, `occasions`, `partners` | Lists | Leads that arrived in the period |
| `upcoming_by_month` | Upcoming events by month | Leads that arrived in the period, by event month (from this month) |
| `needs_attention` | Needs attention | `{not_shared, over_budget, total}` among the leads that arrived in the period. `by_source`: `{wedding, wedding_planner}`, each `{not_shared, over_budget, total}` (wedding leads have no budget fit, so their `over_budget` is 0) |
| `outcomes` | CRM results per source | `{spin, whatsapp, wedding, wedding_planner, all}`, each [the six KPIs](#kpis-the-same-six-everywhere) plus `leads` (= `total`, kept for the current screen) over the leads that arrived in the period (all of the viewer's leads for `period=all`). `interested` / `converted`: the Update is "interested" / "converted".  The values come from the CRM details (`crm_lead_meta`) with the same lead keys as the lists, so they equal the list totals for `update_status=interested`, `update_status=not_interested`, `update_status=converted`, `quote=has` and `conversion=has` with the same dates, and each list's stats `kpis`. `spin` is `null` (and left out of `all`) when the workflow database can't be read. Spin covers the live workflow rows and every CRM spin lead |
| Removed | | `average_per_day`, `upcoming_30_days` and `wedding_budget` are no longer returned |
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
- the stats endpoints (`GET /spin-leads/stats`, `/whatsapp-leads/stats`, `/wedding-leads/stats`,
  `/wedding-plans/stats`), whose numbers are all computed over the filtered leads. Each has
  [`kpis`](#kpis-the-same-six-everywhere); the sheet sources keep their other numbers
  (`last_7_days`, `upcoming_events`, …). Spin's returns `{total, kpis}`.

A list, its export and its stats share one filter definition, so for the same parameters the list
`total`, the export's rows and the stats `total` are always equal.

**Combining:** different parameters combine with AND; repeated values of one parameter
(`?update_status=interested&update_status=pending`) combine with OR. Vendor users only ever get their own
leads, whatever they send.

### Parameters

✔ = available for that source.

| Parameter | Spin | International | Wedding | Planner | Meaning |
|---|---|---|---|---|---|
| `hot` | yes / no | yes / no | yes / no | yes / no | Whether the lead is marked hot; workflow Spin rows use the CRM hot flag sidecar |
| `follow_up` | `due`, `overdue`, `today`, `upcoming` | same | same | same | Follow-up date relative to today (IST); no-date and closed leads do not match |
| `search` | ✔ | ✔ | ✔ | ✔ | Text match. Name and email where the source has them, and the phone by its digits (`+91 98450` finds `919845…`). Spin: every visible text field. International: phone, country and event-date text. Wedding: name, email, city, occasion, leads shared. Planner: name, email, city, plan ID, traditions, leads shared |
| `date_from`, `date_to` | ✔ | ✔ | ✔ | ✔ | Arrival date, IST days, inclusive (see [Dashboard](#9-dashboard-date-filter)) |
| `event_from`, `event_to` | ✔ | ✔ | ✔ | ✔ | Event date, `YYYY-MM-DD`, inclusive. Spin: occasion date. International and Wedding: the event date when it reads as a date (text such as "April 2027" never matches). Planner: wedding date (first day, else the 1st of the month) |
| `upcoming_days` | ✔ | ✔ | ✔ | ✔ | Event from today (IST) to today + N, N from 1 to 366 |
| `vendor_id` (repeatable) | ✔ | ✔ | ✔ | ✔ | Assigned to, exactly as the "Assigned to" chip. **Admins only**; ignored for vendor users |
| `unassigned=true` | ✔ | ✔ | ✔ | ✔ | No vendor. Admins only. With `vendor_id`: either |
| `update_status` (repeatable) | ✔ | ✔ | ✔ | ✔ | `converted`, `not_converted`, `purchased_elsewhere`, `purchased_with_us`, `will_call_back`, `interested`, `not_interested`, `pending` (no update yet) |
| `quote` | ✔ | ✔ | ✔ | ✔ | Quoted amount: `has`, `none`, `gt:<amount>` (more than), `lt:<amount>` (less than), in rupees |
| `conversion` | ✔ | ✔ | ✔ | ✔ | Conversion amount: same values as `quote` |
| `origin` (repeatable) | `workflow`, `added`, `imported` | `sheet`, `added`, `imported` | same | same | Where the lead came from |
| `also_in` | ✔ | ✔ | ✔ | ✔ | `yes` / `no`: the phone also exists in another source or in Vendor → KOC that the viewer may see (see [Cross-source duplicates](#cross-source-duplicates-also_in)); other values: 422 |
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
| `page`, `page_size`, `sort`, `order` | ✔ | ✔ | ✔ | ✔ | Paging (lists) and sort (lists and exports). `page_size`: 10, 25, 50 or 100, default 10; anything else answers 422 (`page_size must be one of 10, 25, 50, 100`). Responses keep `total` |

### Errors (400)

| Case | `detail` |
|---|---|
| Bad `quote` / `conversion` | `quote must be has, none, gt:<amount> or lt:<amount> (e.g. gt:50000)` (`conversion must be …`) |
| Bad `update_status` | `update_status must be one of: converted, …, pending` |
| The old `interest` / `converted` filters | `interest and converted were replaced by update_status (…)` |
| `event_from` after `event_to` | `event_from must be on or before event_to` |
| `upcoming_days` with `event_from` / `event_to` | `Use either upcoming_days or event_from / event_to, not both` |
| `upcoming_days` outside 1–366 | `upcoming_days must be between 1 and 366` |
| Event date not `YYYY-MM-DD` | `event_from must be a date like 2026-12-20 (YYYY-MM-DD)` |
| `origin` not available for the source | `origin 'workflow' doesn't exist for International leads; use sheet, added, imported` |
| A minimum above its maximum | `budget_min must not be more than budget_max` (also guests) |
| Arrival dates | As in [Dashboard](#9-dashboard-date-filter) |

### Cross-source duplicates (`also_in`)

Every lead in the 6 lists (`GET /spin-leads`, `/whatsapp-leads`, `/wedding-leads`, `/wedding-plans`,
`/vendor-to-koc/leads`, `/dhruva/leads`) has `also_in`: the **other** places where the same phone
exists.

```json
"also_in": [
  {"source": "spin", "count": 1},
  {"source": "whatsapp", "count": 1},
  {"source": "vendor_to_koc", "count": 2, "vendors": ["OHP", "Phodeo"]}
]
```

- **Places:** `spin` (workflow rows on or after the spin cut-off, and CRM spin leads), `whatsapp`, `wedding`, `wedding_planner`,
  `vendor_to_koc`, `dhruva` and `koc_to_dhruva` (admins and view-only only; see [KOC → Dhruva](#18-koc--dhruva)), always in that order. A lead's own source is never listed (duplicates inside one
  source are the [duplicate check](#4-duplicates)'s job). Empty list: the phone is nowhere else.
- **Matching:** by E.164 phone (`phone_e164`; Vendor → KOC's 10-digit numbers as `+91…`; Dhruva:
  phone **and** alt phone, so a Dhruva lead is found by either, and a lead found through both of a
  Dhruva lead's numbers counts once). Archived and removed rows count, like the upload duplicate
  check. This cross-source matching remains phone-only; it is separate from Dhruva's same-source
  duplicate rule in section 13.
- **`count`:** how many leads in that place. **`vendors`** (Vendor → KOC, admins only): the names
  of the vendors who sent that phone.
- **Vendor logins** only get places they can see themselves: their own leads in the other sources
  (the same assignment as the lists; archived leads aren't theirs to see) and their own Vendor → KOC
  leads. Other vendors' and unassigned leads are never counted, and `vendors` is left out. They
  never see `dhruva`.
- **Dhruva leads** are shown to admins only: in every other list (`dhruva`) and, on Dhruva leads,
  every other section. **Dhruva logins** don't see other sections, so their `also_in` is always
  empty and `also_in=yes` finds nothing.
- **Cost:** computed per page, one query per source (plus the workflow database for spin).
- **Filter `also_in=yes|no`** on all 6 lists, their stats and their exports: the same rule over
  the whole source, so `also_in=yes` rows always have a non-empty `also_in`. If the workflow
  database can't be read, spin workflow leads are left out of `also_in` (a warning is logged).

### Filter options (dropdown values)

| Endpoint | Returns |
|---|---|
| `GET /spin-leads/filter-options` | `budgets`, `pincodes`, `has_branch` / `has_pincode` / `has_budget` (as before), plus `branches` (`[{id, name, count}]`), `origins`, `updates`, `vendors`, `other_services` (each Other wedding service) |
| `GET /whatsapp-leads/filter-options` | `countries`, `origins`, `updates`, `vendors` |
| `GET /wedding-leads/filter-options` | `occasions`, `cities`, `shared_with` (including `__none__`), `origins`, `updates`, `vendors` |
| `GET /wedding-plans/filter-options` | `cities`, `last_steps`, `shared_with` (including `__none__`), `origins`, `updates`, `vendors` |

- **Format:** values are `[{value, count}]`, most common first. Counts are over all the leads the
  viewer may see (vendor users: their own), not narrowed by other selected filters.
- **Cities:** cleaned like the dashboard's Top cities ("Shamirpet, Hyderabad" becomes "Hyderabad"),
  which suits the `city` "contains" filter.
- **`updates`:** `pending` first, then the 7 update values, each with its count (0 included).
- **`vendors`:** `[{id, name}]` of the active vendors for admins; empty for vendor users, since
  vendor filtering is admin-only.

---

## 11. Vendor → KOC

Leads that vendors send to KOC: table `crm_vendor_to_koc_leads`, vendors in `crm_vendors`,
vendor logins linked in `crm_vendor_users`. Ported from the other CRM backend with the same
paths, shapes and rules. Base path: `/api/v1/vendor-to-koc`.

**Who:**
- **Admins** see every vendor's leads (list, stats, export, details), and edit the admin fields
  (`update_status`, `additional_comments`, `follow_up_date`). They **don't add leads**:
  `POST /upload/preview`, `POST /leads/bulk` and `POST /leads` answer
  `403 {"detail": "Only vendor logins can add Vendor → KOC leads"}` for admins (checked before
  the body or file). The template stays available to everyone.
- **Vendor logins** add leads and work only with their own vendor's leads. Their vendor comes from
  the login and is never taken from input: a `vendor_id` they send is ignored. They **see KOC's
  update read-only** (`update_status`, `additional_comments`, `follow_up_date`,
  `status_updated_at`, `status_updated_by_name`) and may filter and sort by it; `PATCH` answers
  403. They never get user ids of admins (`status_updated_by`), other vendors' leads or numbers.
  Another vendor's lead answers 404.
- **A vendor login without a vendor:** `403 {"detail": "This login isn't linked to a vendor."}`.

### Lead rules

| Field | Rule |
|---|---|
| `phone` | **Required.** A 10-digit Indian mobile number starting with 6–9. `+91`, a 12-digit `91…`, a leading `0`, spaces, dashes and a leading `'` (the CSV export's formula guard) are removed. Stored as the 10 digits |
| `name`, `city` | Optional, at most 100 characters |
| `date` (the lead's own date) | Optional. Files: a real date, or text `DD-MM-YYYY` (day first) or `13 Dec 2026` / `13 December 2026` (the export's format). JSON: `YYYY-MM-DD` |
| Duplicates | One lead per vendor and phone. Duplicates are skipped in bulk and answer 409 for a single add |
| Exported files | An export (CSV or Excel) uploads back unchanged: Name, Date, Phone and City are read, the other columns ignored, and its rows show as duplicates when that vendor's login uploads it (an admin's export too) |
| Template example row | The template's example row, uploaded unchanged, is an error (`"This is the example row from the template; delete it"`) |
| `update_status` | `null` (pending), `converted`, `not_converted`, `purchased_elsewhere`, `purchased_with_us`, `will_call_back`, `interested`, `not_interested` |
| `source` | `bulk_upload` or `manual` |

### Endpoints

| Method and path | Who | What |
|---|---|---|
| `POST /upload/preview` | vendor | `multipart/form-data`: `file` (`.xlsx` / `.csv`, at most 5 MB and 5,000 rows), `sheet_name` (`.xlsx`, default the first sheet). Checked against the login's vendor (a `vendor_id` sent is ignored). Saves nothing. Returns `{rows: [{row, name, date, phone, city, errors, duplicate}], valid_count, invalid_count, duplicate_count}`. File too large: 413 |
| `POST /leads/bulk` | vendor | `{leads: [{name, date, phone, city}]}` (at most 5,000). Validates each lead again, saves the valid ones, skips duplicates. Returns `{added, duplicates_skipped, failed, errors: [{index, field, message}]}` |
| `POST /leads` | vendor | One lead: `{name, date, phone, city}`. 201 with the lead; 409 duplicate; 422 field errors |
| `GET /leads` | all | The list with [the filters below](#filters-list-stats-and-export), `page`, `page_size` (10, 25, 50 or 100, default 10; others: 422). Returns `{items (each with sl_no), total, page, page_size, pages}` |
| `GET /leads/stats` | all | Header numbers over the same filtered leads: `{total, by_source, by_status (incl. pending), follow_ups_due, kpis}` ([KPIs](#kpis-the-same-six-everywhere)); admins also `by_vendor` |
| `GET /leads/export` | all | The same filtered leads as a file (`format=xlsx` (default) or `csv`), see [Export](#export-1) |
| `GET /leads/template` | any login | The upload template (`format=xlsx` or `csv`): Name, Date, Phone, City and one example row, dates `DD-MM-YYYY` |
| `GET /leads/{id}` | all | One lead (another vendor's: 404) |
| `PATCH /leads/{id}` | admin | Any of `update_status`, `additional_comments` (≤ 5,000), `follow_up_date`, `quoted_amount`, `conversion_amount` (rupees, 0–100,000,000). Vendors: 403. Only the fields sent change; `null` clears one (`update_status: null` = back to pending). Records `status_updated_by` / `status_updated_at` |
| `GET /vendors` | admin | Every vendor (active and inactive) with `lead_count`: `[{id, name, is_active, lead_count}]` |
| `POST /vendors` | admin | `{name}`. 409 if the name exists, ignoring case. The vendor gets this CRM's defaults (no areas, not fallback, priority 100); edit them on the Vendors page (`PATCH /api/v1/vendors/{id}`) |
| `PATCH /vendors/{id}` | admin | `{name?, is_active?}`. 409 on a duplicate name |
| `PUT /vendors/{vendor_id}/users/{user_id}` | admin | Links an existing vendor login to a vendor, or moves it. Returns `{user_id, vendor_id, vendor_name}`. 400 if the user isn't a vendor login |

**Lead object:**
- Everyone gets `id, vendor_id, vendor_name, lead_date, name, phone, city, source, created_by,
  created_at, updated_at, update_status (null = pending), additional_comments, follow_up_date,
  quoted_amount, conversion_amount (rupees, null = not set), status_updated_at,
  status_updated_by_name` (the name of who last changed the update or the amounts).
- Admins also get `status_updated_by` (that login's user id).

### Filters (list, stats and export)

`GET /leads`, `GET /leads/stats` and `GET /leads/export` share one filter definition. For the
same parameters, the list `total`, the stats `total` and the export's rows are equal.

| Parameter | Who | Meaning |
|---|---|---|
| `search` | all | Name, city or phone (also by its digits: `98450 12345`) |
| `date_from`, `date_to` | all | **Arrival** (`created_at`), IST days, inclusive. The rules of the other lists: one date = that day, no future dates, at most 366 days (400 otherwise) |
| `lead_date_from`, `lead_date_to` | all | The lead's own date, inclusive (400 if from is after to) |
| `vendor_id` (repeatable) | admin; ignored for vendors | |
| `update_status` (repeatable, incl. `pending`) | all | |
| `follow_up_from`, `follow_up_to` | all | Follow-up date range (400 if from is after to) |
| `follow_up_due=true` | all | Follow-up date today or earlier, open leads only (not converted, not converted, purchased elsewhere, purchased with us or not interested) |
| `city` | all | Contains, ignoring case |
| `source` (repeatable) | all | `bulk_upload` / `manual` |
| `hot` | all | `yes` / `no`: marked hot or not hot |
| `quote` | all | `has`, `none`, `gt:<amount>` or `lt:<amount>` on `quoted_amount` |
| `conversion` | all | `has`, `none`, `gt:<amount>` or `lt:<amount>` on `conversion_amount` |
| `also_in` | all | `yes` / `no`: the phone also exists in one of the 4 sources (vendors: their own leads there); see [Cross-source duplicates](#cross-source-duplicates-also_in). List items carry `also_in` |
| `sort`, `order` | all | `created_at` (default), `updated_at`, `lead_date`, `name`, `city`, `phone`, `vendor_name`, `update_status`, `follow_up_date`, `is_hot` (hot first). Empty values last |

### Export

`GET /leads/export?format=xlsx|csv` exports every lead matching the filters, in list order,
with no paging (50,000 at most; 400 above that).

- **Columns:**
  - Admins: Sl. No, Vendor, Date, Name, Phone, City, Update, Additional comments, Follow-up date,
    Quoted amount, Conversion amount, Updated by, Updated at, Hot.
  - Vendor logins: the same **without Vendor** (their own leads only). Updated by is a name, never
    an email.
- **Formats**, as this CRM's list screens and the other exports:
  - dates `13 Dec 2026` (Excel date cells, `dd mmm yyyy`);
  - date-times `14 Nov 2026, 10:30 am` in IST;
  - phones `+91XXXXXXXXXX` as text;
  - amounts as rupees: Excel number cells with a ₹ format (`₹1,50,000`), CSV plain numbers (`150000`);
  - Update as readable labels (`Pending`, `Converted`, `Already purchased with us`…).
- **Formula safety:** text never becomes a formula; in CSV, text starting with `=`, `+`, `-`, `@`
  gets a leading `'`.
- **File:** `Content-Disposition: attachment; filename="vendor_to_koc_leads_YYYY-MM-DD.xlsx"`
  (vendor logins: `<vendor>_leads_…`), with `Cache-Control: no-store`.

### Dashboard: `GET /api/v1/analytics/vendor-to-koc`

The same period rules as `/analytics/overview` ([Dashboard](#9-dashboard-date-filter)):
`date_from` / `date_to` (IST), `period=all`, the default last 30 days, the previous period, and the
same 400 errors. A lead's arrival date is its `created_at`. Optional `sources` picks the Top cities
sources (default `vendor_to_koc`; see [Top cities sources](#top-cities-sources-sources)). Results are
cached per viewer, period and source selection.

| Field | Who | Covers |
|---|---|---|
| `period` | all | As in the overview (`granularity` day / week / month) |
| `received` | all | Leads that arrived in the period |
| `change_pct` | all | `received` vs the previous period; `null` for all time or with 0 before |
| `kpis` | all | [The six KPIs](#kpis-the-same-six-everywhere) over the period's leads (vendors: their own). `average_per_day` was removed |
| `inflow`, `inflow_vendors` | all | Points `{date, end, total, vendors: {"<vendor id>": n}}` plus the vendor legend `[{id, name}]` |
| `previous_received` | admin | Leads in the previous period |
| `pending`, `converted`, `converted_pct`, `purchased_with_us` | all | Over the period's leads (`converted_pct` of `received`, 1 decimal) |
| `by_status` | all | `pending` plus every status |
| `per_vendor` | admin | `[{vendor_id, vendor_name, leads, interested, converted, pending, converted_pct, average_days_to_update}]`. The last field is the average days from `created_at` to `status_updated_at`, over leads with a status |
| `funnel` | all | `{received, updated (has a status), engaged (interested, will call back or converted), converted}`; `purchased_with_us` is separate |
| `top_cities`, `top_cities_sources`, `top_cities_no_city`, `top_cities_no_city_by_source`, `by_source` | admin | Top cities covers the period's leads of the selected `sources` (default Vendor → KOC only, every vendor); `top_cities_sources` lists the sources included |
| `latest` | admin | The 6 newest leads of the period (lead objects) |
| `follow_ups_due` | all | **Not limited to the period:** open leads with a follow-up date today or earlier |
| `next_follow_ups` | all | **Not limited to the period:** up to 10 open leads by follow-up date, earliest first, overdue included |
| `not_filtered` | all | `["follow_ups_due", "next_follow_ups"]` |

Vendor logins get every "all" field above, **over their own leads only** (follow-ups too); their
lead objects in `next_follow_ups` have no `status_updated_by`. `previous_received`, `per_vendor`,
`top_cities`, `by_source` and `latest` are left out for them.

---

## 12. Users and vendor logins

- **`POST /api/v1/users`** (admin) accepts `vendor_id` for vendor logins (optional):
  - with it, the login is linked to that vendor (404 if the vendor doesn't exist);
  - without it, the login is created unlinked and gets 403 on lead endpoints until it's linked
    (`PUT /api/v1/vendor-to-koc/vendors/{vendor_id}/users/{user_id}`);
  - an admin, Dhruva or view-only login with `vendor_id` gets 422.
- **View-only logins** (`role: "viewer"`, shown as "View only"; created by admins with
  `POST /api/v1/users`, no `vendor_id`) read everything an admin reads and change nothing: see
  [View-only role](#16-view-only-role).
- **Dhruva logins** (`role: "dhruva"`, created by admins with `POST /api/v1/users`; several are
  allowed, no `vendor_id`) only use the Dhruva leads section (`/api/v1/dhruva/*`,
  `/api/v1/analytics/dhruva`). Every other endpoint answers
  `403 {"detail": "Dhruva logins can only use the Dhruva leads section."}`: the 4 sources,
  Vendor → KOC (template too), `/analytics/overview`, `/analytics/vendor-to-koc`, vendors,
  branches, users, sheets. Still open to them: `/auth/*` (login, refresh, logout, `me`) and
  `GET /users/{their own id}`. A test walks every route to keep it that way.
- **`UserRead`** (`POST` / `GET /api/v1/users`, `GET /api/v1/users/{id}`) includes `vendor_id` and
  `vendor_name` (`null` for admins and unlinked logins). `GET /api/v1/users` orders by id and takes
  `skip` (≥ 0) and `limit` (1–200).
- **`GET /api/v1/auth/me`** returns `vendor_id` / `vendor_name` as well as `vendor: {id, name}`.
- **`PATCH /api/v1/users/{id}`** (admin) blocks or unblocks a login: `{"is_active": false|true}`
  (the only field; anything else, a missing or `null` value answers 422). Returns the `UserRead`.
  - Blocking revokes all the login's sessions (refresh tokens) at once; its access tokens stop
    working on the next request, and it can't log in (401 `Incorrect email or password`) or refresh.
  - Unblocking lets it log in again; sessions revoked by the block stay revoked.
  - An admin can't block their own login (400 `You can't block your own login`); another admin can
    be blocked. Unknown user: 404.
- **`DELETE /api/v1/vendor-to-koc/vendors/{vendor_id}/users/{user_id}`** (admin) unlinks a vendor
  login from that vendor: 204. The login stays active but gets 403 on lead endpoints until linked
  again with the `PUT` above. 404 if it isn't linked to that vendor.

### Team page: which endpoint for what

| Need | Endpoint |
|---|---|
| List logins with role, `is_active`, `vendor_id` / `vendor_name` | `GET /api/v1/users` |
| Create a vendor login with its vendor | `POST /api/v1/users` with `role: "vendor"`, `vendor_id` |
| Create a view-only login | `POST /api/v1/users` with `role: "viewer"` (no `vendor_id`) |
| Link or move a login to a vendor | `PUT /api/v1/vendor-to-koc/vendors/{vendor_id}/users/{user_id}` |
| Unlink a login | `DELETE /api/v1/vendor-to-koc/vendors/{vendor_id}/users/{user_id}` |
| Block / unblock | `PATCH /api/v1/users/{id}` with `is_active` |

---

## 13. Dhruva leads

Leads that Dhruva logins (`role: "dhruva"`, see [Users](#12-users-and-vendor-logins)) add or
upload. Table `crm_dhruva_leads` (migration `migrations/2026-11-17_dhruva.sql`). Base path:
`/api/v1/dhruva`.

Hot state uses the existing `crm_hot_leads` sidecar with `source="dhruva"` and the Dhruva lead ID
as `lead_key`; no schema migration is needed.

**Who:**

| | Admins | Dhruva logins | Vendor logins |
|---|---|---|---|
| See leads (list, details, stats, export, dashboard) | all | all | 403 |
| Add, upload | ✔ | ✔ | 403 |
| Edit basic fields (name, venue, date, phone, alt phone, event type) | ✔ | ✔ (any lead) | 403 |
| Edit admin fields (`update_status`, `additional_comments`, `quoted_amount`, `conversion_amount`, `follow_up_date`) | ✔ | **403** (read-only) | 403 |
| Archive / unarchive | ✔ | 403 | 403 |
| Mark / unmark hot | ✔ | ✔ | 403 |

### Lead object

`id, name, venue, event_date (YYYY-MM-DD or null), event_date_text (as typed), phone, alt_phone,
phone_e164, alt_phone_e164, event_type, source (manual | bulk_upload), created_by_name, created_at,
updated_at, archived, is_hot, update_status (null = Pending), additional_comments, quoted_amount,
conversion_amount, follow_up_date, status_updated_at, status_updated_by_name`. Dhruva logins and
admins get `status_updated_by` (user id) on list/detail responses and in the export; admins also get
`created_by`. The status and amount fields remain read-only for Dhruva logins.

### Values (upload, add and edit)

| Field | Rule |
|---|---|
| `name`, `venue` | Whitespace and line breaks collapsed to single spaces ("Main Palace \n" → "Main Palace"). At most 200 / 300 characters |
| `event_type` | Trimmed; line breaks become `, ` ("engagement \nwedding" → "engagement, wedding"). At most 200 |
| `phone`, `alt_phone` | A cell may hold two numbers split by a line break, `,`, `/` or `;`: the first is `phone`, the second `alt_phone`. Excel numbers become text. `nil`, `NA`, `-` or empty = no phone. Indian numbers may skip `+91`; others need their country code. More than two numbers, or a number that isn't valid: error. Stored as written plus E.164 |
| `date` | Kept as typed (`event_date_text`, spacing collapsed). `event_date` is filled when the text is one clear date: a real Excel date, `DD-MM-YYYY`, `YYYY-MM-DD`, or one day and one month in any order ("Dec 4th", "12th Dec", "Feb 28th, Tentative date", "April 23rd 2027"). **Without a year: the next time that day comes** (today included), counted from the day it's entered, and not recalculated later. Text only (no `event_date`): a month alone ("March"), ranges and choices ("Jan 22nd -24th 2027", "Feb 24&25th 2027", "Dec 5th or 6th", "10th & 11th Feb 2027"), "Nil". An impossible date (`31-02-2026`): error |
| A lead | Needs a name or a phone. **Without a phone it's allowed** and flagged `no_phone` |

**Duplicates** (archived leads count): a lead is a duplicate only when its phone or alt phone in
E.164 form matches another lead's phone or alt phone **and** its name, venue, date text, and event
type all match after trimming, collapsing whitespace, and ignoring case. This applies to an earlier
row of the same file/request (`in_file`) and saved leads (`existing`). A lead without a phone is not
a duplicate. `allow_duplicate: true` skips the duplicate rejection on single add and edit.

### Endpoints

| Method and path | Who | What |
|---|---|---|
| `POST /upload/preview` | admin, Dhruva | `multipart/form-data`: `file` (`.xlsx` / `.csv`, ≤ 5 MB, ≤ 5,000 rows), `sheet_name` (`.xlsx`; default the first sheet; matched exactly, else ignoring case and outer spaces). Saves nothing. See below |
| `POST /leads/bulk` | admin, Dhruva | `{leads: [{name, venue, date, phone, alt_phone, event_type}]}` (≤ 5,000; usually the preview rows' `values`, edited). Checks every lead again, saves the valid ones (`source: bulk_upload`), skips duplicates. Returns `{added, duplicates_skipped, failed, errors: [{index, field, message}], ids}` |
| `POST /leads` | admin, Dhruva | One lead (same fields, plus `allow_duplicate`). 201 with the lead; 422 `[{field, message}]`; 409 `{message, duplicate}` unless `allow_duplicate: true` |
| `GET /leads/template` | admin, Dhruva | `format=xlsx` (default) or `csv`: Name, Venue, Date, Phone, Alt phone, Event type and one example row (uploaded unchanged, it's an error: "This is the example row from the template; delete it") |
| `GET /leads/{id}` | admin, Dhruva | One lead (archived ones too). 404 if unknown |
| `PATCH /leads/{id}` | admin, Dhruva | Only the fields sent change; `null` clears one. Basic fields: both (the same duplicate rule as above is rechecked when basic fields change; 409 unless `allow_duplicate`). Admin fields: admins only; Dhruva logins get `403 "Only admins can change …"` and nothing is saved. Admin fields record `status_updated_by` / `status_updated_at`. Unknown fields: 422 |
| `POST /leads/{id}/archive`, `/unarchive` | admin | Archived leads leave the list, stats, export and dashboard but still count as duplicates |

**Upload preview response:**

```json
{
  "sheet": "Oct - Dec ", "sheets": ["Oct - Dec ", "Dec -March 2027"], "heading_row": 2,
  "columns": {"Name": "name", "Venue": "venue", "Date": "date", "Contact No": "phone", "Event Type": "event_type"},
  "ignored_columns": [],
  "rows": [
    {"row": 4, "status": "ok", "flags": [], "errors": [], "duplicate": null, "event_date": "2026-12-25",
     "values": {"name": "Ravi Kumar", "venue": "Main Palace", "date": "dec 25th", "phone": "9800000002",
                "alt_phone": "9000000002", "event_type": "engagement, wedding"}},
    {"row": 6, "status": "ok", "flags": ["no_phone"]},
    {"row": 13, "status": "duplicate", "duplicate": {"reason": "in_file", "duplicate_of_row": 3}},
    {"row": 16, "status": "error", "errors": [{"field": "phone", "message": "Not a valid phone number"}]}
  ],
  "counts": {"total": 13, "valid": 11, "needs_fixing": 1, "duplicates": 1, "no_phone": 3,
             "duplicates_by_reason": {"in_file": 1, "existing": 0}}
}
```

(Rows shortened; every row has all the keys.)

- **Heading row:** found automatically. Title rows above it ("Updated on 12th Nov") and blank rows
  are skipped: it's the first row, within the first 15, where at least two cells are known headings.
- **Headings,** ignoring case, spaces and punctuation: `SL NO` / `SLNO` (ignored), `Name`, `Venue`,
  `Date` / `Event date`, `Contact No` / `MOBILE NO` / `Mobile` / `Phone`, `Alt phone` /
  `Alternate phone`, `Event Type`. Other columns are listed in `ignored_columns`. The file needs a
  Name or a phone column (400).
- **Rows:** empty rows (or rows with only SL NO) are skipped; `row` is the Excel row number.
  `status`: `ok` (will be saved), `error` (needs fixing: edit `values` and send them to the bulk
  save), `duplicate` (`duplicate.reason`: `in_file` with `duplicate_of_row`, or `existing` with
  `lead_id`). `no_phone` rows are `ok` unless something else is wrong.
- **Errors:** 400 for an unreadable file, a missing sheet (the message lists the sheets) or no
  heading row; 413 above 5 MB.

### List, stats, export, filter options

`GET /leads`, `GET /leads/stats` and `GET /leads/export` share one filter definition: for the same
parameters the list `total`, the stats `total` and the export's rows are equal. Archived leads are
left out. Admins and Dhruva logins: all leads; vendor logins: 403.

| Parameter | Meaning |
|---|---|
| `search` | Name, venue, event type, date text, phone or alt phone as written, and phone digits (`98000 00002` finds `+919800000002`) |
| `date_from`, `date_to` | **Received** (`created_at`), IST days, inclusive, the rules of the other lists (no future dates, at most 366 days) |
| `event_from`, `event_to` | Event date, `YYYY-MM-DD`, inclusive. Text-only dates never match |
| `upcoming_days` | Event from today (IST) to today + N, N from 1 to 366 (not with `event_from` / `event_to`) |
| `venue` | Venue contains, ignoring case ("main palace" finds "Main Palace" and "Main palace") |
| `event_type` (repeatable) | Event type contains, ignoring case; repeated values: either ("muhurtham" finds "reception, muhurtham") |
| `update_status` (repeatable) | The 7 Update values, or `pending` (no update yet) |
| `quote`, `conversion` | Quoted / conversion amount: `has`, `none`, `gt:<amount>`, `lt:<amount>` |
| `follow_up_from`, `follow_up_to` | Follow-up date range |
| `follow_up_due=true` | Follow-up date today or earlier, open leads only (not converted, not converted, purchased elsewhere, purchased with us or not interested) |
| `has_phone` | `yes` / `no` (no phone and no alt phone) |
| `hot` | `yes` / `no`: marked hot or not hot |
| `also_in` | `yes` / `no`: the phone or alt phone also exists in another section (see [Cross-source duplicates](#cross-source-duplicates-also_in)); admins only see other sections, so for Dhruva logins `yes` finds nothing |
| `sort`, `order` | `created_at` (default, `desc`), `updated_at`, `event_date`, `name`, `venue`, `event_type`, `update_status`, `follow_up_date`. Empty values last |
| `page`, `page_size` | List only. `page_size` 10, 25, 50 or 100 (default 10; others 422) |

Errors (400): event or follow-up `from` after `to`, `upcoming_days` with event dates or outside
1–366, a bad `quote` / `conversion`, an event date not `YYYY-MM-DD`, and the received-date rules
of the other lists. Unknown `update_status` / `sort`: 422.

| Method and path | Returns |
|---|---|
| `GET /leads` | `{items (lead objects with sl_no and `also_in`), total, page, page_size, pages}` |
| `GET /leads/stats` | `{total, kpis, by_status (pending + the 7 values), no_phone, follow_ups_due, upcoming_30_days}`; `kpis` is [the same object as the other lists](#kpis-the-same-six-everywhere) |
| `GET /leads/export` | `format=xlsx` (default) or `csv`, every matching lead in list order (50,000 at most; 400 above). Includes a `Hot` column. File `dhruva_leads_YYYY-MM-DD.<ext>` |
| `POST /leads/{id}/hot`, `DELETE /leads/{id}/hot` | admin, Dhruva | Mark or unmark an active lead; returns `is_hot`, marker and timestamp. Archived or unknown lead: 404; vendor login: 403. Activity records `hot_on` / `hot_off` with the actor role |
| `GET /leads/filter-options` | `{venues, event_types, updates, has_phone, sources}`, each `[{value, count}]` over every listed lead. Venues and event types: spellings that differ only in case or spacing are one value, shown as written most often; an event type like "engagement, wedding" counts for both |

**Export columns** (the same for admins and Dhruva logins, who see the admin fields): Sl. No, Name,
Venue, Date (the date text as stored), Phone, Alt phone (E.164,
or as written when unreadable), Event type, Update (`Pending`, `Converted`, …), Additional
comments, Follow-up date, Quoted amount, Conversion amount (₹ number cells; CSV plain numbers),
Received, Updated by (a name), Updated by ID (`status_updated_by`), Updated at (`status_updated_at`,
IST), Hot. Dhruva logins receive admin fields read-only; PATCH of admin fields remains 403. Formats and
formula safety as the other exports.
An export preserves the identifying fields so rows with a phone upload back as duplicates; leads
without a phone are never duplicates.

### Dashboard: `GET /api/v1/analytics/dhruva`

Admins and Dhruva logins (read-only; the same numbers); vendor logins: 403. The period rules of
the other dashboards ([Dashboard](#9-dashboard-date-filter)): `date_from` / `date_to` (IST, at most
366 days, not in the future), `period=all`, the same 400 errors, **but all time when no dates are
sent**. A lead's received date is its `created_at` (IST day). Archived leads aren't counted. Every
number covers the leads received in the period.

| Field | Covers |
|---|---|
| `period` | As in the overview (`all_time`, `granularity` day / week / month, `previous`) |
| `received` | Leads received in the period |
| `previous_period`, `change_pct` | Leads in the same number of days right before, and the % change; `null` for all time (or with 0 before, `change_pct`) |
| `kpis` | [The six KPIs](#kpis-the-same-six-everywhere) plus `pending`, `upcoming_30_days` (events from today to today + 30), date-filtered `hot_count`, and `conversion_amount_count` / `conversion_amount_total` for converted leads with an amount |
| `by_event_type` | `[{value, count}]`; "engagement, wedding" counts for both; values lower-case |
| `top_venues` | Top 10 `[{value, count}]`; spellings that differ only in case or spacing are one venue |
| `events_by_month` | `[{month: "2026-12", count}]` by event date; `no_event_date`: leads whose date is text only or empty |
| `inflow` | `[{date, end, total}]` by received date: days, 7-day buckets over 90 days, months for all time |
| `by_status` | `pending` plus the 7 Update values |
| `latest` | The 6 newest leads of the period (lead objects) |

## 14. Notifications and hot leads

Notifications are available to admins and linked vendor logins. Dhruva logins receive 403. Vendor
assignment notifications are shared with every login linked to that vendor; each login has its own
read state. Vendor-added KOC notifications are addressed to each admin login, excluding the actor.
Expired notifications and their read records are cleaned up when notification endpoints or
notification creation run; retention is 90 days.

| Method and path | Who | What |
|---|---|---|
| `GET /notifications` | admin, vendor | Newest first, paged with `page`, `page_size`; optional `unread_only=true` and `type=vendor_assigned|vendor_lead_added`. Items include `id`, `type`, `title`, `body`, `created_at` (UTC ISO 8601), `read`, `source`, `lead_key`, `lead_id`, and `lead_ref` for navigation |
| `GET /notifications/unread-count` | admin, vendor | `{unread_count}` for this login (vendor broadcasts are read per login) |
| `POST /notifications/{id}/read` | admin, vendor | Mark one notification read; another recipient's notification is 404 |
| `POST /notifications/read-all` | admin, vendor | Mark all visible notifications read |

Notification types are `vendor_assigned` (an admin assigns or reassigns a lead to a vendor; only
the newly assigned vendor is notified) and `vendor_lead_added` (a vendor adds a lead to KOC). A
bulk KOC upload creates one summary notification per admin only when at least one lead was saved.
No notification is created for a Dhruva lead action. Notification failures are logged and do not
fail the lead action.

Hot flags appear as `is_hot` on every source list item and lead object. `hot=yes|no` is supported
by all four source lists and their stats/exports, and by Vendor → KOC list, stats and export.
Exports include a `Hot` column. For Spin workflow rows the flag lives in the writable
`crm_hot_leads` sidecar; the workflow database remains read-only. Spin, International, Wedding and
Wedding Planner use that sidecar too. Vendor → KOC stores `is_hot`, `hot_marked_by` and
`hot_marked_at` on `crm_vendor_to_koc_leads`.

| Method and path | Who | What |
|---|---|---|
| `POST /leads/{source}/{lead_key}/hot` | admin, visible vendor | Mark a source lead hot; response includes `is_hot`, marker user and timestamp |
| `DELETE /leads/{source}/{lead_key}/hot` | admin, visible vendor | Clear the hot flag |
| `POST /vendor-to-koc/leads/{id}/hot` | admin, owning vendor | Mark a KOC lead hot |
| `DELETE /vendor-to-koc/leads/{id}/hot` | admin, owning vendor | Clear the hot flag |

Admins can toggle any listed lead. Vendor logins can toggle only leads visible to that vendor;
other vendors receive 404. Dhruva logins receive 403. Apply
`migrations/2026-11-19_notifications_hot.sql` to the CRM MySQL database before deploying; do not
apply it to the read-only Spin workflow database. `create_all()` creates the new tables at startup,
but does not add the Vendor → KOC columns to an existing table.

## 15. Activity and follow-up reminders

Activity is recorded for app-created leads, app field edits, real vendor assignment changes,
archive/unarchive and hot on/off actions. Unchanged fields are omitted. Activity writes are
best-effort and a logging failure does not fail the lead action. Vendor users see only fields
visible to them in the section; assignment entries are hidden. Dhruva users can read all activity
for Dhruva leads, including admin-field changes. Sheet sync updates and edits made directly in
Google Sheets are not logged.

| Method and path | Who | What |
|---|---|---|
| `GET /leads/{source}/{lead_key}/activity` | admin, vendor who can see the lead | Newest first; `page` (default 1), `page_size` (default 25, max 100); items include `actor_name`, `actor_role`, `action`, `field`, `old_value`, `new_value`, `created_at` (ISO UTC) |
| `GET /vendor-to-koc/leads/{id}/activity` | admin, owning vendor | Same paging and item shape; vendor sees lead fields and KOC fields shown read-only |
| `GET /dhruva/leads/{id}/activity` | admin, Dhruva | Same paging and item shape; Dhruva activity is read-only |
| `GET /reminders/follow-ups?days=7&sources=wedding,vendor_to_koc` | admin, vendor | Optional comma-separated `sources` subset (`spin`, `whatsapp`, `wedding`, `wedding_planner`, `vendor_to_koc`, `dhruva`, `koc_to_dhruva`; vendors never get `koc_to_dhruva`); items, counts and the days window apply only to requested sources. Vendors remain scoped to their own visible leads. Omit `sources` for the existing default. Bad values: 400 `Unknown source(s): <names>. Allowed: …` |
| `GET /dhruva/reminders/follow-ups?days=7&sources=dhruva,koc_to_dhruva` | admin, view only, Dhruva | Both Dhruva directions: Dhruva → KOC (`source: "dhruva"`) and KOC → Dhruva (`source: "koc_to_dhruva"`); optional `sources` narrows to one (other values: 400). Dhruva logins do not receive the admin-only status field |

Reminder items contain `source`, `lead_key`, `name`, `phone`, `follow_up_date`, and `days_overdue`
(positive overdue, zero today, negative upcoming). Admins also receive `update_status` and, outside
Dhruva, `vendor_name`. Archived, converted and not-interested leads are excluded. Vendors see only
their assigned CRM leads and their own Vendor → KOC leads. The general reminders endpoint returns
403 to Dhruva logins. Spin workflow names and phones are loaded in batches of at most 200 items.

Follow-up dates for Spin, International, Wedding and Wedding planner live in `crm_lead_meta`.
Apply the reviewed migration once to the CRM MySQL database; do not apply it to the read-only Spin
workflow database:

```powershell
mysql --host=<host> --user=<user> --password <database> < migrations/2026-11-19_lead_activity.sql
```

The migration's `ALTER TABLE crm_lead_meta ADD COLUMN follow_up_date` runs only once.
`create_all()` creates `crm_lead_activity` at startup, but does not add columns to an existing table.

---

## 16. View-only role

A view-only login (`crm_users.role = "viewer"`, shown in the UI as **View only**) sees the same data
as an admin and can't change anything. `crm_users.role` is a plain `VARCHAR(20)`, so no migration
is needed.

- **Creating one:** admins only, `POST /api/v1/users` with `role: "viewer"` and no `vendor_id`
  (`vendor_id` → 422). Block / unblock with `PATCH /api/v1/users/{id}` works as for any login.
- **Reads:** the same responses as an admin, admin-only fields included: the 4 sources (lists,
  stats, filter options, `also_in`), lead details, CRM details, activity logs, Vendors → KOC
  (leads, `status_updated_by`, vendors), Dhruva (admin fields, reminders), all three dashboards
  (`?refresh=true` allowed: it only clears the in-memory cache), `/reminders/follow-ups`, GET
  exports and templates, `GET /crm-leads/{source}/fields`, `GET /crm-leads/{source}/{lead_key}`,
  `GET /vendor-to-koc/vendors`, `GET /vendors`, `GET /vendors/stats`, `GET /branches`.
- **Denied reads (403):** `GET /users` (Team), `/sheets/*` (Settings) and `/read/*` (database
  explorer). `GET /users/{id}` works for their own id only (other ids: 404, as for vendor logins).
  `GET /auth/me` returns `role: "viewer"` and `vendor: null`.
- **Writes:** every `POST`, `PUT`, `PATCH` and `DELETE` answers
  `403 {"detail": "View-only logins cannot change data."}`, before the body is validated or the
  lead is looked up. The only exceptions are `POST /auth/login`, `/auth/refresh` and
  `/auth/logout`. The check sits on the whole API router, so new write endpoints are covered
  automatically; a test sends every write operation in the OpenAPI schema as a viewer.
- **Notifications:** `GET /notifications` is always empty (`items: []`, `total: 0`) and
  `GET /notifications/unread-count` is `{"unread_count": 0}`. The expired-notification cleanup
  doesn't run for them.
- **Frontend:** a viewer's `403` with the text above can be shown as a friendly "View only" toast.

### Who can do what

| Action | Admin | View only | Vendor | Dhruva |
|---|---|---|---|---|
| Read the 4 sources, lead details, CRM, activity | all leads | all leads (admin fields) | own leads | 403 |
| Read Vendors → KOC leads and dashboard | all, admin fields | all, admin fields | own, no admin fields | 403 |
| Read Dhruva leads, dashboard and reminders | ✔ admin fields | ✔ admin fields | 403 | ✔ no admin fields |
| `/analytics/overview`, `/reminders/follow-ups` | ✔ | ✔ (same as admin) | own leads | 403 |
| GET exports and templates | ✔ | ✔ | own leads | Dhruva only |
| `GET /crm-leads/{source}/fields`, `/{lead_key}`, `GET /vendors/stats`, `GET /vendor-to-koc/vendors` | ✔ | ✔ | 403 | 403 |
| `GET /users`, `/sheets/*`, `/read/*` | ✔ | 403 | 403 | 403 |
| Notifications | own inbox | always empty | vendor inbox | 403 |
| Add, import, edit, archive leads; CRM details; hot | ✔ | 403 | own leads (CRM details, hot, Vendors → KOC add) | Dhruva leads |
| Vendors, branches, vendor links, sheet sync | ✔ | 403 | 403 | 403 |
| Create users, block / unblock any login | ✔ | 403 | 403 | 403 |
| Login, refresh, logout, `GET /auth/me` | ✔ | ✔ | ✔ | ✔ |

---

## 17. Spin: cut-off, gift leads and history upload

### Cut-off (`SPIN_LIVE_FROM`)

- The setting `SPIN_LIVE_FROM` (`.env`, an India date `YYYY-MM-DD`, default `2026-11-21`) is when
  Spin goes live. Spin shows the workflow rows (listed, as before) **created on or after 00:00 IST
  that day** (`created_at` is UTC: `2026-11-20 18:30:00`), plus **every** CRM spin lead
  (`crm_spin_leads`: added or uploaded, any date). Restart the app after changing it.
- Workflow rows created before the cut-off are hidden from Spin everywhere: list, stats, export,
  filter options, `/analytics/overview`, vendor statistics, reminders, `also_in`, vendor visibility.
  `GET`/`PUT /leads/spin/{key}/crm`, hot and activity on such a key answer `404`, for every role.
  Their CRM rows (`crm_lead_meta`, hot, activity) stay in the database, hidden.
- **KOC → Dhruva is not affected:** it keeps reading every workflow row of any date (section 18).
- Numbers on 2026-11-21 (read-only): 1,667 listed workflow rows before the cut-off (1,523 + 144 gift
  leads), 5 after; 13 hidden leads had CRM data (11 `crm_lead_meta`, 2 hot, 6 activity entries).

### Gift leads (back in Spin)

The separate `wedding_gifts` source is gone (`/wedding-gifts-leads`, `/crm-leads/wedding_gifts/*`,
`/leads/wedding_gifts/*` and `sources=wedding_gifts` answer 404 / 422 / 400). A **gift lead** is a
spin lead whose Other wedding services (`otherWeddingServices`) contains
`Wedding Gifts & Return Gifts` (case-insensitive, also inside a comma-separated combination). It is
listed, counted and exported as Spin; only its automatic vendor differs:

1. a manual choice wins (`vendor_source: "manual"`);
2. "Unassigned" (uploaded unassigned, `auto_assign = false`) stays unassigned;
3. otherwise the **active vendor named `WEDDING_GIFTS_VENDOR_NAME`** (default `Kraftyz`, exact name,
   any case): `vendor_source: "source_default"`, chip "Kraftyz · auto". No such vendor (or
   inactive): unassigned. Branch and area rules never apply to gift leads.

Every other spin lead follows the branch and area rules as before. Kraftyz's logins see and update
their gift leads. Migration: `migrations/2026-11-21_wedding_gifts_back_to_spin.sql` moves any CRM
row still under `wedding_gifts` back to `spin` (preview, conflict checks, rollback); on 2026-11-21
there were none (the old migration was never applied, and it was deleted).

### History upload (`POST /crm-leads/spin/import`)

- **Duplicate check:** workflow rows created before the cut-off are ignored, so the history can be
  uploaded. Rows on or after it (listed or not) are still `existing_workflow` duplicates, and every
  CRM spin lead (archived too) is still `existing_added`. Other sources' uploads no longer report
  `other_source: spin` for hidden rows.
- **Assigned to:** a vendor name → that vendor (manual); `Automatic` → Kraftyz for gift leads,
  branch / area rules for the rest; empty or `Unassigned` → unassigned.
- **Columns:** the template and the export gain **Other wedding services** and **Quantity
  required** (after Budget); both are stored with the lead (`additional_details`), so an exported
  file uploads back. Budget is the estimated budget per card (`Estimated budget per card` is
  accepted as its header). `Created` keeps the history date (empty = now).
- Uploaded leads are never in KOC → Dhruva.

### Other wedding services in the list

- `GET /spin-leads` items already carry `additional_details.otherWeddingServices` (the column).
- Filter `other_services` (text, contains, any case; max 200), on `GET /spin-leads`, `/stats` and
  `/crm-leads/spin/export` alike, e.g. `?other_services=Wedding%20Gifts%20%26%20Return%20Gifts`.
- `GET /spin-leads/filter-options` adds `other_services`: `[{value, count}]`, each individual
  service (values split on commas) with its number of leads, most common first.

### Re-key the hidden leads' CRM data (after the history upload)

`scripts/rekey_spin_history.py` matches each hidden workflow lead that has CRM data
(`crm_lead_meta`, `crm_hot_leads`, `crm_lead_activity`, plus `crm_notifications` links) to its
uploaded copy in `crm_spin_leads` by phone (last 10 digits) and moves the rows to the new `crm-<id>`
key.

```
venv\Scripts\python scripts\rekey_spin_history.py                                  # preview (read-only)
venv\Scripts\python scripts\rekey_spin_history.py --apply --undo-file rekey_undo.sql
```

- **Preview (default):** read-only. Lists each match (old key → new key, phone's last 4 digits) with
  what would move and merge, and the leads skipped: no uploaded copy, 2+ copies with the phone, a
  copy claimed by 2+ hidden leads, not in the workflow database. Live leads are never touched.
- **Merge:** when the upload already created CRM details for the new key, a field the upload filled
  is never overwritten ("keep upload … (old …)"); an empty one is filled from the old row
  ("merge …"). Assigned to counts as filled when the upload named a vendor or said Unassigned.
  A hot flag moves unless the new lead is already hot; activity and notifications always move.
- **Apply:** one MySQL transaction; it re-plans, checks nothing changed since, moves the rows,
  writes the undo SQL (`--undo-file`, required, must not exist) and commits. Any error or conflict
  rolls everything back. To undo later, run the undo file in MySQL.

---

## 18. KOC → Dhruva

A section of the Dhruva flow, source key `koc_to_dhruva` (label **KOC → Dhruva**): the spin leads
KOC shares with Dhruva by rule. Dhruva works these leads (the receiver edits), as admins work the
Dhruva → KOC leads.

- **Rule:** every workflow `spin_lead` row (listed as Spin or not: `otherWeddingServices` doesn't
  matter; **any date**: the spin cut-off doesn't apply) where the first number of `estimatedBudgetPerCard` is
  **≥ 70** (`70-100`, `70_100`, `100-200`, `500+`, `500_PLUS`, `400` …; `50_100` and `UNDER_50`
  count as 50), **or** `quantityRequired` is **≥ 1000** (also when the budget is empty or below 70).
  `reason` is `budget` ("Budget ≥ ₹70", wins when both match) or `quantity` ("Quantity ≥ 1000").
- **CRM spin leads are never included** (added or uploaded, whatever their budget or quantity).
- **In both places:** leads on or after the spin cut-off are in Spin too. This section keeps its own
  CRM details (`crm_lead_meta` with `source = "koc_to_dhruva"`), hot flags and activity, so a Dhruva
  Update or quote never changes the Spin record, and the other way round. Lead keys are the same
  (`spin_lead` id).
- **Never assigned:** no vendor, no automatic rules (`crm.vendor` is always `null`).
- **No add or upload:** rows come only from the rule. No migration was needed.

### Who

| | Admin | View only | Dhruva login | Vendor |
|---|---|---|---|---|
| List, stats, filter options, export, lead, activity, dashboard | ✔ | ✔ | ✔ | 403 |
| `PUT …/crm`, hot | ✔ | 403 (view only) | ✔ | 403 |
| `also_in` entry "KOC → Dhruva" on other sections' leads | ✔ | ✔ | — | never |

### Endpoints (prefix `/api/v1/koc-to-dhruva`)

| Need | Endpoint |
|---|---|
| List, stats, filter options | `GET /leads`, `/leads/stats`, `/leads/filter-options` |
| Export (exactly the list) | `GET /leads/export?format=xlsx\|csv` |
| One lead | `GET /leads/{lead_key}` (404 when the lead doesn't match the rule) |
| CRM details | `GET`/`PUT /leads/{lead_key}/crm` |
| Hot, activity | `POST`/`DELETE /leads/{lead_key}/hot`, `GET /leads/{lead_key}/activity` |

- **List items** (list, details, export): the Spin list's fields, including the branch exactly as
  Spin gives it (`branch_id`, and `branch`: `{id, name, code, city}` from the Branches page;
  `{id, name: "Branch <id>", code: null, city: null}` for an unknown id; `null` without a branch id,
  no pincode-area fallback), plus `lead_key`, `reason`, `reason_label`, `occasion`,
  `event_date`, `pincode`, `estimated_budget_per_card`, `quantity_required`, `received`, the section's
  `crm` (`update_status`, `notes` = additional comments, `quoted_amount`, `quoted_date`,
  `conversion_amount`, `follow_up_date`), `is_hot` and `also_in`.
- **Filters** (list, stats and export alike): the shared ones (`search`, `date_from`/`date_to` or
  `period` = received, `event_from`/`event_to`/`upcoming_days`, `update_status`, `quote`, `conversion`,
  `follow_up`, `hot`, `also_in`, `origin`), plus `branch`, `pincode`, `budget` (exact), `occasion`
  (contains), `quantity_min` / `quantity_max`, `reason` (`budget` \| `quantity`); `sort` / `order`;
  `page_size` 10, 25, 50 or 100. `vendor_id` and `unassigned` answer 400 (never assigned).
- **Stats:** `{total, kpis, by_reason}`; `total` equals the list's total for the same filters.
- **Filter options:** budgets, pincodes, branches, origins and updates (as Spin), plus `occasions`,
  `reasons` and `quantity_range`.
- **Export columns:** Sl. No, Name, Phone, Email, Occasion, Event date, Branch, Branch code, Estimated
  budget per card, Quantity required, Received, Update, Additional comments, Quoted amount, Quoted
  date, Conversion amount, Follow-up date, Hot. Branch and Branch code are resolved as in the Spin
  export: the Branches page, then the 12 built-in stores; Branch code is code and city
  (`JNG-004 · Bangalore`); empty for an unknown or missing branch id. No Reason or Pincode column
  (the `reason` and `pincode` filters still apply).
- **`PUT …/crm` body** (send only what changes; `null` clears): `update_status` (the same 7 values),
  `notes`, `quoted_amount`, `quoted_date`, `conversion_amount`, `follow_up_date`. Any other field
  (e.g. `vendor_id`): 422. Each change is logged in the activity.

### Dashboard: `GET /api/v1/analytics/koc-to-dhruva`

A separate endpoint (not a parameter on `/analytics/dhruva`: the breakdowns differ). The shared
period rules (`date_from` / `date_to`, `period=all`), **all time by default**; received =
`created_at` (IST day). Admins, view-only and Dhruva logins; vendors 403.

| Field | Covers |
|---|---|
| `received`, `previous_period`, `change_pct` | Leads received in the period, and the comparison |
| `kpis` | [The KPIs](#kpis-the-same-six-everywhere) from this section's CRM details, plus `quotes_count`, `conversion_amount_count` / `conversion_amount_total` (AOV inputs), `hot_count`, `pending` |
| `by_reason` | `[{reason, label, count}]` for `budget` and `quantity` |
| `by_budget_band` | `70-100`, `100-200`, `200-500`, `500+`, `under_70` (quantity-only leads), `not_given` |
| `by_occasion` | Top 10 occasions |
| `inflow` | `[{date, end, total, budget, quantity}]` per day / week / month |
| `by_status` | `pending` plus every Update |
| `top_cities` | City or pincode area, as the other dashboards |
| `latest` | The 6 newest leads (`lead_key`, `name`, `phone`, `occasion`, `reason`, `received`, `update_status`, `is_hot`) |

### Reminders and also_in

- Follow-ups set here appear in `GET /dhruva/reminders/follow-ups` (both Dhruva directions, each item
  with its `source`; `sources=koc_to_dhruva` or `sources=dhruva` narrows it) and, for admins and
  view-only logins, in `GET /reminders/follow-ups` (`sources` accepts `koc_to_dhruva`).
- `also_in`: a lead whose phone belongs to a KOC → Dhruva lead (any date) shows
  `{"source": "koc_to_dhruva", …}` to admins and view-only logins (vendors never). A KOC → Dhruva
  lead shows `spin` only for leads in the Spin list (live workflow rows, CRM spin leads); Dhruva
  logins see no other section here (empty `also_in`).
