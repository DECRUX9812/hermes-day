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

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_INIT = REPO_ROOT / "__init__.py"

# The plugin imports agent-side helpers lazily; make the Hermes install
# importable when the caller has not already put it on sys.path.
HERMES_AGENT = Path(os.environ.get("HERMES_AGENT_DIR", Path.home() / ".hermes" / "hermes-agent"))
if HERMES_AGENT.is_dir() and str(HERMES_AGENT) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT))


class FakeState:
    """Minimal stand-in for ctx.state."""

    def __init__(self) -> None:
        self.store: dict = {}

    def set(self, key, value) -> None:
        self.store[key] = value

    def get(self, key, default=None):
        return self.store.get(key, default)


class FakeCtx:
    """Records what register() wires up, without a live plugin host."""

    def __init__(self) -> None:
        self.state = FakeState()
        self.hooks: dict[str, list] = {}
        self.commands: dict[str, object] = {}
        self.sections: dict[str, object] = {}

    def register_hook(self, name, fn) -> None:
        self.hooks.setdefault(name, []).append(fn)

    def register_command(self, name, fn, **kwargs) -> None:
        self.commands[name] = fn

    def register_system_prompt_section(self, name, fn, position=None, max_chars=None) -> None:
        self.sections[name] = fn

    def register_tool(self, *args, **kwargs) -> None:
        raise AssertionError("hermes-day should not register tools")

    def get_config(self, key):
        return {}


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A disposable git repo, so instinct promotion has somewhere to land."""
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    (path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return path


@pytest.fixture()
def day(repo: Path):
    """Fresh plugin module + registered context, bound to a temp repo."""
    spec = importlib.util.spec_from_file_location("hermes_day_under_test", PLUGIN_INIT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    ctx = FakeCtx()
    module.register(ctx)

    session_id = "sess-test"
    module._SESSIONS[session_id] = module._new_rec()
    module._SESSIONS[session_id]["_root"] = str(repo)

    yield module, ctx, session_id
    module._PENDING_APPROVALS.clear()


def _fire(ctx, hook, **kwargs):
    """Invoke every callback registered for *hook* (register() wires exactly one)."""
    callbacks = ctx.hooks[hook]
    assert callbacks, f"no callback registered for {hook}"
    for callback in callbacks:
        callback(**kwargs)


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
