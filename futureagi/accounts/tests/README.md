# Accounts tests

Unit + functional coverage for signup, auth, RBAC, multi-org, 2FA, and workspace membership.

## Run

From the backend root (`futureagi/`):

```bash
./bin/test app accounts
```

`bin/test` forces test ClickHouse env (`test_tfc` / `18123` / `19000`) and sets `TESTING=true` so the CH DB is auto-created.

## Conventions

- File naming: `test_<feature>_<flow>.py`
- Keep a feature’s tests in one place; extend existing files before adding new ones
- Prefer unit/functional over broad multi-file e2e when both exist
- For endpoints you touch: happy path, auth, error path, cross-tenant (workspace/org-scoped)

### Canonical RBAC files

| Concern                                           | Canonical file                         |
| ------------------------------------------------- | -------------------------------------- |
| Role update + removal permission matrix           | `test_e2e_role_and_removal.py`         |
| Invite / member list / org update flows           | `test_org_e2e.py`                      |
| Multi-org switch / lifecycle journeys             | `test_e2e_multi_org_lifecycle.py`      |
| Login + invite accept + removal recovery journeys | `test_comprehensive_login_rbac_e2e.py` |
| Legacy team/user management APIs                  | `test_workspace_management.py`         |

`test_team.py` was removed — it only re-tested auth/404 already covered in `test_workspace_management.py`.

## EE / live-service mocking

Autouse fixtures in `conftest.py`:

1. **CUSTOM_ROLES gate** via `tfc.ee_gating.check_ee_feature` — always applied.
2. **Plan entitlement** via `Entitlements.check_feature` — applied only when `ee.usage` imports; no-op on OSS checkouts.

Do **not** re-declare local copies of these fixtures in individual test modules.

| Surface                        | How tests stay offline                                                                              |
| ------------------------------ | --------------------------------------------------------------------------------------------------- |
| HubSpot / Slack / signup email | Mocked in `test_post_registration.py`                                                               |
| Cloud config detection         | `test_config_endpoint.py` skips cloud cases if `ee` missing; patches secret validation when present |
| Custom roles billing gate      | `conftest.py` autouse fixtures above                                                                |

If you add a test that needs a real EE license denial path, put it under `ee/` tests — not here.

## PR checklist

- Call out added / removed / changed tests in the PR description
- Prefer extending an existing file over a new e2e suite that re-walks invite→role→remove

## Internal-only endpoints (no unit coverage yet)

These production routes are intentionally out of the current accounts unit/functional scope:

| Surface         | Paths                                                         | Notes                                   |
| --------------- | ------------------------------------------------------------- | --------------------------------------- |
| Appsmith ops    | `/accounts/appsmith/users/`, `/accounts/appsmith/users/login` | `APIKeyPermission` internal tooling     |
| AWS Marketplace | `/accounts/aws-marketplace/*`                                 | Marketplace registration / launch flows |

Track follow-up coverage as Linear tickets rather than blocking the CI green bar.
If you touch these endpoints, add happy/auth/error cases in a dedicated
`test_appsmith_api.py` / `test_aws_marketplace_signup.py` file.
