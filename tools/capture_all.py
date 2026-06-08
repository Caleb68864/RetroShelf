"""Self-contained screenshot capture: starts uvicorn as a subprocess, drives
Playwright against live ManyBooks, then tears uvicorn down. One process."""
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = "/tmp/rs_shots"
os.makedirs(OUT, exist_ok=True)
B = "http://127.0.0.1:8099"

env = dict(os.environ)
env["KAVITA_OPDS_URL"] = "https://manybooks.net/opds"
env["BRIDGE_ID_SECRET"] = "cap"

proc = subprocess.Popen(
    [os.path.join(ROOT, ".venv/bin/python"), "-m", "uvicorn",
     "app.main:app", "--host", "127.0.0.1", "--port", "8099"],
    cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    for _ in range(30):
        try:
            urllib.request.urlopen(B + "/health", timeout=2)
            break
        except Exception:
            time.sleep(1)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        br = p.chromium.launch()
        ctx = br.new_context(viewport={"width": 768, "height": 1024})
        pg = ctx.new_page()
        pg.goto(B, wait_until="networkidle"); pg.screenshot(path=f"{OUT}/home.png", full_page=True)
        pg.goto(B, wait_until="domcontentloaded"); pg.click("a.button"); pg.wait_for_load_state("networkidle")
        nav = pg.query_selector("a.navlink[href*='/feed/']")
        pg.goto(B + nav.get_attribute("href"), wait_until="networkidle")
        pg.screenshot(path=f"{OUT}/list.png", full_page=True)
        pg.goto(B + "/search?q=love", wait_until="networkidle")
        pg.screenshot(path=f"{OUT}/search.png", full_page=True)
        br.close()
    print("CAPTURE OK")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
