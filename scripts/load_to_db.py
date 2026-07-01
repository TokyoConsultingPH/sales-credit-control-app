"""Load the TCF workbook into PostgreSQL as table 'billing_lines'.

Usage (locally, pointing at the Render DB):
    set DATABASE_URL=postgresql://user:pass@host:5432/tcf   (PowerShell: $env:DATABASE_URL=...)
    python scripts/load_to_db.py "Sample file/New-credit control 【Philippines】_2026.xlsx"

The resulting table matches the database.db_columns mapping in settings.yaml, so
the app's 'Database (PostgreSQL)' source works immediately with the default query.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd                            # noqa: E402
from src import workbook_loader as W          # noqa: E402
from src.db_loader import _normalize_url      # noqa: E402

TABLE = "billing_lines"


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        wbs = list((ROOT / "Sample file").glob("*.xlsx")) or list((ROOT / "data").glob("*.xlsx"))
        path = str(wbs[0]) if wbs else None
    if not path:
        print("No workbook found. Pass the .xlsx path as an argument.")
        return 1

    url = os.getenv("DATABASE_URL")
    if not url:
        print("Set DATABASE_URL to your PostgreSQL connection string first.")
        return 1

    from sqlalchemy import create_engine

    print(f"Parsing {path} …")
    tidy = W.load_workbook(path)
    # Store the FULL tidy schema so both the dashboard DB source and the Sales
    # Database page can rebuild everything from the database.
    out = tidy.copy()
    out["due_date"] = out["date"]
    out["next_billing_date"] = pd.NaT

    eng = create_engine(_normalize_url(url))
    print(f"Writing {len(out):,} rows to table '{TABLE}' …")
    out.to_sql(TABLE, eng, if_exists="replace", index=False, chunksize=2000)
    print("Done. The app's Database source can now query 'billing_lines'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
