"""Xero Accounting API integration (standard OAuth 2.0 authorization code flow).

Xero "Custom Connections" are only purchasable for AU/NZ/UK/US organisations,
so a Philippine org must use the standard (free, all-regions) auth code flow:
an admin clicks "Connect to Xero" once, approves access in Xero, and the app
stores the rotating refresh token + tenant id (CSV locally, Postgres when
DATABASE_URL is set — same dual-storage pattern as the rest of the app).

App registration (developer.xero.com/myapps -> New app -> "Web app"):
  - Redirect URI must EXACTLY match the app's own URL (one per deployment).
  - Env vars (never committed): XERO_CLIENT_ID, XERO_CLIENT_SECRET,
    XERO_REDIRECT_URI (defaults to http://localhost:8501/ for local dev).

Xero rotates the refresh token on every use (old one stays valid ~30 min,
which covers the two deployments sharing one Neon-stored token) and expires
it after 60 days idle — if that happens, status() says to reconnect.

If not configured/connected, push_billing_to_xero() returns (False, reason)
and the caller falls back to in-app status only — mirrors src/notify_email.py.
"""
from __future__ import annotations

import os
import secrets as pysecrets
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

import pandas as pd
import requests

from src import engagements as EN

AUTH_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"
API_BASE = "https://api.xero.com/api.xro/2.0"
SCOPES = "offline_access accounting.transactions accounting.contacts"
STATE_TTL_MIN = 30

OAUTH_CSV = EN.ROOT / "data" / "xero_oauth.csv"
OAUTH_TABLE = "xero_oauth"
OAUTH_FIELDS = ["kind", "updated_at", "state", "refresh_token", "tenant_id", "org_name"]

_token_cache: dict = {"access_token": None, "expires_at": 0}


# --------------------------------------------------------------------------- #
# Credentials / storage
# --------------------------------------------------------------------------- #
def _creds() -> tuple[str | None, str | None]:
    return os.getenv("XERO_CLIENT_ID"), os.getenv("XERO_CLIENT_SECRET")


def redirect_uri() -> str:
    return os.getenv("XERO_REDIRECT_URI") or "http://localhost:8501/"


def _load_rows() -> pd.DataFrame:
    return EN._read(OAUTH_CSV, OAUTH_TABLE, OAUTH_FIELDS)


def _save_rows(df: pd.DataFrame) -> None:
    df = df.reindex(columns=OAUTH_FIELDS).fillna("")
    if EN._use_db():
        df.to_sql(OAUTH_TABLE, EN._engine(), if_exists="replace", index=False)
        return
    OAUTH_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OAUTH_CSV, index=False, encoding="utf-8-sig")


def _set_row(kind: str, **vals) -> None:
    rows = _load_rows()
    rows = rows[rows.get("kind", pd.Series(dtype=str)).astype(str) != kind] \
        if not rows.empty else pd.DataFrame(columns=OAUTH_FIELDS)
    row = {f: "" for f in OAUTH_FIELDS}
    row.update({"kind": kind, "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    row.update(vals)
    _save_rows(pd.concat([rows, pd.DataFrame([row])], ignore_index=True))


def _get_row(kind: str) -> dict | None:
    rows = _load_rows()
    if rows.empty or "kind" not in rows.columns:
        return None
    hit = rows[rows["kind"].astype(str) == kind]
    return hit.iloc[-1].to_dict() if not hit.empty else None


def load_connection() -> dict | None:
    row = _get_row("token")
    return row if row and str(row.get("refresh_token") or "").strip() else None


def disconnect() -> None:
    rows = _load_rows()
    if not rows.empty and "kind" in rows.columns:
        _save_rows(rows[rows["kind"].astype(str) != "token"])
    _token_cache["access_token"] = None


def configured() -> tuple[bool, str]:
    cid, secret = _creds()
    if not cid or not secret:
        return False, "Xero not configured (set XERO_CLIENT_ID / XERO_CLIENT_SECRET)."
    conn = load_connection()
    if not conn:
        return False, "Xero not connected yet — an admin must click 'Connect to Xero' (Users page)."
    return True, f"Xero connected to {conn.get('org_name') or 'your organisation'}."


# --------------------------------------------------------------------------- #
# OAuth flow
# --------------------------------------------------------------------------- #
def connect_url() -> str:
    """Build the Xero consent URL; persists a one-time `state` for CSRF check."""
    cid, _ = _creds()
    state = pysecrets.token_urlsafe(24)
    _set_row("state", state=state)
    return AUTH_URL + "?" + urlencode({
        "response_type": "code", "client_id": cid or "",
        "redirect_uri": redirect_uri(), "scope": SCOPES, "state": state,
    })


def complete_connect(code: str, state: str) -> tuple[bool, str]:
    """Exchange the callback `code` for tokens and store the connection."""
    saved = _get_row("state")
    if not saved or str(saved.get("state") or "") != str(state or ""):
        return False, "Xero connect failed: state mismatch — start again from 'Connect to Xero'."
    try:
        issued = pd.to_datetime(saved.get("updated_at"), errors="coerce")
        if pd.isna(issued) or datetime.now() - issued > timedelta(minutes=STATE_TTL_MIN):
            return False, "Xero connect link expired — start again from 'Connect to Xero'."
        cid, secret = _creds()
        resp = requests.post(TOKEN_URL, data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri()}, auth=(cid, secret), timeout=20)
        resp.raise_for_status()
        tok = resp.json()
        conns = requests.get(CONNECTIONS_URL, headers={
            "Authorization": f"Bearer {tok['access_token']}",
            "Accept": "application/json"}, timeout=20)
        conns.raise_for_status()
        orgs = conns.json()
        if not orgs:
            return False, "Xero connect failed: no organisation was authorised."
        org = sorted(orgs, key=lambda o: o.get("createdDateUtc") or "")[-1]
        _set_row("token", refresh_token=tok["refresh_token"],
                 tenant_id=org["tenantId"], org_name=org.get("tenantName") or "")
        _token_cache["access_token"] = tok["access_token"]
        _token_cache["expires_at"] = time.time() + int(tok.get("expires_in", 1800))
        return True, f"Connected to Xero organisation: {org.get('tenantName') or org['tenantId']}."
    except Exception as exc:
        return False, f"Xero connect failed: {exc}"


def _get_access_token() -> str:
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]
    conn = load_connection()
    if not conn:
        raise RuntimeError("Xero not connected — use 'Connect to Xero' on the Users page.")
    cid, secret = _creds()
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": conn["refresh_token"]}, auth=(cid, secret), timeout=20)
    if resp.status_code == 400:
        raise RuntimeError("Xero session expired (refresh token invalid) — an admin must "
                           "reconnect via 'Connect to Xero' on the Users page.")
    resp.raise_for_status()
    tok = resp.json()
    # Xero rotates the refresh token on every refresh — persist the new one.
    _set_row("token", refresh_token=tok["refresh_token"],
             tenant_id=conn.get("tenant_id", ""), org_name=conn.get("org_name", ""))
    _token_cache["access_token"] = tok["access_token"]
    _token_cache["expires_at"] = time.time() + int(tok.get("expires_in", 1800))
    return _token_cache["access_token"]


def _headers() -> dict:
    conn = load_connection() or {}
    return {
        "Authorization": f"Bearer {_get_access_token()}",
        "Xero-tenant-id": str(conn.get("tenant_id") or ""),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


# --------------------------------------------------------------------------- #
# Accounting API
# --------------------------------------------------------------------------- #
def find_contact_id(name: str) -> str | None:
    name = (name or "").strip()
    if not name:
        return None
    safe = name.replace('"', '\\"')
    resp = requests.get(f"{API_BASE}/Contacts", headers=_headers(),
                        params={"where": f'Name=="{safe}"'}, timeout=20)
    resp.raise_for_status()
    contacts = resp.json().get("Contacts", [])
    return contacts[0]["ContactID"] if contacts else None


def create_contact(name: str) -> str:
    resp = requests.put(f"{API_BASE}/Contacts", headers=_headers(),
                        json={"Contacts": [{"Name": name}]}, timeout=20)
    resp.raise_for_status()
    return resp.json()["Contacts"][0]["ContactID"]


def get_or_create_contact(name: str) -> str:
    return find_contact_id(name) or create_contact(name)


def create_draft_invoice(*, company: str, description: str, amount: float,
                         reference: str = "", account_code: str = "200",
                         currency: str = "PHP") -> dict:
    """Create a DRAFT accounts-receivable invoice in Xero with one line item.
    Returns the created Invoice object (raises on HTTP/API error)."""
    contact_id = get_or_create_contact(company)
    body = {
        "Invoices": [{
            "Type": "ACCREC",
            "Contact": {"ContactID": contact_id},
            "LineItems": [{
                "Description": description or "Professional services",
                "Quantity": 1,
                "UnitAmount": round(float(amount), 2),
                "AccountCode": str(account_code),
            }],
            "Reference": reference or "",
            "Status": "DRAFT",
            "CurrencyCode": currency,
        }]
    }
    resp = requests.put(f"{API_BASE}/Invoices", headers=_headers(), json=body, timeout=20)
    resp.raise_for_status()
    return resp.json()["Invoices"][0]


def push_billing_to_xero(cfg: dict, *, company: str, service: str, amount: float,
                         reference: str = "") -> tuple[bool, str]:
    """Best-effort: create a Xero draft invoice for one billing notification.
    Returns (ok, message) — never raises, so a Xero outage can't block
    'Mark billed' from completing in-app."""
    ok, msg = configured()
    if not ok:
        return False, msg
    xc = (cfg or {}).get("xero", {}) or {}
    if not xc.get("enabled", True):
        return False, "Xero integration disabled (set xero.enabled: true in settings.yaml)."
    try:
        inv = create_draft_invoice(
            company=company, description=service, amount=amount, reference=reference,
            account_code=xc.get("sales_account_code", "200"), currency=xc.get("currency", "PHP"))
        num = inv.get("InvoiceNumber") or str(inv.get("InvoiceID", ""))[:8]
        return True, f"Xero draft invoice created ({num})."
    except Exception as exc:
        return False, f"Xero draft invoice failed: {exc}"
