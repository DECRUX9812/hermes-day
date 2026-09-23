# Harvest — ranked

154 patterns across 5 clusters.

## Theme histogram

- **observability** — 32 patterns
- **memory-context** — 25 patterns
- **interop** — 23 patterns
- **orchestration** — 18 patterns
- **durable-state** — 15 patterns
- **typed-output** — 12 patterns
- **safety** — 12 patterns
- **hitl-approval** — 10 patterns
- **retry-failure** — 7 patterns

## Build lanes (top 3 per theme)

### durable-state — resume + checkpointing for long agent runs

- **Continue-As-New: checkpoint the state and start a fresh run to bound history growth** (Temporal, S/high, score 8)
  - build: Add a rollover rule to hday_checkpoint.py: when a run's ledger segment exceeds a configured row or byte budget, write a rollover checkpoint (carrying the live state forward), close the segment, and start a new segment under the same logical run id, linked by a continuation pointer. hday_replay.py tr
  - test: tests/test_hday_checkpoint.py: drive a synthetic run past the configured budget and assert a rollover happened at the boundary, the new segment's first row poin
  - src: https://docs.temporal.io/workflow-execution/continue-as-new
- **Durability modes as a per-run cost/latency dial (exit / async / sync)** (LangGraph, S/high, score 8)
  - build: Add a durability field (exit|async|sync, default async) to the run manifest in hday_manifest.py, resolved from the risk/impact judgment hday_gate.py already computes (high-risk or irreversible tool class -> sync, read-only -> exit). hday_checkpoint.py honours it: sync = append+fsync the ledger row b
  - test: tests/test_hday_checkpoint.py: parametrise over the three modes with a fake writer that counts flush calls and can be told to fail after the Nth write; assert s
  - src: https://docs.langchain.com/oss/python/langgraph/checkpointers
- **Durable sleeps that hold no compute (step.sleep / step.sleepUntil)** (Inngest, S/high, score 8)
  - build: Add a durable wait to hday_steps.py: sleep_until(run_id, at, reason) records a wake row {run_id, at, reason, resume_token} and returns control immediately; a runner (CLI subcommand or the existing scheduler hook) scans due wake rows, resumes the run at the recorded step, and writes a ledger row for 
  - test: tests/test_hday_steps.py: with a frozen clock, schedule a wake 2 hours out and assert no process is blocked (the call returns in <50ms), the run is marked waiti
  - src: https://www.inngest.com/docs/features/inngest-functions/steps-workflows/sleeps

### hitl-approval — interrupts/approvals that survive restarts

- **A2A task lifecycle: contextId/taskId, eight TaskStates, blocking vs non-blocking** (A2A v1.0, M/high, score 7)
  - build: `hday_protocols.py`: `A2ATask{task_id, context_id, state, status_message, artifacts[], timestamp}` with a strict state->section mapping: WORKING/SUBMITTED -> in-flight, INPUT_REQUIRED/AUTH_REQUIRED -> needs-you (AUTH_REQUIRED gets an auth card, not a question card), COMPLETED/FAILED/CANCELED/REJECTE
  - test: tests/test_protocols_a2a_task.py — assert each of the eight states lands in exactly one cockpit section (table-driven, including UNSPECIFIED which must not sile
  - src: https://a2a-protocol.org/latest/topics/life-of-a-task/ , https://a2a-protocol.org/latest/specification/
- **CopilotKit HITL hooks: useHumanInTheLoop (interactive tool) and useInterrupt (AG-UI standard interrupts)** (CopilotKit, M/high, score 7)
  - build: In `desktop/plugin.js`: the approval card is a useHumanInTheLoop-shaped component — while status is Executing the card shows a/d buttons and `respond` is called exactly once (disable after first call); after Complete the card renders the recorded result inline. Wrap the whole board in a useInterrupt
  - test: tests/test_copilotkit_hitl.py + plugin.js harness — assert respond is callable once and a second call is a no-op; that the agent stays paused (no resume emitted
  - src: https://docs.copilotkit.ai/reference/v2/hooks/useHumanInTheLoop , https://docs.copilotkit.ai/reference/v2/hooks/useInterrupt
- **Human-in-the-loop as a pair of ordinary events (input required / human response)** (LlamaIndex Workflows, M/high, score 7)
  - build: Add input_required(prompt, schema, timeout) and human_response(value, responder) events to hday_flow.py, wired into hday_gate.py as the escalation path: a gate decision that needs a human emits an input-required event with a typed schema, the run pauses at a checkpoint, and any surface (cockpit, TUI
  - test: tests/test_hday_flow.py: a run that pauses on input_required; assert no downstream step executes while paused and the checkpoint exists; send a human_response f
  - src: https://developers.llamaindex.ai/python/llamaagents/workflows/human_in_the_loop/

### safety — capability permissions + injection defenses

- **Callback hooks at eight lifecycle points that block, rewrite, or observe** (Google ADK, M/high, score 7)
  - build: Add hday_hooks.py with eight named hook points (before/after x run/lane/model/tool, plus on_model_error/on_tool_error) and our own list semantics (stop at first truthy result; None continues), then re-express the existing free regex honesty guard and the gate's mandate checks as hooks rather than in
  - test: tests/test_hday_hooks.py: register three before_tool hooks returning None, None, and a deny verdict; assert all three ran, the third short-circuited the chain, 
  - src: https://adk.dev/callbacks/types-of-callbacks/
- **Task guardrails as a (passed, value) contract with feedback-as-retry and bounded retries** (crewai, S/high, score 7)
  - build: Add hday_gate.TaskGuardrail = Callable[[TaskOutput], tuple[bool, Any]]. Task(guardrails=[...], guardrail_max_retries=N). Runner: run guardrails sequentially over the candidate output; each guardrail returning (True, v) replaces the working output with v; (False, feedback) aborts the chain and re-run
  - test: Deterministic: (a) assert a guardrail returning (True, transformed) mutates the output seen by the next guardrail in the list; (b) assert (False, 'needs a citat
  - src: https://raw.githubusercontent.com/crewAIInc/crewAI/main/docs/v1.15.22/en/concepts/tasks.mdx
- **nemo-rails-taxonomy** (Guardrail frameworks, S/high, score 7)
  - build: hday_rails.py mapping the taxonomy to hook points, with rail_action in {allow, deny, alter}; an alter rail records before/after hashes plus the reason as a ledger row (never a silent rewrite). Document the mapping in the README so new hooks land in the correct rail and the taxonomy does not drift.
  - test: Unit: every registered rail declares type and action; an alter rail records both hashes and the diff reason; an unknown rail type fails registration loudly; a r
  - src: https://raw.githubusercontent.com/NVIDIA/NeMo-Guardrails/develop/README.md

### memory-context — context hygiene + bi-temporal memory

- **Context state store with atomic edits, and resources kept out of state** (LlamaIndex Workflows, S/high, score 8)
  - build: Add a context store to hday_flow.py: ctx.store.get/set backed by the run's checkpoint segment, an edit_state() context manager taking an exclusive lock for read-modify-write (so two parallel lanes cannot interleave), and a resources registry for non-serializable handles (model clients, browser sessi
  - test: tests/test_hday_flow.py: 50 concurrent increment steps through edit_state() and assert the final counter is exactly 50 (no lost updates) while the same test wit
  - src: https://developers.llamaindex.ai/python/llamaagents/workflows/managing_state/
- **externalize-dont-delete** (context-engineering, S/high, score 8)
  - build: Make every compression in hday_ctxscore restorable by construction: replace payloads with typed pointers {kind: file|url|tool_result|episode, ref, sha, bytes} and never drop the pointer. Add a hard rule (enforced in code, not convention) that gate vetoes, failed checks and error traces are never eli
  - test: tests/test_restorable_compression.py::test_every_cleared_payload_keeps_a_working_pointer_and_failures_are_pinned — clear a long transcript and assert every drop
  - src: https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus ; https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- **procedural-memory-type** (mem0, S/high, score 8)
  - build: Add a procedures store keyed by agent/lane (not by user): a successful run's tool sequence, its preconditions and its outcome becomes a retrievable procedure. Wire it into hday_router.suggest so a repeated task shape recalls the procedure that worked, and expose the count of procedures in the cockpi
  - test: tests/test_procedures.py::test_unknown_memory_type_is_rejected_and_procedure_recall_is_agent_scoped — assert an unknown memory_type raises a typed error naming 
  - src: https://docs.mem0.ai/core-concepts/memory-types

### typed-output — typed judgments with schema retry

- **Event-driven steps: typed events are the edges, plain Python is the logic** (LlamaIndex Workflows, M/high, score 7)
  - build: Add hday_flow.py: a step decorator whose signature (accepted event types -> returned event type) is inspected at registration to build the routing table, so hday_router.py routes by event type instead of a hand-maintained map. Define StartEvent/StopEvent equivalents (run_started, run_finished) plus 
  - test: tests/test_hday_flow.py: register a 4-step workflow with one branch and one loop; assert the derived routing table matches the declared annotations, that an eve
  - src: https://developers.llamaindex.ai/python/llamaagents/workflows/
- **Handoff between steps through named state keys (output_key) with scoped state prefixes** (Google ADK, M/high, score 7)
  - build: Add a scoped state API to hday_actors.py: set(key, value) with mandatory prefixes (run: default, session:, user:, app:, temp:) validated against the actor/session in scope, plus get(key, default). Lanes declare which keys they produce (output_key equivalents) in hday_manifest.py, and hday_ctxscore.p
  - test: tests/test_hday_actors.py: write one key per scope and assert lifetimes (temp: gone at run end, run: visible to later steps in the same run, session: visible to
  - src: https://adk.dev/sessions/state/
- **Signals vs Updates vs Queries as three distinct message channels (plus Signal-With-Start)** (Temporal, M/high, score 7)
  - build: Add hday_messages.py with three typed channel kinds: signal(run_id, kind, payload) (append-only, no response, may start the run if absent), update(run_id, kind, payload) (validated synchronously by the same judgment functions the gate uses; returns a typed result, rejects before persistence when val
  - test: tests/test_hday_lanes.py: assert a signal to a missing run creates it (signal-with-start) and appends exactly one row; assert an update with a payload that fail
  - src: https://docs.temporal.io/sending-messages

### observability — trajectory scoring + OTel-shaped spans

- **observation-tree** (Langfuse, S/high, score 8)
  - build: Add trace_id (one per session turn) and observation_id to every row emitted by hday_gate.decide / hday_ctxscore.score / hday_router.suggest, and copy turn-level attrs (session_id, mandate, cwd, model) onto each row. Extend the day-ledger command with --trace <id> and make the cockpit ledger view gro
  - test: Unit (tests/test_hday_lanes.py style): rows from three lanes in one turn share trace_id and carry session_id; day-ledger --trace returns them ordered; node:vm v
  - src: https://langfuse.com/docs/observability/data-model
- **OTel GenAI events + MCP semantic conventions: opt-in content events, context propagation via params._meta, and MCP metrics** (OpenTelemetry GenAI semantic conventions, M/high, score 7)
  - build: `hday_telemetry.py`: inject traceparent/tracestate/baggage UNPREFIXED into every MCP request's `params._meta`, and extract on the server side using `_meta` as remote parent with a span LINK (not parent) to the ambient HTTP context. Emit the four mcp.* metrics with mcp.method.name and mcp.session.id 
  - test: tests/test_telemetry_mcp.py — assert traceparent is written unprefixed into params._meta (a DNS-prefixed key fails the test), that the server span's parent is t
  - src: https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/gen-ai/gen-ai-events.md , https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/gen-ai/mcp.md , https://modelcontextprotocol.io/seps/414-request-meta
- **OTel GenAI: agent/framework spans (create_agent, invoke_agent, invoke_workflow, plan) and the execute_tool span** (OpenTelemetry GenAI semantic conventions, M/high, score 7)
  - build: `hday_telemetry.py`: emit create_agent / invoke_agent / invoke_workflow / plan spans for profile spawns, subagent dispatch, and mission plans, using gen_ai.agent.id = the stable profile/subagent-definition identifier and gen_ai.agent.name for display. Set agent.name and operation.name at span creati
  - test: tests/test_telemetry_agent_spans.py — assert agent.id is the stable definition id and not a per-run instance id; that a tool call observed by both the Hermes ho
  - src: https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/gen-ai/gen-ai-agent-spans.md

### interop — MCP/A2A/AG-UI adapter surface

- **Extension negotiation: `extensions` capability maps + graceful degradation** (MCP 2026-07-28, S/high, score 8)
  - build: `hday_protocols.py`: `ExtensionRegistry` mapping identifier -> {client_settings, server_settings, mandatory: bool, fallback: callable}. Stamp the client's supported set into every request `_meta`. Add `hday_manifest.py` awareness: the plugin's own manifest gains a `protocol_extensions` block so a de
  - test: tests/test_protocols_mcp_extensions.py — assert the client's extensions map is present on every outgoing request (including ones unrelated to the extension), th
  - src: https://modelcontextprotocol.io/extensions/overview
- **2026 stateless MCP: server/discover + per-request _meta, no initialize handshake** (MCP 2026-07-28, M/high, score 7)
  - build: `hday_protocols.py`: `MCPNegotiator` that (1) calls server/discover first, (2) caches DiscoverResult keyed by server with `ttlMs`/`cacheScope` respected, (3) falls back to the legacy initialize handshake when discover fails on stdio, and (4) stamps every outgoing request's `_meta` with protocolVersi
  - test: tests/test_protocols_mcp2026_discover.py — assert discover is called exactly once per TTL window (not per request), that a 2026 server never receives an initial
  - src: https://modelcontextprotocol.io/specification/2026-07-28/server/discover , https://modelcontextprotocol.io/specification/2026-07-28/changelog
- **A2A streaming: SSE of TaskStatusUpdateEvent / TaskArtifactUpdateEvent with final-chunk semantics** (A2A v1.0, M/high, score 7)
  - build: `hday_protocols.py`: `A2AStreamConsumer` — asserts `capabilities.streaming` before calling; maintains an artifactId -> buffer map; on append:true concatenates, on lastChunk:true seals and emits one finished artifact. Status updates mutate the task's section in place (never append a new row per updat
  - test: tests/test_protocols_a2a_stream.py — fixture stream emitting 3 artifact chunks (2 append:true, then lastChunk:true) interleaved with 4 status updates. Assert ex
  - src: https://a2a-protocol.org/latest/specification/ , https://a2a-protocol.org/latest/topics/streaming-and-async/

### orchestration — handoff + subagent coordination

- **Child workflows with an explicit parent-close policy (and the continue-as-new caveat)** (Temporal, S/high, score 8)
  - build: Add a parent_close_policy field (cancel|abandon|terminate) to delegated sub-runs in hday_manifest.py and enforce it in hday_gate.py: when a parent run closes, iterate its children and apply the declared policy, writing a typed ledger row per child with the action taken. Rollover of a parent (see Con
  - test: tests/test_hday_gate.py: start a parent with three children (one cancel, one abandon, one terminate); close the parent and assert each child's terminal state ma
  - src: https://docs.temporal.io/child-workflows
- **Loop termination owned by the loop plus sub-agent escalation (the exit_loop tool pattern)** (Google ADK, S/high, score 8)
  - build: Add to hday_compose.py: Loop with max_iterations plus an escalation channel on the child context (child.escalate(reason) / skip_summarization equivalents), so a lane can end the loop with a typed reason. Record the terminating condition for every loop in the ledger (max_iterations | escalated:<reaso
  - test: tests/test_hday_compose.py: a writer/critic loop where the critic escalates on iteration 3; assert exactly 3 iterations, the terminating reason is 'escalated:qu
  - src: https://adk.dev/agents/workflow-agents/loop-agents/
- **AG-UI subagents: subagentRunId attribution and suspension** (AG-UI, M/high, score 7)
  - build: `hday_events.py`: map Hermes subagent_start/stop into SUBAGENT_STARTED/FINISHED with a freshly minted `subagentRunId` (never the profile or agent name), keeping `name` for display. Key all transient UI state (spinner, collapsible group, progress row) by subagentRunId and persist NOTHING by it. Imple
  - test: tests/test_subagents_attribution.py — assert two invocations of the same-named subagent get distinct subagentRunIds and produce two rows; that a suspended subag
  - src: https://docs.ag-ui.com/concepts/subagents

### retry-failure — retry budgets + failure compensation

- **Per-step independent retry counters with an attempt index, plus non-retriable and retry-after errors** (Inngest, S/high, score 8)
  - build: Extend the retry policy from hday_router.py so the budget is per (run, step) rather than per run, with the attempt index injected into the lane's context and a reset on step completion. Add two typed exceptions in hday_gate.py - NonRetriable (stop, record why) and RetryAfter(seconds, reason) - that 
  - test: tests/test_hday_router.py: three steps in one run, each failing its own number of times; assert each step gets its full independent budget (total attempts = sum
  - src: https://www.inngest.com/docs/features/inngest-functions/error-retries/retries
- **retry-budget-multiplication** (pydantic-ai, S/high, score 8)
  - build: Add a worst-case request estimator to the cost guard: given (turns, tools per turn, output_retries, sdk_max_retries, transport_attempts, timeout) compute N*M*K and the worst-case wall time, and refuse configurations above a declared ceiling before the run starts. Surface the estimate on the cockpit 
  - test: tests/test_retry_math.py::test_worst_case_request_count_and_refusal — assert (3,3,2) computes 18 and the derived wall time; assert a configuration above the cei
  - src: https://pydantic.dev/docs/ai/core-concepts/retries/
- **Activities as the side-effect boundary: declarative retry policy plus heartbeat checkpointing** (Temporal, M/high, score 7)
  - build: Define the side-effect contract in hday_gate.py: every tool call is executed as an activity record {name, input, attempt, idempotency_key, heartbeat}, with the retry policy and non-retryable set declared per tool in hday_manifest.py. Add heartbeat support to long-running lanes: the lane reports prog
  - test: tests/test_hday_lanes.py: a fake activity that fails on attempt 1 after recording a heartbeat and succeeds on attempt 2 by resuming from the heartbeat payload; 
  - src: https://docs.temporal.io/activities

## Top 40 by score

- 8 · orchestration · Child workflows with an explicit parent-close policy (and the continue-as-new caveat) (Temporal, S)
- 8 · memory-context · Context state store with atomic edits, and resources kept out of state (LlamaIndex Workflows, S)
- 8 · durable-state · Continue-As-New: checkpoint the state and start a fresh run to bound history growth (Temporal, S)
- 8 · durable-state · Durability modes as a per-run cost/latency dial (exit / async / sync) (LangGraph, S)
- 8 · durable-state · Durable sleeps that hold no compute (step.sleep / step.sleepUntil) (Inngest, S)
- 8 · interop · Extension negotiation: `extensions` capability maps + graceful degradation (MCP 2026-07-28, S)
- 8 · orchestration · Loop termination owned by the loop plus sub-agent escalation (the exit_loop tool pattern) (Google ADK, S)
- 8 · retry-failure · Per-step independent retry counters with an attempt index, plus non-retriable and retry-after errors (Inngest, S)
- 8 · durable-state · Step memoization with stable step IDs and per-ID counters (loops reuse one ID) (Inngest, S)
- 8 · memory-context · externalize-dont-delete (context-engineering, S)
- 8 · observability · observation-tree (Langfuse, S)
- 8 · memory-context · procedural-memory-type (mem0, S)
- 8 · retry-failure · retry-budget-multiplication (pydantic-ai, S)
- 8 · memory-context · tool-result-clearing (context-engineering, S)
- 7 · interop · 2026 stateless MCP: server/discover + per-request _meta, no initialize handshake (MCP 2026-07-28, M)
- 7 · interop · A2A streaming: SSE of TaskStatusUpdateEvent / TaskArtifactUpdateEvent with final-chunk semantics (A2A v1.0, M)
- 7 · hitl-approval · A2A task lifecycle: contextId/taskId, eight TaskStates, blocking vs non-blocking (A2A v1.0, M)
- 7 · interop · A2A v1.0 wire contract: PascalCase methods, unified Part, discriminator stream events, A2A-Version (A2A v1.0, M)
- 7 · interop · AG-UI capability discovery: getCapabilities() and feature gating (AG-UI, M)
- 7 · durable-state · AG-UI shared state: STATE_SNAPSHOT + STATE_DELTA (RFC 6902 JSON Patch) and the two-way channel (AG-UI, M)
- 7 · orchestration · AG-UI subagents: subagentRunId attribution and suspension (AG-UI, M)
- 7 · retry-failure · Activities as the side-effect boundary: declarative retry policy plus heartbeat checkpointing (Temporal, M)
- 7 · durable-state · Actor with durable state: c.state plus createState, persisted and restored across restarts (Rivet, M)
- 7 · safety · Callback hooks at eight lifecycle points that block, rewrite, or observe (Google ADK, M)
- 7 · hitl-approval · CopilotKit HITL hooks: useHumanInTheLoop (interactive tool) and useInterrupt (AG-UI standard interrupts) (CopilotKit, M)
- 7 · interop · CopilotKit: useAgent + useAgentContext as the two-way shared-state client (CopilotKit, M)
- 7 · durable-state · Deterministic replay from a recorded event history, with replay-safe primitives (Temporal, M)
- 7 · typed-output · Event-driven steps: typed events are the edges, plain Python is the logic (LlamaIndex Workflows, M)
- 7 · orchestration · Fan-out/fan-in encoded in step signatures, with three separate concurrency limits (LlamaIndex Workflows, M)
- 7 · typed-output · Handoff between steps through named state keys (output_key) with scoped state prefixes (Google ADK, M)
- 7 · hitl-approval · Human-in-the-loop as a pair of ordinary events (input required / human response) (LlamaIndex Workflows, M)
- 7 · durable-state · Interrupt + Command(resume=...) as the HITL pause primitive, with replay re-triggering (LangGraph, M)
- 7 · orchestration · Lifecycle hooks with a defined order, plus shutdown cancellation via an abort signal (Rivet, M)
- 7 · retry-failure · Node retry policy, node timeouts, and routing failures back into the graph (LangGraph, M)
- 7 · observability · OTel GenAI events + MCP semantic conventions: opt-in content events, context propagation via params._meta, and MCP metrics (OpenTelemetry GenAI semantic conventions, M)
- 7 · observability · OTel GenAI: agent/framework spans (create_agent, invoke_agent, invoke_workflow, plan) and the execute_tool span (OpenTelemetry GenAI semantic conventions, M)
- 7 · observability · OTel GenAI: inference client spans and the content-capture contract (OpenTelemetry GenAI semantic conventions, M)
- 7 · interop · Prompts as user-invoked slash commands with typed arguments (MCP, M)
- 7 · orchestration · Queue with completable messages: request/response, durable delivery, sender timeout (Rivet, M)
- 7 · observability · Resources: URI-addressed context, templates, subscribe/list_changed (MCP, M)
