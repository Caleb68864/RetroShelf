---
type: redteam-report
generated: 2026-08-23
findings_count: 9
---

# Red Team Review: 2026-08-23-epub-reader.md

Target: master spec (6 sub-specs, 32 typed acceptance criteria after patching).
All 9 roles ran; construction-site check ran. Entities verified against the
codebase before flagging (`_read_capped`, `_ERROR_TABLE`, `sanitize_record`,
`_record_id`, `_safe_image_type` all exist as referenced).

## CRITICAL Findings (0)

None. The sanitizer seam, SSRF path, cache bounds, and interface contracts are
specified concretely; no finding rose to will-cause-agent-failure.

## ADVISORY Findings (9) — all patched into the spec

- **A-1: Fragment-only hrefs unspecified** (Developer) — `#anchor` links can
  cross part boundaries. Patched: committed default — unwrap to text (SS-01).
- **A-2: `| safe` grep AC brittle to spelling** (QA) — Patched: spelling
  convention pinned in SS-04 Decisions.
- **A-3: No negative test for access gating on reader routes** (QA) —
  Patched: [BEHAVIORAL] 403 AC added to SS-04.
- **A-4: Zero-readable-chapter EPUB unspecified** (End User) — Patched:
  `ReaderError` ("no readable content") in SS-02 scope, AC, and Edge Cases.
- **A-5: Slow first shelve reads as a hang on old Safari** (End User) —
  Patched: "(first open takes a moment)" hint in SS-05.
- **A-6: Shelve spool inherits `read=None` → indefinite hang risk** (SRE) —
  Patched: bounded 120s per-request timeout in SS-02, mirroring the
  `fetch_feed` override pattern.
- **A-7: No shelving observability** (SRE) — Patched: one masked INFO line
  per shelve (book_key, chapters, bytes, ms) in SS-02.
- **A-8: Image index type not pinned** (Security) — Patched: `n: int` path
  converter committed in SS-04 Decisions.
- **A-9: SS-02 is the heavyweight sub-spec** (Scope Realist, YELLOW) — no
  spec change; directive for `/forge-prep` to expand SS-02 with the most
  granular implementation steps and verify commands.

## Construction-Site Check

Clean. Wiring sub-specs name concrete call sites (`_register_routes` in
`app/main.py`; template files enumerated). No `construction-site-without-caller`.

## Role Scorecards

Developer: 1 | QA: 2 | End User: 2 | Architect: 0 | Scope Realist: 1 |
Security: 1 | SRE: 2 | Data: 0 | Product: 0

## Operator Notes (outside the spec)

- The working tree currently carries uncommitted changes (docs/plans, docs/specs,
  and a docstring-audit agent editing `app/`). Commit before `/forge-run` so
  workers verify against the intended baseline.
