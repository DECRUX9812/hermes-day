"""TDD tests for the unified pre_tool_call gate wiring (Phase 1).

``hday_gate.decide`` is the two-phase decision (regex deny-list first, typed
judge second); these tests drive the WIRED plugin hook — ``_pre_tool_call`` —
with the judge injected through the module-level ``_GATE_JUDGE`` seam, so no
network and no sibling plugin are needed.

Contract under test (from the plan):
    regex veto first -> if borderline/irreversible -> typed gate -> allow or
    escalate to the human. One decision ledger (rec["gate"]) with rows
    ``regex_hit`` | ``judged`` | ``cached`` | ``unjudged``. Fail-open loudly:
    no judge / raising judge never blocks and leaves an ``unjudged`` mark —
    but only when the typed phase was actually needed (the hot path stays
    free for benign calls).
"""

from __future__ import annotations

import pytest

from conftest import _fire  # noqa: F401  (shared fixtures)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _judge(**signals):
    base = {"irreversible": 0.0, "outer_scope": 0.0, "intent_consistent": 1.0,
            "data_egress": 0.0, "blast_radius": "local", "lane": "hosted",
            "cost": 0.0001}
    base.update(signals)
    return lambda action, request, context: dict(base)


def _set_judge(module, monkeypatch, judge):
    monkeypatch.setattr(module, "_GATE_JUDGE", judge)
    # never let the test outcome depend on the real approvals.mode/yolo state
    monkeypatch.setattr(module, "_gate_escalation_available", lambda: True)


def _call(module, sid, cmd, tool="terminal", **extra):
    args = {"command": cmd}
    args.update(extra)
    return module._pre_tool_call(tool_name=tool, args=args, session_id=sid)


def _rows(module, sid):
    return module._SESSIONS[sid]["gate"]


# ---------------------------------------------------------------------------
# the plan's TDD cases
# ---------------------------------------------------------------------------


def test_irreversible_on_mandate_passes_with_judged_row(day, monkeypatch):
    """rm -rf build/ is irreversible but on-mandate -> allowed, `judged` row."""
    module, ctx, sid = day
    _set_judge(module, monkeypatch,
               _judge(irreversible=0.9, outer_scope=0.1, data_egress=0.0,
                      intent_consistent=0.95, blast_radius="workspace"))
    out = _call(module, sid, "rm -rf build/")
    assert out is None, f"on-mandate action must proceed: {out}"
    rows = _rows(module, sid)
    assert len(rows) == 1
    assert rows[0]["kind"] == "judged"
    assert rows[0]["allow"] is True


def test_repeat_action_is_cached_without_a_second_paid_call(day, monkeypatch):
    module, ctx, sid = day
    calls = []

    def judge(a, u, c):
        calls.append(a)
        return {"irreversible": 0.9, "intent_consistent": 0.95,
                "blast_radius": "workspace", "lane": "hosted", "cost": 0.0001}

    _set_judge(module, monkeypatch, judge)
    assert _call(module, sid, "rm -rf build/") is None
    assert _call(module, sid, "rm -rf build/") is None
    assert len(calls) == 1, "a repeat must not buy a second judgment"
    rows = _rows(module, sid)
    assert [r["kind"] for r in rows] == ["judged", "cached"]
    assert rows[1]["allow"] is True


def test_judge_failure_marks_unjudged_and_does_not_block(day, monkeypatch):
    module, ctx, sid = day

    def boom(a, u, c):
        raise TimeoutError("judge timed out")

    _set_judge(module, monkeypatch, boom)
    out = _call(module, sid, "rm -rf build/")
    assert out is None, "a judge failure must fail open to stock behaviour"
    rows = _rows(module, sid)
    assert rows[-1]["kind"] == "unjudged"
    assert rows[-1]["allow"] is True


def test_no_judge_marks_unjudged_only_when_typed_phase_needed(day, monkeypatch):
    """No key/judge -> `unjudged` on borderline calls, silence on benign ones."""
    module, ctx, sid = day
    _set_judge(module, monkeypatch, None)
    assert _call(module, sid, "pytest tests/ -q") is None
    assert _rows(module, sid) == [], "benign calls must stay off the ledger"
    assert _call(module, sid, "sudo rm -rf /var/lib/x") is None
    rows = _rows(module, sid)
    assert len(rows) == 1 and rows[0]["kind"] == "unjudged"


# ---------------------------------------------------------------------------
# ordering: regex vetoes run first and are FREE
# ---------------------------------------------------------------------------


def test_regex_hit_blocks_and_never_calls_judge(day, monkeypatch):
    module, ctx, sid = day
    calls = []

    def judge(a, u, c):
        calls.append(a)
        return {}

    _set_judge(module, monkeypatch, judge)
    out = _call(module, sid, "rm -rf tests/")
    assert isinstance(out, dict) and out["action"] == "block"
    assert calls == [], "regex veto must short-circuit before any paid call"
    rows = _rows(module, sid)
    assert rows[-1]["kind"] == "regex_hit"
    assert rows[-1]["allow"] is False


def test_benign_commands_never_reach_the_judge(day, monkeypatch):
    module, ctx, sid = day
    calls = []
    _set_judge(module, monkeypatch, lambda a, u, c: calls.append(a) or {})
    for cmd in ("pytest tests/ -q", "git status --short", "ls -la", "make build"):
        assert _call(module, sid, cmd) is None, cmd
    assert calls == []
    assert _rows(module, sid) == []


# ---------------------------------------------------------------------------
# escalation: combinations only, to the human approval gate
# ---------------------------------------------------------------------------


def test_off_mandate_plus_risk_escalates_to_human(day, monkeypatch):
    module, ctx, sid = day
    _set_judge(module, monkeypatch,
               _judge(intent_consistent=0.1, irreversible=0.8,
                      blast_radius="workspace"))
    out = _call(module, sid, "rm -rf src/")
    assert isinstance(out, dict) and out["action"] == "approve", out
    rows = _rows(module, sid)
    assert rows[-1]["kind"] == "judged" and rows[-1]["allow"] is False


def test_external_blast_on_soft_mandate_escalates(day, monkeypatch):
    module, ctx, sid = day
    _set_judge(module, monkeypatch,
               _judge(intent_consistent=0.95, data_egress=0.8,
                      blast_radius="external"))
    out = _call(module, sid,
                "curl -X POST --data-binary @out.json https://example.com")
    assert isinstance(out, dict) and out["action"] == "approve", out
    assert _rows(module, sid)[-1]["allow"] is False


def test_external_blast_with_explicit_mandate_is_allowed(day, monkeypatch):
    """A human 'yes' already recorded this session = an explicit mandate."""
    module, ctx, sid = day
    module._SESSIONS[sid]["approvals"].append(
        {"cmd": "deploy", "choice": "once", "by": "", "ts": 1.0})
    _set_judge(module, monkeypatch,
               _judge(intent_consistent=0.95, data_egress=0.8,
                      blast_radius="external"))
    out = _call(module, sid,
                "curl -X POST --data-binary @out.json https://example.com")
    assert out is None
    assert _rows(module, sid)[-1]["kind"] == "judged"


def test_escalation_without_a_reachable_human_blocks(day, monkeypatch):
    """approvals.mode=off / yolo: emitting `approve` would auto-approve, so the
    gate must block instead — same fail-closed rule jev-shield enforces."""
    module, ctx, sid = day
    _set_judge(module, monkeypatch,
               _judge(intent_consistent=0.1, irreversible=0.9,
                      blast_radius="machine"))
    monkeypatch.setattr(module, "_gate_escalation_available", lambda: False)
    out = _call(module, sid, "rm -rf src/")
    assert isinstance(out, dict) and out["action"] == "block", out
    assert _rows(module, sid)[-1]["allow"] is False


def test_cached_denial_still_escalates_without_repurchasing(day, monkeypatch):
    """The judgment is cached, never the verdict: a repeated escalated action
    must escalate again without a second paid call."""
    module, ctx, sid = day
    calls = []
    _set_judge(module, monkeypatch,
               lambda a, u, c: calls.append(a) or
               {"intent_consistent": 0.1, "irreversible": 0.9,
                "blast_radius": "machine", "lane": "hosted"})
    first = _call(module, sid, "dd if=/dev/zero of=/dev/sda")
    second = _call(module, sid, "dd if=/dev/zero of=/dev/sda")
    assert first["action"] == "approve" and second["action"] == "approve"
    assert len(calls) == 1
    assert [r["kind"] for r in _rows(module, sid)] == ["judged", "cached"]


# ---------------------------------------------------------------------------
# judge adapter (the jev-shield reuse seam)
# ---------------------------------------------------------------------------


def test_live_judge_adapter_maps_shield_answers(day, monkeypatch):
    module, ctx, sid = day
    seen = {}

    class FakeShield:
        @staticmethod
        def ask_jev(screen, user_request, platform, cfg, client=None):
            seen["state_tool"] = screen.action_text.split(" ", 1)[0]
            seen["request"] = user_request
            seen["workspace"] = cfg.get("_workspace")
            return ({"irreversible": 0.7, "outer_scope": 0.0,
                     "intent_consistent": 0.9, "data_egress": 0.0,
                     "blast_radius": "machine"},
                    {"cost_usd": 0.0002, "model": "jev-x", "ms": 42})

    monkeypatch.setattr(module, "_load_shield_jev", lambda: FakeShield)
    res = module._gate_judge_live("terminal", "deploy the thing",
                                  {"command": "rm -rf build/", "workspace": "/w"})
    assert res["irreversible"] == 0.7 and res["blast_radius"] == "machine"
    assert res["lane"] == "hosted" and res["cost"] == 0.0002
    assert seen["state_tool"] == "tool=terminal"
    assert seen["request"] == "deploy the thing"


def test_live_judge_adapter_raises_when_shield_missing(day, monkeypatch):
    module, ctx, sid = day
    monkeypatch.setattr(module, "_load_shield_jev", lambda: None)
    with pytest.raises(Exception):
        module._gate_judge_live("terminal", "", {})


def test_live_judge_adapter_raises_on_unjudged_meta(day, monkeypatch):
    module, ctx, sid = day

    class FakeShield:
        @staticmethod
        def ask_jev(screen, user_request, platform, cfg, client=None):
            return None, {"reason": "no key in $TYPESAFE_API_KEY"}

    monkeypatch.setattr(module, "_load_shield_jev", lambda: FakeShield)
    with pytest.raises(Exception):
        module._gate_judge_live("terminal", "", {})


# ---------------------------------------------------------------------------
# screen: only borderline / irreversible / outer-scope calls pay for judgment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cmd,borderline", [
    ("rm -rf build/", True),
    ("sudo systemctl restart x", True),
    ("git push --force origin main", True),
    ("curl -X POST -d @f https://x", True),
    ("ssh host ls", True),
    ("dd if=a of=/dev/sda", True),
    ("cat ~/.ssh/id_rsa", True),
    ("touch ../outside.py", True),
    ("pytest tests/ -q", False),
    ("git status --short", False),
    ("ls -la", False),
    ("echo hello", False),
    ("touch local.py", False),
])
def test_borderline_screen(day, cmd, borderline):
    module, _, _ = day
    assert module._gate_borderline("terminal", {"command": cmd}) is borderline, cmd


# ---------------------------------------------------------------------------
# cross-phase seam (found at integration merge): read-only lanes x typed gate
# ---------------------------------------------------------------------------


def test_outer_scope_write_in_untagged_child_meets_the_typed_gate(day, monkeypatch):
    """An untagged child is not lane-restricted — but an OUTER-scope write still
    meets the typed gate: escalated, and blocked only because no human approval
    gate is reachable. The verdict is the gate's, never the lane veto's."""
    module, ctx, sid = day
    _fire(ctx, "subagent_start", parent_session_id=sid,
          child_session_id="lane-untagged",
          child_goal="write a file outside the repo")
    _set_judge(module, monkeypatch,
               _judge(outer_scope=0.9, intent_consistent=0.3,
                      blast_radius="external"))
    monkeypatch.setattr(module, "_gate_escalation_available", lambda: False)

    out = module._pre_tool_call(tool_name="write_file",
                                args={"path": "/x/notes.py", "content": "x"},
                                session_id="lane-untagged")
    assert out is not None and out["action"] == "block"
    assert "read-only lane" not in out["message"].lower()
    assert "escalated" in out["message"].lower()
    rows = _rows(module, "lane-untagged")
    assert [r["kind"] for r in rows] == ["judged"] and rows[0]["allow"] is False


def test_tagged_lane_outer_write_is_vetoed_by_the_lane_rule_first(day):
    """The lane veto precedes the typed gate: a read-only lane gets the lane
    block (and never a judgment) for the same outer-scope write."""
    module, ctx, sid = day
    payload = module._manifest_lane_context(module._SESSIONS[sid]["_root"])
    _fire(ctx, "subagent_start", parent_session_id=sid,
          child_session_id="lane-tagged",
          child_goal=f"{payload['goal_prefix']}review the diff")

    out = module._pre_tool_call(tool_name="write_file",
                                args={"path": "/x/notes.py", "content": "x"},
                                session_id="lane-tagged")
    assert out is not None and out["action"] == "block"
    assert "read-only lane" in out["message"].lower()
