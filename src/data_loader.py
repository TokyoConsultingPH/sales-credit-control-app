"""Read an Excel/CSV file and normalise it to the canonical schema.

Canonical columns (after loading):
    date, department, client, engagement, manager,
    invoiced, received, due_date, next_billing_date, status
Plus derived: outstanding
"""
from __future__ import annotations

from pathlib import Path
import io
import pandas as pd

CANONICAL = [
    "date", "department", "client", "engagement", "manager",
    "invoiced", "received", "due_date", "next_billing_date", "status",
]
NUMERIC = ["invoiced", "received"]
DATES = ["date", "due_date", "next_billing_date"]


def _read_raw(source, filename: str | None = None) -> pd.DataFrame:
    """Read a CSV or Excel file from a path or an uploaded file-like object."""
    name = (filename or getattr(source, "name", "") or str(source)).lower()
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        return pd.read_excel(source)
    # default to CSV
    if hasattr(source, "read"):
        data = source.read()
        if isinstance(data, bytes):
            data = data.decode("utf-8-sig")
        return pd.read_csv(io.StringIO(data))
    return pd.read_csv(source, encoding="utf-8-sig")


def load_data(source, cfg: dict, filename: str | None = None) -> pd.DataFrame:
    """Load `source` and return a cleaned DataFrame in the canonical schema."""
    raw = _read_raw(source, filename)
    colmap = cfg.get("columns", {})

    # Build canonical frame by pulling each mapped source column.
    out = pd.DataFrame()
    for canon in CANONICAL:
        src_col = colmap.get(canon, "")
        if src_col and src_col in raw.columns:
            out[canon] = raw[src_col]
        else:
            out[canon] = pd.NA

    # Coerce types.
    for col in NUMERIC:
        out[col] = (
            out[col].astype(str)
            .str.replace(r"[,₱$PHP\s]", "", regex=True)
            .replace({"<NA>": None, "nan": None, "": None})
        )
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0)

    for col in DATES:
        out[col] = pd.to_datetime(out[col], errors="coerce")

    out["department"] = out["department"].fillna("Unassigned").astype(str).str.strip()
    out["status"] = out["status"].fillna("").astype(str).str.strip()
    out["client"] = out["client"].fillna("Unknown").astype(str).str.strip()

    # Derived: outstanding receivable.
    out["outstanding"] = (out["invoiced"] - out["received"]).clip(lower=0)

    return out


def validate(df: pd.DataFrame) -> list[str]:
    """Return a list of human-readable warnings about the loaded data."""
    warnings: list[str] = []
    if df.empty:
        warnings.append("No rows were loaded.")
        return warnings
    if df["invoiced"].sum() == 0:
        warnings.append("All 'invoiced' values are 0 — check the column mapping in config/settings.yaml.")
    if df["date"].isna().all():
        warnings.append("No valid 'date' values — trend reports will be empty. Check the date column mapping.")
    if (df["department"] == "Unassigned").all():
        warnings.append("Every row is 'Unassigned' — check the 'department' column mapping.")
    return warnings
