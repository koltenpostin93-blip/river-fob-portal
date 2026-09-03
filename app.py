"""
River FOB Values — portal interface.

Renders each commodity as a faithful replica of the JSA FOB Sheet block:
date banner, green commodity banner, month/contract header rows, CBOT and CIF
rows, then each river reach with its freight row (shown as % of tariff) and the
FOB barge rows beneath it (2 decimals, negatives in red parentheses).

Inputs (shared barge freight; per-commodity CIF and CBOT futures) are editable
in the "Edit today's inputs" expander; the sheet recalculates live.
History archiving to Postgres is a separate milestone.
"""
import base64
import datetime as dt
import json
import os
import io
import re
from collections import Counter

import altair as alt
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

import fob_model as M
import seed_data as S
import db
import paste_parse
import fob_pdf
import fob_excel
import bids_data
import river_segments as RS
import delivery_period as DP
import fob_vessel
import massive_futures
import barge_flows as BF

# Local convenience: load a .env if python-dotenv is installed. It's optional —
# on Streamlit Cloud there is no .env and secrets come from st.secrets (below),
# so a missing package must never crash the app.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ModuleNotFoundError:
    pass

# On Streamlit Community Cloud, secrets live in st.secrets rather than .env.
# Inject any secrets not already set by load_dotenv() into os.environ so db.py
# reads the shared Supabase via os.getenv() — same pattern as the basis tracker.
try:
    for _secret_key in ("DATABASE_URL", "BASIS_DATABASE_URL",
                        "FOB_VESSEL_SERVICE_NAME", "FOB_VESSEL_API_KEY",
                        "MASSIVE_API_KEY"):
        if _secret_key in st.secrets and not os.environ.get(_secret_key):
            os.environ[_secret_key] = st.secrets[_secret_key]
except Exception:
    pass  # st.secrets not available (no secrets configured) — fine locally

st.set_page_config(
    page_title="River FOB Values · JPSI",
    page_icon="https://www.jpsi.com/wp-content/uploads/2019/04/cropped-Favicon-1-192x192.png",
    layout="wide",
    initial_sidebar_state="expanded"
)


@st.cache_resource
def _ensure_db():
    # Allow DATABASE_URL via Streamlit secrets (falls back to local SQLite).
    try:
        if "DATABASE_URL" in st.secrets:
            os.environ["DATABASE_URL"] = st.secrets["DATABASE_URL"]
    except Exception:
        pass
    db.init_db()
    return db.backend_name()


DB_BACKEND = _ensure_db()


def _safe(v):
    """Float or None (drops NaN) — used when persisting inputs."""
    try:
        return None if v is None or pd.isna(v) else float(v)
    except (TypeError, ValueError):
        return None


@st.cache_data
def _asset_uri(filename):
    p = os.path.join(os.path.dirname(__file__), "assets", filename)
    try:
        with open(p, "rb") as f:
            return "data:image/png;base64," + base64.b64encode(f.read()).decode()
    except OSError:
        return ""


@st.cache_data(show_spinner=False, ttl=3600)
def _live_fed_funds(as_of_iso):
    """Live front-month fed funds rate and the full-carry interest default it
    implies (fed funds + 2.25%, the cost-of-carry convention). Returns a dict
    {ff, rate} or None if the live rate can't be read. Cached hourly — it moves
    in basis points. Defined early: the Snapshot header uses it at module scope."""
    try:
        ff = massive_futures.fed_funds_rate(dt.date.fromisoformat(as_of_iso))
    except Exception:
        ff = None
    if ff is None:
        return None
    return {"ff": ff, "rate": round(ff + massive_futures.FED_FUNDS_SPREAD_PCT, 2)}


WATERMARK = _asset_uri("jsa_50yr.png")
LOGO_URI = _asset_uri("logo-full.png")           # JSA wordmark (dark)

# --- JPSI brand + smoothed sheet styling ----------------------------------
JPSI_DARK = "#32373c"
JPSI_BLUE = "#0693e3"
NEG_RED = "#d64545"       # softer than pure red

# per-commodity banner gradient (start, end)
COMMODITY_THEME = {
    "Corn":     ("#f4b41a", "#e09600"),   # golden
    "Soybeans": ("#5da34d", "#3e7d33"),   # green
    "Wheat":    ("#cda94a", "#a9772b"),   # wheat tan
}

st.markdown(
    f"""
    <style>
      /* JPSI site typography: Source Sans Pro body + EB Garamond serif headings */
      @import url('https://fonts.googleapis.com/css2?family=Source+Sans+Pro:wght@300;400;600;700&family=EB+Garamond:wght@400;500;600&display=swap');
      html, body, [class*="css"], .stApp, button, input, select, textarea, table, td, th, .stMarkdown {{
        font-family: 'Source Sans Pro', system-ui, -apple-system, sans-serif !important;
      }}
      table td, table th {{ font-variant-numeric: tabular-nums; }}
      .jpsi-serif {{ font-family: 'EB Garamond', Georgia, 'Times New Roman', serif !important; }}

      /* Hide Streamlit's fixed header and menu */
      header[data-testid="stHeader"] {{ display: none !important; }}
      #MainMenu {{ visibility: hidden !important; }}
      footer {{ visibility: hidden !important; }}

      /* Main layout */
      .block-container {{ padding-top: 0.75rem !important; padding-bottom: 1rem !important; max-width: 1200px; }}
      .stApp {{ background-color: #ffffff; }}

      /* Header — JSA logo left, centred title, blue underline (jpsi.com style) */
      .dash-header {{
        background: #ffffff;
        border-bottom: 3px solid {JPSI_BLUE};
        padding: 18px 8px 14px 8px;
        margin: -0.75rem 0 22px 0;
        display: flex;
        align-items: center;
        gap: 20px;
      }}
      .dash-header-logo {{ flex-shrink: 0; }}
      .dash-header-logo img {{ height: 54px; display: block; }}
      .dash-header-text {{ flex: 1; text-align: center; }}
      .dash-header-text h1 {{
        margin: 0; color: {JPSI_DARK} !important;
        font-size: 1.7rem; font-weight: 700; letter-spacing: -0.01em;
      }}
      .dash-header-text .subtitle {{
        color: #6b7280; font-size: 0.85rem; margin: 3px 0 0 0;
      }}

      /* Page title styling */
      .fob-title {{
        background: {JPSI_DARK}; border-left: 6px solid {JPSI_BLUE};
        padding: 12px 20px; border-radius: 10px; margin-bottom: 16px;
      }}
      .fob-title h1 {{ margin: 0; font-size: 1.5rem; color: #ffffff; }}
      .fob-title span {{ color: {JPSI_BLUE}; font-weight: 600; }}

      /* Data tables */
      .sheet-wrap {{
        border-radius: 10px; overflow: hidden; position: relative;
        box-shadow: 0 2px 8px rgba(50,55,60,0.12);
        border: 1px solid #ddd;
        background: #fff; margin-bottom: 16px;
      }}
      .sheet-wrap::after {{
        content: ""; position: absolute; inset: 0;
        background: url('{WATERMARK}') center 46% / 38% auto no-repeat;
        opacity: 0.06; pointer-events: none; z-index: 5;
      }}

      /* External resource callout */
      .resource-link {{
        background: rgba(6,147,227,0.06);
        border: 1px solid #d6e9f7; border-left: 4px solid {JPSI_BLUE};
        border-radius: 8px; padding: 8px 14px; margin: 2px 0 14px 0;
        font-size: 0.88rem; color: {JPSI_DARK};
      }}
      .resource-link a {{ color: {JPSI_BLUE}; font-weight: 600; text-decoration: none; }}
      .resource-link a:hover {{ text-decoration: underline; }}
      .resource-link .rl-sub {{ color: #6b7280; }}

      /* Corridor-trends table (Changes + Barge tabs) */
      .trend-wrap {{
        border: 1px solid #e5e7eb; border-radius: 12px; overflow: hidden;
        background: #fff; box-shadow: 0 1px 3px rgba(50,55,60,0.08);
        margin-bottom: 6px;
      }}
      .trend-title {{
        background: #f3f4f6; padding: 14px 18px; font-size: 1.12rem;
        font-weight: 700; color: {JPSI_DARK};
        border-bottom: 1px solid #e5e7eb;
      }}
      .trend-tbl {{ width: 100%; border-collapse: collapse; font-size: 0.92rem; }}
      .trend-tbl th {{
        text-align: right; padding: 12px 14px; font-weight: 700;
        color: #374151; border-bottom: 2px solid #e5e7eb; white-space: nowrap;
      }}
      .trend-tbl th.loch {{ text-align: left; }}
      .trend-tbl td {{
        text-align: right; padding: 11px 14px; border-bottom: 1px solid #f1f1f1;
        color: #111827; white-space: nowrap;
      }}
      .trend-tbl td.loc {{ text-align: left; color: #c2410c; font-weight: 600; }}
      .trend-tbl tr:nth-child(even) td {{ background: #fafafa; }}
      .trend-tbl tr:hover td {{ background: #eef6fc; }}
      .trend-tbl td.pos {{ color: #0d7f3d; font-weight: 700; }}
      .trend-tbl td.neg {{ color: #c00000; font-weight: 700; }}
      .trend-foot {{
        font-style: italic; color: #6b7280; font-size: 0.78rem;
        padding: 4px 4px 12px;
      }}

      /* Table styling */
      .sheet {{
        width: 100%; border-collapse: collapse; font-size: 0.85rem;
      }}
      .sheet tr.cmdty {{ background: linear-gradient(135deg, {JPSI_BLUE} 0%, #0573b8 100%); }}
      .sheet tr.cmdty td {{
        color: #ffffff; font-weight: 700; padding: 10px 16px; text-align: left;
      }}
      .sheet tr.hdr.months {{
        background: {JPSI_DARK}; color: #ffffff; font-weight: 600;
      }}
      .sheet tr.hdr.months td {{
        padding: 8px 10px; text-align: center; font-size: 0.8rem;
        border-right: 1px solid rgba(255,255,255,0.15); color: #ffffff;
      }}
      .sheet tr.section td {{
        background: #f0f0f0; color: {JPSI_DARK}; font-weight: 700;
        padding: 8px 16px; border-top: 1px solid #ddd; font-size: 0.8rem;
        text-transform: uppercase; letter-spacing: 0.3px; text-align: left;
      }}
      .sheet tr.cash-section td {{
        background: linear-gradient(135deg, {JPSI_BLUE} 0%, #0573b8 100%);
        color: #ffffff; font-weight: 700;
        padding: 10px 16px; border-top: 1px solid #ddd; font-size: 0.85rem;
        text-align: center; letter-spacing: 0.5px;
      }}
      .sheet tr.strong td {{
        padding: 8px 10px; font-weight: 600; border-bottom: 1px solid #f5f5f5;
        color: #1f2328;
      }}
      .sheet tr.frt-row td {{
        padding: 8px 10px; color: #333; border-bottom: 1px solid #f5f5f5;
        font-style: italic; font-weight: 500;
      }}
      .sheet td.lbl {{
        font-weight: 600; color: #2c3e50; width: auto; min-width: 110px;
        padding-left: 12px; text-align: left;
      }}
      .sheet td {{
        padding: 8px 10px; text-align: right; border-right: 1px solid #f5f5f5;
        color: #333; font-weight: 500;
      }}
      .sheet td.de {{
        font-weight: 700; color: #6b7280; text-align: right;
      }}
      .sheet td.up {{
        background-color: #e8f5e9; color: #1f2328; font-weight: 700;
      }}
      .sheet td.down {{
        background-color: #ffebee; color: #1f2328; font-weight: 700;
      }}
      .sheet td.legend {{
        text-align: center; font-size: 0.72rem; color: #555;
        padding: 6px 8px; background: #fbfbfb; border-bottom: 1px solid #eee;
      }}
      .sheet td.legend .lg-sw {{
        display: inline-block; width: 13px; height: 13px; border-radius: 3px;
        vertical-align: middle; margin-right: 5px;
      }}
      .sheet td.legend .lg-sw.up {{ background: #e8f5e9; border: 1px solid #0d7f3d; }}
      .sheet td.legend .lg-sw.dn {{ background: #ffebee; border: 1px solid #c00000; }}
      .sheet .chg {{
        display: block; font-weight: 600; color: #333;
      }}
      .sheet .chg span {{
        font-size: 0.7rem; font-weight: 500; opacity: 0.9;
      }}
      /* Charts */
      .vega-embed {{
        position: relative; background: #ffffff; border-radius: 10px;
        padding: 12px; box-shadow: 0 2px 8px rgba(50,55,60,0.12);
        border: 1px solid #e0e0e0;
      }}
      .vega-embed::before {{
        content: ""; position: absolute; inset: 0;
        background: url('{WATERMARK}') center 48% / 30% auto no-repeat;
        opacity: 0.11; pointer-events: none; z-index: 0;
      }}
      .vega-embed canvas, .vega-embed svg, .vega-embed .marks {{
        position: relative; z-index: 1;
      }}

      /* Streamlit elements */
      h1 {{
        color: {JPSI_DARK} !important;
      }}
      h2 {{
        color: {JPSI_DARK} !important; border-bottom: 3px solid {JPSI_BLUE};
        padding-bottom: 8px; margin-top: 24px; margin-bottom: 16px;
      }}
      h3, h4 {{
        color: {JPSI_DARK} !important; margin-top: 20px; margin-bottom: 12px;
        font-weight: 700;
      }}
      .stMarkdown {{
        color: #2c3e50 !important;
      }}
      label {{
        color: {JPSI_DARK} !important;
        font-weight: 600 !important;
      }}

      /* Buttons — JPSI blue */
      .stButton > button {{
        background: {JPSI_BLUE};
        color: #fff;
        border: none;
        border-radius: 6px;
        font-weight: 600;
      }}
      .stButton > button:hover {{
        background: #057ec2;
        color: #fff;
      }}
      .stDownloadButton > button {{
        background-color: {JPSI_BLUE} !important;
        color: white !important;
        border: none !important;
        border-radius: 6px !important;
        padding: 8px 16px !important;
        font-weight: 600 !important;
      }}
      .stDownloadButton > button:hover {{
        background-color: #057ec2 !important;
      }}

      /* Sidebar — light subtle brand tint (matching Basis Tracker) */
      section[data-testid="stSidebar"] {{
        background: #f6f8fa;
        border-right: 1px solid #e6eaee;
      }}
      section[data-testid="stSidebar"] h3, section[data-testid="stSidebar"] h2 {{
        color: {JPSI_DARK};
      }}
      .stSidebar label {{
        color: {JPSI_DARK} !important;
      }}

      /* Tabs — JPSI blue active indicator */
      .stTabs [data-baseweb="tab-list"] {{
        gap: 0;
        background: #ffffff;
        border-bottom: 1px solid #e2e8f0;
      }}
      /* Force EVERY element inside a tab dark + opaque. Uses the ARIA role
         selector [role="tab"] (stable across Streamlit versions) plus baseweb,
         and sets -webkit-text-fill-color (baseweb sets it, and it overrides
         `color`, which is what made inactive labels faint). */
      [role="tab"],
      [role="tab"] *,
      .stTabs [data-baseweb="tab"],
      .stTabs [data-baseweb="tab"] * {{
        color: {JPSI_DARK} !important;
        opacity: 1 !important;
        font-weight: 700 !important;
        font-size: 14px !important;
        -webkit-text-fill-color: {JPSI_DARK} !important;
      }}
      [role="tab"] {{
        padding: 8px 18px;
        border-radius: 0;
      }}
      [role="tab"]:hover,
      [role="tab"]:hover * {{
        color: {JPSI_BLUE} !important;
        -webkit-text-fill-color: {JPSI_BLUE} !important;
      }}
      [role="tab"][aria-selected="true"] {{
        border-bottom: 3px solid {JPSI_BLUE} !important;
      }}
      [role="tab"][aria-selected="true"],
      [role="tab"][aria-selected="true"] * {{
        color: #000000 !important;
        -webkit-text-fill-color: #000000 !important;
        font-weight: 800 !important;
      }}
      .stTabs [data-baseweb="tab-panel"] {{
        padding-top: 8px !important;
      }}

      /* Text in main area */
      body, .stMarkdown {{
        color: #333;
      }}

      /* Input form styling */
      .stTextArea textarea {{
        color: #1f2328 !important;
      }}
      .stNumberInput input {{
        color: #1f2328 !important;
      }}
      .stSelectbox select {{
        color: #1f2328 !important;
      }}
      .stDateInput input {{
        color: #1f2328 !important;
      }}
      .stButton > button {{
        color: #fff !important;
      }}

      /* Expander styling */
      .stExpander {{
        border: 1px solid #ddd !important;
      }}
      .stExpander > summary {{
        color: {JPSI_DARK} !important;
        font-weight: 600 !important;
      }}

      /* Caption and status text */
      .stCaption {{
        color: #666 !important;
      }}

      /* DataFrame/table text */
      .stDataFrame {{
        color: #333 !important;
      }}

      /* Caption and metadata */
      .caption {{
        color: #666; font-size: 0.85rem; font-style: italic;
        margin-top: 8px;
      }}

      /* Additional table styling */
      td.datebar {{
        text-align: center !important; font-weight: 600;
        font-size: 0.82rem; letter-spacing: .04em; text-transform: uppercase;
        color: #7a828b; background: #fff; padding: 8px;
      }}
      td.cmdty {{
        text-align: center !important; font-weight: 700; color: #fff;
        font-size: 1.15rem; letter-spacing: .06em; padding: 9px;
        text-shadow: 0 1px 2px rgba(0,0,0,0.18);
      }}
      tr.reach td {{
        text-align: center !important; font-weight: 700;
        font-size: 0.7rem; letter-spacing: .08em; text-transform: uppercase;
        background: #f1f3f5; padding-top: 7px; padding-bottom: 7px;
      }}
      table.sheet tr.spread td {{ font-weight: 600; }}
      table.sheet td.slabel {{
        text-align: right; color: #6b7280;
        font-style: italic; font-weight: 600;
      }}
      table.sheet tbody tr:hover td {{ background: #eef6fd; }}
      table.sheet tr.band td {{ background: #fafbfc; }}
      table.sheet tr:last-child td {{ border-bottom: none; }}

      /* Darker text for expanders and warnings */
      [data-testid="stExpander"] summary {{
        color: {JPSI_DARK} !important;
        font-weight: 600 !important;
      }}
      [data-testid="stExpander"] {{
        color: {JPSI_DARK} !important;
      }}
      [data-testid="stAlert"] {{
        color: {JPSI_DARK} !important;
      }}
      [data-testid="stAlert"] div {{
        color: {JPSI_DARK} !important;
      }}
      [data-testid="stAlert"] p {{
        color: {JPSI_DARK} !important;
      }}
    </style>
    """, unsafe_allow_html=True
)

_logo_html = (f'<img src="{LOGO_URI}" alt="John Stewart &amp; Associates">'
              if LOGO_URI else '')
st.markdown(
    f'<div class="dash-header">'
    f'  <div class="dash-header-logo">{_logo_html}</div>'
    f'  <div class="dash-header-text">'
    f'    <h1>River FOB Values</h1>'
    f'    <div class="subtitle">Commodity &amp; Ag Risk Management Specialists '
    f'&nbsp;·&nbsp; est. 1976</div>'
    f'  </div>'
    f'  <div style="width:180px"></div>'  # spacer to keep the title centred
    f'</div>',
    unsafe_allow_html=True,
)


# Read-only / share mode: append ?view=1 to the URL. Exempt from the password.
VIEW_ONLY = str(st.query_params.get("view", "")).lower() in (
    "1", "true", "yes", "read", "readonly", "view")


def _require_password():
    """Gate the editable/download app behind a password. The read-only view
    (?view=1) is exempt. The password comes from EDIT_PASSWORD (Streamlit secrets
    or an env var); if none is configured the app stays open (e.g. local dev)."""
    if VIEW_ONLY or st.session_state.get("_authed"):
        return
    expected = None
    try:
        expected = st.secrets.get("EDIT_PASSWORD")
    except Exception:
        pass
    expected = expected or os.environ.get("EDIT_PASSWORD")
    if not expected:
        return                       # no password configured → open
    _, mid, _ = st.columns([1, 1.5, 1])
    with mid:
        st.markdown("#### 🔒 Protected")
        st.caption("Enter the password to edit and download. Read-only viewers "
                   "can use the shared **?view=1** link — no password needed.")
        pw = st.text_input("Password", type="password", key="_pw")
        if pw:
            if pw == expected:
                st.session_state["_authed"] = True
                st.rerun()
            else:
                st.error("Incorrect password.")
    st.stop()


_require_password()


# --- session state seed ----------------------------------------------------
def _by_month(seed_list):
    """A June-window-aligned seed list -> {month_name: value} for name lookup."""
    return dict(zip(S.SEED_MONTHS, seed_list))


def _init_state():
    """Create the editable input tables if absent, indexed by the current
    rolling window (M.MONTHS). Seeds are matched by month name so a rolled
    window keeps overlapping months and blanks the newly-added ones."""
    months = M.MONTHS
    if "freight" not in st.session_state:
        st.session_state.freight = pd.DataFrame(
            {m: [_by_month(S.SEED_FREIGHT[r]).get(m) for r in M.FREIGHT_REGIONS]
                 for m in months},
            index=M.FREIGHT_REGIONS,
        )
    for c in M.COMMODITIES:
        if f"cif_{c}" not in st.session_state:
            cifm, futm = _by_month(S.SEED_CIF[c]), _by_month(S.SEED_FUTURES[c])
            st.session_state[f"cif_{c}"] = pd.DataFrame(
                {"CIF": [cifm.get(m) for m in months],
                 "Futures": [futm.get(m) for m in months]},
                index=months,
            )
        if f"carry_{c}" not in st.session_state:
            # getattr guard: survive a stale/partial module reload where
            # SEED_SPREAD_LABELS isn't present yet (new spreads just seed to 0).
            seed_labels = getattr(S, "SEED_SPREAD_LABELS", {}).get(c, [])
            seedmap = dict(zip(seed_labels, S.SEED_SPREADS[c]))
            st.session_state[f"carry_{c}"] = pd.DataFrame(
                {lbl: [seedmap.get(lbl, 0.0)] for lbl in M.spread_labels_for(c)},
                index=["Spread"],
            )
        if f"cashc_{c}" not in st.session_state:
            st.session_state[f"cashc_{c}"] = S.SEED_CASH_C[c]
        if f"storage_{c}" not in st.session_state:
            st.session_state[f"storage_{c}"] = S.SEED_STORAGE_MO[c]
    if "interest_pct" not in st.session_state:
        # Default to the live front-month fed funds + 2.25% (same source and
        # convention as the cost-of-carry sheet); fall back to the static seed
        # if the live rate can't be read. Still editable below.
        _ff = _live_fed_funds(dt.date.today().isoformat())
        st.session_state.interest_pct = _ff["rate"] if _ff else S.SEED_INTEREST_PCT
    if "editor_ver" not in st.session_state:
        st.session_state.editor_ver = 0


def _reindex_to_window():
    """Roll the persisted input tables onto the current month window: overlapping
    months keep their values, newly-added months (e.g. Feb) come in blank, and
    months that fell off the front are dropped. Runs when the as-of month changes."""
    months = M.MONTHS
    changed = False
    f = st.session_state.freight
    if list(f.columns) != months:
        st.session_state.freight = f.reindex(columns=months)
        changed = True
    for c in M.COMMODITIES:
        df = st.session_state[f"cif_{c}"]
        if list(df.index) != months:
            st.session_state[f"cif_{c}"] = df.reindex(months)
            changed = True
    if changed:
        # Drop any contract overrides captured from an earlier paste — they were
        # positioned for the old window and would desync the displayed contract
        # row / spread auto-compute from the rolled window. They fall back to
        # M.CONTRACTS (correct for the new window) until the next paste.
        for c in M.COMMODITIES:
            st.session_state.pop(f"contracts_{c}", None)
        _bump_editors()


def _apply_paste_contracts():
    """Let the contracts captured from the pasted CIF sheet drive the model, so a
    manually-rolled front (e.g. soybeans SN→SQ) flows through to the spot anchor,
    spreads, and top-of-carry. Falls back to the computed cycle when nothing was
    pasted for this window."""
    for c in M.COMMODITIES:
        sc = st.session_state.get(f"contracts_{c}")
        if sc and len(sc) == len(M.MONTHS):
            M.CONTRACTS[c] = list(sc)


def _reindex_carry():
    """Keep each commodity's spread editor aligned to its current contract chain.
    When the front rolls, labels change (SN/SQ → SQ/SX …); persisting labels keep
    their value, brand-new ones seed to 0."""
    for c in M.COMMODITIES:
        labels = M.spread_labels_for(c)
        cdf = st.session_state.get(f"carry_{c}")
        if cdf is None or list(cdf.columns) == labels:
            continue
        seedmap = dict(zip(S.SEED_SPREAD_LABELS[c], S.SEED_SPREADS[c]))
        old = {col: cdf.loc["Spread", col] for col in cdf.columns}
        vals = {lbl: (old[lbl] if lbl in old else seedmap.get(lbl, 0.0))
                for lbl in labels}
        st.session_state[f"carry_{c}"] = pd.DataFrame(
            {lbl: [v] for lbl, v in vals.items()}, index=["Spread"])
        _bump_editors()


def _bump_editors():
    """Force input editors to re-read session state after a programmatic load."""
    st.session_state.editor_ver += 1


_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _archive_filter(dates, key):
    """Year + Month selectboxes that narrow an ISO-date list (newest first) so a
    multi-year archive is quick to browse. Returns the filtered list; `key`
    namespaces the widgets per call site. Month options follow the chosen year."""
    if not dates:
        return dates
    years = sorted({d[:4] for d in dates}, reverse=True)
    c1, c2 = st.columns(2)
    with c1:
        fy = st.selectbox("Year", ["All"] + years, key=f"{key}_year")
    pool = dates if fy == "All" else [d for d in dates if d[:4] == fy]
    lbl_to_num = {_MONTH_ABBR[int(m) - 1]: m for m in sorted({d[5:7] for d in pool})}
    with c2:
        fm = st.selectbox("Month", ["All"] + list(lbl_to_num), key=f"{key}_month")
    if fm != "All" and fm in lbl_to_num:
        pool = [d for d in pool if d[5:7] == lbl_to_num[fm]]
    return pool


# (The As-of date is user-controlled; a pasted freight table no longer moves it.)
st.session_state.pop("pending_as_of", None)

# --- sidebar ---------------------------------------------------------------
with st.sidebar:
    _logo_sb = (f'<img src="{LOGO_URI}" style="height:34px;margin-bottom:8px;" '
                f'alt="JSA">' if LOGO_URI else
                '<div style="font-weight:900;font-size:1.4rem;color:#0693e3;">JSA</div>')
    st.markdown(
        '<div style="text-align: center; padding: 14px 8px; border-bottom: 3px solid #0693e3; margin: -1rem -1rem 20px -1rem; background: rgba(6,147,227,0.06);">'
        + _logo_sb +
        '<h3 style="margin: 0; color: #32373c; font-size: 0.9rem; font-weight: 600;">River FOB Portal</h3>'
        '<small style="color: #666; font-size: 0.78rem;">Commodity &amp; Ag Risk Management</small>'
        '</div>',
        unsafe_allow_html=True
    )
    if VIEW_ONLY:
        # Read-only: just a date browser over the archived history.
        _all = db.list_dates()                         # newest first
        st.subheader("History")
        if _all:
            _dates = _archive_filter(_all, "vh")
            view_choice = st.selectbox(
                "Viewing date", _dates, index=0,
                help="Filter by year/month, then pick a day. Read-only view.")
        else:
            view_choice = None
            st.info("No archived dates yet.")
        as_of = (dt.date.fromisoformat(view_choice) if view_choice
                 else dt.date.today())
        M.MONTHS = M.months_for(as_of)
        M.CONTRACTS = {c: M.contracts_for(c, as_of) for c in M.COMMODITIES}
        _init_state()
        st.caption("🔒 Read-only view — editing and downloads are disabled.")
        HIST_DATE = view_choice
    else:
        st.subheader("Snapshot")
        as_of = st.date_input(
            "As-of date", value=dt.date.today(), key="as_of_input",
            help="The date the inputs represent and will save under. Can be a "
                 "future date when prepping the next sheet. Auto-set when you "
                 "paste a dated freight table.")
        # Roll the working delivery window + contracts to the chosen as-of month
        # (e.g. July drops June and adds February), then create/reindex inputs.
        M.MONTHS = M.months_for(as_of)
        M.CONTRACTS = {c: M.contracts_for(c, as_of) for c in M.COMMODITIES}
        _init_state()
        _reindex_to_window()
        # Honor a manually-rolled front captured from the pasted sheet, then
        # align the spread editors to the resulting contract chain.
        _apply_paste_contracts()
        _reindex_carry()
        st.caption(
            f"Delivery window: **{M.MONTHS[0]} – {M.MONTHS[-1]}** (rolls with the "
            "as-of month). Enter CIF & barge freight on the **📝 Inputs** tab — "
            "the commodity tabs update live; *what-if* until you **Save**.")
        st.markdown("**Full-carry assumptions**")
        st.session_state.interest_pct = st.number_input(
            "Annual interest rate (%)", value=float(st.session_state.interest_pct),
            step=0.25, format="%.2f",
            help="Used for % Full Carry; storage is per-commodity on the Inputs tab. "
                 "Defaults to live front-month fed funds + "
                 f"{massive_futures.FED_FUNDS_SPREAD_PCT:.2f}% (CME ZQ).")
        _ff = _live_fed_funds(dt.date.today().isoformat())
        if _ff:
            st.caption(
                f"Live fed funds **{_ff['ff']:.3f}%** + "
                f"{massive_futures.FED_FUNDS_SPREAD_PCT:.2f}% = **{_ff['rate']:.2f}%**")
            if abs(float(st.session_state.interest_pct) - _ff["rate"]) > 1e-9:
                if st.button("↺ Reset to live rate", key="reset_interest_live"):
                    st.session_state.interest_pct = _ff["rate"]
                    st.rerun()
        else:
            st.caption("Live fed funds unavailable — using a static default.")

        st.divider()
        st.subheader("Archive")
        st.caption(f"CIF + barge freight · {DB_BACKEND}")
        _arch_dates = _archive_filter(db.list_dates(), "arch")   # year/month filter
        view_choice = st.selectbox(
            "View archived date", ["✏️ Working (live)"] + _arch_dates,
            index=1 if _arch_dates else 0,                 # default: latest saved day
            help="Filter by year/month above, then pick a day (opens read-only). "
                 "Choose '✏️ Working (live)' to edit on the Inputs tab.")
        if st.button("↺ Reset inputs to seed"):
            for k in list(st.session_state.keys()):
                if k.startswith(("freight", "cif_", "carry_", "cashc_", "storage_")):
                    del st.session_state[k]
            _init_state()
            _bump_editors()
            st.rerun()
        HIST_DATE = None if view_choice.startswith("✏️") else view_choice


# --- formatting helpers ----------------------------------------------------
def _num(v, dec):
    return "" if v is None or pd.isna(v) else f"{v:.{dec}f}"


def _pct(v):
    return "" if v is None or pd.isna(v) else f"{v * 100:.0f}%"


def _fob_cell(v):
    if v is None or pd.isna(v):
        return "<td></td>"
    if v < 0:
        return f'<td class="neg">({abs(v):.2f})</td>'
    return f"<td>{v:.2f}</td>"


def _spread_cell(v):
    if v is None or pd.isna(v):
        return "<td></td>"
    if v < 0:
        return f'<td class="neg">({abs(v):.4f})</td>'
    return f"<td>{v:.4f}</td>"


def _carry_pct_cell(v):
    if v is None or pd.isna(v):
        return "<td></td>"
    cls = ' class="neg"' if v < 0 else ""
    return f"<td{cls}>{v * 100:.0f}%</td>"


# --- PDF export ------------------------------------------------------------
def _fnum(v, dec):
    """(text, is_negative) for the PDF — negatives shown in (parens)."""
    if v is None or pd.isna(v):
        return ("", False)
    if v < 0:
        return (f"({abs(v):.{dec}f})", True)
    return (f"{v:.{dec}f}", False)


def _sheet_source(commodity, hist):
    """Resolve the sheet's inputs from either an archived snapshot or the live
    working state. `has_futures` / `has_spreads` say whether the CBOT and
    Spreads/Top-Carry sections can be shown (older snapshots stored only
    CIF + freight; newer ones also carry futures + spreads).

    hist: (cif, freight, calendar, futures, spreads) or None for live.
    """
    cashc = st.session_state[f"cashc_{commodity}"]
    interest = st.session_state.interest_pct / 100.0
    storage = st.session_state[f"storage_{commodity}"]
    if hist is not None:
        cif, frt, cal, futures, spreads_hist = hist
        cols = (cal or {}).get(commodity)
        months = [m for m, _ in cols] if cols else list(M.MONTHS)
        contracts = [c for _, c in cols] if cols else list(M.CONTRACTS[commodity])
        cif_row = cif.get(commodity) or {}
        fbr = {r: (frt.get(r) or {}) for r in M.FREIGHT_REGIONS}
        fut_row = (futures or {}).get(commodity) or {}
        pairs = (spreads_hist or {}).get(commodity) or []
        labels = [l for l, _ in pairs]
        spreads = [v for _, v in pairs]
        fullcarry = (M.compute_full_carry(commodity, fut_row, interest, storage,
                                          contracts=contracts, months=months)
                     if fut_row else [])
        grid = M.compute_fob_grid(commodity, cif_row, fbr, months)
        return dict(months=months, contracts=contracts, cif_row=cif_row,
                    fut_row=fut_row, fbr=fbr, grid=grid, spreads=spreads,
                    fullcarry=fullcarry, labels=labels, cashc=cashc,
                    has_futures=any(v is not None for v in fut_row.values()),
                    has_spreads=bool(spreads))
    months = list(M.MONTHS)
    df = st.session_state[f"cif_{commodity}"]
    cif_row = {m: _safe(df.loc[m, "CIF"]) for m in months}
    fut_row = {m: _safe(df.loc[m, "Futures"]) for m in months}
    fbr = {r: {m: _safe(st.session_state.freight.loc[r, m]) for m in months}
           for r in M.FREIGHT_REGIONS}
    contracts = (st.session_state.get(f"contracts_{commodity}")
                 or list(M.CONTRACTS[commodity]))
    labels = M.spread_labels_for(commodity)
    spreads = _live_spreads(commodity)      # derived from the CBOT futures row
    fullcarry = M.compute_full_carry(commodity, fut_row, interest, storage)
    grid = M.compute_fob_grid(commodity, cif_row, fbr, months)
    return dict(months=months, contracts=contracts, cif_row=cif_row,
                fut_row=fut_row, fbr=fbr, grid=grid, spreads=spreads,
                fullcarry=fullcarry, labels=labels, cashc=cashc,
                has_futures=True, has_spreads=True)


def _build_pdf_sheet(commodity, hist=None):
    """Structured spec of one commodity's sheet for fob_pdf.build_pdf."""
    s = _sheet_source(commodity, hist)
    months, cfg = s["months"], M.CARRY_CONFIG[commodity]
    grid, fbr = s["grid"], s["fbr"]

    rows = [("months", "", [(m, False) for m in months]),
            ("contracts", "", [(c, False) for c in s["contracts"][:len(months)]])]
    if s["has_futures"]:
        rows.append(("cbot", "CBOT",
                     [(_num(s["fut_row"].get(m), 4), False) for m in months]))
    rows.append(("cif", "CIF", [_fnum(s["cif_row"].get(m), 2) for m in months]))
    rows.append(("section", "Cash vs Delivery", None))
    cash = M.cash_vs_delivery(commodity, grid[cfg["cash_loc"]], s["cashc"], months)
    rows.append(("cash", cfg["cash_label"], [_fnum(v, 2) for v in cash]))

    for item in M.BLOCK_LAYOUT:
        if item[0] == "reach":
            rows.append(("section", item[1], None))
        elif item[0] == "freight":
            _, region, label = item
            fr = fbr.get(region, {})
            rows.append(("freight", label,
                         [(_pct(fr.get(m)), False) for m in months]))
        else:
            loc = item[1]
            rows.append(("fob", f"FOB Barge {loc}",
                         [_fnum(grid[loc].get(m), 2) for m in months]))

    if s["has_spreads"]:
        labels, spreads = s["labels"], s["spreads"]
        rows.append(("section", "Spreads · Carry", None))
        n = len(labels)
        pad = max(0, len(months) - 2 * n)
        scells = [("", False)] * pad
        for i in range(n):
            scells.append((labels[i], False))
            scells.append(_fnum(spreads[i], 4))
        scells = (scells + [("", False)] * len(months))[:len(months)]
        rows.append(("spread", "Spreads", scells))

        carry = M.pct_full_carry(spreads, s["fullcarry"])
        ccells = [("", False)] * len(months)
        for i in range(n):
            pos = pad + 2 * i + 1
            if pos < len(ccells) and i < len(carry) and carry[i] is not None \
                    and not pd.isna(carry[i]):
                ccells[pos] = (f"{carry[i] * 100:.0f}%", carry[i] < 0)
        rows.append(("carry", "% Full Carry", ccells))

        for label, loc in cfg["top_carry"]:
            tc = M.top_carry(commodity, grid[loc], spreads,
                             contracts=s["contracts"], months=months)
            rows.append(("topcarry", label, [_fnum(v, 2) for v in tc]))

    de = getattr(M, "DELIVERY_EQUIV", {}).get(commodity, {})
    if de:
        months = ["Del Equiv"] + list(months)                  # first data column
        out = []
        for kind, label, cells in rows:
            if cells is None:                                   # full-width section
                out.append((kind, label, cells))
            elif kind == "months":
                out.append((kind, label, [("Del Equiv", False)] + list(cells)))
            elif kind == "fob":
                out.append((kind, label,
                            [_fnum(de.get(label.replace("FOB Barge ", "")), 2)] + list(cells)))
            else:
                out.append((kind, label, [("", False)] + list(cells)))
        rows = out

    return {"commodity": commodity, "months": list(months), "rows": rows}


def build_fob_pdf(as_of, hist=None):
    """3-page PDF (Corn, Soybeans, Wheat) — live working sheets, or an archived
    snapshot when `hist` (cif, freight, calendar) is given."""
    sheets = [_build_pdf_sheet(c, hist) for c in M.COMMODITIES]
    return fob_pdf.build_pdf(as_of, sheets)


def _xnum(v):
    """Raw float (or None) for Excel cells."""
    return None if v is None or pd.isna(v) else float(v)


def _build_excel_sheet(commodity, hist=None):
    """Structured spec with raw numeric values for fob_excel.build_xlsx."""
    s = _sheet_source(commodity, hist)
    months, cfg = s["months"], M.CARRY_CONFIG[commodity]
    grid, fbr = s["grid"], s["fbr"]

    rows = [("banner", commodity, None),
            ("months", "", list(months)),
            ("contracts", "", list(s["contracts"][:len(months)]))]
    if s["has_futures"]:
        rows.append(("cbot", "CBOT", [_xnum(s["fut_row"].get(m)) for m in months]))
    rows.append(("cif", "CIF", [_xnum(s["cif_row"].get(m)) for m in months]))
    rows.append(("section", "Cash vs Delivery", None))
    cash = M.cash_vs_delivery(commodity, grid[cfg["cash_loc"]], s["cashc"], months)
    rows.append(("cash", cfg["cash_label"], [_xnum(v) for v in cash]))
    for item in M.BLOCK_LAYOUT:
        if item[0] == "reach":
            rows.append(("section", item[1], None))
        elif item[0] == "freight":
            _, region, label = item
            fr = fbr.get(region, {})
            rows.append(("freight", label, [_xnum(fr.get(m)) for m in months]))
        else:
            loc = item[1]
            rows.append(("fob", f"FOB Barge {loc}",
                         [_xnum(grid[loc].get(m)) for m in months]))

    if s["has_spreads"]:
        labels, spreads = s["labels"], s["spreads"]
        rows.append(("section", "Spreads · Carry", None))
        n = len(labels)
        pad = max(0, len(months) - 2 * n)
        scells = [None] * pad
        for i in range(n):
            scells.append(labels[i])
            scells.append(_xnum(spreads[i]))
        scells = (scells + [None] * len(months))[:len(months)]
        rows.append(("spread", "Spreads", scells))
        carry = M.pct_full_carry(spreads, s["fullcarry"])
        ccells = [None] * len(months)
        for i in range(n):
            pos = pad + 2 * i + 1
            if pos < len(ccells) and i < len(carry):
                ccells[pos] = _xnum(carry[i])
        rows.append(("carry", "% Full Carry", ccells))
        for label, loc in cfg["top_carry"]:
            tc = M.top_carry(commodity, grid[loc], spreads,
                             contracts=s["contracts"], months=months)
            rows.append(("topcarry", label, [_xnum(v) for v in tc]))

    de = getattr(M, "DELIVERY_EQUIV", {}).get(commodity, {})
    if de:
        months = ["Del Equiv"] + list(months)                  # first data column
        out = []
        for kind, label, cells in rows:
            if cells is None:                                   # banner / section
                out.append((kind, label, cells))
            elif kind == "months":
                out.append((kind, label, ["Del Equiv"] + list(cells)))
            elif kind == "fob":
                out.append((kind, label,
                            [_xnum(de.get(label.replace("FOB Barge ", "")))] + list(cells)))
            else:
                out.append((kind, label, [None] + list(cells)))
        rows = out

    return {"commodity": commodity, "months": list(months), "rows": rows}


def build_fob_xlsx(as_of, hist=None):
    """One-sheet workbook (tab = date) with Corn, Soybeans, Wheat stacked —
    live working sheets, or an archived snapshot when `hist` is given."""
    sheets = [_build_excel_sheet(c, hist) for c in M.COMMODITIES]
    return fob_excel.build_xlsx(as_of, sheets)


def _dir_cls(cur, prior):
    """Green 'up' / red 'down' / '' based on change vs the prior day."""
    try:
        if cur is None or prior is None or pd.isna(cur) or pd.isna(prior):
            return ""
    except TypeError:
        return ""
    return "up" if cur > prior else "down" if cur < prior else ""


def _dir_td(cur, prior, kind):
    """A data cell coloured by day-over-day direction. kind: cif|pct|fob."""
    cls = _dir_cls(cur, prior)
    if cur is None or pd.isna(cur):
        return "<td></td>"
    if kind == "pct":
        txt = f"{cur * 100:.0f}%"
    elif kind == "fob":
        txt = f"({abs(cur):.2f})" if cur < 0 else f"{cur:.2f}"
    else:  # cif
        txt = f"{cur:.2f}"
    return f'<td class="{cls}">{txt}</td>' if cls else f"<td>{txt}</td>"


def _data_row(label, vals, fmt, band, lbl_cls="lbl", row_cls="", lead=""):
    cells = "".join(f"<td>{fmt(v)}</td>" for v in vals)
    cls = (" band" if band else "") + (f" {row_cls}" if row_cls else "")
    return (f'<tr class="{cls.strip()}"><td class="{lbl_cls}">{label}</td>'
            f'{lead}{cells}</tr>')


def render_block(commodity, as_of, cif_row, fut_row, freight_by_region,
                 spreads, fullcarry, cash_c, historical=False, contracts=None,
                 months=None, prior=None):
    """Render one commodity as a smoothed spreadsheet-style HTML block.

    months: the column keys to render (defaults to the live MONTHS). Archived
    dates pass their own stored months so older sheets show their real columns.
    When historical=True only the archived/recomputable rows are shown
    (CIF, FOB by reach, Cash vs Delivery) — CBOT and the carry section are
    omitted because futures/spreads aren't stored in the archive.
    """
    months = months or M.MONTHS
    de = getattr(M, "DELIVERY_EQUIV", {}).get(commodity, {})
    show_de = bool(de)
    de_blank = '<td class="de"></td>' if show_de else ''
    de_hdr = '<td class="de de-hdr">Del Equiv</td>' if show_de else ''

    def _de_cell(loc):
        if not show_de:
            return ''
        v = de.get(loc)
        return f'<td class="de">{v:.2f}</td>' if v is not None else de_blank

    ncol = len(months) + 1 + (1 if show_de else 0)
    c0, c1 = COMMODITY_THEME[commodity]
    banner = f"background:linear-gradient(135deg,{c0},{c1});"
    reach_style = f"color:{c1};box-shadow:inset 3px 0 0 {c1};"
    rows = []
    rows.append(
        f'<tr><td class="legend" colspan="{ncol}">'
        f'<span class="lg-sw up"></span>Green shade = daily move higher'
        f'&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;'
        f'<span class="lg-sw dn"></span>Red shade = daily move lower</td></tr>')
    rows.append(f'<tr><td class="datebar" colspan="{ncol}">{as_of:%A, %B %d, %Y}</td></tr>')
    rows.append(f'<tr><td class="cmdty" colspan="{ncol}" style="{banner}">{commodity}</td></tr>')
    # month + contract header rows (contract month is archived per column)
    contracts = contracts or M.CONTRACTS[commodity]
    rows.append('<tr class="hdr months"><td class="lbl"></td>' + de_hdr +
                "".join(f"<td>{m}</td>" for m in months) + "</tr>")
    rows.append('<tr class="hdr"><td class="lbl"></td>' + de_blank +
                "".join(f"<td>{c or ''}</td>" for c in contracts) + "</tr>")
    prior = prior or {}
    # CBOT + carry show whenever the data exists — live always, archived only
    # for days saved with futures/spreads (older snapshots stored just CIF/frt).
    show_cbot = bool(fut_row) and any(fut_row.get(m) is not None for m in months)
    show_carry = bool(spreads) and any(s is not None for s in spreads)
    if show_cbot:
        rows.append(_data_row("CBOT", [fut_row.get(m) for m in months],
                              lambda v: _num(v, 4), band=True, lead=de_blank))
    p_cif = prior.get("cif", {})
    cif_cells = "".join(_dir_td(cif_row.get(m), p_cif.get(m), "cif") for m in months)
    rows.append(f'<tr class="strong"><td class="lbl">CIF</td>{de_blank}{cif_cells}</tr>')

    grid = M.compute_fob_grid(commodity, cif_row, freight_by_region, months)
    p_grid = prior.get("grid", {})
    p_frt = prior.get("freight", {})

    # Cash vs Delivery section (before river reaches)
    cfg = M.CARRY_CONFIG[commodity]
    rows.append(f'<tr class="cash-section"><td colspan="{ncol}">Cash vs Delivery</td></tr>')
    cash = dict(zip(months,
                    M.cash_vs_delivery(commodity, grid[cfg["cash_loc"]], cash_c, months)))
    p_cash = prior.get("cash", {})
    cash_cells = "".join(_dir_td(cash[m], p_cash.get(m), "fob") for m in months)
    rows.append(f'<tr class="strong"><td class="lbl">{cfg["cash_label"]}</td>'
                f'{de_blank}{cash_cells}</tr>')

    # Spreads / Carry section (after Cash vs Delivery, before river reaches).
    # Labels + count follow the current contract chain (spreads roll with the
    # front), laid out label/value across the trailing columns.
    if show_carry:
        labels = M.spread_labels_for(commodity, contracts)
        n = len(labels)
        pad = max(0, len(months) - 2 * n)
        scells = ["<td></td>"] * pad
        for i in range(n):
            scells.append(f'<td class="slabel">{labels[i]}</td>')
            scells.append(_spread_cell(spreads[i]) if i < len(spreads)
                          else "<td></td>")
        rows.append('<tr class="spread"><td class="lbl">Spreads</td>'
                    + de_blank + "".join(scells) + "</tr>")

        # % Full Carry sits under each spread's value column.
        carry = M.pct_full_carry(spreads, fullcarry)
        ccells = ["<td></td>"] * len(months)
        for i in range(min(n, len(carry))):
            pos = pad + 2 * i + 1
            if pos < len(ccells):
                ccells[pos] = _carry_pct_cell(carry[i])
        rows.append('<tr class="spread"><td class="lbl">% Full Carry</td>'
                    + de_blank + "".join(ccells) + "</tr>")

    band = True
    for item in M.BLOCK_LAYOUT:
        if item[0] == "reach":
            rows.append(f'<tr class="reach"><td colspan="{ncol}" '
                        f'style="{reach_style}">{item[1]}</td></tr>')
            band = True
            continue
        if item[0] == "freight":
            _, region, label = item
            fr = freight_by_region.get(region, {})
            pf = p_frt.get(region, {})
            cells = "".join(_dir_td(fr.get(m), pf.get(m), "pct") for m in months)
            rows.append(f'<tr class="frt-row{" band" if band else ""}">'
                        f'<td class="lbl">{label}</td>{de_blank}{cells}</tr>')
        else:  # fob
            loc = item[1]
            pg = p_grid.get(loc, {})
            cells = "".join(_dir_td(grid[loc][m], pg.get(m), "fob") for m in months)
            rows.append(f'<tr class="{"band" if band else ""}">'
                        f'<td class="lbl">FOB Barge {loc}</td>{_de_cell(loc)}{cells}</tr>')
        band = not band

    # Top Carry rows at the bottom (above the chart)
    if show_carry:
        rows.append(f'<tr class="section"><td colspan="{ncol}">Top of Carry</td></tr>')
        for label, loc in cfg["top_carry"]:
            tc = M.top_carry(commodity, grid[loc], spreads,
                             contracts=contracts, months=months)
            rows.append(f'<tr><td class="lbl">{label}</td>'
                        + de_blank + "".join(_fob_cell(v) for v in tc) + "</tr>")

    return f'<div class="sheet-wrap"><table class="sheet">{"".join(rows)}</table></div>'


CHART_LABEL = {"Corn": "Corn", "Soybeans": "Beans", "Wheat": "SRW"}


def _archived_carry(commodity, date_iso, loc, spreads):
    """Top-of-carry curve at `loc` for an archived date, aligned to M.MONTHS.

    The archive stores only CIF + freight, so spreads aren't available per
    date — the current spread structure is reused to anchor to spot. Archived
    month labels are remapped to the canonical columns by month number.
    """
    cif_d, frt_d, _ = db.load_snapshot(date_iso)
    if not cif_d:
        return None
    num_to_canon = {_month_num(m): m for m in M.MONTHS}
    cmcif = cif_d.get(commodity, {}) or {}
    cif_canon = {num_to_canon[n]: v for m, v in cmcif.items()
                 if (n := _month_num(m)) in num_to_canon}
    frt_canon = {}
    for region, mv in (frt_d or {}).items():
        frt_canon[region] = {num_to_canon[n]: v for m, v in mv.items()
                             if (n := _month_num(m)) in num_to_canon}
    grid_d = M.compute_fob_grid(commodity, cif_canon, frt_canon)
    return M.top_carry(commodity, grid_d[loc], spreads)


def render_carry_chart(commodity, grid, spreads, as_of=None, months=None,
                       contracts=None, cur_label=None):
    """Top-of-carry (cash forward curve on spot futures) for a chosen location,
    optionally overlaying the same curve from one or more archived dates.
    `contracts` lets an archived date anchor to its own front contract."""
    months = months or M.MONTHS
    locs = [it[1] for it in M.BLOCK_LAYOUT if it[0] == "fob"]
    default = locs.index("STL") if "STL" in locs else 0
    cc1, cc2 = st.columns([1, 2])
    with cc1:
        loc = st.selectbox(
            "Location", locs, index=default, key=f"carry_chart_loc_{commodity}",
            help="Top-of-carry curve for this location, anchored at its spot "
                 "basis (the first month) and carried out on spot futures.")
    with cc2:
        cmp_dates = st.multiselect(
            "Overlay saved dates", db.list_dates(), key=f"carry_cmp_{commodity}",
            help="Add the forward curve from one or more archived dates to "
                 "compare how it has shifted. Archived curves use the current "
                 "spread structure (spreads aren't stored per date).")

    def _mdy(dd):
        return f"{dd.month}/{dd.day}/{dd.year % 100:02d}"

    if cur_label is None:
        cur_label = f"Working ({_mdy(as_of)})" if as_of else "Working"
    rows = []
    tc = M.top_carry(commodity, grid[loc], spreads, contracts=contracts,
                     months=months)
    for m, v in zip(months, tc):
        if v is not None and not pd.isna(v):
            rows.append({"Month": m, "Carry": float(v), "Series": cur_label})
    for d in cmp_dates:
        tcd = _archived_carry(commodity, d, loc, spreads)
        if not tcd:
            continue
        dl = _mdy(dt.date.fromisoformat(d))
        for m, v in zip(M.MONTHS, tcd):
            if v is not None and not pd.isna(v):
                rows.append({"Month": m, "Carry": float(v), "Series": dl})

    if not rows:
        st.info("No carry data for this selection.")
        return
    df = pd.DataFrame(rows)
    multi = len(cmp_dates) > 0
    title = f"Cash Fwd Curve {CHART_LABEL[commodity]} (Basis Spot Futures): {loc}"

    x = alt.X("Month:N", sort=months, title=None,
              axis=alt.Axis(labelColor="#1f4e79", labelFontWeight="bold",
                            labelFontSize=12, labelAngle=0))
    # Auto-fit the Y domain to the data (don't force zero) so the curve fills the
    # chart instead of hugging a compressed band.
    y = alt.Y("Carry:Q", title=None, scale=alt.Scale(zero=False, nice=True),
              axis=alt.Axis(format=".2f"))

    if not multi:
        # Single curve: keep the original clean styling with value labels.
        base = alt.Chart(df).encode(x=x, y=y)
        line = base.mark_line(color="#1f4e79", strokeWidth=3,
                              point=alt.OverlayMarkDef(color="#1f4e79", size=45))
        labels = base.mark_text(dy=-13, color="#c00000", fontWeight="bold",
                                fontSize=12).encode(
            text=alt.Text("Carry:Q", format=".2f"))
        chart = alt.layer(line, labels)
    else:
        # Multiple curves: color by series, emphasize the working line, legend on.
        order = [cur_label] + [s for s in df["Series"].unique() if s != cur_label]
        color = alt.Color("Series:N", sort=order, title="Curve",
                          scale=alt.Scale(scheme="tableau10"))
        size = alt.condition(f"datum.Series === '{cur_label}'",
                             alt.value(3.5), alt.value(2))
        base = alt.Chart(df).encode(
            x=x, y=y, color=color, size=size,
            tooltip=[alt.Tooltip("Series:N"), alt.Tooltip("Month:N"),
                     alt.Tooltip("Carry:Q", format=".2f")])
        chart = base.mark_line(point=alt.OverlayMarkDef(size=35))

    chart = chart.properties(
        height=360, background="transparent",
        padding={"left": 6, "right": 40, "top": 6, "bottom": 6},
        autosize=alt.AutoSizeParams(type="fit", contains="padding"),
        title=alt.TitleParams(title, color="#c00000", fontSize=17,
                              fontWeight="bold", anchor="middle"),
    ).configure_view(strokeWidth=0, fill=None).configure_axis(
        grid=True, gridColor="#e6e6e6", domainColor="#cccccc"
    ).configure_legend(titleColor="#1f4e79", labelColor="#333",
                       labelFontWeight="bold")
    # Watermark sits behind the chart via CSS (see .vega-embed::before); the
    # chart itself stays clean.
    _snap_anchor(f"snap_carry_{commodity}")
    st.altair_chart(chart, use_container_width=True)
    _snap_toolbar(f"snap_carry_{commodity}", title)
    if multi:
        st.caption("Archived curves reuse the current spread structure to anchor "
                   "to spot (spreads aren't stored per date).")


# Marketing-year start month per commodity (corn/soy Sep, wheat Jun).
SEASON_START = {"Corn": 9, "Soybeans": 9, "Wheat": 6}
# Full label->number map (window labels can now be any month as it rolls).
MONTH_NUM = {M._MONTH_LABEL[n]: n for n in range(1, 13)}

# Map any stored month label (abbrev or full, across import eras) -> month #.
_MNUM = {"jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
         "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
         "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
         "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12}


def _month_num(label):
    """Canonical month number for a stored column label, or None for
    spot/half-month/garbage labels (TW, NW, Spot, FH/LH ..., numeric)."""
    s = str(label).strip().lower().rstrip(".")
    if s[:2] in ("fh", "lh") or s in ("tw", "nw", "spot", ""):
        return None
    return _MNUM.get(s)


@st.cache_data(show_spinner=False)
def seasonal_frame(commodity, metric, location, delivery, _sig, region=None,
                   since=None):
    """One row per archived date with the chosen value, a group label, and a
    synthetic 'season_date' for overlay plotting.

    - delivery == "Nearby": front column, grouped by marketing year, mapped onto
      a Sep->Aug (or Jun->May) span.
    - delivery == a month: that delivery column, grouped by the actual delivery
      CONTRACT (e.g. "Dec 2025"), with the x-axis anchored to the delivery month
      so each contract's life overlays continuously — Jan extends past year-end.
    """
    try:
        cif, frt, cal = db.fetch_all(since)
    except TypeError:
        # Deployed db.py hasn't hot-reloaded the `since` param yet (Streamlit
        # reloads app.py but not imported modules) — load all and window here.
        cif, frt, cal = db.fetch_all()
        if since:
            cif = {d: v for d, v in cif.items() if d >= since}
            frt = {d: v for d, v in frt.items() if d >= since}
            cal = {d: v for d, v in cal.items() if d >= since}
    start = SEASON_START[commodity]
    D = MONTH_NUM.get(delivery) if delivery != "Nearby" else None
    rows = []
    for d, by_comm in cif.items():
        cmcif = by_comm.get(commodity)
        if not cmcif:
            continue
        cols = cal.get(d, {}).get(commodity)
        months = [m for m, _ in cols] if cols else list(cmcif.keys())
        # choose the column: nearby = Spot/first real month; else match by month #
        if D is None:
            col = next((k for k in months if str(k).strip().lower() == "spot"), None)
            if col is None:
                col = next((k for k in months if _month_num(k) is not None), None)
        else:
            col = next((k for k in cmcif if _month_num(k) == D), None)
        if col is None:
            continue
        if metric == "CIF NOLA":
            val = cmcif.get(col)
        elif metric == "Freight":
            val = (frt.get(d, {}).get(region) or {}).get(col)  # tariff multiplier
        else:
            grid = M.compute_fob_grid(commodity, cmcif, frt.get(d, {}), [col])
            val = grid.get(location, {}).get(col)
        if val is None:
            continue
        dd = dt.date.fromisoformat(d)
        if D is None:  # Nearby -> marketing year
            sy = dd.year if dd.month >= start else dd.year - 1
            group, sort_key = f"{sy}/{(sy + 1) % 100:02d}", sy
            syn_year = 2001 if dd.month >= start else 2002
            try:
                syn = dt.date(syn_year, dd.month, dd.day)
            except ValueError:
                syn = dt.date(syn_year, dd.month, 28)
        else:  # specific delivery -> follow the contract to its delivery month
            cy = dd.year if D >= dd.month else dd.year + 1
            group, sort_key = f"{delivery} {cy}", cy
            try:
                deliv = dt.date(cy, D, 1)
            except ValueError:
                continue
            syn = dt.date(2002, D, 1) - (deliv - dd)  # anchor delivery at 2002-D
        rows.append({"date": dd, "season_date": syn, "group": group,
                     "value": float(val), "sort": sort_key})
    return pd.DataFrame(rows)


def render_seasonal_tab():
    st.markdown("### 📈 Seasonal — Basis by Marketing Year")
    c1, c2, c3, c4 = st.columns([1, 1, 1.1, 1.1])
    with c1:
        commodity = st.selectbox("Commodity", M.COMMODITIES, key="seasonal_commodity")
    with c2:
        metric = st.radio("Series", ["FOB at location", "CIF NOLA", "Barge Freight"],
                          key="seasonal_metric")
    location = "STL"
    region = "STL"
    with c3:
        if metric == "FOB at location":
            locs = [it[1] for it in M.BLOCK_LAYOUT if it[0] == "fob"]
            location = st.selectbox("Location", locs,
                                    index=locs.index("STL") if "STL" in locs else 0,
                                    key="seasonal_location")
        elif metric == "Barge Freight":
            regs = list(M.FREIGHT_REGIONS)
            region = st.selectbox("Freight region", regs,
                                  index=regs.index("STL") if "STL" in regs else 0,
                                  key="seasonal_region")
        else:
            st.caption("CIF NOLA export basis — no location.")
    with c4:
        delivery = st.selectbox("Delivery", ["Nearby"] + M.MONTHS,
                                key="seasonal_delivery",
                                help="Nearby = front of the curve, or pick a "
                                     "specific delivery month (e.g. Dec).")

    # Default to a recent window (covers the 5 lines + 10-yr band) so the chart
    # doesn't pull the whole ~20-year archive each time. Toggle to load it all
    # for older analog-year lookback.
    full_hist = st.checkbox("Load full history (slower)", value=False,
                            key="seasonal_fullhist",
                            help="By default the chart loads ~13 recent years — "
                                 "enough for the 5-year lines and 10-year band. "
                                 "Enable this to add analog years further back.")
    since = None if full_hist else (
        dt.date.today() - dt.timedelta(days=365 * 13 + 4)).isoformat()

    dates = db.list_dates()
    sig = (len(dates), dates[0] if dates else "", since or "all")
    metric_key = {"CIF NOLA": "CIF NOLA", "Barge Freight": "Freight"}.get(metric, "FOB")
    df = seasonal_frame(commodity, metric_key, location, delivery, sig, region, since)
    if df.empty:
        st.info("No archived data for this selection yet.")
        return

    order = df.drop_duplicates("group").sort_values("sort")["group"].tolist()
    cur_group = order[-1]
    df = df.assign(Current=df["group"] == cur_group)
    start = SEASON_START[commodity]
    if metric == "CIF NOLA":
        label, val_fmt, val_title, unit = "CIF NOLA", ".2f", "Basis", " Basis"
    elif metric == "Barge Freight":
        label, val_fmt, val_title, unit = f"Barge Freight {region}", ".0%", "Freight", ""
    else:
        label, val_fmt, val_title, unit = f"FOB {location}", ".2f", "Basis", " Basis"
    # Barge freight is commodity-agnostic, so its title carries no commodity.
    prefix = "" if metric == "Barge Freight" else f"{CHART_LABEL[commodity]} "
    title = f"{prefix}Seasonal — {delivery} {label}{unit}"
    legend_title = "Mktg Yr" if delivery == "Nearby" else "Contract"

    # Individual lines: default to the 5 most recent marketing years, but let the
    # user add analog years or drop some (newest-first in the picker).
    recent5 = order[-5:]
    sel = st.multiselect(
        "Years shown — add/remove analog years", list(reversed(order)),
        default=list(reversed(recent5)), key=f"seasonal_years_{commodity}",
        help="Defaults to the last 5 marketing years; add older analog years to "
             "compare or remove any you don't want.")
    sel_years = [g for g in order if g in set(sel)] or recent5
    df5 = df[df["group"].isin(sel_years)].copy()

    # Horizontal reference line at the SELECTED location's CBOT delivery-
    # equivalent basis — only when that location is a delivery house (Chicago,
    # Seneca, Hennepin, Peoria, Havana). Non-delivery locations (Burlington,
    # Quincy, STL, …) show nothing. Stored in ¢/bu, so /100 to the chart's $/bu.
    de_all = getattr(M, "DELIVERY_EQUIV", {}).get(commodity, {})
    de = {location: de_all[location]} if location in de_all else {}
    show_de = False
    if metric_key == "FOB" and de:
        show_de = st.checkbox(
            f"Delivery-equivalent level ({location})", value=True,
            key=f"seasonal_de_{commodity}",
            help="Dashed grey line at this delivery house's delivery-equivalent "
                 "basis. Locations that aren't delivery houses show nothing.")

    yfit = st.radio(
        "Best fit", ["Full range", "Central 90%", "Central 75%"], horizontal=True,
        index=1,
        key=f"seasonal_yfit_{commodity}",
        help="Zoom the Y-axis to the central 90% / 75% of the plotted years, "
             "hiding extreme outliers so the typical range fills the chart. "
             "Reference lines and the forward curve stay in view.")

    # Optional overlay (Nearby only): the current forward curve — the latest
    # snapshot's basis for each forward delivery month, on the season axis.
    fwd = pd.DataFrame()
    if delivery == "Nearby":
        show_fwd = st.checkbox(
            f"Overlay current forward curve (as of {dates[0] if dates else '—'})",
            value=True, key=f"seasonal_fwd_{commodity}",
            help="Plots the latest snapshot's basis for each forward delivery "
                 "month (dashed purple) against the seasonal history — where the "
                 "curve is priced now vs. the historical range.")
        if show_fwd and dates:
            lc, lf, lcal = db.load_snapshot(dates[0])
            cols = (lcal or {}).get(commodity) or []
            fmonths = [m for m, _ in cols] if cols else list(M.MONTHS)
            grid = (M.compute_fob_grid(commodity, (lc or {}).get(commodity) or {},
                                       lf or {}, fmonths)
                    if metric_key == "FOB" else {})
            fwd_rows = []
            for i, m in enumerate(fmonths):
                mn = _month_num(m)
                if mn is None:
                    continue
                if metric_key == "CIF NOLA":
                    v = ((lc or {}).get(commodity) or {}).get(m)
                elif metric_key == "Freight":
                    v = ((lf or {}).get(region) or {}).get(m)
                else:
                    v = grid.get(location, {}).get(m)
                if v is None:
                    continue
                syn = dt.date(2001 if mn >= start else 2002, mn, 15)
                fwd_rows.append({"season_date": syn, "value": float(v), "mon": m})
            # Near the marketing-year turn the front month wraps to the far end
            # of the season axis (e.g. an Aug snapshot: Aug at the right, Sep+ at
            # the left). Drop those leading wrapped months so the forward curve
            # reads as a clean left-to-right line over the deferred months.
            while len(fwd_rows) > 1 and fwd_rows[0]["season_date"] > fwd_rows[1]["season_date"]:
                fwd_rows.pop(0)
            fwd = (pd.DataFrame(fwd_rows).sort_values("season_date")
                   if fwd_rows else pd.DataFrame())

    # 10-year range band + average from the last 10 COMPLETED years (exclude the
    # current partial year), binned by season week.
    completed10 = order[:-1][-10:]
    n10 = len(completed10)
    band = avg = pd.DataFrame()
    if completed10:
        hist = df[df["group"].isin(completed10)].copy()
        hist["wk"] = hist["season_date"].map(
            lambda d: d.isocalendar()[0] * 100 + d.isocalendar()[1])
        g = hist.groupby("wk")
        band = (g.agg(lo=("value", "min"), hi=("value", "max"),
                      season_date=("season_date", "min"))
                .reset_index().sort_values("season_date"))
        avg = (g.agg(value=("value", "mean"), season_date=("season_date", "min"))
               .reset_index().sort_values("season_date"))

    # Best-fit Y domain: clip to the central percentile band of the plotted years
    # so outliers don't squash the chart. Reference lines + forward curve are
    # unioned in so they stay visible.
    if yfit == "Full range":
        yscale = alt.Scale(zero=False, nice=True)
    else:
        _v = df5["value"].dropna()
        _q = 0.05 if yfit == "Central 90%" else 0.125
        if len(_v) >= 8:
            _lo, _hi = float(_v.quantile(_q)), float(_v.quantile(1 - _q))
            _keep = [v / 100.0 for v in de.values() if v is not None] if show_de else []
            if not fwd.empty:
                _keep += [float(x) for x in fwd["value"].dropna()]
            if _keep:
                _lo, _hi = min(_lo, min(_keep)), max(_hi, max(_keep))
            _pad = (_hi - _lo) * 0.06 or 0.05
            yscale = alt.Scale(domain=[_lo - _pad, _hi + _pad], clamp=True, nice=False)
        else:
            yscale = alt.Scale(zero=False, nice=True)

    yaxis = alt.Axis(format=val_fmt, labelColor="#1f4e79", labelFontWeight="bold",
                     labelFontSize=12)
    xaxis = alt.Axis(format="%b", tickCount="month", labelColor="#1f4e79",
                     labelFontWeight="bold")

    # Colour scale: current marketing year = bold dark green; analog years use
    # distinct non-green hues so green reads as "current".
    _palette = ["#c0504d", "#e8871a", "#4472c4", "#7030a0", "#948a54",
                "#31859c", "#8c564b", "#5b5b5b", "#c00000"]
    dom, rng, _pi = [], [], 0
    for grp in sel_years:
        dom.append(grp)
        if grp == cur_group:
            rng.append("#166b34")            # dark green = current year
        else:
            rng.append(_palette[_pi % len(_palette)])
            _pi += 1

    layers = []
    if not band.empty:
        band_area = alt.Chart(band.assign(lbl=f"{n10}-Yr Range")).mark_area(
            color="#b7c7db", opacity=0.45, clip=True).encode(
            x=alt.X("season_date:T", title=None, axis=xaxis),
            y=alt.Y("lo:Q", title=None, axis=yaxis, scale=yscale), y2="hi:Q",
            tooltip=[alt.Tooltip("lbl:N", title="Band"),
                     alt.Tooltip("lo:Q", format=val_fmt, title="Min"),
                     alt.Tooltip("hi:Q", format=val_fmt, title="Max")])
        layers.append(band_area)

    # Emphasis: keep the current + last marketing year bold and opaque; fade the
    # other analog years to faint background context (the average and forward
    # curve are their own bold layers). Two layers so the faint set can't muddy
    # the prominent ones.
    last_group = order[-2] if len(order) >= 2 else None
    prom_set = {g for g in (cur_group, last_group) if g is not None}
    color_enc = alt.Color(
        "group:N", sort=sel_years, scale=alt.Scale(domain=dom, range=rng),
        legend=alt.Legend(title=legend_title, symbolOpacity=1,
                          symbolStrokeWidth=3))
    line_tt = [alt.Tooltip("group:N", title=legend_title),
               alt.Tooltip("date:T", title="Date"),
               alt.Tooltip("value:Q", format=val_fmt, title=val_title)]
    df_faint = df5[~df5["group"].isin(prom_set)]
    if not df_faint.empty:
        layers.append(alt.Chart(df_faint).mark_line(
            point=False, strokeWidth=1.3, opacity=0.16, clip=True).encode(
            x=alt.X("season_date:T", title=None, axis=xaxis),
            y=alt.Y("value:Q", title=None, axis=yaxis, scale=yscale),
            color=color_enc, tooltip=line_tt))
    df_prom = df5[df5["group"].isin(prom_set)]
    if not df_prom.empty:
        layers.append(alt.Chart(df_prom).mark_line(
            point=False, opacity=0.95, clip=True).encode(
            x=alt.X("season_date:T", title=None, axis=xaxis),
            y=alt.Y("value:Q", title=None, axis=yaxis, scale=yscale),
            color=color_enc,
            size=alt.condition("datum.Current", alt.value(4.5), alt.value(3)),
            tooltip=line_tt))

    if not avg.empty:
        avg_line = alt.Chart(avg.assign(lbl=f"{n10}-Yr Avg")).mark_line(
            color="#111111", strokeWidth=3, strokeDash=[7, 4], clip=True).encode(
            x="season_date:T", y=alt.Y("value:Q", scale=yscale),
            tooltip=[alt.Tooltip("lbl:N", title="Series"),
                     alt.Tooltip("value:Q", format=val_fmt, title="Avg")])
        layers.append(avg_line)

    if not fwd.empty:
        fwd_line = alt.Chart(fwd.assign(lbl="Fwd Curve")).mark_line(
            color="#7b2cbf", strokeWidth=4.5, strokeDash=[5, 3], clip=True,
            point=alt.OverlayMarkDef(color="#7b2cbf", size=95, shape="diamond")
        ).encode(
            x="season_date:T", y=alt.Y("value:Q", scale=yscale),
            order=alt.Order("season_date:T"),
            tooltip=[alt.Tooltip("mon:N", title="Fwd month"),
                     alt.Tooltip("value:Q", format=val_fmt, title="Fwd basis")])
        layers.append(fwd_line)

    if show_de:
        _z = getattr(M, "DELIVERY_ZONE", {}).get(location)
        _lab = f"Zone {_z}" if _z else location
        de_df = pd.DataFrame([{"loc": _lab, "lvl": round(v / 100.0, 4)}
                              for v in de.values() if v is not None])
        de_base = alt.Chart(de_df).encode(
            y=alt.Y("lvl:Q", title=None, axis=yaxis, scale=yscale))
        layers.append(de_base.mark_rule(
            color="#8a8a8a", strokeWidth=1, strokeDash=[3, 3], clip=True).encode(
            tooltip=[alt.Tooltip("loc:N", title="Delivery zone"),
                     alt.Tooltip("lvl:Q", format=val_fmt, title="Del equiv")]))
        layers.append(de_base.mark_text(
            align="left", dx=6, dy=-4, fontSize=9, color="#6b6b6b", clip=True).encode(
            x=alt.value(6), text=alt.Text("loc:N")))

    chart = alt.layer(*layers).properties(
        height=400, background="transparent",
        padding={"left": 6, "right": 40, "top": 6, "bottom": 6},
        autosize=alt.AutoSizeParams(type="fit", contains="padding"),
        title=alt.TitleParams(title, color="#c00000", fontSize=17,
                              fontWeight="bold", anchor="middle"),
    ).configure_view(strokeWidth=0, fill=None).configure_axis(
        grid=True, gridColor="#e6e6e6", domainColor="#cccccc"
    ).configure_legend(titleColor="#1f4e79", labelColor="#333", labelFontWeight="bold")
    _snap_anchor("snap_seasonal")
    st.altair_chart(chart, use_container_width=True)
    _snap_toolbar("snap_seasonal", title)
    if delivery == "Nearby":
        basis = (f"Nearby (front of curve) · marketing year starts "
                 f"{'September' if start == 9 else 'June'} 1")
    else:
        basis = (f"{delivery} delivery contract · followed from when it appears "
                 f"until it expires (Jan runs past year-end)")
    st.caption(f"{basis} · current ({cur_group}) and last ({last_group}) year "
               f"bold, other {max(0, len(sel_years) - 2)} year(s) faded for context · "
               f"shaded = {n10}-yr range · black dashed = {n10}-yr avg · "
               f"purple dashed = forward curve.")


def _contract_order(ct):
    """Sort key ordering a contract code within a summer-starting crop window
    (Jul, Aug, Sep, … then Jan, Feb, Mar of the next year)."""
    n = M.CONTRACT_MONTH.get(str(ct)[-1].upper())
    return 99 if n is None else (n if n >= 7 else n + 12)


CASHDEL_PALETTE = ["#a52714", "#e8710a", "#2e8bc0", "#1f5fa8",
                   "#2e7d32", "#7b3fa0", "#b8860b", "#c0392b"]


@st.cache_data(show_spinner=False)
def cashdel_frame(commodity, cash_c, _sig):
    """One row per (archived date, active delivery contract): the Cash-vs-
    Delivery basis (¢/bu) at the commodity's cash location, taken at each
    contract's OWN delivery month (CU→Sep, CZ→Dec, CH→Mar), falling back to the
    first window month that uses the contract when its delivery month isn't in
    the window."""
    cif, frt, cal = db.fetch_all()
    loc = M.CARRY_CONFIG[commodity]["cash_loc"]
    rows = []
    for d, by_comm in cif.items():
        cmcif = by_comm.get(commodity)
        cols = (cal.get(d, {}) or {}).get(commodity)
        if not cmcif or not cols:
            continue
        months = [m for m, _ in cols]
        grid = M.compute_fob_grid(commodity, cmcif, frt.get(d, {}), months)
        if loc not in grid:
            continue
        cvd = dict(zip(months,
                       M.cash_vs_delivery(commodity, grid[loc], cash_c, months)))
        # window month label by calendar month number, so a contract can be read
        # at its delivery month rather than the first column that happens to use it.
        by_mn = {}
        for mm, _ct in cols:
            n = M.label_month_num(mm)
            if n is not None:
                by_mn[n] = mm
        seen = set()
        for m, ct in cols:
            if ct in seen:
                continue
            seen.add(ct)
            deliv_mn = M.CONTRACT_MONTH.get(str(ct)[-1].upper())
            ref_m = by_mn.get(deliv_mn, m)      # contract delivery month, else first-seen
            v = cvd.get(ref_m)
            if v is not None:
                rows.append({"date": dt.date.fromisoformat(d), "contract": ct,
                             "cents": round(float(v) * 100, 1)})
    return pd.DataFrame(rows)


def render_cashdel_tab():
    st.markdown("### 💵 Cash vs Delivery — by delivery month")
    dates = db.list_dates()
    if not dates:
        st.info("No archived data yet.")
        return
    latest = dt.date.fromisoformat(dates[0])
    earliest = dt.date.fromisoformat(dates[-1])
    c1, c2, c3 = st.columns([1, 1, 1])
    with c1:
        commodity = st.selectbox("Commodity", M.COMMODITIES, key="cashdel_commodity")
    with c2:
        weeks = st.slider("Weeks shown", 6, 52, 12, key="cashdel_weeks")
    with c3:
        end = st.date_input("Window ends", value=latest, min_value=earliest,
                            max_value=latest, key="cashdel_end",
                            help="Slide this back to view prior years — the chart "
                                 "shows the chosen number of weeks ending here.")
    cash_c = float(st.session_state[f"cashc_{commodity}"])
    loc = M.CARRY_CONFIG[commodity]["cash_loc"]
    sig = (len(dates), dates[0], round(cash_c, 4))
    df = cashdel_frame(commodity, cash_c, sig)
    if df.empty:
        st.info("No archived data for this selection yet.")
        return

    # Weekly sample (most recent day per ISO week), then keep the last `weeks`
    # weekly points ending at/before the chosen window-end date.
    df = df.assign(wk=df["date"].map(lambda d: d.isocalendar()[:2]))
    weekly = df[df["date"] <= end].groupby("wk")["date"].max().sort_values().tolist()
    keep = weekly[-weeks:]
    if not keep:
        st.info("No data in that window — try an earlier end date.")
        return
    df = df[df["date"].isin(keep)].drop(columns="wk")

    order = sorted(df["contract"].unique(), key=_contract_order)
    color = alt.Color("contract:N", sort=order, title=None,
                      scale=alt.Scale(domain=order,
                                      range=CASHDEL_PALETTE[:len(order)]),
                      legend=alt.Legend(orient="top", labelFontWeight="bold"))
    base = alt.Chart(df).encode(
        x=alt.X("date:T", title=None,
                axis=alt.Axis(format="%-d-%b", labelColor="#333",
                              labelFontWeight="bold", labelAngle=0,
                              tickCount=len(keep))),
        y=alt.Y("cents:Q", title=None,
                axis=alt.Axis(labelColor="#333", labelFontWeight="bold")),
        color=color)
    line = base.mark_line(strokeWidth=2.5,
                          point=alt.OverlayMarkDef(size=38, filled=True))
    labels = base.mark_text(dy=-9, fontSize=10, fontWeight="bold").encode(
        text=alt.Text("cents:Q", format=".0f"))
    yr = (f"{keep[0]:%Y}" if keep[0].year == keep[-1].year
          else f"{keep[0]:%Y}–{keep[-1]:%Y}")
    chart = alt.layer(line, labels).properties(
        height=440, background="transparent",
        padding={"left": 6, "right": 40, "top": 6, "bottom": 6},
        autosize=alt.AutoSizeParams(type="fit", contains="padding"),
        title=alt.TitleParams(
            f"Cash vs. Delivery: {CHART_LABEL[commodity]} ({loc}) · {yr}",
            color="#2e7d32", fontSize=18, fontWeight="bold", anchor="middle")
    ).configure_view(strokeWidth=0, fill=None).configure_axis(
        grid=True, gridColor="#ececec", domainColor="#cccccc"
    ).configure_legend(labelColor="#333", symbolStrokeWidth=3, labelFontSize=12)
    _snap_anchor("snap_cashdel")
    st.altair_chart(chart, use_container_width=True)
    _snap_toolbar("snap_cashdel", f"Cash vs Delivery {commodity} {loc} {yr}")
    st.caption(f"Cash vs Delivery at **{loc}** for each active delivery contract · "
               f"¢/bu · {keep[0]:%b %d, %Y} – {keep[-1]:%b %d, %Y} · weekly · cash "
               f"distance from DVE = {cash_c * 100:.0f}¢ (current value across history).")


_VESSEL_SBY_KEY = {s["key"]: s for s in fob_vessel.SERIES}
_VESSEL_PALETTE = ["#1f5fa8", "#e8710a", "#2e7d32", "#7b3fa0", "#a52714",
                   "#2e8bc0", "#b8860b", "#c0392b"]


_VESSEL_GROUP_ORDER = {"FOB": 0, "CFR China": 1, "Freight": 2}


def _vessel_origin_of(disp):
    """Base origin from a display label ('US Gulf HRW' -> 'US Gulf')."""
    return " ".join(disp.split()[:2]) if disp.startswith("US ") else disp.split()[0]


@st.cache_data(show_spinner=False)
def fob_vessel_frame(_sig):
    """Tidy FOB vessel history -> DataFrame(date, group, commodity, origin, grade,
    disp, metric, value, skey). `disp` = origin plus grade (e.g. 'US Gulf HRW')."""
    try:
        raw = fob_vessel.load_all()
    except Exception:
        return pd.DataFrame()
    rows = []
    for sym, series in raw.items():
        meta = fob_vessel.SYMBOL_META.get(sym)
        if not meta:
            continue
        s, metric = meta
        disp = s["origin"] + (f" {s['label']}" if s["label"] else "")
        for d, v in series.items():
            rows.append({"date": d, "group": s.get("group", "FOB"),
                         "commodity": s["commodity"], "origin": s["origin"],
                         "grade": s["label"], "disp": disp, "metric": metric,
                         "value": v, "skey": s["key"]})
    df = pd.DataFrame(rows)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


def _vessel_line_chart(data, title, unit, o_order, key):
    """Shared line-per-origin chart + snapshot toolbar for the vessel tab.
    Labels the latest point of each line just to its right, and reserves right
    padding (+ fit autosize) so those labels and the axes never clip."""
    dom = sorted(data["disp"].unique(),
                 key=lambda d: (o_order.get(_vessel_origin_of(d), 9), d))
    fmt = ".2f" if unit == "$/mt" else ".0f"
    color = alt.Color("disp:N", title=None, sort=dom,
                      scale=alt.Scale(domain=dom, range=_VESSEL_PALETTE[:len(dom)]),
                      legend=alt.Legend(orient="top", labelFontWeight="bold"))
    x = alt.X("date:T", title=None, axis=alt.Axis(
        format="%b %y", labelColor="#333", labelFontWeight="bold"))
    y = alt.Y("value:Q", title=None, scale=alt.Scale(zero=False, nice=True),
              axis=alt.Axis(labelColor="#333", labelFontWeight="bold"))
    line = alt.Chart(data).mark_line(
        strokeWidth=2.5, point=alt.OverlayMarkDef(size=22, filled=True)).encode(
        x=x, y=y, color=color,
        tooltip=[alt.Tooltip("disp:N", title="Origin"),
                 alt.Tooltip("date:T", title="Date"),
                 alt.Tooltip("value:Q", format=fmt, title=unit)])
    last = data.sort_values("date").groupby("disp", as_index=False).tail(1)
    end = alt.Chart(last).mark_text(
        align="left", dx=7, fontWeight="bold", fontSize=11).encode(
        x=x, y=y, text=alt.Text("value:Q", format=fmt),
        color=alt.Color("disp:N", sort=dom, legend=None,
                        scale=alt.Scale(domain=dom, range=_VESSEL_PALETTE[:len(dom)])))
    chart = alt.layer(line, end).properties(
        height=400, background="transparent",
        padding={"left": 6, "right": 62, "top": 6, "bottom": 6},
        autosize=alt.AutoSizeParams(type="fit", contains="padding"),
        title=alt.TitleParams(title, color="#2e7d32", fontSize=16,
                              fontWeight="bold", anchor="middle")
    ).configure_view(strokeWidth=0, fill=None).configure_axis(
        grid=True, gridColor="#ececec", domainColor="#cccccc"
    ).configure_legend(labelColor="#333", symbolStrokeWidth=3, labelFontSize=12)
    _snap_anchor(key)
    st.altair_chart(chart, use_container_width=True)
    _snap_toolbar(key, title)


def render_fob_vessel_tab():
    """Contain any error so a transient stale-module state (Cloud reloaded app.py
    but kept an older fob_vessel in memory, before a full reboot) shows a notice
    instead of crashing the whole app."""
    try:
        _render_fob_vessel_tab()
    except Exception as e:
        st.markdown("### 🚢 FOB Vessel")
        st.warning("FOB Vessel is temporarily unavailable — if this persists, "
                   "**Reboot app** on Streamlit Cloud (the data module updated "
                   f"and needs a fresh load). [{type(e).__name__}]")


def _render_fob_vessel_tab():
    st.markdown("### 🚢 FOB Vessel — export FOB, CFR China & freight")
    st.caption("Fastmarkets · export FOB (corn/soy/wheat), soybean CFR China, and "
               "ocean freight to NE Asia · US Gulf, PNW, Brazil, Argentina, Ukraine.")
    # Display reads the archive (DB), so it works on Cloud without the Fastmarkets
    # creds — those are only needed by the daily pull (fob_vessel_import.py).
    try:
        dates = fob_vessel.list_dates()
    except Exception:
        dates = []
    if not dates:
        st.info("No FOB Vessel data archived yet — run `python fob_vessel_import.py` "
                "(needs the Fastmarkets FOB_VESSEL_* creds).")
        return
    df = fob_vessel_frame((len(dates), dates[0]))
    if df.empty:
        st.info("No FOB Vessel data yet.")
        return

    c_order = {c: i for i, c in enumerate(fob_vessel.COMMODITIES)}
    o_order = {o: i for i, o in enumerate(fob_vessel.ORIGINS)}
    views = fob_vessel.view_options()
    wins = {"6M": 183, "1Y": 365, "2Y": 730, "5Y": 1825, "Max": 99999}

    # ── Snapshot: latest $/mt + basis per series, with day-over-day change ──
    st.markdown("#### Latest")
    latest = {}
    for (skey, metric), g in df.groupby(["skey", "metric"]):
        g = g.sort_values("date")
        last = g.iloc[-1]
        prev = g.iloc[-2] if len(g) > 1 else None
        chg = (last["value"] - prev["value"]) if prev is not None else None
        latest.setdefault(skey, {})[metric] = (last["value"], chg, last["date"])
    snap = []
    for s in fob_vessel.SERIES:
        d = latest.get(s["key"], {})
        flat = d.get("flat", (None, None, None))
        basis = d.get("basis", (None, None, None))
        asof = flat[2] or basis[2]
        item = "→ NE Asia" if s["group"] == "Freight" else s["commodity"]
        snap.append({
            "Type": s["group"], "Item": item, "Origin": s["origin"],
            "Grade": s["label"], "$/mt": flat[0], "Δ $/mt": flat[1],
            "Basis ¢/bu": basis[0], "Δ basis": basis[1],
            "As of": asof.strftime("%Y-%m-%d") if asof is not None else "—",
            "_g": _VESSEL_GROUP_ORDER.get(s["group"], 9),
            "_c": c_order.get(s["commodity"], 9), "_o": o_order.get(s["origin"], 9)})
    snap = (pd.DataFrame(snap).sort_values(["_g", "_c", "_o", "Grade"])
            .drop(columns=["_g", "_c", "_o"]))
    st.dataframe(snap, use_container_width=True, hide_index=True, column_config={
        "$/mt": st.column_config.NumberColumn(format="%.2f"),
        "Δ $/mt": st.column_config.NumberColumn(format="%+.2f"),
        "Basis ¢/bu": st.column_config.NumberColumn(format="%.0f"),
        "Δ basis": st.column_config.NumberColumn(format="%+.0f")})

    metric = st.radio("Metric", ["$/mt", "Basis (¢/bu)"], index=1, horizontal=True,
                      key="vessel_metric",
                      help="Basis = Fastmarkets premium in ¢/bu over CME (FOB & "
                           "CFR only; freight is $/mt).")
    mkey = "flat" if metric == "$/mt" else "basis"

    # ── Trend: one line per origin for a chosen series ──
    st.markdown("#### Trend")
    t1, t2 = st.columns([1.4, 1])
    with t1:
        vlabel = st.selectbox("Series", [v["label"] for v in views],
                              key="vessel_ts_view")
    with t2:
        win = st.selectbox("Window", list(wins), index=1, key="vessel_ts_window")
    view = next(v for v in views if v["label"] == vlabel)
    use_key = "flat" if view["group"] == "Freight" else mkey
    unit = "$/mt" if use_key == "flat" else "¢/bu basis"
    if view["group"] == "Freight" and mkey == "basis":
        st.caption("Freight has no basis — showing $/mt.")
    since = df["date"].max() - pd.Timedelta(days=wins[win])
    ts = df[(df["metric"] == use_key) & (df["group"] == view["group"])
            & (df["commodity"] == view["commodity"]) & (df["date"] >= since)]
    if ts.empty:
        st.info("No data for that series / metric.")
    else:
        _vessel_line_chart(ts, f"{vlabel} — {unit}", unit, o_order, "snap_vessel_ts")

    # ── Spreads: each origin minus a reference, in $/mt ──
    st.markdown("#### Spreads ($/mt)")
    s1, s2, s3 = st.columns([1.4, 1, 1])
    with s1:
        svlabel = st.selectbox("Series ", [v["label"] for v in views],
                               key="vessel_sp_view")
    sview = next(v for v in views if v["label"] == svlabel)
    fc = df[(df["metric"] == "flat") & (df["group"] == sview["group"])
            & (df["commodity"] == sview["commodity"])]
    disp_avail = sorted(fc["disp"].unique(),
                        key=lambda d: o_order.get(_vessel_origin_of(d), 9))
    if len(disp_avail) < 2:
        st.info("Need at least two origins to show a spread.")
        return
    with s2:
        ref = st.selectbox("Reference", disp_avail,
                           index=next((i for i, d in enumerate(disp_avail)
                                       if d.startswith("US Gulf")), 0),
                           key="vessel_sp_ref")
    with s3:
        swin = st.selectbox("Window ", list(wins), index=1, key="vessel_sp_window")
    ssince = df["date"].max() - pd.Timedelta(days=wins[swin])
    piv = fc[fc["date"] >= ssince].pivot_table(index="date", columns="disp",
                                               values="value", aggfunc="first")
    if ref not in piv.columns:
        st.info("No data for that reference.")
        return
    spread = piv.sub(piv[ref], axis=0).drop(columns=[ref]).reset_index()
    sp = spread.melt(id_vars="date", var_name="disp", value_name="value").dropna()
    if sp.empty:
        st.info("No overlapping data for a spread.")
        return
    sdom = sorted(sp["disp"].unique(),
                  key=lambda d: o_order.get(_vessel_origin_of(d), 9))
    scolor = alt.Color("disp:N", title=None, sort=sdom,
                       scale=alt.Scale(domain=sdom, range=_VESSEL_PALETTE[:len(sdom)]),
                       legend=alt.Legend(orient="top", labelFontWeight="bold"))
    sx = alt.X("date:T", title=None, axis=alt.Axis(
        format="%b %y", labelColor="#333", labelFontWeight="bold"))
    sy = alt.Y("value:Q", title=None, scale=alt.Scale(zero=False, nice=True),
               axis=alt.Axis(labelColor="#333", labelFontWeight="bold"))
    sline = alt.Chart(sp).mark_line(strokeWidth=2.5).encode(
        x=sx, y=sy, color=scolor,
        tooltip=[alt.Tooltip("disp:N", title="vs " + ref),
                 alt.Tooltip("date:T", title="Date"),
                 alt.Tooltip("value:Q", format=".2f", title="Spread $/mt")])
    slast = sp.sort_values("date").groupby("disp", as_index=False).tail(1)
    send = alt.Chart(slast).mark_text(
        align="left", dx=7, fontWeight="bold", fontSize=11).encode(
        x=sx, y=sy, text=alt.Text("value:Q", format=".2f"),
        color=alt.Color("disp:N", sort=sdom, legend=None,
                        scale=alt.Scale(domain=sdom, range=_VESSEL_PALETTE[:len(sdom)])))
    schart = alt.layer(sline, send).properties(
        height=360, background="transparent",
        padding={"left": 6, "right": 62, "top": 6, "bottom": 6},
        autosize=alt.AutoSizeParams(type="fit", contains="padding"),
        title=alt.TitleParams(f"{svlabel} spread vs {ref} ($/mt)",
                              color="#2e7d32", fontSize=16, fontWeight="bold",
                              anchor="middle")
    ).configure_view(strokeWidth=0, fill=None).configure_axis(
        grid=True, gridColor="#ececec", domainColor="#cccccc"
    ).configure_legend(labelColor="#333", symbolStrokeWidth=3, labelFontSize=12)
    _snap_anchor("snap_vessel_spread")
    st.altair_chart(schart, use_container_width=True)
    _snap_toolbar("snap_vessel_spread", f"FOB Vessel spread {svlabel} vs {ref}")
    st.caption("Positive = origin trades over the reference; negative = under it.")


def _dlabel(key):
    """Sort key (100*year + month) back to a label: 202607 -> 'Jul 2026'."""
    k = int(key)
    return DP.label((k // 100, k % 100))


def _decorate(frame):
    """Add canonical delivery month (dkey / deliv) and river segment columns.

    Each provider writes the delivery window differently ("July '26",
    "Dec '26 River Close"), so delivery_period resolves it to a real month,
    falling back to the futures contract when the text omits the year.
    """
    # Tolerate rows without delivery_month (e.g. a cached result from before the
    # column was selected) — canonical() then falls back to the futures contract.
    dm_col = (frame["delivery_month"] if "delivery_month" in frame.columns
              else [""] * len(frame))
    keys, labels = [], []
    for dm, fs in zip(dm_col, frame["futures_symbol"]):
        ym = DP.canonical(dm, fs)
        keys.append(ym[0] * 100 + ym[1] if ym else None)
        labels.append(DP.label(ym) if ym else "")
    frame["dkey"] = keys
    frame["deliv"] = labels
    frame["segment"] = [RS.river_segment(loc) for loc in frame["location"]]


# Bump when the bid queries change shape, so a warm cache can't serve rows
# missing newly-selected columns.
_BIDS_SCHEMA = 2


@st.cache_data(show_spinner=False, ttl=900)
def _bids_current(since_iso, _schema):
    return bids_data.current_bids(since_iso)


@st.cache_data(show_spinner=False, ttl=900)
def _bids_history(grain, since_iso, _schema):
    return bids_data.bid_history(grain, since_iso)


def _exp_month_order(m):
    """Column sort key for the export's wide layout: Spot first, then calendar."""
    if str(m).strip().lower() == "spot":
        return -1
    n = _month_num(m)
    return n if n is not None else 99


@st.cache_data(show_spinner=False)
def export_frame(metric, commodity, location, region, since, to, _sig):
    """Tidy history for the export tab -> DataFrame(date, month[, contract], value)
    for one metric over [since, to]. `since` limits the DB scan; `to` filters."""
    cif, frt, cal = db.fetch_all(since)
    rows = []
    for d in sorted(cif):
        if to and d > to:
            continue
        cmap = dict((cal.get(d, {}) or {}).get(commodity) or []) if commodity else {}
        if metric == "CIF NOLA":
            for m, v in ((cif.get(d, {}) or {}).get(commodity) or {}).items():
                if v is not None:
                    rows.append({"date": d, "month": m,
                                 "contract": cmap.get(m, ""), "value": v})
        elif metric == "Freight":
            for m, v in ((frt.get(d, {}) or {}).get(region) or {}).items():
                if v is not None:
                    rows.append({"date": d, "month": m, "value": v})
        else:  # FOB at a location
            ccif = (cif.get(d, {}) or {}).get(commodity) or {}
            grid = M.compute_fob_grid(commodity, ccif, frt.get(d, {}) or {},
                                      list(ccif.keys()))
            for m, v in (grid.get(location) or {}).items():
                if v is not None:
                    rows.append({"date": d, "month": m,
                                 "contract": cmap.get(m, ""), "value": v})
    return pd.DataFrame(rows)


def render_export_tab():
    """Download archived CIF / freight / FOB history over a date range."""
    st.markdown("### 📤 Export — historical data")
    dates = db.list_dates()
    if not dates:
        st.info("No archived data yet.")
        return
    latest = dt.date.fromisoformat(dates[0])
    earliest = dt.date.fromisoformat(dates[-1])

    c1, c2, c3 = st.columns(3)
    metric = c1.selectbox("Data", ["CIF NOLA", "Barge Freight", "FOB (location)"],
                          key="exp_metric")
    d_from = c2.date_input("From", value=max(earliest, latest - dt.timedelta(days=730)),
                           min_value=earliest, max_value=latest, key="exp_from")
    d_to = c3.date_input("To", value=latest, min_value=earliest, max_value=latest,
                         key="exp_to")

    metric_key = {"CIF NOLA": "CIF NOLA", "Barge Freight": "Freight",
                  "FOB (location)": "FOB"}[metric]
    commodity = location = region = None
    r1, r2, r3 = st.columns(3)
    if metric_key == "Freight":
        region = r1.selectbox("Region", list(M.FREIGHT_REGIONS), key="exp_region")
    else:
        commodity = r1.selectbox("Commodity", M.COMMODITIES, key="exp_commodity")
        if metric_key == "FOB":
            locs = [it[1] for it in M.BLOCK_LAYOUT if it[0] == "fob"]
            location = r2.selectbox("Location", locs,
                                    index=locs.index("STL") if "STL" in locs else 0,
                                    key="exp_loc")
    layout = r3.radio("Layout", ["Wide (dates × months)", "Long (tidy rows)"],
                      key="exp_layout")

    if d_from > d_to:
        st.warning("'From' is after 'To' — adjust the dates.")
        return
    sig = (len(dates), dates[0], metric_key, commodity, location, region,
           d_from.isoformat(), d_to.isoformat())
    df = export_frame(metric_key, commodity, location, region,
                      d_from.isoformat(), d_to.isoformat(), sig)
    if df.empty:
        st.info("No data for that selection and range.")
        return

    month_opts = sorted(df["month"].unique(), key=_exp_month_order)
    picked = st.multiselect("Delivery months (leave empty for all)", month_opts,
                            key="exp_months")
    if picked:
        df = df[df["month"].isin(picked)]
        if df.empty:
            st.info("No data for those delivery months in this range.")
            return

    if layout.startswith("Wide"):
        show = (df.pivot_table(index="date", columns="month", values="value",
                               aggfunc="first")
                .reindex(sorted(df["month"].unique(), key=_exp_month_order), axis=1)
                .reset_index())
    else:
        show = df.sort_values(["date", "month"]).reset_index(drop=True)

    st.caption(f"{len(df)} values · {df['date'].nunique()} dates · "
               f"{df['date'].min()} → {df['date'].max()} · values in "
               f"{'% of tariff' if metric_key == 'Freight' else '$/bu basis'}.")
    st.dataframe(show, use_container_width=True, height=380, hide_index=True)

    who = commodity or region
    base = _safe_filename(f"River {metric_key} {who}"
                          + (f" {location}" if location else "")
                          + f" {d_from:%Y-%m-%d} to {d_to:%Y-%m-%d}")
    dl1, dl2 = st.columns(2)
    dl1.download_button("📥 CSV", data=show.to_csv(index=False).encode("utf-8"),
                        file_name=f"{base}.csv", mime="text/csv",
                        use_container_width=True)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        show.to_excel(xw, index=False, sheet_name="History")
    dl2.download_button(
        "📥 Excel", data=buf.getvalue(), file_name=f"{base}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True)


def render_riverbids_tab():
    """Read-only river-terminal bid views (summary / trend / changes).

    Bids live in the basis tracker's database; this is a SELECT-only view so
    there stays one source of truth. Degrades to a notice if unconfigured.
    """
    st.markdown("### 🛥 River Bids — river terminal basis")
    if not bids_data.configured():
        st.info("River bids aren't configured for this deployment — add the "
                "**BASIS_DATABASE_URL** secret to show them here.")
        return

    today = dt.date.today()
    c1, c2 = st.columns([1, 1])
    with c2:
        days = st.slider("Include bid sheets from the last (days)", 7, 60, 30,
                         key="rb_days")
    try:
        rows = _bids_current((today - dt.timedelta(days=days)).isoformat(),
                             _BIDS_SCHEMA)
    except Exception as e:                      # never break the portal
        st.warning(f"Couldn't load river bids: {e}")
        return
    if not rows:
        st.info("No river-terminal bids in that window.")
        return

    df = pd.DataFrame(rows)
    grains = sorted(df["grain"].unique())
    with c1:
        grain = st.selectbox("Grain", grains,
                             index=grains.index("Corn") if "Corn" in grains else 0,
                             key="rb_grain")
    d = df[df["grain"] == grain].copy()
    if d.empty:
        st.info(f"No river-terminal {grain} bids in that window.")
        return

    # Providers write the delivery window free-form ("Dec '26 River Close"), so
    # normalise it to a real month via delivery_period, using the futures symbol
    # to resolve the year when the text omits it. Group rows by river segment.
    _decorate(d)
    cur_key = today.year * 100 + today.month
    keys = sorted({k for k in d["dkey"].dropna().unique() if k >= cur_key})[:7]
    if not keys:
        keys = sorted(d["dkey"].dropna().unique())[-7:]
    months = [_dlabel(k) for k in keys]
    d = d[d["dkey"].isin(keys)]
    segs = [s for s in RS.SEGMENT_ORDER if s in set(d["segment"])]

    # ── Summary: river segment × delivery month (¢ basis) ───────────────────
    st.markdown("#### Summary — current bids by river segment (¢ basis)")
    seg_piv = (d.pivot_table(index="segment", columns="deliv",
                             values="basis_cents", aggfunc="median")
                 .reindex(index=segs, columns=months))
    st.dataframe(seg_piv.round(1), use_container_width=True)
    st.caption(f"Median basis per segment · {d[['provider','location']].drop_duplicates().shape[0]}"
               f" river terminals · most recent sheet per location · sheets within "
               f"{days} days · source: basis tracker (read-only).")

    with st.expander("Detail — by terminal"):
        loc_piv = (d.pivot_table(index=["segment", "provider", "location"],
                                 columns="deliv", values="basis_cents",
                                 aggfunc="max")
                     .reindex(columns=months))
        loc_piv = loc_piv.reset_index()
        loc_piv["segment"] = pd.Categorical(loc_piv["segment"], categories=segs,
                                            ordered=True)
        loc_piv = loc_piv.sort_values(["segment", "provider", "location"])
        st.dataframe(loc_piv, use_container_width=True, height=420,
                     hide_index=True)

    # ── Trend + changes, from the bid history ───────────────────────────────
    try:
        hist = _bids_history(grain, (today - dt.timedelta(days=120)).isoformat(),
                             _BIDS_SCHEMA)
    except Exception as e:
        st.caption(f"Trend unavailable: {e}")
        return
    if not hist:
        return
    hd = pd.DataFrame(hist)
    _decorate(hd)
    hd["date"] = hd["timestamp"].str[:10]
    hd = hd[hd["dkey"].isin(keys)]
    if hd.empty:
        return

    st.markdown("#### Trend — median river basis by delivery month")
    trend = hd.groupby(["date", "deliv"])["basis_cents"].median().reset_index()
    chart = alt.Chart(trend).mark_line(
        strokeWidth=2.5, point=alt.OverlayMarkDef(size=30, filled=True)).encode(
        x=alt.X("date:T", title=None,
                axis=alt.Axis(format="%-d-%b", labelColor="#333",
                              labelFontWeight="bold", labelAngle=0)),
        y=alt.Y("basis_cents:Q", title=None,
                axis=alt.Axis(labelColor="#333", labelFontWeight="bold")),
        color=alt.Color("deliv:N", title=None, sort=months,
                        scale=alt.Scale(domain=months,
                                        range=CASHDEL_PALETTE[:len(months)]),
                        legend=alt.Legend(orient="top", labelFontWeight="bold")),
    ).properties(
        height=340, background="transparent",
        padding={"left": 6, "right": 40, "top": 6, "bottom": 6},
        autosize=alt.AutoSizeParams(type="fit", contains="padding"),
        title=alt.TitleParams(f"River Terminal Basis: {grain}", color="#2e7d32",
                              fontSize=17, fontWeight="bold", anchor="middle"),
    ).configure_view(strokeWidth=0, fill=None).configure_axis(
        grid=True, gridColor="#ececec", domainColor="#cccccc"
    ).configure_legend(labelColor="#333", symbolStrokeWidth=3)
    st.altair_chart(chart, use_container_width=True)

    # ── Movement for one delivery period: segment median + its terminals ────
    st.markdown("#### Movement by segment — single delivery period")
    mc1, mc2 = st.columns([1, 2])
    with mc1:
        sel = st.selectbox("Delivery period", months, key="rb_period")
    with mc2:
        pick = st.multiselect("Segments", segs, default=segs, key="rb_segs",
                              help="Defaults to every segment; narrow it to "
                                   "focus on a particular reach.")
    sub = hd[(hd["deliv"] == sel) & (hd["segment"].isin(pick))]
    if sub.empty:
        st.info(f"No {sel} history for {grain} in those segments.")
        return

    seg_day = sub.groupby(["date", "segment"])["basis_cents"].median().reset_index()
    loc_day = (sub.groupby(["date", "segment", "provider", "location"])
                  ["basis_cents"].median().reset_index())
    dts = sorted(sub["date"].unique())
    latest = dts[-1]
    L = dt.date.fromisoformat(latest)

    def _baseline(back):
        """Last quoted day on/before `back` days ago — gaps don't skew it."""
        elig = [x for x in dts if x <= (L - dt.timedelta(days=back)).isoformat()]
        return elig[-1] if elig else None

    bases = [(lbl, _baseline(n))
             for lbl, n in (("Day", 1), ("Week", 7), ("Month", 30))]

    def _movement(frame, keys):
        cur = frame[frame["date"] == latest].set_index(keys)["basis_cents"]
        out = pd.DataFrame({"Current ¢": cur})
        for lbl, b in bases:
            if b:
                past = frame[frame["date"] == b].set_index(keys)["basis_cents"]
                out[f"{lbl} Δ"] = cur - past
        return out

    seg_mv = _movement(seg_day, ["segment"])
    loc_mv = _movement(loc_day, ["segment", "provider", "location"])
    val_cols = list(seg_mv.columns)

    # One table: each segment's median, with the terminals it averages listed
    # directly beneath it.
    rows = []
    for s in [x for x in segs if x in pick]:
        kids = (loc_mv[loc_mv.index.get_level_values("segment") == s]
                if len(loc_mv) else loc_mv)
        if s in seg_mv.index:
            r = seg_mv.loc[s]
            rows.append({"Segment": s,
                         "Terminal": f"▸ {s} — segment median ({len(kids)})",
                         **{c: r.get(c) for c in val_cols}})
        for idx, rr in kids.iterrows():
            rows.append({"Segment": "", "Terminal": f"      {idx[1]} · {idx[2]}",
                         **{c: rr.get(c) for c in val_cols}})
    st.dataframe(pd.DataFrame(rows).round(1), use_container_width=True,
                 hide_index=True, height=560)
    note = " · ".join(f"{l.lower()} vs {b}" for l, b in bases if b)
    st.caption(f"**{sel}** · as of {latest}" + (f" · {note}" if note else "")
               + " · each segment row is the median of the terminals listed "
                 "beneath it · Δ in ¢ (positive = basis firmed).")


def _chg_cell(cur, prior, kind):
    """Cell showing the current value plus its signed change, colored by direction."""
    if cur is None or pd.isna(cur):
        return "<td></td>"
    val = f"{cur * 100:.0f}%" if kind == "pct" else f"{cur:.2f}"
    if prior is None or pd.isna(prior):
        return f"<td>{val}</td>"
    d = cur - prior
    if abs(d) < 1e-9:
        return f"<td>{val}</td>"
    cls = "up" if d > 0 else "down"
    delta = f"{d * 100:+.0f}%" if kind == "pct" else f"{d:+.2f}"
    color = "#0d7f3d" if d > 0 else "#c00000"
    return f'<td class="{cls}" style="color: {color};">{val}<span class="chg" style="color: {color};"> {delta}</span></td>'


def _build_daily_changes_df(cur_cif, cur_frt, d_cif, d_frt):
    """Build a DataFrame for daily changes (for PNG export)."""
    rows = []
    for c in M.COMMODITIES:
        row_vals = []
        for m in M.MONTHS:
            cur = cur_cif[c].get(m)
            prior = (d_cif.get(c) or {}).get(m)
            if cur is None:
                row_vals.append("")
            elif prior is None:
                row_vals.append(f"{cur:.2f}")
            else:
                delta = cur - prior
                sign = "+" if delta > 1e-9 else ""
                row_vals.append(f"{cur:.2f}\n{sign}{delta:.2f}")
        rows.append([c, "CIF"] + row_vals)

    for r in M.FREIGHT_REGIONS:
        row_vals = []
        for m in M.MONTHS:
            cur = cur_frt[r].get(m)
            prior = (d_frt.get(r) or {}).get(m)
            if cur is None:
                row_vals.append("")
            elif prior is None:
                row_vals.append(f"{cur*100:.1f}%")
            else:
                delta = cur - prior
                sign = "+" if delta > 1e-9 else ""
                row_vals.append(f"{cur*100:.1f}%\n{sign}{delta*100:.1f}%")
        rows.append([r, "Barge"] + row_vals)

    cols = ["Region/Commodity", "Type"] + M.MONTHS
    return pd.DataFrame(rows, columns=cols)


def _build_weekly_changes_df(cur_cif, cur_frt, w_cif, w_frt):
    """Build a DataFrame for weekly changes (for PNG export)."""
    rows = []

    # STL Freight at the top
    row_vals = []
    for m in M.MONTHS:
        cur = cur_frt["STL"].get(m)
        prior = (w_frt.get("STL") or {}).get(m)
        if cur is None:
            row_vals.append("")
        elif prior is None:
            row_vals.append(f"{cur*100:.1f}%")
        else:
            delta = cur - prior
            sign = "+" if delta > 1e-9 else ""
            row_vals.append(f"{cur*100:.1f}%\n{sign}{delta*100:.1f}%")
    rows.append(["STL Freight", "—"] + row_vals)

    # CIF and FOB by commodity
    for c in M.COMMODITIES:
        cur_fob = M.compute_fob_grid(c, cur_cif[c], cur_frt)["STL"]
        w_fob = (M.compute_fob_grid(c, w_cif.get(c) or {}, w_frt)["STL"]
                 if w_cif.get(c) else {})

        # CIF row
        row_vals = []
        for m in M.MONTHS:
            cur = cur_cif[c].get(m)
            prior = (w_cif.get(c) or {}).get(m)
            if cur is None:
                row_vals.append("")
            elif prior is None:
                row_vals.append(f"{cur:.2f}")
            else:
                delta = cur - prior
                sign = "+" if delta > 1e-9 else ""
                row_vals.append(f"{cur:.2f}\n{sign}{delta:.2f}")
        rows.append([c, "CIF"] + row_vals)

        # FOB row
        row_vals = []
        for m in M.MONTHS:
            cur = cur_fob.get(m)
            prior = w_fob.get(m)
            if cur is None:
                row_vals.append("")
            elif prior is None:
                row_vals.append(f"{cur:.2f}")
            else:
                delta = cur - prior
                sign = "+" if delta > 1e-9 else ""
                row_vals.append(f"{cur:.2f}\n{sign}{delta:.2f}")
        rows.append([c, "FOB"] + row_vals)

    cols = ["Commodity", "Series"] + M.MONTHS
    return pd.DataFrame(rows, columns=cols)


def _safe_filename(name):
    """Strip characters Windows/macOS disallow in filenames (e.g. the ':' in a
    chart title), collapsing them to spaces."""
    return re.sub(r'[\\/:*?"<>|]+', " ", str(name)).strip() or "chart"


def _alt_png(chart, scale=2):
    """Altair chart -> PNG bytes via vl-convert, or None if it isn't available
    (so a missing dependency degrades to no button instead of an error).

    Serialize with json.dumps(..., default=str) rather than chart.to_json():
    the seasonal frames carry synthetic datetime.date values (season_date) that
    to_json() can't serialize ("Object of type date is not JSON serializable"),
    which silently killed the PNG. default=str renders those as ISO strings that
    Vega-Lite parses as temporal."""
    try:
        import json
        import vl_convert as vlc
        spec = json.dumps(chart.to_dict(), default=str)
        return vlc.vegalite_to_png(spec, scale=scale)
    except Exception:
        return None


def _copy_png_button(png_bytes, key, height=46):
    """A '📋 Copy' button that puts the PNG on the clipboard (paste into email /
    Teams). Rendered as a small HTML component because Streamlit has no native
    clipboard-image action; the button self-reports success/failure inline."""
    if not png_bytes:
        return
    b64 = base64.b64encode(png_bytes).decode()
    components.html(
        """
        <button id="cp" style="font:600 14px system-ui,sans-serif;color:#fff;
          background:#0693e3;border:none;border-radius:6px;padding:6px 14px;
          cursor:pointer;width:100%">📋 Copy</button>
        <script>
        const b=document.getElementById("cp");
        // Build the PNG blob SYNCHRONOUSLY from base64 (no await before the
        // clipboard write) — an intervening await drops the user-gesture the
        // Clipboard API requires, which silently fails the copy on a real click.
        function pngBlob(){
          const bin=atob("__B64__"), a=new Uint8Array(bin.length);
          for(let i=0;i<bin.length;i++) a[i]=bin.charCodeAt(i);
          return new Blob([a],{type:"image/png"});
        }
        b.onclick=async()=>{
          try{
            await navigator.clipboard.write([new ClipboardItem({"image/png":pngBlob()})]);
            b.textContent="✓ Copied";
            setTimeout(()=>{b.textContent="📋 Copy";},1500);
          }catch(e){
            b.textContent="⚠ "+(e.name||"blocked");
            setTimeout(()=>{b.textContent="📋 Copy";},2500);
          }
        };
        </script>
        """.replace("__B64__", b64),
        height=height)


_SNAP_JS = """
<div style="display:flex;gap:6px;align-items:center">
  <button id="dl" style="font:600 13px system-ui,sans-serif;color:#fff;
    background:#0693e3;border:none;border-radius:6px;padding:6px 12px;
    cursor:pointer">\U0001F4E5 PNG</button>
  <button id="cp" style="font:600 13px system-ui,sans-serif;color:#0693e3;
    background:#fff;border:1.5px solid #0693e3;border-radius:6px;padding:6px 12px;
    cursor:pointer">\U0001F4CB Copy</button>
  <span id="msg" style="font:12px system-ui,sans-serif;color:#888"></span>
</div>
<script>
const P=window.parent, PD=P.document, TARGET="__ID__", FN="__FN__";
function ensureH2C(){
  if(P.html2canvas) return Promise.resolve();
  if(P.__h2c) return P.__h2c;
  P.__h2c=new Promise((res,rej)=>{
    const s=PD.createElement("script");
    s.src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js";
    s.onload=()=>res(); s.onerror=()=>rej(new Error("load failed"));
    PD.head.appendChild(s); setTimeout(()=>rej(new Error("timeout")),10000);
  });
  return P.__h2c;
}
// Resolve the element to shoot: the tagged wrapper itself if it holds content
// (tables), else the first chart that appears AFTER the anchor in document order
// (a chart can't carry an id; document-order avoids grabbing a table above it).
function target(){
  const el=PD.getElementById(TARGET); if(!el) return null;
  if(el.querySelector("table,canvas,svg")) return el;
  const charts=[...PD.querySelectorAll('[data-testid$="VegaLiteChart"],[data-testid="stVegaLiteChart"]')];
  for(const c of charts){
    if(el.compareDocumentPosition(c) & Node.DOCUMENT_POSITION_FOLLOWING) return c;
  }
  return charts[0]||el;
}
async function shoot(){
  await ensureH2C();
  const el=target(); if(!el) throw new Error("nothing to capture");
  return await P.html2canvas(el,{scale:2,backgroundColor:"#ffffff",logging:false,useCORS:true});
}
const msg=t=>{const m=document.getElementById("msg"); m.textContent=t;
  if(t) setTimeout(()=>{m.textContent="";},2500);};
document.getElementById("dl").onclick=async()=>{
  msg("…"); try{ const c=await shoot(); const a=document.createElement("a");
    a.download=FN+".png"; a.href=c.toDataURL("image/png"); a.click(); msg("✓ saved"); }
  catch(e){ msg("⚠ "+e.message); }
};
// Promise-based ClipboardItem so the async html2canvas work keeps the click's
// user-gesture (a plain await before clipboard.write would drop it).
document.getElementById("cp").onclick=async()=>{
  msg("…");
  try{
    await navigator.clipboard.write([new ClipboardItem({"image/png":(async()=>{
      const c=await shoot();
      return await new Promise(r=>c.toBlob(r,"image/png"));
    })()})]);
    msg("✓ copied");
  }catch(e){ msg("⚠ "+(e.name||e.message)); }
};
</script>
"""


def _snap_anchor(snap_id):
    """Zero-height marker so a following chart can be found by the snapshot tool
    (st.altair_chart can't carry an id of its own)."""
    st.markdown(f'<div id="{snap_id}" style="height:0"></div>',
                unsafe_allow_html=True)


def _snap_toolbar(snap_id, filename, height=44):
    """📥 PNG + 📋 Copy that screenshot the actual on-screen element (styled sheet
    or rendered chart) client-side via html2canvas — a true snapshot, not a
    server-rebuilt image. Main app only. `snap_id` is a wrapper div's id (tables)
    or a _snap_anchor placed just before a chart."""
    if VIEW_ONLY:
        return
    components.html(
        _SNAP_JS.replace("__ID__", snap_id).replace("__FN__", _safe_filename(filename)),
        height=height)


def _png_actions(png, title, key, unavailable_caption=True):
    """Download + Copy pair for a PNG (main app only). Kept side by side."""
    if VIEW_ONLY:
        return
    if not png:
        if unavailable_caption:
            st.caption("⚠ PNG export unavailable in this environment.")
        return
    c1, c2, _ = st.columns([1, 1, 4])
    with c1:
        st.download_button("📥 PNG", data=png, key=key, mime="image/png",
                           file_name=f"{_safe_filename(title)}.png",
                           use_container_width=True)
    with c2:
        _copy_png_button(png, key)


def _chart_download(chart, title, key):
    """Show '📥 PNG' + '📋 Copy' for an Altair chart (main app only, never in the
    read-only client view). Filename is the chart title."""
    if VIEW_ONLY:
        return
    _png_actions(_alt_png(chart), title, key)


def _df_to_png(df, title):
    """Convert DataFrame to PNG using Plotly with JPSI branding."""
    # Alternate row colors for better readability
    cell_colors = []
    for col in df.columns:
        col_colors = []
        for i in range(len(df)):
            if i == 0:
                col_colors.append("#f0f2f5")
            else:
                col_colors.append("#ffffff" if i % 2 == 1 else "#f9f9f9")
        cell_colors.append(col_colors)

    fig = go.Figure(data=[go.Table(
        header=dict(
            values=list(df.columns),
            fill_color="#32373c",
            align="left",
            font=dict(color="white", size=12, family="Arial, sans-serif"),
            height=28,
            line=dict(color="#0693e3", width=2)
        ),
        cells=dict(
            values=[df[col] for col in df.columns],
            fill_color=cell_colors,
            align="left",
            font=dict(size=10, family="Arial, sans-serif", color="#333"),
            height=26,
            line=dict(color="#e0e0e0", width=0.5)
        )
    )])

    fig.update_layout(
        title=dict(
            text=f"<b>{title}</b><br><sub>John Stewart &amp; Associates • River FOB Values</sub>",
            font=dict(size=18, family="Arial, sans-serif", color="#32373c"),
            x=0.5,
            xanchor="center"
        ),
        height=max(450, len(df) * 32 + 140),
        margin=dict(l=30, r=30, t=100, b=30),
        paper_bgcolor="white",
        plot_bgcolor="white"
    )

    try:
        img_bytes = fig.to_image(format="png", scale=2)
        return img_bytes
    except Exception as e:
        return None


@st.cache_data(show_spinner=False)
def _fob_png_from_spec(spec_json, commodity, title):
    """Render a FOB-block PNG from the (already display-formatted) sheet spec.
    Cached on spec_json so kaleido only re-runs when the sheet's numbers change —
    every tab re-executes each rerun, so uncached this would render on every click."""
    spec = json.loads(spec_json)
    months = spec["months"]
    cols = [commodity] + list(months)
    data = []
    for kind, label, cells in spec["rows"]:
        if kind == "months":                      # these ARE the column headers
            continue
        if cells is None:                         # section header row
            data.append([label] + [""] * len(months))
            continue
        texts = []
        for c in cells:
            t = c[0] if isinstance(c, (list, tuple)) else c
            texts.append("" if t is None else str(t))
        texts = (texts + [""] * len(months))[:len(months)]
        data.append([label] + texts)
    return _df_to_png(pd.DataFrame(data, columns=cols), title)


def _fob_block_png(commodity, as_of, hist=None):
    """PNG of a commodity's FOB sheet block, built from the same display spec the
    PDF uses (freight as %, CIF 2dp, FOB 2dp, % Full Carry, …). `hist` is the
    5-tuple (cif, freight, calendar, futures, spreads) for an archived date, or
    None for the live working sheet."""
    try:
        spec = _build_pdf_sheet(commodity, hist)
    except Exception:
        return None
    return _fob_png_from_spec(json.dumps(spec, default=str), commodity,
                              f"{commodity} — River FOB ({as_of:%m/%d/%y})")


def _fob_sheet_actions(commodity, as_of, hist=None):
    """📥 PNG + 📋 Copy for a commodity's FOB sheet block (main app only)."""
    if VIEW_ONLY:
        return
    _png_actions(_fob_block_png(commodity, as_of, hist),
                 f"{commodity} FOB Sheet {as_of:%m-%d-%y}",
                 key=f"fobpng_{commodity}")


BARGE_DASH_URL = "https://agtransport.usda.gov/stories/s/Barge-Dashboard/965a-yzgy/"


@st.cache_data(ttl=6 * 3600, show_spinner="Loading barge-flow history…")
def _barge_flows_df():
    """Full USDA downbound grain barge history (weekly, by lock & commodity).
    Cached 6h — the source updates weekly."""
    return BF.load_flows()


_MONTH_ABBR3 = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def render_barge_flows(allow_download=True):
    """Downbound grain barge flows through the locks: filterable monthly totals
    and a year-over-year seasonal view, from USDA AgTransport."""
    st.markdown("#### 📦 Downbound Grain Barge Flows")
    st.caption("Grain tonnage moving **downbound** past the river locks toward "
               "the Gulf — USDA AgTransport, weekly since 2003. (Grain barge "
               "movement is tracked downbound; northbound is mostly empties.)")
    try:
        df = _barge_flows_df()
    except Exception as e:
        st.warning(f"USDA barge-flow data is unavailable right now ({e}). "
                   "The freight trends above and the USDA dashboard below still "
                   "work.")
        return
    if df is None or df.empty:
        st.info("No barge-flow data available.")
        return

    c1, c2, c3 = st.columns(3)
    comms = c1.multiselect("Commodity", BF.COMMODITIES, default=BF.COMMODITIES,
                           key="bf_comm")
    segs = c2.multiselect("River segment", BF.SEGMENTS, default=BF.SEGMENTS,
                          key="bf_seg")
    lock_opts = [l for l in df["lock"].unique() if BF.RIVER_OF_LOCK.get(l) in segs]
    lock_opts = sorted(lock_opts)
    # Dependency in the key so the lock picker resets cleanly when segments change.
    locks = c3.multiselect("Lock", lock_opts, default=lock_opts,
                           key=f"bf_lock_{'_'.join(sorted(segs)) or 'none'}")
    if not comms or not locks:
        st.info("Pick at least one commodity and one lock.")
        return

    f = df[df["commodity"].isin(comms) & df["lock"].isin(locks)].copy()
    if f.empty:
        st.info("No flows for this selection.")
        return
    f["mt"] = f["tons"] / 1e6  # million short tons, for readable axes

    # --- Summary: YTD vs prior year vs 5-yr avg, through the latest week ---
    latest = f["date"].max()
    cy, cutoff = latest.year, latest.month

    def _ytd(y):
        return f[(f["year"] == y) & (f["month"] <= cutoff)]["tons"].sum()

    cur = _ytd(cy)
    prior = _ytd(cy - 1)
    p5 = [_ytd(cy - k) for k in range(1, 6)]
    avg5 = sum(p5) / len(p5) if p5 else 0.0
    a, b, c = st.columns(3)
    a.metric(f"YTD {cy} (Jan–{latest:%b})", f"{cur / 1e6:,.1f} M tons")
    b.metric(f"vs {cy - 1} YTD", f"{(cur - prior) / 1e6:+,.1f} M",
             f"{(cur / prior - 1) * 100:+.0f}%" if prior else "—")
    c.metric("vs 5-yr avg YTD", f"{(cur - avg5) / 1e6:+,.1f} M",
             f"{(cur / avg5 - 1) * 100:+.0f}%" if avg5 else "—")

    if allow_download:
        _snap_toolbar("snap_bargeflow_month", f"Barge Flows Monthly {latest:%m-%d-%y}")

    # --- Monthly flows: stacked bars by commodity over a chosen window ---
    win = st.radio("Window", ["1 yr", "3 yr", "5 yr", "All"], index=1,
                   horizontal=True, key="bf_window")
    monthly = (f.groupby([pd.Grouper(key="date", freq="MS"), "commodity"])["mt"]
               .sum().reset_index())
    if win != "All":
        yrs = int(win.split()[0])
        monthly = monthly[monthly["date"] >= (latest - pd.DateOffset(years=yrs))]
    bars = (
        alt.Chart(monthly, height=300)
        .mark_bar()
        .encode(
            x=alt.X("yearmonth(date):T", title=None),
            y=alt.Y("sum(mt):Q", title="Million tons",
                    stack=True),
            color=alt.Color("commodity:N", title="Commodity",
                            scale=alt.Scale(domain=BF.COMMODITIES,
                                            range=["#f2b705", "#2e7d32",
                                                   "#8d6e63", "#90a4ae"])),
            tooltip=[alt.Tooltip("yearmonth(date):T", title="Month"),
                     "commodity:N",
                     alt.Tooltip("sum(mt):Q", title="M tons", format=",.2f")],
        )
        .properties(title="Monthly downbound tonnage")
    )
    st.markdown('<div id="snap_bargeflow_month">', unsafe_allow_html=True)
    st.altair_chart(bars, use_container_width=True)
    st.markdown('</div>', unsafe_allow_html=True)

    # --- Seasonal: month-of-year overlays by year + a 5-yr average ---
    if allow_download:
        _snap_toolbar("snap_bargeflow_seasonal",
                      f"Barge Flows Seasonal {latest:%m-%d-%y}")
    seas = f.groupby(["year", "month"])["mt"].sum().reset_index()
    show_years = sorted(seas["year"].unique())[-6:]              # last 6 + current
    if cy not in show_years:
        show_years.append(cy)
    plot = seas[seas["year"].isin(show_years)].copy()
    plot["Month"] = plot["month"].map(lambda m: _MONTH_ABBR3[m])

    # 5-yr average by month over the prior 5 complete years.
    prior_yrs = [cy - k for k in range(1, 6)]
    avg_df = (seas[seas["year"].isin(prior_yrs)]
              .groupby("month")["mt"].mean().reset_index())
    avg_df["Month"] = avg_df["month"].map(lambda m: _MONTH_ABBR3[m])

    month_sort = _MONTH_ABBR3[1:]
    base_x = alt.X("Month:N", sort=month_sort, title=None)
    year_lines = (
        alt.Chart(plot)
        .mark_line(point=True)
        .encode(
            x=base_x,
            y=alt.Y("mt:Q", title="Million tons"),
            color=alt.Color("year:N", title="Year",
                            scale=alt.Scale(scheme="blues")),
            detail="year:N",
            tooltip=["year:N", "Month:N",
                     alt.Tooltip("mt:Q", title="M tons", format=",.2f")],
        )
    )
    avg_line = (
        alt.Chart(avg_df)
        .mark_line(strokeDash=[6, 4], color="#c00000", size=2.5)
        .encode(x=base_x, y="mt:Q",
                tooltip=[alt.Tooltip("mt:Q", title="5-yr avg M tons",
                                     format=",.2f")])
    )
    st.markdown('<div id="snap_bargeflow_seasonal">', unsafe_allow_html=True)
    st.altair_chart((year_lines + avg_line).properties(
        title="Seasonal pattern (dashed red = prior-5-yr average)", height=320),
        use_container_width=True)
    st.markdown('</div>', unsafe_allow_html=True)


def render_barge_dashboard_tab(as_of, cur=None, allow_download=True):
    """JSA barge-freight corridor trends (same look as the Changes tab) above
    USDA AgTransport's Barge Dashboard, embedded for in-app review. If USDA
    blocks framing the embed shows blank, so the direct link is offered too."""
    st.markdown("#### ⚓ Barge Freight Trends")
    _corridor_table_block(as_of, _trend_sides(as_of, cur), "freight",
                          "snap_barge_trends", allow_download, cur is not None)

    st.divider()
    render_barge_flows(allow_download=allow_download)

    st.divider()
    st.markdown("#### 🚢 USDA Barge Dashboard")
    st.markdown(
        '<div class="resource-link">🔗&nbsp;<b>Source:</b> '
        f'<a href="{BARGE_DASH_URL}" target="_blank" rel="noopener">'
        'USDA AgTransport — Barge Dashboard</a>'
        '<span class="rl-sub"> &middot; weekly barge grain movements, tonnage &amp; '
        'freight rates. Open in a new tab if the view below is blank.</span></div>',
        unsafe_allow_html=True)
    components.iframe(BARGE_DASH_URL, height=900, scrolling=True)


# Corridor trends config. Rows are the barge-freight corridors; for the FOB
# views each corridor maps to a representative river delivery point so the
# filter keeps the same rows throughout.
TREND_METRICS = ["Barge Freight", "FOB Corn", "FOB Soybeans"]
N_FORWARD = 2  # forward-month columns shown after Spot
CORRIDOR_REP_FOB = {
    "Lower Miss": "Memphis",
    "Davenport South": "Davenport",
    "McGregor South": "Prairie du Chien",
    "Upper Miss": "Savage",
    "Ohio": "Cincy",
    "STL": "STL",
    "IL": "Peoria",
}


@st.cache_data(ttl=3600, show_spinner=False)
def _corridor_5yr_avg(as_of_iso, kind):
    """Per-corridor average of the 'spot' reading (each snapshot's own front
    month) over the last 5 years, anchored to the same calendar date so the
    average is seasonal. kind: 'freight' | 'Corn' | 'Soybeans'. Cached hourly.
    Returns {region: avg or None}."""
    as_of = dt.date.fromisoformat(as_of_iso)
    parsed = [(str(d)[:10], dt.date.fromisoformat(str(d)[:10])) for d in db.list_dates()]
    acc = {r: [] for r in M.FREIGHT_REGIONS}
    for k in range(1, 6):
        target = dt.date(as_of.year - k, as_of.month, min(as_of.day, 28))
        nearest, best = None, None
        for s, d in parsed:
            diff = abs((d - target).days)
            if best is None or diff < best:
                best, nearest = diff, s
        if nearest is None or best > 45:          # no snapshot near that season
            continue
        cif, frt, cal = db.load_snapshot(nearest)
        hist_months = [m for m, _ in (cal or {}).get("Corn", [])]
        if not hist_months:
            continue
        front = hist_months[0]
        if kind == "freight":
            for region in M.FREIGHT_REGIONS:
                v = ((frt or {}).get(region) or {}).get(front)
                if v is not None:
                    acc[region].append(v)
        else:
            grid = M.compute_fob_grid(kind, (cif or {}).get(kind) or {},
                                      frt or {}, hist_months)
            for region, loc in CORRIDOR_REP_FOB.items():
                v = (grid.get(loc) or {}).get(front)
                if v is not None:
                    acc[region].append(v)
    return {r: (sum(v) / len(v) if v else None) for r, v in acc.items()}


def _trend_sides(as_of, cur):
    """(cur_cif, cur_frt, w_cif, w_frt): the current side (an archived snapshot
    in read-only mode, else the live inputs) plus the ~1-week-ago archived
    snapshot used for the WoW column."""
    if cur is not None:
        cur_cif, cur_frt = cur
    else:
        cur_cif, cur_frt, _ = _current_payloads()
    before = [d for d in sorted(db.list_dates()) if str(d)[:10] < as_of.isoformat()]
    pweek = None
    if before:
        tgt = as_of - dt.timedelta(days=7)
        pweek = min(before, key=lambda d: abs(
            (dt.date.fromisoformat(str(d)[:10]) - tgt).days))
    w_cif, w_frt, _ = db.load_snapshot(pweek) if pweek else ({}, {}, None)
    return cur_cif, cur_frt, (w_cif or {}), (w_frt or {})


def _corridor_table_block(as_of, sides, kind, snap_id, allow_download,
                          cur_is_archived):
    """Render one corridor-trends table for a metric. kind: 'freight' | 'Corn'
    | 'Soybeans'. Shared by the Changes tab (filterable) and the Barge tab
    (freight only)."""
    cur_cif, cur_frt, w_cif, w_frt = sides
    if kind == "freight":
        commodity = None
        title = "Barge Freight Rates — Percentage of Tariff"
    else:
        commodity = kind
        title = f"{commodity} FOB Basis — by Corridor ($/bu)"

    months = M.MONTHS
    spot_m = months[0]
    fwd_ms = list(months[1:1 + N_FORWARD])

    def _m_year(label):
        n = _month_num(label)
        return as_of.year if (n is None or n >= as_of.month) else as_of.year + 1

    cur_grid = wk_grid = None
    if kind != "freight":
        cur_grid = M.compute_fob_grid(commodity, cur_cif.get(commodity) or {}, cur_frt)
        wk_grid = (M.compute_fob_grid(commodity, w_cif.get(commodity) or {}, w_frt)
                   if w_cif.get(commodity) else {})

    def _val(region, m, grid, frt):
        if kind == "freight":
            return (frt.get(region) or {}).get(m)
        return (grid.get(CORRIDOR_REP_FOB[region]) or {}).get(m) if grid else None

    def _lvl(v):
        if v is None or pd.isna(v):
            return "N/A"
        return f"{v * 100:.0f}%" if kind == "freight" else f"{v:.2f}"

    def _chg(d):
        if d is None or pd.isna(d):
            return "", ""
        cls = "pos" if d > 1e-12 else ("neg" if d < -1e-12 else "")
        txt = f"{d * 100:+.0f} pp" if kind == "freight" else f"{d:+.2f}"
        return txt, cls

    avg_map = _corridor_5yr_avg(as_of.isoformat(), kind)
    fwd_hdr = "".join(f"<th>{m} {_m_year(m)}</th>" for m in fwd_ms)
    head = (f'<tr><th class="loch">Corridor</th>'
            f'<th>Spot ({spot_m} {_m_year(spot_m)})</th>'
            f'<th>Change (WoW)</th>{fwd_hdr}'
            f'<th>5-Yr Avg</th><th>vs 5-Yr Avg</th></tr>')

    body = []
    for region in M.FREIGHT_REGIONS:
        label = region if kind == "freight" else f"{region} · {CORRIDOR_REP_FOB[region]}"
        spot = _val(region, spot_m, cur_grid, cur_frt)
        wk = _val(region, spot_m, wk_grid, w_frt)
        wow = (spot - wk) if (spot is not None and wk is not None
                              and not pd.isna(spot) and not pd.isna(wk)) else None
        wow_txt, wow_cls = _chg(wow)
        fwd_cells = "".join(f"<td>{_lvl(_val(region, m, cur_grid, cur_frt))}</td>"
                            for m in fwd_ms)
        avg = avg_map.get(region)
        vs = (spot - avg) if (spot is not None and avg is not None
                              and not pd.isna(spot)) else None
        vs_txt, vs_cls = _chg(vs)
        body.append(
            f'<tr><td class="loc">{label}</td>'
            f'<td>{_lvl(spot)}</td>'
            f'<td class="{wow_cls}">{wow_txt}</td>'
            f'{fwd_cells}'
            f'<td>{_lvl(avg)}</td>'
            f'<td class="{vs_cls}">{vs_txt}</td></tr>')

    src = "selected date" if cur_is_archived else "working sheet"
    if allow_download:
        _snap_toolbar(snap_id, f"{title.split(' —')[0]} {as_of:%m-%d-%y}")
    st.markdown(
        f'<div id="{snap_id}" class="trend-wrap">'
        f'<div class="trend-title">{title}</div>'
        f'<table class="trend-tbl">{head}{"".join(body)}</table></div>'
        f'<div class="trend-foot">Source: JSA River FOB archive ({src}) '
        f'&middot; 5-Yr Avg = same-season spot over the prior 5 years '
        f'&middot; as of {as_of:%Y-%m-%d}</div>',
        unsafe_allow_html=True)


def render_changes_tab(as_of, cur=None, allow_download=True):
    """The original Daily and Weekly change boards, followed by the filterable
    corridor-trends table (Spot / WoW / forward months / 5-yr avg)."""
    # Shared data: current side (archived snapshot in read-only mode, else live
    # inputs), plus the prior-day and ~1-week-ago archived snapshots.
    if cur is not None:
        cur_cif, cur_frt = cur
    else:
        cur_cif, cur_frt, _ = _current_payloads()
    cur_lbl = "selected date" if cur is not None else "working"
    before = [d for d in sorted(db.list_dates()) if str(d)[:10] < as_of.isoformat()]
    pdaily = before[-1] if before else None
    pweek = None
    if before:
        tgt = as_of - dt.timedelta(days=7)
        pweek = min(before, key=lambda d: abs(
            (dt.date.fromisoformat(str(d)[:10]) - tgt).days))
    d_cif, d_frt, _ = db.load_snapshot(pdaily) if pdaily else (None, None, None)
    w_cif, w_frt, _ = db.load_snapshot(pweek) if pweek else (None, None, None)
    d_cif, d_frt = d_cif or {}, d_frt or {}
    w_cif, w_frt = w_cif or {}, w_frt or {}
    ncol = len(M.MONTHS) + 1
    banner = "background:linear-gradient(135deg,#0693e3,#32373c)"

    def hdr(title):
        return (f'<tr><td class="cmdty" colspan="{ncol}" style="{banner}">{title}'
                f'</td></tr><tr class="hdr months"><td class="lbl"></td>'
                + "".join(f"<td>{m}</td>" for m in M.MONTHS) + "</tr>")

    # --- Daily: CIF + barge freight, vs prior day ---
    st.markdown("#### Daily Changes")
    if allow_download:
        _snap_toolbar("snap_daily_chg", f"Daily CIF Changes {as_of:%m-%d-%y}")

    rows = [hdr("Daily Changes")]
    rows.append(f'<tr class="section"><td colspan="{ncol}">CIF</td></tr>')
    for c in M.COMMODITIES:
        cells = "".join(_chg_cell((cur_cif.get(c) or {}).get(m),
                                  (d_cif.get(c) or {}).get(m), "num")
                        for m in M.MONTHS)
        rows.append(f'<tr class="strong"><td class="lbl">{c}</td>{cells}</tr>')
    rows.append(f'<tr class="section"><td colspan="{ncol}">Barge Freight</td></tr>')
    for r in M.FREIGHT_REGIONS:
        cells = "".join(_chg_cell((cur_frt.get(r) or {}).get(m),
                                  (d_frt.get(r) or {}).get(m), "pct")
                        for m in M.MONTHS)
        rows.append(f'<tr class="frt-row"><td class="lbl">{r}</td>{cells}</tr>')
    st.markdown(f'<div id="snap_daily_chg" class="sheet-wrap">'
                f'<table class="sheet">{"".join(rows)}</table></div>',
                unsafe_allow_html=True)
    st.caption(f"Day-over-day: {cur_lbl} values vs prior archived date "
               f"({pdaily or 'none'}).")

    # --- Weekly: CIF / STL freight / STL FOB per commodity, vs ~1 week ago ---
    st.markdown("#### Weekly Changes")
    if allow_download:
        _snap_toolbar("snap_weekly_chg", f"Weekly CIF Changes {as_of:%m-%d-%y}")

    rows = [hdr("Weekly Changes")]
    rows.append(f'<tr class="section"><td colspan="{ncol}">STL Freight</td></tr>')
    cells = "".join(_chg_cell((cur_frt.get("STL") or {}).get(m),
                              (w_frt.get("STL") or {}).get(m), "pct")
                    for m in M.MONTHS)
    rows.append(f'<tr class="frt-row"><td class="lbl">—</td>{cells}</tr>')
    for c in M.COMMODITIES:
        rows.append(f'<tr class="section"><td colspan="{ncol}">{c}</td></tr>')
        cur_fob = M.compute_fob_grid(c, cur_cif.get(c) or {}, cur_frt)["STL"]
        w_fob = (M.compute_fob_grid(c, w_cif.get(c) or {}, w_frt)["STL"]
                 if w_cif.get(c) else {})
        cells = "".join(_chg_cell((cur_cif.get(c) or {}).get(m),
                                  (w_cif.get(c) or {}).get(m), "num")
                        for m in M.MONTHS)
        rows.append(f'<tr class="strong"><td class="lbl">CIF</td>{cells}</tr>')
        cells = "".join(_chg_cell(cur_fob.get(m), w_fob.get(m), "num")
                        for m in M.MONTHS)
        rows.append(f'<tr class="strong"><td class="lbl">FOB</td>{cells}</tr>')
    st.markdown(f'<div id="snap_weekly_chg" class="sheet-wrap">'
                f'<table class="sheet">{"".join(rows)}</table></div>',
                unsafe_allow_html=True)
    st.caption(f"Week-over-week: {cur_lbl} values vs ~7 days ago "
               f"({pweek or 'none'}).")

    # --- Corridor trends table (filterable) at the bottom ---
    st.divider()
    st.markdown("#### Corridor Trends")
    metric = st.radio("View", TREND_METRICS, horizontal=True, key="trend_metric",
                      help="Switch the table between barge freight and corn / "
                           "soybean FOB — same corridors throughout.")
    kind = ("freight" if metric == "Barge Freight"
            else "Corn" if metric == "FOB Corn" else "Soybeans")
    _corridor_table_block(as_of, (cur_cif, cur_frt, w_cif, w_frt), kind,
                          "snap_trends", allow_download, cur is not None)


def load_prior(commodity, as_of_iso, cash_c):
    """Comparison values from the most recent archived date before as_of_iso."""
    pdate = next((d for d in db.list_dates() if d < as_of_iso), None)
    if not pdate:
        return None
    cif, frt, cal = db.load_snapshot(pdate)
    if cif is None:
        return None
    cols = (cal or {}).get(commodity)
    pmonths = [m for m, _ in cols] if cols else M.MONTHS
    cifc = cif.get(commodity, {}) or {}
    grid = M.compute_fob_grid(commodity, cifc, frt, pmonths)
    cfg = M.CARRY_CONFIG[commodity]
    cashvals = M.cash_vs_delivery(commodity, grid[cfg["cash_loc"]], cash_c, pmonths)
    return {"cif": cifc, "freight": frt, "grid": grid,
            "cash": dict(zip(pmonths, cashvals))}


# --- input workflow (Inputs tab) ------------------------------------------
def _current_payloads():
    cif = {c: {m: _safe(st.session_state[f"cif_{c}"].loc[m, "CIF"]) for m in M.MONTHS}
           for c in M.COMMODITIES}
    frt = {r: {m: _safe(st.session_state.freight.loc[r, m]) for m in M.MONTHS}
           for r in M.FREIGHT_REGIONS}
    cal = {c: list(zip(M.MONTHS, M.CONTRACTS[c])) for c in M.COMMODITIES}
    return cif, frt, cal


def _live_spreads(commodity):
    """Inter-contract spreads for the live sheet, derived straight from the CBOT
    futures row (spread = front price − next price). Falls back to the manual
    carry-editor value only where a futures leg is missing — so simply entering
    or pasting the CBOT curve drives the Spreads / % Full Carry / Top Carry."""
    fut_row = {m: _safe(st.session_state[f"cif_{commodity}"].loc[m, "Futures"])
               for m in M.MONTHS}
    labels = M.spread_labels_for(commodity)
    # getattr guard: survive a stale-module reload where spreads_from_futures
    # isn't present yet (falls back to the manual carry values).
    _sff = getattr(M, "spreads_from_futures", None)
    derived = _sff(commodity, fut_row) if _sff else []
    cdf = st.session_state[f"carry_{commodity}"]
    out = []
    for i, l in enumerate(labels):
        dv = derived[i] if i < len(derived) else None
        if dv is not None:
            out.append(dv)
        elif l in cdf.columns:
            out.append(_safe(cdf.loc["Spread", l]))
        else:
            out.append(None)
    return out


def _current_extras():
    """CBOT futures + inter-contract spreads, for archiving alongside inputs."""
    fut = {c: {m: _safe(st.session_state[f"cif_{c}"].loc[m, "Futures"])
               for m in M.MONTHS}
           for c in M.COMMODITIES}
    spr = {c: list(zip(M.spread_labels_for(c), _live_spreads(c)))
           for c in M.COMMODITIES}
    return fut, spr


def save_current(as_of):
    cif, frt, cal = _current_payloads()
    fut, spr = _current_extras()
    return db.save_snapshot(as_of.isoformat(), cif, frt, cal,
                            futures=fut, spreads=spr)


def _close(a, b):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) < 1e-9


def saved_status(as_of):
    """('none'|'insync'|'dirty') comparing current inputs to the saved snapshot."""
    scif, sfrt, _ = db.load_snapshot(as_of.isoformat())
    if scif is None:
        return "none"
    cif, frt, _ = _current_payloads()
    for c in M.COMMODITIES:
        for m in M.MONTHS:
            if not _close(cif[c][m], (scif.get(c) or {}).get(m)):
                return "dirty"
    for r in M.FREIGHT_REGIONS:
        for m in M.MONTHS:
            if not _close(frt[r][m], (sfrt.get(r) or {}).get(m)):
                return "dirty"
    return "insync"


def load_into_inputs(date_iso):
    """Pull a saved date's CIF + freight (+ stored CBOT futures and spreads, if
    any) into the editable input state, so re-saving preserves them."""
    lc, lf, lcal = db.load_snapshot(date_iso)
    if lc is None:
        return False
    lfut, lspr = db.load_extras(date_iso)
    for c in M.COMMODITIES:
        cur = st.session_state[f"cif_{c}"]
        for m in M.MONTHS:
            v = (lc.get(c) or {}).get(m)
            if v is not None:
                cur.loc[m, "CIF"] = v
            fv = (lfut.get(c) or {}).get(m)
            if fv is not None:
                cur.loc[m, "Futures"] = fv
        st.session_state[f"cif_{c}"] = cur
        # Restore stored spreads into the carry editor (labels may differ).
        pairs = (lspr.get(c) or [])
        if pairs:
            st.session_state[f"carry_{c}"] = pd.DataFrame(
                {lbl: [v] for lbl, v in pairs}, index=["Spread"])
    fdf = st.session_state.freight
    for r in M.FREIGHT_REGIONS:
        for m in M.MONTHS:
            v = (lf.get(r) or {}).get(m)
            if v is not None:
                fdf.loc[r, m] = v
    st.session_state.freight = fdf
    _bump_editors()
    return True


def apply_pasted_tables(cif_text, frt_text, fut_text=""):
    """Fill the input editors from pasted CIF / freight / futures. -> (msgs, errs)."""
    msgs, errs = [], []
    if cif_text.strip():
        res, err = paste_parse.parse_cif(cif_text)
        if err:
            errs.append("CIF: " + err)
        else:
            n = 0
            for commodity, mv in res["cif"].items():
                if commodity not in M.COMMODITIES:
                    continue
                cur = st.session_state[f"cif_{commodity}"]
                for m, v in mv.items():
                    if m in M.MONTHS:
                        cur.loc[m, "CIF"] = v
                        n += 1
                st.session_state[f"cif_{commodity}"] = cur
                cons = res["contracts"].get(commodity, {})
                if cons:
                    pre = {"Corn": "C", "Soybeans": "S", "Wheat": "W"}[commodity]
                    st.session_state[f"contracts_{commodity}"] = [
                        pre + cons[m] if m in cons else M.CONTRACTS[commodity][i]
                        for i, m in enumerate(M.MONTHS)]
            msgs.append(f"CIF — filled {n} values across {len(res['cif'])} commodities.")
    if frt_text.strip():
        res, err = paste_parse.parse_freight(frt_text)
        if err:
            errs.append("Freight: " + err)
        else:
            fdf = st.session_state.freight
            n = 0
            for region, mv in res["freight"].items():
                if region not in M.FREIGHT_REGIONS:
                    continue
                for m, v in mv.items():
                    if m in M.MONTHS:
                        fdf.loc[region, m] = v
                        n += 1
            st.session_state.freight = fdf
            msgs.append(f"Freight — filled {n} values across {len(res['freight'])} reaches.")
            if res.get("date"):
                # Don't move the As-of date — barge-freight tables are usually
                # dated the prior session, which kept bumping Save back a day.
                # Just surface it so the user can set the date if they want.
                msgs.append(f"(Freight table is dated {res['date']:%m/%d/%Y} — "
                            "As-of date left unchanged; set it in the sidebar "
                            "if you want to save under that date.)")
    if fut_text.strip():
        res, err = paste_parse.parse_futures(fut_text)
        if err:
            errs.append("Futures: " + err)
        else:
            nf = ns = 0
            skipped = []
            for commodity, lp in res["futures"].items():
                if commodity not in M.COMMODITIES:
                    continue
                # Reject an implausible futures set — a disconnected Barchart
                # add-in pastes junk (e.g. 25/26 for every contract). Grain
                # prices live in ~$1.50–$20/bu; anything outside is not real.
                pvals = [v for v in lp.values() if v is not None]
                if pvals and not all(1.5 <= v <= 20 for v in pvals):
                    skipped.append(commodity)
                    continue
                active = (st.session_state.get(f"contracts_{commodity}")
                          or list(M.CONTRACTS[commodity]))
                cur = st.session_state[f"cif_{commodity}"]
                for i, mth in enumerate(M.MONTHS):
                    letter = active[i][-1]
                    if letter in lp:
                        cur.loc[mth, "Futures"] = lp[letter]
                        nf += 1
                st.session_state[f"cif_{commodity}"] = cur
                # auto-compute spreads for each consecutive distinct-contract pair
                # in the (possibly rolled) chain — labels roll with the front.
                seen = []
                for code in active:
                    if code not in seen:
                        seen.append(code)
                vals = {}
                for j in range(len(seen) - 1):
                    p0, p1 = lp.get(seen[j][-1]), lp.get(seen[j + 1][-1])
                    if p0 is not None and p1 is not None:
                        vals[f"{seen[j]}/{seen[j + 1]}"] = round(p0 - p1, 4)
                if vals:
                    st.session_state[f"carry_{commodity}"] = pd.DataFrame(
                        {lbl: [v] for lbl, v in vals.items()}, index=["Spread"])
                    ns += len(vals)
            if skipped:
                errs.append("Futures looked implausible for " + ", ".join(skipped)
                            + " (values outside $1.50–$20/bu — is the Barchart "
                            "add-in connected?) — left the CBOT row unchanged.")
            msgs.append(f"Futures — filled {nf} CBOT values; computed {ns} spreads.")
    if msgs:
        _bump_editors()
    return msgs, errs


def _pull_massive_futures():
    """Fill the working CBOT row for every commodity from live Massive settlements
    and recompute spreads (same fill path as the futures paste). Returns
    (n_filled, [commodities], error_or_None)."""
    if not massive_futures.configured():
        return 0, [], "MASSIVE_API_KEY is not configured for this deployment."
    filled, done = 0, []
    for commodity in M.COMMODITIES:
        try:
            curve = massive_futures.cbot_curve(commodity)      # {letter: $/bu}
        except Exception as e:
            return filled, done, f"Massive API error: {e}"
        if not curve:
            continue
        active = (st.session_state.get(f"contracts_{commodity}")
                  or list(M.CONTRACTS[commodity]))
        cur = st.session_state[f"cif_{commodity}"]
        for i, mth in enumerate(M.MONTHS):
            if i >= len(active):
                break
            letter = active[i][-1]
            if letter in curve:
                cur.loc[mth, "Futures"] = curve[letter]
                filled += 1
        st.session_state[f"cif_{commodity}"] = cur
        seen = []
        for code in active:
            if code not in seen:
                seen.append(code)
        vals = {}
        for j in range(len(seen) - 1):
            p0, p1 = curve.get(seen[j][-1]), curve.get(seen[j + 1][-1])
            if p0 is not None and p1 is not None:
                vals[f"{seen[j]}/{seen[j + 1]}"] = round(p0 - p1, 4)
        if vals:
            st.session_state[f"carry_{commodity}"] = pd.DataFrame(
                {lbl: [v] for lbl, v in vals.items()}, index=["Spread"])
        done.append(commodity)
    return filled, done, None


def render_inputs_tab(as_of):
    with st.expander("📋 Paste daily tables (CIF & Barge Freight)"):
        pr = st.session_state.pop("paste_result", None)
        if pr:
            for m in pr[0]:
                st.success("✓ " + m)
            for e in pr[1]:
                st.error(e)
        st.caption("Copy each table from your daily source and paste below "
                   "(headers included). MILO, TW and NW rows are ignored; the "
                   "freight date auto-sets the as-of date. Futures come live from "
                   "Massive — use the 🔄 button below.")
        pc1, pc2 = st.columns(2)
        with pc1:
            cif_text = st.text_area("CIF NOLA table", height=220, key="paste_cif")
        with pc2:
            frt_text = st.text_area("Barge Freight table", height=220, key="paste_frt")
        if st.button("⤵ Parse & fill inputs", type="primary"):
            st.session_state["paste_result"] = apply_pasted_tables(cif_text, frt_text)
            st.rerun()

    # Live CBOT futures straight from Massive (no Barchart add-in needed).
    pm = st.session_state.pop("massive_pull_msg", None)
    if pm:
        (st.success if pm[0] == "ok" else st.error)(pm[1])
    if massive_futures.configured():
        if st.button("🔄 Pull live CBOT futures (Massive)",
                     help="Fill the CBOT row for corn/soy/wheat from live Massive "
                          "settlements and recompute spreads — no Barchart add-in "
                          "needed. Save to archive it."):
            with st.spinner("Fetching live CBOT settlements…"):
                n, done, err = _pull_massive_futures()
            st.session_state["massive_pull_msg"] = (
                ("error", f"Massive pull failed: {err}") if err else
                ("ok", f"✓ Filled {n} live CBOT values ({', '.join(done)}). "
                       "Review and Save to archive."))
            if not err:
                _bump_editors()
            st.rerun()

    ver = st.session_state.editor_ver
    status = saved_status(as_of)
    if status == "none":
        st.warning(f"○ Nothing saved for **{as_of:%m/%d/%Y}** yet — this is a "
                   "what-if. Hit **Save to archive** to commit it.")
    elif status == "dirty":
        st.warning(f"● **Unsaved what-if** — inputs differ from the saved "
                   f"{as_of:%m/%d/%Y} snapshot. Save to overwrite, or Revert.")
    else:
        st.success(f"✓ In sync with the saved **{as_of:%m/%d/%Y}** snapshot.")

    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        start = st.selectbox("Start from a saved date", ["—"] + db.list_dates(),
                             key="start_from",
                             help="Copy a prior day's values in as a starting "
                                  "point, then tweak and save under the as-of date.")
    with c2:
        if st.button("📥 Load", use_container_width=True):
            if start != "—" and load_into_inputs(start):
                st.rerun()
    with c3:
        if st.button("↩ Revert", use_container_width=True,
                     disabled=status == "none",
                     help="Discard what-if edits, back to the saved snapshot."):
            if load_into_inputs(as_of.isoformat()):
                st.rerun()

    st.markdown("#### Barge Freight · % of tariff (shared across commodities)")
    fe = st.data_editor(
        st.session_state.freight, use_container_width=True,
        column_config={m: st.column_config.NumberColumn(m, format="%.2f", step=0.05)
                       for m in M.MONTHS},
        key=f"freight_editor_{ver}")
    st.session_state.freight = fe

    st.markdown("#### CIF & Futures by commodity")
    for ct, commodity in zip(st.tabs(M.COMMODITIES), M.COMMODITIES):
        with ct:
            ce = st.data_editor(
                st.session_state[f"cif_{commodity}"].T, use_container_width=True,
                column_config={m: st.column_config.NumberColumn(m, format="%.4f")
                               for m in M.MONTHS},
                key=f"cif_editor_{commodity}_{ver}")
            st.session_state[f"cif_{commodity}"] = ce.T
            st.caption("Spreads auto-derive from the CBOT futures above "
                       "(front price − next price) and drive the Top Carry curve. "
                       "Edit here only to override when a futures leg is missing. "
                       "Full carry is computed from interest + storage.")
            cc = st.data_editor(
                st.session_state[f"carry_{commodity}"], use_container_width=True,
                column_config={lbl: st.column_config.NumberColumn(lbl, format="%.4f")
                               for lbl in M.spread_labels_for(commodity)},
                key=f"carry_editor_{commodity}_{ver}")
            st.session_state[f"carry_{commodity}"] = cc
            a, b = st.columns(2)
            with a:
                st.session_state[f"cashc_{commodity}"] = st.number_input(
                    f"Cash distance from DVE ({M.CARRY_CONFIG[commodity]['cash_loc']})",
                    value=float(st.session_state[f"cashc_{commodity}"]),
                    step=0.01, format="%.2f", key=f"cashc_input_{commodity}_{ver}")
            with b:
                st.session_state[f"storage_{commodity}"] = st.number_input(
                    f"{commodity} storage ($/bu/month)",
                    value=float(st.session_state[f"storage_{commodity}"]),
                    step=0.005, format="%.3f", key=f"storage_input_{commodity}_{ver}",
                    help="Per-commodity; set wheat to its current VSR rate.")

    st.divider()
    s1, s2 = st.columns([1, 3])
    with s1:
        if st.button(f"💾 Save to archive", type="primary",
                     use_container_width=True):
            n_cif, n_frt = save_current(as_of)
            st.success(f"Saved **{as_of:%m/%d/%Y}** — {n_cif} CIF + {n_frt} "
                       "freight values.")
            st.rerun()
    with s2:
        st.caption(f"Writes CIF + barge freight for **{as_of:%m/%d/%Y}** to the "
                   "archive (upsert). Set the as-of date in the sidebar first.")


# --- determine data source: live edit vs archived view --------------------
hist_cif = hist_frt = None
view_date = as_of
hist_cal = None
if HIST_DATE:
    hist_cif, hist_frt, hist_cal = db.load_snapshot(HIST_DATE)
    hist_fut, hist_spr = db.load_extras(HIST_DATE)
    if hist_cif is None:
        st.warning(f"No archived data found for {HIST_DATE}.")
        HIST_DATE = None
    else:
        view_date = dt.date.fromisoformat(HIST_DATE)
        # Ground the month window (and the Changes tab's columns) in the selected
        # date, not today, so an older archived day shows its own months.
        M.MONTHS = M.months_for(view_date)
        M.CONTRACTS = {c: M.contracts_for(c, view_date) for c in M.COMMODITIES}
        _extra = " (incl. CBOT + spreads)" if hist_fut else " (CIF + freight only)"
        st.info(f"📅 Viewing archived snapshot for **{view_date:%A, %B %d, %Y}** — "
                f"read-only · FOB recomputed{_extra}.")

# Where "Save to FOB folder" writes (the SharePoint-synced 2026 folder).
FOB_SAVE_DIR = os.environ.get(
    "FOB_SAVE_DIR",
    r"C:\Users\KoltenPostin\John Stewart and Associates"
    r"\JSA - Documents\St. Louis\JSA FOB Sheet\2026")


# --- sidebar export (defined here so the PDF helpers exist) ----------------
with st.sidebar:
  if not VIEW_ONLY:                       # downloads hidden in read-only mode
    st.divider()
    st.subheader("Export")
    # Export whatever's on screen: the selected archived snapshot, else live.
    if HIST_DATE:
        _exp_date = view_date
        _exp_hist = (hist_cif, hist_frt, hist_cal, hist_fut, hist_spr)
        _note = ("full sheet" if hist_fut else "CIF + freight + FOB; "
                 "no CBOT/spreads — not stored that day")
        st.caption(f"Exporting archived **{view_date:%m/%d/%y}** ({_note}).")
    else:
        _exp_date = as_of
        _exp_hist = None
        st.caption(f"Exporting the working sheet for **{as_of:%m/%d/%y}**.")
    _base = f"JSA FOB Sheet {_exp_date.month}-{_exp_date.day}-{_exp_date.year % 100}"
    _pdf_name, _xlsx_name = _base + ".pdf", _base + ".xlsx"
    try:
        _pdf_bytes = build_fob_pdf(_exp_date, hist=_exp_hist)
        st.download_button(
            "📄 Download FOB Sheet (PDF)", data=_pdf_bytes,
            file_name=_pdf_name, mime="application/pdf",
            use_container_width=True,
            help="One PDF: Corn (p1), Soybeans (p2), Wheat (p3).")
        # "Save to folder" only works when the app runs on a machine that can
        # see the SharePoint-synced folder — hidden on the cloud (Linux) deploy.
        if os.path.isdir(FOB_SAVE_DIR):
            if st.button("💾 Save to FOB 2026 folder", use_container_width=True,
                         help=f"Writes {_pdf_name} to {FOB_SAVE_DIR}"):
                try:
                    _path = os.path.join(FOB_SAVE_DIR, _pdf_name)
                    with open(_path, "wb") as _f:
                        _f.write(_pdf_bytes)
                    st.success(f"Saved to:\n{_path}")
                except OSError as e:
                    st.error(f"Couldn't save to the FOB folder: {e}")
        else:
            st.caption("💡 Run the app locally to enable **Save to FOB folder**; "
                       "on the cloud, use the download button.")
    except Exception as e:  # never let export break the app
        st.caption(f"PDF export unavailable: {e}")

    try:
        st.download_button(
            "📊 Download FOB Sheet (Excel)",
            data=build_fob_xlsx(_exp_date, hist=_exp_hist),
            file_name=_xlsx_name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
            help="One workbook, tab named by date: Corn, Soybeans, Wheat stacked "
                 "with a blank row between each.")
    except Exception as e:
        st.caption(f"Excel export unavailable: {e}")

# --- tabs: Inputs + the three commodity sheets ----------------------------
def _render_archived_commodity(commodity):
    """Read-only sheet for the selected archived date (used by history views)."""
    cols = (hist_cal or {}).get(commodity)
    months = [m for m, _ct in cols] if cols else M.MONTHS
    contracts = ([ct for _m, ct in cols] if cols
                 else list(M.CONTRACTS[commodity]))
    cif_row = (hist_cif or {}).get(commodity) or {}
    fbr = {r: (hist_frt.get(r) or {}) for r in M.FREIGHT_REGIONS}
    cashc = st.session_state[f"cashc_{commodity}"]
    # Stored futures + spreads (empty for days saved before this feature).
    fut_row = (hist_fut or {}).get(commodity) or {}
    spr_pairs = dict((hist_spr or {}).get(commodity) or [])
    h_labels = M.spread_labels_for(commodity, contracts)
    spreads = [spr_pairs.get(l) for l in h_labels]
    fullcarry = (M.compute_full_carry(
        commodity, fut_row, st.session_state.interest_pct / 100.0,
        st.session_state[f"storage_{commodity}"],
        contracts=contracts, months=months) if fut_row else [])
    prior = load_prior(commodity, HIST_DATE, cashc)
    grid = M.compute_fob_grid(commodity, cif_row, fbr, months)
    st.markdown(f'<div id="snap_fob_{commodity}">'
                + render_block(commodity, view_date, cif_row, fut_row, fbr,
                               spreads, fullcarry, cashc, historical=True,
                               contracts=contracts, months=months, prior=prior)
                + '</div>', unsafe_allow_html=True)
    _snap_toolbar(f"snap_fob_{commodity}",
                  f"{commodity} FOB Sheet {view_date:%m-%d-%y}")
    st.markdown("##### 📈 Top of Carry")
    render_carry_chart(commodity, grid, spreads, as_of=view_date, months=months,
                       contracts=contracts, cur_label=f"{view_date:%m/%d/%y}")


if VIEW_ONLY:
    if not HIST_DATE or hist_cif is None:
        st.info("No archived data available to view yet.")
    else:
        tabs = st.tabs(["📊 Changes"] + list(M.COMMODITIES)
                       + ["📈 Seasonal", "💵 Cash vs Del", "🛥 River Bids",
                          "🚢 FOB Vessel", "⚓ Barge Data"])
        with tabs[0]:
            render_changes_tab(view_date, cur=(hist_cif, hist_frt),
                               allow_download=False)
        for tab, commodity in zip(tabs[1:1 + len(M.COMMODITIES)], M.COMMODITIES):
            with tab:
                _render_archived_commodity(commodity)
        with tabs[-5]:
            render_seasonal_tab()
        with tabs[-4]:
            render_cashdel_tab()
        with tabs[-3]:
            render_riverbids_tab()
        with tabs[-2]:
            render_fob_vessel_tab()
        with tabs[-1]:
            render_barge_dashboard_tab(view_date, cur=(hist_cif, hist_frt),
                                       allow_download=False)
elif HIST_DATE:
    tabs = st.tabs(["📊 Changes"] + list(M.COMMODITIES)
                   + ["📈 Seasonal", "💵 Cash vs Del", "🛥 River Bids",
                      "🚢 FOB Vessel", "📤 Export", "⚓ Barge Data"])
    with tabs[0]:
        render_changes_tab(view_date, cur=(hist_cif, hist_frt))
    with tabs[-6]:
        render_seasonal_tab()
    with tabs[-5]:
        render_cashdel_tab()
    with tabs[-4]:
        render_riverbids_tab()
    with tabs[-3]:
        render_fob_vessel_tab()
    with tabs[-2]:
        render_export_tab()
    with tabs[-1]:
        render_barge_dashboard_tab(view_date, cur=(hist_cif, hist_frt))
    for tab, commodity in zip(tabs[1:1 + len(M.COMMODITIES)], M.COMMODITIES):
        with tab:
            _render_archived_commodity(commodity)
else:
    tabs = st.tabs(["📊 Changes", "📝 Inputs"] + M.COMMODITIES
                   + ["📈 Seasonal", "💵 Cash vs Del", "🛥 River Bids",
                      "🚢 FOB Vessel", "📤 Export", "⚓ Barge Data"])
    with tabs[0]:
        render_changes_tab(as_of)
    with tabs[1]:
        render_inputs_tab(as_of)
    with tabs[-6]:
        render_seasonal_tab()
    with tabs[-5]:
        render_cashdel_tab()
    with tabs[-4]:
        render_riverbids_tab()
    with tabs[-3]:
        render_fob_vessel_tab()
    with tabs[-2]:
        render_export_tab()
    with tabs[-1]:
        render_barge_dashboard_tab(as_of)
    for tab, commodity in zip(tabs[2:2 + len(M.COMMODITIES)], M.COMMODITIES):
        with tab:
            df = st.session_state[f"cif_{commodity}"]
            cif_row = {m: df.loc[m, "CIF"] for m in M.MONTHS}
            fut_row = {m: df.loc[m, "Futures"] for m in M.MONTHS}
            fbr = {r: {m: st.session_state.freight.loc[r, m] for m in M.MONTHS}
                   for r in M.FREIGHT_REGIONS}
            spreads = _live_spreads(commodity)   # derived from the CBOT futures
            fullcarry = M.compute_full_carry(
                commodity, fut_row,
                st.session_state.interest_pct / 100.0,
                st.session_state[f"storage_{commodity}"],
            )
            cashc = st.session_state[f"cashc_{commodity}"]
            prior = load_prior(commodity, as_of.isoformat(), cashc)
            st.markdown(f'<div id="snap_fob_{commodity}">'
                        + render_block(commodity, as_of, cif_row, fut_row, fbr,
                                       spreads, fullcarry, cashc, prior=prior,
                                       contracts=st.session_state.get(f"contracts_{commodity}"))
                        + '</div>', unsafe_allow_html=True)
            _snap_toolbar(f"snap_fob_{commodity}",
                          f"{commodity} FOB Sheet {as_of:%m-%d-%y}")
            st.markdown("##### 📈 Top of Carry")
            render_carry_chart(commodity, M.compute_fob_grid(commodity, cif_row, fbr),
                               spreads, as_of=as_of)

st.caption("Mirrors JSA FOB Sheet · FOB = CIF − (tariff factor × freight%) ÷ 2000 × bushel weight")

# Compliance disclaimer — shown at the bottom of every page (all tab branches).
# The copyright year is taken from the calendar, so it rolls over automatically.
_DISCLAIMER = (
    "Trading commodity futures, options on futures, cash commodities, and "
    "over-the-counter derivative products involves substantial risk of loss and "
    "may not be suitable for all investors. This communication is provided for "
    "informational purposes only and does not constitute investment advice, a "
    "recommendation, or an offer or solicitation to buy or sell any futures, "
    "options, cash commodities, or derivative products. John Stewart &amp; "
    "Associates, Inc. does not accept orders to buy or sell any financial "
    "instruments via email. The information contained herein has been obtained "
    "from sources believed to be reliable; however, its accuracy and completeness "
    "are not guaranteed. Any opinions expressed are solely those of the author, "
    "are subject to change without notice, and should not be relied upon as a "
    "basis for investment decisions. Past performance is not indicative of future "
    "results. This message may contain confidential or proprietary information "
    "intended solely for the use of the designated recipient. "
    f"&copy; John Stewart &amp; Associates, Inc. {dt.date.today().year}")
st.markdown(
    '<div style="margin-top:1.5rem;padding-top:0.7rem;border-top:1px solid #e0e0e0;'
    'font-size:0.68rem;line-height:1.4;color:#8a9199;text-align:justify;">'
    f'{_DISCLAIMER}</div>',
    unsafe_allow_html=True)
