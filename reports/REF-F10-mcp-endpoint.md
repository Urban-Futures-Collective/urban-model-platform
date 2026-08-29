_Last_updated: 2026-08-29

# Notes for the assistant

- The user prefers explicit dependency injection. Do not instantiate adapters inside adapters; instantiate them in `main.py` and inject.
- Keep the core free of framework code.
- When proposing changes, include small tests where feasible and run quick syntax/type checks.
- `providers.yaml` uses a list-based format under a `providers:` key — not the old dict-keyed format. See `providers.yaml.example`.
- When the user asks for implementation details for "ensembles": ask for reference code to gain insights; do not reuse the provided code — find a better solution and inform the user.

# Feature X: MCP tool-catalog endpoint 🚧 (phase 1)

Supersedes the UMP-side half of `mcp-integration-strategy.md` (branch `feat/mcp`, written
against the v2 Flask codebase). The strategy, the security model and the roadmap in that
document still stand unchanged; only the implementation location moves into the hexagonal
core. The MCP-server side of that document is implemented in a separate repository:
<https://github.com/Urban-Futures-Collective/ump-x-mcp>.

## Scope

`ump-x-mcp` calls exactly four UMP endpoints:

| Endpoint | Status |
|---|---|
| `GET /mcp/tools` | 🔲 this feature |
| `POST /processes/{provider:process}/execution` | ✅ exists (Feature III) |
| `GET /jobs/{jobID}` | ✅ exists (Feature III) |
| `GET /jobs/{jobID}/results` | ✅ exists (Feature III / VIII) |

Three of the four are OGC API Processes endpoints and are **not touched**: they are
standardised, and the MCP integration must not fork their behaviour. The whole of this
feature is therefore one new endpoint plus the core service behind it.

## Architectural position

`GET /mcp/tools` is a **new driving (primary) adapter surface over the existing core** —
a second projection of the process catalog, shaped for agents instead of for OGC clients.
It is not a new outbound dependency, so **it introduces no new port**.

`mcp-integration-strategy.md` §9 lists `ToolCatalogPort`, `ToolExecutionPort`,
`IdentityValidationPort` and `AuthorizationContextPort`. Those are the **MCP server's**
internal ports and belong to `ump-x-mcp`, which already implements them. Mirroring them
inside UMP would add indirection without removing a dependency: every input the catalog
needs is already reachable through `ProvidersPort` and through two existing core
collaborators, `ProcessManager` and `AuthorizationService`.

`ToolCatalogService` depends on those two **directly**. Both live in the core, so this is a
core-to-core call; ports exist to keep infrastructure out of the core, not to mediate
between core components.

```
            HTTP (driving adapters)
   ┌─────────────────────┬──────────────────┐
   │  OGC router /v1.0/* │  MCP router /mcp │   ← untouched  |  new
   └──────────┬──────────┴────────┬─────────┘
              │                   │
              │            ToolCatalogService        (core/services)
              │             │            │
        ProcessManager ─────┘            └── AuthorizationService
              │                                      │
        HttpClientPort, ProvidersPort, RemoteAuthPort (ports → adapters)
```

## Files

| File | Contents |
|---|---|
| `src/ump/core/models/tool.py` | `ToolDescriptor`, `ToolCatalog` — the published contract |
| `src/ump/core/utils/input_schema.py` | `ogc_inputs_to_json_schema()`, `is_input_required()` — pure |
| `src/ump/core/services/tool_catalog.py` | `ToolCatalogService.build(auth) -> ToolCatalog` |
| `src/ump/core/services/authorization.py` | **add** `can_access_process()` predicate |
| `src/ump/adapters/web/mcp.py` | `create_mcp_router(...)` — HTTP concerns only |
| `src/ump/adapters/web/fastapi.py` | mount the router; new `tool_catalog_factory` param |
| `src/ump/asgi.py` | wiring (composition root) |

## Response contract

```jsonc
{
  "version": "1.0",
  "tools": [
    {
      "tool": "mock:hello-world",       // canonical UMP process id
      "title": "Hello World",
      "description": "...",
      "inputSchema": { "type": "object", "properties": {...}, "required": [...] },
      "provider": "mock",
      "processId": "hello-world"
    }
  ]
}
```

Field names follow §4 of `mcp-integration-strategy.md` and the v2 prototype, so the
existing `ump-x-mcp` mapping code keeps working. `version` is the **catalog** contract
version and is deliberately independent of `UMP_SUPPORTED_API_VERSIONS`.

## Route placement

Mounted as its own router with prefix `/mcp`, registered **outside** the
`for ver in UMP_SUPPORTED_API_VERSIONS` loop in `create_app`. Consequences:

- the OGC router is not modified at all;
- the MCP contract versions on its own clock rather than being dragged along by the
  OGC API version;
- the path matches what `ump-x-mcp` already calls.

## Authorization

Per-user filtering, reusing the existing role model — `mcp-integration-strategy.md` §7
("Rollenlogik nicht im MCP duplizieren") applies inside UMP as well as outside it.

`AuthorizationService` currently exposes only `check_process_access()`, which returns
`None` or raises 401/403. A catalog needs a boolean. Add:

```python
def can_access_process(self, auth: AuthContext, process_id: str) -> bool
```

and re-express `check_process_access()` in terms of it, so the anonymous-access and role
rules keep exactly one implementation.

**`GET /mcp/tools` never returns 401.** An anonymous caller receives the catalog filtered
to `anonymous-access: true` processes, which may legitimately be empty — `ump-x-mcp`
supports anonymous operation via `UMP_MCP_ALLOW_ANONYMOUS`. The gate used by
`GET /processes` (`fastapi.py:270`, `auth_port is not None and not UMP_PUBLIC_PROCESSES`)
is deliberately **not** reused here; see "Known trap" below.

## Known trap: `UMP_AUTH_ENABLED=false`

`settings.py` describes `UMP_AUTH_ENABLED` as the master switch to "bypass all auth
checks", but `JwtAuthAdapter.verify()` returns `is_authenticated=False` when auth is
disabled, while `asgi.py` still wires an auth port. The process routes gate on
`is_authenticated`, so with auth disabled `GET /processes` answers **401** unless
`UMP_PUBLIC_PROCESSES=true` is also set.

The catalog must not inherit that behaviour. Decision: **when `UMP_AUTH_ENABLED` is false,
the catalog treats the caller as fully privileged** (dev/test convenience, matching the
documented intent of the switch). This is handled in the web adapter, which owns the
"is auth wired at all?" question, mirroring `_check_process_access`.

Fixing the same inconsistency on the OGC process routes is out of scope here — it changes
the behaviour of a standardised endpoint and needs its own decision.

## Implementation notes

**Process descriptions, not summaries.** `ProcessManager.get_all_processes()` fetches full
`Process` objects and then downcasts them — `ProcessSummary(**process.model_dump())`
(`process_manager.py:383`) — caching only summaries. `ProcessSummary` has no `inputs`, and
`inputs` is precisely what becomes `inputSchema`. The catalog therefore resolves each
allowed process through `get_process(canonical_id)`, gathered concurrently, which goes
through the per-process cache. A later optimisation is to have `_cache_process` retain the
full `Process`; not required for phase 1.

**Reuse the transport, not the v2 code.** The v2 prototype (`feat/mcp:src/ump/api/mcp.py`)
opens its own `aiohttp.ClientSession`, re-resolves provider credentials through
`get_auth_strategy`, and re-fetches every process description. In v3 all of that is already
provided by `ProcessManager`, `AioHttpClientAdapter` and `RemoteAuthAdapter`, including
caching, retry and remote auth. Port the schema-mapping logic (which correctly handles
`minOccurs`); drop the transport.

**Model detail.** `ProcessInput.scheme` is aliased to `schema` (`process.py`), so the
mapper reads `.scheme`.

**Failure isolation.** One unreachable provider must not fail the whole catalog: gather
with `return_exceptions=True`, log, and omit that tool — same policy `get_all_processes`
already uses for a failing provider.

## Tests

Following the house convention: hand-written in-test port adapters, no mocks of concrete
classes.

| Test | Assertion |
|---|---|
| Parity (§11) | catalog for a token ⊆ `/processes` for the same token |
| Role matrix (§11) | no token / token without role / provider role / process role |
| Anonymous | anonymous caller sees only `anonymous-access: true`, never 401 |
| Schema mapping | table test: `minOccurs`, `required`, missing `schema`, defaults |
| Failure isolation | one provider unreachable → other tools still listed |
| Dynamics (§11) | provider added to `providers.yaml` → appears without code change |

All runnable against `scripts/mock_ogc_server.py`; no Keycloak required.

## Out of scope

The MCP protocol itself — JSON-RPC, streamable HTTP transport, `tools/call` — stays in
`ump-x-mcp` (`mcp-integration-strategy.md` §12.4, "V1 als externer MCP-Service (Sidecar)").
UMP remains an OGC API Processes server that additionally publishes a role-filtered,
agent-shaped view of its catalog.

If tool descriptors later need enrichment the OGC description cannot carry (examples,
output hints, cost estimates), that *is* a genuine outbound dependency and would be added
as a `ToolAnnotationsPort` — the one port this feature might eventually justify.

## Open question — endpoint versioning of the OGC calls

`ump-x-mcp` documents its UMP calls as `POST /processes/{id}/execution` and
`GET /jobs/{id}`, i.e. **unversioned**, but v3 mounts those routes only under `/v1.0/`
(verified: `GET /processes` → 404, `GET /v1.0/processes` → 200). Either `ump-x-mcp`'s UMP
base URL must include the `/v1.0` prefix, or UMP must expose an unversioned alias. To be
confirmed with the `ump-x-mcp` maintainers before integration testing.

## Roadmap position

Phase 1 of `mcp-integration-strategy.md` §10 ("Dynamische Read-Only Tools"), UMP side.
Phase 2 (execution) needs no further UMP work — it reuses the existing OGC execution and
job endpoints with the caller's JWT forwarded unchanged.
