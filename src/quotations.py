"""Storage for employee-submitted 'Ordered Quotation' reports.

Fields mirror the official TCF Quotation document (御見積書): client contact
block, quotation number, issue/order dates, the ordered service line
(type, description, unit, price), and order context. Condition is always 'Order'.

Saves to a local CSV (data/quotations.csv) when running locally, and to a
PostgreSQL table 'quotations' automatically when DATABASE_URL is set.
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
    "submitted_at", "submitted_by",
    "quotation_number", "issue_date", "order_date", "month",
    "company", "contact_person", "contact_title", "contact_email", "contact_address",
    "branch", "classification", "client_type", "process_of_contact",
    "type_of_service", "service_description", "unit", "price",
    "condition", "invoiced_month",
]

# Dropdown choices (match the workbook's data lists and the quotation document).
CLASSIFICATIONS = ["Subscribe", "Spot", "AMP"]
BRANCHES = ["Makati", "Cebu", "AMP"]
CLIENT_TYPES = ["New", "Existing", "Past"]
UNITS = ["PHP/Year", "PHP/Month", "PHP/Spot"]
SERVICE_TYPES = [
    "Annual Statutory Audit Service", "Annual Compilation & Audit Assistance Service",
    "Monthly Accounting", "Accounting Spot", "Accounting Annual",
    "Audit Spot", "Legal Spot", "Legal Annual",
    "Advisory", "Proxy", "HR Spot", "Payroll", "Other",
]
CONTACT_PROCESS = ["Referral", "Existing client", "Website", "Email", "Walk-in", "Other"]


def _use_db() -> bool:
    return bool(os.getenv("DATABASE_URL"))


def storage_label() -> str:
    return "cloud database" if _use_db() else f"local file ({CSV_PATH.name})"


def _engine():
    from src.db_loader import make_engine  # lazy import; uses DATABASE_URL
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
                if not conn.execute(text("SELECT to_regclass('public.quotations')")).scalar():
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


def build_record(*, submitted_by, quotation_number, issue_date, order_date,
                 company, contact_person, contact_title, contact_email, contact_address,
                 branch, classification, client_type, process_of_contact,
                 type_of_service, service_description, unit, price,
                 invoiced_month) -> dict:
    """Assemble a normalised record dict (Condition is always 'Order')."""
    od = pd.to_datetime(order_date, errors="coerce")
    iss = pd.to_datetime(issue_date, errors="coerce")
    basis = od if pd.notna(od) else iss
    return {
        "submitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "submitted_by": (submitted_by or "").strip(),
        "quotation_number": (quotation_number or "").strip(),
        "issue_date": iss.strftime("%Y-%m-%d") if pd.notna(iss) else "",
        "order_date": od.strftime("%Y-%m-%d") if pd.notna(od) else "",
        "month": basis.strftime("%Y-%m") if pd.notna(basis) else "",
        "company": (company or "").strip(),
        "contact_person": (contact_person or "").strip(),
        "contact_title": (contact_title or "").strip(),
        "contact_email": (contact_email or "").strip(),
        "contact_address": (contact_address or "").strip(),
        "branch": branch,
        "classification": classification,
        "client_type": client_type,
        "process_of_contact": (process_of_contact or "").strip(),
        "type_of_service": (type_of_service or "").strip(),
        "service_description": (service_description or "").strip(),
        "unit": unit,
        "price": float(price or 0),
        "condition": "Order",
        "invoiced_month": str(invoiced_month or "").strip(),
    }
