"""
One-time migration: copy the local SQLite archive into the Postgres (Supabase) DB.

Run it from your own machine so your DB password stays local — it's read from the
DATABASE_URL environment variable, never hard-coded.

PowerShell:
    cd river-fob-portal
    $env:DATABASE_URL = "postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres"
    python migrate_to_postgres.py

It's safe to re-run: rows are upserted on their primary keys, so a second run
just refreshes existing rows rather than duplicating them.
"""
import os
import sqlite3
import sys

import db  # reuses the same connection logic + table DDL

LOCAL_SQLITE = db.LOCAL_SQLITE

TABLES = {
    # table: (columns, conflict_key_columns)
    "cif_history":      (("as_of", "commodity", "month", "value"),
                         ("as_of", "commodity", "month")),
    "freight_history":  (("as_of", "region", "month", "value"),
                         ("as_of", "region", "month")),
    "calendar_history": (("as_of", "commodity", "seq", "month", "contract"),
                         ("as_of", "commodity", "seq")),
}


def main():
    if not db._is_postgres():
        sys.exit("DATABASE_URL is not set to a Postgres URL. Set it first "
                 "(see the docstring at the top of this file).")

    if not os.path.exists(LOCAL_SQLITE):
        sys.exit(f"Local SQLite archive not found: {LOCAL_SQLITE}")

    # Make sure the Postgres tables exist (same DDL the app uses).
    print("Connecting to Postgres and ensuring tables exist...")
    db.init_db()

    src = sqlite3.connect(LOCAL_SQLITE)
    pg, ph = db._connect()  # ph == "%s" for Postgres
    try:
        from psycopg2.extras import execute_values
        cur = pg.cursor()
        for table, (cols, key) in TABLES.items():
            rows = src.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
            if not rows:
                print(f"  {table}: nothing to copy")
                continue
            collist = ", ".join(cols)
            updates = ", ".join(f"{c}=EXCLUDED.{c}"
                                for c in cols if c not in key)
            sql = (f"INSERT INTO {table} ({collist}) VALUES %s "
                   f"ON CONFLICT ({', '.join(key)}) DO UPDATE SET {updates}")
            execute_values(cur, sql, rows, page_size=500)
            print(f"  {table}: migrated {len(rows)} rows")
        pg.commit()

        # Report what's now in Postgres.
        cur.execute("SELECT COUNT(DISTINCT as_of), MIN(as_of), MAX(as_of) "
                    "FROM cif_history")
        cnt, lo, hi = cur.fetchone()
        print(f"\nDone. Postgres now holds {cnt} dates ({lo} -> {hi}).")
    finally:
        src.close()
        pg.close()


if __name__ == "__main__":
    main()
