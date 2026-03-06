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
# FIX: clear cart flag handled at very top before any rendering
if "do_clear" not in st.session_state:
    st.session_state["do_clear"] = False

if st.session_state["do_clear"]:
    st.session_state["scan_qty_map"] = {}
    st.session_state["do_clear"] = False

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

# ── URL query params ───────────────────────────────────────────────────────────
params               = st.query_params
product_number_param = params.get("product_number", "")
qty_param            = params.get("qty", "1")
scanned_pid          = params.get("scanned_pid", "")

try:
    default_qty = max(1, int(qty_param))
except (ValueError, TypeError):
    default_qty = 1

# Pre-load from external QR URL
if product_number_param:
    st.session_state["scan_qty_map"][product_number_param] = default_qty

# FIX: always process scanned_pid if present — remove debounce which was blocking adds
if scanned_pid:
    rec = multiplier_map.get(scanned_pid, 1)
    st.session_state["scan_qty_map"][scanned_pid] = rec
    st.query_params.pop("scanned_pid", None)

# ══════════════════════════════════════════════════════════════════════════════
# Header
# ══════════════════════════════════════════════════════════════════════════════
st.markdown(
    '<h2 style="text-align:center;padding:0.5rem 0 0.1rem">📱 Quick Supply Order</h2>'
    '<p style="text-align:center;color:#666;margin-bottom:0">Scan items, adjust quantities, submit.</p>',
    unsafe_allow_html=True,
)
st.divider()

orderer_name = st.text_input("Your name (optional)", placeholder="Leave blank to submit as Anonymous")
st.divider()

# ── Camera QR scanner ──────────────────────────────────────────────────────────
catalog_json = json.dumps(multiplier_map)

scanner_html = f"""<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/jsqr@1.4.0/dist/jsQR.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:transparent;padding:4px 0}}
#wrap{{width:100%;max-width:520px;margin:0 auto}}
#btn{{width:100%;padding:15px;font-size:1.05rem;font-weight:700;background:#0068c9;color:#fff;
  border:none;border-radius:10px;cursor:pointer;transition:background .15s}}
#btn.on{{background:#c0392b}}
#cam-box{{display:none;position:relative;margin-top:10px;border-radius:12px;
  overflow:hidden;background:#000;width:100%}}
#cam-box.active{{display:block}}
video{{width:100%;display:block;max-height:320px;object-fit:cover}}
canvas{{display:none}}
#aim{{position:absolute;top:50%;left:50%;transform:translate(-50%,-52%);
  width:58%;aspect-ratio:1;border:3px solid rgba(255,255,255,.9);border-radius:10px;
  box-shadow:0 0 0 9999px rgba(0,0,0,.5);pointer-events:none}}
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
<div id="wrap">
  <button id="btn" onclick="toggle()">📷&nbsp; Scan Item</button>
  <div id="cam-box">
    <video id="vid" autoplay playsinline muted></video>
    <canvas id="cvs"></canvas>
    <div id="aim"></div>
    <div id="hint">Align QR code inside the box</div>
  </div>
  <div id="msg"></div>
</div>
<script>
const CATALOG = {catalog_json};
let stream=null, active=false, raf=null, cooldown=false;
const vid=document.getElementById("vid");
const cvs=document.getElementById("cvs");
const ctx=cvs.getContext("2d");
const box=document.getElementById("cam-box");
const btn=document.getElementById("btn");
const msg=document.getElementById("msg");

function setMsg(t,c){{ msg.className=c; msg.textContent=t; }}

function toggle(){{ active ? stop() : start(); }}

async function start(){{
  setMsg("Opening camera…","info");
  try{{
    stream = await navigator.mediaDevices.getUserMedia({{
      video:{{ facingMode:{{ideal:"environment"}}, width:{{ideal:1280}}, height:{{ideal:720}} }}
    }});
    vid.srcObject = stream;
    await vid.play();
    active = true;
    box.classList.add("active");
    btn.classList.add("on");
    btn.textContent = "⏹  Stop Scanner";
    msg.className = "";
    tick();
  }} catch(e){{
    setMsg("Camera error: "+e.message,"err");
  }}
}}

function stop(){{
  active = false;
  if(raf) cancelAnimationFrame(raf);
  if(stream) stream.getTracks().forEach(t=>t.stop());
  stream = null;
  box.classList.remove("active");
  btn.classList.remove("on");
  btn.textContent = "📷  Scan Item";
}}

function tick(){{
  if(!active) return;
  if(vid.readyState === vid.HAVE_ENOUGH_DATA){{
    cvs.width=vid.videoWidth; cvs.height=vid.videoHeight;
    ctx.drawImage(vid,0,0,cvs.width,cvs.height);
    const d = ctx.getImageData(0,0,cvs.width,cvs.height);
    const code = jsQR(d.data,d.width,d.height,{{inversionAttempts:"dontInvert"}});
    if(code && !cooldown) handleQR(code.data);
  }}
  raf = requestAnimationFrame(tick);
}}

function handleQR(raw){{
  let pid = null;
  try{{
    const u = new URL(raw);
    pid = u.searchParams.get("product_number");
  }} catch(_){{ pid = raw.trim(); }}

  if(!pid) return;
  if(!(pid in CATALOG)){{ setMsg("Product #"+pid+" not in catalog","err"); return; }}

  cooldown = true;
  setMsg("✅ Added #"+pid+" — updating cart…","ok");

  setTimeout(()=>{{
    const u = new URL(window.parent.location.href);
    u.searchParams.set("scanned_pid", pid);
    u.searchParams.delete("product_number");
    window.parent.location.href = u.toString();
  }}, 1000);
}}
</script>
</body>
</html>
"""

st.markdown("### 📷 Scan Items")
st.caption("Tap **Scan Item**, point at a QR code — each scan adds to your cart.")
components.html(scanner_html, height=440, scrolling=False)

st.divider()

# ── Cart ───────────────────────────────────────────────────────────────────────
st.markdown("### 🛒 Cart")

order_items = []
for pid, qty in st.session_state["scan_qty_map"].items():
    if qty > 0:
        row = catalog.loc[catalog["product_number"] == pid]
        if not row.empty:
            order_items.append({
                "item":        row.iloc[0]["item"],
                "product_number": pid,
                "rec_qty":     int(row.iloc[0].get("multiplier", 1) or 1),  # FIX: rec qty column
                "qty":         qty,
            })

if order_items:
    cart_df = pd.DataFrame(order_items)

    edited_cart = st.data_editor(
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

    # Sync qty edits back to session state
    rerun_needed = False
    for _, r in edited_cart.iterrows():
        pid     = str(r["product_number"])
        new_qty = int(r["qty"])
        if st.session_state["scan_qty_map"].get(pid) != new_qty:
            st.session_state["scan_qty_map"][pid] = new_qty
            rerun_needed = True
    if rerun_needed:
        st.rerun()

    # FIX: use flag pattern so clear fires cleanly on next run
    if st.button("🧹 Clear cart"):
        st.session_state["do_clear"] = True
        st.rerun()

else:
    st.caption("Cart is empty — scan a QR code to add items.")

st.divider()

# ── Notes ─────────────────────────────────────────────────────────────────────
notes = st.text_area("Notes (optional)", placeholder="Any extra context…", height=80)

# ── Submit ────────────────────────────────────────────────────────────────────
submitted = st.button("🧾 Submit Order", type="primary", use_container_width=True)

if submitted:
    if not order_items:
        st.error("Cart is empty — scan some items first.")
    else:
        orderer   = orderer_name.strip() if orderer_name.strip() else "Anonymous"
        order_df  = pd.DataFrame(order_items)

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

        st.session_state["scan_qty_map"] = {}
        st.balloons()
