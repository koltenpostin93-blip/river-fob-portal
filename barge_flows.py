"""Downbound grain barge flows through the river locks, from USDA AgTransport.

One weekly Socrata table (n4pw-9ygw) carries tons of grain moving DOWNBOUND past
seven locks on four rivers, by commodity, from 2003 to the present. Downbound is
the meaningful direction for grain — movement toward the Gulf for export — so the
dataset has no up/southbound split.

Fields: date (week-ending), commodity, lock, tons. We add a `river` segment from
the lock's prefix (MS/IL/OH/AK).
"""
from __future__ import annotations

import pandas as pd
import requests

SODA_URL = "https://agtransport.usda.gov/resource/n4pw-9ygw.json"
STORY_URL = "https://agtransport.usda.gov/stories/s/Barge-Dashboard/965a-yzgy/"

# Lock -> river segment. Prefixes: MS Mississippi, IL Illinois, OH Ohio, AK Arkansas.
RIVER_OF_LOCK = {
    "MS Lock 15": "Mississippi",
    "MS Lock 25": "Mississippi",
    "MS Lock 26": "Mississippi",
    "MS Locks 27": "Mississippi",
    "IL La Grange": "Illinois",
    "OH Olmsted": "Ohio",
    "AK Lock 1": "Arkansas",
}
SEGMENTS = ["Mississippi", "Illinois", "Ohio", "Arkansas"]
COMMODITIES = ["Corn", "Soybeans", "Wheat", "Other Grain"]

# A short gloss on what each lock captures, for tooltips/help.
LOCK_NOTE = {
    "MS Locks 27": "Mississippi at Granite City IL — Upper Miss + Missouri combined",
    "MS Lock 26": "Mississippi at Alton IL (Mel Price)",
    "MS Lock 25": "Mississippi near Winfield MO",
    "MS Lock 15": "Mississippi at the Quad Cities",
    "IL La Grange": "Illinois River at Versailles IL",
    "OH Olmsted": "Ohio River near its mouth",
    "AK Lock 1": "Arkansas River (McClellan-Kerr) near the Mississippi",
}


def load_flows(timeout: int = 60) -> pd.DataFrame:
    """Full tidy history: one row per week x commodity x lock.

    Columns: date, year, month, commodity, lock, river, tons. Empty frame on a
    transport error is left to the caller to handle."""
    r = requests.get(SODA_URL, params={"$limit": 60000, "$order": "date"},
                     timeout=timeout)
    r.raise_for_status()
    df = pd.DataFrame(r.json())
    if df.empty:
        return df
    df["tons"] = pd.to_numeric(df["tons"], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    df["year"] = df["date"].dt.year
    df["month"] = df["date"].dt.month
    df["river"] = df["lock"].map(RIVER_OF_LOCK)
    return df[["date", "year", "month", "commodity", "lock", "river", "tons"]]
