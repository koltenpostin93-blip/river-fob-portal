"""
Minimal Fastmarkets Physical Prices API v2 client.

Auth is OAuth2 "servicekey": POST serviceName + serviceKey to the connect/token
endpoint for a short-lived Bearer access token, then call the data endpoints with
it. Creds come from the environment (kept in the gitignored .env / Streamlit
secrets), never hard-coded:

    FOB_VESSEL_SERVICE_NAME   the Service Name Fastmarkets issued
    FOB_VESSEL_API_KEY        the Service Key

Docs: https://api.fastmarkets.com/physical/v2/documentation
"""
import os
import time
import requests

AUTH_URL = "https://auth.fastmarkets.com/connect/token"
BASE = "https://api.fastmarkets.com/Physical/v2"
SCOPE = "fastmarkets.physicalprices.api"

_token = {"value": None, "exp": 0.0}


def _creds():
    return (os.environ.get("FOB_VESSEL_SERVICE_NAME", "").strip(),
            os.environ.get("FOB_VESSEL_API_KEY", "").strip())


def configured():
    name, key = _creds()
    return bool(name and key)


def get_token(force=False):
    """Return a cached Bearer token, refreshing when missing/near expiry."""
    name, key = _creds()
    if not (name and key):
        raise RuntimeError(
            "Fastmarkets creds not set — need FOB_VESSEL_SERVICE_NAME and "
            "FOB_VESSEL_API_KEY in the environment / .env.")
    now = time.time()
    if not force and _token["value"] and now < _token["exp"] - 60:
        return _token["value"]
    r = requests.post(
        AUTH_URL,
        data={"grant_type": "servicekey", "client_id": "service_client",
              "scope": SCOPE, "serviceName": name, "serviceKey": key},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30)
    r.raise_for_status()
    j = r.json()
    _token["value"] = j["access_token"]
    _token["exp"] = now + float(j.get("expires_in", 3600))
    return _token["value"]


def _get(path, params, _retry=True):
    r = requests.get(f"{BASE}/{path}",
                     headers={"Authorization": "Bearer " + get_token(),
                              "cache-control": "no-cache"},
                     params=params, timeout=60)
    if r.status_code == 401 and _retry:            # token expired mid-flight
        get_token(force=True)
        return _get(path, params, _retry=False)
    r.raise_for_status()
    return r.json()


# Default optional fields so instrument metadata comes back with names, not IDs.
_INSTR_FIELDS = ("Commodity", "Location", "Currency", "UnitOfMeasure", "Incoterm")


def instruments(symbols=None, fields=_INSTR_FIELDS):
    """Instrument metadata. No symbols -> every instrument the service is
    entitled to (used to discover which symbols we can pull)."""
    params = {}
    if symbols:
        params["symbols"] = symbols
    if fields:
        params["fields"] = list(fields)
    return _get("Instruments", params)


def prices(symbols, dates=None, fields=None):
    """Latest (or specific-date) low/mid/high for the given symbol(s)."""
    params = {"symbols": symbols}
    if dates:
        params["dates"] = dates
    if fields:
        params["fields"] = list(fields)
    return _get("Prices", params)


def history(symbols, from_date, to_date, calendar="Weekdays", carry_forward=True):
    """A date-range series (descending by date) for the given symbol(s)."""
    return _get("Prices/history",
                {"symbols": symbols, "fromDate": from_date, "toDate": to_date,
                 "calendarType": calendar, "carryForward": carry_forward})


def references(types):
    """Reference lists (Currency, Commodity, Incoterm, UnitOfMeasure, …)."""
    return _get("References", {"types": types})
