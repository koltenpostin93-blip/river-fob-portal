"""
Read-only access to the basis tracker's bid archive, scoped to river terminals.

The bids live in the basis tracker's own Supabase (a different database from
this portal's). We read them here rather than duplicating the dozen-odd
provider scrapers, so there stays exactly one source of truth for bids.

Configured via the BASIS_DATABASE_URL secret. When it isn't set the tab
degrades to a notice instead of raising, so a missing secret can never break
the FOB portal itself.

Everything here is SELECT-only — this portal never writes to the bid archive.

Bid rows carry a free-form `delivery_month` that differs per provider
("July '26", "October 26'", "Dec '26 River Close"), but `futures_symbol`
(ZCU26 / ZCZ26 / ZCH27) is clean and normalized, so it's used as the grouping
key — which also matches how the FOB sheet is organised by contract.
"""
import os

RIVER_FACILITY = "River Terminal"


def _url() -> str:
    return os.environ.get("BASIS_DATABASE_URL", "").strip()


def configured() -> bool:
    """False when the secret isn't set — the tab shows a notice instead."""
    return bool(_url())


def _conn():
    import psycopg2
    import psycopg2.extras
    return psycopg2.connect(_url(), cursor_factory=psycopg2.extras.RealDictCursor)


def current_bids(since_iso: str):
    """Each river terminal's most recent bid sheet, ignoring sheets older than
    `since_iso`. -> [{provider, location, state, timestamp, grain,
    delivery_month, futures_symbol, basis_cents, is_spot}]

    Uses the latest snapshot per (provider, location) rather than the latest row
    per delivery month — otherwise long-expired months stay on the sheet.
    """
    sql = """
        WITH latest_snap AS (
            SELECT DISTINCT ON (s.provider, s.location)
                   s.id, s.provider, s.location, s.timestamp, lm.state
            FROM snapshots s
            JOIN location_meta lm
              ON lm.provider = s.provider AND lm.location = s.location
            WHERE lm.facility_type = %s
            ORDER BY s.provider, s.location, s.timestamp DESC
        )
        SELECT ls.provider, ls.location, ls.state, ls.timestamp,
               r.grain, r.delivery_month, r.futures_symbol,
               r.basis_cents, r.is_spot
        FROM latest_snap ls
        JOIN snapshot_rows r ON r.snapshot_id = ls.id
        WHERE ls.timestamp >= %s
    """
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, (RIVER_FACILITY, since_iso))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def bid_history(grain: str, since_iso: str):
    """Basis history for river terminals for one grain, for the trend chart.
    -> [{timestamp, provider, location, futures_symbol, basis_cents}]"""
    sql = """
        SELECT s.timestamp, s.provider, s.location,
               r.futures_symbol, r.basis_cents
        FROM snapshots s
        JOIN snapshot_rows r ON r.snapshot_id = s.id
        JOIN location_meta lm
          ON lm.provider = s.provider AND lm.location = s.location
        WHERE lm.facility_type = %s
          AND r.grain = %s
          AND s.timestamp >= %s
          AND r.basis_cents IS NOT NULL
        ORDER BY s.timestamp
    """
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, (RIVER_FACILITY, grain, since_iso))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
