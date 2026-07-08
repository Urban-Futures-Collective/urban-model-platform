# UMP-X System Architecture

UMP-X expands the Urban Model Platform (this repo) with two new components:

1. **UMP-X Frontend** ([`ump-x-frontend`](https://github.com/Urban-Futures-Collective/ump-x-frontend)) — a web UI for browsing providers/processes, running jobs, and viewing results.
2. **MCP Server** — an Agent Experience (AX) layer that exposes UMP processes as MCP tools so LLMs/agents can discover and invoke them (see [`mcp-integration-strategy.md`](./mcp-integration-strategy.md) for the detailed design). This is planned, not yet implemented, and is intended to ship as an independently deployed service ("sidecar"), not as code inside this repo.

Since UMP, the frontend, and the MCP server are separate deployable projects, **Keycloak is the one component all three depend on directly** and is what keeps them consistent with each other. Each service validates the same user JWT independently (zero-trust — see strategy doc §3) rather than trusting a single upstream check.

## Component dependency diagram

```mermaid
flowchart TB
    %% Styles
    classDef client fill:#c9a0dc,stroke:#333,stroke-width:2px,color:black
    classDef agent fill:#f4a261,stroke:#333,stroke-width:2px,color:black
    classDef gateway fill:#e4e4e4,stroke:#333,stroke-width:2px,color:black
    classDef auth fill:#ff9,stroke:#333,stroke-width:2px,color:black
    classDef api fill:#4a90e2,stroke:#333,stroke-width:2px,color:white
    classDef mcp fill:#2a9d8f,stroke:#333,stroke-width:2px,color:white
    classDef geoserver fill:#9acd32,stroke:#333,stroke-width:2px,color:black
    classDef external fill:#f0f0f0,stroke:#999,stroke-width:1px,stroke-dasharray: 4 3,color:black
    classDef db fill:#ffb366,stroke:#333,stroke-width:2px,color:black

    %% Nodes
    user((User))
    frontend[UMP-X Frontend<br/>Nuxt]
    agentclient[AI Agent / LLM Client<br/>e.g. Claude, custom agent]
    gateway[k8s Gateway API]
    keycloak[Keycloak]
    ump[UMP API<br/>this repo]
    mcp[MCP Server<br/>planned, separate service]
    geoserver[GeoServer]
    modelservers[Model Servers<br/>N external OGC API Processes providers]
    db_auth[Auth PostgreSQL]
    db_api[API PostgreSQL]
    db_spatial[Spatial PostgreSQL]

    %% Human path
    user --> frontend
    user --> agentclient

    %% Frontend
    frontend -- login / obtain JWT --> keycloak
    frontend -- REST + JWT --> gateway

    %% Agent / MCP path
    agentclient -- obtain JWT --> keycloak
    agentclient -- MCP tool calls + JWT --> gateway
    gateway --> mcp
    mcp -- validate JWT via JWKS --> keycloak
    mcp -- "GET /mcp/tools, POST /processes/{id}/execution (forwards same JWT)" --> ump

    %% Gateway to core services
    gateway --> ump

    %% UMP core
    ump -- validate JWT --> keycloak
    ump -- discover/execute processes --> modelservers
    ump -- store/query jobs, ensembles, users --> db_api
    ump -- store geo results --> geoserver

    %% Storage-layer dependents
    keycloak --> db_auth
    geoserver --> db_spatial

    %% Apply classes
    class user,frontend client
    class agentclient agent
    class gateway gateway
    class keycloak auth
    class ump api
    class mcp mcp
    class geoserver geoserver
    class modelservers external
    class db_auth,db_api,db_spatial db

    %% Repo/deployment groupings
    subgraph "UMP-X Frontend repo"
        frontend
    end

    subgraph "Clients"
        user
        agentclient
    end

    subgraph "Network Layer"
        gateway
    end

    subgraph "MCP Server repo (planned)"
        mcp
    end

    subgraph "UMP repo (this repo)"
        ump
    end

    subgraph "Auth"
        keycloak
    end

    subgraph "Geospatial Web Data"
        geoserver
    end

    subgraph "External Model Server Deployments"
        modelservers
    end

    subgraph "Storage Layer"
        db_auth
        db_api
        db_spatial
    end
```

## Components and ownership

| Component | Repo / deployment | Responsibility |
|---|---|---|
| UMP-X Frontend | `ump-x-frontend` | Human-facing UI: browse providers/processes, submit jobs, view job/ensemble results. |
| AI Agent / LLM Client | external (Claude, ChatGPT, custom agents, etc.) | Any MCP client acting on a user's behalf; not part of UMP-X itself. |
| MCP Server | new repo, planned | Translates UMP processes into MCP tools (discovery + execution), enforces its own JWT validation, forwards the original user JWT to UMP unchanged. |
| UMP API | this repo | Source of truth for providers/processes, job lifecycle, role-based authorization, results retrieval/storage. |
| Keycloak | shared infra | Issues and signs JWTs; the only identity provider all three services trust. |
| Model Servers | external, one per provider in `providers.yaml` | Actually execute simulation/model jobs; OGC API Processes-compliant. |
| GeoServer | shared infra | Serves geospatial job results as WFS/WMS layers. |
| API / Auth / Spatial PostgreSQL | shared infra | Separate databases for UMP job/ensemble/user data, Keycloak realm data, and GeoServer's spatial datastore, respectively. |

## Why every service talks to Keycloak directly

Per the zero-trust model in the MCP strategy doc: a single login gets a user one JWT, but **every** service that receives that JWT (UMP, MCP) validates it independently against Keycloak's JWKS rather than trusting that an upstream hop already checked it. This means:

- Keycloak is a hard dependency for the frontend (login), the MCP server (token validation), and UMP (token validation) — not just for UMP as today.
- The MCP server never talks to the UMP or Keycloak databases directly, and never mints its own identities — it only forwards the user's original JWT to UMP, which remains the final authority on process-level authorization (provider-level and process-level roles, `anonymous_access`).
- Because of this, UMP's existing role model (`{provider}` and `{provider}_{process_id}` Keycloak client roles) does not need to be duplicated or reimplemented in either the frontend or the MCP server.
