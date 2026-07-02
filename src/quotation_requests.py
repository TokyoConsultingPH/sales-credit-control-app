"""Quotation requests submitted by employees.

An employee requests that a quotation be prepared for a prospective client,
giving the header details (who to address it to) and the list of services to
be quoted. This sits upstream of 'Report Ordered Quotation' — a request here
has no price yet; once the quotation is prepared and signed, it gets logged
on the Report Ordered Quotation page.

Storage mirrors the rest of the app: local CSV now, PostgreSQL when
DATABASE_URL is set (via src.engagements' generic _read/_append helpers).
Status and the (often-assigned-later) quotation number are editable after
submission, so they live in small side-tables overlaid on the append-only
request rows — the same pattern as billing_status / sales_overrides.
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
    "quotation_number", "attachments",
]

UNITS = ["PHP/Year", "PHP/Month", "PHP/Spot"]

STATUS_CSV = ROOT / "data" / "quotation_request_status.csv"
STATUS_TABLE = "quotation_request_status"
STATUS_FIELDS = ["request_id", "status", "updated_by", "updated_at"]
STATUS_OPTIONS = ["Pending", "In Progress", "Quotation Sent", "Declined"]

QUOTNUM_CSV = ROOT / "data" / "quotation_request_qnum.csv"
QUOTNUM_TABLE = "quotation_request_qnum"
QUOTNUM_FIELDS = ["request_id", "quotation_number", "updated_by", "updated_at"]

SUMMARY_FIELDS = [
    "request_id", "submitted_at", "requested_by", "requestee_name", "company_name",
    "addressee", "designation", "company_address", "contact_email", "services",
    "total_price", "quotation_number", "attachments", "status",
]


def new_request_id() -> str:
    return f"QR-{datetime.now():%Y%m%d%H%M%S%f}"


def save_request(*, request_id: str | None = None, requested_by, requestee_name,
                 company_name, addressee, designation, company_address, contact_email,
                 services: list[dict], quotation_number: str = "",
                 attachments: str = "") -> str:
    """Save one quotation request (one row per service line). Returns the request_id.

    Each item in `services` is a dict with keys: service, unit, unit_price.
    Pass `request_id` (from new_request_id()) when attachments need to be saved
    under the same id before calling this.
    """
    request_id = request_id or new_request_id()
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
        "quotation_number": (quotation_number or "").strip(),
        "attachments": attachments or "",
    } for i, svc in enumerate(services, start=1)]
    EN._append(pd.DataFrame(rows, columns=FIELDS), CSV_PATH, TABLE)
    set_status(request_id, "Pending", requested_by)
    if (quotation_number or "").strip():
        set_quotation_number(request_id, quotation_number, requested_by)
    return request_id


def load_requests() -> pd.DataFrame:
    df = EN._read(CSV_PATH, TABLE, FIELDS)
    if not df.empty and "submitted_at" in df.columns:
        df = df.sort_values("submitted_at", ascending=False)
    return df.reset_index(drop=True)


def _load_side_table(csv_path: Path, table: str, fields: list[str], value_col: str) -> dict:
    df = EN._read(csv_path, table, fields)
    if df.empty:
        return {}
    df = df.sort_values("updated_at")
    return dict(zip(df["request_id"].astype(str), df[value_col].astype(str)))


def _set_side_value(csv_path: Path, table: str, fields: list[str], value_col: str,
                    request_id: str, value: str, user: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = pd.DataFrame([{"request_id": request_id, value_col: value,
                         "updated_by": user, "updated_at": now}], columns=fields)
    if EN._use_db():
        if EN._table_exists(table):
            from sqlalchemy import text
            with EN._engine().begin() as conn:
                conn.execute(text(f'DELETE FROM {table} WHERE request_id = :r'),
                             {"r": request_id})
        EN._append(row, csv_path, table)
        return
    df = EN._read(csv_path, table, fields)
    if not df.empty:
        df = df[df["request_id"].astype(str) != str(request_id)]
    df = pd.concat([df, row], ignore_index=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")


def load_statuses() -> dict:
    return _load_side_table(STATUS_CSV, STATUS_TABLE, STATUS_FIELDS, "status")


def set_status(request_id: str, status: str, user: str = "") -> None:
    _set_side_value(STATUS_CSV, STATUS_TABLE, STATUS_FIELDS, "status", request_id, status, user)


def load_quotation_numbers() -> dict:
    return _load_side_table(QUOTNUM_CSV, QUOTNUM_TABLE, QUOTNUM_FIELDS, "quotation_number")


def set_quotation_number(request_id: str, quotation_number: str, user: str = "") -> None:
    _set_side_value(QUOTNUM_CSV, QUOTNUM_TABLE, QUOTNUM_FIELDS, "quotation_number",
                    request_id, quotation_number, user)


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
    total price, quotation number, attachments, and current status."""
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
        quotation_number=("quotation_number", "first"),
        attachments=("attachments", "first"),
    ).reset_index()
    g["quotation_number"] = g["quotation_number"].fillna("")
    g["attachments"] = g["attachments"].fillna("")
    qnums = load_quotation_numbers()
    g["quotation_number"] = [
        qnums.get(str(rid), qn) for rid, qn in zip(g["request_id"], g["quotation_number"])
    ]
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
