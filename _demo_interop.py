"""End-to-end smoke check for the cross-native interop lane.

``tests/test_hday_interop.py`` covers the pieces. This script covers the
*assembled* artifact: it builds one registry and drives all three protocols
through it the way a surface (desktop / TUI / Discord) actually would.

It exists because five real defects survived a fully green unit suite and were
only caught by exercising the assembled object:

* ``negotiated_version()`` stayed ``None`` after a successful ``discover()``,
  which makes the audit record unreadable;
* ``AGUIAdapter.get_capabilities()`` returned the registry summary instead of
  the AgentCapabilities document;
* the shared-state allowlist was applied at every nesting level, emptying
  ``queues.in_flight``;
* ``MCPAdapter`` / ``A2AAdapter`` did not forward the client API a surface
  needs (``supports``, ``authorization_snapshot``, ``get_task``,
  ``send_message``);
* the delta-rejection log omitted the state it was rejecting against.

Run directly (``python3 _demo_interop.py``); exits non-zero on any failure, so
it doubles as a deterministic check.
"""

import json
import sys

import hday_interop as hi

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}" + (f"  -- {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def main() -> int:
    # --- one call: "what can this agent do?" ------------------------------
    registry = hi.build_registry()
    caps = registry.capabilities()

    check("schema is versioned", caps["schema"] == "hermes-day/interop-capabilities/1")
    check("view declares itself offline", caps["offline"] is True)
    check("all three protocols present",
          set(caps["protocols"]) == {"mcp", "a2a", "ag_ui"},
          repr(sorted(caps["protocols"])))
    check("all three surfaces present",
          set(caps["surfaces"]) >= {"desktop", "tui", "discord"},
          repr(sorted(caps["surfaces"])))
    for name in ("mcp", "a2a", "ag_ui"):
        entry = caps["protocols"][name]
        check(f"{name} declares versions", bool(entry["versions"]))
        check(f"{name} declares features", isinstance(entry["features"], dict))
    print()
    for name in sorted(caps["protocols"]):
        entry = caps["protocols"][name]
        print(f"  {name:6s} versions={entry['versions']} "
              f"features={json.dumps(entry['features'], sort_keys=True)}")
    print()

    # --- MCP 2026-07-28 ---------------------------------------------------
    mcp = registry.get("mcp")
    doc = mcp.discover()
    check("mcp discover returns a complete result", doc.result_type == "complete")
    check("mcp speaks the 2026-07-28 surface",
          doc.supported_versions == ("2026-07-28",), repr(doc.supported_versions))
    check("mcp negotiates a version for the audit trail",
          mcp.negotiated_version() == "2026-07-28", repr(mcp.negotiated_version()))
    check("mcp reports a capability it has", mcp.supports("tools") is True)
    check("mcp reports a capability it lacks", mcp.supports("resources") is False)
    check("mcp never sent an initialize handshake",
          "initialize" not in mcp.transport.methods())
    snapshot = json.dumps(mcp.authorization_snapshot(), sort_keys=True)
    check("mcp authorization view excludes serverInfo", "serverInfo" not in snapshot)
    print(f"  mcp auth view: {snapshot}")

    # --- A2A v1.0 ---------------------------------------------------------
    a2a = registry.get("a2a")
    listing = a2a.list_tasks()
    check("a2a ListTasks applies the default page size",
          listing["pageSize"] == hi.A2A_PAGE_SIZE_DEFAULT)
    check("a2a task carries taskId/contextId identity",
          a2a.get_task("t1")["id"] == "t1")

    stream = hi.A2AStreamConsumer(capabilities={"streaming": True})
    outcome = stream.consume([
        {"task": {"id": "t1", "contextId": "c1",
                  "status": {"state": hi.TASK_STATE_SUBMITTED}}},
        {"statusUpdate": {"taskId": "t1", "contextId": "c1",
                          "status": {"state": hi.TASK_STATE_WORKING}}},
        {"artifactUpdate": {"taskId": "t1", "contextId": "c1", "append": False,
                            "artifact": {"artifactId": "a1",
                                         "parts": [{"text": "Hel"}]}}},
        {"artifactUpdate": {"taskId": "t1", "contextId": "c1", "append": True,
                            "lastChunk": True,
                            "artifact": {"artifactId": "a1",
                                         "parts": [{"text": "lo"}]}}},
    ])
    check("a2a chunks collapse to one sealed artifact",
          [a["text"] for a in outcome.artifacts] == ["Hello"],
          repr([a["text"] for a in outcome.artifacts]))
    check("a2a status updates mutate one card in place",
          outcome.cards["t1"]["section"] == "in_flight"
          and outcome.cards["t1"]["mutations"] == 1,
          json.dumps(outcome.cards, sort_keys=True))
    check("a2a every state maps to exactly one section",
          all(hi.section_for_state(s) in
              ("in_flight", "needs_you", "review_queue", "unknown")
              for s in hi.TASK_STATES))
    check("a2a UNSPECIFIED does not look like WORKING",
          hi.section_for_state(hi.TASK_STATE_UNSPECIFIED)
          != hi.section_for_state(hi.TASK_STATE_WORKING))
    frame = hi.SSEShapedEmitter().frame(
        {"statusUpdate": {"taskId": "t1"}}, event_id="e1")
    check("a2a emits a well-formed SSE frame",
          frame.startswith("event: statusUpdate\n") and frame.endswith("\n\n"),
          repr(frame))

    # --- AG-UI ------------------------------------------------------------
    agui = registry.get("ag_ui")
    agent_caps = agui.get_capabilities()
    check("agui getCapabilities returns the capability document",
          agent_caps["transport"]["streaming"] is True
          and agent_caps["humanInTheLoop"]["approvals"] is True)
    check("agui serves capabilities on the event-stream endpoint",
          agui.event_stream_endpoint() == hi.AGUI_ENDPOINT)
    check("agui omitted field gates exactly like an explicit false",
          hi.feature_enabled({"output": {}}, ("output", "structuredOutput")) is False
          and hi.feature_enabled({"output": {"structuredOutput": False}},
                                 ("output", "structuredOutput")) is False)

    state = hi.shared_day_state({"queues": {"in_flight": 2},
                                 "tokens": "must-not-leak"})
    state.apply_delta([{"op": "replace", "path": "/queues/in_flight", "value": 3},
                       {"op": "add", "path": "/focus", "value": {"mode": "deep"}}])
    visible = json.dumps(state.as_dict(), sort_keys=True)
    check("agui STATE_DELTA applies RFC 6902 ops",
          state.as_dict()["queues"] == {"in_flight": 3}
          and state.as_dict()["focus"] == {"mode": "deep"}, visible)
    check("agui allowlist drops unlisted fields", "must-not-leak" not in visible)
    check("agui resync asks for a fresh snapshot",
          state.request_resync()["type"] == "STATE_SNAPSHOT")
    check("agui STATE_SNAPSHOT replaces the whole document",
          _snapshot_replaces())

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED: {failures}")
        return 1
    print("all end-to-end interop checks passed")
    return 0


def _snapshot_replaces() -> bool:
    state = hi.SharedState({"stale": 1, "kept": 2})
    state.apply_snapshot({"kept": 3})
    return state.as_dict() == {"kept": 3}


if __name__ == "__main__":
    sys.exit(main())
