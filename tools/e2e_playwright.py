"""End-to-end Playwright test of RetroShelf against a live OPDS server.

Drives a real (Chromium) browser through the no-JS flow:
  home → Browse the Library → a navigation feed → ... → a book detail page →
  verify the "Open in iBooks/Open PDF" download link, then fetch the download
  URL and assert the iOS-critical headers (Content-Type / Content-Disposition).

Usage: .venv/bin/python tools/e2e_playwright.py [BASE]
Assumes RetroShelf is already running at BASE (default http://127.0.0.1:8099).
Exit 0 on success.
"""
import re
import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8099"
errors: list[str] = []


def check(cond, msg):
    print(("  ok: " if cond else "  FAIL: ") + msg)
    if not cond:
        errors.append(msg)


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        # Emulate an old iPad viewport (1024x768) to mirror the target device.
        ctx = browser.new_context(viewport={"width": 1024, "height": 768})
        page = ctx.new_page()

        print("[home]")
        page.goto(BASE, wait_until="domcontentloaded", timeout=30000)
        check("RetroShelf" in page.content(), "home shows RetroShelf")
        body = page.content()
        check("/api/opds/" not in body and "apiKey=" not in body, "no apiKey leak on home")
        browse = page.query_selector("a.button")
        check(browse is not None, "home has a Browse button")

        print("[browse → library root]")
        page.click("a.button")  # Browse the Library
        page.wait_for_load_state("domcontentloaded")
        check("/feed/" in page.url, "navigated into a /feed/ page")

        # Walk navigation links (depth-first, bounded) until we reach a book detail.
        print("[walk to a book]")
        book_url = None
        visited = set()
        frontier = [page.url]
        depth = 0
        while frontier and book_url is None and depth < 6:
            url = frontier.pop(0)
            if url in visited:
                continue
            visited.add(url)
            depth += 1
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            check("/api/opds/" not in page.content(), f"no apiKey leak @ depth {depth}")
            # A book detail link?
            detail = page.query_selector("a.book[href*='/book/']")
            if detail:
                book_url = BASE + detail.get_attribute("href")
                break
            # else collect nav feed links to follow
            for a in page.query_selector_all("a.navlink[href*='/feed/'], a.book[href*='/book/']"):
                href = a.get_attribute("href")
                if href and href.startswith("/"):
                    frontier.append(BASE + href)
        check(book_url is not None, "reached a book detail link")

        download_href = None
        badge = None
        if book_url:
            print("[book detail]")
            page.goto(book_url, wait_until="domcontentloaded", timeout=30000)
            content = page.content()
            check("/api/opds/" not in content and "apiKey=" not in content, "no apiKey leak on book page")
            dl = page.query_selector("a.button[href*='/download/']")
            check(dl is not None, "book page has a download button")
            if dl:
                download_href = dl.get_attribute("href")
                btn_text = (dl.inner_text() or "").strip()
                check(bool(re.search(r"\.(epub|pdf)$", download_href)),
                      f"download URL is extension-bearing ({download_href[-40:]})")
                check("iBooks" in btn_text or "PDF" in btn_text, f"button says Open in iBooks/PDF ({btn_text!r})")

        if download_href:
            print("[download headers] (via browser request)")
            resp = ctx.request.get(BASE + download_href)
            ct = resp.headers.get("content-type", "")
            cd = resp.headers.get("content-disposition", "")
            check(resp.status in (200, 206), f"download status {resp.status}")
            check(ct in ("application/epub+zip", "application/pdf"), f"Content-Type {ct}")
            check("filename=" in cd, f"Content-Disposition has filename ({cd[:60]})")
            check(resp.headers.get("x-content-type-options") == "nosniff", "nosniff header present")
            if download_href.endswith(".epub"):
                check(ct == "application/epub+zip" and "attachment" in cd and ".zip" not in cd,
                      "EPUB: epub+zip + attachment, not .zip")

        browser.close()


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:  # noqa: BLE001
        print("E2E EXCEPTION:", type(exc).__name__, exc)
        errors.append(str(exc))
    print()
    print("E2E PASS" if not errors else f"E2E FAIL ({len(errors)} issues)")
    sys.exit(1 if errors else 0)
