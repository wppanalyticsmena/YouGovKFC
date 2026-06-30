"""
Run a full BrandIndex report (audiences x brands x metrics) and emit the tidy
long format: date | market | audience | brand | metric | score.

The report is defined in report_config.json — audiences (each with its filter and
brand list), metrics, weekly resampling, and date range. Validated to reproduce
the "Chicken Brands BHT - Nationality Split" / KFC YouGov file exactly.

Usage
-----
    python run_report.py                      # uses report_config.json, writes xlsx+csv
    python run_report.py --config other.json
    python run_report.py --to-bq              # also load into BigQuery
    python run_report.py --end 2026-06-04     # override the end date

Auth: Simple Login with the YOUGOV_EMAIL / YOUGOV_PASSWORD in .env (the script
re-logs in each run, so a weekly schedule is effectively hands-off).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from yougov_to_bigquery import BrandIndexClient, YouGovConfig, reshape_to_long

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("yougov.report")

OUTPUT_COLUMNS = ["date", "market", "audience", "brand", "metric", "score"]


def build_analysis(aud: dict, cfg: dict, start: str, end: str) -> dict:
    """One analysis per audience: a query per brand, all sharing the audience filter."""
    metrics = list(cfg["metrics"].keys())
    queries = []
    for name, brand_id in aud["brands"].items():
        q = {
            "id": f"{aud['audience']}|{name}",
            "entity": {"region": aud["region"], "sector_id": aud["sector_id"], "brand_id": brand_id},
            "filters": [{"expression": aud["filter"]}] if aud.get("filter") else [],
            "metrics_score_types": {m: cfg.get("score_type", "net_score") for m in metrics},
            "period": {"start_date": {"date": start}, "end_date": {"date": end}},
            "scoring": cfg.get("scoring", "total"),
        }
        if cfg.get("resample"):
            q["resample"] = cfg["resample"]
        queries.append(q)
    return {"id": "report", "title": "report", "queries": queries}


def run(config_path: str, start: str | None, end: str | None) -> pd.DataFrame:
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    today = datetime.now(timezone.utc).date()
    start = start or cfg.get("start") or f"{today.year}-01-01"
    end = end or cfg.get("end") or today.isoformat()
    log.info("Report window: %s .. %s", start, end)

    yg = YouGovConfig.from_env()
    client = BrandIndexClient(yg.base_url)
    client.login(yg.email, yg.password)

    frames: list[pd.DataFrame] = []
    for aud in cfg["audiences"]:
        id_to_name = {int(bid): name for name, bid in aud["brands"].items()}
        log.info("Pulling %s / %s (%d brands)...", aud["market"], aud["audience"], len(id_to_name))
        analysis = build_analysis(aud, cfg, start, end)
        csv_bytes = client.execute_analysis_csv(analysis)
        df = reshape_to_long(csv_bytes, id_to_name, audience=aud["audience"])
        log.info("  -> %d rows", len(df))
        if not df.empty:
            frames.append(df)

    if not frames:
        sys.exit("No data returned for any audience.")
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.sort_values(OUTPUT_COLUMNS[:-1]).reset_index(drop=True)
    return combined


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Run a BrandIndex report into the long format.")
    p.add_argument("--config", default="report_config.json", help="Report definition JSON.")
    p.add_argument("--start", help="Start date YYYY-MM-DD (overrides config).")
    p.add_argument("--end", help="End date YYYY-MM-DD (overrides config; default today).")
    p.add_argument("--out", help="Output .xlsx path (default: brandindex_report_long.xlsx).")
    p.add_argument("--to-bq", action="store_true", help="Also load into BigQuery.")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    df = run(args.config, args.start, args.end)

    out = Path(args.out) if args.out else Path("brandindex_report_long.xlsx")
    df.to_excel(out, index=False)
    df.to_csv(out.with_suffix(".csv"), index=False)
    log.info("Wrote %d rows -> %s (+ .csv)", len(df), out)
    print("\nPreview:")
    print(df.head(12).to_string(index=False))
    print(f"\nmarkets={sorted(df['market'].unique())}  audiences={sorted(df['audience'].unique())}  "
          f"metrics={sorted(df['metric'].unique())}  brands={df['brand'].nunique()}  weeks={df['date'].nunique()}")

    if args.to_bq:
        from bq_load import BQConfig, load_long_dataframe
        load_long_dataframe(df, BQConfig.from_env())


if __name__ == "__main__":
    main()
