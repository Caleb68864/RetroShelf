"""Pass 4 probe: render every template with adversarial/edge-case contexts.
Catches Jinja UndefinedError and XSS (autoescape) regressions."""
from app.render import templates

env = templates.env
ok = True
print("autoescape:", env.autoescape)

cases = {
    "home.html": [
        {"kavita_ok": False, "status_detail": None, "root_feed_url": "/feed/x"},
        {"kavita_ok": True, "status_detail": "", "root_feed_url": "/feed/x"},
    ],
    "feed.html": [
        {"feed_title": "", "entries": [], "next_url": None, "prev_url": None, "search_url": None},
        {"feed_title": "T<>&'\"", "entries": [
            {"is_nav": True, "title": None, "href": "/feed/n"},
            {"is_nav": False, "title": None, "author": None, "badge": "PDF", "detail_url": "/book/x", "cover_url": None},
        ], "next_url": "/feed/n", "prev_url": "/feed/p", "search_url": "/search"},
    ],
    "book.html": [
        {"title": "", "author": "", "summary": "", "badge": "EPUB", "cover_url": None,
         "download_url": "/download/x/y.epub", "back_url": "/"},
        {"title": "<x>", "author": "<y>", "summary": "<z>", "badge": "PDF", "cover_url": "/cover/c",
         "download_url": "/download/x/y.pdf", "back_url": "/feed/a"},
    ],
    "search.html": [{"query": "<script>alert(1)</script>", "entries": []}],
    "help.html": [{}],
    "error.html": [{"heading": "Not found", "message": "Bad <id> & stuff"}],
}

for name, ctxs in cases.items():
    for ctx in ctxs:
        try:
            html = env.get_template(name).render(**ctx)
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"RENDER FAIL {name} {ctx}: {type(e).__name__}: {e}")
            continue
        # XSS: raw <script> from user data must be escaped.
        if "<script>alert(1)" in html:
            ok = False
            print(f"XSS: unescaped script in {name}")

print("PASS" if ok else "FAIL")
