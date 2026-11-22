# RetroShelf Roadmap

Status of every design in `docs/plans/`, checked against the code and git log
(last audit: 2026-11-21). Keep this file current when an item lands.

## Hard constraint: old iPads (owner, 2026-11-21)
RetroShelf must keep working on iOS 5.1.1-12 Safari (see the README). Every
feature is server-rendered HTML forms and links. That rules out:
- `<script>`, `on*=` attributes, `javascript:` URLs, fetch/XHR, `<svg>` and
  `<canvas>`;
- CSS grid, custom properties, modern flexbox (`-webkit-box` only),
  `position: sticky`, `:is`/`:has`/`:where`, `gap`, `object-fit` and
  `vw`/`vh`;
- WebP, AVIF, SVG or JS assets.

`tests/test_old_ipad_guard.py` scans every template and stylesheet, the
static assets, and the HTML served by the browse, shelf, search, OPDS,
reader, setup/login and signed-in account pages, and fails on any of these.
A deferred feature that cannot be done well without JS is marked blocked
rather than compromised.

## Done

### Base app (`docs/plans/2026-06-07-retroshelf-design.md`)
- [x] OPDS bridge, downloads with iBooks-safe headers, covers, search
  (`app/main.py`, `app/opds.py`, `app/download.py`, `docs/notes/header-expectations.md`).

### iPad QoL pack (`docs/plans/2026-06-08-ipad-qol-pack-design.md`)
- [x] SS1 home-screen web app: meta tags in `app/templates/base.html`,
  `app/static/icons/apple-touch-icon-180.png`, `tools/make_icon.py`,
  `tests/test_webapp_meta.py`.
- [x] SS2 cover transcoding + disk cache (Pillow) in `app/download.py`;
  `COVER_MAX_EDGE` / `COVER_JPEG_QUALITY` in `app/config.py`; `tests/test_covers.py`.
  The "cover cache eviction" open question is closed: `MAX_COVER_CACHE_BYTES`
  + `_prune_cover_cache`.
- [x] SS4 current-page sort: `_sorted_page` in `app/main.py`, `?sort=` carried by the pager.
- [x] SS6 polish: `autocorrect="off"` on search inputs, `/static` `Cache-Control`,
  HTML-only gzip middleware (`app/main.py`), `body.big` rules in `app/static/app.css`.
- Note: the plan's "Next Steps" checkboxes were never ticked, but the work landed.

### In-browser EPUB reader (`docs/plans/2026-08-23-epub-reader-design.md`)
- [x] `app/reader.py`, `/read/...` routes in `app/main.py`, per-profile positions,
  `tests/test_reader_*.py`. Converged in 6 passes with no residual gaps
  (`docs/converge/2026-08-23-epub-reader/converge-report.md`).
- [x] Built beyond the plan (originally excluded): bookmarks, in-book find, TOC
  page, CBZ comics, PDF reading, typography prefs, series row.

### Accounts & profiles (`docs/plans/2026-08-24-accounts-and-profiles-design.md`)
- [x] All 10 acceptance criteria: `app/accounts.py` (pbkdf2, signed session,
  CSRF), accounts/profiles in `app/store.py`, `/setup` `/login` `/logout`
  `/profiles` `/account` in `app/main.py`; tests in `tests/test_accounts.py`,
  `tests/test_auth_routes.py`, `tests/test_account_authz.py`,
  `tests/test_csrf_and_enumeration.py`, `tests/test_session_hardening.py`,
  `tests/test_gate.py`.
- [x] Two v1 exclusions were built later anyway: brute-force lockout
  (`app/throttle.py`, `tests/test_login_lockout.py`) and "sign out all devices"
  (`signout_all` action on `/account`).
- [x] Custom named per-profile shelves (`/shelves`, `tests/test_shelves.py`).

## Open

### Accounts: admin account management (gap found 2026-11-09)
The admin can *create* accounts but cannot see, reset, or remove them. Email
reset is excluded by design, so a user who forgets their password is locked out
for good. The fix that suits a LAN tool is admin-driven management on `/account`.
- [x] **A1. Admin lists accounts and resets another account's password** (done
  2026-11-09 on `wip/2026-11-09-next-plan`). `Store.list_accounts` in
  `app/store.py`; `reset_password` action on `POST /account` in `app/main.py`;
  "Other accounts" section in `app/templates/account.html`; tests in
  `tests/test_admin_reset.py`. The reset bumps `token_version`, so the target's
  sessions stop working, and it clears the target's per-username login tarpit.
  The admin cannot target itself (it uses "Change your password"). CSRF,
  password policy and admin-only checks apply; a non-admin who forges the
  request gets 403.
- [x] **A2. Admin deletes a non-admin account** (done 2026-11-09 on
  `wip/2026-11-09-next-plan`). `Store.delete_account` in `app/store.py` removes
  the account and every profile's state (positions/bookmarks/favorites/history/
  shelves). The `delete_account` action on `POST /account` is admin-only and
  needs a confirm checkbox; it refuses the admin's own account and any admin
  account. The deleted account's cookies stop working because the session
  lookup no longer finds it. Tests in `tests/test_admin_delete_account.py`.
- [x] **A3. Admin promotes/demotes admins** (done 2026-11-09 on
  `wip/2026-11-09-next-plan`). `Store.set_admin` in `app/store.py` refuses to
  demote the last admin. The `set_admin` action on `POST /account` is admin-only
  (403 otherwise) and CSRF-checked. "Make admin"/"Remove admin" buttons appear
  per account, and "Step down as admin" shows only when another admin exists.
  Tests in `tests/test_admin_promote.py`. To delete an admin account, demote it
  first.
- Review 2026-11-16: A1-A3 merged to `main` by fast-forward (8e8b39b, the
  `wip/2026-11-09-next-plan` branch ref is gone). A re-review of authz, CSRF,
  last-admin, self/admin delete, token_version revocation, deleted-account
  cookies and the tarpit clear found no bugs. Two route-level tests were added
  on `review/2026-11-16-admin-accounts`: a forged delete of another admin is
  refused, and a demoted admin's existing session loses admin rights at once
  (admin rights are read from the store on every request).
- [x] **A2 follow-up: delete clears the login tarpit** (done 2026-11-16 on
  `fix/tarpit-on-delete`). The `delete_account` action now clears the deleted
  username's per-username login tarpit, so an account re-created with the same
  name does not inherit the delay. It shares `_clear_login_tarpit` in
  `app/main.py` with the A1 reset (case-insensitive match, per-IP counter left
  alone). Test: `test_delete_clears_the_deleted_usernames_login_tarpit` in
  `tests/test_admin_delete_account.py`.
- [x] **Throttle wrong current passwords on "Change your password"** (done
  2026-11-16 on `fix/password-change-throttle`, branched from
  `fix/tarpit-on-delete`). Before this, anyone with a valid session cookie
  could guess the current password with no delay. The `password` action on
  `POST /account` now uses the `/login` `LoginThrottle` and shares its state:
  the per-IP hard lock (429, checked before hashing), the per-username tarpit
  (keyed on the account's username, case-insensitive), and on a correct
  current password `record_success` clears both, as a successful login does.
  Failures here slow `/login` for that username, and the reverse is also true.
  CSRF and the new-password checks still apply. Tests in
  `tests/test_password_change_throttle.py`.
- [x] **Cap over-long current passwords on "Change your password"** (done
  2026-11-16 on `fix/current-password-length-cap`). `/login` refuses a password
  over `_MAX_PASSWORD_LEN` (256) before hashing; the `password` action on
  `POST /account` now does the same for the current-password field. An
  over-long value is checked after the per-IP hard lock, never reaches the KDF,
  counts as a failure in the shared `LoginThrottle` (no tarpit sleep, as on
  `/login`), and gets the usual "Your current password is wrong." (400). Test:
  `test_over_long_current_password_is_capped_like_login` in
  `tests/test_password_change_throttle.py`.

### Small fixes (found 2026-11-21)
- [x] **Blank shelf rename reports an error** (done 2026-11-21 on
  `fix/shelf-rename-and-bad-ids`). `/shelves/rename/{id}` with a blank name
  used to redirect to `/shelves` as if it worked. `Store.rename_shelf` now
  raises the same `ValueError` as `create_shelf`, and the route redirects to
  `/shelves?err=Shelf name must not be empty.`, exactly as create does. Tests:
  `test_blank_rename_reports_an_error_like_create` in `tests/test_shelves.py`.
- [x] **Book ids that are not a book record get "Malformed book id"** (done
  2026-11-21, same branch). A signed id whose JSON is not an object, or has no
  non-empty `u` URL, raised `KeyError`/`TypeError` (500) in `/star/{bid}`,
  `/shelf/{id}/add/{bid}` and `/book/{bid}`. All three now use
  `_decode_book_record` in `app/main.py`, which also requires `u`, so they
  return the usual 404 "That link is not valid or has expired." (the reader
  routes share it). Ids are MAC-signed, so this is hardening. Tests:
  `tests/test_bad_book_ids.py`.
- [x] **Upstream robustness: feeds, downloads, covers** (done 2026-11-21, same
  branch; found by a bug hunt of the OPDS/download/Kavita code). Tests in
  `tests/test_upstream_robustness.py`, each failing on the old code:
  - `OpdsParseError` (`app/opds.py`) now subclasses `RetroShelfError`. A feed
    that answers 200 with a non-Atom body (maintenance or login page) gave a
    500 on `/feed` and `/search` and broke `/random`; it is now the 502
    "Library unavailable" page, and one bad library in a fan-out search is an
    error group.
  - A next/previous link that fails the SSRF guard drops that pager link
    instead of failing the whole `/feed` page with a 400.
  - `KavitaClient.open_stream` sends `Accept-Encoding: identity`. Downloads are
    relayed raw, so a gzip'd upstream body used to reach iBooks as a corrupt
    file.
  - `/download` takes the type from the record's MIME when it is EPUB or PDF,
    and otherwise looks for `.pdf` in the URL path and query only, not the
    host (`_media_for_url` in `app/main.py`).
  - A cover stream that dies mid-body is a `KavitaError` (the tiny 404 GIF),
    not a 500 HTML page in an `<img>`.
  - The 64M decoded-pixel cap is enforced at the cap for covers and reader
    images. Pillow only raises above twice `MAX_IMAGE_PIXELS`.
  - `/opds/reading-list` no longer publishes an HTML/text/CBZ book as a
    `.epub` acquisition. Only EPUB/PDF records get one, and an unknown MIME
    still counts as EPUB, as on `/book`.
  - Search results resolve relative links against the search URL, not the
    library root.
- [x] **Reader robustness** (done 2026-11-21, same branch; found by a bug hunt
  of `app/reader.py`). Tests in `tests/test_reader_robustness.py`, each
  failing on the old code except one regression guard:
  - A damaged deflate stream (`zlib.error`/`EOFError`) in an EPUB chapter is
    a `ReaderError`, and a damaged CBZ page or EPUB image is skipped. An
    unsupported zip version (`NotImplementedError` from `ZipFile`) is a
    `ReaderError`. Each used to be a 500.
  - EPUB2/XHTML 1.1 chapters using HTML named entities (`&nbsp;`, `&mdash;`)
    parse. They used to render as escaped source text with no title.
    `_parse_xhtml` retries with the names rewritten as numeric references.
  - A chapter's declared encoding or BOM is honoured (ISO-8859-1, UTF-16),
    because the bytes go to the parser undecoded.
  - Loose text directly in `<body>`, after a top-level element, or between
    the inline children of a `<div>` is kept. It used to be dropped, and a
    body of bare text gave a zero-block chapter.
  - `_scrub_xml` removes U+FFFE/U+FFFF and lone surrogates, so one stray
    non-character no longer fails a whole HTML book.
  - In-book find snippets stay aligned when `lower()` changes a string's
    length (`İ`).
  - EPUB and PDF extraction run in a worker thread (`asyncio.to_thread`), as
    CBZ already did, so shelving a big book no longer stalls every request.
- [x] **HTML book image fetches are time-bounded** (done 2026-11-21 on
  `fix/upstream-and-reader-followups`). The shared client has no read
  timeout, so one stalled image hung the shelve and its per-book lock, and
  every later open of that book, forever. `_fetch_capped_image` in
  `app/reader.py` now runs under `IMAGE_FETCH_TIMEOUT`, which reuses
  `FEED_READ_TIMEOUT` (30 s, not configurable, like the feed timeout). A
  timeout drops that image only. Test: `tests/test_reader_followups.py`.
- [x] **An unsatisfiable Range is relayed as 416** (done 2026-11-21, same
  branch). An upstream 416 to a forwarded `Range` header (Safari's ranged
  PDF reads) became a 502 "Library unavailable" page. `open_stream` in
  `app/kavita.py` now returns a 416 when a range was sent, and
  `stream_download` in `app/download.py` relays it with its
  `Content-Range`. Without a `Range` header a 416 is still a failure. Test:
  `test_unsatisfiable_range_is_relayed_as_416` in
  `tests/test_upstream_robustness.py`.
- [x] **A feed with no HTTP charset is decoded by its XML declaration**
  (done 2026-11-21, same branch). `_decode_feed` in `app/kavita.py` used
  UTF-8 whenever `Content-Type` named no charset, and `opds.parse` then
  stripped the document's own `encoding=`, so ISO-8859-1 feeds showed
  garbled titles. The XML declaration's encoding is now used when the
  server sends none; a server charset still wins. Test:
  `test_feed_without_http_charset_uses_its_xml_encoding`.
- [x] **A `<feed>` without the Atom namespace parses** (done 2026-11-21, same
  branch). `opds.parse` accepted a namespace-less root but looked every
  child up Atom-qualified, so the page came out empty. Its un-namespaced
  elements are now moved into Atom first. Test:
  `test_feed_without_the_atom_namespace_still_parses_its_entries` in
  `tests/test_opds.py`.
- [x] **Relative links resolve against the post-redirect feed URL** (done
  2026-11-21, same branch). `fetch_feed` followed redirects but resolved
  the feed's relative links against the URL it started from, so a feed
  moved by a 302 linked to the wrong paths. The new
  `KavitaClient.fetch_feed_and_url` also returns the final URL.
  `_load_feed` caches it on `Feed.base_url`, and `/feed` (entries and pager),
  `/search` and `/random` resolve against it. Tests:
  `test_redirected_feed_resolves_relative_links_against_the_final_url`,
  `test_fetch_feed_and_url_reports_the_final_url`.
- [x] **Reader placeholders cannot be forged across an unwrapped element**
  (done 2026-11-21, same branch). Text pieces were neutralized one at a
  time, so `{<x/>IMG:0}` joined into a real-looking `{IMG:0}`, which was then
  substituted as visible link text. This was cosmetic only, with no
  injection. `_render_children` now re-neutralizes the text between tags of
  its joined output. Test:
  `test_placeholder_cannot_be_forged_across_an_unwrapped_element` in
  `tests/test_reader_followups.py`.
- [x] **Reader cache pruning spares in-progress builds** (done 2026-11-21,
  same branch). `prune_reader_cache` skipped only dot-names, so under disk
  pressure it could delete a live `{key}.tmp-{pid}` build directory and fail
  that open. A `.tmp-` directory younger than `_BUILD_DIR_GRACE` (1 h) is now
  left alone; an older one, left over from a crash, is still pruned. Test:
  `test_prune_spares_an_in_progress_build_but_not_a_stale_one`.

### Decided by agent (2026-11-21, owner may revisit)
Owner standing order 2026-11-21: take the conservative option, behind a
setting where that fits, without touching existing data.

- [x] **Duplicate shelf names are refused** (on `feat/owner-decisions-batch1`).
  Decided by agent 2026-11-21 (owner may revisit): refuse a shelf name that
  matches another of the profile's shelves, ignoring case, on create and
  rename. Two shelves both called "To Read" can't be told apart on the
  book page. Shelves that already share a name are left alone, and
  re-casing a shelf's own name is allowed. `_refuse_duplicate_shelf_name` in
  `app/store.py`; tests `test_duplicate_shelf_names_*` in
  `tests/test_shelves.py`.

- [x] **Books with obfuscated fonts are readable** (same branch). Decided by
  agent 2026-11-21 (owner may revisit): any `META-INF/encryption.xml` used to
  be treated as DRM. Now a book whose `encryption.xml` lists only IDPF or
  Adobe font obfuscation on font files opens, because the reader never
  serves fonts. Real DRM, unknown algorithms, obfuscation on a non-font,
  and unparsable files are refused as before. `_only_font_obfuscation` in
  `app/reader.py`; tests `test_font_obfuscation_only_is_readable`,
  `test_real_drm_is_still_refused`.

- [x] **CBZ: macOS junk ignored, truncation shown** (same branch). Decided by
  agent 2026-11-21 (owner may revisit): `__MACOSX/` entries and AppleDouble
  `._name` files are never treated as pages, so they no longer use up
  `MAX_CBZ_PAGES` (800). A comic cut at the cap carries a notice ("This
  comic has N pages; only the first 800 can be shown in the browser."). The
  cap itself is unchanged. Notices are a new additive `Manifest.notices`
  field (older manifests load with none), shown on the contents page and
  the book's first page. Tests in `tests/test_reader_followups.py` and
  `test_truncation_notice_shows_on_contents_and_first_page_only` in
  `tests/test_reader_cbz.py`.

- [x] **Downloads are recorded only after success** (same branch). Decided by
  agent 2026-11-21 (owner may revisit): `/download` used to add a book to
  "recently sent" (and its "downloaded" tick) before contacting the library,
  so a failed download still showed as sent. It now records only when the
  library answers 2xx and the body starts streaming; a failure or a 416 is
  not recorded. A transfer cut off mid-body still counts, since the response
  has already started. Tests: `tests/test_download_records.py`.

- [x] **HEAD on `/download` and `/cover` asks the library** (same branch).
  Decided by agent 2026-11-21 (owner may revisit): HEAD used to answer 200
  without contacting the library. It now opens the same upstream GET (Kavita
  does not reliably answer HEAD) and closes it unread. `head_download`
  relays the real status, `Content-Length` and range headers, and a missing
  book is a 502. `head_cover` is 200 when the cover is cached or the library
  has it, else the tiny 404. A HEAD is never recorded as a download. Tests:
  `tests/test_head_proxy.py`.

- [x] **`linear="no"` spine items are out of the main flow** (on
  `feat/owner-decisions-batch2`). Decided by agent 2026-11-21 (owner may
  revisit): an EPUB spine item marked `linear="no"` (answer keys, pop-up
  notes) keeps its place and index, so saved positions and bookmarks are
  unaffected. Prev/Next and "start reading" skip it, while the contents
  page, in-book links and direct URLs still reach it. A book whose every
  item is non-linear opens at the first non-empty one. Additive
  `ChapterMeta.linear` (older manifests load as linear);
  `in_main_flow`/`first_readable_chapter` in `app/reader.py`. Tests:
  `tests/test_reader_nonlinear.py`.

- [x] **Dropped content carries a notice** (same branch). Decided by agent
  2026-11-21 (owner may revisit): shelving used to leave content out
  silently. It now adds a `Manifest.notices` line, shown on the contents
  page and first page, for EPUB or HTML-book images that could not be
  shelved (unreadable, undecodable, over the pixel cap, or past
  `MAX_IMAGES`), comic pages that could not be decoded, and EPUB chapters
  nested past `MAX_NESTING_DEPTH`. The caps themselves are unchanged. Tests:
  `tests/test_reader_dropped_notices.py`.

- [x] **Cover cache: least-recently-used, settings-aware** (same branch).
  Decided by agent 2026-11-21 (owner may revisit):
  - A cache hit refreshes the cover's mtime, so `_prune_cover_cache` evicts
    the least recently used cover, not the oldest written. A cover and its
    `.ct` sidecar are counted and removed together.
  - The cache key includes `COVER_MAX_EDGE`/`COVER_JPEG_QUALITY`, so
    changing either stops serving covers sized for the old value. Stale
    entries age out by LRU.
  - The default settings (320/80) keep the original `sha256(url)` key, so
    existing caches stay valid on upgrade.
  - Tests are the last three in `tests/test_covers.py`.

- [x] **Search results page** (same branch). Decided by agent 2026-11-21
  (owner may revisit): search used to show only the library's first page of
  results. Each results group now carries the library's own
  `rel="next"`/`"previous"` links as "More results"/"Earlier results".
  Under a fan-out search it reads "More from <library>". The links continue
  a one-library search through a signed, SSRF-checked `page=` id. A forged
  id, or a page from another library, restarts at page one. Sorting is
  unchanged (current page only). Tests: `tests/test_search_paging.py`.

### Deferred by design (not scheduled)
- Per-profile PIN; email password reset; per-profile avatars/theming; admin
  dashboard (accounts plan, "Exclusions").
- [x] **Per-device reading positions** (built 2026-11-21 on
  `feat/deferred-features`; EPUB reader plan, "Exclusions"). Opt-in
  `PER_DEVICE_POSITIONS=1` (default off; nothing changes otherwise). Each
  browser gets a random `rs_device` cookie: plain cookie, no JS. The
  profile-wide position is still kept, so "Continue reading" works, and a
  device that has not read a book yet resumes there. A device that has
  resumes at its own place. Stored as a `device_reading` section, written
  only once used and capped at 20 devices per profile. Tests:
  `tests/test_per_device_positions.py`.
- EPUB CSS passthrough; EPUB3 media/fixed-layout (EPUB reader plan,
  "Exclusions").
- [x] **Reader image max-edge setting** (built 2026-11-21 on
  `feat/deferred-features`; EPUB reader plan, open question 2).
  `READER_IMAGE_MAX_EDGE` env var (256-4096, default 1024, so nothing changes
  unless set) in `app/config.py`, applied by `create_app`. Each manifest
  records the edge its images were shelved at (`image_max_edge`; older ones
  read as 1024). A book with images shelved at another edge is re-shelved
  on its next open; chapter numbering, positions and bookmarks are
  unaffected. Tests: `tests/test_reader_image_edge.py`.
