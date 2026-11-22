"""Signed book ids whose payload is not a book record (no ``u`` URL, or not a
JSON object) get the "Malformed book id" 404, never a 500.

Ids are MAC-signed, so these can only come from a token minted for something
else or by a future bug, but every route that decodes a book id must refuse
them cleanly.
"""
import pytest

from tests.test_app import make_client, make_handler, _token

PAYLOADS = ['{"t":"No URL"}', '[1, 2]', '7', '{"u": null, "t": "Null URL"}']


def _mint(client, payload: str) -> str:
    return client.app.state.ids.encode(payload)


@pytest.mark.parametrize("payload", PAYLOADS)
def test_star_refuses_a_record_without_a_url(payload):
    with make_client(make_handler()) as client:
        bid = _mint(client, payload)
        resp = client.get(f"/star/{bid}?t={_token(client)}", follow_redirects=False)
        assert resp.status_code == 404
        assert "not valid" in resp.text
        assert "No URL" not in client.get("/list").text


@pytest.mark.parametrize("payload", PAYLOADS)
def test_shelf_add_refuses_a_record_without_a_url(payload):
    with make_client(make_handler()) as client:
        tok = _token(client)
        client.get(f"/shelves/create?name=S&t={tok}", follow_redirects=False)
        import re
        sid = re.search(r'/shelf/([0-9a-f]+)"', client.get("/shelves").text).group(1)
        bid = _mint(client, payload)
        resp = client.get(f"/shelf/{sid}/add/{bid}?t={tok}", follow_redirects=False)
        assert resp.status_code == 404
        assert "not valid" in resp.text


@pytest.mark.parametrize("payload", ['[1, 2]', '7'])
def test_book_page_refuses_a_non_object_record(payload):
    with make_client(make_handler()) as client:
        resp = client.get(f"/book/{_mint(client, payload)}")
        assert resp.status_code == 404
        assert "not valid" in resp.text
