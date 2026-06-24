"""
The Odyssey — IMAX Havelock booking watcher.

Watches the Scope Cinemas showtimes page for The Odyssey and fires a loud
Telegram alarm the moment a NEW date appears in the Havelock IMAX schedule
(target: Jul 22, 12:45 PM). Built on the proven Avatar approach: a headless
Playwright browser reloads the page on a short interval; GitHub Actions keeps
it running in the cloud.

Run locally with no secrets set -> TEST MODE: prints what it sees, sends nothing.
"""

import json
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
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "15"))  # 15s (Avatar proved this is fine)
RUN_TIME = 18000                # total run ~5 hours (under GitHub's 6h job limit)

MONTHS = "JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"

# Blindness / block detection: a real loaded page must contain the movie title.
# (We do NOT use "SCOPE" — it appears in the nav/footer even when the showtimes
# content fails to render, which would mask a broken read.)
PAGE_OK_MARKERS = ("ODYSSEY",)
# Recycling wipes the Cloudflare clearance cookie -> forces a fresh challenge each time,
# which is counter-productive. Keep one browser so the cf_clearance cookie persists.
RECYCLE_AFTER = 10 ** 9  # effectively off
BLOCK_HINTS = ("ATTENTION REQUIRED", "CLOUDFLARE", "ACCESS DENIED", "ARE YOU HUMAN",
               "CAPTCHA", "FORBIDDEN", "TOO MANY REQUESTS", "RATE LIMIT")
BLIND_THRESHOLD = 5  # consecutive unreadable checks (~75s) before warning you

# --- SECRETS (set as GitHub Actions secrets) ---
BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
CHAT_ID = os.getenv("TG_CHAT_ID")
TEST_MODE = not (BOT_TOKEN and CHAT_ID)


def send_telegram(message, with_stop_button=False):
    if TEST_MODE:
        print("[TEST MODE] would send Telegram:", message)
        return
    data = {"chat_id": CHAT_ID, "text": message}
    if with_stop_button:
        data["reply_markup"] = json.dumps(
            {"inline_keyboard": [[{"text": "🛑 STOP ALERTS", "callback_data": "stop"}]]}
        )
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=data, timeout=10
        )
    except Exception as e:
        print(f"Telegram error: {e}")


def latest_update_id():
    """Baseline so we only react to NEW taps/replies once the alarm starts."""
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"timeout": 0}, timeout=10,
        )
        res = r.json().get("result", [])
        return res[-1]["update_id"] if res else 0
    except Exception:
        return 0


def user_acknowledged(offset):
    """True if you tapped STOP or replied 'stop' since the alarm began."""
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"offset": offset + 1, "timeout": 0}, timeout=10,
        )
        for u in r.json().get("result", []):
            cb = u.get("callback_query")
            if cb and cb.get("data") == "stop":
                try:
                    requests.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery",
                        data={"callback_query_id": cb["id"], "text": "Alerts stopped"},
                        timeout=10,
                    )
                except Exception:
                    pass
                return True
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) == str(CHAT_ID) and \
                    msg.get("text", "").strip().lower() in ("stop", "/stop"):
                return True
    except Exception:
        pass
    return False


def spam_alarm(message, times=200, gap=5):
    print(f"ALARM: {message}")
    offset = latest_update_id()
    for _ in range(times):
        send_telegram(message, with_stop_button=True)
        if TEST_MODE:
            break  # don't loop in test mode
        for _ in range(gap):  # check once per second so STOP is near-instant
            time.sleep(1)
            if user_acknowledged(offset):
                send_telegram("🔕 Alerts stopped — go book E13/E14, 12:45 PM. Good luck!")
                print("user acknowledged — stopping alerts")
                return


def read_page(page):
    """Return (full_body_text, set_of_day_numbers_in_date_row)."""
    for attempt in range(4):
        try:
            page.goto(URL, timeout=60000, wait_until="domcontentloaded")
            break
        except Exception as e:
            print(f"goto attempt {attempt + 1} failed: {e}")
            page.wait_for_timeout(3000)
    # Let content render — and give any Cloudflare challenge time to auto-solve
    # (a real browser passes the managed challenge and gets a cf_clearance cookie).
    body = ""
    for _ in range(3):
        page.wait_for_timeout(8000)
        try:
            body = page.inner_text("body")
        except Exception as e:
            print(f"could not read page body: {e}")
            body = ""
        if "ODYSSEY" in body.upper():
            break  # real content rendered; challenge (if any) has cleared
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

        def make_browser():
            b = p.chromium.launch(
                headless=True, args=["--disable-blink-features=AutomationControlled"]
            )
            ctx = b.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            return b, ctx.new_page()

        browser, page = make_browser()
        browser_started = time.time()

        start = time.time()
        heartbeat_sent = False
        blind_count = 0
        blind_alerted = False
        while time.time() - start < RUN_TIME:
            # recycle the browser periodically so it never runs long enough to rot
            if time.time() - browser_started > RECYCLE_AFTER:
                print("recycling browser (anti-rot)")
                try:
                    browser.close()
                except Exception:
                    pass
                browser, page = make_browser()
                browser_started = time.time()
            try:
                body, days = read_page(page)
                up = body.upper()

                # --- blindness / block detection ---
                page_readable = len(up) > 50 and any(m in up for m in PAGE_OK_MARKERS)
                if not page_readable:
                    blind_count += 1
                    print(f"BLIND read #{blind_count} (body len={len(up)})")
                    if blind_count >= BLIND_THRESHOLD and not blind_alerted:
                        hint = next((h for h in BLOCK_HINTS if h in up), "")
                        reason = f"possible block: {hint}" if hint else "page empty/unrecognized"
                        send_telegram(
                            f"⚠️ Watcher BLIND for {blind_count} checks ({reason}). "
                            f"It may be blocked — check the page manually: {URL}"
                        )
                        blind_alerted = True
                    if TEST_MODE:
                        break
                    time.sleep(CHECK_INTERVAL)
                    continue

                if blind_alerted:  # recovered after a blind spell
                    send_telegram("✅ Watcher can read the Scope page again (recovered).")
                blind_count = 0
                blind_alerted = False

                has_loc = TARGET_LOC in up
                new_days = sorted(days - BASELINE_DAYS)
                print(f"dates listed: {sorted(days)} | Havelock: {has_loc} "
                      f"| {TARGET_TIME} present: {TARGET_TIME in body}")

                if not heartbeat_sent:
                    # one ping per run, proving the cloud job loaded the page + can reach you
                    send_telegram(
                        f"✅ Watcher alive (GitHub cloud). Sees dates {sorted(days)}, "
                        f"Havelock={has_loc}. Waiting for Jul {TARGET_DAY} {TARGET_TIME}."
                    )
                    heartbeat_sent = True

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
