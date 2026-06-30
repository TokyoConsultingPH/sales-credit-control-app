"""Generate a realistic sample sales/credit-control dataset.

Run:  python scripts/make_sample_data.py
Creates data/sample_sales.csv with the default column names from settings.yaml.
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path
import csv

random.seed(42)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "sample_sales.csv"
OUT.parent.mkdir(parents=True, exist_ok=True)

DEPARTMENTS = ["Accounting", "Tax", "Audit", "Advisory", "Payroll"]
CLIENTS = [
    "Sakura Foods Inc.", "Manila Logistics Corp.", "Nippon Steel PH", "BPO Global",
    "GreenField Realty", "Pacific Garments", "TechBridge Solutions", "Yamato Trading",
    "Cebu Resorts Group", "Metro Pharma", "AgriHarvest Co.", "Sunrise Motors",
]
MANAGERS = ["A. Tanaka", "R. Cruz", "M. Yamada", "J. Santos", "K. Reyes"]
STATUSES = ["Paid", "Partial", "Unpaid", "Draft"]

today = date(2026, 6, 30)
start = date(2025, 1, 1)


def rand_date(a: date, b: date) -> date:
    return a + timedelta(days=random.randint(0, (b - a).days))


rows = []
eng_counter = 1000
for _ in range(420):
    dept = random.choice(DEPARTMENTS)
    inv_date = rand_date(start, today)
    invoiced = round(random.uniform(40_000, 600_000), 2)

    # Collection behaviour varies by status.
    status = random.choices(STATUSES, weights=[55, 15, 22, 8])[0]
    if status == "Paid":
        received = invoiced
    elif status == "Partial":
        received = round(invoiced * random.uniform(0.3, 0.8), 2)
    elif status == "Draft":
        invoiced = 0.0          # not yet billed
        received = 0.0
    else:                       # Unpaid
        received = 0.0

    due_date = inv_date + timedelta(days=random.choice([15, 30, 45, 60]))
    # Some engagements have an upcoming/overdue next billing milestone.
    next_billing = ""
    if random.random() < 0.4:
        nb = today + timedelta(days=random.randint(-20, 40))
        next_billing = nb.isoformat()

    eng_counter += 1
    rows.append({
        "Date": inv_date.isoformat(),
        "Department": dept,
        "Client": random.choice(CLIENTS),
        "Engagement": f"ENG-{eng_counter}",
        "Manager": random.choice(MANAGERS),
        "Invoiced": invoiced,
        "Received": received,
        "DueDate": due_date.isoformat(),
        "NextBillingDate": next_billing,
        "Status": status,
    })

fields = ["Date", "Department", "Client", "Engagement", "Manager",
          "Invoiced", "Received", "DueDate", "NextBillingDate", "Status"]
with open(OUT, "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=fields)
    w.writeheader()
    w.writerows(rows)

print(f"Wrote {len(rows)} rows to {OUT}")
