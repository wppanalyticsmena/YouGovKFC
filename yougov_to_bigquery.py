"""
Pull YouGov BrandIndex data via the v1 API and load it into BigQuery.

Two modes:

  discover  Log in and print the regions / sectors / brands available to your
            account, so you can find the numeric IDs the `pull` mode needs.

  pull      Execute a BrandIndex timeline analysis for one or more brands and
            load the resulting rows into a BigQuery table.

Auth note: the BrandIndex API uses a *session cookie* login (POST /v1/auth/login).
We keep a single requests.Session so the cookie is reused for every request.
API access must be enabled on your YouGov account by YouGov Support first.

Examples
--------
  # See what your account can access
  python yougov_to_bigquery.py discover --region us

  # Pull buzz + index for two brands for 2024 and load to BigQuery
  python yougov_to_bigquery.py pull \
      --region us --sector-id 1 --brand-id 1007 --brand-id 1011 \
      --metric buzz --metric index \
      --start 2024-01-01 --end 2024-12-31
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd
import requests
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("yougov")

# The API wraps every request/response body in {"meta": {"version": "v1"}, "data": ...}
API_META = {"version": "v1"}

# Valid metrics per the BrandIndex v1 spec (components.schemas.Metric).
VALID_METRICS = {
    "index", "buzz", "impression", "quality", "value", "reputation",
    "satisfaction", "recommend", "aided", "attention", "adaware", "wom",
    "consider", "likelybuy", "current_own", "former_own",
}


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass
class YouGovConfig:
    """Only the YouGov login — BigQuery config is loaded separately (bq_load.BQConfig),
    so discover / dry-run work without any Google credentials."""
    email: str
    password: str
    base_url: str

    @classmethod
    def from_env(cls) -> "YouGovConfig":
        load_dotenv(interpolate=False)  # interpolate=False: never expand $ in passwords

        def need(key: str) -> str:
            val = os.getenv(key, "").strip()
            if not val:
                sys.exit(f"Missing required environment variable: {key} (set it in .env)")
            return val

        return cls(
            email=need("YOUGOV_EMAIL"),
            password=need("YOUGOV_PASSWORD"),
            base_url=os.getenv("YOUGOV_BASE_URL", "https://api.brandindex.com").rstrip("/"),
        )


# --------------------------------------------------------------------------- #
# YouGov BrandIndex client
# --------------------------------------------------------------------------- #
class BrandIndexClient:
    def __init__(self, base_url: str, timeout: int = 60, token_provider=None):
        self.base_url = base_url
        self.timeout = timeout
        self.token_provider = token_provider  # OAuth2TokenProvider, or None for cookie login
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    # -- low-level helpers -------------------------------------------------- #
    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _request(self, method: str, path: str, *, retries: int = 3, **kwargs) -> requests.Response:
        """Issue a request, retrying transient (5xx / connection) failures."""
        url = self._url(path)
        # In OAuth mode, attach a fresh bearer token (cached until near expiry).
        if self.token_provider is not None:
            headers = {**(kwargs.pop("headers", None) or {})}
            headers["Authorization"] = f"Bearer {self.token_provider.get_token()}"
            kwargs["headers"] = headers
        for attempt in range(1, retries + 1):
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                if attempt == retries:
                    raise
                wait = 2 ** attempt
                log.warning("%s %s failed (%s); retry %d/%d in %ds", method, path, exc, attempt, retries, wait)
                time.sleep(wait)
                continue

            if resp.status_code >= 500 and attempt < retries:
                wait = 2 ** attempt
                log.warning("%s %s -> HTTP %d; retry %d/%d in %ds", method, path, resp.status_code, attempt, retries, wait)
                time.sleep(wait)
                continue
            return resp
        raise RuntimeError("unreachable")

    @staticmethod
    def _raise_for_api_error(resp: requests.Response) -> None:
        if resp.ok:
            return
        # The API returns a JSON error body (components.schemas.JSONErrorResponse).
        detail = resp.text
        try:
            body = resp.json()
            detail = body.get("message", detail)
            if body.get("request_id"):
                detail += f"  (request_id={body['request_id']})"
        except ValueError:
            pass
        raise RuntimeError(f"HTTP {resp.status_code} on {resp.request.method} {resp.url}: {detail}")

    # -- auth --------------------------------------------------------------- #
    def login(self, email: str, password: str) -> None:
        payload = {"meta": API_META, "data": {"email": email, "password": password}}
        resp = self._request("POST", "/v1/auth/login", json=payload)
        if resp.status_code == 401:
            sys.exit("Login failed (401). Check credentials, and confirm YouGov Support has "
                     "enabled API access on your account.")
        self._raise_for_api_error(resp)
        log.info("Logged in to BrandIndex as %s", email)

    # -- taxonomy / discovery ---------------------------------------------- #
    def get_regions(self) -> dict:
        resp = self._request("GET", "/v1/taxonomies/regions")
        self._raise_for_api_error(resp)
        return resp.json()["data"]  # {name: Region}

    def get_sectors(self, region: str) -> dict:
        resp = self._request("GET", f"/v1/taxonomies/regions/{region}/sectors")
        self._raise_for_api_error(resp)
        return resp.json()["data"]  # {sector_id: Sector}

    def get_brands(self, region: str, sector_id: int) -> dict:
        resp = self._request("GET", f"/v1/taxonomies/regions/{region}/sectors/{sector_id}/brands")
        self._raise_for_api_error(resp)
        return resp.json()["data"]  # {brand_id: Brand}

    # -- data pull ---------------------------------------------------------- #
    def execute_analysis_csv(self, analysis: dict) -> bytes:
        """POST an analysis definition to /v1/analyses/execute.csv and return CSV bytes."""
        payload = {"meta": API_META, "data": analysis}
        resp = self._request(
            "POST", "/v1/analyses/execute.csv",
            json=payload,
            headers={"Accept": "application/octet-stream, application/json"},
        )
        self._raise_for_api_error(resp)
        return resp.content


# --------------------------------------------------------------------------- #
# Building an analysis request
# --------------------------------------------------------------------------- #
@dataclass
class Query:
    region: str
    sector_id: int
    metrics: list[str]
    start_date: str
    end_date: str
    brand_id: int | None = None
    scoring: str = "total"
    score_type: str = "net_score"
    moving_average: int | None = None

    def entity(self) -> dict:
        ent = {"region": self.region, "sector_id": self.sector_id}
        if self.brand_id is not None:
            ent["brand_id"] = self.brand_id
        return ent

    def to_analysis(self, analysis_id: str = "yougov-bq") -> dict:
        query: dict = {
            "id": analysis_id,
            "entity": self.entity(),
            "filters": [],
            "metrics_score_types": {m: self.score_type for m in self.metrics},
            "period": {
                "start_date": {"date": self.start_date},
                "end_date": {"date": self.end_date},
            },
            "scoring": self.scoring,
        }
        if self.moving_average:
            query["moving_average"] = self.moving_average
        return {"id": analysis_id, "title": analysis_id, "queries": [query]}


# --------------------------------------------------------------------------- #
# CSV -> DataFrame normalisation
# --------------------------------------------------------------------------- #
def sanitize_column(name: str) -> str:
    """Make a CSV header safe as a BigQuery column name."""
    name = name.strip().lower()
    name = re.sub(r"[^0-9a-z_]+", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    if not name:
        name = "col"
    if name[0].isdigit():
        name = f"_{name}"
    return name


# Region code -> the market label used in the long output.
REGION_TO_MARKET = {"ae": "UAE", "sa": "KSA", "eg": "Egypt"}

# API metric code -> the metric label used in the long output (matches the report).
METRIC_LABELS = {
    "buzz": "Buzz", "index": "Brand Index", "reputation": "Reputation",
    "consider": "Consideration", "aided": "Awareness", "adaware": "Ad Awareness",
    "likelybuy": "Purchase Intent", "impression": "Impression", "quality": "Quality",
    "value": "Value", "satisfaction": "Satisfaction", "recommend": "Recommend",
    "attention": "Attention", "wom": "Word of Mouth", "current_own": "Current Owner",
    "former_own": "Former Owner",
}


def reshape_to_long(csv_bytes: bytes, brand_names: dict, audience: str = "Total") -> pd.DataFrame:
    """Map the API's CSV (already long: one row per date/brand/metric) into the
    canonical schema: date | market | audience | brand | metric | score."""
    if not csv_bytes.strip():
        return pd.DataFrame()
    df = pd.read_csv(io.BytesIO(csv_bytes))
    if df.empty:
        return df

    out = pd.DataFrame({
        "date": pd.to_datetime(df["date"], errors="coerce"),
        "market": df["region"].map(lambda r: REGION_TO_MARKET.get(r, r)),
        "audience": audience,
        "brand": df["brand_id"].map(lambda b: brand_names.get(int(b), str(b))),
        "metric": df["metric"].map(lambda m: METRIC_LABELS.get(m, m)),
        "score": pd.to_numeric(df["score"], errors="coerce"),
    })
    return out.dropna(subset=["score"]).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# BigQuery load
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #
def run_discover(client: BrandIndexClient, args: argparse.Namespace) -> None:
    if not args.region:
        regions = client.get_regions()
        print("\nRegions available to your account:")
        for name, r in sorted(regions.items()):
            label = r.get("label") or ""
            permitted = r.get("permitted")
            flag = "" if permitted is None else ("  [permitted]" if permitted else "  [NOT permitted]")
            print(f"  {name:<8} {label}{flag}")
        print("\nRe-run with --region <name> to list its sectors.")
        return

    sectors = client.get_sectors(args.region)
    print(f"\nSectors in region '{args.region}':")
    for sid, s in sorted(sectors.items(), key=lambda kv: int(kv[0])):
        print(f"  sector_id={sid:<6} {s.get('label', '')}")

    if args.sector_id is not None:
        brands = client.get_brands(args.region, args.sector_id)
        print(f"\nBrands in region '{args.region}', sector_id={args.sector_id}:")
        for bid, b in sorted(brands.items(), key=lambda kv: int(kv[0])):
            active = "" if b.get("is_active", True) else "  (inactive)"
            print(f"  brand_id={bid:<7} {b.get('label', '')}{active}")
    else:
        print("\nAdd --sector-id <id> to list the brands inside a sector.")


def run_pull(client: BrandIndexClient, args: argparse.Namespace) -> None:
    brand_ids: list[int | None] = args.brand_id or [None]  # None => whole-sector query

    # Fetch brand id -> label for this sector so the output uses readable names.
    brand_names: dict[int, str] = {}
    try:
        for bid, b in client.get_brands(args.region, args.sector_id).items():
            brand_names[int(bid)] = b.get("label") or str(bid)
    except Exception as exc:  # names are nice-to-have, not essential
        log.warning("Could not fetch brand names (%s); using ids.", exc)

    frames: list[pd.DataFrame] = []
    for brand_id in brand_ids:
        query = Query(
            region=args.region,
            sector_id=args.sector_id,
            brand_id=brand_id,
            metrics=args.metric,
            start_date=args.start,
            end_date=args.end,
            scoring=args.scoring,
            score_type=args.score_type,
            moving_average=args.moving_average,
        )
        label = f"brand {brand_id}" if brand_id is not None else f"sector {args.sector_id}"
        log.info("Executing analysis for %s (%s..%s, metrics=%s)",
                 label, args.start, args.end, ",".join(args.metric))

        csv_bytes = client.execute_analysis_csv(query.to_analysis())

        if args.save_csv:
            fname = f"brandindex_{args.region}_{args.sector_id}_{brand_id}.csv"
            with open(fname, "wb") as fh:
                fh.write(csv_bytes)
            log.info("Saved raw CSV -> %s", fname)

        df = reshape_to_long(csv_bytes, brand_names, audience=args.audience)
        log.info("  -> %d rows", len(df))
        if not df.empty:
            frames.append(df)

    if not frames:
        log.warning("No data returned for any query; nothing to load.")
        return

    combined = pd.concat(frames, ignore_index=True)

    if args.dry_run:
        log.info("Dry run: %d total rows. Columns: %s", len(combined), list(combined.columns))
        print(combined.head(20).to_string())
        return

    from bq_load import BQConfig, load_long_dataframe
    load_long_dataframe(combined, BQConfig.from_env())


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pull YouGov BrandIndex data into BigQuery.")
    sub = p.add_subparsers(dest="mode", required=True)

    # Credentials shared by every subcommand. All can come from the environment
    # instead (YOUGOV_CLIENT_ID, YOUGOV_CLIENT_SECRET, ...), which is what a
    # scheduler should use. CLI args override env so they can be injected at runtime.
    creds = argparse.ArgumentParser(add_help=False)
    g = creds.add_argument_group("credentials (or set via environment)")
    g.add_argument("--client-id", help="OAuth2 client_id (env: YOUGOV_CLIENT_ID).")
    g.add_argument("--client-secret", help="OAuth2 client_secret (env: YOUGOV_CLIENT_SECRET).")
    g.add_argument("--token-url", help="OAuth2 token endpoint (env: YOUGOV_TOKEN_URL).")
    g.add_argument("--scope", help="OAuth2 scope (env: YOUGOV_SCOPE, default 'brandindex').")
    g.add_argument("--grant-type", choices=["client_credentials", "refresh_token"],
                   help="OAuth2 grant (env: YOUGOV_GRANT_TYPE, default client_credentials).")
    g.add_argument("--refresh-token", help="OAuth2 refresh_token, if grant_type=refresh_token "
                                           "(env: YOUGOV_REFRESH_TOKEN).")
    g.add_argument("--client-auth", choices=["body", "basic"],
                   help="How to send client creds to the token endpoint (env: YOUGOV_CLIENT_AUTH).")
    g.add_argument("--base-url", help="API base URL (env: YOUGOV_BASE_URL).")

    d = sub.add_parser("discover", parents=[creds],
                       help="List regions / sectors / brands your account can access.")
    d.add_argument("--region", help="Region name, e.g. 'us'. Omit to list all regions.")
    d.add_argument("--sector-id", type=int, help="Sector id to list its brands.")

    pull = sub.add_parser("pull", parents=[creds],
                          help="Execute an analysis and load it into BigQuery.")
    pull.add_argument("--region", required=True, help="Region name, e.g. 'us'.")
    pull.add_argument("--sector-id", type=int, required=True, help="Sector id (see discover).")
    pull.add_argument("--brand-id", type=int, action="append",
                      help="Brand id. Repeat for multiple brands. Omit for a whole-sector query.")
    pull.add_argument("--metric", action="append", required=True,
                      help=f"Metric to pull (repeatable). One of: {', '.join(sorted(VALID_METRICS))}")
    pull.add_argument("--start", help="Start date YYYY-MM-DD (default: Jan 1 of the current year).")
    pull.add_argument("--end", help="End date YYYY-MM-DD (default: today, UTC).")
    pull.add_argument("--scoring", default="total", choices=["total", "aware", "opinion"],
                      help="Scoring population (default: total).")
    pull.add_argument("--score-type", default="net_score",
                      choices=["net_score", "positives", "negatives", "neutrals",
                               "positives_neutrals", "negatives_neutrals"],
                      help="Score type applied to every metric (default: net_score).")
    pull.add_argument("--moving-average", type=int, help="Rolling-mean window in days (optional).")
    pull.add_argument("--audience", default="Total",
                      help="Audience label for the output rows (default: Total). "
                           "Used when a demographic/composite filter defines the audience.")
    pull.add_argument("--save-csv", action="store_true", help="Also write the raw CSV(s) to disk.")
    pull.add_argument("--dry-run", action="store_true", help="Fetch & print, but do not load to BigQuery.")

    return p.parse_args(argv)


def validate_dates(*dates: str) -> None:
    for d in dates:
        try:
            datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            sys.exit(f"Invalid date '{d}'. Use YYYY-MM-DD.")


def build_client(args: argparse.Namespace) -> BrandIndexClient:
    """Choose auth at runtime: OAuth2 (preferred, unattended) if client credentials
    are supplied via args/env; otherwise the email/password cookie login."""
    from auth import OAuth2Settings, OAuth2TokenProvider

    base_url = (getattr(args, "base_url", None)
                or os.getenv("YOUGOV_BASE_URL", "https://api.brandindex.com")).rstrip("/")

    oauth = OAuth2Settings.resolve(args)
    if oauth is not None:
        log.info("Auth: OAuth2 (grant=%s) — unattended mode.", oauth.grant_type)
        provider = OAuth2TokenProvider(oauth)
        provider.get_token()  # fail fast if credentials are wrong
        return BrandIndexClient(base_url, token_provider=provider)

    # Fallback: email/password cookie login (interactive-style; not API-authorized
    # on accounts without API access enabled).
    cfg = YouGovConfig.from_env()
    log.info("Auth: email/password cookie login (no OAuth client credentials found).")
    client = BrandIndexClient(cfg.base_url)
    client.login(cfg.email, cfg.password)
    return client


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    client = build_client(args)

    if args.mode == "discover":
        run_discover(client, args)
        return

    # mode == pull — default to a year-to-date window so a scheduled command is static.
    today = datetime.now(timezone.utc).date()
    if not args.start:
        args.start = f"{today.year}-01-01"
    if not args.end:
        args.end = today.isoformat()
    log.info("Date window: %s .. %s", args.start, args.end)
    validate_dates(args.start, args.end)
    bad = [m for m in args.metric if m not in VALID_METRICS]
    if bad:
        sys.exit(f"Unknown metric(s): {', '.join(bad)}. Valid: {', '.join(sorted(VALID_METRICS))}")

    run_pull(client, args)


if __name__ == "__main__":
    main()
