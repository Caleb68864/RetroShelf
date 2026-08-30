"""Template info-leak / XSS audit for the account chrome and account page.

A profile name or username is attacker-influenced text that is echoed into the
shared "Signed in as …" chrome and the /account page. These prove it is always
auto-escaped (no raw HTML reaches the page) and that the chrome only ever shows
the *current* session's own identity.
"""
from tests.test_auth_routes import _csrf, make_client, register_admin


def _add_and_select_profile(client, name: str) -> None:
    token = _csrf(client.get("/account").text)
    client.post("/account", data={"_csrf": token, "action": "add_profile",
                                  "name": name, "color": "green"}, follow_redirects=False)
    acct = client._store.get_account_by_username("admin")
    pid = next(pid for pid, p in acct["profiles"].items() if p["name"] == name.strip())
    token = _csrf(client.get("/profiles").text)
    client.post("/profiles", data={"_csrf": token, "action": "select",
                                   "profile_id": pid}, follow_redirects=False)


def test_hostile_profile_name_is_escaped_in_chrome_and_account():
    payload = '<script>alert(1)</script>'
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        _add_and_select_profile(client, payload)
        # The shared chrome on the home page shows the current profile name…
        home = client.get("/").text
        assert payload not in home  # never the raw tag
        assert "&lt;script&gt;" in home  # only the escaped form
        # …and the /account profile list is escaped the same way.
        acct_page = client.get("/account").text
        assert payload not in acct_page
        assert "&lt;script&gt;" in acct_page


def test_hostile_username_is_escaped_on_account_page():
    payload = 'ad"><img src=x>min'
    with make_client() as client:
        # Create the admin with a username carrying HTML metacharacters.
        token = _csrf(client.get("/setup").text)
        client.post("/setup", data={"_csrf": token, "username": payload,
                                    "password": "hunter2000", "confirm": "hunter2000"},
                    follow_redirects=False)
        page = client.get("/account").text
        assert "<img src=x>" not in page  # the raw tag never lands in the markup
        assert "&lt;img" in page  # escaped instead


# --- Structural invariants over the template sources --------------------------
# These read the .html files directly (no HTTP round-trip), so they hold even if
# a route stops exercising a template. They pin the project's core XSS contract:
# autoescape is on everywhere, and the ONLY place raw HTML is emitted is the
# sanitizer's own output on the reader page.
import pathlib  # noqa: E402
import re  # noqa: E402

_TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "app" / "templates"
# Matches Jinja's safe filter with any surrounding whitespace: `|safe`, `| safe`.
_SAFE_RE = re.compile(r"\|\s*safe\b")
# The auth chrome — a profile name / username is attacker-influenced text, so
# these must never mark anything safe.
_AUTH_TEMPLATES = ("login.html", "setup.html", "profiles.html", "account.html")


def _safe_hits(text: str) -> int:
    return len(_SAFE_RE.findall(text))


def test_exactly_one_safe_filter_across_all_templates():
    """The whole template tree contains exactly one `| safe`, and it is the
    sanitizer hand-off on the reader page (read.html). Any new `| safe` — the
    classic way an XSS creeps into a Jinja app — trips this."""
    hits = {p.name: _safe_hits(p.read_text(encoding="utf-8"))
            for p in sorted(_TEMPLATES.glob("*.html"))}
    offenders = {name: n for name, n in hits.items() if n and name != "read.html"}
    assert not offenders, f"unexpected `| safe` outside read.html: {offenders}"
    assert hits.get("read.html") == 1, (
        f"read.html must carry exactly one `| safe` (the sanitizer seam), "
        f"found {hits.get('read.html')}")


def test_auth_templates_never_mark_anything_safe():
    """Login/setup/profiles/account render attacker-influenced identity text and
    must keep every value autoescaped — zero `| safe`, belt-and-braces with the
    all-templates count above."""
    for name in _AUTH_TEMPLATES:
        text = (_TEMPLATES / name).read_text(encoding="utf-8")
        assert _safe_hits(text) == 0, f"{name} must not use the `| safe` filter"


def test_templates_do_not_disable_autoescape_or_reach_for_markup():
    """No template may switch autoescaping off or hand-build Markup — both route
    around the escaping the whole XSS story depends on."""
    for p in sorted(_TEMPLATES.glob("*.html")):
        text = p.read_text(encoding="utf-8")
        assert "autoescape false" not in text, f"{p.name} disables autoescape"
        assert "{% autoescape" not in text, f"{p.name} toggles autoescape"
        assert "Markup(" not in text, f"{p.name} constructs raw Markup"
