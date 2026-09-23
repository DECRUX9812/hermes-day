#!/usr/bin/env python3
"""Deterministic validator for the observability-safety harvest deliverable.

Asserts the strict deliverable contract:
  {cluster, patterns:[{name, framework, source, what, why, build, effort, test, confidence}]}

Plus: 18-30 patterns, 3-6 per framework, every framework named in the brief present,
every mechanism named in the brief covered by some pattern, sources are real http(s)
URLs, and the .md companion exists and names the cluster.

Exit 0 only if every assertion holds. No network, no side effects.
Usage: python3 runs/harvest/validate_observability_safety.py
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

HARVEST = Path(__file__).resolve().parent
JSON_PATH = HARVEST / "observability-safety.json"
MD_PATH = HARVEST / "observability-safety.md"

CLUSTER = "observability-safety"
REQUIRED = ("name", "framework", "source", "what", "why", "build", "effort", "test", "confidence")
EFFORTS = {"S", "M", "L"}
CONFIDENCE = {"high", "medium", "low"}
MIN_PATTERNS, MAX_PATTERNS = 18, 30
MIN_PER_FW, MAX_PER_FW = 3, 6

# Frameworks explicitly requested by the task brief.
REQUIRED_FRAMEWORKS = {
    "Langfuse",
    "Braintrust",
    "OpenTelemetry GenAI",
    "Agent evaluation benchmarks",
    "Guardrail frameworks",
    "Prompt injection / agent security",
}

# Mechanisms the brief named by name; each must be covered by some pattern.
REQUIRED_TOPICS = {
    "langfuse.traces_spans": r"trace_id|observation",
    "langfuse.scores": r"NUMERIC.*CATEGORICAL|CATEGORICAL.*BOOLEAN",
    "langfuse.datasets": r"dataset",
    "langfuse.prompt_management": r"label|version",
    "langfuse.events_only_scores": r"events_only|v3/scores|ingestion",
    "braintrust.evals": r"experiment|eval",
    "braintrust.scorers": r"scorer",
    "otel.span_names": r"gen_ai\.operation\.name|execute_tool \{",
    "otel.attributes": r"gen_ai\.",
    "bench.swe_bench": r"SWE-bench",
    "bench.tau_bench": r"tau-bench|pass\^k",
    "bench.gaia": r"GAIA",
    "bench.webarena": r"WebArena",
    "bench.trajectory": r"trajector",
    "guard.nemo": r"NeMo|rail",
    "guard.guardrails_ai": r"Guardrails AI|Guard\(\)",
    "guard.llama_guard": r"Llama Guard|moderation",
    "sec.dual_llm": r"dual-LLM|quarantin",
    "sec.camel": r"CaMeL|capabilit",
    "sec.sandboxing": r"sandbox|bubblewrap",
    "sec.capability_permissions": r"privilege|capabilit",
    "sec.human_approval": r"human approval|human-in-the-loop|approval",
}

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)


def main() -> int:
    check(JSON_PATH.is_file(), f"missing {JSON_PATH}")
    check(MD_PATH.is_file(), f"missing {MD_PATH}")
    if FAILURES:
        print("\n".join(FAILURES))
        return 1

    doc = json.loads(JSON_PATH.read_text())
    check(isinstance(doc, dict), "top level must be an object")
    check(doc.get("cluster") == CLUSTER, f"cluster must be {CLUSTER!r}, got {doc.get('cluster')!r}")

    patterns = doc.get("patterns")
    check(isinstance(patterns, list), "patterns must be a list")
    if not isinstance(patterns, list):
        print("\n".join(FAILURES))
        return 1

    n = len(patterns)
    check(MIN_PATTERNS <= n <= MAX_PATTERNS, f"pattern count {n} outside {MIN_PATTERNS}-{MAX_PATTERNS}")

    names = [p.get("name") for p in patterns]
    check(len(names) == len(set(names)), f"duplicate pattern names: {[k for k, v in Counter(names).items() if v > 1]}")

    fw = Counter(p.get("framework") for p in patterns)
    for f in sorted(REQUIRED_FRAMEWORKS):
        check(f in fw, f"framework missing from deliverable: {f}")
    for f, c in fw.items():
        check(MIN_PER_FW <= c <= MAX_PER_FW, f"framework {f!r} has {c} patterns (want {MIN_PER_FW}-{MAX_PER_FW})")

    for i, p in enumerate(patterns):
        tag = f"patterns[{i}] ({p.get('name')!r})"
        check(set(p) == set(REQUIRED), f"{tag}: keys {sorted(set(p) ^ set(REQUIRED))} differ from contract")
        check(p.get("effort") in EFFORTS, f"{tag}: effort {p.get('effort')!r} not in {sorted(EFFORTS)}")
        check(p.get("confidence") in CONFIDENCE, f"{tag}: confidence {p.get('confidence')!r} not in {sorted(CONFIDENCE)}")
        src = p.get("source", "")
        check(isinstance(src, str) and src.startswith("http"), f"{tag}: source must be an http(s) URL, got {src!r}")
        for field in ("what", "why", "build", "test"):
            val = p.get(field, "")
            check(isinstance(val, str) and len(val) >= 40, f"{tag}: field {field!r} too short/empty")

    blob = " ".join(
        " ".join(str(p.get(k, "")) for k in ("name", "framework", "what", "why", "build", "test"))
        for p in patterns
    )
    for topic, pat in sorted(REQUIRED_TOPICS.items()):
        check(re.search(pat, blob, re.I) is not None, f"brief topic not covered: {topic} (/{pat}/)")

    md = MD_PATH.read_text()
    check(CLUSTER in md, f"{MD_PATH.name} does not name the cluster")
    for p in patterns:
        check(p["name"] in md, f"md companion does not mention pattern {p['name']!r}")

    if FAILURES:
        print(f"FAIL ({len(FAILURES)}):")
        print("\n".join(f"  - {f}" for f in FAILURES))
        return 1

    print(f"OK: {n} patterns, {len(fw)} frameworks, contract + brief coverage + md companion all verified")
    print("frameworks: " + ", ".join(f"{f}={c}" for f, c in sorted(fw.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
