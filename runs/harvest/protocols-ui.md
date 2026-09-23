# Cluster: `protocols-ui` — agent interoperability protocols & agent-UI integration

**Target:** `/home/decrux/Code/hermes-day` (Hermes Agent plugin harness: typed gate, context scoring, manifest, router, gotchas; desktop cockpit; unified decision ledger).
**Question answered:** what must this harness implement to be *cross-native* — cross-protocol (MCP / A2A / AG-UI), cross-surface (desktop / TUI / Discord), cross-provider.
**Companion data:** `protocols-ui.json` — 32 patterns, each with `{name, framework, source, what, why, build, effort, test, confidence}`, 53 distinct fetched source URLs.

## Counts per framework / spec

| Framework / spec | Patterns |
|---|---|
| MCP (2025-06-18 core) | 6 |
| MCP 2026-07-28 (+ official extensions) | 6 |
| A2A v1.0 | 5 |
| AG-UI | 5 |
| CopilotKit | 3 |
| OpenTelemetry GenAI semantic conventions | 3 |
| OpenAI Realtime API | 2 |
| 2026 protocol registries | 2 |
| **total** | **32** |

Confidence: 26 high / 6 medium. Effort: 21 M / 10 L / 1 S.

## The shape of the answer

Four protocol families, and they do **not** overlap:

- **MCP** — agent ↔ tool. Tools, resources, prompts, roots, sampling, elicitation. As of the **2026-07-28 revision** it is stateless: no `initialize` handshake, no session id, capabilities travel per-request in `_meta`, and server→client requests are replaced by **Multi Round-Trip Requests**.
- **A2A** — agent ↔ agent. Agent Cards, `contextId`/`taskId`, an eight-state task machine, SSE streaming with `append`/`lastChunk` artifact chunking, and webhook push notifications with JWT/JWKS.
- **AG-UI** — agent ↔ user. A typed event catalogue, RFC 6902 JSON-Patch shared state, and an interrupt/resume contract that is the closest thing in any spec to hermes-day's actual job.
- **OTel GenAI + Realtime** — the cross-provider vocabulary (spans/events/metrics, plus an MCP trace-context propagation rule) and the voice surface.

A harness is cross-native when one internal event model, one trust vocabulary, and one gate pipeline sit underneath all four — not when it has four adapters.

## Load-bearing mechanisms, per spec

### MCP (core, 2025-06-18)
1. **`tools/list` + `tools/call`** — cursor pagination (`cursor` → `nextCursor`); `isError` on the result is a *different* thing from JSON-RPC `-32602`/`-32601`. `MCP-Protocol-Version` header required on every post-init request. <https://modelcontextprotocol.io/specification/2025-06-18/server/tools>
2. **Resources** — `resources/list`, `resources/templates/list` (RFC 6570), `resources/read` → `{text}` or `{blob}`; `subscribe`/`listChanged` are independent optional capabilities; missing resource is `-32002`. <https://modelcontextprotocol.io/specification/2025-06-18/server/resources>
3. **Prompts** — the spec's own model is a *user-invoked slash command* with typed `arguments[]` and a `-32602` on missing required args. <https://modelcontextprotocol.io/specification/2025-06-18/server/prompts>
4. **Roots** — `roots/list` is a *server→client request*; client declares `roots:{listChanged:true}` and pushes `notifications/roots/list_changed`. This is protocol-level blast-radius scope. **Deprecated by SEP-2577 in 2026-07-28.** <https://modelcontextprotocol.io/specification/2025-06-18/client/roots>
5. **Sampling + elicitation** — `sampling/createMessage` and `elicitation/create` (`mode:'form'`, `requestedSchema`, `ElicitResult{action:'accept'|'decline'|'cancel'}`). Both are the reverse channel, and both are replaced by MRTR in 2026. <https://modelcontextprotocol.io/specification/2025-06-18/client/sampling> · <https://modelcontextprotocol.io/specification/2025-06-18/client/elicitation>
6. **Streamable HTTP + auth** — one endpoint, `Accept: application/json, text/event-stream`, `Mcp-Session-Id`; the server is an OAuth 2.1 **resource server** publishing RFC 9728 metadata, reached via `401` plus a `WWW-Authenticate` pointer to `resource_metadata`. <https://modelcontextprotocol.io/specification/2025-06-18/basic/transports> · <https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization>

### MCP 2026-07-28 — the revision that changes harness design
7. **Stateless + `server/discover`** — `initialize` and `Mcp-Session-Id` removed. Every request carries `_meta['io.modelcontextprotocol/protocolVersion' | '/clientCapabilities' | '/clientInfo']`. `server/discover` MUST be implemented, is cacheable (`ttlMs`, `cacheScope`), returns `supportedVersions` + `capabilities` + `instructions`; mismatch → `UnsupportedProtocolVersionError`. `serverInfo` is self-reported and MUST NOT drive security decisions. <https://modelcontextprotocol.io/specification/2026-07-28/server/discover> · <https://modelcontextprotocol.io/specification/2026-07-28/changelog>
8. **MRTR** — `InputRequiredResult{resultType:'input_required', inputRequests:{key:{method,params}}, requestState?}`; the client retries the *original* request with `inputResponses` keyed identically, a **new JSON-RPC id**, and `requestState` echoed byte-for-byte and never parsed. <https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr>
9. **`subscriptions/listen`** — one long-lived stream with a negotiated filter (`toolsListChanged`, `promptsListChanged`, `resourcesListChanged`, `resourceSubscriptions[]`). Server MUST send `notifications/subscriptions/acknowledged` **first**, carrying `_meta['io.modelcontextprotocol/subscriptionId']`; every later notification carries that id; concurrent subscriptions demultiplex by it; stdio reconnect requires re-sending the filter. <https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/subscriptions>
10. **Extensions** — `{reverse-dns}/{name}` identifiers, client set in `_meta['io.modelcontextprotocol/clientCapabilities'].extensions`, server set in the discover response. Extensions MUST NOT remove/rename fields, change types, alter semantics, or add required fields. <https://modelcontextprotocol.io/extensions/overview>
11. **MCP Apps (`io.modelcontextprotocol/ui`)** — HTML-only MVP, MIME `text/html;profile=mcp-app`, sandboxed iframe, JSON-RPC bridge (explicitly *not* a host-injected global API object), predeclared templates, consent for UI-initiated tool calls. <https://modelcontextprotocol.io/seps/1865-mcp-apps-interactive-user-interfaces-for-mcp>
12. **Tasks + Skills extensions** — Tasks: poll `tasks/get`, input via `tasks/update`, cancel via `tasks/cancel`; `tasks/result` and `tasks/list` are **removed** (`-32601`), results are polymorphic, task IDs MUST be unguessable, authz re-checked per request. Skills: `skills/list`/`skills/get` plus `resources/directory/read` serving the agentskills.io SKILL.md format with lazy file retrieval. <https://modelcontextprotocol.io/seps/2663-tasks-extension> · <https://modelcontextprotocol.io/seps/2640-skills-extension>

### A2A v1.0
13. **Agent Card** — identity, `url`, `capabilities{streaming, pushNotifications, extendedAgentCard}`, `skills[]`, auth schemes; well-known-URI / registry / direct discovery; optional JWS signature (RFC 7515 + JCS RFC 8785); ETag from `version` or content hash. <https://a2a-protocol.org/latest/topics/agent-discovery/>
14. **Task lifecycle** — `contextId` groups, `taskId` identifies. Exact `TaskState` enum: `TASK_STATE_UNSPECIFIED, TASK_STATE_SUBMITTED, TASK_STATE_WORKING, TASK_STATE_COMPLETED, TASK_STATE_FAILED, TASK_STATE_CANCELED, TASK_STATE_REJECTED, TASK_STATE_INPUT_REQUIRED, TASK_STATE_AUTH_REQUIRED` — terminal = COMPLETED/FAILED/CANCELED/REJECTED, interrupted = INPUT_REQUIRED/AUTH_REQUIRED (the two "waiting on you" states). `returnImmediately` chooses blocking vs non-blocking; message to a terminal task → `UnsupportedOperationError`. <https://a2a-protocol.org/latest/topics/life-of-a-task/>
15. **Streaming** — `SendStreamingMessage` → initial Task|Message then `TaskStatusUpdateEvent` / `TaskArtifactUpdateEvent{append?, lastChunk?}`. `append:true` concatenates into the same `artifactId`; `SubscribeToTask` re-attaches after a drop. Check `capabilities.streaming` first. <https://a2a-protocol.org/latest/topics/streaming-and-async/>
16. **Push notifications** — `TaskPushNotificationConfig{url (required), token?, authentication?}`; the notification is `POST {url}` with `Content-Type: application/a2a+json` and a body that is a `StreamResponse` carrying exactly one of `task|message|statusUpdate|artifactUpdate`. Recommended auth is an asymmetric JWT (iss/aud/iat/exp/jti/taskId) verified against a JWKS, with timestamp freshness and `jti` replay rejection. <https://a2a-protocol.org/latest/specification/#43-push-notification-objects>
17. **v1.0 wire contract** — PascalCase methods (`message/send`→`SendMessage`, `tasks/resubscribe`→`SubscribeToTask`, …), unified `Part{text|raw|url|data}` (breaking), discriminator-based stream events, `google.rpc.Status` errors, ISO 8601, cursor pagination, `A2A-Version` + `A2A-Extensions` headers. <https://a2a-protocol.org/latest/whats-new-v1/>

### AG-UI + CopilotKit
18. **Event catalogue** — Lifecycle (`RUN_STARTED/FINISHED/ERROR`, `STEP_*`), Text (`TEXT_MESSAGE_START/CONTENT/END/CHUNK`), Tool (`TOOL_CALL_START/ARGS/END/RESULT/CHUNK`), State (`STATE_SNAPSHOT/DELTA`, `MESSAGES_SNAPSHOT`), Activity, Reasoning, Subagent, `RAW`/`CUSTOM`. Three flow patterns: Start-Content-End, Snapshot-Delta, Started-Finished|Error. Agent interface is `run(RunAgentInput) -> Observable<BaseEvent>`. <https://docs.ag-ui.com/concepts/events>
19. **Shared state** — `STATE_SNAPSHOT` **replaces** the whole model; `STATE_DELTA` is an array of RFC 6902 ops with RFC 6901 JSON Pointers applied in sequence, non-mutatingly, with failure logged rather than corrupting state; resync by requesting a fresh snapshot. <https://docs.ag-ui.com/concepts/state>
20. **Interrupts** — `RUN_FINISHED.outcome = {type:'interrupt', interrupts:[{id, reason, message?, toolCallId?, responseSchema?, expiresAt?, metadata?, subagentRunId?}]}`. Resume on the **same threadId** with `resume:[{interruptId, status:'resolved'|'cancelled', payload?, metadata?}]`; the resume array must address **every** open interrupt; denial goes *inside* `payload` (`{approved:false}`), not in `status`; expired interrupts produce `RunError`; `metadata` reserves the `ag-ui` key and can carry a signed human-decision envelope. <https://docs.ag-ui.com/concepts/interrupts>
21. **Capabilities** — `getCapabilities()` returning `{identity, transport, tools, output, state, multiAgent, reasoning, multimodal, execution, humanInTheLoop, custom}`. Discovery only, dynamic, optional, and **absent means unknown, not unsupported** — clients gate on explicit equality. <https://docs.ag-ui.com/concepts/capabilities>
22. **Subagents** — `SubagentStarted{subagentRunId, name, parentSubagentRunId?, parentToolCallId?, parentMessageId?}`, `SubagentFinished{result?, outcome?}`, `SubagentError`. `subagentRunId` is a **per-invocation** handle, never a reusable definition; a suspended subagent emits `outcome:{type:'suspended', interruptIds}` and MAY reuse its id on resume, which the client must treat as a continuation. <https://docs.ag-ui.com/concepts/subagents>
23. **CopilotKit `useAgent` / `useAgentContext`** — `useAgent(options?)` → `{agent, isReady}`; binding is either the shared registry instance or the private-agent triple `{agentId, runtimeAgentId, threadId}` (partial combinations are type errors); `updates:[UseAgentUpdate.OnStateChanged]` throttles re-renders; `isReady` false means the returned agent is a provisional stand-in. `useAgentContext` is the one-way, UI-owned, auto-unregistering read-only channel. Server side: `copilotkit_emit_state` and `StateStreamingMiddleware(StateItem(state_key, tool, tool_argument))` — **the tool argument name must equal `state_key`**. `useCoAgent` now resolves to `useAgent`. <https://docs.copilotkit.ai/reference/v2/hooks/useAgent> · <https://docs.copilotkit.ai/langgraph-python/shared-state>
24. **CopilotKit HITL hooks** — `useHumanInTheLoop` registers a handler-less interactive tool with status machine `InProgress → Executing → Complete` and a single-call `respond`; the agent stays paused until it fires. `useInterrupt` handles `RUN_FINISHED` interrupts (and the legacy `on_interrupt` custom event, standard taking precedence), exposes `interrupt` + `interrupts`, accumulates one response per open interrupt, starts the resume only when all are addressed, and refuses to resume expired ones. <https://docs.copilotkit.ai/reference/v2/hooks/useHumanInTheLoop> · <https://docs.copilotkit.ai/reference/v2/hooks/useInterrupt>
25. **Generative UI** — AG-UI is the *connection*, not the spec. Three siblings: **A2UI** (declarative, JSONL-streaming, `application/a2ui+json`; v0.9.1 current, v1.0 candidate adds `actionResponse` + action IDs + `surfaceProperties`; catalog-restricted so agents can only compose client-approved components), **MCP-UI** (iframe-based, extends MCP), **Open-JSON-UI**. A2UI transports: A2A extension (stable), AG-UI (stable), REST/WS/SSE proposed. <https://docs.ag-ui.com/concepts/generative-ui-specs> · <https://a2ui.org/> · <https://a2ui.org/concepts/transports/> · <https://github.com/MCP-UI-Org/mcp-ui>

### OTel GenAI + Realtime
26. **Inference spans** — `gen_ai.inference.client`, span name `{gen_ai.operation.name} {gen_ai.request.model}`, required `gen_ai.operation.name` + `gen_ai.provider.name` (a discriminator selecting provider-specific attributes), one span across retries, `gen_ai.response.time_to_first_chunk` for streaming, opt-in three-tier content capture with fixed JSON schemas. <https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/gen-ai/gen-ai-spans.md>
27. **Agent spans** — `gen_ai.create_agent.client` with a **stable** `gen_ai.agent.id` (registry ARN/URN; in-memory ids explicitly not recommended), plus `invoke_agent` / `invoke_workflow` / `plan` / `retrieval` / `fetch_response` (no usage reported). Tool calls use `execute_tool`; an MCP instrumentation detecting outer GenAI tool tracing SHOULD annotate the existing span, not create a second one. <https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/gen-ai/gen-ai-agent-spans.md>
28. **Events + MCP conventions** — `gen_ai.client.inference.operation.details` (opt-in) and `gen_ai.evaluation.result`; MCP takes precedence over generic RPC/HTTP conventions; **W3C Trace Context is injected into MCP `params._meta` UNPREFIXED** (`traceparent`, `tracestate`, `baggage` — an explicit exception to `_meta`'s DNS-prefix rule, per SEP-414), received as a remote parent with the ambient HTTP context attached as a **link**; MCP spans `{mcp.method.name} {target}`; metrics `mcp.client/server.operation.duration`, `mcp.client/server.session.duration`. <https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/gen-ai/mcp.md> · <https://modelcontextprotocol.io/community/seps/414-request-meta>
29. **Realtime event model** — every message is a typed event with an explicit `type`; client events (`session.update`, `input_audio_buffer.append/commit/clear`, `conversation.item.create`, `conversation.item.truncate`, `response.create/cancel`) and server events (`session.created`, `conversation.item.created`, `input_audio_buffer.speech_started/stopped/committed/cleared/dtmf_event_received`, `conversation.item.input_audio_transcription.completed`, `response.*`). `session.update` carries `instructions`, `voice`, `tools` (function + JSON-Schema), `turn_detection` (`server_vad` with `prefix_padding_ms`/`silence_duration_ms`/`threshold`, or `null`), `input_audio_transcription`, `max_output_tokens`. <https://developers.openai.com/api/docs/api-reference/realtime-server-events> · <https://developers.openai.com/api/docs/guides/realtime-conversations>
30. **Realtime interruption + MCP approval** — cancel *and* truncate at the samples actually played so the model's memory matches what the human heard; WebRTC/SIP additionally needs `input_audio_buffer.clear` and `output_audio_buffer.clear`. The Realtime model set includes a `realtime_mcp_approval_request` — MCP tool calls inside a voice session carry an approval step in the event model. <https://developers.openai.com/api/docs/guides/realtime-conversations> · <https://developers.openai.com/api/docs/api-reference/realtime-server-events>

### 2026 registries
31. **MCP Registry** — `server.json` metadata, reverse-DNS server names (`io.github.user/server-name`), namespace ownership proven by GitHub/DNS/HTTP challenge, published OpenAPI surface, downstream aggregators (hosts are *not* meant to hit it directly; ~hourly pull), private servers self-hosted. <https://modelcontextprotocol.io/registry/about>
32. **GCP Agent Registry + AGNTCY / OASF** — GCP: agents, endpoints, MCP servers, tools, bindings, skills with revisions, publisher review, RBAC, audit logging, and its own Agent Registry MCP server; its agent identifier is a URN (the same value the OTel agent-spans doc uses for `gen_ai.agent.id`). AGNTCY (Linux Foundation): federated **Agent Directory Service**, **OASF** (OCSF-inspired record model with skills/domains/modules, explicitly spanning A2A *and* MCP), **SLIM** messaging, decentralized Identity (verifiable credentials), SHADI runtime. <https://docs.cloud.google.com/agent-registry/concepts> · <https://docs.agntcy.org/> · <https://docs.agntcy.org/oasf/open-agentic-schema-framework/>

## Cross-cutting mechanisms worth building once

These recur across specs; implementing them once is what makes the harness cross-native rather than adapter-shaped.

1. **One internal event model.** AG-UI's catalogue is the best available vocabulary; the Realtime event grammar maps onto it, MCP notifications map onto it, A2A `StreamResponse` maps onto it. Everything else (cockpit rendering, ledger recording, telemetry) consumes the reducer output. Adapters translate *into* the model; nothing reads a raw protocol payload downstream.
2. **One interrupt/approval object.** AG-UI's `Interrupt` + `resume[]` completeness rule, A2A's `INPUT_REQUIRED`/`AUTH_REQUIRED`, MCP's elicitation `ElicitResult` and MRTR `inputResponses`, and Realtime's MCP approval request are four encodings of one thing. One `Interrupt` type, four codecs, one `Needs you` queue. The completeness rule ("resume only when every open interrupt is addressed") must be enforced centrally.
3. **One trust vocabulary.** MCP registry namespace verification, A2A card JWS signatures, GCP publisher review, and OASF validation all answer "who published this, and has anyone checked". Normalize to `{publisher, verification_method, verified}` and render one chip.
4. **One capability document per surface.** `getCapabilities()` per surface (desktop / TUI / Discord) and per connected agent, with **absent = unknown** discipline. Every affordance gates on explicit truthiness; a degraded surface declares a smaller catalogue instead of silently rendering nothing.
5. **One version-negotiation helper.** MCP `_meta.protocolVersion` + extensions map, A2A `A2A-Version` + `A2A-Extensions`, and AG-UI's capability snapshot are the same conversation. One `negotiate()` implementation emitting the right headers/keys per protocol.
6. **One gate origin tag.** Every inbound thing that can cause a write or a spend — MCP tool call, MCP App UI action, A2A task, generated-UI action, voice tool call — becomes a gate decision record with an `origin` field. This is what makes the ledger an audit trail rather than a log.

## Traps (each is a real, specified behavior — not a hypothetical)

- `isError: true` on an MCP tool result is **not** a JSON-RPC error (`-32602`/`-32601`); conflating them hides the difference between "no such tool" and "the tool ran and failed".
- MCP 2026 is **stateless**: caching capabilities from a handshake will silently mis-negotiate. `serverInfo` is self-reported — never a security input.
- `requestState` in MRTR must be echoed **byte-for-byte and never parsed**. Parsing it is a spec violation, not an optimization.
- `notifications/subscriptions/acknowledged` MUST be the first message on a listen stream; dispatching before it is a race the spec closes explicitly.
- MCP task IDs are **bearer tokens** — they must not leak into logs, and `tasks/list` is gone precisely so nobody can enumerate them.
- Trace context in MCP `_meta` is written **unprefixed** — the one documented exception to the DNS-prefix rule. DNS-prefixing `traceparent` breaks propagation silently.
- A2A `append:true`/`lastChunk:true` are what make one artifact out of N events; ignoring them renders a long artifact as N disconnected blobs. `SubscribeToTask` (not a fresh send) is the reconnect path.
- A2A push notifications are a **webhook authenticated by a header** — the highest-risk surface in the cluster. Default to reject when `authentication` is absent; verify the JWT signature *and* its claims; dedupe on `jti`.
- A2A v1.0's unified `Part` and PascalCase method names are **breaking**; a v0.3.0-shaped payload must be reported as version skew, not parsed on a hope.
- AG-UI `STATE_SNAPSHOT` **replaces**, it does not merge. A delta whose Nth op is invalid must leave state untouched (all-or-nothing), or the board desyncs from the agent.
- AG-UI denial lives in `payload` (`{approved:false}`), never in `status`; `status:'cancelled'` means the human abandoned, which is a different fact in the audit trail.
- AG-UI `subagentRunId` is **per invocation** — persisting anything under it creates state keyed by a meaningless value. `name` is the reusable key.
- CopilotKit: binding a runtime agent under a per-hook `threadId` is a type error for a reason — runtime agents are singletons and two hooks would clobber each other's threads.
- CopilotKit `StateStreamingMiddleware` indexes partial args by `state_key`; if the tool argument name ≠ `state_key`, the streamed state silently never lands.
- Realtime: cancelling without **truncating at the played sample count** leaves the model remembering words the human never heard.
- Generative UI: keep the component catalog **closed**. An unknown component type renders a labeled placeholder, never raw HTML.

## Unreachable / do-not-guess

- `https://open-json-ui.org/` — fetch blocked ("URL targets a private or internal network address"). No authoritative OpenAI Open-JSON-UI spec page was reachable. **Do not implement against a guessed schema**; A2UI (v0.9.1) and MCP Apps are the reachable declarative-UI specs and cover the same need.
- `https://docs.copilotkit.ai/coagents/generative-ui/agentic` — returns a client-side error shell. Current generative-UI docs live under `/langgraph-python/*` and the v2 reference tree.
- `https://docs.copilotkit.ai/reference/hooks/useCoAgent` — serves the `useAgent` reference; `useCoAgent` is superseded in the v2 SDK.

## Suggested build order

1. `hday_events.py` — the internal event model + reducer (unblocks everything; pure Python, no network).
2. `hday_interrupts.py` — one `Interrupt` type + the resume-completeness rule (this is the product).
3. `hday_state.py` — shared day state with snapshot/delta + the UI-authoritative vs agent-authoritative key split.
4. `hday_protocols.py` — MCP client (discover-first, MRTR, listen-stream) and A2A client (card, task machine, stream, push receiver).
5. `hday_telemetry.py` — OTel GenAI spans/events/metrics + the MCP trace-context rule, joined to the ledger.
6. `hday_registry.py` — one `RemoteEntity` normalizer over MCP registry / GCP registry / AGNTCY-OASF / A2A cards.
7. `desktop/plugin.js` — capability-gated cockpit rendering, sandboxed MCP-App iframe, closed-catalog A2UI renderer.

Each step is testable without the next; the pytest suite named in each pattern's `test` field is the acceptance criterion.
