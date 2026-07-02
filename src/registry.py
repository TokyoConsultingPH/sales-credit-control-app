"""User roster/activity and a client database derived from reported quotations.

- Users: a small editable roster (name, role) plus an activity rollup showing
  who reported / completed which engagements.
- Clients: unique companies pulled from the logged (signed) quotations with
  their contact details, engagement counts, and total quoted value.

Storage follows the rest of the app: local CSV now, PostgreSQL when
DATABASE_URL is set.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
import pandas as pd

from src import quotations as Q
from src import engagements as EN
from src import security

ROOT = Path(__file__).resolve().parent.parent
USERS_CSV = ROOT / "data" / "users.csv"
USERS_TABLE = "app_users"
USER_FIELDS = ["username", "name", "role", "email", "salt", "pw_hash", "added_at"]
DISPLAY_FIELDS = ["username", "name", "role", "email", "added_at"]
ROLES = ["Staff", "Manager", "Admin"]


def _use_db() -> bool:
    return bool(os.getenv("DATABASE_URL"))


def _engine():
    from src.db_loader import make_engine
    return make_engine({}, None, None)


def _ensure_email_column() -> None:
    """Add the 'email' column to an existing users table/file that predates it.

    Safe/idempotent: no-op if the column is already there or nothing exists yet.
    """
    if _use_db():
        if EN._table_exists(USERS_TABLE):
            from sqlalchemy import text
            with _engine().begin() as conn:
                conn.execute(text(f"ALTER TABLE {USERS_TABLE} ADD COLUMN IF NOT EXISTS email TEXT"))
        return
    if USERS_CSV.exists():
        df = pd.read_csv(USERS_CSV, encoding="utf-8-sig")
        if "email" not in df.columns:
            df["email"] = ""
            cols = [c for c in USER_FIELDS if c in df.columns] + \
                   [c for c in df.columns if c not in USER_FIELDS]
            df[cols].to_csv(USERS_CSV, index=False, encoding="utf-8-sig")


def _first_nonempty(series: pd.Series) -> str:
    for v in series:
        if pd.notna(v) and str(v).strip():
            return str(v).strip()
    return ""


# --------------------------------------------------------------------------- #
# User roster
# --------------------------------------------------------------------------- #
def list_users() -> pd.DataFrame:
    return EN._read(USERS_CSV, USERS_TABLE, USER_FIELDS)


def list_users_display() -> pd.DataFrame:
    _ensure_email_column()
    df = list_users()
    return df[[c for c in DISPLAY_FIELDS if c in df.columns]] if not df.empty else \
        pd.DataFrame(columns=DISPLAY_FIELDS)


def get_user(username: str) -> dict | None:
    df = list_users()
    if df.empty or not username:
        return None
    m = df[df["username"].astype(str).str.lower() == str(username).strip().lower()]
    return m.iloc[0].to_dict() if not m.empty else None


def get_user_by_email(email: str) -> dict | None:
    _ensure_email_column()
    df = list_users()
    email = (email or "").strip().lower()
    if df.empty or not email or "email" not in df.columns:
        return None
    m = df[df["email"].astype(str).str.lower() == email]
    return m.iloc[0].to_dict() if not m.empty else None


def email_taken(email: str) -> bool:
    return get_user_by_email(email) is not None


def add_user(username: str, name: str, role: str, password: str,
            email: str = "") -> tuple[bool, str]:
    _ensure_email_column()
    username = (username or "").strip()
    if not username or not password:
        return False, "Username and password are required."
    if get_user(username):
        return False, f"User '{username}' already exists."
    salt, pw_hash = security.hash_password(password)
    row = pd.DataFrame([{
        "username": username, "name": (name or username).strip(), "role": role,
        "email": (email or "").strip().lower(), "salt": salt, "pw_hash": pw_hash,
        "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }], columns=USER_FIELDS)
    EN._append(row, USERS_CSV, USERS_TABLE)
    return True, f"User '{username}' added."


def set_password(username: str, password: str) -> None:
    salt, pw_hash = security.hash_password(password)
    if _use_db():
        from sqlalchemy import text
        with _engine().begin() as conn:
            conn.execute(text(f"UPDATE {USERS_TABLE} SET salt=:s, pw_hash=:h WHERE username=:u"),
                         {"s": salt, "h": pw_hash, "u": username})
        return
    df = list_users()
    if df.empty:
        return
    df = df.astype(object)
    mask = df["username"].astype(str).str.lower() == str(username).lower()
    df.loc[mask, "salt"] = salt
    df.loc[mask, "pw_hash"] = pw_hash
    df.to_csv(USERS_CSV, index=False, encoding="utf-8-sig")


def verify_login(username: str, password: str) -> dict | None:
    u = get_user(username)
    if u and security.verify_password(password, u.get("salt", ""), u.get("pw_hash", "")):
        return {"username": u["username"], "name": u.get("name") or u["username"],
                "role": u.get("role", "Staff"), "master": False}
    return None


def remove_user(username: str) -> None:
    if _use_db():
        from sqlalchemy import text
        with _engine().begin() as conn:
            conn.execute(text(f"DELETE FROM {USERS_TABLE} WHERE username = :n"), {"n": username})
        return
    df = list_users()
    if df.empty:
        return
    df = df[df["username"].astype(str) != str(username)]
    df.to_csv(USERS_CSV, index=False, encoding="utf-8-sig")


# --------------------------------------------------------------------------- #
# Activity — who reported / completed which engagements
# --------------------------------------------------------------------------- #
def user_activity() -> pd.DataFrame:
    q = Q.load_quotations()
    comp = EN._read(EN.COMPLETIONS_CSV, EN.COMPLETIONS_TABLE, EN.COMPLETION_FIELDS)

    if q.empty:
        rep = pd.DataFrame(columns=["Name", "Engagements reported", "Quotations",
                                    "Clients", "Total value", "Last reported"])
    else:
        q = q.copy()
        q["price"] = pd.to_numeric(q.get("price"), errors="coerce").fillna(0.0)
        q["who"] = q.get("submitted_by", "").fillna("").replace("", "(unknown)")
        rep = q.groupby("who").agg(
            **{"Engagements reported": ("line_no", "size"),
               "Quotations": ("quotation_number", lambda s: s.astype(str).nunique()),
               "Clients": ("company", "nunique"),
               "Total value": ("price", "sum"),
               "Last reported": ("submitted_at", "max")}
        ).reset_index().rename(columns={"who": "Name"})

    completed = (comp.assign(who=comp.get("completed_by", "").fillna("").replace("", "(unknown)"))
                 .groupby("who").size().rename("Engagements completed").reset_index()
                 .rename(columns={"who": "Name"})) if not comp.empty else \
        pd.DataFrame(columns=["Name", "Engagements completed"])

    out = rep.merge(completed, on="Name", how="outer")
    if "Engagements completed" in out:
        out["Engagements completed"] = out["Engagements completed"].fillna(0).astype(int)
    for c in ["Engagements reported", "Quotations", "Clients"]:
        if c in out:
            out[c] = out[c].fillna(0).astype(int)
    if "Total value" in out:
        out["Total value"] = out["Total value"].fillna(0.0)
    return out.sort_values("Engagements reported", ascending=False).reset_index(drop=True)


def user_engagements(name: str) -> pd.DataFrame:
    q = Q.load_quotations()
    if q.empty:
        return q
    who = q.get("submitted_by", "").fillna("").replace("", "(unknown)")
    sub = q[who == name]
    cols = ["order_date", "quotation_number", "line_no", "company", "department",
            "type_of_service", "price", "invoiced_month"]
    return sub[[c for c in cols if c in sub.columns]].reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Client database (from reported signed quotations)
# --------------------------------------------------------------------------- #
def client_database() -> pd.DataFrame:
    q = Q.load_quotations()
    if q.empty:
        return pd.DataFrame(columns=[
            "Client", "Contact", "Title", "Email", "Address", "Branch", "Type",
            "Quotations", "Engagements", "Total value", "First order", "Last order", "Services"])
    q = q.copy()
    q["price"] = pd.to_numeric(q.get("price"), errors="coerce").fillna(0.0)
    g = q.groupby(q["company"].fillna("(unknown)")).agg(
        Contact=("contact_person", _first_nonempty),
        Title=("contact_title", _first_nonempty),
        Email=("contact_email", _first_nonempty),
        Address=("contact_address", _first_nonempty),
        Branch=("branch", _first_nonempty),
        Type=("client_type", _first_nonempty),
        Quotations=("quotation_number", lambda s: s.astype(str).nunique()),
        Engagements=("line_no", "size"),
        **{"Total value": ("price", "sum")},
        **{"First order": ("order_date", "min")},
        **{"Last order": ("order_date", "max")},
        Services=("type_of_service", lambda s: ", ".join(sorted({str(x).strip() for x in s if str(x).strip()}))),
    ).reset_index().rename(columns={"company": "Client"})
    return g.sort_values("Total value", ascending=False).reset_index(drop=True)


def client_engagements(company: str) -> pd.DataFrame:
    q = Q.load_quotations()
    if q.empty:
        return q
    sub = q[q["company"].fillna("(unknown)") == company]
    cols = ["order_date", "quotation_number", "line_no", "department", "pic",
            "type_of_service", "service_description", "price", "invoiced_month"]
    return sub[[c for c in cols if c in sub.columns]].reset_index(drop=True)
