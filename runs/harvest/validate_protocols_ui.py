#!/usr/bin/env python3
"""Deterministic validator for the protocols-ui harvest deliverable.

Asserts the strict deliverable contract:
  {cluster, patterns:[{name, framework, source, what, why, build, effort, test, confidence}]}

Exit 0 only if every assertion holds. No network, no side effects.
Usage: python3 runs/harvest/validate_protocols_ui.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HARVEST = Path(__file__).resolve().parent
JSON_PATH = HARVEST / "protocols-ui.json"
MD_PATH = HARVEST / "protocols-ui.md"

CLUSTER = "protocols-ui"
REQUIRED = ("name", "framework", "source", "what", "why", "build", "effort", "test", "confidence")
EFFORTS = {"S", "M", "L"}
CONFIDENCE = {"high", "medium", "low"}
# Frameworks explicitly requested by the task brief.
REQUIRED_FRAMEWORKS = {
    "MCP",
    "MCP 2026-07-28",
    "A2A v1.0",
    "AG-UI",
    "CopilotKit",
    "OpenTelemetry GenAI semantic conventions",
    "OpenAI Realtime API",
    "2026 protocol registries",
}
# Mechanisms the brief named by name; each must be covered by some pattern.
REQUIRED_TOPICS = {
    "mcp.tools": r"tools/list",
    "mcp.resources": r"resources/read",
    "mcp.prompts": r"prompts/get",
    "mcp.sampling": r"sampling/createMessage",
    "mcp.elicitation": r"elicitation/create",
    "mcp.streamable_http": r"text/event-stream",
    "mcp.server_auth": r"resource_metadata",
    "mcp.roots": r"roots/list",
    "a2a.agent_card": r"Agent Card",
    "a2a.task_lifecycle": r"TASK_STATE_INPUT_REQUIRED",
    "a2a.streaming": r"SendStreamingMessage",
    "a2a.push_notifications": r"TaskPushNotificationConfig",
    "agui.events": r"RUN_FINISHED",
    "agui.shared_state": r"STATE_DELTA",
    "agui.interrupts": r"outcome\.type === \"interrupt\"|type:'interrupt'|\"interrupt\"",
    "agui.generative_ui": r"generative[- ]UI",
    "agui.useCoAgent": r"useCoAgent",
    "otel.genai": r"gen_ai\.",
    "realtime.events": r"input_audio_buffer",
    "registry.2026": r"registry",
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
check("pattern count in 18..32", 18 <= len(patterns) <= 32, f"got {len(patterns)}")
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
        check(f"{where}.{k} non-empty string", isinstance(v, str) and v.strip(), repr(v)[:60])
    check(f"{where}.effort in {sorted(EFFORTS)}", p.get("effort") in EFFORTS, repr(p.get("effort")))
    check(f"{where}.confidence in {sorted(CONFIDENCE)}",
          p.get("confidence") in CONFIDENCE, repr(p.get("confidence")))
    name = p.get("name", "")
    check(f"{where}.name unique", name not in seen_names, name)
    seen_names.add(name)
    # every source must contain at least one https URL and no malformed fragments
    src = p.get("source", "")
    urls = [u.strip() for u in re.split(r"\s*·\s*|\s*,\s*", src) if u.strip()]
    check(f"{where}.source has >=1 url", any(u.startswith("https://") for u in urls), src[:80])
    check(f"{where}.source urls all clean https",
          all(u.startswith("https://") and " " not in u and "(" not in u for u in urls),
          str([u for u in urls if not (u.startswith("https://") and " " not in u and "(" not in u)]))
    # build must name a concrete hermes-day artifact
    build = p.get("build", "")
    check(f"{where}.build names a concrete artifact",
          bool(re.search(r"hday_\w+\.py|desktop/plugin\.js|tests/", build)),
          build[:80])
    # test must name a runnable check
    check(f"{where}.test names a runnable check",
          bool(re.search(r"tests/|harness|fixture", p.get("test", ""))),
          p.get("test", "")[:80])

# ---------------------------------------------------------------- frameworks
frameworks = {p.get("framework") for p in patterns}
check("all briefed frameworks present", REQUIRED_FRAMEWORKS <= frameworks,
      f"missing {sorted(REQUIRED_FRAMEWORKS - frameworks)}")
check("no unknown framework labels", not (frameworks - REQUIRED_FRAMEWORKS),
      f"extra {sorted(frameworks - REQUIRED_FRAMEWORKS)}")
per_fw: dict[str, int] = {}
for p in patterns:
    per_fw[p["framework"]] = per_fw.get(p["framework"], 0) + 1
for fw in sorted(REQUIRED_FRAMEWORKS):
    n = per_fw.get(fw, 0)
    check(f"framework '{fw}' has >=2 patterns", n >= 2, f"got {n}")
    check(f"framework '{fw}' has <=6 patterns", n <= 6, f"got {n}")

# ---------------------------------------------------------------- topic coverage
blob = json.dumps(patterns, ensure_ascii=False)
for topic, rx in REQUIRED_TOPICS.items():
    check(f"topic covered: {topic}", bool(re.search(rx, blob, re.IGNORECASE)))

# ---------------------------------------------------------------- honesty
check("unreachable list present and non-empty",
      isinstance(doc.get("unreachable"), list) and len(doc["unreachable"]) >= 1)
check("unreachable entries name a URL and a reason",
      all("http" in u and ("—" in u or "-" in u) for u in doc.get("unreachable", [])))

# source URLs must all be from the fetched allowlist (no invented hosts)
all_urls = set()
for p in patterns:
    all_urls |= {u.strip() for u in re.split(r"\s*·\s*|\s*,\s*", p["source"]) if u.strip().startswith("http")}
ALLOWED_HOSTS = {
    "modelcontextprotocol.io",
    "a2a-protocol.org",
    "docs.ag-ui.com",
    "docs.copilotkit.ai",
    "opentelemetry.io",
    "raw.githubusercontent.com",
    "developers.openai.com",
    "docs.cloud.google.com",
    "docs.agntcy.org",
    "a2ui.org",
    "github.com",
}
bad_hosts = sorted({re.sub(r"^https://([^/]+).*$", r"\1", u) for u in all_urls} - ALLOWED_HOSTS)
check("all source URLs on fetched allowlist", not bad_hosts, f"unexpected hosts {bad_hosts}")

# ---------------------------------------------------------------- md companion
md = MD_PATH.read_text(encoding="utf-8")
check("md is non-trivial", len(md) > 8000, f"{len(md)} chars")
check("md documents the pattern total", str(len(patterns)) in md, f"{len(patterns)} not found in md")
check("md lists unreachable sources", "Unreachable" in md or "unreachable" in md)
check("md has the build-order section", "build order" in md.lower())

# ---------------------------------------------------------------- report
print(f"protocols-ui harvest: {len(patterns)} patterns, {len(all_urls)} unique source URLs")
print("per-framework:", dict(sorted(per_fw.items())))
print(f"assertions run: {checks}")
if failures:
    print(f"\n{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  FAIL {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
