"""Passes 7-9 probe (real HTTP against the live bridge on BASE):
  7. Range request → 206 + Content-Range relay
  8. PDF acquisition → application/pdf + inline disposition
Assumes RetroShelf + fake Kavita are running. Exit 0 on success."""
import re
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8099"
errors = []


def check(cond, msg):
    print(("  ok: " if cond else "  FAIL: ") + msg)
    if not cond:
        errors.append(msg)


with httpx.Client(base_url=BASE, timeout=10) as c:
    home = c.get("/").text
    fid = re.search(r"/feed/([\w\-.]+)", home).group(1)
    # collect all book detail ids across feeds
    book_ids = []
    page = c.get(f"/feed/{fid}").text
    for f2 in re.findall(r'/feed/([\w\-.]+)"', page):
        sub = c.get(f"/feed/{f2}").text
        book_ids += re.findall(r'/book/([\w\-.]+)"', sub)
    check(bool(book_ids), "found book detail links")

    epub_dl = pdf_dl = None
    for bid in book_ids:
        detail = c.get(f"/book/{bid}").text
        m = re.search(r'/download/([\w\-.]+)/([^"]+\.(epub|pdf))', detail)
        if not m:
            continue
        if m.group(3) == "epub" and not epub_dl:
            epub_dl = (m.group(1), m.group(2))
        if m.group(3) == "pdf" and not pdf_dl:
            pdf_dl = (m.group(1), m.group(2))

    # Pass 8: PDF disposition + mime
    check(pdf_dl is not None, "found a PDF book")
    if pdf_dl:
        did, fname = pdf_dl
        r = c.get(f"/download/{did}/{fname}")
        check(r.headers["content-type"] == "application/pdf", f"PDF mime ({r.headers['content-type']})")
        check("inline" in r.headers["content-disposition"], f"PDF inline disposition ({r.headers['content-disposition']})")
        check(r.content.startswith(b"%PDF"), "PDF body streamed")

    # Pass 7: Range relay on the EPUB
    check(epub_dl is not None, "found an EPUB book")
    if epub_dl:
        did, fname = epub_dl
        r = c.get(f"/download/{did}/{fname}", headers={"Range": "bytes=0-3"})
        check(r.status_code == 206, f"Range → 206 ({r.status_code})")
        check("content-range" in r.headers, f"Content-Range relayed ({r.headers.get('content-range')})")
        check(len(r.content) == 4, f"partial body is 4 bytes ({len(r.content)})")
        # full GET still works
        full = c.get(f"/download/{did}/{fname}")
        check(full.status_code == 200, "full GET still 200")

print()
print("ADVANCED PROBE PASS" if not errors else f"ADVANCED PROBE FAIL ({len(errors)})")
sys.exit(1 if errors else 0)
