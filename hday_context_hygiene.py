"""hday_context_hygiene — restorable context reduction for long agent sessions.

Four mechanisms, all pure/local (stdlib only, no network, no import-time I/O):

**1. Externalize, don't delete.**  Every reduction replaces a payload with a
typed pointer ``{kind, ref, sha, bytes}`` and never drops the pointer, so the
payload can be pulled back with :func:`rehydrate`.  Payloads that must never be
compressed — gate vetoes, failed checks, error traces, decisions — are *pinned*
in code (:data:`PINNED_KINDS`), not by convention: :func:`externalize` raises
:class:`Pinned` for them.

**2. Bi-temporal invalidation.**  :func:`remember` stores a row with both
clocks: ``valid_from`` / ``valid_to`` (when the fact was true) and
``created_at`` / ``expired_at`` (when the system recorded / superseded it).
:func:`supersede` invalidates the old row — it is never deleted or rewritten —
so :func:`memory_as_of` can still answer "what did we believe then".

**3. Tool-result clearing.**  :func:`clear_tool_results` replaces large,
re-fetchable tool results *older than the last N* with a compact stub carrying
the tool name, its arguments and a retrievable pointer.  Assistant text,
decisions and gate events are never eligible; a token ``trigger`` and a
``clear_at_least`` floor keep it from firing when there is nothing to win.

**4. Prefix hygiene + retry budgets.**  :func:`check_prefix_stability` reports
volatile content at the top of the prompt (per-turn timestamps, counters, pids)
and undeclared prefix changes as typed :class:`Finding` rows, because a moving
prefix destroys provider KV-cache reuse; :func:`tool_mask` expresses tool
availability as a selection-time mask so the tool block is never rewritten.
:class:`RetryBudget` keeps per-tool counters that reset on success and
multiplies retries across nested layers (N x M x K), refusing configurations
above a declared ceiling before a single request is issued.

Nothing in this module raises out of the hot path: every entry point catches,
marks the failure in :data:`LEDGER` and keeps the data.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, List, Mapping, Optional, Sequence

__all__ = [
    "ARCHIVE",
    "BudgetRefused",
    "ClearResult",
    "Finding",
    "LEDGER",
    "PINNED_KINDS",
    "Pinned",
    "Pointer",
    "PrefixReport",
    "RECORDS",
    "Record",
    "RetryBudget",
    "RetryExhausted",
    "RunEstimate",
    "assert_within_ceiling",
    "check_prefix_stability",
    "clear_tool_results",
    "digest",
    "estimate_run",
    "estimate_tokens",
    "externalize",
    "is_pinned",
    "memory_as_of",
    "prefix_hash",
    "recall",
    "record",
    "rehydrate",
    "rehydration_stats",
    "remember",
    "reset_archive",
    "reset_ledger",
    "reset_records",
    "supersede",
    "tool_mask",
    "worst_case_requests",
    "worst_case_seconds",
]

#: payload classes that are never eligible for clearing or summarization.
#: A gate veto, a failed check or an error trace is the evidence the model
#: needs to update its beliefs; erasing it removes the signal.
PINNED_KINDS = ("gate_veto", "failed_check", "error_trace", "decision")

#: pointer kinds (spec: ``{kind: file|url|tool_result|episode}``)
POINTER_KINDS = ("file", "url", "tool_result", "episode")

#: the three nested retry multipliers from the provider docs
DEFAULT_TRIGGER_TOKENS = 100_000   # tool-result clearing fires above this
DEFAULT_KEEP_TOOL_USES = 3         # ... keeping the most recent N results

_log = logging.getLogger("hermes_day.context_hygiene")

#: bounded audit ledger; every row is ``{"event": ...}`` plus its own fields
LEDGER: deque = deque(maxlen=1000)

#: the externalized-payload archive, keyed by ``ref``.  In production the
#: integrator injects a disk-backed ``archive=`` callable; the in-memory map is
#: what makes the module testable and standalone.
ARCHIVE: "OrderedDict[str, Any]" = OrderedDict()

#: bi-temporal rows, keyed by id, in insertion order
RECORDS: "OrderedDict[str, Record]" = OrderedDict()


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class Pinned(Exception):
    """Raised when a pinned payload class is handed to :func:`externalize`."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(
            f"{kind!r} is pinned: gate vetoes, failed checks, error traces and "
            "decisions are never eligible for clearing or summarization"
        )


class RetryExhausted(Exception):
    """Typed exhaustion of a per-tool (or output) retry budget."""

    def __init__(self, tool: str, budget: int, attempts: int) -> None:
        self.tool = tool
        self.budget = budget
        self.attempts = attempts
        super().__init__(
            f"retry budget exhausted for {tool!r}: {attempts} attempts > budget {budget}"
        )


class BudgetRefused(Exception):
    """A run configuration above the declared worst-case ceiling."""

    def __init__(self, message: str, *, estimate: "RunEstimate") -> None:
        self.estimate = estimate
        super().__init__(message)


# ---------------------------------------------------------------------------
# bookkeeping
# ---------------------------------------------------------------------------


def reset_ledger() -> None:
    """Clear the audit ledger (tests / session rollover)."""
    LEDGER.clear()


def reset_archive() -> None:
    """Clear the in-memory pointer archive (tests / session rollover)."""
    ARCHIVE.clear()


def reset_records() -> None:
    """Clear the bi-temporal store (tests / session rollover)."""
    RECORDS.clear()


def _emit(rows: Sequence[dict], sink: Optional[Callable[[dict], None]] = None) -> None:
    """Record rows in the module ledger and an optional injected sink.

    Never raises: a broken sink is logged, the rows still exist in the result.
    """
    for row in rows:
        try:
            LEDGER.append(row)
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("context-hygiene ledger append failed: %s", exc)
        if callable(sink):
            try:
                sink(row)
            except Exception as exc:
                _log.warning("context-hygiene ledger sink failed: %s", exc)


def _now(at: Optional[float]) -> float:
    return time.time() if at is None else float(at)


def digest(payload: Any) -> str:
    """Stable content digest of a payload (``str``/``bytes``/anything)."""
    if isinstance(payload, bytes):
        raw = payload
    elif isinstance(payload, str):
        raw = payload.encode("utf-8", "replace")
    else:
        try:
            raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8", "replace")
        except Exception:
            raw = repr(payload).encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# 1. externalize, don't delete
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Pointer:
    """A durable, restorable locator for a payload that left the prompt.

    ``ref`` is the locator the agent re-fetches with (a path, a URL, a tool
    result id, an episode id); ``sha`` is the digest of what was there, so a
    mismatch on rehydration is detectable rather than silent.
    """

    kind: str
    ref: str
    sha: str = ""
    bytes: int = 0
    pinned: bool = False

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "sha": self.sha,
            "bytes": self.bytes,
            "pinned": self.pinned,
        }

    def line(self) -> str:
        """The one-line stub that replaces the payload in context."""
        short = self.sha[:12] if self.sha else "-"
        return f"[externalized {self.kind}: {self.ref} sha={short} bytes={self.bytes}]"


def _coerce_pointer(pointer: Any) -> Optional[Pointer]:
    if isinstance(pointer, Pointer):
        return pointer
    if isinstance(pointer, Mapping):
        kind = pointer.get("kind")
        ref = pointer.get("ref")
        if isinstance(kind, str) and isinstance(ref, str):
            return Pointer(
                kind=kind,
                ref=ref,
                sha=str(pointer.get("sha") or ""),
                bytes=int(pointer.get("bytes") or 0),
                pinned=bool(pointer.get("pinned", False)),
            )
    return None


def is_pinned(item: Any) -> bool:
    """True when this item must never be cleared, summarized or externalized.

    Accepts a kind string or an item mapping; an explicit ``pinned: True``
    flag pins anything (the integrator's escape hatch for custom classes).
    """
    if isinstance(item, str):
        return item in PINNED_KINDS
    if isinstance(item, Mapping):
        if item.get("pinned") is True:
            return True
        kind = item.get("kind")
        if isinstance(kind, str) and kind in PINNED_KINDS:
            return True
        if item.get("failed") is True or item.get("error") is True:
            return True
    return False


def externalize(
    payload: Any,
    *,
    kind: str,
    ref: str,
    archive: Optional[Callable[[str, Any], None]] = None,
    at: Optional[float] = None,
) -> Pointer:
    """Replace ``payload`` with a durable pointer; archive the payload itself.

    Raises :class:`Pinned` for :data:`PINNED_KINDS` — the rule is enforced in
    code so no caller can quietly compress away the failure trail.
    """
    if is_pinned(kind):
        raise Pinned(kind)
    if not isinstance(ref, str) or not ref:
        raise ValueError("externalize() needs a non-empty string ref")

    sha = digest(payload)
    size = len(payload.encode("utf-8", "replace")) if isinstance(payload, str) else _size_of(payload)
    ptr = Pointer(kind=str(kind), ref=ref, sha=sha, bytes=size)

    try:
        ARCHIVE[ref] = payload
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("context-hygiene archive write failed for %s: %s", ref, exc)
    if callable(archive):
        try:
            archive(ref, payload)
        except Exception as exc:
            _log.warning("context-hygiene archive sink failed for %s: %s", ref, exc)

    _emit([{
        "event": "externalize", "kind": ptr.kind, "ref": ptr.ref,
        "sha": ptr.sha, "bytes": ptr.bytes, "at": _now(at),
    }])
    return ptr


def _size_of(payload: Any) -> int:
    try:
        return len(json.dumps(payload, sort_keys=True, default=str).encode("utf-8", "replace"))
    except Exception:
        return len(repr(payload).encode("utf-8", "replace"))


def rehydrate(
    pointer: Any,
    *,
    archive: Optional[Callable[[str], Any]] = None,
) -> Any:
    """Pull an externalized payload back. Returns ``None`` on an honest miss.

    Every attempt (hit or miss) lands in the ledger: a high miss rate on one
    pointer kind is evidence the compressor dropped something it should have
    kept.
    """
    ptr = _coerce_pointer(pointer)
    if ptr is None:
        _emit([{"event": "rehydrate", "kind": "", "ref": "", "hit": False, "reason": "bad-pointer"}])
        return None

    payload: Any = None
    hit = False
    if ptr.ref in ARCHIVE:
        payload, hit = ARCHIVE[ptr.ref], True
    elif callable(archive):
        try:
            payload = archive(ptr.ref)
            hit = payload is not None
        except Exception as exc:
            _log.warning("context-hygiene rehydrate sink failed for %s: %s", ptr.ref, exc)

    reason = ""
    if hit and ptr.sha and digest(payload) != ptr.sha:
        reason = "sha-mismatch"
    _emit([{
        "event": "rehydrate", "kind": ptr.kind, "ref": ptr.ref,
        "hit": bool(hit), "reason": reason,
    }])
    return payload if hit else None


def rehydration_stats() -> dict:
    """Rehydration hits per pointer kind (the compressor's regret signal)."""
    stats: dict = {}
    for row in LEDGER:
        if row.get("event") == "rehydrate" and row.get("hit"):
            stats[row.get("kind", "")] = stats.get(row.get("kind", ""), 0) + 1
    return stats


# ---------------------------------------------------------------------------
# 2. bi-temporal invalidation
# ---------------------------------------------------------------------------


@dataclass
class Record:
    """A belief with both clocks, per the bi-temporal model.

    ``valid_from``/``valid_to`` are event time (when the fact held in the
    world); ``created_at``/``expired_at`` are ingestion time (when the system
    recorded it and when it learned better).
    """

    id: str
    payload: Any
    valid_from: float
    created_at: float
    valid_to: Optional[float] = None
    expired_at: Optional[float] = None
    superseded_by: Optional[str] = None
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "payload": self.payload,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "created_at": self.created_at,
            "expired_at": self.expired_at,
            "superseded_by": self.superseded_by,
            "reason": self.reason,
            "expired": self.expired_at is not None,
        }


def remember(
    payload: Any,
    *,
    id: Optional[str] = None,
    at: Optional[float] = None,
    valid_from: Optional[float] = None,
) -> Record:
    """Record a belief. ``at`` sets both clocks unless ``valid_from`` overrides."""
    stamp = _now(at)
    rec = Record(
        id=id or f"r{len(RECORDS) + 1}",
        payload=payload,
        valid_from=stamp if valid_from is None else float(valid_from),
        created_at=stamp,
    )
    RECORDS[rec.id] = rec
    _emit([{"event": "remember", "id": rec.id, "at": rec.created_at}])
    return rec


def record(record_id: str) -> Record:
    """Fetch one row by id (``KeyError`` when unknown — no silent absence)."""
    return RECORDS[record_id]


def supersede(old_id: str, new_id: str, *, reason: str = "", at: Optional[float] = None) -> dict:
    """Invalidate ``old_id`` in favour of ``new_id`` — without deleting it.

    ``valid_to`` is set to the *new* row's ``valid_from`` (event time, so the
    timeline has no gap or overlap) and ``expired_at`` to now (ingestion time).
    The superseded payload is left exactly as written.
    """
    old = RECORDS[old_id]      # KeyError when unknown: never a silent no-op
    new = RECORDS[new_id]
    now = _now(at)
    old.valid_to = new.valid_from
    old.expired_at = now
    old.superseded_by = new_id
    old.reason = reason
    row = old.to_dict()
    _emit([{
        "event": "supersede", "id": old_id, "by": new_id,
        "valid_to": old.valid_to, "expired_at": now, "reason": reason,
    }])
    return row


def _in_force(rec: Record, as_of: Optional[float] = None) -> bool:
    if as_of is None:
        return rec.expired_at is None
    if rec.valid_from > as_of:
        return False
    return rec.valid_to is None or rec.valid_to > as_of


def recall(*, include_expired: bool = False, as_of: Optional[float] = None) -> List[Record]:
    """Rows in force now (default), all rows, or rows in force at ``as_of``."""
    if as_of is not None:
        return [r for r in RECORDS.values() if _in_force(r, as_of)]
    if include_expired:
        return list(RECORDS.values())
    return [r for r in RECORDS.values() if _in_force(r)]


def memory_as_of(ts: float) -> List[Record]:
    """Point-in-time recall: what the system believed to be true at ``ts``."""
    return recall(as_of=float(ts))


# ---------------------------------------------------------------------------
# 3. tool-result clearing
# ---------------------------------------------------------------------------


@dataclass
class ClearResult:
    """The rewritten transcript plus the honest accounting behind it."""

    transcript: List[dict] = field(default_factory=list)
    cleared: int = 0
    saved_tokens: int = 0
    fired: bool = False
    reason: str = ""
    rows: List[dict] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.transcript)


def estimate_tokens(text: Any) -> int:
    """Cheap, dependency-free token estimate (~4 chars/token)."""
    if text is None:
        return 0
    if not isinstance(text, str):
        text = str(text)
    return max(1, len(text) // 4)


def _item_text(item: Mapping) -> str:
    for key in ("text", "content", "output"):
        value = item.get(key)
        if isinstance(value, str):
            return value
    try:
        return json.dumps(item, sort_keys=True, default=str)
    except Exception:
        return repr(item)


def _as_item(item: Any) -> dict:
    if isinstance(item, Mapping):
        return dict(item)
    return {"kind": "text", "text": item if isinstance(item, str) else repr(item)}


def _tool_name(item: Mapping) -> str:
    for key in ("tool", "tool_name", "name"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def clear_tool_results(
    transcript: Sequence[Any],
    *,
    token_trigger: int = DEFAULT_TRIGGER_TOKENS,
    keep: int = DEFAULT_KEEP_TOOL_USES,
    exclude_tools: Sequence[str] = (),
    clear_at_least: int = 1,
    tokens_of: Optional[Callable[[Any], int]] = None,
    archive: Optional[Callable[[str, Any], None]] = None,
    at: Optional[float] = None,
    ledger: Optional[Callable[[dict], None]] = None,
) -> ClearResult:
    """Replace old, re-fetchable tool results with a stub + retrievable pointer.

    Runs *before* any summarization: no model call, no loss of the decision
    trail.  Assistant text, decisions, gate events, pinned items, excluded
    tools and the last ``keep`` tool results are never touched.  The caller's
    transcript is not mutated — the rewrite is returned, so history stays
    append-only.
    """
    try:
        items = [_as_item(i) for i in (transcript or [])]
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("context-hygiene could not normalize transcript: %s", exc)
        items = []
    counter = tokens_of or estimate_tokens

    try:
        return _clear(
            items, counter,
            token_trigger=token_trigger, keep=keep, exclude_tools=exclude_tools,
            clear_at_least=clear_at_least, archive=archive, at=at, ledger=ledger,
        )
    except Exception as exc:  # never lose context on a bug in here
        _log.warning("context-hygiene clearing failed open: %s: %s", type(exc).__name__, exc)
        rows = [{"event": "clear", "status": "unjudged", "reason": f"error: {type(exc).__name__}"}]
        _emit(rows, ledger)
        return ClearResult(transcript=items, cleared=0, saved_tokens=0,
                           fired=False, reason="error", rows=rows)


def _clear(
    items: List[dict],
    counter: Callable[[Any], int],
    *,
    token_trigger: int,
    keep: int,
    exclude_tools: Sequence[str],
    clear_at_least: int,
    archive: Optional[Callable[[str, Any], None]],
    at: Optional[float],
    ledger: Optional[Callable[[dict], None]],
) -> ClearResult:
    try:
        trigger = int(token_trigger)
    except (TypeError, ValueError):
        trigger = DEFAULT_TRIGGER_TOKENS
    try:
        keep_n = max(0, int(keep))
    except (TypeError, ValueError):
        keep_n = DEFAULT_KEEP_TOOL_USES
    try:
        floor = max(0, int(clear_at_least))
    except (TypeError, ValueError):
        floor = 1

    excluded = {str(t) for t in (exclude_tools or ())}
    total = sum(counter(_item_text(i)) for i in items)

    if total <= trigger:
        rows = [{"event": "clear", "status": "skipped", "reason": "below-trigger",
                 "tokens": total, "trigger": trigger}]
        _emit(rows, ledger)
        return ClearResult(transcript=items, cleared=0, saved_tokens=0,
                           fired=False, reason="below-trigger", rows=rows)

    tool_idx = [i for i, it in enumerate(items) if it.get("kind") == "tool_result"]
    protected = set(tool_idx[-keep_n:]) if keep_n else set()
    candidates: List[int] = []
    for i in tool_idx:
        item = items[i]
        if i in protected or item.get("cleared") is True or is_pinned(item):
            continue
        if _tool_name(item) in excluded:
            continue
        candidates.append(i)

    stubs: dict = {}
    savings = 0
    for i in candidates:
        text = _item_text(items[i])
        tool = _tool_name(items[i]) or "tool"
        ptr = externalize(
            text, kind="tool_result", ref=_ref_for(i, tool, text), archive=archive, at=at
        )
        args = items[i].get("args", items[i].get("arguments"))
        try:
            args_json = json.dumps(args, sort_keys=True, default=str)
        except Exception:
            args_json = repr(args)
        stub_text = f"{ptr.line()} re-fetch: {tool}({args_json})"
        saved = max(0, counter(text) - counter(stub_text))
        stubs[i] = (ptr, stub_text, saved)
        savings += saved

    if savings < floor:
        rows = [{"event": "clear", "status": "skipped", "reason": "below-floor",
                 "savings": savings, "clear_at_least": floor}]
        _emit(rows, ledger)
        return ClearResult(transcript=items, cleared=0, saved_tokens=0,
                           fired=False, reason="below-floor", rows=rows)

    out: List[dict] = []
    rows: List[dict] = []
    cleared = 0
    for i, item in enumerate(items):
        if i not in stubs:
            out.append(item)
            continue
        ptr, stub_text, saved = stubs[i]
        tool = _tool_name(item) or "tool"
        stub = dict(item)
        stub["text"] = stub_text
        stub["cleared"] = True
        stub["pointer"] = ptr
        stub["cleared_tokens"] = counter(_item_text(item))
        out.append(stub)
        cleared += 1
        rows.append({
            "event": "clear", "status": "cleared", "tool": tool,
            "ref": ptr.ref, "sha": ptr.sha, "saved_tokens": saved,
            "at": _now(at),
        })

    _emit(rows, ledger)
    return ClearResult(transcript=out, cleared=cleared, saved_tokens=savings,
                       fired=True, reason="cleared", rows=rows)


def _ref_for(index: int, tool: str, text: str) -> str:
    return f"tool_result:{tool}:{index}:{digest(text)[:12]}"


# ---------------------------------------------------------------------------
# 4a. prefix stability (KV-cache) + mask-don't-remove
# ---------------------------------------------------------------------------

#: (severity, pattern, label) — anything matching here changes between turns
#: while sitting in the *stable* prefix, so it kills the provider's cache from
#: that token on.
VOLATILE_PATTERNS = (
    ("high", re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"),
     "second-precision timestamp"),
    ("high", re.compile(r"\b\d{2}:\d{2}:\d{2}\b"), "clock time"),
    ("high", re.compile(r"\b1[0-9]{9}\b|\b1[0-9]{12}\b"), "epoch timestamp"),
    ("medium", re.compile(r"\bpid\s*[=:]\s*\d+", re.I), "process id"),
    ("medium", re.compile(r"\bturn\s*[:=]?\s*\d+\b", re.I), "per-turn counter"),
    ("medium", re.compile(r"\b(?:nonce|request_id|trace_id|session_id)\s*[=:]\s*\S+", re.I),
     "per-request id"),
    ("medium", re.compile(r"\b(?:counter|cwd|current_file)\s*[=:]\s*\S+", re.I), "volatile state"),
)

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass(frozen=True)
class Finding:
    """A typed prefix-hygiene finding (serializable for the ledger/cockpit)."""

    code: str
    severity: str
    where: str
    evidence: str
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "where": self.where,
            "evidence": self.evidence,
            "reason": self.reason,
        }


@dataclass
class PrefixReport:
    """The prefix verdict: hash, whether it moved, and why that matters."""

    hash: str
    stable: bool = True
    changed: bool = False
    findings: List[Finding] = field(default_factory=list)
    declared_reason: str = ""
    previous_hash: Optional[str] = None


def _block_text(block: Any) -> str:
    if isinstance(block, str):
        return block
    try:
        return json.dumps(block, sort_keys=True, default=str)
    except Exception:
        return repr(block)


def prefix_hash(blocks: Sequence[Any]) -> str:
    """Deterministic hash of the stable prefix.

    Serialization is canonical (sorted keys, no incidental whitespace) so two
    structurally identical prefixes hash the same — a JSON serializer that
    reorders keys would otherwise bust the cache on every turn.
    """
    try:
        raw = json.dumps(list(blocks or []), sort_keys=True, separators=(",", ":"),
                         default=str, ensure_ascii=False)
    except Exception:
        raw = repr(blocks)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def check_prefix_stability(
    blocks: Sequence[Any],
    *,
    previous_hash: Optional[str] = None,
    declared_reason: str = "",
) -> PrefixReport:
    """Hash the prefix, report volatile content, flag undeclared cache busts.

    ``previous_hash`` (the hash logged on the previous turn) turns a silent
    cache-bust into a typed finding; ``declared_reason`` records an intentional
    change and suppresses that finding — the change is then auditable, not
    invisible.
    """
    current = prefix_hash(blocks)
    findings: List[Finding] = []
    for i, block in enumerate(blocks or []):
        text = _block_text(block)
        for severity, pattern, label in VOLATILE_PATTERNS:
            match = pattern.search(text)
            if match:
                findings.append(Finding(
                    code="volatile-prefix",
                    severity=severity,
                    where=f"block[{i}]",
                    evidence=match.group(0)[:80],
                    reason=f"{label} in the stable prefix changes between turns "
                           "and invalidates the provider KV cache from that token on",
                ))
                break

    changed = bool(previous_hash) and previous_hash != current
    if changed and not declared_reason:
        findings.append(Finding(
            code="prefix-changed",
            severity="high",
            where="prefix",
            evidence=f"{str(previous_hash)[:12]} -> {current[:12]}",
            reason="the prompt prefix changed with no declared reason; every token "
                   "after the change is recomputed uncached",
        ))

    findings.sort(key=lambda f: _SEVERITY_ORDER.get(f.severity, 9))
    report = PrefixReport(
        hash=current,
        stable=not findings,
        changed=changed,
        findings=findings,
        declared_reason=declared_reason,
        previous_hash=previous_hash,
    )
    _emit([{
        "event": "prefix-check", "hash": current, "changed": changed,
        "stable": report.stable, "findings": [f.to_dict() for f in findings],
    }])
    return report


def tool_mask(
    tools: Iterable[Any],
    *,
    allow: Optional[Sequence[str]] = None,
    deny: Optional[Sequence[str]] = None,
) -> dict:
    """Availability as a selection-time mask — never by rewriting the tool block.

    Tool definitions sit near the front of the prompt: adding or removing one
    invalidates the cache for everything after it *and* leaves earlier actions
    referring to tools the model can no longer see.  ``allow``/``deny`` match
    full names or group prefixes (``browser_*``), which is how a whole group is
    gated without touching the prefix.
    """
    def _matches(name: str, pattern: str) -> bool:
        pattern = str(pattern)
        if pattern.endswith("*"):
            return name.startswith(pattern[:-1])
        return name == pattern

    allow_p = [str(p) for p in (allow or ())]
    deny_p = [str(p) for p in (deny or ())]

    allowed: List[str] = []
    blocked: List[str] = []
    for tool in tools:
        name = tool if isinstance(tool, str) else _tool_name(tool) if isinstance(tool, Mapping) else str(tool)
        ok = True
        if allow_p:
            ok = any(_matches(name, p) for p in allow_p)
        if ok and any(_matches(name, p) for p in deny_p):
            ok = False
        (allowed if ok else blocked).append(name)

    return {"allowed": sorted(allowed), "blocked": sorted(blocked),
            "mask": {n: True for n in sorted(allowed)} | {n: False for n in sorted(blocked)}}


# ---------------------------------------------------------------------------
# 4b. retry budgets
# ---------------------------------------------------------------------------


class RetryBudget:
    """Per-tool retry counters, reset on success, with the two failure channels.

    Semantics mirror the typed-judgment retry contract: ``max_retries=N`` means
    N retries / N+1 attempts; the counter is keyed by tool name and **resets on
    success** (so a tool alternating fail/success never exhausts a budget of 1);
    a hallucinated tool name gets its own budget under the invented name; and
    the explicit "failed, do not retry" channel (:meth:`note_failed`) never
    consumes the budget.
    """

    DEFAULT_TOOLS = 3
    DEFAULT_OUTPUT = 1

    def __init__(self, retries: Any = None, *, tools: Optional[int] = None,
                 output: Optional[int] = None) -> None:
        if isinstance(retries, Mapping):
            tools = retries.get("tools", tools)
            output = retries.get("output", output)
        elif isinstance(retries, bool):
            tools = output = int(retries)
        elif isinstance(retries, int):
            tools = output = retries
        self.tools = self.DEFAULT_TOOLS if tools is None else max(0, int(tools))
        self.output = self.DEFAULT_OUTPUT if output is None else max(0, int(output))
        self._counts: dict = {}
        self._output_count = 0
        self._no_retry: dict = {}

    # -- tools ---------------------------------------------------------------
    def note_failure(self, tool: Any) -> int:
        """One failed attempt for ``tool``; raises when the budget is spent."""
        name = str(tool)
        count = self._counts.get(name, 0) + 1
        self._counts[name] = count
        _emit([{"event": "retry", "tool": name, "attempt": count, "budget": self.tools}])
        if count > self.tools:
            raise RetryExhausted(name, self.tools, count)
        return count

    def note_success(self, tool: Any) -> None:
        """Reset the counter for ``tool`` — success clears the slate."""
        name = str(tool)
        self._counts.pop(name, None)
        _emit([{"event": "retry-reset", "tool": name}])

    def note_failed(self, tool: Any, reason: str = "") -> None:
        """Explicit "failed, do not retry": records it, consumes no budget."""
        name = str(tool)
        self._no_retry[name] = self._no_retry.get(name, 0) + 1
        _emit([{"event": "failed", "tool": name, "retried": False, "reason": reason}])

    def note_unknown_tool(self, name: Any) -> int:
        """A hallucinated tool name: its own budget, bounded agent-wide."""
        return self.note_failure(str(name))

    def remaining(self, tool: Any) -> int:
        return max(0, self.tools - self._counts.get(str(tool), 0))

    def exhausted(self, tool: Any) -> bool:
        return self.remaining(tool) <= 0

    @property
    def counts(self) -> dict:
        return dict(self._counts)

    @property
    def no_retry_counts(self) -> dict:
        return dict(self._no_retry)

    # -- output --------------------------------------------------------------
    def note_output_failure(self) -> int:
        """One failed output-validation attempt; same N-retries contract."""
        self._output_count += 1
        _emit([{"event": "retry", "tool": "", "channel": "output",
                "attempt": self._output_count, "budget": self.output}])
        if self._output_count > self.output:
            raise RetryExhausted("output", self.output, self._output_count)
        return self._output_count

    def note_output_success(self) -> None:
        self._output_count = 0
        _emit([{"event": "retry-reset", "tool": "", "channel": "output"}])

    def output_remaining(self) -> int:
        return max(0, self.output - self._output_count)


# ---------------------------------------------------------------------------
# 4c. worst-case request multiplication
# ---------------------------------------------------------------------------


@dataclass
class RunEstimate:
    """Worst-case request volume for a run, decomposed into its multipliers."""

    requests: int
    seconds: float
    multipliers: dict

    def to_dict(self) -> dict:
        return {"requests": self.requests, "seconds": self.seconds,
                "multipliers": dict(self.multipliers)}


def _worst_case_seconds(requests: int, timeout: float, backoff: float) -> float:
    return requests * float(timeout) + float(backoff)


def worst_case_requests(
    *,
    turns: int,
    tools_per_turn: int,
    output_retries: int,
    sdk_max_retries: int,
    transport_attempts: int,
) -> int:
    """N x M x K, the three nested multipliers from the provider docs.

    N = requests per logical call (initial + one follow-up per tool call +
    output retry prompts); M = attempts inside the SDK client
    (``max_retries=2`` -> 3); K = attempts on the wire (``stop_after_attempt(2)``
    -> 2).
    """
    n = max(0, int(turns)) * (1 + max(0, int(tools_per_turn))) + max(0, int(output_retries))
    m = max(0, int(sdk_max_retries)) + 1
    k = max(0, int(transport_attempts))
    return n * m * k


def worst_case_seconds(
    *,
    turns: int,
    tools_per_turn: int,
    output_retries: int,
    sdk_max_retries: int,
    transport_attempts: int,
    timeout: float,
    backoff: float = 0.0,
) -> float:
    """Worst-case wall time: every wire request carries its own timeout."""
    requests = worst_case_requests(
        turns=turns, tools_per_turn=tools_per_turn, output_retries=output_retries,
        sdk_max_retries=sdk_max_retries, transport_attempts=transport_attempts,
    )
    return round(_worst_case_seconds(requests, timeout, backoff), 6)


def estimate_run(
    *,
    turns: int,
    tools_per_turn: int,
    output_retries: int,
    sdk_max_retries: int,
    transport_attempts: int,
    timeout: float,
    backoff: float = 0.0,
) -> RunEstimate:
    """Pure estimator for the cockpit card — no network, no side effects."""
    n = max(0, int(turns)) * (1 + max(0, int(tools_per_turn))) + max(0, int(output_retries))
    m = max(0, int(sdk_max_retries)) + 1
    k = max(0, int(transport_attempts))
    requests = max(0, n) * m * k
    estimate = RunEstimate(
        requests=requests,
        seconds=round(_worst_case_seconds(requests, timeout, backoff), 6),
        multipliers={"N": n, "M": m, "K": k},
    )
    _emit([{"event": "estimate", **estimate.to_dict()}])
    return estimate


def assert_within_ceiling(
    estimate: RunEstimate,
    *,
    ceiling_requests: Optional[int] = None,
    ceiling_seconds: Optional[float] = None,
) -> RunEstimate:
    """Refuse an over-budget configuration *before* the run starts."""
    if ceiling_requests is not None and estimate.requests > int(ceiling_requests):
        raise BudgetRefused(
            f"refused before run: worst case {estimate.requests} requests "
            f"({estimate.multipliers['N']}x{estimate.multipliers['M']}x{estimate.multipliers['K']}) "
            f"> ceiling {ceiling_requests}",
            estimate=estimate,
        )
    if ceiling_seconds is not None and estimate.seconds > float(ceiling_seconds):
        raise BudgetRefused(
            f"refused before run: worst case {estimate.seconds}s "
            f"> ceiling {float(ceiling_seconds)}s ({estimate.requests} requests)",
            estimate=estimate,
        )
    return estimate
