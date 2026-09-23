# Multi-agent SDK mechanism harvest

Cluster: `multi-agent-sdks`. Companion data: `multi-agent-sdks.json` (30 patterns, schema-validated).

**This deliverable is self-validating.** `runs/harvest/validate_multi_agent_sdks.py` asserts the
contract below (693 assertions: strict pattern schema, 18–30 patterns, 3–6 per briefed framework, every
mechanism the brief named actually described, every source URL on a host that was really fetched,
unreachable docs recorded, md companion present). `tests/test_harvest_multi_agent_sdks.py` runs it in
the repo suite. It was checked for teeth: seven deliberately broken copies (dropped key, invented host,
removed named topic, truncated pattern list, bad `effort`, citation of a known-404 page, missing
`unreachable` list) are all rejected. Run it with:

```
python3 runs/harvest/validate_multi_agent_sdks.py
```

**Method.** Documentation was fetched as raw markdown rather than rendered HTML — GitHub
`raw.githubusercontent.com` paths for repo-hosted docs, and Mintlify `.md` endpoints where the
docs site is Mintlify (`code.claude.com/docs/en/<page>.md`, `docs.agno.com/<path>.md`). Notebook
sources (`.ipynb`) were parsed cell-by-cell so code and prose survive intact. 158 documents (~5.7 MB)
were fetched and are on disk; every `source` URL in the JSON is a document that was actually retrieved,
and every API name quoted below appears in one of them. Nothing here is reconstructed from memory.

**Coverage:** OpenAI Agents SDK (5), Claude Agent SDK / Claude Code (5), AutoGen (3), AG2 (3),
CrewAI (5), Agno (4), Mastra (5).

---

## OpenAI Agents SDK

Docs: `raw.githubusercontent.com/openai/openai-agents-python/main/docs/{handoffs,guardrails,sessions/index,tracing,tools,multi_agent,results}.md`

### Handoffs are tools, and the schema is the contract

A handoff is not an internal call — it is rendered to the model as a tool named
`transfer_to_<agent_name>` (`Handoff.default_tool_name()`). `handoff(agent, tool_name_override=,
on_handoff=, input_type=, input_filter=, is_enabled=)` declares it. `input_type` is a Pydantic
schema for the handoff tool-call arguments: the SDK publishes it as the tool's `parameters`,
validates the returned JSON locally, and passes the parsed value to `on_handoff(ctx, input_data)`.
The security-relevant detail is stated plainly in the docs: `is_enabled` is evaluated while the SDK
prepares available handoffs, *before* the model returns arguments, so it **cannot** authorize values
inside an argument-bearing handoff; authorization on parsed fields belongs at the top of
`on_handoff`, and must `raise` to refuse, because the transfer continues once `on_handoff` returns
normally.

History is a separate axis. `input_filter` receives a `HandoffInputData` and must return a new one —
that is the only supported way to change what the receiving agent sees. `handoff_filters.remove_all_tools`
strips structured tool items but does **not** redact tool text already copied into ordinary messages.
`RunConfig.handoff_input_filter` is a run-wide default and a per-handoff filter wins over it. There is
an opt-in compaction mode (`RunConfig.nest_handoff_history`) that folds history into ordered
`<CONVERSATION HISTORY>` segments while keeping lossless message items in place, with
`RunConfig.handoff_history_mapper` as a full override; nesting applies only when neither filter is set.

### Guardrails are typed verdicts, and the latency/cost tradeoff is a parameter

`@input_guardrail` / `@output_guardrail` functions return
`GuardrailFunctionOutput(output_info=<any>, tripwire_triggered=<bool>)`, wrapped by the runner into
`InputGuardrailResult` / `OutputGuardrailResult`. A true tripwire raises
`InputGuardrailTripwireTriggered` / `OutputGuardrailTripwireTriggered`. `run_in_parallel=True`
(default) starts the guardrail concurrently with the agent — best latency, but the agent may already
have burned tokens and executed tools before cancellation; `run_in_parallel=False` runs the guardrail
to completion **before** the agent starts, so a tripwire prevents both token spend and tool side
effects. Input guardrails run only for the **first** agent in a chain. Exceptions carry evidence:
`guardrail_result` identifies the trigger, and `run_data.input_guardrail_results` /
`output_guardrail_results` hold every result completed before the stop.

The persistence rule is the part worth stealing outright. A tripwire persists already-completed tool
call and tool output items plus the reasoning needed to replay them, while **excluding** the rejected
candidate final output — the retained `function_call_output` payload is replaced with the literal text
`"Output withheld by an output guardrail."`. A guardrail function that *raises* instead is treated as
an unknown verdict and persists the completed final-turn items. `RunConfig.output_guardrail_blocked_message`
can replace the placeholder but its formatter never receives the rejected output.

### Sessions are a five-method interface with a merge callback

`get_items(limit=...)`, `add_items(items)`, `pop_item()`, `clear_session()`, and a `session_id` key.
`Runner.run(..., session=session)` merges session history with the current input; the sanctioned seam
for policy is `RunConfig.session_input_callback(existing, new) -> list`. `RunConfig.session_settings=SessionSettings(limit=N)`
bounds how much history is fetched before each run (`None` = all). Implementations are a matrix over
the same interface (`SQLiteSession`, `AsyncSQLiteSession`, `AdvancedSQLiteSession`), and `pop_item()`
is documented for corrections — pop the assistant item, then the user item, to undo a turn.

### Tracing is a typed span tree with additive processors

One trace = typed spans. `Trace` has `trace_id` (format `trace_<32 alphanumeric>`); each `Span` has
`trace_id`, `span_id`, `parent_id`, `started_at`/`finished_at`, and `span_data` (a typed payload such
as `AgentSpanData`, `GenerationSpanData`, `FunctionSpanData`, `GuardrailSpanData`, `HandoffSpanData`).
The runner auto-instruments: `Runner.run` in `trace()`, each agent run in `agent_span()`, generations
in `generation_span()`, tool calls in `function_span()`, guardrails in `guardrail_span()`, handoffs in
`handoff_span()`, and `custom_span()` for anything else. Wrapping several `run()` calls in one
`trace()` groups them into a single trace — the documented way to say "these runs are one unit of work."

Two processor lessons. `add_trace_processor()` **adds** an observer alongside the default;
`set_trace_processors()` **replaces** the set. And processors are independent observers whose
exceptions are caught, so *a redaction processor registered before an exporter does not protect that
exporter* — when export depends on redaction, redaction and delivery must live inside one
application-owned exporter that copies, redacts, and discards the batch on failure. `flush_traces()`
blocks until buffered spans are exported; `RunConfig.trace_include_sensitive_data=False` disables
capturing generation and function inputs/outputs.

### The tool surface is declared in four classes, with deferred loading

Hosted tools (`WebSearchTool`, `FileSearchTool`, `CodeInterpreterTool`, `HostedMCPTool`,
`ImageGenerationTool`) run provider-side. Local tools (`ComputerTool`, `ApplyPatchTool`, `ShellTool`)
need an implementation you supply — and `ShellTool` in hosted-container mode must *not* be given
`executor`, `needs_approval`, or `on_approval`. Deferred loading is the interesting one:
`@function_tool(defer_loading=True)`, `tool_namespace(name=, description=, tools=[...])`, and
`HostedMCPTool(tool_config={..., "defer_loading": True})` are searchable surfaces, and the agent then
needs exactly one `ToolSearchTool()` to load them on demand; namespaces may mix immediate and deferred
members. `allowed_callers` gates *how* a tool may be invoked (omitted = direct only, `["programmatic"]`,
or `["direct","programmatic"]`), and a structured return annotation on a programmatically-callable tool
becomes a strict output schema validated before the value returns. Separately, `Agent.as_tool()` turns a
whole agent into a tool with `max_turns`, `run_config`, `hooks`, `session`, `needs_approval`,
`parameters`/`input_builder`/`include_input_schema`, `custom_output_extractor`, `on_stream`, and
`is_enabled` — and the docs repeat the same warning: `is_enabled` controls visibility and dispatch, it
does not replace authorization on arguments or the accessed resource.

---

## Claude Agent SDK / Claude Code

Docs: `code.claude.com/docs/en/{sub-agents,skills,mcp,permission-modes,hooks-guide,settings}.md` and `docs.claude.com/en/docs/claude-code/hooks.md`.

> Reachability note: `docs.claude.com/en/api/agent-sdk/*.md` returns the Next.js HTML shell rather
> than markdown, so Agent-SDK Python specifics were not usable from that path. Claude Code's own
> reference pages (hooks, sub-agents, skills, mcp, permission-modes) **are** served as clean markdown
> and are the basis for everything below.

### A subagent is a file whose frontmatter is the whole contract

`name` and `description` are required; the frontmatter also carries `tools` (an allowlist — omitting
it inherits every tool), `disallowedTools`, `model` (`inherit` or explicit), `permission-mode`,
`skills`, `hooks`, and `memory`. Files resolve across user, project, and plugin scopes with documented
precedence. Two invocation paths: automatic delegation driven by `description`, and explicit user
naming. The operational constraints are the valuable part: a subagent runs with its **own context
window** and cannot see the parent's conversation except what the delegation prompt carries; it
**cannot spawn further subagents**; there is a documented concurrency limit for parallel subagents.
Context management is first-class — a subagent can be given a persistent identity (worktree-isolated
and/or background) so it survives across turns, its transcript can be retrieved afterwards with
per-subagent transcript files, and a fork mode (`CLAUDE_CODE_FORK_SUBAGENT`) lets it inherit the
parent's full conversation instead of starting clean.

### Hooks: many events, a matcher/handler split, and five handler types

Hooks are configured as event → matcher groups → handlers, and the event surface is large:
`PreToolUse`, `PermissionRequest`, `PermissionDenied`, `PostToolUse`, `PostToolUseFailure`,
`PostToolBatch`, `SubagentStart`, `SubagentStop`, `TaskCreated`, `TaskCompleted`, `Stop`,
`StopFailure`, `UserPromptSubmit`, `PreCompact`, `PostCompact`, `SessionStart`, `SessionEnd`,
`Notification`, `ConfigChange`, `InstructionsLoaded`, `CwdChanged`, `FileChanged`, `PreModelSwitch`,
`PostModelSwitch`, `Elicitation`, `ElicitationResult`, `WorktreeCreate`, `WorktreeRemove`,
`TeammateIdle`, `MessageDisplay`. A handler is one of five types — shell command, HTTP endpoint, MCP
tool call, LLM prompt, or subagent — so the extension point is not tied to the shell.

Matcher semantics are the trap worth copying exactly. A plain value or `|`-separated alternatives is
compared against the event's matcher field (`tool_name` for tool events); a matcher containing any
other character is compiled as a JavaScript regular expression and tested with
`RegExp.prototype.test`, which matches **anywhere** in the value — so `Edit.*` matches both `Edit` and
`NotebookEdit`, and `^Edit$` is required for whole-string matching. Some events have no matcher
support and a matcher on them is silently ignored. MCP tools match as `mcp__<server>__<tool>`, and a
server-wide match requires the `.*` suffix: `mcp__memory` alone is an exact string that matches
nothing. Input arrives as JSON on stdin (command hooks) or as the POST body (HTTP hooks); output may
be text or JSON with fields including `permissionDecision`, and `additionalContext` can inject text
back into the model's context. There is also an `if` field for a second, narrower filter beyond the
matcher, plus async hooks.

### Permission modes are a baseline, and rules layer on top

One mode per session — Manual, `acceptEdits`, `plan`, `auto`, `bypassPermissions` — selected by CLI
flag, `permissions.defaultMode`, or the in-session indicator, with documented precedence. Layered on
top: `allow`/`ask`/`deny` rules where **deny applies in every mode**, and rules match commands *as
written* rather than as resolved (a documented limitation of bash rule matching). An administrator can
forbid a mode entirely (`disableAutoMode: "disable"` removes `auto` from the mode cycle and an explicit
`--permission-mode auto` starts in Manual instead; a running session leaves auto when the setting
reaches it). Working directories define the trust boundary, and the first read outside them prompts
once with three answers: keep allowing, block from now on (which persists
`permissions.blockReadsOutsideWorkingDirectories=true` in user settings and makes file tools refuse
such reads in *every later session and every mode*), or ask again.

Auto mode adds a classifier whose verdicts are enumerated as data. Blocked by default includes
`curl | bash`, sending sensitive data to external endpoints, production deploys and migrations, mass
deletion on cloud storage, granting IAM or repo permissions, force push, `git reset --hard` /
`checkout -- .` / `restore .` / `clean -fd` / `stash drop` / `stash clear`, `terraform|pulumi|cdk|terragrunt destroy`,
writing to a secret manager, changing DNS or TLS, merging a PR no human approved, disarming a safety
flag (`--insecure`), launching an agent loop without approval or sandbox, and **commenting out,
deleting, or force-passing a test or assertion that guards security behavior**. Allowed by default
includes local file operations in the working directory, installing dependencies declared in lock
files, reading `.env` and sending credentials to their matching API, read-only HTTP requests, and
pushing to any branch of the working repo. Two rules deserve emphasis for our own gate: boundaries
stated in conversation are treated as a block signal and are **re-read from the transcript on each
check**, so a boundary can be *lost to context compaction*; and an approval stated in conversation
only clears a block when the message names the action **and** the specific thing that makes it
dangerous — naming the verb alone clears nothing.

### Skills carry an enforceable capability contract

`SKILL.md` frontmatter declares `name`, `description`, and controls including `allowed-tools`
(pre-approve tools the skill may use), `disable-model-invocation` (the model cannot auto-invoke it),
`user-invocable`, `context` (including running the skill in a forked context), `agent`, `model`, and
`hooks`. Invocation has two independent axes — model-driven by description match, and user-driven
directly — each switchable, which is what lets you ship a human-only skill. A skill can execute in a
subagent context and can carry hooks, making it a portable unit of instructions + tool permissions +
lifecycle behavior.

### MCP client hardening

Four behaviors matter more than the happy path. **Dynamic tool updates**: a server changing its tool
set is picked up without a reconnect. **Automatic reconnection with backoff**: a dropped connection is
retried and the session restored rather than surfaced to the model as a hard failure. **Push messages**:
a server can push messages into the conversation through a channel, which is how an MCP server *drives*
the agent rather than only answering it. **Automatic backgrounding**: a long-running tool call moves to
the background and the agent continues, with the result delivered when it lands. Tool naming is
`mcp__<server>__<tool>`, and plugin-bundled servers get a scoped segment
`mcp__plugin_<plugin>_<server>__<tool>` — so a matcher written against the bare server key never fires.

---

## AutoGen

Docs: `raw.githubusercontent.com/microsoft/autogen/main/python/docs/src/user-guide/core-user-guide/{core-concepts/topic-and-subscription.md, design-patterns/group-chat.ipynb, components/workbench.ipynb}` and `agentchat-user-guide/{selector-group-chat.ipynb, tutorial/termination.ipynb}`.

### Topics decouple agents, and the source field is the addressing scheme

A topic is a `TopicId` of `(type, source)` with `source` defaulting to `DefaultTopicId`
(`type="default", source="default"`). Broadcasting is publishing to the default topic; **direct
addressing** is publishing to a topic whose `source` is the target agent's id. Subscriptions are typed
— `TypeSubscription(topic_type=..., agent_type=...)` means every agent of that type receives every
message published to that topic type — and an agent id is auto-generated from the topic source when the
runtime creates the agent, which is what makes "publish to (T, agent_id)" work without the publisher
holding an instance reference. Handlers are declared with `@message_handler` on a `RoutedAgent`.

### Group chat is a manager-mediated protocol with pluggable speaker selection

The manager owns the roster and the transcript and runs an explicit loop: participants register with
the manager (`RegisterAgent` carrying the agent's topic type); the manager publishes `RequestToSpeak`
to the selected participant; receives `GroupChatRequestPublish`; publishes the participant's message to
the group topic; records it; then selects the next speaker and repeats until termination. The
high-level `SelectorGroupChat` documents the same loop: selection uses conversation context plus each
participant's `name` and `description`, will not select the same speaker consecutively unless
`allow_repeated_speaker=True`, supports a custom `selection_function` and a `candidate_function` that
narrows the roster *before* selection, and supports a custom `selector_prompt`. After a task completes
the conversation context is retained by the team and participants, so the next task continues from it;
`BaseGroupChat.reset()` clears it.

### Workbench: a stateful tool collection behind `list_tools`/`call_tool`

A `Workbench` is an interface to a *collection* of tools that share state and resources — distinct from
a `Tool`, which wraps one callable. Two methods: `list_tools()` returns current schemas,
`call_tool(name, arguments, cancellation_token)` returns a `ToolResult` (`to_text()`, `is_error`,
`name`). Because the workbench owns the state, tools can share a connection, sandbox, or resource pool
without the agent knowing. The reference agent loop is written out explicitly: `create()` with
`tools=await workbench.list_tools()`, then while the model returns `FunctionCall` content, add the
`AssistantMessage`, call the workbench per call, add a `FunctionExecutionResultMessage` whose entries
carry `call_id`, `content=result.to_text()`, `is_error=result.is_error`, `name`, then re-`create()`.
`MCPWorkbench` is a provided implementation where an MCP server *is* the tool collection — local and
remote tools behind one interface. Cancellation is threaded through every call via the message
context's cancellation token.

> Also fetched and usable: `tutorial/termination.ipynb` (eleven composable termination conditions with
> a `|` operator, `reset()` semantics, `ExternalTermination` for UI stop buttons, and
> `HandoffTermination` as the documented pause-for-human mechanism) and `design-patterns/handoffs.ipynb`
> (handoff as delegate tools where the tool call *is* the routing decision, and the receiver's context
> is parent messages + the assistant function call + a synthetic `FunctionExecutionResult` reading
> "Transferred to X. Adopt persona immediately."). Both are listed in the appendix.

---

## AG2

Docs: `raw.githubusercontent.com/ag2ai/ag2/main/website/docs/user-guide/{middleware.mdx, network/overview.mdx, network/expectations_and_audit.mdx, tools/approval_required.mdx, advanced/observers.mdx, advanced/watches.mdx, skills.mdx, subagents.mdx}`.

> AG2 has moved group chat into its **network** model — the repo tree contains no `groupchat` directory,
> only `network/migration_from_group_chat.mdx`. Group-chat coverage here therefore comes from AutoGen's
> Core/AgentChat docs plus AG2's network and middleware docs.

### Middleware: four typed async hooks wrapping `call_next`, with `describe()`

AG2 middleware defines four typed async hooks — `on_turn`, `on_llm_call`, `on_tool_execution`,
`on_human_input` — each receiving the event plus a `call_next` continuation. A hook calls
`call_next(...)` to continue (optionally with modified arguments), returns its own result to replace
the call, or raises to stop. Instances hold state that lives for the duration of one turn and are
re-created for the next. `describe()` exposes configuration in a loggable/assertable form, and the docs
are explicit that state which must *not* leak — per-user approval answers — lives in `context.variables`
rather than on the instance, which is why it never appears in the description.

### Approval-gated tools that fail closed

`approval_required()` is a built-in tool middleware. It takes `message`, `denied_message`, `timeout`,
`allow_always` and returns an `ApprovalRequired` instance reporting its settings via `describe().config`;
approval memory lives in `context.variables`, not on the instance. The rule that matters: it relies on
the agent's `hitl_hook`, and **with no `hitl_hook` configured there is nobody to ask, so
`context.input()` raises `HumanInputNotProvidedError` and the turn ends** — the gated tool does not run
and, critically, the model is *not* handed a tool failure it could route around. The same applies on
timeout (`HumanInputTimeoutError`), and a late answer after the deadline does not release the approval.
The docs also record a fixed bug worth not re-introducing: `timeout` used to default to `30` and never
fired, because the hook ran inline so the clock only started after it returned; it now binds and
defaults to `None` so enabling a timeout does not silently start failing turns.

### The hub: arbiter before, listeners after, audit log registered first

The network model interposes a hub between agents; traffic moves as envelopes and identity is a
`Passport` plus a `Resume` (claimed and observed capabilities). Two asymmetric seams surround every
decision: a `HubArbiter` is consulted **before** the hub commits a register / channel-open / send /
dispatch decision (a policy can veto a state transition), while `HubListener`s are read-only observers
the hub fans state transitions out to **after** the fact. `hub.audit_log` is itself a `HubListener`,
and — the ordering guarantee worth copying — `replace_audit_log(custom)` registers the replacement as
the *first* listener so audit writes still complete before other listeners observe the same event. The
log is append-only `audit.jsonl` under the hub's `KnowledgeStore`; records are plain dicts with at least
`kind` and `at`; the kind set is explicitly **open** for tenants to extend; `subscribe(cb)`/`unsubscribe(cb)`
give a live tail with subscriber exceptions logged and swallowed. Observability is layered on top: pure
evaluators over channel state return zero or more `Violation` records, and a violation handler decides
what happens — `audit` (log only, channel continues), `warn` (post an event on the channel WAL), or
`auto_close` (close with `reason="expectation_violated:<name>"` and record to audit). Evaluators are
pure by contract — no I/O, no mutation — which is why they are trivially testable, and a sweeper task
runs them on an interval with `expectation_sweep_interval=0` disabling the background loop for
deterministic tests.

> Also fetched and usable: `advanced/observers.mdx` + `advanced/watches.mdx` (`BaseObserver` pairing a
> `Watch` — `EventWatch`, `CadenceWatch`, `DelayWatch`, `IntervalWatch`, `CronWatch`, `AllOf`, `AnyOf`,
> `Sequence` — with `process()`, and the important default that `ObserverAlert` is persisted in history
> but **not** rendered back to the LLM), `skills.mdx` (three-level progressive disclosure with a
> name-constrained `load_skill` tool, capability gating, and code-defined `MemorySkill`), and
> `subagents.mdx`. All are listed in the appendix.

---

## CrewAI

Docs: `raw.githubusercontent.com/crewAIInc/crewAI/main/docs/v1.15.22/en/{concepts/tasks.mdx, concepts/flows.mdx, concepts/memory.mdx, concepts/checkpointing.mdx, concepts/crews.mdx, concepts/collaboration.mdx, learn/execution-hooks.mdx, learn/human-feedback-in-flows.mdx}`.

### Task guardrails return `(passed, value)` — and the value is feedback

A `Task` carries `guardrail` (single) or `guardrails` (a list run sequentially), where each guardrail
is a callable **or a natural-language string** that is compiled into an LLM guardrail automatically.
The callable contract is a tuple: `(True, Any)` proceeds with the possibly-transformed output;
`(False, Any)` treats the value as **feedback and retries the task with it**. So one hook both
validates and repairs, `guardrail_max_retries` bounds the loop, and because guardrails are a list,
normalization and validation compose — an early guardrail can normalize output that later guardrails
then check.

### Execution hooks: typed points, four verbs, and an asymmetric failure policy

Hooks are typed by `InterceptionPoint` and registered per agent, per tool, or crew-wide with
`agents=`/`tools=` scoping. A hook may `proceed` (continue, optionally with modified arguments),
`mutate`, `replace` (substitute the result), or `abort` — where abort raises `HookAborted` carrying a
`reason` and a `source`, a typed control-flow signal rather than a generic exception. The failure policy
is the reusable insight: `HookAborted` is deliberate, while any **other** exception inside a hook is
**fail-open** — logged and execution continues, so a broken observability hook cannot take down a run.
Hooks also emit `HookDispatchedEvent` for telemetry, making hook activity observable without
instrumenting each hook.

### Memory: hierarchical scopes, composite scoring, private views

One system with scopes (`short_term`, `long_term`, `entity`, `external`) rather than several stores;
writes are routed into the right scope by an LLM that infers placement. Retrieval is a **composite** —
semantic similarity combined with recency and importance, with a tunable recency half-life in days — so
a fresh, loosely-similar item can outrank an old near-exact match. The API is uniform across scopes
(`remember`, `recall`, `forget`, `extract_memories`, `tree`), `scope(path)` exposes a private namespace
so one agent gets its own view without a second store, extraction runs automatically after a task and
recall is injected automatically before one.

### Human feedback collapsed into declared outcomes

`@human_feedback(emit=[...], llm=..., default_outcome=...)` names the branches the human can produce.
The human types prose; a model collapses it into exactly one declared outcome; the outcome is what
`@listen`/`@router` conditions branch on. The result is a `HumanFeedbackResult` dataclass carrying both
raw text and resolved outcome, and an async provider form lets a webhook or UI supply the answer without
blocking the flow thread. The flow graph stays finite (N branches) while the human speaks naturally.

### Crews versus flows: autonomy as a bounded node

A Crew is autonomous and role-based — agents with roles and goals under a `process` (sequential, or
hierarchical where a manager LLM or manager agent plans and delegates) — and decides its own internal
path. A Flow is an event-driven graph: `@start`, `@listen(...)`, `@router`, conditions composed with
`or_()`/`and_()`, and explicit state passed between steps. The composition rule is the architecture
lesson: **a crew can be kicked off as a step inside a flow, and a flow can be invoked from within a
crew** — autonomy is a bounded node inside a deterministic graph, not the top-level control structure.
Flows also persist: a persisted flow started with a stable id resumes prior state, while a distinct
restore-from id forks a new run from a previous state.

> Also fetched and usable: `concepts/checkpointing.mdx` (event-driven checkpoints via `on_events=[...]`,
> JSON/SQLite providers, `max_checkpoints` pruning, resume-versus-fork lineage) and
> `concepts/collaboration.mdx` (auto-injected `Delegate work to coworker` / `Ask question to coworker`
> tools). Listed in the appendix.

---

## Agno

Docs: `docs.agno.com/{teams/delegation.md, database/session-storage.md, workflows/hitl/overview.md, workflows/hitl/pause-anatomy.md, tools/hooks.md, agents/usage/agent-with-structured-output.md, agents/overview.md}`.

### Team modes: the orchestration pattern is a swap-in enum

`TeamMode.coordinate` (default) has the leader select members, formulate each member's task, and
synthesize; `route` picks one member and returns that response directly (no synthesis, lower latency);
`broadcast` sends the same task to every member; `tasks` has the leader build and execute a shared task
list to completion. Two orthogonal flags survive as legacy: `determine_input_for_members=False` passes
raw user content instead of a leader-formulated task (across coordinate, route and broadcast), and
`respond_directly=True` returns member responses without synthesis. Member identity is explicit —
members should carry stable `id` values because **member selection and run tracking are keyed on them**
— and members may come from callable factories resolved at run time. The leader can still answer
directly or use its own tools in any mode.

### Sessions: `session_id` key, owner discriminator, and separate run tables

Persistence is opt-in by attaching a database, and everything hangs off `session_id`. A hydrated
session carries `session_id`, the owner id (`agent_id`/`team_id`/`workflow_id`), `user_id`, per-owner
data blobs (`session_data`, `agent_data`, `team_data`, `workflow_data`), `metadata`, `runs`, `summary`,
`created_at`, `updated_at`. The physical layout separates concerns: session metadata in a session table
(default `agno_sessions`) and run records in a separate runs table (default `agno_runs`) loaded into
`session.runs` on hydration — **`runs` is not a column on the session table** — with a `session_type`
discriminator column distinguishing agent, team, and workflow sessions so one storage layer serves all
three owner types. Tables are created on first relevant operation if absent, `session_table` and
`runs_table` are independently configurable for environment isolation, and `get_session(session_id=...)`
exists identically on Agent, Team and Workflow. There is also storage control for choosing what gets
persisted and session summaries for condensing long conversations.

### HITL is a persisted pause with a three-level requirement tree

When a workflow pauses, three nested objects expose it. `WorkflowRunOutput` carries `is_paused`,
`pause_kind` (`"step"` | `"executor"` | `None`) and requirement collections naming what is pending:
`steps_requiring_confirmation` (confirm/reject), `steps_requiring_user_input` (values),
`steps_requiring_output_review`, `steps_requiring_route`, `steps_requiring_executor_resolution`. Below
that, a `StepRequirement` describes one step's requirement, and below that executor-level requirements
describe a tool-level gate (e.g. `tool_execution.requires_confirmation`, or `confirmation`/`confirmed`
pairs to approve a `@tool(requires_confirmation=True)`). The documented driver is a loop —
`while run_output.is_paused:` resolve the pending requirements, then
`run_output = workflow.continue_run(run_output)` — and because pause state is part of the persisted run
output, a pause survives the process.

### Tool hooks: ordered wrappers with injected run context, plus observe-only hooks

A tool hook receives the function name, the wrapped function call, and the arguments, and controls
execution by what it does with the callable: calling `function_call(**arguments)` continues the chain
(possibly with mutated arguments), while raising or returning another value stops or replaces it.
Hooks are assigned per agent or team (applying to every tool call that owner makes) or per tool via the
decorator; a list applies in order with the **first** hook as the outermost layer. A hook can declare a
supported parameter name to receive the `RunContext` — giving access to session state, dependencies and
metadata — and the documented pattern is to read a value out of `session_state` and rewrite an argument
before continuing. Alongside these, observe-only `pre_hook`/`post_hook` callbacks receive the
`FunctionCall` object; exceptions in them are logged and execution **continues**, and the docs state
explicitly that they are not equivalent to a wrapping gate — deliberate aborts need a supported
run-control exception such as `StopAgentRun`.

> Also fetched: `agents/usage/agent-with-structured-output.md` (`output_schema=<Pydantic model>` on the
> agent, with the parsed instance exposed as `RunOutput.content`; the same declaration site serves teams
> and workflows). Listed in the appendix.

---

## Mastra

Docs: `raw.githubusercontent.com/mastra-ai/mastra/main/docs/src/content/en/docs/{workflows/control-flow.mdx, workflows/suspend-and-resume.mdx, agents/processors.mdx, agents/guardrails.mdx, evals/gates-and-verdicts.mdx, subagents.mdx, agents/networks.mdx}`.

### Workflow as a typed chain DSL where step ids are the data keys

`createStep({id, inputSchema, outputSchema, execute})`, then `.then(step)`, `.parallel([...])`,
`.branch([...])`, `.map(fn)`, terminated by `.commit()`. Because every step declares zod input and
output schemas, the graph is type-checked at composition time, and **step ids are the addressing
scheme**: after `.parallel()` or `.branch()` the result is an object keyed by child step id, a following
step reads the previous value through `inputData`, and a specific result is retrieved with
`getStepResult(step)`. `.branch()` requires every branch to share the same `inputSchema` and
`outputSchema` because branching must keep schemas consistent; only the first branch whose condition is
true executes, and its output is keyed by that step's id. `.map()` exists precisely to reshape keyed
output into the next step's input shape. Failure handling is a declared property of the parallel step
rather than ad-hoc try/catch.

### Suspend and resume with schema-validated resume data

A step declares a `resumeSchema` in addition to `inputSchema`/`outputSchema`, and inside `execute`
calls `suspend()` when its resume condition is unmet. Suspension saves the current execution state as a
snapshot in the configured storage provider, so it persists across deployments and restarts.
`run.resume({step, resumeData})` restarts from that step with `resumeData` validated against the step's
`resumeSchema`; the step object may be passed for full type-safety or a step id string when the id comes
from user input or a database; when only one step is suspended the `step` argument can be omitted and
the last suspended step resumes; and resuming with only a `runId` requires `createRun({runId})` first.
Because `resume()` is an ordinary call it can be invoked from an HTTP endpoint, an event handler, or a
timer — which is what makes "wait for a human, an API callback, or a delay" one mechanism.

### Six processor seams, with an abort tripwire that can request a retry

Processors attach as `inputProcessors`/`outputProcessors` and expose up to six methods at different
granularities: `processInput` (once at the start; receives `messages`, `systemMessages`, `abort()`;
returns a replacement array or `{messages, systemMessages}`), `processInputStep` (at **every** step of
the agentic loop including tool-call continuations, enabling per-step model switching or tool-choice
changes), `processLLMRequest` (rewrite the request immediately before the provider call, with `prompt`,
`model`, `stepNumber`, `steps`, `state`, `abort()`), `processOutputStep` (after each LLM step, to
validate and optionally request a retry), `processOutputStream` (per streamed chunk — per-chunk drop
versus `abort()`), and `processOutputResult`. `abort()` is the tripwire; `abort('reason', {retry: true})`
requests a retry instead of a hard stop. Output methods share a `state` object living for one request,
**keyed by the processor's `id`** so each processor sees only its own data, created fresh per
`generate()`/`stream()`. Ordering relative to memory is documented and matters: an output guardrail
that calls `abort()` skips memory processors, so rejected messages are never saved. `prepareStep()` is a
shorthand that wraps `processInputStep()`.

### Guardrails as configured objects with a fail-open/fail-closed switch

Guardrails ship as configured processor instances. Input: `UnicodeNormalizer({stripControlChars, collapseWhitespace})`,
`PromptInjectionDetector({model, threshold, strategy, errorStrategy, detectionTypes})` (scans for
injection, jailbreak, system-override; classifies with an LLM; can block or rewrite),
`LanguageDetector({model, targetLanguages, threshold, strategy})`. Output: `BatchPartsProcessor({batchSize, maxWaitTime, emitOnNonText})`,
`SystemPromptScrubber({model, strategy, customPatterns, includeDetections, instructions, redactionMethod, placeholderText})`.
The reusable design decisions: a `strategy` field selecting behavior (rewrite / redact / translate /
block) instead of one hard-coded action; a numeric `threshold` for model verdicts; and `errorStrategy`
defaulting to `'warn'` — log the guardrail's own failure and continue with its fallback — with `'strict'`
available when unchecked content must not proceed, in which case the processor stops with a tripwire.
There is also a server-level default that redacts system prompts, tool definitions and API keys from
stream chunks.

### Evals as gates plus thresholds with a three-valued verdict

`runEvals({data, target, gates, scorers})` separates hard requirements from tracked metrics. **Gates**
must average 1.0 across all data items or the run fails; **scorers** are tracked metrics that may carry
a `threshold` — a number (minimum) or an object with `min`/`max`, where `max` exists specifically for
scorers where a high score is bad (hallucination, toxicity) and `{min, max}` expresses a band. The
verdict is computed after all items: `'failed'` if any gate averaged below 1.0, `'scored'` if all gates
passed but a threshold was missed, `'passed'` if everything held. Two details are the anti-laundering
core: the `verdict` field is **omitted entirely** when no gates or threshold-bearing scorers are
configured, or when every configured gate and threshold returned `notScorable()` (no numeric evidence) —
`scores` and `summary` are still returned, but nothing claims success; and a run with neither gates nor
scorers **throws**. Gates run before regular scorers on each item, a gate-only run is allowed
(`scorers` is optional when at least one gate is present, documented for deterministic CI checks), and
results expose `gateResults [{id, passed, score}]` and `thresholdResults [{id, passed, averageScore, threshold}]`
so CI can print exactly which gate failed.

### Delegation hooks that rewrite, bound, or reject a delegation

Subagents are declared on the parent as an `agents` map, and the parent decides when to delegate from
its own instructions plus each subagent's `description`. Delegation is interceptable through a
`delegation` option (in `defaultOptions` or per call) with `onDelegationStart(context)`, which returns
`proceed: true`, or `proceed: false` with a `rejectionReason` (handed back to the parent, which can then
synthesize instead), plus `modifiedPrompt` to rewrite what the subagent receives and `modifiedMaxSteps`
to bound the subagent's own iteration count. The context exposes `primitiveId`, `prompt`, `iteration`
and `requestContext` — and `iteration` is what lets a hook implement "reject delegation after N
iterations and force synthesis". Request context is **shallow-copied at the delegation boundary**,
excluding run-scoped identity keys, so entries set or deleted during the subagent run do not affect the
parent, while the hook can set entries on `context.requestContext` to pass values down.

---

## Where these land in hermes-day

The mechanisms cluster onto our existing lanes rather than asking for new architecture:

| Lane | Mechanisms worth importing |
|---|---|
| `hday_gate.py` | Claude permission modes + rule layering; AG2 `approval_required` fail-closed turn ending; Mastra `errorStrategy` warn/strict; CrewAI `(passed, value)` guardrail feedback |
| `hday_router.py` | OpenAI typed handoffs with `input_filter`; Agno `TeamMode`; Mastra `onDelegationStart`; AutoGen group-chat `RequestToSpeak` |
| `hday_ctxscore.py` | CrewAI composite scoring with recency half-life and importance; Agno session summaries |
| `hday_manifest.py` | Claude subagent frontmatter contract (incl. no-nesting); Agno `output_schema`; Mastra step schemas |
| `hday_gotchas.py` | Claude matcher semantics and compaction-lost boundaries; AG2 audit-log-first ordering |
| unified ledger | OpenAI typed span tree; AG2 open-kind audit log with arbiter-before/listener-after; CrewAI `HookDispatchedEvent` |
| cockpit UI | Mastra suspend/resume snapshots; Agno three-level requirement tree; AG2 `ObserverAlert` (ledger + cockpit, not the prompt) |
| test/CI story | Mastra gates + three-valued verdict with `notScorable()` omitting the verdict |

Two mechanisms are worth flagging as *highest leverage* for the stated goals. For "Jev-native typed
judgments everywhere", the OpenAI handoff contract and Agno's `output_schema` make every lane boundary a
validated typed object. For "LLM-native", the Mastra processor pipeline's six seams plus its
`abort(retry=True)` give per-step enforcement instead of per-turn enforcement — which is what our
ctxscore currently lacks.

---

## Honesty notes

* **Unreachable docs.** `docs.claude.com/en/api/agent-sdk/*.md` (the Agent SDK pages themselves)
  return the site's Next.js HTML shell rather than markdown, so those specific pages were not usable;
  Claude material here comes from `code.claude.com/docs/en/*.md` and
  `docs.claude.com/en/docs/claude-code/hooks.md`, both of which are served as clean markdown.
  `mastra.ai/docs` HTML was not used at all — the repo's `.mdx` sources were fetched instead. AG2's
  `website/docs/user-guide/**` is on the `main` branch, which is a moving target; AG2 also has no
  in-repo group-chat documentation anymore (only `network/migration_from_group_chat.mdx`), so
  group-chat mechanics are cited from AutoGen rather than AG2.
* **Version pinning.** CrewAI docs were fetched from the versioned path `docs/v1.15.22/en/...`, so
  those citations are pinned. OpenAI Agents SDK, Claude Code, AutoGen, AG2 and Mastra docs were fetched
  from default branches (`main`), so exact option names may drift; the mechanism shapes are stable.
* **404s.** Two API-reference pages, `docs/ref/guardrail.md` and `docs/ref/handoffs.md` in the
  openai-agents-python repo, returned empty responses. Neither is cited anywhere in the JSON or this
  document — API names for that SDK are taken from the prose docs (`guardrails.md`, `handoffs.md`,
  `sessions/index.md`, `tracing.md`, `tools.md`), which is why the citations point there.
* **Everything else is `high` confidence** in the JSON because each pattern was written from a document
  that was actually retrieved during this run. Nothing is attributed to a page that failed to fetch.
* **Not attempted:** running any of these SDKs. This is a documentation harvest — every mechanism is
  described precisely enough to implement from the spec, but none of it has been executed.

## Appendix — additional mechanisms found but cut for the 30-pattern cap

These were all fetched and verified during the harvest. Seven of them were written up as patterns and
then removed to respect the 30-pattern budget; one (CrewAI collaboration) was covered in the prose
above but never became a pattern. Each is available on request — the source documents are already on
disk.

| Framework | Mechanism | Source |
|---|---|---|
| AutoGen | Eleven composable termination conditions with `\|` composition, `reset()` semantics, `ExternalTermination` for UI stop, `HandoffTermination` as the pause-for-human mechanism | `.../agentchat-user-guide/tutorial/termination.ipynb` |
| AutoGen | Handoff as delegate tools: the tool call *is* the routing decision, and the receiver's context is parent messages + the assistant function call + a synthetic `FunctionExecutionResult` reading "Transferred to X. Adopt persona immediately." | `.../core-user-guide/design-patterns/handoffs.ipynb` |
| AG2 | `BaseObserver` + `Watch` (`EventWatch`, `CadenceWatch`, `DelayWatch`, `IntervalWatch`, `CronWatch`, `AllOf`, `AnyOf`, `Sequence`) separating *when* from *what*; `ObserverAlert` persisted but not rendered to the LLM | `.../advanced/observers.mdx`, `.../advanced/watches.mdx` |
| AG2 | Three-level progressive disclosure with a name-constrained `load_skill` tool, capability gating, and code-defined `MemorySkill` | `.../skills.mdx` |
| CrewAI | Event-driven checkpoints (`on_events=[...]`, JSON/SQLite providers, `max_checkpoints` pruning) with resume-versus-fork lineage | `.../concepts/checkpointing.mdx` |
| CrewAI | Auto-injected `Delegate work to coworker` / `Ask question to coworker` tools with `(task, context, coworker)` parameters | `.../concepts/collaboration.mdx` |
| Mastra | Guardrail processors as configured objects with `strategy` and a fail-open/fail-closed `errorStrategy` switch | `.../agents/guardrails.mdx` |
| Agno | `output_schema=<Pydantic model>` declared on the agent, with the parsed instance exposed as `RunOutput.content` | `docs.agno.com/agents/usage/agent-with-structured-output.md` |

Fetched corpus (71 documents, cleaned markdown) is in
`/home/decrux/.hermes/cache/scratch/masd/harvest/` for follow-up extraction.
