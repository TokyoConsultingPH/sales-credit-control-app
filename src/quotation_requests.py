"""Quotation requests submitted by employees.

An employee requests that a quotation be prepared for a prospective client,
giving the header details (who to address it to) and the list of services to
be quoted. This sits upstream of 'Report Ordered Quotation' — a request here
has no price yet; once the quotation is prepared and signed, it gets logged
on the Report Ordered Quotation page.

Storage mirrors the rest of the app: local CSV now, PostgreSQL when
DATABASE_URL is set (via src.engagements' generic _read/_append helpers).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import pandas as pd

from src import engagements as EN

ROOT = Path(__file__).resolve().parent.parent

CSV_PATH = ROOT / "data" / "quotation_requests.csv"
TABLE = "quotation_requests"
FIELDS = [
    "request_id", "submitted_at", "requested_by",
    "requestee_name", "company_name", "addressee", "designation",
    "company_address", "contact_email", "line_no", "service", "unit", "unit_price",
]

UNITS = ["PHP/Year", "PHP/Month", "PHP/Spot"]

STATUS_CSV = ROOT / "data" / "quotation_request_status.csv"
STATUS_TABLE = "quotation_request_status"
STATUS_FIELDS = ["request_id", "status", "updated_by", "updated_at"]
STATUS_OPTIONS = ["Pending", "In Progress", "Quotation Sent", "Declined"]

SUMMARY_FIELDS = [
    "request_id", "submitted_at", "requested_by", "requestee_name", "company_name",
    "addressee", "designation", "company_address", "contact_email", "services",
    "total_price", "status",
]


def save_request(*, requested_by, requestee_name, company_name, addressee,
                 designation, company_address, contact_email,
                 services: list[dict]) -> str:
    """Save one quotation request (one row per service line). Returns the request_id.

    Each item in `services` is a dict with keys: service, unit, unit_price.
    """
    request_id = f"QR-{datetime.now():%Y%m%d%H%M%S%f}"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = [{
        "request_id": request_id, "submitted_at": now,
        "requested_by": (requested_by or "").strip(),
        "requestee_name": (requestee_name or "").strip(),
        "company_name": (company_name or "").strip(),
        "addressee": (addressee or "").strip(),
        "designation": (designation or "").strip(),
        "company_address": (company_address or "").strip(),
        "contact_email": (contact_email or "").strip(),
        "line_no": i, "service": (svc.get("service") or "").strip(),
        "unit": (svc.get("unit") or "").strip(),
        "unit_price": float(svc.get("unit_price") or 0),
    } for i, svc in enumerate(services, start=1)]
    EN._append(pd.DataFrame(rows, columns=FIELDS), CSV_PATH, TABLE)
    set_status(request_id, "Pending", requested_by)
    return request_id


def load_requests() -> pd.DataFrame:
    df = EN._read(CSV_PATH, TABLE, FIELDS)
    if not df.empty and "submitted_at" in df.columns:
        df = df.sort_values("submitted_at", ascending=False)
    return df.reset_index(drop=True)


def load_statuses() -> dict:
    df = EN._read(STATUS_CSV, STATUS_TABLE, STATUS_FIELDS)
    if df.empty:
        return {}
    df = df.sort_values("updated_at")
    return dict(zip(df["request_id"].astype(str), df["status"].astype(str)))


def set_status(request_id: str, status: str, user: str = "") -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = pd.DataFrame([{"request_id": request_id, "status": status,
                         "updated_by": user, "updated_at": now}], columns=STATUS_FIELDS)
    if EN._use_db():
        from sqlalchemy import text
        with EN._engine().begin() as conn:
            conn.execute(text(f'DELETE FROM {STATUS_TABLE} WHERE request_id = :r'),
                         {"r": request_id})
        EN._append(row, STATUS_CSV, STATUS_TABLE)
        return
    df = EN._read(STATUS_CSV, STATUS_TABLE, STATUS_FIELDS)
    if not df.empty:
        df = df[df["request_id"].astype(str) != str(request_id)]
    df = pd.concat([df, row], ignore_index=True)
    STATUS_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(STATUS_CSV, index=False, encoding="utf-8-sig")


def _line_label(row) -> str:
    svc = str(row.get("service") or "").strip()
    unit = str(row.get("unit") or "").strip()
    price = float(row.get("unit_price") or 0)
    if not svc:
        return ""
    bits = [svc]
    if price:
        bits.append(f"{price:,.0f}{'/' + unit.replace('PHP/', '') if unit else ''}")
    return " — ".join(bits) if len(bits) > 1 else svc


def request_summary() -> pd.DataFrame:
    """One row per request: header fields, services (with unit/price) joined,
    total price, and current status."""
    df = load_requests()
    if df.empty:
        return pd.DataFrame(columns=SUMMARY_FIELDS)
    df = df.copy()
    df["unit_price"] = pd.to_numeric(df.get("unit_price"), errors="coerce").fillna(0.0)
    df["_line_label"] = df.apply(_line_label, axis=1)
    g = df.groupby("request_id", sort=False).agg(
        submitted_at=("submitted_at", "first"),
        requested_by=("requested_by", "first"),
        requestee_name=("requestee_name", "first"),
        company_name=("company_name", "first"),
        addressee=("addressee", "first"),
        designation=("designation", "first"),
        company_address=("company_address", "first"),
        contact_email=("contact_email", "first"),
        services=("_line_label", lambda s: "; ".join(x for x in s if x)),
        total_price=("unit_price", "sum"),
    ).reset_index()
    statuses = load_statuses()
    g["status"] = g["request_id"].map(lambda k: statuses.get(str(k), "Pending"))
    return g.sort_values("submitted_at", ascending=False).reset_index(drop=True)


def request_lines(request_id: str) -> pd.DataFrame:
    """Line-level services (with unit/price) for one request."""
    df = load_requests()
    if df.empty:
        return df
    sub = df[df["request_id"].astype(str) == str(request_id)].sort_values("line_no")
    cols = {"line_no": "Line", "service": "Service", "unit": "Unit", "unit_price": "Unit Price"}
    return sub.rename(columns=cols)[list(cols.values())].reset_index(drop=True)
