# supply_QR/services/email_service.py

import streamlit as st
import re
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
import pandas as pd
from pandas.errors import EmptyDataError

# ---------------- Paths ----------------
APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = APP_DIR / "data"
EMAILS_PATH = DATA_DIR / "emails.csv"
EMAILS2_PATH = DATA_DIR / "emails2.csv"

# Orders from this person go only to emails2.csv (no default recipients).
SPECIAL_ORDERER = "Greg"


# ---------------- Helpers ----------------
def _split_emails(txt: str) -> list[str]:
    if not txt:
        return []
    parts = re.split(r"[;,]\s*", str(txt))
    return [p.strip() for p in parts if p.strip()]


# ---------------- SMTP config (Gmail by default) ----------------
def get_smtp_config() -> dict:
    try:
        smtp_config = st.secrets["smtp"]
        return {
            "host": smtp_config.get("host", "smtp.gmail.com"),
            "port": int(smtp_config.get("port", 587)),
            "username": smtp_config.get("user", ""),
            "password": smtp_config.get("password", "").replace(" ", ""),
            "from": smtp_config.get("from", ""),
            "subject_prefix": smtp_config.get("subject_prefix", ""),
            "default_to": _split_emails(smtp_config.get("to", "")) if smtp_config.get("to") else [],
            "use_ssl": bool(smtp_config.get("use_ssl", False)),
        }
    except Exception:
        return {}


def smtp_ok() -> bool:
    cfg = get_smtp_config()
    required = ["host", "port", "username", "password", "from"]
    return all(cfg.get(k) for k in required)


def smtp_status_text() -> str:
    """Header text: ✅, or ❌ plus which config keys were found (debug aid)."""
    if smtp_ok():
        return "✅"
    cfg = get_smtp_config()
    return f"❌ (keys found: {[k for k, v in cfg.items() if v]})"


# ---------------- Email sending ----------------
def send_email(
    subject: str,
    body_html: str,
    to_emails: list[str] | None = None,
    include_default_recipients: bool = True,
):
    cfg = get_smtp_config()

    recipients = list(to_emails or [])
    if include_default_recipients:
        recipients += cfg.get("default_to", [])
    recipients = sorted({e.strip() for e in recipients if e and "@" in e})

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


# ---------------- Recipient files ----------------
def read_email_file(path: Path) -> pd.DataFrame:
    # Key the cache on the file's mtime so edits to the CSV are picked
    # up immediately instead of serving a stale (possibly empty) list.
    mtime = path.stat().st_mtime if path.exists() else None
    return _read_email_file_cached(path, mtime)


@st.cache_data
def _read_email_file_cached(path: Path, mtime: float | None) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=["name", "email"])
    try:
        df = pd.read_csv(path)
    except EmptyDataError:
        return pd.DataFrame(columns=["name", "email"])
    except Exception as e:
        st.warning(f"Couldn't read {path.name}: {e}")
        return pd.DataFrame(columns=["name", "email"])

    df.columns = [str(c).strip().lower() for c in df.columns]

    email_re = re.compile(r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
    rows = []
    if "email" in df.columns:
        for _, r in df.iterrows():
            m = email_re.search(str(r.get("email", "")))
            if m:
                rows.append({"name": str(r.get("name", "")).strip(), "email": m.group(1)})

    return pd.DataFrame(rows, columns=["name", "email"])


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


def recipients_for_orderer(
    orderer: str,
    normal_emails_df: pd.DataFrame,
    special_emails_df: pd.DataFrame,
) -> tuple[list[str], bool]:
    """
    Returns (recipients, special_only). special_only=True means the order is
    from SPECIAL_ORDERER and goes only to emails2.csv, without the default
    SMTP recipient list.
    """
    if str(orderer).strip().casefold() == SPECIAL_ORDERER.casefold():
        file_recipients = (
            special_emails_df["email"].tolist()
            if not special_emails_df.empty
            else []
        )
        return sorted({e for e in file_recipients if e and "@" in e}), True

    return all_recipients(normal_emails_df), False


# ---------------- Email body builder ----------------
GROUP_MAX_TOTAL = 4999
# Max characters the order system accepts for a pasted group, e.g.
# "20545", "117993", "246132", "100538", "21296", "23075", "23081", "252321", "70028", "100868"
GROUP_MAX_CHARS = 93


def _group_text_length(group: list[tuple]) -> int:
    return len(", ".join(f'"{pid}"' for pid, *_ in group))


def _category_sort_key(category):
    try:
        return (0, float(category))
    except (TypeError, ValueError):
        return (1, str(category))


def build_email_body(
    qty_map: dict,
    catalog: pd.DataFrame,
    orderer: str,
    when_str: str,
    heading: str | None = None,
    notes: str = "",
) -> str:
    items = []
    for pid, qty in qty_map.items():
        if qty <= 0:
            continue
        row = catalog.loc[catalog["product_number"].astype(str) == str(pid)]
        if row.empty:
            continue
        item_name = row.iloc[0]["item"]
        price     = float(row.iloc[0].get("price", 0) or 0)
        category  = row.iloc[0].get("category", "")
        items.append((pid, qty, item_name, qty * price, category))

    # First-fit bin packing, limited by both dollar total and the
    # length of the quoted product-number list the order system accepts.
    bins: list[list[tuple]] = []
    bin_totals: list[float] = []
    for item in items:
        total = item[3]
        placed = False
        for i, bin_total in enumerate(bin_totals):
            if (
                bin_total + total <= GROUP_MAX_TOTAL
                and _group_text_length(bins[i] + [item]) <= GROUP_MAX_CHARS
            ):
                bins[i].append(item)
                bin_totals[i] += total
                placed = True
                break
        if not placed:
            bins.append([item])
            bin_totals.append(total)

    # Details are sorted by category, but the category value isn't shown.
    details_lines = [
        f"<label><input type='checkbox'/> {item_name} (#{pid}): {qty}</label>"
        for pid, qty, item_name, _, _ in sorted(items, key=lambda it: _category_sort_key(it[4]))
    ]
    group_lines = [
        f"<label><input type='checkbox'/> {', '.join(f'&quot;{pid}&quot;' for pid, *_ in grp)} = ${sub:,.0f}</label>"
        for grp, sub in zip(bins, bin_totals)
    ]

    heading = heading or f"New supply order at {when_str}"
    notes_html = f"<p><strong>Notes:</strong> {notes}</p>" if notes.strip() else ""

    return f"""<html><body>
    <p><strong>{heading}</strong><br>Ordered by: {orderer}</p>
    <p><strong>Details:</strong><br>{"<br>".join(details_lines)}</p>
    <p><strong>Product groups (≤$4,999 and ≤{GROUP_MAX_CHARS} characters each):</strong><br>{"<br>".join(group_lines)}</p>
    {notes_html}
    </body></html>"""
