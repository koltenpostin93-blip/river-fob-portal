"""
Daily River FOB import — reads the current JSA FOB Sheet workbook and upserts its
most recent dated tabs into the shared archive (Supabase), so the basis tracker's
River FOB tab stays current without anyone opening the portal.

Reuses import_history.parse_tab (adaptive: auto-detects label column, reads each
tab's own month/contract headers so month-turn rolls are captured) and
import_history._tab_date. Picks the newest workbook matching the current month
(fallback: previous month), copies it first (Excel read-locks the original), and
imports the last RECENT_TABS trading days — recent-only so it never clobbers older
archived history.

Run manually:  python daily_fob_import.py            (uses the auto-found workbook)
               python daily_fob_import.py "<path.xlsx>"   (explicit workbook)
Scheduled via Windows Task Scheduler (see setup notes in the basis-tracker memory).
"""
import os
import sys
import glob
import shutil
import tempfile
import logging
import datetime as dt
import warnings

warnings.filterwarnings("ignore")
try:
    from dotenv import load_dotenv
    # override=True so the project .env is authoritative even if a stray machine
    # env var DATABASE_URL points somewhere else (this caused pulls to be written
    # to an old, wrong Supabase project the live app doesn't read).
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=True)
except ImportError:
    pass  # DATABASE_URL may come from the environment instead

import openpyxl
import db
import import_history as IH

RECENT_TABS = 5                                    # import the last N trading days
LOG_PATH = os.path.join(os.path.dirname(__file__), "daily_fob_import.log")

# Basis-band sanity: CIF NOLA is stored as a basis ($/bu over CBOT), so a plausible
# value sits in a tight band. Anything outside almost certainly means a flat price,
# a cents/dollars slip, or a garbage cell (e.g. a stray 389.95) — not a real day.
# Bands are set well outside the 20-yr observed min/max per commodity (see the
# archive: Corn 0.02–2.15, Soybeans -0.34–3.25, Wheat -1.55–1.15 legit).
CIF_BAND = {"Corn": (-1.0, 3.0), "Soybeans": (-1.5, 4.0), "Wheat": (-2.5, 4.0)}
_DEFAULT_BAND = (-2.5, 5.0)


def _basis_flags(cif):
    """Return [(commodity, month, value), ...] for CIF values outside the plausible
    basis band — i.e. suspect entries that shouldn't silently enter the archive."""
    out = []
    for c, mv in cif.items():
        lo, hi = CIF_BAND.get(c, _DEFAULT_BAND)
        for m, v in mv.items():
            if v is not None and not (lo <= v <= hi):
                out.append((c, m, v))
    return out

# import_history abbreviates months (Jun/Jul); the live archive + the basis
# tracker's CIF-prefill lookup use the sheet's convention (June/July full, others
# 3-letter). Remap so a daily import stays key-consistent with existing dates.
_MFIX = {"Jun": "June", "Jul": "July"}


def _fix_months(mv):
    return {_MFIX.get(m, m): v for m, v in mv.items()}


def _fix_calendar(cols):
    return [(_MFIX.get(m, m), c) for m, c in cols]

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"),
              logging.StreamHandler(sys.stdout)])
log = logging.getLogger("daily_fob")


def find_active_workbook(today: dt.date):
    """Newest 'JSA FOB Sheet … <Month> <Year>.xlsx' for the current month in the
    year folder; falls back to the previous month at a month boundary."""
    folder = os.path.join(IH.FOB_ROOT, str(today.year))

    def candidates(month_name: str, yr: int):
        out = []
        for f in glob.glob(os.path.join(folder, "*.xlsx")):
            b = os.path.basename(f)
            if b.startswith("~$"):
                continue                            # Excel lock/temp file
            m = IH.MONTHS_RE.search(b)
            if m and m.group(1).lower() == month_name.lower() and int(m.group(2)) == yr:
                out.append(f)
        return sorted(out, key=os.path.getmtime, reverse=True)

    c = candidates(today.strftime("%B"), today.year)
    if not c:
        prev = today.replace(day=1) - dt.timedelta(days=1)
        c = candidates(prev.strftime("%B"), prev.year)
    return c[0] if c else None


def main():
    if not db._is_postgres():
        log.error("DATABASE_URL not set — refusing to write to the SQLite fallback.")
        sys.exit(1)

    today = dt.date.today()
    db.init_db()

    # Stop the 15-min polling loop once today's sheet is in. The task fires every
    # 15 min (3:15–5:30) only because we don't know WHEN JSA will save the day's
    # FOB tab. As soon as today's as-of date is archived, an earlier run already
    # captured the uploaded sheet — so skip the workbook copy + reparse entirely
    # and let every remaining run be an instant no-op. Pass --force to override
    # (e.g. to re-pull a corrected sheet). Skipped when an explicit workbook path
    # is given, since that's an intentional manual re-import.
    force = "--force" in sys.argv[1:]
    explicit = [a for a in sys.argv[1:] if a != "--force"]
    if not explicit and not force:
        have = {str(x)[:10] for x in db.list_dates()}
        if today.isoformat() in have:
            log.info("Today's FOB sheet (%s) already imported — nothing to do.", today)
            return

    wb_path = explicit[0] if explicit else find_active_workbook(today)
    if not wb_path or not os.path.exists(wb_path):
        log.error("No active FOB workbook found for %s.", today)
        sys.exit(1)

    b = os.path.basename(wb_path)
    m = IH.MONTHS_RE.search(b)
    if not m:
        log.error("Workbook name not recognized: %s", b)
        sys.exit(1)
    wb_month = IH.MONTH_NAME.index(m.group(1).capitalize()) + 1
    wb_year = int(m.group(2))
    log.info("Active workbook: %s", b)

    # Copy first — the original is read-locked while open in Excel.
    tmp = os.path.join(tempfile.gettempdir(), "fob_daily_" + b.replace(" ", "_"))
    shutil.copy2(wb_path, tmp)
    try:
        wb = openpyxl.load_workbook(tmp, read_only=True, data_only=True)

        dated = []
        for tab in wb.sheetnames:
            if not IH.DATED_TAB.fullmatch(tab.strip()):
                continue
            d = IH._tab_date(tab, wb_year, wb_month)
            if d:
                dated.append((d, tab))
        dated.sort()
        recent = dated[-RECENT_TABS:]
        if not recent:
            log.warning("No dated tabs found in %s.", b)
            sys.exit(1)

        # History-write lock: this pass re-reads the last RECENT_TABS tabs to
        # backfill any missed days, but it must NOT silently overwrite a date the
        # archive already holds (that's how a good 8/5 got clobbered by a later,
        # worse 8/5 tab). New dates write freely; existing dates are left alone
        # unless --force is passed to intentionally re-pull a corrected sheet.
        already = {str(x)[:10] for x in db.list_dates()}

        saved = 0
        for d, tab in recent:
            iso = d.isoformat()
            if iso in already and not force:
                log.info("  %-6s -> %s  already archived — left as-is "
                         "(use --force to overwrite).", tab, d)
                continue
            res = IH.parse_tab(wb[tab])
            if not res:
                log.warning("  tab %s (%s): parse failed — skipped.", tab, d)
                continue
            cif, freight, calendar, futures = res
            cif = {c: _fix_months(mv) for c, mv in cif.items()}
            freight = {r: _fix_months(mv) for r, mv in freight.items()}
            calendar = {c: _fix_calendar(cols) for c, cols in calendar.items()}
            futures = {c: _fix_months(mv) for c, mv in futures.items()}

            # Basis-band sanity: block a snapshot whose CIF has implausible values
            # rather than let garbage (flat prices, decimal slips) into the archive.
            bad = _basis_flags(cif)
            if bad and not force:
                log.warning("  %-6s -> %s  SKIPPED: CIF outside plausible basis "
                            "band: %s  (use --force to import anyway).", tab, d,
                            ", ".join(f"{c} {m}={v}" for c, m, v in bad))
                continue

            spreads = IH.spreads_from(futures, calendar)   # from CBOT if present
            n_cif, n_frt = db.save_snapshot(
                iso, cif, freight, calendar,
                futures=futures, spreads=spreads)
            months = [mm for mm, _ in calendar.get("Corn", [])]
            log.info("  %-6s -> %s  months=%s  (%d CIF, %d freight, %s CBOT)",
                     tab, d, months, n_cif, n_frt,
                     "with" if futures else "no")
            saved += 1
        log.info("Imported %d/%d recent tabs. Archive latest: %s",
                 saved, len(recent), db.list_dates()[0])
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


if __name__ == "__main__":
    main()
