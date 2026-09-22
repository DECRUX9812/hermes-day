"""Phase 5 wiring: ONE repo manifest shared by N read-only background lanes.

The helper ``_manifest_lane_context(root)`` builds (or reuses) the single
repo-state manifest and returns the JSON a caller drops into a
``delegate_task`` child's ``context``. A child spawned with the payload's
``goal_prefix`` in its goal is marked read-only via ``subagent_start``;
``_pre_tool_call`` then vetoes its write attempts.
"""

import json
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import hday_manifest as hm  # noqa: E402
from conftest import _fire  # noqa: E402


def test_one_build_serves_n_lanes(day, monkeypatch):
    module, ctx, sid = day
    root = module._SESSIONS[sid]["_root"]
    builds = []
    real = hm.build_manifest
    monkeypatch.setattr(
        hm, "build_manifest",
        lambda *a, **k: builds.append(1) or real(*a, **k))

    payloads = [module._manifest_lane_context(root) for _ in range(4)]

    # N=4 consumers, ONE walk — the single build is what gets handed out.
    assert len(builds) == 1
    assert all(p["ok"] for p in payloads)
    assert len({p["manifest"]["built_at"] for p in payloads}) == 1
    led = next(iter(module._MANIFEST_RUNS.values()))
    assert led["builds"] == 1
    assert led["consumers"] == 4
    # disk hand-off under the workspace dir actually happened
    on_disk, why = hm.load(payloads[0]["manifest_path"])
    assert why is None
    assert on_disk["total_loc"] == payloads[0]["manifest"]["total_loc"]
    # the payload is the JSON a caller drops into delegate_task context
    assert json.loads(json.dumps(payloads[0]))["manifest"]["root"] == payloads[0]["manifest"]["root"]


def test_lane_context_reuses_and_rebuilds(day, monkeypatch):
    module, ctx, sid = day
    root = module._SESSIONS[sid]["_root"]
    builds = []
    real = hm.build_manifest
    monkeypatch.setattr(
        hm, "build_manifest",
        lambda *a, **k: builds.append(1) or real(*a, **k))

    first = module._manifest_lane_context(root)
    second = module._manifest_lane_context(root)
    assert len(builds) == 1
    assert second["reused"] is True and first["reused"] is False
    assert second["manifest"]["built_at"] == first["manifest"]["built_at"]
    # an explicit rebuild walks again
    third = module._manifest_lane_context(root, rebuild=True)
    assert len(builds) == 2 and third["reused"] is False
    assert third["manifest"]["built_at"] >= first["manifest"]["built_at"]


def test_read_only_lane_vetoes_writes(day):
    module, ctx, parent = day
    root = module._SESSIONS[parent]["_root"]
    payload = module._manifest_lane_context(root)
    child = "lane-child-1"
    _fire(ctx, "subagent_start", parent_session_id=parent,
          child_session_id=child,
          child_goal=f"{payload['goal_prefix']}review a.py")
    assert child in module._READ_ONLY_LANES
    # a blocked call lands on a rec that needs a root for instinct promotion
    module._rec(child)["_root"] = root
    pre = module._pre_tool_call
    for tool, args in (
        ("write_file", {"path": "/x/notes.py", "content": "x = 1\n"}),
        ("edit_file", {"path": "/x/notes.py",
                       "old_string": "a", "new_string": "b"}),
        ("terminal", {"command": "echo hi > notes.py"}),
        ("terminal", {"command": "rm -f notes.py"}),
        ("terminal", {"command": "git commit -am wip"}),
    ):
        out = pre(tool_name=tool, args=args, session_id=child)
        assert out and out["action"] == "block", (tool, args, out)


def test_lane_allows_reads_and_untagged_children(day):
    module, ctx, parent = day
    root = module._SESSIONS[parent]["_root"]
    payload = module._manifest_lane_context(root)
    _fire(ctx, "subagent_start", parent_session_id=parent,
          child_session_id="lane-1",
          child_goal=f"{payload['goal_prefix']}review the diff")
    _fire(ctx, "subagent_start", parent_session_id=parent,
          child_session_id="lane-2", child_goal="review without the tag")
    assert "lane-1" in module._READ_ONLY_LANES
    assert "lane-2" not in module._READ_ONLY_LANES
    pre = module._pre_tool_call
    # reads, checks and inspection commands pass through the lane veto
    assert pre(tool_name="terminal", args={"command": "git status --short"},
               session_id="lane-1") is None
    assert pre(tool_name="terminal", args={"command": "pytest tests/ -q"},
               session_id="lane-1") is None
    assert pre(tool_name="read_file", args={"path": "/x/a.py"},
               session_id="lane-1") is None
    # an untagged child writing a non-test file is not lane-restricted: the
    # verdict must not be the read-only-lane block. (An OUTER-scope path like
    # /x/notes.py legitimately trips the typed gate's escalation — see
    # test_outer_scope_write_in_untagged_child_escalates — so this contract is
    # asserted on an in-scope path, which is what the lane rule itself governs.)
    assert pre(tool_name="write_file",
               args={"path": os.path.join(root, "notes.py"), "content": "x"},
               session_id="lane-2") is None
    # the lane ends with the child
    _fire(ctx, "subagent_stop", parent_session_id=parent,
          child_session_id="lane-1", child_role="leaf",
          child_status="completed")
    assert "lane-1" not in module._READ_ONLY_LANES
    # lane ended → the lane veto is gone (in-scope path; see the note above on
    # why this contract is asserted in-scope)
    assert pre(tool_name="write_file",
               args={"path": os.path.join(root, "notes.py"), "content": "x"},
               session_id="lane-1") is None


def test_day_manifest_command(day):
    module, ctx, sid = day
    root = module._SESSIONS[sid]["_root"]
    cmd = ctx.commands["day-manifest"]
    # context mode: the delegate hand-off JSON
    out = json.loads(cmd(f"{root} context"))
    assert out["ok"] and out["lane"] == "read-only"
    assert out["goal_prefix"].strip() == module._LANE_TAG
    assert out["manifest"]["total_loc"] > 0
    assert out["manifest_path"].startswith(root)
    assert os.path.isfile(out["manifest_path"])
    # status mode: what got built, how many consumers it served
    status = json.loads(cmd(root))
    assert status["ok"]
    man = status["manifests"][0]
    assert man["state"] == "fresh" and man["builds"] == 1
    assert man["consumers"] >= 1
    # no-arg: every known root
    listing = json.loads(cmd(""))
    assert listing["ok"] and listing["manifests"]
    # rebuild bumps the build counter
    json.loads(cmd(f"{root} rebuild"))
    again = json.loads(cmd(root))["manifests"][0]
    assert again["builds"] == 2
