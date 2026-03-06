"""
pages/scan_order.py — Quick Supply Order

Cart is encoded in the URL as ?cart=PID:QTY,PID:QTY so it survives
across QR code scans (each scan is a new browser navigation but carries
the existing cart forward in the URL).

Individual QR code format:  /scan_order?product_number=ABC&qty=2
When scanned, the new item is merged into the existing cart param and
the page reloads with the full cart in the URL. One submit = one email.
"""

import streamlit as st
import pandas as pd
from pathlib import Path
import sys
import re

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from db.supabase_client import append_log
from data.catalog import read_catalog
from services.email_service import send_email, smtp_ok, all_recipients

st.set_page_config(page_title="Quick Supply Order", page_icon="📱", layout="centered")

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


def decode_cart(s: str) -> dict:
    cart = {}
    for chunk in s.split(","):
        chunk = chunk.strip()
        if ":" in chunk:
            pid, qty = chunk.rsplit(":", 1)
            try:
                cart[pid.strip()] = max(0, int(qty))
            except ValueError:
                pass
    return cart


def encode_cart(cart: dict) -> str:
    return ",".join(f"{pid}:{qty}" for pid, qty in cart.items() if qty > 0)


# ── Load data ──────────────────────────────────────────────────────────────────
catalog = read_catalog()
emails_df = read_emails()

if catalog.empty:
    st.error("Catalog is not available.")
    st.stop()

catalog["product_number"] = catalog["product_number"].astype(str)

multiplier_map = {
    str(r["product_number"]): int(r.get("multiplier", 1) or 1)
    for _, r in catalog.iterrows()
}

# ── Parse URL — merge new scan into existing cart ─────────────────────────────
params               = st.query_params
product_number_param = params.get("product_number", "").strip()
qty_param            = params.get("qty", "").strip()
cart_param           = params.get("cart", "").strip()
name_param           = params.get("name", "").strip()

# Start from cart already in URL
cart = decode_cart(cart_param)

# Merge newly scanned item
just_scanned = None
if product_number_param and product_number_param in multiplier_map:
    try:
        qty = max(1, int(qty_param)) if qty_param else multiplier_map[product_number_param]
    except ValueError:
        qty = multiplier_map[product_number_param]
    cart[product_number_param] = qty
    just_scanned = product_number_param

# ── Header ─────────────────────────────────────────────────────────────────────
st.markdown(
    '<h2 style="text-align:center;padding:0.5rem 0 0.1rem">📱 Quick Supply Order</h2>',
    unsafe_allow_html=True,
)

if just_scanned:
    row = catalog.loc[catalog["product_number"] == just_scanned]
    name = row.iloc[0]["item"] if not row.empty else just_scanned
    st.success(f"✅ **{name}** added — {len(cart)} item(s) in cart. Scan more or submit.")
elif cart:
    st.info(f"🛒 {len(cart)} item(s) in cart. Scan more QR codes or submit.")
else:
    st.markdown(
        '<p style="text-align:center;color:#666;margin-bottom:0">'
        'Scan a product QR code to start your order.</p>',
        unsafe_allow_html=True,
    )

st.divider()

# ── Name — persisted in URL so it survives scans ──────────────────────────────
orderer_name = st.text_input(
    "Your name (optional)",
    value=name_param,
    placeholder="Leave blank to submit as Anonymous",
)

st.divider()

# ── Cart — editable table ──────────────────────────────────────────────────────
st.markdown("### 🛒 Cart")

order_items = []
for pid, qty in cart.items():
    if qty > 0:
        row = catalog.loc[catalog["product_number"] == pid]
        if not row.empty:
            order_items.append({
                "item":           row.iloc[0]["item"],
                "product_number": pid,
                "rec_qty":        int(row.iloc[0].get("multiplier", 1) or 1),
                "qty":            qty,
            })

if order_items:
    cart_df = pd.DataFrame(order_items)
    edited = st.data_editor(
        cart_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "item":           st.column_config.TextColumn("Item", disabled=True),
            "product_number": st.column_config.TextColumn("Product #", disabled=True),
            "rec_qty":        st.column_config.NumberColumn("Rec. Qty", disabled=True),
            "qty":            st.column_config.NumberColumn("Qty", min_value=0, step=1),
        },
        key="cart_editor",
    )
    # Sync manual edits back into cart dict
    for _, r in edited.iterrows():
        pid     = str(r["product_number"])
        new_qty = int(r["qty"])
        cart[pid] = new_qty

    # Update URL to reflect any manual qty changes
    new_cart_str = encode_cart(cart)
    name_str     = orderer_name.strip()
    st.query_params.update({
        "cart": new_cart_str,
        **({"name": name_str} if name_str else {}),
    })

    if st.button("🧹 Clear cart"):
        st.query_params.clear()
        st.rerun()
else:
    st.caption("Cart is empty — scan a product QR code to add items.")

st.divider()

# ── Notes ─────────────────────────────────────────────────────────────────────
notes = st.text_area("Notes (optional)", placeholder="Any extra context…", height=80)

# ── Submit — one email with ALL items ─────────────────────────────────────────
submitted = st.button("🧾 Submit Order", type="primary", use_container_width=True)

if submitted:
    if not order_items:
        st.error("Cart is empty — scan some items first.")
    else:
        orderer  = orderer_name.strip() if orderer_name.strip() else "Anonymous"
        order_df = pd.DataFrame(order_items)

        with st.spinner("Logging order…"):
            when_str = append_log(order_df, orderer)

        email_sent  = False
        email_error = None
        recipients  = []

        if smtp_ok():
            recipients = all_recipients(emails_df)
            if recipients:
                product_groups, current_group, running_total, details_lines = [], [], 0.0, []
                for it in order_items:
                    pid, qty = it["product_number"], it["qty"]
                    row = catalog.loc[catalog["product_number"].astype(str) == str(pid)]
                    if not row.empty:
                        item_name = row.iloc[0]["item"]
                        price     = float(row.iloc[0].get("price", 0) or 0)
                        total     = qty * price
                        if running_total + total > 4999 and current_group:
                            product_groups.append((current_group.copy(), running_total))
                            current_group, running_total = [], 0.0
                        running_total += total
                        current_group.append(pid)
                        details_lines.append(
                            f"<label><input type='checkbox'/> - {item_name} (#{pid}): {qty}</label>"
                        )
                if current_group:
                    product_groups.append((current_group, running_total))
                group_lines = [
                    f"<label><input type='checkbox'/> "
                    f"{', '.join(chr(34)+p+chr(34) for p in g)} = ${t:,.0f}</label>"
                    for g, t in product_groups
                ]
                notes_html = f"<p><strong>Notes:</strong> {notes}</p>" if notes.strip() else ""
                body = f"""<html><body>
<p><strong>📱 New scan order at {when_str}</strong><br>Ordered by: {orderer}</p>
<p><strong>Details:</strong><br>{"<br>".join(details_lines)}</p>
<p><strong>Product:</strong><br>{"<br>".join(group_lines)}</p>
{notes_html}
</body></html>"""
                try:
                    send_email("Supply Order Logged", body, recipients)
                    email_sent = True
                except Exception as e:
                    email_error = str(e)

        st.success("✅ Order submitted!")
        st.markdown(f"**Time:** {when_str}  |  **By:** {orderer}")
        for it in order_items:
            st.markdown(f"- {it['item']} (#{it['product_number']}): **{it['qty']}**")
        if email_sent:
            st.info(f"📧 Email sent to {len(recipients)} recipient(s).")
        elif email_error:
            st.warning(f"Logged but email failed: {email_error}")

        # Clear cart
        st.query_params.clear()
        st.balloons()
