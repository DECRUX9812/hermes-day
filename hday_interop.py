"""Cross-native interop: MCP 2026-07-28, A2A v1.0, AG-UI behind one registry.

Hermes Day is asked to be *cross-native*: the same harness must speak to the
2026-07-28 stateless MCP surface, hold an A2A v1.0 conversation with a remote
agent, and drive an AG-UI surface (desktop / TUI / Discord). This module is the
single place where those three wire contracts live, so the rest of the plugin
never re-derives a protocol detail from memory.

Everything here is offline by construction: every adapter drives an
:class:`InMemoryTransport` (a local echo server), so the test suite never opens
a socket. :func:`build_registry` is the one entry point a surface needs — it
answers "what can this agent do?" in a single ``capabilities()`` call.

Three precision-critical details, each of which has a known failure mode:

* **A2A stream events are discriminated by JSON member name** (``statusUpdate``
  / ``artifactUpdate``), not by a ``kind`` field and not by shape sniffing.
  v1.0 removed ``kind`` and ``final``; classifying on either silently mis-renders
  a live stream.
* **A2A states are ``SCREAMING_SNAKE`` with a type prefix** (``TASK_STATE_*``).
  The v0.3.0 lowercase forms (``"completed"``) are detected and reported as
  version skew rather than being coerced.
* **``TASK_STATE_UNSPECIFIED`` must not look like ``TASK_STATE_WORKING``** — an
  unset state is ``unknown``, never "in flight".

``serverInfo`` is self-reported and unverified, so it is deliberately kept out
of :meth:`MCPClient.authorization_snapshot`: capability decisions read the
capability map only.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping, Sequence

__all__ = [
    "A2AClient", "A2AStreamConsumer", "AdapterRegistry", "Adapters",
    "A2AVersionSkewError", "A2A_HEADER_EXTENSIONS", "A2A_HEADER_VERSION",
    "A2A_MEDIA_TYPE", "A2A_METHOD", "A2A_VERSION", "AGUIAdapter",
    "AGUIAdapterProtocol", "AGUI_STATE_EVENT_TYPES", "ASSIGNABLE_TASK_STATES",
    "Call", "DEFAULT_MAX_RAW_BYTES", "ExtensionNotSupportedError",
    "ExtensionNegotiation", "ExtensionRegistry", "InMemoryTransport",
    "InteropError", "MCPAdapter", "MCPClient", "MCP_META_CLIENT_CAPABILITIES",
    "MCP_META_CLIENT_INFO", "MCP_META_PROTOCOL_VERSION", "MCP_META_SERVER_INFO",
    "MCP_PROTOCOL_VERSION", "Part", "RPCError", "RPCStatus", "SSEShapedEmitter",
    "SECTION_IN_FLIGHT", "SECTION_NEEDS_YOU", "SECTION_REVIEW", "SECTION_UNKNOWN",
    "SharedState", "SurfaceCapabilities", "TASK_STATES", "TASK_STATE_AUTH_REQUIRED",
    "TASK_STATE_CANCELED", "TASK_STATE_COMPLETED", "TASK_STATE_FAILED",
    "TASK_STATE_INPUT_REQUIRED", "TASK_STATE_INTERRUPTED", "TASK_STATE_REJECTED",
    "TASK_STATE_SUBMITTED", "TASK_STATE_TERMINAL", "TASK_STATE_UNSPECIFIED",
    "TASK_STATE_WORKING", "UnsupportedOperationError",
    "UnsupportedProtocolVersionError", "artifact_event", "build_registry",
    "card_kind_for_state", "classify_stream_event", "detect_version_skew",
    "feature_enabled", "get_capabilities", "negotiate_headers",
    "normalize_extensions", "normalize_part", "output_plan", "parse_rpc_status",
    "section_for_state", "state_delta_event", "state_snapshot_event",
    "status_event", "surface_capabilities",
]

log = logging.getLogger("hermes-day.interop")


# =============================================================================
# errors
# =============================================================================


class InteropError(Exception):
    """Base class for every interop failure this module raises."""


class UnsupportedProtocolVersionError(InteropError):
    """Our protocol version is not in the peer's ``supportedVersions``.

    Never degrade silently on a version mismatch: the caller has to pick a
    different negotiated version, or stop.
    """


class UnsupportedOperationError(InteropError):
    """The peer (or our own capability gate) does not support this operation."""


class ExtensionNotSupportedError(InteropError):
    """A *mandatory* extension is not offered by the peer: explicit startup error."""


class A2AVersionSkewError(InteropError, ValueError):
    """A v0.3.0-shaped A2A payload arrived on a v1.0 wire."""


class RPCError(InteropError):
    """An error envelope carrying a ``google.rpc.Status``."""

    def __init__(self, status: "RPCStatus") -> None:
        super().__init__(status.render())
        self.status = status


# =============================================================================
# local-only transport
# =============================================================================


@dataclass
class Call:
    """One recorded request."""

    method: str
    params: dict
    headers: dict
    media_type: str | None


class InMemoryTransport:
    """A local echo/in-memory server. Never opens a socket.

    ``handlers`` maps a method name to the result it should return (or to a
    callable taking the params). ``failures`` maps a method name to an int error
    code or an exception instance, so failure paths (method-not-found, 404,
    server errors) are exercised without a network.
    """

    is_in_memory = True
    network_calls = 0

    def __init__(self, handlers: Mapping[str, Any] | None = None, *,
                 kind: str = "memory",
                 failures: Mapping[str, Any] | None = None) -> None:
        self.kind = kind
        self._handlers = dict(handlers or {})
        self._failures = dict(failures or {})
        self._calls: list[Call] = []

    # -- server side --

    def call(self, method: str, params: Mapping[str, Any] | None = None, *,
             headers: Mapping[str, str] | None = None,
             media_type: str | None = None) -> Any:
        params = dict(params or {})
        headers = dict(headers or {})
        self._calls.append(Call(method, params, headers, media_type))

        if method in self._failures:
            failure = self._failures[method]
            if isinstance(failure, BaseException):
                raise failure
            raise RPCError(RPCStatus(int(failure), f"{method}: method not found"))

        if method not in self._handlers:
            raise RPCError(RPCStatus(-32601, f"{method}: method not found"))

        handler = self._handlers[method]
        result = handler(params) if callable(handler) else handler
        return copy.deepcopy(result)

    # -- recorded traffic --

    def calls(self) -> list[Call]:
        return list(self._calls)

    def methods(self) -> tuple[str, ...]:
        return tuple(call.method for call in self._calls)

    def count(self, method: str) -> int:
        return sum(1 for call in self._calls if call.method == method)

    def _last(self, method: str, index: int = -1) -> Call:
        matching = [call for call in self._calls if call.method == method]
        if not matching:
            raise AssertionError(f"no recorded call to {method!r}")
        return matching[index]

    def params_for(self, method: str, index: int = -1) -> dict:
        return self._last(method, index).params

    def headers_for(self, method: str, index: int = -1) -> dict:
        return self._last(method, index).headers

    def media_type_for(self, method: str, index: int = -1) -> str | None:
        return self._last(method, index).media_type


def normalize_extensions(extensions: Iterable[str]) -> tuple[str, ...]:
    """Deterministic, deduplicated extension identifier list.

    Both clients order their advertised extensions the same way so a
    negotiated set is comparable (and cacheable) across protocols.
    """
    return tuple(sorted({str(item) for item in extensions}))


def negotiate_headers(version: str, extensions: Iterable[str] = (), *,
                      protocol: str = "a2a") -> dict:
    """The single negotiation point both clients route through.

    A2A registers two HTTP headers and carries version + extensions there. MCP
    2026-07-28 moved the protocol version out of headers and into
    ``params._meta`` (see :meth:`MCPClient._meta`), so the MCP branch is
    deliberately **empty**: inventing an ``MCP-*`` header would be a local
    fiction a real server would ignore. The set normalisation is shared, only
    the carrier differs.
    """
    normalized = normalize_extensions(extensions)
    if protocol == "a2a":
        return {
            A2A_HEADER_VERSION: version,
            A2A_HEADER_EXTENSIONS: ",".join(normalized),
        }
    if protocol == "mcp":
        return {}
    raise ValueError(f"unknown protocol for header negotiation: {protocol!r}")


# =============================================================================
# MCP 2026-07-28 — stateless surface
# =============================================================================

MCP_PROTOCOL_VERSION = "2026-07-28"

# _meta keys registered by the MCP spec.
MCP_META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
MCP_META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
MCP_META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
MCP_META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

MCP_METHOD_DISCOVER = "server/discover"
MCP_METHOD_INITIALIZE = "initialize"
MCP_METHOD_INITIALIZED = "notifications/initialized"
MCP_ERROR_METHOD_NOT_FOUND = -32601

# {reverse-dns-prefix}/{lowercase-name}
_EXTENSION_PREFIX_RE = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
_EXTENSION_NAME_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")


class ExtensionRegistry:
    """Declares the extensions this client offers, and what to do without them."""

    def __init__(self) -> None:
        self._entries: dict[str, dict] = {}

    def register(self, identifier: str, *, settings: Mapping[str, Any] | None = None,
                 mandatory: bool = False,
                 fallback: Callable[[], None] | None = None) -> None:
        if not isinstance(identifier, str) or "/" not in identifier:
            raise ValueError(
                f"extension identifier must be '{{reverse-dns}}/{{name}}': {identifier!r}")
        prefix, _, name = identifier.partition("/")
        if "." not in prefix or not _EXTENSION_PREFIX_RE.match(prefix):
            raise ValueError(
                f"extension prefix must be reverse-DNS (lowercase, dotted): {identifier!r}")
        if not _EXTENSION_NAME_RE.match(name):
            raise ValueError(f"extension name must be lowercase/dashed: {identifier!r}")

        self._entries[identifier] = {
            "settings": dict(settings or {}),
            "mandatory": bool(mandatory),
            "fallback": fallback,
        }

    def identifiers(self) -> tuple[str, ...]:
        return normalize_extensions(self._entries)

    def client_map(self) -> dict:
        """The map advertised in ``io.modelcontextprotocol/clientCapabilities``."""
        return {name: copy.deepcopy(entry["settings"])
                for name, entry in sorted(self._entries.items())}

    def entries(self) -> dict:
        return copy.deepcopy(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)


@dataclass
class ExtensionNegotiation:
    """Outcome of comparing our extensions against the peer's advertised set."""

    supported: tuple[str, ...] = ()
    degraded: tuple[str, ...] = ()
    fallbacks_invoked: tuple[str, ...] = ()
    server_settings: dict = field(default_factory=dict)
    mandatory_missing: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "supported": list(self.supported),
            "degraded": list(self.degraded),
            "fallbacks_invoked": list(self.fallbacks_invoked),
            "mandatory_missing": list(self.mandatory_missing),
        }


def _negotiate_extensions(registry: ExtensionRegistry | None,
                          server_extensions: Mapping[str, Any] | None) -> ExtensionNegotiation:
    """Negotiate extensions, degrading gracefully where the spec allows it."""
    if registry is None:
        return ExtensionNegotiation()

    offered = dict(server_extensions or {})
    supported, degraded, invoked, mandatory_missing = [], [], [], []

    for identifier, entry in sorted(registry._entries.items()):
        if identifier in offered:
            supported.append(identifier)
            continue
        if entry["mandatory"]:
            mandatory_missing.append(identifier)
            continue
        degraded.append(identifier)
        if entry["fallback"] is not None:
            try:
                entry["fallback"]()
            except Exception:  # pragma: no cover - a bad fallback must not break discovery
                log.exception("interop: extension fallback for %s raised", identifier)
            else:
                invoked.append(identifier)

    if mandatory_missing:
        raise ExtensionNotSupportedError(
            "mandatory MCP extension(s) not offered by peer: "
            + ", ".join(mandatory_missing))

    return ExtensionNegotiation(
        supported=tuple(supported),
        degraded=tuple(degraded),
        fallbacks_invoked=tuple(invoked),
        server_settings=copy.deepcopy(offered),
    )


@dataclass
class DiscoverResult:
    """The ``server/discover`` capability document."""

    supported_versions: tuple[str, ...]
    capabilities: dict
    server_info: dict
    instructions: str | None = None
    ttl_ms: int | None = None
    cache_scope: str | None = None
    result_type: str = "complete"
    from_cache: bool = False
    legacy: bool = False
    extensions: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        wire = {
            "resultType": self.result_type,
            "supportedVersions": list(self.supported_versions),
            "capabilities": copy.deepcopy(self.capabilities),
            "_meta": {MCP_META_SERVER_INFO: copy.deepcopy(self.server_info)},
        }
        if self.instructions is not None:
            wire["instructions"] = self.instructions
        if self.ttl_ms is not None:
            wire["ttlMs"] = self.ttl_ms
        if self.cache_scope is not None:
            wire["cacheScope"] = self.cache_scope
        return wire


class MCPClient:
    """A stateless MCP 2026-07-28 client.

    Every request stands alone: the protocol version, our capabilities and our
    client info ride in ``params._meta`` on *that* request. There is no session
    and no module-global negotiated version to leak between servers.
    """

    def __init__(self, transport: InMemoryTransport, *,
                 client_info: Mapping[str, Any] | None = None,
                 client_capabilities: Mapping[str, Any] | None = None,
                 extensions: ExtensionRegistry | None = None,
                 protocol_version: str = MCP_PROTOCOL_VERSION,
                 allow_legacy_fallback: bool = False,
                 now: Callable[[], float] | None = None) -> None:
        self.transport = transport
        self.protocol_version = protocol_version
        self.client_info = dict(client_info or {"name": "hermes-day", "version": "0.0"})
        self.client_capabilities = dict(client_capabilities or {})
        self.extensions = extensions
        self.allow_legacy_fallback = allow_legacy_fallback
        self.legacy_handshake = False
        self.negotiation_result = ExtensionNegotiation()
        self._now = now or (lambda: 0.0)
        self._discover: DiscoverResult | None = None
        self._discover_at: float = 0.0
        self._version: str | None = None

    # -- request plumbing --

    def _meta(self) -> dict:
        capabilities = copy.deepcopy(self.client_capabilities)
        if self.extensions:
            capabilities["extensions"] = self.extensions.client_map()
        return {
            MCP_META_PROTOCOL_VERSION: self.protocol_version,
            MCP_META_CLIENT_CAPABILITIES: capabilities,
            MCP_META_CLIENT_INFO: copy.deepcopy(self.client_info),
        }

    def request(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        payload = dict(params or {})
        payload["_meta"] = self._meta()
        headers = negotiate_headers(
            self.protocol_version,
            self.extensions.identifiers() if self.extensions else (),
            protocol="mcp")
        return self.transport.call(method, payload, headers=headers)

    # -- discovery --

    def discover(self, *, force: bool = False) -> DiscoverResult:
        cached = self._discover
        if cached is not None and not force:
            ttl = cached.ttl_ms
            fresh = ttl is None or (self._now() - self._discover_at) * 1000.0 < ttl
            if fresh:
                return replace(cached, from_cache=True)

        legacy = False
        try:
            result = self.request(MCP_METHOD_DISCOVER)
        except RPCError as exc:
            result, legacy = self._legacy_or_raise(exc)

        doc = self._to_document(result, legacy=legacy)
        self._accept_version(doc)

        self.negotiation_result = _negotiate_extensions(
            self.extensions, doc.extensions)
        doc.extensions = dict(doc.capabilities.get("extensions") or {})

        self._discover = doc
        self._discover_at = self._now()
        return doc

    def _legacy_or_raise(self, exc: RPCError) -> tuple[Any, bool]:
        not_found = exc.status.code == MCP_ERROR_METHOD_NOT_FOUND
        if not_found and self.allow_legacy_fallback and self.transport.kind == "stdio":
            log.warning("interop: stdio peer lacks server/discover; falling back to the "
                        "legacy initialize handshake")
            result = self.request(MCP_METHOD_INITIALIZE)
            self.legacy_handshake = True
            return result, True
        if not_found:
            raise InteropError(
                f"peer does not implement {MCP_METHOD_DISCOVER!r} and a legacy fallback "
                f"is not available for a {self.transport.kind!r} transport") from exc
        raise exc

    def _to_document(self, result: Mapping[str, Any], *, legacy: bool) -> DiscoverResult:
        if not isinstance(result, Mapping):
            raise InteropError(f"malformed discover result: {result!r}")

        if legacy:
            version = result.get("protocolVersion")
            versions = (version,) if version else ()
            capabilities = dict(result.get("capabilities") or {})
            server_info = dict(result.get("serverInfo") or {})
            return DiscoverResult(supported_versions=versions, capabilities=capabilities,
                                  server_info=server_info, legacy=True,
                                  extensions=dict(capabilities.get("extensions") or {}))

        versions = tuple(result.get("supportedVersions") or ())
        capabilities = dict(result.get("capabilities") or {})
        meta = dict(result.get("_meta") or {})
        return DiscoverResult(
            supported_versions=versions,
            capabilities=capabilities,
            server_info=dict(meta.get(MCP_META_SERVER_INFO) or {}),
            instructions=result.get("instructions"),
            ttl_ms=result.get("ttlMs"),
            cache_scope=result.get("cacheScope"),
            result_type=result.get("resultType", "complete"),
            extensions=dict(capabilities.get("extensions") or {}),
        )

    def _accept_version(self, doc: DiscoverResult) -> None:
        if doc.legacy:
            self._version = doc.supported_versions[0] if doc.supported_versions else None
            return
        if self.protocol_version not in doc.supported_versions:
            raise UnsupportedProtocolVersionError(
                f"peer does not support protocol version {self.protocol_version!r}; "
                f"it offers {list(doc.supported_versions)!r}")
        # The peer accepted our version, so this is what every decision under
        # this client was made under. Leaving it unset would make the audit
        # record (authorization_snapshot / the gate ledger) unreadable.
        self._version = self.protocol_version

    # -- views for the rest of the plugin --

    def negotiated_version(self) -> str | None:
        return self._version

    def supports(self, capability: str) -> bool:
        doc = self._discover
        return bool(doc is not None and capability in doc.capabilities)

    def capability_names(self) -> tuple[str, ...]:
        doc = self._discover
        return tuple(sorted(doc.capabilities)) if doc else ()

    def negotiation(self) -> ExtensionNegotiation:
        return self.negotiation_result

    def authorization_snapshot(self) -> dict:
        """The only thing an authorization decision may read.

        ``serverInfo`` is peer-asserted and unverified, so it is structurally
        absent from this snapshot: a peer cannot talk its way into a capability
        by naming itself something trusted.
        """
        return {
            "protocol_version": self._version,
            "legacy_handshake": self.legacy_handshake,
            "capabilities": list(self.capability_names()),
            "extensions": list(self.negotiation_result.supported),
            "degraded_extensions": list(self.negotiation_result.degraded),
        }


def mcp_capabilities(client: MCPClient) -> dict:
    return {
        "protocol": "mcp",
        "versions": [MCP_PROTOCOL_VERSION],
        "features": {
            "stateless": True,
            "server_discover": True,
            "per_request_meta": True,
            "extension_negotiation": True,
            "legacy_stdio_fallback": True,
        },
        "meta_keys": {
            "protocol_version": MCP_META_PROTOCOL_VERSION,
            "client_capabilities": MCP_META_CLIENT_CAPABILITIES,
            "client_info": MCP_META_CLIENT_INFO,
            "server_info": MCP_META_SERVER_INFO,
        },
    }


# =============================================================================
# A2A v1.0 — wire contract
# =============================================================================

A2A_VERSION = "1.0"
A2A_MEDIA_TYPE = "application/a2a+json"
A2A_HEADER_VERSION = "A2A-Version"
A2A_HEADER_EXTENSIONS = "A2A-Extensions"

# v1.0 renamed every operation to a PascalCase method name.
A2A_METHOD = {
    "send": "SendMessage",
    "stream": "SendStreamingMessage",
    "get_task": "GetTask",
    "list_tasks": "ListTasks",
    "cancel_task": "CancelTask",
    "subscribe": "SubscribeToTask",
    "create_push_config": "CreateTaskPushNotificationConfig",
    "get_push_config": "GetTaskPushNotificationConfig",
    "list_push_configs": "ListTaskPushNotificationConfigs",
    "delete_push_config": "DeleteTaskPushNotificationConfig",
    "extended_card": "GetExtendedAgentCard",
}

# The harvest brief for this repo lists the push-config CRUD under its shorter
# names (CreatePushNotificationConfig / GetPushNotificationConfig / ...). The
# live v1.0 spec the same brief cites registers the Task-prefixed names above.
# The Task-prefixed forms are canonical on the wire; the short forms are
# accepted as input aliases so a caller written against either spelling lands
# on the right method. Never emit the aliases.
A2A_METHOD_ALIASES = {
    "CreatePushNotificationConfig": "CreateTaskPushNotificationConfig",
    "GetPushNotificationConfig": "GetTaskPushNotificationConfig",
    "ListPushNotificationConfigs": "ListTaskPushNotificationConfigs",
    "DeletePushNotificationConfig": "DeleteTaskPushNotificationConfig",
}

# ListTasks cursor pagination bounds (spec: pageSize 1..100, default 50).
A2A_PAGE_SIZE_MIN = 1
A2A_PAGE_SIZE_MAX = 100
A2A_PAGE_SIZE_DEFAULT = 50

# The exact v1.0 enum strings, verbatim. The type prefix is load-bearing.
TASK_STATE_UNSPECIFIED = "TASK_STATE_UNSPECIFIED"
TASK_STATE_SUBMITTED = "TASK_STATE_SUBMITTED"
TASK_STATE_WORKING = "TASK_STATE_WORKING"
TASK_STATE_COMPLETED = "TASK_STATE_COMPLETED"
TASK_STATE_FAILED = "TASK_STATE_FAILED"
TASK_STATE_CANCELED = "TASK_STATE_CANCELED"
TASK_STATE_REJECTED = "TASK_STATE_REJECTED"
TASK_STATE_INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
TASK_STATE_AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"

TASK_STATES = frozenset({
    TASK_STATE_UNSPECIFIED,
    TASK_STATE_SUBMITTED,
    TASK_STATE_WORKING,
    TASK_STATE_COMPLETED,
    TASK_STATE_FAILED,
    TASK_STATE_CANCELED,
    TASK_STATE_REJECTED,
    TASK_STATE_INPUT_REQUIRED,
    TASK_STATE_AUTH_REQUIRED,
})

# The eight states the spec enumerates as assignable; UNSPECIFIED is the
# proto default meaning "not set", not a lifecycle position.
ASSIGNABLE_TASK_STATES = frozenset(TASK_STATES - {TASK_STATE_UNSPECIFIED})

TASK_STATE_TERMINAL = frozenset({
    TASK_STATE_COMPLETED, TASK_STATE_FAILED, TASK_STATE_CANCELED, TASK_STATE_REJECTED,
})
TASK_STATE_INTERRUPTED = frozenset({
    TASK_STATE_INPUT_REQUIRED, TASK_STATE_AUTH_REQUIRED,
})

# Cockpit sections. UNSPECIFIED gets its own bucket so an unset state cannot be
# mistaken for live work.
SECTION_IN_FLIGHT = "in_flight"
SECTION_NEEDS_YOU = "needs_you"
SECTION_REVIEW = "review_queue"
SECTION_UNKNOWN = "unknown"

_TASK_STATE_SECTION = {
    TASK_STATE_UNSPECIFIED: SECTION_UNKNOWN,
    TASK_STATE_SUBMITTED: SECTION_IN_FLIGHT,
    TASK_STATE_WORKING: SECTION_IN_FLIGHT,
    TASK_STATE_INPUT_REQUIRED: SECTION_NEEDS_YOU,
    TASK_STATE_AUTH_REQUIRED: SECTION_NEEDS_YOU,
    TASK_STATE_COMPLETED: SECTION_REVIEW,
    TASK_STATE_FAILED: SECTION_REVIEW,
    TASK_STATE_CANCELED: SECTION_REVIEW,
    TASK_STATE_REJECTED: SECTION_REVIEW,
}

# AUTH_REQUIRED is a credential prompt, not a question: different card, and it
# must never be rendered as something the model can answer from context alone.
_TASK_STATE_CARD_KIND = {
    TASK_STATE_INPUT_REQUIRED: "question",
    TASK_STATE_AUTH_REQUIRED: "auth",
}

_A2A_V030_STATES = {
    "submitted": TASK_STATE_SUBMITTED,
    "working": TASK_STATE_WORKING,
    "completed": TASK_STATE_COMPLETED,
    "failed": TASK_STATE_FAILED,
    "canceled": TASK_STATE_CANCELED,
    "rejected": TASK_STATE_REJECTED,
    "input-required": TASK_STATE_INPUT_REQUIRED,
    "auth-required": TASK_STATE_AUTH_REQUIRED,
}
_A2A_V030_KINDS = {"text", "file", "data", "status-update", "artifact-update"}

A2A_EVENT_STATUS_UPDATE = "statusUpdate"
A2A_EVENT_ARTIFACT_UPDATE = "artifactUpdate"
A2A_EVENT_TASK = "task"
A2A_EVENT_MESSAGE = "message"
_A2A_EVENT_MEMBERS = (A2A_EVENT_STATUS_UPDATE, A2A_EVENT_ARTIFACT_UPDATE,
                      A2A_EVENT_TASK, A2A_EVENT_MESSAGE)


def section_for_state(state: str) -> str:
    """Map an exact ``TASK_STATE_*`` string onto exactly one cockpit section.

    Unknown strings raise rather than defaulting: silently bucketing a misread
    state is how a v0.3.0 payload ends up displayed as live work.
    """
    try:
        return _TASK_STATE_SECTION[state]
    except KeyError:
        raise ValueError(
            f"not a v1.0 A2A task state: {state!r} (expected one of "
            f"{sorted(TASK_STATES)})") from None


def card_kind_for_state(state: str) -> str | None:
    section_for_state(state)  # validate
    return _TASK_STATE_CARD_KIND.get(state)


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _walk_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_strings(item)


def detect_version_skew(payload: Any) -> str | None:
    """Return an actionable message if *payload* looks like a v0.3.0 A2A payload.

    Returns ``None`` for a well-formed v1.0 payload. The message always names
    the v1.0 form of whatever legacy value was found.
    """
    if not isinstance(payload, Mapping):
        return None

    legacy_kind = payload.get("kind") if payload.get("kind") in _A2A_V030_KINDS else None
    legacy_states = [s for s in _walk_strings(payload) if s in _A2A_V030_STATES]
    if legacy_kind is None and not legacy_states:
        return None

    parts = ["v0.3.0-shaped A2A payload detected"]
    if legacy_kind is not None:
        parts.append(
            f"the 'kind' discriminator ({legacy_kind!r}) was removed in v1.0; Parts are "
            "unified (text/raw/url/data) and stream events are discriminated by member "
            f"name ({A2A_EVENT_STATUS_UPDATE}/{A2A_EVENT_ARTIFACT_UPDATE})")
    if legacy_states:
        mapping = ", ".join(f"{old!r} -> {_A2A_V030_STATES[old]!r}"
                            for old in dict.fromkeys(legacy_states))
        parts.append(f"legacy enum form(s): {mapping}")
    return "; ".join(parts)


# -- unified Part -------------------------------------------------------------

DEFAULT_MAX_RAW_BYTES = 10 * 1024 * 1024
_PART_ARMS = ("text", "raw", "url", "data")


@dataclass
class Part:
    """The v1.0 unified Part: exactly one of text/raw/url/data."""

    kind: str
    text: str | None = None
    raw: bytes | None = None
    url: str | None = None
    data: Any = None
    media_type: str | None = None
    filename: str | None = None
    metadata: dict | None = None
    #: The url arm is a *deferred* fetch — this module never dereferences it.
    deferred: bool = False

    def to_wire(self) -> dict:
        wire: dict = {}
        if self.kind == "text":
            wire["text"] = self.text
        elif self.kind == "raw":
            wire["raw"] = base64.b64encode(self.raw or b"").decode("ascii")
        elif self.kind == "url":
            wire["url"] = self.url
            wire["deferred"] = self.deferred
        elif self.kind == "data":
            wire["data"] = copy.deepcopy(self.data)
        if self.media_type is not None:
            wire["mediaType"] = self.media_type
        if self.filename is not None:
            wire["filename"] = self.filename
        if self.metadata is not None:
            wire["metadata"] = copy.deepcopy(self.metadata)
        return wire


def normalize_part(payload: Mapping[str, Any], *,
                   max_raw_bytes: int = DEFAULT_MAX_RAW_BYTES) -> Part:
    """Coerce a wire Part into :class:`Part`, refusing ambiguity.

    Exactly one arm must be present — the spec's OneOf is a hard constraint, so
    zero or two arms is an error rather than a "best guess" merge.
    """
    if not isinstance(payload, Mapping):
        raise ValueError(f"part must be an object: {payload!r}")

    skew = detect_version_skew(payload)
    if skew is not None:
        raise A2AVersionSkewError(skew)

    arms = [arm for arm in _PART_ARMS if arm in payload]
    if len(arms) != 1:
        raise ValueError(
            f"Part must carry exactly one of {_PART_ARMS}, got {arms!r}")

    arm = arms[0]
    media_type = payload.get("mediaType")
    filename = payload.get("filename")
    metadata = payload.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, Mapping) else None

    if arm == "text":
        return Part("text", text=str(payload["text"]), media_type=media_type,
                    filename=filename, metadata=metadata)

    if arm == "raw":
        encoded = payload["raw"]
        if not isinstance(encoded, str):
            raise ValueError("raw part must be base64 text")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise ValueError(f"raw part is not valid base64: {exc}") from exc
        if len(decoded) > max_raw_bytes:
            raise ValueError(
                f"raw part is {len(decoded)} bytes, over the {max_raw_bytes} byte limit")
        return Part("raw", raw=decoded, media_type=media_type,
                    filename=filename, metadata=metadata)

    if arm == "url":
        url = str(payload["url"])
        if not url:
            raise ValueError("url part must not be empty")
        return Part("url", url=url, media_type=media_type, filename=filename,
                    metadata=metadata, deferred=True)

    return Part("data", data=copy.deepcopy(payload["data"]), media_type=media_type,
                filename=filename, metadata=metadata)


def _part_text(parts: Any) -> str:
    """Concatenated text of a parts list, ignoring non-text arms."""
    chunks = []
    for raw in parts or ():
        try:
            part = normalize_part(raw)
        except (ValueError, InteropError):
            continue
        if part.kind == "text":
            chunks.append(part.text or "")
    return "".join(chunks)


def classify_stream_event(payload: Mapping[str, Any]) -> str | None:
    """Discriminate a v1.0 stream event by JSON member name.

    v1.0 removed the v0.3.0 ``kind`` field and the ``final`` flag: the event
    type *is* the member name. Anything ambiguous or unrecognised returns
    ``None`` so the caller logs and skips instead of guessing (key-presence
    sniffing is exactly how a v0.3.0 ``{"taskId":..., "status":...}`` gets
    promoted into a bogus update).
    """
    if not isinstance(payload, Mapping):
        return None
    present = [member for member in _A2A_EVENT_MEMBERS if member in payload]
    if len(present) == 1:
        return present[0]
    if len(present) > 1:
        log.warning("a2a: ambiguous stream event names %s; skipping", present)
    return None


def unknown_variant_hint(payload: Mapping[str, Any]) -> str:
    known = set(_A2A_EVENT_MEMBERS) | {"kind", "taskId", "contextId", "status", "artifact"}
    for key in payload:
        if key not in known:
            return str(key)
    return "?"


def status_event(state: str, *, task_id: str = "t1", context_id: str = "c1") -> dict:
    """Build a ``TaskStatusUpdateEvent`` (handy for tests and local emitters)."""
    section_for_state(state)  # nothing may emit a state we cannot place
    return {A2A_EVENT_STATUS_UPDATE: {
        "taskId": task_id, "contextId": context_id, "status": {"state": state},
    }}


def artifact_event(artifact_id: str, text: str, *, append: bool | None = None,
                   last_chunk: bool | None = None, task_id: str = "t1",
                   context_id: str = "c1") -> dict:
    """Build a ``TaskArtifactUpdateEvent``."""
    artifact: dict = {"artifactId": artifact_id,
                      "parts": [{"text": text, "mediaType": "text/plain"}]}
    if append is not None:
        artifact["append"] = append
    if last_chunk is not None:
        artifact["lastChunk"] = last_chunk
    return {A2A_EVENT_ARTIFACT_UPDATE: {
        "taskId": task_id, "contextId": context_id, "artifact": artifact,
    }}


# -- google.rpc.Status --------------------------------------------------------

RPC_STATUS_NAMES = {
    0: "OK",
    1: "CANCELLED",
    2: "UNKNOWN",
    3: "INVALID_ARGUMENT",
    4: "DEADLINE_EXCEEDED",
    5: "NOT_FOUND",
    6: "ALREADY_EXISTS",
    7: "PERMISSION_DENIED",
    8: "RESOURCE_EXHAUSTED",
    9: "FAILED_PRECONDITION",
    10: "ABORTED",
    11: "OUT_OF_RANGE",
    12: "UNIMPLEMENTED",
    13: "INTERNAL",
    14: "UNAVAILABLE",
    15: "DATA_LOSS",
    16: "UNAUTHENTICATED",
}

_ERROR_INFO_TYPE = "type.googleapis.com/google.rpc.ErrorInfo"


@dataclass
class RPCStatus:
    code: int
    message: str = ""
    details: tuple = ()
    error_info: dict | None = None

    @property
    def name(self) -> str:
        return RPC_STATUS_NAMES.get(self.code, "UNKNOWN")

    @property
    def reason(self) -> str | None:
        return (self.error_info or {}).get("reason")

    @property
    def domain(self) -> str | None:
        return (self.error_info or {}).get("domain")

    def render(self) -> str:
        """Canonical-code rendering — never a bare integer."""
        return f"{self.name}: {self.message}" if self.message else self.name

    def as_dict(self) -> dict:
        out = {"code": self.code, "name": self.name, "message": self.message}
        if self.error_info:
            out["errorInfo"] = copy.deepcopy(self.error_info)
        return out


def parse_rpc_status(payload: Mapping[str, Any]) -> RPCStatus:
    if not isinstance(payload, Mapping):
        raise ValueError(f"rpc status must be an object: {payload!r}")
    details = tuple(payload.get("details") or ())
    error_info = None
    for detail in details:
        if isinstance(detail, Mapping) and detail.get("@type") == _ERROR_INFO_TYPE:
            error_info = copy.deepcopy(dict(detail))
            break
    try:
        code = int(payload.get("code", 2))
    except (TypeError, ValueError):
        code = 2
    return RPCStatus(code=code, message=str(payload.get("message") or ""),
                     details=details, error_info=error_info)


# -- SSE-shaped stream emitter ------------------------------------------------


class SSEShapedEmitter:
    """Formats A2A stream events as ``text/event-stream`` frames.

    ``event:`` carries the v1.0 discriminator (the member name); unknown
    variants fall back to ``message`` so a forward-compatible peer cannot crash
    the stream.
    """

    media_type = "text/event-stream"

    def frame(self, event: Mapping[str, Any], *, event_id: str | None = None) -> str:
        name = classify_stream_event(event) or A2A_EVENT_MESSAGE
        lines = [f"event: {name}"]
        if event_id is not None:
            lines.append(f"id: {event_id}")
        lines.append("data: " + json.dumps(event, sort_keys=True))
        return "\n".join(lines) + "\n\n"

    def frames(self, events: Iterable[Mapping[str, Any]]) -> str:
        return "".join(self.frame(event) for event in events)


# -- streaming consumer -------------------------------------------------------


@dataclass
class StreamOutcome:
    """What one reduction of a stream did to the cockpit."""

    cards: dict = field(default_factory=dict)
    artifacts: list = field(default_factory=list)
    open_artifacts: tuple = ()
    chunk_count: int = 0
    new_rows: int = 0
    skipped: tuple = ()
    dropped_duplicate_chunks: int = 0
    buffers: dict = field(default_factory=dict)

    def buffer_for(self, artifact_id: str) -> str:
        return self.buffers.get(artifact_id, "")


class A2AStreamConsumer:
    """Consumes ``SendStreamingMessage`` / ``SubscribeToTask`` event streams.

    Design rules encoded here, all learned from the spec's change list:

    * events are switched on the discriminator, with an explicit unknown branch
      that logs and skips;
    * artifact chunks with ``append`` accumulate into one artifact, sealed by
      ``lastChunk`` — a chunked reply is one row, not one row per chunk;
    * status updates mutate a card in place;
    * a dropped stream re-attaches via ``SubscribeToTask`` and reconciles by
      artifactId without duplicating content.
    """

    def __init__(self, transport: InMemoryTransport | None = None, *,
                 capabilities: Mapping[str, bool] | None = None,
                 extensions: Sequence[str] = ()) -> None:
        self.transport = transport
        self.capabilities = dict(capabilities or {})
        self.extensions = tuple(extensions)
        self.reconciliations = 0
        self.dropped_duplicate_chunks = 0

        self._cards: dict[str, dict] = {}
        self._sealed: list[dict] = []
        self._buffers: dict[str, str] = {}
        self._open: dict[str, dict] = {}
        self._chunk_count = 0
        self._new_rows = 0
        self._applied_chunks: set[tuple[str, str]] = set()

    # -- capability gate --

    def _require_streaming(self) -> None:
        if self.capabilities.get("streaming") is not True:
            raise UnsupportedOperationError(
                "peer does not advertise the streaming capability; "
                "SendStreamingMessage/SubscribeToTask must not be called")

    def open(self, *, task_id: str, context_id: str) -> Any:
        """Open a stream. The capability check runs *before* the RPC."""
        self._require_streaming()
        if self.transport is None:
            raise InteropError("streaming requires a transport")
        return self.transport.call(
            A2A_METHOD["stream"],
            {"taskId": task_id, "contextId": context_id},
            headers=negotiate_headers(A2A_VERSION, self.extensions),
            media_type=A2A_MEDIA_TYPE,
        )

    # -- reduction --

    def consume(self, events: Iterable[Mapping[str, Any]]) -> StreamOutcome:
        for event in events:
            self._apply(event)
        return self.outcome()

    def resubscribe_events(self, events: Iterable[Mapping[str, Any]]) -> StreamOutcome:
        """Re-attach after a drop: replay without duplicating what we have."""
        self.reconciliations += 1
        for event in events:
            self._apply(event, reconcile=True)
        return self.outcome()

    def _apply(self, event: Mapping[str, Any], *, reconcile: bool = False) -> None:
        variant = classify_stream_event(event)

        if variant == A2A_EVENT_TASK:
            self._apply_task(event[A2A_EVENT_TASK])
        elif variant == A2A_EVENT_STATUS_UPDATE:
            self._apply_status(event[A2A_EVENT_STATUS_UPDATE])
        elif variant == A2A_EVENT_ARTIFACT_UPDATE:
            self._apply_artifact(event[A2A_EVENT_ARTIFACT_UPDATE], reconcile=reconcile)
        elif variant == A2A_EVENT_MESSAGE:
            log.debug("a2a: standalone message event ignored by the task cockpit")
        else:
            hint = unknown_variant_hint(event)
            log.warning(
                "a2a: skipping unrecognised stream variant %r (v1.0 discriminates by "
                "member name: %s / %s)", hint, A2A_EVENT_STATUS_UPDATE,
                A2A_EVENT_ARTIFACT_UPDATE)
            self._skipped = getattr(self, "_skipped", [])
            self._skipped.append(event)

    # -- individual handlers --

    def _card(self, task_id: str, context_id: str | None) -> dict:
        card = self._cards.get(task_id)
        if card is None:
            card = {"taskId": task_id, "contextId": context_id, "mutations": 0,
                    "state": TASK_STATE_UNSPECIFIED, "section": SECTION_UNKNOWN,
                    "cardKind": None}
            self._cards[task_id] = card
            self._new_rows += 1
        elif context_id and not card.get("contextId"):
            card["contextId"] = context_id
        return card

    def _mutate(self, task_id: str, context_id: str | None, state: str,
                *, mutate: bool) -> None:
        section = section_for_state(state)
        card = self._card(task_id, context_id)
        card["state"] = state
        card["section"] = section
        card["cardKind"] = card_kind_for_state(state)
        if mutate:
            card["mutations"] += 1

    def _apply_task(self, task: Mapping[str, Any]) -> None:
        task_id = str(task.get("id") or task.get("taskId") or "?")
        state = (task.get("status") or {}).get("state") or TASK_STATE_UNSPECIFIED
        self._mutate(task_id, task.get("contextId"), state, mutate=False)

    def _apply_status(self, update: Mapping[str, Any]) -> None:
        task_id = str(update.get("taskId") or "?")
        state = (update.get("status") or {}).get("state") or TASK_STATE_UNSPECIFIED
        self._mutate(task_id, update.get("contextId"), state, mutate=True)

    def _apply_artifact(self, update: Mapping[str, Any],
                        *, reconcile: bool = False) -> None:
        artifact = update.get("artifact")
        if not isinstance(artifact, Mapping):
            artifact = {}
        if "append" in artifact or "lastChunk" in artifact:
            # A common misreading that silently renders a chunked reply as N blobs.
            log.warning(
                "a2a: append/lastChunk found inside `artifact`; the spec places them on "
                "the TaskArtifactUpdateEvent itself, so they will be ignored here")

        artifact_id = str(artifact.get("artifactId") or "?")
        text = _part_text(artifact.get("parts"))
        key = (artifact_id, hashlib.sha1(text.encode("utf-8")).hexdigest())

        if key in self._applied_chunks:
            self.dropped_duplicate_chunks += 1
            log.debug("a2a: dropping already-applied chunk for artifact %s", artifact_id)
            return
        self._applied_chunks.add(key)

        append = bool(update.get("append"))
        if not append and artifact_id not in self._open:
            self._buffers[artifact_id] = text
            self._open[artifact_id] = {"taskId": update.get("taskId"),
                                       "contextId": update.get("contextId")}
        else:
            self._buffers[artifact_id] = self._buffers.get(artifact_id, "") + text
        self._open.setdefault(artifact_id, {"taskId": update.get("taskId"),
                                            "contextId": update.get("contextId")})
        self._chunk_count += 1

        if update.get("lastChunk"):
            self._seal(artifact_id)

    def _seal(self, artifact_id: str) -> None:
        meta = self._open.pop(artifact_id, {})
        self._sealed.append({
            "artifactId": artifact_id,
            "text": self._buffers.get(artifact_id, ""),
            "sealed": True,
            "taskId": meta.get("taskId"),
            "contextId": meta.get("contextId"),
        })

    # -- views --

    def outcome(self) -> StreamOutcome:
        return StreamOutcome(
            cards=copy.deepcopy(self._cards),
            artifacts=copy.deepcopy(self._sealed),
            open_artifacts=tuple(sorted(self._open)),
            chunk_count=self._chunk_count,
            new_rows=self._new_rows,
            skipped=tuple(getattr(self, "_skipped", ())),
            dropped_duplicate_chunks=self.dropped_duplicate_chunks,
            buffers=copy.deepcopy(self._buffers),
        )

    def sealed_artifacts(self) -> list[dict]:
        return copy.deepcopy(self._sealed)

    def cards(self) -> dict:
        return copy.deepcopy(self._cards)

    def buffer_for(self, artifact_id: str) -> str:
        return self._buffers.get(artifact_id, "")


class A2AClient:
    """A thin A2A v1.0 client over a local transport."""

    def __init__(self, transport: InMemoryTransport, *,
                 extensions: Sequence[str] = (),
                 capabilities: Mapping[str, bool] | None = None) -> None:
        self.transport = transport
        self.extensions = tuple(extensions)
        self.capabilities = dict(capabilities or {})

    def call(self, op: str, params: Mapping[str, Any] | None = None) -> Any:
        method = A2A_METHOD.get(op, op)
        method = A2A_METHOD_ALIASES.get(method, method)
        result = self.transport.call(
            method, dict(params or {}),
            headers=negotiate_headers(A2A_VERSION, self.extensions),
            media_type=A2A_MEDIA_TYPE,
        )
        if isinstance(result, Mapping) and "error" in result:
            raise RPCError(parse_rpc_status(result["error"]))
        return result

    def get_task(self, task_id: str) -> dict:
        return self.call("get_task", {"id": task_id})

    def list_tasks(self, *, page_size: int | None = None) -> dict:
        params: dict = {}
        if page_size is not None:
            if isinstance(page_size, bool) or not isinstance(page_size, int):
                raise ValueError(f"pageSize must be an integer, got {page_size!r}")
            if not (A2A_PAGE_SIZE_MIN <= page_size <= A2A_PAGE_SIZE_MAX):
                raise ValueError(
                    f"pageSize must be {A2A_PAGE_SIZE_MIN}..{A2A_PAGE_SIZE_MAX} "
                    f"per the spec, got {page_size}")
            params["pageSize"] = page_size
        return self.call("list_tasks", params)

    def send_message(self, *, text: str, context_id: str | None = None,
                     task_id: str | None = None) -> dict:
        if task_id is not None:
            existing = self.get_task(task_id)
            state = (existing.get("status") or {}).get("state") or TASK_STATE_UNSPECIFIED
            if state in TASK_STATE_TERMINAL:
                raise UnsupportedOperationError(
                    f"task {task_id!r} is in terminal state {state!r}; "
                    "SendMessage to a terminal task is unsupported")

        message: dict = {
            "role": "ROLE_USER",
            "messageId": uuid.uuid4().hex,
            "parts": [{"text": text, "mediaType": "text/plain"}],
        }
        params: dict = {"message": message,
                        "configuration": {"returnImmediately": True}}
        if context_id is not None:
            params["contextId"] = context_id
        if task_id is not None:
            params["taskId"] = task_id

        result = self.call("send", params)
        if isinstance(result, Mapping) and "task" in result:
            return result["task"]
        return result


def a2a_capabilities() -> dict:
    return {
        "protocol": "a2a",
        "versions": [A2A_VERSION],
        "features": {
            "streaming": True,
            "resumable_stream": True,
            "unified_part": True,
            "push_notifications": True,
            "extended_agent_card": True,
        },
        "methods": dict(sorted(A2A_METHOD.items())),
        "states": sorted(TASK_STATES),
        "terminal_states": sorted(TASK_STATE_TERMINAL),
        "interrupted_states": sorted(TASK_STATE_INTERRUPTED),
        "event_variants": [A2A_EVENT_STATUS_UPDATE, A2A_EVENT_ARTIFACT_UPDATE],
    }


# =============================================================================
# AG-UI — capability discovery + shared state
# =============================================================================

AGUI_STATE_EVENT_TYPES = ("STATE_SNAPSHOT", "STATE_DELTA")

#: The capability document is served over the same endpoint as the event
#: stream — one URL a client both discovers and subscribes on.
AGUI_ENDPOINT = "/ag-ui/agent"

#: The real shared day-state document is an explicit *allowlist* (queues,
#: mutes, pins, focus, filters). An unknown key is dropped at the boundary
#: rather than mirrored, so a new sensitive field cannot leak just because
#: nobody remembered to add it to a denylist.
SHARED_DAY_STATE_KEYS = ("queues", "mutes", "pins", "focus", "filters")

AGUI_IDENTITY = {
    "name": "hermes-day",
    "type": "agent",
    "description": "Hermes Day interop harness",
    "version": "1.0",
    "provider": "hermes-day",
    "documentationUrl": "https://hermes-agent.nousresearch.com/docs",
}

_AGUI_BASE = {
    "transport": {"streaming": True, "websocket": False, "httpBinary": False,
                  "pushNotifications": False, "resumable": False},
    "state": {"snapshots": True, "deltas": True, "persistence": False},
    "humanInTheLoop": {"approvals": True, "interventions": False, "feedback": False,
                       "interrupts": True, "approveWithEdits": False},
    "reasoning": {"supported": False},
    "multiAgent": {"delegation": True, "handoffs": False, "subagents": True},
    "tools": {"supported": True, "items": [], "clientProvided": False},
    "output": {"structuredOutput": True, "mimeTypes": ["application/json", "text/plain"]},
}


def _surface(**overrides) -> dict:
    import copy as _copy
    doc = _copy.deepcopy(_AGUI_BASE)
    for key, value in overrides.items():
        if isinstance(value, Mapping) and isinstance(doc.get(key), Mapping):
            doc[key].update(value)
        else:
            doc[key] = value
    return doc


_DEFAULT_SURFACES = {
    "desktop": _surface(
        transport={"streaming": True, "websocket": True, "httpBinary": False,
                   "pushNotifications": True, "resumable": True},
        state={"snapshots": True, "deltas": True, "persistence": True},
        humanInTheLoop={"approvals": True, "interventions": True, "feedback": True,
                        "interrupts": True, "approveWithEdits": True},
        reasoning={"supported": True},
        output={"structuredOutput": True,
                "mimeTypes": ["application/json", "text/plain", "text/markdown"]},
    ),
    "tui": _surface(
        transport={"websocket": False, "pushNotifications": False, "resumable": False},
        state={"persistence": False},
        humanInTheLoop={"interventions": False, "feedback": False,
                        "approveWithEdits": False},
        reasoning={"supported": False},
        output={"structuredOutput": False, "mimeTypes": ["text/plain"]},
    ),
    "discord": _surface(
        transport={"streaming": False, "websocket": False, "pushNotifications": False,
                   "resumable": False},
        state={"deltas": False, "persistence": False},
        humanInTheLoop={"interventions": False, "feedback": True,
                        "approveWithEdits": False},
        reasoning={"supported": False},
        multiAgent={"handoffs": False},
        output={"structuredOutput": False, "mimeTypes": ["text/markdown"]},
    ),
}

_AGUI_CUSTOM = {"com.hermes-day/surfaces": ["desktop", "tui", "discord"]}


class SurfaceCapabilities:
    """Per-surface capability documents (AG-UI ``getCapabilities``).

    A capability is only present when the surface explicitly declares it —
    ``feature_enabled`` gates on ``is True``, so an omitted field and an
    explicit ``false`` behave identically instead of one of them silently
    meaning "maybe".
    """

    def __init__(self, surfaces: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        source = surfaces if surfaces is not None else _DEFAULT_SURFACES
        self._surfaces = copy.deepcopy(dict(source))

    def register(self, surface: str, capabilities: Mapping[str, Any], *,
                 merge: bool = True) -> None:
        if merge and surface in self._surfaces:
            doc = self._surfaces[surface]
            for key, value in capabilities.items():
                if isinstance(value, Mapping) and isinstance(doc.get(key), Mapping):
                    doc[key].update(copy.deepcopy(dict(value)))
                else:
                    doc[key] = copy.deepcopy(value)
        else:
            self._surfaces[surface] = copy.deepcopy(dict(capabilities))

    def get(self, surface: str) -> dict:
        doc = copy.deepcopy(self._surfaces.get(surface, {}))
        if not doc:
            # unknown surface: assert nothing (absent-means-unknown)
            return {"identity": {"name": surface, "type": "unknown"},
                    "transport": {}, "custom": {}}
        doc.setdefault("identity", copy.deepcopy(AGUI_IDENTITY))
        doc.setdefault("custom", copy.deepcopy(_AGUI_CUSTOM))
        return doc

    def surfaces(self) -> tuple[str, ...]:
        return tuple(sorted(self._surfaces))

    def as_dict(self) -> dict:
        return {name: self.get(name) for name in self.surfaces()}


_DEFAULT_SURFACE_REGISTRY = SurfaceCapabilities()


def surface_capabilities(surface: str, *,
                         registry: SurfaceCapabilities | None = None) -> dict:
    return (registry or _DEFAULT_SURFACE_REGISTRY).get(surface)


def get_capabilities(surface: str = "desktop", *,
                     registry: SurfaceCapabilities | None = None) -> dict:
    """AG-UI capability discovery for one surface."""
    return surface_capabilities(surface, registry=registry)


def feature_enabled(capabilities: Mapping[str, Any] | None, path: Sequence[str]) -> bool:
    """Explicit-equality capability gate.

    Returns ``True`` only when every step of *path* exists and the leaf is
    exactly ``True``. An absent field is unknown, and unknown is never consent.
    """
    if not isinstance(capabilities, Mapping):
        return False
    node: Any = capabilities
    for step in path:
        if not isinstance(node, Mapping) or step not in node:
            return False
        node = node[step]
    return node is True


def output_plan(capabilities: Mapping[str, Any], *, wants: str = "structured") -> dict:
    """Pick an output mode, degrading to text rather than erroring."""
    mime_types = list(((capabilities or {}).get("output") or {}).get("mimeTypes") or ())
    if wants == "structured" and feature_enabled(capabilities,
                                                 ("output", "structuredOutput")):
        return {"mode": "structured", "mime_type": "application/json", "degraded": False}
    fallback = mime_types[0] if mime_types else "text/plain"
    return {"mode": "text", "mime_type": fallback, "degraded": wants == "structured"}


# -- shared state (RFC 6902) --------------------------------------------------

_JSON_POINTER_ESCAPES = (("~1", "/"), ("~0", "~"))


def _pointer_tokens(path: str) -> list[str]:
    if path == "":
        return []
    if not path.startswith("/"):
        raise ValueError(f"JSON Pointer must start with '/': {path!r}")
    tokens = []
    for raw in path.split("/")[1:]:
        for old, new in _JSON_POINTER_ESCAPES:
            raw = raw.replace(old, new)
        tokens.append(raw)
    return tokens


def _resolve(document: Any, tokens: list[str]) -> Any:
    node = document
    for token in tokens:
        if isinstance(node, Mapping):
            if token not in node:
                raise KeyError("/".join(tokens))
            node = node[token]
        elif isinstance(node, list):
            node = node[int(token)]  # raises ValueError on a non-index token
        else:
            raise KeyError("/".join(tokens))
    return node


def _parent_and_key(document: Any, tokens: list[str]):
    if not tokens:
        raise KeyError("the whole document has no parent")
    parent = _resolve(document, tokens[:-1])
    return parent, tokens[-1]


def _op_add(document: Any, op: Mapping[str, Any]) -> None:
    tokens = _pointer_tokens(str(op.get("path", "")))
    if not tokens:
        raise ValueError("add at the document root is not supported here")
    parent, key = _parent_and_key(document, tokens)
    if isinstance(parent, list):
        if key == "-":
            parent.append(copy.deepcopy(op.get("value")))
            return
        parent.insert(int(key), copy.deepcopy(op.get("value")))
        return
    parent[key] = copy.deepcopy(op.get("value"))


def _op_remove(document: Any, op: Mapping[str, Any]) -> None:
    tokens = _pointer_tokens(str(op.get("path", "")))
    parent, key = _parent_and_key(document, tokens)
    if isinstance(parent, list):
        del parent[int(key)]
        return
    if key not in parent:
        raise KeyError(key)
    del parent[key]


def _op_replace(document: Any, op: Mapping[str, Any]) -> None:
    tokens = _pointer_tokens(str(op.get("path", "")))
    parent, key = _parent_and_key(document, tokens)
    if isinstance(parent, list):
        parent[int(key)] = copy.deepcopy(op.get("value"))
        return
    if key not in parent:
        raise KeyError(key)
    parent[key] = copy.deepcopy(op.get("value"))


def _op_move(document: Any, op: Mapping[str, Any]) -> None:
    source = _resolve(document, _pointer_tokens(str(op.get("from", ""))))
    _op_remove(document, {"path": op.get("from")})
    _op_add(document, {"path": op.get("path"), "value": source})


def _op_copy(document: Any, op: Mapping[str, Any]) -> None:
    source = _resolve(document, _pointer_tokens(str(op.get("from", ""))))
    _op_add(document, {"path": op.get("path"), "value": source})


def _op_test(document: Any, op: Mapping[str, Any]) -> None:
    actual = _resolve(document, _pointer_tokens(str(op.get("path", ""))))
    if actual != op.get("value"):
        raise ValueError(
            f"test failed at {op.get('path')!r}: {actual!r} != {op.get('value')!r}")


_RFC6902_OPS = {
    "add": _op_add, "remove": _op_remove, "replace": _op_replace,
    "move": _op_move, "copy": _op_copy, "test": _op_test,
}


def _apply_patch(document: Any, patch: Sequence[Mapping[str, Any]]) -> Any:
    """Apply an RFC 6902 patch to a copy, all-or-nothing."""
    working = copy.deepcopy(document)
    for op in patch:
        if not isinstance(op, Mapping):
            raise ValueError(f"patch operation must be an object: {op!r}")
        handler = _RFC6902_OPS.get(str(op.get("op")))
        if handler is None:
            raise ValueError(f"unknown JSON-Patch operation: {op.get('op')!r}")
        handler(working, op)
    return working


class SharedState:
    """AG-UI shared state: snapshots replace, deltas are RFC 6902 patches.

    Two invariants the spec's tests exist to protect:

    * a ``STATE_SNAPSHOT`` **replaces** the whole document — merging leaves
      stale keys alive forever;
    * a ``STATE_DELTA`` is applied in sequence with all-or-nothing semantics,
      to a copy, so a patch that fails on its 3rd operation leaves the visible
      state byte-identical.
    """

    #: Never mirrored into a surface: presenters are not a trust boundary.
    DEFAULT_EXCLUDED_KEYS = ("session_content", "tokens")

    def __init__(self, initial: Mapping[str, Any] | None = None, *,
                 excluded_keys: Sequence[str] | None = None,
                 allowlist: Sequence[str] | None = None) -> None:
        self.excluded_keys = tuple(sorted(
            self.DEFAULT_EXCLUDED_KEYS if excluded_keys is None else excluded_keys))
        #: When set, only these top-level keys survive at all. The spec's
        #: wording for the real shared document is an *explicit allowlist*
        #: (``queues, mutes, pins, focus, filters``): an unknown key is dropped
        #: rather than mirrored, so a new sensitive field cannot leak just
        #: because nobody remembered to add it to a denylist.
        self.allowlist = tuple(sorted(allowlist)) if allowlist is not None else None
        self._state: dict = self._redact(dict(initial or {}))
        self.snapshot_count = 0
        self.delta_count = 0
        self.rejected_count = 0
        self.resync_count = 0
        self.last_rejected_patch: list | None = None

    def _redact(self, document: Any, *, root: bool = True) -> Any:
        if isinstance(document, Mapping):
            # The allowlist gates the *document root*: the shared day-state is
            # a fixed set of top-level keys, but their values are free-form
            # (queues.in_flight, focus.mode) and must not be filtered.
            return {key: self._redact(value, root=False)
                    for key, value in document.items()
                    if key not in self.excluded_keys
                    and (not root or self.allowlist is None
                         or key in self.allowlist)}
        if isinstance(document, list):
            return [self._redact(item, root=False) for item in document]
        return copy.deepcopy(document)

    def apply_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        self._state = self._redact(copy.deepcopy(dict(snapshot)))
        self.snapshot_count += 1

    def apply_delta(self, patch: Sequence[Mapping[str, Any]]) -> bool:
        try:
            working = _apply_patch(self._state, patch)
        except Exception as exc:
            self.rejected_count += 1
            self.last_rejected_patch = copy.deepcopy(list(patch))
            log.warning(
                "ag-ui: rejecting state delta (%s); state was %s; patch was %s",
                exc, json.dumps(self._state, sort_keys=True),
                json.dumps(self.last_rejected_patch, sort_keys=True))
            return False
        self._state = self._redact(working)
        self.delta_count += 1
        return True

    def as_dict(self) -> dict:
        return copy.deepcopy(self._state)

    def request_resync(self) -> dict:
        """Ask the agent for a fresh snapshot (recovery from a lost delta)."""
        self.resync_count += 1
        return state_snapshot_event(self.as_dict())


def shared_day_state(initial: Mapping[str, Any] | None = None) -> SharedState:
    """The real agent/frontend shared document, built on an explicit allowlist.

    Only :data:`SHARED_DAY_STATE_KEYS` are ever mirrored, so a field nobody
    thought about (a credential, a transcript) is excluded by default instead
    of by remembering to add it to a denylist.
    """
    return SharedState(initial, allowlist=SHARED_DAY_STATE_KEYS)


def state_snapshot_event(snapshot: Mapping[str, Any]) -> dict:
    return {"type": "STATE_SNAPSHOT", "snapshot": copy.deepcopy(dict(snapshot))}


def state_delta_event(patch: Sequence[Mapping[str, Any]]) -> dict:
    return {"type": "STATE_DELTA", "delta": copy.deepcopy(list(patch))}


def agui_capabilities(surface: str = "desktop", *,
                      registry: SurfaceCapabilities | None = None) -> dict:
    return {
        "protocol": "ag_ui",
        "versions": ["1.0"],
        "features": {
            "capability_discovery": True,
            "state_snapshots": True,
            "state_deltas": True,
            "rfc6902_deltas": True,
            "absent_means_unknown": True,
        },
        "surfaces": list((registry or _DEFAULT_SURFACE_REGISTRY).surfaces()),
        "surface": surface,
        "state_event_types": list(AGUI_STATE_EVENT_TYPES),
    }


# =============================================================================
# The one adapter registry
# =============================================================================

CAPABILITIES_SCHEMA = "hermes-day/interop-capabilities/1"


class MCPAdapter:
    protocol = "mcp"

    def __init__(self, transport: InMemoryTransport | None = None, *,
                 client: MCPClient | None = None,
                 extensions: ExtensionRegistry | None = None) -> None:
        self.extensions = extensions or ExtensionRegistry()
        self.transport = transport or InMemoryTransport(
            {"server/discover": {
                "resultType": "complete",
                "supportedVersions": [MCP_PROTOCOL_VERSION],
                "capabilities": {"tools": {"listChanged": True},
                                 "extensions": {"com.hermes-day/surfaces": {}}},
                "instructions": "hermes-day local adapter",
                "ttlMs": 60000,
                "cacheScope": "private",
                "_meta": {MCP_META_SERVER_INFO: {"name": "hermes-day-local",
                                                 "version": "1.0"}},
            }},
            kind="memory")
        self.client = client or MCPClient(self.transport, extensions=self.extensions)

    def discover(self, *, force: bool = False) -> DiscoverResult:
        return self.client.discover(force=force)

    def supports(self, capability: str) -> bool:
        """Ask through the registry, not the raw client."""
        return self.client.supports(capability)

    def negotiated_version(self) -> str | None:
        return self.client.negotiated_version()

    def authorization_snapshot(self) -> dict:
        return self.client.authorization_snapshot()

    def negotiation(self):
        return self.client.negotiation()

    def capabilities(self) -> dict:
        return mcp_capabilities(self.client)


class A2AAdapter:
    protocol = "a2a"

    def __init__(self, transport: InMemoryTransport | None = None, *,
                 client: A2AClient | None = None) -> None:
        self.transport = transport or InMemoryTransport(
            {"ListTasks": {"tasks": [], "nextPageToken": "",
                           "pageSize": A2A_PAGE_SIZE_DEFAULT},
             "GetTask": lambda params: {
                 "id": params.get("id", "local"),
                 "contextId": params.get("contextId", "local"),
                 "status": {"state": TASK_STATE_SUBMITTED}},
             "SendMessage": lambda params: {
                 "id": params.get("taskId") or "local",
                 "contextId": params.get("contextId") or "local",
                 "status": {"state": TASK_STATE_SUBMITTED}},
             "CancelTask": lambda params: {
                 "id": params.get("id", "local"),
                 "status": {"state": TASK_STATE_CANCELED}}},
            kind="memory")
        self.client = client or A2AClient(self.transport)

    def list_tasks(self, *, page_size: int | None = None) -> dict:
        return self.client.list_tasks(page_size=page_size)

    def get_task(self, task_id: str) -> dict:
        return self.client.get_task(task_id)

    def send_message(self, **kwargs: Any) -> dict:
        return self.client.send_message(**kwargs)

    def cancel_task(self, task_id: str) -> dict:
        return self.client.call("cancel_task", {"id": task_id})

    def subscribe(self, task_id: str) -> dict:
        return self.client.call("subscribe", {"taskId": task_id})

    def capabilities(self) -> dict:
        return a2a_capabilities()


class AGUIAdapter:
    protocol = "ag_ui"

    #: The capability document and the event stream share this endpoint.
    endpoint = AGUI_ENDPOINT

    def __init__(self, *, surface: str = "desktop",
                 surface_registry: SurfaceCapabilities | None = None) -> None:
        self.surface = surface
        self.surface_registry = surface_registry or _DEFAULT_SURFACE_REGISTRY
        self.transport = InMemoryTransport({}, kind="memory")
        self.state = SharedState()

    def event_stream_endpoint(self) -> str:
        """Where a client subscribes; the capability doc rides the same URL."""
        return self.endpoint

    def get_capabilities(self, surface: str | None = None) -> dict:
        """The AgentCapabilities document for one surface.

        This is the AG-UI ``getCapabilities()`` call, not the registry view: it
        answers "what can this agent do *here*" with the identity / transport /
        state / humanInTheLoop / reasoning / output categories a client gates
        affordances on.
        """
        return surface_capabilities(surface or self.surface,
                                    registry=self.surface_registry)

    def protocol_view(self) -> dict:
        """The registry's one-line summary of this protocol."""
        return agui_capabilities(self.surface, registry=self.surface_registry)

    def capabilities(self) -> dict:
        return self.protocol_view()


class AdapterRegistry:
    """Every protocol behind one view.

    A surface asks once (``capabilities()``) and gets what the agent can do:
    which protocol versions it speaks, which features each one has, and what the
    surface itself is allowed to render.
    """

    def __init__(self, *, surfaces: SurfaceCapabilities | None = None,
                 extensions: ExtensionRegistry | None = None) -> None:
        self.surfaces = surfaces or SurfaceCapabilities()
        self.extensions = extensions or ExtensionRegistry()
        self._adapters: dict[str, Any] = {}

    def register(self, adapter: Any) -> None:
        self._adapters[adapter.protocol] = adapter

    def get(self, protocol: str) -> Any | None:
        return self._adapters.get(protocol)

    def adapters(self) -> dict:
        return dict(self._adapters)

    def capabilities(self, *, surface: str = "desktop") -> dict:
        return {
            "schema": CAPABILITIES_SCHEMA,
            "offline": True,
            "protocols": {name: adapter.capabilities()
                          for name, adapter in sorted(self._adapters.items())},
            "surfaces": {"desktop": self.surfaces.get("desktop"),
                         "tui": self.surfaces.get("tui"),
                         "discord": self.surfaces.get("discord")},
            "surface": self.surfaces.get(surface),
            "extensions": self.extensions.client_map(),
        }


def build_registry(*, surface: str = "desktop",
                   surfaces: SurfaceCapabilities | None = None) -> AdapterRegistry:
    """Build the offline registry: MCP + A2A + AG-UI over local transports."""
    extensions = ExtensionRegistry()
    extensions.register("com.hermes-day/surfaces")
    registry = AdapterRegistry(surfaces=surfaces, extensions=extensions)
    registry.register(MCPAdapter(extensions=extensions))
    registry.register(A2AAdapter())
    registry.register(AGUIAdapter(surface=surface,
                                  surface_registry=registry.surfaces))
    return registry
