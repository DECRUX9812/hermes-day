"""Phase 2 wiring tests — query-aware scoring inside the armed vacuum.

When the context vacuum is armed AND a judge is reachable, a >40-line tool
dump is chunked and scored against the session's current goal: a chunk carrying
a needle fact survives verbatim (disposition ``whole``) while irrelevant chunks
collapse to marked placeholders — never silently. With no judge the hook
degrades to the legacy head/tail trim, and an unarmed session is untouched.

The judge seam is ``_CTX_JUDGE``: tests inject an in-process stub, production
resolves a lane lazily. No network anywhere here.
"""

from __future__ import annotations

import json
from types import SimpleNamespace


ANCHOR = "needle fact NEEDLE-7734 lives in this chunk"


def make_dump() -> str:
    """A >40-line tool dump: junk, then a needle section, then junk.

    The needle sits in the MIDDLE on purpose — the legacy head-6/tail-3 trim
    destroys it, so its survival proves the scoring path ran, not luck.
    """
    junk_cache = ["=== cache ==="] + [f"cache entry {i}: item {i} of many" for i in range(15)]
    needle = ["## build output", ANCHOR] + [f"step {i}: compiling widget_{i}" for i in range(30)]
    junk_warn = ["## warnings"] + [f"warning {i}: deprecated widget_{i}" for i in range(12)]
    return "\n\n".join("\n".join(s) for s in (junk_cache, needle, junk_warn))


class StubJudge:
    """In-process judge: the chunk holding the anchor is relevant, the rest is junk."""

    lane = "local"

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, state, questions):
        self.calls.append({"state": state, "questions": questions})
        texts = {c["id"]: c["text"] for c in state["chunks"]}
        out = {}
        for qid, q in questions.items():
            _kind, cid = qid.split(":", 1)
            relevant = ANCHOR in texts[cid]
            if q["type"] == "noul":
                out[qid] = SimpleNamespace(kind="noul", noul=0.97 if relevant else 0.03)
            else:
                out[qid] = SimpleNamespace(kind="choice", choice="whole" if relevant else "hide")
        return out


def _arm(day, judge=None):
    module, _ctx, sid = day
    module._VACUUM_ARMED.add(sid)
    module._CTX_JUDGE = judge
    return module, sid


def test_armed_vacuum_keeps_needle_collapses_junk(day, tmp_path, monkeypatch):
    module, sid = _arm(day, StubJudge())
    monkeypatch.setattr(module, "_vacuum_dir", lambda s: str(tmp_path / "vac"))
    out = module._transform_tool_result(
        tool_name="terminal", args={}, result=make_dump(), session_id=sid)

    assert isinstance(out, str)
    assert ANCHOR in out                        # needle survives — disposition whole
    assert "cache entry 7:" not in out          # junk chunk collapsed
    assert "warning 5:" not in out
    assert "[ctxscore" in out                   # every drop leaves a marker line
    assert len(module._CTX_JUDGE.calls) == 1    # one batched request, not one per chunk


def test_vacuum_scoring_uses_current_goal(day, tmp_path, monkeypatch):
    """The judge sees the session's latest user message as the goal."""
    module, sid = _arm(day, StubJudge())
    monkeypatch.setattr(module, "_vacuum_dir", lambda s: str(tmp_path / "vac"))
    module._pre_llm_call(session_id=sid,
                         user_message="recover NEEDLE-7734 from the build log")
    module._transform_tool_result(
        tool_name="terminal", result=make_dump(), session_id=sid)
    judge = module._CTX_JUDGE
    assert judge.calls, "judge was never invoked"
    assert judge.calls[0]["state"]["goal"] == "recover NEEDLE-7734 from the build log"


def test_vacuum_ctxscore_inside_json_envelope(day, tmp_path, monkeypatch):
    """The envelope survives; only the big dump field is dispositioned."""
    module, sid = _arm(day, StubJudge())
    monkeypatch.setattr(module, "_vacuum_dir", lambda s: str(tmp_path / "vac"))
    payload = json.dumps({"stdout": make_dump(), "exit_code": 0})
    out = module._transform_tool_result(
        tool_name="terminal", result=payload, session_id=sid)
    env = json.loads(out)
    assert env["exit_code"] == 0
    assert ANCHOR in env["stdout"]
    assert "cache entry 7:" not in env["stdout"]


def test_vacuum_degrades_to_head_tail_trim_without_judge(day, tmp_path, monkeypatch):
    """No judge reachable → the stock >40-line trim runs unchanged."""
    module, _ctx, sid = day
    module._VACUUM_ARMED.add(sid)
    monkeypatch.setattr(module, "_ctx_judge", lambda: None, raising=False)
    monkeypatch.setattr(module, "_vacuum_dir", lambda s: str(tmp_path / "vac"))
    out = module._transform_tool_result(
        tool_name="terminal", result=make_dump(), session_id=sid)
    assert isinstance(out, str)
    assert "Trimmed stdout" in out              # legacy placeholder path
    assert "[ctxscore" not in out


def test_vacuum_unarmed_sessions_untouched(day, monkeypatch):
    """Vacuum stays OFF by default — a judge must not change that."""
    module, _ctx, sid = day
    module._CTX_JUDGE = StubJudge()
    monkeypatch.setattr(module, "_ctx_judge",
                        lambda: (_ for _ in ()).throw(AssertionError("judge ran unarmed")),
                        raising=False)
    out = module._transform_tool_result(
        tool_name="terminal", result=make_dump(), session_id=sid)
    assert out is None


def test_hidden_chunks_survive_in_the_archive(day, tmp_path, monkeypatch):
    """hide = archive-only: the dropped text is on disk, pointer in context."""
    module, sid = _arm(day, StubJudge())
    vacdir = tmp_path / "vac"
    monkeypatch.setattr(module, "_vacuum_dir", lambda s: str(vacdir))
    out = module._transform_tool_result(
        tool_name="terminal", result=make_dump(), session_id=sid)
    dumps = list(vacdir.glob("dump-*.log"))
    assert dumps, "vacuum archive was not written"
    archived = dumps[0].read_text(encoding="utf-8")
    assert "cache entry 7:" in archived          # hidden chunk is recoverable
    assert str(vacdir) in out                    # marker points at the archive
