"""Engagement completion + final-50% billing notifications.

An employee marks a logged quotation as complete; this records the completion
and raises a billing notification for the final 50% (half of the total fee).
Notifications appear in-app and can be emailed. Storage mirrors quotations:
local CSV now, PostgreSQL when DATABASE_URL is set.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
import pandas as pd

from src import quotations as Q

ROOT = Path(__file__).resolve().parent.parent
COMPLETIONS_CSV = ROOT / "data" / "completions.csv"
NOTIF_CSV = ROOT / "data" / "notifications.csv"
COMPLETIONS_TABLE = "engagement_completions"
NOTIF_TABLE = "billing_notifications"

COMPLETION_FIELDS = [
    "completed_at", "completed_by", "engagement_key", "quotation_number",
    "company", "branch", "total_fee", "final_amount", "completion_date", "notes",
]
NOTIF_FIELDS = [
    "notif_id", "created_at", "type", "engagement_key", "quotation_number",
    "company", "amount", "status", "message", "emailed", "billed_at",
]

FINAL_SHARE = 0.5


def _use_db() -> bool:
    return bool(os.getenv("DATABASE_URL"))


def _engine():
    from src.db_loader import make_engine
    return make_engine({}, None, None)


def _engagement_key(quotation_number: str, company: str, order_date: str) -> str:
    q = str(quotation_number or "").strip()
    return q if q else f"{str(company).strip()} @ {str(order_date).strip()}"


# --------------------------------------------------------------------------- #
# Generic append / read (CSV or DB)
# --------------------------------------------------------------------------- #
def _append(df: pd.DataFrame, csv_path: Path, table: str) -> None:
    if _use_db():
        df.to_sql(table, _engine(), if_exists="append", index=False)
        return
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    header = not csv_path.exists()
    df.to_csv(csv_path, mode="a", header=header, index=False, encoding="utf-8-sig")


def _read(csv_path: Path, table: str, fields: list[str]) -> pd.DataFrame:
    try:
        if _use_db():
            from sqlalchemy import text
            with _engine().connect() as conn:
                if not conn.execute(text(f"SELECT to_regclass('public.{table}')")).scalar():
                    return pd.DataFrame(columns=fields)
                return pd.read_sql(f"SELECT * FROM {table}", conn)
        if csv_path.exists():
            return pd.read_csv(csv_path, encoding="utf-8-sig")
    except Exception:
        pass
    return pd.DataFrame(columns=fields)


# --------------------------------------------------------------------------- #
# Pending engagements (from logged quotations, not yet completed)
# --------------------------------------------------------------------------- #
def pending_engagements() -> pd.DataFrame:
    """One row per logged quotation not yet marked complete, with total fee
    and the computed final-50% amount."""
    qdf = Q.load_quotations()
    if qdf.empty:
        return pd.DataFrame(columns=["engagement_key", "quotation_number", "company",
                                     "branch", "lines", "total_fee", "final_amount"])
    qdf = qdf.copy()
    qdf["price"] = pd.to_numeric(qdf.get("price"), errors="coerce").fillna(0.0)
    qdf["engagement_key"] = [
        _engagement_key(q, c, o) for q, c, o in
        zip(qdf.get("quotation_number", ""), qdf.get("company", ""), qdf.get("order_date", ""))
    ]
    grp = qdf.groupby("engagement_key").agg(
        quotation_number=("quotation_number", "first"),
        company=("company", "first"),
        branch=("branch", "first"),
        lines=("price", "size"),
        total_fee=("price", "sum"),
    ).reset_index()
    grp["final_amount"] = (grp["total_fee"] * FINAL_SHARE).round(2)

    done = set(_read(COMPLETIONS_CSV, COMPLETIONS_TABLE, COMPLETION_FIELDS)
               .get("engagement_key", pd.Series(dtype=str)).astype(str))
    grp = grp[~grp["engagement_key"].astype(str).isin(done)]
    return grp.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Complete an engagement -> raise a billing notification
# --------------------------------------------------------------------------- #
def complete_engagement(*, completed_by, engagement_key, quotation_number, company,
                        branch, total_fee, final_amount, completion_date, notes) -> dict:
    """Record the completion and create a pending final-50% notification.
    Returns the notification dict."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    completion = {
        "completed_at": now, "completed_by": (completed_by or "").strip(),
        "engagement_key": engagement_key, "quotation_number": (quotation_number or "").strip(),
        "company": company, "branch": branch, "total_fee": float(total_fee or 0),
        "final_amount": float(final_amount or 0),
        "completion_date": str(completion_date or ""), "notes": (notes or "").strip(),
    }
    _append(pd.DataFrame([completion], columns=COMPLETION_FIELDS),
            COMPLETIONS_CSV, COMPLETIONS_TABLE)

    notif = {
        "notif_id": f"BILL-{datetime.now():%Y%m%d%H%M%S}-{str(engagement_key)[:12]}",
        "created_at": now, "type": "final_50_billing", "engagement_key": engagement_key,
        "quotation_number": completion["quotation_number"], "company": company,
        "amount": float(final_amount or 0), "status": "pending",
        "message": f"Final 50% billing due: {company} — {float(final_amount or 0):,.0f} "
                   f"({completion['quotation_number'] or 'no quotation no.'})",
        "emailed": False, "billed_at": "",
    }
    _append(pd.DataFrame([notif], columns=NOTIF_FIELDS), NOTIF_CSV, NOTIF_TABLE)
    return notif


def load_notifications(status: str | None = None) -> pd.DataFrame:
    df = _read(NOTIF_CSV, NOTIF_TABLE, NOTIF_FIELDS)
    if not df.empty and status:
        df = df[df["status"].astype(str) == status]
    if not df.empty and "created_at" in df.columns:
        df = df.sort_values("created_at", ascending=False)
    return df.reset_index(drop=True)


def pending_count() -> int:
    return len(load_notifications(status="pending"))


def mark_billed(notif_id: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if _use_db():
        from sqlalchemy import text
        with _engine().begin() as conn:
            conn.execute(text(
                f"UPDATE {NOTIF_TABLE} SET status='billed', billed_at=:t WHERE notif_id=:i"),
                {"t": now, "i": notif_id})
        return
    df = _read(NOTIF_CSV, NOTIF_TABLE, NOTIF_FIELDS)
    if df.empty:
        return
    df = df.astype(object)
    mask = df["notif_id"].astype(str) == str(notif_id)
    df.loc[mask, "status"] = "billed"
    df.loc[mask, "billed_at"] = now
    df.to_csv(NOTIF_CSV, index=False, encoding="utf-8-sig")


def mark_emailed(notif_id: str) -> None:
    if _use_db():
        from sqlalchemy import text
        with _engine().begin() as conn:
            conn.execute(text(f"UPDATE {NOTIF_TABLE} SET emailed=TRUE WHERE notif_id=:i"),
                         {"i": notif_id})
        return
    df = _read(NOTIF_CSV, NOTIF_TABLE, NOTIF_FIELDS)
    if df.empty:
        return
    df = df.astype(object)
    df.loc[df["notif_id"].astype(str) == str(notif_id), "emailed"] = True
    df.to_csv(NOTIF_CSV, index=False, encoding="utf-8-sig")
