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
import os
import io

import altair as alt
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import fob_model as M
import seed_data as S
import db
import paste_parse
import fob_pdf
import fob_excel

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
    for _secret_key in ("DATABASE_URL",):
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
        border-left: 2px solid {JPSI_BLUE}; font-weight: 700; color: #1f2328;
      }}
      .sheet tr.hdr.months td.de-hdr {{
        border-left: 2px solid {JPSI_BLUE};
      }}
      .sheet td.up {{
        background-color: #e8f5e9; color: #0d7f3d; font-weight: 700;
      }}
      .sheet td.down {{
        background-color: #ffebee; color: #c00000; font-weight: 700;
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
        st.session_state.interest_pct = S.SEED_INTEREST_PCT
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
        _dates = db.list_dates()                       # newest first
        st.subheader("History")
        if _dates:
            view_choice = st.selectbox(
                "Viewing date", _dates, index=0,
                help="Browse any archived day. This is a read-only view.")
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
            help="Used for % Full Carry; storage is per-commodity on the Inputs tab.")

        st.divider()
        st.subheader("Archive")
        st.caption(f"CIF + barge freight · {DB_BACKEND}")
        view_choice = st.selectbox(
            "View archived date", ["✏️ Working (live)"] + db.list_dates(),
            help="Pick a saved date to view its FOB sheet read-only. Choose "
                 "'Working (live)' to edit on the Inputs tab.")
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
        months = list(months) + ["Del Equiv"]
        out = []
        for kind, label, cells in rows:
            if cells is None:                                   # full-width section
                out.append((kind, label, cells))
            elif kind == "months":
                out.append((kind, label, list(cells) + [("Del Equiv", False)]))
            elif kind == "fob":
                out.append((kind, label,
                            list(cells) + [_fnum(de.get(label.replace("FOB Barge ", "")), 2)]))
            else:
                out.append((kind, label, list(cells) + [("", False)]))
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
        months = list(months) + ["Del Equiv"]
        out = []
        for kind, label, cells in rows:
            if cells is None:                                   # banner / section
                out.append((kind, label, cells))
            elif kind == "months":
                out.append((kind, label, list(cells) + ["Del Equiv"]))
            elif kind == "fob":
                out.append((kind, label,
                            list(cells) + [_xnum(de.get(label.replace("FOB Barge ", "")))]))
            else:
                out.append((kind, label, list(cells) + [None]))
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


def _data_row(label, vals, fmt, band, lbl_cls="lbl", row_cls="", trail=""):
    cells = "".join(f"<td>{fmt(v)}</td>" for v in vals)
    cls = (" band" if band else "") + (f" {row_cls}" if row_cls else "")
    return (f'<tr class="{cls.strip()}"><td class="{lbl_cls}">{label}</td>'
            f'{cells}{trail}</tr>')


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
    rows.append('<tr class="hdr months"><td class="lbl"></td>' +
                "".join(f"<td>{m}</td>" for m in months) + de_hdr + "</tr>")
    rows.append('<tr class="hdr"><td class="lbl"></td>' +
                "".join(f"<td>{c or ''}</td>" for c in contracts) + de_blank + "</tr>")
    prior = prior or {}
    # CBOT + carry show whenever the data exists — live always, archived only
    # for days saved with futures/spreads (older snapshots stored just CIF/frt).
    show_cbot = bool(fut_row) and any(fut_row.get(m) is not None for m in months)
    show_carry = bool(spreads) and any(s is not None for s in spreads)
    if show_cbot:
        rows.append(_data_row("CBOT", [fut_row.get(m) for m in months],
                              lambda v: _num(v, 4), band=True, trail=de_blank))
    p_cif = prior.get("cif", {})
    cif_cells = "".join(_dir_td(cif_row.get(m), p_cif.get(m), "cif") for m in months)
    rows.append(f'<tr class="strong"><td class="lbl">CIF</td>{cif_cells}{de_blank}</tr>')

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
                f'{cash_cells}{de_blank}</tr>')

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
                    + "".join(scells) + de_blank + "</tr>")

        # % Full Carry sits under each spread's value column.
        carry = M.pct_full_carry(spreads, fullcarry)
        ccells = ["<td></td>"] * len(months)
        for i in range(min(n, len(carry))):
            pos = pad + 2 * i + 1
            if pos < len(ccells):
                ccells[pos] = _carry_pct_cell(carry[i])
        rows.append('<tr class="spread"><td class="lbl">% Full Carry</td>'
                    + "".join(ccells) + de_blank + "</tr>")

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
                        f'<td class="lbl">{label}</td>{cells}{de_blank}</tr>')
        else:  # fob
            loc = item[1]
            pg = p_grid.get(loc, {})
            cells = "".join(_dir_td(grid[loc][m], pg.get(m), "fob") for m in months)
            rows.append(f'<tr class="{"band" if band else ""}">'
                        f'<td class="lbl">FOB Barge {loc}</td>{cells}{_de_cell(loc)}</tr>')
        band = not band

    # Top Carry rows at the bottom (above the chart)
    if show_carry:
        rows.append(f'<tr class="section"><td colspan="{ncol}">Top of Carry</td></tr>')
        for label, loc in cfg["top_carry"]:
            tc = M.top_carry(commodity, grid[loc], spreads,
                             contracts=contracts, months=months)
            rows.append(f'<tr><td class="lbl">{label}</td>'
                        + "".join(_fob_cell(v) for v in tc) + de_blank + "</tr>")

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
    y = alt.Y("Carry:Q", title=None, axis=alt.Axis(format=".2f"))

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
        title=alt.TitleParams(title, color="#c00000", fontSize=17,
                              fontWeight="bold", anchor="middle"),
    ).configure_view(strokeWidth=0, fill=None).configure_axis(
        grid=True, gridColor="#e6e6e6", domainColor="#cccccc"
    ).configure_legend(titleColor="#1f4e79", labelColor="#333",
                       labelFontWeight="bold")
    # Watermark sits behind the chart via CSS (see .vega-embed::before); the
    # chart itself stays clean.
    st.altair_chart(chart, use_container_width=True)
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
def seasonal_frame(commodity, metric, location, delivery, _sig, region=None):
    """One row per archived date with the chosen value, a group label, and a
    synthetic 'season_date' for overlay plotting.

    - delivery == "Nearby": front column, grouped by marketing year, mapped onto
      a Sep->Aug (or Jun->May) span.
    - delivery == a month: that delivery column, grouped by the actual delivery
      CONTRACT (e.g. "Dec 2025"), with the x-axis anchored to the delivery month
      so each contract's life overlays continuously — Jan extends past year-end.
    """
    cif, frt, cal = db.fetch_all()
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

    dates = db.list_dates()
    sig = (len(dates), dates[0] if dates else "")
    metric_key = {"CIF NOLA": "CIF NOLA", "Barge Freight": "Freight"}.get(metric, "FOB")
    df = seasonal_frame(commodity, metric_key, location, delivery, sig, region)
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
    title = f"{CHART_LABEL[commodity]} Seasonal — {delivery} {label}{unit}"
    legend_title = "Mktg Yr" if delivery == "Nearby" else "Contract"

    # 5-year (or fewer) average of completed groups, binned by season week
    completed = order[:-1][-5:]
    avg = pd.DataFrame()
    if completed:
        hist = df[df["group"].isin(completed)].copy()
        hist["wk"] = hist["season_date"].map(
            lambda d: d.isocalendar()[0] * 100 + d.isocalendar()[1])
        avg = (hist.groupby("wk")
               .agg(value=("value", "mean"), season_date=("season_date", "min"))
               .reset_index().sort_values("season_date"))

    hover = alt.selection_point(fields=["group"], on="pointerover", nearest=True,
                                empty=True)
    yaxis = alt.Axis(format=val_fmt, labelColor="#1f4e79", labelFontWeight="bold",
                     labelFontSize=12)
    xaxis = alt.Axis(format="%b", tickCount="month", labelColor="#1f4e79",
                     labelFontWeight="bold")
    year_lines = alt.Chart(df).mark_line(point=False).encode(
        x=alt.X("season_date:T", title=None, axis=xaxis),
        y=alt.Y("value:Q", title=None, axis=yaxis),
        color=alt.Color("group:N", title=legend_title, sort=order,
                        scale=alt.Scale(scheme="tableau10")),
        size=alt.condition("datum.Current", alt.value(4.5), alt.value(2)),
        opacity=alt.condition(hover, alt.value(1.0), alt.value(0.2)),
        tooltip=[alt.Tooltip("group:N", title=legend_title),
                 alt.Tooltip("date:T", title="Date"),
                 alt.Tooltip("value:Q", format=val_fmt, title=val_title)],
    ).add_params(hover)

    layers = [year_lines]
    if not avg.empty:
        avg_line = alt.Chart(avg.assign(lbl=f"{len(completed)}-Yr Avg")).mark_line(
            color="#111111", strokeWidth=3, strokeDash=[7, 4]).encode(
            x="season_date:T", y="value:Q",
            tooltip=[alt.Tooltip("lbl:N", title="Series"),
                     alt.Tooltip("value:Q", format=val_fmt, title="Avg")])
        layers.append(avg_line)

    chart = alt.layer(*layers).properties(
        height=400, background="transparent",
        title=alt.TitleParams(title, color="#c00000", fontSize=17,
                              fontWeight="bold", anchor="middle"),
    ).configure_view(strokeWidth=0, fill=None).configure_axis(
        grid=True, gridColor="#e6e6e6", domainColor="#cccccc"
    ).configure_legend(titleColor="#1f4e79", labelColor="#333", labelFontWeight="bold")
    st.altair_chart(chart, use_container_width=True)
    if delivery == "Nearby":
        basis = (f"Nearby (front of curve) · marketing year starts "
                 f"{'September' if start == 9 else 'June'} 1")
    else:
        basis = (f"{delivery} delivery contract · followed from when it appears "
                 f"until it expires (Jan runs past year-end)")
    st.caption(f"{basis} · current ({cur_group}) drawn heavier · black dashed = "
               f"{len(completed)}-yr avg · hover a line to isolate · "
               f"{len(df)} points / {df['group'].nunique()} contracts.")


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


def render_changes_tab(as_of, cur=None, allow_download=True):
    # cur = (cif, freight) to use as the "current" side (an archived snapshot in
    # read-only mode); otherwise the live working inputs.
    if cur is not None:
        cur_cif, cur_frt = cur
    else:
        cur_cif, cur_frt, _ = _current_payloads()
    cur_lbl = "selected date" if cur is not None else "working"
    dates = sorted(db.list_dates())
    before = [d for d in dates if d < as_of.isoformat()]
    pdaily = before[-1] if before else None
    pweek = None
    if before:
        tgt = as_of - dt.timedelta(days=7)
        pweek = min(before, key=lambda d: abs((dt.date.fromisoformat(d) - tgt).days))
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
        _c1, _c2 = st.columns([0.9, 0.1])
        with _c2:
            daily_png = _df_to_png(
                _build_daily_changes_df(cur_cif, cur_frt, d_cif, d_frt),
                "Daily Changes")
            if daily_png:
                st.download_button(
                    label="📥 PNG", data=daily_png,
                    file_name=f"daily_changes_{as_of.isoformat()}.png",
                    mime="image/png")

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
    st.markdown(f'<div class="sheet-wrap"><table class="sheet">{"".join(rows)}</table></div>',
                unsafe_allow_html=True)
    st.caption(f"Day-over-day: {cur_lbl} values vs prior archived date "
               f"({pdaily or 'none'}).")

    # --- Weekly: CIF / STL freight / STL FOB per commodity, vs ~1 week ago ---
    st.markdown("#### Weekly Changes")
    if allow_download:
        _c1, _c2 = st.columns([0.9, 0.1])
        with _c2:
            weekly_png = _df_to_png(
                _build_weekly_changes_df(cur_cif, cur_frt, w_cif, w_frt),
                "Weekly Changes")
            if weekly_png:
                st.download_button(
                    label="📥 PNG", data=weekly_png,
                    file_name=f"weekly_changes_{as_of.isoformat()}.png",
                    mime="image/png")

    rows = [hdr("Weekly Changes")]

    # STL Freight once at the top
    rows.append(f'<tr class="section"><td colspan="{ncol}">STL Freight</td></tr>')
    cells = "".join(_chg_cell((cur_frt.get("STL") or {}).get(m),
                              (w_frt.get("STL") or {}).get(m), "pct")
                    for m in M.MONTHS)
    rows.append(f'<tr class="frt-row"><td class="lbl">—</td>{cells}</tr>')

    # CIF and FOB by commodity
    for c in M.COMMODITIES:
        rows.append(f'<tr class="section"><td colspan="{ncol}">{c}</td></tr>')
        cur_fob = M.compute_fob_grid(c, cur_cif.get(c) or {}, cur_frt)["STL"]
        w_fob = (M.compute_fob_grid(c, w_cif.get(c) or {}, w_frt)["STL"]
                 if w_cif.get(c) else {})

        # CIF
        cells = "".join(_chg_cell((cur_cif.get(c) or {}).get(m),
                                  (w_cif.get(c) or {}).get(m), "num")
                        for m in M.MONTHS)
        rows.append(f'<tr class="strong"><td class="lbl">CIF</td>{cells}</tr>')

        # FOB
        cells = "".join(_chg_cell(cur_fob.get(m), w_fob.get(m), "num")
                        for m in M.MONTHS)
        rows.append(f'<tr class="strong"><td class="lbl">FOB</td>{cells}</tr>')

    st.markdown(f'<div class="sheet-wrap"><table class="sheet">{"".join(rows)}</table></div>',
                unsafe_allow_html=True)
    st.caption(f"Week-over-week: {cur_lbl} values vs ~7 days ago "
               f"({pweek or 'none'}).")


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


def apply_pasted_tables(cif_text, frt_text, fut_text):
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
            for commodity, lp in res["futures"].items():
                if commodity not in M.COMMODITIES:
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
            msgs.append(f"Futures — filled {nf} CBOT values; computed {ns} spreads.")
    if msgs:
        _bump_editors()
    return msgs, errs


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
                   "freight date auto-sets the as-of date; futures fill the CBOT "
                   "row and compute spreads.")
        pc1, pc2, pc3 = st.columns(3)
        with pc1:
            cif_text = st.text_area("CIF NOLA table", height=220, key="paste_cif")
        with pc2:
            frt_text = st.text_area("Barge Freight table", height=220, key="paste_frt")
        with pc3:
            fut_text = st.text_area("Futures (Symbol / Last)", height=220,
                                    key="paste_fut")
        if st.button("⤵ Parse & fill inputs", type="primary"):
            st.session_state["paste_result"] = apply_pasted_tables(
                cif_text, frt_text, fut_text)
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
    st.markdown(render_block(commodity, view_date, cif_row, fut_row, fbr,
                             spreads, fullcarry, cashc, historical=True,
                             contracts=contracts, months=months, prior=prior),
                unsafe_allow_html=True)
    st.markdown("##### 📈 Top of Carry")
    render_carry_chart(commodity, grid, spreads, as_of=view_date, months=months,
                       contracts=contracts, cur_label=f"{view_date:%m/%d/%y}")


if VIEW_ONLY:
    if not HIST_DATE or hist_cif is None:
        st.info("No archived data available to view yet.")
    else:
        tabs = st.tabs(["📊 Changes"] + list(M.COMMODITIES) + ["📈 Seasonal"])
        with tabs[0]:
            render_changes_tab(view_date, cur=(hist_cif, hist_frt),
                               allow_download=False)
        for tab, commodity in zip(tabs[1:1 + len(M.COMMODITIES)], M.COMMODITIES):
            with tab:
                _render_archived_commodity(commodity)
        with tabs[-1]:
            render_seasonal_tab()
elif HIST_DATE:
    tabs = st.tabs(M.COMMODITIES + ["📈 Seasonal"])
    with tabs[-1]:
        render_seasonal_tab()
    for tab, commodity in zip(tabs[:len(M.COMMODITIES)], M.COMMODITIES):
        with tab:
            _render_archived_commodity(commodity)
else:
    tabs = st.tabs(["📊 Changes", "📝 Inputs"] + M.COMMODITIES + ["📈 Seasonal"])
    with tabs[0]:
        render_changes_tab(as_of)
    with tabs[1]:
        render_inputs_tab(as_of)
    with tabs[-1]:
        render_seasonal_tab()
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
            st.markdown(render_block(commodity, as_of, cif_row, fut_row, fbr,
                                     spreads, fullcarry, cashc, prior=prior,
                                     contracts=st.session_state.get(f"contracts_{commodity}")),
                        unsafe_allow_html=True)
            st.markdown("##### 📈 Top of Carry")
            render_carry_chart(commodity, M.compute_fob_grid(commodity, cif_row, fbr),
                               spreads, as_of=as_of)

st.caption("Mirrors JSA FOB Sheet · FOB = CIF − (tariff factor × freight%) ÷ 2000 × bushel weight")
