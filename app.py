"""Sales Reporting & Credit Control Automation — Streamlit web app.

Reads the TCF 'New-credit control [Philippines]' workbook directly and produces
per-department (Branch / Category / PIC) reporting and monitoring.

Run:  streamlit run app.py
"""
from __future__ import annotations

import os
from pathlib import Path
import pandas as pd
import plotly.express as px
import streamlit as st

from src.config import load_config, normalize_branch, department_for
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
from src import sales_overrides as SO
from src.auth import require_login, current_user, logout

ROOT = Path(__file__).resolve().parent
SAMPLE = ROOT / "data" / "sample_sales.csv"


def _find_logo():
    d = ROOT / "assets"
    for name in ("logo.png", "logo.jpg", "logo.jpeg"):
        if (d / name).exists():
            return d / name
    imgs = sorted(d.glob("*.png")) + sorted(d.glob("*.jpg")) + sorted(d.glob("*.jpeg"))
    return imgs[0] if imgs else None


LOGO_PATH = _find_logo()

st.set_page_config(page_title="Sales & Credit Control", layout="wide", page_icon="📊")

# On Streamlit Cloud, secrets are in st.secrets but not always exported as env
# vars — bridge them so os.getenv() (used across the app) sees them.
for _k in ("DATABASE_URL", "APP_PASSWORD", "TCF_SMTP_PASSWORD"):
    try:
        if _k in st.secrets and not os.getenv(_k):
            os.environ[_k] = str(st.secrets[_k])
    except Exception:
        pass

USER = require_login()
cfg = load_config()
CUR = cfg.get("general", {}).get("currency_symbol", "")

# Centered logo banner at the top of every page.
if LOGO_PATH:
    _lc = st.columns([2, 1, 2])
    _lc[1].image(str(LOGO_PATH), width=200)

DIMENSIONS = {"Branch": "branch", "Category": "category",
              "Classification": "classification", "PIC / staff": "pic"}


def money(x) -> str:
    try:
        return f"{CUR}{x:,.0f}"
    except (TypeError, ValueError):
        return "-"


def money_compact(x) -> str:
    """Short currency for KPI cards, e.g. PHP 22.3M / 1.31M / 698K."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "-"
    a = abs(x)
    if a >= 1e9:
        return f"{CUR}{x / 1e9:.2f}B"
    if a >= 1e6:
        return f"{CUR}{x / 1e6:.2f}M"
    if a >= 1e3:
        return f"{CUR}{x / 1e3:.0f}K"
    return f"{CUR}{x:,.0f}"


def _sales_detail(title, sub):
    """Full sales-detail view for a selected client / department / engagement."""
    st.markdown(f"## 🧾 Sales detail — {title}")
    if sub.empty:
        st.info("No sales rows for this selection.")
        return
    m = st.columns(5)
    m[0].metric("Billed", money_compact(sub["invoiced"].sum()), help=money(sub["invoiced"].sum()))
    m[1].metric("Collected", money_compact(sub["received"].sum()), help=money(sub["received"].sum()))
    m[2].metric("Outstanding", money_compact(sub["outstanding"].sum()), help=money(sub["outstanding"].sum()))
    m[3].metric("Clients", f"{sub['client'].nunique()}")
    m[4].metric("Engagements", f"{sub['engagement'].nunique()}")
    ms = sub.dropna(subset=["date"]).copy()
    if not ms.empty:
        ms["Month"] = ms["date"].dt.to_period("M").dt.to_timestamp()
        mm = ms.groupby("Month")[["invoiced", "received"]].sum().reset_index().melt(
            "Month", var_name="Metric", value_name="Amount")
        mm["Metric"] = mm["Metric"].map({"invoiced": "Billed", "received": "Collected"})
        fig = px.bar(mm, x="Month", y="Amount", color="Metric", barmode="group",
                     title="Monthly billings vs collections",
                     color_discrete_map={"Billed": "#378ADD", "Collected": "#1D9E75"})
        fig.update_layout(height=260, margin=dict(t=40, b=0, l=0, r=0),
                          yaxis_title=None, xaxis_title=None, legend_title=None)
        st.plotly_chart(fig, use_container_width=True)
    dc = {"date": "Date", "client": "Client", "department": "Group",
          "engagement": "Engagement", "invoiced": "Billed", "received": "Collected",
          "outstanding": "Outstanding", "status": "Status"}
    det = sub.rename(columns=dc)
    det = det[[v for v in dc.values() if v in det.columns]].sort_values("Date")
    st.dataframe(det.style.format(
        {"Billed": "{:,.0f}", "Collected": "{:,.0f}", "Outstanding": "{:,.0f}"}),
        use_container_width=True, hide_index=True)

    import io
    import re as _re
    _buf = io.BytesIO()
    with pd.ExcelWriter(_buf, engine="xlsxwriter") as _xw:
        det.to_excel(_xw, index=False, sheet_name="Sales detail")
    _safe = _re.sub(r"[^A-Za-z0-9._-]+", "_", str(title))[:60].strip("_") or "sales_detail"
    st.download_button("⬇️ Download this detail (Excel)", _buf.getvalue(),
                       file_name=f"sales_detail_{_safe}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


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
            {"Service": "", "Department": None, "PIC": "",
             "Classification": "Spot", "Unit": "PHP/Year", "Price": 0.0, "Invoiced month": ""}
        ])
        lines = st.data_editor(
            line_template, num_rows="dynamic", use_container_width=True, hide_index=True,
            column_config={
                "Service": st.column_config.TextColumn("Service", width="medium",
                                                       help="Type the service name"),
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
                    service_description="", classification=r.get("Classification"),
                    unit=r.get("Unit"), price=r.get("Price"), invoiced_month=r.get("Invoiced month"),
                    attachments=attach_str)
                for i, r in enumerate(valid_lines)
            ]
            where = Q.save_quotations(records)
            # Auto-flag the initial 50% of each engagement as due for billing.
            flagged = 0
            for rec in records:
                ek = EN._engagement_key(rec["quotation_number"], rec["company"],
                                        rec["order_date"], rec["line_no"])
                n = EN.raise_initial_billing(
                    engagement_key=ek, quotation_number=rec["quotation_number"],
                    line_no=rec["line_no"], company=rec["company"],
                    service=rec["type_of_service"], amount=rec["price"] * EN.FINAL_SHARE,
                    due_month=rec.get("invoiced_month"), created_by=rec["submitted_by"])
                flagged += 1 if n else 0
            total = sum(rec["price"] for rec in records)
            extra = f", {len(stored)} file(s) attached" if stored else ""
            st.success(f"Saved **{len(records)} service line(s)** for **{company}** "
                       f"(total {total:,.0f}{extra}) to {where}.")
            if flagged:
                st.info(f"🔔 Flagged **initial 50%** of {flagged} engagement(s) as due for "
                        f"billing (total {total * EN.FINAL_SHARE:,.0f}) — see **Due for Billing**.")

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
    st.subheader(f"🔔 Pending billings — initial & final 50% ({len(pending)})")
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
        m1.metric("Engagement fee", money_compact(row.engagement_fee),
                  help=money(row.engagement_fee))
        m2.metric("Final 50% to bill", money_compact(final_amt), help=money(final_amt))

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


@st.cache_data(show_spinner="Loading from database…", ttl=300)
def _billing_lines_from_db() -> pd.DataFrame:
    return pd.read_sql("SELECT * FROM billing_lines", DB.make_engine({}, None, None))


def render_sales_database() -> None:
    st.title("🧾 Sales database")
    tcf = find_tcf_workbook()
    if tcf:
        tidy = load_tcf(str(tcf))
        source_note = tcf.name
    elif os.getenv("DATABASE_URL"):
        try:
            tidy = _billing_lines_from_db()
            source_note = "database (billing_lines)"
        except Exception as e:
            st.error(f"Could not read from database: {e}")
            return
    else:
        st.info("No data source: add a workbook to 'Sample file/' or set DATABASE_URL.")
        return
    if tidy.empty:
        st.info("No sales rows found.")
        return
    st.caption(f"Source: {source_note}")

    years = sorted(int(y) for y in tidy["year"].dropna().unique())
    default_idx = years.index(2026) if 2026 in years else len(years) - 1
    year = st.selectbox("Credit Control year", years, index=default_idx)
    st.caption(f"**Credit Control {year}** layout — one row per engagement with monthly "
               f"fees (Jan–Dec), from {tcf.name}.")

    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    d = tidy[tidy["year"] == year].copy()
    d["MonthAbbr"] = d["date"].dt.strftime("%b")
    idcols = ["company", "content", "branch", "classification", "category", "pic"]

    piv = d.pivot_table(index=idcols, columns="MonthAbbr", values="fee",
                        aggfunc="sum", fill_value=0.0)
    for mm in months:
        if mm not in piv.columns:
            piv[mm] = 0.0
    piv = piv[months]
    meta = d.groupby(idcols).agg(**{"TOTAL SERVICE FEE": ("total_fee", "first"),
                                    "Status": ("status_overall", "first")})
    out = meta.join(piv).reset_index()
    out["branch"] = out["branch"].map(lambda b: normalize_branch(cfg, b))
    out = out.rename(columns={"company": "Company", "content": "Engagement",
                              "branch": "Branch", "classification": "Classification",
                              "category": "Category", "pic": "PIC"})
    out["Total"] = out[months].sum(axis=1)
    ordered = (["Company", "Engagement", "Branch", "Classification", "Category", "PIC",
                "Status", "TOTAL SERVICE FEE"] + months + ["Total"])
    out = out[[c for c in ordered if c in out.columns]].reset_index(drop=True)

    # Overlay saved overrides for the editable fields.
    overrides = SO.load_overrides()
    if overrides:
        out = out.astype({c: object for c in SO.EDITABLE})
        for i in out.index:
            k = SO.make_key(year, out.at[i, "Company"], out.at[i, "Engagement"])
            for fld, val in overrides.get(k, {}).items():
                if fld in out.columns:
                    out.at[i, fld] = val

    f1, f2, f3 = st.columns([1, 1, 2])
    brs = sorted(out["Branch"].dropna().unique())
    bsel = f1.multiselect("Branch", brs, default=brs)
    cats = sorted(out["Category"].dropna().unique())
    csel = f2.multiselect("Category", cats, default=cats)
    q = f3.text_input("Search company / engagement").strip().lower()

    fv = out[out["Branch"].isin(bsel) & out["Category"].isin(csel)]
    if q:
        fv = fv[fv["Company"].astype(str).str.lower().str.contains(q)
                | fv["Engagement"].astype(str).str.lower().str.contains(q)]
    fv = fv.reset_index(drop=True)

    m = st.columns(3)
    m[0].metric("Engagements", f"{len(fv):,}")
    m[1].metric("Total service fees", money_compact(fv["TOTAL SERVICE FEE"].sum()),
                help=money(fv["TOTAL SERVICE FEE"].sum()))
    m[2].metric(f"Billed in {year}", money_compact(fv[months].to_numpy().sum()),
                help=money(fv[months].to_numpy().sum()))

    st.caption("Branch, Classification, Category, PIC and Status are editable dropdowns — "
               "changes are saved.")
    # Dropdown option lists (existing values + standard choices).
    branch_opts = sorted(set(out["Branch"].dropna()) | set(Q.BRANCHES) | {"Unknown"})
    class_opts = sorted(set(out["Classification"].dropna()) | set(Q.CLASSIFICATIONS))
    cat_opts = sorted(set(out["Category"].dropna()))
    pic_opts = sorted(set(out["PIC"].dropna()) | {"Unassigned"})
    status_opts = sorted(set(out["Status"].dropna()))
    col_cfg = {
        "Branch": st.column_config.SelectboxColumn("Branch", options=branch_opts),
        "Classification": st.column_config.SelectboxColumn("Classification", options=class_opts),
        "Category": st.column_config.SelectboxColumn("Category", options=cat_opts),
        "PIC": st.column_config.SelectboxColumn("PIC", options=pic_opts),
        "Status": st.column_config.SelectboxColumn("Status", options=status_opts),
        "TOTAL SERVICE FEE": st.column_config.NumberColumn("TOTAL SERVICE FEE", format="%.0f"),
        "Total": st.column_config.NumberColumn("Total", format="%.0f"),
    }
    for mm in months:
        col_cfg[mm] = st.column_config.NumberColumn(mm, format="%.0f")

    edited = st.data_editor(
        fv, use_container_width=True, hide_index=True, key="sales_db_editor",
        disabled=[c for c in fv.columns if c not in SO.EDITABLE], column_config=col_cfg)

    changed = 0
    for i in range(len(fv)):
        k = SO.make_key(year, edited.at[i, "Company"], edited.at[i, "Engagement"])
        for fld in SO.EDITABLE:
            if str(fv.at[i, fld]) != str(edited.at[i, fld]):
                SO.set_override(k, fld, edited.at[i, fld], current_user().get("name", ""))
                changed += 1
    if changed:
        st.success(f"Saved {changed} change(s).")

    # Monthly totals row at the bottom.
    _num_cols = months + ["TOTAL SERVICE FEE", "Total"]
    totals = {c: "" for c in edited.columns}
    totals[edited.columns[0]] = "TOTAL"
    for c in _num_cols:
        if c in edited.columns:
            totals[c] = pd.to_numeric(edited[c], errors="coerce").sum()
    tot_df = pd.DataFrame([totals])[edited.columns]
    st.markdown("**Monthly totals**")
    st.dataframe(
        tot_df.style.format({c: "{:,.0f}" for c in _num_cols if c in tot_df.columns}),
        use_container_width=True, hide_index=True)

    import io as _io
    _buf = _io.BytesIO()
    with pd.ExcelWriter(_buf, engine="xlsxwriter") as _xw:
        edited.to_excel(_xw, index=False, sheet_name=f"Credit Control {year}")
    dl = st.columns(2)
    dl[0].download_button("⬇️ Download (Excel)", _buf.getvalue(),
                          file_name=f"credit_control_{year}.xlsx",
                          mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                          use_container_width=True)
    dl[1].download_button("⬇️ Download (CSV)", edited.to_csv(index=False).encode("utf-8-sig"),
                          file_name=f"credit_control_{year}.csv", mime="text/csv",
                          use_container_width=True)


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
    m[2].metric("Total value", money_compact(view["Total value"].sum()),
                help=money(view["Total value"].sum()))
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
_pages = ["📊 Dashboard", "📝 Report Ordered Quotation", _complete_label,
          "🧾 Sales Database", "🗂️ Client Database"]
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
if page == "🧾 Sales Database":
    render_sales_database()
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

df = None          # canonical frame for monitoring (selected years)
df_full = None     # canonical frame across ALL years (for YoY / YTD comparison)
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
    df_full = W.to_canonical(tidy, DIMENSIONS[dim_label])

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
            st.warning("DATABASE_URL not detected — using manual connection. "
                       "On the cloud, add it to the app's Secrets and reboot.")
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
    _full_tidy = st.session_state.get("db_tidy")
    if _full_tidy is not None:
        df_full = DB.to_canonical(_full_tidy, dim_field)

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
            df_full = W.to_canonical(tidy, DIMENSIONS[dim_label])
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

if df_full is None:
    df_full = df

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
    st.warning(f"🔔 {_pending} billing item(s) pending (initial / final 50%) — "
               "see the **Due for Billing** tab or **✅ Complete Engagement** page.")

# Drill-down view: when a leaderboard item is selected, show its detail page.
_drill = st.session_state.get("drill")
if _drill:
    if st.button("← Back to dashboard", key="back_top"):
        st.session_state.pop("drill", None)
        st.rerun()
    if _drill["type"] == "client":
        _sales_detail(f"Client: {_drill['value']}", df[df["client"] == _drill["value"]])
    elif _drill["type"] == "department":
        _sales_detail(f"{dim_label}: {_drill['value']}", df[df["department"] == _drill["value"]])
    else:
        _lab = df["engagement"].astype(str) + " · " + df["client"].astype(str)
        _sales_detail(f"Engagement: {_drill['value']}", df[_lab == _drill["value"]])
    st.divider()
    if st.button("← Back to dashboard", key="back_bottom", use_container_width=True):
        st.session_state.pop("drill", None)
        st.rerun()
    st.stop()

summary = M.department_summary(df, cfg)
total_inv = df["invoiced"].sum()
total_rec = df["received"].sum()
total_ar = df["outstanding"].sum()
# Due for billing = pending billing notifications (reported quotations + completed
# engagements) whose due month is the current (as-of) month.
_cur_month = as_of.strftime("%Y-%m")
_bill = EN.load_notifications(status="pending")
if not _bill.empty:
    _bill = _bill.copy()
    _bill["DueMonth"] = _bill["due_month"].map(EN.normalize_month)
    due = _bill[_bill["DueMonth"] == _cur_month]
else:
    due = _bill
due_amt = pd.to_numeric(due["amount"], errors="coerce").sum() if not due.empty else 0.0
od = M.overdue_detail(df, cfg, as_of)
hr = od[od["High Risk"]] if not od.empty else od
hr_amt = float(hr["Outstanding"].sum()) if not hr.empty else 0.0
coll_pct = (total_rec / total_inv * 100) if total_inv else 0.0

# Year-to-date sales + prior-year same-period comparison (from the all-years frame,
# so it works even when only the current year is selected).
ytd_sales, ytd_prior, yoy_ytd = total_inv, 0.0, None
if df_full is not None and "date" in df_full.columns and df_full["date"].notna().any():
    _base = df_full[df_full["department"].isin(chosen)] if chosen else df_full
    _y = as_of.year
    _prior_asof = as_of - pd.DateOffset(years=1)
    ytd_sales = _base.loc[(_base["date"] >= pd.Timestamp(_y, 1, 1))
                          & (_base["date"] <= as_of), "invoiced"].sum()
    ytd_prior = _base.loc[(_base["date"] >= pd.Timestamp(_y - 1, 1, 1))
                          & (_base["date"] <= _prior_asof), "invoiced"].sum()
    if ytd_prior:
        yoy_ytd = (ytd_sales - ytd_prior) / ytd_prior * 100

# Monthly totals (for the MoM delta and the trend chart).
_mt = df.dropna(subset=["date"]).copy()
monthly = None
mom = None
if not _mt.empty:
    _mt["Month"] = _mt["date"].dt.to_period("M").dt.to_timestamp()
    monthly = _mt.groupby("Month")[["invoiced", "received"]].sum().sort_index()
    if len(monthly) >= 2 and monthly["invoiced"].iloc[-2]:
        mom = (monthly["invoiced"].iloc[-1] - monthly["invoiced"].iloc[-2]) / monthly["invoiced"].iloc[-2] * 100


def _kpi(col, label, value, delta=None, delta_color="normal", help=None):
    with col.container(border=True):
        st.metric(label, value, delta=delta, delta_color=delta_color, help=help)


st.markdown(
    "<style>[data-testid='stMetricValue']{font-size:1.7rem;}</style>",
    unsafe_allow_html=True)

k = st.columns(6)
_kpi(k[0], f"YTD sales ({as_of.year})", money_compact(ytd_sales),
     f"{yoy_ytd:+.0f}% vs {as_of.year - 1}" if yoy_ytd is not None else f"through {as_of.date()}",
     "normal" if yoy_ytd is not None else "off",
     help=f"{money(ytd_sales)} · Jan 1–{as_of.date()} vs same period {as_of.year - 1} "
          f"({money(ytd_prior)}).")
_kpi(k[1], "Billed (selected)", money_compact(total_inv),
     f"{mom:+.0f}% MoM" if mom is not None else None, help=money(total_inv))
_kpi(k[2], "Collected", money_compact(total_rec), f"{coll_pct:.0f}% collection rate",
     "off", help=money(total_rec))
_kpi(k[3], "Outstanding AR", money_compact(total_ar),
     f"{total_ar / total_inv * 100:.0f}% of billed" if total_inv else None,
     "off", help=money(total_ar))
_kpi(k[4], "High-risk overdue", money_compact(hr_amt), f"{len(hr)} items",
     "off", help=money(hr_amt))
_kpi(k[5], "Due for billing", f"{len(due)} items", money(due_amt), "off",
     help=f"Current month ({_cur_month}) — reported quotations + completions.")

# --------------------------------------------------------------------------- #
# At a glance
# --------------------------------------------------------------------------- #
st.markdown("#### At a glance")
g1, g2 = st.columns([3, 2])
with g1:
    if monthly is not None and not monthly.empty:
        long = monthly.reset_index().melt(
            "Month", value_vars=["invoiced", "received"], var_name="Metric", value_name="Amount")
        long["Metric"] = long["Metric"].map({"invoiced": "Billed", "received": "Collected"})
        fig = px.area(long, x="Month", y="Amount", color="Metric",
                      title="Billings vs collections by month",
                      color_discrete_map={"Billed": "#378ADD", "Collected": "#1D9E75"})
        fig.update_layout(height=290, margin=dict(t=40, b=0, l=0, r=0),
                          legend=dict(orientation="h", y=1.12, title=None))
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No dated data to chart.")
with g2:
    aging = M.ar_aging(df, cfg, as_of)
    buckets = [c for c in aging.columns if c not in ("Department", "Total AR")]
    if not aging.empty and buckets and aging[buckets].to_numpy().sum() > 0:
        tot = aging[buckets].sum()
        ad = pd.DataFrame({"Bucket": tot.index, "Amount": tot.values})
        fig = px.pie(ad, names="Bucket", values="Amount", hole=0.58, title="AR aging",
                     color_discrete_sequence=["#9FE1CB", "#FAC775", "#F0997B", "#E24B4A", "#A32D2D"])
        fig.update_layout(height=290, margin=dict(t=40, b=0, l=0, r=0),
                          legend=dict(orientation="h", y=-0.1, title=None))
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No open AR.")

# --------------------------------------------------------------------------- #
# Leaderboards
# --------------------------------------------------------------------------- #
st.markdown("#### Leaderboards")


def _lb_chart(agg, title, n=8):
    agg = agg[agg["Amount"] > 0].sort_values("Amount", ascending=True).tail(n)
    if agg.empty:
        st.caption(f"No data for {title.lower()}.")
        return False
    fig = px.bar(agg, x="Amount", y="Label", orientation="h", title=title, text="Amount")
    fig.update_traces(marker_color="#534AB7", texttemplate="%{text:,.0f}",
                      textposition="outside", cliponaxis=False)
    fig.update_layout(height=300, margin=dict(t=40, b=0, l=0, r=30),
                      yaxis_title=None, xaxis_title=None, xaxis_visible=False, showlegend=False)
    st.plotly_chart(fig, use_container_width=True)
    return True


def _drill_to(dtype, value):
    st.session_state["drill"] = {"type": dtype, "value": value}
    st.rerun()


lb = st.columns(3)
with lb[0]:
    _cl = df.groupby("client")["invoiced"].sum().reset_index()
    _cl.columns = ["Label", "Amount"]
    _lb_chart(_cl, "Top clients (billed)")
with lb[1]:
    _dp = df.groupby("department")["invoiced"].sum().reset_index()
    _dp.columns = ["Label", "Amount"]
    _lb_chart(_dp, f"Top {dim_label.lower()}s (billed)")
with lb[2]:
    _en = df.copy()
    _en["Label"] = _en["engagement"].astype(str) + " · " + _en["client"].astype(str)
    _ea = _en.groupby("Label")["invoiced"].sum().reset_index().rename(columns={"invoiced": "Amount"})
    _lb_chart(_ea, "Top engagements (billed)")

# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #
alerts = M.build_alerts(df, cfg, as_of)
# Billing alerts from reported quotations + completions (same source as Due for Billing).
_bill_for_alerts = _bill.copy() if (_bill is not None and not _bill.empty) else pd.DataFrame()
if not _bill_for_alerts.empty and "DueMonth" not in _bill_for_alerts.columns:
    _bill_for_alerts["DueMonth"] = _bill_for_alerts["due_month"].map(EN.normalize_month)
alerts = M.sort_alerts(alerts + M.billing_alerts(_bill_for_alerts, _cur_month))
st.markdown("#### Monitoring")
if not alerts:
    st.success("✅ No alerts — all departments within thresholds.")
else:
    sev = {s: sum(1 for a in alerts if a["severity"] == s) for s in ("high", "medium", "low")}
    st.markdown(
        f"<span style='background:#FCEBEB;color:#A32D2D;padding:3px 12px;border-radius:12px;margin-right:8px'>"
        f"● High {sev['high']}</span>"
        f"<span style='background:#FAEEDA;color:#854F0B;padding:3px 12px;border-radius:12px;margin-right:8px'>"
        f"● Medium {sev['medium']}</span>"
        f"<span style='background:#F1EFE8;color:#5F5E5A;padding:3px 12px;border-radius:12px'>"
        f"● Low {sev['low']}</span>", unsafe_allow_html=True)
    st.write("")
    icon = {"high": "🔴", "medium": "🟠", "low": "🟡"}
    cols = st.columns(2)
    for n, a in enumerate(alerts[:12]):
        with cols[n % 2].container(border=True):
            st.markdown(f"{icon.get(a['severity'], '•')} **[{a['category']}]** {a['message']}")
    if len(alerts) > 12:
        st.caption(f"…and {len(alerts) - 12} more (see the Excel report).")

st.divider()

# --------------------------------------------------------------------------- #
# Tabs
# --------------------------------------------------------------------------- #
t1, t2, t3, t4 = st.tabs(
    [f"📈 Sales by {dim_label}", "📉 Trends", "💳 Credit / AR", "🧾 Due for Billing"])

with t1:
    show_target = (summary["Target"] > 0).any()
    disp = summary.rename(columns={"Invoiced": "Billed", "Received": "Collected"})
    c1, c2 = st.columns([3, 2])
    with c1:
        ycols = ["Billed", "Collected"] + (["Target"] if show_target else [])
        st.plotly_chart(
            px.bar(disp, x="Department", y=ycols, barmode="group",
                   title=f"Billed / Collected by {dim_label}",
                   color_discrete_map={"Billed": "#378ADD", "Collected": "#1D9E75",
                                       "Target": "#9ca3af"}),
            use_container_width=True)
    with c2:
        st.plotly_chart(
            px.pie(disp, names="Department", values="Billed", title="Billing share"),
            use_container_width=True)

    # Hide the target columns entirely when no targets are configured.
    if not show_target:
        disp = disp.drop(columns=["Target", "Attainment %", "Gap to Target"], errors="ignore")
    cols_order = [c for c in ["Department", "Billed", "Collected", "Outstanding",
                              "Engagements", "Clients", "Collection %",
                              "Target", "Attainment %", "Gap to Target"] if c in disp.columns]
    disp = disp[cols_order]
    fmt = {"Billed": "{:,.0f}", "Collected": "{:,.0f}", "Outstanding": "{:,.0f}",
           "Target": "{:,.0f}", "Gap to Target": "{:,.0f}",
           "Attainment %": "{:.1f}", "Collection %": "{:.1f}"}
    st.dataframe(
        disp.style.format({k: v for k, v in fmt.items() if k in disp.columns}, na_rep="—"),
        use_container_width=True, hide_index=True)

with t2:
    st.markdown("**Year-over-year by month** — each line is a year, Jan–Dec")
    _yb = (df_full[df_full["department"].isin(chosen)] if chosen else df_full)
    _yb = _yb.dropna(subset=["date"]).copy() if _yb is not None else pd.DataFrame()
    if _yb.empty:
        st.info("No dated rows to compare across years.")
    else:
        _months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        _yb["Year"] = _yb["date"].dt.year.astype(str)
        _yb["MonthNum"] = _yb["date"].dt.month
        yoy = _yb.groupby(["Year", "MonthNum"])["invoiced"].sum().reset_index()
        yoy["Month"] = yoy["MonthNum"].map(lambda m: _months[m - 1])
        figyoy = px.line(yoy.sort_values("MonthNum"), x="Month", y="invoiced",
                         color="Year", markers=True, title="Monthly billings by year")
        figyoy.update_xaxes(categoryorder="array", categoryarray=_months)
        figyoy.update_layout(yaxis_title="Billed", legend_title="Year")
        st.plotly_chart(figyoy, use_container_width=True)

        # Latest-month YoY summary (this year's latest month vs same month last year).
        latest = yoy["MonthNum"].max()
        cur_y = as_of.year
        cur_v = yoy[(yoy["Year"] == str(cur_y)) & (yoy["MonthNum"] == latest)]["invoiced"].sum()
        prev_v = yoy[(yoy["Year"] == str(cur_y - 1)) & (yoy["MonthNum"] == latest)]["invoiced"].sum()
        cyt = yoy[yoy["Year"] == str(cur_y)]["invoiced"].sum()
        pyt = yoy[yoy["Year"] == str(cur_y - 1)]["invoiced"].sum()
        mc = st.columns(2)
        mc[0].metric(f"{_months[latest - 1]} {cur_y} vs {cur_y - 1}", money_compact(cur_v),
                     f"{(cur_v - prev_v) / prev_v * 100:+.0f}%" if prev_v else "—",
                     help=money(cur_v))
        mc[1].metric(f"{cur_y} vs {cur_y - 1} (full-year to date)", money_compact(cyt),
                     f"{(cyt - pyt) / pyt * 100:+.0f}%" if pyt else "—", help=money(cyt))

    st.divider()
    trend = M.monthly_trend(df)
    if trend.empty:
        st.info("No dated rows to plot trends.")
    else:
        st.markdown(f"**Monthly billings by {dim_label}** (selected years)")
        st.plotly_chart(
            px.line(trend, x="Month", y="Invoiced", color="Department", markers=True,
                    title=f"Monthly billings by {dim_label}"),
            use_container_width=True)
        st.dataframe(
            M.trend_metrics(df).style.format(
                {"Invoiced": "{:,.0f}", "MoM %": "{:.1f}", "YoY %": "{:.1f}"}, na_rep="—"),
            use_container_width=True)

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
    st.markdown("#### 🔔 Due for billing — reported quotations (initial 50%) & "
                "completed engagements (final 50%)")
    only_cur = st.checkbox(f"Current month only ({_cur_month})", value=True)
    fin = EN.load_notifications(status="pending")
    if fin.empty:
        st.info("Nothing due. Submit a quotation (flags the initial 50%) or complete an "
                "engagement (flags the final 50%).")
    else:
        fin = fin.copy()
        fin["DueMonth"] = fin["due_month"].map(EN.normalize_month)
        fin["Stage"] = fin["type"].map({"initial_50_billing": "Initial 50%",
                                        "final_50_billing": "Final 50%"}).fillna(fin["type"])
        view = fin[fin["DueMonth"] == _cur_month] if only_cur else fin
        if view.empty:
            st.info(f"No billables due in {_cur_month}. Untick 'Current month only' to see all.")
        else:
            tbl = pd.DataFrame({
                "Stage": view["Stage"],
                "Company": view["company"],
                "Service": view.get("service", ""),
                "Quotation": view.get("quotation_number", ""),
                "Line": view.get("line_no", ""),
                "Amount": pd.to_numeric(view["amount"], errors="coerce"),
                "Due month": view["DueMonth"],
                "Raised": view["created_at"],
            })
            c = st.columns(3)
            c[0].metric("Billables", f"{len(tbl)}")
            c[1].metric("Initial 50%",
                        money_compact(tbl.loc[tbl["Stage"] == "Initial 50%", "Amount"].sum()))
            c[2].metric("Final 50%",
                        money_compact(tbl.loc[tbl["Stage"] == "Final 50%", "Amount"].sum()))
            st.dataframe(tbl.style.format({"Amount": "{:,.0f}"}),
                         use_container_width=True, hide_index=True)
            st.caption("Mark items billed on the **✅ Complete Engagement** page.")
