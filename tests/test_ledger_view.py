"""Phase 6 — /day cockpit observability: decision ledger, relevance heat,
prompt-section toggles.

Two halves, one contract:

* backend — ``day-ledger`` surfaces hday_gate/hday_ctxscore rows over the
  existing ``command.dispatch`` RPC surface; ``day-sections`` /
  ``day-section-set`` toggle the instincts/gotchas prompt sections; a
  ``hermes_day.gotchas`` system-prompt section is registered.
* frontend — the pure view-model block inside ``desktop/plugin.js`` turns raw
  ledger rows into the displayed columns (lane, judged/cached/unjudged, cost,
  ms) with an honest ``unknown`` state when fields are missing. The bundle
  imports the desktop SDK so it cannot be imported by Node; the marked block
  is extracted verbatim and evaluated under ``node:vm``.

Run: python -m pytest tests/test_ledger_view.py -q
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hday_ctxscore  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_JS = REPO_ROOT / "desktop" / "plugin.js"
DRIVER = REPO_ROOT / "tests" / "node_ledger_driver.cjs"
NODE = shutil.which("node")


def _vm(expr: str, tmp_path: Path):
    """Evaluate *expr* against the shipped bundle's view-model block."""
    expr_file = tmp_path / "expr.js"
    expr_file.write_text(expr)
    out = subprocess.run(
        [NODE, str(DRIVER), str(PLUGIN_JS), str(expr_file)],
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, f"view-model driver failed: {out.stderr.strip()}"
    return json.loads(out.stdout)


def _dispatch(ctx, name, arg=""):
    fn = ctx.commands.get(name)
    assert callable(fn), f"command {name!r} not registered"
    return json.loads(fn(arg))


# ---------------------------------------------------------------------------
# JS view-model — extracted verbatim from desktop/plugin.js, run under node:vm
# ---------------------------------------------------------------------------

@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_bundle_syntax_check():
    out = subprocess.run([NODE, "--check", str(PLUGIN_JS)],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_ledger_view_merges_and_normalizes(tmp_path):
    payload = {
        "gate": [
            {"kind": "judged", "lane": "hosted", "ms": 12.5, "cost": 0.002,
             "action": "terminal", "reason": "allowed: on-mandate",
             "ts": 1758000000000},
            {"kind": "regex_hit", "lane": "local", "ms": 0.4, "cost": 0,
             "action": "terminal", "rule": "Test Deletion"},
            {"lane": "local"},                       # kind/ms/cost missing
        ],
        "ctxscore": [
            {"status": "cached", "lane": "classifier", "ms": 3, "cost": 0.0004,
             "noul": 0.9, "chunk": 0, "disposition": "whole"},
            {"status": "unjudged", "lane": "local", "ms": 0.1, "cost": 0,
             "chunk": 1, "reason": "no-judge"},
        ],
    }
    view = _vm(f"hdayLedgerView({json.dumps(payload)})", tmp_path)
    rows = view["rows"]
    assert len(rows) == 5

    g = rows[0]
    assert g["src"] == "gate" and g["status"] == "judged" and g["lane"] == "hosted"
    assert g["ms"] == 12.5 and g["cost"] == 0.002
    assert g["when"] == 1758000000000

    miss = rows[2]
    assert miss["src"] == "gate" and miss["status"] == "unknown"
    assert miss["lane"] == "local"                # present fields stay real
    assert miss["ms"] is None and miss["cost"] is None   # honest unknown

    c = rows[3]
    assert c["src"] == "ctx" and c["status"] == "cached"
    assert c["lane"] == "classifier" and c["ms"] == 3

    counts = view["counts"]
    assert counts == {"judged": 1, "cached": 1, "unjudged": 1,
                      "regex_hit": 1, "unknown": 1}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_ledger_view_unknown_states(tmp_path):
    rows = _vm(
        "hdayLedgerView({gate: [{kind: 'bogus', lane: 'mars', ms: 'x', cost: {}}], "
        "ctxscore: 'garbage'}).rows",
        tmp_path)
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "unknown" and r["lane"] == "unknown"
    assert r["ms"] is None and r["cost"] is None
    # a wholly missing payload is an empty view, never a crash
    assert _vm("hdayLedgerView(null).rows", tmp_path) == []


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_heat_ticks_from_ctxscore_rows(tmp_path):
    ticks = _vm(
        "hdayHeatTicks([{noul: 0.9, disposition: 'whole'},"
        "              {noul: 0.5},"
        "              {noul: 0.2},"
        "              {noul: null, status: 'unjudged'},"
        "              {}])",
        tmp_path)
    assert [t["tone"] for t in ticks] == ["hot", "warm", "cold",
                                        "unknown", "unknown"]
    assert abs(ticks[0]["heat"] - 0.9) < 1e-9
    assert ticks[3]["heat"] is None


# ---------------------------------------------------------------------------
# Backend — the RPC surface the cockpit consumes
# ---------------------------------------------------------------------------

def test_day_ledger_command_registered(day):
    _module, ctx, _sid = day
    out = _dispatch(ctx, "day-ledger")
    assert out["ok"] is True
    assert out["gate"] == [] and out["ctxscore"] == []


def test_gate_rows_appear_when_gate_fires(day, monkeypatch):
    """Merged contract: /day-ledger reads the ONE real gate ledger, not a shadow.

    A deny-listed call lands ``regex_hit``; a borderline-but-not-denied call
    with no judge reachable lands ``unjudged`` (fail-open, allow) — both on the
    real path in _pre_tool_call.
    """
    module, ctx, sid = day
    monkeypatch.setattr(module, "_GATE_JUDGE", None)  # no judge -> deterministic
    module._pre_tool_call(tool_name="terminal",
                          args={"command": "rm -rf tests/"}, session_id=sid)
    module._pre_tool_call(tool_name="terminal",
                          args={"command": "sudo systemctl restart hermes"}, session_id=sid)
    out = _dispatch(ctx, "day-ledger", sid)
    kinds = [r["kind"] for r in out["gate"]]
    assert "regex_hit" in kinds and "unjudged" in kinds
    hit = next(r for r in out["gate"] if r["kind"] == "regex_hit")
    assert hit["lane"] == "local" and hit["ms"] >= 0 and hit["cost"] == 0.0
    assert hit.get("allow") is False
    assert all(r.get("sid") == sid for r in out["gate"])


def test_day_ledger_surfaces_ctxscore_rows(day):
    _module, ctx, _sid = day
    hday_ctxscore.reset_ledger()
    res = hday_ctxscore.score(["alpha chunk", "beta chunk"], "goal", judge=None)
    out = _dispatch(ctx, "day-ledger")
    assert len(out["ctxscore"]) == len(res.rows)
    assert all(r["status"] == "unjudged" for r in out["ctxscore"])


def test_vacuum_trim_feeds_ctxscore_ledger(day, monkeypatch):
    """No ctxscore judge -> the legacy trim runs, and the observe-only pass keeps
    ONE ledger of what the scorer would have discriminated (unjudged rows)."""
    module, ctx, sid = day
    monkeypatch.setattr(module, "_CTX_JUDGE", None)
    monkeypatch.setattr(module, "_CTX_JUDGE_RESOLVED", True)  # pin: no judge
    hday_ctxscore.reset_ledger()
    _dispatch(ctx, "day-vacuum", sid)
    dump = "\n".join(f"line {i}" for i in range(60))
    out = module._transform_tool_result(tool_name="terminal", args={},
                                        result=dump, session_id=sid)
    assert out is not None and "Trimmed" in out
    rows = _dispatch(ctx, "day-ledger", sid)["ctxscore"]
    assert rows, "vacuum-trimmed dump should leave ctxscore rows"
    assert all(r["status"] == "unjudged" for r in rows)
    assert all(r.get("sid") == sid for r in rows)


def test_sections_command_lists_toggleable(day):
    _module, ctx, _sid = day
    out = _dispatch(ctx, "day-sections")
    assert out["ok"] is True
    assert {"instincts", "gotchas"} <= set(out["sections"])
    assert out["sections"]["instincts"]["enabled"] is True
    assert out["sections"]["gotchas"]["enabled"] is True


def test_section_toggle_changes_next_prompt(day, repo):
    module, ctx, sid = day
    (repo / ".hermes").mkdir(exist_ok=True)
    (repo / ".hermes" / "instincts.json").write_text(json.dumps({"instincts": [{
        "id": "i1", "trigger_pattern": "rm -rf tests",
        "disproven_approach": "deleting the suite", "reason": "blocked",
        "enabled": True, "hits": 1}]}))
    module._remember_root(str(repo))
    info = {"session_id": sid, "cwd": str(repo)}
    assert module._instincts_section(info)          # enabled by default
    assert _dispatch(ctx, "day-section-set", "instincts 0")["ok"] is True
    assert module._instincts_section(info) == ""    # next session: gone
    assert _dispatch(ctx, "day-section-set", "instincts 1")["ok"] is True
    assert module._instincts_section(info)


def test_section_set_rejects_unknown(day):
    _module, ctx, _sid = day
    out = _dispatch(ctx, "day-section-set", "bogus 1")
    assert out["ok"] is False


def test_gotchas_section_registered_and_toggleable(day, repo):
    _module, ctx, sid = day
    (repo / "GOTCHAS.md").write_text("never run pytest with -x here")
    fn = ctx.sections.get("hermes_day.gotchas")
    assert callable(fn), "hermes_day.gotchas prompt section not registered"
    body = fn({"session_id": sid, "cwd": str(repo)})
    assert "never run pytest with -x here" in body
    assert _dispatch(ctx, "day-section-set", "gotchas 0")["ok"] is True
    assert fn({"session_id": sid, "cwd": str(repo)}) == ""
