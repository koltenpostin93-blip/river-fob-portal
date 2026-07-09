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
# Full set so the rolling window works year-round (June/July spelled out to
# match the sheet's column labels; the rest 3-letter).
_MONTHS = {"JAN": "Jan", "JANUARY": "Jan", "FEB": "Feb", "FEBRUARY": "Feb",
           "MAR": "Mar", "MARCH": "Mar", "APR": "Apr", "APRIL": "Apr",
           "MAY": "May", "JUNE": "June", "JUN": "June", "JULY": "July",
           "JUL": "July", "AUG": "Aug", "AUGUST": "Aug", "SEP": "Sep",
           "SEPT": "Sep", "SEPTEMBER": "Sep", "OCT": "Oct", "OCTOBER": "Oct",
           "NOV": "Nov", "NOVEMBER": "Nov", "DEC": "Dec", "DECEMBER": "Dec"}

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
_FRAC_RE = re.compile(r"^(\d+)[\-'’](\d)$")   # grain fractional: 427'6 = 427 6/8


def _fut_price(tok):
    """A futures price token -> float, or None. Accepts decimals and grain
    fractional notation (427'6 or 427-6 = 427 and 6/8)."""
    tok = str(tok).strip().replace(",", "").replace("$", "")
    if not tok:
        return None
    m = _FRAC_RE.match(tok)
    if m:
        return int(m.group(1)) + int(m.group(2)) / 8.0
    try:
        return float(tok)
    except ValueError:
        return None


def parse_futures(text):
    """-> ({commodity: {contract_letter: $/bu price}}) or (None, error).

    Handles symbols like ZCN26 (corn July '26) whether the price is in the next
    column, glued to the symbol, or on the same line separated by a single
    space; decimals or grain fractionals (427'6). Cent quotes (>100) convert to
    $/bu; dollar quotes are kept as-is.
    """
    out = {}
    for r in _rows(text):
        sym, tail = None, []
        for i, cell in enumerate(r):
            m = _SYM_RE.match(str(cell).strip().upper())
            if m:
                sym = m
                after = str(cell).strip()[m.end():]     # price glued to symbol?
                tail = ([after] if after else []) + list(r[i + 1:])
                break
        if not sym:
            continue
        price = None
        for chunk in tail:
            for piece in re.split(r"\s+", str(chunk).strip()):
                price = _fut_price(piece)
                if price is not None:
                    break
            if price is not None:
                break
        if price is None:
            continue
        if price > 100:                                  # cents -> $/bu
            price /= 100.0
        out.setdefault(_COMM_LETTER[sym.group(1)], {})[sym.group(2)] = round(price, 4)
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
