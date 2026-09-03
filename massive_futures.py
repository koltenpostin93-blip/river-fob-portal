"""
Live CBOT futures for the FOB sheet, from the Massive futures API.

Maps the sheet's contract chain (CU/CZ/CH …, SX/SF/SH …, WU/WZ/WH …) to the
matching Massive outright contract's settlement and returns prices in $/bu — the
unit the archive/sheet use. This replaces the workbook's Barchart-add-in CBOT row,
which is garbage whenever the add-in is disconnected on save.

Key from the environment (MASSIVE_API_KEY, in the gitignored .env / secrets).
"""
import os
import re
import datetime as dt

import massive_api as MA

# FOB commodity -> Massive product code. Wheat = Chicago/SRW (ZW), matching the
# river sheet's Chicago wheat futures.
PRODUCT = {"Corn": "ZC", "Soybeans": "ZS", "Wheat": "ZW"}
_LETTER_RE = re.compile(r"^[A-Z]{1,3}([FGHJKMNQUVXZ])\d$")


def configured():
    return bool(os.environ.get("MASSIVE_API_KEY", "").strip())


def _key():
    return os.environ["MASSIVE_API_KEY"].strip()


def cbot_curve(commodity, as_of=None):
    """{month_letter: price $/bu} for the nearest CBOT contracts of a commodity.
    Massive quotes grains in cents/bu, so divide by 100."""
    as_of = as_of or dt.date.today()
    df = MA.get_futures_curve(PRODUCT[commodity], _key(), as_of, n_contracts=14)
    out = {}
    for _, r in df.iterrows():
        m = _LETTER_RE.match(str(r["ticker"]))
        if not m or not r["price"]:
            continue
        letter = m.group(1)
        if letter not in out:                       # nearest-first: keep the front
            out[letter] = round(float(r["price"]) / 100.0, 4)
    return out


# Full-carry convention (matches the cost-of-carry calculator): the reference
# workbook uses fed funds + 2.25% as the interest leg of full carry.
FED_FUNDS_SPREAD_PCT = 2.25


def fed_funds_rate(as_of=None):
    """Live front-month fed funds rate (%) from CME ZQ, or None if unavailable.
    Same source the cost-of-carry calculator uses."""
    if not configured():
        return None
    as_of = as_of or dt.date.today()
    try:
        return MA.get_fed_funds_rate(_key(), as_of)["rate_pct"]
    except Exception:
        return None


def interest_rate_pct(as_of=None):
    """Default annual interest rate for full carry: live fed funds + 2.25%.
    Returns None if the live rate can't be read (caller falls back to a static
    seed)."""
    ff = fed_funds_rate(as_of)
    return None if ff is None else round(ff + FED_FUNDS_SPREAD_PCT, 2)


def futures_for_calendar(calendar, as_of=None):
    """calendar: {commodity: [(month, contract), ...]} from a snapshot's chain.
    -> {commodity: {month: price $/bu}} using live CBOT settlements. Commodities
    without a Massive product (none here) and contracts with no live price are
    simply omitted, so callers can fall back to the workbook value."""
    out = {}
    for commodity, cols in (calendar or {}).items():
        if commodity not in PRODUCT:
            continue
        try:
            curve = cbot_curve(commodity, as_of)
        except Exception:
            continue
        row = {}
        for month, contract in cols:
            if not contract:
                continue
            price = curve.get(str(contract)[-1].upper())
            if price is not None:
                row[month] = price
        if row:
            out[commodity] = row
    return out
