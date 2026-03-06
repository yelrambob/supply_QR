"""
components/qr_scanner/__init__.py

Registers the QR scanner as a Streamlit custom component.
The HTML/JS frontend lives in index.html alongside this file.

Usage:
    from components.qr_scanner import qr_scanner

    result = qr_scanner(catalog={"ABC": 2, "DEF": 1})
    # result is None until a QR is scanned, then:
    # {"product_number": "ABC", "qty": 2}
"""

import streamlit.components.v1 as components
from pathlib import Path

_COMPONENT_DIR = Path(__file__).parent

# Declare the component pointing at the local HTML file
_qr_scanner_func = components.declare_component(
    "qr_scanner",
    path=str(_COMPONENT_DIR),
)


def qr_scanner(catalog: dict, key: str = "qr_scanner") -> dict | None:
    """
    Render the QR scanner component.

    Parameters
    ----------
    catalog : dict
        Mapping of {product_number: recommended_qty} — passed to the
        JS frontend so it can validate scanned codes client-side.
    key : str
        Streamlit component key.

    Returns
    -------
    dict | None
        None until a QR code is scanned, then {"product_number": str, "qty": int}.
    """
    return _qr_scanner_func(catalog=catalog, key=key, default=None)
