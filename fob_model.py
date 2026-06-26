"""
River FOB model — configuration and calculation.

Mirrors the JSA FOB Sheet workbook. The core relationship is:

    FOB[location][month] = CIF[month] - (TariffFactor * Freight%[region][month]) / 2000 * BushelWeight

Freight % is entered once (shared across all commodities). CIF and CBOT futures
are per-commodity. Bushel weight differs by commodity (corn 56, soy/wheat 60).
"""
from __future__ import annotations
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Static structure
# ---------------------------------------------------------------------------

MONTHS = ["June", "July", "Aug", "Sep", "Oct", "Nov", "Dec", "Jan"]

COMMODITIES = ["Corn", "Soybeans", "Wheat"]

BUSHEL_WEIGHT = {"Corn": 56, "Soybeans": 60, "Wheat": 60}

# Futures contract symbol mapped to each month column, per commodity.
CONTRACTS = {
    "Corn":     ["CN", "CN", "CU", "CU", "CZ", "CZ", "CZ", "CH"],
    "Soybeans": ["SN", "SN", "SQ", "SX", "SX", "SX", "SF", "SF"],
    "Wheat":    ["WN", "WN", "WU", "WU", "WZ", "WZ", "WZ", "WH"],
}

# Freight reaches the user enters (the % of tariff, by month).
# MTCT mirrors Lower Miss in the sheet, so Memphis/Cairo draw from Lower Miss.
FREIGHT_REGIONS = [
    "Lower Miss",
    "Davenport South",
    "McGregor South",
    "Upper Miss",
    "Ohio",
    "STL",
    "IL",
]


@dataclass(frozen=True)
class Location:
    name: str            # display name (e.g. "FOB Barge Greenville")
    factor: float        # published tariff rate ($/ton at 100%)
    region: str          # which freight reach feeds it
    reach: str           # river-reach heading for display grouping


# Ordered exactly as the workbook lists them, grouped by river reach.
LOCATIONS = [
    # Lower Mississippi
    Location("Greenville",        2.29, "Lower Miss",      "Lower Mississippi"),
    Location("Memphis",           3.14, "Lower Miss",      "Lower Mississippi"),
    Location("Cairo",             3.80, "Lower Miss",      "Lower Mississippi"),
    # Mid Miss
    Location("Quincy",            4.84, "Davenport South", "Mid Mississippi"),
    Location("Burlington",        5.08, "Davenport South", "Mid Mississippi"),
    Location("Davenport",         5.32, "Davenport South", "Mid Mississippi"),
    Location("Prairie du Chien",  6.00, "McGregor South",  "Mid Mississippi"),
    # Gulfport — discontinued (appears in older sheets, e.g. 2023). Kept so its
    # historical FOB recomputes; intentionally not in BLOCK_LAYOUT (live view).
    Location("Gulfport",          5.08, "Davenport South", "Mid Mississippi"),
    # Upper Mississippi
    Location("Savage",            6.19, "Upper Miss",      "Upper Mississippi"),
    # Ohio River
    Location("MTV",               3.99, "Ohio",            "Ohio River"),
    Location("Louisville",        4.46, "Ohio",            "Ohio River"),
    Location("Cincy",             4.69, "Ohio",            "Ohio River"),
    # St. Louis
    Location("STL",               3.99, "STL",             "St. Louis"),
    # Illinois River
    Location("Chicago",           5.78, "IL",              "Illinois River"),
    Location("Seneca",            5.24, "IL",              "Illinois River"),
    Location("Hennepin",          5.07, "IL",              "Illinois River"),
    Location("Peoria",            4.81, "IL",              "Illinois River"),
    Location("Havana",            4.64, "IL",              "Illinois River"),
]

REACH_ORDER = [
    "Lower Mississippi", "Mid Mississippi", "Upper Mississippi",
    "Ohio River", "St. Louis", "Illinois River",
]

FACTOR = {loc.name: loc.factor for loc in LOCATIONS}

# Exact vertical row order of a commodity block, mirroring the workbook.
# Each entry is one of:
#   ("reach",   reach_heading)                      grey centered header row
#   ("freight", region, label)                      italic freight row (shown as %)
#   ("fob",     location_name)                       FOB barge row (2dp, red negs)
# MTCT mirrors Lower Miss (Memphis/Cairo draw from it), exactly as in the sheet.
BLOCK_LAYOUT = [
    ("reach", "Illinois River"),
    ("freight", "IL", "IL Freight"),
    ("fob", "Chicago"),
    ("fob", "Seneca"),
    ("fob", "Hennepin"),
    ("fob", "Peoria"),
    ("fob", "Havana"),
    ("reach", "St. Louis"),
    ("freight", "STL", "STL Freight"),
    ("fob", "STL"),
    ("reach", "Upper Mississippi"),
    ("freight", "Upper Miss", "Upper Miss Freight"),
    ("fob", "Savage"),
    ("reach", "Mid Miss"),
    ("freight", "Davenport South", "Davenport South Freight"),
    ("fob", "Quincy"),
    ("fob", "Burlington"),
    ("fob", "Davenport"),
    ("freight", "McGregor South", "McGregor South Freight"),
    ("fob", "Prairie du Chien"),
    ("reach", "Ohio River"),
    ("freight", "Ohio", "Ohio Freight"),
    ("fob", "MTV"),
    ("fob", "Louisville"),
    ("fob", "Cincy"),
    ("reach", "Lower Mississippi"),
    ("freight", "Lower Miss", "Lower Miss Freight"),
    ("fob", "Greenville"),
    ("freight", "Lower Miss", "MTCT Freight"),
    ("fob", "Memphis"),
    ("fob", "Cairo"),
]


# ---------------------------------------------------------------------------
# Calculation
# ---------------------------------------------------------------------------

def fob_value(cif, freight_pct, factor, bushel_weight):
    """FOB barge basis for one location/month. Returns None if inputs missing."""
    if cif is None or freight_pct is None:
        return None
    return cif - (factor * freight_pct) / 2000 * bushel_weight


# ---------------------------------------------------------------------------
# Cash-vs-delivery / spreads / carry section (bottom of each commodity block)
# ---------------------------------------------------------------------------

CARRY_CONFIG = {
    "Corn": {
        "cash_loc": "Hennepin", "cash_mode": "flat",
        "cash_label": "Cash vs Delivery (Hennepin)",
        "spread_labels": ["CN/U", "CU/Z", "CZ/H"],
        "top_carry": [("STL Top Carry (Spot Futures)", "STL"),
                      ("Henn Top Carry (Spot Futures)", "Hennepin")],
    },
    "Soybeans": {
        "cash_loc": "Hennepin", "cash_mode": "flat",
        "cash_label": "Cash vs Delivery (Hennepin)",
        "spread_labels": ["SN/SQ", "SQ/SX", "SX/SF"],
        "top_carry": [("STL Top Carry (Spot Futures)", "STL"),
                      ("STL Henn Carry (Spot Futures)", "Hennepin")],
    },
    "Wheat": {
        "cash_loc": "STL", "cash_mode": "cumulative",
        "cash_label": "Cash vs Delivery (STL)",
        "spread_labels": ["WN/WU", "WU/WZ", "WZ/H"],
        "top_carry": [("STL Top Carry (Spot Futures)", "STL")],
    },
}


def contract_indices(commodity):
    """0-based index of each month's contract among the distinct contracts."""
    seen, idx = [], []
    for c in CONTRACTS[commodity]:
        if c not in seen:
            seen.append(c)
        idx.append(seen.index(c))
    return idx


def spread_offsets(commodity, spreads):
    """Cumulative spread offset per month (sum of spreads before its contract)."""
    cum = [0.0]
    for s in spreads:
        cum.append(cum[-1] + (s or 0.0))
    return [cum[i] for i in contract_indices(commodity)]


def pct_full_carry(spreads, fullcarry):
    """% of full carry per spread = spread / -fullcarry."""
    out = []
    for s, fc in zip(spreads, fullcarry):
        out.append(None if not fc else s / (-fc))
    return out


# Futures month codes -> calendar month number.
CONTRACT_MONTH = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
                  "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}


def distinct_contracts(commodity):
    """The 4 distinct futures contracts of a commodity, in calendar order."""
    seen = []
    for c in CONTRACTS[commodity]:
        if c not in seen:
            seen.append(c)
    return seen


def spread_months(commodity):
    """Months between each consecutive contract pair (handles year wrap)."""
    dc = distinct_contracts(commodity)
    out = []
    for a, b in zip(dc, dc[1:]):
        d = (CONTRACT_MONTH[b[-1]] - CONTRACT_MONTH[a[-1]]) % 12
        out.append(d or 12)
    return out


def futures_by_contract(commodity, fut_row):
    """First futures price seen for each distinct contract."""
    out = {}
    for j, m in enumerate(MONTHS):
        c = CONTRACTS[commodity][j]
        if c not in out and fut_row.get(m) is not None:
            out[c] = fut_row[m]
    return out


def compute_full_carry(commodity, fut_row, interest_annual, storage_per_mo):
    """Theoretical full carry per spread from interest + storage.

    full carry = months * (storage/bu/mo + front_price * annual_interest / 12)
    interest_annual is a decimal (e.g. 0.07).
    """
    dc = distinct_contracts(commodity)
    fbc = futures_by_contract(commodity, fut_row)
    out = []
    for i, mo in enumerate(spread_months(commodity)):
        price = fbc.get(dc[i])
        if price is None:
            out.append(None)
            continue
        out.append(mo * (storage_per_mo + price * interest_annual / 12.0))
    return out


def cash_vs_delivery(commodity, fob_row, cash_c, months=None):
    """FOB(cash location) less the DVE cash distance, by month.

    Corn/soy subtract a flat constant; wheat is cumulative (each month
    subtracts the prior month's result), matching the workbook exactly.
    """
    months = months or MONTHS
    mode = CARRY_CONFIG[commodity]["cash_mode"]
    vals, prev = [], None
    for m in months:
        f = fob_row.get(m)
        if f is None:
            vals.append(None)
            continue
        base = cash_c if (mode == "cumulative" and prev is None) else (
            prev if mode == "cumulative" else cash_c)
        v = f - base
        vals.append(v)
        prev = v
    return vals


def top_carry(commodity, fob_row, spreads):
    """FOB(location) shifted to the spot (front) contract via spread offsets."""
    off = spread_offsets(commodity, spreads)
    out = []
    for j, m in enumerate(MONTHS):
        f = fob_row.get(m)
        out.append(None if f is None else f - off[j])
    return out


def compute_fob_grid(commodity, cif_by_month, freight_by_region, months=None):
    """
    Build the full FOB grid for a commodity.

    cif_by_month: {month: value}
    freight_by_region: {region: {month: value}}  (shared across commodities)
    months: column keys to compute (defaults to the current MONTHS; pass a
            snapshot's own months when rendering archived dates).

    Returns: {location_name: {month: fob_value_or_None}}
    """
    months = months or MONTHS
    bu = BUSHEL_WEIGHT[commodity]
    grid = {}
    for loc in LOCATIONS:
        row = {}
        region_freight = freight_by_region.get(loc.region, {})
        for m in months:
            row[m] = fob_value(cif_by_month.get(m), region_freight.get(m), loc.factor, bu)
        grid[loc.name] = row
    return grid
