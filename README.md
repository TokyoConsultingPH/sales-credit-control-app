# Sales Reporting & Credit Control Automation

A local web app that reads the **TCF "New-credit control [Philippines]" workbook**
directly and turns it into a per-department dashboard, monitoring alerts, and a
downloadable Excel report — on demand. Built for **Tokyo Consulting Firm – Phil
Branch**. (It can also read a generic flat Excel/CSV export.)

## How it reads your workbook

It parses every `Credit Control YYYY` sheet, un-pivoting the 12 monthly blocks
(`Collect, Send, Invoiced, Complete, Fee, JPY, status, Advance`) into one tidy
row per engagement-month, and normalises the messy values (e.g. `MAKATI`→`Makati`,
`Monthly Accountng`→`Monthly Accounting`). The billing-status ladder is read as:
`2 = can be invoiced (due)` · `3 = invoiced` · `4 = sent` · `5 = collected`.

**Group by (your "department"):** switch the whole dashboard between **Branch**
(Makati/Cebu), **Category** (service line), **Classification**, or **PIC** — and
pick which **year(s)** to include (current year + history for trends).

## What it does

**Sales reporting (per department)**
- Billings vs target, collection %, engagement & client counts
- Billing-share pie and billings-vs-target bar charts
- Monthly trend lines with MoM / YoY growth per department
- One Excel sheet per department, plus summary sheets

**Sales / credit monitoring**
- 🧾 **Due for billing** — engagements with a next-billing date overdue or within a horizon
- 🎯 **Targets vs actuals** — flags departments below a target-attainment %
- 🔔 **Threshold alerts** — attainment, client concentration, high-risk overdue AR, billing
- 📈 **Trend tracking** — month-over-month and year-over-year
- 💳 **Credit / AR control** — AR aging buckets and an overdue-receivables list

## Quick start (Windows)

Double-click **`run.bat`**. The first run creates a virtual environment,
installs dependencies, generates sample data, and opens the app in your browser.

Or manually:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python scripts\make_sample_data.py   # optional demo data
streamlit run app.py
```

The app opens at http://localhost:8501.

## Using your workbook

1. Keep your `New-credit control 【Philippines】_*.xlsx` in the `Sample file\`
   (or `data\`) folder — the app finds it automatically and selects
   **TCF workbook** as the data source.
2. In the sidebar, choose **Group by** (Branch/Category/Classification/PIC),
   the **Year(s)**, and the **as-of date**.
3. Read alerts and the four tabs; click **Download Excel report** for a workbook
   with a summary, alerts, trends, AR aging, due-for-billing, and one detail
   sheet per department.

## Reporting Ordered Quotations (employee data entry)

Use the **📝 Report Ordered Quotation** page (sidebar) to log a new order. The
form mirrors the Quotation control sheet (Date, Company, Branch, Classification,
Type of service, Client type, Process of contact, Monthly / Yearly-Spot fee,
Contents) and records **Condition = Order** plus who submitted it and when.

Submissions are saved to:
- a **local file** `data/quotations.csv` when running on your computer, and
- the **PostgreSQL database** automatically when `DATABASE_URL` is set (cloud).

> On the free Streamlit Cloud host (no database) the local file resets on
> restart — connect a database for permanent multi-user storage.

## Deploying to the cloud (GitHub + Render)

The app can run on a public Render URL instead of your laptop.

> ⚠️ **The repo includes the client workbook, so the GitHub repo MUST be
> private.** Access to the live app is protected by a password (`APP_PASSWORD`).

**1. Push to a private GitHub repo**

```bash
git init
git add .
git commit -m "Sales & credit control app"
git branch -M main
# create an EMPTY PRIVATE repo on github.com, then:
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

**2. Deploy on Render**
1. Render → **New + → Blueprint** → pick this repo. Render reads
   [render.yaml](render.yaml) and creates the **web service** + a **PostgreSQL** db.
2. When prompted, set **`APP_PASSWORD`** to a strong shared password (this is the
   login for the app). `DATABASE_URL` is wired automatically from the database.
3. Wait for the build; open the service URL and log in with `APP_PASSWORD`.

Out of the box the cloud app reads the committed **TCF workbook**. To use the
Render database instead, load it once (see below) and pick the **Database** source.

**3. (Optional) Load the workbook into the Render PostgreSQL**

Copy the database's *External* connection string from Render, then locally:

```powershell
$env:DATABASE_URL = "postgresql://user:pass@host:5432/tcf"
.venv\Scripts\python scripts\load_to_db.py "Sample file\New-credit control 【Philippines】_2026.xlsx"
```

This creates a `billing_lines` table matching the default query, so the app's
**Database (PostgreSQL)** source works immediately (it auto-detects `DATABASE_URL`
on Render).

## Reading from PostgreSQL (live source)

The app can read directly from your PostgreSQL ERP/database **alongside** the
workbook (pick **Database (PostgreSQL)** as the data source).

**1. Credentials (never stored on disk).** Set an environment variable before
launching, or type the password in the sidebar:

```powershell
$env:TCF_DB_PASSWORD = "your-password"
```

**2. Connection + query.** Fill the `database:` section of
[config/settings.yaml](config/settings.yaml) — host, port, dbname, user, schema —
or override them live in the sidebar. Then either:
- put your SQL in `database.query`, or
- click **Browse schema** in the app to list tables/columns, then write the
  query in the sidebar and hit **▶ Run query & load**.

**3. Map columns.** Under `database.db_columns`, point each canonical field at the
column your query returns. The selectable "department" dimensions are
`branch`, `category`, `classification`, `pic` — include whichever you have.

| Canonical           | Meaning                                  |
|---------------------|------------------------------------------|
| `date`              | Invoice / billing date (drives trends)   |
| `branch` / `category` / `classification` / `pic` | grouping dimensions |
| `client`, `engagement` | names                                 |
| `invoiced`          | amount billed                            |
| `received`          | amount collected                         |
| `due_date`          | payment due date (AR aging)              |
| `next_billing_date` | drives "due for billing"                 |
| `status`            | billing/collection status text           |

Use **Test connection** to confirm access before loading. Outstanding AR is
computed as `invoiced − received`.

### Setting targets (optional)

Budgets aren't filled in your workbook, so **targets vs actuals** uses targets
you set in **`config\settings.yaml`** under `targets:` (keyed by the Branch or
Category name you group by). With no target, attainment shows as `—` instead of
a misleading number. Alert thresholds (target %, AR aging buckets, high-risk
days, client concentration, billing horizon) live in the same file.

## Project layout

```
app.py                     Streamlit web app (the UI)
config/settings.yaml       Targets & thresholds (+ flat-file column map)  ← edit this
src/workbook_loader.py     Parses the TCF workbook → tidy billing rows
src/config.py              Config loader
src/data_loader.py         Generic flat Excel/CSV reader (fallback)
src/monitoring.py          All reporting & monitoring calculations
src/reports.py             Excel report builder
scripts/make_sample_data.py  Demo data generator
Sample file/               Your TCF workbook lives here
run.bat                    One-click launcher (Windows)
```

## Notes
- Everything runs locally; no data leaves your machine. The workbook is read
  only — nothing is written back to it.
- Analysis is bounded to the **as-of date**: future scheduled billing months are
  excluded from KPIs/trends/AR so the report reflects the reporting date.
- 2026 PIC is mostly blank, so grouping by PIC shows "Unassigned"; Branch and
  Category are the most complete dimensions.
- A future step can read directly from your ERP/SQL database instead of the
  workbook.
