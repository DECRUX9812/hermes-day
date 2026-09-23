# observability-safety — harvest notes

Companion to `observability-safety.json` (30 patterns, 6 frameworks, 3–6 each).
Target: hermes-day (`hday_gate` / `hday_ctxscore` / `hday_router` / `hday_manifest` / `hday_gotchas`,
hooks `pre_tool_call` / `post_tool_call` / `pre_llm_call` / `pre_verify` / `on_session_finalize`,
per-session decision ledger, cockpit panels + `day-*` commands, ~267 tests).

## Method and honesty notes

- Everything cited was fetched during this harvest with the built-in `web_extract` tool.
  The repo's own gate blocked terminal network egress in this session
  (`monid`/`curl` → "external blast radius … no human approval gate is reachable"), so TinyFish
  via monid was unavailable; the documented fallback (built-in search/extract) was used instead.
- One pattern is **medium** confidence and says so in the JSON: `prompt-guard-injection-classifier`
  (the HF model card is access-gated; the mechanism is documented, the model's scores were not verified).
- Where a page's body was nav-only (NIST landing page, Langfuse masking/playground, OpenAI moderation
  header) it is listed as fetched-but-not-mined and no pattern depends on it.
- Unreachable / unusable sources: `github.com/humanlayer/humanlayer` README now states the code is
  deprecated (points to humanlayer.com) → no HumanLayer pattern was specced;
  the OTel semconv GenAI pages have **moved** out of `open-telemetry/semantic-conventions` into
  `open-telemetry/semantic-conventions-genai` — cite the new repo, not the old site paths.

## Pattern index (30)

| # | pattern | framework | effort | confidence |
|---|---|---|---|---|
| 1 | observation-tree | Langfuse | S | high |
| 2 | typed-scores | Langfuse | S | high |
| 3 | dataset-experiments | Langfuse | M | high |
| 4 | prompt-version-labels | Langfuse | M | high |
| 5 | events-only-scores | Langfuse | S | high |
| 6 | experiment-loop | Braintrust | M | high |
| 7 | scorer-scope | Braintrust | M | high |
| 8 | online-scoring-rules | Braintrust | M | high |
| 9 | span-type-taxonomy | Braintrust | S | high |
| 10 | span-naming | OpenTelemetry GenAI | S | high |
| 11 | genai-attributes | OpenTelemetry GenAI | M | high |
| 12 | evaluation-result-events | OpenTelemetry GenAI | S | high |
| 13 | content-capture-optin | OpenTelemetry GenAI | M | high |
| 14 | genai-metrics | OpenTelemetry GenAI | M | high |
| 15 | swe-bench-verified | Agent evaluation benchmarks | L | high |
| 16 | tau-bench-passk | Agent evaluation benchmarks | M | high |
| 17 | gaia-levels | Agent evaluation benchmarks | M | high |
| 18 | webarena-functional-validators | Agent evaluation benchmarks | M | high |
| 19 | trajectory-evals | Agent evaluation benchmarks | M | high |
| 20 | nemo-rails-taxonomy | Guardrail frameworks | S | high |
| 21 | self-check-rail | Guardrail frameworks | M | high |
| 22 | validator-chain-onfail | Guardrail frameworks | S | high |
| 23 | llama-guard-taxonomy | Guardrail frameworks | M | high |
| 24 | prompt-guard-injection-classifier | Guardrail frameworks | M | medium |
| 25 | dual-llm-quarantine | Prompt injection / agent security | M | high |
| 26 | camel-capabilities | Prompt injection / agent security | L | high |
| 27 | control-flow-integrity | Prompt injection / agent security | L | high |
| 28 | privilege-control-progent | Prompt injection / agent security | L | high |
| 29 | sandbox-runtime-os-isolation | Prompt injection / agent security | M | high |
| 30 | human-approval-gates | Prompt injection / agent security | M | high |

## Source ledger (fetched, and what was taken)

### Langfuse
| Source | Taken |
|---|---|
| https://langfuse.com/docs/observability/data-model | observation/trace/session model; trace attrs copied onto every row |
| https://langfuse.com/docs/evaluation/scores/overview | 4 score data types; 5 creation paths; TEXT excluded from aggregation |
| https://langfuse.com/docs/evaluation/experiments/datasets (reached via /docs/datasets/overview) | dataset items, expected output, experiment runs |
| https://langfuse.com/docs/prompt-management/features/prompt-version-control | versions + labels for env/tenant/experiment routing |
| https://langfuse.com/docs/prompt-management/get-started | prompt↔trace linkage, prompt caching |
| https://langfuse.com/docs/evaluation/evaluation-methods/llm-as-a-judge | observation-level vs experiment evaluators; trace-level deprecated |
| https://langfuse.com/changelog/2026-08-17-langfuse-v4 | v4 exists (165× faster claim); the page does **not** document `events_only` |
| fetched, not mined: /docs/observability/features/masking, /features/playground, /features/link-to-traces, /docs/observability/features/observation-types | nav-heavy bodies |

Local verified knowledge used for `events-only-scores`: the `langfuse-events-only-scores` skill
(v4 events_only 404s legacy endpoints; `POST /api/public/ingestion` score-create; `GET /api/public/v3/scores`;
uuid5 idempotency; poll-with-deadline). Its claims were confirmed against a live self-hosted instance
in a prior session — mark this pattern as repo-verified rather than doc-verified.

### Braintrust
| Source | Taken |
|---|---|
| https://www.braintrust.dev/docs/evaluate/best-practices | change one variable at a time; nondeterminism; keep baseline current; segment by metadata; eval loop |
| https://www.braintrust.dev/docs/evaluate/write-scorers | scorers grade the span/trace they attach to; code vs LLM-judge |
| https://www.braintrust.dev/docs/evaluate/score-online | scope trace/span, idle timeout (default 30 s), SQL filters, sampling rate |
| https://www.braintrust.dev/docs/observe/examine-traces | span-type table (eval/task/llm/function/tool/score/…) |
| https://www.braintrust.dev/docs/best-practices/agents | adaptive sampling; end-to-end vs component evals; feed low scores back into datasets |
| fetched, not specced: /docs/evaluate/autoevals, /docs/evaluate/run-evaluations, /docs/evaluate/llm-as-a-judge, /docs/deploy/prompts, /docs/loop | library/feature pages |

### OpenTelemetry GenAI
Canonical (new repo): https://github.com/open-telemetry/semantic-conventions-genai
| Source | Taken |
|---|---|
| raw …/docs/gen-ai/gen-ai-spans.md | span naming `{gen_ai.operation.name} {gen_ai.request.model}`; required attrs; content attrs are **Opt-In**; external-storage pattern |
| raw …/docs/gen-ai/gen-ai-agent-spans.md | `create_agent {gen_ai.agent.name}`, `invoke_agent …`; span kind CLIENT vs INTERNAL |
| raw …/docs/gen-ai/gen-ai-spans.md (execute_tool section) | `execute_tool {gen_ai.tool.name}`, gen_ai.tool.name required, gen_ai.tool.call.id |
| raw …/docs/gen-ai/gen-ai-events.md | `gen_ai.evaluation.result` + name/label/value, parenting rule |
| raw …/docs/gen-ai/gen-ai-metrics.md | `gen_ai.client.operation.duration`, `gen_ai.invoke_agent.duration/.inference_calls/.tool_calls`, `gen_ai.execute_tool.duration` |

### Agent evaluation benchmarks
| Source | Taken |
|---|---|
| https://arxiv.org/abs/2310.06770 | SWE-bench: 2,294 issues, 12 Python repos, execution-based scoring; Claude 2 solved 1.96% |
| https://openai.com/index/introducing-swe-bench-verified/ | Verified = 500 human-screened samples; easy 196 (<15 min) / hard 45 (>1 h); containerized Docker harness |
| https://www.swebench.com/ + https://swebench.com/SWE-bench/ | family (Lite, Multilingual, Multimodal), harness docs, mini-SWE-agent |
| https://arxiv.org/abs/2406.12045 | τ-bench: simulated user + APIs + policy; DB-state vs goal-state; pass^k; gpt-4o <50%, pass^8 <25% retail |
| https://arxiv.org/abs/2311.12983 | GAIA: 466 questions, human 92% vs GPT-4+plugins 15%, 300-answer holdout |
| https://arxiv.org/abs/2307.13854 | WebArena: reproducible self-hosted envs (4 domains + tools + KBs), functional-correctness scoring |
| https://raw.githubusercontent.com/langchain-ai/agentevals/main/README.md | trajectory evaluators, TRAJECTORY_ACCURACY_PROMPT, {key, reasoning, score} |
| https://arxiv.org/abs/2410.10934 | Agent-as-a-Judge (step-level judging) |
| also fetched: 2506.07982 (τ²-bench), 2308.03688 (AgentBench), 2405.15793 (SWE-agent), HF SWE-bench_Verified, github tau-bench | context |

### Guardrails
| Source | Taken |
|---|---|
| https://raw.githubusercontent.com/NVIDIA/NeMo-Guardrails/develop/README.md | 5 rail types (input/dialog/retrieval/execution/output); reject **or alter**; config.yml + `.co` flows; `self check facts` / `self check hallucination`; sensitive-data masking |
| https://arxiv.org/abs/2310.10501 | NeMo Guardrails paper (programmable rails, Colang) |
| https://raw.githubusercontent.com/guardrails-ai/guardrails/main/README.md | `Guard().use(validator, on_fail=OnFailAction.X)`, hub validators, Pydantic structured output |
| https://huggingface.co/meta-llama/Llama-Guard-3-8B | Llama Guard 3: MLCommons hazard taxonomy, 8 languages, tool-call safety, first-token unsafe probability |
| https://arxiv.org/abs/2312.06674 | Llama Guard: prompt + response classification, taxonomy, binary decision scores, customizable taxonomy |
| https://huggingface.co/meta-llama/Prompt-Guard-86M | Prompt Guard 86M: injection vs jailbreak; fine-tune on app data; layer protections — **card is gated, medium confidence** |
| https://arxiv.org/abs/2407.21772 | ShieldGemma: harm types (sexual, dangerous, harassment, hate), +10.8% AU-PRC vs Llama Guard |
| https://platform.openai.com/docs/guides/moderation | `omni-moderation-latest` accepts text+images, free to use, category list |

### Prompt injection / agent security
| Source | Taken |
|---|---|
| https://simonwillison.net/2023/Apr/25/dual-llm-pattern/ | dual-LLM / quarantined LLM, symbolic-only answers |
| https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/ | private data + untrusted content + external comms as the risk conjunction |
| https://arxiv.org/abs/2503.18813 | CaMeL: control/data-flow extraction, capabilities, tool-call policy enforcement; 77% vs 84% AgentDojo |
| https://arxiv.org/html/2506.08837v3 | six patterns incl. action-selector, plan-then-execute ("control flow integrity"), LLM map-reduce, context-minimization |
| https://arxiv.org/abs/2403.14720 | Spotlighting: delimiting / datamarking / encoding as provenance signals |
| https://arxiv.org/abs/2504.11703 | Progent: symbolic privilege rules over tool name+args, deterministic check, LLM-generated + dynamic policy |
| https://arxiv.org/abs/2402.06363 | StruQ (structured queries; fetched, not specced) |
| https://github.com/anthropic-experimental/sandbox-runtime | srt: sandbox-exec/bubblewrap, proxy network allowlist, fs/socket restrictions, MCP-server sandboxing, macOS-only violation log |
| https://genai.owasp.org/llmrisk/llm01-prompt-injection/ | LLM01 mitigations #1–#7 incl. privilege control and "require human approval for high-risk actions" |
| https://langchain-ai.github.io/langgraph/concepts/human_in_the_loop/ (→ docs.langchain.com/oss/python/langgraph/interrupts) | `interrupt()` semantics: dynamic pause, checkpointer, thread_id cursor, `Command` resume, `stream.interrupts` |
| https://arxiv.org/abs/2406.13352 | AgentDojo: 97 tasks, 629 security cases, extensible attack/defense environment |
| https://csrc.nist.gov/pubs/ai/100/2/e2025/final | NIST AI 100-2e2025 exists (Mar 2025, adversarial ML taxonomy) — body is the PDF; not mined |

## Bonus mechanisms (fetched, not specced — cheap to add later)

- **Spotlighting** (https://arxiv.org/abs/2403.14720): delimit / datamark / encode untrusted input so
  provenance survives concatenation. Natural fit for `hday_ctxscore` chunk headers; would be a 4th
  pattern in the security cluster.
- **AgentDojo** (https://arxiv.org/abs/2406.13352): 97 tasks / 629 security cases, utility *and*
  attack-success scoring. The right harness if the repo ever builds a "gate under injection" suite;
  also the evaluation vehicle CaMeL reports against.
- **autoevals** (https://www.braintrust.dev/docs/evaluate/autoevals): prebuilt scorers (factuality,
  semantic, JSON validity) — a drop-in start for `hday_scorers.py` code scorers.
- **ShieldGemma** (https://arxiv.org/abs/2407.21772) and **OpenAI omni-moderation**
  (https://platform.openai.com/docs/guides/moderation, free): output-side content moderation options
  if cockpit-generated summaries ever need classification.
- **NIST AI 100-2e2025** (https://csrc.nist.gov/pubs/ai/100/2/e2025/final): authoritative vocabulary
  for the adversarial-ML threat model; PDF not fetched, so nothing cites it.
- **τ²-bench / AgentBench / SWE-agent**: additional benchmark lineage fetched for context only.

## Suggested build order (dependency-aware)

1. `observation-tree` + `span-type-taxonomy` + `span-naming` — pure ledger schema, unlocks every later view.
2. `typed-scores` + `evaluation-result-events` + `scorer-scope` — the Jev-native score/event spine.
3. `dataset-experiments` + `experiment-loop` + `tau-bench-passk` — make rule changes falsifiable.
4. `nemo-rails-taxonomy` + `validator-chain-onfail` + `llama-guard-taxonomy` — rails + judge-output hygiene.
5. `human-approval-gates` + `privilege-control-progent` + `sandbox-runtime-os-isolation` — enforcement.
6. `dual-llm-quarantine` + `control-flow-integrity` + `camel-capabilities` — the structural defenses (L effort; land last).
