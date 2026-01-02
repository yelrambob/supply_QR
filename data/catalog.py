# supply_QR/data/catalog.py

import streamlit as st
import pandas as pd
from pandas.errors import EmptyDataError
from pathlib import Path

# ---------------- Paths ----------------
APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = APP_DIR / "data"
CATALOG_PATH = DATA_DIR / "catalog.csv"


# ---------------- Robust CSV reader ----------------
def safe_read_csv(path: Path, **kwargs) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path, encoding="utf-8", **kwargs)
    except UnicodeDecodeError:
        return pd.read_csv(path, encoding="latin-1", **kwargs)
    except EmptyDataError:
        return pd.DataFrame()
    except Exception as e:
        st.warning(f"Couldn't read {path.name}: {e}")
        return pd.DataFrame()


# ---------------- Catalog API ----------------
@st.cache_data
def read_catalog() -> pd.DataFrame:
    df = safe_read_csv(CATALOG_PATH)

    if df.empty:
        return pd.DataFrame(
            columns=[
                "item",
                "product_number",
                "multiplier",
                "items_per_order",
                "current_qty",
                "sort_order",
                "price",
            ]
        )

    # Ensure required columns exist
    for c in [
        "item",
        "product_number",
        "multiplier",
        "items_per_order",
        "current_qty",
        "sort_order",
        "price",
    ]:
        if c not in df.columns:
            df[c] = pd.NA

    # Normalize types
    df["item"] = df["item"].astype(str).str.strip()
    df["product_number"] = df["product_number"].astype(str).str.strip()
    df["multiplier"] = pd.to_numeric(df["multiplier"], errors="coerce").fillna(1).astype(int)
    df["items_per_order"] = pd.to_numeric(df["items_per_order"], errors="coerce").fillna(1).astype(int)
    df["current_qty"] = pd.to_numeric(df["current_qty"], errors="coerce").fillna(0).astype(int)
    df["price"] = pd.to_numeric(df["price"], errors="coerce").fillna(0).astype(float)

    # Sort order fallback
    so = pd.to_numeric(df["sort_order"], errors="coerce")
    filler = pd.Series(range(len(df)), index=df.index)
    df["sort_order"] = so.fillna(filler).astype(int)

    return df.reset_index(drop=True)


def write_catalog(df: pd.DataFrame) -> None:
    """
    Persist inventory changes back to catalog.csv
    """
    df.to_csv(CATALOG_PATH, index=False)
