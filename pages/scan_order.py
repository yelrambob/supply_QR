"""
pages/scan_order.py
────────────────────────────────────────────────────────
Public-facing order page reachable via QR code.

URL params:
  ?product_number=<pid>&qty=<recommended_qty>

Anyone who scans the QR code lands here, enters their name,
adjusts the quantity if needed, and submits — which logs the
order and fires the same email as the main app.
"""

import streamlit as st
import pandas as pd
import zoneinfo
from datetime import datetime
from pathlib import Path
import sys
import re

# ── Make sure the parent package is importable when Streamlit
#    runs this file directly from the pages/ folder ──────────
APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from db.supabase_client import append_log, read_log
from data.catalog import read_catalog
from services.email_service import send_email, smtp_ok, all_recipients

st.set_page_config(
    page_title="Quick Supply Order",
    page_icon="📱",
    layout="centered",
)

# ── Paths ────────────────────────────────────────────────────
DATA_DIR = APP_DIR / "data"
EMAILS_PATH = DATA_DIR / "emails.csv"

# ── Helpers ─────────────────────────────────────────────────
NYC = zoneinfo.ZoneInfo("America/New_York")

@st.cache_data
def read_emails() -> pd.DataFrame:
    if not EMAILS_PATH.exists():
        return pd.DataFrame(columns=["name", "email"])
    try:
        df = pd.read_csv(EMAILS_PATH)
    except Exception:
        return pd.DataFrame(columns=["name", "email"])
    df.columns = [str(c).strip().lower() for c in df.columns]
    email_re = re.compile(r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
    rows = []
    for _, r in df.iterrows():
        m = email_re.search(str(r.get("email", "")))
        if m:
            rows.append({"name": str(r.get("name", "")), "email": m.group(1)})
    return pd.DataFrame(rows)

# ── URL query params ─────────────────────────────────────────
params = st.query_params
product_number_param = params.get("product_number", "")
qty_param = params.get("qty", "1")

try:
    default_qty = max(1, int(qty_param))
except (ValueError, TypeError):
    default_qty = 1

# ── Load catalog ─────────────────────────────────────────────
catalog = read_catalog()
emails_df = read_emails()

if catalog.empty:
    st.error("Catalog is not available. Please contact an administrator.")
    st.stop()

catalog["product_number"] = catalog["product_number"].astype(str)

# ── Find the pre-selected item (if any) ─────────────────────
preselected_row = None
if product_number_param:
    match = catalog.loc[catalog["product_number"] == str(product_number_param)]
    if not match.empty:
        preselected_row = match.iloc[0]

# ══════════════════════════════════════════════════════════════
# UI
# ══════════════════════════════════════════════════════════════

st.markdown(
    """
    <style>
    .scan-header {
        text-align: center;
        padding: 1rem 0 0.5rem 0;
    }
    .item-card {
        background: #f8f9fa;
        border-radius: 12px;
        padding: 1.2rem 1.5rem;
        margin-bottom: 1rem;
        border: 1px solid #dee2e6;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    '<div class="scan-header"><h2>📱 Quick Supply Order</h2>'
    "<p>Submit a supply request — it will be logged and emailed automatically.</p></div>",
    unsafe_allow_html=True,
)

st.divider()

# ── Orderer name ─────────────────────────────────────────────
orderer_name = st.text_input(
    "Your name *",
    placeholder="e.g. Jane Smith",
    help="Required so the order can be attributed to you.",
)

st.divider()

# ── Item selection ───────────────────────────────────────────
if preselected_row is not None:
    # Single-item mode: came from a specific QR code
    st.markdown("### Item from QR code")

    item_name = str(preselected_row["item"])
    pid = str(preselected_row["product_number"])
    price = preselected_row.get("price", None)
    current_qty = preselected_row.get("current_qty", None)

    st.markdown(
        f"""
        <div class="item-card">
        <strong style="font-size:1.15rem">{item_name}</strong><br>
        <span style="color:#6c757d">Product #: <code>{pid}</code></span>
        {"<br>💵 $" + f"{float(price):.2f}" + "/unit" if price else ""}
        {"<br>🗃️ Currently in stock: " + str(int(current_qty)) if current_qty is not None else ""}
        </div>
        """,
        unsafe_allow_html=True,
    )

    order_qty = st.number_input(
        "Quantity to order",
        min_value=1,
        value=default_qty,
        step=1,
        help=f"Recommended quantity: {default_qty}",
    )

    order_items = [{"item": item_name, "product_number": pid, "qty": order_qty}]

else:
    # Multi-item mode: generic scan or direct URL visit
    st.markdown("### Select items to order")
    st.caption("Adjust quantities to 0 to skip an item.")

    item_rows = []
    for _, row in catalog.iterrows():
        rec = int(row.get("items_per_order", 1) or 1)
        item_rows.append(
            {
                "order": False,
                "item": str(row["item"]),
                "product_number": str(row["product_number"]),
                "qty": rec,
                "price": row.get("price", None),
                "current_qty": row.get("current_qty", None),
            }
        )

    item_df = pd.DataFrame(item_rows)

    edited_df = st.data_editor(
        item_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "order": st.column_config.CheckboxColumn("Order?"),
            "item": st.column_config.TextColumn("Item", disabled=True),
            "product_number": st.column_config.TextColumn("Product #", disabled=True),
            "qty": st.column_config.NumberColumn("Qty", min_value=0, step=1),
            "price": st.column_config.NumberColumn("Price ($)", disabled=True),
            "current_qty": st.column_config.NumberColumn("In Stock", disabled=True),
        },
        key="scan_editor",
    )

    order_items = []
    for _, r in edited_df.iterrows():
        if r["order"] and int(r["qty"]) > 0:
            order_items.append(
                {
                    "item": r["item"],
                    "product_number": r["product_number"],
                    "qty": int(r["qty"]),
                }
            )

st.divider()

# ── Notes (optional) ─────────────────────────────────────────
notes = st.text_area(
    "Notes (optional)",
    placeholder="Any additional context for this order...",
    height=80,
)

# ── Submit ───────────────────────────────────────────────────
st.markdown("")  # spacing

submitted = st.button("🧾 Submit Order", type="primary", use_container_width=True)

if submitted:
    if not orderer_name.strip():
        st.error("Please enter your name before submitting.")
    elif not order_items:
        st.error("No items selected. Please select at least one item with a quantity > 0.")
    else:
        order_df = pd.DataFrame(order_items)

        with st.spinner("Logging order..."):
            when_str = append_log(order_df, orderer_name.strip())

        # ── Email ────────────────────────────────────────────
        email_sent = False
        email_error = None

        if smtp_ok():
            recipients = all_recipients(emails_df)
            if recipients:
                details_lines = []
                for r in order_items:
                    details_lines.append(
                        f"<label><input type='checkbox'/> "
                        f"- {r['item']} (#{r['product_number']}): {r['qty']}</label>"
                    )

                notes_html = (
                    f"<p><strong>Notes:</strong> {notes}</p>" if notes.strip() else ""
                )

                body = f"""
                <html><body>
                <p><strong>📱 Quick scan order at {when_str}</strong><br>
                Ordered by: {orderer_name.strip()}</p>

                <p><strong>Items:</strong><br>
                {"<br>".join(details_lines)}</p>

                {notes_html}
                </body></html>
                """

                try:
                    send_email(
                        f"Supply Order — {orderer_name.strip()}",
                        body,
                        recipients,
                    )
                    email_sent = True
                except Exception as e:
                    email_error = str(e)

        # ── Confirmation ─────────────────────────────────────
        st.success("✅ Order submitted successfully!")
        st.markdown(f"**Order time:** {when_str}")
        st.markdown(f"**Submitted by:** {orderer_name.strip()}")
        st.markdown("**Items ordered:**")
        for it in order_items:
            st.markdown(f"- {it['item']} (#{it['product_number']}): **{it['qty']}**")

        if email_sent:
            st.info(f"📧 Confirmation email sent to {len(recipients)} recipient(s).")
        elif email_error:
            st.warning(f"Order logged but email failed: {email_error}")
        elif not smtp_ok():
            st.caption("(Email not configured — order is logged in the database.)")

        st.balloons()
