"""Login gate for the Streamlit app.

Per-user accounts live in the roster (src/registry). The env var APP_PASSWORD,
if set, is an admin master key (recovery / initial setup). On a fresh install
with no users and no APP_PASSWORD, a one-time 'create first admin' screen shows.
"""
from __future__ import annotations

import hmac
import os
from pathlib import Path
import streamlit as st

from src import registry as REG

_ASSETS = Path(__file__).resolve().parent.parent / "assets"


def _find_logo():
    for name in ("logo.png", "logo.jpg", "logo.jpeg"):
        if (_ASSETS / name).exists():
            return _ASSETS / name
    imgs = sorted(_ASSETS.glob("*.png")) + sorted(_ASSETS.glob("*.jpg")) + sorted(_ASSETS.glob("*.jpeg"))
    return imgs[0] if imgs else None


def _logo() -> None:
    p = _find_logo()
    if p:
        c = st.columns([2, 1, 2])
        c[1].image(str(p), width=200)


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


APP_NAME = "Sales & Credit Control"


def _top_bar() -> None:
    """Header bar: logo + app name on the left, firm name on the right."""
    p = _find_logo()
    left, right = st.columns([3, 2])
    with left:
        c = st.columns([1, 4])
        if p:
            c[0].image(str(p), width=56)
        c[1].markdown(
            f"<div style='padding-top:16px;font-size:1.25rem;font-weight:600'>{APP_NAME}</div>",
            unsafe_allow_html=True)
    right.markdown(
        "<div style='text-align:right;padding-top:10px'>"
        "<span style='color:#1F4E78;font-weight:700;font-size:1.1rem'>TOKYO CONSULTING FIRM</span><br>"
        "<span style='color:#6b7280;font-size:.85rem'>PH Branch</span></div>",
        unsafe_allow_html=True)
    st.divider()


def _brand_center(mid) -> None:
    p = _find_logo()
    if p:
        cc = mid.columns([1, 2, 1])
        cc[1].image(str(p), width=170)
    mid.markdown(
        "<p style='text-align:center;color:#9ca3af;letter-spacing:2px;font-size:.75rem;margin:.3rem 0 0'>"
        "TOKYO CONSULTING FIRM · PH BRANCH</p>", unsafe_allow_html=True)


def _render_login(master: str | None) -> None:
    _top_bar()
    _, mid, _ = st.columns([1, 1.4, 1])
    _brand_center(mid)
    mid.markdown(f"<h3 style='text-align:center;margin:.3rem 0 1rem'>{APP_NAME}</h3>",
                 unsafe_allow_html=True)
    with mid.form("login"):
        username = st.text_input("Email / username")
        password = st.text_input("Password", type="password")
        ok = st.form_submit_button("Log in", type="primary", use_container_width=True)
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
            mid.error("Invalid email/username or password.")


def _render_first_admin() -> None:
    _top_bar()
    _, mid, _ = st.columns([1, 1.4, 1])
    _brand_center(mid)
    mid.markdown("<h3 style='text-align:center;margin:.3rem 0'>Create the first admin</h3>",
                 unsafe_allow_html=True)
    mid.caption("No users exist yet — set up an administrator account to begin.")
    with mid.form("first_admin"):
        name = st.text_input("Full name")
        username = st.text_input("Email / username")
        p1 = st.text_input("Password", type="password")
        p2 = st.text_input("Confirm password", type="password")
        ok = st.form_submit_button("Create admin", type="primary", use_container_width=True)
    if ok:
        if not (username.strip() and p1):
            mid.error("Username and password are required.")
        elif p1 != p2:
            mid.error("Passwords do not match.")
        else:
            added, msg = REG.add_user(username, name or username, "Admin", p1)
            if added:
                st.session_state["user"] = {"username": username.strip(),
                                            "name": (name or username).strip(),
                                            "role": "Admin", "master": False}
                st.rerun()
            else:
                mid.error(msg)
