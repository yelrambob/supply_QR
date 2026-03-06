"""
pages/scan_order.py — Quick Supply Order

How it works:
- Each supply location has a fixed cart_id (e.g. "supply-room-1")
- Item barcode labels encode: /scan_order?product_number=ABC&cart_id=supply-room-1
- Scanning any item label with phone camera adds it to that location's Supabase cart
- This page reads the cart from Supabase and shows it live
- Submit sends one email with all items, clears the cart
"""

import streamlit as st
import pandas as pd
import json
import re
import uuid
from pathlib import Path
import sys

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from data.catalog import read_catalog
from services.email_service import send_email, smtp_ok, all_recipients
from db.supabase_client import append_log

st.set_page_config(page_title="Quick Supply Order", page_icon="📱", layout="centered")

DATA_DIR = APP_DIR / "data"
EMAILS_PATH = DATA_DIR / "emails.csv"
CART_TABLE  = "pending_carts"


@st.cache_resource
def get_sb():
    from supabase import create_client
    return create_client(st.secrets["supabase"]["url"], st.secrets["supabase"]["key"])


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


# ── Supabase cart helpers ──────────────────────────────────────────────────────
def cart_get(cart_id: str) -> dict:
    try:
        res = get_sb().table(CART_TABLE).select("items").eq("cart_id", cart_id).execute()
        if res.data:
            return json.loads(res.data[0]["items"] or "{}")
    except Exception:
        pass
    return {}

def cart_upsert(cart_id: str, items: dict, orderer: str = ""):
    try:
        get_sb().table(CART_TABLE).upsert({
            "cart_id": cart_id,
            "items":   json.dumps(items),
            "orderer": orderer,
        }).execute()
    except Exception as e:
        st.warning(f"Cart save error: {e}")

def cart_clear(cart_id: str):
    try:
        get_sb().table(CART_TABLE).upsert({
            "cart_id": cart_id,
            "items":   "{}",
            "orderer": "",
        }).execute()
    except Exception:
        pass


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

# ── URL params ─────────────────────────────────────────────────────────────────
params               = st.query_params
product_number_param = params.get("product_number", "").strip()
qty_param            = params.get("qty", "").strip()
cart_id              = params.get("cart_id", "").strip()

# If no cart_id in URL, show a friendly landing page
if not cart_id:
    st.markdown(
        '<h2 style="text-align:center;padding:1rem 0 0.5rem">📱 Quick Supply Order</h2>'
        '<p style="text-align:center;color:#666">Scan an item label to start your order.<br>'
        'Make sure you are using the correct location QR code.</p>',
        unsafe_allow_html=True,
    )
    st.stop()

# ── Process incoming scan ──────────────────────────────────────────────────────
# Load current cart from Supabase
cart = cart_get(cart_id)
just_added = None

if product_number_param and product_number_param in multiplier_map:
    try:
        qty = max(1, int(qty_param)) if qty_param else multiplier_map[product_number_param]
    except ValueError:
        qty = multiplier_map[product_number_param]
    cart[product_number_param] = qty
    cart_upsert(cart_id, cart)
    just_added = product_number_param
    # Clean URL — remove product_number/qty but keep cart_id
    st.query_params.update({"cart_id": cart_id})

# ── Header ─────────────────────────────────────────────────────────────────────
st.markdown(
    f'<h2 style="text-align:center;padding:0.5rem 0 0.1rem">📱 Quick Supply Order</h2>'
    f'<p style="text-align:center;color:#888;font-size:.85rem">Cart: {cart_id}</p>',
    unsafe_allow_html=True,
)

if just_added:
    row = catalog.loc[catalog["product_number"] == just_added]
    name = row.iloc[0]["item"] if not row.empty else just_added
    st.success(f"✅ **{name}** added — {len(cart)} item(s) in cart. Scan another or submit.")
elif cart:
    st.info(f"🛒 {len(cart)} item(s) in cart. Scan more items or submit below.")
else:
    st.info("Cart is empty — scan an item barcode label to add items.")

st.divider()

orderer_name = st.text_input(
    "Your name (optional)",
    placeholder="Leave blank to submit as Anonymous",
)
st.divider()

# ── Cart — live from Supabase ─────────────────────────────────────────────────
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
    edited  = st.data_editor(
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
    changed = False
    for _, r in edited.iterrows():
        pid, new_qty = str(r["product_number"]), int(r["qty"])
        if cart.get(pid) != new_qty:
            cart[pid] = new_qty
            changed = True
    if changed:
        cart_upsert(cart_id, cart, orderer_name.strip())

    if st.button("🧹 Clear cart"):
        cart_clear(cart_id)
        st.query_params.update({"cart_id": cart_id})
        st.rerun()
else:
    st.caption("Cart is empty — scan item barcodes to add.")

st.divider()
notes = st.text_area("Notes (optional)", placeholder="Any extra context…", height=80)

# ── Submit ─────────────────────────────────────────────────────────────────────
submitted = st.button("🧾 Submit Order", type="primary", use_container_width=True)

if submitted:
    if not order_items:
        st.error("Cart is empty.")
    else:
        orderer  = orderer_name.strip() if orderer_name.strip() else "Anonymous"
        order_df = pd.DataFrame(order_items)

        with st.spinner("Logging order…"):
            when_str = append_log(order_df, orderer)

        email_sent, email_error, recipients = False, None, []

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
<p><strong>📱 Scan order [{cart_id}] at {when_str}</strong><br>Ordered by: {orderer}</p>
<p><strong>Details:</strong><br>{"<br>".join(details_lines)}</p>
<p><strong>Product:</strong><br>{"<br>".join(group_lines)}</p>
{notes_html}
</body></html>"""
                try:
                    send_email("Supply Order Logged", body, recipients)
                    email_sent = True
                except Exception as e:
                    email_error = str(e)

        cart_clear(cart_id)
        st.success("✅ Order submitted!")
        st.markdown(f"**Time:** {when_str}  |  **By:** {orderer}  |  **Location:** {cart_id}")
        for it in order_items:
            st.markdown(f"- {it['item']} (#{it['product_number']}): **{it['qty']}**")
        if email_sent:
            st.info(f"📧 Email sent to {len(recipients)} recipient(s).")
        elif email_error:
            st.warning(f"Logged but email failed: {email_error}")
        st.balloons()
