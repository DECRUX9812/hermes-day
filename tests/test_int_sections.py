"""Integration tests — Phase 3+4 wiring of the mandate modules into __init__.py.

Phase 4 (conditional context): paths that enter a session's context activate the
owning directory's GOTCHAS.md exactly once, through the pre_llm_call channel,
with a size receipt and the 60-line standing cap.

Phase 3 (tool-set router): pure group selection over a (name, one-liner)
catalog, exercised through the wiring with the gate.tool_router flag.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conftest import _fire  # noqa: E402


@pytest.fixture()
def gotchas_repo(repo: Path) -> Path:
    """The shared git repo fixture, plus GOTCHAS.md files in two subdirs."""
    (repo / "src" / "foo").mkdir(parents=True)
    (repo / "src" / "foo" / "GOTCHAS.md").write_text(
        "foo footgun: never mutate the index directly", encoding="utf-8")
    (repo / "src" / "bar").mkdir(parents=True)
    (repo / "src" / "bar" / "GOTCHAS.md").write_text(
        "bar footgun: config lives in etcd", encoding="utf-8")
    (repo / "src" / "foo" / "x.py").write_text("x = 1\n", encoding="utf-8")
    return repo


def _pre_llm(ctx, **kw):
    """Collect every pre_llm_call callback's contribution (host joins them)."""
    return [r for r in (cb(**kw) for cb in ctx.hooks["pre_llm_call"]) if r]


# --------------------------------------------------------------- phase 4


def test_gotchas_injected_once_for_touched_dir(day, gotchas_repo):
    module, ctx, sid = day
    target = gotchas_repo / "src" / "foo" / "x.py"
    # a read (not an edit) is still a file entering context
    _fire(ctx, "post_tool_call", tool_name="read_file",
          args={"path": str(target)}, result="x = 1\n", status="ok",
          session_id=sid)

    first = _pre_llm(ctx, session_id=sid, user_message="look at x.py")
    assert first, "expected a gotchas block for src/foo"
    block = first[0]
    assert "foo footgun: never mutate the index directly" in block
    assert "bar footgun" not in block          # untouched sibling stays out
    # size receipt, like the instincts receipt
    assert any("lines" in ln and "gotchas" in ln.lower()
               for ln in block.splitlines() if ln.startswith("#"))
    assert len(block.splitlines()) <= module._GOTCHAS_MAX + 1  # +1 receipt

    second = _pre_llm(ctx, session_id=sid, user_message="again")
    assert not any("foo footgun" in (p or "") for p in second), \
        "src/foo gotchas must inject exactly once per session"


def test_gotchas_new_dir_injects_on_later_turn(day, gotchas_repo):
    module, ctx, sid = day
    _fire(ctx, "post_tool_call", tool_name="read_file",
          args={"path": str(gotchas_repo / "src" / "foo" / "x.py")},
          result="ok", status="ok", session_id=sid)
    _pre_llm(ctx, session_id=sid, user_message="first")
    _fire(ctx, "post_tool_call", tool_name="edit_file",
          args={"file_path": str(gotchas_repo / "src" / "bar" / "y.py")},
          result="ok", status="ok", session_id=sid)
    later = _pre_llm(ctx, session_id=sid, user_message="now bar")
    assert any("bar footgun" in (p or "") for p in later)
    assert not any("foo footgun" in (p or "") for p in later)


def test_gotchas_silent_when_nothing_touched(day, gotchas_repo):
    module, ctx, sid = day
    out = _pre_llm(ctx, session_id=sid, user_message="just chatting")
    assert not any("footgun" in (p or "") for p in out)


def test_gotchas_silent_for_dir_without_gotchas(day, gotchas_repo):
    module, ctx, sid = day
    plain = gotchas_repo / "docs"
    plain.mkdir()
    (plain / "z.md").write_text("hi\n")
    _fire(ctx, "post_tool_call", tool_name="read_file",
          args={"path": str(plain / "z.md")}, result="hi", status="ok",
          session_id=sid)
    out = _pre_llm(ctx, session_id=sid, user_message="hi")
    assert not any("footgun" in (p or "") for p in out)


def test_gotchas_respects_gate_flag(day, gotchas_repo, monkeypatch):
    module, ctx, sid = day
    monkeypatch.setattr(module, "_enabled",
                        lambda section: section != "gotchas")
    _fire(ctx, "post_tool_call", tool_name="read_file",
          args={"path": str(gotchas_repo / "src" / "foo" / "x.py")},
          result="ok", status="ok", session_id=sid)
    out = _pre_llm(ctx, session_id=sid, user_message="hi")
    assert not any("footgun" in (p or "") for p in out)


# --------------------------------------------------------------- phase 3


_CATALOG = {
    "github": {"create_pull_request": "Create a GitHub pull request",
               "list_pull_requests": "List GitHub pull requests for review",
               "request_review": "Request a PR review on GitHub"},
    "spotify": {"play_track": "Play a Spotify track",
                "search_playlist": "Search Spotify playlists"},
}


def test_router_advisory_off_by_default(day, monkeypatch):
    module, ctx, sid = day
    module._SESSIONS[sid] = module._new_rec()
    monkeypatch.setattr(module, "_tool_catalog", lambda: _CATALOG)
    out = _pre_llm(ctx, session_id=sid,
                   user_message="review my pull request on github")
    assert not any("github" in (p or "").lower() for p in out), \
        "tool_router must default OFF"


def test_router_advisory_when_flagged(day, monkeypatch):
    module, ctx, sid = day
    module._SESSIONS[sid] = module._new_rec()
    monkeypatch.setattr(module, "_tool_catalog", lambda: _CATALOG)
    monkeypatch.setattr(module, "_flag",
                        lambda section, default: section == "tool_router")
    out = _pre_llm(ctx, session_id=sid,
                   user_message="review my pull request on github")
    assert any("github" in (p or "") for p in out)
    assert not any("spotify" in (p or "") for p in out)
