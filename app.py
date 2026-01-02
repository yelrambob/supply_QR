import streamlit as st
import pandas as pd
from pandas.errors import EmptyDataError
import zoneinfo
from datetime import datetime
from pathlib import Path
import re
import smtplib, ssl
from email.message import EmailMessage

from db.supabase_client import (
    append_log,
    read_log,
    last_info_map,
)

st.set_page_config(page_title="Supply Ordering", page_icon="📦", layout="wide")

NYC = zoneinfo.ZoneInfo("America/New_York")
now = datetime.now(NYC).strftime("%Y-%m-%d %H:%M:%S")

# ---------------- Paths ----------------
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

CATALOG_PATH = DATA_DIR / "catalog.csv"
PEOPLE_PATH = DATA_DIR / "people.txt"
EMAILS_PATH = DATA_DIR / "emails.csv"

# ---------------- Robust file helpers ----------------
def safe_read_csv(path: Path, **kwargs) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path, encoding="utf-8", **kwargs)
    except UnicodeDecodeError:
        return pd.read_csv(path, encoding="latin-1", **kwargs)
    except EmptyDataError:
        return pd.DataFrame()
    except Exception as e:
        st.warning(f"Couldn't read {path.name}: {e}")
        return pd.DataFrame()

# ---------------- SMTP ----------------
def _split_emails(txt: str) -> list[str]:
    if not txt:
        return []
    parts = re.split(r"[;,]\s*", str(txt))
    return [p.strip() for p in parts if p.strip()]

def get_smtp_config():
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

def send_email(subject: str, body: str, to_emails: list[str] | None):
    cfg = get_smtp_config()
    recipients = (to_emails or []) + cfg.get("default_to", [])
    recipients = sorted({e for e in recipients if e and "@" in e})
    if not recipients:
        raise RuntimeError("No recipients found.")

    msg = EmailMessage()
    msg["Subject"] = f'{cfg["subject_prefix"]}{subject}' if cfg["subject_prefix"] else subject
    msg["From"] = cfg["from"]
    msg["To"] = ", ".join(recipients)
    msg.add_alternative(body, subtype="html")

    if cfg["use_ssl"]:
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=ssl.create_default_context()) as server:
            server.login(cfg["username"], cfg["password"])
            server.send_message(msg)
    else:
        with smtplib.SMTP(cfg["host"], cfg["port"]) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.login(cfg["username"], cfg["password"])
            server.send_message(msg)

# ---------------- Load core data ----------------
@st.cache_data
def read_people() -> list[str]:
    if not PEOPLE_PATH.exists():
        return []
    try:
        return [ln.strip() for ln in PEOPLE_PATH.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except Exception as e:
        st.warning(f"Couldn't read people.txt: {e}")
        return []

@st.cache_data
def read_catalog() -> pd.DataFrame:
    df = safe_read_csv(CATALOG_PATH)
    if df.empty:
        return pd.DataFrame(
            columns=[
                "item",
                "product_number",
                "multiplier",
                "items_per_order",
                "current_qty",
                "sort_order",
                "price",
            ]
        )
    # unchanged logic...
    return df.reset_index(drop=True)

def write_catalog(df: pd.DataFrame):
    df.to_csv(CATALOG_PATH, index=False)

# ---------------- UI ----------------
st.title("📦 Supply Ordering & Inventory Tracker")

people = read_people()
emails_df = read_emails()
catalog = read_catalog()
logs = read_log()
