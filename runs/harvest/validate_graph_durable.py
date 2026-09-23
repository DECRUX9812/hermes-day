#!/usr/bin/env python3
"""Deterministic validator for the graph-durable harvest cluster.

Checks the deliverable contract for runs/harvest/graph-durable.json (+ its .md twin):
  * exact top-level shape and cluster name
  * per-pattern: exactly the 9 required keys, non-empty, effort in {S,M,L},
    confidence in {high,medium}, https:// source URL
  * 3-6 patterns per framework, >=3 frameworks, 18-32 patterns total
  * unique pattern names, unique (framework, name) pairs
  * the .md twin is in sync with the JSON: same pattern count, every pattern
    name rendered, and the framework headings present

Exit 0 = deliverable is well-formed. Exit 1 = first contract violation, printed.
Run: python3 runs/harvest/validate_graph_durable.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
JSON_PATH = HERE / "graph-durable.json"
MD_PATH = HERE / "graph-durable.md"

REQUIRED_KEYS = {
    "name",
    "framework",
    "source",
    "what",
    "why",
    "build",
    "effort",
    "test",
    "confidence",
}
MIN_PATTERNS, MAX_PATTERNS = 18, 32
MIN_PER_FRAMEWORK, MAX_PER_FRAMEWORK = 3, 6
EXPECTED_CLUSTER = "graph-durable"
EFFORTS = {"S", "M", "L"}
CONFIDENCES = {"high", "medium"}
MIN_FIELD_CHARS = {"what": 80, "why": 60, "build": 120, "test": 60}


class Violation(AssertionError):
    pass


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise Violation(msg)


def main() -> int:
    check(JSON_PATH.exists(), f"missing {JSON_PATH}")
    check(MD_PATH.exists(), f"missing {MD_PATH}")

    data = json.loads(JSON_PATH.read_text())
    check(isinstance(data, dict), "top level must be an object")
    check(
        set(data) == {"cluster", "patterns"},
        f"top-level keys must be exactly cluster+patterns, got {sorted(data)}",
    )
    check(data["cluster"] == EXPECTED_CLUSTER, f"cluster must be {EXPECTED_CLUSTER!r}")

    patterns = data["patterns"]
    check(isinstance(patterns, list), "patterns must be a list")
    check(
        MIN_PATTERNS <= len(patterns) <= MAX_PATTERNS,
        f"pattern count {len(patterns)} outside [{MIN_PATTERNS},{MAX_PATTERNS}]",
    )

    for i, p in enumerate(patterns):
        where = f"patterns[{i}] ({p.get('name', '<no name>')!r})"
        check(isinstance(p, dict), f"{where}: not an object")
        check(set(p) == REQUIRED_KEYS, f"{where}: keys must be exactly {sorted(REQUIRED_KEYS)}")
        for key in REQUIRED_KEYS:
            check(
                isinstance(p[key], str) and p[key].strip(),
                f"{where}: field {key!r} must be a non-empty string",
            )
        check(p["source"].startswith("https://"), f"{where}: source must be https")
        check(p["effort"] in EFFORTS, f"{where}: effort {p['effort']!r} not in {sorted(EFFORTS)}")
        check(
            p["confidence"] in CONFIDENCES,
            f"{where}: confidence {p['confidence']!r} not in {sorted(CONFIDENCES)}",
        )
        for key, floor in MIN_FIELD_CHARS.items():
            check(
                len(p[key]) >= floor,
                f"{where}: field {key!r} is {len(p[key])} chars (< {floor}); too thin to implement",
            )

    names = [p["name"] for p in patterns]
    dupes = [n for n, c in Counter(names).items() if c > 1]
    check(not dupes, f"duplicate pattern names: {dupes}")

    per_fw = Counter(p["framework"] for p in patterns)
    check(len(per_fw) >= 3, f"expected >=3 frameworks, got {len(per_fw)}")
    for fw, count in per_fw.items():
        check(
            MIN_PER_FRAMEWORK <= count <= MAX_PER_FRAMEWORK,
            f"framework {fw!r} has {count} patterns (must be {MIN_PER_FRAMEWORK}-{MAX_PER_FRAMEWORK})",
        )

    md = MD_PATH.read_text()
    headings = [line for line in md.splitlines() if line.startswith("### ")]
    check(
        len(headings) == len(patterns),
        f"md has {len(headings)} pattern headings but json has {len(patterns)} patterns",
    )
    for p in patterns:
        check(p["name"] in md, f"md is missing pattern {p['name']!r}")
        check(f"- **Source:** {p['source']}" in md, f"md is missing source line for {p['name']!r}")
    for fw in per_fw:
        check(f"\n## {fw}\n" in md, f"md is missing framework section {fw!r}")

    print(
        f"OK  {JSON_PATH.name}: {len(patterns)} patterns across {len(per_fw)} frameworks; "
        f"md twin in sync ({len(headings)} headings)"
    )
    for fw, count in per_fw.most_common():
        print(f"    {fw}: {count}")
    print(f"    effort mix: {dict(Counter(p['effort'] for p in patterns))}")
    print(f"    confidence: {dict(Counter(p['confidence'] for p in patterns))}")
    print(f"    unique sources: {len({p['source'] for p in patterns})}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Violation as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        sys.exit(1)
