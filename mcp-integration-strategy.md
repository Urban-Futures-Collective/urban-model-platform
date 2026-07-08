# MCP Implementation Strategy fuer UMP (JWT, Keycloak, dynamische Tools)

## Ziel
Diese Strategie beschreibt, wie ein MCP-Server in UMP integriert wird, so dass:

1. Authentifizierung und Autorisierung mit Keycloak/JWT sauber funktionieren.
2. Die Tool-Liste dynamisch aus den aktuell verfuegbaren UMP-Prozessen entsteht.
3. Die Architektur spaeter ohne Bruch in Richtung Hexagonal Architecture refaktorierbar bleibt.

## 1. Grundverstaendnis: Authentifizierung vs Autorisierung

### Authentifizierung (Wer bist du?)
- Der User loggt sich bei Keycloak ein.
- Keycloak stellt ein signiertes Access Token (JWT) aus.
- Das JWT enthaelt Claims wie `sub`, `exp`, `iss`, `aud`, Rollen in `realm_access` oder `resource_access`.

### Autorisierung (Was darfst du?)
- UMP liest Rollen aus dem JWT.
- UMP entscheidet auf Basis der Rollen, welche Prozesse sichtbar/ausfuehrbar sind.
- UMP nutzt bereits provider- und process-spezifische Rollen (`provider`, `provider_process`).

Wichtig: Keycloak verwaltet Identitaeten und Rollen. Die fachliche Regel, welcher Prozess welche Rolle braucht, bleibt in UMP.

## 2. Typischer Ziel-Flow mit MCP

Empfohlenes Zielbild:

1. Client authentifiziert User bei Keycloak.
2. Client ruft UMP/MCP mit `Authorization: Bearer <JWT>` auf.
3. UMP und MCP pruefen Token jeweils selbst (Zero Trust).
4. MCP leitet dasselbe JWT an UMP weiter.
5. UMP entscheidet final ueber Prozesszugriff.
6. MCP exponiert nur Tools, die fuer diesen User erlaubt sind.

Damit gilt: einmal Login, aber verteilte, unabhaengige Sicherheitspruefung pro Dienst.

## 3. Sicherheitsmodell: Variante 2 (Zero Trust) als Standard

Es gibt zwei typische Muster:

- Variante 1: Nur UMP validiert JWT, MCP vertraut blind.
- Variante 2: UMP und MCP validieren JWT jeweils selbst.

Fuer UMP wird Variante 2 empfohlen.

Gruende:
- Robust gegen Fehlkonfigurationen zwischen Services.
- Jeder Dienst hat eine klare Security-Verantwortung.
- Besser auditierbar.
- Standard in modernen Service-Architekturen.

## 4. Dynamischer MCP statt statischer Tool-Definitionen

Die UMP-Prozesslandschaft aendert sich laufend (Provider kommen/gehen, Prozesse aendern sich). Daher darf MCP keine hart codierte Tool-Liste verwenden.

### Zielprinzip
- UMP ist Source of Truth fuer:
  - verfuegbare Provider/Prozesse
  - Prozessbeschreibung
  - Input-Parameter/Input-Schema
  - Rollenbasierte Sichtbarkeit
- MCP ist eine Uebersetzungsschicht von UMP-Prozessen zu MCP-Tools.

### Empfohlener Discovery-Endpoint in UMP
Einfuehrung eines dedizierten Endpunkts, z. B.:

- `GET /mcp/tools`

Antwort pro User-token gefiltert, z. B.:

- `tool` (z. B. `bikebox.plan_network`)
- `title`
- `description`
- `inputSchema` (JSON Schema)
- optional `examples`, `outputHints`, `provider`, `processId`

Wichtig: Dieselbe Anfrage liefert je nach Userrollen unterschiedliche Tool-Listen.

## 5. Empfohlene Laufzeitlogik im MCP-Server

### Tool Discovery
1. JWT vom Client empfangen.
2. JWT lokal validieren (JWKS mit Cache).
3. `GET /mcp/tools` mit demselben JWT gegen UMP.
4. Antwort in MCP-Tools transformieren.
5. Nur diese Tools fuer die Session sichtbar machen.

### Tool Execution
1. MCP-Tool-Aufruf kommt mit Parametern.
2. MCP validiert Parameter gegen `inputSchema`.
3. MCP ruft UMP-Prozessausfuehrung auf (`POST /processes/{id}/execution`) mit demselben JWT.
4. UMP prueft erneut Rollen/Fachregeln.
5. Ergebnis wird fuer MCP normalisiert zurueckgegeben.

So entstehen neue KI-Faehigkeiten automatisch, sobald neue Prozesse in UMP verfuegbar sind.

## 6. JWT/Keycloak Implementierungsdetails

### Tokenpruefung im MCP
- Issuer (`iss`) muss zum erwarteten Realm passen.
- Audience (`aud`) muss geprueft werden (oder klar dokumentiertes alternatives Modell).
- Ablaufzeit (`exp`) und optional `nbf` pruefen.
- Signatur via Keycloak-JWKS pruefen.

JWKS-URL (typisch):
- `/realms/{realm}/protocol/openid-connect/certs`

### Tokenweitergabe
- Immer das originale User-JWT an UMP weiterreichen.
- Keine synthetischen User-Identitaeten erzeugen.
- Kein Zugriff des MCP direkt auf UMP-DB.

## 7. UMP-Rollenmodell im MCP korrekt abbilden

UMP hat heute bereits:
- anonyme Sichtbarkeit je Prozess (`anonymous_access`)
- provider-level Rollen
- process-level Rollen
- Job/Ensemble Zugriff ueber Owner/Sharing

Empfehlung:
- Rollenlogik nicht im MCP duplizieren.
- MCP nutzt UMPs gefilterte Discovery (`/mcp/tools`) als Wahrheit.
- UMP bleibt finale Autorisierungsinstanz fuer Execution.

## 8. Architekturempfehlung fuer Clients und SDKs

Fuer mehrere Clients (Nuxt, MCP, CLI) sollte dieselbe UMP-API genutzt werden.

Kurzfristig:
- MCP nutzt einen Python-Client/Adapter fuer UMP-REST.
- Frontend nutzt TypeScript-Client.

Langfristig:
- OpenAPI-Spezifikation stabilisieren.
- SDKs (TS/Python) daraus generieren.
- MCP bleibt duenn: Tool-Discovery + Tool-Execution Mapping.

## 9. Bezug zur kuenftigen Hexagonal Architecture

MCP sofort mit Ports/Adapters strukturieren:

- Ports:
  - `ToolCatalogPort`
  - `ToolExecutionPort`
  - `IdentityValidationPort`
  - `AuthorizationContextPort`

- Adapter v1:
  - `UmpHttpToolCatalogAdapter`
  - `UmpHttpExecutionAdapter`
  - `KeycloakJwtValidationAdapter`

- Adapter spaeter (bei UMP-Refactor):
  - direkter Application-Service Adapter statt HTTP, ohne Aenderung der MCP-Toolvertraege.

Damit bleibt MCP stabil, obwohl UMP intern refaktoriert.

## 10. Incremental Roadmap

### Phase 0 - Security und Contracts
- Threat Model und Trust Boundaries definieren.
- Claim-Policy (`iss`, `aud`, Rollenclaims) festlegen.
- MCP-Fehlerformat und Logging-Standards definieren.

### Phase 1 - Dynamische Read-Only Tools
- `GET /mcp/tools` in UMP bereitstellen.
- MCP Discovery + Tokenvalidation + Tokenforwarding umsetzen.
- Read-only Tools (z. B. Prozesse listen/details, Jobstatus lesen).

### Phase 2 - Execution
- Prozessausfuehrung als dynamische MCP-Tools.
- Input-Validierung via Schema.
- Timeouts, Retry-Regeln, Idempotenzstrategie.

### Phase 3 - Kollaboration
- Optional Jobs teilen, Kommentare, Ensembles.
- Deployment-spezifische Allow-Lists fuer schreibende Tools.

### Phase 4 - Hexagonal Hardening
- MCP intern strikt auf Ports/Adapters ziehen.
- OpenAPI-basierte SDK-Generierung etablieren.

## 11. Teststrategie (Pflicht)

- Parity-Tests: MCP-Ergebnis muss mit direktem UMP-API-Aufruf (gleiches JWT) uebereinstimmen.
- Rollenmatrix-Tests:
  - ohne Token
  - gueltiges Token ohne relevante Rolle
  - provider-Rolle
  - process-Rolle
  - Job-owner vs shared-user
- Negative Tests: abgelaufenes Token, falscher Issuer, falsche Audience, manipulierte Signatur.
- Dynamik-Tests: neuer Provider erscheint/verschwindet -> Toolliste aktualisiert sich ohne Codeaenderung.

## 12. Konkrete Entscheidungen fuer den Start

1. Zero-Trust-Modell verwenden: JWT in UMP und MCP validieren.
2. MCP dynamisch bauen, keine hart codierten Prozess-Tools.
3. UMP als Source of Truth fuer Toolkatalog und Autorisierung nutzen.
4. V1 als externer MCP-Service (Sidecar) umsetzen.
5. OpenAPI/SDKs als strategischen Ausbau einplanen, aber nicht als Blocker fuer V1.


