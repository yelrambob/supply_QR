"""Load the Streamlit app in a real browser and click through the sleep screen if present.

A plain HTTP ping doesn't keep a Streamlit Community Cloud app awake because the
"gone to sleep" screen only wakes up in response to a real page load (with the
"Yes, get this app back up!" button clicked), not a bare curl request.

A cold rebuild (reinstalling requirements.txt and starting the container) can take
several minutes, not seconds -- give it a generous window rather than failing fast.
"""
import sys

from playwright.sync_api import sync_playwright

APP_URL = "https://qrsupply.streamlit.app/"
WAKE_BUTTON_TEXT = "get this app back up"
READY_TEXT = "Supply Ordering & Inventory Tracker"
READY_TIMEOUT_MS = 300_000  # cold rebuilds have been observed to exceed 120s


def wake_app() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(APP_URL, timeout=60_000, wait_until="networkidle")

        wake_button = page.get_by_role("button", name=WAKE_BUTTON_TEXT, exact=False)
        if wake_button.count() > 0:
            print("App is asleep, clicking the wake button...", flush=True)
            wake_button.first.click()
        else:
            print("No wake button found; app may already be awake.", flush=True)

        try:
            page.get_by_text(READY_TEXT).wait_for(timeout=READY_TIMEOUT_MS)
            print("App is awake and loaded.", flush=True)
        except Exception as exc:
            print(f"Timed out waiting for app to load: {exc}", flush=True)
            page.screenshot(path="wake_streamlit_failure.png")
            browser.close()
            sys.exit(1)

        browser.close()


if __name__ == "__main__":
    wake_app()
