# Plan: QR / deep-link routing on mobile (#27)

Spec: `docs/superpowers/specs/2026-07-05-qr-deeplink-routing-design.md`
Issue: #27 (INVESTIGATE). Tasks 2+ are **conditional** on Task 1's findings —
do not start them until the matrix is filled in and attached to the issue.

House rules: draft PRs (`gh pr create --draft`), CodeRabbit before merge, TDD for all
code tasks, env-guard preamble **verbatim from `tests/test_seasons.py:1-18`** in any
new test file importing lfg_core. Coordinate any `xumm_ops.py` edit with tx-hygiene
PR-1 (origin/main spec, site 7) — same file, overlapping fix.

---

## Task 1 — Reproduction matrix on real devices (HUMAN GATE — blocks everything)

Owner: user (needs physical iPhone + Android + Xaman installed). Claude prepares the
test payloads; the human scans.

1. Prep (Claude, testnet): trigger one Activity mint payment QR, one Discord bot
   trustline QR (XUMM-hosted qr_png), one CLI extract accept link, and one forced
   detect-link fallback (temporarily break XUMM creds in a scratch env — do NOT
   commit).
2. Human executes the grid from spec §4 (device × scanner/action × QR-or-link
   content × surface), recording per cell: app that opens, tap count, payload
   received in Xaman y/n, post-sign return y/n, screenshots of any intermediary
   page. **Include the mobile-display tap rows** (no scan): mobile Discord bot
   embed link (render.py:24, 48), mobile Discord Activity `openExternal`
   (app.js:97-100), and mobile Telegram caption link (swap_view.py:247) — these
   probe universal-link handoff from inside the Discord/Telegram in-app browser
   (root-cause candidate #2), same pass criteria as scan cells.
3. Post the filled matrix as a comment on #27.
4. **Decision gate:** map each failing cell to a fix (F1–F6) or to "cannot reproduce —
   report predates spine refactor". If nothing reproduces → skip to Task 6.

## Task 2 — F1: retire the user-facing detect-link fallback (conditional)

**Cross-spec note (reconciled disposition — see spec §5.1):** this spec OWNS the
end state of the detect-link path: **retired**. tx-hygiene PR-1's
add-SourceTag-to-`generate_static_payment_link` fix (origin/main tx-hygiene spec
§2.1) is the **interim hotfix** — let it land first if ready first (live mainnet
leak). This task then deletes the tagged fallback and retires the detect-hex
SourceTag assertion tx-hygiene §2.3 adds to `tests/test_xumm_source_tag.py`. If
this task lands first, tx-hygiene PR-1 drops its detect-link item. The tx-hygiene
plan on main needs a matching one-line "interim, superseded by #27/F1" note
(coordinator to amend on main).

1. RED: tests asserting `ensure_payment_fallback` (`lfg_core/mint_flow.py:127-135`)
   and the swap fallback (`lfg_core/swap_flow.py:306-312`) yield an error/retry state
   (no `xaman.app/detect` URL) when the XUMM payload is None. Env-guard preamble.
2. GREEN: replace fallback link with explicit failed-payment state + regen affordance
   (mint already has `regenerate_payment`, mint_flow.py:118-124 — reuse).
3. Surfaces: verify Discord/Telegram/Activity render the error state sanely (existing
   `friendly_error` paths).
4. Draft PR → CodeRabbit.

## Task 3 — F3: trustline payload consolidation + return_url (conditional/cleanup)

1. RED: test that the trustline payload goes through `xumm_ops._create_xumm_payload`
   (SourceTag + return_url handling for free) instead of the hand-rolled POST at
   `surfaces/discord_bot/trustline.py:31-75`.
2. GREEN: refactor trustline.py onto `xumm_ops`; decide return_url (drop the web-only
   letseffinggo.com or pass a Discord return_url when channel ctx is available).
3. Draft PR → CodeRabbit.

## Task 4 — F5: surface `pushed` for paired users (recommended regardless of matrix)

**Contract note:** `pushed` is an ADDITIVE optional key on the
`{qr_url, xumm_url, uuid}` return dict (default absent/False) — the existing three
keys are untouched. All consumers are dict-key access, no positional/tuple
unpacking anywhere (verified: mint_flow.py:115-116, 350-352; swap_flow.py:283-284,
307; app.py:964; scripts/economy_extract.py:38, economy_assemble.py:60; test fakes
in webapp/test_smoke.py:189, 213, 733, 974-986 and
tests/test_service_signin_platform.py:35 return plain dicts). Surfacing sites:
mint/swap session dicts → Discord/Telegram captions + Activity panel. Ignoring
sites: signin (app.py:964), trustline (until Task 3), CLI scripts.

1. RED: unit test that `_create_xumm_payload` returns `pushed: bool` from the API
   response (stub requests), and that `MintSession.to_dict` /swap session carry it.
   **Plus a compatibility test: the existing three-key consumers pass unchanged**
   — run the existing xumm/mint/swap suites against the four-key return and
   assert no assertion anywhere depends on key-set equality.
2. GREEN: add `"pushed": bool(data.get("pushed"))` in `lfg_core/xumm_ops.py:158-162`;
   thread through `mint_flow`/`swap_flow` session dicts.
3. Surfaces: when `pushed`, Discord/Telegram captions and Activity flow panel say
   "Request sent to your Xaman app" (QR remains as backup). Activity change in
   `webapp/client/app.js` flow renderers.
4. Draft PR → CodeRabbit. Manual verify on testnet with a paired account.

## Task 5 — F6: scan-guidance copy (zero-risk, bundle with Task 4 PR)

- Update `surfaces/discord_bot/render.py:22,46`, telegram render captions, and the
  Activity QR labels to say "scan with your phone's camera app or Xaman's built-in
  scanner" (steers users off in-app browsers). Snapshot-style copy tests only.

## Task 6 — Close out #27 (always)

1. Comment on #27: filled matrix, root-cause statement (or "not reproducible on
   current architecture — report predates spine refactor"), and the explicit
   UA-question answer: **not applicable — LFG hosts no redirect/callback page in the
   scan-to-sign path** (spec §6), so UA-aware redirect logic has nowhere to live and
   adding a shim would create the intermediary hop the issue complains about.
2. Link spec + plan permalinks (blob URLs at commit SHA) per repo CLAUDE.md workflow.
3. Check acceptance boxes; close, or split surviving fix tasks into follow-up issues.
