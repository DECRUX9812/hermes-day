#!/usr/bin/env python3
"""Rank the harvest clusters: score every pattern, bucket into build lanes.

Deterministic and transparent — no model calls. Reads runs/harvest/<cluster>.json,
writes runs/harvest/RANKED.md + ranked.json, prints a compact summary.
"""
import json, pathlib, re, collections

H = pathlib.Path("runs/harvest")
CLUSTERS = ["graph-durable", "multi-agent-sdks", "protocols-ui",
            "memory-context", "observability-safety"]
SURFACES = ["hday_", "ledger", "cockpit", "gate", "ctxscore", "router",
            "manifest", "patch", "plugin.js", "tests/"]
THEMES = {
    "durable-state":   ["checkpoint", "resume", "snapshot", "durable", "replay",
                        "time-travel", "persist", "memoiz", "idempot", "continue-as-new"],
    "hitl-approval":   ["interrupt", "approval", "human-in-the-loop", "hitl",
                        "elicitation", "consent", "auth_required", "input_required"],
    "typed-output":    ["structured", "schema", "typed", "validation", "pydantic",
                        "signature", "output_key", "json mode"],
    "memory-context":  ["memory", "context", "compaction", "retrieval", "summariz",
                        "kv-cache", "vacuum", "knowledge graph", "episodic"],
    "observability":   ["trace", "span", "score", "eval", "ledger", "telemetry",
                        "metric", "dataset", "trajectory"],
    "safety":          ["guardrail", "injection", "sandbox", "permission", "capability",
                        "moderation", "risk", "rail", "policy"],
    "interop":         ["mcp", "a2a", "ag-ui", "protocol", "adapter", "registry",
                        "event stream", "copilotkit", "otel"],
    "orchestration":   ["handoff", "subagent", "delegat", "fan-out", "group chat",
                        "routing", "workflow", "graph", "actor", "loop"],
    "retry-failure":   ["retry", "backoff", "timeout", "heartbeat", "failure",
                        "compensat", "circuit"],
}

def theme_of(p):
    blob = " ".join(str(p.get(k, "")) for k in ("name", "what", "why", "build")).lower()
    best, score = "other", 0
    for t, kws in THEMES.items():
        s = sum(blob.count(k) for k in kws)
        if s > score:
            best, score = t, s
    return best

def score(p):
    s = {"S": 3, "M": 2, "L": 1}.get(p.get("effort", "M"), 2)
    s += {"high": 2, "medium": 1}.get(p.get("confidence", "medium"), 1)
    build = str(p.get("build", "")).lower()
    s += 2 if any(x in build for x in SURFACES) else 0
    t = str(p.get("test", ""))
    s += 1 if ("::" in t or "test_" in t) else 0
    return s

rows, per_theme, per_cluster = [], collections.Counter(), collections.Counter()
for c in CLUSTERS:
    f = H / f"{c}.json"
    if not f.exists():
        print(f"MISSING {f}")
        continue
    data = json.loads(f.read_text())
    for p in data.get("patterns", []):
        th = theme_of(p)
        per_theme[th] += 1
        per_cluster[c] += 1
        rows.append({
            "score": score(p), "theme": th, "cluster": c,
            "name": p.get("name", ""), "framework": p.get("framework", ""),
            "effort": p.get("effort", "?"), "confidence": p.get("confidence", "?"),
            "source": p.get("source", ""), "what": p.get("what", ""),
            "build": p.get("build", ""), "test": p.get("test", ""),
        })

rows.sort(key=lambda r: (-r["score"], r["effort"], r["name"]))
(H / "ranked.json").write_text(json.dumps(rows, indent=1))

# lane grouping: top patterns per theme, best-effort budget of ~14 for the wave
LANES = {
    "durable-state":   "resume + checkpointing for long agent runs",
    "hitl-approval":   "interrupts/approvals that survive restarts",
    "safety":          "capability permissions + injection defenses",
    "memory-context":  "context hygiene + bi-temporal memory",
    "typed-output":    "typed judgments with schema retry",
    "observability":   "trajectory scoring + OTel-shaped spans",
    "interop":         "MCP/A2A/AG-UI adapter surface",
    "orchestration":   "handoff + subagent coordination",
    "retry-failure":   "retry budgets + failure compensation",
}
lane_pick = {}
for th in LANES:
    lane_pick[th] = [r for r in rows if r["theme"] == th][:3]

md = ["# Harvest — ranked", "",
      f"{len(rows)} patterns across {len(per_cluster)} clusters.", "",
      "## Theme histogram", ""]
md += [f"- **{t}** — {n} patterns" for t, n in per_theme.most_common()]
md += ["", "## Build lanes (top 3 per theme)", ""]
for th, desc in LANES.items():
    md += [f"### {th} — {desc}", ""]
    for r in lane_pick[th]:
        md += [f"- **{r['name']}** ({r['framework']}, {r['effort']}/{r['confidence']}, score {r['score']})",
               f"  - build: {r['build'][:300]}",
               f"  - test: {r['test'][:160]}",
               f"  - src: {r['source']}"]
    md += [""]
md += ["## Top 40 by score", ""]
for r in rows[:40]:
    md += [f"- {r['score']} · {r['theme']} · {r['name']} ({r['framework']}, {r['effort']})"]
(H / "RANKED.md").write_text("\n".join(md) + "\n")

print(f"patterns={len(rows)}  clusters={dict(per_cluster)}")
print("themes:", dict(per_theme.most_common()))
print("\nTOP 25:")
for r in rows[:25]:
    print(f"{r['score']:>2} {r['theme'][:14]:<14} {r['effort']} {r['name'][:58]:<58} {r['framework'][:20]}")
print("\nLANE PICKS:")
for th, picks in lane_pick.items():
    print(f"  {th}: " + " | ".join(p["name"][:40] for p in picks))
