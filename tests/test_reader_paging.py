"""Tests for app.reader's pagination/progress helpers and app.store's
reading-position persistence (SS-03).
"""
from __future__ import annotations

import json
import os
import tempfile

from app.reader import SPLIT_TARGETS, ChapterMeta, Manifest, parts_for, part_containing, percent_of
from app.store import Store, book_key


def _lengths(n: int, each: int = 500) -> list[int]:
    return [each] * n


def _manifest(chapters: list[ChapterMeta]) -> Manifest:
    return Manifest(
        version=1,
        book_key="abc123",
        title="T",
        author="A",
        chapters=chapters,
        images=0,
        total_chars=sum(c.chars for c in chapters),
        created=0.0,
    )


# -- parts_for / part_containing ---------------------------------------------

def test_parts_for_empty_input():
    assert parts_for([], 6000) == []
    assert parts_for([], None) == []


def test_parts_for_none_target_is_single_part():
    lengths = _lengths(10)
    parts = parts_for(lengths, None)
    assert parts == [(0, 10)]


def test_parts_for_totality_all_split_settings():
    lengths = [200, 400, 6000, 900, 12500, 1, 1, 1, 30000, 500]
    for target in SPLIT_TARGETS.values():
        parts = parts_for(lengths, target)
        # Every block appears in exactly one part; none dropped/duplicated.
        covered: list[int] = []
        for start, end in parts:
            covered.extend(range(start, end))
        assert covered == list(range(len(lengths)))


def test_parts_for_oversized_block_is_singleton():
    # The first part fills exactly at block 0 (6000 >= target), so the
    # oversized block that follows starts a fresh part on its own —
    # greedy accumulation never splits it, and nothing precedes it to
    # merge with.
    lengths = [6000, 50000, 100]
    parts = parts_for(lengths, 6000)
    assert (1, 2) in parts
    for start, end in parts:
        if start == 1:
            assert end == 2


def test_parts_for_deterministic():
    lengths = [500, 500, 500, 500, 500, 500]
    assert parts_for(lengths, 1200) == parts_for(lengths, 1200)


def test_part_containing_round_trip_every_block():
    lengths = [300, 700, 5000, 200, 8000, 400]
    for target in SPLIT_TARGETS.values():
        parts = parts_for(lengths, target)
        for block_index in range(len(lengths)):
            part_no = part_containing(block_index, parts)
            assert 1 <= part_no <= len(parts)
            start, end = parts[part_no - 1]
            assert start <= block_index < end


def test_part_containing_out_of_range_clamps():
    parts = parts_for(_lengths(5), 2000)
    assert part_containing(-1, parts) == 1
    assert part_containing(1000, parts) == len(parts)


def test_part_containing_empty_parts():
    assert part_containing(0, []) == 1


def test_resume_across_size_change():
    # A block stored while reading at "medium" must resolve via
    # part_containing to the part holding the same block at "small" and
    # "whole".
    lengths = [400, 3000, 5000, 200, 9000, 1500, 300, 7000]
    medium_parts = parts_for(lengths, SPLIT_TARGETS["medium"])
    block_index = 4  # inside some medium-sized part
    medium_part_no = part_containing(block_index, medium_parts)
    assert 1 <= medium_part_no <= len(medium_parts)

    for setting in ("small", "whole"):
        other_parts = parts_for(lengths, SPLIT_TARGETS[setting])
        other_part_no = part_containing(block_index, other_parts)
        start, end = other_parts[other_part_no - 1]
        assert start <= block_index < end


# -- percent_of ---------------------------------------------------------------

def test_percent_of_monotonic_and_final_is_100():
    chapters = [
        ChapterMeta(title="1", blocks=4, chars=1000),
        ChapterMeta(title="2", blocks=4, chars=1000),
        ChapterMeta(title="3", blocks=4, chars=1000),
    ]
    manifest = _manifest(chapters)
    prior = -1
    for chapter in range(3):
        for block in range(4):
            pct = percent_of(manifest, chapter, block)
            assert pct >= prior
            prior = pct
    assert percent_of(manifest, 2, 3) == 100


def test_percent_of_empty_manifest():
    manifest = _manifest([])
    assert percent_of(manifest, 0, 0) == 0


# -- Store reading positions ---------------------------------------------------

def _store() -> Store:
    return Store(os.path.join(tempfile.mkdtemp(), "state.json"))


def test_set_and_get_position():
    s = _store()
    record = {"u": "http://x/1.epub", "t": "Book One"}
    s.set_position(record, chapter=2, block=5, percent=42)
    key = book_key("http://x/1.epub")
    pos = s.get_position(key)
    assert pos is not None
    assert pos["chapter"] == 2
    assert pos["block"] == 5
    assert pos["percent"] == 42
    assert pos["t"] == "Book One"


def test_get_position_missing_returns_none():
    s = _store()
    assert s.get_position("no-such-key") is None


def test_reading_list_most_recent_first():
    s = _store()
    s.set_position({"u": "http://x/1.epub", "t": "A"}, 0, 0, 10)
    s.set_position({"u": "http://x/2.epub", "t": "B"}, 0, 0, 20)
    s.set_position({"u": "http://x/3.epub", "t": "C"}, 0, 0, 30)
    titles = [r["t"] for r in s.reading_list(limit=4)]
    assert titles == ["C", "B", "A"]


def test_reading_list_limit():
    s = _store()
    for i in range(6):
        s.set_position({"u": f"http://x/{i}.epub", "t": str(i)}, 0, 0, 0)
    assert len(s.reading_list(limit=4)) == 4


def test_reading_position_persists_across_reload():
    path = os.path.join(tempfile.mkdtemp(), "state.json")
    s1 = Store(path)
    s1.set_position({"u": "http://x/1.epub", "t": "Persisted"}, chapter=1, block=2, percent=55)
    key = book_key("http://x/1.epub")
    s2 = Store(path)  # fresh instance reloads from disk
    pos = s2.get_position(key)
    assert pos is not None
    assert pos["chapter"] == 1
    assert pos["block"] == 2
    assert pos["percent"] == 55


def test_reading_cap_drops_oldest():
    s = _store()
    for i in range(105):
        s.set_position({"u": f"http://x/{i}.epub", "t": str(i)}, 0, 0, 0)
    assert len(s.reading_list(limit=1000)) == 100
    # The earliest-added entry should have been dropped.
    assert s.get_position(book_key("http://x/0.epub")) is None
    assert s.get_position(book_key("http://x/104.epub")) is not None


def test_wrong_shaped_reading_data_loads_empty():
    path = os.path.join(tempfile.mkdtemp(), "state.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"reading": ["not", "a", "dict"]}, f)
    s = Store(path)
    assert s.reading_list() == []

    path2 = os.path.join(tempfile.mkdtemp(), "state.json")
    with open(path2, "w", encoding="utf-8") as f:
        json.dump({"reading": {"junk1": "not-a-dict", "junk2": {"no": "position-fields"}}}, f)
    s2 = Store(path2)
    assert s2.reading_list() == []
