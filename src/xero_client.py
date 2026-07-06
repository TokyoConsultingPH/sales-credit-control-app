"""Xero Accounting API integration (Custom Connection).

Uses a Xero "Custom Connection" — client_credentials OAuth2 grant, the right
fit here since this app talks to just one company's own Xero org (no
per-user login/consent redirect flow needed, unlike Xero's standard
Authorization Code flow for multi-tenant apps). Set one up at
developer.xero.com/myapps -> New app -> Custom connection (requires a Xero
Standard or Premium plan; Custom Connections aren't available on Starter).

Credentials come from env vars (never stored in the repo or settings.yaml):
  XERO_CLIENT_ID, XERO_CLIENT_SECRET
  XERO_TENANT_ID (optional — only needed if Xero asks for it; most custom
  connections don't require it since the connection is already scoped to
  one org).

If not configured, push_billing_to_xero() returns (False, reason) and the
caller falls back to in-app status only — mirrors src/notify_email.py.
"""
from __future__ import annotations

import os
import time
import requests

TOKEN_URL = "https://identity.xero.com/connect/token"
API_BASE = "https://api.xero.com/api.xro/2.0"

_token_cache: dict = {"access_token": None, "expires_at": 0}


def _creds() -> tuple[str | None, str | None]:
    return os.getenv("XERO_CLIENT_ID"), os.getenv("XERO_CLIENT_SECRET")


def configured() -> tuple[bool, str]:
    cid, secret = _creds()
    if not cid or not secret:
        return False, "Xero not configured (set XERO_CLIENT_ID / XERO_CLIENT_SECRET)."
    return True, "Xero configured."


def _get_token() -> str:
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]
    cid, secret = _creds()
    if not cid or not secret:
        raise RuntimeError("Xero not configured (set XERO_CLIENT_ID / XERO_CLIENT_SECRET).")
    resp = requests.post(TOKEN_URL, data={"grant_type": "client_credentials"},
                         auth=(cid, secret), timeout=20)
    resp.raise_for_status()
    data = resp.json()
    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + int(data.get("expires_in", 1800))
    return _token_cache["access_token"]


def _headers() -> dict:
    h = {
        "Authorization": f"Bearer {_get_token()}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    tenant_id = os.getenv("XERO_TENANT_ID")
    if tenant_id:
        h["Xero-tenant-id"] = tenant_id
    return h


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
