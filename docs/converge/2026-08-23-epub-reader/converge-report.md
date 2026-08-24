# Converge Report — epub-reader

**Outcome:** CONVERGED
**Passes used:** 6 / 20
**References:** docs/specs/2026-08-23-epub-reader.md

Convergence = 3 consecutive clean passes (4, 5, 6), the 3rd adversarial and
context-free, verifying by execution + graph reachability rather than reading.

## Met% per pass
| Pass | Mode | Met% | Gaps | Fixed |
|------|------|------|------|-------|
| 1 | standard | 12/13 | 1 (R8 prune unwired) | yes |
| 2 | standard | 13/13 | 0 | — |
| 3 | standard (ACs + edge cases) | — | 2 (stalled-upstream timeout untested; stale-rename branch untested) | yes |
| 4 | standard (comprehensive) | all Met | 0 | — |
| 5 | standard (constraints focus) | all Met | 0 | — |
| 6 | **adversarial** (execution + reachability) | all Met | 0 | — |

## Gaps found and closed
1. **R8 reader-cache prune (pass 1)** — `prune_reader_cache` /
   `MAX_READER_CACHE_BYTES` were defined but never called; the 1GB cap was not
   enforced at runtime. Wired into `shelve_book` after a successful shelve;
   regression test `test_shelve_invokes_reader_cache_prune`.
2. **Stalled-upstream timeout (pass 3)** — the `asyncio.wait_for(SHELVE_TIMEOUT)`
   bound existed but was untested; added
   `test_stalled_upstream_is_bounded_not_indefinite`. Spec Edge Cases wording
   aligned to the code's correct reader-specific `ReaderError` (a
   `RetroShelfError`), not the imprecise "KavitaError".
3. **Stale-final-dir rename recovery (pass 3)** — the `os.rename`-onto-non-empty
   recovery branch was untested; added `test_stale_final_dir_is_replaced_not_a_500`.

## Adversarial confirmation (pass 6)
- Ran the gate itself: 305 passed, mypy 0 issues, ruff clean.
- Graph reachability: every reader symbol (shelve_book, load_manifest,
  load_chapter, prune_reader_cache, parts_for, part_containing, percent_of,
  set_position/get_position/reading_list, sanitize_chapter) and all four /read
  routes have live callers in `_register_routes` — none defined-but-unwired.
- Disproof attempts all failed: no book-controlled string reaches `| safe`
  besides sanitizer output; `{IMG:}/{CH:}` cannot be forged into a link; the
  SS-06 download-header regression assertion is real (byte-identical `==`);
  no stubs or hardcoded returns.

## Residual gaps
None.

## Frozen gaps
None.
