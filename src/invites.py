"""Account-setup invitations and password-reset codes.

An admin invites a user by email only (no username/password chosen by the
admin); the user activates their account with a one-time setup code, choosing
their own username and password. 'Forgot password' uses the same mechanism:
a short-lived reset code tied to the user's account.

Codes are short, typable strings (not links) so they work the same way on
every deployment (local, Streamlit Cloud, Cloud Run) without needing to know
the app's public URL. Storage mirrors the rest of the app: local CSV now,
PostgreSQL when DATABASE_URL is set.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd

from src import engagements as EN

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "data" / "invites.csv"
TABLE = "invites"
FIELDS = [
    "code", "kind", "email", "name", "role", "username",
    "invited_by", "created_at", "expires_at", "used",
]

SETUP_TTL_HOURS = 48
RESET_TTL_HOURS = 2


def _make_code() -> str:
    return secrets.token_hex(5).upper()  # 10 hex chars, easy to copy/paste


def create_setup_invite(email: str, name: str, role: str, invited_by: str = "") -> str:
    """Invite a new user by email. Returns the one-time setup code."""
    code = _make_code()
    now = datetime.now()
    row = pd.DataFrame([{
        "code": code, "kind": "setup", "email": (email or "").strip().lower(),
        "name": (name or "").strip(), "role": role, "username": "",
        "invited_by": invited_by, "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "expires_at": (now + timedelta(hours=SETUP_TTL_HOURS)).strftime("%Y-%m-%d %H:%M:%S"),
        "used": False,
    }], columns=FIELDS)
    EN._append(row, CSV_PATH, TABLE)
    return code


def create_reset_code(username: str, email: str = "", requested_by: str = "") -> str:
    """Issue a password-reset code for an existing username. Returns the code."""
    code = _make_code()
    now = datetime.now()
    row = pd.DataFrame([{
        "code": code, "kind": "reset", "email": (email or "").strip().lower(),
        "name": "", "role": "", "username": username,
        "invited_by": requested_by, "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "expires_at": (now + timedelta(hours=RESET_TTL_HOURS)).strftime("%Y-%m-%d %H:%M:%S"),
        "used": False,
    }], columns=FIELDS)
    EN._append(row, CSV_PATH, TABLE)
    return code


def _load() -> pd.DataFrame:
    return EN._read(CSV_PATH, TABLE, FIELDS)


def get_valid(code: str, kind: str) -> dict | None:
    """Return the invite/reset row if `code` is valid, unused, and unexpired."""
    df = _load()
    if df.empty:
        return None
    code = (code or "").strip().upper()
    if not code:
        return None
    m = df[(df["code"].astype(str).str.upper() == code) & (df["kind"].astype(str) == kind)]
    if m.empty:
        return None
    row = m.sort_values("created_at").iloc[-1]
    used = str(row.get("used")).strip().lower() in ("true", "1")
    if used:
        return None
    exp = pd.to_datetime(row.get("expires_at"), errors="coerce")
    if pd.isna(exp) or datetime.now() > exp.to_pydatetime():
        return None
    return row.to_dict()


def mark_used(code: str) -> None:
    code = (code or "").strip().upper()
    if not code:
        return
    if EN._use_db():
        if EN._table_exists(TABLE):
            from sqlalchemy import text
            with EN._engine().begin() as conn:
                conn.execute(text(f"UPDATE {TABLE} SET used = TRUE WHERE code = :c"), {"c": code})
        return
    df = _load()
    if df.empty:
        return
    df = df.astype(object)
    df.loc[df["code"].astype(str).str.upper() == code, "used"] = True
    df.to_csv(CSV_PATH, index=False, encoding="utf-8-sig")


def pending(kind: str | None = None) -> pd.DataFrame:
    """Active (unused, unexpired) invites/reset codes — for the admin's Manage Users view."""
    df = _load()
    if df.empty:
        return df
    if kind:
        df = df[df["kind"].astype(str) == kind]
    used = df["used"].astype(str).str.strip().str.lower().isin(("true", "1"))
    exp = pd.to_datetime(df["expires_at"], errors="coerce")
    active = (~used) & (exp >= pd.Timestamp.now())
    return df[active].sort_values("created_at", ascending=False).reset_index(drop=True)
