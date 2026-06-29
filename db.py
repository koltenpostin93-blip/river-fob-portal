"""
Archive storage for the River FOB portal.

Only the independent inputs are stored — CIF basis and barge freight — keyed by
as-of date. Everything else (FOB, cash vs delivery, carry) is recomputed from
these on read, so history stays small and always reflects the current formulas.

Backend: SQLite by default (a local file), or Postgres when DATABASE_URL is set
(e.g. postgresql://user:pass@host/db). Same SQL either way.
"""
import os
import sqlite3

LOCAL_SQLITE = os.path.join(os.path.dirname(__file__), "river_fob_history.db")


def _database_url():
    return os.environ.get("DATABASE_URL", "").strip()


def _is_postgres():
    return _database_url().startswith(("postgres://", "postgresql://"))


def _pg_dsn():
    """Postgres URL with sslmode forced on (Supabase and most managed PG
    require SSL; pooler URLs copied from the dashboard often omit it)."""
    url = _database_url()
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


def _connect():
    """Return (connection, paramstyle_placeholder)."""
    if _is_postgres():
        import psycopg2
        return psycopg2.connect(_pg_dsn()), "%s"
    conn = sqlite3.connect(LOCAL_SQLITE)
    return conn, "?"


def backend_name():
    return "Postgres" if _is_postgres() else f"SQLite ({os.path.basename(LOCAL_SQLITE)})"


def init_db():
    conn, _ = _connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cif_history (
                as_of TEXT NOT NULL,
                commodity TEXT NOT NULL,
                month TEXT NOT NULL,
                value DOUBLE PRECISION,
                PRIMARY KEY (as_of, commodity, month)
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS freight_history (
                as_of TEXT NOT NULL,
                region TEXT NOT NULL,
                month TEXT NOT NULL,
                value DOUBLE PRECISION,
                PRIMARY KEY (as_of, region, month)
            )""")
        # Delivery window (calendar month) + the futures month (contract code)
        # for each column, per commodity, as they stood on that date.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS calendar_history (
                as_of TEXT NOT NULL,
                commodity TEXT NOT NULL,
                seq INTEGER NOT NULL,
                month TEXT NOT NULL,
                contract TEXT,
                PRIMARY KEY (as_of, commodity, seq)
            )""")
        conn.commit()
    finally:
        conn.close()


def save_snapshot(as_of, cif_by_commodity, freight_by_region, calendar=None):
    """Upsert one day's inputs. as_of is an ISO date string.

    calendar (optional): {commodity: [(month, contract), ...]} — the delivery
    windows and the futures month associated with each column on that date.
    """
    conn, ph = _connect()
    try:
        cur = conn.cursor()
        for t in ("cif_history", "freight_history", "calendar_history"):
            cur.execute(f"DELETE FROM {t} WHERE as_of = {ph}", (as_of,))
        cif_rows = [(as_of, c, m, _f(v))
                    for c, mv in cif_by_commodity.items()
                    for m, v in mv.items() if _f(v) is not None]
        frt_rows = [(as_of, r, m, _f(v))
                    for r, mv in freight_by_region.items()
                    for m, v in mv.items() if _f(v) is not None]
        cal_rows = [(as_of, c, i, m, ct)
                    for c, cols in (calendar or {}).items()
                    for i, (m, ct) in enumerate(cols)]
        if cif_rows:
            cur.executemany(
                f"INSERT INTO cif_history VALUES ({ph},{ph},{ph},{ph})", cif_rows)
        if frt_rows:
            cur.executemany(
                f"INSERT INTO freight_history VALUES ({ph},{ph},{ph},{ph})", frt_rows)
        if cal_rows:
            cur.executemany(
                f"INSERT INTO calendar_history VALUES ({ph},{ph},{ph},{ph},{ph})", cal_rows)
        conn.commit()
        return len(cif_rows), len(frt_rows)
    finally:
        conn.close()


def list_dates():
    """All archived as-of dates, newest first."""
    conn, _ = _connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT as_of FROM cif_history
            UNION SELECT as_of FROM freight_history
            ORDER BY as_of DESC""")
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def load_snapshot(as_of):
    """Return (cif_by_commodity, freight_by_region, calendar) for a date.

    calendar: {commodity: [(month, contract), ...]} in column order.
    Returns (None, None, None) if the date has no data.
    """
    conn, ph = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT commodity, month, value FROM cif_history WHERE as_of = {ph}",
            (as_of,))
        cif = {}
        for c, m, v in cur.fetchall():
            cif.setdefault(c, {})[m] = v
        cur.execute(
            f"SELECT region, month, value FROM freight_history WHERE as_of = {ph}",
            (as_of,))
        frt = {}
        for r, m, v in cur.fetchall():
            frt.setdefault(r, {})[m] = v
        cur.execute(
            f"SELECT commodity, seq, month, contract FROM calendar_history "
            f"WHERE as_of = {ph} ORDER BY commodity, seq", (as_of,))
        cal = {}
        for c, _seq, m, ct in cur.fetchall():
            cal.setdefault(c, []).append((m, ct))
        if not cif and not frt:
            return None, None, None
        return cif, frt, cal
    finally:
        conn.close()


def fetch_all():
    """Bulk-load the whole archive for analytics. -> (cif, freight, calendar)
    each keyed {as_of: {...}}. Used by the seasonal chart."""
    conn, _ = _connect()
    try:
        cur = conn.cursor()
        cif = {}
        cur.execute("SELECT as_of, commodity, month, value FROM cif_history")
        for d, c, m, v in cur.fetchall():
            cif.setdefault(d, {}).setdefault(c, {})[m] = v
        frt = {}
        cur.execute("SELECT as_of, region, month, value FROM freight_history")
        for d, r, m, v in cur.fetchall():
            frt.setdefault(d, {}).setdefault(r, {})[m] = v
        cal = {}
        cur.execute("SELECT as_of, commodity, seq, month, contract "
                    "FROM calendar_history ORDER BY as_of, commodity, seq")
        for d, c, _s, m, ct in cur.fetchall():
            cal.setdefault(d, {}).setdefault(c, []).append((m, ct))
        return cif, frt, cal
    finally:
        conn.close()


def _f(v):
    try:
        if v is None:
            return None
        import math
        v = float(v)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None
