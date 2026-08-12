"""
One-off: authenticate to Fastmarkets and list the grain FOB instruments this
service is entitled to, so we can pick the symbols for the FOB Vessel tab
(corn / soybeans / wheat FOB at US Gulf, PNW, Brazil, Argentina, Ukraine).

Run:  python fm_discover.py
Needs FOB_VESSEL_SERVICE_NAME + FOB_VESSEL_API_KEY in .env.
"""
import os
import re
import warnings

warnings.filterwarnings("ignore")
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
                override=True)
except ImportError:
    pass

import fastmarkets as FM

GRAINS = re.compile(r"\b(corn|maize|soybean|soya|soy|wheat)\b", re.I)
ORIGINS = re.compile(r"\b(gulf|pnw|pacific northwest|brazil|argentin|ukrain|"
                     r"paranagua|santos|rosario|us\b|u\.s\.)\b", re.I)


def _txt(v):
    return "" if v is None else str(v)


def main():
    if not FM.configured():
        print("Missing creds — set FOB_VESSEL_SERVICE_NAME and FOB_VESSEL_API_KEY "
              "in .env.")
        return
    print("Authenticating…")
    FM.get_token()
    print("OK. Pulling entitled instruments…")
    data = FM.instruments()
    instr = data.get("instruments", data if isinstance(data, list) else [])
    print(f"Total entitled instruments: {len(instr)}\n")

    def field(it, *keys):
        for k in keys:
            if it.get(k):
                return _txt(it[k])
        return ""

    hits = []
    for it in instr:
        desc = field(it, "description", "descriptionShort")
        commodity = field(it, "commodity", "commodityId")
        incoterm = field(it, "incoterm", "incotermId")
        location = field(it, "location", "locationId")
        blob = " ".join([desc, commodity, location])
        if GRAINS.search(blob) and ("fob" in blob.lower() or "fob" in incoterm.lower()):
            hits.append((commodity, location, incoterm, it.get("symbol", ""), desc))

    print(f"Grain FOB instruments: {len(hits)}")
    for c, loc, inc, sym, desc in sorted(hits):
        star = " *" if ORIGINS.search(f"{loc} {desc}") else ""
        print(f"  [{sym:16}] {c:10} {inc:6} {loc:12} | {desc}{star}")

    if not hits:
        print("\nNo FOB grain instruments matched — printing a sample of what IS "
              "entitled so we can adjust the filter:")
        for it in instr[:40]:
            print(f"  [{it.get('symbol',''):16}] "
                  f"{field(it,'commodity','commodityId'):10} | "
                  f"{field(it,'description','descriptionShort')}")


if __name__ == "__main__":
    main()
