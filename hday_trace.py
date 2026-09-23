"""hday_trace — the observability lane: observation tree, OTel GenAI naming,
trajectory scoring, compact tree text.

Four mechanisms, one contract, no second store:

* **observation tree** — ``trace`` > ``span`` > ``generation`` nesting with
  parent/child linkage, durations and status. Every node emits a row in the
  EXISTING ledger row shape (``kind | lane | ms | cost | ts | sid`` — the shape
  ``_gate_log`` writes) plus an additive span sidecar keyed by run id
  (``trace_id | span_id | parent_id | run_id``). ``day-ledger`` surfaces the
  sidecar under a ``trace`` key; nothing existing is replaced.
* **OTel GenAI naming** — span names follow the published grammar
  ``{gen_ai.operation.name} {subject}`` (``gate.decide gpt-4o``,
  ``execute_tool read_file``) and attributes use the published semconv keys
  (``gen_ai.system``, ``gen_ai.request.model``, ``gen_ai.usage.input_tokens``,
  ...) so a run could be exported without renaming. A missing subject falls back
  to ``unknown`` — a name is never empty. Local-only concepts stay under
  ``hday.*``; the module never invents a ``gen_ai.*`` key.
* **trajectory scoring** — ``score_run()`` grades a completed run into a small
  TYPED score row carrying named axes (tool-call validity, wasted steps,
  retries, approval waits, honesty events). Never one opaque number.
* **text renderer** — ``render_tree()`` renders one run's tree as compact text
  for the cockpit/TUI.

Content capture (``gen_ai.input.messages`` / ``gen_ai.output.messages``) is
Opt-In per the semconv: absent unless ``capture_content=True``.

Pure stdlib, import-safe: no host imports, no side effects at import time.
"""

from __future__ import annotations

import contextvars
import logging
import re
import time
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "CONTENT_ATTRS",
    "DEFAULT_PASS_BAR",
    "FALLBACK_SUBJECT",
    "LEDGER",
    "LANES",
    "LOCAL_PREFIX",
    "NO_ROWS",
    "OPERATIONS",
    "PROVIDER_ALIASES",
    "SEMCONV_ATTRS",
    "SPAN_KINDS",
    "SPAN_TYPES",
    "STATUSES",
    "SCORE_TYPES",
    "TRAJ_AXES",
    "TRAJ_VERDICTS",
    "TRACE_VERSION",
    "ScoreRow",
    "Span",
    "Trace",
    "TrajectoryScore",
    "emit",
    "generation",
    "group_by_trace",
    "is_valid_span_name",
    "ledger_rows",
    "render_tree",
    "reset_ledger",
    "score_run",
    "semconv_attrs",
    "span",
    "span_name",
    "tool_span",
    "tool_span_call_id",
    "trace",
    "unmapped_attr_keys",
]

log = logging.getLogger("hermes-day.trace")

TRACE_VERSION = 1

#: closed span-type vocabulary (Braintrust-style: each type means one thing)
SPAN_TYPES: Tuple[str, ...] = ("trace", "span", "generation", "tool", "score",
                               "event")
#: OTel span kinds. CLIENT for remote GenAI calls, INTERNAL for in-process work.
SPAN_KINDS: Tuple[str, ...] = ("CLIENT", "INTERNAL", "SERVER")
STATUSES: Tuple[str, ...] = ("ok", "error", "unset")
#: the lane vocabulary the rest of the ledger already uses
LANES: Tuple[str, ...] = ("local", "classifier", "hosted")

#: closed operation vocabulary — the first token of every span name. A lane that
#: wants a new operation adds it here; a free-form name fails the suite.
OPERATIONS: Tuple[str, ...] = (
    "trace", "span",
    "gate.decide", "ctxscore.score", "router.rank",
    "execute_tool", "chat", "generate_content", "text_completion", "embeddings",
    "invoke_agent", "invoke_workflow", "plan", "retrieval", "fetch_response",
    "approval.wait", "honesty.check", "guardrail.check",
    "score", "evaluation.result",
)

#: documented fallback: a span name is never empty
FALLBACK_SUBJECT = "unknown"
_DEFAULT_OPERATION = "span"
_NAME_RE = re.compile(r"^[a-z][a-z0-9_.]* \S+$")

#: local row field -> published OTel GenAI semconv attribute key
SEMCONV_ATTRS: Dict[str, str] = {
    "operation_name": "gen_ai.operation.name",
    "provider": "gen_ai.system",
    "request_model": "gen_ai.request.model",
    "response_model": "gen_ai.response.model",
    "input_tokens": "gen_ai.usage.input_tokens",
    "output_tokens": "gen_ai.usage.output_tokens",
    "tool_name": "gen_ai.tool.name",
    "tool_call_id": "gen_ai.tool.call.id",
    "tool_type": "gen_ai.tool.type",
    "error_type": "error.type",
    "conversation_id": "gen_ai.conversation.id",
    "evaluation_name": "gen_ai.evaluation.name",
    "evaluation_label": "gen_ai.evaluation.score.label",
    "evaluation_value": "gen_ai.evaluation.score.value",
}
#: the provider attribute ships under either published spelling; we emit the
#: first and accept the second on ingest.
PROVIDER_ALIASES: Tuple[str, ...] = ("gen_ai.system", "gen_ai.provider.name")
#: Opt-In message content — never emitted unless capture_content=True
CONTENT_ATTRS: Tuple[str, ...] = ("gen_ai.input.messages",
                                  "gen_ai.output.messages")
#: local-only concepts live under this prefix, never under gen_ai.*
LOCAL_PREFIX = "hday."

_ATTR_VOCAB = frozenset(SEMCONV_ATTRS.values()) | frozenset(CONTENT_ATTRS) \
    | frozenset(PROVIDER_ALIASES)

#: typed score data types (NUMERIC | CATEGORICAL | BOOLEAN | TEXT)
SCORE_TYPES: Tuple[str, ...] = ("NUMERIC", "CATEGORICAL", "BOOLEAN", "TEXT")
TRAJ_VERDICTS: Tuple[str, ...] = ("pass", "fail", "unknown")
DEFAULT_PASS_BAR = 0.7

#: the named axes a trajectory score carries — a score is never one number
TRAJ_AXES: Tuple[str, ...] = (
    "traj.tool_call_validity", "traj.wasted_steps", "traj.retries",
    "traj.approval_waits", "traj.honesty_events",
)
APPROVAL_OPS: Tuple[str, ...] = ("approval.wait",)
HONESTY_OPS: Tuple[str, ...] = ("honesty.check", "guardrail.check")
_CALL_TYPES: Tuple[str, ...] = ("tool", "generation", "span")

NO_ROWS = "(no rows)"
_MAX_DEPTH = 32

#: additive sidecar ledger keyed by run id. Rows are the SAME shape the gate and
#: ctxscore ledgers use, so one view-model can render all three.
LEDGER: deque = deque(maxlen=2000)

#: ambient trace + current span, so nested work links without plumbing
_TRACE_CTX: contextvars.ContextVar[Optional["Trace"]] = \
    contextvars.ContextVar("hday_trace_ambient", default=None)
_CURRENT_SPAN: contextvars.ContextVar[Optional[str]] = \
    contextvars.ContextVar("hday_trace_current_span", default=None)


# ---------------------------------------------------------------------------
# naming — OTel GenAI span-name grammar
# ---------------------------------------------------------------------------


def span_name(operation: Any, subject: Any = None) -> str:
    """``{gen_ai.operation.name} {subject}`` — the published naming grammar.

    A missing/blank subject falls back to ``unknown`` (documented) so a name is
    never empty and never a bare operation.
    """
    op = str(operation or "").strip() or _DEFAULT_OPERATION
    subj = str(subject or "").strip() or FALLBACK_SUBJECT
    return f"{op} {subj}"


def is_valid_span_name(name: Any) -> bool:
    """True when *name* is ``{operation} {subject}`` with a known operation."""
    text = str(name or "")
    if not _NAME_RE.match(text):
        return False
    return text.split(" ", 1)[0] in OPERATIONS


def tool_span_call_id(sid: Any, run_id: Any, index: Any) -> str:
    """Stable ``gen_ai.tool.call.id`` — uuid5 over (session, run, index).

    Deterministic: re-scoring or re-exporting one run yields the same id.
    """
    try:
        idx = int(index)
    except Exception:
        idx = 0
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f"{sid}|{run_id}|{idx}"))


# ---------------------------------------------------------------------------
# attributes — the semconv field dictionary
# ---------------------------------------------------------------------------


def unmapped_attr_keys(attrs: Any) -> List[str]:
    """Keys that are neither a published semconv key nor a local ``hday.*`` one.

    The drift test: a lane adding an unmapped field fails here rather than
    silently shipping a bespoke schema.
    """
    out: List[str] = []
    if not isinstance(attrs, dict):
        return out
    for key in attrs:
        text = str(key)
        if text in _ATTR_VOCAB or text.startswith(LOCAL_PREFIX):
            continue
        out.append(text)
    return sorted(out)


def _as_int(value: Any) -> Optional[int]:
    """A token count as an int, or None. Never the string ``unknown``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 and value.is_integer() else None
    text = str(value).strip()
    return int(text) if text.isdigit() else None


def semconv_attrs(span: Any, *, capture_content: bool = False) -> Dict[str, Any]:
    """Map a span onto published OTel GenAI attribute keys.

    ``gen_ai.operation.name`` is always present; provider/model/tool/usage/error
    appear only when known (an unknown value is absent, never a fake string).
    Message content is Opt-In: absent unless *capture_content*.
    """
    out: Dict[str, Any] = {}
    get = lambda key, default=None: getattr(span, key, default)  # noqa: E731

    operation = str(get("operation_name") or "")
    if operation:
        out[SEMCONV_ATTRS["operation_name"]] = operation

    provider = str(get("provider") or "")
    if provider:
        out[SEMCONV_ATTRS["provider"]] = provider
    for local, semconv in (("request_model", "request_model"),
                           ("response_model", "response_model")):
        value = str(get(local) or "")
        if value:
            out[SEMCONV_ATTRS[semconv]] = value

    for local in ("input_tokens", "output_tokens"):
        value = _as_int(get(local))
        if value is not None:
            out[SEMCONV_ATTRS[local]] = value

    tool_name = str(get("tool_name") or "")
    if tool_name:
        out[SEMCONV_ATTRS["tool_name"]] = tool_name
    tool_type = str(get("tool_type") or "")
    if tool_name and tool_type:
        out[SEMCONV_ATTRS["tool_type"]] = tool_type
    call_id = str(get("tool_call_id") or "")
    if tool_name and call_id:
        out[SEMCONV_ATTRS["tool_call_id"]] = call_id

    error_type = str(get("error_type") or "")
    if error_type:
        out[SEMCONV_ATTRS["error_type"]] = error_type

    conversation = str(get("conversation_id") or "")
    if conversation:
        out[SEMCONV_ATTRS["conversation_id"]] = conversation

    for local in ("evaluation_name", "evaluation_label", "evaluation_value"):
        value = get(local)
        if value in (None, ""):
            continue
        out[SEMCONV_ATTRS[local]] = value

    if capture_content:
        for local, semconv in (("input_messages", "gen_ai.input.messages"),
                               ("output_messages", "gen_ai.output.messages")):
            value = get(local)
            if value is not None:
                out[semconv] = value

    # local-only fields keep their own prefix
    span_type = str(get("span_type") or "")
    if span_type:
        out[LOCAL_PREFIX + "span_type"] = span_type
    lane = str(get("lane") or "")
    if lane:
        out[LOCAL_PREFIX + "lane"] = lane
    run_id = str(get("run_id") or "")
    if run_id:
        out[LOCAL_PREFIX + "run_id"] = run_id
    extra = get("attrs")
    if isinstance(extra, dict):
        for key, value in extra.items():
            out[str(key)] = value
    return out


# ---------------------------------------------------------------------------
# the observation tree
# ---------------------------------------------------------------------------


@dataclass
class Span:
    """One node: a span, a generation, a tool call, a score or an event."""

    span_id: str
    name: str                       # span_name, e.g. "gate.decide gpt-4o"
    trace_id: str = ""
    parent_id: Optional[str] = None
    span_type: str = "span"
    operation_name: str = ""
    span_kind: str = "INTERNAL"
    status: str = "unset"
    started_at: float = 0.0
    finished_at: Optional[float] = None
    lane: str = "local"
    cost: float = 0.0
    sid: str = ""
    run_id: str = ""
    provider: str = ""
    request_model: str = ""
    response_model: str = ""
    input_tokens: Optional[Any] = None
    output_tokens: Optional[Any] = None
    tool_name: str = ""
    tool_call_id: str = ""
    tool_type: str = "function"
    error_type: str = ""
    conversation_id: str = ""
    evaluation_name: str = ""
    evaluation_label: str = ""
    evaluation_value: Optional[Any] = None
    input_messages: Optional[Any] = None
    output_messages: Optional[Any] = None
    attrs: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> Optional[float]:
        if self.finished_at is None:
            return None
        return max(0.0, (float(self.finished_at) - float(self.started_at)) * 1000.0)

    def finish(self, status: str = "ok") -> "Span":
        self.finished_at = time.time()
        self.status = status if status in STATUSES else "unset"
        return self

    def as_row(self) -> Dict[str, Any]:
        """The existing ledger row shape + the additive span sidecar."""
        return {
            # --- shape _gate_log / ctxscore already write ---
            "kind": self.span_type,
            "lane": self.lane if self.lane in LANES else "local",
            "ms": float(self.duration_ms or 0.0),
            "cost": float(self.cost or 0.0),
            "ts": float(self.started_at or 0.0),
            "sid": self.sid,
            "tool": self.tool_name,
            "allow": self.attrs.get(LOCAL_PREFIX + "allow"),
            "reason": str(self.attrs.get(LOCAL_PREFIX + "reason") or ""),
            # --- additive span sidecar, keyed by run id ---
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "span_name": self.name,
            "operation_name": self.operation_name,
            "span_type": self.span_type,
            "span_kind": self.span_kind,
            "status": self.status,
            "run_id": self.run_id,
            "attrs": semconv_attrs(self),
        }


@dataclass
class Trace:
    """A run: one root observation plus its spans, linked by parent_id."""

    trace_id: str
    name: str = "trace"
    run_id: str = ""
    sid: str = ""
    started_at: float = 0.0
    finished_at: Optional[float] = None
    spans: List[Span] = field(default_factory=list)
    _seq: int = 0
    _token: Any = None

    # -- linkage ----------------------------------------------------------

    @property
    def root_id(self) -> str:
        return self.trace_id

    def add(self, node: Span) -> Span:
        self.spans.append(node)
        return node

    def open(self, operation: Any, subject: Any = None, *,
             span_type: str = "span", kind: Optional[str] = None,
             attrs: Optional[Dict[str, Any]] = None, lane: str = "local",
             cost: float = 0.0, **fields: Any) -> Span:
        """Create a child of the ambient span (or of the trace root)."""
        self._seq += 1
        parent = _CURRENT_SPAN.get() or self.trace_id
        node = Span(
            span_id=f"span_{uuid.uuid4().hex[:16]}",
            name=span_name(operation, subject),
            trace_id=self.trace_id,
            parent_id=parent,
            span_type=span_type if span_type in SPAN_TYPES else "span",
            operation_name=str(operation or "").strip() or _DEFAULT_OPERATION,
            span_kind=kind or ("CLIENT" if span_type == "generation"
                               else "INTERNAL"),
            started_at=time.time(),
            lane=lane,
            cost=float(cost or 0.0),
            sid=self.sid,
            run_id=self.run_id,
            conversation_id=self.sid,
            attrs=dict(attrs or {}),
        )
        for key, value in fields.items():
            if key in Span.__dataclass_fields__:
                setattr(node, key, value)
            else:
                node.attrs[str(key)] = value
        return self.add(node)

    def children(self, parent_id: Optional[str]) -> List[Span]:
        out = [s for s in self.spans if s.parent_id == parent_id]
        return sorted(out, key=lambda s: (s.started_at, self.spans.index(s)))

    # -- roll-ups ---------------------------------------------------------

    @property
    def duration_ms(self) -> Optional[float]:
        if self.finished_at is None:
            return None
        return max(0.0, (float(self.finished_at) - float(self.started_at)) * 1000.0)

    @property
    def status(self) -> str:
        if not self.spans:
            return "unset"
        return "error" if any(s.status == "error" for s in self.spans) else "ok"

    def as_rows(self) -> List[Dict[str, Any]]:
        """Root row first, then every span in creation order."""
        root = Span(span_id=self.trace_id, name=span_name("trace", self.name),
                    trace_id=self.trace_id, parent_id=None, span_type="trace",
                    operation_name="trace", span_kind="INTERNAL",
                    status=self.status, started_at=self.started_at,
                    finished_at=self.finished_at, lane="local", cost=0.0,
                    sid=self.sid, run_id=self.run_id,
                    conversation_id=self.sid)
        return [root.as_row()] + [s.as_row() for s in self.spans]

    def emit(self, sink: Optional[Callable[[dict], None]] = None) -> List[dict]:
        return emit(self.as_rows(), sink=sink)

    # -- ambient context --------------------------------------------------

    def __enter__(self) -> "Trace":
        self._token = _TRACE_CTX.set(self)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.finished_at = time.time()
        token, self._token = self._token, None
        if token is not None:
            try:
                _TRACE_CTX.reset(token)
            except Exception:
                pass
        return False


def trace(name: Any = "turn", *, run_id: Any = None, sid: Any = "",
          trace_id: Optional[str] = None) -> Trace:
    """Start a run. Use as a context manager so nested spans link themselves."""
    return Trace(trace_id=str(trace_id or f"trace_{uuid.uuid4().hex}"),
                 name=str(name or "turn"), run_id=str(run_id or ""),
                 sid=str(sid or ""), started_at=time.time())


@contextmanager
def span(operation: Any, subject: Any = None, *,
         trace: Optional[Trace] = None, span_type: str = "span",
         kind: Optional[str] = None, attrs: Optional[Dict[str, Any]] = None,
         lane: str = "local", cost: float = 0.0, **fields: Any):
    """Open a child observation under the ambient span (or the trace root).

    Raises ``RuntimeError`` when there is no trace to attach to: a detached span
    is a bug, not telemetry.
    """
    tr = trace if trace is not None else _TRACE_CTX.get()
    if tr is None:
        raise RuntimeError(
            "hday_trace.span() needs an ambient trace: wrap the work in "
            "`with hday_trace.trace(...)` or pass trace=")
    node = tr.open(operation, subject, span_type=span_type, kind=kind,
                   attrs=attrs, lane=lane, cost=cost, **fields)
    marker = _CURRENT_SPAN.set(node.span_id)
    try:
        yield node
    except BaseException as exc:
        node.error_type = type(exc).__name__
        node.finish("error")
        raise
    else:
        node.finish("ok")
    finally:
        try:
            _CURRENT_SPAN.reset(marker)
        except Exception:
            pass


@contextmanager
def generation(operation: Any = "chat", model: Any = None, *,
               trace: Optional[Trace] = None, provider: str = "",
               response_model: str = "", input_tokens: Any = None,
               output_tokens: Any = None, cost: float = 0.0,
               attrs: Optional[Dict[str, Any]] = None, lane: str = "local",
               input_messages: Any = None, output_messages: Any = None,
               **fields: Any):
    """An inference call: CLIENT span, model in the name, usage in attributes."""
    with span(operation, model, trace=trace, span_type="generation",
              kind="CLIENT", attrs=attrs, lane=lane, cost=cost,
              provider=provider, response_model=response_model,
              request_model=str(model or ""),
              input_tokens=input_tokens, output_tokens=output_tokens,
              input_messages=input_messages,
              output_messages=output_messages, **fields) as node:
        yield node


@contextmanager
def tool_span(tool_name: Any, *, trace: Optional[Trace] = None,
              call_id: Optional[str] = None, tool_type: str = "function",
              attrs: Optional[Dict[str, Any]] = None, cost: float = 0.0,
              lane: str = "local", **fields: Any):
    """A model-issued tool call: INTERNAL span named ``execute_tool {tool}``."""
    tr = trace if trace is not None else _TRACE_CTX.get()
    if tr is None:
        raise RuntimeError(
            "hday_trace.tool_span() needs an ambient trace: wrap the work in "
            "`with hday_trace.trace(...)` or pass trace=")
    if not call_id:
        call_id = tool_span_call_id(tr.sid, tr.run_id, tr._seq)
    with span("execute_tool", tool_name, trace=tr, span_type="tool",
              kind="INTERNAL", attrs=attrs, lane=lane, cost=cost,
              tool_name=str(tool_name or ""), tool_call_id=str(call_id),
              tool_type=tool_type, **fields) as node:
        yield node


# ---------------------------------------------------------------------------
# the additive sidecar ledger
# ---------------------------------------------------------------------------


def reset_ledger() -> None:
    """Clear the sidecar ledger (tests / session rollover)."""
    LEDGER.clear()


def emit(rows: Iterable[Any],
         sink: Optional[Callable[[dict], None]] = None) -> List[dict]:
    """Record rows in the sidecar ledger and an optional injected sink.

    Never raises: a broken sink is logged and the rows still exist in the
    result. Non-dict input is skipped rather than guessed at.
    """
    out: List[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        rec = dict(row)
        out.append(rec)
        try:
            LEDGER.append(rec)
        except Exception:
            log.warning("hday_trace: ledger append failed", exc_info=True)
        if sink is not None:
            try:
                sink(rec)
            except Exception:
                log.warning("hday_trace: sink failed", exc_info=True)
    return out


#: internal alias — ``score_run`` has an ``emit`` parameter that would shadow
#: the ledger writer inside its own body.
_emit_ledger = emit


def ledger_rows(run_id: Optional[str] = None,
                trace_id: Optional[str] = None) -> List[dict]:
    """The sidecar rows, optionally narrowed to one run or one trace."""
    rows = [dict(r) for r in LEDGER]
    if run_id is not None:
        rows = [r for r in rows if str(r.get("run_id") or "") == str(run_id)]
    if trace_id is not None:
        rows = [r for r in rows if str(r.get("trace_id") or "") == str(trace_id)]
    return rows


def group_by_trace(rows: Iterable[Any]) -> Dict[str, List[dict]]:
    """Group ledger rows by trace id, insertion-ordered."""
    groups: Dict[str, List[dict]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        groups.setdefault(str(row.get("trace_id") or ""), []).append(row)
    return groups


# ---------------------------------------------------------------------------
# trajectory scoring — typed axes, not one opaque number
# ---------------------------------------------------------------------------


@dataclass
class ScoreRow:
    """One typed score: value + dataType + the reasoning behind it."""

    name: str
    value: Any = None
    data_type: str = "NUMERIC"
    comment: str = ""
    observation_id: str = ""
    source: str = "code"
    status: str = "scored"          # scored | unjudged
    trace_id: str = ""
    run_id: str = ""
    sid: str = ""
    lane: str = "local"
    ts: float = 0.0

    def as_row(self) -> Dict[str, Any]:
        """A ledger row: the existing shape + the typed score fields."""
        return {
            "kind": "score",
            "lane": self.lane if self.lane in LANES else "local",
            "ms": 0.0,
            "cost": 0.0,
            "ts": float(self.ts or time.time()),
            "sid": self.sid,
            "name": self.name,
            "value": self.value,
            "dataType": self.data_type if self.data_type in SCORE_TYPES
                     else "TEXT",
            "comment": self.comment,
            "observation_id": self.observation_id or self.trace_id,
            "source": self.source,
            "status": self.status,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "span_type": "score",
            "span_name": span_name("score", self.name),
            "attrs": {LOCAL_PREFIX + "score_status": self.status,
                      LOCAL_PREFIX + "data_type": self.data_type},
        }


@dataclass
class TrajectoryScore:
    """A completed run graded into named, typed axes."""

    trace_id: str = ""
    run_id: str = ""
    sid: str = ""
    verdict: str = "unknown"
    scores: List[ScoreRow] = field(default_factory=list)
    spans: int = 0
    pass_bar: float = DEFAULT_PASS_BAR
    source: str = "code"
    ts: float = 0.0

    def get(self, name: str) -> Optional[ScoreRow]:
        for row in self.scores:
            if row.name == name:
                return row
        return None

    @property
    def components(self) -> Dict[str, Dict[str, Any]]:
        """The named axes behind the verdict (never a single opaque number)."""
        out: Dict[str, Dict[str, Any]] = {}
        for name in TRAJ_AXES:
            row = self.get(name)
            if row is None:
                continue
            out[name] = {"value": row.value, "dataType": row.data_type,
                         "comment": row.comment, "status": row.status}
        return out

    def reason(self) -> str:
        """Why this verdict, in one line naming the axes that decided it."""
        axes = " ".join(f"{name.split('.', 1)[-1]}={self.get(name).value}"
                        for name in TRAJ_AXES if self.get(name) is not None)
        if self.verdict == "unknown":
            return f"unknown: no scorable calls in the run ({axes})"
        if self.verdict == "fail":
            why: List[str] = []
            honesty = self.get("traj.honesty_events")
            wasted = self.get("traj.wasted_steps")
            validity = self.get("traj.tool_call_validity")
            if honesty is not None and honesty.value:
                why.append(f"{honesty.value} honesty event(s)")
            if validity is not None and validity.value is not None \
                    and validity.value < self.pass_bar:
                why.append(f"tool-call validity {validity.value:.2f} < "
                           f"{self.pass_bar}")
            if wasted is not None and wasted.value:
                why.append(f"{wasted.value} wasted step(s)")
            return "fail: " + ("; ".join(why) or "unexplained") + f" ({axes})"
        return f"pass ({axes})"

    def as_row(self) -> Dict[str, Any]:
        """The small typed score row for the whole run."""
        return {
            "kind": "score",
            "lane": "local",
            "ms": 0.0,
            "cost": 0.0,
            "ts": float(self.ts or time.time()),
            "sid": self.sid,
            "name": "traj.run",
            "value": self.verdict,
            "dataType": "CATEGORICAL",
            "comment": self.reason(),
            "observation_id": self.trace_id,
            "source": self.source,
            "status": "unjudged" if self.verdict == "unknown" else "scored",
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "span_type": "score",
            "span_name": span_name("score", "traj.run"),
            "verdict": self.verdict,
            "spans": self.spans,
            "components": self.components,
            "attrs": {LOCAL_PREFIX + "verdict": self.verdict},
        }

    def as_rows(self) -> List[Dict[str, Any]]:
        """One ledger row per score, plus the run-level row last."""
        return [s.as_row() for s in self.scores] + [self.as_row()]


def _coerce_rows(rows: Any) -> List[dict]:
    if isinstance(rows, Trace):
        return rows.as_rows()
    if isinstance(rows, dict):
        return [rows]
    if isinstance(rows, (list, tuple)):
        return [r for r in rows if isinstance(r, dict)]
    return []


def _row_status(row: dict) -> str:
    return str(row.get("status") or row.get("kind") or "")


def _row_ok(row: dict) -> bool:
    attrs = row.get("attrs")
    if isinstance(attrs, dict) and attrs.get("error.type"):
        return False
    return _row_status(row) == "ok"


def _flag(row: dict, key: str) -> bool:
    if bool(row.get(key)):
        return True
    attrs = row.get("attrs")
    return bool(isinstance(attrs, dict) and attrs.get(key))


def _is_call(row: dict) -> bool:
    kind = str(row.get("span_type") or row.get("kind") or "")
    operation = str(row.get("operation_name") or "")
    return kind in _CALL_TYPES and operation not in APPROVAL_OPS + HONESTY_OPS


def score_run(rows: Any, *, pass_bar: float = DEFAULT_PASS_BAR,
              judge: Optional[Callable[[List[dict]], Any]] = None,
              emit: bool = False,
              sink: Optional[Callable[[dict], None]] = None,
              source: str = "code") -> TrajectoryScore:
    """Grade a completed run into typed axes + a verdict.

    Deterministic axes (all read from the rows, no model call):
    ``traj.tool_call_validity`` (NUMERIC 0-1), ``traj.wasted_steps``,
    ``traj.retries``, ``traj.approval_waits`` (+ ``traj.approval_wait_ms``) and
    ``traj.honesty_events``. A verdict of ``unknown`` means the run had nothing
    scorable — never a silent zero. An optional *judge* must return reasoning
    alongside its score; a judge that does not is recorded ``unjudged``, not
    scored, and can never raise into the caller.
    """
    try:
        data = _coerce_rows(rows)
        trace_id = next((str(r.get("trace_id") or "") for r in data
                         if r.get("trace_id")), "")
        run_id = next((str(r.get("run_id") or "") for r in data
                       if r.get("run_id")), "")
        sid = next((str(r.get("sid") or "") for r in data if r.get("sid")), "")

        calls = [r for r in data if _is_call(r)]
        good = [r for r in calls if _row_ok(r)]
        validity = (len(good) / len(calls)) if calls else None

        wasted = 0
        retries = 0
        seen: Optional[str] = None
        for row in calls:
            if not _row_ok(row) or _flag(row, LOCAL_PREFIX + "wasted"):
                wasted += 1
            name = str(row.get("span_name") or row.get("operation_name") or "")
            if name and name == seen:
                retries += 1
            seen = name or seen
        retries += sum(1 for r in calls if _flag(r, LOCAL_PREFIX + "retry"))

        approvals = [r for r in data
                     if str(r.get("operation_name") or "") in APPROVAL_OPS
                     or str(r.get("span_type") or "") == "approval"]
        approval_ms = round(sum(float(r.get("ms") or 0.0)
                                for r in approvals), 3)

        honesty = [r for r in data
                   if (str(r.get("operation_name") or "") in HONESTY_OPS
                       and _row_status(r) == "error")
                   or _flag(r, LOCAL_PREFIX + "honesty")]

        if validity is None and not honesty:
            verdict = "unknown"
        elif honesty:
            verdict = "fail"
        elif validity is not None and validity < pass_bar:
            verdict = "fail"
        elif wasted:
            verdict = "fail"
        else:
            verdict = "pass"

        ts = time.time()

        def _axis(name: str, value: Any, data_type: str, comment: str,
                  status: str = "scored") -> ScoreRow:
            return ScoreRow(name=name, value=value, data_type=data_type,
                            comment=comment, observation_id=trace_id,
                            source=source, status=status, trace_id=trace_id,
                            run_id=run_id, sid=sid, ts=ts)

        scores = [
            _axis("traj.tool_call_validity",
                  round(validity, 4) if validity is not None else None,
                  "NUMERIC",
                  f"{len(good)}/{len(calls)} calls returned ok" if calls
                  else "no scorable calls in this run",
                  "scored" if validity is not None else "unjudged"),
            _axis("traj.wasted_steps", wasted, "NUMERIC",
                  f"{wasted} step(s) errored or were flagged wasted"),
            _axis("traj.retries", retries, "NUMERIC",
                  f"{retries} repeated call(s) in the run"),
            _axis("traj.approval_waits", len(approvals), "NUMERIC",
                  f"{len(approvals)} approval wait(s), {approval_ms:.0f}ms total"),
            _axis("traj.honesty_events", len(honesty), "NUMERIC",
                  f"{len(honesty)} honesty event(s) recorded by the guard"),
            _axis("traj.approval_wait_ms", approval_ms, "NUMERIC",
                  "total ms spent waiting on approvals"),
        ]

        score = TrajectoryScore(trace_id=trace_id, run_id=run_id, sid=sid,
                                verdict=verdict, scores=scores,
                                spans=len(data), pass_bar=pass_bar,
                                source=source, ts=ts)
        score.scores.append(_axis("traj.verdict", verdict, "CATEGORICAL",
                                  score.reason()))

        if judge is not None:
            row = _axis("traj.judge", None, "NUMERIC", "")
            try:
                answer = judge(data)
                value = answer.get("score") if isinstance(answer, dict) else None
                reasoning = (str(answer.get("reasoning") or "")
                             if isinstance(answer, dict) else "")
                numeric = (value if isinstance(value, (int, float))
                           and not isinstance(value, bool) else None)
                if not reasoning:
                    row.status = "unjudged"
                    row.comment = ("judge returned no reasoning — recorded as "
                                   "unjudged, not scored")
                elif numeric is None:
                    row.status = "unjudged"
                    row.comment = (f"judge returned no numeric score "
                                   f"({reasoning})")
                else:
                    row.status = "scored"
                    row.value = float(numeric)
                    row.comment = reasoning
            except Exception as exc:
                row.status = "unjudged"
                row.comment = f"judge raised {type(exc).__name__}: {exc}"
                log.warning("hday_trace: judge failed", exc_info=True)
            score.scores.append(row)

        if emit:
            _emit_ledger(score.as_rows(), sink=sink)
        return score
    except Exception as exc:                      # never raise into a caller
        log.warning("hday_trace: score_run failed", exc_info=True)
        return TrajectoryScore(verdict="unknown", source=source,
                               scores=[ScoreRow(
                                   "traj.run", None, "CATEGORICAL",
                                   f"score_run failed: {type(exc).__name__}: "
                                   f"{exc}", status="unjudged")])


# ---------------------------------------------------------------------------
# compact text renderer for the cockpit/TUI
# ---------------------------------------------------------------------------


def _num(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return -1.0
    try:
        return float(value)
    except Exception:
        return -1.0


def _label(row: dict) -> str:
    return str(row.get("span_name") or row.get("operation_name") or "unknown")


def _status_text(row: dict) -> str:
    return str(row.get("status") or "unknown")


def _dur_text(row: dict) -> str:
    ms = row.get("ms")
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return "?"
    return f"{float(ms):.1f}ms"


def _synthetic_root(data: Sequence[dict]) -> dict:
    """A root line for a run whose own trace row was not emitted."""
    trace_id = next((str(r.get("trace_id") or "") for r in data
                     if r.get("trace_id")), "")
    label = (trace_id[len("trace_"):] if trace_id.startswith("trace_")
             else trace_id)
    return {"kind": "trace", "span_type": "trace",
            "span_id": trace_id or "trace_unknown",
            "parent_id": None, "trace_id": trace_id or "trace_unknown",
            "span_name": span_name("trace", label or "unknown"),
            "status": "unset", "ms": None, "ts": -1.0, "lane": "local",
            "cost": 0.0, "sid": "", "attrs": {}}


def render_tree(rows: Any, *, max_rows: int = 500,
                show_attrs: bool = False) -> str:
    """One run's tree as compact text for the cockpit/TUI.

    Deterministic (children ordered by ts, then row order), honest about
    unknowns (``?`` duration, ``unknown`` status) and bounded: a dangling parent
    is re-attached to the root and marked ``?``, a parent cycle is broken and
    marked ``!``, and the walk is depth-capped.
    """
    if isinstance(rows, Trace):
        rows = rows.as_rows()
    if not isinstance(rows, (list, tuple)):
        return NO_ROWS
    raw = [r for r in rows if isinstance(r, dict)]
    if not raw:
        return NO_ROWS

    limit = max(1, int(max_rows))
    data = raw[:limit]
    roots = [r for r in raw
             if str(r.get("kind") or r.get("span_type") or "") == "trace"]
    if roots and roots[0] not in data:
        data = [roots[0]] + data[:limit - 1]

    synthetic = not roots
    if synthetic:
        data = [_synthetic_root(data)] + data
    root = data[0]
    root_id = str(root.get("span_id") or "")

    nodes = {str(r.get("span_id") or ""): r for r in data}
    decided: Dict[str, str] = {}
    marks: Dict[str, str] = {}
    for row in data:
        sid = str(row.get("span_id") or "")
        if row is root or sid == root_id:
            continue
        parent_id = str(row.get("parent_id") or "")
        if not parent_id or parent_id == sid or parent_id not in nodes:
            marks[sid] = "?"            # dangling parent: say so, do not drop
            decided[sid] = root_id
        else:
            decided[sid] = parent_id

    children: Dict[str, List[Tuple[int, dict]]] = {}
    for index, row in enumerate(data):
        sid = str(row.get("span_id") or "")
        if sid in decided:
            children.setdefault(decided[sid], []).append((index, row))

    reachable = {root_id}
    frontier = [root_id]
    while frontier:
        for _, child in list(children.get(frontier.pop(), [])):
            cid = str(child.get("span_id") or "")
            if cid and cid not in reachable:
                reachable.add(cid)
                frontier.append(cid)

    for index, row in enumerate(data):
        sid = str(row.get("span_id") or "")
        if not sid or sid in reachable or sid not in decided:
            continue
        old = decided[sid]                       # inside a cycle: break the link
        children[old] = [t for t in children.get(old, [])
                         if str(t[1].get("span_id") or "") != sid]
        decided[sid] = root_id
        marks[sid] = "!"
        children.setdefault(root_id, []).append((index, row))

    count = len(data) - (1 if synthetic else 0)
    lines = [f"{_label(root)}  {root_id}  {_status_text(root)}  "
             f"{_dur_text(root)}  spans={count}"]

    def _walk(parent_id: str, prefix: str, depth: int,
              path: frozenset) -> None:
        kids = sorted(children.get(parent_id, []),
                      key=lambda t: (_num(t[1].get("ts")), t[0]))
        for position, (_, child) in enumerate(kids):
            cid = str(child.get("span_id") or "")
            last = position == len(kids) - 1
            glyph = "└─" if last else "├─"
            mark = marks.get(cid, "")
            label = f"{mark} {_label(child)}" if mark else _label(child)
            suffix = ""
            if show_attrs:
                attrs = child.get("attrs")
                if isinstance(attrs, dict) and attrs:
                    suffix = "  " + " ".join(
                        f"{name}={attrs[name]}" for name in sorted(attrs))
            lines.append(f"{prefix}{glyph} {label}  {_status_text(child)}  "
                         f"{_dur_text(child)}{suffix}")
            if cid in path or depth + 1 >= _MAX_DEPTH:
                continue                          # cycle / depth guard
            _walk(cid, prefix + ("   " if last else "│  "), depth + 1,
                  path | {cid})

    _walk(root_id, "", 0, frozenset({root_id}))
    return "\n".join(lines)




