"""Reporting and monitoring calculations.

Every function takes the canonical DataFrame from data_loader and the config
dict, and returns plain DataFrames / dicts ready for display or export.
"""
from __future__ import annotations

import pandas as pd
from .config import target_for, normalize_branch, department_for


# --------------------------------------------------------------------------- #
# Sales reporting
# --------------------------------------------------------------------------- #
def department_summary(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """One row per department: billings, collections, AR, target, attainment."""
    g = df.groupby("department", dropna=False).agg(
        Invoiced=("invoiced", "sum"),
        Received=("received", "sum"),
        Outstanding=("outstanding", "sum"),
        Engagements=("engagement", "nunique"),
        Clients=("client", "nunique"),
    ).reset_index().rename(columns={"department": "Department"})

    g["Target"] = g["Department"].map(lambda d: target_for(cfg, d)).astype(float)
    target = g["Target"].where(g["Target"] > 0)
    invoiced = g["Invoiced"].where(g["Invoiced"] > 0)
    g["Attainment %"] = (g["Invoiced"] / target * 100).round(1)
    g["Gap to Target"] = (g["Target"] - g["Invoiced"]).where(g["Target"] > 0)
    g["Collection %"] = (g["Received"] / invoiced * 100).round(1)
    return g.sort_values("Invoiced", ascending=False).reset_index(drop=True)


def monthly_trend(df: pd.DataFrame) -> pd.DataFrame:
    """Billings by month and department (for MoM / YoY trend charts)."""
    d = df.dropna(subset=["date"]).copy()
    if d.empty:
        return pd.DataFrame(columns=["Month", "Department", "Invoiced"])
    d["Month"] = d["date"].dt.to_period("M").dt.to_timestamp()
    t = d.groupby(["Month", "department"])["invoiced"].sum().reset_index()
    return t.rename(columns={"department": "Department", "invoiced": "Invoiced"})


def trend_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """Per-department MoM and YoY growth on the latest month present."""
    t = monthly_trend(df)
    if t.empty:
        return pd.DataFrame(columns=["Department", "Latest Month", "Invoiced", "MoM %", "YoY %"])
    rows = []
    for dept, sub in t.groupby("Department"):
        sub = sub.set_index("Month").sort_index()
        latest = sub.index.max()
        cur = sub.loc[latest, "Invoiced"]
        prev_m = latest - pd.offsets.MonthBegin(1)
        prev_y = latest - pd.offsets.DateOffset(years=1)
        mom = sub["Invoiced"].get(prev_m)
        yoy = sub["Invoiced"].get(prev_y)
        rows.append({
            "Department": dept,
            "Latest Month": latest.strftime("%Y-%m"),
            "Invoiced": cur,
            "MoM %": round((cur - mom) / mom * 100, 1) if mom else None,
            "YoY %": round((cur - yoy) / yoy * 100, 1) if yoy else None,
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Credit control / AR monitoring
# --------------------------------------------------------------------------- #
def ar_aging(df: pd.DataFrame, cfg: dict, as_of: pd.Timestamp | None = None) -> pd.DataFrame:
    """AR aging by department using outstanding amounts and due dates."""
    as_of = as_of or pd.Timestamp.today().normalize()
    buckets = cfg.get("monitoring", {}).get("ar_aging_buckets", [30, 60, 90])

    open_ar = df[df["outstanding"] > 0].copy()
    if open_ar.empty:
        return pd.DataFrame(columns=["Department", "Current"] + _bucket_labels(buckets) + ["Total AR"])

    open_ar["days_past_due"] = (as_of - open_ar["due_date"]).dt.days
    open_ar["bucket"] = open_ar["days_past_due"].apply(lambda d: _bucket_for(d, buckets))

    pivot = open_ar.pivot_table(
        index="department", columns="bucket", values="outstanding", aggfunc="sum", fill_value=0
    )
    order = ["Current"] + _bucket_labels(buckets)
    for col in order:
        if col not in pivot.columns:
            pivot[col] = 0.0
    pivot = pivot[order]
    pivot["Total AR"] = pivot.sum(axis=1)
    return pivot.reset_index().rename(columns={"department": "Department"})


def _bucket_labels(buckets: list[int]) -> list[str]:
    labels = []
    prev = 0
    for b in buckets:
        labels.append(f"{prev + 1}-{b}")
        prev = b
    labels.append(f"{prev}+")
    return labels


def _bucket_for(days, buckets: list[int]) -> str:
    if days is None or pd.isna(days) or days <= 0:
        return "Current"
    prev = 0
    for b in buckets:
        if days <= b:
            return f"{prev + 1}-{b}"
        prev = b
    return f"{prev}+"


def overdue_detail(df: pd.DataFrame, cfg: dict, as_of: pd.Timestamp | None = None) -> pd.DataFrame:
    """Line-level overdue receivables, high-risk flagged, worst first."""
    as_of = as_of or pd.Timestamp.today().normalize()
    high_risk_days = cfg.get("monitoring", {}).get("ar_high_risk_days", 60)

    open_ar = df[df["outstanding"] > 0].copy()
    open_ar["Days Past Due"] = (as_of - open_ar["due_date"]).dt.days
    overdue = open_ar[open_ar["Days Past Due"] > 0].copy()
    if overdue.empty:
        return pd.DataFrame(columns=[
            "Department", "Client", "Engagement", "DueDate",
            "Outstanding", "Days Past Due", "High Risk"
        ])
    overdue["High Risk"] = overdue["Days Past Due"] > high_risk_days
    cols = {
        "department": "Department", "client": "Client", "engagement": "Engagement",
        "due_date": "DueDate", "outstanding": "Outstanding",
    }
    out = overdue.rename(columns=cols)[list(cols.values()) + ["Days Past Due", "High Risk"]]
    return out.sort_values("Days Past Due", ascending=False).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Billing monitoring
# --------------------------------------------------------------------------- #
def due_for_billing(df: pd.DataFrame, cfg: dict, as_of: pd.Timestamp | None = None) -> pd.DataFrame:
    """Engagements with a next-billing date already due or due within horizon."""
    as_of = as_of or pd.Timestamp.today().normalize()
    horizon = cfg.get("monitoring", {}).get("billing_due_horizon_days", 14)
    cutoff = as_of + pd.Timedelta(days=horizon)

    has_taxonomy = "branch" in df.columns and "category" in df.columns
    lead = ["Branch", "Department"] if has_taxonomy else ["Department"]
    result_cols = lead + ["Client", "Engagement", "Manager", "Amount",
                          "NextBillingDate", "Status", "Days Until Due", "Key"]

    d = df.dropna(subset=["next_billing_date"]).copy()
    d = d[d["next_billing_date"] <= cutoff]
    if d.empty:
        return pd.DataFrame(columns=result_cols)
    d["Days Until Due"] = (d["next_billing_date"] - as_of).dt.days

    out = pd.DataFrame(index=d.index)
    if has_taxonomy:
        out["Branch"] = d["branch"].map(lambda b: normalize_branch(cfg, b))
        out["Department"] = d["category"].map(lambda c: department_for(cfg, c))
    else:
        out["Department"] = d["department"]
    out["Client"] = d["client"]
    out["Engagement"] = d["engagement"]
    out["Manager"] = d["manager"]
    amount = d["amount"] if "amount" in d.columns else d.get("invoiced", 0)
    out["Amount"] = pd.to_numeric(amount, errors="coerce").fillna(0.0).values
    out["NextBillingDate"] = d["next_billing_date"]
    out["Status"] = d["status"]
    out["Days Until Due"] = d["Days Until Due"]
    grp = d["branch"] if "branch" in d.columns else d["department"]
    out["Key"] = (d["client"].astype(str) + "||" + d["engagement"].astype(str) + "||"
                  + d["next_billing_date"].dt.strftime("%Y-%m-%d").astype(str) + "||"
                  + grp.astype(str)).values
    return out.sort_values("Days Until Due").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #
def build_alerts(df: pd.DataFrame, cfg: dict, as_of: pd.Timestamp | None = None) -> list[dict]:
    """Return a list of alert dicts: {severity, category, department, message}."""
    as_of = as_of or pd.Timestamp.today().normalize()
    mon = cfg.get("monitoring", {})
    alerts: list[dict] = []

    # 1. Target attainment.
    warn_pct = mon.get("target_attainment_warn_pct", 80)
    summ = department_summary(df, cfg)
    for _, r in summ.iterrows():
        if pd.notna(r["Attainment %"]) and r["Attainment %"] < warn_pct:
            alerts.append({
                "severity": "high" if r["Attainment %"] < warn_pct * 0.625 else "medium",
                "category": "Target",
                "department": r["Department"],
                "message": f"{r['Department']} at {r['Attainment %']:.0f}% of target "
                           f"(gap {r['Gap to Target']:,.0f}).",
            })

    # 2. Client concentration.
    conc_pct = mon.get("client_concentration_warn_pct", 40)
    for dept, sub in df.groupby("department"):
        total = sub["invoiced"].sum()
        if total <= 0:
            continue
        top = sub.groupby("client")["invoiced"].sum().sort_values(ascending=False)
        share = top.iloc[0] / total * 100
        if share >= conc_pct:
            alerts.append({
                "severity": "medium",
                "category": "Concentration",
                "department": dept,
                "message": f"{dept}: client '{top.index[0]}' is {share:.0f}% of billings.",
            })

    # 3. High-risk overdue AR.
    od = overdue_detail(df, cfg, as_of)
    if not od.empty:
        hr = od[od["High Risk"]]
        for dept, sub in hr.groupby("Department"):
            alerts.append({
                "severity": "high",
                "category": "Credit/AR",
                "department": dept,
                "message": f"{dept}: {len(sub)} high-risk overdue item(s), "
                           f"{sub['Outstanding'].sum():,.0f} outstanding.",
            })

    # 4. Billing due.
    due = due_for_billing(df, cfg, as_of)
    if not due.empty:
        overdue_bill = due[due["Days Until Due"] < 0]
        for dept, sub in due.groupby("Department"):
            n_over = (sub["Days Until Due"] < 0).sum()
            sev = "high" if n_over else "low"
            alerts.append({
                "severity": sev,
                "category": "Billing",
                "department": dept,
                "message": f"{dept}: {len(sub)} engagement(s) due for billing"
                           + (f" ({n_over} overdue)." if n_over else " soon."),
            })

    sev_order = {"high": 0, "medium": 1, "low": 2}
    return sorted(alerts, key=lambda a: sev_order.get(a["severity"], 3))
