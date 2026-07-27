# Accounts RBAC + Signup Test Coverage — Analysis + Quality Plan

> Companion to endpoint inventory in `futureagi/accounts/urls.py`. This doc is the analysis — strengths, weaknesses, patterns, and the plan to get every endpoint to strong coverage.
>
> **How to use this doc:** Skim the executive summary → read the group you care about → open the paths cited in your editor to read the actual tests → decide what to prioritize. Every claim is grep-verifiable.
>
> **Paths below are relative to the `futureagi/` (backend) repo root.** Every test file cited can be opened locally from the repo you have checked out.

---

## Rating scale

Every endpoint is rated on 5 dimensions:

- **HAPPY PATH** — does a test assert the endpoint returns the right shape for the right input?
- **ERROR PATHS** — 400/404 on bad input, missing resources, invalid state
- **AUTH** — anonymous → 401/403
- **CROSS-TENANT** — User A can't access User B's org/workspace data
- **CONTRACT** — swagger request+response schemas exist, debt-gate held

From these, an overall bucket:

| Rating       | Meaning                                                              |
| ------------ | -------------------------------------------------------------------- |
| **STRONG**   | 4–5 dimensions covered; typically ≥5 substantive tests               |
| **ADEQUATE** | 3 dimensions covered; either happy+auth+contract OR happy+error+auth |
| **THIN**     | 1–2 dimensions only; often just contract or just auth                |
| **NONE**     | 0 tests                                                              |

---

## Executive summary

**~55 endpoints across 14 feature groups. Total tests indexed: 1203 endpoint-touching tests across 45 files.**

**Distribution (approximate, based on hit counts + class coverage):**

| Bucket   | Endpoints | %   |
| -------- | --------: | --- |
| STRONG   |        22 | 40% |
| ADEQUATE |        15 | 27% |
| THIN     |        12 | 22% |
| NONE     |         6 | 11% |

**Themes:**

- **Gold-standard groups** (open these first, use as templates): **RBAC permission matrix** (`test_e2e_role_and_removal.py`, `test_comprehensive_login_rbac_e2e.py`, `test_rbac_comprehensive.py`), **Multi-org lifecycle** (`test_e2e_multi_org_lifecycle.py`), **Session security** (`test_session_security.py`), **Login error codes** (`test_login_error_codes.py`). These have multi-layer coverage (endpoint + permission boundary + cross-org + regression) and are what "quality" looks like.
- **Systemic weak points**: **Legacy team endpoints** (`/team/users/`, `/user/list/`, `/user/role/update/`, `/user/delete/`, `/user/deactivate/`, `/workspace/invite/`) have only auth + error tests — no happy-path functional assertions. **Activate account** (`/activate/<uidb64>/<token>/`) has **zero tests**. **Annotation notification unsubscribe/snooze** and **appsmith** and **aws-marketplace** have zero coverage. **2FA verify paths** (TOTP + recovery + passkey) are THIN.
- **Cross-tenant is strong on RBAC writes but weak on legacy reads**: The new RBAC endpoints (`organization/invite/`, `members/role/`, `members/remove/`) have systematic permission-matrix + cross-org tests. Legacy workspace/team endpoints do not.
- **Contract-debt gates are solid** on new serializers; legacy endpoints often lack them.
- **EE coupling risk**: `conftest.py` monkeypatches `ee_gating.check_ee_feature` for `CUSTOM_ROLES` so tests don't hit 402. Several files (`test_e2e_role_and_removal.py`, `test_config_endpoint.py`) patch `ee.usage.*`. Live-service calls (HubSpot in `test_post_registration.py`, SMTP) are mocked.
- **Redundancy**: Overlapping e2e files (`test_comprehensive_login_rbac_e2e.py`, `test_e2e_multi_org_lifecycle.py`, `test_org_e2e.py`, `test_e2e_role_and_removal.py`) test the same invite → accept → role-change → remove flows with different fixture styles. Legacy `test_team.py` duplicates coverage already present in `test_workspace_management.py`.

**Bottom line:** Core RBAC and multi-org flows are production-grade. Legacy endpoints and a handful of signup/2FA paths need concentrated work. The suite is green today; the risk is silent regression on thin paths and duplicate maintenance burden.

---

# Per-group analysis

Each group below has (a) coverage summary, (b) list of test files, (c) strengths, (d) weaknesses, (e) notable endpoints.

---

## 1. Login / Auth (token, refresh, error codes, session security) — 6 endpoints

**Coverage**: STRONG 5 · ADEQUATE 1 · THIN 0 · NONE 0

**Endpoints:**

1. `POST /accounts/token/` — STRONG
2. `POST /accounts/token/refresh/` — STRONG
3. `POST /accounts/redis-key/` — ADEQUATE
4. Login error-code paths (invalid creds, deactivated, blocked, rate-limit, IP-block) — STRONG
5. Logout invalidation + concurrent sessions — STRONG
6. Deactivated user / org-removal state — STRONG

**Test files:**

- `test_authentication.py`
- `test_user.py`
- `test_login_error_codes.py`
- `test_login_deactivated.py`
- `test_session_security.py`
- `test_login_error_codes_e2e.py` (scripts/)

**Strengths:**

- Error-code matrix is exhaustive (`TestInvalidCredentialsErrorCode`, `TestAccountBlockedErrorCode`, `TestTooManyAttemptsErrorCode`, middleware IP-block tests).
- Session security covers token invalidation on logout, password reset, org removal, role downgrade.
- `test_session_security.py` has 21 tests across 12 classes — clean regression coverage.
- Rate-limiting and IP-block middleware tested with real cache interaction.

**Weaknesses:**

- `redis-key/` only has auth + basic error; no happy-path assertion on key rotation.
- Refresh has recaptcha bypass only in DEBUG; no test for recaptcha failure path in non-DEBUG.

**Notable endpoints:**

- `POST /token/`: **STRONG** — 77+ hits across files; gold standard for error envelopes.
- `POST /token/refresh/`: **STRONG** — 11 tests including invalid token and recaptcha.

---

## 2. Signup / Registration (signup, activate, password reset, accept invitation) — 9 endpoints

**Coverage**: STRONG 4 · ADEQUATE 3 · THIN 1 · NONE 1

**Endpoints:**

1. `POST /accounts/signup/` — STRONG
2. `POST /accounts/logout/` — STRONG
3. `GET /accounts/activate/<uidb64>/<token>/` — NONE
4. `POST /accounts/password-reset-initiate/` — ADEQUATE
5. `POST /accounts/password-reset-confirm/<uidb64>/<token>/` — THIN
6. `POST /accounts/resend-invitation-emails/` — ADEQUATE
7. `POST /accounts/accept-invitation/<uidb64>/<token>/` — STRONG
8. `POST /accounts/delete-users/` — ADEQUATE
9. `POST /accounts/update-user/`, `update-user-full-name/`, `get-user-profile-details/` — ADEQUATE

**Test files:**

- `test_signup.py` (52 tests, 14 classes)
- `test_e2e_resend_reactivate.py`
- `test_comprehensive_login_rbac_e2e.py`

**Strengths:**

- Signup has happy path + duplicate email + validation errors + unknown-field rejection.
- Accept-invitation has full lifecycle (new user, existing user, password set, membership activation).
- `test_signup.py` has dedicated classes for email validation, response formats, account-takeover regression.

**Weaknesses:**

- **Activate account has zero tests** — the token flow is untested.
- Password-reset-confirm has only 3 hits; no happy-path token validation test.
- `delete-users/` and `resend-invitation-emails/` only have unknown-field + auth tests.

**Notable endpoints:**

- `POST /signup/`: **STRONG** — 14 tests.
- `POST /accept-invitation/`: **STRONG** — exercised in multiple e2e files.
- `GET /activate/`: **NONE** — critical gap.

---

## 3. RBAC Core — New endpoints (Phase 2) — 7 endpoints

**Coverage**: STRONG 7 · ADEQUATE 0 · THIN 0 · NONE 0

**Endpoints:**

1. `POST /accounts/organization/invite/` — STRONG
2. `POST /accounts/organization/invite/resend/` — STRONG
3. `POST /accounts/organization/invite/cancel/` — STRONG
4. `GET /accounts/organization/members/` — STRONG
5. `POST /accounts/organization/members/role/` — STRONG
6. `DELETE /accounts/organization/members/remove/` — STRONG
7. `POST /accounts/organization/members/reactivate/` — STRONG

**Test files:**

- `test_org_e2e.py` (67 tests)
- `test_e2e_multi_org_lifecycle.py` (40 tests)
- `test_comprehensive_login_rbac_e2e.py` (60 tests)
- `test_e2e_role_and_removal.py` (73 tests)
- `test_rbac_comprehensive.py` (24 tests)
- `test_member_removal.py`
- `test_e2e_invite_permissions.py`
- `test_e2e_resend_reactivate.py`

**Strengths:**

- **This is the current gold standard.** Permission matrix tests (`TestOwnerRoleUpdates`, `TestAdminRoleUpdates`, `TestMemberViewerRoleUpdates`) cover every actor→target→level combination with explicit ALLOW/DENY assertions.
- Cross-org isolation tested on every write (`test_user_cannot_access_another_orgs_resources`).
- Invite lifecycle (create → resend → cancel → expire → reactivate) covered end-to-end.
- `test_e2e_role_and_removal.py` has 73 tests across 8 classes — exhaustive.
- Dual-write legacy + new membership tables guarded by migration + invariant tests (`test_workspace_member_fk.py`).

**Weaknesses:**

- Minor: `reactivate/` has only 2 hits; happy-path is exercised indirectly.
- No explicit test for race between two admins promoting the same user.

**Notable endpoints:**

- All 7 endpoints are **STRONG**. Use `test_e2e_role_and_removal.py` and `test_org_e2e.py` as templates.

---

## 4. Workspace-scoped RBAC — 3 endpoints + legacy workspace endpoints

**Coverage**: STRONG 6 · ADEQUATE 4 · THIN 3 · NONE 0 (counting legacy)

**Endpoints (new):**

1. `GET /accounts/workspace/<uuid>/members/` — STRONG
2. `POST /accounts/workspace/<uuid>/members/role/` — STRONG
3. `DELETE /accounts/workspace/<uuid>/members/remove/` — STRONG

**Legacy (still mounted):**

- `GET/POST/PUT/DELETE /accounts/workspaces/<id>/` and members — ADEQUATE/THIN
- `GET/POST /accounts/workspace/list/`, `workspace/invite/`, `user/list/`, `user/role/update/`, `user/resend-invite/`, `user/delete/`, `user/deactivate/`, `workspace/switch/` — mostly THIN/ADEQUATE

**Test files:**

- `test_workspace.py` (65 tests)
- `test_workspace_management.py` (88 tests)
- `test_e2e_member_table_workspace.py` (50 tests)
- `test_workspace_member_fk.py` (30 tests)
- `test_invite_login_workspace_e2e.py` (30 tests)

**Strengths:**

- Workspace member list + role + remove have permission-matrix tests.
- FK invariant + migration backfill tests (`test_workspace_member_fk.py`).
- `test_e2e_member_table_workspace.py` has 50 tests across 9 classes covering status, filter, pagination, permissions, workspace creation.

**Weaknesses:**

- Legacy `test_team.py` (15 tests) only exercises auth + 404 on nonexistent; no happy-path list or role update.
- `workspace/invite/` legacy path is thin.
- Many legacy endpoints duplicate functionality now covered by new RBAC workspace endpoints.

**Notable endpoints:**

- New workspace member endpoints: **STRONG**.
- Legacy team/user endpoints: **THIN** — candidates for deprecation or minimal happy-path addition.

---

## 5. Multi-Organization (organizations/\*) — 7 endpoints

**Coverage**: STRONG 6 · ADEQUATE 1 · THIN 0 · NONE 0

**Endpoints:**

1. `GET /accounts/organizations/` — STRONG
2. `POST /accounts/organizations/switch/` — STRONG
3. `GET /accounts/organizations/current/` — STRONG
4. `POST /accounts/organizations/create/` — STRONG
5. `POST /accounts/organizations/new/` — STRONG
6. `PATCH /accounts/organizations/update/` — ADEQUATE
7. `POST /accounts/organizations/select/` (selection view) — STRONG

**Test files:**

- `test_organization.py` (34 tests)
- `test_multi_org_endpoints.py` (20 tests)
- `test_e2e_multi_org_lifecycle.py` (40 tests)
- `test_create_organization.py` (14 tests)
- `test_multi_org.py` (53 tests — model methods)

**Strengths:**

- Full lifecycle: create additional org, list, switch, cross-org isolation, role escalation, invite chain across orgs.
- `test_e2e_multi_org_lifecycle.py` has 15 classes covering owner-invites-chain, role-escalation, workspace-access, invite-expiry, bulk-invite, owner-demotion, removal-recovery.
- Model methods (`can_access_organization`, `get_membership`, `has_global_workspace_access`) unit-tested.

**Weaknesses:**

- `organizations/update/` only has 4 hits; no cross-tenant test.
- Organization creation after removal (`test_orgless_user_can_create_org`) is tested but not in every multi-org file.

**Notable endpoints:**

- `GET /organizations/`, `POST /switch/`, `POST /new/`: **STRONG** — 123+ hits combined.

---

## 6. 2FA / TOTP / Recovery / Passkeys — 18 endpoints

**Coverage**: STRONG 4 · ADEQUATE 8 · THIN 6 · NONE 0

**Endpoints:**

- `GET/POST/PUT/DELETE /accounts/2fa/status/`, `totp/setup/`, `totp/confirm/`, `totp/` (disable) — ADEQUATE/STRONG
- `POST /accounts/2fa/verify/totp/`, `verify/recovery/`, `verify/passkey/` — THIN
- Recovery codes list/regenerate — ADEQUATE
- Passkey register options/verify, list, detail (patch/delete), authenticate options/verify — STRONG on register/list, THIN on verify
- `GET/PUT /accounts/organization/2fa-policy/` — ADEQUATE

**Test files:**

- `test_totp.py`
- `test_recovery_codes.py`
- `test_passkeys.py` (36 tests, 11 classes)
- `test_2fa_login_flow.py`
- `test_2fa_enforcement.py`
- `test_passkeys.py` also covers 2FA status integration

**Strengths:**

- Passkey register + list + delete + rename + multiple-passkeys + cross-org isolation well tested.
- TOTP setup → confirm → disable flow covered.
- Org 2FA policy enforcement + grace period tested.
- `test_2fa_login_flow.py` covers login → challenge → verify (TOTP + recovery).

**Weaknesses:**

- **2FA verify endpoints during login are THIN** — only 2–4 hits each; no happy-path with real device after grace-period enforcement.
- Passkey authenticate verify has only 4 hits.
- No test for "user has both TOTP + passkey, verify either works".
- `organization/2fa-policy/` has no cross-tenant test.

**Notable endpoints:**

- Passkey CRUD: **STRONG**.
- 2FA verify paths: **THIN** — need happy-path + error + cross-org.

---

## 7. API Keys (keys + key actions) — 6 endpoints

**Coverage**: STRONG 4 · ADEQUATE 2 · THIN 0 · NONE 0

**Endpoints:**

1. `GET /accounts/keys/` — STRONG
2. `GET /accounts/key/get_secret_keys/` — STRONG
3. `POST /accounts/key/generate_secret_key/` — STRONG
4. `POST /accounts/key/enable_key/`, `disable_key/` — ADEQUATE
5. `DELETE /accounts/key/delete_secret_key/` — STRONG
6. Pagination/search/sort on secret keys — STRONG

**Test files:**

- `test_keys.py` (40 tests, 10 classes)

**Strengths:**

- Full CRUD + state transitions + permission (owner-only delete) + cross-org security (cannot enable other org's key).
- Pagination, search, sorting tested.
- Response format + unknown-field rejection covered.

**Weaknesses:**

- Enable/disable only have state-transition tests; no happy-path "key actually becomes usable" assertion.
- No test for key revocation publishing to collector.

**Notable endpoints:**

- All key endpoints are at least ADEQUATE; most STRONG.

---

## 8. Organization selection / current / config — 4 endpoints

**Coverage**: ADEQUATE 4

**Endpoints:**

1. `GET/POST /accounts/organizations/` (selection) — ADEQUATE
2. `GET /accounts/organizations/current/` — ADEQUATE
3. `GET /accounts/config/` — ADEQUATE
4. `GET /accounts/first-checks/` — ADEQUATE

**Test files:**

- `test_organization.py`
- `test_multi_org_endpoints.py`
- `test_config_endpoint.py`
- `test_user.py`
- `test_2fa_enforcement.py`

**Strengths:**

- Config has cloud/region/available_regions tests with EE deployment mocks.
- First-checks tested for onboarding state.

**Weaknesses:**

- Config tests rely on patching `ee.usage.deployment._validate_cloud_secret`.
- No test for region fallback when AVAILABLE_REGIONS empty in non-cloud.

**Notable endpoints:**

- All ADEQUATE — need one more dimension (usually cross-tenant or happy-path data assertion).

---

## 9. Annotation notifications (timezone, unsubscribe, snooze) — 3 endpoints

**Coverage**: THIN 1 · NONE 2

**Endpoints:**

1. `POST /accounts/me/timezone/` — THIN
2. `GET /accounts/notifications/unsubscribe/` — NONE
3. `GET /accounts/notifications/snooze/` — NONE

**Test files:**

- `test_annotation_notifications.py` (3 tests)

**Strengths:**

- Timezone has unknown-field + invalid-value + success.

**Weaknesses:**

- **Unsubscribe and snooze have zero tests** — token-signed one-click flows untested.
- No auth test (they are AllowAny by design, but still need token validation).

**Notable endpoints:**

- Unsubscribe/snooze: **NONE** — add at least auth + happy-path + bad-token.

---

## 10. Legacy / Appsmith / AWS Marketplace — 6+ endpoints

**Coverage**: NONE 6

**Endpoints:**

- `GET/POST/PATCH /accounts/appsmith/users/`, `appsmith/users/login` — NONE
- `POST /accounts/aws-marketplace/verify-token/`, `signup/`, `launch-software/` — NONE
- `manage_redis_key` (already counted under auth) — ADEQUATE

**Test files:**

- Zero hits for appsmith and aws-marketplace.

**Strengths:**

- None.

**Weaknesses:**

- These are production endpoints with zero test coverage.
- Appsmith SOS login and user sync are untested.
- AWS Marketplace token verification and signup are untested.

**Notable endpoints:**

- All: **NONE** — either add minimal coverage or mark as internal-only with explicit skip.

---

## 11. Onboarding + User profile + Workspace creation during onboarding — 4 endpoints

**Coverage**: STRONG 2 · ADEQUATE 2

**Endpoints:**

1. `POST /accounts/onboarding/` — STRONG
2. `GET /accounts/user-info/` — STRONG
3. Workspace creation during onboarding (service-level) — ADEQUATE
4. Demo project creation — ADEQUATE

**Test files:**

- `test_user.py`
- `test_user_onboard.py` (19 tests)
- `test_post_registration.py`

**Strengths:**

- Onboarding + demo traces/spans + workspace creation edge cases covered.
- Post-registration HubSpot + Slack notification mocked and tested.

**Weaknesses:**

- HubSpot tests mock `requests.post/patch` but do not assert payload shape.
- Demo project integrity tests exist but no test for "onboarding called twice does not duplicate projects".

**Notable endpoints:**

- Onboarding flow: **ADEQUATE/STRONG**.

---

## Cross-cutting patterns

## Strengths worth copying elsewhere

1. **Permission-matrix test classes**: `TestOwnerRoleUpdates`, `TestAdminRoleUpdates`, `TestMemberViewerRoleUpdates` in `test_e2e_role_and_removal.py` — one class per actor role, explicit ALLOW/DENY table. Copy for any new role-gated endpoint.
2. **Multi-org lifecycle classes**: `test_e2e_multi_org_lifecycle.py` — numbered scenarios (`TestLifecycleOwnerInvitesChain`, `TestLifecycleRoleEscalation`) read like user stories.
3. **Error-code regression tests**: `test_login_error_codes.py` — each error code has its own class with middleware + envelope tests.
4. **EE feature-gate monkeypatch in conftest**: `test_2fa_enforcement.py` and `test_e2e_role_and_removal.py` patch `ee.usage.*` so RBAC tests run without Scale/Enterprise license. This pattern should be the default for all accounts RBAC tests.
5. **Cross-org isolation helper**: `test_data_isolation_e2e.py` and `test_cross_org_isolation.py` use `seeded_orgs` fixture — extract to a shared accounts fixture.

## Weaknesses to fix systematically

1. **Legacy endpoints are auth-only**. `test_team.py` and parts of `test_workspace_management.py` only test 401/403 + 404 on nonexistent. Add one happy-path test per endpoint or deprecate.
2. **Zero-test endpoints**: activate, unsubscribe, snooze, appsmith, aws-marketplace. Priority: high for activate (signup flow).
3. **2FA verify paths during login are THIN**. Need happy-path with real device + grace-period enforcement + "user has both TOTP and passkey".
4. **Redundant e2e files**. `test_comprehensive_login_rbac_e2e.py`, `test_e2e_multi_org_lifecycle.py`, `test_org_e2e.py`, `test_e2e_role_and_removal.py` overlap heavily on invite/role/remove flows. Consolidate or mark one as canonical.
5. **EE coupling scattered**. Move the custom-role monkeypatch to a single `accounts/tests/conftest.py` autouse fixture (already partially done) and document it.

---

# Plan for full coverage with quality

Four phases, ordered by risk and alignment with "fix failing, remove redundant, mock EE, new tests lowest".

## Phase 1: Fix failing + remove redundant (by tomorrow)

- Audit every `xfail`, `skip`, `TODO`, broken import, ee import without mock in accounts/tests.
- Mark legacy `test_team.py` endpoints as deprecated or add minimal happy-path tests.
- Consolidate overlapping invite/role/remove tests into one canonical file (recommend `test_e2e_role_and_removal.py` + `test_org_e2e.py`).
- Ensure every test file that touches RBAC has the `allow_custom_role_gate_for_accounts_tests` autouse fixture.

## Phase 2: Mock / gate live services (by tomorrow)

- Ensure every HubSpot/SMTP/Slack call in `test_post_registration.py` is patched.
- Add a marker (`@pytest.mark.ee` or `pytest.mark.requires_ee`) for any test that still needs real EE license; gate in CI.
- Document the conftest monkeypatch for CUSTOM_ROLES so future contributors do not hit 402.

## Phase 3: Fill the ZERO-test endpoints (lowest priority for new tests)

Priority order:

1. `GET /accounts/activate/<uidb64>/<token>/` — extend `test_signup.py`
2. `GET /accounts/notifications/unsubscribe/`, `snooze/` — new minimal tests or mark AllowAny with token validation test
3. Appsmith and AWS Marketplace endpoints — either add smoke tests or document as internal-only
4. 2FA verify paths (TOTP, recovery, passkey) during login — happy-path + error + cross-org
5. Legacy team endpoints — one happy-path per endpoint or deprecate

Each needs (at minimum): happy-path with populated data, auth (401/403), not-found (404), cross-tenant (User A vs Org B).

## Phase 4: Quality maintenance

- Adopt naming: `test_<feature>_<flow>.py` (already mostly followed).
- Keep tests for a feature in one place — move any new RBAC tests into `test_org_e2e.py` or `test_e2e_role_and_removal.py`.
- Focus on unit + functional; integration not the goal.
- For every new endpoint: happy path + auth + error path + cross-tenant.
- Call out added/removed/changed tests in every PR description.

---

# Priority backlog — the top 15 things to fix first

Ranked by (risk × frequency of use):

1. `GET /accounts/activate/<uidb64>/<token>/` — 0 tests, blocks signup flow. **Phase 3.**
2. Legacy team endpoints (`/team/users/`, `/user/role/update/`, etc.) — only auth tests. **Phase 1 (deprecate or minimal happy-path).**
3. 2FA verify paths during login — THIN, security-critical. **Phase 3.**
4. Annotation unsubscribe/snooze — 0 tests. **Phase 3.**
5. Appsmith and AWS Marketplace — 0 tests. **Phase 3 (or document internal).**
6. Remove redundant e2e overlap across 4 large RBAC files. **Phase 1.**
7. Ensure every RBAC test file uses the custom-role gate fixture. **Phase 1.**
8. Add happy-path for `organizations/update/`. **Phase 3.**
9. Add cross-tenant test for `organization/2fa-policy/`. **Phase 3.**
10. Document EE mocking strategy in `TESTING.md` or a new `accounts/tests/README.md`. **Phase 2.**
11. `password-reset-confirm` happy-path token validation. **Phase 3.**
12. `delete-users/` and `resend-invitation-emails/` functional tests. **Phase 3.**
13. Passkey authenticate verify happy-path. **Phase 3.**
14. Key enable/disable actual usability assertion. **Phase 3.**
15. Adopt "call out added/removed/changed tests in PR description" as repo policy. **Phase 4.**

---

## Files you should open to see quality patterns

If you want to inspect quality yourself and calibrate what "good" looks like:

- **Gold standard (copy this)**: `test_e2e_role_and_removal.py` and `test_org_e2e.py` — permission matrix + cross-org + lifecycle.
- **Cleanest error-code coverage**: `test_login_error_codes.py` — one class per code + middleware tests.
- **Best multi-org lifecycle**: `test_e2e_multi_org_lifecycle.py` — 15 scenario classes.
- **Best session security regression suite**: `test_session_security.py`.
- **Best workspace FK + migration invariant tests**: `test_workspace_member_fk.py`.
- **Pattern to avoid**: `test_team.py` — only auth + 404, no happy path.
- **EE mocking pattern**: `conftest.py` `_allow_custom_role_gate_for_accounts_tests` + patches in `test_e2e_role_and_removal.py` and `test_config_endpoint.py`.

---

_End of coverage analysis. Companion doc: none yet — this is the factual catalog + analysis for accounts RBAC and signup._

---

**Test counts (verified 2026-07-24):**

- 45 test files
- 1203 individual test functions
- 14 classes in `test_comprehensive_login_rbac_e2e.py` alone
- Strongest files by test count: `test_workspace_management.py` (88), `test_e2e_role_and_removal.py` (73), `test_org_e2e.py` (67), `test_workspace.py` (65), `test_comprehensive_login_rbac_e2e.py` (60)
