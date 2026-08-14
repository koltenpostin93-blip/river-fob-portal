"""
FOB Vessel (Fastmarkets) — configuration, archive storage, and fetch.

Tracks headline export FOB benchmarks for corn / soybeans / wheat at the major
origins, in both flat $/mt and the FOB basis (premium, US cents/bu over CME)
where Fastmarkets publishes one. Prices come from the Fastmarkets Physical Prices
API (see fastmarkets.py) and are archived to the shared Postgres so the tab
builds its own history and doesn't depend on the API being up.

Storage: a fob_vessel_history table keyed (as_of, symbol). The symbol→(commodity,
origin, metric) mapping lives in SERIES here, not the DB, so the raw archive stays
small and the meaning is recomputed on read (same pattern as the river archive).
"""
import db
import fastmarkets as FM

ORIGINS = ["US Gulf", "US PNW", "Brazil", "Argentina", "Ukraine"]
COMMODITIES = ["Corn", "Soybeans", "Wheat"]


def _s(group, commodity, origin, label, flat, basis):
    key = f"{group}|{commodity}|{origin}" + (f"|{label}" if label else "")
    return {"group": group, "commodity": commodity, "origin": origin,
            "label": label, "flat": flat, "basis": basis, "key": key}


# The tracked series: flat $/mt symbol + basis c$/bu symbol (or None where
# Fastmarkets has no premium quote). `group` separates FOB export prices, CFR
# China delivered soybeans, and ocean freight routes.
SERIES = [
    # ── Export FOB ($/mt, basis ¢/bu over CME) ──
    _s("FOB", "Corn", "US Gulf",   "",     "AG-CRN-0075", "AG-CRN-0076"),
    _s("FOB", "Corn", "US PNW",    "",     "AG-CRN-0077", "AG-CRN-0078"),
    _s("FOB", "Corn", "Brazil",    "",     "AG-CRN-0071", "AG-CRN-0072"),
    _s("FOB", "Corn", "Argentina", "",     "AG-CRN-0069", "AG-CRN-0070"),
    _s("FOB", "Corn", "Ukraine",   "HIPP", "AG-CRN-0062", "AG-CRN-0063"),
    _s("FOB", "Soybeans", "US Gulf",   "",       "AG-SYB-0020", "AG-SYB-0021"),
    _s("FOB", "Soybeans", "US PNW",    "",       "AG-SYB-0022", "AG-SYB-0023"),
    _s("FOB", "Soybeans", "Brazil",    "Santos", "AG-SYB-0014", "AG-SYB-0015"),
    _s("FOB", "Soybeans", "Argentina", "",       "AG-SYB-0016", "AG-SYB-0017"),
    _s("FOB", "Wheat", "US Gulf",   "HRW",   "AG-WHE-0026", "AG-WHE-0024"),
    _s("FOB", "Wheat", "US Gulf",   "SRW",   "AG-WHE-0058", "AG-WHE-0057"),
    _s("FOB", "Wheat", "US PNW",    "SW",    "AG-WHE-0027", "AG-WHE-0025"),
    _s("FOB", "Wheat", "Argentina", "",      "AG-WHE-0003", None),
    _s("FOB", "Wheat", "Ukraine",   "11.5%", "AG-WHE-0018", None),
    # ── Soybean CFR China (delivered China), by shipment origin ──
    _s("CFR China", "Soybeans", "US Gulf", "", "AG-SYB-0005", "AG-SYB-0006"),
    _s("CFR China", "Soybeans", "US PNW",  "", "AG-SYB-0086", "AG-SYB-0087"),
    _s("CFR China", "Soybeans", "Brazil",  "", "AG-SYB-0003", "AG-SYB-0004"),
    # ── Ocean freight to Northeast Asia (China/Japan), $/mt, no basis ──
    _s("Freight", "Freight", "US Gulf",   "", "FM-FRT-0003", None),
    _s("Freight", "Freight", "US PNW",    "", "FM-FRT-0002", None),
    _s("Freight", "Freight", "Brazil",    "", "FM-FRT-0001", None),
    _s("Freight", "Freight", "Argentina", "", "FM-FRT-0004", None),
]


def view_options():
    """Ordered [{label, group, commodity}] for the trend/spread selectors."""
    out, seen = [], set()
    for r in SERIES:
        gc = (r["group"], r["commodity"])
        if gc in seen:
            continue
        seen.add(gc)
        g, c = gc
        if g == "FOB":
            lbl = f"{c} FOB"
        elif g == "CFR China":
            lbl = f"{c} CFR China"
        elif g == "Freight":
            lbl = "Freight → NE Asia"
        else:
            lbl = f"{c} {g}"
        out.append({"label": lbl, "group": g, "commodity": c})
    return out

# symbol -> (series, metric) so a stored row can be mapped back on read.
SYMBOL_META = {}
for _r in SERIES:
    SYMBOL_META[_r["flat"]] = (_r, "flat")
    if _r["basis"]:
        SYMBOL_META[_r["basis"]] = (_r, "basis")


def configured():
    return FM.configured()


def all_symbols():
    out = []
    for r in SERIES:
        out.append(r["flat"])
        if r["basis"]:
            out.append(r["basis"])
    return sorted(set(out))


# ---------------------------------------------------------------- storage -----
def init_table():
    conn, _ = db._connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fob_vessel_history (
                as_of TEXT NOT NULL,
                symbol TEXT NOT NULL,
                value DOUBLE PRECISION,
                PRIMARY KEY (as_of, symbol)
            )""")
        conn.commit()
    finally:
        conn.close()


def save(rows):
    """Upsert (as_of_iso, symbol, value) rows. -> count written.

    Uses a batched bulk upsert on Postgres (execute_values) — a plain executemany
    is one network round-trip per row, which crawls over the Supabase pooler for
    a multi-year backfill."""
    conn, ph = db._connect()
    try:
        cur = conn.cursor()
        data = [(a, s, float(v)) for a, s, v in rows if v is not None]
        if not data:
            return 0
        if db._is_postgres():
            from psycopg2.extras import execute_values
            execute_values(
                cur,
                "INSERT INTO fob_vessel_history (as_of, symbol, value) VALUES %s "
                "ON CONFLICT (as_of, symbol) DO UPDATE SET value = excluded.value",
                data, page_size=1000)
        else:
            cur.executemany(
                f"INSERT INTO fob_vessel_history VALUES ({ph},{ph},{ph}) "
                f"ON CONFLICT (as_of, symbol) DO UPDATE SET value = excluded.value",
                data)
        conn.commit()
        return len(data)
    finally:
        conn.close()


def load_all(since=None):
    """-> {symbol: {as_of: value}}. `since` (ISO) limits the scan."""
    conn, ph = db._connect()
    where = f" WHERE as_of >= {ph}" if since else ""
    args = (since,) if since else ()
    try:
        cur = conn.cursor()
        cur.execute("SELECT as_of, symbol, value FROM fob_vessel_history" + where,
                    args)
        out = {}
        for a, s, v in cur.fetchall():
            out.setdefault(s, {})[a] = v
        return out
    finally:
        conn.close()


def list_dates():
    conn, _ = db._connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT as_of FROM fob_vessel_history "
                    "ORDER BY as_of DESC")
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


# ------------------------------------------------------------- API fetch ------
def fetch_latest():
    """Most-recent assessment per symbol. -> [(date, symbol, value)]."""
    out = []
    r = FM.prices(all_symbols())
    for it in r.get("instruments", []):
        ps = it.get("prices") or []
        if ps and ps[0].get("mid") is not None:
            out.append((ps[0]["date"], it["symbol"], ps[0]["mid"]))
    return out


def fetch_history(from_date, to_date):
    """Date-range series across all symbols. -> [(date, symbol, value)]."""
    out = []
    r = FM.history(all_symbols(), from_date, to_date)
    for it in r.get("instruments", []):
        sym = it.get("symbol")
        for p in it.get("prices", []):
            if p.get("mid") is not None:
                out.append((p["date"], sym, p["mid"]))
    return out
