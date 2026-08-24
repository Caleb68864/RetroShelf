"""End-to-end integration test for the in-browser EPUB reader (SS-06).

One [INTEGRATION] test that walks the complete user journey across every
reader sub-spec boundary (SS-01..05) through :class:`~fastapi.testclient.
TestClient` with Kavita mocked at the httpx transport layer, mirroring the
harness used by ``tests/test_reader_routes.py``: home -> book detail ->
"Read here" -> shelve -> read across a chapter boundary -> TOC -> change
page-size preference -> resume -> home shelf progress -> finish the book ->
re-check the EPUB download headers are byte-identical to before any reader
use (the iBooks-delivery regression guard).
"""
from __future__ import annotations

import re

from tests.test_reader_routes import _BIG_CHAPTER, _ONE_BLOCK_CHAPTER, _bid, _token, make_client, make_handler
from tests.test_reader_shelve import make_epub


def test_full_reader_journey_across_all_sub_spec_boundaries(tmp_path):
    # Three chapters: a big, multi-part chapter 0 (crosses into chapter 1
    # under both the default "medium" split and the smaller "small" split),
    # a second big chapter 1 to exercise mid-book resume-after-split-change,
    # and a single-block final chapter 2 so reaching "finished" is exact,
    # mirroring the pattern in test_book_page_shows_finished_after_last_part.
    handler, calls = make_handler(
        make_epub(chapters=3, chapter_bytes={0: _BIG_CHAPTER, 1: _BIG_CHAPTER, 2: _ONE_BLOCK_CHAPTER})
    )
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client)

        # -- capture the EPUB download headers BEFORE any reader use ---------
        book_page = client.get(f"/book/{bid}")
        assert book_page.status_code == 200
        assert "Read here" in book_page.text
        m = re.search(r'href="(/download/[^"]+)"', book_page.text)
        assert m, "no download link found on book page"
        download_url = m.group(1)
        baseline = client.get(download_url)
        assert baseline.status_code == 200
        baseline_ctype = baseline.headers["content-type"]
        baseline_disposition = baseline.headers["content-disposition"]

        # -- home page: no reading position yet -------------------------------
        home_before = client.get("/")
        assert "Currently Reading" not in home_before.text

        # -- follow the "Read here" link: shelve-once + resume ---------------
        r = client.get(f"/read/{bid}", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == f"/read/{bid}/0/1"
        assert calls  # shelving fetched the upstream book exactly here

        # -- read three parts crossing a chapter boundary ---------------------
        p1 = client.get(f"/read/{bid}/0/1")
        assert p1.status_code == 200
        p2 = client.get(f"/read/{bid}/0/2")  # still chapter 0, second part
        assert p2.status_code == 200
        p3 = client.get(f"/read/{bid}/1/1")  # crosses into chapter 1
        assert p3.status_code == 200

        # move further into chapter 1 so the stored block is not its first
        # (block 0 would trivially resolve to part 1 under any split size)
        p4 = client.get(f"/read/{bid}/1/2")
        assert p4.status_code == 200

        # -- TOC lists all chapters -------------------------------------------
        toc = client.get(f"/read/{bid}/toc")
        assert toc.status_code == 200
        assert f"/read/{bid}/0/1" in toc.text
        assert f"/read/{bid}/1/1" in toc.text
        assert f"/read/{bid}/2/1" in toc.text

        # -- home shows a rising percent under "Currently Reading" ------------
        home_mid = client.get("/")
        assert "Currently Reading" in home_mid.text
        m_pct = re.search(r"(\d+)%", home_mid.text)
        assert m_pct, "no progress percent on home page"
        percent_mid = int(m_pct.group(1))
        assert 0 < percent_mid < 100

        # -- change split size; resume lands on the part containing the ------
        # -- same stored block under the new size -----------------------------
        t = _token(client)
        pref_r = client.get(f"/prefs?split=small&next=/&t={t}", follow_redirects=False)
        assert pref_r.status_code == 303
        assert pref_r.cookies.get("rs_split") == "small"
        client.cookies.set("rs_split", "small")

        resume = client.get(f"/read/{bid}", follow_redirects=False)
        assert resume.status_code == 303
        loc = resume.headers["location"]
        assert loc.startswith(f"/read/{bid}/1/")  # still chapter 1, not reset
        assert loc != f"/read/{bid}/1/1"  # not reset to the chapter's first part
        assert client.get(loc).status_code == 200  # the resolved part is real

        # -- read to the final part: book page shows finished (100%) ----------
        finish = client.get(f"/read/{bid}/2/1")
        assert finish.status_code == 200
        book_after = client.get(f"/book/{bid}")
        assert "Read again" in book_after.text
        assert "finished" in book_after.text

        home_after = client.get("/")
        assert "finished" in home_after.text

        # -- re-fetch the EPUB download: headers byte-identical to baseline ---
        after = client.get(download_url)
        assert after.status_code == 200
        assert after.headers["content-type"] == baseline_ctype
        assert after.headers["content-disposition"] == baseline_disposition
