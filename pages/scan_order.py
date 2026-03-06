"""
pages/scan_order.py — Quick Supply Order

Architecture:
  - Fixed cart_id per location (e.g. "supply-room-1") lives in the URL
  - In-app camera scanner (zxing-js) reads barcodes AND QR codes
  - On scan, JS writes directly to Supabase REST API — no Python bridge
  - @st.fragment polls Supabase every 3s to refresh ONLY the cart section
  - Camera iframe is never reloaded — stays alive between scans
  - Submit reads full cart, sends one email, clears Supabase cart
"""

import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import json
import re
from pathlib import Path
import sys
import time

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from data.catalog import read_catalog
from services.email_service import send_email, smtp_ok, all_recipients
from db.supabase_client import append_log

st.set_page_config(page_title="Quick Supply Order", page_icon="📱", layout="centered")

DATA_DIR    = APP_DIR / "data"
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
    cart_upsert(cart_id, {}, "")


# ── Load data ──────────────────────────────────────────────────────────────────
catalog   = read_catalog()
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
cart_id = st.query_params.get("cart_id", "").strip()

if not cart_id:
    st.markdown(
        '<h2 style="text-align:center;padding:1rem 0 0.5rem">📱 Quick Supply Order</h2>'
        '<p style="text-align:center;color:#666">Scan your location QR code to start an order.</p>',
        unsafe_allow_html=True,
    )
    st.stop()

# ── Supabase credentials for JS ────────────────────────────────────────────────
# anon key is safe for browser — it's designed for client-side use
# Row Level Security in Supabase controls what it can access
sb_url = st.secrets["supabase"]["url"]
sb_key = st.secrets["supabase"]["key"]

catalog_json = json.dumps(multiplier_map)

# ── Header ─────────────────────────────────────────────────────────────────────
st.markdown(
    f'<h2 style="text-align:center;padding:0.5rem 0 0">📱 Quick Supply Order</h2>'
    f'<p style="text-align:center;color:#999;font-size:.8rem;margin:0">📍 {cart_id}</p>',
    unsafe_allow_html=True,
)
st.divider()

orderer_name = st.text_input(
    "Your name (optional)",
    placeholder="Leave blank to submit as Anonymous",
)
st.divider()

# ── Scanner — JS writes directly to Supabase, never touches Python ─────────────
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
  width:80%;height:35%;border:3px solid rgba(255,255,255,.9);border-radius:6px;
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
<button id="btn" onclick="toggle()">📷 Scan Item Barcode</button>
<div id="cam-box">
  <video id="vid" autoplay playsinline muted></video>
  <div id="aim"></div>
  <div id="hint">Centre barcode or QR in the box</div>
</div>
<div id="msg"></div>

<script type="module">
import {{ BrowserMultiFormatReader }}
  from 'https://cdn.jsdelivr.net/npm/@zxing/browser@0.1.5/esm/index.min.js';

const CATALOG    = {catalog_json};
const CART_ID    = "{cart_id}";
const SB_URL     = "{sb_url}";
const SB_KEY     = "{sb_key}";
const CART_TABLE = "{CART_TABLE}";

const reader   = new BrowserMultiFormatReader();
let active     = false;
let cooldown   = false;
let controls   = null;

const btn = document.getElementById('btn');
const box = document.getElementById('cam-box');
const vid = document.getElementById('vid');
const msg = document.getElementById('msg');

function setMsg(t, c) {{ msg.className = c; msg.textContent = t; }}
function toggle() {{ active ? stop() : start(); }}

async function start() {{
  setMsg('Opening camera…', 'info');
  try {{
    const devices  = await BrowserMultiFormatReader.listVideoInputDevices();
    const back     = devices.find(d => /back|rear|environment/i.test(d.label));
    const deviceId = (back || devices[devices.length - 1])?.deviceId;
    controls = await reader.decodeFromVideoDevice(
      deviceId, vid,
      (result, err) => {{ if (result && !cooldown) handleScan(result.getText()); }}
    );
    active = true;
    box.classList.add('active');
    btn.classList.add('on');
    btn.textContent = '⏹ Stop Scanner';
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
  btn.textContent = '📷 Scan Item Barcode';
  msg.className = '';
}}

async function handleScan(raw) {{
  let pid = null;
  try {{
    const u = new URL(raw);
    pid = u.searchParams.get('product_number');
  }} catch(_) {{ pid = raw.trim(); }}

  if (!pid || !(pid in CATALOG)) {{
    setMsg('Not recognised: ' + raw.slice(0, 30), 'err');
    return;
  }}

  cooldown = true;
  const qty = CATALOG[pid];
  setMsg('Saving ' + pid + '…', 'info');

  try {{
    // Fetch current cart
    const getRes = await fetch(
      `${{SB_URL}}/rest/v1/${{CART_TABLE}}?cart_id=eq.${{encodeURIComponent(CART_ID)}}&select=items`,
      {{ headers: {{ apikey: SB_KEY, Authorization: `Bearer ${{SB_KEY}}` }} }}
    );
    const rows  = await getRes.json();
    const items = rows.length && rows[0].items ? JSON.parse(rows[0].items) : {{}};

    // Merge item
    items[pid] = qty;

    // Upsert back
    const putRes = await fetch(`${{SB_URL}}/rest/v1/${{CART_TABLE}}`, {{
      method: 'POST',
      headers: {{
        apikey: SB_KEY,
        Authorization: `Bearer ${{SB_KEY}}`,
        'Content-Type': 'application/json',
        Prefer: 'resolution=merge-duplicates',
      }},
      body: JSON.stringify({{ cart_id: CART_ID, items: JSON.stringify(items), orderer: '' }}),
    }});

    if (putRes.ok) {{
      setMsg('✅ ' + pid + ' ×' + qty + ' added — scan next', 'ok');
    }} else {{
      const err = await putRes.text();
      setMsg('Save failed: ' + err.slice(0, 60), 'err');
    }}
  }} catch(e) {{
    setMsg('Network error: ' + e.message, 'err');
  }}

  setTimeout(() => {{
    cooldown = false;
    if (active) setMsg('Ready — scan next item', 'info');
  }}, 2000);
}}
</script>
</body>
</html>"""

st.markdown("### 📷 Scanner")
st.caption("Tap **Scan Item Barcode**, point at any label. Cart updates every few seconds below.")
components.html(scanner_html, height=460, scrolling=False)

st.divider()

# ── Cart — fragment so only THIS section reruns every 3s, camera iframe untouched
@st.fragment(run_every=3)
def live_cart():
    cart = cart_get(cart_id)

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
            cart_upsert(cart_id, cart)

        if st.button("🧹 Clear cart"):
            cart_clear(cart_id)
            st.rerun()
    else:
        st.caption("Cart is empty — scan item barcodes above.")

    st.divider()

    notes = st.text_area("Notes (optional)", placeholder="Any extra context…", height=80)

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

live_cart()
