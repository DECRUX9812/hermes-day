"""Tests for hday_orchestration — the orchestration lane (harvest, orchestration theme).

Four mechanisms, each grounded in a fetched source (runs/harvest/):

1. Child-run attribution — a delegated child carries ``parent_run_id`` plus a
   freshly minted ``subagent_run_id`` (never the agent name), a checkpoint_ns-
   shaped namespace, and a state copy taken at the delegation boundary.
   Sources: AG-UI subagents (subagentRunId attribution and suspension),
   LangGraph subgraph namespacing, Mastra delegation hooks (copied request
   context, no leak back to the parent).
2. Child-workflow close policy — a parent cannot close while children are open,
   and an explicit per-child policy (``cancel`` | ``orphan_adopt`` | ``hold``)
   decides what happens to a child when its parent dies, one ledger row per
   child. Source: Temporal child workflows.
3. Loop termination owned by the loop — an iteration budget plus a no-progress
   detector; termination is a typed reason (``escalated:<reason>`` |
   ``max_iterations`` | ``no_progress:<key>`` | ``error:<class>``), never a
   silent spin. Source: Google ADK LoopAgent (exit_loop escalation pattern).
4. Handoff records — a typed handoff (from, to, why, carried state keys) written
   to the ledger when work moves between agents; an undeclared key is refused
   without a transfer. Sources: openai-agents typed handoff tool, ADK
   output_key scoped state.

Deterministic and offline: the clock and the id minter are injected, and child
execution is a stub callable — no subprocess, no network, no threads.
"""

from __future__ import annotations

import json
import os
import sys
from itertools import count

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hday_orchestration as ho  # noqa: E402


class FakeClock:
    """Injected clock: every ledger row's ``ts`` is reproducible."""

    def __init__(self, start: float = 1_000.0, step: float = 0.25) -> None:
        self.now = float(start)
        self.step = float(step)

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def counter_mint():
    """Deterministic id minter: ``hint-1``, ``hint-2``, ... per orchestrator."""
    seq = count(1)
    return lambda hint: f"{hint}-{next(seq)}"


def make(**kwargs) -> "ho.Orchestrator":
    kwargs.setdefault("clock", FakeClock())
    kwargs.setdefault("mint", counter_mint())
    return ho.Orchestrator(**kwargs)


@pytest.fixture(autouse=True)
def _clean_ledger():
    ho.reset_ledger()
    yield
    ho.reset_ledger()


def rows(kind=None):
    return [r for r in ho.LEDGER if kind is None or r["kind"] == kind]


# ---------------------------------------------------------------------------
# 1. child-run attribution
# ---------------------------------------------------------------------------


def test_child_run_carries_parent_run_id_and_a_fresh_subagent_run_id():
    orch = make()
    parent = orch.start_root("coordinator")
    first = orch.open_child(parent.run_id, "critic", max_steps=2)
    second = orch.open_child(parent.run_id, "critic")

    assert isinstance(first, ho.Run) and isinstance(second, ho.Run)
    assert first.parent_run_id == parent.run_id == second.parent_run_id
    assert first.name == second.name == "critic"            # display name only
    assert first.subagent_run_id and first.subagent_run_id != "critic"
    assert first.subagent_run_id != second.subagent_run_id  # two invocations, two ids
    assert first.run_id != second.run_id

    attr = orch.attribute(first.run_id)
    assert attr["parent_run_id"] == parent.run_id
    assert attr["subagent_run_id"] == first.subagent_run_id
    assert attr["chain"] == [parent.run_id, first.run_id]
    assert attr["max_steps"] == 2

    spawn = rows("child_spawn")
    assert len(spawn) == 2
    assert [r["subagent_run_id"] for r in spawn] == [first.subagent_run_id, second.subagent_run_id]
    assert {r["parent_run_id"] for r in spawn} == {parent.run_id}
    assert {r["run_id"] for r in spawn} == {first.run_id, second.run_id}


def test_child_state_is_copied_at_the_boundary_and_never_leaks_back():
    orch = make()
    parent = orch.start_root(
        "coordinator", state={"brief": "ship it", "draft": "", "run_id": "parent-identity"})
    child = orch.open_child(parent.run_id, "writer", state={"draft": "v1"})

    assert child.state["brief"] == "ship it"      # inherited (copied, not shared)
    assert child.state["draft"] == "v1"
    assert child.state is not parent.state
    assert "run_id" not in child.state            # run-scoped identity stays out of the copy

    child.state["draft"] = "child-only"
    child.state["leaked"] = True
    assert parent.state["draft"] == ""            # a child write never reaches the parent
    assert "leaked" not in parent.state

    stateless = orch.open_child(parent.run_id, "plain_call", persistence="stateless")
    assert stateless.state == {}                  # stateless: no inheritance at all


def test_nested_namespaces_join_with_a_pipe_and_siblings_keep_separate_state():
    orch = make()
    root = orch.start_root("root")
    a = orch.open_child(root.run_id, "lane_a", state={"k": "a"})
    b = orch.open_child(root.run_id, "lane_b", state={"k": "b"})
    inner = orch.open_child(a.run_id, "lane_a_inner", state={"k": "inner"})

    assert root.namespace == ""
    assert a.namespace == f"lane_a:{a.subagent_run_id}"
    assert inner.namespace == f"{a.namespace}|lane_a_inner:{inner.subagent_run_id}"
    assert a.depth == 1 and inner.depth == 2

    assert (a.state["k"], b.state["k"], inner.state["k"]) == ("a", "b", "inner")
    assert a.state is not b.state and b.state is not inner.state
    assert orch.attribute(inner.run_id)["chain"] == [root.run_id, a.run_id, inner.run_id]


def test_a_second_per_thread_child_of_one_lane_is_a_typed_namespace_conflict():
    orch = make()
    root = orch.start_root("root")
    first = orch.open_child(root.run_id, "solo", persistence="per_thread")
    before = len(ho.LEDGER)

    refused = orch.open_child(root.run_id, "solo", persistence="per_thread")

    assert isinstance(refused, dict) and refused["ok"] is False
    assert refused["reason"] == "namespace_conflict"
    assert refused["namespace"] == first.namespace
    assert orch.children(root.run_id) == [first]        # no partial run was created
    assert len(ho.LEDGER) == before + 1
    assert ho.LEDGER[-1]["kind"] == "spawn_refused"
    assert ho.LEDGER[-1]["reason"] == "namespace_conflict"
    # the same lane as a per_invocation child is fine — the conflict is per-thread only
    assert isinstance(orch.open_child(root.run_id, "solo"), ho.Run)


# ---------------------------------------------------------------------------
# 2. child-workflow close policy
# ---------------------------------------------------------------------------


def test_parent_cannot_close_while_children_are_open():
    orch = make()
    parent = orch.start_root("coordinator")
    child = orch.open_child(parent.run_id, "worker")

    refused = orch.close_run(parent.run_id)

    assert refused["ok"] is False and refused["reason"] == "children_open"
    assert refused["children"] == [child.run_id]
    assert orch.get(parent.run_id).status == "running"
    assert orch.get(child.run_id).status == "running"
    assert rows("run_close") == []                       # nothing closed at all
    assert rows("parent_close_refused")[-1]["children"] == [child.run_id]


def test_close_policy_decides_each_childs_fate_with_exactly_one_row_each():
    orch = make()
    parent = orch.start_root("coordinator")
    cancelled = orch.open_child(parent.run_id, "cancelled_one", close_policy="cancel")
    adopted = orch.open_child(parent.run_id, "adopted_one", close_policy="orphan_adopt")
    held = orch.open_child(parent.run_id, "held_one", close_policy="hold")

    result = orch.close_run(parent.run_id, force=True)

    assert result["ok"] is True
    assert orch.get(parent.run_id).status == "closed"
    assert orch.get(cancelled.run_id).status == "cancelled"
    assert orch.get(held.run_id).status == "held"

    adopted_run = orch.get(adopted.run_id)
    assert adopted_run.status == "running"               # orphan-adopt: still alive
    assert adopted_run.parent_run_id is None             # re-attached at the root
    assert adopted_run.adopted_by == ho.ADOPTED_BY
    assert adopted_run.origin_parent_run_id == parent.run_id

    for run, policy in ((cancelled, "cancel"), (adopted, "orphan_adopt"), (held, "hold")):
        policy_rows = [r for r in rows("child_close") if r["run_id"] == run.run_id]
        assert len(policy_rows) == 1                     # exactly one row per child
        assert policy_rows[0]["action"] == policy
        assert policy_rows[0]["reason"] == f"parent_close:{policy}"
        assert policy_rows[0]["parent_run_id"] == parent.run_id
        assert policy_rows[0]["subagent_run_id"] == run.subagent_run_id

    # an adopted child is an ordinary live run again: it can be closed on its own
    assert orch.close_run(adopted.run_id)["ok"] is True
    assert orch.get(adopted.run_id).status == "closed"

    # closing an already-closed run is a typed refusal, not a second policy pass
    before = len(ho.LEDGER)
    again = orch.close_run(parent.run_id)
    assert again["ok"] is False and again["reason"] == "already_closed"
    assert len(ho.LEDGER) == before + 1


def test_a_held_child_is_non_terminal_until_it_is_released():
    orch = make()
    parent = orch.start_root("coordinator")
    held = orch.open_child(parent.run_id, "held_one", close_policy="hold")
    orch.close_run(parent.run_id, force=True)

    assert orch.get(held.run_id).status == "held"
    assert orch.get(held.run_id).open is True            # held is alive, not terminal
    assert orch.children(held.run_id) == []

    released = orch.resume(held.run_id)
    assert [r.run_id for r in released] == [held.run_id]
    assert orch.get(held.run_id).status == "running"
    assert rows("child_resume")[-1]["action"] == "release_hold"
    assert orch.close_run(held.run_id)["ok"] is True


def test_suspend_cascades_to_descendants_and_resume_follows_the_same_set():
    orch = make()
    root = orch.start_root("root")
    mid = orch.open_child(root.run_id, "mid")
    leaf = orch.open_child(mid.run_id, "leaf")

    suspended = orch.suspend(mid.run_id, reason="waiting_on_human")

    assert {r.run_id for r in suspended} == {mid.run_id, leaf.run_id}
    assert orch.get(mid.run_id).status == "suspended"
    assert orch.get(leaf.run_id).status == "suspended"
    assert orch.get(leaf.run_id).suspended_by == mid.run_id
    assert orch.get(root.run_id).status == "running"     # ancestors are untouched

    suspend_rows = rows("child_suspend")
    assert {r["run_id"] for r in suspend_rows} == {mid.run_id, leaf.run_id}
    assert {r["reason"] for r in suspend_rows} == {"waiting_on_human"}
    assert [r["parent_run_id"] for r in suspend_rows if r["run_id"] == leaf.run_id] == [mid.run_id]

    resumed = orch.resume(mid.run_id)
    assert {r.run_id for r in resumed} == {mid.run_id, leaf.run_id}
    assert orch.get(leaf.run_id).status == "running"
    assert orch.get(leaf.run_id).suspended_by is None


def test_a_directly_suspended_child_does_not_follow_the_parents_resume():
    orch = make()
    root = orch.start_root("root")
    child = orch.open_child(root.run_id, "child")

    orch.suspend(child.run_id, reason="local_pause")
    orch.suspend(root.run_id, reason="parent_pause")

    assert orch.get(child.run_id).suspended_by is None    # not re-stamped by the cascade
    assert orch.get(root.run_id).suspended_by is None

    orch.resume(root.run_id)
    assert orch.get(root.run_id).status == "running"
    assert orch.get(child.run_id).status == "suspended"   # still its own pause

    orch.resume(child.run_id)
    assert orch.get(child.run_id).status == "running"


# ---------------------------------------------------------------------------
# 3. loop termination owned by the loop
# ---------------------------------------------------------------------------


def test_loop_escalation_terminates_with_a_typed_reason_and_one_row_per_iteration():
    orch = make()
    parent = orch.start_root("coordinator")
    child = orch.open_child(parent.run_id, "writer")
    loop = orch.loop("writer_critic", max_iterations=9, run_id=child.run_id)

    def critic(iteration):
        iteration.observe({"draft": f"v{iteration.index}"}, key="draft")
        if iteration.index == 3:
            iteration.escalate("quality_ok")

    result = loop.run(critic)

    assert result.iterations == 3
    assert result.terminated_by == "escalated"
    assert result.reason == "escalated:quality_ok"
    assert result.warning is False

    iterations = [r for r in rows("loop_iteration") if r["loop"] == "writer_critic"]
    assert [r["iteration"] for r in iterations] == [1, 2, 3]
    assert all(r["run_id"] == child.run_id for r in iterations)
    assert all(r["subagent_run_id"] == child.subagent_run_id for r in iterations)
    assert iterations[-1]["escalated"] is True
    assert iterations[-1]["escalation_reason"] == "quality_ok"

    terminal = rows("loop_terminated")[-1]
    assert terminal["reason"] == "escalated:quality_ok"
    assert terminal["warning"] is False
    assert terminal["iterations"] == 3


def test_loop_stops_at_max_iterations_with_a_typed_warning_not_a_silent_truncation():
    orch = make()
    calls = []

    def never_escalates(iteration):
        calls.append(iteration.index)
        iteration.observe({"draft": f"v{iteration.index}"}, key="draft")

    loop = orch.loop("bounded", max_iterations=2)
    result = loop.run(never_escalates)

    assert result.iterations == 2 and calls == [1, 2]    # the budget belongs to the loop
    assert result.terminated_by == "max_iterations"
    assert result.reason == "max_iterations"
    assert result.warning is True
    terminal = rows("loop_terminated")[-1]
    assert terminal["reason"] == "max_iterations" and terminal["warning"] is True

    # a delegated child's declared max_steps bounds the loop it owns
    parent = orch.start_root("coordinator")
    child = orch.open_child(parent.run_id, "bounded_worker", max_steps=2)
    bounded = orch.loop("child_loop", run_id=child.run_id)
    assert bounded.max_iterations == 2
    second = bounded.run(never_escalates)
    assert second.reason == "max_iterations" and second.iterations == 2


def test_no_progress_detector_escalates_with_a_typed_reason_instead_of_spinning():
    orch = make()
    calls = []
    loop = orch.loop("spin_guard", max_iterations=50, no_progress_after=3)

    def stuck(iteration):
        calls.append(iteration.index)
        iteration.observe({"draft": "unchanged"}, key="draft")

    result = loop.run(stuck)

    assert result.terminated_by == "no_progress"
    assert result.reason == "no_progress:draft"          # typed, names the stalled key
    assert result.iterations == 3 and len(calls) == 3     # bounded: it did not spin
    assert result.warning is True
    terminal = rows("loop_terminated")[-1]
    assert terminal["reason"] == "no_progress:draft" and terminal["warning"] is True
    digests = [r["digest"] for r in rows("loop_iteration")]
    assert len(digests) == 3 and len(set(digests)) == 1

    # a loop whose state actually moves never trips the detector
    progressing = orch.loop("progressing", max_iterations=4, no_progress_after=2)
    moved = progressing.run(lambda it: it.observe({"draft": f"v{it.index}"}, key="draft"))
    assert moved.terminated_by == "max_iterations" and moved.iterations == 4


def test_a_step_error_is_recorded_as_a_typed_reason_then_propagates():
    orch = make()
    loop = orch.loop("fragile", max_iterations=5)

    def breaks(iteration):
        iteration.observe({"draft": f"v{iteration.index}"}, key="draft")
        if iteration.index == 2:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        loop.run(breaks)

    assert [r["iteration"] for r in rows("loop_iteration")] == [1, 2]
    terminal = rows("loop_terminated")[-1]
    assert terminal["reason"] == "error:RuntimeError"
    assert terminal["warning"] is True
    assert terminal["iterations"] == 2


def test_escalation_requires_a_typed_reason_and_rejects_a_blank():
    orch = make()
    loop = orch.loop("strict", max_iterations=3)

    with pytest.raises(ho.OrchestrationError) as caught:
        loop.run(lambda iteration: iteration.escalate("   "))

    assert caught.value.reason == "empty_escalation_reason"
    assert rows("loop_terminated")[-1]["reason"] == "error:OrchestrationError"


# ---------------------------------------------------------------------------
# 4. handoff records
# ---------------------------------------------------------------------------


def test_handoff_record_is_typed_and_written_to_the_ledger():
    orch = make()
    source = orch.start_root(
        "coordinator", state={"draft": "v1", "notes": "n", "internal": "secret"})

    record = orch.handoff(source.run_id, "reviewer", why="needs_review", carry=("draft",))

    assert isinstance(record, ho.Handoff)
    assert record.from_run == source.run_id
    assert record.to_lane == "reviewer"
    assert record.why == "needs_review"
    assert record.carried == ("draft",)
    assert record.tool_name == "transfer_to_reviewer"    # a rendered tool, not a function call

    receiver = orch.get(record.to_run)
    assert receiver.name == "reviewer"
    assert receiver.parent_run_id == source.run_id
    assert receiver.state["draft"] == "v1"               # the declared key moved
    assert "internal" not in receiver.state              # undeclared state did not
    assert "notes" not in receiver.state

    row = rows("handoff")[-1]
    assert row["from_run"] == source.run_id and row["to_run"] == record.to_run
    assert row["to_lane"] == "reviewer" and row["why"] == "needs_review"
    assert row["carried"] == ["draft"]
    assert row["tool_name"] == "transfer_to_reviewer"
    assert row["parent_run_id"] == source.run_id
    assert row["subagent_run_id"] == receiver.subagent_run_id
    assert row["ts"] == record.ts


def test_handoff_with_an_undeclared_state_key_is_refused_without_a_transfer():
    orch = make()
    source = orch.start_root("coordinator", state={"draft": "v1"})
    before = len(ho.LEDGER)

    refused = orch.handoff(source.run_id, "reviewer", why="needs_review",
                           carry=("draft", "missing_key"))

    assert refused["ok"] is False and refused["reason"] == "missing_state_keys"
    assert refused["missing"] == ["missing_key"]
    assert len(ho.LEDGER) == before                      # no handoff row: nothing moved
    assert rows("handoff") == []
    assert orch.children(source.run_id) == []            # and no receiver run was spawned


def test_declared_vocabulary_is_enforced_at_the_boundary():
    orch = make()
    root = orch.start_root("root")

    with pytest.raises(ho.OrchestrationError) as bad_policy:
        orch.open_child(root.run_id, "worker", close_policy="terminate")
    assert bad_policy.value.reason == "unknown_close_policy"
    assert set(bad_policy.value.detail["allowed"]) == set(ho.CLOSE_POLICIES)
    assert ho.CLOSE_POLICIES == ("cancel", "orphan_adopt", "hold")

    with pytest.raises(ho.OrchestrationError) as bad_why:
        orch.handoff(root.run_id, "reviewer", why="  ", carry=())
    assert bad_why.value.reason == "missing_handoff_reason"

    with pytest.raises(ho.OrchestrationError) as bad_budget:
        orch.loop("nope", max_iterations=0)
    assert bad_budget.value.reason == "invalid_iteration_budget"

    assert orch.children(root.run_id) == []              # a refusal leaves no run behind


# ---------------------------------------------------------------------------
# determinism (the repo's acceptance bar: a deterministic offline path)
# ---------------------------------------------------------------------------


def test_the_whole_lane_is_deterministic_with_an_injected_clock_and_minter():
    def drive() -> str:
        sink: list = []
        orch = make(ledger=sink.append)
        parent = orch.start_root("coordinator", state={"draft": "v1"})
        child = orch.open_child(parent.run_id, "writer", close_policy="hold")
        loop = orch.loop("writer_critic", max_iterations=2, run_id=child.run_id)
        loop.run(lambda iteration: iteration.observe({"draft": "same"}, key="draft"))
        orch.handoff(parent.run_id, "reviewer", why="needs_review", carry=("draft",))
        orch.close_run(parent.run_id, force=True)
        return json.dumps(sink, sort_keys=True)

    first, second = drive(), drive()

    assert first == second
    assert "handoff" in first and "loop_terminated" in first and "child_close" in first
