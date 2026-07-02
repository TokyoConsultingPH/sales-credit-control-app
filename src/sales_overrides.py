"""Per-engagement overrides for the Sales Database editable fields.

Lets users correct/standardise Branch, Classification, Category, PIC and Status
without touching the read-only workbook. Stored long (one row per field change),
local CSV now / PostgreSQL when DATABASE_URL is set, and overlaid on load.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import pandas as pd

from src import engagements as EN

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "data" / "sales_overrides.csv"
TABLE = "sales_overrides"
FIELDS = ["key", "field", "value", "updated_by", "updated_at"]
EDITABLE = ["Branch", "Classification", "Category", "PIC", "Status"]


def make_key(year, company, engagement) -> str:
    return f"{year}|{str(company).strip()}|{str(engagement).strip()}"


def load_overrides() -> dict:
    df = EN._read(CSV_PATH, TABLE, FIELDS)
    if df.empty:
        return {}
    df = df.sort_values("updated_at")
    out: dict = {}
    for _, r in df.iterrows():
        out.setdefault(str(r["key"]), {})[str(r["field"])] = r["value"]
    return out


def set_override(key: str, field: str, value, user: str = "") -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = pd.DataFrame([{"key": key, "field": field, "value": value,
                         "updated_by": user, "updated_at": now}], columns=FIELDS)
    if EN._use_db():
        if EN._table_exists(TABLE):
            from sqlalchemy import text
            with EN._engine().begin() as conn:
                conn.execute(text(f'DELETE FROM {TABLE} WHERE "key"=:k AND field=:f'),
                             {"k": key, "f": field})
        EN._append(row, CSV_PATH, TABLE)
        return
    df = EN._read(CSV_PATH, TABLE, FIELDS)
    if not df.empty:
        df = df[~((df["key"].astype(str) == key) & (df["field"].astype(str) == field))]
    df = pd.concat([df, row], ignore_index=True)
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(CSV_PATH, index=False, encoding="utf-8-sig")
