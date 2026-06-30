"""Generate formatted Excel reports (overall + one sheet per department)."""
from __future__ import annotations

import io
import pandas as pd

from . import monitoring as M


def build_excel_report(df: pd.DataFrame, cfg: dict, as_of: pd.Timestamp | None = None) -> bytes:
    """Return an .xlsx workbook (as bytes) with summary, monitoring and
    per-department sheets."""
    as_of = as_of or pd.Timestamp.today().normalize()
    buf = io.BytesIO()

    summary = M.department_summary(df, cfg)
    trends = M.trend_metrics(df)
    aging = M.ar_aging(df, cfg, as_of)
    overdue = M.overdue_detail(df, cfg, as_of)
    billing = M.due_for_billing(df, cfg, as_of)
    alerts = pd.DataFrame(M.build_alerts(df, cfg, as_of))

    with pd.ExcelWriter(buf, engine="xlsxwriter") as xw:
        book = xw.book
        money = book.add_format({"num_format": "#,##0.00"})
        pct = book.add_format({"num_format": "0.0"})
        header = book.add_format({"bold": True, "bg_color": "#1F4E78",
                                  "font_color": "white", "border": 1})
        title = book.add_format({"bold": True, "font_size": 14})

        used: set[str] = set()

        def unique_name(name: str) -> str:
            base = name[:31]
            cand, n = base, 1
            while cand.lower() in used:
                suffix = f"~{n}"
                cand = base[:31 - len(suffix)] + suffix
                n += 1
            used.add(cand.lower())
            return cand

        def write_sheet(name, frame, money_cols=(), pct_cols=()):
            frame = frame.copy()
            sheet_name = unique_name(name)
            frame.to_excel(xw, sheet_name=sheet_name, index=False, startrow=1)
            ws = xw.sheets[sheet_name]
            ws.write(0, 0, name, title)
            for c, col in enumerate(frame.columns):
                ws.write(1, c, col, header)
                max_len = frame[col].astype(str).str.len().max()
                max_len = int(max_len) if pd.notna(max_len) else 12
                width = max(12, min(40, max_len + 2))
                fmt = money if col in money_cols else (pct if col in pct_cols else None)
                ws.set_column(c, c, width, fmt)
            ws.freeze_panes(2, 0)

        write_sheet("Department Summary", summary,
                    money_cols=("Invoiced", "Received", "Outstanding", "Target", "Gap to Target"),
                    pct_cols=("Attainment %", "Collection %"))
        if not alerts.empty:
            write_sheet("Alerts", alerts)
        write_sheet("Trends", trends, money_cols=("Invoiced",), pct_cols=("MoM %", "YoY %"))
        write_sheet("AR Aging", aging, money_cols=[c for c in aging.columns if c != "Department"])
        write_sheet("Overdue AR", overdue, money_cols=("Outstanding",))
        write_sheet("Due for Billing", billing)

        # One detail sheet per department.
        for dept, sub in df.groupby("department"):
            detail = sub.rename(columns={
                "date": "Date", "client": "Client", "engagement": "Engagement",
                "manager": "Manager", "invoiced": "Invoiced", "received": "Received",
                "outstanding": "Outstanding", "due_date": "DueDate",
                "next_billing_date": "NextBillingDate", "status": "Status",
            })[["Date", "Client", "Engagement", "Manager", "Invoiced", "Received",
                "Outstanding", "DueDate", "NextBillingDate", "Status"]]
            sheet = f"Dept-{dept}"[:31]
            write_sheet(sheet, detail, money_cols=("Invoiced", "Received", "Outstanding"))

    buf.seek(0)
    return buf.getvalue()
