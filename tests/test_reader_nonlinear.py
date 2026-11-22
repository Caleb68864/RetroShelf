"""EPUB spine items marked linear="no" stay reachable but out of the
Prev/Next flow (decided by agent 2026-11-21)."""
import io
import zipfile

from app import reader
from tests.test_reader_routes import _bid, make_client, make_handler
from tests.test_reader_shelve import make_epub


def _nonlinear_epub(*nonlinear: int, chapters: int = 3) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(make_epub(chapters=chapters, image=False)))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for info in src.infolist():
            data = src.read(info)
            if info.filename.endswith(".opf"):
                for i in nonlinear:
                    data = data.replace(f'<itemref idref="ch{i}"/>'.encode(),
                                        f'<itemref idref="ch{i}" linear="no"/>'.encode())
            z.writestr(info, data)
    return out.getvalue()


def test_next_and_prev_skip_a_nonlinear_chapter(tmp_path):
    with make_client(make_handler(_nonlinear_epub(1))[0], str(tmp_path)) as client:
        bid = _bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)
        ch0 = client.get(f"/read/{bid}/0/1").text
        assert f"/read/{bid}/2/1" in ch0 and f"/read/{bid}/1/1" not in ch0
        ch2 = client.get(f"/read/{bid}/2/1").text
        assert f"/read/{bid}/0/" in ch2 and f"/read/{bid}/1/" not in ch2
        # Still reachable directly and from the contents page.
        assert client.get(f"/read/{bid}/1/1").status_code == 200
        assert f"/read/{bid}/1/1" in client.get(f"/read/{bid}/toc").text


def test_reading_starts_at_the_first_linear_chapter(tmp_path):
    with make_client(make_handler(_nonlinear_epub(0))[0], str(tmp_path)) as client:
        bid = _bid(client)
        r = client.get(f"/read/{bid}", follow_redirects=False)
        assert r.headers["location"] == f"/read/{bid}/1/1"


def test_all_nonlinear_book_still_opens(tmp_path):
    with make_client(make_handler(_nonlinear_epub(0, 1, 2))[0], str(tmp_path)) as client:
        bid = _bid(client)
        r = client.get(f"/read/{bid}", follow_redirects=False)
        assert r.headers["location"] == f"/read/{bid}/0/1"


def test_linear_flag_round_trips_and_old_manifests_are_linear():
    m = reader.Manifest(version=2, book_key="k", title="t", author="", images=0,
                        total_chars=0, created=0.0, chapters=[
                            reader.ChapterMeta("a", 1, 1),
                            reader.ChapterMeta("b", 1, 1, linear=False)])
    d = reader._manifest_to_dict(m)
    assert "linear" not in d["chapters"][0]  # unchanged shape for linear ones
    assert [c.linear for c in reader._manifest_from_dict(d).chapters] == [True, False]
