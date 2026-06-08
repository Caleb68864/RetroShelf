"""Capture home + a books-listing + search for visual review (app must be live)."""
from playwright.sync_api import sync_playwright

B = "http://127.0.0.1:8099"
with sync_playwright() as p:
    br = p.chromium.launch()
    ctx = br.new_context(viewport={"width": 768, "height": 1024})
    pg = ctx.new_page()
    pg.goto(B, wait_until="networkidle"); pg.screenshot(path="/tmp/rs_shots/home.png", full_page=True)
    pg.goto(B, wait_until="domcontentloaded"); pg.click("a.button"); pg.wait_for_load_state("networkidle")
    nav = pg.query_selector("a.navlink[href*='/feed/']")
    pg.goto(B + nav.get_attribute("href"), wait_until="networkidle")
    pg.screenshot(path="/tmp/rs_shots/list.png", full_page=True)
    pg.goto(B + "/search?q=love", wait_until="networkidle")
    pg.screenshot(path="/tmp/rs_shots/search.png", full_page=True)
    br.close()
print("shots done")
