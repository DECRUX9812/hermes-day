# Harvest — next-level harness research → build

**Mandate (user):** "get this thing to next level shit… research the best agentic
frameworks, get all of them, patch this into that… Jev-native, LLM-native,
cross-native harvest."

## The three natives (acceptance bar)

- **Jev-native** — every harvested capability exposes *typed judgments*, not
  booleans: it routes through the existing judgment lanes (`hday_gate`,
  `hday_ctxscore`, `hday_manifest`) and writes rows to the one decision ledger.
  No shadow state, no second source of truth.
- **LLM-native** — provider-agnostic: structured outputs with schema validation +
  retry, streaming, and model routing that works for any provider (cloud or
  local), with a deterministic offline path so every feature is testable without
  a network call.
- **Cross-native** — cross-protocol (MCP / A2A / AG-UI adapters), cross-surface
  (desktop cockpit / TUI / Discord all read the same event stream), cross-framework
  (the harvested patterns are implemented in-repo, not vendored dependencies).

## Pipeline

1. **Harvest** — 5 research clusters, each writing `runs/harvest/<cluster>.json`:
   `graph-durable`, `multi-agent-sdks`, `protocols-ui`, `memory-context`,
   `observability-safety`. Every pattern cites a source URL that was actually
   fetched; no invented APIs.
2. **Rank** — score each pattern on (impact × feasibility × testability) and cut
   the list to what one build wave can actually land verified.
3. **Build** — one lane per pattern, TDD: failing test first, then the smallest
   implementation that passes; RED-prove each new test against the pre-fix code.
4. **Verify** — full suite green, independent verifier re-runs RED/GREEN, deploy
   through the fail-closed script, prove the live effect.
5. **Ship** — merge to `main`, push to `DECRUX9812/hermes-day`, update the demo
   numbers so the video's claims stay true.

## Rules (carried from this project's history)

- Self-reports don't count: a claim needs raw output from a runner.
- Never weaken a test or an assertion to make something pass.
- The free regex honesty guard stays intact — harvested features may *add*
  enforcement, never subtract it.
- One ledger. If a new feature needs decision rows, they use the existing shape.
- Every new module ships with a deterministic offline path (no network in tests).

## Pattern schema (per cluster JSON)

```json
{"cluster": "name",
 "patterns": [{"name": "", "framework": "", "source": "https://...",
   "what": "", "why": "", "build": "", "effort": "S|M|L",
   "test": "", "confidence": "high|medium"}]}
```
