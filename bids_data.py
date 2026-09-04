"""
Read-only access to the basis tracker's bid archive, scoped to river terminals.

The bids live in the basis tracker's database. Since that app moved to Snowflake
(JSA.BASIS_TRACKER), we read them from Snowflake too when USE_SNOWFLAKE is set —
the same connection the FOB archive uses — and otherwise fall back to the basis
tracker's Postgres via BASIS_DATABASE_URL. We read rather than duplicate the
dozen-odd provider scrapers, so there stays exactly one source of truth for bids.

When neither source is configured the tab degrades to a notice instead of
raising, so a missing secret can never break the FOB portal itself.

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


def _use_snowflake() -> bool:
    return os.environ.get("USE_SNOWFLAKE", "").strip().lower() in (
        "1", "true", "yes", "on")


def configured() -> bool:
    """False when no source is available — the tab shows a notice instead.
    Snowflake (JSA.BASIS_TRACKER) counts as configured on its own; otherwise a
    BASIS_DATABASE_URL is required."""
    return _use_snowflake() or bool(_url())


def source_name() -> str:
    return "Snowflake" if _use_snowflake() else "Postgres"


def _pg_rows(sql, params):
    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(_url(),
                            cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _sf_rows(sql, params):
    """Run a bids query on Snowflake. The bids tables share the FOB archive's
    JSA.BASIS_TRACKER schema, so we reuse the portal's Snowflake connection.
    Snowflake returns UPPERCASE column names — lowercase them so downstream code
    (r['basis_cents'], r['futures_symbol'] …) is unchanged."""
    import db
    import snowflake.connector
    conn = db._sf_connect()
    try:
        cur = conn.cursor(snowflake.connector.DictCursor)
        cur.execute(sql, params)
        return [{k.lower(): v for k, v in r.items()} for r in cur.fetchall()]
    finally:
        conn.close()


# --- current bids: latest snapshot per (provider, location) --------------
# Postgres uses DISTINCT ON; Snowflake has no DISTINCT ON, so it uses QUALIFY
# ROW_NUMBER() to keep one row per (provider, location) — same result.
_CURRENT_PG = """
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

_CURRENT_SF = """
    WITH latest_snap AS (
        SELECT s.id, s.provider, s.location, s.timestamp, lm.state
        FROM snapshots s
        JOIN location_meta lm
          ON lm.provider = s.provider AND lm.location = s.location
        WHERE lm.facility_type = %s
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY s.provider, s.location ORDER BY s.timestamp DESC) = 1
    )
    SELECT ls.provider, ls.location, ls.state, ls.timestamp,
           r.grain, r.delivery_month, r.futures_symbol,
           r.basis_cents, r.is_spot
    FROM latest_snap ls
    JOIN snapshot_rows r ON r.snapshot_id = ls.id
    WHERE ls.timestamp >= %s
"""

_HISTORY_SQL = """
    SELECT s.timestamp, s.provider, s.location,
           r.delivery_month, r.futures_symbol, r.basis_cents
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


def current_bids(since_iso: str):
    """Each river terminal's most recent bid sheet, ignoring sheets older than
    `since_iso`. -> [{provider, location, state, timestamp, grain,
    delivery_month, futures_symbol, basis_cents, is_spot}]

    Uses the latest snapshot per (provider, location) rather than the latest row
    per delivery month — otherwise long-expired months stay on the sheet.
    """
    params = (RIVER_FACILITY, since_iso)
    if _use_snowflake():
        return _sf_rows(_CURRENT_SF, params)
    return _pg_rows(_CURRENT_PG, params)


def bid_history(grain: str, since_iso: str):
    """Basis history for river terminals for one grain, for the trend chart.
    -> [{timestamp, provider, location, delivery_month, futures_symbol,
         basis_cents}]"""
    params = (RIVER_FACILITY, grain, since_iso)
    if _use_snowflake():
        return _sf_rows(_HISTORY_SQL, params)
    return _pg_rows(_HISTORY_SQL, params)
