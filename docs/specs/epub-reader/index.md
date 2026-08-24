---
type: phase-spec-index
master_spec: "docs/specs/2026-08-23-epub-reader.md"
date: 2026-08-23
sub_specs: 6
---

# In-Browser EPUB Reader — Phase Specs

Refined from [2026-08-23-epub-reader.md](../2026-08-23-epub-reader.md).

| Sub-Spec | Title | Dependencies | Phase Spec |
|----------|-------|--------------|------------|
| 1 | Sanitizer and block splitter | none | [sub-spec-1-sanitizer-and-block-splitter.md](sub-spec-1-sanitizer-and-block-splitter.md) |
| 2 | EPUB parsing and shelving | SS-01 | [sub-spec-2-epub-parsing-and-shelving.md](sub-spec-2-epub-parsing-and-shelving.md) |
| 3 | Pagination, progress, Store positions | SS-02 | [sub-spec-3-pagination-progress-and-store-positions.md](sub-spec-3-pagination-progress-and-store-positions.md) |
| 4 | Reader routes and templates | SS-03 | [sub-spec-4-reader-routes-and-templates.md](sub-spec-4-reader-routes-and-templates.md) |
| 5 | UI integration and reader themes | SS-04 | [sub-spec-5-ui-integration-and-reader-themes.md](sub-spec-5-ui-integration-and-reader-themes.md) |
| 6 | End-to-end integration, docs, evidence (integration) | SS-04, SS-05 | [sub-spec-6-end-to-end-integration-docs-and-evidence.md](sub-spec-6-end-to-end-integration-docs-and-evidence.md) |

## Requirement Traceability Matrix

| Requirement | Covered By |
|-------------|-----------|
| R1: Read here / Continue reading button | Sub-spec 5 |
| R2: Shelve once, serve from cache | Sub-specs 2, 4 |
| R3: Chapter-scroll + rs_split parts | Sub-specs 3, 4 |
| R4: Position recording + resume + finished | Sub-specs 3, 4 |
| R5: Currently Reading shelf | Sub-spec 5 |
| R6: TOC page | Sub-spec 4 |
| R7: Sanitizer allowlist | Sub-spec 1 |
| R8: Resource caps | Sub-spec 2 |
| R9: Friendly failures / degradation | Sub-specs 2, 4 |
| R10: Themes via /prefs | Sub-specs 4, 5 |
| R11: Images from shelved cache | Sub-specs 2, 4 |
| R12: No JS / no grid / plain links | Sub-specs 4, 5 (verified 6) |
| R13: mypy/ruff/suite green | All (verified 6) |

No orphaned requirements.

## Execution

Run `/forge-run docs/specs/2026-08-23-epub-reader.md` to execute all phase specs
(point at the master spec file — forge-run auto-detects linked phase specs).
