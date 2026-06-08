"""Real-HTTP end-to-end smoke driver. Assumes RetroShelf is live on BASE and a
fake/real Kavita is reachable. Exercises home → feed → book → download/cover and
asserts the iOS-critical headers over real sockets. Exit 0 on success."""
import re
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8099"
SECRET_MARKERS = ("/api/opds/", "apiKey=", "KEY/")

errors: list[str] = []


def check(cond, msg):
    if not cond:
        errors.append(msg)
        print("  FAIL:", msg)
    else:
        print("  ok:", msg)


def no_leak(text, where):
    for m in SECRET_MARKERS:
        if m in text:
            errors.append(f"apiKey/upstream leak ({m}) in {where}")
            print(f"  FAIL: leak {m} in {where}")
            return
    print(f"  ok: no leak in {where}")


with httpx.Client(base_url=BASE, timeout=10, follow_redirects=True) as c:
    print("[health]")
    r = c.get("/health")
    check(r.status_code == 200 and r.text == "ok", "/health == 'ok'")
    check(r.headers["content-type"].startswith("text/plain"), "/health is text/plain")

    print("[home]")
    r = c.get("/")
    check(r.status_code == 200, "/ 200")
    no_leak(r.text, "home")
    fid = re.search(r"/feed/([\w\-.]+)", r.text).group(1)

    print("[feed root]")
    r = c.get(f"/feed/{fid}")
    check(r.status_code == 200, "/feed root 200")
    no_leak(r.text, "feed root")

    # Walk feeds until we find a book detail link.
    book_id = None
    seen = set()
    frontier = re.findall(r'/feed/([\w\-.]+)"', r.text)
    for f2 in frontier:
        if f2 in seen:
            continue
        seen.add(f2)
        page = c.get(f"/feed/{f2}")
        no_leak(page.text, f"feed {f2[:8]}")
        m = re.search(r'/book/([\w\-.]+)"', page.text)
        if m:
            book_id = m.group(1)
            break
    check(book_id is not None, "found a book detail link")

    print("[book detail]")
    rb = c.get(f"/book/{book_id}")
    check(rb.status_code == 200, "/book 200")
    no_leak(rb.text, "book detail")
    dl = re.search(r'/download/([\w\-.]+)/([^"]+\.(?:epub|pdf))', rb.text)
    check(dl is not None, "extension-bearing download link present")

    if dl:
        did, fname = dl.group(1), dl.group(2)
        print(f"[download] {fname}")
        # HEAD first (header check), then GET (body).
        rh = c.head(f"/download/{did}/{fname}")
        ct = rh.headers.get("content-type", "")
        cd = rh.headers.get("content-disposition", "")
        check(ct in ("application/epub+zip", "application/pdf"), f"download Content-Type ({ct})")
        check("filename=" in cd, f"Content-Disposition has filename ({cd})")
        check(rh.headers.get("x-content-type-options") == "nosniff", "nosniff present")
        if fname.endswith(".epub"):
            check(ct == "application/epub+zip", "EPUB mime")
            check("attachment" in cd, "EPUB attachment disposition")
            check(".epub" in cd and ".zip" not in cd, "EPUB filename not .zip")
        rget = c.get(f"/download/{did}/{fname}")
        check(rget.status_code == 200 and len(rget.content) > 0, "download body streams")

    # cover (if present)
    cov = re.search(r'/cover/([\w\-.]+)', rb.text) or re.search(r'/cover/([\w\-.]+)', c.get(f"/feed/{f2}").text)
    if cov:
        print("[cover]")
        rc = c.get(f"/cover/{cov.group(1)}")
        check(rc.status_code == 200, "cover 200")
        check(rc.headers.get("content-type", "").startswith("image/"), "cover is image/*")

print()
if errors:
    print(f"SMOKE FAIL ({len(errors)} issues)")
    sys.exit(1)
print("SMOKE PASS")
