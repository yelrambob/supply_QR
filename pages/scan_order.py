"""
pages/scan_order.py  —  Quick Supply Order with built-in camera QR scanner
"""

import json
import streamlit as st
import streamlit.components.v1 as components
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

# ── Session state ──────────────────────────────────────────────────────────────
if "scan_qty_map" not in st.session_state:
    st.session_state["scan_qty_map"] = {}
if "last_scanned" not in st.session_state:
    st.session_state["last_scanned"] = ""

# ── URL query params (first-scan via external QR) ─────────────────────────────
params = st.query_params
product_number_param = params.get("product_number", "")
qty_param = params.get("qty", "1")
try:
    default_qty = max(1, int(qty_param))
except (ValueError, TypeError):
    default_qty = 1

# ── Load data ──────────────────────────────────────────────────────────────────
catalog = read_catalog()
emails_df = read_emails()

if catalog.empty:
    st.error("Catalog is not available. Please contact an administrator.")
    st.stop()

catalog["product_number"] = catalog["product_number"].astype(str)

# Build a lookup: product_number -> multiplier
multiplier_map = {
    str(r["product_number"]): int(r.get("multiplier", 1) or 1)
    for _, r in catalog.iterrows()
}

# ── Pre-load item from URL param (external QR scan) ───────────────────────────
if product_number_param:
    st.session_state["scan_qty_map"][product_number_param] = default_qty

# ── Handle item posted back from the in-page camera scanner ───────────────────
scanned_pid = st.query_params.get("scanned_pid", "")
if scanned_pid and scanned_pid != st.session_state["last_scanned"]:
    rec = multiplier_map.get(scanned_pid, 1)
    st.session_state["scan_qty_map"][scanned_pid] = rec
    st.session_state["last_scanned"] = scanned_pid

# ══════════════════════════════════════════════════════════════════════════════
# Page header
# ══════════════════════════════════════════════════════════════════════════════
st.markdown("""
<style>
.scan-header { text-align:center; padding:1rem 0 0.25rem 0; }
</style>
""", unsafe_allow_html=True)

st.markdown(
    '<div class="scan-header"><h2>📱 Quick Supply Order</h2>'
    '<p style="color:#666">Scan items with the camera below, adjust quantities, then submit.</p></div>',
    unsafe_allow_html=True,
)

st.divider()

# ── Name ──────────────────────────────────────────────────────────────────────
orderer_name = st.text_input(
    "Your name (optional)",
    placeholder="Leave blank to submit as Anonymous",
)

st.divider()

# ══════════════════════════════════════════════════════════════════════════════
# In-page camera QR scanner
# Uses jsQR (CDN) to decode frames from the device camera.
# When a valid product_number URL is detected, it calls window.parent.postMessage
# which Streamlit catches via a query-param round-trip to add the item to the cart.
# ══════════════════════════════════════════════════════════════════════════════

# Pass the catalog as JSON so the scanner can validate scanned codes client-side
catalog_json = json.dumps(
    {str(r["product_number"]): int(r.get("multiplier", 1) or 1)
     for _, r in catalog.iterrows()}
)

scanner_html = f"""
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/jsqr@1.4.0/dist/jsQR.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, sans-serif; background: #f8f9fa; }}

  #scanner-wrap {{
    width: 100%;
    max-width: 480px;
    margin: 0 auto;
    padding: 12px;
  }}

  #btn-scan {{
    width: 100%;
    padding: 14px;
    font-size: 1.05rem;
    font-weight: 600;
    background: #0068c9;
    color: white;
    border: none;
    border-radius: 10px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    transition: background 0.2s;
  }}
  #btn-scan:hover {{ background: #0052a3; }}
  #btn-scan.scanning {{ background: #d62728; }}

  #cam-container {{
    display: none;
    position: relative;
    width: 100%;
    border-radius: 12px;
    overflow: hidden;
    margin-top: 12px;
    background: #000;
  }}
  #cam-container.active {{ display: block; }}

  video {{
    width: 100%;
    display: block;
  }}

  canvas {{ display: none; }}

  #overlay {{
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    width: 60%;
    aspect-ratio: 1;
    border: 3px solid rgba(255,255,255,0.8);
    border-radius: 12px;
    box-shadow: 0 0 0 9999px rgba(0,0,0,0.45);
    pointer-events: none;
  }}

  #corner {{
    position: absolute;
    top: 8px; right: 8px;
    background: rgba(0,0,0,0.6);
    color: white;
    font-size: 0.75rem;
    padding: 4px 8px;
    border-radius: 6px;
  }}

  #status {{
    margin-top: 10px;
    padding: 10px 14px;
    border-radius: 8px;
    font-size: 0.9rem;
    text-align: center;
    display: none;
  }}
  #status.ok  {{ background: #d4edda; color: #155724; display: block; }}
  #status.err {{ background: #f8d7da; color: #721c24; display: block; }}
  #status.info {{ background: #cce5ff; color: #004085; display: block; }}
</style>
</head>
<body>
<div id="scanner-wrap">
  <button id="btn-scan" onclick="toggleScanner()">
    <span>📷</span><span id="btn-label">Scan Another Item</span>
  </button>
  <div id="cam-container">
    <video id="video" autoplay playsinline muted></video>
    <canvas id="canvas"></canvas>
    <div id="overlay"></div>
    <div id="corner">Point at QR code</div>
  </div>
  <div id="status"></div>
</div>

<script>
const CATALOG = {catalog_json};
let stream = null;
let scanning = false;
let animFrame = null;
let lastSeen = "";

const video    = document.getElementById("video");
const canvas   = document.getElementById("canvas");
const ctx      = canvas.getContext("2d");
const camWrap  = document.getElementById("cam-container");
const btn      = document.getElementById("btn-scan");
const btnLabel = document.getElementById("btn-label");
const status   = document.getElementById("status");

function setStatus(msg, type) {{
  status.className = type;
  status.textContent = msg;
}}

async function toggleScanner() {{
  if (scanning) {{
    stopScanner();
  }} else {{
    await startScanner();
  }}
}}

async function startScanner() {{
  setStatus("Requesting camera…", "info");
  try {{
    stream = await navigator.mediaDevices.getUserMedia({{
      video: {{ facingMode: {{ ideal: "environment" }}, width: {{ ideal: 1280 }} }}
    }});
    video.srcObject = stream;
    await video.play();
    scanning = true;
    camWrap.classList.add("active");
    btn.classList.add("scanning");
    btnLabel.textContent = "Stop Scanner";
    status.className = "";   // hide
    tick();
  }} catch(e) {{
    setStatus("Camera access denied or unavailable: " + e.message, "err");
  }}
}}

function stopScanner() {{
  scanning = false;
  if (animFrame) cancelAnimationFrame(animFrame);
  if (stream) stream.getTracks().forEach(t => t.stop());
  stream = null;
  camWrap.classList.remove("active");
  btn.classList.remove("scanning");
  btnLabel.textContent = "Scan Another Item";
}}

function tick() {{
  if (!scanning) return;
  if (video.readyState === video.HAVE_ENOUGH_DATA) {{
    canvas.width  = video.videoWidth;
    canvas.height = video.videoHeight;
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
    const imgData = ctx.getImageData(0, 0, canvas.width, canvas.height);
    const code = jsQR(imgData.data, imgData.width, imgData.height, {{
      inversionAttempts: "dontInvert"
    }});
    if (code) {{
      handleQR(code.data);
    }}
  }}
  animFrame = requestAnimationFrame(tick);
}}

function handleQR(raw) {{
  // Parse product_number from URL or raw string
  let pid = null;
  try {{
    const url = new URL(raw);
    pid = url.searchParams.get("product_number");
  }} catch(_) {{
    // Maybe raw is just the product number itself
    pid = raw.trim();
  }}

  if (!pid) {{ setStatus("QR not recognised as a product.", "err"); return; }}
  if (!(pid in CATALOG))  {{ setStatus("Product #" + pid + " not found in catalog.", "err"); return; }}
  if (pid === lastSeen)   {{ return; }}  // debounce same code

  lastSeen = pid;
  const qty = CATALOG[pid];
  setStatus("✅ Added: #" + pid + " (qty " + qty + ") — keep scanning or tap Stop.", "ok");

  // Send to Streamlit via query param update so Python can pick it up
  const current = new URL(window.parent.location.href);
  current.searchParams.set("scanned_pid", pid);
  window.parent.history.replaceState(null, "", current.toString());
  // Trigger a Streamlit rerun by posting a tiny form
  window.parent.postMessage({{type: "streamlit:setQueryParam", key: "scanned_pid", value: pid}}, "*");

  // Also fire a direct navigation so Streamlit definitely picks it up
  setTimeout(() => {{
    window.parent.location.href = current.toString();
  }}, 800);
}}
</script>
</body>
</html>
"""

st.markdown("### 📷 Scan Items")
st.caption("Tap the button below to open the camera and scan item QR codes one at a time. Each scan adds to your cart.")

components.html(scanner_html, height=320, scrolling=False)

st.divider()

# ── Item table ────────────────────────────────────────────────────────────────
st.markdown("### 🛒 Cart")
st.caption("Items auto-added by scanning appear here. You can also manually set any qty.")

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

rerun_needed = False
for _, r in edited_df.iterrows():
    pid = str(r["product_number"])
    new_qty = int(r["qty"])
    if st.session_state["scan_qty_map"].get(pid) != new_qty:
        st.session_state["scan_qty_map"][pid] = new_qty
        rerun_needed = True
if rerun_needed:
    st.rerun()

# Order summary
order_items = []
for pid, qty in st.session_state["scan_qty_map"].items():
    if qty > 0:
        row = catalog.loc[catalog["product_number"] == pid]
        if not row.empty:
            order_items.append({"item": row.iloc[0]["item"], "product_number": pid, "qty": qty})

if order_items:
    st.markdown("### 🧾 Order Summary")
    st.dataframe(pd.DataFrame(order_items), hide_index=True, use_container_width=True)
    if st.button("🧹 Clear all"):
        st.session_state["scan_qty_map"] = {}
        st.rerun()
else:
    st.caption("No items in cart yet.")

st.divider()

# ── Notes ─────────────────────────────────────────────────────────────────────
notes = st.text_area("Notes (optional)", placeholder="Any extra context…", height=80)

# ── Submit ────────────────────────────────────────────────────────────────────
submitted = st.button("🧾 Submit Order", type="primary", use_container_width=True)

if submitted:
    if not order_items:
        st.error("Cart is empty — scan some items or set quantities above.")
    else:
        orderer = orderer_name.strip() if orderer_name.strip() else "Anonymous"
        order_df = pd.DataFrame(order_items)

        with st.spinner("Logging order…"):
            when_str = append_log(order_df, orderer)

        email_sent = False
        email_error = None
        recipients = []

        if smtp_ok():
            recipients = all_recipients(emails_df)
            if recipients:
                product_groups, current_group, running_total, details_lines = [], [], 0.0, []

                for it in order_items:
                    pid, qty = it["product_number"], it["qty"]
                    row = catalog.loc[catalog["product_number"].astype(str) == str(pid)]
                    if not row.empty:
                        item_name = row.iloc[0]["item"]
                        price = float(row.iloc[0].get("price", 0) or 0)
                        total = qty * price
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
                    f"<label><input type='checkbox'/> {', '.join(f'{chr(34)}{p}{chr(34)}' for p in g)} = ${t:,.0f}</label>"
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

        st.session_state["scan_qty_map"] = {}
        st.balloons()
