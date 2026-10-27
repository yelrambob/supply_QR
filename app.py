import streamlit as st
import pandas as pd
import zoneinfo
from datetime import datetime
from pathlib import Path
import io
import base64

from db.supabase_client import (
    append_log,
    read_log,
    last_info_map,
)

from data.catalog import (
    read_catalog,
    write_catalog,
)

from services.email_service import (
    EMAILS_PATH,
    EMAILS2_PATH,
    send_email,
    smtp_ok,
    smtp_status_text,
    read_email_file,
    recipients_for_orderer,
    build_email_body,
)

st.set_page_config(page_title="Supply Ordering", page_icon="📦", layout="wide")

# ---------------- Time ----------------
NYC = zoneinfo.ZoneInfo("America/New_York")
now = datetime.now(NYC).strftime("%Y-%m-%d %H:%M:%S")

# ---------------- Paths ----------------
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

PEOPLE_PATH = DATA_DIR / "people.txt"

# ---------------- Load people ----------------
@st.cache_data
def read_people() -> list[str]:
    if not PEOPLE_PATH.exists():
        return []
    try:
        return [
            ln.strip()
            for ln in PEOPLE_PATH.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
    except Exception as e:
        st.warning(f"Couldn't read people.txt: {e}")
        return []

# ---------------- QR Code Generator ----------------
def generate_qr_code(data: str, box_size: int = 6, border: int = 2) -> str:
    """Generate a QR code as a base64-encoded PNG string."""
    try:
        import qrcode
        from PIL import Image

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=box_size,
            border=border,
        )
        qr.add_data(data)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("utf-8")
    except ImportError:
        return None

# ---------------- Order helpers ----------------
def create_order_dataframe(qty_map: dict, catalog) -> pd.DataFrame:
    full_order = [
        {
            "item":           catalog.loc[catalog["product_number"].astype(str) == str(pid)].iloc[0]["item"],
            "product_number": pid,
            "qty":            qty,
        }
        for pid, qty in qty_map.items()
        if qty > 0 and not catalog.loc[catalog["product_number"].astype(str) == str(pid)].empty
    ]
    return pd.DataFrame(full_order)


def flash(kind: str, message: str):
    """Queue a status message to survive the st.rerun() that
    follows a successful order submission."""
    st.session_state.setdefault("flash", []).append((kind, message))


def log_and_email_order(orderer: str, qty_map: dict, catalog,
                        normal_emails_df: pd.DataFrame,
                        special_emails_df: pd.DataFrame) -> bool:
    order_df = create_order_dataframe(qty_map, catalog)
    if order_df.empty:
        st.warning("No items selected.")
        return False

    when_str = append_log(order_df, orderer)
    flash("success", f"Order logged at {when_str}.")

    if not smtp_ok():
        flash(
            "error",
            "Order was logged, but no email was sent: SMTP is not configured "
            "(missing host/user/password/from in st.secrets['smtp']).",
        )
        return True

    recipients, special_only = recipients_for_orderer(
        orderer, normal_emails_df, special_emails_df
    )
    if not recipients:
        if special_only:
            flash("error", f"{orderer} was selected, but no valid email address was found in data/emails2.csv.")
        else:
            flash("error", "No valid email recipients were found.")
        return True

    body = build_email_body(qty_map, catalog, orderer, when_str)
    try:
        send_email(
            "Supplies Requested", body, recipients,
            include_default_recipients=not special_only,
        )
        if special_only:
            flash("success", f"{orderer}'s order email was sent only to the emails2.csv recipient list ({len(recipients)} recipient(s)).")
        else:
            flash("success", f"Emailed {len(recipients)} recipient(s).")
    except Exception as e:
        flash("error", f"Email failed: {e}")
    return True

# ---------------- Label PDF builder ----------------
def make_label_pdf(items_df, lbl_w_in, lbl_h_in, cols, rows,
                   ml_in, mt_in, h_gap_in, v_gap_in) -> bytes:
    import io as _io
    import barcode as _pbc
    from barcode.writer import ImageWriter as _IW
    from reportlab.pdfgen import canvas as _rl_canvas
    from reportlab.lib.pagesizes import letter as _letter
    from reportlab.lib.units import inch as _inch
    from reportlab.lib.utils import ImageReader as _ImageReader
    from PIL import Image as _Image

    PAGE_W, PAGE_H = _letter
    LABEL_W = lbl_w_in * _inch
    LABEL_H = lbl_h_in * _inch
    COLS    = int(cols)
    ROWS    = int(rows)
    ML      = ml_in   * _inch
    MT      = mt_in   * _inch
    H_GAP   = h_gap_in * _inch
    V_GAP   = v_gap_in * _inch
    PAD     = 4
    CODE128 = _pbc.get_barcode_class("code128")

    buf = _io.BytesIO()
    c = _rl_canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))

    for label_idx, (_, row) in enumerate(items_df.iterrows()):
        col_i = label_idx % COLS
        row_i = (label_idx // COLS) % ROWS
        if label_idx > 0 and label_idx % (COLS * ROWS) == 0:
            c.showPage()

        pid       = str(row["product_number"])
        item_name = str(row["item"])

        x = ML + col_i * (LABEL_W + H_GAP)
        y = PAGE_H - MT - (row_i + 1) * LABEL_H - row_i * V_GAP

        c.setFont("Helvetica-Bold", 12)
        max_chars = int((LABEL_W - PAD * 2) / 6.8)
        if len(item_name) <= max_chars:
            c.drawCentredString(x + LABEL_W / 2, y + LABEL_H - PAD - 12, item_name)
        else:
            split = item_name[:max_chars].rfind(" ")
            if split == -1:
                split = max_chars
            line1 = item_name[:split]
            line2 = item_name[split:].strip()
            if len(line2) > max_chars:
                line2 = line2[:max_chars - 1] + "…"
            c.drawCentredString(x + LABEL_W / 2, y + LABEL_H - PAD - 12, line1)
            c.drawCentredString(x + LABEL_W / 2, y + LABEL_H - PAD - 26, line2)

        try:
            bc_buf = _io.BytesIO()
            bc = CODE128(pid, writer=_IW())
            bc.write(bc_buf, options={"write_text": False, "module_height": 10.0, "quiet_zone": 2.0})
            bc_buf.seek(0)
            bc_img    = _Image.open(bc_buf)
            bc_draw_h = LABEL_H * 0.52
            bc_draw_w = LABEL_W * 0.85
            bc_x      = x + (LABEL_W - bc_draw_w) / 2
            bc_y      = y + PAD
            out_buf   = _io.BytesIO()
            bc_img.save(out_buf, format="PNG")
            out_buf.seek(0)
            c.drawImage(_ImageReader(out_buf), bc_x, bc_y, width=bc_draw_w, height=bc_draw_h)
        except Exception:
            pass

    c.save()
    buf.seek(0)
    return buf.getvalue()

# ---------------- Session state ----------------
if "orderer" not in st.session_state:
    st.session_state["orderer"] = None

if "qty_map" not in st.session_state:
    st.session_state["qty_map"] = {}

for _flash_kind, _flash_message in st.session_state.pop("flash", []):
    getattr(st, _flash_kind)(_flash_message)

# ---------------- UI ----------------
st.title("📦 Supply Ordering & Inventory Tracker")

people = read_people()
emails_df = read_email_file(EMAILS_PATH)
special_emails_df = read_email_file(EMAILS2_PATH)
catalog = read_catalog()
logs = read_log()

email_ready = smtp_status_text()
st.caption(
    f"Loaded {len(catalog)} catalog rows • "
    f"{len(logs)} log rows • "
    f"Email configured: {email_ready}"
)

# ---------------- Current Order Preview ----------------
selected_items = []
for pid, qty in st.session_state["qty_map"].items():
    if qty > 0:
        row = catalog.loc[catalog["product_number"].astype(str) == str(pid)]
        if not row.empty:
            selected_items.append(
                {
                    "item": row.iloc[0]["item"],
                    "product_number": pid,
                    "qty": qty,
                }
            )

if selected_items:
    st.markdown("### 🛒 Current Order (in progress)")
    st.dataframe(pd.DataFrame(selected_items), hide_index=True, use_container_width=True)

    product_numbers = [i["product_number"] for i in selected_items]
    st.markdown(f"**Product Numbers:** {', '.join(product_numbers)}")

    if st.button("🧾 Generate & Log Order", key="gen_log_top"):
        top_orderer = st.session_state.get("orderer") or (people[0] if people else "Unknown")
        if log_and_email_order(top_orderer, st.session_state["qty_map"], catalog,
                               emails_df, special_emails_df):
            st.session_state["qty_map"] = {}
            st.rerun()
else:
    st.caption("🛒 No items currently selected.")

# ---------------- Tabs ----------------
tabs = st.tabs(["Create Order", "Adjust Inventory", "Catalog", "Order Logs", "QR Codes", "Print Select Labels"])

# =====================================================
# Create Order
# =====================================================
with tabs[0]:
    if catalog.empty:
        st.info("No catalog found.")
    else:
        c1, c2 = st.columns([2, 3])

        with c1:
            current_orderer = (
                st.session_state["orderer"]
                or (people[0] if people else "Unknown")
            )

            orderer = st.selectbox(
                "Who is ordering?",
                options=(people if people else ["Unknown"]),
                index=(
                    people.index(current_orderer)
                    if people and current_orderer in people
                    else 0
                ),
            )
            st.session_state["orderer"] = orderer

        with c2:
            search = st.text_input("Search items")

        last_map = last_info_map()
        table = catalog.merge(
            last_map, on=["item", "product_number"], how="left"
        )

        for c in ["last_ordered_at", "last_qty", "last_orderer"]:
            if c not in table.columns:
                table[c] = pd.NA

        table["last_ordered_at"] = pd.to_datetime(
            table["last_ordered_at"], errors="coerce"
        )

        table = (
            table.sort_values(
                ["last_ordered_at", "item"],
                ascending=[False, True],
                na_position="last",
            )
            .reset_index(drop=True)
        )

        table["product_number"] = table["product_number"].astype(str)
        table["qty"] = (
            table["product_number"]
            .map(st.session_state["qty_map"])
            .fillna(0)
            .astype(int)
        )

        if search:
            mask = (
                table["item"].str.contains(search, case=False, na=False)
                | table["product_number"].str.contains(search, case=False, na=False)
            )
            table = table[mask]

        edited = st.data_editor(
            table[
                [
                    "qty",
                    "item",
                    "product_number",
                    "multiplier",
                    "items_per_order",
                    "current_qty",
                    "price",
                    "last_ordered_at",
                    "last_qty",
                    "last_orderer",
                ]
            ],
            use_container_width=True,
            hide_index=True,
            column_config={
                "qty": st.column_config.NumberColumn("Qty", min_value=0, step=1),
                "item": st.column_config.TextColumn("Item", disabled=True),
                "product_number": st.column_config.TextColumn("Product #", disabled=True),
                "multiplier": st.column_config.NumberColumn("Multiplier", disabled=True),
                "items_per_order": st.column_config.NumberColumn("Items/Order", disabled=True),
                "current_qty": st.column_config.NumberColumn("Current Qty", disabled=True),
                "price": st.column_config.NumberColumn("Price", disabled=True),
                "last_ordered_at": st.column_config.DatetimeColumn(
                    "Last ordered", format="YYYY-MM-DD HH:mm", disabled=True
                ),
                "last_qty": st.column_config.NumberColumn("Last qty", disabled=True),
                "last_orderer": st.column_config.TextColumn("Last by", disabled=True),
            },
            key="order_editor",
        )

        rerun_needed = False
        for _, r in edited.iterrows():
            pid     = str(r["product_number"])
            new_qty = int(r["qty"]) if pd.notna(r["qty"]) else 0
            old_qty = st.session_state["qty_map"].get(pid, 0)
            if old_qty != new_qty:
                st.session_state["qty_map"][pid] = new_qty
                if new_qty != 0:
                    rerun_needed = True

        if rerun_needed:
            st.rerun()

        # ---------------- Generate & Log Order ----------------
        if st.button("🧾 Generate & Log Order", key="gen_log_main"):
            if log_and_email_order(orderer, st.session_state["qty_map"], catalog,
                                   emails_df, special_emails_df):
                st.session_state["qty_map"] = {}
                st.rerun()

        if st.button("🧹 Clear Current Order"):
            st.session_state["qty_map"] = {}
            st.rerun()

# =====================================================
# Adjust Inventory
# =====================================================
with tabs[1]:
    if catalog.empty:
        st.info("No catalog found.")
    else:
        edited = st.data_editor(
            catalog.copy(),
            use_container_width=True,
            hide_index=True,
            column_config={
                "item": st.column_config.TextColumn("Item", disabled=True),
                "product_number": st.column_config.TextColumn("Product #", disabled=True),
                "multiplier": st.column_config.NumberColumn("Multiplier", min_value=1),
                "items_per_order": st.column_config.NumberColumn("Items/Order", min_value=1),
                "current_qty": st.column_config.NumberColumn("Current Qty", min_value=0),
                "sort_order": st.column_config.NumberColumn("Sort order", min_value=0),
                "price": st.column_config.NumberColumn("Price ($)", min_value=0.0),
            },
            key="inventory_editor",
        )

        if st.button("💾 Save inventory changes"):
            write_catalog(edited)
            st.success("Inventory saved.")

# =====================================================
# Catalog
# =====================================================
with tabs[2]:
    st.dataframe(catalog, use_container_width=True, hide_index=True)

# =====================================================
# Order Logs
# =====================================================
with tabs[3]:
    if logs.empty:
        st.info("No orders logged yet.")
    else:
        st.dataframe(logs, use_container_width=True, hide_index=True)
        st.download_button(
            "⬇️ Download full log (CSV)",
            data=logs.to_csv(index=False).encode("utf-8"),
            file_name="order_log.csv",
            mime="text/csv",
        )

# =====================================================
# QR Codes & Labels
# =====================================================
with tabs[4]:
    st.markdown("## 📱 Locations & Item Labels")
    st.markdown(
        "Each **location** (supply room, closet, etc.) gets a fixed QR code that opens "
        "the order page for that location. Print **barcode labels** for each item and "
        "stick them on the shelf. Scan item barcodes with your phone camera to build an order."
    )

    if catalog.empty:
        st.info("No catalog items found.")
    else:
        # Auto-detect URL
        try:
            host = st.context.headers.get("host", "")
            detected_url = ("https://" + host) if host and not host.startswith("http") else host
        except Exception:
            detected_url = "https://qrsupply.streamlit.app"

        app_base_url = st.text_input(
            "App URL",
            value=detected_url or "https://qrsupply.streamlit.app",
            key="qr_url",
        ).rstrip("/")

        try:
            import qrcode as _qrcode
            qr_available = True
        except ImportError:
            qr_available = False

        try:
            import barcode as _pbc
            from barcode.writer import ImageWriter as _IW
            bc_available = True
        except ImportError:
            bc_available = False

        if not qr_available:
            st.warning("Add `qrcode[pil]` to requirements.txt for QR images.")
        if not bc_available:
            st.warning("Add `python-barcode[images]` to requirements.txt for barcode images.")

        st.divider()

        # ── Location manager ──────────────────────────────────────────────────
        st.markdown("### 📍 Locations")
        st.caption("Add a location name — it becomes the cart ID. Keep it short and descriptive.")

        if "locations" not in st.session_state:
            st.session_state["locations"] = ["supply-room-1"]

        loc_col1, loc_col2 = st.columns([3, 1])
        with loc_col1:
            new_loc = st.text_input("Add location", placeholder="e.g. supply-room-2, nurses-station", key="new_loc")
        with loc_col2:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("➕ Add") and new_loc.strip():
                slug = new_loc.strip().lower().replace(" ", "-")
                if slug not in st.session_state["locations"]:
                    st.session_state["locations"].append(slug)
                    st.rerun()

        for loc in st.session_state["locations"]:
            loc_url = f"{app_base_url}/scan_order?cart_id={loc}"
            with st.expander(f"📍 {loc}", expanded=True):
                c1, c2 = st.columns([1, 2])
                with c1:
                    if qr_available:
                        b64 = generate_qr_code(loc_url, box_size=6, border=2)
                        if b64:
                            st.markdown(
                                f'<img src="data:image/png;base64,{b64}" width="160" '                                f'style="display:block;margin:4px 0"/>',
                                unsafe_allow_html=True,
                            )
                    else:
                        st.code(loc_url, language=None)
                with c2:
                    st.markdown(f"**Cart ID:** `{loc}`")
                    st.markdown(f"**URL:** `{loc_url}`")
                    st.caption(
                        "Post this QR code at the location. Anyone who scans it "
                        "opens the order page for this location. Item barcodes scanned "
                        "there add to this location's cart."
                    )
                    st.markdown(f"[🔗 Open order page]({loc_url})")
                if st.button(f"🗑️ Remove {loc}", key=f"rm_{loc}"):
                    st.session_state["locations"].remove(loc)
                    st.rerun()

        st.divider()

        # ── Item barcode labels ───────────────────────────────────────────────
        st.markdown("### 🏷️ Item Barcode Labels")
        st.caption(
            "Select a location then print these labels. Each barcode encodes the item "
            "product number — scanning it adds that item to the selected location's cart."
        )

        selected_loc = st.selectbox(
            "Location for labels",
            options=st.session_state["locations"],
            key="label_loc",
        )

        search_bc = st.text_input("Filter items", placeholder="Search by name or product #", key="bc_search")
        cols_count_bc = st.radio("Labels per row", [2, 3, 4], index=2, horizontal=True, key="bc_cols")

        display_bc = catalog.copy()
        display_bc["product_number"] = display_bc["product_number"].astype(str)
        if search_bc:
            mask = (
                display_bc["item"].str.contains(search_bc, case=False, na=False)
                | display_bc["product_number"].str.contains(search_bc, case=False, na=False)
            )
            display_bc = display_bc[mask]

        if display_bc.empty:
            st.info("No items match.")
        else:
            # ── Label sheet dimensions ─────────────────────────────────────
            st.markdown("#### 📐 Label Sheet Settings")
            st.caption("Default is Avery 5163 / 2×4 inch, 10 per sheet. Adjust for your sheet (e.g. 04A-U1).")
            dim_col1, dim_col2, dim_col3 = st.columns(3)
            with dim_col1:
                lbl_w_in = st.number_input("Label width (in)", value=2.625, step=0.05, format="%.3f", key="lbl_w")
                lbl_h_in = st.number_input("Label height (in)", value=1.0, step=0.05, format="%.3f", key="lbl_h")
            with dim_col2:
                lbl_cols = st.number_input("Columns", value=3, min_value=1, max_value=6, step=1, key="lbl_cols")
                lbl_rows = st.number_input("Rows per page", value=9, min_value=1, max_value=20, step=1, key="lbl_rows")
            with dim_col3:
                margin_top_in    = st.number_input("Top margin (in)", value=0.50, step=0.005, format="%.3f", key="lbl_mt")
                margin_bottom_in = st.number_input("Bottom margin (in)", value=0.50, step=0.005, format="%.3f", key="lbl_mb")
                margin_left_in   = st.number_input("Left margin (in)", value=0.19, step=0.005, format="%.3f", key="lbl_ml")
            dim_col4, dim_col5 = st.columns(2)
            with dim_col4:
                h_gap_in = st.number_input("Horizontal gap between labels (in)", value=0.13, step=0.005, format="%.3f", key="lbl_hgap")
            with dim_col5:
                v_gap_in = st.number_input("Vertical gap between labels (in)", value=0.0, step=0.005, format="%.3f", key="lbl_vgap")

            total_h = margin_top_in + int(lbl_rows) * lbl_h_in + max(0, int(lbl_rows) - 1) * v_gap_in + margin_bottom_in
            row_pitch = lbl_h_in + v_gap_in
            col_pitch = lbl_w_in + h_gap_in
            if total_h > 11.0:
                st.warning(f"Total height used: **{total_h:.3f} in** — exceeds 11 in letter page. Reduce rows or margins to prevent cut-off.")
            else:
                st.caption(f"Total height used: **{total_h:.3f} in** / 11 in  ·  Row pitch: **{row_pitch:.3f} in**  ·  Col pitch: **{col_pitch:.3f} in** — if rows drift up, increase v-gap; drift down, decrease it.")

            # ── PDF generation ─────────────────────────────────────────────
            if bc_available:
                try:
                    from reportlab.pdfgen import canvas as _rl_canvas
                    from reportlab.lib.pagesizes import letter as _letter
                    from reportlab.lib.units import inch as _inch
                    from reportlab.lib.utils import ImageReader as _ImageReader
                    from PIL import Image as _Image
                    rl_available = True
                except ImportError:
                    rl_available = False
            else:
                rl_available = False

            if rl_available and st.button("📄 Download label sheet PDF", key="bc_pdf"):
                pdf_bytes = make_label_pdf(
                    display_bc, lbl_w_in, lbl_h_in, lbl_cols, lbl_rows,
                    margin_left_in, margin_top_in, h_gap_in, v_gap_in,
                )
                st.download_button(
                    "💾 Save label PDF",
                    data=pdf_bytes,
                    file_name="barcode_labels.pdf",
                    mime="application/pdf",
                    key="bc_pdf_dl",
                )
            elif not bc_available:
                st.info("Add `python-barcode[images]` and `reportlab` to requirements.txt to generate PDFs.")
            elif not rl_available:
                st.info("Add `reportlab` to requirements.txt to generate PDFs.")

            rows_iter = [
                display_bc.iloc[i: i + cols_count_bc]
                for i in range(0, len(display_bc), cols_count_bc)
            ]
            for row_group in rows_iter:
                cols = st.columns(cols_count_bc)
                for col, (_, item_row) in zip(cols, row_group.iterrows()):
                    pid       = str(item_row["product_number"])
                    item_name = str(item_row["item"])
                    rec_qty   = int(item_row.get("multiplier", 1) or 1)
                    scan_url  = f"{app_base_url}/scan_order?product_number={pid}&cart_id={selected_loc}"
                    with col:
                        with st.container(border=True):
                            st.markdown(
                                f'<p style="text-align:center;font-size:1.1rem;font-weight:700;margin:4px 0">{item_name}</p>',
                                unsafe_allow_html=True,
                            )
                            if bc_available:
                                try:
                                    import io as _io
                                    CODE128 = _pbc.get_barcode_class("code128")
                                    buf = _io.BytesIO()
                                    bc = CODE128(pid, writer=_IW())
                                    bc.write(buf, options={
                                        "write_text": False, "module_height": 7.0,
                                    })
                                    buf.seek(0)
                                    b64 = base64.b64encode(buf.read()).decode()
                                    st.markdown(
                                        f'<img src="data:image/png;base64,{b64}" '
                                        f'style="width:100%;display:block;margin:4px 0"/>',
                                        unsafe_allow_html=True,
                                    )
                                except Exception as e:
                                    st.code(pid, language=None)
                            else:
                                st.code(pid, language=None)

# =====================================================
# Print Select Labels
# =====================================================
with tabs[5]:
    st.markdown("### 🖨️ Print Selected Labels")
    st.caption("Check the items you want, then download a PDF with just those labels.")

    if catalog.empty:
        st.info("No catalog items found.")
    else:
        # ── Search ──────────────────────────────────────────────────────────
        search_sel = st.text_input("Filter items", placeholder="Search by name or product #", key="sel_search")

        sel_catalog = catalog.copy()
        sel_catalog["product_number"] = sel_catalog["product_number"].astype(str)
        if search_sel:
            mask = (
                sel_catalog["item"].str.contains(search_sel, case=False, na=False)
                | sel_catalog["product_number"].str.contains(search_sel, case=False, na=False)
            )
            sel_catalog = sel_catalog[mask]

        # ── Select / deselect all ───────────────────────────────────────────
        sc1, sc2 = st.columns(2)
        with sc1:
            if st.button("✅ Select all visible", key="sel_all"):
                for pid in sel_catalog["product_number"].tolist():
                    st.session_state[f"chk_{pid}"] = True
                st.rerun()
        with sc2:
            if st.button("⬜ Deselect all", key="sel_none"):
                for pid in catalog["product_number"].astype(str).tolist():
                    st.session_state[f"chk_{pid}"] = False
                st.rerun()

        # ── Checkbox table ──────────────────────────────────────────────────
        sel_catalog["print"] = sel_catalog["product_number"].apply(
            lambda pid: bool(st.session_state.get(f"chk_{pid}", False))
        )

        edited_sel = st.data_editor(
            sel_catalog[["print", "item", "product_number"]],
            use_container_width=True,
            hide_index=True,
            column_config={
                "print":          st.column_config.CheckboxColumn("Print", default=False),
                "item":           st.column_config.TextColumn("Item", disabled=True),
                "product_number": st.column_config.TextColumn("Product #", disabled=True),
            },
            key="sel_editor",
        )

        for _, r in edited_sel.iterrows():
            st.session_state[f"chk_{r['product_number']}"] = bool(r["print"])

        selected_pids = [str(r["product_number"]) for _, r in edited_sel.iterrows() if r["print"]]
        selected_df   = catalog[catalog["product_number"].astype(str).isin(selected_pids)].copy()

        st.caption(f"**{len(selected_df)}** item(s) selected.")

        # ── Label sheet settings ────────────────────────────────────────────
        st.markdown("#### 📐 Label Sheet Settings")
        s1, s2, s3 = st.columns(3)
        with s1:
            s_lbl_w  = st.number_input("Label width (in)",  value=2.625, step=0.05,  format="%.3f", key="s_lbl_w")
            s_lbl_h  = st.number_input("Label height (in)", value=1.0,   step=0.05,  format="%.3f", key="s_lbl_h")
        with s2:
            s_cols   = st.number_input("Columns",           value=3,     min_value=1, max_value=6, step=1, key="s_cols")
            s_rows   = st.number_input("Rows per page",     value=9,     min_value=1, max_value=20, step=1, key="s_rows")
        with s3:
            s_mt     = st.number_input("Top margin (in)",    value=0.50, step=0.005, format="%.3f", key="s_mt")
            s_mb     = st.number_input("Bottom margin (in)", value=0.50, step=0.005, format="%.3f", key="s_mb")
            s_ml     = st.number_input("Left margin (in)",   value=0.19, step=0.005, format="%.3f", key="s_ml")
        s4, s5 = st.columns(2)
        with s4:
            s_hgap   = st.number_input("H gap (in)", value=0.13, step=0.005, format="%.3f", key="s_hgap")
        with s5:
            s_vgap   = st.number_input("V gap (in)", value=0.0,  step=0.005, format="%.3f", key="s_vgap")

        s_total_h = s_mt + int(s_rows) * s_lbl_h + max(0, int(s_rows) - 1) * s_vgap + s_mb
        if s_total_h > 11.0:
            st.warning(f"Total height: **{s_total_h:.3f} in** — exceeds 11 in. Reduce rows or margins.")
        else:
            st.caption(f"Total height: **{s_total_h:.3f} in** / 11 in  ·  Row pitch: **{s_lbl_h + s_vgap:.3f} in**")

        # ── PDF download ────────────────────────────────────────────────────
        try:
            import barcode as _pbc2
            from barcode.writer import ImageWriter as _IW2
            from reportlab.pdfgen import canvas as _rlc2
            sel_pdf_ok = True
        except ImportError:
            sel_pdf_ok = False

        if sel_pdf_ok:
            if st.button("📄 Generate selected labels PDF", key="sel_pdf", disabled=selected_df.empty):
                pdf_bytes = make_label_pdf(
                    selected_df, s_lbl_w, s_lbl_h, s_cols, s_rows,
                    s_ml, s_mt, s_hgap, s_vgap,
                )
                st.download_button(
                    "💾 Save selected labels PDF",
                    data=pdf_bytes,
                    file_name="selected_labels.pdf",
                    mime="application/pdf",
                    key="sel_pdf_dl",
                )
        else:
            st.info("Add `python-barcode[images]` and `reportlab` to requirements.txt to generate PDFs.")
