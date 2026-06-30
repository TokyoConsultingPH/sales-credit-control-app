"""PostgreSQL data source.

Connects with SQLAlchemy + psycopg2, runs a configurable query, and maps the
result to the canonical schema used by monitoring/reports. The password is read
from (in order): explicit argument -> env TCF_DB_PASSWORD -> env PGPASSWORD.
Nothing is written to disk.
"""
from __future__ import annotations

import os
from urllib.parse import quote_plus
import pandas as pd

# Dimensions that can act as "department".
DIMENSION_FIELDS = ["branch", "category", "classification", "pic"]
NUMERIC = ["invoiced", "received"]
DATES = ["date", "due_date", "next_billing_date"]


def _password(explicit: str | None) -> str:
    return explicit or os.getenv("TCF_DB_PASSWORD") or os.getenv("PGPASSWORD") or ""


def _normalize_url(url: str) -> str:
    """Render/Heroku give 'postgres://...'; SQLAlchemy + psycopg2 needs
    'postgresql+psycopg2://...'."""
    if url.startswith("postgres://"):
        url = "postgresql+psycopg2://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


def make_engine(db_cfg: dict, password: str | None = None, overrides: dict | None = None):
    """Create a SQLAlchemy engine for PostgreSQL. Imports SQLAlchemy lazily so
    the rest of the app works even if the DB drivers aren't installed.

    If DATABASE_URL is set (e.g. on Render) it takes precedence over the
    host/user/password fields.
    """
    from sqlalchemy import create_engine

    env_url = os.getenv("DATABASE_URL")
    if env_url and not overrides:
        return create_engine(_normalize_url(env_url), pool_pre_ping=True,
                             connect_args={"connect_timeout": 10})

    o = {**db_cfg, **(overrides or {})}
    pwd = _password(password or o.get("password"))
    url = (
        f"postgresql+psycopg2://{quote_plus(str(o['user']))}:{quote_plus(pwd)}"
        f"@{o['host']}:{int(o['port'])}/{o['dbname']}"
    )
    return create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 10})


def test_connection(engine) -> str:
    """Return the server version string, or raise."""
    from sqlalchemy import text
    with engine.connect() as conn:
        return conn.execute(text("SELECT version();")).scalar_one()


def list_tables(engine, schema: str = "public") -> pd.DataFrame:
    from sqlalchemy import text
    q = text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = :s ORDER BY table_name"
    )
    with engine.connect() as conn:
        return pd.read_sql(q, conn, params={"s": schema})


def list_columns(engine, table: str, schema: str = "public") -> pd.DataFrame:
    from sqlalchemy import text
    q = text(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = :s AND table_name = :t ORDER BY ordinal_position"
    )
    with engine.connect() as conn:
        return pd.read_sql(q, conn, params={"s": schema, "t": table})


def run_query(engine, sql: str) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(sql, conn)


def map_to_tidy(raw: pd.DataFrame, db_columns: dict) -> pd.DataFrame:
    """Rename/coerce a raw query result into the tidy frame the app expects.

    Produces canonical numeric/date columns plus whichever dimension columns
    (branch/category/classification/pic) were mapped and present.
    """
    out = pd.DataFrame(index=raw.index)

    def pull(canon):
        src = db_columns.get(canon)
        return raw[src] if src and src in raw.columns else None

    # Identity / text.
    for canon in ["client", "engagement", "status"] + DIMENSION_FIELDS:
        col = pull(canon)
        if col is not None:
            out[canon] = col.astype(str).str.strip()

    # Numerics.
    for canon in NUMERIC:
        col = pull(canon)
        out[canon] = pd.to_numeric(col, errors="coerce").fillna(0.0) if col is not None else 0.0

    # Dates.
    for canon in DATES:
        col = pull(canon)
        out[canon] = pd.to_datetime(col, errors="coerce") if col is not None else pd.NaT

    out["outstanding"] = (out["invoiced"] - out["received"]).clip(lower=0)
    if "date" in out:
        out["year"] = out["date"].dt.year
    return out


def to_canonical(tidy: pd.DataFrame, dimension: str) -> pd.DataFrame:
    """Build the canonical frame for monitoring, grouping by `dimension`."""
    if tidy.empty:
        return tidy
    dim = dimension if dimension in tidy.columns else None
    out = pd.DataFrame({
        "date": tidy.get("date"),
        "department": (tidy[dim] if dim else "All").astype(str) if dim else "All",
        "client": tidy.get("client", "Unknown"),
        "engagement": tidy.get("engagement", ""),
        "manager": tidy.get("pic", "Unassigned"),
        "invoiced": tidy["invoiced"],
        "received": tidy["received"],
        "due_date": tidy.get("due_date"),
        "next_billing_date": tidy.get("next_billing_date"),
        "status": tidy.get("status", ""),
        "outstanding": tidy["outstanding"],
    })
    return out


def load_from_db(cfg: dict, password: str | None = None, overrides: dict | None = None,
                 sql: str | None = None) -> pd.DataFrame:
    """End-to-end: connect, run the configured (or supplied) query, map to tidy."""
    db_cfg = cfg.get("database", {})
    query = (sql if sql is not None else db_cfg.get("query") or "").strip()
    # Strip pure-comment template so an unconfigured query is treated as empty.
    meaningful = "\n".join(
        ln for ln in query.splitlines() if ln.strip() and not ln.strip().startswith("--")
    ).strip()
    if not meaningful:
        raise ValueError("No SQL query configured. Set database.query in settings.yaml "
                         "or enter one in the app.")
    engine = make_engine(db_cfg, password, overrides)
    raw = run_query(engine, meaningful)
    return map_to_tidy(raw, db_cfg.get("db_columns", {}))


def available_dimensions(tidy: pd.DataFrame) -> list[str]:
    return [d for d in DIMENSION_FIELDS if d in tidy.columns and tidy[d].notna().any()]
