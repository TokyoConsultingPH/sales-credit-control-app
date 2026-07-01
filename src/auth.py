"""Login gate for the Streamlit app.

Per-user accounts live in the roster (src/registry). The env var APP_PASSWORD,
if set, is an admin master key (recovery / initial setup). On a fresh install
with no users and no APP_PASSWORD, a one-time 'create first admin' screen shows.
"""
from __future__ import annotations

import hmac
import os
import streamlit as st

from src import registry as REG


def _master_password() -> str | None:
    pw = os.getenv("APP_PASSWORD")
    if pw:
        return pw
    try:
        return st.secrets["APP_PASSWORD"]
    except Exception:
        return None


def current_user() -> dict:
    return st.session_state.get("user") or {}


def logout() -> None:
    st.session_state.pop("user", None)


def _has_users() -> bool:
    df = REG.list_users()
    return df is not None and not df.empty


def require_login() -> dict:
    """Block the app until a valid user is signed in; return the user dict."""
    if st.session_state.get("user"):
        return st.session_state["user"]

    master = _master_password()
    if not _has_users() and not master:
        _render_first_admin()
        st.stop()

    _render_login(master)
    st.stop()


def _render_login(master: str | None) -> None:
    st.title("🔒 Sign in")
    st.caption("Sales & Credit Control")
    with st.form("login"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        ok = st.form_submit_button("Sign in", type="primary")
    if ok:
        user = REG.verify_login(username, password)
        if user:
            st.session_state["user"] = user
            st.rerun()
        elif master and hmac.compare_digest(password or "", master):
            st.session_state["user"] = {"username": username or "admin",
                                        "name": username or "Administrator",
                                        "role": "Admin", "master": True}
            st.rerun()
        else:
            st.error("Invalid username or password.")


def _render_first_admin() -> None:
    st.title("👋 Create the first admin")
    st.caption("No users exist yet — set up an administrator account to begin.")
    with st.form("first_admin"):
        name = st.text_input("Full name")
        username = st.text_input("Username")
        p1 = st.text_input("Password", type="password")
        p2 = st.text_input("Confirm password", type="password")
        ok = st.form_submit_button("Create admin", type="primary")
    if ok:
        if not (username.strip() and p1):
            st.error("Username and password are required.")
        elif p1 != p2:
            st.error("Passwords do not match.")
        else:
            added, msg = REG.add_user(username, name or username, "Admin", p1)
            if added:
                st.session_state["user"] = {"username": username.strip(),
                                            "name": (name or username).strip(),
                                            "role": "Admin", "master": False}
                st.rerun()
            else:
                st.error(msg)
