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
    "completed_at", "completed_by", "engagement_key", "quotation_number", "line_no",
    "type_of_service", "service_description", "company", "branch",
    "engagement_fee", "final_amount", "completion_date", "notes", "attachments",
]
NOTIF_FIELDS = [
    "notif_id", "created_at", "type", "engagement_key", "quotation_number", "line_no",
    "company", "service", "amount", "due_month", "status", "message", "emailed",
    "billed_at", "attachments",
]

FINAL_SHARE = 0.5


def _use_db() -> bool:
    return bool(os.getenv("DATABASE_URL"))


def _engine():
    from src.db_loader import make_engine
    return make_engine({}, None, None)


def _engagement_key(quotation_number: str, company: str, order_date: str, line_no) -> str:
    """A key unique to one service line (engagement) within a quotation."""
    q = str(quotation_number or "").strip() or f"{str(company).strip()} @ {str(order_date).strip()}"
    return f"{q} #L{str(line_no).strip() if line_no is not None else '?'}"


def normalize_month(s) -> str:
    """Return a 'YYYY-MM' month string from a date/text, or '' if unparseable."""
    import re
    if s is None:
        return ""
    txt = str(s).strip()
    if not txt:
        return ""
    m = re.search(r"(\d{4})[-/](\d{1,2})", txt)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    d = pd.to_datetime(txt, errors="coerce")
    return d.strftime("%Y-%m") if pd.notna(d) else ""


# --------------------------------------------------------------------------- #
# Generic append / read (CSV or DB)
# --------------------------------------------------------------------------- #
def _table_exists(table: str) -> bool:
    """True if `table` exists in the DB. Only meaningful when _use_db() is True."""
    from sqlalchemy import text
    with _engine().connect() as conn:
        return bool(conn.execute(text(f"SELECT to_regclass('public.{table}')")).scalar())


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
PENDING_COLUMNS = ["engagement_key", "quotation_number", "line_no", "company", "branch",
                   "type_of_service", "service_description", "engagement_fee", "final_amount"]


def pending_engagements() -> pd.DataFrame:
    """One row per service line (engagement) not yet marked complete, with that
    engagement's fee and its computed final-50% amount."""
    qdf = Q.load_quotations()
    if qdf.empty:
        return pd.DataFrame(columns=PENDING_COLUMNS)
    qdf = qdf.copy()
    qdf["engagement_fee"] = pd.to_numeric(qdf.get("price"), errors="coerce").fillna(0.0)
    qdf["engagement_key"] = [
        _engagement_key(q, c, o, ln) for q, c, o, ln in
        zip(qdf.get("quotation_number", ""), qdf.get("company", ""),
            qdf.get("order_date", ""), qdf.get("line_no", ""))
    ]
    out = qdf.drop_duplicates("engagement_key").copy()
    out["final_amount"] = (out["engagement_fee"] * FINAL_SHARE).round(2)
    out = out.rename(columns={"line_no": "line_no", "type_of_service": "type_of_service",
                              "service_description": "service_description"})
    out = out[[c for c in PENDING_COLUMNS if c in out.columns]]

    done = set(_read(COMPLETIONS_CSV, COMPLETIONS_TABLE, COMPLETION_FIELDS)
               .get("engagement_key", pd.Series(dtype=str)).astype(str))
    out = out[~out["engagement_key"].astype(str).isin(done)]
    return out.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Complete an engagement -> raise a billing notification
# --------------------------------------------------------------------------- #
def complete_engagement(*, completed_by, engagement_key, quotation_number, line_no,
                        type_of_service, service_description, company, branch,
                        engagement_fee, final_amount, completion_date, notes,
                        attachments="") -> dict:
    """Record completion of one engagement (service line) and create a pending
    final-50% notification. `attachments` is a '; '-joined list of stored
    Notice-of-Completion filenames. Returns the notification dict."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    qn = str(quotation_number or "").strip()
    svc = (type_of_service or "").strip() or "service"
    completion = {
        "completed_at": now, "completed_by": (completed_by or "").strip(),
        "engagement_key": engagement_key, "quotation_number": qn, "line_no": line_no,
        "type_of_service": svc, "service_description": (service_description or "").strip(),
        "company": company, "branch": branch, "engagement_fee": float(engagement_fee or 0),
        "final_amount": float(final_amount or 0),
        "completion_date": str(completion_date or ""), "notes": (notes or "").strip(),
        "attachments": attachments or "",
    }
    _append(pd.DataFrame([completion], columns=COMPLETION_FIELDS),
            COMPLETIONS_CSV, COMPLETIONS_TABLE)

    where = f"{qn or 'no quotation no.'} line {line_no}"
    desc = f" — {completion['service_description']}" if completion['service_description'] else ""
    notif = {
        "notif_id": f"BILL-{datetime.now():%Y%m%d%H%M%S}-{str(engagement_key)[:16]}",
        "created_at": now, "type": "final_50_billing", "engagement_key": engagement_key,
        "quotation_number": qn, "line_no": line_no, "company": company, "service": svc,
        "amount": float(final_amount or 0),
        "due_month": normalize_month(completion_date) or datetime.now().strftime("%Y-%m"),
        "status": "pending",
        "message": f"Final 50% billing due: {company} — {svc}{desc} — "
                   f"{float(final_amount or 0):,.0f} ({where})",
        "emailed": False, "billed_at": "", "attachments": attachments or "",
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


def has_notification(engagement_key: str, ntype: str) -> bool:
    df = load_notifications()
    if df.empty:
        return False
    return bool(((df["engagement_key"].astype(str) == str(engagement_key))
                 & (df["type"].astype(str) == ntype)).any())


def raise_initial_billing(*, engagement_key, quotation_number, line_no, company,
                          service, amount, due_month="", created_by="") -> dict | None:
    """Flag the initial 50% of an ordered engagement as due for billing.
    Skips if an initial-50% notification already exists for this engagement."""
    if has_notification(engagement_key, "initial_50_billing"):
        return None
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    qn = str(quotation_number or "").strip()
    svc = (service or "").strip() or "service"
    dm = normalize_month(due_month) or now[:7]
    notif = {
        "notif_id": f"INIT-{datetime.now():%Y%m%d%H%M%S%f}-{str(engagement_key)[:12]}",
        "created_at": now, "type": "initial_50_billing", "engagement_key": engagement_key,
        "quotation_number": qn, "line_no": line_no, "company": company, "service": svc,
        "amount": float(amount or 0), "due_month": dm, "status": "pending",
        "message": f"Initial 50% billing due: {company} — {svc} — "
                   f"{float(amount or 0):,.0f} ({qn or 'no quotation no.'} line {line_no})",
        "emailed": False, "billed_at": "", "attachments": "",
    }
    _append(pd.DataFrame([notif], columns=NOTIF_FIELDS), NOTIF_CSV, NOTIF_TABLE)
    return notif


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
