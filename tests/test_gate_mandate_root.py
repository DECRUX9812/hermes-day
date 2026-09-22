"""TDD tests for the two LIVE gate defects: mandate source + workspace root.

Both were verified live in a real session:

* **mandate source** — ``_pre_llm_call`` recorded the LATEST user message as
  ``rec["user_request"]``. Host notifications ("[ASYNC DELEGATION BATCH
  COMPLETE...]", "[IMPORTANT: Background process ...]", "[System: ...]") are
  delivered as user messages, so they became the mandate; the typed gate then
  judged every later action off-mandate (``intent_consistent`` ~0.25) and — with
  no reachable approval path — blocked it.
* **workspace root** — ``_gate_path_classes`` compared absolute paths against
  the GATEWAY process cwd, so a write inside the session's own repo was classed
  ``outside_workspace`` -> borderline -> judged -> blocked.

Contract under test:
    * only SUBSTANTIVE user messages become the mandate (``user_request``, with
      the first one kept as ``mandate_first``); system notifications never do.
    * no substantive mandate -> the typed phase fails OPEN with an ``unjudged``
      row whose reason marks ``no-mandate`` -- never a block.
    * path classification uses the SESSION's recorded root, never the plugin
      process cwd.
    * the free regex honesty guard is untouched: test deletion / assertion
      gutting / exit-code laundering still block with no mandate in sight.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# helpers (same seams as tests/test_gate_wiring.py)
# ---------------------------------------------------------------------------


def _judge(**signals):
    base = {"irreversible": 0.0, "outer_scope": 0.0, "intent_consistent": 1.0,
            "data_egress": 0.0, "blast_radius": "local", "lane": "hosted",
            "cost": 0.0001}
    base.update(signals)
    return base


def _set_judge(module, monkeypatch, judge):
    monkeypatch.setattr(module, "_GATE_JUDGE", judge)
    # never let the test outcome depend on the real approvals.mode/yolo state
    monkeypatch.setattr(module, "_gate_escalation_available", lambda: True)


def _rows(module, sid):
    return module._SESSIONS[sid]["gate"]


NOTES = (
    "[ASYNC DELEGATION BATCH COMPLETE] 5/5 children verified and merged",
    "[IMPORTANT: Background process 4211 exited with code 1]",
    "[System: context compression dropped 12 messages]",
)


# ---------------------------------------------------------------------------
# (a) notifications are not a mandate -> fail open with a marked row
# ---------------------------------------------------------------------------


def test_notification_only_session_is_never_blocked(day, monkeypatch):
    """A session whose only user messages are notifications has NO mandate:
    the borderline write is not blocked, no paid judgment is bought, and the
    ledger carries an explicit ``unjudged`` / ``no-mandate`` row."""
    module, ctx, sid = day
    calls = []
    # worst case on the old code: off-mandate + machine blast + no approval path
    _set_judge(module, monkeypatch,
               lambda a, u, c: calls.append(u) or
               _judge(intent_consistent=0.2, irreversible=0.9,
                      blast_radius="machine"))
    monkeypatch.setattr(module, "_gate_escalation_available", lambda: False)
    for note in NOTES:
        module._pre_llm_call(session_id=sid, user_message=note, platform="cli")

    assert not module._SESSIONS[sid].get("user_request"), \
        "a system notification must never become the mandate"

    out = module._pre_tool_call(tool_name="write_file",
                                args={"path": "/x/notes.py", "content": "x"},
                                session_id=sid)
    assert out is None, f"no mandate means no off-mandate block: {out}"
    assert calls == [], "no mandate -> the paid judge must not be called at all"
    rows = _rows(module, sid)
    assert rows, "the fail-open must leave a ledger row"
    assert rows[-1]["kind"] == "unjudged" and rows[-1]["allow"] is True
    assert "no-mandate" in rows[-1]["reason"], rows[-1]


@pytest.mark.parametrize("note", NOTES)
def test_each_system_marker_is_skipped(day, note):
    module, ctx, sid = day
    module._pre_llm_call(session_id=sid, user_message=note, platform="cli")
    rec = module._SESSIONS[sid]
    assert not rec.get("user_request") and not rec.get("mandate_first")


def test_short_messages_are_not_a_mandate(day):
    """'ok' / 'go on' are acknowledgements, not a mandate."""
    module, ctx, sid = day
    module._pre_llm_call(session_id=sid, user_message="ok", platform="cli")
    module._pre_llm_call(session_id=sid, user_message="go on", platform="cli")
    assert not module._SESSIONS[sid].get("user_request")


# ---------------------------------------------------------------------------
# (b) a real request is the mandate -- and a later notification cannot replace it
# ---------------------------------------------------------------------------


def test_real_request_is_the_mandate_and_survives_a_notification(day, monkeypatch):
    module, ctx, sid = day
    seen = []
    _set_judge(module, monkeypatch,
               lambda a, u, c: seen.append(u) or
               _judge(irreversible=0.4, blast_radius="workspace"))

    ask = "Tidy the release notes and write the summary to /x/notes.py"
    module._pre_llm_call(session_id=sid, user_message=ask, platform="cli")
    module._pre_llm_call(session_id=sid,
                         user_message=NOTES[0], platform="cli")

    rec = module._SESSIONS[sid]
    assert rec["user_request"] == ask, "a notification must not overwrite the mandate"
    assert rec["mandate_first"] == ask, "the first substantive message is the fallback"

    out = module._pre_tool_call(tool_name="write_file",
                                args={"path": "/x/notes.py", "content": "x"},
                                session_id=sid)
    assert out is None, out
    assert seen == [ask], f"judged against the notification: {seen}"
    assert _rows(module, sid)[-1]["kind"] == "judged"


def test_mandate_first_survives_a_later_substantive_turn(day):
    module, ctx, sid = day
    first = "Fix the gate mandate recording in the plugin"
    module._pre_llm_call(session_id=sid, user_message=first, platform="cli")
    module._pre_llm_call(session_id=sid,
                         user_message="Also update the changelog entry",
                         platform="cli")
    rec = module._SESSIONS[sid]
    assert rec["user_request"] == "Also update the changelog entry"
    assert rec["mandate_first"] == first


# ---------------------------------------------------------------------------
# (c) path classification uses the SESSION root, not the gateway process cwd
# ---------------------------------------------------------------------------


def test_gate_screen_uses_the_session_root(day, repo, tmp_path, monkeypatch):
    module, ctx, sid = day
    # the pytest tmp tree lives under TMPDIR, which the screen exempts as
    # scratch: move the scratch root away so only the session root can
    # classify the repo path as inside.
    monkeypatch.setenv("TMPDIR", str(tmp_path / "not-scratch"))

    inside = module._gate_screen(
        "write_file", {"path": str(repo / "sub" / "x.py"), "content": "x"}, sid)
    assert "outside_workspace" not in inside, inside

    outside = module._gate_screen(
        "write_file", {"path": "/x/notes.py", "content": "x"}, sid)
    assert "outside_workspace" in outside, outside


def test_shell_relative_paths_are_not_outer_scope(day):
    """A relative path token is inside the workspace; only word-initial
    (absolute / ~ / ../) path tokens can be outer-scope."""
    module, ctx, sid = day
    assert "outside_workspace" not in module._gate_screen(
        "terminal", {"command": "grep -n needle tests/test_x.py"}, sid)
    assert "outside_workspace" not in module._gate_screen(
        "terminal", {"command": "python -m pytest tests/ -q"}, sid)
    assert "outside_workspace" in module._gate_screen(
        "terminal", {"command": "cat /etc/passwd"}, sid)


def test_unknown_session_falls_back_to_the_ambient_session_cwd(day, repo, tmp_path,
                                                               monkeypatch):
    """No recorded root -> the session's ambient cwd (agent.runtime_cwd), so a
    path in the session's own repo is still inside the workspace."""
    module, ctx, _sid = day
    rc = pytest.importorskip("agent.runtime_cwd")
    monkeypatch.setenv("TMPDIR", str(tmp_path / "not-scratch"))
    tok = rc.set_session_cwd(str(repo))
    try:
        classes = module._gate_screen(
            "write_file", {"path": str(repo / "x.py"), "content": "x"},
            "sess-ambient")
    finally:
        rc.reset_session_cwd(tok)
    assert "outside_workspace" not in classes, classes
    classes = module._gate_screen(
        "write_file", {"path": "/x/notes.py", "content": "x"}, "sess-ambient")
    assert "outside_workspace" in classes, classes


def test_no_ambient_cwd_falls_back_to_the_process_cwd(day, repo, tmp_path,
                                                      monkeypatch):
    """Last resort stays exactly as before: the plugin process cwd."""
    module, ctx, _sid = day
    rc = pytest.importorskip("agent.runtime_cwd")
    monkeypatch.setenv("TMPDIR", str(tmp_path / "not-scratch"))
    monkeypatch.setattr(rc, "resolve_context_cwd", lambda: None)
    classes = module._gate_screen(
        "write_file", {"path": str(repo / "x.py"), "content": "x"}, "sess-no-cwd")
    assert "outside_workspace" in classes, classes


# ---------------------------------------------------------------------------
# the free regex honesty guard is NOT weakened by any of the above
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cmd", [
    "rm -rf tests/",
    "git rm tests/test_x.py",
    "> tests/test_x.py",
    "pytest -q || true",
    "pytest --passWithNoTests",
])
def test_regex_honesty_guard_blocks_without_a_mandate(day, monkeypatch, cmd):
    module, ctx, sid = day
    monkeypatch.setattr(module, "_GATE_JUDGE", None)
    out = module._pre_tool_call(tool_name="terminal", args={"command": cmd},
                                session_id=sid)
    assert isinstance(out, dict) and out["action"] == "block", out
    assert _rows(module, sid)[-1]["kind"] == "regex_hit"


def test_assertion_gutting_blocks_without_a_mandate(day, monkeypatch):
    """The file-edit half of the honesty guard still blocks with no mandate."""
    module, ctx, sid = day
    # NB: /tmp, not the pytest tmp tree — the scratch TMPDIR carries a
    # ``cache`` component, which _is_generated_path() treats as vendored.
    root = Path(tempfile.mkdtemp(prefix="hday-gut-", dir="/tmp"))
    try:
        (root / "tests").mkdir()
        target = root / "tests" / "test_x.py"
        target.write_text("assert a == 1\nassert b == 2\n")
        monkeypatch.setattr(module, "_GATE_JUDGE", None)
        out = module._pre_tool_call(tool_name="write_file",
                                    args={"path": str(target), "content": "pass\n"},
                                    session_id=sid)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    assert isinstance(out, dict) and out["action"] == "block", out
    assert _rows(module, sid)[-1]["kind"] == "regex_hit"
