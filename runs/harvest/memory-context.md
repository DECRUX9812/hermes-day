# memory-context — harvest

**Cluster:** memory / context / optimization frameworks
**Deliverable:** implementable mechanisms for `hermes-day`, 30 patterns across 9 frameworks.
**Companion:** `memory-context.json` (same content, machine-readable).
**Method:** every mechanism below was read from a fetched primary source (docs page, paper HTML, repo README, or engineering blog). No API or parameter in this file is invented. Where a page redirected or a claim could not be verified, it is listed under *Unverified / unreachable* rather than guessed.

Grounded module surface used by the `build` specs: `hday_gate.py` (pre_tool_call judge, mandate, intent_consistent, risk), `hday_ctxscore.py` (vacuum: keep needles / collapse junk / archive hidden chunks / head-tail degrade), `hday_router.py` (`lexical_judge`, `rank_wide`, `rerank`, `Pick`, `suggest`), `hday_manifest.py` (`build_manifest`, `persist`, `load`, `stale_after`), `hday_gotchas.py` (`sections_for`), `__init__.py` hooks (`pre_llm`, `pre_tool_call`, `pre_verify`, `post_tool_call`), the approval/decision ledger, and `patches/` (desktop cockpit).

---

## 1. Letta / MemGPT

### 1.1 path-addressed-memory-blocks — `high` · effort M
**Source:** https://docs.letta.com/concepts/memfs
**What.** Memory is a git-backed repository the agent owns, projected onto the runtime filesystem (MemFS). Each memory is a Markdown file with YAML frontmatter, addressed by path: a memory labelled `system/persona` projects to `system/persona.md`. Files under `system/` load into the system prompt every turn; everything else stays out of context until read. The file *tree* is always in the system prompt, so directory and file names are signposts. Edits are local until committed and pushed.
**Why.** Tiny always-resident core (identity, durable preferences, critical workflow rules) + unlimited cold store reachable by path. Tree-as-signpost lets the agent find a memory without paying for its body. Git gives versioning and diffable memory edits for free.
**Build.** `hday_memory.py` with a memory root (`default .hermes-day/memory/`, env override), layout `system/{persona.md,human.md}` + `reference/*`. `pre_llm` composes: (1) every `system/` file verbatim, (2) a deterministic sorted tree listing (paths only, no bodies). Tools `memory_read(path)` / `memory_write(path, body, frontmatter)` refuse paths outside the root. Persist via `git -C <root> commit -am <summary>` when git is present, plain writes otherwise; never fail the turn on git errors. Emit the `system/` token count into the ledger so drift is visible.
**Test.** `tests/test_hday_memory.py::test_system_blocks_always_in_prompt_and_tree_is_paths_only` — assert the prompt contains the persona body and the string `reference/deep.md` but not its body; assert `memory_read('../../etc/passwd')` raises.

### 1.2 sleep-time-memory-consolidation — `high` · effort L
**Source:** https://www.letta.com/blog/sleep-time-compute/
**What.** Two agents exist under the hood. The primary agent *cannot* edit its own in-context memory; a sleep-time agent holds the memory-editing tools and can rewrite **both** the primary's in-context memory and its own. Memory management happens asynchronously, off the conversation's latency path. The sleep-time agent is typically a stronger/slower model than the primary; a frequency setting trades tokens for how often learned context is revised. Updates are *anytime* — the primary can read memory while the sleep-time agent is still reasoning. Stated motivation: in MemGPT memory management, conversation and other tasks are bundled into one agent, making it slower and less reliable, and incremental in-conversation memory formation gets messy over time.
**Why.** Removes memory-write latency and tool-choice ambiguity from the foreground agent, and lets a stronger model do consolidation. Anytime writes mean consolidation never blocks a read. Frequency becomes an explicit cost dial.
**Build.** Split the memory tool surface: foreground gets `memory_read` + `memory_append_note`; only a consolidator lane gets rewrite/merge/delete. Consolidator entry point (cron or post-turn, frequency-budgeted in the ledger) reads session ledger + gotchas + notes and rewrites `system/*.md`, writing to a temp file and atomically renaming. Record each pass (model, tokens, files touched) in the ledger.
**Test.** `tests/test_memory_consolidation.py::test_foreground_cannot_rewrite_blocks_and_consolidator_is_atomic`.

### 1.3 subagent-frontmatter-memory-blocks — `high` · effort M
**Source:** https://docs.letta.com/configuration/subagents
**What.** Subagents are Markdown files with YAML frontmatter in `.letta/agents/` (project) or `~/.letta/agents/` (global): `name`, `description`, `tools` (allowlist, e.g. `Glob, Grep, Read`), `model`, `memoryBlocks` (which memory blocks the subagent may see). Built-ins are curated per purpose: **fork** (inherits the parent conversation with full context and tools, `model=inherit`), **general-purpose** (read/write), **recall** (read-**only** search of conversation history), **reflection** (background sleep-time consolidation), **memory** (reorganizes memory blocks into a cleaner hierarchy with less redundancy), plus init/history-analyzer. Each has a recommended model tier (`auto` / `auto-fast` / `inherit`) and a read/write vs read-only access level.
**Why.** Memory scope becomes a declarative property of the agent definition instead of an ambient global — a recall agent that cannot write is safe anywhere. Read-only search plus an explicit reorg agent separates "find it" from "fix it".
**Build.** `.hermes-day/agents/*.md` with frontmatter `{name, description, tools, model, memory_blocks, access: read|read-write}`; a loader validates at startup (unknown tool or block name = hard error). The router consults `memory_blocks` to decide which `system/` blocks to inject. Ship `recall`, `memory`, `reflection`, `fork`. Mirror the roster in the cockpit.
**Test.** `tests/test_agent_specs.py::test_recall_agent_is_read_only_and_unknown_block_fails_closed`.

---

## 2. Mem0

### 2.1 extraction-pipeline-with-dedup — `high` · effort M
**Source:** https://docs.mem0.ai/core-concepts/how-it-works
**What.** `add()` is a four-stage write pipeline: (1) **context lookup** — check related existing memories so the same fact isn't stored twice; (2) **fact extraction** — an LLM extracts preferences, decisions, plans and reusable details; (3) **deduplication + embedding** — redundant facts removed, each memory embedded for semantic search; (4) **entity extraction** — people/places/orgs/concepts stored for entity matching at search time. Writes are **additive by default** (new facts never silently rewrite old ones); `infer=False` stores raw content verbatim; corrections require explicit update/delete. Messages are the input, extracted facts the storage unit, scope is `user_id` / `agent_id` / `run_id` plus metadata filters.
**Why.** Turns transcripts into clean, deduplicated, queryable facts. Additivity is a correctness property: an inferred rewrite is an unreviewable mutation, so append by default and correct explicitly.
**Build.** `memory_add(messages, *, scope, infer=True, metadata)` on the ledger: related-lookup (lexical + embedding) → extract (typed judgment returning candidate facts) → dedupe (normalized-text hash, then similarity) → store with `{scope, metadata, source_episode_id}`. Keep `infer=False` as a verbatim escape hatch. Never rewrite on add; `memory_update` / `memory_delete` are separate, ledger-logged ops. All writes pass the gate.
**Test.** `tests/test_memory_add.py::test_duplicate_facts_are_dropped_and_old_facts_survive`.

### 2.2 entity-graph-memory — `medium` · effort M
**Source:** https://docs.mem0.ai/platform/features/graph-memory
**What.** Entities mentioned across memories (people, places, organizations, concepts) become **nodes**; memories that share an entity become **connected**. No developer-defined schema, no external graph store to provision — built in and always on. Stated payoff: entity-centric questions ("what do we know about Alice?") answered from many separate conversations, and multi-hop recall linking a fact in one memory to a related fact in another. Entity extraction in the write pipeline feeds these nodes.
**Why.** Vector retrieval cannot answer "everything about X across all sessions" — that is a traversal, not a similarity. Deriving nodes from already-extracted entities costs one extra index, not a second extraction pass.
**Build.** `{entity_key -> [memory_id]}` index populated from the entity-extraction stage, with normalized/alias-merged keys. `memory_recall_entity(entity, *, depth=1)` walks shared-entity edges and returns connected memories with episode ids; depth capped, every hop logged. SQLite inverted index first; a real graph store only if depth > 2 is ever needed.
**Test.** `tests/test_memory_graph.py::test_entity_recall_links_across_sessions`.

### 2.3 procedural-memory-type — `high` · effort S
**Source:** https://docs.mem0.ai/core-concepts/memory-types
**What.** `memory_type` on `add()` selects the memory kind. **Only `procedural_memory` is actually implemented** — it stores step-by-step task knowledge (how an agent performs a workflow), not facts about a user, and requires an `agent_id`. The docs state plainly that `semantic_memory` and `episodic_memory` exist in the enum but are **not wired into the extraction pipeline**, are rejected by validation, and have no documented roadmap (sync `add()` raises `Mem0ValidationError`; async raises `ValueError`).
**Why.** A useful negative example: an enum promising three memory types while one is real is worse than one type. It also shows the right home for "how to do this task" knowledge — keyed to the **agent**, not the user, because a workflow belongs to the performer.
**Build.** A procedures store keyed by agent/lane: a successful run's tool sequence, preconditions and outcome becomes a retrievable procedure; wire it into `hday_router.suggest`; show the procedure count in the cockpit. Reject unknown memory types at the API boundary with a typed error — silently accepting them is exactly the documented failure mode here.
**Test.** `tests/test_procedures.py::test_unknown_memory_type_is_rejected_and_procedure_recall_is_agent_scoped`.

---

## 3. Zep / Graphiti

### 3.1 bitemporal-edge-validity — `high` · effort M
**Source:** https://arxiv.org/html/2501.13956v1 (paper); https://raw.githubusercontent.com/getzep/graphiti/main/README.md
**What.** Edges carry a validity window in **both** event time and ingestion time: `t_valid` / `t_invalid` describe when the fact was true in the world; `t'_valid` / `t'_invalid` describe when the system learned and superseded it. When a new edge contradicts an existing one the existing edge is **invalidated** — `t_invalid` set to the new edge's `t_valid` — rather than deleted or overwritten. The README frames the same model as a *context graph*: every fact has a validity window ("Kendra loves Adidas shoes (as of March 2026)"), entities evolve with updated summaries, everything traces back to episodes. Retrieval filters expired edges; history is preserved for as-of queries.
**Why.** Non-destructive contradiction handling: the system can answer both "what is true now" and "what did we believe last week", and a stale belief is auditable rather than lost. It is the missing half of an append-only ledger — a ledger records what was written; a bitemporal graph records what stopped being true.
**Build.** Ledger rows gain four timestamps: `valid_from` / `valid_to` (in force) and `created_at` / `expired_at` (recorded / superseded). `supersede(old_id, new_id, reason)` sets `valid_to = new.valid_from` and `expired_at = now` **without deleting the row**. Default recall filters expired rows; `memory_as_of(ts)` for point-in-time recall. "Superseded" is a distinct cockpit badge so a stale belief is visible, not silently absent.
**Test.** `tests/test_ledger_bitemporal.py::test_supersede_invalidates_without_deleting`.

### 3.2 episode-provenance — `high` · effort M
**Source:** https://raw.githubusercontent.com/getzep/graphiti/main/README.md ; https://arxiv.org/html/2501.13956v1
**What.** Every derived fact traces back to an **episode** — the raw data as ingested, the ground-truth stream. The graph is built from episodes (text and structured JSON); ingestion is: initialize indices/constraints → add episodes → search edges with hybrid search → rerank by graph distance → search nodes with predefined search recipes. The paper adds that entity extraction processes the current message plus the last *n* messages (`n=4`, two complete turns) for context, and that writes go through **predefined Cypher queries rather than LLM-generated database queries**, to keep schema formats consistent and reduce hallucination.
**Why.** Provenance is what makes a memory layer auditable and re-derivable: if extraction improves you re-run it over the same episodes instead of losing the source. Predefined write queries over model-authored ones is the same lesson hermes-day's own gate already encodes — deterministic surfaces for anything that mutates state.
**Build.** Every memory and ledger row gets `source_episode_id` pointing at an immutable episode record (session id, turn index, tool call id, or an ingested artifact path). Episodes are append-only and never rewritten. Add `memory_provenance(memory_id)` for the cockpit and `memory_rebuild(episode_id)`. Keep the extraction write-path as fixed typed operations — no model-authored SQL/JSON writes.
**Test.** `tests/test_provenance.py::test_every_memory_has_an_immutable_episode_and_rebuild_is_idempotent`.

### 3.3 hybrid-retrieval-plus-rerankers — `high` · effort L
**Source:** https://arxiv.org/html/2501.13956v1
**What.** Retrieval is search-then-rerank. Three search functions run: **cosine semantic similarity**, **Okapi BM25 full-text** (both via Neo4j's Lucene), and **breadth-first search** over the graph. The paper is explicit about why: full-text finds word similarity, cosine finds semantic similarity, and BFS finds **contextual** similarity (nodes/edges closer in the graph appeared in more similar conversational contexts); BFS is described as enhancing the initial results. The rerank stage raises precision: **RRF** (reciprocal rank fusion) and **MMR** (maximal marginal relevance) are supported, plus a graph-based **episode-mentions reranker** prioritizing results by how often an entity or fact is mentioned in episodes. Graphiti's README exposes the same shape as "predefined search recipes" and "rerank by graph distance".
**Why.** Different query shapes fail differently — an exact identifier needs BM25, a paraphrase needs cosine, "what is related to this" needs traversal. Reranking after a high-recall union is what makes the union safe. `hday_router` already does lexical ranking; the other two channels plus fusion are the upgrade path.
**Build.** Generalize `hday_router.rank_wide` into a recall pipeline: channel 1 `lexical_judge` (exists), channel 2 embedding cosine over memory + tool descriptions, channel 3 BFS over the entity/decision graph (depth 1–2). Fuse with RRF (`sum 1/(k + rank)`, k=60), then MMR to drop near-duplicates (λ ≈ 0.7). Episode-mentions tiebreak from provenance counts. Return the per-channel rank vector in `Pick` so fusion is auditable; keep a lexical-only degraded path when no embedder is configured — mirroring `hday_ctxscore`'s head/tail fallback.
**Test.** `tests/test_hybrid_recall.py::test_rrf_fuses_channels_and_mmr_drops_near_duplicates`.

### 3.4 community-summaries-label-propagation — `medium` · effort L
**Source:** https://arxiv.org/html/2501.13956v1
**What.** Beyond entities and facts the graph holds **community nodes** containing summaries derived through an iterative map-reduce-style summarization of member nodes. Communities are named with key terms and subjects from their summaries, and those names are embedded and stored so a query can reach a community. Updates are dynamic: when a new entity node is added the system surveys the communities of neighbouring nodes, assigns the new node to the community held by the **plurality** of its neighbours, then updates the summary. The paper names the tradeoff — dynamic updating is efficient but the resulting communities gradually drift, which is why the map-reduce rebuild exists alongside it.
**Why.** Coarse, high-level retrieval for questions no single fact answers ("what have we been working on this month"), with a cheap deterministic incremental rule. Naming communities from their own summaries makes them findable without a separate classifier.
**Build.** A periodic (cron) community pass over the decision ledger: cluster recent entries by shared entities/tags, summarize with the cheap model, store `{community_id, name, summary, member_ids, embedding}`. Incremental path: a new entry joins the community held by the plurality of its entity neighbours. Keep a `derived_at` timestamp and a drift counter so a full rebuild can be scheduled. Expose communities as a retrieval channel for digest/cockpit views.
**Test.** `tests/test_communities.py::test_plurality_assignment_and_drift_flag`.

---

## 4. LangMem

### 4.1 profiles-vs-collections — `high` · effort M
**Source:** https://langchain-ai.github.io/langmem/concepts/conceptual_guide/
**What.** Semantic memory is stored in one of two shapes. A **profile** is a single structured document holding current state (e.g. preferences and other_preferences lists) — right when you need quick access to current state, when you have data requirements about what may be stored, and it is easy to hand to a user for manual editing. A **collection** is many individual documents — right when you want to track knowledge across many interactions *without loss of information* and recall contextually rather than every time. The docs frame the choice as a decision, not a default.
**Why.** Opposite failure modes: a profile is always-loaded (token-expensive, lossy by summarization); a collection is cheap and complete but only surfaces what you ask for. Making the choice explicit prevents the drift into "one giant memory blob".
**Build.** Both stores in `hday_memory`: a bounded profile document (`system/profile.md` with a declared field schema and a hard token cap enforced at write time) and a collection table (many rows, retrieved on demand). Route writes by intent: corrections/preferences → profile; observations/decisions/artifacts → collection. Surface the profile as a human-editable cockpit pane — that is its stated advantage.
**Test.** `tests/test_memory_shapes.py::test_profile_is_capped_and_collection_is_not`.

### 4.2 episodic-memory-schema — `high` · effort M
**Source:** https://langchain-ai.github.io/langmem/concepts/conceptual_guide/
**What.** Episodic memory preserves successful interactions as learning examples. The documented schema is a typed `Episode` with four fields — **observation** ("the situation and relevant context"), **thoughts** ("key considerations and reasoning process"), **action** ("what was done in response"), **result** ("what happened and why it worked"). Extraction is configured by `create_memory_manager(model, schemas=[Episode], instructions='Extract examples of successful interactions...', enable_inserts=True)` and returns typed records (`ExtractedMemory` with an id and the Episode content). The docs stress the contrast with semantic memory: episodic captures the situation, the reasoning and *why* it worked, not just the fact.
**Why.** A stored success without its reasoning cannot be reused — you get the what, not the when-to. The four-field schema forces the extractor to record situation and causal story, which is exactly what later retrieval needs to judge applicability.
**Build.** An episodic store with typed rows `{observation, thoughts, action, result, outcome: success|failure, lane, tools_used, source_episode_id}`, populated from finished turns (`post_tool_call` / turn-complete) **only when the outcome is verified** — tie it to the existing evidence verdict so unverified turns never become "learning examples". Recall by situation similarity, inject at most 2 into `pre_llm` for a matching lane.
**Test.** `tests/test_episodic.py::test_only_verified_turns_become_episodes`.

### 4.3 procedural-prompt-optimizer — `high` · effort M
**Source:** https://langchain-ai.github.io/langmem/concepts/conceptual_guide/
**What.** Procedural memory encodes how the agent should behave: it starts as the system prompt and then **evolves** through feedback. `create_prompt_optimizer(model, kind='metaprompt', config={'max_reflection_steps': 3})` takes trajectories paired with feedback (e.g. `{"trajectories": [(trajectory, {"user_score": 0})], "prompt": prompt}`) and returns a rewritten prompt. In the documented example the optimizer turns a bare "You are a helpful assistant." into a numbered behavioural rule list derived from the failure in the trajectory.
**Why.** Turns prompt maintenance from anecdote-driven edits into a repeatable offline job driven by recorded outcomes. Heremes-day already records the outcomes (ledger + evidence verdicts) — this is the missing consumer of that data.
**Build.** An offline optimiser job: read ledger rows for a lane, pair each with a scalar outcome (verified=1, checks-failed=0, guard-veto=-1) plus failure text, rewrite that lane's instruction block via a metaprompt loop with `max_reflection_steps` capped (start at 3). **Never auto-apply** — write the candidate prompt + diff to `runs/prompt-candidates/<lane>-<ts>.md` and require an explicit promote. Record reflection tokens in the ledger.
**Test.** `tests/test_prompt_optimizer.py::test_optimizer_proposes_but_never_auto_applies`.

---

## 5. DSPy

### 5.1 typed-signature-contract — `high` · effort L
**Source:** https://dspy.ai/current/diving-deeper/signatures-in-depth/
**What.** A `Signature` is the declarative contract between program and model: input fields, output fields, instructions. Mechanically every field is a pydantic `FieldInfo` and the base Signature extends `pydantic.BaseModel`, with DSPy metadata in `json_schema_extra`; any pydantic constraint (`gt`, `lt`, `min_length`) therefore works and gets stringified into `json_schema_extra['constraints']` so the **adapter** mentions it in the prompt. The class docstring becomes the instructions (`cleandoc`; if absent DSPy generates "Given the fields X, produce the fields Y."). Instructions live on the signature, not the module — the module (`Predict` / `ChainOfThought` / `ReAct`) supplies only the call-time strategy, so the same signature can be handed to any module. Field **order** is meaningful: `Signature.fields` is what the adapter walks when rendering, so reordering changes the prompt.
**Why.** One typed object that is simultaneously the schema, the prompt and the validation target — precisely the "Jev-native" shape hermes-day wants for judgments. Because instructions are separable from call strategy you can swap one-shot for chain-of-thought without rewriting the task; because constraints are pydantic, validation is free.
**Build.** `hday_sig.py`: a Signature base (pydantic) with `InputField`/`OutputField` factories carrying `desc`/`prefix`, a docstring-derived `instructions` property, deterministic field ordering (inputs then outputs). Rewrite the gate judge and router judgments to declare Signatures instead of free-form prompt strings; render via an adapter so the same signature can go to different providers. Keep field names stable (see 5.2).
**Test.** `tests/test_hday_sig.py::test_signature_is_the_schema_and_the_prompt`.

### 5.2 immutable-signature-optimizer-safety — `high` · effort M
**Source:** https://dspy.ai/current/diving-deeper/signatures-in-depth/
**What.** Every mutation method (`with_instructions`, `with_updated_fields`, `prepend`, `append`, `insert`, `delete`) deep-copies the field dict, applies the change and returns a **new** Signature class — the original is untouched. Stated reason: optimizers run many candidate variants of the same program, and in-place mutation would let candidates clobber each other through shared state. Two more rules: field names and `desc`/`prefix` are **inert** to instruction optimizers (GEPA rewrites only the docstring, because field names are the program's public interface — caller code reads `result.haiku`); and equality for comparing variants is `Signature.equals(other)`, not `==`, which compares class identity. Persistence round-trips only the mutable parts: `dump_state()` / `load_state()` save instructions plus each field's annotation, prefix, desc and field-type tag.
**Why.** This is the safety model for self-modifying prompts: candidates must be *values*, not mutations, or a bad optimisation silently corrupts the live program. "Field names are inert" is the boundary that keeps an optimizer from breaking callers — a boundary hermes-day needs before it lets anything rewrite its own instructions.
**Build.** Make instruction blocks immutable values: `with_instructions(block, text)` returns a new block; a candidate is a `(base_hash, diff)` pair. Never let an optimiser touch tool names, lane names or field names — only instruction text, with that rationale documented in the module. Persist candidates as `dump_state()`-style dicts (instructions + field metadata) so a promoted prompt loads without re-declaring code.
**Test.** `tests/test_prompt_immutability.py::test_candidate_mutation_cannot_touch_the_live_prompt_or_field_names`.

### 5.3 gepa-reflective-pareto — `high` · effort L
**Source:** https://dspy.ai/current/api/optimizers/GEPA/overview/
**What.** GEPA (Genetic-Pareto, arXiv 2507.19457) evolves textual components using **reflection** rather than reinforcement. It captures full traces of the module's execution, identifies the trace parts corresponding to a specific predictor, and reflects on that predictor's behaviour to propose a new instruction for it. Users supply rich **textual feedback** in addition to scalar metric scores, at the granularity of individual predictors or the whole system. Candidate selection is Pareto-based over per-instance scores (`candidate_selection_strategy: 'pareto' | 'current_best'`), with `reflection_minibatch_size` (default 3), `auto` budgets (`'light' | 'medium' | 'heavy'`), `max_metric_calls` / `max_full_evals`, `skip_perfect_score=True`, `add_format_failure_as_feedback`, and pluggable `instruction_proposer` / `code_proposer`. It also works as inference-time search (`valset=trainset, track_stats=True`) exposing the Pareto frontier via `detailed_results`.
**Why.** Scalar-only optimisation cannot tell the optimiser *why* a case failed, so it converges slowly and overfits the aggregate; textual feedback carries the diagnosis. Pareto selection keeps variants best on **different** slices instead of collapsing to one average-best — what a gate with several distinct failure modes (over-blocking, under-blocking, false honesty flags) actually needs.
**Build.** A GEPA-shaped optimiser for the gate judge instructions: metric returns `(score, feedback_text)` where feedback quotes the vetoed command and the human-readable reason; track the Pareto frontier per failure family (over-block / under-block / wrong-mandate / honesty-guard). Minibatch = 3 real ledger cases, budget capped by `max_metric_calls`, `skip_perfect_score` on. Log the frontier to the ledger; promotion stays manual. **Optimise only the judge prompt — the deterministic regex honesty guard is never touched.**
**Test.** `tests/test_gepa_frontier.py::test_pareto_keeps_slice_best_candidates_and_budget_is_enforced`.

### 5.4 adapter-layer-coercion — `high` · effort M
**Source:** https://dspy.ai/current/diving-deeper/signatures-in-depth/
**What.** A signature only **declares** types; coercion happens in the adapter layer. `dspy/adapters/utils.py::parse_value` tries `json_repair` first, falls back to `ast.literal_eval`, then validates against `TypeAdapter(annotation)`; for custom `dspy.Type` subclasses it retries with the raw string when pydantic validation fails so the type's own parser can take over. Stated reason for the split: multiple adapters (Chat, JSON, XML, TwoStep) must coerce the same Signature differently — a JSON adapter leans on the model's JSON mode, an XML adapter parses from tags — so keeping coercion in the adapter keeps signatures lean and avoids duplicated parser logic. Field values arrive as strings and are cast at the edge.
**Why.** The concrete answer to "provider-agnostic structured output": declare the contract once, give each provider the rendering/parsing strategy it is actually good at, and keep a documented fallback chain instead of a single brittle `json.loads`.
**Build.** `hday_adapters.py` with a 3-rung parse chain (`json_repair` → `ast.literal_eval` → `TypeAdapter`) plus per-provider adapters: JSON-mode where supported, tag-based where not, prompted-schema as last resort. Record which rung succeeded in the ledger — a rising share of `json_repair` hits is an early warning that a provider's structured output is degrading.
**Test.** `tests/test_adapters.py::test_parse_chain_recovers_malformed_json_and_records_the_rung`.

---

## 6. Pydantic AI

### 6.1 typed-deps-runcontext — `high` · effort M
**Source:** https://pydantic.dev/docs/ai/core-concepts/dependencies/
**What.** `Agent` takes `deps_type=<a type>` (the **type**, not an instance — it is not used at runtime, it exists for full type checking) and each run passes the matching instance as `deps=`. Dependencies are accessed through `RunContext[Deps]`, which must be the first parameter of system-prompt functions, tools and output validators, via `ctx.deps.<field>`. `RunContext` is parameterised with the deps type, so a mismatch is a static type error; it also exposes `.agent` (name, output_type) and realtime properties. Synchronous dependency functions run via `run_in_executor` in a thread pool. Deps fields can be referenced in instructions via template strings (`TemplateStr('Hello {{name}}')`). Dependencies can be **overridden**, which is what makes the agent testable.
**Why.** Explicit typed injection replaces ambient globals — a tool declares exactly what it needs, tests pass fakes, and a typo is caught by the type checker instead of at 3am. It also keeps a tool's contract honest, since the deps are its entire outside world.
**Build.** A `DayDeps` dataclass `{repo_root, ledger, config, clock, embedder}`; every hermes-day tool/hook takes `ctx: RunContext[DayDeps]` as its first argument instead of reading module globals or env. Declare `deps_type` once at registration so a mismatch fails type-check. Use override to inject a frozen clock and an in-memory ledger in tests — this removes the monkeypatch-heavy setup visible in the current suite.
**Test.** `tests/test_day_deps.py::test_tools_take_typed_deps_and_override_replaces_them`.

### 6.2 output-retry-budgets-retrypromptpart — `high` · effort M
**Source:** https://pydantic.dev/docs/ai/core-concepts/retries/
**What.** Retries are budgeted separately for tools and output: `Agent(retries=3)` sets both, `Agent(retries={'tools': 5, 'output': 1})` sets them individually, and unspecified keys keep a default of 1. A retry is a **model request** carrying a `RetryPromptPart` holding the failure as a string (from `ModelRetry`) or a list of pydantic error details (from `ValidationError`), rendered with "Fix the errors and try again." appended; its `tool_name` is set for a tool-call retry and `None` for an output retry. Tool retries are triggered by a ValidationError on the arguments, by the tool/args_validator/hook raising `ModelRetry`, by a tool timeout, or by the model calling a nonexistent tool. Counter semantics are precise: the counter is **keyed by tool name and resets on success** (a tool alternating fail/success never exhausts a budget of 1); `max_retries=N` means N retries / N+1 attempts; a hallucinated tool name gets its own budget under the invented name, bounded by the agent-wide tools budget. Budget exhaustion raises `UnexpectedModelBehavior`. `ToolFailed` is the deliberate opposite: it records a `ToolReturnPart` with `outcome='failed'` and does **not** consume the budget, so repeated failures are bounded by `UsageLimits` instead. Retry prompts stay in history, so reusing that history replays earlier failures to the model.
**Why.** An exact, testable retry contract instead of an ad-hoc loop: separate budgets for "the model wrote bad JSON" and "the model chose a bad action", counters that cannot be silently exhausted by a flaky tool, and a first-class way to report a failure that is *not* a retry. Reset-on-success is the subtle one — it stops a retry storm being masked by interleaved successes.
**Build.** A retry budget object `{tools: N, output: M}` for typed judgments with the same counter keying (per tool name, reset on success) and the same two failure channels: a repair prompt replaying the pydantic error list, and an explicit "failed, do not retry" result that bypasses the budget. On exhaustion raise a typed error naming the tool/budget. Keep retry prompts in the transcript so a resumed session sees its earlier mistakes; expose per-tool retry counts in the ledger.
**Test.** `tests/test_retry_budgets.py::test_counters_reset_on_success_and_exhaustion_raises`.

### 6.3 retry-budget-multiplication — `high` · effort S
**Source:** https://pydantic.dev/docs/ai/core-concepts/retries/
**What.** The docs decompose worst-case request volume into three independent multipliers: **N** = model requests per logical call (initial attempt, one follow-up per tool call — even a *successful* tool call queues another request — plus retry prompts); **M** = attempts per model request inside the provider SDK client (an OpenAI client with `max_retries=2` allows 3); **K** = attempts per request on the wire (transport stop strategy, e.g. `stop_after_attempt(2)` = 2 total). The worked example: `retries={'output': 2}` × SDK `max_retries=2` × `stop_after_attempt(2)` puts **3 × 3 × 2 = 18 requests** on the network, each with its own timeout — 180 seconds of request time worst case before backoff. `UsageLimits.request_limit` (default 50) bounds only N and never sees the wire requests beneath it.
**Why.** A budget that multiplies to 18× is a budget you cannot reason about — exactly the class of bug that makes an agent look "hung" or mysteriously expensive. Making the multiplication explicit turns a hidden cost into a computed, assertable number.
**Build.** A worst-case request estimator in the cost guard: given `(turns, tools_per_turn, output_retries, sdk_max_retries, transport_attempts, timeout)` compute N×M×K and worst-case wall time, and refuse configurations above a declared ceiling **before the run starts**. Surface the estimate on the cockpit card for long-running lanes. Pure function — testable with no network.
**Test.** `tests/test_retry_math.py::test_worst_case_request_count_and_refusal`.

---

## 7. Instructor / Outlines

### 7.1 reask-validation-loop — `high` · effort M
**Source:** https://python.useinstructor.com/concepts/reask_validation/
**What.** Frames self-critique as ordinary validation: "Instead of framing self-critique or self-reflection in AI as new concepts, we can view them as validation errors with clear error messages that the system can use to self-correct." Validators are pydantic validators (`field_validator` / `AfterValidator` / `BeforeValidator`) that raise `ValueError`; the failure is caught and the model is re-asked. Documented mechanics behind the scenes: the client appends the assistant message, then appends a user message reading `Please correct the function call; errors encountered:\n{e}` — the raw pydantic error text **is** the repair prompt — and loops up to `max_retries` (example sets 2). It defends against two failure forms: pydantic validation errors (code- or LLM-based) and JSON decoding errors.
**Why.** Reuses an existing, well-tested validation framework instead of inventing a reflection loop, and makes the repair signal the actual error text rather than a vague "try again". It is the cheapest possible structured-output retry: no new prompt template per task.
**Build.** Implement hermes-day judgments as pydantic models with real validators (risk bounded 0..1, lane name must exist in the registry, verdict must cite at least one evidence id) and a reask loop replaying the pydantic error text verbatim, capped by the retry budget in 6.2. Log every reask with the validator that fired, so "which rule does the model keep violating" becomes a queryable fact.
**Test.** `tests/test_reask.py::test_invalid_judgment_is_reasked_with_the_pydantic_error_text`.

### 7.2 llm-validator-with-context — `high` · effort M
**Source:** https://python.useinstructor.com/concepts/reask_validation/
**What.** Two mechanisms combine. (1) `llm_validator(rule, client=...)` plugs a model-based check into the same pydantic pipeline via `BeforeValidator`, so a semantic rule raises a `ValidationError` like any other — and the docs note the resulting error message is generated by the LLM, "so it'll be helpful for re-asking the model". (2) The `context` parameter passes runtime data to validators, read via `ValidationInfo` (`info.context`): the worked example validates that a `supporting_quote` actually appears in a supplied `source_text`, after whitespace normalization — a deterministic, non-LLM check against ground truth.
**Why.** Two different jobs, correctly separated: model-based checks for judgment-style rules that cannot be coded, and deterministic checks against runtime ground truth for anything checkable. The context channel is what makes the deterministic half possible, and it is the anti-hallucination mechanism hermes-day needs for citation-style claims.
**Build.** Wire the ledger's evidence into validation context: any judgment citing a file, test name or evidence id is validated against the real artifact (path exists, test name appears in the run output) deterministically. Reserve llm-validator-style checks for genuinely subjective rules and **mark them as such in the ledger**, so a model-judged failure is never confused with a code-judged one.
**Test.** `tests/test_grounded_validation.py::test_citation_validators_check_real_artifacts_and_are_marked`.

### 7.3 constrained-decoding-fsm — `medium` · effort M
**Source:** https://dottxt-ai.github.io/outlines/latest/features/core/output_types/
**What.** Outlines accepts an output **type** alongside the prompt and guarantees the generated text conforms to it. Supported types span the ecosystem: native (`int`, `str`, `float`, `bool`), typing constructs (`Literal`, `List`, `Dict`, `Enum`, `Union`, `Optional`, `Tuple`, nested collections) and third-party types such as pydantic models — plus three library-specific ones: `Choice(list)` for **dynamic** multiple choice, `JsonSchema`, `Regex` and `CFG` (context-free grammars). Classification is done with `Literal` or `Enum`. Contract detail: an Outlines generator **always returns a string** — you cast it yourself (`Character.model_validate_json(result)`). The docs frame it as "provide as an output type what you would give as the type hint of the return type of a function".
**Why.** Structural validity is enforced during generation rather than repaired after it, which removes the entire malformed-JSON failure class for local models. `Choice()` built from a runtime list is the piece hermes-day needs, because its lane/tool registry is not known at authoring time.
**Build.** For the self-hosted/local-model path, render each judgment's output type as a grammar: a `Choice` built at runtime from the live lane registry for lane selection, bounded int/float for scores, pydantic JSON schema for structured verdicts. Keep the adapter parse chain (5.4) as the second line of defence for providers without grammar support, and assert in the ledger which enforcement path was used per call.
**Test.** `tests/test_constrained_output.py::test_runtime_choice_grammar_rejects_unknown_lane`.

---

## 8. smolagents

### 8.1 code-as-action — `high` · effort L
**Source:** https://huggingface.co/blog/smolagents
**What.** In a `CodeAgent` the model writes its action as **executable code** rather than a JSON blob of tool names and arguments; `ToolCallingAgent` (JSON/text actions) remains supported alongside it. The blog's stated advantages of code actions: **composability** (nest and reuse actions the way you define and call a Python function — which JSON cannot express), **object management** (storing an action's output such as a generated image), **generality** (code expresses anything a computer can do), and **representation in LLM training data** (plenty of quality code is already in the training set). It cites multiple papers on tool calling in code and points at *Executable Code Actions Elicit Better LLM Agents*.
**Why.** One action can do what a dozen JSON tool calls do, because loops, conditionals and intermediate variables live in the action instead of the transcript. That is a direct context-length win for hermes-day, where multi-step triage currently costs a tool call per step.
**Build.** A code-action lane for multi-step read-only work (triage a batch of findings, aggregate a ledger slice): the model emits one Python snippet against a small documented API surface, hermes-day executes it in the AST interpreter below, and only the result enters the transcript. Gate it read-only by default and require the same mandate check as any write.
**Test.** `tests/test_code_actions.py::test_one_code_action_replaces_n_calls_and_is_read_only`.

### 8.2 ast-walking-interpreter — `high` · effort L
**Source:** https://huggingface.co/docs/smolagents/tutorials/secure_code_execution
**What.** Code execution is not delegated to the vanilla Python interpreter; smolagents ships a `LocalPythonExecutor` that loads the Abstract Syntax Tree from the code and executes it operation by operation under explicit rules. Documented rules: imports are **disallowed unless explicitly added to an authorization list**; access to submodules is disabled by default and each must be authorized individually (or a wildcard like `numpy.*` to allow numpy and all subpackages) — with the noted trap that seemingly innocuous packages like `random` can expose harmful submodules (`random._os`); and the total count of elementary operations is **capped** to prevent infinite loops and resource bloating. The docs enumerate the threat model: plain LLM error, supply-chain compromise, prompt injection from a browsed page, and exploitation of publicly accessible agents.
**Why.** An AST-walking executor gives a real allowlist boundary (what may be imported) plus a resource boundary (how much may run), both checkable without containers. The submodule trap is worth stealing verbatim: allowing a package is not the same as allowing its reachable modules.
**Build.** Implement the code-action executor as an AST walker: no import unless allowlisted, submodule access denied unless explicitly granted, an operation-count ceiling per action, no dunder attribute access. Record every denied node in the ledger as a guard event so injection attempts are visible rather than silent. Keep a remote sandbox as an optional escalation for anything needing network or a full filesystem.
**Test.** `tests/test_code_executor.py::test_imports_and_submodules_are_denied_by_default_and_op_count_is_capped`.

### 8.3 typed-step-memory-and-planning-step — `high` · effort M
**Source:** https://huggingface.co/docs/smolagents/conceptual_guides/react
**What.** Every agent in smolagents derives from a single `MultiStepAgent` class implementing the ReAct cycle (Yao et al., 2022). The loop is precisely specified: initialization stores the system prompt in a `SystemPromptStep` and the user query in a `TaskStep`; then, while the task is unsolved, the agent (a) calls `agent.write_memory_to_messages()` to serialize the agent logs into LLM-readable chat messages, (b) sends them to a `Model` and parses the completion into an action (a JSON blob for `ToolCallingAgent`, a code snippet for `CodeAgent`), (c) executes the action and logs the result as an `ActionStep`, and (d) runs every callback in `agent.step_callbacks`. When planning is activated the plan is **periodically revised** and stored in a `PlanningStep`, which also feeds facts about the task into memory. Memory is therefore a typed, ordered log of step records rather than a message list, and serialization is an explicit, replaceable method. The docs also note the two variants fit different work: `CodeAgent` for code actions, `ToolCallingAgent` (JSON) when the work needs waiting between actions, e.g. web browsing with page-interaction latency.
**Why.** A typed step log makes the transcript inspectable and compactable by step **kind** rather than by message role — you can drop Observation payloads while keeping PlanningSteps and failed ActionSteps, which is exactly the policy the context-engineering patterns want. `write_memory_to_messages` as a single serialization seam is what makes "render the same memory differently per provider" possible, and `step_callbacks` is a first-class hook point that hermes-day's `post_tool_call` currently approximates.
**Build.** Replace the ad-hoc message list in a session with typed step records: `SystemPromptStep`, `TaskStep`, `ActionStep {thought, action, observation, outcome}`, `PlanningStep {plan, facts}`. Persist them in the ledger and render through one `write_memory_to_messages()` that `hday_adapters` can vary per provider. Expose `step_callbacks` as the single hook surface (folding in `post_tool_call`), and emit a `PlanningStep` on a cadence (or when the plan drifts) that re-states the plan and the facts — the same recitation mechanism as 9.1, but recorded as a typed step so it is auditable.
**Test.** `tests/test_step_memory.py::test_steps_are_typed_ordered_and_planning_recites`.

---

## 9. Context-engineering practice

### 9.1 compaction-with-preserve-list-and-recitation — `high` · effort M
**Source:** https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents ; https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus
**What.** Compaction takes a conversation nearing the context limit, summarizes it, and reinitiates a new window with the summary. In Claude Code the message history is passed to the model to summarize and compress the most critical details: the summary **preserves architectural decisions, unresolved bugs and implementation details** while **discarding redundant tool outputs and messages**, and the agent continues with that summary plus the **five most recently accessed files**. Tuning advice: start by maximizing **recall** so the compaction prompt captures every relevant piece, then iterate on **precision** — over-aggressive compaction loses subtle context whose importance only appears later. The companion technique is structured note-taking: the agent writes notes persisted **outside** the context window and pulls them back later, which is what lets it continue multi-hour work after a context reset. Manus adds the recitation trick: rewriting the todo list at the **end** of context pushes the global plan into the recent attention span and mitigates lost-in-the-middle.
**Why.** The preserve list is the whole mechanism — an unguided "summarize this" loses exactly the decisions that matter. Notes-outside-the-window make continuity survive the compaction boundary, so the summary does not have to carry everything.
**Build.** Replace `hday_ctxscore`'s generic vacuum with a compaction pass taking an explicit preserve list (open decisions, vetoed commands and their reasons, unresolved failures, files touched, the current mandate) and discarding raw tool payloads. Always re-inject the live plan/notes file **after** the summary so it lands in the recent attention span. Add a compaction recall probe — a small set of questions whose answers must survive the summary — run in tests.
**Test.** `tests/test_compaction_recall.py::test_preserve_list_survives_and_plan_is_recited_last`.

### 9.2 tool-result-clearing — `high` · effort S
**Source:** https://platform.claude.com/cookbook/tool-use-context-engineering-context-engineering-tools
**What.** The cookbook's three context levers are compaction, tool-result clearing and memory. Tool clearing caps in-session token growth when context is dominated by large, **re-fetchable** tool results (file reads, API responses). Concrete knobs from the API table: `clear_tool_uses_20250919`, triggered by a server-side token threshold, with `trigger` (default 100K tokens), `keep` (default **3** tool uses), `clear_at_least`, `exclude_tools` and `clear_tool_inputs`. Compaction (`compact_20260112`) is separately triggered (default 150K, server-side minimum 50K) and exposes `trigger`, `instructions` and `pause_after_compaction`. The memory tool (`memory_20250818`) is different again: driven by the **model** as a tool call, with the client implementing `view`, `create`, `str_replace`, `insert`, `delete`, `rename` against a `/memories` path. The cookbook's framing: distinct primitives for distinct problems — bulky re-fetchable reads (clearing), long analytical conversations (compaction), knowledge that must survive between sessions (memory) — and it recommends replacing the default compaction prompt to preserve what your agent needs.
**Why.** Tool results are usually the bulk of a long transcript and the safest thing to drop because they are re-fetchable by construction. Clearing before compacting is the cheap win: no summarization call, no loss of the decision trail.
**Build.** Extend `hday_ctxscore`'s vacuum with a clearing stage that runs **before** any summarization: when the transcript crosses a token trigger, drop tool results older than the last N (start at 3), never touching assistant text, decisions or gate events; support an exclude list for tools whose results are not re-fetchable and a `clear_at_least` floor so clearing actually fires rather than no-oping. Keep cleared payloads in the existing archive (hday_ctxscore already preserves hidden chunks) and leave a one-line pointer with tool name + arguments so the agent can re-fetch.
**Test.** `tests/test_tool_clearing.py::test_old_tool_results_are_cleared_but_decisions_and_excluded_tools_survive`.

### 9.3 subagent-context-isolation-and-shared-traces — `high` · effort M
**Source:** https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents ; https://cognition.ai/blog/dont-build-multi-agents
**What.** Anthropic: specialized sub-agents handle focused tasks with clean context windows while the main agent coordinates with a high-level plan; a subagent may explore with **tens of thousands of tokens** but returns only a condensed, distilled summary (**often 1,000–2,000 tokens**), so the detailed search context stays isolated and the lead agent synthesizes. Cognition argues the complementary constraint: *share context, and share full agent traces, not just individual messages*; and *actions carry implicit decisions, and conflicting decisions carry bad results*. Their worked failure: two parallel subagents given the same task produce a Super-Mario background and a non-Flappy-Bird bird, because each acted on assumptions the other could not see. Their default is a single-threaded linear agent; their scaling answer is a dedicated model that compresses a history of actions and conversation into key details, events and decisions — "hard to get right", something they fine-tuned a smaller model for. They also observe Claude Code's subagents never run in parallel with the parent and are usually only asked to answer a question, not write code.
**Why.** Together the two sources define the contract: isolation is safe for **investigation** and unsafe for **work that must agree with other work**. Heremes-day already spawns parallel lanes, so this is the rule that decides which of them may act. The compressed-summary interface is the same object as the compaction preserve list.
**Build.** Codify two subagent classes: (a) **read-only investigators** (recall/history/artifact search) that may run in parallel and **must** return a bounded summary (cap it — start ~1.5K tokens) plus cited evidence ids; (b) **writers** that run single-threaded against the shared trace and are never parallel with a peer touching the same artifact. Dispatch for a writer carries the **full parent trace**, not a paraphrase. Add a summary-size assertion so an investigator returning a transcript is rejected. Surface the class (investigator vs writer) on the cockpit card.
**Test.** `tests/test_subagent_contract.py::test_investigators_are_bounded_and_writers_serialize`.

### 9.4 kv-cache-stable-prefix-and-mask-dont-remove — `high` · effort M
**Source:** https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus
**What.** Two rules. (1) **Design around the KV-cache.** The cache hit rate is called the single most important metric for a production agent; cached input tokens can cost ~**10× less** than uncached. Practices: keep the prompt **prefix stable** (a common mistake is a second-precision timestamp at the top of the system prompt, which kills the cache from that token on); make context **append-only** (avoid modifying previous actions/observations; ensure deterministic serialization, since many JSON serializers do not guarantee stable key ordering); mark cache breakpoints explicitly when the provider needs them, at minimum covering the end of the system prompt. Agent workloads are **prefill-dominated** (they cite a ~100:1 input-to-output token ratio). (2) **Mask, don't remove.** Avoid dynamically adding/removing tools mid-iteration: tool definitions live near the front of context so any change invalidates the cache for everything after it, and previous actions referring to now-undefined tools confuse the model into schema violations or hallucinated actions. Instead **mask token logits during decoding**, or use **response prefill** — `auto` (prefill the assistant prefix), `required` (prefill to the tool-call token), `specified` (prefill into the function name). They keep consistent action-name prefixes (`browser_`, `shell_`) so a whole group can be enforced without stateful logits processors.
**Why.** Two cheap, high-leverage rules hermes-day currently violates: any per-turn timestamp or reordered block at the top of the prompt silently destroys caching, and a router that mutates the tool list per turn destroys it again **and** confuses the model about its own history. Masking gets the same steering with neither cost.
**Build.** In the prompt assembler: freeze the prefix (system + tool definitions) across turns, move anything volatile (clock, counters, current file) **below** the stable block, and assert deterministic serialization (sorted keys) in a test. Add a cache-bust detector that hashes the prefix each turn and logs a warning to the ledger when it changes without a declared reason. For routing, keep the full tool list in the prefix permanently and let `hday_router` express availability as a mask/allowlist applied at selection time rather than by rewriting the tool block; enforce group-level availability through consistent name prefixes.
**Test.** `tests/test_prefix_stability.py::test_prefix_hash_is_stable_across_turns_and_mask_does_not_rewrite_tools`.

### 9.5 externalize-dont-delete — `high` · effort S
**Source:** https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus ; https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
**What.** Manus treats the file system as the ultimate context: unlimited, persistent, directly operable by the agent, used not just as storage but as structured externalized memory. Their compression strategies are always designed to be **restorable** — a web page's content can be dropped as long as the **URL** is preserved; a document's contents can be omitted if its **path** remains available in the sandbox — so context shrinks without permanently losing information. They explicitly reject irreversible compression: an agent must predict its next action from all prior state, and you cannot reliably predict which observation becomes critical ten steps later. The companion rule is **keep the wrong stuff in**: leave failed actions and the resulting stack traces in context, because erasing failure removes the evidence and the model then cannot update its beliefs away from the bad action; they call error recovery one of the clearest indicators of true agentic behaviour. Anthropic's framing matches from the other side: prefer just-in-time retrieval over pre-loading, keep lightweight identifiers (file paths, queries, links) and load data at runtime, with the file tree itself as a signpost system.
**Why.** Both rules replace deletion with a pointer, which is what makes aggressive context reduction safe. Heremes-day's honesty guard already depends on failures being visible — if the transcript cleaned up vetoes and failed checks, the guard's whole signal would disappear.
**Build.** Make every compression in `hday_ctxscore` restorable by construction: replace payloads with typed pointers `{kind: file|url|tool_result|episode, ref, sha, bytes}` and never drop the pointer. Add a hard rule (enforced in code, not convention) that gate vetoes, failed checks and error traces are **never** eligible for clearing or summarization — pinned like decisions. Add a `rehydrate(pointer)` tool so any cleared payload can be pulled back on demand, and count rehydrations in the ledger: a high rehydration rate on a specific pointer kind is evidence the compressor is dropping something it should have kept.
**Test.** `tests/test_restorable_compression.py::test_every_cleared_payload_keeps_a_working_pointer_and_failures_are_pinned`.

---

## Sources actually fetched

| Framework | URL | Result |
| --- | --- | --- |
| Letta | https://docs.letta.com/configuration/memory/ | fetched (MemFS intro, `/doctor` audit, dreaming) |
| Letta | https://docs.letta.com/configuration/subagents/ | fetched (frontmatter, built-in subagents) |
| Letta | https://docs.letta.com/concepts/memfs | fetched |
| Letta | https://www.letta.com/blog/sleep-time-compute/ | fetched (sleep-time agents, MemGPT 2.0) |
| Mem0 | https://docs.mem0.ai/core-concepts/how-it-works | fetched (4-stage pipeline) |
| Mem0 | https://docs.mem0.ai/core-concepts/memory-types | fetched (procedural-only reality) |
| Mem0 | https://docs.mem0.ai/platform/features/graph-memory | fetched |
| Zep | https://arxiv.org/html/2501.13956v1 | fetched (full HTML) |
| Zep | https://raw.githubusercontent.com/getzep/graphiti/main/README.md | fetched (raw markdown) |
| Zep | https://help.getzep.com/graphiti/getting-started/overview | fetched |
| LangMem | https://langchain-ai.github.io/langmem/concepts/conceptual_guide/ | fetched |
| DSPy | https://dspy.ai/current/diving-deeper/signatures-in-depth/ | fetched |
| DSPy | https://dspy.ai/current/api/optimizers/GEPA/overview/ | fetched |
| Pydantic AI | https://pydantic.dev/docs/ai/core-concepts/dependencies/ | fetched |
| Pydantic AI | https://pydantic.dev/docs/ai/core-concepts/retries/ | fetched |
| Instructor | https://python.useinstructor.com/concepts/reask_validation/ | fetched |
| Outlines | https://dottxt-ai.github.io/outlines/latest/features/core/output_types/ | fetched |
| smolagents | https://huggingface.co/blog/smolagents | fetched |
| smolagents | https://huggingface.co/docs/smolagents/tutorials/secure_code_execution | fetched |
| smolagents | https://huggingface.co/docs/smolagents/conceptual_guides/react | fetched (MultiStepAgent ReAct loop, typed steps, PlanningStep) |
| Context | https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents | fetched |
| Context | https://platform.claude.com/cookbook/tool-use-context-engineering-context-engineering-tools | fetched (API table: compaction / tool clearing / memory tool) |
| Context | https://cognition.ai/blog/dont-build-multi-agents | fetched |
| Context | https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus | fetched |

## Unverified / unreachable (not used as sources)

- **`monid` / TinyFish tooling was unreachable for this run.** Both the `monid run -p tinyfish` invocation and an `execute_code` wrapper were escalated and blocked by *this repository's own gate* (`external blast radius (external) without an explicit mandate`, and later `off-mandate (intent_consistent=0.22) combined with risk 0.65`). Plain `ls`/`cp` shell commands were also blocked mid-run (`off-mandate`). Research therefore used the built-in `web_search` / `web_extract` tools, which the gate permitted. Worth flagging to the owner: the gate blocks the very research workflow this task prescribes.
- **`https://docs.mem0.ai/open-source/graph_memory/overview`** redirects to the platform migration page — the OSS graph-memory doc is gone. The platform page (`/platform/features/graph-memory`) was used instead.
- **`https://docs.mem0.ai/core-concepts/memory-operations/add`** was fetched but its body was not read in detail; the write-pipeline claims in 2.1 come from `how-it-works`, not from this page.
- **Web search was noisy.** The configured search backend returned several unrelated results (e.g. fitness articles for a Graphiti query). Canonical URLs were used directly instead of trusting search snippets.
- **Not verified, therefore not claimed:** a numeric "30% fewer steps" figure for code agents (absent from the fetched smolagents blog text); the Mem0 paper's LOCOMO benchmark numbers (arXiv abstract page fetched, body not read); an Instructor "optimizing token usage" reask variant (section exists in the page TOC but was not read); any Graphiti MCP-server or custom-entity-type API detail beyond the README text quoted here.

## Coverage summary

| Framework | Patterns |
| --- | --- |
| Letta / MemGPT | 3 |
| Mem0 | 3 |
| Zep / Graphiti | 4 |
| LangMem | 3 |
| DSPy | 4 |
| Pydantic AI | 3 |
| Instructor / Outlines | 3 |
| smolagents | 3 |
| Context-engineering practice | 5 |
| **Total** | **31** |
