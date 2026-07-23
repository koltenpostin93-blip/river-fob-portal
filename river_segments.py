"""
River-terminal segmentation by waterway area.

Segments:
  Upper Miss  — Mississippi River north of the Southern MN / Northern IA line (~43.5°N)
  Mid Miss    — Mississippi River from just north of St. Louis up to that line
  STL         — within ~20 miles of St. Louis
  Lower Miss  — Mississippi River south of St. Louis
  Ohio        — Ohio River
  Illinois    — Illinois River (to be further split into Zones 1-5)
  Other       — Arkansas River / Pacific NW / non-river or mislabeled terminals

Assignment is a curated lookup by location name (geocodes in the data are
unreliable). Unknown locations default to "Other" so new terminals surface for
review rather than being silently mis-grouped.
"""

SEGMENT_ORDER = [
    "Illinois Z1", "Illinois Z2", "Illinois Z3", "Illinois Z4", "Illinois Z5",
    "Upper Miss", "Mid Miss", "STL", "Lower Miss", "Ohio",
    "Other",
]

_SEGMENT: dict[str, str] = {
    # ── Ohio River ────────────────────────────────────────────────────────────
    "CGB MOUND CITY": "Ohio", "Mound City, IL": "Ohio",
    "Evansville, IN (1st Ave.)": "Ohio", "Evansville, IN (Broadway)": "Ohio",
    "Evansville, IN (Ohio St.)": "Ohio", "Evansville, IN (River South)": "Ohio",
    "Mt. Vernon, IN (Elevator)": "Ohio", "Newburgh, IN": "Ohio", "Rockport, IN": "Ohio",
    "CGB OWENSBORO": "Ohio", "Henderson, KY": "Ohio", "Livingston Point, KY": "Ohio",
    "Paducah, KY": "Ohio", "Silver Grove, KY": "Ohio",

    # ── Upper Mississippi (north of ~43.5°N) ──────────────────────────────────
    "CGB SAVAGE": "Upper Miss", "Savage": "Upper Miss",
    "St. Paul, MN (Elevator D)": "Upper Miss",
    "Winona": "Upper Miss", "Winona, MN": "Upper Miss",

    # ── Mid Mississippi (STL → MN/IA line) ────────────────────────────────────
    "Bettendorf": "Mid Miss", "Burlington, IA": "Mid Miss",
    "CGB - Meekers Landing": "Mid Miss", "CGB CLAYTON": "Mid Miss",
    "Clinton, IA (Elevator)": "Mid Miss", "Davenport": "Mid Miss", "Muscatine": "Mid Miss",
    "CGB - Dallas City": "Mid Miss", "CGB - East Hannibal": "Mid Miss",
    "CGB - Nauvoo": "Mid Miss", "CGB ALBANY": "Mid Miss", "Gulfport, IL": "Mid Miss",
    "Keithsburg": "Mid Miss", "New Boston": "Mid Miss", "Quincy Elevator": "Mid Miss",
    "Quincy, IL (Barge Dock)": "Mid Miss", "Buffalo Island": "Mid Miss",
    "CGB - Lagrange": "Mid Miss",

    # ── St. Louis (within ~20 mi) ─────────────────────────────────────────────
    "CGB - Cahokia": "STL", "CHS Cahokia": "STL", "East St. Louis": "STL",
    "Sauget, IL": "STL", "St. Louis, MO (Elevator)": "STL", "Fairmont City, IL": "STL",

    # ── Lower Mississippi (south of STL) ──────────────────────────────────────
    "CGB - Desoto Landing": "Lower Miss", "CGB - Helena North (River)": "Lower Miss",
    "CGB - Old Town": "Lower Miss", "CGB OSCEOLA": "Lower Miss",
    "Helena, AR": "Lower Miss", "West Memphis": "Lower Miss",
    "CGB GRAND TOWER": "Lower Miss", "CGB HICKMAN": "Lower Miss", "Hickman (Ashland)": "Lower Miss",
    "CGB - Jonesville": "Lower Miss", "CGB - Louisiana": "Lower Miss",
    "CGB - Tallulah Port": "Lower Miss", "CGB - Vidalia": "Lower Miss",
    "Port Allen": "Lower Miss", "Vidalia, LA": "Lower Miss", "Boyle Terminal": "Lower Miss",
    "CGB BIRDS POINT": "Lower Miss", "CGB LINDA": "Lower Miss", "CGB SCOTT CITY": "Lower Miss",
    "New Madrid": "Lower Miss", "New Madrid, MO": "Lower Miss",
    "CGB - Friars Point": "Lower Miss", "CGB - Greenville": "Lower Miss",
    "CGB - Mayersville": "Lower Miss", "CGB - Rosedale": "Lower Miss",
    "CGB - Vicksburg": "Lower Miss", "CGB - Yazoo City": "Lower Miss", "Rosedale": "Lower Miss",
    "CGB DYERSBURG": "Lower Miss", "Hales Point": "Lower Miss",
    "Heloise, TN": "Lower Miss", "Memphis, TN": "Lower Miss",

    # ── Illinois River (Zone 1 = Chicago, downriver to Zone 5) ────────────────
    # Zone 2 — Morris / Seneca
    "Morris, IL": "Illinois Z2", "Seneca": "Illinois Z2",
    "CGB - River Landing": "Illinois Z2",
    # Zone 3 — Ottawa / Peru / Spring Valley / Hennepin / Lacon
    "Ottawa, IL (North Side)": "Illinois Z3", "Ottawa, IL (South)": "Illinois Z3",
    "Peru Marketstreet": "Illinois Z3", "Spring Valley South": "Illinois Z3",
    "Spring Valley, IL": "Illinois Z3", "CGB HENNEPIN": "Illinois Z3",
    "Hennepin, IL": "Illinois Z3", "Lacon": "Illinois Z3", "Lacon, IL": "Illinois Z3",
    # Zone 4 — Creve Coeur / Peoria
    "Creve Coeur, IL": "Illinois Z4",
    # Zone 5 — Havana / Beardstown / Meredosia
    "Havana": "Illinois Z5", "Havana, IL": "Illinois Z5", "Havana/Beardstown": "Illinois Z5",
    "Beardstown": "Illinois Z5", "Meredosia": "Illinois Z5",
    # (CGB JOLIET reclassified as a container terminal — see cgb_scraper overrides)

    # ── Other (Arkansas River / Pacific NW / non-river or mislabeled) ──────────
    "CGB - Port 33": "Other", "CGB - Wagoner": "Other", "CGB - Webbers Falls": "Other",
    "CGB - Pine Bluff": "Other", "CGB - Van Buren": "Other",   # Arkansas River
    "Kennewick/LoMo": "Other",                                 # Columbia/Snake (PNW)
    "Longview, WA": "Other",                                    # Columbia River export (PNW)
    "Macon": "Other",                                          # not on a river (mislabeled)
}


def river_segment(location: str) -> str:
    """Return the river segment for a terminal location name (default 'Other')."""
    return _SEGMENT.get(location, "Other")
