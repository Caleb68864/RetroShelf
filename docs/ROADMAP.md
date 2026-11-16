# RetroShelf Roadmap

Status of every design in `docs/plans/`, checked against the code and git log
(last audit: 2026-11-16). Keep this file current when an item lands.

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

### Deferred by design (not scheduled)
- Per-profile PIN; email password reset; per-profile avatars/theming; admin
  dashboard (accounts plan, "Exclusions").
- Per-device reading positions; EPUB CSS passthrough; EPUB3 media/fixed-layout
  (EPUB reader plan, "Exclusions").
- Reader image max-edge config knob (`READER_IMAGE_MAX_EDGE` in `app/reader.py`
  is fixed at 1024): EPUB reader plan, open question 2.
