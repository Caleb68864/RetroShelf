# SS-06 evidence: end-to-end integration, docs

Final verification command outputs (last lines) after adding
`tests/test_reader_e2e.py`, the README "Read in the browser" section, and
this file. Run from the repo root.

## `pytest -q` (full suite)

```
$ .venv/bin/python -m pytest -q
........................................................................ [ 23%]
........................................................................ [ 47%]
........................................................................ [ 71%]
........................................................................ [ 95%]
..............                                                           [100%]
=============================== warnings summary ===============================
.venv/lib/python3.14/site-packages/fastapi/testclient.py:1
  /home/caleb/Projects/Kavita-Retro-iPad/.venv/lib/python3.14/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
302 passed, 1 warning in 1.54s
```

302 = 301 pre-existing + 1 new (`tests/test_reader_e2e.py::test_full_reader_journey_across_all_sub_spec_boundaries`).

## `mypy app`

```
$ .venv/bin/python -m mypy app
Success: no issues found in 13 source files
```

## `ruff check app tests`

```
$ .venv/bin/python -m ruff check app tests
All checks passed!
```
