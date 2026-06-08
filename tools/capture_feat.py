"""Self-contained feature screenshots: uvicorn subprocess + Playwright."""
import os
import re
import subprocess
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = "/tmp/rs_shots"
os.makedirs(OUT, exist_ok=True)
B = "http://127.0.0.1:8099"
env = {**os.environ, "KAVITA_OPDS_URL": "https://manybooks.net/opds",
       "KAVITA_FEED_NAME": "ManyBooks", "BRIDGE_ID_SECRET": "shot", "STATE_DIR": "/tmp/rs-shot"}
proc = subprocess.Popen([os.path.join(ROOT, ".venv/bin/python"), "-m", "uvicorn",
                         "app.main:app", "--host", "127.0.0.1", "--port", "8099"],
                        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(30):
        try:
            urllib.request.urlopen(B + "/health", timeout=2); break
        except Exception:
            time.sleep(1)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        br = p.chromium.launch()
        ctx = br.new_context(viewport={"width": 768, "height": 1024})
        pg = ctx.new_page()
        # green phosphor home
        pg.goto(B + "/prefs?color=green&next=/", wait_until="networkidle")
        pg.screenshot(path=f"{OUT}/feat-green-home.png", full_page=True)
        # walk to a book, star it
        pg.goto(B, wait_until="domcontentloaded"); pg.click("a.button")
        pg.wait_for_load_state("networkidle")
        nav = pg.query_selector("a.navlink[href*='/feed/']")
        pg.goto(B + nav.get_attribute("href"), wait_until="networkidle")
        book = pg.query_selector("a.book[href*='/book/']")
        pg.goto(B + book.get_attribute("href"), wait_until="networkidle")
        pg.screenshot(path=f"{OUT}/feat-book.png", full_page=True)
        star = pg.query_selector("a.button[href*='/star/']")
        if star:
            pg.goto(B + star.get_attribute("href"), wait_until="networkidle")
        # switch to amber + view reading list
        pg.goto(B + "/prefs?color=amber&next=/list", wait_until="networkidle")
        pg.screenshot(path=f"{OUT}/feat-list.png", full_page=True)
        br.close()
    print("CAPTURE OK")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
