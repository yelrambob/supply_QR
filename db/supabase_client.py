# supply_QR/db/supabase_client.py

import streamlit as st
import pandas as pd
import zoneinfo
from datetime import datetime
from supabase import create_client

NYC = zoneinfo.ZoneInfo("America/New_York")


def get_supabase():
    url = st.secrets["supabase"]["url"]
    key = st.secrets["supabase"]["key"]
    return create_client(url, key)


supabase = get_supabase()


# ---------------- Orders log ----------------

def append_log(order_df: pd.DataFrame, orderer: str) -> str:
    """
    Insert order rows into Supabase and return timestamp string
    """
    now = datetime.now(NYC).isoformat(sep=" ", timespec="seconds")

    rows = []
    for _, r in order_df.iterrows():
        rows.append(
            {
                "item": r["item"],
                "product_number": str(r["product_number"]),
                "qty": int(r["qty"]),
                "ordered_at": now,
                "orderer": orderer,
            }
        )

    supabase.table("orders_log").insert(rows).execute()
    return now


def read_log() -> pd.DataFrame:
    res = (
        supabase
        .table("orders_log")
        .select("*")
        .order("ordered_at", desc=True)
        .execute()
    )

    if not getattr(res, "data", None):
        return pd.DataFrame(
            columns=["item", "product_number", "qty", "ordered_at", "orderer"]
        )

    return pd.DataFrame(res.data)


def last_info_map() -> pd.DataFrame:
    """
    Returns last ordered info per item/product_number
    """
    logs = read_log()
    if logs.empty:
        return pd.DataFrame(
            columns=[
                "item",
                "product_number",
                "last_ordered_at",
                "last_qty",
                "last_orderer",
            ]
        )

    logs = logs.copy()
    logs["ordered_at"] = pd.to_datetime(logs["ordered_at"], errors="coerce")

    tail = (
        logs
        .sort_values("ordered_at")
        .groupby(["item", "product_number"], as_index=False)
        .tail(1)
    )

    return tail.rename(
        columns={
            "ordered_at": "last_ordered_at",
            "qty": "last_qty",
            "orderer": "last_orderer",
        }
    )[
        ["item", "product_number", "last_ordered_at", "last_qty", "last_orderer"]
    ]
