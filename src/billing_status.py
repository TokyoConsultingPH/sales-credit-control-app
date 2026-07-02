"""Editable billing-status overrides for the due-for-billing register.

The workbook is read-only, so status changes are stored here (keyed by a stable
row key) and overlaid on top of the workbook-derived status. Local CSV now,
PostgreSQL when DATABASE_URL is set.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import pandas as pd

from src import engagements as EN

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "data" / "billing_status.csv"
TABLE = "billing_status"
FIELDS = ["key", "status", "updated_by", "updated_at"]

STATUS_FLOW = ["Ordered", "Can be invoiced", "Invoiced", "Collected"]


def flow_status(text) -> str:
    """Normalise a raw workbook status into the 4-step billing workflow."""
    s = str(text or "").lower().strip()
    if "collect" in s or s.startswith("5"):
        return "Collected"
    if "can be invoiced" in s or "can invoice" in s or s.startswith("2"):
        return "Can be invoiced"
    if "invoiced" in s or "sent" in s or s.startswith(("3", "4")):
        return "Invoiced"
    return "Ordered"


def load_overrides() -> dict:
    df = EN._read(CSV_PATH, TABLE, FIELDS)
    if df.empty:
        return {}
    if "updated_at" in df.columns:
        df = df.sort_values("updated_at")
    return dict(zip(df["key"].astype(str), df["status"].astype(str)))


def set_status(key: str, status: str, user: str = "") -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = pd.DataFrame([{"key": key, "status": status, "updated_by": user,
                         "updated_at": now}], columns=FIELDS)
    if EN._use_db():
        if EN._table_exists(TABLE):
            from sqlalchemy import text
            with EN._engine().begin() as conn:
                conn.execute(text(f'DELETE FROM {TABLE} WHERE "key" = :k'), {"k": key})
        EN._append(row, CSV_PATH, TABLE)
        return
    df = EN._read(CSV_PATH, TABLE, FIELDS)
    if not df.empty:
        df = df[df["key"].astype(str) != str(key)]
    df = pd.concat([df, row], ignore_index=True)
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(CSV_PATH, index=False, encoding="utf-8-sig")
