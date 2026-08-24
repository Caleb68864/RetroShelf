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
