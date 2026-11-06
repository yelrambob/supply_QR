"""Load the Streamlit app in a real browser and click through the sleep screen if present.

A plain HTTP ping doesn't keep a Streamlit Community Cloud app awake because the
"gone to sleep" screen only wakes up in response to a real page load (with the
"Yes, get this app back up!" button clicked), not a bare curl request.

A cold rebuild (reinstalling requirements.txt and starting the container) can take
several minutes, not seconds -- give it a generous window rather than failing fast.
"""
import sys
import time

from playwright.sync_api import Page, sync_playwright

APP_URL = "https://qrsupply.streamlit.app/"
WAKE_BUTTON_TEXT = "get this app back up"
READY_TEXT = "Supply Ordering & Inventory Tracker"
READY_TIMEOUT_S = 300  # cold rebuilds have been observed to exceed 120s
POLL_INTERVAL_S = 5


def _any_frame_has_text(page: Page, text: str) -> bool:
    # Streamlit Community Cloud renders the actual app inside an iframe
    # (".../~/+/"), so searching only the top-level page never finds it.
    for frame in page.frames:
        try:
            if frame.get_by_text(text, exact=False).count() > 0:
                return True
        except Exception:
            # Frames can detach/navigate mid-check while the app boots.
            continue
    return False


def _click_wake_button(page: Page) -> bool:
    for frame in page.frames:
        try:
            button = frame.get_by_role("button", name=WAKE_BUTTON_TEXT, exact=False)
            if button.count() > 0:
                button.first.click()
                return True
        except Exception:
            continue
    return False


def wake_app() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(APP_URL, timeout=60_000, wait_until="domcontentloaded")

        clicked = False
        deadline = time.monotonic() + READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if _any_frame_has_text(page, READY_TEXT):
                print("App is awake and loaded.", flush=True)
                browser.close()
                return
            # The sleep screen may render a few seconds after the initial load,
            # so keep checking for the button rather than looking only once.
            if not clicked and _click_wake_button(page):
                print("App is asleep, clicked the wake button...", flush=True)
                clicked = True
            time.sleep(POLL_INTERVAL_S)

        if not clicked:
            print("Never saw a wake button.", flush=True)
        print(
            f"Timed out after {READY_TIMEOUT_S}s waiting for {READY_TEXT!r}. "
            f"Frames seen: {[f.url for f in page.frames]}",
            flush=True,
        )
        page.screenshot(path="wake_streamlit_failure.png", full_page=True)
        browser.close()
        sys.exit(1)


if __name__ == "__main__":
    wake_app()
