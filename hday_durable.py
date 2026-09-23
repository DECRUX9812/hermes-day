"""hday_durable — the DURABLE-STATE lane (harvest: ``runs/harvest/graph-durable.json``).

Four mechanisms, deterministic and stdlib-only:

1. **Step memoization** (Inngest steps). ``step()`` keys a result by
   ``(run_id, step_id, occurrence)`` — loops reuse one step id and the
   occurrence counter keeps their memo keys distinct. A re-executed pass (a
   resume) returns the stored result instead of calling the body, and the
   per-(run, step) attempt counter climbs across passes and resets when the
   step completes.
2. **Continue-As-New** (Temporal). ``append_row()`` rolls the live journal
   segment over when it reaches a row/byte budget: the segment is sealed, the
   live state is carried forward and a new generation starts under the SAME
   logical run id, linked by a continuation pointer. ``chain()`` / ``fold()``
   replay the whole chain as one execution.
3. **The durability dial** (LangGraph). ``judge_durability()`` returns a typed
   judgment — mode ``exit`` | ``async`` | ``sync`` plus the reason, cost class
   and latency class — and ``DurableWriter`` honours it: sync persists before
   the next step starts, async flushes at step boundaries, exit flushes once at
   run end. A failed flush is surfaced (``DurabilityWriteError``), never
   swallowed.
4. **Durable sleeps** (Inngest sleeps). ``sleep_until()`` records a wake row
   and returns immediately — no process is held. ``scan_due()`` /
   ``resume_due()`` resume exactly once per wake and record planned vs actual
   wait.

Clock: nothing here reads the wall clock unless the caller lets it — pass
``now=`` (a callable) or an explicit timestamp. No network, no threads.

Rows: every decision (step status, rollover, mode, wait) is a typed row handed
to an injected ``ledger`` sink. In the plugin that sink appends to the ONE
existing decision ledger (``rec["gate"]``); this module never creates a second
store. The ``Journal`` holds run STATE (segments, memo entries, attempt
counters, wakes) — it is not a ledger.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("hday.durable")

#: The durability dial's values, cheapest first.
DURABILITIES: Tuple[str, ...] = ("exit", "async", "sync")
#: What each mode costs per step (typed, so a cockpit can show WHY a run was slow).
COST_CLASSES: Tuple[str, ...] = ("none", "batched", "fsync_per_step")
LATENCY_CLASSES: Tuple[str, ...] = ("none", "step_boundary", "inline")
LANES: Tuple[str, ...] = ("local",)
STEP_STATUSES: Tuple[str, ...] = ("ran", "memoized")
KINDS: Tuple[str, ...] = ("durable_step", "durable_rollover", "durable_mode", "durable_sleep")

#: Risk at/above which a run must checkpoint synchronously.
SYNC_RISK = 0.6

Ledger = Callable[[Dict[str, Any]], None]

_MISSING = object()


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _clock(now: Optional[Callable[[], float]]) -> Callable[[], float]:
    """The injected clock, or the wall clock when the caller supplied none."""
    return now if callable(now) else time.time


def _row(kind: str, *, reason: str = "", ms: float = 0.0, cost: float = 0.0,
         **extra: Any) -> Dict[str, Any]:
    """One typed decision row, shaped like the gate's ledger rows."""
    row: Dict[str, Any] = {"kind": str(kind), "lane": LANES[0],
                           "ms": float(ms), "cost": float(cost),
                           "reason": str(reason)}
    row.update(extra)
    return row


def _emit(row: Dict[str, Any], ledger: Optional[Ledger]) -> Dict[str, Any]:
    """Hand a row to the injected sink. A broken sink is logged, never raised —
    the caller still gets the row back in its result."""
    if callable(ledger):
        try:
            ledger(row)
        except Exception as exc:  # noqa: BLE001 — fail-open, loudly
            log.warning("durable ledger sink failed: %s", exc)
    return row


def memo_key(run_id: object, step_id: object, occurrence: int) -> str:
    """The state identifier for one step execution: sha1 of
    ``run_id \\x1f step_id \\x1f occurrence`` (16 hex chars)."""
    raw = f"{run_id}\x1f{step_id}\x1f{int(occurrence)}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


# ---------------------------------------------------------------------------
# the journal (run state — not a ledger)
# ---------------------------------------------------------------------------


def _segment(run_id: str, generation: int, prev: Optional[str]) -> Dict[str, Any]:
    return {"id": f"{run_id}#g{generation}", "run": run_id,
            "generation": int(generation), "prev": prev,
            "rows": [], "rows_count": 0, "bytes": 0,
            "sealed": False, "sealed_at": None,
            "state": {}, "at": None}


class Journal:
    """Run state for one or more logical runs.

    ``path`` (optional) makes it file-backed: ``save()`` writes atomically,
    ``load()`` reads back. The clock and the ledger sink are injected; nothing
    here touches the network or spawns a thread.
    """

    def __init__(self, path: Any = None, *, now: Optional[Callable[[], float]] = None,
                 ledger: Optional[Ledger] = None) -> None:
        self.path = (os.path.abspath(os.path.expanduser(os.fspath(path)))
                     if path is not None else None)
        self._now = _clock(now)
        self.ledger = ledger
        self.data: Dict[str, Any] = {"version": 1, "runs": {}}

    # -- clock / rows -------------------------------------------------------

    def now(self) -> float:
        return float(self._now())

    # -- persistence --------------------------------------------------------

    def save(self) -> bool:
        """Write the journal atomically. Fail-open: False, never raises."""
        if not self.path:
            log.warning("durable journal has no path; save() is a no-op")
            return False
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, default=_jsonable, separators=(",", ":"),
                          sort_keys=True)
            os.replace(tmp, self.path)          # atomic on POSIX
            return True
        except Exception as exc:  # noqa: BLE001 — fail-open, loudly
            log.warning("durable journal save to %s failed: %s", self.path, exc)
            return False

    def load(self) -> bool:
        """Read the journal back. Fail-open: False (and empty state), never raises."""
        if not self.path:
            return False
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            log.warning("durable journal load from %s failed: %s", self.path, exc)
            return False
        if not isinstance(data, dict) or not isinstance(data.get("runs"), dict):
            log.warning("durable journal %s has an unexpected shape", self.path)
            return False
        self.data = data
        return True

    # -- runs ---------------------------------------------------------------

    def run(self, run_id: object) -> Dict[str, Any]:
        """The run record, created on first use. The logical run id never changes."""
        rid = str(run_id)
        run = self.data["runs"].get(rid)
        if run is None:
            run = {"run_id": rid, "generation": 0,
                   "segments": [_segment(rid, 0, None)],
                   "memo": {}, "attempts": {}, "wakes": [], "state": {},
                   "pass": 0, "occurrence": {}, "waiting": False, "at": self.now()}
            run["segments"][0]["at"] = run["at"]
            self.data["runs"][rid] = run
        return run


def _jsonable(value: Any) -> Any:
    """Fallback for json.dump — non-serializable memo results become a marker
    rather than blowing up the whole save (fail-open, loudly)."""
    log.warning("durable journal: value of type %s is not JSON-serializable",
                type(value).__name__)
    return {"__unserializable__": type(value).__name__}


def _live_segment(run: Dict[str, Any]) -> Dict[str, Any]:
    return run["segments"][-1]


# ---------------------------------------------------------------------------
# 1. step memoization
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepResult:
    """One step execution — memoized or freshly ran."""

    run: str
    step: str
    occurrence: int
    memo_key: str
    status: str                 # ran | memoized
    result: Any
    attempt: int                # zero-indexed attempt index of this call
    ms: float

    def as_row(self) -> Dict[str, Any]:
        return _row("durable_step", ms=self.ms, run=self.run, step=self.step,
                    occurrence=self.occurrence, memo_key=self.memo_key,
                    status=self.status, attempt=self.attempt,
                    reason=("memo hit — the body was not re-executed"
                            if self.status == "memoized" else "step ran"))


def open_run(journal: Journal, run_id: object, *,
             state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Create/return a run and seed its live state."""
    run = journal.run(run_id)
    if state:
        run["state"].update(dict(state))
        _live_segment(run)["state"] = dict(run["state"])
    return run


def set_state(journal: Journal, run_id: object, patch: Dict[str, Any]) -> Dict[str, Any]:
    """Merge into the run's live state (what a rollover carries forward)."""
    run = journal.run(run_id)
    run["state"].update(dict(patch or {}))
    return run["state"]


def begin_pass(journal: Journal, run_id: object) -> int:
    """Start a new execution pass: occurrence counters reset, so the pass
    replays the same ``(step_id, occurrence)`` sequence and hits the memos."""
    run = journal.run(run_id)
    run["pass"] = int(run.get("pass") or 0) + 1
    run["occurrence"] = {}
    return run["pass"]


def next_occurrence(journal: Journal, run_id: object, step_id: object) -> int:
    """1-based occurrence of ``step_id`` within the live pass (loops reuse an id)."""
    run = journal.run(run_id)
    if not run.get("pass"):
        run["pass"] = 1
    sid = str(step_id)
    occ = int(run["occurrence"].get(sid, 0)) + 1
    run["occurrence"][sid] = occ
    return occ


def memo(journal: Journal, run_id: object, step_id: object,
         occurrence: int) -> Any:
    """The memoized result for one step execution, or None."""
    entry = journal.run(run_id)["memo"].get(memo_key(run_id, step_id, occurrence))
    return entry["result"] if isinstance(entry, dict) else None


def attempts(journal: Journal, run_id: object, step_id: object) -> int:
    """Failed attempts recorded for the current occurrence (0 once it completes)."""
    return int(journal.run(run_id)["attempts"].get(str(step_id), 0))


def step(journal: Journal, run_id: object, step_id: object, fn: Callable[..., Any], *,
         args: Sequence[Any] = (), kwargs: Optional[Dict[str, Any]] = None,
         now: Optional[Callable[[], float]] = None,
         ledger: Optional[Ledger] = None) -> StepResult:
    """Run ``fn`` once per ``(run_id, step_id, occurrence)`` and memoize it.

    A memo hit returns the stored result without calling ``fn``. A body that
    raises leaves NO memo entry (the step re-runs on resume) and increments the
    per-(run, step) attempt counter; a completed step resets that counter.
    """
    clock = _clock(now) if callable(now) else journal.now
    sink = ledger if ledger is not None else journal.ledger
    run = journal.run(run_id)
    sid = str(step_id)
    occurrence = next_occurrence(journal, run_id, sid)
    key = memo_key(run_id, sid, occurrence)

    entry = run["memo"].get(key)
    if isinstance(entry, dict):
        result = StepResult(run=str(run_id), step=sid, occurrence=occurrence,
                            memo_key=key, status="memoized",
                            result=entry.get("result"), attempt=0, ms=0.0)
        _emit(result.as_row(), sink)
        return result

    attempt = int(run["attempts"].get(sid, 0))
    started = clock()
    try:
        value = fn(*args, **(kwargs or {}))
    except Exception as exc:
        run["attempts"][sid] = attempt + 1
        _emit(_row("durable_step", ms=max(0.0, clock() - started), run=str(run_id),
                   step=sid, occurrence=occurrence, memo_key=key, status="failed",
                   attempt=attempt, error=type(exc).__name__,
                   reason=f"{type(exc).__name__}: {exc}"[:200]), sink)
        raise
    elapsed = max(0.0, clock() - started)
    run["attempts"][sid] = 0                       # reset when the step completes
    run["memo"][key] = {"result": value, "at": clock(), "attempt": attempt,
                        "step": sid, "occurrence": occurrence}
    out = StepResult(run=str(run_id), step=sid, occurrence=occurrence, memo_key=key,
                     status="ran", result=value, attempt=attempt, ms=elapsed)
    _emit(out.as_row(), sink)
    return out


# ---------------------------------------------------------------------------
# 2. Continue-As-New
# ---------------------------------------------------------------------------


def _over_budget(seg: Dict[str, Any], budget_rows: Optional[int],
                 budget_bytes: Optional[int]) -> bool:
    if budget_rows is not None and seg["rows_count"] >= int(budget_rows):
        return True
    if budget_bytes is not None and seg["bytes"] >= int(budget_bytes):
        return True
    return False


def needs_rollover(journal: Journal, run_id: object, *, budget_rows: Optional[int] = None,
                   budget_bytes: Optional[int] = None) -> bool:
    return _over_budget(_live_segment(journal.run(run_id)), budget_rows, budget_bytes)


def rollover(journal: Journal, run_id: object, *, carry: Optional[Dict[str, Any]] = None,
             reason: str = "segment budget reached",
             now: Optional[Callable[[], float]] = None,
             ledger: Optional[Ledger] = None) -> Dict[str, Any]:
    """Continue-As-New: seal the live segment, carry the state forward, start a
    fresh generation under the same logical run id."""
    clock = _clock(now) if callable(now) else journal.now
    sink = ledger if ledger is not None else journal.ledger
    run = journal.run(run_id)
    if carry:
        run["state"].update(dict(carry))
    old = _live_segment(run)
    old["sealed"] = True
    old["sealed_at"] = clock()
    run["generation"] = int(old["generation"]) + 1
    seg = _segment(str(run_id), run["generation"], prev=old["id"])
    seg["state"] = dict(run["state"])              # the compact blob carried forward
    seg["at"] = clock()
    run["segments"].append(seg)
    row = _row("durable_rollover", reason=reason, run=str(run_id),
               generation=seg["generation"], segment=seg["id"], prev=old["id"],
               rows_count=old["rows_count"], bytes=old["bytes"],
               carried_keys=sorted(run["state"]))
    _emit(row, sink)
    return row


def append_row(journal: Journal, run_id: object, row: Dict[str, Any], *,
               budget_rows: Optional[int] = None, budget_bytes: Optional[int] = None,
               carry: Optional[Dict[str, Any]] = None,
               now: Optional[Callable[[], float]] = None,
               ledger: Optional[Ledger] = None) -> Dict[str, Any]:
    """Append one row to the live segment, rolling over first when the segment
    is at its budget (so the new segment's first row points at the previous one)."""
    run = journal.run(run_id)
    seg = _live_segment(run)
    if seg["sealed"] or _over_budget(seg, budget_rows, budget_bytes):
        budget = "row budget" if budget_rows is not None else "byte budget"
        rollover(journal, run_id, carry=carry, reason=f"{budget} reached",
                 now=now, ledger=ledger)
        run = journal.run(run_id)
        seg = _live_segment(run)
    elif carry:
        run["state"].update(dict(carry))
    entry = dict(row or {})
    entry.setdefault("run", str(run_id))
    entry["segment"] = seg["id"]
    entry["prev"] = seg["prev"]                    # continuation pointer
    seg["rows"].append(entry)
    seg["rows_count"] = len(seg["rows"])
    try:
        seg["bytes"] += len(json.dumps(entry, default=str))
    except Exception:  # noqa: BLE001 — a weird payload must not kill the run
        seg["bytes"] += 1
    return entry


def chain(journal: Journal, run_id: object) -> List[Dict[str, Any]]:
    """Every segment of the logical run, oldest first (one execution)."""
    return list(journal.run(run_id)["segments"])


def chain_state(journal: Journal, run_id: object) -> Dict[str, Any]:
    """The live state carried across the chain."""
    return dict(journal.run(run_id)["state"])


def fold(journal: Journal, run_id: object, fn: Callable[[Any, Dict[str, Any]], Any],
         init: Any, *, include_segments: bool = False) -> Any:
    """Replay the whole chain through ``fn`` as if it had never been rolled over."""
    acc = init
    for seg in chain(journal, run_id):
        if include_segments:
            acc = fn(acc, {"segment": seg["id"], "state": dict(seg["state"])})
        for row in seg["rows"]:
            acc = fn(acc, row)
    return acc


# ---------------------------------------------------------------------------
# 3. the durability dial
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Durability:
    """A typed judgment, not a boolean: which mode, why, and what it costs."""

    mode: str
    reason: str
    cost_class: str
    latency_class: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    run: str = ""

    def as_row(self, run: Optional[str] = None) -> Dict[str, Any]:
        return _row("durable_mode", reason=self.reason, run=self.run if run is None else run,
                    mode=self.mode, cost_class=self.cost_class,
                    latency_class=self.latency_class, evidence=dict(self.evidence))


def judge_durability(*, risk: float = 0.0, irreversible: bool = False,
                     read_only: bool = False, reason: str = "",
                     run: str = "") -> Durability:
    """Resolve the per-run durability dial from the risk/impact signals.

    irreversible or high risk -> ``sync`` (checkpoint on disk before the next
    step), a read-only run -> ``exit`` (nothing to recover, no fsync), anything
    else -> ``async`` (one step-boundary flush; a crash loses at most the live
    step). The returned judgment carries the reason, the cost/latency classes
    and the evidence it was decided from.
    """
    try:
        risk_value = float(risk)
    except (TypeError, ValueError):
        risk_value = 0.0
    evidence = {"risk": risk_value, "irreversible": bool(irreversible),
                "read_only": bool(read_only)}

    if irreversible:
        return Durability("sync", reason or "irreversible side effect — the checkpoint "
                          "must be on disk before the next step starts",
                          "fsync_per_step", "inline", evidence, run)
    if risk_value >= SYNC_RISK:
        return Durability("sync", reason or f"risk {risk_value:.2f} >= {SYNC_RISK} — a "
                          "crash must not lose the last checkpoint",
                          "fsync_per_step", "inline", evidence, run)
    if read_only:
        return Durability("exit", reason or "read-only run — nothing to recover, no fsync",
                          "none", "none", evidence, run)
    return Durability("async", reason or "default — one step-boundary flush; a crash "
                      "loses at most the live step", "batched", "step_boundary",
                      evidence, run)


class DurabilityWriteError(RuntimeError):
    """A flush failed. Typed so the caller can see the mode, how many flushes
    had succeeded, and how many rows are still held (never silently dropped)."""

    def __init__(self, mode: str, flushes: int, pending: int, cause: BaseException) -> None:
        super().__init__(f"durability {mode}: flush #{flushes + 1} failed "
                         f"({type(cause).__name__}: {cause}); {pending} row(s) still buffered")
        self.mode = mode
        self.flushes = int(flushes)
        self.pending_rows = int(pending)
        self.cause = cause


class DurableWriter:
    """Honours a run's durability mode for its rows.

    ``sink`` is the ONE ledger's writer (``callable(rows) -> None``). sync =
    append + flush before the next step starts; async = flush at step
    boundaries; exit = buffer and flush exactly once at run end. A failing sink
    raises :class:`DurabilityWriteError` with the rows still held.
    """

    def __init__(self, sink: Callable[[List[Dict[str, Any]]], None],
                 mode: str = "async") -> None:
        if mode not in DURABILITIES:
            raise ValueError(f"unknown durability mode {mode!r}; expected one of {DURABILITIES}")
        self.mode = str(mode)
        self.sink = sink
        self.flushes = 0
        self.written_rows = 0
        self.closed = False
        self._buf: List[Dict[str, Any]] = []

    def pending(self) -> int:
        return len(self._buf)

    def append(self, row: Dict[str, Any]) -> str:
        """Buffer a row; in sync mode it is flushed before this returns."""
        self._buf.append(dict(row))
        if self.mode == "sync":
            self.flush()
            return "flushed"
        return "buffered"

    def step_boundary(self) -> str:
        """Called before dispatching the next step."""
        if self.mode in ("sync", "async"):
            self.flush()
            return "flushed"
        return "buffered"                     # exit: nothing until run end

    def flush(self) -> int:
        if not self._buf:
            return self.flushes
        batch = list(self._buf)
        try:
            self.sink(batch)
        except Exception as exc:
            raise DurabilityWriteError(self.mode, self.flushes, len(self._buf), exc) from exc
        self._buf.clear()
        self.flushes += 1
        self.written_rows += len(batch)
        return self.flushes

    def close(self) -> int:
        """Run end: flush whatever is buffered, exactly once (idempotent)."""
        if not self.closed:
            self.closed = True
            if self.mode in ("exit", "async"):
                self.flush()
        return self.flushes

    def crash(self) -> Dict[str, Any]:
        """Simulate a process crash: the buffer is lost, flushed rows survive."""
        lost = len(self._buf)
        self._buf.clear()
        return {"mode": self.mode, "lost": lost, "readable": self.written_rows}


# ---------------------------------------------------------------------------
# 4. durable sleeps
# ---------------------------------------------------------------------------


def sleep_until(journal: Journal, run_id: object, at: float, *, reason: str = "",
                resume_token: Optional[str] = None, step_id: str = "",
                now: Optional[Callable[[], float]] = None,
                ledger: Optional[Ledger] = None) -> Dict[str, Any]:
    """Record a wake row and return immediately — no process is held.

    The run is marked waiting; ``resume_due()`` resumes it at/after ``at``.
    """
    clock = _clock(now) if callable(now) else journal.now
    sink = ledger if ledger is not None else journal.ledger
    run = journal.run(run_id)
    started = clock()
    at_value = float(at)
    token = (str(resume_token) if resume_token
             else memo_key(run_id, f"{step_id or 'sleep'}@{len(run['wakes'])}", 1))
    wake = {"run": str(run_id), "at": at_value, "reason": str(reason),
            "resume_token": token, "step": str(step_id),
            "scheduled_at": started, "resumed_at": None, "state": "waiting"}
    run["wakes"].append(wake)
    run["waiting"] = True
    _emit(_row("durable_sleep", reason=reason or "durable sleep — no process held",
               run=str(run_id), status="scheduled", resume_token=token,
               step=str(step_id), at=at_value,
               wait_s=max(0.0, at_value - started)), sink)
    return wake


def scan_due(journal: Journal, now: Optional[Callable[[], float]] = None, *,
             run_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Wakes that are due and not yet resumed, oldest first. Read-only."""
    clock = _clock(now) if callable(now) else journal.now
    t = clock()
    due: List[Dict[str, Any]] = []
    for rid, run in journal.data["runs"].items():
        if run_id is not None and rid != str(run_id):
            continue
        for wake in run["wakes"]:
            if wake.get("resumed_at") is None and float(wake["at"]) <= t:
                due.append(wake)
    return sorted(due, key=lambda w: (float(w["at"]), str(w["resume_token"])))


def resume_due(journal: Journal, now: Optional[Callable[[], float]] = None, *,
               ledger: Optional[Ledger] = None,
               run_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Resume every due wake exactly once, recording planned vs actual wait."""
    clock = _clock(now) if callable(now) else journal.now
    sink = ledger if ledger is not None else journal.ledger
    t = clock()
    out: List[Dict[str, Any]] = []
    for wake in scan_due(journal, now=clock, run_id=run_id):
        wake["resumed_at"] = t
        wake["state"] = "resumed"
        run = journal.data["runs"][wake["run"]]
        run["waiting"] = any(w.get("resumed_at") is None for w in run["wakes"])
        planned = max(0.0, float(wake["at"]) - float(wake["scheduled_at"]))
        entry = {"run": wake["run"], "step": wake["step"],
                 "resume_token": wake["resume_token"], "resumed_at": t,
                 "planned_s": planned, "actual_s": t - float(wake["scheduled_at"]),
                 "lateness_s": round(t - float(wake["at"]), 6)}
        _emit(_row("durable_sleep", reason=wake["reason"] or "durable sleep",
                   run=wake["run"], status="resumed",
                   **{k: v for k, v in entry.items() if k != "run"}), sink)
        out.append(entry)
    return out


def wait_state(journal: Journal, run_id: object) -> str:
    """``waiting`` while the run holds an unresumed wake, else ``running``."""
    run = journal.data["runs"].get(str(run_id))
    if run is None:
        return "unknown"
    return "waiting" if any(w.get("resumed_at") is None for w in run["wakes"]) else "running"
