"""Simple shared-password gate for the Streamlit app.

The expected password comes from the env var APP_PASSWORD (set it as a Render
environment variable in production) or from .streamlit/secrets.toml locally.
If no password is configured, the app stays open (convenient for local use).
"""
from __future__ import annotations

import hmac
import os
import streamlit as st


def _expected_password() -> str | None:
    pw = os.getenv("APP_PASSWORD")
    if pw:
        return pw
    try:
        return st.secrets["APP_PASSWORD"]  # raises if no secrets file
    except Exception:
        return None


def require_password() -> bool:
    """Return True if access is granted; otherwise render the prompt and stop."""
    expected = _expected_password()
    if not expected:
        return True  # no password configured -> open
    if st.session_state.get("auth_ok"):
        return True

    st.title("🔒 Sales & Credit Control")
    pw = st.text_input("Password", type="password", key="_pw")
    if st.button("Enter"):
        if hmac.compare_digest(pw, expected):
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    st.stop()
    return False
