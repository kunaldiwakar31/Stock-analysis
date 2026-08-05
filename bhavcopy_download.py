#!/usr/bin/env python3
"""
Download the NSE UDiFF bhavcopy for one or more trading days and upload each to the
stock-screener API.

Equivalent of:
    curl -H 'User-Agent: ...' -H 'Referer: ...' https://nsearchives.../BhavCopy_..._F_0000.csv.zip
    unzip
    curl -F file=@... http://localhost:8080/api/v1/prices/bhavcopy?tradeDate=...

Why this needs more than a bare curl in practice:

- NSE serves nsearchives.nseindia.com behind the same bot-detection as the main site.
  A request with no prior cookie frequently gets a 403 even with a normal User-Agent.
  This script warms up a session against nseindia.com first, the way a browser would,
  before requesting the archive host.
- Weekends and holidays 404. That is expected, not a bug - the script treats it as
  "no trading day" and moves on instead of erroring.
- The response is a zip containing exactly one CSV, so it never touches disk twice:
  the zip is read into memory, the CSV member is extracted directly, and that is what
  gets POSTed to the API.

Usage:
    python download_bhavcopy.py 2026-08-04
    python download_bhavcopy.py 2026-08-01 2026-08-07          # inclusive range, skips weekends
    python download_bhavcopy.py 2026-08-04 --api http://localhost:8080/api/v1
    python download_bhavcopy.py 2026-08-04 --save-dir ./bhavcopies   # also keep the CSV on disk
    python download_bhavcopy.py 2026-08-04 --dry-run             # download + parse only, no upload

Requires: requests  (pip install requests)
"""

import argparse
import datetime as dt
import io
import sys
import time
import zipfile
from pathlib import Path

import requests

NSE_HOME = "https://www.nseindia.com"
NSE_REPORTS_PAGE = "https://www.nseindia.com/all-reports"
BHAVCOPY_URL_TEMPLATE = (
    "https://nsearchives.nseindia.com/content/cm/"
    "BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip"
)

# A plain requests default UA gets blocked outright; this is what a real browser sends.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def make_session() -> requests.Session:
    """A session carrying the cookies NSE's archive host expects to see."""
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    # Visiting the homepage and the reports page first is what sets the cookies that
    # let the next request to nsearchives.nseindia.com through. Skipping this step is
    # the most common cause of a 403 here.
    s.get(NSE_HOME, timeout=15)
    s.get(NSE_REPORTS_PAGE, timeout=15, headers={"Referer": NSE_HOME})
    return s


def fetch_bhavcopy_csv(session: requests.Session, trade_date: dt.date) -> bytes | None:
    """
    Downloads and unzips one day's bhavcopy.

    Returns the raw CSV bytes, or None if the day has no bhavcopy (weekend/holiday).
    Raises for any other HTTP failure so a real outage isn't silently skipped.
    """
    url = BHAVCOPY_URL_TEMPLATE.format(yyyymmdd=trade_date.strftime("%Y%m%d"))
    resp = session.get(url, headers={"Referer": NSE_REPORTS_PAGE}, timeout=30)

    if resp.status_code == 404:
        return None
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
        if not names:
            raise RuntimeError(f"{url} returned an empty zip")
        # The archive holds exactly one CSV; take it by name rather than assuming index 0
        # in case NSE ever adds a readme alongside it.
        csv_name = next((n for n in names if n.lower().endswith(".csv")), names[0])
        return zf.read(csv_name)


def upload_to_screener(csv_bytes: bytes, trade_date: dt.date, api_base: str) -> dict:
    """POSTs the CSV to /prices/bhavcopy. Returns the parsed LoadSummary JSON."""
    url = f"{api_base.rstrip('/')}/prices/bhavcopy"
    files = {"file": (f"bhavcopy-{trade_date.isoformat()}.csv", csv_bytes, "text/csv")}
    params = {"tradeDate": trade_date.isoformat()}
    resp = requests.post(url, files=files, params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()


def daterange(start: dt.date, end: dt.date):
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def is_weekend(d: dt.date) -> bool:
    return d.weekday() >= 5  # Saturday=5, Sunday=6


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("start_date", help="YYYY-MM-DD")
    parser.add_argument("end_date", nargs="?", default=None,
                        help="YYYY-MM-DD (inclusive). Omit for a single day.")
    parser.add_argument("--api", default="http://localhost:8080/api/v1",
                        help="Base URL of the stock-screener API")
    parser.add_argument("--save-dir", default=None,
                        help="Also write each day's CSV to this directory")
    parser.add_argument("--dry-run", action="store_true",
                        help="Download and unzip only; skip the upload")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="Seconds to wait between days (politeness). Default 1.0")
    args = parser.parse_args()

    start = dt.date.fromisoformat(args.start_date)
    end = dt.date.fromisoformat(args.end_date) if args.end_date else start

    if args.save_dir:
        Path(args.save_dir).mkdir(parents=True, exist_ok=True)

    session = make_session()

    ok, skipped, failed = 0, 0, 0
    for i, d in enumerate(daterange(start, end)):
        if i > 0:
            time.sleep(args.delay)

        if is_weekend(d):
            print(f"{d}  weekend, skipping")
            skipped += 1
            continue

        try:
            csv_bytes = fetch_bhavcopy_csv(session, d)
        except requests.HTTPError as e:
            print(f"{d}  FAILED to download: {e}", file=sys.stderr)
            failed += 1
            continue

        if csv_bytes is None:
            # 404 on a weekday almost always means a market holiday.
            print(f"{d}  no bhavcopy (holiday?), skipping")
            skipped += 1
            continue

        if args.save_dir:
            out = Path(args.save_dir) / f"bhavcopy-{d.isoformat()}.csv"
            out.write_bytes(csv_bytes)

        if args.dry_run:
            line_count = csv_bytes.count(b"\n")
            print(f"{d}  downloaded {len(csv_bytes):,} bytes, ~{line_count:,} lines "
                  f"(dry run, not uploaded)")
            ok += 1
            continue

        try:
            summary = upload_to_screener(csv_bytes, d, args.api)
        except requests.HTTPError as e:
            body = e.response.text[:300] if e.response is not None else ""
            print(f"{d}  FAILED to upload: {e}  {body}", file=sys.stderr)
            failed += 1
            continue

        print(f"{d}  saved={summary.get('saved')} "
              f"unknownSymbols={summary.get('unknownSymbols')} "
              f"rowsRead={summary.get('rowsRead')}")
        ok += 1

    print(f"\ndone: {ok} loaded, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
