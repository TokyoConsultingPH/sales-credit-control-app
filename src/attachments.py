"""File attachments for ordered quotations.

Local mode: files are written to data/attachments/ and referenced by their
stored filename. Cloud mode (DATABASE_URL set): files are stored in a
'quotation_files' table (BYTEA) so they persist on the ephemeral cloud disk.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ATTACH_DIR = ROOT / "data" / "attachments"
TABLE = "quotation_files"


def _use_db() -> bool:
    return bool(os.getenv("DATABASE_URL"))


def _engine():
    from src.db_loader import make_engine
    return make_engine({}, None, None)


def _safe(name: str) -> str:
    name = os.path.basename(str(name or "file"))
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "file"


def _stored_name(quotation_id: str, n: int, original: str) -> str:
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{_safe(quotation_id)}_{ts}_{n}_{_safe(original)}"


def _ensure_table(conn) -> None:
    from sqlalchemy import text
    conn.execute(text(
        f"CREATE TABLE IF NOT EXISTS {TABLE} ("
        "id SERIAL PRIMARY KEY, quotation_number TEXT, filename TEXT, "
        "content BYTEA, uploaded_at TIMESTAMP DEFAULT now())"))


def save_attachments(quotation_id: str, files) -> list[str]:
    """Persist uploaded files. `files` are Streamlit UploadedFile objects.
    Returns the list of stored filenames."""
    if not files:
        return []
    stored: list[str] = []

    if _use_db():
        from sqlalchemy import text
        with _engine().begin() as conn:
            _ensure_table(conn)
            for n, f in enumerate(files, start=1):
                name = _stored_name(quotation_id, n, f.name)
                conn.execute(
                    text(f"INSERT INTO {TABLE} (quotation_number, filename, content) "
                         "VALUES (:q, :f, :c)"),
                    {"q": quotation_id, "f": name, "c": f.getvalue()})
                stored.append(name)
        return stored

    ATTACH_DIR.mkdir(parents=True, exist_ok=True)
    for n, f in enumerate(files, start=1):
        name = _stored_name(quotation_id, n, f.name)
        (ATTACH_DIR / name).write_bytes(f.getvalue())
        stored.append(name)
    return stored


def get_attachment_bytes(filename: str) -> bytes | None:
    """Return the bytes for a stored attachment, or None if missing."""
    if not filename:
        return None
    if _use_db():
        from sqlalchemy import text
        try:
            with _engine().connect() as conn:
                row = conn.execute(
                    text(f"SELECT content FROM {TABLE} WHERE filename = :f LIMIT 1"),
                    {"f": filename}).scalar()
            return bytes(row) if row is not None else None
        except Exception:
            return None
    path = ATTACH_DIR / _safe(filename)
    return path.read_bytes() if path.exists() else None
