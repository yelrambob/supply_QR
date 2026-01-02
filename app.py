import streamlit as st
import pandas as pd
import zoneinfo
from datetime import datetime
from pathlib import Path
import re

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
    send_email,
    smtp_ok,
    all_recipients,
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
EMAILS_PATH = DATA_DIR / "emails.csv"

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

# ---------------- Load emails CSV ----------------
@st.cache_data
def read_emails() -> pd.DataFrame:
    if not EMAILS_PATH.exists():
        return pd.DataFrame(columns=["name", "email"])

    try:
        df = pd.read_csv(EMAILS_PATH)
    except Exception as e:
        st.warning(f"Couldn't read emails.csv: {e}")
        return pd.DataFrame(columns=["name", "email"])

    df.columns = [str(c).strip().lower() for c in df.columns]

    email_re = re.compile(r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
    rows = []

    for _, r in df.iterrows():
        raw = str(r.get("email", ""))
        m = email_re.search(raw)
        if m:
            rows.append(
                {
                    "name": str(r.get("name", "")),
                    "email": m.group(1),
                }
            )

    return pd.DataFrame(rows)

# ---------------- Session state ----------------
if "orderer" not in st.session_state:
    st.session_state["orderer"] = None

if "qty_map" not in st.session_state:
    st.session_state["qty_map"] = {}

# ---------------- UI ----------------
st.title("📦 Supply Ordering & Inventory Tracker")

people = read_people()
emails_df = read_emails()
catalog = read_catalog()
logs = read_log()

email_ready = "✅" if smtp_ok() else "❌"
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

    if st.button("🧹 Clear Current Order"):
        st.session_state["qty_map"] = {}
        st.rerun()
else:
    st.caption("🛒 No items currently selected.")

# ---------------- Tabs ----------------
tabs = st.tabs(["Create Order", "Adjust Inventory", "Catalog", "Order Logs"])

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
            pid = str(r["product_number"])
            new_qty = int(r["qty"])
            if st.session_state["qty_map"].get(pid) != new_qty:
                st.session_state["qty_map"][pid] = new_qty
                rerun_needed = True

        if rerun_needed:
            st.rerun()

        # ---------------- Generate & Log Order ----------------
        if st.button("🧾 Generate & Log Order"):
            rows = []
            for pid, qty in st.session_state["qty_map"].items():
                if qty > 0:
                    row = catalog.loc[
                        catalog["product_number"].astype(str) == str(pid)
                    ]
                    if not row.empty:
                        rows.append(
                            {
                                "item": row.iloc[0]["item"],
                                "product_number": pid,
                                "qty": qty,
                            }
                        )

            order_df = pd.DataFrame(rows)

            if not order_df.empty:
                when_str = append_log(order_df, orderer)

                if smtp_ok():
                    recipients = all_recipients(emails_df)

                    if recipients:
                        product_groups = []
                        current_group = []
                        running_total = 0.0
                        details_lines = []

                        for _, r in order_df.iterrows():
                            pid = r["product_number"]
                            qty = r["qty"]
                            row = catalog.loc[
                                catalog["product_number"].astype(str) == str(pid)
                            ]

                            price = float(row.iloc[0].get("price", 0) or 0)
                            total = qty * price

                            if running_total + total > 4999 and current_group:
                                product_groups.append(
                                    (current_group.copy(), running_total)
                                )
                                current_group = []
                                running_total = 0.0

                            running_total += total
                            current_group.append(pid)

                            details_lines.append(
                                f"<label><input type='checkbox'/> "
                                f"- {row.iloc[0]['item']} (#{pid}): {qty}</label>"
                            )

                        if current_group:
                            product_groups.append(
                                (current_group, running_total)
                            )

                        group_lines = [
                            f"<label><input type='checkbox'/> "
                            f"{', '.join(map(str, g))} = ${t:,.0f}</label>"
                            for g, t in product_groups
                        ]

                        body = f"""
                        <html><body>
                        <p><strong>New supply order at {when_str}</strong><br>
                        Ordered by: {orderer}</p>

                        <p><strong>Details:</strong><br>
                        {"<br>".join(details_lines)}</p>

                        <p><strong>Product:</strong><br>
                        {"<br>".join(group_lines)}</p>
                        </body></html>
                        """

                        try:
                            send_email(
                                "Supply Order Logged",
                                body,
                                recipients,
                            )
                            st.success(
                                f"Emailed {len(recipients)} recipient(s)."
                            )
                        except Exception as e:
                            st.error(f"Email failed: {e}")

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
