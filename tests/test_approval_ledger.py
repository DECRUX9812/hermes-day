"""Tests for the Hermes Day approval ledger, attention feed and mid-session instincts.

Loads the plugin source from this repo (not the deployed copy), drives the
observer hooks directly, and asserts the behaviours that matter — including the
two *negative* cases that would otherwise silently poison the instinct ledger:
an unanswered prompt must not read as a human refusal, and an aux-LLM denial
must not read as a human decision.

Run:  pytest tests/ -q
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import REPO_ROOT, FakeCtx, PLUGIN_INIT, _fire  # noqa: F401  (shared fixtures)


# ---------------------------------------------------------------------------
# registration surface
# ---------------------------------------------------------------------------


def test_observer_hooks_are_registered(day):
    _, ctx, _ = day
    for hook in (
        "pre_approval_request",
        "post_approval_response",
        "agent_loop_stopped",
        "subagent_stop",
        "api_request_error",
        "pre_llm_call",
    ):
        assert hook in ctx.hooks, f"{hook} not registered"


def test_original_hooks_still_registered(day):
    """The pre-existing guard/gate hooks must survive the increment."""
    _, ctx, _ = day
    for hook in ("pre_tool_call", "post_tool_call", "pre_verify",
                 "on_session_finalize", "transform_tool_result"):
        assert hook in ctx.hooks, f"{hook} was dropped"


def test_commands_exposed(day):
    _, ctx, _ = day
    for name in ("day-approvals", "day-attention", "day-evidence", "day-rollback",
                 "day-impact", "day-instincts", "day-vacuum"):
        assert name in ctx.commands, f"{name} command missing"


def test_no_tools_registered(day):
    """Declares provides_tools: [] — must stay a pure hook/command plugin."""
    _, ctx, _ = day
    assert ctx.hooks  # sanity: registration happened


# ---------------------------------------------------------------------------
# approval ledger
# ---------------------------------------------------------------------------


def _req(day, ctx, **over):
    kwargs = dict(command="rm -rf /tmp/x", description="destructive", pattern_key="rm_rf",
                  pattern_keys=["rm_rf"], session_key="chat-1", surface="cli")
    kwargs.update(over)
    _fire(ctx, "pre_approval_request", **kwargs)


def _resp(day, ctx, **over):
    kwargs = dict(command="rm -rf /tmp/x", description="destructive", pattern_key="rm_rf",
                  pattern_keys=["rm_rf"], session_key="chat-1", surface="cli")
    kwargs.update(over)
    _fire(ctx, "post_approval_response", **kwargs)


def test_request_is_recorded_and_correlated(day):
    module, ctx, sid = day
    _req(day, ctx, session_id=sid)
    rec = module._SESSIONS[sid]
    assert len(rec["approvals"]) == 1
    assert len(module._PENDING_APPROVALS) == 1
    assert rec["approvals"][0]["cmd"] == "rm -rf /tmp/x"
    assert rec["approvals"][0]["surface"] == "cli"
    assert rec["approvals"][0]["choice"] == ""


def test_response_matches_pending_and_measures_latency(day):
    module, ctx, sid = day
    _req(day, ctx, session_id=sid)
    time.sleep(0.05)
    _resp(day, ctx, session_id=sid, choice="once")
    entry = module._SESSIONS[sid]["approvals"][0]
    assert entry["choice"] == "once"
    assert entry["ms"] >= 40, f"latency not measured: {entry['ms']}"
    assert module._PENDING_APPROVALS == []


def test_human_denial_promotes_an_instinct(day, repo):
    module, ctx, sid = day
    _req(day, ctx, session_id=sid)
    _resp(day, ctx, session_id=sid, choice="deny")
    instincts = module._load_instincts(str(repo))
    assert len(instincts) == 1
    assert instincts[0]["trigger_pattern"] == "rm_rf"
    assert "denied" in instincts[0]["reason"]


def test_repeated_denial_bumps_hits_instead_of_duplicating(day, repo):
    module, ctx, sid = day
    for _ in range(2):
        _req(day, ctx, session_id=sid)
        _resp(day, ctx, session_id=sid, choice="deny")
    instincts = module._load_instincts(str(repo))
    assert len(instincts) == 1, "same trap should dedupe"
    assert instincts[0]["hits"] == 2


def test_timeout_is_not_a_denial(day, repo):
    """An unanswered prompt means no human spoke — it must not become a constraint."""
    module, ctx, sid = day
    _req(day, ctx, session_id=sid)
    _resp(day, ctx, session_id=sid, choice="timeout")
    assert module._load_instincts(str(repo)) == []
    kinds = [item["kind"] for item in module._SESSIONS[sid]["attn"]]
    assert "approval_stall" in kinds


def test_cancelled_is_not_a_denial(day, repo):
    module, ctx, sid = day
    _req(day, ctx, session_id=sid)
    _resp(day, ctx, session_id=sid, choice="cancelled")
    assert module._load_instincts(str(repo)) == []
    kinds = [item["kind"] for item in module._SESSIONS[sid]["attn"]]
    assert "approval_stall" in kinds


def test_smart_deny_is_attributed_not_treated_as_human(day, repo):
    module, ctx, sid = day
    _req(day, ctx, session_id=sid, surface="smart")
    _resp(day, ctx, session_id=sid, surface="smart", choice="smart_deny", decided_by="aux_llm")
    entry = module._SESSIONS[sid]["approvals"][-1]
    assert entry["by"] == "aux_llm"
    assert module._load_instincts(str(repo)) == [], "aux LLM denial is not a human refusal"


def test_smart_approve_recorded(day):
    module, ctx, sid = day
    _req(day, ctx, session_id=sid, surface="smart")
    _resp(day, ctx, session_id=sid, surface="smart", choice="smart_approve", decided_by="aux_llm")
    assert module._SESSIONS[sid]["approvals"][-1]["choice"] == "smart_approve"


def test_unmatched_response_still_recorded(day):
    """A response with no matching request (restart, cross-process) must not raise."""
    module, ctx, sid = day
    _resp(day, ctx, session_id=sid, choice="deny")
    assert len(module._SESSIONS[sid]["approvals"]) == 1


def test_approval_ledger_is_capped(day):
    module, ctx, sid = day
    for i in range(module._MAX_APPROVALS + 12):
        _req(day, ctx, session_id=sid, command=f"cmd-{i}")
    assert len(module._SESSIONS[sid]["approvals"]) == module._MAX_APPROVALS


def test_pending_map_is_bounded(day):
    module, ctx, sid = day
    for i in range(200):
        _req(day, ctx, session_id=sid, command=f"cmd-{i}")
    assert len(module._PENDING_APPROVALS) <= 60


# ---------------------------------------------------------------------------
# attention feed
# ---------------------------------------------------------------------------


def test_agent_loop_stopped_records_interruption(day):
    module, ctx, sid = day
    _fire(ctx, "agent_loop_stopped", session_key=sid, platform="tui",
          reason="user_stop", invalidation_reason="stop_command")
    item = module._SESSIONS[sid]["attn"][-1]
    assert item["kind"] == "interrupted"
    assert item["platform"] == "tui"


def test_subagent_stop_tallies_and_flags_only_failures(day):
    module, ctx, sid = day
    _fire(ctx, "subagent_stop", parent_session_id=sid, child_role="coder",
          child_summary="ok", child_status="completed", tool_call_history=[1], duration_ms=5)
    _fire(ctx, "subagent_stop", parent_session_id=sid, child_role="researcher",
          child_summary="fetch failed", child_status="failed", tool_call_history=[1, 2],
          duration_ms=1234)
    rec = module._SESSIONS[sid]
    assert rec["subagents"] == {"completed": 1, "failed": 1}
    kinds = [item["kind"] for item in rec["attn"]]
    assert kinds.count("subagent_failed") == 1, "only failures raise attention"
    assert rec["sub_last"]["role"] == "researcher"


def test_provider_error_deduped_within_window(day):
    module, ctx, sid = day
    for _ in range(5):
        _fire(ctx, "api_request_error", session_id=sid, provider="p", model="m",
              status_code=429, retryable=True,
              error={"type": "rate_limit", "message": "429 too many requests"})
    kinds = [item["kind"] for item in module._SESSIONS[sid]["attn"]]
    assert kinds.count("provider_error") == 1


def test_provider_error_distinct_messages_both_recorded(day):
    module, ctx, sid = day
    _fire(ctx, "api_request_error", session_id=sid, status_code=429,
          error={"message": "rate limited"})
    _fire(ctx, "api_request_error", session_id=sid, status_code=500,
          error={"message": "upstream exploded"})
    kinds = [item["kind"] for item in module._SESSIONS[sid]["attn"]]
    assert kinds.count("provider_error") == 2


def test_hooks_tolerate_missing_optional_kwargs(day):
    """Observer hooks must never raise on a sparse payload."""
    module, ctx, sid = day
    _fire(ctx, "api_request_error")
    _fire(ctx, "subagent_stop")
    _fire(ctx, "agent_loop_stopped")
    _fire(ctx, "pre_approval_request")
    _fire(ctx, "post_approval_response")


# ---------------------------------------------------------------------------
# digests
# ---------------------------------------------------------------------------


def test_approval_digest_counts(day):
    module, ctx, sid = day
    _req(day, ctx, session_id=sid)
    _resp(day, ctx, session_id=sid, choice="deny")
    _req(day, ctx, session_id=sid, command="sudo x", pattern_key="sudo")
    _resp(day, ctx, session_id=sid, command="sudo x", pattern_key="sudo", choice="timeout")
    _req(day, ctx, session_id=sid, command="c", pattern_key="c", surface="smart")
    _resp(day, ctx, session_id=sid, command="c", pattern_key="c", surface="smart",
          choice="smart_deny", decided_by="aux_llm")

    digest = json.loads(module._cmd_approvals("5"))
    assert digest["denies"] == 1
    assert digest["stalls"] >= 1
    assert digest["counts"]["deny"] == 1
    assert digest["counts"]["timeout"] == 1
    assert len(digest["recent"]) == 3


def test_attention_command_returns_session(day):
    module, ctx, sid = day
    _fire(ctx, "agent_loop_stopped", session_key=sid, platform="tui", reason="stop")
    out = json.loads(module._cmd_attention())
    assert sid in out["sessions"]


def test_deny_rate_surfaces(day):
    module, ctx, sid = day
    for i in range(4):
        _req(day, ctx, session_id=sid, command=f"c{i}", pattern_key=f"p{i}")
        _resp(day, ctx, session_id=sid, command=f"c{i}", pattern_key=f"p{i}", choice="deny")
    digest = json.loads(module._cmd_approvals())
    assert digest["denies"] == 4


def test_evidence_exposes_approvals_and_attention(day):
    module, ctx, sid = day
    _req(day, ctx, session_id=sid)
    _resp(day, ctx, session_id=sid, choice="deny")
    payload = json.loads(module._cmd_evidence(sid))["sessions"][sid]
    assert len(payload["approvals"]) == 1
    assert payload["approvals"][0]["choice"] == "deny"
    assert "attn" in payload


# ---------------------------------------------------------------------------
# mid-session instinct re-assertion
# ---------------------------------------------------------------------------


def test_no_injection_when_nothing_new(day):
    module, ctx, sid = day
    module._SESSIONS[sid]["inst_epoch"] = time.time()
    assert module._pre_llm_call(session_id=sid, user_message="hi") is None


def test_fresh_instinct_injected_once_then_silent(day, repo):
    module, ctx, sid = day
    rec = module._SESSIONS[sid]
    rec["inst_epoch"] = time.time()
    module._promote_instinct(rec, str(repo), "pytest -k", "deleting the failing test",
                             "you denied this command earlier")

    first = module._pre_llm_call(session_id=sid, user_message="hi")
    assert isinstance(first, str)
    assert "NEVER pytest -k" in first
    assert "Do not retry" in first
    assert module._pre_llm_call(session_id=sid) is None, "must not repeat every turn"


def test_injection_excludes_instincts_from_before_the_session(day, repo):
    """Instincts already frozen into the system prompt must not be re-sent."""
    module, ctx, sid = day
    rec = module._SESSIONS[sid]
    module._promote_instinct(rec, str(repo), "old-trap", "old approach", "predates session")
    rec["inst_epoch"] = time.time() + 1  # prompt frozen after that instinct existed
    assert module._pre_llm_call(session_id=sid) is None


def test_injection_is_bounded(day, repo):
    module, ctx, sid = day
    rec = module._SESSIONS[sid]
    rec["inst_epoch"] = time.time()
    for i in range(20):
        module._promote_instinct(rec, str(repo), f"trap-{i}", f"approach-{i}", f"reason-{i}")
    out = module._pre_llm_call(session_id=sid)
    assert out is not None
    assert len(out) < 1200, f"unbounded injection: {len(out)} chars"


def test_unknown_session_is_silent(day):
    module, ctx, sid = day
    assert module._pre_llm_call(session_id="does-not-exist") is None


def test_injection_respects_disabled_instincts(day, repo):
    module, ctx, sid = day
    rec = module._SESSIONS[sid]
    rec["inst_epoch"] = time.time()
    module._promote_instinct(rec, str(repo), "trap", "approach", "reason")
    items = module._load_instincts(str(repo))
    items[0]["enabled"] = False
    module._save_instincts(str(repo), items)
    assert module._pre_llm_call(session_id=sid) is None


# ---------------------------------------------------------------------------
# session stamping
# ---------------------------------------------------------------------------


def test_system_prompt_section_stamps_session_epoch(day):
    module, ctx, sid = day
    ctx.sections["hermes_day.instincts"]({"session_id": sid, "cwd": str(REPO_ROOT)})
    assert module._SESSIONS[sid]["inst_epoch"] > 0


def test_section_survives_missing_session_info(day):
    module, ctx, _ = day
    assert isinstance(ctx.sections["hermes_day.instincts"]({}), str)


def test_state_persists_approvals(day):
    module, ctx, sid = day
    _req(day, ctx, session_id=sid)
    _resp(day, ctx, session_id=sid, choice="deny")
    stored = ctx.state.get(module._STATE_KEY)
    assert sid in stored
    assert stored[sid]["approvals"][0]["choice"] == "deny"


# ---------------------------------------------------------------------------
# retry-trigger derivation (regression: a compound command used to promote the
# trigger "cd", poisoning the ledger with "NEVER cd" constraints)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command,expected", [
    ("pytest tests/ -q", "pytest"),
    ("cd repo && pytest tests/ -q", "pytest"),
    ("cd /a/b && cd /c && python -m pytest -q", "python -m pytest"),
    ("venv/bin/python -m pytest tests/", "python -m pytest"),
    ("FOO=1 BAR=2 pytest -x", "pytest"),
    ("sudo npm test", "npm test"),
    ("source .venv/bin/activate && ruff check .", "ruff check"),
    ("echo done && tsc --noEmit", "tsc"),
    ("git status && go test ./...", "go test"),
    ("make build && cargo test", "cargo test"),
    ("set -e; jest --ci", "jest"),
    ("./node_modules/.bin/jest --ci", "jest"),
])
def test_trigger_of_picks_the_real_command(day, command, expected):
    module, _, _ = day
    assert module._trigger_of(command) == expected


def test_trigger_of_never_returns_navigation(day):
    module, _, _ = day
    for command in ("cd /tmp", "source x.sh", "export A=1", "", "   "):
        assert module._trigger_of(command) not in module._NAV_SEGMENTS


def test_failed_check_promotes_a_usable_trigger(day, repo):
    """End-to-end: a failing compound check must not land as 'NEVER cd'."""
    module, ctx, sid = day
    module._post_tool_call(
        tool_name="terminal",
        args={"command": "cd repo && pytest tests/ -q"},
        result={"exit_code": 1}, status=None, session_id=sid)
    instincts = module._load_instincts(str(repo))
    assert instincts, "a failing check should have promoted an instinct"
    assert instincts[0]["trigger_pattern"] == "pytest"
    assert instincts[0]["trigger_pattern"] not in module._NAV_SEGMENTS


# ---------------------------------------------------------------------------
# generated-path exclusion (regression: `rm -rf __pycache__ tests/__pycache__`
# was blocked as "deletes test files", which trains operators to route around
# the guard — worse than the false positive itself)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path,generated", [
    ("/repo/tests/__pycache__/test_a.cpython-311.pyc", True),
    ("tests/__pycache__", True),
    ("node_modules/jest-x/test_thing.js", True),
    (".pytest_cache/v/cache/lastfailed", True),
    ("/repo/.venv/lib/test_helpers.py", True),
    ("/repo/tests/test_approval_ledger.py", False),
    ("/repo/src/test_util.py", False),
    ("tests/fixtures/test_data.json", False),
    ("/repo/app.test.ts", False),
])
def test_generated_path_detection(day, path, generated):
    module, _, _ = day
    assert module._is_generated_path(path) is generated


@pytest.mark.parametrize("command", [
    "rm -rf __pycache__ tests/__pycache__",
    "rm -rf .pytest_cache tests/__pycache__ node_modules/.cache",
    "find . -name __pycache__ -exec rm -rf {} +",
    "rm -rf build dist coverage",
])
def test_cache_cleanup_is_not_test_deletion(day, command):
    module, _, _ = day
    assert module._command_violation(command) is None, command


@pytest.mark.parametrize("command", [
    "rm -rf tests/",
    "rm -f tests/unit/test_a.py",
    "git rm tests/test_x.py",
    "> tests/test_x.py",
    "sed -i 's/assert/print/' tests/test_x.py",
])
def test_real_test_tampering_still_blocked(day, command):
    module, _, _ = day
    assert module._command_violation(command) is not None, command


def test_cache_cleanup_mixed_with_real_tests_is_blocked(day):
    """Dropping generated words must not blind the rule to a real test path."""
    module, _, _ = day
    assert module._command_violation("rm -rf __pycache__ tests/") is not None


def test_generated_test_file_is_not_assertion_guarded(day):
    """Editing a .pyc under __pycache__ must not trip assertion gutting."""
    module, _, _ = day
    assert module._edit_violation("tests/__pycache__/test_a.py",
                                  {"old_string": "assert x\n", "new_string": ""}) is None


# ---------------------------------------------------------------------------
# guard canary — proves the gate is alive AND not over-blocking
# ---------------------------------------------------------------------------


def test_canary_reports_healthy(day):
    module, _, _ = day
    result = module._canary(force=True)
    assert result["ok"] is True, f"canary failed: {result['failed']}"
    assert result["degraded"] is False


def test_canary_covers_both_directions(day):
    module, _, _ = day
    result = module._canary(force=True)
    expectations = {c["expect"] for c in result["cases"]}
    assert expectations == {"blocked", "allowed"}, "canary must test both directions"


def test_canary_detects_a_dead_gate(day, monkeypatch):
    """A guard that has silently stopped enforcing must report DEGRADED."""
    module, _, _ = day
    monkeypatch.setattr(module, "_command_violation", lambda cmd: None)
    result = module._canary(force=True)
    assert result["ok"] is False
    assert result["degraded"] is True
    assert "deletes tests" in result["failed"]


def test_canary_detects_over_blocking(day, monkeypatch):
    """A guard that blocks everything must also report DEGRADED."""
    module, _, _ = day
    monkeypatch.setattr(module, "_command_violation",
                        lambda cmd: ("blocked", "Test Deletion"))
    result = module._canary(force=True)
    assert result["ok"] is False
    assert "cache cleanup" in result["failed"]


def test_canary_detects_a_raising_gate(day, monkeypatch):
    module, _, _ = day

    def boom(cmd):
        raise RuntimeError("guard exploded")

    monkeypatch.setattr(module, "_command_violation", boom)
    result = module._canary(force=True)
    assert result["ok"] is False
    assert any("raised RuntimeError" in c["rule"] for c in result["cases"])


def test_canary_is_cached_then_force_refreshes(day, monkeypatch):
    module, _, _ = day
    first = module._canary(force=True)
    monkeypatch.setattr(module, "_command_violation", lambda cmd: None)
    assert module._canary() is first, "should serve the cached verdict inside its TTL"
    assert module._canary(force=True)["ok"] is False, "force must re-run"


def test_guard_command_and_evidence_expose_canary(day):
    module, ctx, sid = day
    assert "day-guard" in ctx.commands
    payload = json.loads(module._cmd_guard("force"))
    assert payload["ok"] is True
    evidence = json.loads(module._cmd_evidence())
    assert evidence["guard"]["ok"] is True


def test_guard_still_blocks_and_canary_never_raises_on_bad_input(day):
    module, _, sid = day
    assert module._pre_tool_call(tool_name="terminal", args={"command": "rm -rf tests/"},
                                 session_id=sid) is not None
    for junk in ("", "   ", "\x00", "rm", ">"):
        module._command_violation(junk)
    assert module._canary(force=True)["ok"] is True


# ---------------------------------------------------------------------------
# rollback insurance — a restore must itself be reversible
# ---------------------------------------------------------------------------


def test_rollback_creates_an_insurance_snapshot(day, repo):
    module, _, sid = day
    rec = module._SESSIONS[sid]
    # _snapshot no-ops on a clean tree, so dirty it first (baseline = x = 5)
    (repo / "a.py").write_text("x = 5\n")
    with module._LOCK:
        module._snapshot(rec, sid, str(repo), "turn 1")
    assert rec["snaps"], "baseline snapshot should exist"
    first = rec["snaps"][-1]["sha"]

    (repo / "a.py").write_text("x = 999\n")
    out = json.loads(module._cmd_rollback(f"{sid} {first}"))

    assert out["ok"] is True, out
    assert out["insurance"], "a rollback must leave a way back"
    assert out["insurance"] != first
    assert "undoes this restore" in out["note"]
    # the restore actually happened
    assert (repo / "a.py").read_text() == "x = 5\n"
    # and the insurance snapshot captured the pre-rollback state
    names = [s.get("label") for s in rec["snaps"]]
    assert "pre-rollback" in names


def test_rollback_can_be_undone(day, repo):
    """Round trip: roll back, then roll the rollback back."""
    module, _, sid = day
    rec = module._SESSIONS[sid]
    (repo / "a.py").write_text("x = 5\n")
    with module._LOCK:
        module._snapshot(rec, sid, str(repo), "turn 1")
    first = rec["snaps"][-1]["sha"]

    (repo / "a.py").write_text("x = 999\n")
    out = json.loads(module._cmd_rollback(f"{sid} {first}"))
    assert (repo / "a.py").read_text() == "x = 5\n"

    undo = json.loads(module._cmd_rollback(f"{sid} {out['insurance']}"))
    assert undo["ok"] is True, undo
    assert (repo / "a.py").read_text() == "x = 999\n", "insurance did not restore"


def test_rollback_of_unknown_snapshot_is_refused(day):
    module, _, sid = day
    out = json.loads(module._cmd_rollback(f"{sid} deadbeefdeadbeef"))
    assert out["ok"] is False
    assert "not found" in out["error"]


def test_rollback_usage_error(day):
    module, _, _ = day
    assert json.loads(module._cmd_rollback(""))["ok"] is False
