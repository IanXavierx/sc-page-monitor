"""
The Odyssey — IMAX Havelock booking watcher.

Watches the Scope Cinemas showtimes page for The Odyssey and fires a loud
Telegram alarm the moment a NEW date appears in the Havelock IMAX schedule
(target: Jul 22, 12:45 PM). Built on the proven Avatar approach: a headless
Playwright browser reloads the page on a short interval; GitHub Actions keeps
it running in the cloud.

Run locally with no secrets set -> TEST MODE: prints what it sees, sends nothing.
"""

import os
import re
import time

import requests
from playwright.sync_api import sync_playwright

# --- CONFIGURATION ---
URL = "https://www.scopecinemas.com/movies/the-odyssey/showtimes"
TARGET_DAY = 22                 # the date you want (Jul 22)
TARGET_TIME = "12:45 PM"        # the showtime you want
TARGET_LOC = "HAVELOCK"         # IMAX is only at Havelock City Mall
BASELINE_DAYS = {17, 18, 19}    # dates already listed as of 2026-06-19
CHECK_INTERVAL = 15             # seconds between page reloads (same as Avatar)
RUN_TIME = 18000                # total run ~5 hours (under GitHub's 6h job limit)

MONTHS = "JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"

# --- SECRETS (set as GitHub Actions secrets) ---
BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
CHAT_ID = os.getenv("TG_CHAT_ID")
TEST_MODE = not (BOT_TOKEN and CHAT_ID)


def send_telegram(message):
    if TEST_MODE:
        print("[TEST MODE] would send Telegram:", message)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": message},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram error: {e}")


def spam_alarm(message, times=200, gap=5):
    print(f"ALARM: {message}")
    for _ in range(times):
        send_telegram(message)
        if TEST_MODE:
            break  # don't loop 200x in test mode
        time.sleep(gap)


def read_page(page):
    """Return (full_body_text, set_of_day_numbers_in_date_row)."""
    for attempt in range(4):
        try:
            page.goto(URL, timeout=60000, wait_until="domcontentloaded")
            break
        except Exception as e:
            print(f"goto attempt {attempt + 1} failed: {e}")
            page.wait_for_timeout(3000)
    page.wait_for_timeout(7000)

    body = page.inner_text("body")
    days = set()
    for b in page.query_selector_all("button"):
        txt = (b.inner_text() or "").strip().replace("\n", " ").upper()
        if re.search(rf"\b({MONTHS})\b", txt):
            m = re.search(r"\b(\d{1,2})\b", txt)
            if m:
                days.add(int(m.group(1)))
    return body, days


def run():
    with sync_playwright() as p:
        print(f"Odyssey watcher started ({'TEST MODE' if TEST_MODE else 'LIVE'})...")
        browser = p.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = ctx.new_page()

        start = time.time()
        while time.time() - start < RUN_TIME:
            try:
                body, days = read_page(page)
                up = body.upper()
                has_loc = TARGET_LOC in up
                new_days = sorted(days - BASELINE_DAYS)
                print(f"dates listed: {sorted(days)} | Havelock: {has_loc} "
                      f"| {TARGET_TIME} present: {TARGET_TIME in body}")

                if TARGET_DAY in days and has_loc:
                    time_note = "12:45 PM listed" if TARGET_TIME in body else "check times"
                    spam_alarm(
                        f"WAKE UP! The Odyssey IMAX Havelock — Jul {TARGET_DAY} IS LIVE "
                        f"({time_note})! BOOK NOW: {URL}"
                    )
                    browser.close()
                    return

                if new_days:
                    send_telegram(
                        f"Heads up: new Odyssey date(s) added at Havelock: "
                        f"Jul {new_days} (target Jul {TARGET_DAY} not up yet). {URL}"
                    )

            except Exception as e:
                print(f"loop error: {e}")

            if TEST_MODE:
                break  # one pass only when testing locally
            time.sleep(CHECK_INTERVAL)

        browser.close()


if __name__ == "__main__":
    run()
