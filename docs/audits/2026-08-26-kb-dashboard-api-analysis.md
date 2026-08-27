## 1. **Surface inventory**

Critical finding: the v1 routes are declared, but the production composition at snapshot `b7cad232` does not bind `ctx.v1`. `makeSurfaceContext` merely copies `overrides.v1`, while both `buildApp()` and `start()` omit it; node routes also decline to register without node UID/map configuration. Therefore Atlas must treat v1 as unavailable until feature probes pass and use explicitly selected legacy routes meanwhile. `dashboard/server/http/surface.ts:399-404`, `dashboard/server/index.ts:192-207`, `dashboard/server/index.ts:574-581`, `dashboard/server/api/v1/routes.ts:195-200`

Envelope notation used below:

- `S(kind,data; meta)` = `{apiVersion:"v1", kind, data, meta:{etag?,watermark?,nextCursor?}, actions?}`.
- `E(code; meta)` = `{apiVersion:"v1", error:{code,message,retryable}, meta:{currentEtag?,currentWatermark?}}`.
- Unused metadata fields are absent, not `null`; `actions` is optional. `dashboard/server/api/v1/envelope.ts:5-27`, `dashboard/server/api/v1/envelope.ts:41-67`
- Conditional service reads return an empty `304` with the transport `ETag`; successful service responses also expose the ETag in the envelope. `dashboard/server/api/v1/routes.ts:376-404`
- `Idempotency-Key` syntax is 16–128 characters matching `[A-Za-z0-9_.:-]+`. Current routes validate that header but do not connect it to the defined v1 replay-record subsystem or pass its value onward; domain bodies often carry a separate `idempotencyKey`. `dashboard/server/api/v1/idempotency.ts:8-15`, `dashboard/server/api/v1/idempotency.ts:23-35`, `dashboard/server/api/v1/routes.ts:84-122`, `dashboard/server/api/v1/routes.ts:408-417`

`read` means authenticated operator read; `operator mutation` means authenticated operator mutation; `node` means the separately rate-limited, peer-UID- and host-map-authenticated node scope. `dashboard/server/api/v1/routes.ts:1-25`, `dashboard/server/http/surface.ts:578-612`

| Method | Path | Auth scope | Preconditions / request → response | Replay safety |
|---|---|---|---|---|
| GET | `/api/v1/agents` | read | Optional `If-None-Match`; `S(agent-list; watermark)` or 304. `dashboard/server/api/v1/routes.ts:433-439` | Idempotent |
| GET | `/api/v1/agents/:id` | read | Optional `If-None-Match`; `S(agent; etag=source hash)`. `dashboard/server/api/v1/routes.ts:441-447` | Idempotent |
| GET | `/api/v1/workflows` | read | Optional `If-None-Match`; `S(workflow-list; watermark)`. `dashboard/server/api/v1/routes.ts:449-455` | Idempotent |
| GET | `/api/v1/workflows/:id` | read | Optional `If-None-Match`; `S(workflow; etag)`. `dashboard/server/api/v1/routes.ts:457-463` | Idempotent |
| GET | `/api/v1/runs` | read | Query `cursor?`, `includeArchived=1?`; signed cursor is bound to the current watermark; `S(run-list; watermark,nextCursor)`. Malformed cursor 400, stale/signature failure 409. `dashboard/server/api/v1/routes.ts:465-489`, `dashboard/server/api/v1/cursor.ts:6-12` | Idempotent per watermark |
| GET | `/api/v1/runs/:runRef` | read | No conditional-header handling; `S(run; etag=run:<ref>:<version>)`. `dashboard/server/api/v1/routes.ts:491-509` | Idempotent snapshot |
| GET | `/api/v1/runs/:runRef/events` | read | Query `after?`, `limit=1..250?`, `stageRef?`; optional `If-None-Match`. `Accept:text/event-stream` writes one replay event and closes—it is not a live stream. `dashboard/server/api/v1/routes.ts:511-530`, `dashboard/server/services/runReadService.ts:74-93` | Idempotent replay |
| GET | `/api/v1/schedules` | read | Optional `If-None-Match`; `S(schedule-list; watermark=schedules:<revision>)`. `dashboard/server/api/v1/routes.ts:532-538` | Idempotent |
| GET | `/api/v1/inbox` | read | Query `refresh?`; `S(inbox)` without envelope revision metadata. `dashboard/server/api/v1/routes.ts:540-547` | Read, but `refresh` may invoke a bounded external refresh |
| GET | `/api/v1/health` | read | Optional `If-None-Match`; `S(health; aggregate watermark)`. That watermark may never be used as a mutation precondition. `dashboard/server/api/v1/routes.ts:549-555`, `dashboard/server/api/v1/contracts.ts:96-103` | Idempotent |
| GET | `/api/v1/deployments/:ref/inspect` | read | No request conditional passed to the port; `S(deployment; etag)`. `dashboard/server/api/v1/routes.ts:557-563` | Idempotent |
| GET | `/api/v1/asset-pulls/:intentRef/inspect` | read | No request conditional passed to the port; `S(asset-pull; etag)`. `dashboard/server/api/v1/routes.ts:565-571` | Idempotent |
| POST | `/api/v1/runs` | operator mutation | Body `{workflowId,idempotencyKey,expectedSourceRevision,composerRef?,parameters?}`; notably no `Idempotency-Key` header check. `S(run)`. `dashboard/server/api/v1/routes.ts:581-605`, `dashboard/server/services/launchService.ts:109-141` | Service-level body-key dedupe |
| POST | `/api/v1/agents` | operator mutation | Header `Idempotency-Key`; body builder fields plus `selector,project,expectedCollectionRevision,idempotencyKey`; `S(agent; etag)`. `dashboard/server/api/v1/routes.ts:608-615`, `dashboard/server/services/entityService.ts:12-20`, `dashboard/server/services/entityService.ts:96-103` | Body-key/domain dedupe; header itself is not replayed |
| PUT | `/api/v1/agents/:id` | operator mutation | Header `Idempotency-Key`; body builder fields plus `expectedSourceRevision,idempotencyKey`; no HTTP `If-Match`. `S(agent; etag)`. `dashboard/server/api/v1/routes.ts:617-627`, `dashboard/server/services/entityService.ts:109-116` | Body-key + source CAS |
| POST | `/api/v1/workflows` | operator mutation | Header `Idempotency-Key`; create body mirrors agent builder; `S(workflow; etag)`. `dashboard/server/api/v1/routes.ts:629-637`, `dashboard/server/services/entityService.ts:122-133` | Body-key/domain dedupe |
| PUT | `/api/v1/workflows/:id` | operator mutation | Header `Idempotency-Key`; body builder fields plus `expectedSourceRevision,idempotencyKey`; `S(workflow; etag)`. `dashboard/server/api/v1/routes.ts:639-648`, `dashboard/server/services/entityService.ts:139-146` | Body-key + source CAS |
| POST | `/api/v1/schedules` | operator mutation | Header `Idempotency-Key`; body `{owner,cadence,expectedCollectionRevision,idempotencyKey}`; `S(schedule; watermark)`. `dashboard/server/api/v1/routes.ts:650-658`, `dashboard/server/services/scheduleService.ts:34-36` | Body-key + collection CAS |
| POST | `/api/v1/schedules/:id/arm` and `/disarm` | operator mutation | Header `Idempotency-Key`; body `{expectedVersion,idempotencyKey,armed}`; `S(schedule; etag)`. `dashboard/server/api/v1/routes.ts:660-669`, `dashboard/server/services/scheduleService.ts:54-56` | Body-key + item CAS |
| DELETE | `/api/v1/schedules/:id` | operator mutation | Header `Idempotency-Key`; body `{expectedVersion,idempotencyKey}`; `S(schedule; etag)`. `dashboard/server/api/v1/routes.ts:671-678`, `dashboard/server/services/scheduleService.ts:62-64` | Body-key + item CAS |
| POST | `/api/v1/runs/:runRef/human-requests/:requestRef/respond` | operator mutation | Header `Idempotency-Key`; body `{expectedRevision,decision,idempotencyKey,response?,ceremonyId?,assertion?}`; decisions are `responded|approved|rejected|changes-requested`; `S(human-response)`. `dashboard/server/api/v1/routes.ts:680-692`, `dashboard/server/services/runReadService.ts:116-153` | Body-key + request CAS |
| POST | `/api/v1/deployments/:ref/confirm`, `/deploy`, `/abort`, `/close-ptys-and-continue` | operator mutation, T3 | Header `Idempotency-Key`; body must contain `ceremonyId` and `assertion`; `S(deployment; etag)`. `dashboard/server/api/v1/routes.ts:694-705` | Domain-dependent; v1 header is only syntactically checked |
| POST | `/api/v1/deployments/:ref/acknowledge` | operator mutation | Header `Idempotency-Key`; no route-level ceremony requirement; `S(deployment; etag)`. `dashboard/server/api/v1/routes.ts:707-713` | Domain-dependent |
| POST | `/api/v1/asset-pulls/:intentRef/pull` and `/retry` | operator mutation | Header `Idempotency-Key`; domain body; `S(asset-pull; etag)`. `dashboard/server/api/v1/routes.ts:715-724` | Domain-dependent |
| PUT | `/api/v1/hosts/:hostId` | node | First write requires `If-None-Match:*`; update requires `If-Match: host:<host>:<version>`; 428 if absent, 412 if stale. Body is a host advertisement excluding authoritative `hostId`; `S(host; etag)`. `dashboard/server/api/v1/routes.ts:223-256`, `dashboard/server/api/v1/routes.ts:317-335` | Conditional PUT; duplicate replay becomes stale rather than replaying |
| POST | `/api/v1/hosts/:hostId/leases/claim` | node | Body exactly `{waitMs}`, 0–25,000; 204 if no lease, otherwise `S(lease; etag)`. `dashboard/server/api/v1/routes.ts:258-278`, `dashboard/server/api/v1/contracts.ts:172-181` | Not replay-idempotent; server chooses/consumes claim |
| POST | `/api/v1/runs/:runRef/leases/renew` | node | Body `{expectedLeaseRevision>=1}`; `S(lease; etag)`. `dashboard/server/api/v1/routes.ts:280-298` | CAS-safe, not response-replayable |
| POST | `/api/v1/runs/:runRef/reports` | node | Body `{expectedLeaseRevision,sequence,kind,payload}`; kinds `started|event|gate-opened|completed|failed`; `S(report)` may contain `requestRef`. `dashboard/server/api/v1/routes.ts:300-313`, `dashboard/server/api/v1/contracts.ts:141-152` | Sequence-pinned, at-most-once effect |

The current router never supplies `actions`, despite the envelope and closed action vocabulary supporting them. `dashboard/server/api/v1/contracts.ts:106-138`, `dashboard/server/api/v1/routes.ts:382-399`

### Legacy/non-v1 routes still needed

| Need | Relevant route |
|---|---|
| Direct agent launch | `POST /api/agents/:id/launch`, body `{expectedSourceRevision,idempotencyKey}`. There is explicitly no shared/v1 service for this operation. `dashboard/server/agents/routes.ts:258-268`, `dashboard/server/agents/routes.ts:308-323` |
| Workflow launch fallback | `POST /api/workflows/:id/launch`. `dashboard/server/workflows/routes.ts:1229-1239` |
| Rich run monitoring and control | `GET /api/control/runs`, detail, attempt I/O, event replay and live `/events/stream`; mutations include reroute, messages, steer, stop, archive, successor and activate. `dashboard/server/control/routes.ts:920-968`, `dashboard/server/control/routes.ts:1108`, `dashboard/server/control/routes.ts:1452-1763` |
| Truly live global changes | Authenticated SSE `/events` and read-only WebSocket `/ws`. `dashboard/server/hub/index.ts:46-56`, `dashboard/server/hub/sse.ts:9-34`, `dashboard/server/hub/ws.ts:16-35` |
| Human-response fallback | Challenge and response routes under `/api/control/human-requests/:requestRef/respond`. `dashboard/server/control/routes.ts:1889`, `dashboard/server/control/routes.ts:1988` |
| Deployment/asset actions | `/api/inbox/deployment/:ref/{deploy,confirm,abort,acknowledge,close-ptys-and-continue}` and `/api/inbox/asset-pull/:intentRef/{pull,retry}`. `dashboard/server/inbox/routes.ts:394-483` |
| Code/docs browsing | `GET /api/kb/tree`, `/api/kb/file`, `/api/kb/history`; semantic/CLI search at `GET /api/brain/search`. `dashboard/server/kb/routes.ts:35-63`, `dashboard/server/brain/routes.ts:64-70` |
| Fleet/ledger analytics | `GET /api/index`; its payload includes card state, org state and ledger rollups including raw `grades` and `activity` rows. `dashboard/server/planeA/routes.ts:25`, `dashboard/server/planeA/indexer.ts:19-25`, `dashboard/server/planeA/indexer.ts:67-72`, `dashboard/server/planeA/ledgers.ts:153-169` |
| Transcripts | `GET /api/trace`, `/api/trace/:sessionId`. `dashboard/server/trace/routes.ts:174-176` |
| PTY/session management | WebSocket `GET /api/pty`, REST `GET /api/pty/sessions`, `DELETE /api/pty/sessions/:sessionId`. `dashboard/server/pty/route.ts:613-634` |
| Schedule fallback | Legacy `/api/schedules` GET/POST, arm/disarm and DELETE. `dashboard/server/schedules/service.ts:305-314` |

No registered HTTP “vibe session” route was found; the daemon only tracks/drains internal Composer/vibe processes during composition and shutdown. `dashboard/server/http/surface.ts:40-43`, `dashboard/server/http/surface.ts:567-576`

## 2. **Auth for an external local client**

There are two materially different modes.

### `win32-desktop`

A successful WebAuthn assertion is the only production session-mint path:

1. `POST /api/auth/assert/options` returns `{ceremonyId,options}`.
2. The client completes the WebAuthn ceremony.
3. `POST /api/auth/assert/verify` verifies the registered credential and returns `{token,expiresAt}` while setting `kb_session=<token>; HttpOnly; SameSite=Strict`. `dashboard/server/auth/routes.ts:209-220`, `dashboard/server/auth/routes.ts:222-280`
4. Present the token as either `Authorization: Bearer <token>` or the `kb_session` cookie. Bearer takes precedence. `dashboard/server/http/middleware.ts:27-49`
5. Default TTL is five minutes, configurable through `DASHBOARD_SESSION_TTL_MS`. Tokens are HMAC-SHA256 signed and stateless. `dashboard/server/auth/session.ts:5-10`, `dashboard/server/auth/session.ts:22-39`, `dashboard/server/auth/session.ts:137-161`

A non-browser process cannot legitimately mint a session merely because it runs locally: it still needs the interactive WebAuthn assertion. Reading the signing secret and calling `mintSession` would be credential handling and is not a production API. `dashboard/server/auth/session.ts:144-162`, `dashboard/server/auth/routes.ts:236-263`

The test harness bypass is test-only: it uses a fixed in-memory secret and calls `mintSession("operator", OP_SESSION)` directly to build `Authorization: Bearer …`. `dashboard/server/api/v1/_nodeHarness.ts:83-88`

### `tailnet`

The request transport is the credential. `requireSession`:

- Ignores any client-supplied bearer.
- Verifies the connection came from the trusted proxy UID.
- Requires the configured Tailscale login.
- Rejects cross-site browser requests.
- Mints a real signed session internally for downstream handlers on each accepted request. `dashboard/server/http/middleware.ts:72-129`, `dashboard/server/auth/tailnetOperator.ts:46-77`

A non-browser client is allowed to omit `Sec-Fetch-Site`, but it must connect through the approved tailnet proxy. A direct loopback process cannot forge the proxy socket’s operating-system owner, even if it forges every Tailscale header. `dashboard/server/auth/tailnetOperator.ts:17-24`, `dashboard/server/auth/peerUid.ts:1-15`, `dashboard/server/auth/peerUid.ts:207-227`

For Atlas, tailnet mode is safest because it holds no bearer at all. In WebAuthn mode, any separately approved implementation should keep the short-lived bearer only in process memory, inject it directly into the HTTP client, redact authorization/cookie headers before logging, never place it in command arguments or environment variables, and discard it at expiry. Persisting, retrieving or rotating it as a credential object is T4 and never unattended. `CLAUDE.md:28-31`, `governance/risk-tiers.md:9-10`

## 3. **Capability map**

The authorization categories are:

- **READ**: may execute instantly after authentication.
- **MUTATION**: Atlas must obtain an explicit voice confirmation for the exact verb and target.
- **T3**: requires the dashboard/WebAuthn approval channel; voice confirmation is not an approval token.
- **T4**: credentials-as-objects or real money; never unattended and should not be exposed as Atlas tools. `governance/risk-tiers.md:7-12`, `governance/risk-tiers.md:35-40`

The Atlas project contract is narrower: Atlas may only stage a T3 card from inbox to approvals after a separate post-TTS confirmation and may never commit an approval. No inspected v1 route implements that staging transition. `orgs/atlas-prep/contract.md:29-36`

| Daniel’s ask | Minimal route sequence | Classification / gap |
|---|---|---|
| Create an agent | `GET /api/v1/agents` → take `meta.watermark` → `POST /api/v1/agents` with both header and body idempotency keys plus `expectedCollectionRevision`. Until v1 wiring lands, use legacy `POST /api/agents`. `dashboard/server/api/v1/routes.ts:433-447`, `dashboard/server/api/v1/routes.ts:608-615`, `dashboard/server/agents/routes.ts:288-307` | **MUTATION**, normally branch/durable-PR work |
| Update an agent | Detail GET → use source ETag as `expectedSourceRevision` → PUT with a fresh body/header key. `dashboard/server/api/v1/routes.ts:617-627` | **MUTATION** |
| Launch a workflow | Workflow detail GET → `POST /api/v1/runs` with `{workflowId,expectedSourceRevision,idempotencyKey,parameters?}`; legacy fallback is `/api/workflows/:id/launch`. `dashboard/server/api/v1/routes.ts:581-605`, `dashboard/server/workflows/routes.ts:1229-1239` | **MUTATION**; confirm because execution may consume compute/model budget |
| Launch an agent directly | GET legacy agent detail → `POST /api/agents/:id/launch`. There is no v1 equivalent. `dashboard/server/agents/routes.ts:278-280`, `dashboard/server/agents/routes.ts:308-323` | **MUTATION**; explicit gap in v1 |
| Monitor runs/status | v1 list → detail → periodic event replay. For actual streaming use legacy `/api/control/runs/:runRef/events/stream` or authenticated hub `/events`; v1 SSE is one replay frame and closes. `dashboard/server/api/v1/routes.ts:465-530`, `dashboard/server/control/routes.ts:920-968` | **READ** |
| Resolve an inbox item | Read `/api/v1/inbox`; deployment/asset subjects have specific action routes. There is no universal v1 “resolve inbox item” or card inbox→approvals transition. `dashboard/server/api/v1/routes.ts:540-547`, `dashboard/server/api/v1/routes.ts:694-724` | **READ** for listing; **MUTATION/T3** by action; generic resolution is a gap |
| Answer a human request | Obtain request revision from run detail/event data, then POST response with expected revision and body key. T3 request kinds additionally need a fresh ceremony. `dashboard/server/api/v1/routes.ts:680-692`, `dashboard/server/services/runReadService.ts:129-153` | **MUTATION**; approvals/reviews are **T3** and Atlas’s current contract forbids committing them |
| Read run feedback/reviews | Run detail, event replay, attempt I/O and human-request records. `dashboard/server/api/v1/routes.ts:491-530`, `dashboard/server/control/routes.ts:925-968` | **READ**; no dedicated joined “feedback/reviews” v1 resource |
| Read grades | `GET /api/index` and inspect `ledgers.grades.rows`; optionally correlate with agent/run data client-side. `dashboard/server/planeA/indexer.ts:19-25`, `dashboard/server/planeA/ledgers.ts:153-169` | **READ**; no typed v1 grade endpoint |
| Dispatch a review agent | Launch a predeclared review workflow through `POST /api/v1/runs`, or use legacy direct-agent launch for a known reviewer. No generic `dispatch-review-agent(target)` API exists. `dashboard/server/api/v1/routes.ts:581-605`, `dashboard/server/agents/routes.ts:263-268` | **MUTATION**; bridge must use an allow-listed workflow mapping |
| Browse code/docs | `/api/kb/tree`, `/api/kb/file`, `/api/kb/history`, `/api/brain/search`. `dashboard/server/kb/routes.ts:35-63`, `dashboard/server/brain/routes.ts:64-70` | **READ**; entirely legacy |
| Analytics/reports | `/api/index`, `/api/home`, `/api/health`, `/api/routing`, `/api/trace`; generated dashboard/report files can be read through `/api/kb/file`. `dashboard/server/index.ts:248-268`, `dashboard/server/home/routes.ts:171-175`, `dashboard/server/routing/routes.ts:97` | **READ**; no general v1 reports endpoint |
| Schedules | List to get collection watermark; create with `expectedCollectionRevision`; arm/disarm/delete with item `expectedVersion`. `dashboard/server/api/v1/routes.ts:532-538`, `dashboard/server/api/v1/routes.ts:650-678` | List is **READ**; all changes **MUTATION** and future-action sensitive |
| PTY/session operations | Legacy WebSocket `/api/pty`, REST list/delete. `dashboard/server/pty/route.ts:613-634` | Listing **READ**; opening/writing/closing **MUTATION** and raw-shell sensitive |
| Credentials or paid actions | Auth registration/assertion and the separately registered paid-action path are outside the recommended bridge. `dashboard/server/http/surface.ts:586-590`, `dashboard/server/auth/routes.ts:157-280` | **T4**, never unattended |

Never perform automatic legacy fallback after an ambiguous mutation timeout or 5xx: that risks applying the same intent through two routes. Route selection must be fixed during connection negotiation; reads may safely retry or fall back.

## 4. **Change risk**

Snapshot: branch `claude/dashboard-v3`, HEAD `b7cad232`. HEAD advanced during this analysis. The latest 25 commits are predominantly behavior-preserving workover slices: store/type extraction, service layering fixes, shared HTTP reply helpers, shared hashing/decoders, migration cleanup and removal of dead exports. The newest relevant entries are `b7cad232`, `ae2f261b`, `29c76df0`, `ef1d7d51`, `a98fc134` and `f92628c4`. Source: `git log --oneline -25` at the final snapshot.

What is still moving:

- P4 owns Inbox shape, reconciliation, PR/escalation subjects and ledger-facing behavior. `docs/plans/2026-08-23-dv3-p4-plan.md:5-9`
- P5 owns deployment/asset-pull Inbox subjects, their actions and machine/release health rows. `docs/plans/2026-08-23-dv3-p5-plan.md:5-9`
- P6 owns v1, placement, host advertisements, node identity and service extraction. `docs/plans/2026-08-23-dv3-p6-plan.md:5-9`
- The current production v1 dependency wiring is absent, making `routes.ts`, `index.ts`, `surface.ts` and their service ports likely rebase hotspots. `dashboard/server/http/surface.ts:399-404`, `dashboard/server/index.ts:192-207`
- The latest Inbox layering commit makes Inbox/service adapter locations especially likely to move, even if behavior remains intended to stay identical. Source: commit `ae2f261b`.

Relatively stable contracts:

- `contracts.ts` declares the branded revision domains, closed action vocabulary and node DTOs as a frozen W0 contract. `dashboard/server/api/v1/contracts.ts:1-4`, `dashboard/server/api/v1/contracts.ts:11-30`
- `envelope.ts` explicitly defines builders only, with absent-not-null metadata. `dashboard/server/api/v1/envelope.ts:1-2`, `dashboard/server/api/v1/envelope.ts:37-67`
- `cursor.ts` fixes the signed four-field cursor and 400-versus-409 behavior. `dashboard/server/api/v1/cursor.ts:1-12`, `dashboard/server/api/v1/cursor.ts:15-27`
- P6’s plan says W0 freezes cross-language contracts before route verticals. `docs/plans/2026-08-23-dv3-p6-plan.md:7-9`

Do not infer Files/Docs support from the earlier P4 phase note: P6’s final hard wall explicitly excludes filesystem browsing and raw PTY spawn from v1. `docs/plans/2026-08-23-dv3-p4-plan.md:550-552`, `docs/plans/2026-08-23-dv3-p6-plan.md:561-563`

Connect-time fail-closed negotiation should be:

1. `GET /api/auth/context`; require exactly `win32-desktop` or `tailnet`. `dashboard/server/auth/routes.ts:152-155`
2. Authenticate, then read `/api/runtime/capabilities`; this describes runtime features, not the v1 route inventory. `dashboard/server/index.ts:244-268`
3. Probe `/api/v1/health`, `/api/v1/agents`, `/api/v1/workflows` and `/api/v1/runs`; require `apiVersion:"v1"` and the exact expected `kind`.
4. Enable each mutation family only if its paired read probe and expected metadata domain succeed.
5. A 404/503 means “feature unavailable,” not “empty.”
6. Reject unknown envelope versions, kinds, malformed ETags/watermarks and unexpected response bodies.
7. There is no `/api/v1/capabilities` or schema-hash advertisement in the present router; adding one should be a bridge prerequisite before removing legacy probes. `dashboard/server/api/v1/routes.ts:195-313`, `dashboard/server/api/v1/routes.ts:430-725`

## 5. **Recommended bridge shape**

Build a separately reviewed, standalone MCP package under `dashboard/`, then install its built artifact beside Atlas. Do not put the implementation in `Atlas/worker/kbbridge.py`.

That shape:

- Keeps dashboard HTTP semantics and compatibility code versioned with the server.
- Satisfies Atlas’s rule that it must not import kb modules or assume a checkout.
- Lets Atlas consume a narrow typed MCP surface while the package uses HTTP internally.
- Leaves dashboard authentication, authorization, CAS and WebAuthn as the real security boundary.
- Allows the package to choose v1 or an explicit legacy adapter at startup without retrying ambiguous mutations through another route.

Dormancy controls:

- Package absent from Atlas’s MCP registration by default.
- `ATLAS_KB_BRIDGE_ENABLED=0` by default; both registration and package startup require `1`.
- `ATLAS_KB_MUTATIONS_ENABLED=0` independently controls mutation-tool registration.
- T3 tools remain absent unless a separately reviewed `ATLAS_KB_T3_ENABLED=1` posture is active; each call still needs a fresh dashboard ceremony.
- No credential-registration, paid-action or raw-shell-writing tools.
- Pin one exact dashboard origin; in tailnet mode, use the tailnet proxy URL rather than direct loopback.
- Redact `Authorization`, `Cookie`, `Set-Cookie`, `assertion`, `ceremonyId` and response bodies marked sensitive before all logs.
- Persist no token. Persist only non-secret feature flags/schema version; keep cursors and ETags in bounded memory.

Recommended typed tools:

| Tool | Arguments | Policy |
|---|---|---|
| `kb_capabilities` | `{}` | instant |
| `kb_agents_list`, `kb_agent_get` | `{cursor?}`, `{agent_id}` | instant |
| `kb_agent_create`, `kb_agent_update` | typed builder body | confirm |
| `kb_workflows_list`, `kb_workflow_get` | `{cursor?}`, `{workflow_id}` | instant |
| `kb_workflow_create`, `kb_workflow_update` | typed builder body | confirm |
| `kb_workflow_launch` | `{workflow_id,parameters?,composer_ref?}` | confirm |
| `kb_agent_launch` | `{agent_id}` | confirm; explicit legacy adapter |
| `kb_runs_list`, `kb_run_get` | filters / `{run_ref}` | instant |
| `kb_run_events` | `{run_ref,after?,limit?,stage_ref?}` | instant |
| `kb_run_watch` | `{run_ref}` | instant; legacy live stream until v1 becomes live |
| `kb_inbox_list` | `{refresh?:boolean}` | instant |
| `kb_human_respond` | `{request_ref,expected_revision,decision,response?}` | confirm; refuse T3 without fresh passkey ceremony |
| `kb_review_dispatch` | `{review_profile,target_ref,context?}` | confirm; only map configured profiles to predeclared workflows |
| `kb_schedules_list` | `{}` | instant |
| `kb_schedule_create`, `kb_schedule_set_armed`, `kb_schedule_delete` | typed schedule/CAS args | confirm |
| `kb_deployment_inspect` | `{deployment_ref}` | instant |
| `kb_deployment_action` | `{deployment_ref,action}` | confirm; passkey for T3 verbs |
| `kb_asset_pull_inspect` | `{intent_ref}` | instant |
| `kb_asset_pull_action` | `{intent_ref,action:"pull"|"retry"}` | confirm |
| `kb_repo_tree`, `kb_repo_file`, `kb_repo_history` | `{path,...}` | instant |
| `kb_repo_search` | `{query,scope?}` | instant |
| `kb_analytics_snapshot` | `{sections?}` | instant |
| `kb_trace_list`, `kb_trace_get` | `{}`, `{session_id}` | instant |
| `kb_terminal_list` | `{}` | instant |
| `kb_terminal_close` | `{session_id}` | confirm |
| `kb_run_control` | `{run_ref,action,args}` | confirm; explicit allow-list only |

## 6. **Not covered**

- Production v1 ports and node identity are not wired at this snapshot; the declared surface cannot yet be Atlas’s sole backend.
- No live daemon calls, production credentials, WebAuthn ceremonies or VM configuration were exercised.
- Tests other than `_nodeHarness.ts` were deliberately not read, per the read budget.
- The PTY WebSocket frame protocol was not inventoried; only its registered HTTP/WS endpoints were mapped.
- There is no typed v1 endpoint for direct agent launch, grade analytics, code/docs browsing, generic review dispatch, generic inbox resolution, PTY control or vibe sessions.
- There is no v1 route-capability/schema advertisement.
- There is no legitimate unattended WebAuthn-session mint path for a local non-browser process in `win32-desktop` mode.
- There is no API implementing Atlas’s narrowly authorized inbox→approvals voice-staging transition.
- Run feedback, human-review state and grade ledgers are available through separate projections but not joined by a stable typed resource.
- Deployment and asset-pull replay guarantees are not established by the v1 router; its `Idempotency-Key` header is currently validation-only.
- Raw remote shell control, credential handling, paid actions, approval commits, merges and spending authority should remain outside the bridge.

--- codex-dispatch card 6a8fa093-e8ae49f5 | model gpt-5.6-sol | exit 0 | 1627s | ops publish: pushed | log: C:\Users\danie\AppData\Local\kb-codex-dispatch\logs\6a8f9a45-821a8e6e.jsonl | session 01a040f2-c620-78b0-8fed-7cf44ec55864 (follow up with --follow-up 01a040f2-c620-78b0-8fed-7cf44ec55864)
