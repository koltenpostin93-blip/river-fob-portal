"""
Parse the daily Barge Freight and CIF NOLA tables (copy-pasted from the source)
into the portal's input structures.

Both tables are month-down-the-side, with a couple of spot rows (TW/NW) that we
skip. CIF values are in cents (82.0 -> 0.82) with a contract-month letter beside
each; freight values are percentages (550% -> 5.50). Tab-separated (Excel copy),
with a 2+-space fallback.
"""
import re
import datetime as dt

# Source month-row label -> sheet month key. TW/NW (spot week) are skipped.
_MONTHS = {"JUNE": "June", "JULY": "July", "AUG": "Aug", "SEPT": "Sep",
           "SEP": "Sep", "OCT": "Oct", "NOV": "Nov", "DEC": "Dec", "JAN": "Jan"}

_COMMODITIES = {"CORN": "Corn", "BEANS": "Soybeans", "SOYBEANS": "Soybeans",
                "WHEAT": "Wheat"}  # MILO intentionally absent

# Freight source column -> sheet freight region(s). L OHIO duplicates OHIO.
_FREIGHT_MAP = {
    "ILL": ["IL"], "OHIO": ["Ohio"],
    "MM": ["Davenport South", "McGregor South"],
    "CITIES": ["Upper Miss"], "STL": ["STL"], "MTCT": ["Lower Miss"],
}
_FREIGHT_HDRS = set(_FREIGHT_MAP)


def _split(line):
    if "\t" in line:
        return [c.strip() for c in line.split("\t")]
    return [c.strip() for c in re.split(r" {2,}", line)]


def _rows(text):
    return [_split(ln) for ln in text.splitlines() if ln.strip()]


def parse_cif(text):
    """-> ({commodity: {month: basis}}, {commodity: {month: contract_letter}}) or (None, error)."""
    rows = _rows(text)
    col = {}
    for r in rows:
        for i, c in enumerate(r):
            key = _COMMODITIES.get(c.upper())
            if key and key not in col:
                col[key] = i
        if col:
            break
    if not col:
        return None, "Couldn't find a CORN / BEANS / WHEAT header row."

    cif = {c: {} for c in col}
    contracts = {c: {} for c in col}
    for r in rows:
        mon = _MONTHS.get(r[0].upper()) if r else None
        if not mon:
            continue
        for commodity, ci in col.items():
            if ci >= len(r) or r[ci] == "":
                continue
            try:
                cif[commodity][mon] = float(r[ci].replace("%", "")) / 100.0
            except ValueError:
                continue
            if ci + 1 < len(r) and r[ci + 1]:
                contracts[commodity][mon] = r[ci + 1].upper()
    return {"cif": cif, "contracts": contracts}, None


def parse_freight(text):
    """-> ({region: {month: tariff_mult}}, detected_date) or (None, error)."""
    rows = _rows(text)
    col = {}
    for r in rows:
        for i, c in enumerate(r):
            key = c.upper().rstrip(".")
            if key in _FREIGHT_HDRS and key not in col:
                col[key] = i
        if "MTCT" in col:
            break
    if not col:
        return None, "Couldn't find a freight header (ILL/OHIO/MM/CITIES/STL/MTCT)."

    freight = {}
    for r in rows:
        mon = _MONTHS.get(r[0].upper()) if r else None
        if not mon:
            continue
        for src, ci in col.items():
            if ci >= len(r) or r[ci] == "":
                continue
            try:
                val = float(r[ci].replace("%", "")) / 100.0
            except ValueError:
                continue
            for region in _FREIGHT_MAP[src]:
                freight.setdefault(region, {})[mon] = val
    return {"freight": freight, "date": _find_date(text)}, None


_COMM_LETTER = {"C": "Corn", "S": "Soybeans", "W": "Wheat"}
_SYM_RE = re.compile(r"Z([CSW])([FGHJKMNQUVXZ])(\d{2})")


def parse_futures(text):
    """-> ({commodity: {contract_letter: $/bu price}}) or (None, error).

    Symbols like ZCN26 (corn July '26); Last is in cents, converted to $/bu.
    """
    out = {}
    for r in _rows(text):
        if not r:
            continue
        m = _SYM_RE.match(r[0].upper())
        if not m:
            continue
        price = None
        for c in r[1:]:
            try:
                price = float(c.replace(",", ""))
                break
            except ValueError:
                continue
        if price is None:
            continue
        out.setdefault(_COMM_LETTER[m.group(1)], {})[m.group(2)] = price / 100.0
    if not out:
        return None, "Couldn't find futures symbols like ZCN26."
    return {"futures": out}, None


def _find_date(text):
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", text)
    if not m:
        return None
    mo, day, yr = (int(x) for x in m.groups())
    yr = 2000 + yr if yr < 100 else yr
    try:
        return dt.date(yr, mo, day)
    except ValueError:
        return None
