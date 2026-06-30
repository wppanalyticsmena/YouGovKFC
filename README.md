# YouGov BrandIndex → BigQuery

Pulls a YouGov **BrandIndex** report (audiences × brands × metrics) from the live
v1 API and emits the tidy long format, ready for BigQuery:

```
date | market | audience | brand | metric | score
```

Validated to reproduce the "Chicken Brands BHT – Nationality Split" report to
within survey-revision noise (98.7% of cells exact).

## Files

| File | Role |
|------|------|
| `run_report.py` | Runs the full report → long format (xlsx + csv), optional `--to-bq` |
| `report_config.json` | The report definition — audiences (filters), brand IDs, metrics, weekly resample. **Edit here.** |
| `yougov_to_bigquery.py` | `discover` (regions/sectors/brands) + ad-hoc single `pull` |
| `auth.py` | Auth: Simple Login (email/password) + OAuth2 token support |
| `bq_load.py` | Loads the long DataFrame into BigQuery |

## Setup

```powershell
pip install -r requirements.txt
# put credentials in .env (see .env.example)
```

`.env` needs the YouGov login; add the BigQuery target when loading:

```
YOUGOV_EMAIL=...
YOUGOV_PASSWORD=...
BQ_PROJECT=...        # only for --to-bq
BQ_DATASET=...
BQ_TABLE=brandindex_long
```

## Usage

```powershell
# Find region / sector / brand ids
python yougov_to_bigquery.py discover --region ae --sector-id 699

# Run the report (Jan 1 -> today) -> brandindex_report_long.xlsx + .csv
python run_report.py

# Run and load straight into BigQuery
python run_report.py --to-bq

# Override the window
python run_report.py --start 2026-01-01 --end 2026-06-30
```

## How the report is defined

`report_config.json` lists one entry per **audience** (market + region + sector +
filter expression + brand list) plus the shared metrics and weekly resample.
Validated specifics:

- **Audiences (filters):** UAE Locals + Arab Expats = `bixdemo_uaebixnat in [1,2]`;
  KSA Locals = `bixdemo_ksabixnat in [1]`; MF 18-34 = `bixdemo_bixage in [1,2]`.
- **Weekly:** `resample = {size:7, type:"calendar_day"}`, anchored to the start date.
- **Metrics → labels:** `aided`→Awareness, `index`→Brand Index, `buzz`→Buzz,
  `consider`→Consideration, `likelybuy`→Purchase Intent, `reputation`→Reputation.

To track different brands/markets/audiences, edit `report_config.json` — no code
changes needed. Use `discover` to find new sector/brand ids.

## Notes

- Auth re-logs in on every run, so a **weekly schedule is effectively hands-off**
  (the only manual touch is if the YouGov password changes). Store credentials as
  environment variables / secrets in your scheduler.
- The most recent weekly point covers the week **starting** that date, so a live
  run's final week is partial until the week completes — expected for current data.
