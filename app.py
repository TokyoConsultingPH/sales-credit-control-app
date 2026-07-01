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
from src import attachments as AT
from src import engagements as EN
from src import notify_email as MAIL
from src import registry as REG
from src import billing_status as BS
from src.auth import require_login, current_user, logout

ROOT = Path(__file__).resolve().parent
SAMPLE = ROOT / "data" / "sample_sales.csv"

st.set_page_config(page_title="Sales & Credit Control", layout="wide", page_icon="📊")
USER = require_login()
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
    """Employee form to report an Ordered Quotation (mirrors the TCF Quotation doc)."""
    import datetime as _dt
    st.title("📝 Report an Ordered Quotation")
    st.caption(f"Fields follow the official Quotation (御見積書). Saved to the "
               f"**{Q.storage_label()}** with Condition = **Order**.")

    with st.form("ordered_quotation", clear_on_submit=True):
        st.markdown("##### 1 · Quotation")
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            submitted_by = st.text_input("Your name *", value=current_user().get("name", ""))
        with c2:
            quotation_number = st.text_input("Quotation no.", placeholder="Q-TCF-PM-26-122")
        with c3:
            issue_date = st.date_input("Issue date", value=_dt.date.today())
        with c4:
            order_date = st.date_input("Order date *", value=_dt.date.today(),
                                       help="When the client acknowledged/ordered.")

        st.markdown("##### 2 · Client")
        d1, d2, d3 = st.columns(3)
        with d1:
            company = st.text_input("Company name *")
            client_type = st.selectbox("Client", Q.CLIENT_TYPES)
        with d2:
            contact_person = st.text_input("Contact person")
            contact_title = st.text_input("Title / position")
        with d3:
            contact_email = st.text_input("Email")
            process = st.selectbox("Process of contact", Q.CONTACT_PROCESS)
        contact_address = st.text_input("Address")

        branch = st.selectbox("Branch", Q.BRANCHES)

        st.markdown("##### 3 · Services ordered")
        st.caption("Add one row per service. Use the **+** at the bottom of the table for more lines. "
                   "Set each line's **Invoiced month** (e.g. one line for the initial 50%, another for the final 50%).")
        line_template = pd.DataFrame([
            {"Service": "", "Description": "", "Department": None, "PIC": "",
             "Classification": "Spot", "Unit": "PHP/Year", "Price": 0.0, "Invoiced month": ""}
        ])
        lines = st.data_editor(
            line_template, num_rows="dynamic", use_container_width=True, hide_index=True,
            column_config={
                "Service": st.column_config.SelectboxColumn("Service", options=Q.SERVICE_TYPES, width="medium"),
                "Description": st.column_config.TextColumn("Description", width="large"),
                "Department": st.column_config.SelectboxColumn("Department", options=Q.DEPARTMENTS),
                "PIC": st.column_config.TextColumn("PIC", help="Person in charge of this service"),
                "Classification": st.column_config.SelectboxColumn("Classification", options=Q.CLASSIFICATIONS),
                "Unit": st.column_config.SelectboxColumn("Unit", options=Q.UNITS),
                "Price": st.column_config.NumberColumn("Price (PHP)", min_value=0.0, format="%.0f"),
                "Invoiced month": st.column_config.TextColumn("Invoiced month", help="e.g. 2026-05 or 2026-05 (50%)"),
            })

        st.markdown("##### 4 · Attachments")
        uploaded_files = st.file_uploader(
            "Attach the signed quotation, PO, or supporting documents",
            accept_multiple_files=True,
            type=["pdf", "png", "jpg", "jpeg", "xlsx", "xls", "docx", "doc", "csv"])

        submitted = st.form_submit_button("✅ Submit ordered quotation", type="primary")

    if submitted:
        valid_lines = [r for _, r in lines.iterrows()
                       if float(r.get("Price") or 0) > 0 or str(r.get("Service") or "").strip()]
        errors = []
        if not submitted_by.strip():
            errors.append("Your name is required.")
        if not company.strip():
            errors.append("Company name is required.")
        if not valid_lines:
            errors.append("Add at least one service line with a price.")
        elif any(float(r.get("Price") or 0) <= 0 for r in valid_lines):
            errors.append("Every service line needs a price.")
        if errors:
            for e in errors:
                st.error(e)
        else:
            quote_id = quotation_number.strip() or f"NOQ-{pd.Timestamp.now():%Y%m%d%H%M%S}"
            stored = AT.save_attachments(quote_id, uploaded_files)
            attach_str = "; ".join(stored)
            records = [
                Q.build_record(
                    submitted_by=submitted_by, quotation_number=quotation_number,
                    issue_date=issue_date, order_date=order_date, company=company,
                    contact_person=contact_person, contact_title=contact_title,
                    contact_email=contact_email, contact_address=contact_address,
                    branch=branch, client_type=client_type, process_of_contact=process,
                    line_no=i + 1, department=r.get("Department"), pic=r.get("PIC"),
                    type_of_service=(r.get("Service") or "Other"),
                    service_description=r.get("Description"), classification=r.get("Classification"),
                    unit=r.get("Unit"), price=r.get("Price"), invoiced_month=r.get("Invoiced month"),
                    attachments=attach_str)
                for i, r in enumerate(valid_lines)
            ]
            where = Q.save_quotations(records)
            total = sum(rec["price"] for rec in records)
            extra = f", {len(stored)} file(s) attached" if stored else ""
            st.success(f"Saved **{len(records)} service line(s)** for **{company}** "
                       f"(total {total:,.0f}{extra}) to {where}.")

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

        names = sorted({n.strip() for cell in qdf.get("attachments", pd.Series(dtype=str)).dropna()
                        for n in str(cell).split(";") if n.strip()})
        if names:
            with st.expander(f"📎 Attachments ({len(names)})"):
                for nm in names:
                    data = AT.get_attachment_bytes(nm)
                    if data is not None:
                        st.download_button(f"⬇️ {nm}", data, file_name=nm, key=f"att_{nm}")
                    else:
                        st.caption(f"⚠️ {nm} (file not found)")


def render_billing_notifications() -> None:
    """Pending final-50% billing notifications with a 'mark as billed' action."""
    pending = EN.load_notifications(status="pending")
    st.subheader(f"🔔 Pending final-50% billings ({len(pending)})")
    if pending.empty:
        st.success("No pending billings. ✅")
        return
    for _, n in pending.iterrows():
        c1, c2 = st.columns([5, 1])
        c1.markdown(f"🔴 **{n['company']}** — {float(n['amount']):,.0f}  \n"
                    f"<span style='color:gray'>{n['quotation_number'] or 'no quotation no.'} · "
                    f"raised {n['created_at']}"
                    f"{' · emailed' if str(n.get('emailed')).lower() in ('true','1') else ''}</span>",
                    unsafe_allow_html=True)
        for nm in [a.strip() for a in str(n.get("attachments") or "").split(";") if a.strip()]:
            data = AT.get_attachment_bytes(nm)
            if data is not None:
                c1.download_button(f"📄 Notice of Completion — {nm}", data,
                                   file_name=nm, key=f"noc_{n['notif_id']}_{nm}")
        if c2.button("Mark billed", key=f"bill_{n['notif_id']}"):
            EN.mark_billed(n["notif_id"])
            st.rerun()


def render_complete_engagement() -> None:
    """Employee marks a logged quotation complete -> triggers final-50% billing."""
    st.title("✅ Complete an engagement")
    email_ok, email_msg = MAIL.status(cfg)
    st.caption(f"Completing an engagement raises a final-50% billing notification. "
               f"Email: {'on' if email_ok else 'off'} — {email_msg}")

    pend = EN.pending_engagements()
    if pend.empty:
        st.info("No engagements awaiting completion. Add a quotation on the "
                "**📝 Report Ordered Quotation** page first.")
    else:
        st.caption(f"{len(pend)} engagement(s) awaiting completion. "
                   "One quotation may list several engagements — pick the exact one.")
        labels = {
            f"{r.quotation_number or 'no-quo'} · line {r.line_no} · {r.type_of_service} "
            f"· {r.company} · fee {r.engagement_fee:,.0f}": r
            for r in pend.itertuples()
        }
        # Selector is OUTSIDE the form so the fee/detail update live on change.
        choice = st.selectbox("Engagement to complete (one service line)", list(labels))
        row = labels[choice]
        final_amt = round(float(row.engagement_fee) * EN.FINAL_SHARE, 2)

        st.markdown(
            f"**Engagement being completed**  \n"
            f"🏢 {row.company} · {row.branch}  \n"
            f"🧾 Quotation **{row.quotation_number or 'no quotation no.'}**, line **{row.line_no}**  \n"
            f"🛠️ **{row.type_of_service}**"
            + (f" — {row.service_description}" if str(row.service_description).strip() else ""))
        m1, m2 = st.columns(2)
        m1.metric("Engagement fee", f"{row.engagement_fee:,.0f}")
        m2.metric("Final 50% to bill", f"{final_amt:,.0f}")

        with st.form("complete_engagement", clear_on_submit=True):
            c1, c2 = st.columns(2)
            completed_by = c1.text_input("Your name *", value=current_user().get("name", ""))
            completion_date = c2.date_input("Completion date", value=pd.Timestamp.today())
            notes = st.text_area("Completion notes", placeholder="Report submitted, deliverables sent…")
            noc_files = st.file_uploader(
                "Notice of Completion sent to client (PDF or JPG)",
                accept_multiple_files=True, type=["pdf", "jpg", "jpeg", "png"])
            submitted = st.form_submit_button("✅ Submit completion & trigger billing", type="primary")

        if submitted:
            if not completed_by.strip():
                st.error("Your name is required.")
            elif not noc_files:
                st.error("Attach the Notice of Completion (PDF or JPG) sent to the client.")
            else:
                stored = AT.save_attachments(f"NOC-{row.engagement_key}", noc_files)
                notif = EN.complete_engagement(
                    completed_by=completed_by, engagement_key=row.engagement_key,
                    quotation_number=row.quotation_number, line_no=row.line_no,
                    type_of_service=row.type_of_service, service_description=row.service_description,
                    company=row.company, branch=row.branch, engagement_fee=row.engagement_fee,
                    final_amount=final_amt, completion_date=completion_date, notes=notes,
                    attachments="; ".join(stored))
                sent, mmsg = MAIL.send(
                    cfg, subject=f"[Billing] Final 50% due — {row.company}",
                    body=notif["message"] + f"\n\nCompleted by {completed_by} on {completion_date}.")
                if sent:
                    EN.mark_emailed(notif["notif_id"])
                st.success(f"Engagement completed. Billing notification raised for "
                           f"**{row.company}** ({final_amt:,.0f}).")
                st.caption(("📧 " + mmsg) if sent else ("📧 not emailed — " + mmsg))
                st.rerun()

    st.divider()
    render_billing_notifications()


def render_manage_users() -> None:
    st.title("👥 Manage users")

    st.subheader("Employee roster")
    with st.form("add_user", clear_on_submit=True):
        a, b = st.columns(2)
        new_username = a.text_input("Username *")
        new_name = b.text_input("Full name")
        c, d = st.columns(2)
        new_role = c.selectbox("Role", REG.ROLES)
        new_pw = d.text_input("Password *", type="password")
        if st.form_submit_button("Add user", type="primary"):
            ok, msg = REG.add_user(new_username, new_name, new_role, new_pw)
            (st.success if ok else st.error)(msg)
            if ok:
                st.rerun()

    users = REG.list_users_display()
    if users.empty:
        st.caption("No users yet.")
    else:
        st.dataframe(users, use_container_width=True, hide_index=True)
        col1, col2 = st.columns(2)
        with col1:
            rm = st.selectbox("Remove a user", ["—"] + users["username"].astype(str).tolist())
            if rm != "—" and st.button(f"Remove {rm}"):
                REG.remove_user(rm)
                st.rerun()
        with col2:
            ru = st.selectbox("Reset password for", ["—"] + users["username"].astype(str).tolist())
            rp = st.text_input("New password", type="password", key="resetpw")
            if ru != "—" and rp and st.button("Reset password"):
                REG.set_password(ru, rp)
                st.success(f"Password reset for {ru}.")

    st.divider()
    st.subheader("Activity — who reported which engagements")
    act = REG.user_activity()
    if act.empty:
        st.info("No reported engagements yet.")
        return
    st.dataframe(act.style.format({"Total value": "{:,.0f}"}),
                 use_container_width=True, hide_index=True)
    who = st.selectbox("View engagements reported by", act["Name"].tolist())
    eng = REG.user_engagements(who)
    st.caption(f"{len(eng)} engagement(s) reported by {who}")
    st.dataframe(eng.style.format({"price": "{:,.0f}"}), use_container_width=True, hide_index=True)


def render_client_database() -> None:
    st.title("🗂️ Client database")
    st.caption("Built automatically from reported (signed) quotations.")
    cdb = REG.client_database()
    if cdb.empty:
        st.info("No clients yet. Report an ordered quotation to populate this.")
        return

    q = st.text_input("Search client / contact / email").strip().lower()
    view = cdb
    if q:
        mask = (cdb["Client"].astype(str).str.lower().str.contains(q)
                | cdb["Contact"].astype(str).str.lower().str.contains(q)
                | cdb["Email"].astype(str).str.lower().str.contains(q))
        view = cdb[mask]

    m = st.columns(3)
    m[0].metric("Clients", f"{len(view)}")
    m[1].metric("Engagements", f"{int(view['Engagements'].sum())}")
    m[2].metric("Total value", money(view["Total value"].sum()))
    st.dataframe(view.style.format({"Total value": "{:,.0f}"}),
                 use_container_width=True, hide_index=True)
    st.download_button("⬇️ Download client database (CSV)",
                       cdb.to_csv(index=False).encode("utf-8-sig"),
                       file_name="client_database.csv", mime="text/csv")

    st.divider()
    pick = st.selectbox("Open a client", view["Client"].tolist())
    if pick:
        rec = cdb[cdb["Client"] == pick].iloc[0]
        st.markdown(
            f"### {pick}\n"
            f"👤 {rec['Contact'] or '—'} · {rec['Title'] or '—'}  \n"
            f"✉️ {rec['Email'] or '—'}  \n"
            f"📍 {rec['Address'] or '—'}  \n"
            f"🏢 {rec['Branch'] or '—'} · {rec['Type'] or '—'} client")
        eng = REG.client_engagements(pick)
        st.caption(f"{len(eng)} engagement(s)")
        st.dataframe(eng.style.format({"price": "{:,.0f}"}),
                     use_container_width=True, hide_index=True)


# --------------------------------------------------------------------------- #
# Sidebar — page selector
# --------------------------------------------------------------------------- #
st.sidebar.title("📊 Sales & Credit Control")
st.sidebar.caption(cfg.get("general", {}).get("company_name", ""))

_is_admin = USER.get("role") == "Admin" or USER.get("master")
st.sidebar.success(f"👤 {USER.get('name', 'User')} · {USER.get('role', '')}")
if st.sidebar.button("Log out", use_container_width=True):
    logout()
    st.rerun()

_pending_badge = EN.pending_count()
_complete_label = f"✅ Complete Engagement ({_pending_badge})" if _pending_badge else "✅ Complete Engagement"
_pages = ["📊 Dashboard", "📝 Report Ordered Quotation", _complete_label, "🗂️ Client Database"]
if _is_admin:
    _pages.append("👥 Manage Users")
page = st.sidebar.radio("Page", _pages)
st.sidebar.divider()
if page == "📝 Report Ordered Quotation":
    render_quotation_form()
    st.stop()
if page.startswith("✅ Complete Engagement"):
    render_complete_engagement()
    st.stop()
if page == "👥 Manage Users":
    render_manage_users()
    st.stop()
if page == "🗂️ Client Database":
    render_client_database()
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
st.sidebar.download_button(
    "🗓️ Download monthly sales matrix",
    data=R.build_monthly_matrix_excel(df, cfg),
    file_name=f"monthly_sales_matrix_{as_of.date()}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    use_container_width=True,
    help="Month-by-month grid (like the Credit Control layout): engagements × months.",
)

for w in DL.validate(df):
    st.sidebar.warning(w)

# --------------------------------------------------------------------------- #
# Header KPIs
# --------------------------------------------------------------------------- #
st.title("Sales Reporting & Credit Control")
st.caption(f"Source: **{source_label}**  ·  grouped by **{dim_label}**  ·  "
           f"as of **{as_of.date()}**  ·  {len(df):,} billing rows")

_pending = EN.pending_count()
if _pending:
    st.warning(f"🔔 {_pending} engagement(s) completed and awaiting **final 50% billing** — "
               "see the **✅ Complete Engagement** page.")

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
    st.markdown("#### 🔔 Final 50% due — from completed engagements")
    fin = EN.load_notifications(status="pending")
    if fin.empty:
        st.info("No final-50% billings pending. Complete an engagement on the "
                "**✅ Complete Engagement** page to raise one.")
    else:
        fin_view = pd.DataFrame({
            "Company": fin["company"],
            "Service": fin.get("service", ""),
            "Quotation": fin.get("quotation_number", ""),
            "Line": fin.get("line_no", ""),
            "Final 50%": pd.to_numeric(fin["amount"], errors="coerce"),
            "Raised": fin["created_at"],
            "Emailed": fin.get("emailed", ""),
        })
        c = st.columns(2)
        c[0].metric("Engagements awaiting final billing", f"{len(fin_view)}")
        c[1].metric("Total final 50% due", money(fin_view["Final 50%"].sum()))
        st.dataframe(fin_view.style.format({"Final 50%": "{:,.0f}"}),
                     use_container_width=True, hide_index=True)

    st.divider()
    horizon = cfg.get("monitoring", {}).get("billing_due_horizon_days", 14)
    st.markdown(f"#### 🧾 From the register — flagged **can be invoiced** "
                f"(overdue, or due within {horizon} days)")
    if due.empty:
        st.info("Nothing due for billing in the window.")
    else:
        mcol = st.columns(2)
        mcol[0].metric("Total billable not yet invoiced", f"{len(due)} engagements")
        mcol[1].metric("Total amount", money(due["Amount"].sum()))
        st.caption("Edit **Status** to move an engagement through the workflow: "
                   "Ordered → Can be invoiced → Invoiced → Collected. Changes are saved.")

        keys = due["Key"].tolist()
        overrides = BS.load_overrides()
        disp = due.drop(columns=["Key"]).copy()
        disp["Status"] = [overrides.get(k, BS.flow_status(s))
                          for k, s in zip(keys, disp["Status"])]
        edited = st.data_editor(
            disp, use_container_width=True, hide_index=True, key="due_status_editor",
            disabled=[c for c in disp.columns if c != "Status"],
            column_config={
                "Amount": st.column_config.NumberColumn("Amount", format="%.0f"),
                "Status": st.column_config.SelectboxColumn(
                    "Status", options=BS.STATUS_FLOW, required=True),
            })
        changed = 0
        for k, old, new in zip(keys, disp["Status"], edited["Status"]):
            if str(new) != str(old):
                BS.set_status(k, str(new), current_user().get("name", ""))
                changed += 1
        if changed:
            st.success(f"Saved {changed} status change(s).")
