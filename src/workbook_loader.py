"""Parser for the TCF 'New-credit control [Philippines]' workbook.

The yearly 'Credit Control YYYY' sheets store one engagement per row with a
two-row header (English / Japanese) followed by 12 monthly blocks of 8
sub-columns each:  [Collect, Send, Invoiced, Complete, Fee, JPY, status, Advance].

This module un-pivots those blocks into a tidy "billing rows" DataFrame
(one row per engagement-month with a fee) and normalises the messy
branch / category / status values.
"""
from __future__ import annotations

import re
from pathlib import Path
import pandas as pd

# --------------------------------------------------------------------------- #
# Normalisation helpers
# --------------------------------------------------------------------------- #
_BRANCH_MAP = {"MAKATI": "Makati", "CEBU": "Cebu", "AMP": "AMP"}

_CATEGORY_FIXES = {
    "monthly accountng": "Monthly Accounting",
    "accounting spot": "Accounting Spot",
    "audit annual": "Annual Audit",
    "annual accounting": "Accounting Annual",
}


def _norm_branch(v) -> str:
    if pd.isna(v):
        return "Unknown"
    s = str(v).strip()
    return _BRANCH_MAP.get(s.upper(), s.title())


def _norm_category(v) -> str:
    if pd.isna(v) or str(v).strip() == "":
        return "Uncategorised"
    s = re.sub(r"\s+", " ", str(v).strip())
    return _CATEGORY_FIXES.get(s.lower(), s.title())


def _norm_classification(v) -> str:
    if pd.isna(v):
        return "Unknown"
    return str(v).strip().title()


def _norm_status(v) -> str:
    """Collapse the free-text overall status into a few buckets."""
    if pd.isna(v):
        return "Unknown"
    s = str(v).lower()
    if "cancel" in s:
        return "Cancelled"
    if "hold" in s:
        return "On Hold"
    if "complet" in s or "done" in s or "reported" in s:
        return "Completed"
    if "ongoing" in s or "on-going" in s or "on going" in s:
        return "Ongoing"
    if "active" in s:
        return "Active"
    return str(v).strip().title()[:30]


def _to_num(v) -> float:
    if pd.isna(v):
        return 0.0
    s = re.sub(r"[,\s₱]", "", str(v))
    try:
        return float(s)
    except ValueError:
        return 0.0


def _flag_ok(v) -> bool:
    if pd.isna(v):
        return False
    return str(v).strip().lower() in {"ok", "yes", "y", "done", "true", "1"}


# --------------------------------------------------------------------------- #
# Sheet parsing
# --------------------------------------------------------------------------- #
_IDENTITY = {
    "ordered date": "ordered_date",
    "quotation number": "quotation",
    "company": "company",
    "content": "content",
    "total service fee": "total_fee",
    "status": "status",
    "billed?": "billed",
    "branch": "branch",
    "classification": "classification",
    "category": "category",
    "pic": "pic",
    "estimated completion date": "est_completion",
}


def _find_header_row(raw: pd.DataFrame) -> int | None:
    for i in range(min(25, len(raw))):
        row = raw.iloc[i].astype(str).str.strip().str.lower()
        if (row == "company").any() and row.str.contains("service fee").any():
            return i
    return None


def _identity_cols(en_row: pd.Series) -> dict[str, int]:
    cols: dict[str, int] = {}
    for idx, val in en_row.items():
        key = str(val).strip().lower()
        if key in _IDENTITY and _IDENTITY[key] not in cols:
            cols[_IDENTITY[key]] = idx
    return cols


def _classify_month(amount, collect, invoiced, sent, status_txt) -> tuple[bool, bool, bool]:
    """Return (is_invoiced, is_collected, can_invoice) for a month cell.

    Status workflow ladder: 2 = can be invoiced (due for billing),
    3 = invoiced, 4 = sent, 5 = collected.
    """
    s = str(status_txt).lower().strip() if pd.notna(status_txt) else ""
    collected = _flag_ok(collect) or "collected" in s or s.startswith("5")
    # 'can be invoiced' must be checked first because it contains 'invoiced'.
    can_invoice = "can be invoiced" in s or "can invoice" in s or s.startswith("2")
    invoiced_f = (not can_invoice) and (
        _flag_ok(invoiced) or _flag_ok(sent) or "invoiced" in s or "sent" in s
        or s.startswith(("3", "4"))
    )
    if collected:
        invoiced_f, can_invoice = True, False
    return invoiced_f, collected, can_invoice


def parse_year_sheet(raw: pd.DataFrame, year: int) -> pd.DataFrame:
    """Un-pivot one 'Credit Control YYYY' sheet into tidy billing rows."""
    hr = _find_header_row(raw)
    if hr is None:
        return pd.DataFrame()
    en_row = raw.iloc[hr]
    sub_row = raw.iloc[hr + 1].astype(str).str.strip().str.lower()
    ident = _identity_cols(en_row)
    if "company" not in ident:
        return pd.DataFrame()

    # Month blocks: every column whose sub-header == 'fee' marks a block.
    fee_cols = [c for c, v in sub_row.items() if v == "fee"]

    data = raw.iloc[hr + 2:].reset_index(drop=True)
    data = data[data[ident["company"]].notna()]  # rows with a company

    def g(col_key):
        return data[ident[col_key]] if col_key in ident else pd.Series([None] * len(data))

    base = pd.DataFrame({
        "year": year,
        "company": g("company").astype(str).str.strip(),
        "content": g("content").astype(str).str.strip(),
        "quotation": g("quotation").astype(str).str.strip(),
        "total_fee": g("total_fee").map(_to_num),
        "status_overall": g("status").map(_norm_status),
        "billed": g("billed").astype(str).str.strip(),
        "branch": g("branch").map(_norm_branch),
        "classification": g("classification").map(_norm_classification),
        "category": g("category").map(_norm_category),
        "pic": g("pic").fillna("Unassigned").astype(str).str.strip().replace({"nan": "Unassigned", "": "Unassigned"}),
    }).reset_index(drop=True)

    rows = []
    for fee_c in fee_cols:
        # Block layout relative to the Fee column.
        date_c = fee_c - 4      # 'Collect' col, holds the month date in header
        invoiced_c = fee_c - 2
        sent_c = fee_c - 3
        complete_c = fee_c - 1
        status_c = fee_c + 2
        month_val = en_row.get(date_c)
        month = pd.to_datetime(month_val, errors="coerce")
        if pd.isna(month):
            continue

        fee = data[fee_c].map(_to_num).reset_index(drop=True)
        collect = data[date_c].reset_index(drop=True)
        invoiced = data[invoiced_c].reset_index(drop=True)
        sent = data[sent_c].reset_index(drop=True)
        status_txt = data[status_c].reset_index(drop=True)

        for i in range(len(data)):
            amt = fee.iloc[i]
            st = status_txt.iloc[i]
            has_activity = amt > 0 or (pd.notna(st) and str(st).strip() != "")
            if not has_activity:
                continue
            inv, col, can = _classify_month(amt, collect.iloc[i], invoiced.iloc[i], sent.iloc[i], st)
            r = base.iloc[i].to_dict()
            r.update({
                "date": month,
                "fee": amt,
                "invoiced": inv,
                "collected": col,
                "can_invoice": can,
                "month_status": str(st).strip() if pd.notna(st) else "",
                "invoiced_amt": amt if inv else 0.0,
                "collected_amt": amt if col else 0.0,
                "outstanding": amt if (inv and not col) else 0.0,
                "due_billing_amt": amt if (can and not inv) else 0.0,
            })
            rows.append(r)

    return pd.DataFrame(rows)


def load_workbook(path: str | Path, years: list[int] | None = None) -> pd.DataFrame:
    """Load and combine all 'Credit Control YYYY' sheets into tidy billing rows."""
    path = Path(path)
    xl = pd.ExcelFile(path)
    frames = []
    for sheet in xl.sheet_names:
        m = re.fullmatch(r"Credit Control (\d{4})", sheet.strip())
        if not m:
            continue
        yr = int(m.group(1))
        if years and yr not in years:
            continue
        raw = xl.parse(sheet, header=None)
        tidy = parse_year_sheet(raw, yr)
        if not tidy.empty:
            frames.append(tidy)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# --------------------------------------------------------------------------- #
# Map tidy billing rows -> canonical schema expected by monitoring.py
# --------------------------------------------------------------------------- #
def to_canonical(tidy: pd.DataFrame, dimension: str) -> pd.DataFrame:
    """`dimension` is one of: branch, category, pic, classification."""
    if tidy.empty:
        return tidy
    out = pd.DataFrame({
        "date": tidy["date"],
        "department": tidy[dimension].fillna("Unknown").astype(str),
        "client": tidy["company"],
        "engagement": tidy["content"].where(tidy["content"].astype(bool), tidy["quotation"]),
        "manager": tidy["pic"],
        "invoiced": tidy["invoiced_amt"],
        "received": tidy["collected_amt"],
        "due_date": tidy["date"],          # billing month doubles as invoice date for aging
        "status": tidy["month_status"],
        "outstanding": tidy["outstanding"],
    })
    # Carry Branch + service Category through for Branch/Department columns.
    out["branch"] = tidy["branch"]
    out["category"] = tidy["category"]
    out["amount"] = tidy["fee"]        # per-engagement fee for that month
    # 'due for billing' months: surface the month as the next billing date.
    out["next_billing_date"] = tidy["date"].where(tidy["due_billing_amt"] > 0)
    return out
