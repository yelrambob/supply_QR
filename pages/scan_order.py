"""
pages/scan_order.py — Quick Supply Order
- One general QR code opens this page
- In-app camera scanner reads barcodes/QR codes of individual items
- Cart stored in Supabase — persists across reruns without killing the camera
- Camera stays open between scans
"""

import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import json
import uuid
import re
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
def get_supabase():
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
        res = get_supabase().table(CART_TABLE).select("items").eq("cart_id", cart_id).execute()
        if res.data:
            return json.loads(res.data[0]["items"])
    except Exception:
        pass
    return {}

def cart_upsert(cart_id: str, items: dict, orderer: str = ""):
    try:
        get_supabase().table(CART_TABLE).upsert({
            "cart_id": cart_id,
            "items":   json.dumps(items),
            "orderer": orderer,
        }).execute()
    except Exception as e:
        st.warning(f"Cart save error: {e}")

def cart_delete(cart_id: str):
    try:
        get_supabase().table(CART_TABLE).delete().eq("cart_id", cart_id).execute()
    except Exception:
        pass


# ── Session state ──────────────────────────────────────────────────────────────
if "cart_id" not in st.session_state:
    # Check URL first, else generate new id
    params = st.query_params
    cid = params.get("cart_id", "").strip()
    st.session_state["cart_id"] = cid if cid else str(uuid.uuid4())[:8]
    st.query_params.update({"cart_id": st.session_state["cart_id"]})

if "last_scan" not in st.session_state:
    st.session_state["last_scan"] = ""
if "just_added" not in st.session_state:
    st.session_state["just_added"] = ""

cart_id = st.session_state["cart_id"]

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

# Load cart from Supabase
cart = cart_get(cart_id)

# ── Header ─────────────────────────────────────────────────────────────────────
st.markdown(
    '<h2 style="text-align:center;padding:0.5rem 0 0.1rem">📱 Quick Supply Order</h2>',
    unsafe_allow_html=True,
)

if st.session_state["just_added"]:
    pid = st.session_state["just_added"]
    row = catalog.loc[catalog["product_number"] == pid]
    item_name = row.iloc[0]["item"] if not row.empty else pid
    st.success(f"✅ **{item_name}** added — {len(cart)} item(s) in cart.")
    st.session_state["just_added"] = ""
elif cart:
    st.info(f"🛒 {len(cart)} item(s) in cart.")
else:
    st.caption("Scan an item barcode to start your order.")

st.divider()

orderer_name = st.text_input("Your name (optional)", placeholder="Leave blank to submit as Anonymous")
st.divider()

# ── Scanner ────────────────────────────────────────────────────────────────────
# zxing-js reads Code128 barcodes AND QR codes
# Sends scanned value to Streamlit via a hidden text input (same-origin iframe trick)
# The iframe is rendered with a fixed key so Streamlit never destroys it on rerun

catalog_json = json.dumps(multiplier_map)

scanner_html = f"""<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:transparent;padding:4px 2px}}
#btn{{width:100%;padding:15px;font-size:1.05rem;font-weight:700;background:#0068c9;
  color:#fff;border:none;border-radius:10px;cursor:pointer;transition:background .15s}}
#btn.on{{background:#c0392b}}
#cam-box{{display:none;position:relative;margin-top:10px;border-radius:12px;
  overflow:hidden;background:#000;width:100%}}
#cam-box.active{{display:block}}
video{{width:100%;display:block;max-height:300px;object-fit:cover}}
#aim{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
  width:80%;height:30%;border:3px solid rgba(255,255,255,.9);border-radius:6px;
  box-shadow:0 0 0 9999px rgba(0,0,0,.45);pointer-events:none}}
#hint{{position:absolute;bottom:10px;left:50%;transform:translateX(-50%);
  background:rgba(0,0,0,.65);color:#fff;font-size:.78rem;padding:4px 12px;
  border-radius:20px;white-space:nowrap}}
#msg{{margin-top:8px;padding:11px 14px;border-radius:8px;font-size:.9rem;
  text-align:center;display:none;font-weight:500}}
#msg.ok  {{background:#d4edda;color:#155724;display:block}}
#msg.err {{background:#f8d7da;color:#721c24;display:block}}
#msg.info{{background:#cce5ff;color:#004085;display:block}}
</style>
</head>
<body>
<button id="btn" onclick="toggle()">📷&nbsp; Scan Barcode / QR</button>
<div id="cam-box">
  <video id="vid" autoplay playsinline muted></video>
  <div id="aim"></div>
  <div id="hint">Centre barcode or QR in the box</div>
</div>
<div id="msg"></div>

<script type="module">
import {{ BrowserMultiFormatReader }} from 'https://cdn.jsdelivr.net/npm/@zxing/browser@0.1.5/esm/index.min.js';

const CATALOG = {catalog_json};
const reader  = new BrowserMultiFormatReader();
let active    = false;
let cooldown  = false;
let controls  = null;

const btn = document.getElementById('btn');
const box = document.getElementById('cam-box');
const vid = document.getElementById('vid');
const msg = document.getElementById('msg');

function setMsg(t, c) {{ msg.className = c; msg.textContent = t; }}
function toggle() {{ active ? stop() : start(); }}

async function start() {{
  setMsg('Opening camera…', 'info');
  try {{
    const devices = await BrowserMultiFormatReader.listVideoInputDevices();
    // Prefer back camera
    const device  = devices.find(d => /back|rear|environment/i.test(d.label)) || devices[devices.length - 1];
    const deviceId = device ? device.deviceId : undefined;

    controls = await reader.decodeFromVideoDevice(deviceId, vid, (result, err) => {{
      if (result && !cooldown) handleScan(result.getText());
    }});

    active = true;
    box.classList.add('active');
    btn.classList.add('on');
    btn.textContent = '⏹  Stop Scanner';
    msg.className = '';
  }} catch(e) {{
    setMsg('Camera error: ' + e.message, 'err');
  }}
}}

function stop() {{
  if (controls) {{ controls.stop(); controls = null; }}
  active = false;
  box.classList.remove('active');
  btn.classList.remove('on');
  btn.textContent = '📷  Scan Barcode / QR';
  msg.className = '';
}}

function handleScan(raw) {{
  // Accept plain product number OR full URL with ?product_number=
  let pid = null;
  try {{
    const u = new URL(raw);
    pid = u.searchParams.get('product_number');
  }} catch(_) {{
    pid = raw.trim();
  }}

  if (!pid || !(pid in CATALOG)) {{
    setMsg('Not recognised: ' + raw.slice(0, 30), 'err');
    return;
  }}

  const qty = CATALOG[pid];
  cooldown = true;
  setMsg('✅ ' + pid + '  ×' + qty + ' — scan next item', 'ok');

  // Post to parent — no navigation, camera stays alive
  window.parent.postMessage({{ type: 'barcode_scanned', pid, qty }}, '*');

  setTimeout(() => {{
    cooldown = false;
    if (active) setMsg('Ready — scan next item', 'info');
  }}, 2000);
}}
</script>
</body>
</html>"""

# Parent-side listener injected into Streamlit page
# Writes scanned value into the hidden text input via DOM manipulation
st.markdown("""
<script>
(function() {
  function handleMsg(e) {
    if (!e.data || e.data.type !== 'barcode_scanned') return;
    const val = e.data.pid + ':' + e.data.qty;
    // Find the hidden input by its aria-label
    const allInputs = document.querySelectorAll('input[type="text"]');
    for (const inp of allInputs) {
      const label = inp.closest('[data-testid="stTextInput"]');
      if (label && label.querySelector('label') &&
          label.querySelector('label').textContent.includes('__barcode_bridge')) {
        const nativeInput = Object.getOwnPropertyDescriptor(
          window.HTMLInputElement.prototype, 'value');
        nativeInput.set.call(inp, val);
        inp.dispatchEvent(new Event('input', { bubbles: true }));
        break;
      }
    }
  }
  window.addEventListener('message', handleMsg);
})();
</script>
""", unsafe_allow_html=True)

st.markdown("### 📷 Scanner")
st.caption("Tap **Scan Barcode / QR**, point at any product label. Cart updates automatically — camera stays on.")
components.html(scanner_html, height=460, scrolling=False)

# Hidden bridge input — receives barcode scans from the JS postMessage listener
bridge_val = st.text_input("__barcode_bridge", key="barcode_bridge", label_visibility="collapsed")

if bridge_val and ":" in bridge_val:
    pid, qty_str = bridge_val.split(":", 1)
    pid = pid.strip()
    try:
        qty = int(qty_str.strip())
        if pid in multiplier_map:
            cart[pid] = qty
            cart_upsert(cart_id, cart, orderer_name.strip())
            st.session_state["just_added"] = pid
            st.session_state["barcode_bridge"] = ""   # clear input
            st.rerun()
    except ValueError:
        pass

st.divider()

# ── Cart ───────────────────────────────────────────────────────────────────────
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
    # Sync manual edits to Supabase
    changed = False
    for _, r in edited.iterrows():
        pid, new_qty = str(r["product_number"]), int(r["qty"])
        if cart.get(pid) != new_qty:
            cart[pid] = new_qty
            changed = True
    if changed:
        cart_upsert(cart_id, cart, orderer_name.strip())

    if st.button("🧹 Clear cart"):
        cart_delete(cart_id)
        st.session_state["cart_id"] = str(uuid.uuid4())[:8]
        st.query_params.update({"cart_id": st.session_state["cart_id"]})
        st.rerun()
else:
    st.caption("Cart is empty — scan a barcode to add items.")

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

        cart_delete(cart_id)
        st.success("✅ Order submitted!")
        st.markdown(f"**Time:** {when_str}  |  **By:** {orderer}")
        for it in order_items:
            st.markdown(f"- {it['item']} (#{it['product_number']}): **{it['qty']}**")
        if email_sent:
            st.info(f"📧 Email sent to {len(recipients)} recipient(s).")
        elif email_error:
            st.warning(f"Logged but email failed: {email_error}")

        st.session_state["cart_id"] = str(uuid.uuid4())[:8]
        st.query_params.update({"cart_id": st.session_state["cart_id"]})
        st.balloons()
