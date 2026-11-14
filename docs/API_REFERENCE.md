# CRM leads API: add, import, export, edit, archive

Endpoints for leads that the team adds or imports in the CRM, for all four lead sources, plus
the related change to `PUT /leads/{source}/{lead_key}/crm`.

- Base path: `/api/v1`
- Auth: every endpoint needs an admin access token (`Authorization: Bearer <token>`).
  Missing or invalid token: `401`. Logged in but not an admin: `403 {"detail": "Admin access required"}`.
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
5. [Change to PUT /leads/{source}/{lead_key}/crm](#5-change-to-put-leadssourcelead_keycrm)
6. [Changes to the existing list endpoints](#6-changes-to-the-existing-list-endpoints)
7. [Limits](#7-limits)
8. [File downloads: filenames and CORS](#8-file-downloads-filenames-and-cors)

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
`PUT /leads/{source}/{lead_key}/crm`. The automatic vendor (branch or area rules) is
calculated each time a list loads, the same way as for workflow and sheet leads.

**Phone numbers.** A phone is required in every source. Each number is converted to one
E.164 form (`+919845012345`) for duplicate checks across all sources:

- `spin`, `wedding`, `wedding_planner`: India by default. `9845012345`, `09845012345` and
  `+91 98450 12345` are the same number. Other countries need their country code
  (`+44…` or `0044…`).
- `whatsapp` (international): the number must carry its country code (`+971…`, `00971…` or
  `971…`), or the `country` field must say which country it is from (a name such as
  `United Arab Emirates` or `UAE`, an ISO code such as `AE`, or a dial code such as `+971`).
- Numbers that aren't valid for their country are rejected.

**Archived.** Archiving hides a lead from lists, counts, statistics and exports. Its phone
still counts in duplicate checks, and its CRM details are kept. It can be unarchived.

**Dates and times in requests.**

- Dates: `DD-MM-YYYY` is the main format. `DD/MM/YYYY`, `YYYY-MM-DD`, `20 December 2026` and
  `December 20, 2026` are also accepted. Excel date cells work as they are.
- Date-times: ISO 8601 with an offset (`2026-11-14T05:00:00Z`) or `DD-MM-YYYY HH:MM`. A time
  without a time zone (typed, or from an Excel cell) is read as **India time (IST)**. Times
  are stored in UTC.

---

## 2. Fields per source

Every field can be sent in the JSON `values` of an add/edit request (by **key**) and used as
an upload column (by **label**, key or alias). Header matching ignores case, spaces and
punctuation. `GET /crm-leads/{source}/fields` returns the same definitions.

**Field types**

| type | Accepts | Stored as |
|---|---|---|
| `text` | Any text up to `max_length` | Text |
| `email` | A valid email address | Lower case |
| `phone` | See [Phone numbers](#1-concepts) | As written, plus the E.164 form |
| `country` | Country name, ISO code or dial code | Text |
| `date` | See dates above | Date |
| `date_text` | Any text | The text as written, plus a date when it can be read clearly (no error if it can't) |
| `datetime` | See date-times above. Empty means now. | UTC date-time |
| `month` | `December 2027`, `Dec 2027`, `12/2027`, `2027-12` | `December 2027` |
| `int` | Whole number, commas allowed (`1,50,000`) | Number |
| `bool` | `Yes`/`No`, `Y`/`N`, `true`/`false`, `1`/`0` | Boolean |
| `range` | Any text, for example `10-20` | Text (sorted by its first number) |
| `budget` | `50000`, `50k`, `1.5 lakh` | The text as written, plus the amount in rupees when readable |
| `pincode` | 6 digits | Digits |
| `branch` | Spin only. A branch **name** exactly as on the Branches page (case and punctuation ignored), or in JSON its numeric id | Branch id |

### spin

| Key | Label | Type | Req. | Max | Upload aliases |
|---|---|---|---|---|---|
| `phone` | Phone | phone | ✔ | 20 | mobile, phone number, contact number, whatsapp number |
| `name` | Name | text | | 150 | full name |
| `email` | Email | email | | 150 | email address |
| `occasion` | Occasion | text | | 100 | |
| `branch` | Branch | branch | | | store, branch name |
| `pincode` | Pincode | pincode | | | pin, zipcode, postal code |
| `occasionType` | Occasion type | text | | 100 | |
| `occasionDate` | Occasion date | date | | | event date |
| `brideGroomName` | Bride/Groom name | text | | 150 | |
| `brideGroomPhone` | Bride/Groom phone | text | | 30 | |
| `eventVenue` | Event venue | text | | 300 | |
| `coverageDays` | Coverage days | text | | 50 | |
| `estimatedBudgetPerCard` | Budget per card | range | | 100 | |
| `quantityRequired` | Quantity required | int | | | |
| `service` | Service | text | | 200 | |
| `serviceTypes` | Service types | text | | 300 | |
| `serviceBudget` | Service budget | range | | 100 | |
| `serviceQuantity` | Service quantity | int | | | |
| `otherWeddingServices` | Other wedding services | text | | 500 | |

Indian numbers are stored in the workflow's 10-digit form (`9845012345`). Other numbers are
stored in E.164 form. `occasionDate` is stored as `YYYY-MM-DD`.

### whatsapp (International)

| Key | Label | Type | Req. | Max | Upload aliases |
|---|---|---|---|---|---|
| `phone` | Phone | phone | ✔ | 32 | whatsapp, whatsapp number, mobile, phone number, number |
| `country` | Country | country | | 100 | |
| `event_date` | Event date | date_text | | 100 | wedding date |
| `submitted_at` | Submitted | datetime | | | submitted at, submission time, timestamp |

### wedding (Wedding & event photography)

| Key | Label | Type | Req. | Max | Upload aliases |
|---|---|---|---|---|---|
| `phone` | Phone | phone | ✔ | 50 | contact number, mobile, phone number |
| `name` | Name | text | | 200 | full name |
| `email` | Email | email | | 254 | email address |
| `city` | City | text | | 200 | city event location, event location, location |
| `occasion` | Occasion | text | | 100 | what is the occasion |
| `event_date` | Event date | date_text | | 100 | |
| `budget` | Budget | budget | | 100 | photography budget, what is your approximate photography budget |
| `submitted_at` | Submitted | datetime | | | timestamp, submitted at |

`is_repeat` is set automatically when the phone already exists in this source.

### wedding_planner

| Key | Label | Type | Req. | Max | Upload aliases |
|---|---|---|---|---|---|
| `phone` | Phone | phone | ✔ | 32 | whatsapp number, contact number, mobile, phone number |
| `name` | Name | text | | 200 | full name |
| `email` | Email | email | | 254 | email address |
| `role` | You are the | text | | 100 | role |
| `wedding_type` | Wedding type | text | | 100 | |
| `city` | Wedding city | text | | 200 | wedding city destination, city |
| `home_city` | Family home city | text | | 200 | home city |
| `place_kind` | Kind of place | text | | 200 | kind of place if not sure |
| `wedding_month` | Wedding month | month | | 50 | |
| `first_day` | First day of celebrations | date | | | first day |
| `total_guests` | Total guests | int | | | |
| `guests_staying` | Guests staying | int | | | guests staying needing rooms |
| `traditions` | Traditions | text | | 300 | |
| `food` | Food | text | | 100 | |
| `alcohol` | Alcohol | text | | 50 | |
| `budget_range` | Budget range | text | | 100 | |
| `total_budget` | Total budget | int | | | |
| `budget_includes_jewellery` | Budget includes jewellery and outfits | bool | | | |
| `jewellery_outfits` | Jewellery and outfits | int | | | |
| `priority1` / `priority2` / `priority3` | Priority 1 / 2 / 3 | text | | 100 | |
| `shortlisted_vendors` | Shortlisted vendors | text | | 5000 | |
| `consent_share` | Consent: share plan | bool | | | consent share plan with king of cards |
| `consent_offers` | Consent: offers on WhatsApp | bool | | | consent offers on whatsapp |

The wedding date is worked out from `first_day`, or else the first day of `wedding_month`.

**Never accepted as lead fields:** "leads shared", vendor, interest, quote, converted, notes,
and fields that only the planner tool or the workflow fill in (`last_step`, `plan_estimate`,
`fits_budget`, `events`, campaign/customer ids). Unknown keys in `values` are ignored.

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
      "label": "Phone",
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

Downloads an empty import file.

| Query | Values | Default |
|---|---|---|
| `format` | `xlsx`, `csv` | `xlsx` |

**Response `200`:** a file with `Content-Disposition: attachment; filename="<source>-leads-template-YYYY-MM-DD.<ext>"`.

- **Excel** (`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`) has these sheets:
  - **Leads:** one header row (field labels; required ones in red) and one example row. The
    Phone and Pincode columns are pre-formatted as text so Excel keeps `+` signs and leading
    zeros.
  - **Instructions:** each column, whether it's required, and how to fill it in.
  - **Branches** (spin only): every branch name. The Leads sheet's Branch column has a
    dropdown from this list.
- **CSV** (`text/csv; charset=utf-8`, UTF-8 with BOM): the header row and the example row.

The example row must be deleted before uploading, or it will be imported as a lead.

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
  field, the first one wins. Columns that match no field are listed in `ignored_columns`.

**Response `200`**

```json
{
  "source": "spin",
  "columns": { "Phone": "phone", "Name": "name", "Branch": "branch" },
  "ignored_columns": ["Notes from sales"],
  "rows": [
    {
      "row": 2,
      "status": "ok",
      "values": { "phone": "9845020001", "name": "Asha", "branch": 4, "occasionDate": "20-12-2026" },
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
| `status` | `ok` (will be imported), `error` (has field errors), or `duplicate` (phone already exists; will be skipped). |
| `values` | Every field of the source (abbreviated above). Values are cleaned: dates `DD-MM-YYYY`, date-times ISO 8601 UTC, numbers, booleans, branch as id. A field that has an error keeps the text as written. Send these to `POST /import` unchanged, or after the user fixes them. |
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
| 400 | `The file has no Phone column. Use the template's column names.` |
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

Importing the same file twice is safe: the second time every row is reported as a duplicate.

**Errors**

| Status | Cause |
|---|---|
| 400 | `No rows to import.` (empty `rows`) |
| 422 | More than 5,000 rows, or a malformed body (FastAPI validation format) |
| 503 | Workflow duplicate check unavailable |

---

### `GET /crm-leads/{source}/export`

Downloads the source's list (all origins) with the same filters as the list endpoint, plus
the CRM details. **Archived leads, and sheet rows removed from the sheet, are never
exported.**

There is one route per source, because each list has its own filters. `format` is
`xlsx` (default) or `csv` for all of them.

| Route | Other query parameters (same meaning as the list endpoint) |
|---|---|
| `GET /crm-leads/spin/export` | `search`, `sort`, `order` (default `asc`), `branch`, `pincode`, `budget`, `vendor_id`, `interest`, `converted`, `quoted_op`, `quoted_value` (as `GET /spin-leads`) |
| `GET /crm-leads/whatsapp/export` | `search`, `country`, `sort` (`submitted_at` default, `event_date`, `country`, `phone`, `first_seen_at`), `order` (default `desc`) |
| `GET /crm-leads/wedding/export` | `search`, `occasion`, `shared_with`, `hide_repeats`, `sort` (`submitted_at` default, `event_date`, `budget_amount`, `name`, `city`), `order` (default `desc`) |
| `GET /crm-leads/wedding_planner/export` | `search`, `shared_with`, `fits_budget`, `last_step`, `sort` (`updated` default, `wedding_date`, `total_budget`, `total_guests`, `plan_estimate`, `name`), `order` (default `desc`) |

For spin, added/imported leads come first, then workflow leads, the same as the list.

**Response `200`:** a file with `Content-Disposition: attachment; filename="<source>-leads-YYYY-MM-DD.<ext>"`
(`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` or `text/csv; charset=utf-8`).

**Columns:** the lead columns below, followed by the CRM columns.

| Source | Lead columns |
|---|---|
| `whatsapp` | Lead key, Origin, Phone, Country, Event date (as written), Event date, Submitted, First seen |
| `wedding` | Lead key, Origin, Name, Phone, Email, City, Occasion, Event date (as written), Event date, Budget (as written), Budget amount, Leads shared, Repeat, Submitted |
| `wedding_planner` | Plan ID, Origin, Last step, Updated, Name, Phone, Email, You are the, Wedding type, Wedding city, Family home city, Kind of place, Wedding month, First day, Wedding date, Total guests, Guests staying, Traditions, Food, Alcohol, Budget range, Total budget, Budget includes jewellery and outfits, Jewellery and outfits, Plan estimate, Fits budget, Priorities, Shortlisted vendors, Leads shared |
| `spin` | Origin, Branch (name), then the spin list's columns in list order: Lead key (`id`), Name, Phone, Created, the `additional_details` fields by label (Pincode, Occasion date, Budget per card…), and so on |

**CRM columns (every source):** Assigned to, Assigned how (`manual`, `sheet`, `branch` or
`suggested`), Suggested vendor, Interest, Converted, Converted at, Quoted date, Quoted amount,
Notes, CRM updated.

**Formatting**

| | Excel (.xlsx) | CSV |
|---|---|---|
| Text | Always a text cell, never a formula (`=SUM(…)` shows as text) | Values starting with `=`, `+`, `-`, `@`, tab or CR get a leading `'` |
| Phone, lead key, pincode | Text cells | Text. E.164 phones start with `+`, so they come out as `'+919845012345` |
| Dates | Real date cells, format `DD-MM-YYYY` | `DD-MM-YYYY` |
| Times | Converted to IST, format `DD-MM-YYYY HH:MM` | IST, `DD-MM-YYYY HH:MM` |
| Yes/no values | `Yes` / `No` | `Yes` / `No` |
| Numbers | Number cells | Plain digits |
| Other | Header row bold and frozen | UTF-8 with BOM, comma-separated, CRLF line endings |

**Errors**

| Status | `detail` |
|---|---|
| 400 | `This export has 61,234 leads; the limit is 50,000. Narrow the filters and try again.` |
| 400 | `Invalid sort column` (spin) |
| 503 | `Read database is not configured` / `Read database unavailable` / `Read database query timed out` (spin: workflow database) |

---

### `POST /crm-leads/{source}`

Adds one lead (`origin = added`).

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
- Unknown keys are ignored. If no known key is sent:
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
`existing_sheet`/`existing_workflow`, then `existing_added`, then `in_file`.

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

## 5. Change to `PUT /leads/{source}/{lead_key}/crm`

The request and response are unchanged. One rule is new:

- If `lead_key` starts with `crm-` and no added/imported lead with that key exists in
  `{source}`, the response is **`404 {"detail": "Lead not found"}`**. Archived leads still
  exist, so their CRM details can be edited.
- This applies to all four sources.
- Workflow ids (`101`) and sheet keys are accepted exactly as before, without a lookup.

The CRM details of an added/imported lead are stored like any other lead's:
`crm_lead_meta(source, lead_key = "crm-…")`. They are never stored on the lead itself.

---

## 6. Changes to the existing list endpoints

- `GET /whatsapp-leads`, `GET /wedding-leads` and `GET /wedding-plans`: each item now has
  `origin` (`sheet`, `added` or `imported`). Added/imported leads are listed with the sheet
  rows. Archived ones are hidden.
- `GET /spin-leads`:
  - Each item now has `origin` (`workflow`, `added` or `imported`).
  - Added/imported spin leads (not archived) are listed **first**, sorted among themselves,
    then the workflow leads. Paging and `total` cover both.
  - Their `id` and `crm.lead_key` are `crm-<n>`.
  - All filters apply to both groups.
- Branch lead counts, vendor statistics and analytics include added/imported spin leads that
  aren't archived.

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
