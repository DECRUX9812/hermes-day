"""hday_orchestration — delegated child runs, close policy, loops, handoffs.

The orchestration lane: everything about work that leaves this agent and comes
back. Four mechanisms, each grounded in a fetched harvest source
(``runs/harvest/``, orchestration theme):

1. **Child-run attribution** — a delegated child carries ``parent_run_id`` plus a
   freshly minted ``subagent_run_id`` (never the agent/profile name; the name is
   kept for display only), a ``checkpoint_ns``-shaped ``namespace``, and a state
   copy taken at the delegation boundary that never writes back into the parent.
   * AG-UI subagents: subagentRunId attribution and suspension
     (https://docs.ag-ui.com/concepts/subagents)
   * LangGraph subgraph persistence modes + checkpoint_ns namespacing
   * Mastra delegation hooks: request context shallow-copied at the boundary
2. **Child-workflow close policy** — a parent cannot close while children are
   open, and an explicit per-child policy (``cancel`` | ``orphan_adopt`` |
   ``hold``) decides what happens to each child when its parent dies, with one
   typed ledger row per child.
   * Temporal child workflows (https://docs.temporal.io/child-workflows)
3. **Loop termination owned by the loop** — an iteration budget plus a
   no-progress detector; a loop ends with a typed reason (``escalated:<reason>``
   | ``max_iterations`` | ``no_progress:<key>`` | ``error:<class>``) and never
   spins silently.
   * Google ADK LoopAgent, the exit_loop escalation pattern
     (https://adk.dev/agents/workflow-agents/loop-agents/)
4. **Handoff records** — a typed handoff (from, to, why, carried state keys)
   written to the ledger when work moves between agents; an undeclared state key
   is refused *without* a transfer.
   * openai-agents typed handoff tool (declared destination, argument schema)
   * ADK scoped state keys / ``output_key`` (https://adk.dev/sessions/state/)

Design rules
------------
* **Standalone + import-safe.** No PluginContext, no hooks, no I/O at import;
  stdlib only. The integrator wires it into the plugin; nothing here reaches out.
* **Deterministic offline path.** The clock and the id minter are injected
  (``clock=`` / ``mint=``), so a run is reproducible byte-for-byte; child
  execution is a caller-supplied stub callable (no subprocess, no network).
* **Decisions vs programming errors.** Anything that is a *decision* is returned
  as ``{"ok": False, "reason": <token>}`` and always leaves a ledger row.
  Anything that is a *programming error* raises :class:`OrchestrationError`
  carrying a stable ``reason`` token — never free text.
* **One ledger, existing shape.** Rows keep the shared keys (``kind``, ``lane``,
  ``ms``, ``cost``, ``ts``) and add the orchestration ones (``run_id``,
  ``parent_run_id``, ``subagent_run_id``, ``action``, ``reason``) so the cockpit
  ledger view needs no special-casing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Tuple

log = logging.getLogger("hermes-day.orchestration")

__all__ = [
    "OrchestrationError",
    "Run",
    "Iteration",
    "LoopResult",
    "Loop",
    "Handoff",
    "Orchestrator",
    "LEDGER",
    "LEDGER_CAP",
    "reset_ledger",
    "CLOSE_POLICIES",
    "PERSISTENCE_MODES",
    "LIVE_STATUSES",
    "ADOPTED_BY",
    "DEFAULT_MAX_ITERATIONS",
]

LEDGER_CAP = 500
LEDGER: Deque[Dict[str, Any]] = deque(maxlen=LEDGER_CAP)

#: What may happen to a child when its parent dies (Temporal's parent_close_policy,
#: with ``abandon``/``terminate`` collapsed into ``orphan_adopt``/``cancel``).
CLOSE_POLICIES: Tuple[str, ...] = ("cancel", "orphan_adopt", "hold")

#: Subgraph persistence modes: a plain call, a thread-scoped subgraph, or one
#: invocation-scoped subgraph per delegation.
PERSISTENCE_MODES: Tuple[str, ...] = ("per_invocation", "per_thread", "stateless")

#: Statuses that count as "still open" — a suspended or held run is alive.
LIVE_STATUSES: Tuple[str, ...] = ("running", "suspended", "held")

#: Who adopts an orphaned child (the runtime, not another lane).
ADOPTED_BY = "orchestrator"

DEFAULT_MAX_ITERATIONS = 10

#: Run-scoped identity keys never cross a delegation boundary.
_IDENTITY_KEYS = frozenset({"run_id", "parent_run_id", "subagent_run_id", "namespace",
                            "session_id", "sid"})


def reset_ledger() -> None:
    """Clear the module ledger (tests / session rollover)."""
    LEDGER.clear()


class OrchestrationError(Exception):
    """Typed orchestration failure — ``reason`` is a stable token, never prose."""

    def __init__(self, reason: str, **detail: Any) -> None:
        self.reason = str(reason)
        self.detail = dict(detail)
        super().__init__(self.reason + (f" {self.detail}" if self.detail else ""))


def _digest(value: Any) -> str:
    """Stable 12-hex digest of an observed value (JSON-canonical, repr fallback)."""
    try:
        payload = json.dumps(value, sort_keys=True, default=repr)
    except Exception:  # pragma: no cover - defensive
        payload = repr(value)
    return hashlib.sha1(payload.encode("utf-8", "replace")).hexdigest()[:12]


@dataclass
class Run:
    """A run: the root, or a delegated child carrying its parent's identity."""

    run_id: str
    name: str
    parent_run_id: Optional[str] = None
    subagent_run_id: Optional[str] = None
    namespace: str = ""
    persistence: str = "per_invocation"
    close_policy: str = "cancel"
    max_steps: Optional[int] = None
    state: Dict[str, Any] = field(default_factory=dict)
    status: str = "running"
    suspended_by: Optional[str] = None
    adopted_by: Optional[str] = None
    origin_parent_run_id: Optional[str] = None
    closed_reason: str = ""
    depth: int = 0

    @property
    def open(self) -> bool:
        """True while the run is alive (running, suspended or held)."""
        return self.status in LIVE_STATUSES

    def as_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "name": self.name,
            "parent_run_id": self.parent_run_id,
            "subagent_run_id": self.subagent_run_id,
            "namespace": self.namespace,
            "persistence": self.persistence,
            "close_policy": self.close_policy,
            "max_steps": self.max_steps,
            "status": self.status,
            "suspended_by": self.suspended_by,
            "adopted_by": self.adopted_by,
            "origin_parent_run_id": self.origin_parent_run_id,
            "closed_reason": self.closed_reason,
            "depth": self.depth,
            "state_keys": sorted(self.state),
        }


@dataclass
class Iteration:
    """One pass of a loop, handed to the loop's step callable."""

    index: int
    _escalation: Optional[str] = None
    _digest: Optional[Tuple[str, str]] = None

    def escalate(self, reason: str) -> None:
        """End the loop now with a typed reason (ADK's exit-tool pattern)."""
        text = str(reason or "").strip()
        if not text:
            raise OrchestrationError("empty_escalation_reason", iteration=self.index)
        self._escalation = text

    def observe(self, value: Any, key: str = "state") -> None:
        """Report this iteration's progress signal; the loop owns the detector."""
        self._digest = (str(key), _digest(value))


@dataclass
class LoopResult:
    """Why a loop stopped — always a typed reason, never a silent truncation."""

    loop: str = ""
    iterations: int = 0
    terminated_by: str = ""
    reason: str = ""
    warning: bool = False
    rows: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "loop": self.loop,
            "iterations": self.iterations,
            "terminated_by": self.terminated_by,
            "reason": self.reason,
            "warning": self.warning,
        }


@dataclass
class Handoff:
    """A typed transfer of work between agents, written to the ledger."""

    from_run: str
    to_run: str
    to_lane: str
    why: str
    carried: Tuple[str, ...] = ()
    ts: float = 0.0
    tool_name: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "from_run": self.from_run,
            "to_run": self.to_run,
            "to_lane": self.to_lane,
            "why": self.why,
            "carried": list(self.carried),
            "ts": self.ts,
            "tool_name": self.tool_name,
        }


class Loop:
    """A bounded loop that owns its own termination.

    The budget lives here, not in the lane: a step cannot extend it, and the
    no-progress detector turns "spinning" into a typed escalation instead.
    """

    def __init__(self, orchestrator: "Orchestrator", name: str, *, max_iterations: int = 10,
                 no_progress_after: Optional[int] = None, run: Optional[Run] = None) -> None:
        if int(max_iterations) < 1:
            raise OrchestrationError("invalid_iteration_budget", max_iterations=max_iterations)
        self.orchestrator = orchestrator
        self.name = str(name)
        self.max_iterations = int(max_iterations)
        # A detector needs at least two observations to see "no movement".
        self.no_progress_after = (None if no_progress_after is None
                                  else max(2, int(no_progress_after)))
        #: the delegated run that owns this loop (attribution), or None
        self.owner = run

    # -- ledger -----------------------------------------------------------------
    def _emit(self, kind: str, **fields: Any) -> Dict[str, Any]:
        """Emit through the orchestrator, stamped with the owning run's identity."""
        run = self.owner
        if run is not None:
            fields.setdefault("lane", run.name)
            fields.setdefault("run_id", run.run_id)
            fields.setdefault("parent_run_id", run.parent_run_id)
            fields.setdefault("subagent_run_id", run.subagent_run_id)
        return self.orchestrator._emit(kind, **fields)

    def _emit_iteration(self, iteration: Iteration, index: int,
                        digest: Optional[Tuple[str, str]] = None,
                        error: Optional[str] = None) -> Dict[str, Any]:
        if error:
            action, reason = "error", f"error:{error}"
        elif iteration._escalation:
            action, reason = "escalate", f"escalated:{iteration._escalation}"
        else:
            action, reason = "iterate", ""
        return self._emit(
            "loop_iteration",
            action=action,
            reason=reason,
            loop=self.name,
            iteration=index,
            digest=(digest[1] if digest else ""),
            digest_key=(digest[0] if digest else ""),
            escalated=bool(iteration._escalation),
            escalation_reason=(iteration._escalation or ""),
        )

    def _terminate(self, result: LoopResult, terminated_by: str, reason: str, *,
                   warning: bool, iterations: int) -> Dict[str, Any]:
        result.terminated_by = terminated_by
        result.reason = reason
        result.warning = bool(warning)
        row = self._emit("loop_terminated", action="terminate", reason=reason, loop=self.name,
                         terminated_by=terminated_by, iterations=iterations,
                         warning=bool(warning))
        result.rows.append(row)
        return row

    # -- the loop ---------------------------------------------------------------
    def run(self, step: Callable[[Iteration], Any]) -> LoopResult:
        """Run ``step`` until it escalates, stops progressing, or the budget ends.

        ``step`` raising propagates: the loop records the typed reason first, so
        "why did this stop" is still answerable from the ledger.
        """
        result = LoopResult(loop=self.name)
        previous: Optional[Tuple[str, str]] = None
        streak = 0

        for index in range(1, self.max_iterations + 1):
            iteration = Iteration(index=index)
            try:
                step(iteration)
            except BaseException as exc:
                result.iterations = index
                self._emit_iteration(iteration, index, error=type(exc).__name__)
                self._terminate(result, "error", f"error:{type(exc).__name__}",
                                warning=True, iterations=index)
                raise

            result.iterations = index
            observed = iteration._digest
            if observed is None:
                previous, streak = None, 0
            else:
                streak = streak + 1 if (previous is not None and observed == previous) else 0
                previous = observed

            result.rows.append(self._emit_iteration(iteration, index, digest=observed))

            if iteration._escalation:
                self._terminate(result, "escalated", f"escalated:{iteration._escalation}",
                                warning=False, iterations=index)
                return result
            if (self.no_progress_after is not None and observed is not None
                    and streak >= self.no_progress_after - 1):
                self._terminate(result, "no_progress", f"no_progress:{observed[0]}",
                                warning=True, iterations=index)
                return result

        self._terminate(result, "max_iterations", "max_iterations", warning=True,
                        iterations=result.iterations)
        return result


class Orchestrator:
    """Owns runs, their close policy, their loops and the handoffs between them."""

    def __init__(self, *, clock: Optional[Callable[[], float]] = None,
                 mint: Optional[Callable[[str], str]] = None,
                 ledger: Optional[Callable[[dict], None]] = None,
                 max_depth: int = 8) -> None:
        self._clock = clock if callable(clock) else time.time
        self._mint = mint if callable(mint) else (lambda hint: f"{hint}-{uuid.uuid4().hex[:8]}")
        self._sink = ledger if callable(ledger) else None
        self.max_depth = int(max_depth)
        self._runs: Dict[str, Run] = {}
        self._order: List[str] = []

    # -- ledger -----------------------------------------------------------------
    def _emit(self, kind: str, *, lane: str = "", run_id: str = "",
              parent_run_id: Optional[str] = None, subagent_run_id: Optional[str] = None,
              action: str = "", reason: str = "", ts: Optional[float] = None,
              **extra: Any) -> Dict[str, Any]:
        """Record one decision row (module ledger + optional injected sink).

        Never raises: a broken sink is logged, the row still exists.
        """
        row: Dict[str, Any] = {
            "kind": str(kind),
            "lane": str(lane or ""),
            "run_id": run_id or "",
            "parent_run_id": parent_run_id,
            "subagent_run_id": subagent_run_id,
            "action": str(action or ""),
            "reason": str(reason or ""),
            "ms": 0.0,
            "cost": 0.0,
            "ts": float(self._clock()) if ts is None else float(ts),
        }
        row.update(extra)
        try:
            LEDGER.append(row)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("orchestration ledger append failed: %s", exc)
        if self._sink is not None:
            try:
                self._sink(row)
            except Exception as exc:
                log.warning("orchestration ledger sink failed: %s", exc)
        return row

    def _refuse(self, reason: str, *, kind: str = "spawn_refused", action: str = "refuse",
                lane: str = "", run_id: str = "", parent_run_id: Optional[str] = None,
                subagent_run_id: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
        """A decision: refuse, record why, and return the typed refusal."""
        self._emit(kind, lane=lane, run_id=run_id, parent_run_id=parent_run_id,
                   subagent_run_id=subagent_run_id, action=action, reason=reason, **extra)
        return {"ok": False, "reason": reason, **extra}

    # -- vocabulary -------------------------------------------------------------
    @staticmethod
    def _checked_policy(value: Any) -> str:
        policy = str(value)
        if policy not in CLOSE_POLICIES:
            raise OrchestrationError("unknown_close_policy", policy=policy,
                                     allowed=list(CLOSE_POLICIES))
        return policy

    @staticmethod
    def _checked_persistence(value: Any) -> str:
        mode = str(value)
        if mode not in PERSISTENCE_MODES:
            raise OrchestrationError("unknown_persistence_mode", persistence=mode,
                                     allowed=list(PERSISTENCE_MODES))
        return mode

    @staticmethod
    def _inherit(parent_state: Dict[str, Any], override: Optional[Dict[str, Any]],
                 mode: str) -> Dict[str, Any]:
        """Shallow-copy state at the delegation boundary; never share the dict."""
        if mode == "stateless":
            return dict(override or {})
        inherited = {k: v for k, v in parent_state.items() if k not in _IDENTITY_KEYS}
        inherited.update(override or {})
        return inherited

    # -- runs -------------------------------------------------------------------
    def start_root(self, name: str, *, state: Optional[Dict[str, Any]] = None,
                   persistence: str = "per_invocation") -> Run:
        """Open the run everything else hangs off (no parent, no namespace)."""
        mode = self._checked_persistence(persistence)
        run_id = self._mint(str(name))
        if run_id in self._runs:
            raise OrchestrationError("duplicate_run_id", run_id=run_id)
        run = Run(run_id=run_id, name=str(name), persistence=mode,
                  state=dict(state or {}), depth=0)
        self._runs[run_id] = run
        self._order.append(run_id)
        self._emit("run_start", lane=run.name, run_id=run_id, action="start", reason="",
                   namespace="", persistence=mode)
        return run

    def open_child(self, parent_run_id: str, name: str, *, state: Optional[Dict[str, Any]] = None,
                   close_policy: str = "cancel", persistence: str = "per_invocation",
                   max_steps: Optional[int] = None):
        """Delegate: a child run with its own subagentRunId, namespace and state copy.

        Returns the :class:`Run` on success, or a typed refusal dict — delegating
        into an unknown/closed parent, past ``max_depth``, or onto a live
        ``per_thread`` namespace is a decision, not a crash.
        """
        parent = self._runs.get(str(parent_run_id))
        if parent is None:
            return self._refuse("unknown_parent", parent_run_id=str(parent_run_id),
                                lane=str(name))
        if not parent.open:
            return self._refuse("parent_not_running", parent_run_id=parent.run_id,
                                lane=str(name), status=parent.status)
        policy = self._checked_policy(close_policy)
        mode = self._checked_persistence(persistence)
        if parent.depth + 1 > self.max_depth:
            return self._refuse("max_depth_exceeded", parent_run_id=parent.run_id, lane=str(name),
                                depth=parent.depth + 1, max_depth=self.max_depth)
        if mode == "per_thread":
            for other in self.children(parent.run_id, live_only=True):
                if other.name == str(name) and other.persistence == "per_thread":
                    return self._refuse("namespace_conflict", parent_run_id=parent.run_id,
                                        lane=str(name), namespace=other.namespace,
                                        existing_run_id=other.run_id)

        subagent_run_id = self._mint("sa")
        run_id = self._mint(str(name))
        if run_id in self._runs:
            raise OrchestrationError("duplicate_run_id", run_id=run_id)
        namespace = (f"{parent.namespace}|{name}:{subagent_run_id}" if parent.namespace
                     else f"{name}:{subagent_run_id}")
        child = Run(
            run_id=run_id,
            name=str(name),
            parent_run_id=parent.run_id,
            subagent_run_id=subagent_run_id,
            namespace=namespace,
            persistence=mode,
            close_policy=policy,
            max_steps=(int(max_steps) if max_steps is not None else None),
            state=self._inherit(parent.state, state, mode),
            depth=parent.depth + 1,
        )
        self._runs[run_id] = child
        self._order.append(run_id)
        self._emit("child_spawn", lane=child.name, run_id=run_id, parent_run_id=parent.run_id,
                   subagent_run_id=subagent_run_id, action="spawn", reason="",
                   namespace=namespace, persistence=mode, close_policy=policy,
                   max_steps=child.max_steps, stateless=(mode == "stateless"))
        return child

    def get(self, run_id: str) -> Optional[Run]:
        return self._runs.get(str(run_id))

    def children(self, run_id: str, *, live_only: bool = False) -> List[Run]:
        """Direct children, in creation order."""
        run = self._require(run_id)
        kids = [self._runs[rid] for rid in self._order
                if self._runs[rid].parent_run_id == run.run_id]
        return [k for k in kids if k.open] if live_only else kids

    def descendants(self, run_id: str) -> List[Run]:
        """Every descendant, breadth-first in creation order."""
        self._require(run_id)
        out: List[Run] = []
        frontier = [str(run_id)]
        while frontier:
            current = frontier.pop(0)
            kids = [self._runs[rid] for rid in self._order
                    if self._runs[rid].parent_run_id == current]
            out.extend(kids)
            frontier.extend(k.run_id for k in kids)
        return out

    def attribute(self, run_id: str) -> Dict[str, Any]:
        """The attribution record: parent, subagentRunId, namespace and the chain."""
        run = self._require(run_id)
        chain: List[str] = []
        cursor: Optional[Run] = run
        while cursor is not None:
            chain.append(cursor.run_id)
            cursor = self._runs.get(cursor.parent_run_id) if cursor.parent_run_id else None
        chain.reverse()
        return {
            "run_id": run.run_id,
            "name": run.name,
            "parent_run_id": run.parent_run_id,
            "subagent_run_id": run.subagent_run_id,
            "namespace": run.namespace,
            "persistence": run.persistence,
            "close_policy": run.close_policy,
            "max_steps": run.max_steps,
            "status": run.status,
            "depth": run.depth,
            "chain": chain,
        }

    def _require(self, run_id: str) -> Run:
        run = self._runs.get(str(run_id))
        if run is None:
            raise OrchestrationError("unknown_run", run_id=str(run_id))
        return run

    # -- lifecycle --------------------------------------------------------------
    def close_run(self, run_id: str, *, force: bool = False,
                  policy: Optional[str] = None) -> Dict[str, Any]:
        """Close a run; children decide its outcome, never a silent drop.

        With live children and no ``force`` this refuses (``children_open``) and
        the parent stays open. With ``force`` each live child's declared policy
        (or the ``policy`` override) is applied, one ledger row per child:

        * ``cancel`` — the child is cancelled (terminal);
        * ``orphan_adopt`` — the child keeps running, detached from the dead
          parent and adopted by the runtime (``adopted_by``);
        * ``hold`` — the child is held, non-terminal and resumable.
        """
        run = self._require(run_id)
        if not run.open:
            return self._refuse("already_closed", kind="parent_close_refused",
                                action="close_refused", lane=run.name, run_id=run.run_id,
                                status=run.status)
        live = self.children(run.run_id, live_only=True)
        if live and not force:
            return self._refuse("children_open", kind="parent_close_refused",
                                action="close_refused", lane=run.name, run_id=run.run_id,
                                children=[c.run_id for c in live])

        actions: List[Dict[str, Any]] = []
        for child in live:
            chosen = self._checked_policy(child.close_policy if policy is None else policy)
            if chosen == "cancel":
                child.status = "cancelled"
                child.closed_reason = "parent_close:cancel"
            elif chosen == "orphan_adopt":
                child.origin_parent_run_id = child.parent_run_id
                child.parent_run_id = None
                child.adopted_by = ADOPTED_BY
                child.closed_reason = "parent_close:orphan_adopt"
            else:  # hold
                child.status = "held"
                child.closed_reason = "parent_close:hold"
            self._emit("child_close", lane=child.name, run_id=child.run_id,
                       parent_run_id=run.run_id, subagent_run_id=child.subagent_run_id,
                       action=chosen, reason=f"parent_close:{chosen}", policy=chosen,
                       status=child.status)
            actions.append({"run_id": child.run_id, "policy": chosen, "status": child.status})

        run.status = "closed"
        run.closed_reason = "closed"
        self._emit("run_close", lane=run.name, run_id=run.run_id, action="close",
                   reason="closed", children=[a["run_id"] for a in actions])
        return {"ok": True, "run_id": run.run_id, "closed": run.run_id, "children": actions}

    def suspend(self, run_id: str, *, reason: str = "", cascade: bool = True) -> List[Run]:
        """Suspend a run (and, by default, its live descendants).

        Descendants suspended by this call record ``suspended_by`` so that only
        they follow the same run's resume; a child paused on its own stays paused.
        """
        run = self._require(run_id)
        targets = [run] + (self.descendants(run.run_id) if cascade else [])
        touched: List[Run] = []
        for target in targets:
            if not target.open or target.status == "suspended":
                continue
            target.status = "suspended"
            target.suspended_by = None if target.run_id == run.run_id else run.run_id
            self._emit("child_suspend", lane=target.name, run_id=target.run_id,
                       parent_run_id=target.parent_run_id,
                       subagent_run_id=target.subagent_run_id,
                       action=("suspend" if target.run_id == run.run_id else "cascade_suspend"),
                       reason=str(reason or ""), suspended_by=target.suspended_by)
            touched.append(target)
        return touched

    def resume(self, run_id: str) -> List[Run]:
        """Resume a run, the descendants it suspended, and any child it holds."""
        run = self._require(run_id)
        touched: List[Run] = []
        for target in [run] + self.descendants(run.run_id):
            if target.status == "held":
                target.status = "running"
                target.closed_reason = ""
                self._emit("child_resume", lane=target.name, run_id=target.run_id,
                           parent_run_id=target.parent_run_id,
                           subagent_run_id=target.subagent_run_id,
                           action="release_hold", reason="hold_released")
                touched.append(target)
            elif target.status == "suspended":
                if target.run_id != run.run_id and target.suspended_by != run.run_id:
                    continue
                target.status = "running"
                target.suspended_by = None
                self._emit("child_resume", lane=target.name, run_id=target.run_id,
                           parent_run_id=target.parent_run_id,
                           subagent_run_id=target.subagent_run_id,
                           action="resume", reason="resumed")
                touched.append(target)
        return touched

    # -- loops ------------------------------------------------------------------
    def loop(self, name: str, *, max_iterations: Optional[int] = None,
             no_progress_after: Optional[int] = None, run_id: Optional[str] = None) -> Loop:
        """Open a loop owned by this orchestrator (optionally by one delegated run).

        Without an explicit ``max_iterations`` the loop inherits the owning run's
        declared ``max_steps`` — the delegation bound is what bounds the loop.
        """
        run = self._require(run_id) if run_id is not None else None
        if max_iterations is None:
            max_iterations = (run.max_steps if run is not None and run.max_steps
                              else DEFAULT_MAX_ITERATIONS)
        return Loop(self, name, max_iterations=max_iterations,
                    no_progress_after=no_progress_after, run=run)

    # -- handoffs ---------------------------------------------------------------
    def handoff(self, from_run_id: str, to_lane: str, *, why: str, carry: Iterable[str] = (),
                to_run_id: Optional[str] = None):
        """Move work to another agent and record the typed handoff.

        A handoff is a declared transfer: only the ``carry`` keys move, the
        receiver starts without inherited context, and an undeclared key is
        refused before anything moves (no transfer ⇒ no handoff row).
        """
        source = self._require(from_run_id)
        if not str(why or "").strip():
            raise OrchestrationError("missing_handoff_reason", to_lane=str(to_lane))
        if not source.open:
            return {"ok": False, "reason": "source_not_running", "run_id": source.run_id}
        keys = tuple(carry or ())
        for key in keys:
            if not isinstance(key, str) or not key.strip():
                raise OrchestrationError("invalid_state_key", key=repr(key))
        missing = [k for k in keys if k not in source.state]
        if missing:
            return {"ok": False, "reason": "missing_state_keys", "missing": missing}

        if to_run_id is not None:
            receiver = self._require(to_run_id)
        else:
            receiver = self.open_child(source.run_id, str(to_lane), persistence="stateless")
            if not isinstance(receiver, Run):
                return receiver  # the spawn was refused: nothing moved, no handoff row

        for key in keys:
            receiver.state[key] = source.state[key]

        ts = float(self._clock())
        record = Handoff(from_run=source.run_id, to_run=receiver.run_id,
                         to_lane=receiver.name, why=str(why).strip(),
                         carried=tuple(keys), ts=ts,
                         tool_name=f"transfer_to_{receiver.name}")
        self._emit("handoff", lane=receiver.name, run_id=receiver.run_id,
                   parent_run_id=receiver.parent_run_id,
                   subagent_run_id=receiver.subagent_run_id, action="handoff",
                   reason=record.why, ts=ts, from_run=record.from_run, to_run=record.to_run,
                   to_lane=record.to_lane, why=record.why, carried=list(record.carried),
                   tool_name=record.tool_name)
        return record
