"""Storage for employee-submitted 'Ordered Quotation' reports.

Saves to a local CSV (data/quotations.csv) when running locally, and to a
PostgreSQL table 'quotations' automatically when DATABASE_URL is set (e.g. on
the cloud host). The field set mirrors the workbook's Quotation control sheet.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "data" / "quotations.csv"
TABLE = "quotations"

# Column order used for both the CSV and the DB table.
FIELDS = [
    "submitted_at", "submitted_by", "quotation_number", "date", "month",
    "company", "branch", "classification", "type_of_service", "condition",
    "client_type", "process_of_contact", "invoiced_month",
    "monthly_fee", "yearly_or_spot_fee", "contents",
]

# Dropdown choices (match the workbook's data lists).
CLASSIFICATIONS = ["Subscribe", "Spot", "AMP"]
BRANCHES = ["Makati", "Cebu", "AMP"]
CLIENT_TYPES = ["New", "Existing", "Past"]
SERVICE_TYPES = [
    "Monthly Accounting", "Accounting Spot", "Accounting Annual",
    "Annual Audit", "Audit Spot", "Legal Spot", "Legal Annual",
    "Advisory", "Proxy", "HR Spot", "Payroll", "Other",
]
CONTACT_PROCESS = ["Referral", "Existing client", "Website", "Email", "Walk-in", "Other"]


def _use_db() -> bool:
    return bool(os.getenv("DATABASE_URL"))


def storage_label() -> str:
    return "cloud database" if _use_db() else f"local file ({CSV_PATH.name})"


def _engine():
    from src.db_loader import make_engine, _normalize_url  # lazy import
    # DATABASE_URL path inside make_engine handles the connection string.
    return make_engine({}, None, None)


def save_quotation(record: dict) -> str:
    """Persist one ordered-quotation record. Returns where it was saved."""
    row = {k: record.get(k, "") for k in FIELDS}
    df = pd.DataFrame([row], columns=FIELDS)

    if _use_db():
        df.to_sql(TABLE, _engine(), if_exists="append", index=False)
        return "cloud database"

    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    header = not CSV_PATH.exists()
    df.to_csv(CSV_PATH, mode="a", header=header, index=False, encoding="utf-8-sig")
    return f"local file ({CSV_PATH})"


def load_quotations() -> pd.DataFrame:
    """Return all submitted quotations (newest first), or an empty frame."""
    try:
        if _use_db():
            from sqlalchemy import text
            with _engine().connect() as conn:
                exists = conn.execute(text(
                    "SELECT to_regclass('public.quotations')")).scalar()
                if not exists:
                    return pd.DataFrame(columns=FIELDS)
                df = pd.read_sql(f"SELECT * FROM {TABLE}", conn)
        elif CSV_PATH.exists():
            df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")
        else:
            return pd.DataFrame(columns=FIELDS)
    except Exception:
        return pd.DataFrame(columns=FIELDS)

    if "submitted_at" in df.columns:
        df = df.sort_values("submitted_at", ascending=False)
    return df.reset_index(drop=True)


def build_record(*, submitted_by, quotation_number, date, company, branch,
                 classification, type_of_service, client_type, process_of_contact,
                 invoiced_month, monthly_fee, yearly_or_spot_fee, contents) -> dict:
    """Assemble a normalised record dict (Condition is always 'Order')."""
    d = pd.to_datetime(date, errors="coerce")
    return {
        "submitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "submitted_by": (submitted_by or "").strip(),
        "quotation_number": (quotation_number or "").strip(),
        "date": d.strftime("%Y-%m-%d") if pd.notna(d) else "",
        "month": d.strftime("%Y-%m") if pd.notna(d) else "",
        "company": (company or "").strip(),
        "branch": branch,
        "classification": classification,
        "type_of_service": (type_of_service or "").strip(),
        "condition": "Order",
        "client_type": client_type,
        "process_of_contact": (process_of_contact or "").strip(),
        "invoiced_month": str(invoiced_month) if invoiced_month else "",
        "monthly_fee": float(monthly_fee or 0),
        "yearly_or_spot_fee": float(yearly_or_spot_fee or 0),
        "contents": (contents or "").strip(),
    }
