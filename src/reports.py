"""Generate formatted Excel reports (overall + one sheet per department)."""
from __future__ import annotations

import io
import pandas as pd

from . import monitoring as M


def _write_matrix(xw, book, sheet: str, matrix: pd.DataFrame, id_cols: list[str],
                  month_cols: list[str]) -> None:
    """Write a month-by-month matrix sheet (credit-control style)."""
    money = book.add_format({"num_format": "#,##0", "border": 1})
    idfmt = book.add_format({"border": 1})
    header = book.add_format({"bold": True, "bg_color": "#1F4E78", "font_color": "white",
                              "border": 1, "align": "center"})
    totfmt = book.add_format({"num_format": "#,##0", "bold": True, "bg_color": "#DDEBF7", "border": 1})
    title = book.add_format({"bold": True, "font_size": 14})

    ws = xw.book.add_worksheet(sheet[:31])
    xw.sheets[sheet[:31]] = ws
    ws.write(0, 0, sheet, title)
    cols = id_cols + month_cols + ["Total"]
    for c, name in enumerate(cols):
        ws.write(2, c, name, header)
    for r, (_, row) in enumerate(matrix.iterrows(), start=3):
        for c, name in enumerate(cols):
            val = row.get(name)
            if name in id_cols:
                ws.write(r, c, "" if pd.isna(val) else str(val), idfmt)
            elif name == "Total":
                ws.write_number(r, c, float(val or 0), totfmt)
            else:
                ws.write_number(r, c, float(val or 0), money)
    # column widths
    for c, name in enumerate(id_cols):
        width = max(14, min(40, int(matrix[name].astype(str).str.len().max() or 14) + 2))
        ws.set_column(c, c, width)
    ws.set_column(len(id_cols), len(cols) - 1, 12)
    ws.freeze_panes(3, len(id_cols))


def build_monthly_matrix_excel(df: pd.DataFrame, cfg: dict) -> bytes:
    """Month-by-month sales matrix, mirroring the Credit Control layout:
    rows = engagements (and a by-department sheet), columns = each month."""
    buf = io.BytesIO()
    d = df.copy()
    d = d[d["date"].notna()]
    d["invoiced"] = pd.to_numeric(d["invoiced"], errors="coerce").fillna(0.0)
    d["received"] = pd.to_numeric(d.get("received"), errors="coerce").fillna(0.0)

    if d.empty:
        with pd.ExcelWriter(buf, engine="xlsxwriter") as xw:
            pd.DataFrame({"note": ["No dated sales to plot."]}).to_excel(xw, index=False)
        buf.seek(0)
        return buf.getvalue()

    d["Month"] = d["date"].dt.to_period("M").dt.to_timestamp()
    months = sorted(d["Month"].unique())
    month_cols = [pd.Timestamp(m).strftime("%Y-%m") for m in months]
    d["MonthLabel"] = d["Month"].dt.strftime("%Y-%m")

    def matrix_by(index_cols: list[str], value: str) -> pd.DataFrame:
        piv = pd.pivot_table(d, index=index_cols, columns="MonthLabel", values=value,
                             aggfunc="sum", fill_value=0.0)
        for mc in month_cols:
            if mc not in piv.columns:
                piv[mc] = 0.0
        piv = piv[month_cols]
        piv["Total"] = piv.sum(axis=1)
        piv = piv.reset_index()
        return piv.sort_values("Total", ascending=False)

    eng = matrix_by(["client", "engagement", "department"], "invoiced").rename(
        columns={"client": "Client", "engagement": "Engagement", "department": "Department"})
    dept = matrix_by(["department"], "invoiced").rename(columns={"department": "Department"})
    coll = matrix_by(["department"], "received").rename(columns={"department": "Department"})

    with pd.ExcelWriter(buf, engine="xlsxwriter") as xw:
        book = xw.book
        _write_matrix(xw, book, "Monthly Sales by Dept", dept, ["Department"], month_cols)
        _write_matrix(xw, book, "Monthly Collections by Dept", coll, ["Department"], month_cols)
        _write_matrix(xw, book, "Monthly Sales by Engagement", eng,
                      ["Client", "Engagement", "Department"], month_cols)
    buf.seek(0)
    return buf.getvalue()


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
