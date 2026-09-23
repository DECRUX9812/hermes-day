#!/usr/bin/env python3
"""Deterministic validator for the multi-agent-sdks harvest deliverable.

Asserts the strict deliverable contract from the task brief:
  {cluster, patterns:[{name, framework, source, what, why, build, effort, test, confidence}]}
plus the brief's coverage rules (18..30 patterns, 3..6 per framework, every briefed
framework present, the named mechanisms actually covered) and the honesty rules
(unreachable documentation recorded, every source URL on a host we really fetched).

Exit 0 only if every assertion holds. No network, no side effects.
Usage: python3 runs/harvest/validate_multi_agent_sdks.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HARVEST = Path(__file__).resolve().parent
JSON_PATH = HARVEST / "multi-agent-sdks.json"
MD_PATH = HARVEST / "multi-agent-sdks.md"

CLUSTER = "multi-agent-sdks"
REQUIRED = ("name", "framework", "source", "what", "why", "build", "effort", "test", "confidence")
EFFORTS = {"S", "M", "L"}
CONFIDENCE = {"high", "medium", "low"}

# The frameworks the brief named, mapped to the label(s) used in the artifact.
# AutoGen and AG2 are two projects and are counted separately here; the brief
# grouped them as one "AutoGen / AG2" slot.
REQUIRED_FRAMEWORKS = {
    "openai-agents-sdk": {"openai-agents-sdk"},
    "claude-agent-sdk": {"claude-agent-sdk"},
    "autogen/ag2": {"autogen", "ag2"},
    "crewai": {"crewai"},
    "agno": {"agno"},
    "mastra": {"mastra"},
}
KNOWN_LABELS = set().union(*REQUIRED_FRAMEWORKS.values())

# Heading regexes for the md companion, one per briefed framework group.
HEADING_RE = {
    "openai-agents-sdk": r"OpenAI Agents SDK",
    "claude-agent-sdk": r"Claude Agent SDK",
    "autogen/ag2": r"AutoGen",
    "crewai": r"CrewAI",
    "agno": r"Agno",
    "mastra": r"Mastra",
}

# Mechanisms the brief named by name; each must be covered by some pattern.
REQUIRED_TOPICS = {
    "oai.handoffs": r"handoff",
    "oai.guardrails": r"guardrail|tripwire",
    "oai.sessions": r"session",
    "oai.tracing_spans": r"span",
    "oai.hosted_tools": r"HostedMCPTool|WebSearchTool|hosted tool",
    "claude.subagents": r"subagent",
    "claude.hooks_lifecycle": r"hook",
    "claude.permission_modes": r"permission mode|acceptEdits|bypassPermissions",
    "claude.mcp": r"mcp__|MCP",
    "claude.skills": r"SKILL\.md|skill",
    "ag2.group_chat": r"RequestToSpeak|group chat",
    "ag2.tool_use": r"call_tool|tool_hook|tool",
    "ag2.governance": r"audit",
    "crewai.crews_vs_flows": r"crew|flow",
    "crewai.delegation": r"delegat",
    "crewai.memory": r"memory",
    "crewai.guardrails": r"guardrail",
    "agno.agent_teams": r"TeamMode|team",
    "agno.sessions": r"session_id|session",
    "mastra.workflows": r"workflow|createStep",
    "mastra.agent_network": r"onDelegationStart|subagent|delegation",
    "mastra.evals": r"runEvals|verdict",
}

# Hosts actually fetched during this harvest.
ALLOWED_HOSTS = {
    "raw.githubusercontent.com",
    "code.claude.com",
    "docs.claude.com",
    "docs.agno.com",
}

failures: list[str] = []
checks = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if not cond:
        failures.append(f"{label}{(': ' + detail) if detail else ''}")


# ---------------------------------------------------------------- files exist
check("json file exists", JSON_PATH.is_file(), str(JSON_PATH))
check("md file exists", MD_PATH.is_file(), str(MD_PATH))
if failures:
    print("\n".join(f"FAIL {f}" for f in failures))
    sys.exit(1)

# ---------------------------------------------------------------- json parses
try:
    doc = json.loads(JSON_PATH.read_text(encoding="utf-8"))
except json.JSONDecodeError as exc:  # pragma: no cover - defensive
    print(f"FAIL json does not parse: {exc}")
    sys.exit(1)

check("top-level cluster name", doc.get("cluster") == CLUSTER, repr(doc.get("cluster")))
check("top-level 'patterns' is a list", isinstance(doc.get("patterns"), list))
patterns = doc.get("patterns") or []

# ---------------------------------------------------------------- pattern count
check("pattern count in 18..30", 18 <= len(patterns) <= 30, f"got {len(patterns)}")
check("pattern_count field matches len(patterns)",
      doc.get("pattern_count") == len(patterns),
      f"{doc.get('pattern_count')} vs {len(patterns)}")

# ---------------------------------------------------------------- per-pattern
seen_names: set[str] = set()
for i, p in enumerate(patterns):
    where = f"patterns[{i}]"
    check(f"{where} is an object", isinstance(p, dict))
    if not isinstance(p, dict):
        continue
    missing = [k for k in REQUIRED if k not in p]
    check(f"{where} has all required keys", not missing, f"missing {missing}")
    extra = [k for k in p if k not in REQUIRED]
    check(f"{where} has no extra keys", not extra, f"extra {extra}")
    for k in REQUIRED:
        v = p.get(k)
        check(f"{where}.{k} non-empty string", isinstance(v, str) and bool(v.strip()), repr(v)[:60])
    check(f"{where}.effort in {sorted(EFFORTS)}", p.get("effort") in EFFORTS, repr(p.get("effort")))
    check(f"{where}.confidence in {sorted(CONFIDENCE)}",
          p.get("confidence") in CONFIDENCE, repr(p.get("confidence")))
    name = p.get("name", "")
    check(f"{where}.name unique", name not in seen_names, name)
    seen_names.add(name)
    check(f"{where}.framework is a known label",
          p.get("framework") in KNOWN_LABELS, repr(p.get("framework")))
    # every source must be exactly one clean https URL (no fragments, no commas)
    src = p.get("source", "")
    check(f"{where}.source is a single clean https url",
          bool(re.fullmatch(r"https://\S+", src)) and " " not in src and "," not in src,
          src[:90])
    # build must name a concrete hermes-day artifact
    build = p.get("build", "")
    check(f"{where}.build names a concrete artifact",
          bool(re.search(r"hday_[a-z_]+|tests/|desktop/|cockpit|ledger", build)),
          build[:80])
    # test must describe a deterministic check
    test = p.get("test", "")
    check(f"{where}.test is a deterministic check",
          bool(re.search(r"assert|deterministic|fixture|golden", test, re.IGNORECASE)),
          test[:80])
    # spec must be concrete enough to implement from: mechanism names, not vibes
    check(f"{where}.what is substantive",
          len(p.get("what", "")) >= 200, f"{len(p.get('what', ''))} chars")
    check(f"{where}.build is substantive",
          len(build) >= 150, f"{len(build)} chars")

# ---------------------------------------------------------------- frameworks
per_fw: dict[str, int] = {}
for p in patterns:
    per_fw[p["framework"]] = per_fw.get(p["framework"], 0) + 1

labels = set(per_fw)
check("no unknown framework labels", not (labels - KNOWN_LABELS),
      f"extra {sorted(labels - KNOWN_LABELS)}")
for brief_name, accepted in REQUIRED_FRAMEWORKS.items():
    n = sum(per_fw.get(lbl, 0) for lbl in accepted)
    check(f"framework group '{brief_name}' covered", n >= 3, f"got {n}")
    check(f"framework group '{brief_name}' has <=6 patterns", n <= 6, f"got {n}")
for lbl in sorted(labels):
    check(f"label '{lbl}' has 3..6 patterns", 3 <= per_fw[lbl] <= 6, f"got {per_fw[lbl]}")

# ---------------------------------------------------------------- topic coverage
# Coverage is asserted against the mechanism-bearing prose only. Source URLs, names
# and test descriptions are deliberately excluded: a topic must be *described* to
# count, not merely named in a filename.
blob = json.dumps(
    [{k: p.get(k, "") for k in ("what", "why", "build")} for p in patterns],
    ensure_ascii=False,
)
for topic, rx in REQUIRED_TOPICS.items():
    check(f"topic covered: {topic}", bool(re.search(rx, blob, re.IGNORECASE)))

# ---------------------------------------------------------------- honesty
check("sources_fetched recorded as a positive int",
      isinstance(doc.get("sources_fetched"), int) and doc["sources_fetched"] > 0,
      repr(doc.get("sources_fetched")))
check("unreachable list present and non-empty",
      isinstance(doc.get("unreachable"), list) and len(doc["unreachable"]) >= 1)
check("unreachable entries name a URL and a reason",
      all("http" in u and "\u2014" in u for u in doc.get("unreachable", [])))

all_urls = {p["source"] for p in patterns if isinstance(p.get("source"), str)}
bad_hosts = sorted({re.sub(r"^https://([^/]+).*$", r"\1", u) for u in all_urls} - ALLOWED_HOSTS)
check("all source URLs on fetched allowlist", not bad_hosts, f"unexpected hosts {bad_hosts}")
# the two known-404 pages must not be cited as sources
check("no pattern cites a 404 page",
      not any("docs/ref/" in u for u in all_urls),
      str([u for u in all_urls if "docs/ref/" in u]))

# ---------------------------------------------------------------- md companion
md = MD_PATH.read_text(encoding="utf-8")
check("md is non-trivial", len(md) > 8000, f"{len(md)} chars")
check("md documents the pattern total", str(len(patterns)) in md, f"{len(patterns)} not found in md")
check("md records unreachable documentation", "Unreachable" in md or "unreachable" in md)
for brief_name in REQUIRED_FRAMEWORKS:
    head = HEADING_RE[brief_name]
    check(f"md has a section for {brief_name}",
          bool(re.search(rf"^## .*{head}", md, re.IGNORECASE | re.MULTILINE)),
          head)
check("md maps mechanisms onto hermes-day lanes", "hday_gate" in md and "hday_router" in md)

# ---------------------------------------------------------------- report
print(f"{CLUSTER} harvest: {len(patterns)} patterns, {len(all_urls)} unique source URLs")
print("per-framework:", dict(sorted(per_fw.items())))
print(f"assertions run: {checks}")
if failures:
    print(f"\n{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  FAIL {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
