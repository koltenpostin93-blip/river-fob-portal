"""Create the standalone RIVER_FOB Snowflake database and load the FOB archive
into it from Supabase.

The five FOB history tables were originally loaded into JSA.BASIS_TRACKER (shared
with the basis tracker). Per Kolten, the portal should own its data in its own
database. This creates RIVER_FOB.PUBLIC, its five tables, and loads them from the
live Supabase copy — atomically per table (DELETE + chunked INSERT + COMMIT).

Non-destructive: it does NOT touch JSA.BASIS_TRACKER (those get dropped later,
once the portal + basis tracker both read RIVER_FOB).

Run:  python snowflake/setup_standalone.py
Reads Supabase from DATABASE_URL; connects Snowflake from SNOWFLAKE_* (.env).
Idempotent (CREATE IF NOT EXISTS + reload).
"""
import sys
import pathlib

from dotenv import load_dotenv

PROJ = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(PROJ / ".env", override=True)
sys.path.insert(0, str(PROJ))

import db  # noqa: E402

TARGET_DB = "RIVER_FOB"
TARGET_SCHEMA = "PUBLIC"

DDL = {
    "cif_history": "as_of VARCHAR, commodity VARCHAR, month VARCHAR, value FLOAT",
    "freight_history": "as_of VARCHAR, region VARCHAR, month VARCHAR, value FLOAT",
    "calendar_history": "as_of VARCHAR, commodity VARCHAR, seq NUMBER, month VARCHAR, contract VARCHAR",
    "futures_history": "as_of VARCHAR, commodity VARCHAR, month VARCHAR, value FLOAT",
    "spreads_history": "as_of VARCHAR, commodity VARCHAR, seq NUMBER, label VARCHAR, value FLOAT",
}
COLS = {
    "cif_history": ["as_of", "commodity", "month", "value"],
    "freight_history": ["as_of", "region", "month", "value"],
    "calendar_history": ["as_of", "commodity", "seq", "month", "contract"],
    "futures_history": ["as_of", "commodity", "month", "value"],
    "spreads_history": ["as_of", "commodity", "seq", "label", "value"],
}
CHUNK = 1500


def _pg():
    if not db._is_postgres():
        sys.exit("DATABASE_URL must be the Supabase source.")
    import psycopg2
    return psycopg2.connect(db._pg_dsn())


def main():
    pg = _pg()
    sf = db._sf_connect()
    try:
        sf.autocommit(False)
    except Exception:
        pass
    c = sf.cursor()
    c.execute(f"CREATE DATABASE IF NOT EXISTS {TARGET_DB}")
    c.execute(f"USE DATABASE {TARGET_DB}")
    c.execute(f"CREATE SCHEMA IF NOT EXISTS {TARGET_SCHEMA}")
    c.execute(f"USE SCHEMA {TARGET_SCHEMA}")
    for t, cols in DDL.items():
        c.execute(f"CREATE TABLE IF NOT EXISTS {t} ({cols})")
    sf.commit()
    print(f"Schema ready: {TARGET_DB}.{TARGET_SCHEMA}")

    ok = True
    for t, cols in COLS.items():
        pc = pg.cursor()
        pc.execute(f"SELECT {', '.join(cols)} FROM {t}")
        rows = pc.fetchall()
        try:
            c.execute(f"DELETE FROM {t}")
            row_ph = "(" + ",".join(["%s"] * len(cols)) + ")"
            for i in range(0, len(rows), CHUNK):
                part = rows[i:i + CHUNK]
                c.execute(
                    f"INSERT INTO {t} ({', '.join(cols)}) VALUES "
                    + ",".join([row_ph] * len(part)),
                    [v for row in part for v in row])
            sf.commit()
        except Exception as e:
            sf.rollback()
            print(f"  {t}: FAILED, rolled back — {e}")
            ok = False
            continue
        c.execute(f"SELECT COUNT(*) FROM {t}")
        n = c.fetchone()[0]
        print(f"  {t:18s} PG={len(rows):>6}  SF={n:>6}  "
              + ("OK" if n == len(rows) else "MISMATCH"))
        ok = ok and n == len(rows)
    pg.close()
    sf.close()
    print("DONE" if ok else "DONE WITH ERRORS")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
