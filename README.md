# Prestart Monitor

A single-file web page for reviewing vehicle prestart checks exported from [Gearbox](https://www.gearbox.com.au/). Import the Gearbox *Prestart Report* spreadsheet, then filter, sort and visualise prestart usage, missing days and odometer (km) discrepancies.

The page is a single `index.html` with no build step. Hosted at https://prestart.mgncivil.com.au it receives prestarts live from Gearbox behind Microsoft sign-in (see [Live mode](#live-mode-gearbox--aws)). Opened as a plain file, it works offline from Excel imports.

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

## Live mode (Gearbox → AWS)

When the page is opened at **https://prestart.mgncivil.com.au** it runs in *live mode*: users sign in with their Microsoft (MGN Civil) account and see shared data that updates automatically from Gearbox. Opened any other way (e.g. a local copy of `index.html`), it falls back to *local mode* (Excel import, data in that browser only).

```
Gearbox ──prestart.created webhook──▶ API Gateway /webhook/{token} ──▶ Lambda ──▶ DynamoDB
Gearbox API (vehicles, daily 5am) ◀── EventBridge ── Lambda ──────────────────────▲
Browser ──Microsoft sign-in (Cognito + Entra SAML)──▶ API Gateway (JWT) ──▶ Lambda ┘
Browser ◀── CloudFront ◀── S3 (index.html, private)
```

The Gearbox public API cannot *read* prestarts (the endpoint is create-only), so new prestarts arrive via the **`prestart.created` webhook**. History is loaded once by importing a Gearbox *Prestart Report* export; in live mode **Import / update** saves to the shared store for everyone. The webhook doesn't include *time taken* or *incomplete* prestarts. Those come only from Excel imports.

| AWS resource (ap-southeast-2) | Name |
|---|---|
| CloudFront + private S3 (page) | `E3PFFGWD843NTL` → bucket `prestart.mgncivil.com.au` |
| HTTP API | `prestart-monitor-api` (`iqz9c493v6`) |
| Lambda (Python 3.12) | `prestart-monitor-api`, source in [`backend/lambda_function.py`](backend/lambda_function.py) |
| DynamoDB | `PrestartMonitor` (prestarts + `vehicle#…` items) |
| Secrets Manager | `prestart-monitor/gearbox` |
| Sign-in | Cognito pool `contractorportal` (Microsoft SAML IdP), app client `prestart-monitor` |
| Schedule | EventBridge `prestart-monitor-vehicle-sync` (daily 05:00 AEST) |

API routes: `GET /prestarts`, `POST /prestarts/import`, `POST /vehicles/sync` (all need a Microsoft sign-in from an `@mgncivil.com.au` account), and `POST /webhook/{token}` (Gearbox only, secret path token + HMAC signature).

### One-off setup

1. **Gearbox credentials → Secrets Manager.** In the AWS console open *Secrets Manager → `prestart-monitor/gearbox` → Retrieve secret value → Edit* and fill in `client_id`, `client_secret` and `business_token` (Gearbox *Settings → Integrations*). Leave `webhook_path_token` as it is.
2. **Gearbox webhook.** In Gearbox *Settings → Webhooks → Add New Webhook*: URL `https://iqz9c493v6.execute-api.ap-southeast-2.amazonaws.com/webhook/<webhook_path_token>`, event **Prestarts → prestart.created**. Gearbox verifies the endpoint automatically. Then copy the webhook's **signing key** into the secret's `webhook_signing_key`.
3. **DNS.** Add a CNAME `prestart.mgncivil.com.au → d1fz2h5h7lkkjo.cloudfront.net` at the DNS provider. Until then the page also works at https://d1fz2h5h7lkkjo.cloudfront.net.
4. **History.** Sign in and use **Import / update** with the latest Gearbox Prestart Report export.

### Deploying changes

Pushing to `main` runs [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml). It assumes the IAM role `prestart-monitor-github-deploy` through GitHub OIDC (no stored AWS keys), updates the Lambda code, uploads `index.html` to S3 and invalidates CloudFront.

Configuration for the page (API URL, Cognito domain, client ID, allowed origins) is the `CONFIG` block at the top of the script in `index.html`.

## Data & privacy

- **Live mode:** prestart data is stored in DynamoDB in Sydney and only served to signed-in `@mgncivil.com.au` Microsoft accounts. The page itself contains no data. Sign-in tokens are kept in the browser tab's session storage.
- **Local mode:** imported data stays in that browser's `localStorage`; *Clear* removes it.
- No data, credentials or keys are stored in this repository.

## Dependencies

- [SheetJS](https://sheetjs.com/) `xlsx@0.18.5`, loaded from `cdnjs.cloudflare.com` to read spreadsheets. Viewers need internet access to that site. JSON backups import without it.
- No other libraries; charts are plain SVG.

## Browser support

Current Chrome, Edge, Firefox and Safari. Works at phone width; wide tables and calendars scroll sideways inside their cards.
