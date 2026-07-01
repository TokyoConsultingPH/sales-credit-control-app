"""Best-effort email notifications (SMTP).

Configuration comes from settings.yaml `email:` plus the env var
TCF_SMTP_PASSWORD for the password. If email isn't configured, send() returns
(False, reason) and the caller falls back to the in-app notification only.
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage


def _to_list(v) -> list[str]:
    if not v:
        return []
    return [v] if isinstance(v, str) else list(v)


def status(cfg: dict) -> tuple[bool, str]:
    """Return (configured, human message)."""
    e = cfg.get("email", {}) or {}
    if not e.get("enabled"):
        return False, "Email is disabled (set email.enabled: true in settings.yaml)."
    if not e.get("smtp_host") or not e.get("from_addr") or not _to_list(e.get("to_addrs")):
        return False, "Email not fully configured (smtp_host, from_addr, to_addrs)."
    if not (os.getenv("TCF_SMTP_PASSWORD") or e.get("smtp_password")):
        return False, "No SMTP password (set env TCF_SMTP_PASSWORD)."
    return True, "Email configured."


def send(cfg: dict, subject: str, body: str) -> tuple[bool, str]:
    ok, msg = status(cfg)
    if not ok:
        return False, msg
    e = cfg["email"]
    recipients = _to_list(e["to_addrs"])
    password = os.getenv("TCF_SMTP_PASSWORD") or e.get("smtp_password")
    user = e.get("smtp_user") or e["from_addr"]

    em = EmailMessage()
    em["Subject"] = subject
    em["From"] = e["from_addr"]
    em["To"] = ", ".join(recipients)
    em.set_content(body)

    try:
        port = int(e.get("smtp_port", 587))
        if port == 465:
            with smtplib.SMTP_SSL(e["smtp_host"], port, context=ssl.create_default_context(),
                                  timeout=20) as s:
                s.login(user, password)
                s.send_message(em)
        else:
            with smtplib.SMTP(e["smtp_host"], port, timeout=20) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(user, password)
                s.send_message(em)
        return True, f"Emailed {len(recipients)} recipient(s)."
    except Exception as exc:
        return False, f"Email failed: {exc}"
