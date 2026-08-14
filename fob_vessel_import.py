"""
Daily FOB Vessel import — pull the latest Fastmarkets export-FOB assessments for
corn / soybeans / wheat across the tracked origins and upsert them into the
fob_vessel_history archive.

Run daily:   python fob_vessel_import.py
Backfill:    python fob_vessel_import.py --backfill 2024-08-13 2026-08-12
             (Fastmarkets history is capped at a 2-year range per call.)

Needs DATABASE_URL + FOB_VESSEL_SERVICE_NAME + FOB_VESSEL_API_KEY (from .env /
Streamlit secrets).
"""
import os
import sys
import datetime as dt
import warnings

warnings.filterwarnings("ignore")
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
                override=True)
except ImportError:
    pass

import db
import fob_vessel as V


def main():
    if not db._is_postgres():
        print("DATABASE_URL not set — refusing to write to the SQLite fallback.")
        sys.exit(1)
    if not V.configured():
        print("Fastmarkets creds not set (FOB_VESSEL_SERVICE_NAME / "
              "FOB_VESSEL_API_KEY).")
        sys.exit(1)
    V.init_table()

    args = sys.argv[1:]
    if "--backfill" in args:
        i = args.index("--backfill")
        lo, hi = args[i + 1], args[i + 2]
        rows = V.fetch_history(lo, hi)
        print(f"backfill {lo} -> {hi}: fetched {len(rows)}, saved {V.save(rows)}")
        return

    # Refresh a trailing window rather than just the latest snapshot, so a late
    # or revised assessment (Fastmarkets publishes across the day) still lands.
    today = dt.date.today()
    lo = (today - dt.timedelta(days=7)).isoformat()
    rows = V.fetch_history(lo, today.isoformat())
    n = V.save(rows)
    latest = max((r[0] for r in rows), default="—")
    print(f"{today}: refreshed {n} rows over {lo}..{today} (latest {latest}).")


if __name__ == "__main__":
    main()
