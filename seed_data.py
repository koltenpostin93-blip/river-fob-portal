"""
Default seed values taken from the 6.23 tab of the JSA FOB Sheet (June 2026).
Used to pre-populate the portal so it mirrors the live workbook. Order of each
list matches fob_model.MONTHS = [June, July, Aug, Sep, Oct, Nov, Dec, Jan].

Freight % is shared across all commodities (entered in the corn section of the
sheet). None = month not quoted (shows blank).
"""

SEED_FREIGHT = {
    "Lower Miss":      [3.75, 3.75, 5.25, 7.40, 7.25, 5.00, 4.60, 4.25],
    "Davenport South": [6.00, 6.00, 6.65, 7.50, 8.00, 7.00, None, None],
    "McGregor South":  [6.00, 6.00, 6.65, 7.50, 8.00, 7.00, None, None],
    "Upper Miss":      [6.75, 6.75, 7.00, 8.00, 8.50, None, None, None],
    "Ohio":            [3.60, 3.60, 5.50, 7.40, 7.90, 6.50, 5.75, 5.00],
    "STL":             [3.75, 3.90, 5.50, 7.25, 7.50, 5.60, 5.00, 4.75],
    "IL":              [5.60, 5.60, 6.25, 7.25, 7.75, 6.75, 6.15, 5.75],
}

SEED_CIF = {
    "Corn":     [0.82, 0.87, 0.92, 1.02, 0.92, 0.92, 0.92, 0.82],
    "Soybeans": [0.80, 0.88, 0.95, 0.80, 0.95, 1.00, 0.80, 0.87],
    "Wheat":    [0.55, 0.60, 0.60, 0.85, 0.95, 0.95, 0.95, 0.90],
}

SEED_FUTURES = {  # CBOT flat price $/bu
    "Corn":     [4.0975, 4.0975, 4.1775, 4.1775, 4.3725, 4.3725, 4.3725, 4.5175],
    "Soybeans": [11.17, 11.17, 11.24, 11.24, 11.4175, 11.4175, 11.56, 11.56],
    "Wheat":    [5.8675, 5.8675, 5.8675, 5.97, 5.97, 6.1375, 6.1375, 6.1375],
}

# Three inter-contract spreads (front − next) per commodity.
SEED_SPREADS = {
    "Corn":     [-0.0800, -0.1950, -0.1450],
    "Soybeans": [-0.0700, -0.0325, -0.1425],
    "Wheat":    [-0.1025, -0.1675, -0.1475],
}

# Full-carry reference per spread (the theoretical max carry; from quote feed).
SEED_FULLCARRY = {
    "Corn":     [0.2157, 0.3173, 0.3040],
    "Soybeans": [0.1468, 0.4299, 0.2908],
    "Wheat":    [0.1701, 0.2520, 0.3293],
}

# Cash distance from delivery (DVE) constant per commodity.
SEED_CASH_C = {"Corn": 0.12, "Soybeans": 0.18, "Wheat": 0.16}

# Full-carry assumptions (drive the % Full Carry denominator).
SEED_INTEREST_PCT = 7.0     # annual %, e.g. 7.0 = 7% (market-wide)
# Storage is per-commodity ($/bu/month); wheat is lower to reflect its VSR level.
SEED_STORAGE_MO = {"Corn": 0.080, "Soybeans": 0.080, "Wheat": 0.050}
