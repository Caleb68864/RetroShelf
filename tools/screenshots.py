"""Capture screenshots of RetroShelf pages at old-iPad resolution for visual
review. Assumes the app is live at BASE. Saves PNGs to /tmp/rs_shots/."""
import os
import re
import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8099"
OUT = "/tmp/rs_shots"
os.makedirs(OUT, exist_ok=True)


def shot(page, name):
    page.screenshot(path=f"{OUT}/{name}.png", full_page=True)
    print("shot:", name)


with sync_playwright() as p:
    b = p.chromium.launch()
    # iPad portrait 768x1024 and landscape 1024x768.
    for label, vw, vh in (("ipad-portrait", 768, 1024), ("ipad-landscape", 1024, 768)):
        ctx = b.new_context(viewport={"width": vw, "height": vh}, device_scale_factor=1)
        page = ctx.new_page()
        page.goto(BASE, wait_until="networkidle", timeout=45000)
        shot(page, f"01-home-{label}")
        # Search page
        page.goto(f"{BASE}/search?q=love", wait_until="networkidle", timeout=45000)
        shot(page, f"02-search-{label}")
        # Browse into a feed
        page.goto(BASE, wait_until="domcontentloaded")
        page.click("a.button")
        page.wait_for_load_state("networkidle")
        shot(page, f"03-feed-{label}")
        # Into a book detail
        book = page.query_selector("a.navlink[href*='/feed/'], a.book[href*='/book/']")
        # walk to a book
        for _ in range(4):
            d = page.query_selector("a.book[href*='/book/']")
            if d:
                page.goto(BASE + d.get_attribute("href"), wait_until="networkidle")
                shot(page, f"04-book-{label}")
                break
            nav = page.query_selector("a.navlink[href*='/feed/']")
            if not nav:
                break
            page.goto(BASE + nav.get_attribute("href"), wait_until="networkidle")
        page.goto(f"{BASE}/help", wait_until="networkidle")
        shot(page, f"05-help-{label}")
        ctx.close()
    b.close()
print("done →", OUT)
