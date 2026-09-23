"""DURABLE-STATE lane — step memoization, Continue-As-New, the durability dial,
durable sleeps.

Harvested from ``runs/harvest/graph-durable.json``:
  * Inngest steps (stable step IDs + per-ID occurrence/attempt counters),
  * Temporal Continue-As-New (seal a journal segment, carry state, new
    generation under the same logical run id),
  * LangGraph durability modes (exit | async | sync as a per-run dial),
  * Inngest sleeps (a scheduled resume that holds no compute).

Every mechanism is deterministic: the clock is injected, nothing here sleeps
and nothing touches the network. Decision rows go to the ONE existing ledger
through an injected sink — this module never creates a second store.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import hday_durable as hd  # noqa: E402

_PLUGIN_ROOT = pathlib.Path(__file__).resolve().parent.parent


class Clock:
    """Injected clock — the module must never read the wall clock itself."""

    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += float(seconds)
        return self.t


# ---------------------------------------------------------------------------
# 1. Step memoization: stable step IDs, per-ID occurrence + attempt counters
# ---------------------------------------------------------------------------


def test_memo_key_is_stable_and_loop_occurrences_are_distinct():
    keys = [hd.memo_key("run-1", "fetch", i) for i in range(1, 6)]
    assert keys == [hd.memo_key("run-1", "fetch", i) for i in range(1, 6)]
    assert len(set(keys)) == 5                                   # loops stay distinct
    assert hd.memo_key("run-1", "fetch", 1) != hd.memo_key("run-2", "fetch", 1)
    assert hd.memo_key("run-1", "fetch", 1) != hd.memo_key("run-1", "other", 1)
    assert all(isinstance(k, str) and len(k) == 16 for k in keys)


def test_step_memoizes_a_loop_and_a_rerun_never_calls_the_body():
    j = hd.Journal(now=Clock())
    seen = {"n": 0}

    def body():
        seen["n"] += 1
        return seen["n"]

    first = [hd.step(j, "run-1", "tick", body) for _ in range(5)]
    assert [r.occurrence for r in first] == [1, 2, 3, 4, 5]
    assert [r.status for r in first] == ["ran"] * 5
    assert [r.result for r in first] == [1, 2, 3, 4, 5]
    assert seen["n"] == 5
    assert [r.memo_key for r in first] == [
        hd.memo_key("run-1", "tick", i) for i in range(1, 6)]

    # a re-executed pass (a resume) replays the same occurrences: all memo hits
    hd.begin_pass(j, "run-1")
    again = [hd.step(j, "run-1", "tick", body) for _ in range(5)]
    assert [r.status for r in again] == ["memoized"] * 5
    assert [r.result for r in again] == [1, 2, 3, 4, 5]
    assert seen["n"] == 5                       # the body never ran a second time
    assert all(r.attempt == 0 and r.ms == 0.0 for r in again)


def test_a_failed_step_memoizes_nothing_and_reruns_on_resume():
    j = hd.Journal(now=Clock())
    calls = []
    armed = {"yes": True}

    def body():
        calls.append(1)
        if armed["yes"]:
            raise ValueError("provider 500")
        return "ok"

    with pytest.raises(ValueError):
        hd.step(j, "run-1", "call", body)
    assert hd.memo(j, "run-1", "call", 1) is None      # nothing was memoized
    assert hd.attempts(j, "run-1", "call") == 1        # the failure is counted

    armed["yes"] = False
    hd.begin_pass(j, "run-1")                          # resume
    out = hd.step(j, "run-1", "call", body)
    assert out.status == "ran" and out.result == "ok" and out.attempt == 1
    assert hd.attempts(j, "run-1", "call") == 0        # reset when the step completes
    assert len(calls) == 2                             # the body re-ran, once

    hd.begin_pass(j, "run-1")
    assert hd.step(j, "run-1", "call", body).status == "memoized"
    assert len(calls) == 2


def test_attempt_counters_are_per_step_not_a_shared_budget():
    j = hd.Journal(now=Clock())
    flaky = {"a": 0, "b": 0}

    def a():
        flaky["a"] += 1
        if flaky["a"] < 3:
            raise RuntimeError("a flaked")
        return "a-ok"

    def b():
        flaky["b"] += 1
        if flaky["b"] < 2:
            raise RuntimeError("b flaked")
        return "b-ok"

    hd.begin_pass(j, "run-1")
    with pytest.raises(RuntimeError):
        hd.step(j, "run-1", "a", a)
    with pytest.raises(RuntimeError):
        hd.step(j, "run-1", "b", b)
    assert hd.attempts(j, "run-1", "a") == 1
    assert hd.attempts(j, "run-1", "b") == 1

    hd.begin_pass(j, "run-1")
    with pytest.raises(RuntimeError):
        hd.step(j, "run-1", "a", a)
    assert hd.step(j, "run-1", "b", b).attempt == 1
    assert hd.attempts(j, "run-1", "a") == 2            # a keeps its own budget
    assert hd.attempts(j, "run-1", "b") == 0            # b completed -> reset

    hd.begin_pass(j, "run-1")
    out = hd.step(j, "run-1", "a", a)
    assert out.attempt == 2 and out.result == "a-ok"
    assert hd.step(j, "run-1", "b", b).status == "memoized"
    assert flaky == {"a": 3, "b": 2}                    # total = sum, not min
    assert hd.attempts(j, "run-1", "a") == 0


def test_journal_persists_and_a_reloaded_run_resumes_from_the_memo(tmp_path):
    clock = Clock()
    path = tmp_path / "journal.json"
    j = hd.Journal(path, now=clock)
    hd.open_run(j, "run-1")
    assert hd.step(j, "run-1", "fetch", lambda: {"v": 7}).result == {"v": 7}
    assert j.save() is True

    k = hd.Journal(path, now=clock)
    assert k.load() is True
    hd.begin_pass(k, "run-1")
    out = hd.step(k, "run-1", "fetch", lambda: pytest.fail("body must not re-run"))
    assert out.status == "memoized" and out.result == {"v": 7}


# ---------------------------------------------------------------------------
# 2. Continue-As-New: seal the segment, carry state, new generation
# ---------------------------------------------------------------------------


def test_rollover_seals_the_segment_and_starts_a_new_generation():
    clock = Clock()
    j = hd.Journal(now=clock)
    hd.open_run(j, "run-1", state={"seen": 0})
    for i in range(6):
        if i == 3:
            hd.set_state(j, "run-1", {"seen": 7})
        hd.append_row(j, "run-1", {"delta": i}, budget_rows=3)

    segs = hd.chain(j, "run-1")
    assert [s["generation"] for s in segs] == [0, 1]         # exactly one rollover
    assert segs[0]["sealed"] is True
    assert segs[0]["rows_count"] == 3                        # sealed at the budget
    assert segs[1]["rows_count"] == 3
    assert segs[1]["prev"] == segs[0]["id"]                  # continuation pointer
    assert segs[1]["rows"][0]["prev"] == segs[0]["id"]       # the new segment's first row points back
    assert segs[1]["state"] == {"seen": 7}                   # state carried forward
    assert j.run("run-1")["generation"] == 1
    assert j.run("run-1")["run_id"] == "run-1"               # logical run id unchanged
    assert all(r["run"] == "run-1" for s in segs for r in s["rows"])
    assert hd.chain_state(j, "run-1") == {"seen": 7}


def test_chain_replay_matches_an_unrolled_run_of_the_same_inputs():
    def fold_row(acc, row):
        if "delta" in row:
            acc = {"total": acc["total"] + row["delta"], "n": acc["n"] + 1}
        return acc

    rows = [{"delta": i} for i in range(1, 8)]

    rolled = hd.Journal(now=Clock())
    hd.open_run(rolled, "rolled", state={"total": 0, "n": 0})
    for row in rows:
        hd.append_row(rolled, "rolled", row, budget_rows=2)

    flat = hd.Journal(now=Clock())
    hd.open_run(flat, "flat", state={"total": 0, "n": 0})
    for row in rows:
        hd.append_row(flat, "flat", row)

    assert len(hd.chain(rolled, "rolled")) > 1                # the chain really rolled
    assert len(hd.chain(flat, "flat")) == 1
    init = {"total": 0, "n": 0}
    assert hd.fold(rolled, "rolled", fold_row, dict(init)) == \
        hd.fold(flat, "flat", fold_row, dict(init)) == {"total": 28, "n": 7}


# ---------------------------------------------------------------------------
# 3. The durability dial: a typed judgment, honoured by the writer
# ---------------------------------------------------------------------------


def test_durability_is_a_typed_judgment_not_a_boolean():
    for kwargs, mode in (
        ({"irreversible": True}, "sync"),
        ({"risk": 0.9}, "sync"),
        ({"read_only": True}, "exit"),
        ({}, "async"),
    ):
        d = hd.judge_durability(**kwargs)
        assert isinstance(d, hd.Durability) and not isinstance(d, bool)
        assert d.mode == mode and d.mode in hd.DURABILITIES
        assert d.reason.strip() and d.reason != d.mode
        assert d.cost_class in hd.COST_CLASSES
        assert d.latency_class in hd.LATENCY_CLASSES
        assert d.evidence["irreversible"] == bool(kwargs.get("irreversible"))
        assert d.evidence["read_only"] == bool(kwargs.get("read_only"))

    # the reason names the input that decided it, and irreversible wins ties
    assert "irreversible" in hd.judge_durability(irreversible=True).reason
    assert "read-only" in hd.judge_durability(read_only=True).reason
    assert hd.judge_durability(read_only=True, risk=0.4).mode == "exit"
    assert hd.judge_durability(read_only=True, irreversible=True).mode == "sync"

    row = hd.judge_durability(risk=0.2, run="run-1").as_row()
    assert row["kind"] == "durable_mode" and row["mode"] == "async"
    assert row["lane"] == "local" and row["cost"] == 0.0


def test_sync_mode_persists_before_the_next_step():
    written = []
    w = hd.DurableWriter(written.extend, "sync")
    w.append({"step": "a"})
    assert written == [{"step": "a"}]            # on disk before the next step starts
    w.append({"step": "b"})
    assert [r["step"] for r in written] == ["a", "b"]
    assert w.flushes == 2
    w.close()
    assert w.flushes == 2                        # nothing left to flush at run end


def test_async_mode_flushes_at_step_boundaries():
    written = []
    w = hd.DurableWriter(written.extend, "async")
    w.append({"step": "a"})
    assert written == []                         # buffered while the step runs
    w.step_boundary()
    assert [r["step"] for r in written] == ["a"] and w.flushes == 1
    w.append({"step": "b"})
    w.close()
    assert [r["step"] for r in written] == ["a", "b"] and w.flushes == 2


def test_exit_mode_flushes_exactly_once_at_run_end():
    written = []
    w = hd.DurableWriter(written.extend, "exit")
    for name in ("a", "b", "c"):
        w.append({"step": name})
        w.step_boundary()
    assert written == []                         # nothing persisted mid-run
    w.close()
    assert [r["step"] for r in written] == ["a", "b", "c"]
    assert w.flushes == 1
    w.close()
    assert w.flushes == 1                        # close is idempotent


def test_a_crash_loses_only_the_unflushed_window():
    # sync: the last completed step is readable after a crash
    written = []
    w = hd.DurableWriter(written.extend, "sync")
    w.append({"step": "a"})
    crash = w.crash()
    assert [r["step"] for r in written] == ["a"]
    assert crash["readable"] == 1 and crash["lost"] == 0

    # exit: a crash before run end leaves nothing
    lost_sink = []
    w2 = hd.DurableWriter(lost_sink.extend, "exit")
    w2.append({"step": "a"})
    crash2 = w2.crash()
    assert lost_sink == []
    assert crash2["readable"] == 0 and crash2["lost"] == 1


def test_a_failing_sink_is_surfaced_not_swallowed():
    class FlakySink:
        def __init__(self, fail_after):
            self.rows, self.fail_after, self.calls = [], fail_after, 0

        def __call__(self, rows):
            self.calls += 1
            if self.calls > self.fail_after:
                raise OSError("disk full")
            self.rows.extend(rows)

    sink = FlakySink(fail_after=1)
    w = hd.DurableWriter(sink, "sync")
    w.append({"step": "a"})
    with pytest.raises(hd.DurabilityWriteError) as err:
        w.append({"step": "b"})
    assert err.value.mode == "sync" and err.value.flushes == 1
    assert w.pending() == 1                       # the unflushed row is held, not dropped
    assert [r["step"] for r in sink.rows] == ["a"]


# ---------------------------------------------------------------------------
# 4. Durable sleeps: a scheduled resume that holds no compute
# ---------------------------------------------------------------------------


def test_a_durable_sleep_returns_at_once_and_resumes_exactly_once():
    clock = Clock(1_700_000_000.0)
    rows = []
    j = hd.Journal(now=clock, ledger=rows.append)
    hd.open_run(j, "run-1")

    started = time.perf_counter()
    wake = hd.sleep_until(j, "run-1", clock() + 2 * 3600, reason="rate limit",
                          resume_token="tok-1", step_id="poll")
    elapsed = time.perf_counter() - started
    assert elapsed < 0.05                        # no process is held during the wait
    assert wake["resume_token"] == "tok-1" and wake["at"] == clock() + 7200
    assert hd.wait_state(j, "run-1") == "waiting"
    assert hd.scan_due(j, clock()) == []         # not due yet
    assert hd.resume_due(j, clock()) == []

    clock.advance(7200)
    due = hd.scan_due(j, clock())
    assert [w["resume_token"] for w in due] == ["tok-1"]
    resumed = hd.resume_due(j, clock())
    assert len(resumed) == 1 and resumed[0]["resume_token"] == "tok-1"
    assert hd.wait_state(j, "run-1") == "running"
    assert hd.resume_due(j, clock()) == []       # exactly one resume per wake

    wait_rows = [r for r in rows if r["kind"] == "durable_sleep"]
    assert [r["status"] for r in wait_rows] == ["scheduled", "resumed"]
    assert wait_rows[0]["wait_s"] == 7200 and wait_rows[1]["actual_s"] == 7200
    assert wait_rows[1]["planned_s"] == 7200 and wait_rows[1]["lateness_s"] == 0.0
    assert all(r["lane"] == "local" and r["cost"] == 0.0 for r in wait_rows)


def test_a_late_wake_records_the_lateness():
    clock = Clock(1000.0)
    rows = []
    j = hd.Journal(now=clock, ledger=rows.append)
    hd.open_run(j, "run-1")
    hd.sleep_until(j, "run-1", 1060.0, reason="backoff", resume_token="tok")
    clock.advance(600)                           # woke 540s late
    out = hd.resume_due(j, clock())
    assert out[0]["lateness_s"] == 540.0
    assert rows[-1]["lateness_s"] == 540.0


# ---------------------------------------------------------------------------
# Ledger: rows land on the ONE existing decision ledger
# ---------------------------------------------------------------------------


def test_durable_rows_land_on_the_one_decision_ledger(day):
    module, ctx, sid = day
    clock = Clock()
    sink = module._durable_sink(sid)
    j = hd.Journal(now=clock, ledger=sink)
    hd.open_run(j, "run-1")
    hd.step(j, "run-1", "noop", lambda: 1)
    hd.sleep_until(j, "run-1", clock() + 60, reason="wait for a human",
                   resume_token="tok", step_id="approve")

    rows = module._SESSIONS[sid]["gate"]          # the SAME ledger the gate writes
    assert [r["kind"] for r in rows] == ["durable_step", "durable_sleep"]
    assert rows[0]["step"] == "noop" and rows[0]["status"] == "ran"
    assert rows[1]["resume_token"] == "tok" and rows[1]["wait_s"] == 60.0
    assert all(r["lane"] == "local" and r["cost"] == 0.0 for r in rows)
    assert all("ts" in r and "reason" in r for r in rows)
    # the loader finds the sibling module; no second row store was created
    assert module._load_hday_durable() is not None
    assert not hasattr(module, "_DURABLE_ROWS")


def test_import_is_standalone():
    """Import-safe: works from the plugin root with no env, no side effects."""
    out = subprocess.run(
        [sys.executable, "-c", "import hday_durable; print('ok')"],
        cwd=_PLUGIN_ROOT, capture_output=True, text=True, timeout=60,
        env={"PATH": os.environ.get("PATH", "")},
    )
    assert out.returncode == 0, out.stderr
