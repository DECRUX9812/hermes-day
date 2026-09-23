"""TDD tests for hday_approvals — HITL pause/resume + capability safety.

Import-safe standalone: bootstrap sys.path to the plugin dir so ``hday_approvals``
imports without pulling in ``__init__.py``.

Four mechanisms, each spec'd from the harvest (runs/harvest/*.json):

1. **HITL as a pair of ordinary functions** — ``pause()`` persists a pending
   decision to the ONE existing ledger and returns a token; ``resume(token,
   decision)`` continues deterministically. Spec: LangGraph's
   ``interrupt``/``Command(resume=...)`` ("the pause point lives in the run, not
   in a side channel"), LlamaIndex's ``input_required``/``human_response`` pair
   ("a second response for the same prompt id is rejected as already-answered
   (typed, no duplicate continuation)"), AG-UI's Interrupt ("a denial
   round-trips as payload{approved:false} and never as status:'cancelled'") and
   OWASP's human-approval-gates ("an approval for action hash A cannot authorize
   action hash B"; "unreachable rows never become approvals").
2. **Capability-based permissions** — each tool declares the capabilities it
   needs and the gate consults a *typed* verdict, never a boolean. Spec:
   CaMeL's capability table ("a capability grant for one call does not persist
   to the next (no ambient authority)") and Progent's per-task allowlist
   ("a policy that permits nothing denies everything (fail-closed control)").
3. **Guardrails as a (passed, value) contract** that can rewrite the payload,
   not just block it. Spec: NeMo's rail_action in {allow, deny, alter} ("an
   alter rail records before/after hashes plus the reason as a ledger row
   (never a silent rewrite)") and ADK callbacks ("one returning a truthy
   rewrite replaces the args").
4. **A durable approval queue** — persisted, not in-memory, so a pending
   approval survives a process restart.

Run:  pytest tests -q
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hday_approvals as A  # noqa: E402  (must import standalone, no PluginContext)

from conftest import _fire  # noqa: E402  (shared fixtures)


@pytest.fixture(autouse=True)
def _isolate_module_state():
    """Module-level configure() state must not leak between tests."""
    A.reset()
    yield
    A.reset()


def _clock(start: float = 1_000_000.0):
    """A deterministic injected clock: ``now()`` returns a settable float."""
    state = {"t": start}
    return state, (lambda: state["t"])


# ---------------------------------------------------------------------------
# mechanism 2 — capability-based permissions (typed verdict, never a boolean)
# ---------------------------------------------------------------------------

def test_capability_vocabulary_is_closed():
    """Every declared capability is part of the vocabulary — no typos pass."""
    for tool, caps in A.TOOL_CAPABILITIES.items():
        for cap in caps:
            assert cap in A.CAPABILITIES, f"{tool} declares unknown capability {cap!r}"


def test_each_tool_declares_the_capabilities_it_needs():
    assert A.capabilities_for("write_file", {"path": "a.py"}) == frozenset({"fs.write"})
    assert A.capabilities_for("read_file", {"path": "a.py"}) == frozenset({"fs.read"})
    assert A.capabilities_for("patch", {"path": "a.py"}) == frozenset({"fs.read", "fs.write"})


def test_terminal_capabilities_are_derived_from_the_command():
    assert A.capabilities_for("terminal", {"command": "ls -la"}) == frozenset({"proc.spawn"})
    assert "net.egress" in A.capabilities_for("terminal", {"command": "git push origin main"})
    assert "net.egress" in A.capabilities_for("terminal", {"command": "curl -X POST https://x/y"})
    assert "proc.privilege" in A.capabilities_for("terminal", {"command": "sudo apt update"})
    assert "proc.signal" in A.capabilities_for("terminal", {"command": "kill -9 1234"})
    assert "secret.read" in A.capabilities_for("read_file", {"path": "/home/u/.ssh/id_rsa"})
    # a read-only lookup must not pick up a write capability
    assert "fs.write" not in A.capabilities_for("terminal", {"command": "grep x a.py"})


def test_unknown_capability_denies_fail_closed():
    A.declare_capabilities("weird_tool", ("fs.write", "quantum.teleport"))
    verdict = A.verdict_for(A.capabilities_for("weird_tool", {}), A.Policy.default())
    assert verdict.decision == "deny"
    assert verdict.rule == "capability.unknown"
    assert verdict.unknown == ("quantum.teleport",)


def test_policy_verdict_is_typed_not_a_boolean():
    verdict = A.verdict_for({"fs.read"}, A.Policy.default())
    assert isinstance(verdict, A.PolicyVerdict)
    assert not isinstance(verdict, bool)
    assert verdict.decision == "allow"
    with pytest.raises(TypeError):
        bool(verdict)          # a verdict must be branched on, never truth-tested


def test_read_only_policy_denies_a_write_and_names_the_capability():
    policy = A.Policy(name="read-only", grants=("fs.read", "net.read"))
    verdict = A.verdict_for({"fs.read", "fs.write"}, policy)
    assert verdict.decision == "deny"
    assert verdict.rule == "capability.denied"
    assert verdict.missing == ("fs.write",)
    assert "fs.write" in verdict.reason
    assert verdict.policy == "read-only"


def test_escalatable_capability_escalates_instead_of_denying():
    policy = A.Policy(name="supervised", grants=("fs.read",), escalate=("net.egress",))
    verdict = A.verdict_for({"net.egress"}, policy)
    assert verdict.decision == "escalate"
    assert verdict.rule == "capability.escalate"
    assert verdict.escalate == ("net.egress",)


def test_deny_beats_grant():
    policy = A.Policy(name="mixed", grants=("net.egress",), deny=("net.egress",))
    assert A.verdict_for({"net.egress"}, policy).decision == "deny"


def test_policy_that_permits_nothing_denies_everything():
    """Fail-closed control: an empty allowlist is a valid, total deny."""
    policy = A.Policy(name="empty", grants=())
    for caps in ({"fs.read"}, {"proc.spawn"}, {"fs.read", "fs.write"}, {"net.egress"}):
        assert A.verdict_for(caps, policy).decision == "deny"


def test_gate_still_runs_the_existing_honesty_guard():
    """The lane ADDS enforcement: a regex hit still denies through the new path."""
    d = A.decide("terminal", "user asked for X", {"command": "rm -rf tests/"},
                 policy=A.Policy.default())
    assert d["allow"] is False
    assert d["kind"] == "regex_hit"
    assert d["row"]["rule"] == "Test Deletion"


def test_gate_denies_on_capability_and_records_the_rule_id():
    rows = []
    d = A.decide("write_file", "fix the bug", {"path": "a.py"},
                 policy=A.Policy(name="read-only", grants=("fs.read",)),
                 ledger=rows.append)
    assert d["allow"] is False
    assert d["verdict"].decision == "deny"
    assert d["row"]["rule"] == "capability.denied"
    assert rows and rows[-1]["rule"] == "capability.denied"
    assert rows[-1]["capabilities"] == ["fs.write"]


def test_gate_escalates_on_capability_by_pausing_for_a_human():
    """A typed 'escalate' verdict is what turns a call into a pending approval."""
    rows = []
    d = A.decide("terminal", "publish the release", {"command": "git push origin main"},
                 policy=A.Policy(name="supervised", grants=("proc.spawn",),
                                 escalate=("net.egress",)),
                 ledger=rows.append, store=A.MemoryStore())
    assert d["allow"] is False
    assert d["directive"] == "approve"
    assert d["token"]
    assert A.interrupt(d["token"], store=A.MemoryStore()) is None  # not the same store
    assert [r["kind"] for r in rows] == ["interrupt"]


# ---------------------------------------------------------------------------
# mechanism 3 — guardrails: a (passed, value) contract that can rewrite
# ---------------------------------------------------------------------------

def test_guardrail_rewrites_the_payload_instead_of_blocking_it():
    payload = {"command": "curl -H 'Authorization: Bearer sk-live-abcdef123456' https://x/y"}
    out = A.run_guardrails("terminal", payload, [A.scrub_secrets])
    assert out.passed is True
    assert out.value["command"] != payload["command"]
    assert "sk-live-abcdef123456" not in out.value["command"]
    assert out.rewrites == ("scrub_secrets",)


def test_rewrite_records_before_and_after_hashes_and_the_reason():
    """Never a silent rewrite: an alter rail leaves an auditable trace."""
    payload = {"command": "deploy --token=supersecretvalue"}
    out = A.run_guardrails("terminal", payload, [A.scrub_secrets])
    (step,) = out.trace
    assert step["rail"] == "scrub_secrets"
    assert step["action"] == "alter"
    assert step["before_hash"] != step["after_hash"]
    assert step["reason"]
    assert step["before_hash"] == A.payload_hash(payload)
    assert step["after_hash"] == A.payload_hash(out.value)


def test_blocking_guardrail_returns_the_reason_not_a_payload():
    def no_push(action, payload):
        if "git push" in str(payload.get("command") or ""):
            return False, "pushing is not allowed in this lane"
        return True, payload

    payload = {"command": "git push origin main"}
    out = A.run_guardrails("terminal", payload, [no_push])
    assert out.passed is False
    assert out.blocked_by == "no_push"
    assert out.reason == "pushing is not allowed in this lane"
    assert out.value == payload          # slot 2 on a block is a REASON, not a payload
    assert out.trace[-1]["action"] == "deny"


def test_guardrails_chain_and_later_rails_see_the_rewrite():
    seen = {}

    def first(action, payload):
        return True, dict(payload, command="rewritten-by-first")

    def second(action, payload):
        seen["command"] = payload["command"]
        return True, payload

    out = A.run_guardrails("terminal", {"command": "original"}, [first, second])
    assert seen["command"] == "rewritten-by-first"
    assert out.value["command"] == "rewritten-by-first"
    assert out.rewrites == ("first",)


def test_raising_guardrail_fails_open_unjudged():
    """Mirrors the repo's failure policy: a broken rail contributes nothing,
    loudly — it must not silently rewrite, and it must not block."""
    def broken(action, payload):
        raise RuntimeError("rail exploded")

    payload = {"command": "ls"}
    out = A.run_guardrails("terminal", payload, [broken])
    assert out.passed is True
    assert out.value == payload
    assert out.rewrites == ()
    assert out.trace[-1]["action"] == "unjudged"
    assert "rail exploded" in out.trace[-1]["reason"]


def test_clean_payload_passes_through_untouched():
    payload = {"command": "ls -la"}
    out = A.run_guardrails("terminal", payload, [A.scrub_secrets])
    assert out.passed is True
    assert out.value == payload
    assert out.rewrites == ()


def test_guardrail_alter_lands_on_the_ledger():
    rows = []
    A.run_guardrails("terminal", {"command": "x --api_key=abcdef123456"},
                     [A.scrub_secrets], ledger=rows.append)
    assert rows and rows[-1]["rule"] == "Guardrail"
    assert rows[-1]["rail"] == "scrub_secrets"
    assert rows[-1]["before_hash"] != rows[-1]["after_hash"]


def test_clamp_timeout_rail_rewrites_a_numeric_field():
    out = A.run_guardrails("terminal", {"command": "sleep 1", "timeout": 9999},
                           [A.clamp_timeout(limit=30)])
    assert out.passed is True
    assert out.value["timeout"] == 30
    assert out.rewrites == ("clamp_timeout",)


def test_gate_runs_guardrails_and_the_rewrite_is_visible_on_the_row():
    d = A.decide("terminal", "check the api", {"command": "curl --token=abcdef123456 https://x"},
                 policy=A.Policy.default(), guardrails=[A.scrub_secrets])
    assert d["allow"] is True
    assert d["row"]["guardrails"]["rewrites"] == ["scrub_secrets"]
    assert "abcdef123456" not in json.dumps(d["row"])


# ---------------------------------------------------------------------------
# mechanism 1 — human-in-the-loop: pause() / resume()
# ---------------------------------------------------------------------------

def _pause(store=None, ledger=None, **kw):
    kw.setdefault("session", "sess-1")
    return A.pause("terminal", {"command": "rm -rf build"}, store=store,
                   ledger=ledger, **kw)


def test_pause_persists_a_pending_decision_to_the_ledger_and_returns_a_token():
    rows = []
    store = A.MemoryStore()
    token = _pause(store=store, ledger=rows.append, prompt="delete the build dir?")
    assert isinstance(token, str) and token
    assert rows and rows[-1]["kind"] == "interrupt"
    assert rows[-1]["status"] == "pending"
    assert rows[-1]["allow"] is False
    assert rows[-1]["directive"] == "approve"
    assert rows[-1]["interrupt"] == token
    pending = A.pending(store=store)
    assert [p.id for p in pending] == [token]
    assert pending[0].prompt == "delete the build dir?"


def test_interrupt_is_a_typed_object_with_the_published_fields():
    store = A.MemoryStore()
    token = _pause(store=store, reason="irreversible", blast_radius="machine", ttl_s=60)
    it = A.interrupt(token, store=store)
    assert it.id == token
    assert it.action == "terminal"
    assert it.status == "pending"
    assert it.blast_radius == "machine"
    assert it.ttl_s == 60
    assert it.expires_at > it.created_at
    assert it.payload == {"command": "rm -rf build"}
    assert it.action_hash == A.action_hash("terminal", {"command": "rm -rf build"})


def test_action_hash_binds_the_approval_to_one_exact_action():
    a = A.action_hash("terminal", {"command": "ls"})
    assert a == A.action_hash("terminal", {"command": "ls"})       # stable
    assert a != A.action_hash("terminal", {"command": "ls -la"})   # payload-bound
    assert a != A.action_hash("write_file", {"command": "ls"})     # action-bound


def test_resume_approve_continues_deterministically_with_the_original_payload():
    rows = []
    store = A.MemoryStore()
    token = _pause(store=store, ledger=rows.append)
    out = A.resume(token, "approve", responder="human:decrux", store=store, ledger=rows.append)
    assert out.status == "approved"
    assert out.allow is True
    assert out.fresh is True
    assert out.action == "terminal"
    assert out.payload == {"command": "rm -rf build"}   # byte-identical continuation
    assert out.action_hash == A.action_hash("terminal", {"command": "rm -rf build"})
    assert rows[-1]["status"] == "approved"
    assert rows[-1]["responder"] == "human:decrux"
    assert A.pending(store=store) == ()


def test_resume_deny_round_trips_as_denied_and_never_as_cancelled():
    store = A.MemoryStore()
    token = _pause(store=store)
    out = A.resume(token, "deny", responder="human:decrux", store=store)
    assert out.status == "denied"
    assert out.allow is False
    assert out.status != "cancelled"
    assert A.interrupt(token, store=store).decision == "deny"


def test_a_second_resume_is_already_answered_and_appends_no_second_row():
    rows = []
    store = A.MemoryStore()
    token = _pause(store=store, ledger=rows.append)
    A.resume(token, "approve", store=store, ledger=rows.append)
    n = len(rows)
    again = A.resume(token, "approve", store=store, ledger=rows.append)
    assert again.status == "already_answered"
    assert again.fresh is False
    assert again.allow is True          # the recorded decision, not a new one
    assert len(rows) == n               # no duplicate continuation


def test_resume_of_an_unknown_token_raises():
    with pytest.raises(A.ApprovalError):
        A.resume("nope-not-a-token", "approve", store=A.MemoryStore())


def test_resume_with_an_unknown_decision_raises():
    store = A.MemoryStore()
    token = _pause(store=store)
    with pytest.raises(A.ApprovalError):
        A.resume(token, "maybe", store=store)


def test_expiry_flips_pending_to_expired_and_re_escalates():
    state, now = _clock()
    store = A.MemoryStore()
    token = _pause(store=store, ttl_s=60, now=now)
    state["t"] += 120
    out = A.resume(token, "approve", store=store, now=now)
    assert out.status == "expired"
    assert out.allow is False
    assert out.rule == "approval.expired"
    # the next matching call re-escalates under a NEW request id
    fresh = _pause(store=store, ttl_s=60, now=now)
    assert fresh != token
    assert [p.id for p in A.pending(store=store, now=now)] == [fresh]


def test_a_stale_approval_cannot_be_replayed_after_its_ttl():
    state, now = _clock()
    store = A.MemoryStore()
    token = _pause(store=store, ttl_s=60, now=now)
    A.resume(token, "approve", store=store, now=now)
    state["t"] += 120
    auth = A.authorize(token, "terminal", {"command": "rm -rf build"}, store=store, now=now)
    assert auth.allow is False
    assert auth.rule == "approval.expired"


def test_unreachable_is_recorded_and_is_never_a_silent_approval():
    rows = []
    store = A.MemoryStore()
    token = _pause(store=store, ledger=rows.append, escalation_available=lambda: False)
    assert A.interrupt(token, store=store).status == "unreachable"
    assert rows[-1]["status"] == "unreachable"
    out = A.resume(token, "approve", store=store)
    assert out.allow is False
    assert out.rule == "approval.unreachable"


def test_unreachable_read_only_action_may_be_allowed_plus_recorded():
    rows = []
    store = A.MemoryStore()
    policy = A.Policy(name="p", grants=("fs.read",), unreachable="record")
    token = A.pause("read_file", {"path": "a.py"}, store=store, ledger=rows.append,
                    policy=policy, escalation_available=lambda: False)
    out = A.resume(token, "approve", store=store, ledger=rows.append)
    assert out.allow is True
    assert out.rule == "approval.unreachable_recorded"
    assert rows[-1]["status"] == "unreachable"


def test_an_approval_for_one_action_hash_cannot_authorize_another():
    store = A.MemoryStore()
    token = _pause(store=store)
    A.resume(token, "approve", store=store)
    good = A.authorize(token, "terminal", {"command": "rm -rf build"}, store=store)
    assert good.allow is True
    other = A.authorize(token, "terminal", {"command": "rm -rf /"}, store=store)
    assert other.allow is False
    assert other.rule == "approval.action_mismatch"


def test_a_capability_grant_for_one_call_does_not_persist_to_the_next():
    """No ambient authority: consuming the approval spends it."""
    store = A.MemoryStore()
    token = _pause(store=store)
    A.resume(token, "approve", store=store)
    first = A.authorize(token, "terminal", {"command": "rm -rf build"}, store=store)
    second = A.authorize(token, "terminal", {"command": "rm -rf build"}, store=store)
    assert first.allow is True
    assert second.allow is False
    assert second.rule == "approval.consumed"


def test_a_denied_approval_never_authorizes():
    store = A.MemoryStore()
    token = _pause(store=store)
    A.resume(token, "deny", store=store)
    auth = A.authorize(token, "terminal", {"command": "rm -rf build"}, store=store)
    assert auth.allow is False
    assert auth.rule == "approval.denied"


# ---------------------------------------------------------------------------
# mechanism 4 — a queue that survives a process restart
# ---------------------------------------------------------------------------

def test_queue_survives_a_process_restart(tmp_path: Path):
    path = str(tmp_path / "approvals.json")
    token = _pause(store=path, ttl_s=3600, now=lambda: 1_000_000.0)

    # the bytes are on disk, not in a process-local dict
    on_disk = json.loads(Path(path).read_text())
    assert token in json.dumps(on_disk)

    # simulate a restart: brand-new queue object, brand-new store, same file
    restarted = A.ApprovalQueue(store=A.FileStore(path))
    assert [p.id for p in restarted.pending(1_000_000.0)] == [token]
    out = A.resume(token, "approve", store=A.FileStore(path), now=lambda: 1_000_000.0)
    assert out.allow is True
    assert out.payload == {"command": "rm -rf build"}


def test_resolutions_survive_a_restart_so_a_replay_is_still_refused(tmp_path: Path):
    path = str(tmp_path / "approvals.json")
    token = _pause(store=path, ttl_s=3600, now=lambda: 1_000_000.0)
    A.resume(token, "approve", store=path, now=lambda: 1_000_000.0)
    replay = A.resume(token, "approve", store=A.FileStore(path), now=lambda: 1_000_000.0)
    assert replay.status == "already_answered"
    assert replay.fresh is False


def test_pending_queue_is_bounded(tmp_path: Path):
    path = str(tmp_path / "approvals.json")
    store = A.ApprovalQueue(store=path, cap=3)
    base = time.time()
    for i in range(6):
        store.put(A.Interrupt(id=f"t{i}", action="terminal", action_hash="h",
                              payload={"command": f"c{i}"}, prompt="", reason="",
                              blast_radius="local", capabilities=(), status="pending",
                              created_at=base + i, expires_at=base + i + 60, ttl_s=60))
    assert [p.id for p in store.pending()] == ["t3", "t4", "t5"]


def test_pause_without_a_store_is_marked_non_durable():
    """Honest degradation: no store means the row says so, never a silent claim."""
    rows = []
    token = _pause(ledger=rows.append)
    assert rows[-1]["durable"] is False
    assert A.interrupt(token).status == "pending"


def test_pause_survives_a_failing_store_without_losing_the_decision():
    class Broken:
        def get(self, key, default=None):
            raise OSError("disk gone")

        def set(self, key, value):
            raise OSError("disk gone")

    rows = []
    token = _pause(store=Broken(), ledger=rows.append)
    assert token
    assert rows[-1]["durable"] is False
    assert rows[-1]["status"] == "pending"


def test_interrupt_and_resume_are_plain_functions_not_a_subclass_of_anything():
    """HITL rides the ordinary call path: no exception, no special type."""
    assert callable(A.pause) and callable(A.resume)
    store = A.MemoryStore()
    token = _pause(store=store)
    assert isinstance(token, str)
    assert isinstance(A.resume(token, "approve", store=store), A.ResumeOutcome)


# ---------------------------------------------------------------------------
# plugin wiring — the pending decision lands on the ONE existing ledger
# ---------------------------------------------------------------------------

def test_plugin_registers_the_queue_command(day):
    module, ctx, _ = day
    assert "day-queue" in ctx.commands, "day-queue command missing"


def test_plugin_pause_lands_on_the_one_gate_ledger(day, monkeypatch):
    module, ctx, sid = day
    # pin the escalation probe: the live host may have approvals off, and that
    # environment-dependent branch is covered by its own test below.
    monkeypatch.setattr(module, "_gate_escalation_available", lambda: True)
    appr = module._approvals()          # re-wire with the pinned probe
    assert appr is not None, "hday_approvals did not load as a sibling module"
    token = appr.pause("terminal", {"command": "rm -rf build"}, session=sid,
                       prompt="delete the build dir?")
    gate = module._SESSIONS[sid]["gate"]
    assert gate and gate[-1]["kind"] == "interrupt"
    assert gate[-1]["interrupt"] == token
    assert gate[-1]["status"] == "pending"
    assert gate[-1]["allow"] is False
    assert gate[-1]["directive"] == "approve"
    assert gate[-1]["tool"] == "terminal"


def test_plugin_queue_survives_a_reload(day, monkeypatch):
    module, ctx, sid = day
    monkeypatch.setattr(module, "_gate_escalation_available", lambda: True)
    appr = module._approvals()
    token = appr.pause("terminal", {"command": "rm -rf build"}, session=sid)
    A.reset()                       # drop every in-process cache: store only
    assert module._approvals().pending(), "queue did not survive the reload"
    out = module._approvals().resume(token, "approve")
    assert out.allow is True
    assert out.payload == {"command": "rm -rf build"}


def test_day_queue_command_reports_the_pending_decision(day, monkeypatch):
    module, ctx, sid = day
    monkeypatch.setattr(module, "_gate_escalation_available", lambda: True)
    appr = module._approvals()
    token = appr.pause("terminal", {"command": "rm -rf build"}, session=sid)
    payload = json.loads(ctx.commands["day-queue"](sid))
    assert payload["ok"] is True
    assert [p["id"] for p in payload["pending"]] == [token]
    assert payload["pending"][0]["action"] == "terminal"
    filtered = json.loads(ctx.commands["day-queue"]("sess-nonexistent"))
    assert filtered["ok"] is True
    assert filtered["pending"] == []


def test_plugin_escalation_probe_is_wired(day):
    """pause() must know whether a human is reachable (approvals off => never
    park a decision that nothing can answer)."""
    module, ctx, sid = day
    appr = module._hday("hday_approvals")
    assert appr._ESCALATION is not None
    assert appr._ESCALATION is module._gate_escalation_available


def test_plugin_records_unreachable_when_no_human_gate_is_reachable(day, monkeypatch):
    """The live branch when approvals are off / yolo: recorded, never approved."""
    module, ctx, sid = day
    monkeypatch.setattr(module, "_gate_escalation_available", lambda: False)
    appr = module._approvals()
    parked = appr.pause("terminal", {"command": "rm -rf build"}, session=sid)
    row = module._SESSIONS[sid]["gate"][-1]
    assert row["kind"] == "interrupt"
    assert row["status"] == "unreachable"
    assert row["directive"] == "block"
    answered = appr.resume(parked, "approve")
    assert answered.allow is False
    assert answered.rule == "approval.unreachable"
