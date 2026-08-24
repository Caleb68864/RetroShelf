# Converge pass 6 — ADVERSARIAL (execution + reachability)

Fresh, context-free agent with Bash + graph tools, a different modality than
the static-reading standard passes: it executed the gate and traced live-path
reachability.

Executed itself:
- pytest -q → 305 passed
- mypy app → 0 issues
- ruff check app tests → clean
- structural greps: `| safe` = 1/0, no grid/flex, no `<script`, no new deps

Graph reachability: every reader symbol and all four /read routes have live
callers in `_register_routes` — none defined-but-unwired (prune_reader_cache
confirmed wired at reader.py:1126 inside shelve_book).

Disproof attempts all failed: no book-controlled string reaches `| safe`
besides sanitizer output; placeholder forgery neutralized; SS-06 iBooks header
guard is a real byte-identical `==` assertion; no stubs.

Verdict: CLEAN — zero gaps, zero unverified criteria. clean_streak → 3.
**CONVERGED.**
