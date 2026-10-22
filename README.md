# Prestart Monitor

A single-file web page for reviewing vehicle prestart checks exported from [Gearbox](https://www.gearbox.com.au/). Import the Gearbox *Prestart Report* spreadsheet, then filter, sort and visualise prestart usage, missing days and odometer (km) discrepancies.

Everything runs in the browser. There is no server, no build step and no data in the repository, so the page can be hosted on S3 (or any static host) as-is.

## Features

| Area | What it does |
|---|---|
| **Import / update** | Load a Gearbox export (`.xlsx`, `.xls`, `.csv`) or a JSON backup. Rows are merged by **Prestart #**: new prestarts are added and existing ones updated, so you can import each new export over the old data. Drag-and-drop works anywhere on the page. |
| **Filters** | Fleet #, Employee, date range, status (completed / incomplete / missing / km discrepancies), free-text search and *Show missing days*. They apply to every tab. |
| **Records** | A sortable table (click any column header). Incomplete prestarts, missing days and km issues are highlighted. Missing days appear as rows of their own. |
| **Usage by day** | A Mon–Sun stacked bar chart (completed / incomplete only / missing, with % coverage) and a day-by-day calendar heatmap by fleet or by employee. Click a day to open its records. |
| **Calendar** | Week and Month views listing each vehicle's **rego** per day. Green = completed prestart, grey outline = incomplete, red struck-through = missing. Uses the same filters, e.g. *Show → Completed* for green regos only. |
| **Km discrepancies** | An odometer chart for each fleet plus a table of flagged readings, each with a plain-English reason. |
| **Export** | *Export CSV* downloads the current table view, including flags. *Backup* downloads all data as JSON to move to another browser or device. |

### Km checks

Each fleet's completed prestarts are checked in date order:

- **No km entered:** km is blank or ≤ the "treat as blank" value (default 10).
- **Km & hours swapped?:** km ≤ 10 while hours > 1,000.
- **Km lower than previous:** the odometer went backwards.
- **Km jump too large:** more than *max km per day* (default 1,200) × days since the previous reading.
- **Sequence reset:** a jump or rollback that the following readings continue from, e.g. a different vehicle or a corrected odometer.
- **Conflicting km same day:** two readings for one fleet on the same day differ by more than the tolerance (default 50 km).
- **Matches another fleet:** a flagged reading equals another fleet's reading within ±1 day, which suggests the wrong Fleet # was selected.

A single typo (e.g. 1,598 between 14,653 and 15,543) is flagged once, without also flagging the next good reading.

### Missing days

A day counts as **missing** for a fleet (or employee) when it is an *expected prestart day* (default Mon–Sat) with no prestart at all. By default only days between that fleet's first and last prestart are counted. All of these rules can be changed under **Rules for missing days & km checks**.

## Expected input columns

The column names from the Gearbox export (matching ignores case, spaces and punctuation):

`Groups, SubGroups, Prestart #, Fleet #, Registration, Description, Date, Employee, Employee #, Kilometres, Hours, Type, Completed, Not Completed, Page Number, Time taken, Gearbox Entry, Notes`

- **Required:** Fleet # and Date. The header row doesn't have to be the first row.
- **Date format:** Australian `dd/mm/yyyy` text, or real Excel dates.
- **Completed status:** taken from `Completed` (1/0), otherwise from `Time taken` ≠ "Incomplete".

## Hosting on Amazon S3

1. Create a bucket and turn on **Static website hosting** with index document `index.html`.
2. Upload `index.html` with content type `text/html`.
3. Give access through a bucket policy, CloudFront, or whatever your organisation uses.

```bash
aws s3 cp index.html s3://YOUR-BUCKET/index.html --content-type text/html
```

## Data & privacy

- The file contains **no prestart data**, so a public bucket exposes nothing.
- Imported data is stored only in the viewer's own browser (`localStorage`). Other users and devices don't see it; use *Backup* / import to move it.
- *Clear* removes the stored data from that browser.

## Dependencies

- [SheetJS](https://sheetjs.com/) `xlsx@0.18.5`, loaded from `cdnjs.cloudflare.com` to read spreadsheets. Viewers need internet access to that site. JSON backups import without it.
- No other libraries; charts are plain SVG.

## Browser support

Current Chrome, Edge, Firefox and Safari. Works at phone width; wide tables and calendars scroll sideways inside their cards.
