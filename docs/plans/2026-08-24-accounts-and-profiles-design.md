---
date: 2026-08-24
topic: "Accounts + profiles (multi-tenant login, no-JS, old-iPad compatible)"
author: Caleb Bennett
status: draft
tags: [design, accounts, auth]
---

# Accounts & Profiles — Design

## Summary
Add a first real authentication layer to RetroShelf: multiple **accounts**
(households) on one instance, each with a login and a set of **profiles**
(family members, Netflix-style — no per-profile password). Reading state
(positions, bookmarks, reading list, history) becomes per-profile. Fully
no-JavaScript (plain POST forms + a signed session cookie), **zero new
dependencies** (stdlib `hashlib` password hashing, existing HMAC signing for
sessions). Opt-in: instances that don't enable accounts behave exactly as today.

## Decisions (confirmed with the operator)
- **Multi-account**: many isolated accounts per instance; each has its own
  login and profiles.
- **First-run admin; login replaces the access key**: a one-time setup creates
  the first (admin) account; the login page is then the gate. Admin creates
  further accounts. No open self-registration.
- **No per-profile PIN**: log into the account, then pick a profile.
- **Opt-in**: gated by an env flag, so today's zero-config public-library
  deployment is untouched.

## Opt-in & modes
- Env flag `ACCOUNTS_ENABLED` (default off).
  - **Off** → exactly today's behavior: no login, reading state is global,
    the optional `BRIDGE_ACCESS_KEY`/`ALLOWED_IPS` gate is unchanged.
  - **On** → the account system activates. If no accounts exist, every gated
    route redirects to `/setup` (create the admin). Once ≥1 account exists,
    gated routes require a valid session with a selected profile, else
    redirect to `/login`.
- When accounts are enabled, `BRIDGE_ACCESS_KEY` is superseded by the login
  (the login is the gate). `ALLOWED_IPS` still applies as an outer network
  filter if set.

## Data model (in the JSON Store under /config)
- `accounts`: `{account_id: {username, pw_hash, pw_salt, kdf, iterations,
  is_admin, token_version, created, profiles: {profile_id: {name, color,
  created}}}}`.
- Reading state moves under profile: `positions`, `bookmarks`, `favorites`,
  `history` become `{profile_id: <existing-shape>}`. (Comfort prefs stay
  cookie-based — they are per-device, not per-profile.)
- **Migration**: when the admin account + its first profile are created,
  adopt any pre-existing global reading state into that profile, so the
  operator's own history/positions/bookmarks/list carry over. Accounts-off
  data is left intact for accounts-off mode.

## Auth mechanics (stdlib only)
- **Passwords**: `hashlib.pbkdf2_hmac("sha256", password, salt, iterations)`
  with a fresh 16-byte `os.urandom` salt and a high iteration count
  (~600k). Stored as salt + iterations + hash so params can evolve. Verify
  with `hmac.compare_digest` (constant-time). Passwords are never stored in
  plaintext, never logged (login is POST-only, so they never enter a URL or
  the access log), and never returned in any response.
- **Sessions**: a stateless, HMAC-signed cookie (reuse the `IdCodec`
  signing / app secret, stable across restarts via `BRIDGE_ID_SECRET`)
  carrying `account_id`, `profile_id`, `token_version`, and an absolute
  `expiry`. Tamper → reject; expired → reject; `token_version` mismatch →
  reject (bumping it on password change / "log out" invalidates old
  sessions). HttpOnly + SameSite=Lax + Path=/.
- **No user enumeration**: `/login` returns one uniform error for unknown
  user or wrong password, and runs pbkdf2 against a dummy hash for unknown
  users so timing does not distinguish them.

## Pages / routes (no-JS; mutations are POST with a CSRF token)
- `GET/POST /setup` — first-run: create the admin account (only reachable
  when accounts enabled AND none exist). POST → create → session → `/profiles`.
- `GET/POST /login` — username + password form. POST → verify → session →
  `/profiles`.
- `POST /logout` — clear the session cookie (and optionally bump
  `token_version`); → `/login`.
- `GET/POST /profiles` — list the account's profiles + "add profile";
  selecting one re-mints the session with `profile_id` → `/`.
- `GET/POST /account` — the signed-in account's management: add/rename/delete
  profile, change own password; **admin only**: create another account. Kept
  minimal for v1.
- Middleware: when accounts enabled, gate every non-open path (open set:
  `/health`, `/static`, `/login`, `/setup`) on a valid session+profile.

## Threading profile through the reader
- Store state methods (`set_position`/`get_position`/`reading_list`/
  `bookmarks`/`add_bookmark`/`remove_bookmark`/`favorites`/`add_favorite`/
  `record_download`/`downloaded_keys`/…) take a `profile_id` (or are reached
  through a per-profile view). Routes read the current `profile_id` from the
  session and pass it. Accounts-off mode uses a fixed sentinel profile id so
  the same code path serves both modes.

## Security (the highest-stakes surface in the app — adversarially verified)
- Salted pbkdf2 password hashing; constant-time verify; no plaintext/logging.
- Signed session cookie: tamper-rejecting, expiring, `token_version`-revocable,
  HttpOnly + SameSite=Lax.
- **CSRF**: every mutating POST (setup/login/logout/profile-switch/account) has
  a hidden CSRF token validated server-side, in addition to SameSite=Lax.
- **Cross-account isolation**: the session binds `account_id`; a profile switch
  is only ever among the session account's own profiles; one account can never
  read or switch into another's profiles/state. (Adversarial test.)
- No user enumeration (uniform error + uniform timing).
- Admin-only guards on account creation.

## Acceptance criteria
1. `ACCOUNTS_ENABLED` off → byte-identical to today (no login; global state;
   all existing tests green).
2. Enabled + no accounts → `/setup` creates the admin, migrates existing
   global reading state into the admin's first profile.
3. Login: correct creds → session + profile picker; wrong creds → one uniform
   error, no enumeration; passwords never logged/returned; pbkdf2 salted +
   constant-time.
4. Session cookie is HMAC-signed: a tampered/expired/wrong-token_version cookie
   is rejected and redirects to login.
5. Two profiles have independent positions/bookmarks/reading-lists/history;
   switching profiles switches the reading state.
6. Two accounts are fully isolated: account A cannot see or switch into
   account B's profiles or state (adversarial test).
7. Every mutating POST requires a valid CSRF token; a missing/wrong token is
   refused.
8. Admin can create another account + profiles; a non-admin cannot.
9. Logout clears the session; the cookie no longer authenticates.
10. No JavaScript, no CSS grid/flex, plain forms/links; Python 3.12 container
    imports; mypy 0; ruff clean; all prior tests still pass.

## Exclusions (v1 — deferred, noted)
- Per-profile PIN; password-reset/email; rate-limiting/lockout on brute force
  (LAN tool — noted, not built); avatars/theming per profile; "log out
  everywhere" UI (the `token_version` mechanism exists, no dedicated page);
  fancy admin dashboard. Comfort prefs stay per-device (cookies).

## Approaches considered
- Server-side session store vs stateless signed cookie → **stateless signed
  cookie** (no DB, survives restart via the stable secret, trivially scales;
  revocation via `token_version`).
- bcrypt/argon2 dependency vs stdlib pbkdf2 → **stdlib pbkdf2** (no new dep,
  portable on the old-ARM 3.12 container; params stored for future upgrade).
- Accounts replace vs layer on the access key → **replace when enabled**,
  opt-in, so the default deployment is unchanged.
