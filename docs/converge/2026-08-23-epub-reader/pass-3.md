# Converge pass 3 — standard (sub-spec ACs + edge cases)

Different-angle scan: scored every SS-01..06 acceptance criterion and every
`## Edge Cases` item, not the 13 top-level requirements.

## Gaps found (2 — both code-present, test-absent; behavioral = unverified)
1. **Stalled upstream 120s timeout** — the `asyncio.wait_for(SHELVE_TIMEOUT)`
   path existed but no test exercised it; spec prose also said "KavitaError
   page" while the code (correctly) raises the reader-specific `ReaderError`.
2. **Concurrent shelve / lost-`os.rename` branch** — implemented but only the
   idempotent fast path was tested; the rename-onto-non-empty recovery branch
   was uncovered.

## Fixes
- `test_stalled_upstream_is_bounded_not_indefinite`: a stalling upstream +
  shrunk timeout asserts a bounded `RetroShelfError` (never a hang).
- `test_stale_final_dir_is_replaced_not_a_500`: a pre-seeded stale `{book_key}`
  dir forces the `os.rename` OSError → clear-and-retry recovery branch.
- Spec Edge Cases wording aligned to the correct working behavior (friendly
  `ReaderError` page, a `RetroShelfError`) — the code's reader-specific error
  type is more accurate than the imprecise "KavitaError" prose.

clean_streak reset to 0. Gate: pytest 305 passed, mypy 0, ruff clean.
