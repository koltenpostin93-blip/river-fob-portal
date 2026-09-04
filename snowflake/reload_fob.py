"""One-shot (re-runnable) reload of the River FOB archive from Supabase Postgres
into Snowflake JSA.BASIS_TRACKER.

The five history tables already exist in Snowflake (created with the basis
tracker's migration) but drifted stale; this refreshes them to match the live
Supabase copy — the 20-year backfill included. Each table is reloaded inside one
transaction (DELETE + chunked INSERT + COMMIT) so any reader (e.g. the basis
tracker's River FOB tab) sees either the old rows or the new rows, never a
half-loaded table.

Run:  python snowflake/reload_fob.py
Reads Supabase from DATABASE_URL; writes Snowflake from SNOWFLAKE_* — both in the
project .env. Idempotent: safe to run repeatedly (e.g. right before cutover).
"""
import os
import sys
import pathlib

from dotenv import load_dotenv

PROJ = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(PROJ / ".env", override=True)
sys.path.insert(0, str(PROJ))

import db  # noqa: E402  (uses the project's own connection helpers)

# table -> ordered columns (must match both schemas; Snowflake insert is positional)
TABLES = {
    "cif_history": ["as_of", "commodity", "month", "value"],
    "freight_history": ["as_of", "region", "month", "value"],
    "calendar_history": ["as_of", "commodity", "seq", "month", "contract"],
    "futures_history": ["as_of", "commodity", "month", "value"],
    "spreads_history": ["as_of", "commodity", "seq", "label", "value"],
}
CHUNK = 1500  # rows per INSERT (×5 cols max = 7500 binds, well under the limit)


def _pg_conn():
    """Direct Supabase (Postgres) read connection, independent of USE_SNOWFLAKE."""
    if not db._is_postgres():
        sys.exit("DATABASE_URL is not a Postgres URL — refusing (need the Supabase "
                 "source to read from).")
    import psycopg2
    return psycopg2.connect(db._pg_dsn())


def _read_pg(pg, table, cols):
    cur = pg.cursor()
    cur.execute(f"SELECT {', '.join(cols)} FROM {table}")
    return cur.fetchall()


def _insert_chunks(sf_cur, table, cols, rows):
    ncol = len(cols)
    row_ph = "(" + ",".join(["%s"] * ncol) + ")"
    for i in range(0, len(rows), CHUNK):
        part = rows[i:i + CHUNK]
        values = ",".join([row_ph] * len(part))
        sf_cur.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES {values}",
                       [v for row in part for v in row])


def main():
    acct = os.environ.get("SNOWFLAKE_ACCOUNT", "")
    sdb = os.environ.get("SNOWFLAKE_DATABASE", "")
    ssc = os.environ.get("SNOWFLAKE_SCHEMA", "")
    if not (acct and sdb and ssc):
        sys.exit("SNOWFLAKE_ACCOUNT / DATABASE / SCHEMA must be set in .env.")
    print(f"Target Snowflake: {sdb}.{ssc} (account {acct})")

    pg = _pg_conn()
    sf = db._sf_connect()
    try:
        sf.autocommit(False)
    except Exception:
        pass
    sf_cur = sf.cursor()
    sf_cur.execute(f"USE DATABASE {sdb}")
    sf_cur.execute(f"USE SCHEMA {ssc}")

    ok = True
    for table, cols in TABLES.items():
        rows = _read_pg(pg, table, cols)
        try:
            sf_cur.execute(f"DELETE FROM {table}")
            _insert_chunks(sf_cur, table, cols, rows)
            sf.commit()
        except Exception as e:
            sf.rollback()
            print(f"  {table}: FAILED, rolled back — {e}")
            ok = False
            continue
        sf_cur.execute(f"SELECT COUNT(*) FROM {table}")
        n_sf = sf_cur.fetchone()[0]
        flag = "OK" if n_sf == len(rows) else "MISMATCH"
        if n_sf != len(rows):
            ok = False
        print(f"  {table:18s} PG={len(rows):>6}  SF={n_sf:>6}  {flag}")

    pg.close()
    sf.close()
    print("DONE" if ok else "DONE WITH ERRORS")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
