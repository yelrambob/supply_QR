"""
pages/scan_order.py
────────────────────────────────────────────────────────
Public-facing order page reachable via QR code.

URL params:
  ?product_number=<pid>&qty=<recommended_qty>

Anyone who scans the QR code lands here, enters their name,
adjusts quantities, adds more items, and submits — which logs
the order and fires the same email as the main app.
"""

import streamlit as st
import pandas as pd
import zoneinfo
from pathlib import Path
import sys
import re

# ── Make sure the parent package is importable ──────────────
APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from db.supabase_client import append_log
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

# ── Session state ────────────────────────────────────────────
if "scan_qty_map" not in st.session_state:
    st.session_state["scan_qty_map"] = {}

# ── URL query params ─────────────────────────────────────────
params = st.query_params
product_number_param = params.get("product_number", "")
qty_param = params.get("qty", "1")

try:
    default_qty = max(1, int(qty_param))
except (ValueError, TypeError):
    default_qty = 1

# ── Load data ────────────────────────────────────────────────
catalog = read_catalog()
emails_df = read_emails()

if catalog.empty:
    st.error("Catalog is not available. Please contact an administrator.")
    st.stop()

catalog["product_number"] = catalog["product_number"].astype(str)

# ── Pre-load QR item — update qty each time a new QR is scanned ─
if product_number_param:
    st.session_state["scan_qty_map"][product_number_param] = default_qty

# ══════════════════════════════════════════════════════════════
# UI
# ══════════════════════════════════════════════════════════════

st.markdown(
    """
    <style>
    .scan-header { text-align: center; padding: 1rem 0 0.5rem 0; }
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
    "Your name (optional)",
    placeholder="e.g. Jane Smith",
    help="Leave blank to submit as Anonymous.",
)

st.divider()

# ── Item table ───────────────────────────────────────────────
st.markdown("### 🛒 Select Items to Order")
st.caption("The scanned item is pre-filled. Add more items by setting their quantity above 0.")

table_rows = []
for _, row in catalog.iterrows():
    pid = str(row["product_number"])
    rec_qty = int(row.get("multiplier", 1) or 1)
    current_in_map = st.session_state["scan_qty_map"].get(pid, 0)
    table_rows.append({
        "product_number": pid,
        "item": str(row["item"]),
        "qty": current_in_map,
        "rec_qty": rec_qty,
        "price": row.get("price", None),
        "current_qty": row.get("current_qty", None),
    })

table_df = pd.DataFrame(table_rows)

edited_df = st.data_editor(
    table_df,
    use_container_width=True,
    hide_index=True,
    column_config={
        "product_number": st.column_config.TextColumn("Product #", disabled=True),
        "item": st.column_config.TextColumn("Item", disabled=True),
        "qty": st.column_config.NumberColumn("Qty to Order", min_value=0, step=1),
        "rec_qty": st.column_config.NumberColumn("Rec. Qty", disabled=True),
        "price": st.column_config.NumberColumn("Price ($)", disabled=True),
        "current_qty": st.column_config.NumberColumn("In Stock", disabled=True),
    },
    key="scan_editor",
)

# Sync edits back to session state
rerun_needed = False
for _, r in edited_df.iterrows():
    pid = str(r["product_number"])
    new_qty = int(r["qty"])
    if st.session_state["scan_qty_map"].get(pid) != new_qty:
        st.session_state["scan_qty_map"][pid] = new_qty
        rerun_needed = True
if rerun_needed:
    st.rerun()

# Build current order preview
order_items = []
for pid, qty in st.session_state["scan_qty_map"].items():
    if qty > 0:
        row = catalog.loc[catalog["product_number"] == pid]
        if not row.empty:
            order_items.append({
                "item": row.iloc[0]["item"],
                "product_number": pid,
                "qty": qty,
            })

if order_items:
    st.markdown("### 🧾 Order Summary")
    st.dataframe(pd.DataFrame(order_items), hide_index=True, use_container_width=True)
    if st.button("🧹 Clear all"):
        st.session_state["scan_qty_map"] = {}
        st.rerun()
else:
    st.caption("No items selected yet.")

st.divider()

# ── Notes ────────────────────────────────────────────────────
notes = st.text_area(
    "Notes (optional)",
    placeholder="Any additional context for this order...",
    height=80,
)

# ── Submit ───────────────────────────────────────────────────
submitted = st.button("🧾 Submit Order", type="primary", use_container_width=True)

if submitted:
    if not order_items:
        st.error("No items selected. Set at least one item qty above 0.")
    else:
        orderer = orderer_name.strip() if orderer_name.strip() else "Anonymous"
        order_df = pd.DataFrame(order_items)

        with st.spinner("Logging order..."):
            when_str = append_log(order_df, orderer)

        # ── Email — exact same logic as main app ─────────────
        email_sent = False
        email_error = None
        recipients = []

        if smtp_ok():
            recipients = all_recipients(emails_df)
            if recipients:
                product_groups = []
                current_group = []
                running_total = 0.0
                details_lines = []

                for it in order_items:
                    pid = it["product_number"]
                    qty = it["qty"]
                    row = catalog.loc[catalog["product_number"].astype(str) == str(pid)]
                    if not row.empty:
                        item_name = row.iloc[0]["item"]
                        price = float(row.iloc[0].get("price", 0) or 0)
                        total = qty * price

                        if running_total + total > 4999 and current_group:
                            product_groups.append((current_group.copy(), running_total))
                            current_group = []
                            running_total = 0.0

                        running_total += total
                        current_group.append(pid)
                        details_lines.append(
                            f"<label><input type='checkbox'/> - {item_name} (#{pid}): {qty}</label>"
                        )

                if current_group:
                    product_groups.append((current_group, running_total))

                group_lines = []
                for group, subtotal in product_groups:
                    product_str = ", ".join(f'"{p}"' for p in group)
                    group_lines.append(
                        f"<label><input type='checkbox'/> {product_str} = ${subtotal:,.0f}</label>"
                    )

                notes_html = f"<p><strong>Notes:</strong> {notes}</p>" if notes.strip() else ""

                body = f"""
                <html>
                <body>
                <p><strong>📱 New scan order at {when_str}</strong><br>
                Ordered by: {orderer}</p>

                <p><strong>Details:</strong><br>
                {"<br>".join(details_lines)}
                </p>

                <p><strong>Product:</strong><br>
                {"<br>".join(group_lines)}
                </p>

                {notes_html}
                </body>
                </html>
                """

                try:
                    send_email(
                        "Supply Order Logged",
                        body,
                        recipients,
                    )
                    email_sent = True
                except Exception as e:
                    email_error = str(e)

        # ── Confirmation ─────────────────────────────────────
        st.success("✅ Order submitted successfully!")
        st.markdown(f"**Order time:** {when_str}")
        st.markdown(f"**Submitted by:** {orderer}")
        st.markdown("**Items ordered:**")
        for it in order_items:
            st.markdown(f"- {it['item']} (#{it['product_number']}): **{it['qty']}**")

        if email_sent:
            st.info(f"📧 Email sent to {len(recipients)} recipient(s).")
        elif email_error:
            st.warning(f"Order logged but email failed: {email_error}")
        elif not smtp_ok():
            st.caption("(Email not configured — order is logged in the database.)")

        st.session_state["scan_qty_map"] = {}
        st.balloons()
