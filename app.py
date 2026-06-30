"""Sales Reporting & Credit Control Automation — Streamlit web app.

Reads the TCF 'New-credit control [Philippines]' workbook directly and produces
per-department (Branch / Category / PIC) reporting and monitoring.

Run:  streamlit run app.py
"""
from __future__ import annotations

from pathlib import Path
import pandas as pd
import plotly.express as px
import streamlit as st

from src.config import load_config
from src import data_loader as DL
from src import workbook_loader as W
from src import db_loader as DB
from src import monitoring as M
from src import reports as R
from src import quotations as Q
from src.auth import require_password

ROOT = Path(__file__).resolve().parent
SAMPLE = ROOT / "data" / "sample_sales.csv"

st.set_page_config(page_title="Sales & Credit Control", layout="wide", page_icon="📊")
require_password()
cfg = load_config()
CUR = cfg.get("general", {}).get("currency_symbol", "")

DIMENSIONS = {"Branch": "branch", "Category": "category",
              "Classification": "classification", "PIC / staff": "pic"}


def money(x) -> str:
    try:
        return f"{CUR}{x:,.0f}"
    except (TypeError, ValueError):
        return "-"


def find_tcf_workbook() -> Path | None:
    for folder in ("Sample file", "data"):
        for p in sorted((ROOT / folder).glob("*.xlsx")):
            try:
                if any(s.strip().startswith("Credit Control 2")
                       for s in pd.ExcelFile(p).sheet_names):
                    return p
            except Exception:
                continue
    return None


@st.cache_data(show_spinner="Parsing workbook…")
def load_tcf(path_str: str) -> pd.DataFrame:
    return W.load_workbook(path_str)


def render_quotation_form() -> None:
    """Employee form to report an Ordered Quotation."""
    import datetime as _dt
    st.title("📝 Report an Ordered Quotation")
    st.caption(f"New orders are saved to the **{Q.storage_label()}**. "
               "Condition is recorded as **Order**.")

    with st.form("ordered_quotation", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        with c1:
            submitted_by = st.text_input("Your name *")
            quotation_number = st.text_input("Quotation number")
            date = st.date_input("Date ordered *", value=_dt.date.today())
        with c2:
            company = st.text_input("Company name *")
            branch = st.selectbox("Branch", Q.BRANCHES)
            classification = st.selectbox("Classification", Q.CLASSIFICATIONS)
        with c3:
            client_type = st.selectbox("Client", Q.CLIENT_TYPES)
            type_sel = st.selectbox("Type of service", Q.SERVICE_TYPES)
            process = st.selectbox("Process of contact", Q.CONTACT_PROCESS)

        type_other = st.text_input("If 'Other' service, specify") if type_sel == "Other" else ""
        c4, c5, c6 = st.columns(3)
        with c4:
            monthly_fee = st.number_input("Monthly total fee (if monthly)", min_value=0.0, step=1000.0)
        with c5:
            yearly_fee = st.number_input("Yearly / spot total fee", min_value=0.0, step=1000.0)
        with c6:
            invoiced_month = st.text_input("Invoiced month (e.g. 2026-07)")
        contents = st.text_area("Contents / description")

        submitted = st.form_submit_button("✅ Submit ordered quotation", type="primary")

    if submitted:
        errors = []
        if not submitted_by.strip():
            errors.append("Your name is required.")
        if not company.strip():
            errors.append("Company name is required.")
        if monthly_fee <= 0 and yearly_fee <= 0:
            errors.append("Enter a monthly fee or a yearly/spot fee.")
        if errors:
            for e in errors:
                st.error(e)
        else:
            rec = Q.build_record(
                submitted_by=submitted_by, quotation_number=quotation_number, date=date,
                company=company, branch=branch, classification=classification,
                type_of_service=(type_other or type_sel), client_type=client_type,
                process_of_contact=process, invoiced_month=invoiced_month,
                monthly_fee=monthly_fee, yearly_or_spot_fee=yearly_fee, contents=contents)
            where = Q.save_quotation(rec)
            st.success(f"Saved order for **{company}** to {where}.")

    st.divider()
    st.subheader("Recent ordered quotations")
    qdf = Q.load_quotations()
    if qdf.empty:
        st.info("No quotations submitted yet.")
    else:
        st.caption(f"{len(qdf)} total")
        st.dataframe(qdf, use_container_width=True, hide_index=True)
        st.download_button(
            "⬇️ Download all (CSV)", qdf.to_csv(index=False).encode("utf-8-sig"),
            file_name="ordered_quotations.csv", mime="text/csv")


# --------------------------------------------------------------------------- #
# Sidebar — page selector
# --------------------------------------------------------------------------- #
st.sidebar.title("📊 Sales & Credit Control")
st.sidebar.caption(cfg.get("general", {}).get("company_name", ""))

page = st.sidebar.radio("Page", ["📊 Dashboard", "📝 Report Ordered Quotation"])
st.sidebar.divider()
if page == "📝 Report Ordered Quotation":
    render_quotation_form()
    st.stop()

# --------------------------------------------------------------------------- #
# Sidebar — data source
# --------------------------------------------------------------------------- #
tcf_path = find_tcf_workbook()
db_enabled = cfg.get("database", {}).get("enabled", False)
sources = []
if tcf_path:
    sources.append("TCF workbook (this file)")
if db_enabled:
    sources.append("Database (PostgreSQL)")
sources += ["Upload a file", "Demo data"]
source = st.sidebar.radio("Data source", sources)

df = None          # canonical frame for monitoring
source_label = ""
dim_label = "Branch"

if source == "TCF workbook (this file)":
    tidy = load_tcf(str(tcf_path))
    source_label = tcf_path.name
    years = sorted(tidy["year"].unique())
    sel_years = st.sidebar.multiselect("Year(s)", years, default=[max(years)])
    dim_label = st.sidebar.radio("Group by (department)", list(DIMENSIONS), index=0)
    sub = tidy[tidy["year"].isin(sel_years)] if sel_years else tidy
    df = W.to_canonical(sub, DIMENSIONS[dim_label])

elif source == "Database (PostgreSQL)":
    import os as _os
    dbc = cfg.get("database", {})
    use_env_url = bool(_os.getenv("DATABASE_URL"))
    schema = dbc.get("schema", "public")
    pwd, overrides = None, None
    with st.sidebar.expander("🔌 Connection", expanded="db_tidy" not in st.session_state):
        if use_env_url:
            st.success("Using DATABASE_URL from environment (Render).")
            schema = st.text_input("Schema", schema)
        else:
            host = st.text_input("Host", dbc.get("host", "localhost"))
            port = st.number_input("Port", value=int(dbc.get("port", 5432)), step=1)
            dbname = st.text_input("Database", dbc.get("dbname", ""))
            user = st.text_input("User", dbc.get("user", ""))
            pwd = st.text_input("Password", type="password",
                                help="Leave blank to use env var TCF_DB_PASSWORD / PGPASSWORD.")
            schema = st.text_input("Schema", schema)
            overrides = {"host": host, "port": port, "dbname": dbname,
                         "user": user, "schema": schema}

        if st.button("Test connection", use_container_width=True):
            try:
                ver = DB.test_connection(DB.make_engine(dbc, pwd, overrides))
                st.success(f"Connected: {ver.split(',')[0]}")
            except Exception as e:
                st.error(f"Connection failed: {e}")

        with st.expander("Browse schema"):
            try:
                eng = DB.make_engine(dbc, pwd, overrides)
                tbls = DB.list_tables(eng, schema)["table_name"].tolist()
                tsel = st.selectbox("Table", tbls) if tbls else None
                if tsel:
                    st.dataframe(DB.list_columns(eng, tsel, schema), use_container_width=True)
            except Exception as e:
                st.caption(f"(Connect to browse) {e}")

    sql = st.sidebar.text_area("SQL query", value=(dbc.get("query") or "").strip(), height=140)
    if st.sidebar.button("▶ Run query & load", type="primary", use_container_width=True):
        try:
            st.session_state["db_tidy"] = DB.load_from_db(cfg, pwd, overrides, sql=sql)
            st.sidebar.success(f"Loaded {len(st.session_state['db_tidy']):,} rows.")
        except Exception as e:
            st.sidebar.error(f"Load failed: {e}")

    tidy = st.session_state.get("db_tidy")
    if tidy is None or tidy.empty:
        st.info("Configure the connection and SQL in the sidebar, then **Run query & load**.")
        st.stop()

    source_label = "PostgreSQL (DATABASE_URL)" if use_env_url else f"PostgreSQL · {dbc.get('dbname','')}"
    dims = DB.available_dimensions(tidy)
    if dims:
        dim_field = st.sidebar.radio("Group by (department)", dims, index=0)
        dim_label = dim_field.title()
    else:
        dim_field, dim_label = "department", "Department"
    if "year" in tidy.columns and tidy["year"].notna().any():
        years = sorted(int(y) for y in tidy["year"].dropna().unique())
        sel_years = st.sidebar.multiselect("Year(s)", years, default=years[-1:])
        tidy = tidy[tidy["year"].isin(sel_years)] if sel_years else tidy
    df = DB.to_canonical(tidy, dim_field)

elif source == "Upload a file":
    up = st.sidebar.file_uploader("Upload Excel/CSV", type=["csv", "xlsx", "xls"])
    if up is not None:
        # Is it a TCF-format workbook?
        is_tcf = up.name.lower().endswith(".xlsx") and any(
            s.strip().startswith("Credit Control 2") for s in pd.ExcelFile(up).sheet_names)
        if is_tcf:
            tidy = W.load_workbook(up)
            years = sorted(tidy["year"].unique())
            sel_years = st.sidebar.multiselect("Year(s)", years, default=[max(years)])
            dim_label = st.sidebar.radio("Group by (department)", list(DIMENSIONS), index=0)
            sub = tidy[tidy["year"].isin(sel_years)] if sel_years else tidy
            df = W.to_canonical(sub, DIMENSIONS[dim_label])
        else:
            df = DL.load_data(up, cfg, filename=up.name)
            dim_label = "Department"
        source_label = up.name
    else:
        st.info("Upload a file in the sidebar to begin.")
        st.stop()

else:  # Demo data
    if not SAMPLE.exists():
        st.warning("Run `python scripts/make_sample_data.py` to create demo data.")
        st.stop()
    df = DL.load_data(SAMPLE, cfg)
    source_label = "sample_sales.csv (demo)"
    dim_label = "Department"

as_of = pd.Timestamp(st.sidebar.date_input("Monitoring 'as of' date", value=pd.Timestamp("2026-06-30")))

# Bound analysis to realized billings up to the 'as of' date (exclude future
# scheduled months so KPIs, trends and AR reflect the reporting date).
if "date" in df.columns and df["date"].notna().any():
    df = df[df["date"].isna() | (df["date"] <= as_of)]

# Department filter.
all_depts = sorted(df["department"].dropna().unique())
chosen = st.sidebar.multiselect(f"{dim_label}s", all_depts, default=all_depts)
if chosen:
    df = df[df["department"].isin(chosen)]

st.sidebar.divider()
st.sidebar.download_button(
    "⬇️ Download Excel report",
    data=R.build_excel_report(df, cfg, as_of),
    file_name=f"sales_credit_report_{dim_label}_{as_of.date()}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    use_container_width=True,
)

for w in DL.validate(df):
    st.sidebar.warning(w)

# --------------------------------------------------------------------------- #
# Header KPIs
# --------------------------------------------------------------------------- #
st.title("Sales Reporting & Credit Control")
st.caption(f"Source: **{source_label}**  ·  grouped by **{dim_label}**  ·  "
           f"as of **{as_of.date()}**  ·  {len(df):,} billing rows")

summary = M.department_summary(df, cfg)
total_inv = df["invoiced"].sum()
total_rec = df["received"].sum()
total_ar = df["outstanding"].sum()
due = M.due_for_billing(df, cfg, as_of)

k = st.columns(5)
k[0].metric("Billed (invoiced)", money(total_inv))
k[1].metric("Collected", money(total_rec))
k[2].metric("Outstanding AR", money(total_ar))
k[3].metric("Collection %", f"{(total_rec / total_inv * 100):.0f}%" if total_inv else "-")
k[4].metric("Due for billing", f"{len(due)} items")

# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #
alerts = M.build_alerts(df, cfg, as_of)
if alerts:
    st.subheader(f"⚠️ Monitoring alerts ({len(alerts)})")
    icon = {"high": "🔴", "medium": "🟠", "low": "🟡"}
    cols = st.columns(2)
    for n, a in enumerate(alerts[:12]):
        cols[n % 2].markdown(f"{icon.get(a['severity'], '•')} **[{a['category']}]** {a['message']}")
    if len(alerts) > 12:
        st.caption(f"…and {len(alerts) - 12} more (see Excel report).")
else:
    st.success("✅ No alerts — all departments within thresholds.")

st.divider()

# --------------------------------------------------------------------------- #
# Tabs
# --------------------------------------------------------------------------- #
t1, t2, t3, t4 = st.tabs(
    [f"📈 Sales by {dim_label}", "📉 Trends", "💳 Credit / AR", "🧾 Due for Billing"])

with t1:
    show_target = (summary["Target"] > 0).any()
    c1, c2 = st.columns([3, 2])
    with c1:
        ycols = ["Invoiced", "Received"] + (["Target"] if show_target else [])
        st.plotly_chart(
            px.bar(summary, x="Department", y=ycols, barmode="group",
                   title=f"Billed / Collected by {dim_label}"),
            use_container_width=True)
    with c2:
        st.plotly_chart(
            px.pie(summary, names="Department", values="Invoiced",
                   title="Billing share"),
            use_container_width=True)
    fmt = {"Invoiced": "{:,.0f}", "Received": "{:,.0f}", "Outstanding": "{:,.0f}",
           "Target": "{:,.0f}", "Gap to Target": "{:,.0f}",
           "Attainment %": "{:.1f}", "Collection %": "{:.1f}"}
    st.dataframe(summary.style.format(fmt, na_rep="—"), use_container_width=True)

with t2:
    trend = M.monthly_trend(df)
    if trend.empty:
        st.info("No dated rows to plot trends.")
    else:
        st.plotly_chart(
            px.line(trend, x="Month", y="Invoiced", color="Department", markers=True,
                    title=f"Monthly billings by {dim_label}"),
            use_container_width=True)
        st.dataframe(
            M.trend_metrics(df).style.format(
                {"Invoiced": "{:,.0f}", "MoM %": "{:.1f}", "YoY %": "{:.1f}"}, na_rep="—"),
            use_container_width=True)
        st.caption("Tip: select multiple years in the sidebar for year-over-year trends.")

with t3:
    aging = M.ar_aging(df, cfg, as_of)
    st.markdown("**AR aging by department** (invoiced but not yet collected)")
    st.dataframe(
        aging.style.format({c: "{:,.0f}" for c in aging.columns if c != "Department"}),
        use_container_width=True)
    bucket_cols = [c for c in aging.columns if c not in ("Department", "Total AR")]
    if not aging.empty:
        long = aging.melt(id_vars="Department", value_vars=bucket_cols,
                          var_name="Bucket", value_name="Amount")
        st.plotly_chart(
            px.bar(long, x="Department", y="Amount", color="Bucket", title="AR aging buckets"),
            use_container_width=True)
    st.markdown("**Overdue receivables (worst first)**")
    od = M.overdue_detail(df, cfg, as_of)
    st.dataframe(od.style.format({"Outstanding": "{:,.0f}"}), use_container_width=True)

with t4:
    horizon = cfg.get("monitoring", {}).get("billing_due_horizon_days", 14)
    st.markdown(f"Engagements flagged **can be invoiced** but not yet billed "
                f"(overdue, or due within {horizon} days).")
    if due.empty:
        st.info("Nothing due for billing in the window.")
    else:
        st.metric("Total billable not yet invoiced", f"{len(due)} engagements")
        st.dataframe(due, use_container_width=True)
