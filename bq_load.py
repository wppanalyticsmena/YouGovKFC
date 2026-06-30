"""
Shared BigQuery loader for the long-format YouGov table.

The long schema (matches the KFC YouGov file) is:
    date | market | audience | brand | metric | score

Both the local report transformer and the API pull produce this schema and
load it through `load_long_dataframe()`.

BigQuery credentials are only needed here — nothing else in the project touches
Google. Provide them via either:
  * `gcloud auth application-default login`  (leave GOOGLE_APPLICATION_CREDENTIALS unset), or
  * GOOGLE_APPLICATION_CREDENTIALS pointing at a service-account JSON key.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import pandas as pd

log = logging.getLogger("yougov.bq")

LONG_COLUMNS = ["date", "market", "audience", "brand", "metric", "score"]


@dataclass
class BQConfig:
    project: str
    dataset: str
    table: str
    location: str = "US"
    write_disposition: str = "WRITE_TRUNCATE"

    @classmethod
    def from_env(cls) -> "BQConfig":
        from dotenv import load_dotenv
        load_dotenv(interpolate=False)  # never expand $ in secret values

        def need(key: str) -> str:
            val = os.getenv(key, "").strip()
            if not val:
                raise SystemExit(f"Missing required environment variable: {key} (set it in .env)")
            return val

        return cls(
            project=need("BQ_PROJECT"),
            dataset=need("BQ_DATASET"),
            table=os.getenv("BQ_TABLE", "brandindex_long").strip(),
            location=os.getenv("BQ_LOCATION", "US").strip(),
            write_disposition=os.getenv("BQ_WRITE_DISPOSITION", "WRITE_TRUNCATE").strip(),
        )


def load_long_dataframe(df: pd.DataFrame, cfg: BQConfig) -> None:
    """Load a long-format DataFrame into BigQuery with an explicit, stable schema."""
    # Imported here so the rest of the project runs without google libs installed.
    from google.cloud import bigquery

    if df.empty:
        log.warning("No rows to load into BigQuery; skipping.")
        return

    missing = [c for c in LONG_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"DataFrame is missing required columns: {missing}")

    df = df[LONG_COLUMNS].copy()
    df["date"] = pd.to_datetime(df["date"])

    client = bigquery.Client(project=cfg.project, location=cfg.location)
    table_id = f"{cfg.project}.{cfg.dataset}.{cfg.table}"

    job_config = bigquery.LoadJobConfig(
        write_disposition=cfg.write_disposition,
        schema=[
            bigquery.SchemaField("date", "TIMESTAMP"),
            bigquery.SchemaField("market", "STRING"),
            bigquery.SchemaField("audience", "STRING"),
            bigquery.SchemaField("brand", "STRING"),
            bigquery.SchemaField("metric", "STRING"),
            bigquery.SchemaField("score", "FLOAT"),
        ],
    )

    log.info("Loading %d rows into %s (%s)...", len(df), table_id, cfg.write_disposition)
    job = client.load_table_from_dataframe(df, table_id, job_config=job_config)
    job.result()
    table = client.get_table(table_id)
    log.info("Done. %s now has %d rows.", table_id, table.num_rows)


def load_dataframe_autodetect(df: pd.DataFrame, cfg: BQConfig) -> None:
    """Load an arbitrary DataFrame, letting BigQuery auto-detect the schema.

    Used by the live API pull as an interim until the exact live CSV layout is
    confirmed and reshaped into the canonical long schema. Column names should
    already be BigQuery-safe (the pull sanitises them)."""
    from google.cloud import bigquery

    if df.empty:
        log.warning("No rows to load into BigQuery; skipping.")
        return

    client = bigquery.Client(project=cfg.project, location=cfg.location)
    table_id = f"{cfg.project}.{cfg.dataset}.{cfg.table}"
    job_config = bigquery.LoadJobConfig(
        write_disposition=cfg.write_disposition,
        schema_update_options=[bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION],
        autodetect=True,
    )
    log.info("Loading %d rows into %s (%s, autodetect)...", len(df), table_id, cfg.write_disposition)
    job = client.load_table_from_dataframe(df, table_id, job_config=job_config)
    job.result()
    table = client.get_table(table_id)
    log.info("Done. %s now has %d rows.", table_id, table.num_rows)
