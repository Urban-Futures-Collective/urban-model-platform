_Last_updated: 2026-09-26

# Notes for the assistant

- The user prefers explicit dependency injection. Do not instantiate adapters inside adapters; instantiate them in `main.py` and inject.
- Keep the core free of framework code.
- When proposing changes, include small tests where feasible and run quick syntax/type checks.
- `providers.yaml` uses a list-based format under a `providers:` key — not the old dict-keyed format. See `providers.yaml.example`.
- When the user asks for implementation details for "ensembles": ask for reference code to gain insights; do not reuse the provided code — find a better solution and inform the user.

# Feature XI: Role administration (Keycloak Admin API) 🔲 (not started)

Administering users and their model-access roles from the UMP-X frontend instead of the
Keycloak admin console.

**Supersedes** the decision recorded in `ump-x-frontend/docs/model-access-admin-decision-de.md`
(2026-07-09), which recommended **Option B** — "the backend exposes a management endpoint,
UMP talks to Keycloak internally". That option assumed the v2 Flask codebase, which held
`UMP_KEYCLOAK_*` credentials and called Keycloak's admin API. **v3 removed that capability
deliberately**: it validates tokens offline against JWKS and has no Keycloak client, no admin
credential and no outbound dependency on the IdP at all (see `REF-F4-jwt-auth.md` and commit
`b5e13cd`). Option B would mean re-introducing exactly the coupling v3 was built to shed.

This document therefore records the reversal: **role administration lives in UMP-X, not in
UMP.** From the July document, groups over per-user grants still stand and are carried
forward below. The single `ump_admin` role proposed there is **replaced** (decision
2026-09-26) by two admin roles with disjoint powers, `access-admin` and `platform-admin` —
see [Platform roles](#platform-roles) and [Two admin gates](#two-admin-gates-not-one).

**Keycloak is the single source of truth for all roles** — the six platform roles *and* the
model-access roles. The model registry (`REF-F12-model-registry.md`) deliberately stores no
grants and no visibility level; it only reads roles from the token.

## Scope

**In scope (UMP-X):**
- `platform-admin`: listing, creating and deactivating user accounts; assigning and removing
  the six platform roles.
- `access-admin`: granting and revoking model access (`{provider}`, `{provider}:{process}`),
  per user or per group; the access-matrix UI behind the existing `/admin` route.

**Out of scope (UMP):** nothing. This feature requires **no change to UMP**. It is listed
here because it decides a boundary of UMP's architecture — namely that UMP keeps no IdP
write path — and because the role *vocabulary* it administers is defined by UMP: the
model-access roles by `AuthorizationService`, the platform roles by the model registry
(Feature XII), which reads them from the token like any other role.

## Architectural position

UMP's relationship to the IdP is read-only and offline: JWKS in, roles out of the token.
Nothing writes back.

```
                 ┌───────────────────────────────┐
   admin user ──►│ UMP-X (Nuxt BFF)              │
                 │  server/api/admin/*           │──── service account ───► Keycloak
                 │  (2 admin gates, allowlisted) │      (Admin REST API)      Admin API
                 └───────────────────────────────┘                              │
                                                                                │ realm roles
   any user ────► UMP-X ──► /ump/** proxy ──► UMP ◄──── JWKS (read-only) ───────┘
                            (user's token)     │
                                               └─ AuthorizationService reads roles from token
```

The write path (top) and the enforcement path (bottom) never touch. UMP learns about a role
change only because the user's *next token* carries different roles.

## Role vocabulary — what the admin UI must produce

Two kinds of realm role. Both are plain realm roles in the same claim
(`realm_access.roles`); they differ only in who may assign them.

### Platform roles

Fixed vocabulary of six roles. They say *what kind of actor* someone is; they never name a
model.

| Role | May do | Assigned by |
|---|---|---|
| `provider` | register model servers, submit and maintain own models (F12) | `platform-admin` |
| `verifier` | review, verify, publish, reset verification (F12) | `platform-admin` |
| `access-admin` | grant/revoke model-access roles (this feature) | `platform-admin` |
| `platform-admin` | manage accounts and platform roles (this feature); deactivate models (F12) | `platform-admin` |
| `user` | run models it holds a model-access role for; view/export results | `platform-admin` |
| `viewer` | browse the catalogue, no execution | `platform-admin` |

`user` should be a **composite role containing `viewer`**, otherwise a `user` cannot browse
the catalogue. Roles are additive: one person may hold several (e.g. `provider` + `user`).

Bootstrapping: the first `platform-admin` is assigned once in the Keycloak admin console;
everything after that goes through UMP-X.

### Model-access roles

This is the contract between the two systems and the part most likely to be got wrong,
because v2 and v3 disagree on both the **name** and the **claim**.

| Grant | v2 (Flask) | v3 | Compatible? |
|---|---|---|---|
| Whole provider | `{provider}` | `{provider}` | ✅ unchanged |
| Single process | `{provider}_{process}` | `{provider}:{process}` | ❌ separator changed |
| Read from | `realm_access.roles` **and** `resource_access.{client}.roles`, always both | only the dot-paths in `UMP_JWT_ROLES_CLAIMS` (default `realm_access.roles`) | ⚠️ see below |

Source: `old_src/ump/api/processes.py:26-35,179-189` vs `core/services/authorization.py:44-54`
and `adapters/jwt_auth_adapter.py:_extract_roles`.

Two consequences for the admin UI:

1. **Create realm roles, not client roles** — unless `UMP_JWT_ROLES_CLAIMS` is explicitly set
   to include `resource_access.{client}.roles`. Per the July document the realm's existing
   model-access roles are *client* roles on `ump-client` (`modelserver`,
   `modelserver_hello-world`, …), so on the current deployment they are invisible to v3 twice
   over: wrong separator *and* wrong claim. Worth re-verifying against the live realm before
   any migration, since the provider set has changed since July.
2. **The role name is the provider prefix from `providers.yaml`**, because `_provider_of()`
   splits the canonical id on `:`. The admin UI should therefore derive its role vocabulary
   from `GET /v1.0/processes` rather than from a hand-maintained list — the ids returned there
   *are* the process-role names, and their prefixes *are* the provider-role names.

Current deployment (`ump.urbanfuturescollective.org`, verified 2026-08-31): providers
`modelserver-1` and `bikebox-modelserver`, four processes, of which only
`bikebox-modelserver:growbike` is `anonymous-access: true`.

## Keycloak Admin REST API surface

Admin base on the current instance is `{host}/auth/admin/realms/UrbanModelPlatform` — note
the legacy `/auth` prefix, per `ump-x-frontend/.env.example:7`. Paths below are relative to it.

| Purpose | Call |
|---|---|
| List roles | `GET /roles` |
| Create role | `POST /roles` — `{"name":"modelserver-1:abm-test-model"}` |
| Resolve role → uuid | `GET /roles/{role-name}` |
| Delete role | `DELETE /roles/{role-name}` |
| Who holds a role | `GET /roles/{role-name}/users?first=&max=` |
| Search users | `GET /users?search=&first=&max=` |
| A user's grants | `GET /users/{id}/role-mappings/realm` |
| What they could get | `GET /users/{id}/role-mappings/realm/available` |
| **Grant** | `POST /users/{id}/role-mappings/realm` — `[{"id":"<uuid>","name":"<name>"}]` |
| **Revoke** | `DELETE /users/{id}/role-mappings/realm` — same body |
| Force re-auth | `POST /users/{id}/logout` |
| Create account (`platform-admin`) | `POST /users` — `{"username":…,"email":…,"enabled":true}` |
| Deactivate account (`platform-admin`) | `PUT /users/{id}` — `{"enabled":false}`, then `POST /users/{id}/logout` |

Grant/revoke take an *array* of role representations and need both `id` and `name`, so a
grant is always two calls (resolve, then map) unless the role list is cached.

The last two rows of the table plus `GET /roles/{role-name}/users` are enough to render the
access matrix in either orientation (per-user or per-model).

### Groups (recommended for the matrix)

Carried forward from the July recommendation: attach roles to groups and users to groups,
rather than granting per user. Same token output, far fewer writes.

| Purpose | Call |
|---|---|
| List groups | `GET /groups` |
| Group's roles | `POST /groups/{id}/role-mappings/realm` |
| Add user to group | `PUT /users/{id}/groups/{groupId}` |
| Remove | `DELETE /users/{id}/groups/{groupId}` |

## Authentication: a dedicated service account

Do **not** reuse the logged-in admin's token — that would require granting real
`realm-management` rights to human users and would put admin privileges into a browser
session. Instead add a confidential client, e.g. `ump-x-admin`:

- Standard flow **off**, Direct access grants **off**, Service accounts **on**
- Service-account roles, from the `realm-management` client:

| Role | Needed for |
|---|---|
| `view-users`, `query-users` | listing and searching users |
| `manage-users` | grant / revoke role mappings, group membership |
| `view-realm` | reading the realm role list |
| `manage-realm` | creating model-access roles on publish (F12, D8) and in the reconciler |

**`manage-users` is not scoped.** It lets the service account map *any* realm role to *any*
user — including `platform-admin`. Keycloak does not know which UMP-X admin triggered the
call, so the only thing standing between an `access-admin` and self-promotion is the
server-side allowlist described in [Two admin gates](#two-admin-gates-not-one). Keycloak's
fine-grained admin permissions (v2) could narrow the service account further; verify the
feature is available on the deployed Keycloak version before relying on it.

**`manage-realm` is broader still.** Creating realm roles requires it, but it also allows
changing realm settings, clients and identity providers. With D8 (automatic role creation
on publish, F12) the service account must hold it, which makes the client secret a
realm-takeover credential. Mitigations: keep the secret server-only and rotated; restrict
the client by network (only the UMP-X pod may use it); use fine-grained admin permissions
(v2) to limit it to role management where the Keycloak version supports it. The
alternative — an ops-run script that creates roles after publish — avoids `manage-realm`
in UMP-X at the cost of a manual step.

Token via `POST {host}/auth/realms/UrbanModelPlatform/protocol/openid-connect/token` with
`grant_type=client_credentials`. The secret is server-only (`NUXT_*`, never `NUXT_PUBLIC_*`)
and the token is cached in the Nitro server, never sent to the browser.

## The frontend seam

UMP-X already has a gate: `app/middleware/admin.ts` guards `/admin` on the `ump_admin`
role, and `app/pages/admin/index.vue` is a placeholder marked "Folge-Sprint". The gate must
change to the two roles below; `ump_admin` is dropped.

**Do not implement this as a second pass-through.** `server/routes/ump/[...path].ts` is a
generic `/ump/**` → UMP proxy, and that is safe *because UMP re-checks authorization on every
request using the caller's own token*. A `/kc/**` equivalent would run under the **service
account's** privileges, so any logged-in user could administer the entire realm. The admin
surface must be a small set of explicit, typed operations:

```
# access-admin — model access
server/api/admin/access/principals.get.ts   search users and groups
server/api/admin/access/roles.get.ts        list model-access roles
server/api/admin/access/grants.get.ts       matrix for one user/group (or one role)
server/api/admin/access/grants.post.ts      grant   { principal, roleName }
server/api/admin/access/grants.delete.ts    revoke  { principal, roleName }
server/api/admin/access/sync-roles.post.ts  create missing model-access roles (reconciler)

# verifier — publish wrapper (F12, D8)
server/api/registry/models/[id]/publish.post.ts
                                            forward publish to UMP with the verifier's
                                            token; on success create {provider} and
                                            {provider}:{process} roles (409 = ok)

# platform-admin — accounts and platform roles
server/api/admin/platform/users.get.ts      search users (incl. disabled)
server/api/admin/platform/users.post.ts     create account
server/api/admin/platform/users.patch.ts    enable / disable account
server/api/admin/platform/roles.post.ts     assign platform role { userId, roleName }
server/api/admin/platform/roles.delete.ts   remove platform role { userId, roleName }
```

`app/middleware/admin.ts` is client-side route middleware: it hides the UI, it does not
protect an endpoint. Protection is server-side only, per route group.

### Two admin gates, not one

| Route group | Caller must hold | `roleName` must be in |
|---|---|---|
| `server/api/admin/access/*` | `access-admin` | the model-access vocabulary from `GET /v1.0/registry/roles/vocabulary` (F12); `GET /v1.0/processes` until F12 ships |
| `server/api/registry/models/[id]/publish.post.ts` | `verifier` (UMP re-checks with the verifier's token) | only the two role names derived from the published model — never caller-supplied |
| `server/api/admin/platform/*` | `platform-admin` | the fixed set `provider`, `verifier`, `access-admin`, `platform-admin`, `user`, `viewer` |

Both checks are required on every write. Checking only the caller's role is not enough:
because the service account can map any role, an `access-admin` route that accepts an
arbitrary `roleName` is a privilege-escalation path. Reject everything outside the
allowlist, including `default-roles-*`, `offline_access`, `uma_authorization` and any
`realm-management` role.

Two further rules:
- A `platform-admin` must not remove `platform-admin` from themselves or disable their own
  account (prevents locking the realm out of UMP-X administration).
- The platform vocabulary is a constant shared by the Nitro routes and `useUmpRoles`; it is
  not derived from Keycloak, so a role created by hand in the console never becomes
  assignable through UMP-X.

A `useUmpAccess` composable (`listModels / listPrincipals / getGrants / grant / revoke`) was
already sketched in the July document and remains the right client-side shape — it now calls
these Nitro routes instead of a UMP endpoint.

## Caveats

- **Role names**: colons are legal in Keycloak role names, so `modelserver-1:abm-test-model`
  is fine, but URL-encode it when it appears as a path parameter. Never allow `/` in a role
  name — it breaks every `/roles/{role-name}` endpoint.
- **Propagation delay**: changing roles does not invalidate issued tokens. A grant or revoke
  takes effect only when the user's access token is refreshed, i.e. within one access-token
  lifespan. Use `POST /users/{id}/logout` when a revoke must be immediate — and note that the
  UI should say so, or admins will report it as a bug.
- **Matching is exact and case-sensitive.** `_extract_roles` applies no normalisation.
- **Renaming a provider in `providers.yaml` silently invalidates its roles**, since the
  provider role name *is* the `name:` key. `scripts/convert_providers_yaml.py` preserves the
  v2 mapping key as `name` for exactly this reason. Once the model registry (F12) owns
  provider servers, `provider_server.name` is immutable for the same reason.
- **Audit trail lives in Keycloak.** Grants, revokes and account changes never pass through
  UMP, so UMP's audit log (F12) cannot record them. Enable **Admin events** for the realm
  (Realm settings → Events → Admin events, *Include representation* on) and set a retention
  period. The events record the service account as actor, not the human admin — so each
  Nitro admin route must also log the calling user's `sub` alongside the Keycloak call
  (server log at minimum) to keep "who granted this" answerable.

## Open questions

1. **Migrate or re-create?** The existing client roles on `ump-client` are v2-shaped. Rename
   them to the v3 form and move them to realm roles, or set
   `UMP_JWT_ROLES_CLAIMS=realm_access.roles,resource_access.ump-client.roles` and rename only
   the separator? The latter is one env var and no user re-mapping, but keeps two claim
   sources alive indefinitely.
2. **Groups now or later?** Groups are the better model but add a migration step; per-user
   grants ship faster and can be converted later.
3. ~~Should the admin UI create roles at all?~~ **Resolved 2026-09-26:** roles are created
   automatically when a model is published (F12, D8), and by the reconciler from the
   registry's vocabulary. The UI never takes a free-text role name. Roles are never deleted
   automatically.
4. **Where does "human admin" get recorded?** See the audit caveat: Keycloak admin events
   show the service account only. A server log is the minimum; a dedicated audit sink is the
   alternative.

## Files to create / modify

All in `ump-x-frontend`; **no UMP files change**.

| File | Action | Notes |
|---|---|---|
| `server/utils/keycloakAdmin.ts` | CREATE | Service-account token fetch + cache; typed Admin-API wrapper |
| `server/utils/requireRole.ts` | CREATE | Server-side check for `access-admin` / `platform-admin`, one helper per route group |
| `server/utils/roleVocabulary.ts` | CREATE | Platform-role constant + model-access vocabulary from `GET /v1.0/processes`; the allowlists |
| `server/api/admin/access/*.ts` | CREATE | The five `access-admin` operations above |
| `server/api/admin/platform/*.ts` | CREATE | The five `platform-admin` operations above |
| `server/api/admin/access/sync-roles.post.ts` | CREATE | Reconciler: registry vocabulary vs. Keycloak realm roles |
| `server/api/registry/models/[id]/publish.post.ts` | CREATE | Publish wrapper that creates the model-access roles (F12, D8) |
| `app/middleware/admin.ts` | MODIFY | Gate on `access-admin` OR `platform-admin` instead of `ump_admin` |
| `app/composables/useUmpRoles.ts` | MODIFY | Expose `isAccessAdmin` / `isPlatformAdmin`; drop `ADMIN_ROLE = ump_admin` |
| `app/composables/useUmpAccess.ts` | CREATE | Client-side facade (replaces the planned mock adapter) |
| `app/pages/admin/index.vue` | MODIFY | Replace the placeholder with two tabs: access matrix (`access-admin`), accounts & platform roles (`platform-admin`) |
| `nuxt.config.ts` | MODIFY | `runtimeConfig` entries for the admin client id/secret (server-only) |
| `.env.example` | MODIFY | `NUXT_KEYCLOAK_ADMIN_CLIENT_ID` / `_SECRET`, admin base URL |
| `docs/model-access-admin-decision-de.md` | MODIFY | Note that decision 1 (Option B) is superseded by this document |
