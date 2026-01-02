# supply_QR/services/email_service.py

import streamlit as st
import re
import smtplib
import ssl
from email.message import EmailMessage
import pandas as pd


# ---------------- Helpers ----------------
def _split_emails(txt: str) -> list[str]:
    if not txt:
        return []
    parts = re.split(r"[;,]\s*", str(txt))
    return [p.strip() for p in parts if p.strip()]


# ---------------- SMTP config ----------------
def get_smtp_config() -> dict:
    try:
        smtp_config = st.secrets["smtp"]
        return {
            "host": smtp_config.get("host"),
            "port": int(smtp_config.get("port", 587)),
            "username": smtp_config.get("user"),
            "password": smtp_config.get("password", "").replace(" ", ""),
            "from": smtp_config.get("from"),
            "subject_prefix": smtp_config.get("subject_prefix", ""),
            "default_to": _split_emails(smtp_config.get("to", "")) if smtp_config.get("to") else [],
            "use_ssl": bool(smtp_config.get("use_ssl", False)),
        }
    except Exception as e:
        st.error(f"Error reading SMTP config: {e}")
        return {}


def smtp_ok() -> bool:
    cfg = get_smtp_config()
    required = ["host", "port", "username", "password", "from"]
    return all(cfg.get(k) for k in required)


# ---------------- Email sending ----------------
def send_email(subject: str, body_html: str, to_emails: list[str] | None = None):
    cfg = get_smtp_config()

    recipients = (to_emails or []) + cfg.get("default_to", [])
    recipients = sorted({e for e in recipients if e and "@" in e})

    if not recipients:
        raise RuntimeError("No recipients found.")

    msg = EmailMessage()
    msg["Subject"] = (
        f'{cfg["subject_prefix"]}{subject}'
        if cfg["subject_prefix"]
        else subject
    )
    msg["From"] = cfg["from"]
    msg["To"] = ", ".join(recipients)
    msg.add_alternative(body_html, subtype="html")

    if cfg["use_ssl"]:
        with smtplib.SMTP_SSL(
            cfg["host"], cfg["port"], context=ssl.create_default_context()
        ) as server:
            server.login(cfg["username"], cfg["password"])
            server.send_message(msg)
    else:
        with smtplib.SMTP(cfg["host"], cfg["port"]) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.login(cfg["username"], cfg["password"])
            server.send_message(msg)


# ---------------- Recipient helpers ----------------
def all_recipients(emails_df: pd.DataFrame) -> list[str]:
    """
    Combine emails.csv + default SMTP recipients
    """
    cfg = get_smtp_config()

    file_recipients = (
        emails_df["email"].tolist()
        if not emails_df.empty and "email" in emails_df.columns
        else []
    )

    recipients = (
        {e for e in file_recipients if e}
        | {e for e in cfg.get("default_to", []) if e}
    )

    return sorted({e for e in recipients if "@" in e})
