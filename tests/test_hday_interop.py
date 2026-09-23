"""Tests for hday_interop — the cross-native interop lane.

Three wire contracts behind one adapter registry, all driven by local
in-memory transports (no sockets, no fixtures on disk):

* MCP 2026-07-28 stateless surface — ``server/discover`` capability document,
  per-request ``params._meta`` stamping, extension negotiation with graceful
  degradation, legacy stdio handshake fallback, ``UnsupportedProtocolVersionError``.
* A2A v1.0 wire contract — PascalCase method names, the unified ``Part``,
  the eight assignable ``TASK_STATE_*`` values, task lifecycle on
  contextId/taskId, and an SSE-shaped stream emitter with append/lastChunk
  artifact semantics.
* AG-UI — ``getCapabilities`` discovery with absent-means-unknown gating, plus
  ``STATE_SNAPSHOT`` / ``STATE_DELTA`` shared state on RFC 6902 JSON-Patch
  semantics (non-mutating, all-or-nothing).

Precision matters here: enum strings, method names and the stream-event
discriminator are asserted verbatim against the specs. See
``runs/harvest/protocols-ui.json``.
"""
import base64
import json
import logging
import os
import socket
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hday_interop as hi  # noqa: E402

LOG = "hermes-day.interop"

# The exact strings from the A2A v1.0 spec (whats-new-v1 §"Enum Value Changes").
EXACT_STATES = frozenset({
    "TASK_STATE_UNSPECIFIED",
    "TASK_STATE_SUBMITTED",
    "TASK_STATE_WORKING",
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
    "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_AUTH_REQUIRED",
})


# -- fixtures / helpers -------------------------------------------------------


def mcp_discover_result(**overrides):
    """A well-formed 2026-07-28 ``server/discover`` result."""
    result = {
        "resultType": "complete",
        "supportedVersions": ["2026-07-28"],
        "capabilities": {"tools": {"listChanged": True}},
        "instructions": "echo things back",
        "ttlMs": 60000,
        "cacheScope": "public",
        "_meta": {
            hi.MCP_META_SERVER_INFO: {"name": "echo-server", "version": "1.2.3"},
        },
    }
    result.update(overrides)
    return result


def transport(handlers=None, *, kind="memory", failures=None):
    return hi.InMemoryTransport(handlers or {}, kind=kind, failures=failures or {})


def mcp_client(**kwargs):
    handlers = kwargs.pop("handlers", None) or {}
    handlers.setdefault("server/discover", mcp_discover_result())
    kwargs.setdefault("client_info", {"name": "hermes-day", "version": "0.1"})
    kwargs.setdefault("client_capabilities", {"tools": {}})
    return hi.MCPClient(transport(handlers, kind=kwargs.pop("kind", "memory")), **kwargs)


def status_event(state, task_id="t1", context_id="c1"):
    return {"statusUpdate": {
        "taskId": task_id, "contextId": context_id,
        "status": {"state": state, "timestamp": "2026-09-22T19:00:00Z"},
    }}


def artifact_event(artifact_id, text, *, append=None, last_chunk=None,
                   task_id="t1", context_id="c1"):
    # Per the spec, `append`/`lastChunk` are siblings of `artifact` on the
    # TaskArtifactUpdateEvent, not fields of the artifact itself:
    # TaskArtifactUpdateEvent {taskId, contextId, artifact, append?, lastChunk?, metadata?}
    body = {"taskId": task_id, "contextId": context_id,
            "artifact": {"artifactId": artifact_id,
                         "parts": [{"text": text, "mediaType": "text/plain"}]}}
    if append is not None:
        body["append"] = append
    if last_chunk is not None:
        body["lastChunk"] = last_chunk
    return {"artifactUpdate": body}


def consumer(capabilities=None):
    caps = {"streaming": True} if capabilities is None else capabilities
    return hi.A2AStreamConsumer(capabilities=caps)


# =============================================================================
# MCP 2026-07-28 — server/discover + per-request _meta
# =============================================================================


def test_mcp_meta_key_constants_are_the_registered_strings():
    assert hi.MCP_PROTOCOL_VERSION == "2026-07-28"
    assert hi.MCP_META_PROTOCOL_VERSION == "io.modelcontextprotocol/protocolVersion"
    assert hi.MCP_META_CLIENT_CAPABILITIES == "io.modelcontextprotocol/clientCapabilities"
    assert hi.MCP_META_CLIENT_INFO == "io.modelcontextprotocol/clientInfo"
    assert hi.MCP_META_SERVER_INFO == "io.modelcontextprotocol/serverInfo"


def test_mcp_discover_returns_capability_document():
    client = mcp_client()
    doc = client.discover()

    assert doc.result_type == "complete"
    assert doc.supported_versions == ("2026-07-28",)
    assert doc.capabilities["tools"]["listChanged"] is True
    assert doc.instructions == "echo things back"
    assert doc.ttl_ms == 60000
    assert doc.cache_scope == "public"
    assert doc.server_info["name"] == "echo-server"
    assert doc.from_cache is False

    # the wire shape is the spec's, not a local invention
    wire = doc.as_dict()
    assert wire["resultType"] == "complete"
    assert wire["supportedVersions"] == ["2026-07-28"]
    assert wire["ttlMs"] == 60000
    assert wire["cacheScope"] == "public"
    assert hi.MCP_META_SERVER_INFO in wire["_meta"]


def test_mcp_discover_called_once_per_ttl_window():
    clock = [1000.0]
    t = transport({"server/discover": mcp_discover_result(ttlMs=60000)})
    client = hi.MCPClient(t, now=lambda: clock[0])

    first = client.discover()
    second = client.discover()
    third = client.discover()

    assert t.count("server/discover") == 1, "discover must be cached inside its TTL"
    assert first.from_cache is False
    assert second.from_cache is True and third.from_cache is True

    clock[0] += 61.0  # past ttlMs
    refreshed = client.discover()
    assert t.count("server/discover") == 2
    assert refreshed.from_cache is False


def test_mcp_2026_server_never_receives_initialize():
    t = transport({"server/discover": mcp_discover_result(), "tools/list": {"tools": []}})
    client = hi.MCPClient(t)
    client.discover()
    client.request("tools/list")

    assert "initialize" not in t.methods()
    assert "notifications/initialized" not in t.methods()
    assert client.legacy_handshake is False


def test_mcp_every_request_carries_its_own_meta():
    t = transport({"server/discover": mcp_discover_result(), "tools/list": {"tools": []}})
    client = hi.MCPClient(
        t,
        client_info={"name": "hermes-day", "version": "0.1"},
        client_capabilities={"tools": {}, "roots": {"listChanged": True}},
    )
    client.discover()
    client.request("tools/list")

    for method in ("server/discover", "tools/list"):
        meta = t.params_for(method)["_meta"]
        assert meta[hi.MCP_META_PROTOCOL_VERSION] == "2026-07-28"
        assert meta[hi.MCP_META_CLIENT_CAPABILITIES]["tools"] == {}
        assert meta[hi.MCP_META_CLIENT_CAPABILITIES]["roots"]["listChanged"] is True
        assert meta[hi.MCP_META_CLIENT_INFO]["name"] == "hermes-day"

    # no session header / module-global version: each request stands alone
    assert all(c.headers.get("Mcp-Session-Id") is None for c in t.calls())


def test_mcp_request_without_params_still_stamps_meta():
    t = transport({"server/discover": mcp_discover_result(), "ping": {}})
    client = hi.MCPClient(t)
    client.discover()
    client.request("ping")
    assert t.params_for("ping")["_meta"][hi.MCP_META_PROTOCOL_VERSION] == "2026-07-28"


def test_mcp_unsupported_version_raises_rather_than_degrades():
    t = transport({"server/discover": mcp_discover_result(supportedVersions=["2025-06-18"])})
    client = hi.MCPClient(t)

    with pytest.raises(hi.UnsupportedProtocolVersionError) as excinfo:
        client.discover()

    message = str(excinfo.value)
    assert "2026-07-28" in message and "2025-06-18" in message
    assert client.negotiated_version() is None


def test_mcp_legacy_stdio_server_gets_handshake_fallback():
    legacy = {
        "protocolVersion": "2025-06-18",
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "legacy-stdio", "version": "0.9"},
    }
    t = transport({"initialize": legacy}, kind="stdio", failures={"server/discover": -32601})
    client = hi.MCPClient(t, allow_legacy_fallback=True)

    doc = client.discover()

    assert t.count("initialize") == 1
    assert doc.legacy is True
    assert doc.supported_versions == ("2025-06-18",)
    assert client.legacy_handshake is True
    assert client.negotiated_version() == "2025-06-18"


def test_mcp_legacy_fallback_is_stdio_only():
    t = transport({"initialize": {"protocolVersion": "2025-06-18"}},
                  kind="http", failures={"server/discover": -32601})
    client = hi.MCPClient(t, allow_legacy_fallback=True)

    with pytest.raises(hi.InteropError):
        client.discover()
    assert t.count("initialize") == 0, "a non-stdio server must not be driven through the legacy handshake"


def test_mcp_server_info_never_drives_authorization():
    """serverInfo is self-reported and unverified: it must not grant anything."""
    spoofed = mcp_discover_result(
        capabilities={"tools": {}},
        _meta={hi.MCP_META_SERVER_INFO: {
            "name": "trusted-local", "trusted": True,
            "capabilities": {"tools": True, "resources": True},
        }},
    )
    client = hi.MCPClient(transport({"server/discover": spoofed}))
    client.discover()

    assert client.supports("tools") is True      # from the real capability map
    assert client.supports("resources") is False  # serverInfo claims it; it does not count

    snapshot = client.authorization_snapshot()
    blob = json.dumps(snapshot)
    assert "serverInfo" not in blob and "server_info" not in snapshot
    assert "trusted-local" not in blob and "trusted" not in snapshot
    assert snapshot["capabilities"] == ["tools"]


def test_mcp_discover_capability_check_reads_per_request_set_not_a_global():
    a = hi.MCPClient(transport({"server/discover": mcp_discover_result(capabilities={"tools": {}})}))
    b = hi.MCPClient(transport({"server/discover": mcp_discover_result(capabilities={"resources": {}})}))
    a.discover()
    b.discover()

    assert a.supports("tools") is True and a.supports("resources") is False
    assert b.supports("resources") is True and b.supports("tools") is False


# -- MCP extensions -----------------------------------------------------------


def test_mcp_extension_id_must_be_reverse_dns_prefixed():
    registry = hi.ExtensionRegistry()
    for bad in ("ui", "modelcontextprotocol/ui", "io.modelcontextprotocol",
                "/ui", "io.modelcontextprotocol/", "IO.Modelcontextprotocol/ui",
                "io modelcontextprotocol/ui", ""):
        with pytest.raises(ValueError):
            registry.register(bad)

    registry.register("io.modelcontextprotocol/ui")
    registry.register("com.hermes-day/surfaces")
    assert registry.identifiers() == ("com.hermes-day/surfaces", "io.modelcontextprotocol/ui")


def test_mcp_client_extensions_present_on_every_request():
    registry = hi.ExtensionRegistry()
    registry.register("io.modelcontextprotocol/ui",
                      settings={"mimeTypes": ["text/html;profile=mcp-app"]})
    t = transport({"server/discover": mcp_discover_result(), "tools/list": {"tools": []}})
    client = hi.MCPClient(t, extensions=registry)
    client.discover()
    client.request("tools/list")

    for method in ("server/discover", "tools/list"):
        extensions = t.params_for(method)["_meta"][hi.MCP_META_CLIENT_CAPABILITIES]["extensions"]
        assert extensions == {"io.modelcontextprotocol/ui": {
            "mimeTypes": ["text/html;profile=mcp-app"]}}


def test_mcp_mandatory_missing_extension_errors_nonmandatory_degrades():
    registry = hi.ExtensionRegistry()
    registry.register("io.modelcontextprotocol/tasks", mandatory=True)
    t = transport({"server/discover": mcp_discover_result(capabilities={})})
    client = hi.MCPClient(t, extensions=registry)

    with pytest.raises(hi.ExtensionNotSupportedError) as excinfo:
        client.discover()
    assert "io.modelcontextprotocol/tasks" in str(excinfo.value)


def test_mcp_nonmandatory_unsupported_extension_degrades_to_core():
    degraded = []
    registry = hi.ExtensionRegistry()
    registry.register("io.modelcontextprotocol/ui",
                      fallback=lambda: degraded.append("ui"))
    t = transport({"server/discover": mcp_discover_result(capabilities={})})
    client = hi.MCPClient(t, extensions=registry)

    doc = client.discover()
    negotiation = client.negotiation()

    assert doc.capabilities == {}
    assert negotiation.supported == ()
    assert negotiation.degraded == ("io.modelcontextprotocol/ui",)
    assert negotiation.fallbacks_invoked == ("io.modelcontextprotocol/ui",)
    assert degraded == ["ui"], "the declared fallback must actually run"


def test_mcp_negotiated_extension_is_reported_as_supported():
    registry = hi.ExtensionRegistry()
    registry.register("io.modelcontextprotocol/ui")
    t = transport({"server/discover": mcp_discover_result(
        capabilities={"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": []}}})})
    client = hi.MCPClient(t, extensions=registry)
    client.discover()

    negotiation = client.negotiation()
    assert negotiation.supported == ("io.modelcontextprotocol/ui",)
    assert negotiation.degraded == ()
    assert negotiation.server_settings["io.modelcontextprotocol/ui"] == {"mimeTypes": []}


# =============================================================================
# A2A v1.0 — wire contract
# =============================================================================


def test_a2a_method_names_are_pascal_case():
    m = hi.A2A_METHOD
    assert m["send"] == "SendMessage"
    assert m["stream"] == "SendStreamingMessage"
    assert m["get_task"] == "GetTask"
    assert m["list_tasks"] == "ListTasks"
    assert m["cancel_task"] == "CancelTask"
    assert m["subscribe"] == "SubscribeToTask"
    assert m["create_push_config"] == "CreateTaskPushNotificationConfig"
    assert m["get_push_config"] == "GetTaskPushNotificationConfig"
    assert m["list_push_configs"] == "ListTaskPushNotificationConfigs"
    assert m["delete_push_config"] == "DeleteTaskPushNotificationConfig"
    assert m["extended_card"] == "GetExtendedAgentCard"

    for name in m.values():
        assert name[0].isupper() and "/" not in name
    legacy = {"message/send", "message/stream", "tasks/get", "tasks/list",
              "tasks/cancel", "tasks/resubscribe", "tasks/pushNotificationConfig/set"}
    assert legacy.isdisjoint(set(m.values()))


def test_a2a_task_state_enum_matches_spec_exactly():
    assert hi.TASK_STATES == EXACT_STATES
    assert len(hi.TASK_STATES) == 9
    for state in hi.TASK_STATES:
        assert state.startswith("TASK_STATE_"), state
    # the eight states the spec enumerates as assignable
    assert len(hi.ASSIGNABLE_TASK_STATES) == 8
    assert hi.TASK_STATE_UNSPECIFIED not in hi.ASSIGNABLE_TASK_STATES
    assert hi.ASSIGNABLE_TASK_STATES | {hi.TASK_STATE_UNSPECIFIED} == hi.TASK_STATES
    # the v0.3.0 lowercase / hyphenated forms are gone
    for old in ("submitted", "working", "completed", "failed", "canceled",
                "rejected", "input-required", "auth-required", "unspecified"):
        assert old not in hi.TASK_STATES


def test_a2a_each_state_maps_to_exactly_one_section():
    sections = (hi.SECTION_IN_FLIGHT, hi.SECTION_NEEDS_YOU,
                hi.SECTION_REVIEW, hi.SECTION_UNKNOWN)
    buckets = {}
    for state in sorted(hi.TASK_STATES):
        section = hi.section_for_state(state)
        assert section in sections
        buckets.setdefault(section, []).append(state)

    assert sum(len(v) for v in buckets.values()) == len(hi.TASK_STATES)
    assert buckets[hi.SECTION_IN_FLIGHT] == ["TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"]
    assert buckets[hi.SECTION_NEEDS_YOU] == ["TASK_STATE_AUTH_REQUIRED",
                                             "TASK_STATE_INPUT_REQUIRED"]
    assert sorted(buckets[hi.SECTION_REVIEW]) == ["TASK_STATE_CANCELED", "TASK_STATE_COMPLETED",
                                                  "TASK_STATE_FAILED", "TASK_STATE_REJECTED"]
    assert buckets[hi.SECTION_UNKNOWN] == ["TASK_STATE_UNSPECIFIED"]


def test_a2a_unspecified_does_not_look_like_working():
    unspecified = hi.section_for_state(hi.TASK_STATE_UNSPECIFIED)
    assert unspecified == hi.SECTION_UNKNOWN
    assert unspecified != hi.section_for_state(hi.TASK_STATE_WORKING)
    assert hi.card_kind_for_state(hi.TASK_STATE_UNSPECIFIED) is None


def test_a2a_unknown_state_string_is_refused_loudly():
    for bogus in ("completed", "input-required", "TASK_STATE_DONE", ""):
        with pytest.raises(ValueError):
            hi.section_for_state(bogus)


def test_a2a_auth_required_is_an_auth_card_not_a_question_card():
    assert hi.card_kind_for_state(hi.TASK_STATE_AUTH_REQUIRED) == "auth"
    assert hi.card_kind_for_state(hi.TASK_STATE_INPUT_REQUIRED) == "question"
    assert hi.card_kind_for_state(hi.TASK_STATE_AUTH_REQUIRED) != "question"
    for state in (hi.TASK_STATE_WORKING, hi.TASK_STATE_COMPLETED):
        assert hi.card_kind_for_state(state) is None


def test_a2a_terminal_and_interrupted_sets_are_exact():
    assert hi.TASK_STATE_TERMINAL == frozenset({
        "TASK_STATE_COMPLETED", "TASK_STATE_FAILED",
        "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"})
    assert hi.TASK_STATE_INTERRUPTED == frozenset({
        "TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"})


# -- A2A unified Part ---------------------------------------------------------


def test_a2a_part_normalizes_all_four_arms():
    text = hi.normalize_part({"text": "hello", "mediaType": "text/plain"})
    assert text.kind == "text" and text.text == "hello"
    assert text.media_type == "text/plain"

    payload = b"\x00\x01hello"
    raw = hi.normalize_part({"raw": base64.b64encode(payload).decode("ascii"),
                             "mediaType": "application/octet-stream"})
    assert raw.kind == "raw" and raw.raw == payload

    url = hi.normalize_part({"url": "https://example.com/doc.pdf",
                             "filename": "doc.pdf", "mediaType": "application/pdf"})
    assert url.kind == "url" and url.url == "https://example.com/doc.pdf"
    assert url.filename == "doc.pdf"
    assert url.deferred is True, "the url arm is a deferred fetch, never fetched inline"

    data = hi.normalize_part({"data": {"k": 1}, "metadata": {"m": 2}})
    assert data.kind == "data" and data.data == {"k": 1}
    assert data.metadata == {"m": 2}


def test_a2a_part_oneof_rejects_zero_or_two_arms():
    for bad in ({}, {"text": "a", "data": {"b": 1}}, {"text": "a", "url": "https://x"},
                {"raw": "", "url": "https://x", "data": {}}):
        with pytest.raises(ValueError):
            hi.normalize_part(bad)


def test_a2a_raw_part_size_guard_refuses_oversize():
    assert hi.DEFAULT_MAX_RAW_BYTES == 10 * 1024 * 1024
    oversize = base64.b64encode(b"x" * 4096).decode("ascii")
    with pytest.raises(ValueError):
        hi.normalize_part({"raw": oversize}, max_raw_bytes=1024)
    # the guard measures decoded bytes, and the small case still round-trips
    ok = hi.normalize_part({"raw": base64.b64encode(b"y" * 512).decode("ascii")})
    assert ok.raw == b"y" * 512


def test_a2a_part_to_wire_roundtrips():
    part = hi.normalize_part({"text": "hi", "mediaType": "text/plain",
                              "filename": "note.txt", "metadata": {"m": 1}})
    wire = part.to_wire()
    assert wire["text"] == "hi"
    assert wire["mediaType"] == "text/plain"
    assert wire["filename"] == "note.txt"
    assert wire["metadata"] == {"m": 1}
    assert "kind" not in wire, "v1.0 removed the Part 'kind' discriminator"
    assert hi.normalize_part(wire).text == "hi"


# -- A2A stream events --------------------------------------------------------


def test_a2a_stream_event_discriminated_by_member_name():
    status = status_event(hi.TASK_STATE_WORKING)
    artifact = artifact_event("a1", "x", append=True)
    assert hi.classify_stream_event(status) == hi.A2A_EVENT_STATUS_UPDATE
    assert hi.classify_stream_event(artifact) == hi.A2A_EVENT_ARTIFACT_UPDATE
    assert hi.A2A_EVENT_STATUS_UPDATE == "statusUpdate"
    assert hi.A2A_EVENT_ARTIFACT_UPDATE == "artifactUpdate"

    # v1.0 removed the v0.3.0 'kind' discriminator; sniffing it must not work
    assert hi.classify_stream_event({"kind": "status-update", "taskId": "t"}) is None
    # nor may key-presence shape sniffing promote a v0.3-shaped event
    assert hi.classify_stream_event({"taskId": "t", "status": {}}) is None
    # ambiguous or unknown variants are not guessed at
    assert hi.classify_stream_event({"statusUpdate": {}, "artifactUpdate": {}}) is None
    assert hi.classify_stream_event({"mysteryUpdate": {}}) is None


def test_a2a_v030_payload_reports_actionable_version_skew():
    message = hi.detect_version_skew(
        {"kind": "status-update", "taskId": "t", "status": {"state": "working"}})
    assert message and "v0.3" in message
    assert "TASK_STATE_WORKING" in message, "skew reports must name the v1.0 form"

    assert hi.detect_version_skew({"statusUpdate": {"taskId": "t"}}) is None
    assert hi.detect_version_skew({"status": {"state": "TASK_STATE_WORKING"}}) is None


def test_a2a_unknown_stream_variant_logs_and_skips(caplog):
    events = [
        status_event(hi.TASK_STATE_WORKING),
        {"mysteryUpdate": {"taskId": "t1"}},
    ]
    with caplog.at_level(logging.WARNING, logger=LOG):
        outcome = consumer().consume(events)

    assert len(outcome.skipped) == 1
    assert outcome.cards["t1"]["state"] == hi.TASK_STATE_WORKING
    assert any("mysteryUpdate" in record.getMessage() for record in caplog.records)


def test_a2a_stream_chunks_collapse_to_one_artifact():
    events = [
        status_event(hi.TASK_STATE_WORKING),
        artifact_event("a1", "Hel"),
        status_event(hi.TASK_STATE_WORKING),
        artifact_event("a1", "lo ", append=True),
        artifact_event("a1", "world", append=True, last_chunk=True),
    ]
    outcome = consumer().consume(events)

    assert len(outcome.artifacts) == 1, "3 chunks must seal into exactly one artifact"
    artifact = outcome.artifacts[0]
    assert artifact["artifactId"] == "a1"
    assert artifact["text"] == "Hello world"
    assert artifact["sealed"] is True
    assert outcome.chunk_count == 3
    assert outcome.open_artifacts == ()


def test_a2a_unsealed_artifact_is_not_emitted():
    outcome = consumer().consume([artifact_event("a1", "partial", append=True)])
    assert outcome.artifacts == []
    assert len(outcome.open_artifacts) == 1
    assert outcome.buffer_for("a1") == "partial"


def test_a2a_status_updates_mutate_one_card_and_add_no_rows():
    events = [{"task": {"id": "t1", "contextId": "c1",
                        "status": {"state": hi.TASK_STATE_SUBMITTED}}}]
    events += [status_event(hi.TASK_STATE_WORKING),
               status_event(hi.TASK_STATE_WORKING),
               status_event(hi.TASK_STATE_INPUT_REQUIRED),
               status_event(hi.TASK_STATE_COMPLETED)]
    outcome = consumer().consume(events)

    assert outcome.new_rows == 1
    assert len(outcome.cards) == 1
    assert outcome.cards["t1"]["mutations"] == 4
    assert outcome.cards["t1"]["state"] == hi.TASK_STATE_COMPLETED
    assert outcome.cards["t1"]["section"] == hi.SECTION_REVIEW


def test_a2a_stream_refused_without_capability():
    t = transport({"SendStreamingMessage": []})
    c = hi.A2AStreamConsumer(t, capabilities={"streaming": False})
    with pytest.raises(hi.UnsupportedOperationError):
        c.open(task_id="t1", context_id="c1")
    assert t.calls() == [], "capability validation must happen before the RPC"


def test_a2a_stream_open_sends_the_v1_method_and_headers():
    t = transport({"SendStreamingMessage": {"task": {"id": "t1", "contextId": "c1",
                                                     "status": {"state": "TASK_STATE_WORKING"}}}})
    c = hi.A2AStreamConsumer(t, capabilities={"streaming": True},
                             extensions=("com.hermes-day/surfaces",))
    initial = c.open(task_id="t1", context_id="c1")

    assert t.methods() == ("SendStreamingMessage",)
    assert t.headers_for("SendStreamingMessage")["A2A-Version"] == "1.0"
    assert t.media_type_for("SendStreamingMessage") == "application/a2a+json"
    assert initial["task"]["id"] == "t1"


def test_a2a_resubscribe_reconciles_without_duplicating_content():
    c = consumer()
    c.consume([artifact_event("a1", "Hel"),
               artifact_event("a1", "lo ", append=True),
               artifact_event("a1", "world", append=True, last_chunk=True)])
    assert c.sealed_artifacts()[0]["text"] == "Hello world"

    # the stream dropped; SubscribeToTask replays the tail
    c.resubscribe_events([artifact_event("a1", "lo ", append=True),
                          artifact_event("a1", "world", append=True, last_chunk=True)])

    sealed = c.sealed_artifacts()
    assert len(sealed) == 1
    assert sealed[0]["artifactId"] == "a1"
    assert sealed[0]["text"] == "Hello world", "reconciliation must not duplicate content"
    assert c.reconciliations == 1
    assert c.dropped_duplicate_chunks == 2


def test_a2a_sse_shaped_emitter_frames():
    emitter = hi.SSEShapedEmitter()
    assert emitter.media_type == "text/event-stream"

    frame = emitter.frame(status_event(hi.TASK_STATE_WORKING), event_id="e1")
    assert frame.endswith("\n\n")
    lines = [line for line in frame.split("\n") if line]
    assert lines[0] == "event: statusUpdate"
    assert "id: e1" in lines
    names = {line.split(":", 1)[0] for line in lines}
    assert names <= {"event", "id", "data"}
    data = "\n".join(line[len("data:"):].lstrip() for line in lines if line.startswith("data:"))
    assert json.loads(data)["statusUpdate"]["taskId"] == "t1"

    # the artifact variant names itself too, and an unknown variant is 'message'
    assert emitter.frame(artifact_event("a1", "x")).startswith("event: artifactUpdate\n")
    assert emitter.frame({"mysteryUpdate": {}}).startswith("event: message\n")

    # a payload containing newlines still yields one data line
    awkward = emitter.frame(artifact_event("a1", "line1\nline2"))
    assert awkward.count("data:") == 1

    stream = emitter.frames([status_event(hi.TASK_STATE_WORKING),
                             status_event(hi.TASK_STATE_COMPLETED)])
    assert stream.count("event: statusUpdate") == 2


def test_a2a_negotiate_headers_and_extension_normalization():
    assert hi.negotiate_headers("1.0", ("com.hermes-day/surfaces",
                                        "io.modelcontextprotocol/ui")) == {
        "A2A-Version": "1.0",
        "A2A-Extensions": "com.hermes-day/surfaces,io.modelcontextprotocol/ui",
    }
    assert hi.negotiate_headers("1.0") == {"A2A-Version": "1.0", "A2A-Extensions": ""}
    # deterministic, deduped normalization shared with the MCP client
    assert hi.normalize_extensions(["b.x/y", "a.x/z", "b.x/y"]) == ("a.x/z", "b.x/y")


def test_a2a_client_headers_on_every_request():
    t = transport({"GetTask": {"id": "t1", "contextId": "c1",
                               "status": {"state": "TASK_STATE_WORKING"}},
                   "ListTasks": {"tasks": [], "next_page_token": ""}})
    client = hi.A2AClient(t, extensions=("com.hermes-day/surfaces",))
    client.call("get_task", {"id": "t1"})
    client.list_tasks()

    for method in ("GetTask", "ListTasks"):
        headers = t.headers_for(method)
        assert headers[hi.A2A_HEADER_VERSION] == "1.0"
        assert headers[hi.A2A_HEADER_EXTENSIONS] == "com.hermes-day/surfaces"
        assert t.media_type_for(method) == "application/a2a+json"
    assert hi.A2A_MEDIA_TYPE == "application/a2a+json"


def test_a2a_mcp_branch_of_negotiate_headers_invents_nothing():
    # The MCP branch must be empty rather than fabricating MCP-* headers the
    # 2026-07-28 spec never registered; version rides in params._meta instead.
    assert hi.negotiate_headers("2026-07-28", ("io.modelcontextprotocol/ui",),
                                protocol="mcp") == {}
    t = transport({"server/discover": mcp_discover_result()})
    client = hi.MCPClient(t)
    client.discover()
    assert t.calls()[0].headers == {}
    assert t.calls()[0].headers.get("Mcp-Session-Id") is None
    with pytest.raises(ValueError):
        hi.negotiate_headers("1.0", protocol="grpc")


def test_a2a_push_config_aliases_resolve_to_canonical_methods():
    # The harvest brief spells the push-config CRUD without the Task infix; the
    # v1.0 spec registers the Task-prefixed names. Both must land on the
    # canonical wire method.
    assert hi.A2A_METHOD_ALIASES["CreatePushNotificationConfig"] == \
        "CreateTaskPushNotificationConfig"
    assert hi.A2A_METHOD_ALIASES["GetPushNotificationConfig"] == \
        "GetTaskPushNotificationConfig"
    assert hi.A2A_METHOD_ALIASES["ListPushNotificationConfigs"] == \
        "ListTaskPushNotificationConfigs"
    assert hi.A2A_METHOD_ALIASES["DeletePushNotificationConfig"] == \
        "DeleteTaskPushNotificationConfig"
    # and no alias may ever be emitted as a method name
    assert not set(hi.A2A_METHOD_ALIASES) & set(hi.A2A_METHOD.values())

    handlers = {name: {"ok": True} for name in hi.A2A_METHOD_ALIASES.values()}
    t = transport(handlers)
    client = hi.A2AClient(t)
    for legacy in hi.A2A_METHOD_ALIASES:
        client.call(legacy)
    assert t.methods() == ("CreateTaskPushNotificationConfig",
                           "GetTaskPushNotificationConfig",
                           "ListTaskPushNotificationConfigs",
                           "DeleteTaskPushNotificationConfig")


def test_a2a_list_tasks_page_size_is_bounded():
    t = transport({"ListTasks": {"tasks": [], "next_page_token": ""}})
    client = hi.A2AClient(t)
    client.list_tasks(page_size=50)
    assert t.params_for("ListTasks")["pageSize"] == 50
    assert (hi.A2A_PAGE_SIZE_MIN, hi.A2A_PAGE_SIZE_MAX,
            hi.A2A_PAGE_SIZE_DEFAULT) == (1, 100, 50)
    for bad in (0, 101, -1, "50", 50.0, True):
        with pytest.raises(ValueError):
            client.list_tasks(page_size=bad)
    # only the one valid call reached the wire
    assert t.count("ListTasks") == 1


def test_a2a_misplaced_append_is_flagged_not_silently_misrendered(caplog):
    # If a peer puts append/lastChunk inside `artifact` the chunks would render
    # as N disconnected blobs. That must be visible in the log.
    misplaced = {"artifactUpdate": {
        "taskId": "t1", "contextId": "c1",
        "artifact": {"artifactId": "a1", "parts": [{"text": "chunk"}],
                     "append": True, "lastChunk": True}}}
    with caplog.at_level(logging.WARNING, logger="hermes-day.interop"):
        outcome = consumer().consume([misplaced])
    assert any("append/lastChunk" in r.message for r in caplog.records)
    # the misplacement is not honoured: nothing seals
    assert outcome.artifacts == []


def test_a2a_message_to_terminal_task_is_unsupported_operation():
    t = transport({"GetTask": {"id": "t1", "contextId": "c1",
                               "status": {"state": "TASK_STATE_COMPLETED"}},
                   "SendMessage": {"error": {"code": 12, "message": "task is terminal"}}})
    client = hi.A2AClient(t)

    with pytest.raises(hi.UnsupportedOperationError):
        client.send_message(task_id="t1", text="one more thing")
    assert t.count("SendMessage") == 0, "the terminal check must precede the send"


def test_a2a_nonblocking_send_sets_return_immediately():
    t = transport({"GetTask": {"id": "t1", "contextId": "c1",
                               "status": {"state": "TASK_STATE_WORKING"}},
                   "SendMessage": {"task": {"id": "t2", "contextId": "c1",
                                            "status": {"state": "TASK_STATE_SUBMITTED"}}}})
    client = hi.A2AClient(t)
    task = client.send_message(context_id="c1", text="go", task_id="t1")

    params = t.params_for("SendMessage")
    assert params["configuration"]["returnImmediately"] is True
    assert task["status"]["state"] == "TASK_STATE_SUBMITTED"


def test_a2a_rpc_status_renders_canonical_code():
    status = hi.parse_rpc_status({
        "code": 3,
        "message": "bad part",
        "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                     "reason": "INVALID_PART", "domain": "a2a-protocol.org"}],
    })
    assert status.code == 3
    assert status.name == "INVALID_ARGUMENT"
    assert status.message == "bad part"
    assert status.error_info["reason"] == "INVALID_PART"
    assert status.domain == "a2a-protocol.org"
    assert status.render() == "INVALID_ARGUMENT: bad part"

    assert hi.parse_rpc_status({"code": 5}).name == "NOT_FOUND"
    assert hi.parse_rpc_status({"code": 12}).name == "UNIMPLEMENTED"
    assert hi.parse_rpc_status({"code": 999}).name == "UNKNOWN"
    assert hi.parse_rpc_status({"code": 999}).render().startswith("UNKNOWN")


def test_a2a_server_error_surfaces_as_canonical_status():
    t = transport({"GetTask": {"error": {"code": 5, "message": "no such task"}}})
    client = hi.A2AClient(t)
    with pytest.raises(hi.RPCError) as excinfo:
        client.call("get_task", {"id": "nope"})
    assert excinfo.value.status.name == "NOT_FOUND"
    assert "NOT_FOUND" in str(excinfo.value)


def test_a2a_task_keeps_context_and_task_identity():
    t = transport({"GetTask": {"id": "task-1", "contextId": "ctx-9",
                               "status": {"state": "TASK_STATE_INPUT_REQUIRED"}},
                   "ListTasks": {"tasks": [], "nextPageToken": "c2", "pageSize": 50}})
    client = hi.A2AClient(t)
    task = client.get_task("task-1")
    assert task["id"] == "task-1" and task["contextId"] == "ctx-9"
    assert hi.card_kind_for_state(task["status"]["state"]) == "question"

    listing = client.list_tasks(page_size=50)
    assert t.params_for("ListTasks")["pageSize"] == 50
    assert listing["nextPageToken"] == "c2"


# =============================================================================
# AG-UI — capability discovery + shared state
# =============================================================================


def test_agui_state_event_type_constants():
    assert hi.AGUI_STATE_EVENT_TYPES == ("STATE_SNAPSHOT", "STATE_DELTA")
    snapshot = hi.state_snapshot_event({"a": 1})
    assert snapshot["type"] == "STATE_SNAPSHOT" and snapshot["snapshot"] == {"a": 1}
    delta = hi.state_delta_event([{"op": "remove", "path": "/a"}])
    assert delta["type"] == "STATE_DELTA"
    assert delta["delta"] == [{"op": "remove", "path": "/a"}]


def test_agui_state_snapshot_replaces_entirely():
    state = hi.SharedState({"queues": {"in_flight": 2}, "mutes": ["x"]})
    state.apply_snapshot({"focus": "deep"})

    assert state.as_dict() == {"focus": "deep"}, "a snapshot replaces, never merges"
    assert "queues" not in state.as_dict() and "mutes" not in state.as_dict()
    assert state.snapshot_count == 1
    assert state.delta_count == 0


def test_agui_state_delta_applies_rfc6902_ops():
    state = hi.SharedState({"queues": {"in_flight": 2}, "mutes": []})
    ok = state.apply_delta([
        {"op": "replace", "path": "/queues/in_flight", "value": 3},
        {"op": "add", "path": "/mutes/-", "value": "source:discord"},
        {"op": "add", "path": "/focus", "value": {"mode": "deep"}},
        {"op": "test", "path": "/queues/in_flight", "value": 3},
        {"op": "copy", "from": "/focus", "path": "/focus_copy"},
        {"op": "move", "from": "/focus_copy", "path": "/focus_moved"},
        {"op": "remove", "path": "/focus_moved"},
    ])

    assert ok is True
    assert state.as_dict() == {"queues": {"in_flight": 3},
                               "mutes": ["source:discord"],
                               "focus": {"mode": "deep"}}
    assert state.delta_count == 1 and state.rejected_count == 0


def test_agui_delta_is_all_or_nothing():
    state = hi.SharedState({"a": 1, "b": [1, 2]})
    before = json.dumps(state.as_dict(), sort_keys=True)

    ok = state.apply_delta([
        {"op": "replace", "path": "/a", "value": 99},
        {"op": "add", "path": "/b/-", "value": 3},
        {"op": "replace", "path": "/missing/deep", "value": 1},  # 3rd op is invalid
    ])

    assert ok is False
    assert json.dumps(state.as_dict(), sort_keys=True) == before, "state must be untouched"
    assert state.rejected_count == 1
    assert state.delta_count == 0
    assert state.last_rejected_patch is not None
    assert len(state.last_rejected_patch) == 3, "the full patch is logged on failure"


def test_agui_failed_test_op_rejects_the_whole_patch():
    state = hi.SharedState({"a": 1})
    ok = state.apply_delta([{"op": "add", "path": "/b", "value": 2},
                            {"op": "test", "path": "/a", "value": 999}])
    assert ok is False
    assert state.as_dict() == {"a": 1}


def test_agui_unknown_op_rejects_the_whole_patch():
    state = hi.SharedState({"a": 1})
    assert state.apply_delta([{"op": "frobnicate", "path": "/a"}]) is False
    assert state.as_dict() == {"a": 1}


def test_agui_delta_is_non_mutating_for_callers_and_readers():
    ops = [{"op": "add", "path": "/x", "value": {"nested": [1]}}]
    before = json.dumps(ops, sort_keys=True)
    state = hi.SharedState({})
    assert state.apply_delta(ops) is True
    assert json.dumps(ops, sort_keys=True) == before, "the caller's patch is not touched"

    view = state.as_dict()
    view["x"]["nested"].append(2)
    assert state.as_dict()["x"]["nested"] == [1], "as_dict() hands out a copy"


def test_agui_many_deltas_equal_the_equivalent_snapshot():
    ops = []
    for i in range(200):
        ops.append({"op": "add", "path": "/items/-", "value": i})
    by_delta = hi.SharedState({"items": []})
    assert by_delta.apply_delta(ops) is True

    by_snapshot = hi.SharedState({})
    by_snapshot.apply_snapshot({"items": list(range(200))})

    assert by_delta.as_dict() == by_snapshot.as_dict()


def test_agui_request_resync_emits_a_fresh_snapshot():
    state = hi.SharedState({"a": 1})
    state.apply_delta([{"op": "replace", "path": "/a", "value": 2}])
    event = state.request_resync()

    assert event["type"] == "STATE_SNAPSHOT"
    assert event["snapshot"] == {"a": 2}
    assert state.resync_count == 1
    # the resync snapshot is a copy, not a live handle on internal state
    event["snapshot"]["a"] = 99
    assert state.as_dict()["a"] == 2


def test_agui_sensitive_fields_never_appear_in_shared_state():
    state = hi.SharedState({"queues": {}, "tokens": "should-not-leak",
                            "session_content": "private"})
    blob = json.dumps(state.as_dict())
    assert "should-not-leak" not in blob
    assert "private" not in blob
    assert "tokens" not in state.as_dict()
    assert "session_content" not in state.as_dict()
    assert state.excluded_keys == ("session_content", "tokens")


def test_agui_get_capabilities_shape():
    caps = hi.get_capabilities(surface="desktop")
    assert caps["identity"]["name"]
    assert caps["identity"]["type"]
    assert caps["transport"]["streaming"] is True
    assert caps["state"]["snapshots"] is True
    assert caps["humanInTheLoop"]["approvals"] is True
    assert "com.hermes-day/surfaces" in caps["custom"]
    assert caps["custom"]["com.hermes-day/surfaces"] == ["desktop", "tui", "discord"]


def test_agui_absent_means_unknown_and_gates_like_false():
    assert hi.feature_enabled({}, ("state", "deltas")) is False
    assert hi.feature_enabled(None, ("state", "deltas")) is False
    assert hi.feature_enabled({"state": {}}, ("state", "deltas")) is False
    assert hi.feature_enabled({"state": {"deltas": False}}, ("state", "deltas")) is False
    assert hi.feature_enabled({"state": {"deltas": True}}, ("state", "deltas")) is True
    # an omitted field and an explicit false must behave identically
    omitted = {"humanInTheLoop": {}, "transport": {}, "reasoning": {}}
    explicit = {"humanInTheLoop": {"approvals": False},
                "transport": {"streaming": False},
                "reasoning": {"supported": False}}
    for path in (("humanInTheLoop", "approvals"), ("transport", "streaming"),
                 ("reasoning", "supported")):
        assert hi.feature_enabled(omitted, path) is False
        assert hi.feature_enabled(omitted, path) == hi.feature_enabled(explicit, path)


def test_agui_each_surface_declares_a_distinct_transport_set():
    registry = hi.SurfaceCapabilities()
    desktop = registry.get("desktop")["transport"]
    tui = registry.get("tui")["transport"]
    discord = registry.get("discord")["transport"]

    assert desktop["streaming"] is True
    assert desktop["resumable"] is True
    assert tui["streaming"] is True and tui["resumable"] is False
    assert discord["streaming"] is False
    assert desktop != tui and tui != discord and desktop != discord


def test_agui_desktop_gates_affordances_on_explicit_truth():
    desktop = hi.get_capabilities(surface="desktop")
    assert hi.feature_enabled(desktop, ("humanInTheLoop", "approvals")) is True
    assert hi.feature_enabled(desktop, ("reasoning", "supported")) is True
    assert hi.feature_enabled(desktop, ("transport", "streaming")) is True

    discord = hi.get_capabilities(surface="discord")
    assert hi.feature_enabled(discord, ("transport", "streaming")) is False
    assert hi.feature_enabled(discord, ("reasoning", "supported")) is False


def test_agui_runtime_capability_change_is_reflected_on_the_next_call():
    registry = hi.SurfaceCapabilities()
    assert hi.feature_enabled(registry.get("discord"), ("transport", "streaming")) is False
    registry.register("discord", {"transport": {"streaming": True}})
    assert hi.feature_enabled(registry.get("discord"), ("transport", "streaming")) is True


def test_agui_unknown_surface_declares_nothing():
    registry = hi.SurfaceCapabilities()
    caps = registry.get("toaster")
    assert caps["transport"] == {}
    assert hi.feature_enabled(caps, ("transport", "streaming")) is False


def test_agui_structured_output_falls_back_to_text():
    discord = hi.get_capabilities(surface="discord")
    plan = hi.output_plan(discord, wants="structured")
    assert plan["mode"] == "text"
    assert plan["degraded"] is True
    assert plan["mime_type"] in discord["output"]["mimeTypes"]

    desktop = hi.get_capabilities(surface="desktop")
    assert hi.output_plan(desktop, wants="structured")["mode"] == "structured"
    assert hi.output_plan(desktop, wants="structured")["degraded"] is False


# =============================================================================
# The one adapter registry
# =============================================================================


def test_registry_capabilities_single_call_covers_all_three():
    registry = hi.build_registry()
    caps = registry.capabilities()

    assert set(caps["protocols"]) == {"mcp", "a2a", "ag_ui"}
    for name, entry in caps["protocols"].items():
        assert entry["protocol"] == name
        assert entry["versions"], name
        assert isinstance(entry["features"], dict) and entry["features"], name

    assert caps["schema"] == "hermes-day/interop-capabilities/1"
    assert caps["offline"] is True
    assert set(caps["surfaces"]) >= {"desktop", "tui", "discord"}

    # the three things a surface asks for are all in the one document
    assert caps["protocols"]["mcp"]["features"]["stateless"] is True
    assert caps["protocols"]["a2a"]["features"]["streaming"] is True
    assert caps["protocols"]["ag_ui"]["features"]["state_deltas"] is True
    assert caps["protocols"]["a2a"]["states"] == sorted(EXACT_STATES)


def test_registry_get_returns_the_adapter_and_unknown_is_reported():
    registry = hi.build_registry()
    assert registry.get("mcp").protocol == "mcp"
    assert registry.get("grpc") is None
    assert "grpc" not in registry.capabilities()["protocols"]
    assert sorted(registry.adapters()) == ["a2a", "ag_ui", "mcp"]


def test_registry_exposes_its_own_capability_per_adapter():
    registry = hi.build_registry()
    for name, adapter in registry.adapters().items():
        own = adapter.capabilities()
        assert own == registry.capabilities()["protocols"][name]


def test_registry_never_touches_the_network(monkeypatch):
    def boom(*args, **kwargs):  # pragma: no cover - only runs on a regression
        raise AssertionError("hday_interop attempted real network I/O")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)

    registry = hi.build_registry()
    caps = registry.capabilities()
    assert caps["offline"] is True

    registry.get("mcp").discover()
    registry.get("a2a").list_tasks()
    registry.get("ag_ui").get_capabilities()
    registry.capabilities()


def test_registry_adapters_drive_local_transports_only():
    registry = hi.build_registry()
    for adapter in registry.adapters().values():
        assert adapter.transport.is_in_memory is True
        assert adapter.transport.network_calls == 0


def test_registry_capabilities_is_json_serializable():
    blob = json.dumps(hi.build_registry().capabilities(), sort_keys=True)
    assert "TASK_STATE_WORKING" in blob
    assert "2026-07-28" in blob


def test_registry_extension_registry_is_visible_in_the_view():
    registry = hi.build_registry()
    registry.extensions.register("com.hermes-day/surfaces", settings={})
    caps = registry.capabilities()
    assert "com.hermes-day/surfaces" in caps["extensions"]


# =============================================================================
# Gaps the end-to-end run exposed after the first green suite.
#
# Every one of these passed a full green suite before it existed: the unit
# tests were green while the assembled artifact was wrong. Kept as tests so
# the assembly is covered, not just the pieces.
# =============================================================================


def test_mcp_negotiated_version_is_recorded_for_the_audit_trail():
    # A green discover() that leaves negotiated_version() as None makes the
    # audit record ("which protocol revision was this decided under?")
    # unreadable, which is the whole point of recording it.
    client = mcp_client()
    assert client.negotiated_version() is None      # nothing negotiated yet
    doc = client.discover()
    assert doc.supported_versions == ("2026-07-28",)
    assert client.negotiated_version() == hi.MCP_PROTOCOL_VERSION
    # the version is in the authorization snapshot the gate ledger would read
    assert client.authorization_snapshot()["protocol_version"] == "2026-07-28"
    # a legacy peer records the version it actually agreed to, not ours
    legacy_t = transport({"initialize": {"protocolVersion": "2025-06-18",
                                         "capabilities": {"tools": {}},
                                         "serverInfo": {"name": "old"}}},
                         kind="stdio", failures={"server/discover": -32601})
    legacy = hi.MCPClient(legacy_t, allow_legacy_fallback=True)
    legacy.discover()
    assert legacy.negotiated_version() == "2025-06-18"


def test_agui_adapter_get_capabilities_is_the_agent_capabilities_document():
    # getCapabilities() answers "what can this agent do *here*" — the
    # AgentCapabilities categories — not the registry's protocol summary.
    adapter = hi.AGUIAdapter(surface="desktop")
    caps = adapter.get_capabilities()
    for category in ("identity", "transport", "tools", "output", "state",
                     "multiAgent", "reasoning", "humanInTheLoop", "custom"):
        assert category in caps, category
    assert caps == hi.surface_capabilities("desktop")
    assert caps["identity"]["name"]
    assert caps["transport"]["streaming"] is True
    # the protocol summary is a different document, reached a different way
    summary = adapter.protocol_view()
    assert summary["protocol"] == "ag_ui"
    assert summary["features"]["state_deltas"] is True
    assert "transport" not in summary
    assert adapter.capabilities() == summary


def test_agui_rejected_delta_logs_state_and_full_patch(caplog):
    state = hi.SharedState({"queues": {"in_flight": 2}})
    patch = [{"op": "replace", "path": "/queues/in_flight", "value": 9},
             {"op": "replace", "path": "/nope/deep", "value": 1}]
    with caplog.at_level(logging.WARNING, logger="hermes-day.interop"):
        assert state.apply_delta(patch) is False
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "in_flight" in logged          # the state at rejection time
    assert "/nope/deep" in logged         # the full patch


def test_agui_shared_day_state_allowlist_excludes_anything_unlisted():
    state = hi.shared_day_state({
        "queues": {"in_flight": 2},
        "mutes": ["source:discord"],
        "tokens": "should-not-ever-leak",
        "session_content": "private transcript",
        "a_field_nobody_thought_about": {"secret": "x"},
        "api_key": "also-not-listed",
    })
    visible = state.as_dict()
    assert set(visible) == {"queues", "mutes"}
    blob = json.dumps(visible, sort_keys=True)
    for leaked in ("should-not-ever-leak", "private transcript", "also-not-listed"):
        assert leaked not in blob
    # a delta cannot smuggle an unlisted key in either
    state.apply_delta([{"op": "add", "path": "/injected", "value": {"secret": 1}}])
    assert "injected" not in state.as_dict()
    # and an allowlisted key still round-trips normally
    state.apply_delta([{"op": "replace", "path": "/queues/in_flight", "value": 3}])
    assert state.as_dict()["queues"] == {"in_flight": 3}
    assert hi.SHARED_DAY_STATE_KEYS == ("queues", "mutes", "pins", "focus", "filters")


def test_agui_capability_doc_and_event_stream_share_the_endpoint():
    adapter = hi.AGUIAdapter()
    assert adapter.event_stream_endpoint() == hi.AGUI_ENDPOINT
    # one URL serves both, so a client can discover and subscribe on the same
    # connection target
    assert adapter.get_capabilities()["identity"]["name"]
    assert hi.build_registry().get("ag_ui").event_stream_endpoint() == hi.AGUI_ENDPOINT


def test_every_adapter_forwards_the_client_api_a_surface_needs():
    # A surface only ever holds the registry, so anything it needs must be
    # reachable on the adapter — not only on the raw client underneath.
    registry = hi.build_registry()
    mcp = registry.get("mcp")
    assert mcp.discover().result_type == "complete"
    assert mcp.negotiated_version() == hi.MCP_PROTOCOL_VERSION
    assert mcp.supports("tools") is True
    assert mcp.supports("resources") is False
    assert "serverInfo" not in json.dumps(mcp.authorization_snapshot())
    assert mcp.negotiation() is not None

    a2a = registry.get("a2a")
    assert a2a.list_tasks()["pageSize"] == hi.A2A_PAGE_SIZE_DEFAULT
    assert a2a.get_task("t1")["id"] == "t1"
    sent = a2a.send_message(context_id="c1", text="hi")
    assert sent["contextId"] == "c1"
    assert sent["status"]["state"] == hi.TASK_STATE_SUBMITTED
    assert a2a.cancel_task("t1")["status"]["state"] == hi.TASK_STATE_CANCELED

    agui = registry.get("ag_ui")
    assert agui.get_capabilities()["state"]["snapshots"] is True
    assert agui.protocol_view()["protocol"] == "ag_ui"
    assert agui.capabilities()["protocol"] == "ag_ui"
