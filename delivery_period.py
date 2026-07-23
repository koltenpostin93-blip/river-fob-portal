"""
Delivery-period normalization.

Bids across providers label their delivery window inconsistently — "JUN",
"June 2026", "FH June", "By June 19th", "Bal June/FH July", "Fall 2026",
"New Crop", "Aug 1-15", etc. This module folds them to a canonical
(year, month) delivery period so the Summary tab can filter by the physical
delivery window rather than the CME futures contract.

Rules:
  • Parse the first month name in the text.
  • Year: an explicit 4-digit year in the text wins; otherwise derive it from
    the futures contract (delivery month <= futures month → futures year, else
    futures year - 1). 2-digit numbers are ignored (they're usually date ranges
    like "16-30", not years).
  • No month in the text (Fall / New Crop / Harvest / Old Crop / Cash …) → fall
    back to the futures contract's own month.

sub_rank() ranks split months within the same period so callers can pick the
"nearest" slot: first-half / balance / by-date → 0, plain → 1, last-half → 2.
"""
import re

_CME = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
        "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}
_M3 = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
       "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_ABBR = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
         7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}

_MONTH_RE = re.compile(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)")
_YEAR_RE  = re.compile(r"\b(20\d\d)\b")

# Two-letter slash codes (Oct/Nov etc.) → the first (nearer) month.
_SLASH = {"j/f": 1, "f/m": 2, "m/a": 3, "a/m": 4, "m/j": 5, "j/j": 6,
          "j/a": 7, "a/s": 8, "s/o": 9, "o/n": 10, "n/d": 11, "d/j": 12}


def _fut_ym(futures_symbol: str):
    """(year, month) of a CME symbol like 'ZSN26', or None."""
    fs = futures_symbol or ""
    if len(fs) < 5 or fs[2] not in _CME or not fs[3:5].isdigit():
        return None
    return (2000 + int(fs[3:5]), _CME[fs[2]])


def canonical(delivery_month: str, futures_symbol: str):
    """Return (year, month) for a bid's delivery window, or None if unknowable."""
    fy = _fut_ym(futures_symbol)
    text = (delivery_month or "").lower()
    m = _MONTH_RE.search(text)
    mon = _M3[m.group(1)] if m else next((v for k, v in _SLASH.items() if k in text), None)
    if mon:
        y = _YEAR_RE.search(delivery_month or "")
        if y:
            return (int(y.group(1)), mon)
        if fy:
            return (fy[0] if mon <= fy[1] else fy[0] - 1, mon)
        return None
    return fy  # no month in text → fall back to the futures contract month


def label(ym) -> str:
    """(year, month) → 'Jun 2026'."""
    return f"{_ABBR[ym[1]]} {ym[0]}" if ym else ""


def sub_rank(delivery_month: str) -> int:
    """Order split slots within a month: first-half→0, plain→1, last-half→2."""
    t = (delivery_month or "").lower()
    if re.search(r"\bf\.?h\b|first half|^bal\b|\bbal |\bby \b|1-1[05]\b|1-10\b", t):
        return 0
    if re.search(r"\bl\.?h\b|last half|16-\d", t):
        return 2
    return 1


def slot_key(delivery_month: str):
    """Within a delivery month: nearest slot first, then prefer an explicit month
    label (e.g. 'Nov') over a generic one (New Crop / Fall / N/C)."""
    explicit = 0 if _MONTH_RE.search((delivery_month or "").lower()) else 1
    return (sub_rank(delivery_month), explicit)


def deliv_key(delivery_month: str, futures_symbol: str):
    """Sortable nearness key: (year, month, slot…). Far future if unknowable."""
    ym = canonical(delivery_month, futures_symbol)
    if ym is None:
        return (9999, 99, 9, 9)
    return (ym[0], ym[1]) + slot_key(delivery_month)
