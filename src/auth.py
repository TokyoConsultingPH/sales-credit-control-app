"""Login gate for the Streamlit app.

Per-user accounts live in the roster (src/registry), created via email invite
(src/invites) rather than an admin choosing a password for someone else — the
user picks their own username and password when they activate their invite,
and the same code mechanism powers 'forgot password'. The env var
APP_PASSWORD, if set, is an admin master key (recovery / initial setup). On a
fresh install with no users and no APP_PASSWORD, a one-time 'create first
admin' screen shows (the only case where an account is self-provisioned
without an invite, since no admin exists yet to send one).
"""
from __future__ import annotations

import hmac
import os
from pathlib import Path
import streamlit as st

from src import registry as REG
from src import invites as INV
from src import notify_email as MAIL
from src.config import load_config

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

    tab_login, tab_activate, tab_forgot = mid.tabs(
        ["🔒 Sign in", "✨ Activate invite", "❓ Forgot password"])

    with tab_login:
        with st.form("login"):
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
                st.error("Invalid email/username or password.")

    with tab_activate:
        st.caption("Have a setup code from your admin? Enter it below and choose your "
                   "own username and password.")
        with st.form("activate_invite"):
            code = st.text_input("Setup code")
            act_username = st.text_input("Choose a username")
            act_name = st.text_input("Full name")
            p1 = st.text_input("Password", type="password", key="act_p1")
            p2 = st.text_input("Confirm password", type="password", key="act_p2")
            ok2 = st.form_submit_button("Activate account", type="primary", use_container_width=True)
        if ok2:
            inv = INV.get_valid(code, "setup")
            if not inv:
                st.error("Invalid or expired code. Ask your admin to resend the invite.")
            elif not act_username.strip() or not p1:
                st.error("Choose a username and password.")
            elif p1 != p2:
                st.error("Passwords do not match.")
            else:
                final_name = (act_name or inv.get("name") or act_username).strip()
                added, msg = REG.add_user(act_username, final_name, inv.get("role") or "Staff",
                                          p1, email=inv.get("email", ""))
                if added:
                    INV.mark_used(code)
                    st.session_state["user"] = {"username": act_username.strip(), "name": final_name,
                                                "role": inv.get("role") or "Staff", "master": False}
                    st.success("Account activated — signing you in…")
                    st.rerun()
                else:
                    st.error(msg)

    with tab_forgot:
        st.caption("Enter your username or email. If it matches an account, a reset code "
                   "will be emailed to you (or your admin will be notified to assist).")
        with st.form("forgot_password"):
            ident = st.text_input("Username or email")
            ok3 = st.form_submit_button("Request reset code", use_container_width=True)
        if ok3:
            user = REG.get_user(ident) or REG.get_user_by_email(ident)
            if user:
                code = INV.create_reset_code(user["username"], user.get("email", ""),
                                             requested_by=ident)
                email = (user.get("email") or "").strip()
                if email:
                    MAIL.send(
                        load_config(), to=email, subject="Your password reset code",
                        body=f"Your password reset code is: {code}\n"
                             f"It expires in {INV.RESET_TTL_HOURS} hours.\n"
                             f"Enter it under 'Forgot password → Reset with code' "
                             f"on the app's login page.")
            # Same message whether or not a match was found — avoids revealing
            # which usernames/emails exist to an anonymous visitor.
            st.success("If that account exists, reset instructions have been sent — "
                       "or ask your admin to check pending resets in Manage Users.")

        st.divider()
        st.caption("Already have a reset code?")
        with st.form("reset_with_code"):
            rcode = st.text_input("Reset code")
            rp1 = st.text_input("New password", type="password", key="reset_p1")
            rp2 = st.text_input("Confirm new password", type="password", key="reset_p2")
            ok4 = st.form_submit_button("Reset password", type="primary", use_container_width=True)
        if ok4:
            inv = INV.get_valid(rcode, "reset")
            if not inv:
                st.error("Invalid or expired code.")
            elif not rp1:
                st.error("Enter a new password.")
            elif rp1 != rp2:
                st.error("Passwords do not match.")
            else:
                REG.set_password(inv["username"], rp1)
                INV.mark_used(rcode)
                st.success("Password reset. You can now sign in on the **Sign in** tab.")


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
