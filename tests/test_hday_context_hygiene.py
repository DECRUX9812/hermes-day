"""TDD tests for hday_context_hygiene — restorable context reduction.

Four mechanisms, one lane:

1. **externalize-don't-delete** — a payload is never dropped, it is replaced by
   a typed pointer ``{kind, ref, sha, bytes}`` that ``rehydrate()`` can turn
   back into the original bytes; gate vetoes, failed checks, error traces and
   decisions are pinned (enforced in code, not convention).
2. **bi-temporal invalidation** — a superseded record keeps its history: the
   old row is invalidated with timestamps, never deleted or rewritten, so
   ``memory_as_of(ts)`` can still answer "what did we believe then".
3. **tool-result clearing** — large, re-fetchable tool results older than the
   last N are replaced by a compact stub + retrievable reference, behind a
   token trigger and a ``clear_at_least`` floor, never touching assistant text,
   decisions or gate events.
4. **prefix hygiene + retry budgets** — a volatile prefix (per-turn timestamp,
   counter, pid) is a typed KV-cache finding, and retries multiply across
   nested layers with counters keyed per tool and reset on success.

No network, no filesystem: every seam here is an in-process stub.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

import hday_context_hygiene as ch


@pytest.fixture(autouse=True)
def _clean_state():
    ch.reset_ledger()
    ch.reset_archive()
    ch.reset_records()
    yield
    ch.reset_ledger()
    ch.reset_archive()
    ch.reset_records()


# ---------------------------------------------------------------------------
# 1. externalize-don't-delete
# ---------------------------------------------------------------------------


def test_externalize_replaces_payload_with_working_pointer():
    payload = "".join(f"line {i}: widget output\n" for i in range(400))

    ptr = ch.externalize(payload, kind="tool_result", ref="tool_result:read_file:42")

    assert isinstance(ptr, ch.Pointer)
    assert ptr.kind == "tool_result"
    assert ptr.ref == "tool_result:read_file:42"
    assert ptr.bytes == len(payload.encode("utf-8"))
    assert ptr.sha and ptr.sha == ch.digest(payload)

    stub = ptr.line()
    # the stub is compact, one line, and carries the locator the agent re-fetches
    assert "\n" not in stub
    assert ptr.ref in stub and str(ptr.bytes) in stub
    assert len(stub) < 200

    # nothing is silently lost: the exact payload comes back
    assert ch.rehydrate(ptr) == payload
    assert any(r["event"] == "rehydrate" and r["hit"] is True for r in ch.LEDGER)
    assert ch.rehydration_stats()["tool_result"] == 1

    # a pointer whose payload was never archived is an honest miss, not a crash
    ghost = ch.Pointer(kind="url", ref="https://example.com/gone", sha="", bytes=0)
    assert ch.rehydrate(ghost) is None
    assert any(r["event"] == "rehydrate" and r["hit"] is False for r in ch.LEDGER)


def test_pinned_failures_are_never_eligible_for_clearing():
    for kind in ("gate_veto", "failed_check", "error_trace", "decision"):
        assert ch.is_pinned({"kind": kind}) is True
        with pytest.raises(ch.Pinned):
            ch.externalize("rm -rf vetoed", kind=kind, ref=f"{kind}:1")

    # and clearing skips them even when they are old, large and unpinned-looking
    transcript = [
        {"kind": "error_trace", "text": "Traceback ...\n" + "x" * 4000},
        {"kind": "gate_veto", "text": "veto: rm -rf\n" + "y" * 4000},
        {"kind": "tool_result", "tool": "read_file", "args": {"path": "a.py"}, "text": "z" * 4000},
        {"kind": "tool_result", "tool": "read_file", "args": {"path": "b.py"}, "text": "w" * 4000},
    ]
    res = ch.clear_tool_results(transcript, token_trigger=10, keep=1)
    assert res.cleared == 1
    assert res.transcript[0]["text"].startswith("Traceback")
    assert res.transcript[1]["text"].startswith("veto: rm -rf")
    assert res.transcript[2]["cleared"] is True


# ---------------------------------------------------------------------------
# 2. bi-temporal invalidation
# ---------------------------------------------------------------------------


def test_supersede_invalidates_without_deleting():
    ch.remember("the gate allows rm -rf", id="r1", at=100.0)
    ch.remember("the gate blocks rm -rf", id="r2", at=200.0)

    row = ch.supersede("r1", "r2", reason="contradicted by r2", at=250.0)

    # event time: valid_to is the new record's valid_from (not "now")
    assert row["valid_to"] == 200.0
    # ingestion time: when the system learned the old belief was superseded
    assert row["expired_at"] == 250.0
    assert row["valid_from"] == 100.0 and row["created_at"] == 100.0
    assert row["superseded_by"] == "r2"
    assert row["reason"] == "contradicted by r2"

    # default recall filters expired rows; history is still queryable
    assert [r.id for r in ch.recall()] == ["r2"]
    assert [r.id for r in ch.recall(include_expired=True)] == ["r1", "r2"]
    assert [r.id for r in ch.memory_as_of(150.0)] == ["r1"]
    assert [r.id for r in ch.memory_as_of(210.0)] == ["r2"]

    # never mutated: the superseded row still holds its original payload
    old = ch.record("r1")
    assert old.payload == "the gate allows rm -rf"
    assert old.expired_at == 250.0 and old.valid_to == 200.0

    # superseding an unknown id is an explicit error, not a silent no-op
    with pytest.raises(KeyError):
        ch.supersede("nope", "r2", reason="x")


# ---------------------------------------------------------------------------
# 3. tool-result clearing
# ---------------------------------------------------------------------------


def test_old_tool_results_are_cleared_but_decisions_and_excluded_tools_survive():
    transcript = [
        {"kind": "assistant", "text": "let me look around"},
        {"kind": "tool_result", "tool": "read_file", "args": {"path": "a.py"}, "text": "A" * 4000},
        {"kind": "decision", "text": "chose a.py"},
        {"kind": "tool_result", "tool": "vault_read", "args": {"key": "k"}, "text": "B" * 4000},
        {"kind": "gate_event", "text": "veto: rm -rf"},
        {"kind": "tool_result", "tool": "read_file", "args": {"path": "b.py"}, "text": "C" * 4000},
        {"kind": "tool_result", "tool": "read_file", "args": {"path": "c.py"}, "text": "D" * 4000},
        {"kind": "tool_result", "tool": "read_file", "args": {"path": "d.py"}, "text": "E" * 4000},
    ]

    res = ch.clear_tool_results(
        transcript, token_trigger=100, keep=3, exclude_tools=("vault_read",)
    )

    assert res.fired is True and res.reason == "cleared"
    assert res.cleared == 1 and res.saved_tokens > 0
    out = res.transcript

    # assistant text, decisions and gate events are never touched
    assert out[0]["text"] == "let me look around"
    assert out[2]["text"] == "chose a.py"
    assert out[4]["text"] == "veto: rm -rf"

    # the cleared result keeps its tool name + arguments so it can be re-fetched
    cleared = out[1]
    assert cleared["cleared"] is True
    assert cleared["tool"] == "read_file" and cleared["args"] == {"path": "a.py"}
    assert "a.py" in cleared["text"] and len(cleared["text"]) < 200
    assert ch.rehydrate(cleared["pointer"]) == "A" * 4000

    # the last N tool results survive verbatim; the excluded tool is untouched
    assert [out[i]["text"] for i in (5, 6, 7)] == ["C" * 4000, "D" * 4000, "E" * 4000]
    assert out[3]["text"] == "B" * 4000

    # append-only: the caller's transcript list is not mutated in place
    assert transcript[1]["text"] == "A" * 4000 and transcript[1].get("cleared") is None
    assert any(r["event"] == "clear" and r["tool"] == "read_file" for r in res.rows)


def test_clearing_respects_trigger_and_clear_at_least_floor():
    small = [{"kind": "tool_result", "tool": "read_file", "args": {"path": "a"}, "text": "x" * 100}]
    below = ch.clear_tool_results(small, token_trigger=10_000, keep=0)
    assert below.fired is False and below.reason == "below-trigger"
    assert below.cleared == 0 and below.transcript[0]["text"] == "x" * 100

    tiny = [
        {"kind": "tool_result", "tool": "read_file", "args": {"path": "a"}, "text": "x" * 400},
        {"kind": "tool_result", "tool": "read_file", "args": {"path": "b"}, "text": "y" * 400},
    ]
    floored = ch.clear_tool_results(tiny, token_trigger=10, keep=1, clear_at_least=10_000)
    assert floored.fired is False and floored.reason == "below-floor"
    assert floored.cleared == 0
    assert [i["text"] for i in floored.transcript] == ["x" * 400, "y" * 400]

    # the same input fires once the floor is reachable
    fired = ch.clear_tool_results(tiny, token_trigger=10, keep=1, clear_at_least=50)
    assert fired.fired is True and fired.cleared == 1 and fired.saved_tokens >= 50


# ---------------------------------------------------------------------------
# 4a. KV-cache: stable prefix + typed volatile findings
# ---------------------------------------------------------------------------


def test_prefix_hash_is_stable_across_turns_and_mask_does_not_rewrite_tools():
    system = "You are Hermes Day."
    tools = {"browser_open": {"desc": "open"}, "shell_exec": {"desc": "run"}}

    h1 = ch.prefix_hash([system, tools])
    # a new turn with the same prefix: same hash, so the provider reuses the cache
    assert ch.prefix_hash([system, tools]) == h1
    # deterministic serialization: mapping key order must not matter
    assert ch.prefix_hash([system, {"b": 1, "a": 2}]) == ch.prefix_hash([system, {"a": 2, "b": 1}])

    rep = ch.check_prefix_stability([system, tools], previous_hash=h1)
    assert rep.stable is True and rep.changed is False and rep.findings == []
    assert rep.hash == h1

    # availability is a selection-time mask: the tool block is never rewritten
    names = ["browser_open", "browser_close", "shell_exec", "vault_read"]
    mask = ch.tool_mask(names, allow=["browser_*"])
    assert mask["allowed"] == ["browser_close", "browser_open"]
    assert mask["blocked"] == ["shell_exec", "vault_read"]
    assert ch.tool_mask(names, deny=["vault_*"])["blocked"] == ["vault_read"]
    assert ch.prefix_hash([system, tools]) == h1  # nothing above touched the prefix


def test_volatile_prefix_is_reported_as_typed_finding():
    stable = ["You are Hermes Day.", {"tools": ["shell_exec"]}]
    assert ch.check_prefix_stability(stable).findings == []

    volatile = ["You are Hermes Day. [2026-09-22T22:46:31]", {"tools": ["shell_exec"]}]
    rep = ch.check_prefix_stability(volatile)
    assert rep.stable is False
    finding = rep.findings[0]
    assert isinstance(finding, ch.Finding)
    assert finding.code == "volatile-prefix"
    assert finding.severity == "high"
    assert finding.where == "block[0]"
    assert "2026-09-22T22:46:31" in finding.evidence
    assert json.loads(json.dumps(finding.to_dict()))["code"] == "volatile-prefix"

    # a per-turn counter or pid is volatile too, but a lower grade of it
    assert {f.code for f in ch.check_prefix_stability(["turn 7 of run", {}]).findings} == {
        "volatile-prefix"
    }
    assert ch.check_prefix_stability(["pid=4242", {}]).findings[0].severity == "medium"

    # a hash change with no declared reason is itself a cache-bust finding
    bust = ch.check_prefix_stability(stable, previous_hash="deadbeef")
    assert bust.changed is True and bust.stable is False
    assert [f.code for f in bust.findings] == ["prefix-changed"]

    # a declared reason silences it — the change is intentional and recorded
    declared = ch.check_prefix_stability(
        stable, previous_hash="deadbeef", declared_reason="new tool group"
    )
    assert declared.changed is True and declared.findings == []
    assert declared.declared_reason == "new tool group"


# ---------------------------------------------------------------------------
# 4b. retry budgets: per-tool counters, reset on success, multiplying layers
# ---------------------------------------------------------------------------


def test_counters_reset_on_success_and_exhaustion_raises():
    b = ch.RetryBudget(tools=1, output=1)
    b.note_failure("read_file")   # retry 1 of 1
    b.note_success("read_file")   # reset: fail/succeed alternation never exhausts
    b.note_failure("read_file")   # retry 1 of 1 again
    b.note_success("read_file")
    assert b.counts.get("read_file", 0) == 0
    assert b.remaining("read_file") == 1

    b.note_failure("grep")
    with pytest.raises(ch.RetryExhausted) as ei:
        b.note_failure("grep")    # 2 retries > budget 1
    assert ei.value.tool == "grep" and ei.value.budget == 1
    assert "grep" in str(ei.value) and "1" in str(ei.value)
    assert b.counts["grep"] == 2  # attempts are visible, not laundered

    # the "failed, do not retry" channel never consumes the budget
    b2 = ch.RetryBudget(tools=1, output=1)
    b2.note_failed("vault_read")
    b2.note_failed("vault_read")
    assert b2.counts.get("vault_read", 0) == 0 and b2.remaining("vault_read") == 1

    # a hallucinated tool name gets its own budget under the invented name
    b3 = ch.RetryBudget(tools=1, output=1)
    b3.note_failure("read_fil")
    assert "read_fil" in b3.counts and b3.counts["read_fil"] == 1

    # tools and output are budgeted separately; a bare int sets both
    b4 = ch.RetryBudget({"tools": 5, "output": 1})
    assert (b4.tools, b4.output) == (5, 1)
    assert (ch.RetryBudget(3).tools, ch.RetryBudget(3).output) == (3, 3)

    # per-tool retry counts surface in the ledger
    assert any(r["event"] == "retry" and r["tool"] == "grep" for r in ch.LEDGER)


def test_worst_case_request_count_and_refusal():
    # the worked example from the docs: output retries x SDK retries x wire attempts
    assert ch.worst_case_requests(
        turns=1, tools_per_turn=0, output_retries=2, sdk_max_retries=2, transport_attempts=2
    ) == 18
    assert ch.worst_case_seconds(
        turns=1, tools_per_turn=0, output_retries=2, sdk_max_retries=2,
        transport_attempts=2, timeout=10.0,
    ) == 180.0

    est = ch.estimate_run(
        turns=3, tools_per_turn=2, output_retries=1, sdk_max_retries=1,
        transport_attempts=1, timeout=30.0,
    )
    # N = turns x (1 + tools_per_turn) + output_retries; M = sdk_max_retries + 1; K = attempts
    assert est.requests == (3 * 3 + 1) * 2 * 1
    assert est.seconds == est.requests * 30.0
    assert est.multipliers == {"N": 10, "M": 2, "K": 1}

    # a configuration above the declared ceiling is refused before any request
    with pytest.raises(ch.BudgetRefused) as ei:
        ch.assert_within_ceiling(est, ceiling_requests=10)
    assert "10" in str(ei.value) and str(est.requests) in str(ei.value)
    assert ch.assert_within_ceiling(est, ceiling_requests=100) is est
    with pytest.raises(ch.BudgetRefused):
        ch.assert_within_ceiling(est, ceiling_requests=1000, ceiling_seconds=100.0)
