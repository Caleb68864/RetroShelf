# Converge pass 1 — standard

Reference: docs/specs/2026-08-23-epub-reader.md
Requirements scored: 13. Met: 12. Gaps: 1.

## Gap found (R8, Partial)
`prune_reader_cache` + `MAX_READER_CACHE_BYTES` (app/reader.py) were
defined-but-unwired — zero callers — so Requirement 8's "≤ 1GB reader cache,
pruned oldest-first" was never enforced at runtime.

## Fix dispatched
Wired `prune_reader_cache(cache_dir, MAX_READER_CACHE_BYTES)` into `shelve_book`
after the successful rename (best-effort, never fails a completed shelve).
Regression test `test_shelve_invokes_reader_cache_prune` asserts the call site.

Met% this pass: 12/13 → 13/13 after fix. clean_streak reset to 0.
Gate: pytest 303 passed, mypy 0, ruff clean.
