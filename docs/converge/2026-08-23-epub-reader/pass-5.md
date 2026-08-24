# Converge pass 5 — standard (constraints focus)

Emphasis on the `## Constraints` Musts/Must-Nots, verified in code (not just
tests): every upstream byte through `resolve_url`; sanitize at shelve time;
single commented `| safe` seam; atomic temp+rename writes; no upstream URL on
disk; NO new runtime deps (requirements.txt unchanged); download/cover/OPDS
headers unchanged (SS-06 byte-identical assertion); no JS / no grid-flex.

Verdict: CLEAN — all Met, every Must/Must-Not honored. clean_streak → 2.
