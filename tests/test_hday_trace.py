"""Observability lane — observation tree, OTel GenAI naming, trajectory scoring,
compact tree rendering.

Four mechanisms, one contract:

* observation tree — ``trace`` > ``span`` > ``generation`` nesting with
  parent/child linkage, durations and status. Rows land in the EXISTING ledger
  row shape (``kind|lane|ms|cost|ts|sid``, the shape ``_gate_log`` writes) plus
  an additive span sidecar keyed by run id (``trace_id|span_id|parent_id``).
  No second store.
* OTel GenAI naming — span names follow the published grammar
  ``{gen_ai.operation.name} {subject}`` and attributes use the published
  semconv keys (``gen_ai.system``, ``gen_ai.request.model``,
  ``gen_ai.usage.input_tokens``, ...) so a run could be exported without
  renaming. Local-only concepts stay under ``hday.*``.
* trajectory scoring — a completed run scores into a small TYPED score row with
  named axes (tool-call validity, wasted steps, retries, approval waits,
  honesty events), never a single opaque number.
* text renderer — one run's tree as compact text for the cockpit/TUI.

Run: python -m pytest tests/test_hday_trace.py -q
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import hday_trace as ht  # noqa: E402

TRACE = "trace_run1"


def _dispatch(ctx, name, arg=""):
    fn = ctx.commands.get(name)
    assert callable(fn), f"command {name!r} not registered"
    return json.loads(fn(arg))


def _call_row(i, *, name="execute_tool read_file", op="execute_tool",
              span_type="tool", status="ok", parent=TRACE, attrs=None,
              ms=1.0, run_id="run-1"):
    """One ledger-shaped run row, the shape hday_trace emits."""
    return {"kind": span_type, "lane": "local", "ms": ms, "cost": 0.0,
            "ts": 100.0 + i, "sid": "s1", "trace_id": TRACE,
            "span_id": f"span_{i}", "parent_id": parent, "span_name": name,
            "operation_name": op, "span_type": span_type,
            "span_kind": "INTERNAL", "status": status, "run_id": run_id,
            "attrs": dict(attrs or {})}


# ---------------------------------------------------------------------------
# observation tree — trace > span > generation
# ---------------------------------------------------------------------------


def test_trace_span_generation_nesting_and_linkage():
    with ht.trace("turn", run_id="run-1", sid="s1") as tr:
        with ht.span("gate.decide", "gpt-4o") as gate:
            with ht.generation("chat", "gpt-4o", provider="opencode-go",
                               input_tokens=120, output_tokens=8) as gen:
                pass
        with ht.tool_span("read_file") as tool:
            pass

    rows = tr.as_rows()
    assert rows[0]["kind"] == "trace"
    assert rows[0]["span_id"] == tr.trace_id
    assert rows[0]["parent_id"] is None

    gate_row = next(r for r in rows if r["span_name"] == "gate.decide gpt-4o")
    gen_row = next(r for r in rows if r["span_type"] == "generation")
    tool_row = next(r for r in rows if r["span_type"] == "tool")

    # parent/child linkage: span under trace, generation under span
    assert gate_row["parent_id"] == tr.trace_id
    assert gen_row["parent_id"] == gate_row["span_id"]
    assert tool_row["parent_id"] == tr.trace_id
    assert gen_row["span_name"] == "chat gpt-4o"
    assert tool_row["span_name"] == "execute_tool read_file"

    # durations and status on every node
    assert all(r["trace_id"] == tr.trace_id for r in rows)
    assert all(isinstance(r["ms"], float) and r["ms"] >= 0.0 for r in rows)
    assert all(r["status"] in ht.STATUSES for r in rows)
    assert tr.status == "ok"
    assert tr.duration_ms >= 0.0


def test_span_context_manager_nests_from_the_ambient_span():
    with ht.trace("turn") as tr:
        with ht.span("gate.decide", "m") as outer:
            with ht.span("router.rank", "bm25") as inner:
                pass
        with ht.span("ctxscore.score", "m") as sibling:
            pass
    assert inner.parent_id == outer.span_id
    assert sibling.parent_id == tr.trace_id   # stack unwound
    assert len(tr.spans) == 3


def test_span_error_marks_status_and_error_type():
    with ht.trace("turn") as tr:
        with pytest.raises(ValueError):
            with ht.span("gate.decide", "m") as sp:
                raise ValueError("boom")
    row = sp.as_row()
    assert row["status"] == "error"
    assert row["attrs"]["error.type"] == "ValueError"
    assert tr.status == "error"
    assert row["ms"] >= 0.0


def test_span_without_a_trace_is_refused_loudly():
    with pytest.raises(RuntimeError):
        with ht.span("gate.decide", "m"):
            pass


def test_rows_use_the_existing_ledger_row_shape():
    with ht.trace("turn", run_id="run-3", sid="s1") as tr:
        with ht.tool_span("read_file") as tool:
            pass
    row = tool.as_row()
    # the shape _gate_log writes: kind | lane | ms | cost | ts (+ sid, tool)
    for key in ("kind", "lane", "ms", "cost", "ts", "sid"):
        assert key in row, f"ledger row missing {key!r}"
    assert row["kind"] in ht.SPAN_TYPES
    assert row["lane"] in ht.LANES
    # additive span sidecar keyed by run id
    for key in ("trace_id", "span_id", "parent_id", "span_name",
                "operation_name", "span_type", "span_kind", "status",
                "run_id", "attrs"):
        assert key in row, f"span sidecar missing {key!r}"
    assert row["run_id"] == "run-3"
    assert json.loads(json.dumps(row))["span_id"] == row["span_id"]


def test_rows_land_in_the_module_ledger_keyed_by_run_id():
    ht.reset_ledger()
    with ht.trace("turn", run_id="run-7", sid="s1") as tr:
        with ht.span("gate.decide", "m"):
            pass
    emitted = tr.emit()
    assert len(emitted) == len(tr.as_rows())

    rows = ht.ledger_rows(run_id="run-7")
    assert len(rows) == len(emitted)
    assert all(r["run_id"] == "run-7" for r in rows)
    assert ht.ledger_rows(run_id="other-run") == []

    groups = ht.group_by_trace(rows)
    assert list(groups) == [tr.trace_id]
    assert len(groups[tr.trace_id]) == len(rows)
    assert ht.group_by_trace([]) == {}


def test_emit_never_raises_on_a_broken_sink():
    ht.reset_ledger()

    def _boom(_row):
        raise RuntimeError("sink is down")

    rows = ht.emit([_call_row(0)], sink=_boom)
    assert len(rows) == 1
    assert len(ht.ledger_rows()) == 1   # rows still exist despite the sink


# ---------------------------------------------------------------------------
# OTel GenAI naming + attributes
# ---------------------------------------------------------------------------


def test_span_name_grammar_over_every_operation():
    assert ht.OPERATIONS, "the operation vocabulary must be closed and non-empty"
    for op in ht.OPERATIONS:
        name = ht.span_name(op, "subject-1")
        assert name == f"{op} subject-1"
        assert ht.is_valid_span_name(name), name


def test_missing_subject_uses_the_documented_fallback():
    assert ht.span_name("gate.decide") == "gate.decide unknown"
    assert ht.span_name("gate.decide", "   ") == "gate.decide unknown"
    assert ht.span_name("", None) == "span unknown"
    assert ht.FALLBACK_SUBJECT == "unknown"


def test_free_form_span_names_are_rejected():
    assert not ht.is_valid_span_name("Gate decision thing!")
    assert not ht.is_valid_span_name("whatever x")   # operation off the list
    assert not ht.is_valid_span_name("gate.decide")  # subject missing
    assert ht.is_valid_span_name("execute_tool read_file")


def test_semconv_attribute_keys_match_the_published_conventions():
    with ht.trace("turn", run_id="run-1", sid="s1") as tr:
        with ht.generation("chat", "gpt-4o", provider="opencode-go",
                           input_tokens=120, output_tokens=8,
                           cost=0.002) as gen:
            pass
        with ht.tool_span("read_file") as tool:
            pass

    attrs = gen.as_row()["attrs"]
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.system"] == "opencode-go"
    assert attrs["gen_ai.request.model"] == "gpt-4o"
    assert attrs["gen_ai.usage.input_tokens"] == 120
    assert attrs["gen_ai.usage.output_tokens"] == 8
    assert gen.as_row()["cost"] == 0.002

    t_attrs = tool.as_row()["attrs"]
    assert t_attrs["gen_ai.tool.name"] == "read_file"
    assert len(t_attrs["gen_ai.tool.call.id"]) == 36

    assert ht.SEMCONV_ATTRS["provider"] == "gen_ai.system"
    assert "gen_ai.provider.name" in ht.PROVIDER_ALIASES


def test_token_fields_are_ints_or_absent():
    with ht.trace("turn") as tr:
        with ht.generation("chat", "m", input_tokens="unknown",
                           output_tokens=None) as gen:
            pass
        with ht.generation("chat", "m", input_tokens=12.0,
                           output_tokens=12.5) as half:
            pass
    attrs = gen.as_row()["attrs"]
    assert "gen_ai.usage.input_tokens" not in attrs
    assert "gen_ai.usage.output_tokens" not in attrs
    half_attrs = half.as_row()["attrs"]
    assert half_attrs["gen_ai.usage.input_tokens"] == 12
    assert isinstance(half_attrs["gen_ai.usage.input_tokens"], int)
    assert "gen_ai.usage.output_tokens" not in half_attrs
    for row in tr.as_rows():
        for key, value in row["attrs"].items():
            if "tokens" in key:
                assert isinstance(value, int) and not isinstance(value, bool)


def test_content_attributes_are_absent_unless_opted_in():
    with ht.trace("turn") as tr:
        with ht.generation("chat", "m", input_messages=[{"role": "user"}],
                           output_messages=[{"role": "assistant"}]) as gen:
            pass
    assert not [k for k in gen.as_row()["attrs"] if k.startswith("gen_ai.input.")]
    assert not [k for k in gen.as_row()["attrs"] if k.startswith("gen_ai.output.")]
    captured = ht.semconv_attrs(gen, capture_content=True)
    assert captured["gen_ai.input.messages"] == [{"role": "user"}]
    assert captured["gen_ai.output.messages"] == [{"role": "assistant"}]


def test_no_unmapped_attribute_keys():
    with ht.trace("turn", run_id="run-1", sid="s1") as tr:
        with ht.generation("chat", "m", provider="p", input_tokens=1) as gen:
            pass
        with ht.tool_span("read_file") as tool:
            pass
    for row in tr.as_rows():
        assert ht.unmapped_attr_keys(row["attrs"]) == [], row["attrs"]
    assert ht.unmapped_attr_keys({"gen_ai.request.model": "m",
                                  "hday.lane": "local"}) == []
    assert ht.unmapped_attr_keys({"model": "m"}) == ["model"]
    assert ht.unmapped_attr_keys({"openai.api": 1}) == ["openai.api"]


def test_tool_call_id_is_stable_for_one_run():
    first = ht.tool_span_call_id("s1", "run-1", 0)
    again = ht.tool_span_call_id("s1", "run-1", 0)
    other = ht.tool_span_call_id("s1", "run-1", 1)
    assert first == again and first != other and len(first) == 36


def test_span_kinds_are_a_closed_vocabulary():
    with ht.trace("turn") as tr:
        with ht.generation("chat", "m") as gen:
            pass
        with ht.tool_span("read_file") as tool:
            pass
        with ht.span("gate.decide", "m") as gate:
            pass
    assert gen.as_row()["span_kind"] == "CLIENT"      # remote GenAI call
    assert tool.as_row()["span_kind"] == "INTERNAL"   # in-process tool
    assert gate.as_row()["span_kind"] == "INTERNAL"
    assert set(ht.SPAN_KINDS) == {"CLIENT", "INTERNAL", "SERVER"}


# ---------------------------------------------------------------------------
# trajectory scoring — typed axes, never one opaque number
# ---------------------------------------------------------------------------

_AXES = ("traj.tool_call_validity", "traj.wasted_steps", "traj.retries",
         "traj.approval_waits", "traj.honesty_events")


def test_score_run_returns_typed_axes_not_one_number():
    rows = [_call_row(i) for i in range(3)]
    score = ht.score_run(rows)
    row = score.as_row()
    assert row["kind"] == "score"
    assert row["dataType"] == "CATEGORICAL"
    assert row["value"] in ht.TRAJ_VERDICTS
    assert set(row["components"]) == set(_AXES)
    for name, comp in row["components"].items():
        assert comp["dataType"] in ht.SCORE_TYPES, name
        assert "value" in comp and "comment" in comp
    assert score.verdict == "pass"
    assert score.get("traj.tool_call_validity").value == 1.0
    assert score.get("traj.wasted_steps").value == 0


def test_trajectory_score_sees_a_skipped_evidence_step():
    """Verdict-only reading says pass; the trajectory reading does not."""
    clean = [_call_row(0), _call_row(1), _call_row(2)]
    skipped = [_call_row(0), _call_row(1),
               _call_row(2, attrs={"hday.wasted": True})]

    assert ht.score_run(clean).verdict == "pass"
    traj = ht.score_run(skipped)
    assert traj.get("traj.tool_call_validity").value == 1.0   # every call ok
    assert traj.get("traj.wasted_steps").value == 1
    assert traj.verdict == "fail"
    assert traj.get("traj.verdict").comment            # the reason is named


def test_score_run_reports_retries_approvals_and_honesty():
    rows = [
        _call_row(0, name="execute_tool read_file"),
        _call_row(1, name="execute_tool read_file"),        # retry of same call
        _call_row(2, name="approval.wait turn", op="approval.wait",
                  span_type="span", ms=4200.0),
        _call_row(3, name="honesty.check run", op="honesty.check",
                  span_type="event", status="error"),
    ]
    score = ht.score_run(rows)
    assert score.get("traj.retries").value == 1
    assert score.get("traj.approval_waits").value == 1
    assert score.get("traj.approval_wait_ms").value == 4200.0
    assert score.get("traj.honesty_events").value == 1
    assert score.verdict == "fail"


def test_score_run_is_honestly_unknown_without_scorable_calls():
    score = ht.score_run([])
    assert score.verdict == "unknown"
    validity = score.get("traj.tool_call_validity")
    assert validity.value is None
    assert validity.comment           # why it is unknown, not a silent zero
    assert score.as_row()["value"] == "unknown"


def test_judge_without_reasoning_is_unjudged_not_scored():
    rows = [_call_row(0), _call_row(1)]
    bare = ht.score_run(rows, judge=lambda _rows: {"score": 0.9})
    assert bare.get("traj.judge").status == "unjudged"
    assert bare.get("traj.judge").value is None
    assert "reasoning" in bare.get("traj.judge").comment

    scored = ht.score_run(rows, judge=lambda _rows: {"score": 0.9,
                                                     "reasoning": "solid"})
    assert scored.get("traj.judge").status == "scored"
    assert scored.get("traj.judge").value == 0.9
    assert scored.get("traj.judge").comment == "solid"


def test_crashing_judge_is_unjudged_and_never_raises():
    rows = [_call_row(0)]

    def _boom(_rows):
        raise RuntimeError("judge exploded")

    score = ht.score_run(rows, judge=_boom)
    assert score.get("traj.judge").status == "unjudged"
    assert score.verdict in ht.TRAJ_VERDICTS      # deterministic axes survive


def test_scoring_never_mutates_the_rows_it_grades():
    rows = [_call_row(0), _call_row(1, status="error")]
    snapshot = copy.deepcopy(rows)
    ht.score_run(rows)
    assert rows == snapshot


def test_score_rows_land_in_the_existing_ledger():
    ht.reset_ledger()
    score = ht.score_run([_call_row(0), _call_row(1)], emit=True)
    rows = ht.ledger_rows(run_id="run-1")
    assert rows, "score rows must reach the ledger"
    assert all(r["kind"] == "score" for r in rows)
    assert all(r["dataType"] in ht.SCORE_TYPES for r in rows)
    assert {r["name"] for r in rows} >= set(_AXES)
    assert score.verdict in {r["value"] for r in rows}


# ---------------------------------------------------------------------------
# compact text renderer for the cockpit/TUI
# ---------------------------------------------------------------------------


def test_render_tree_is_compact_text():
    with ht.trace("turn", run_id="run-1", sid="s1") as tr:
        with ht.span("gate.decide", "gpt-4o") as gate:
            with ht.generation("chat", "gpt-4o"):
                pass
        with ht.tool_span("read_file"):
            pass
    text = ht.render_tree(tr)
    lines = text.splitlines()
    assert tr.trace_id in lines[0]
    assert lines[0].startswith("trace turn")
    assert "spans=4" in lines[0]
    assert "gate.decide gpt-4o" in text
    assert "chat gpt-4o" in text
    assert "execute_tool read_file" in text

    gate_i = next(i for i, l in enumerate(lines) if "gate.decide gpt-4o" in l)
    gen_i = next(i for i, l in enumerate(lines) if "chat gpt-4o" in l)
    assert gen_i > gate_i
    indent = lambda s: len(s) - len(s.lstrip(" │├└─"))     # noqa: E731
    assert indent(lines[gen_i]) > indent(lines[gate_i])
    assert ht.render_tree(tr) == text                      # deterministic


def test_render_tree_marks_an_unknown_parent_honestly():
    rows = [
        {"kind": "trace", "span_id": TRACE, "parent_id": None,
         "span_name": "trace turn", "trace_id": TRACE, "status": "ok",
         "ms": 10.0, "ts": 1.0, "lane": "local", "cost": 0.0, "sid": "s1"},
        _call_row(1, name="gate.decide m", op="gate.decide",
                  parent="span_missing"),
    ]
    lines = ht.render_tree(rows).splitlines()
    orphan = next(l for l in lines if "gate.decide m" in l)
    assert "? gate.decide m" in orphan
    assert orphan.startswith(("├─", "└─"))     # re-attached at depth 1


def test_render_tree_is_honest_about_unknowns_and_garbage():
    text = ht.render_tree([{"span_id": "s", "trace_id": "trace_z"}])
    leaf = [ln for ln in text.splitlines() if ln.startswith(("├─", "└─"))][0]
    assert leaf.endswith("unknown  unknown  ?")   # label, status, duration
    assert "0.0ms" not in leaf                    # missing duration is ?, not 0
    assert ht.render_tree([]) == ht.NO_ROWS
    assert ht.render_tree(None) == ht.NO_ROWS
    assert ht.render_tree("garbage") == ht.NO_ROWS


def test_render_tree_terminates_on_a_parent_cycle():
    rows = [
        {"kind": "trace", "span_id": TRACE, "parent_id": None,
         "span_name": "trace turn", "trace_id": TRACE, "status": "ok",
         "ms": 1.0, "ts": 0.0, "lane": "local", "cost": 0.0, "sid": "s1"},
        _call_row(1, name="a", parent="span_2"),
        _call_row(2, name="b", parent="span_1"),
    ]
    text = ht.render_tree(rows)          # must terminate, not recurse forever
    assert "a" in text and "b" in text
    assert "! " in text                  # the broken link is marked


# ---------------------------------------------------------------------------
# wiring — the rows reach the existing ledger command and the cockpit renderer
# ---------------------------------------------------------------------------


def _seed_run(module, sid, run_id="run-9"):
    tr_mod = module._hday("hday_trace")
    assert tr_mod is not None, "hday_trace.py not loadable by the plugin"
    tr_mod.reset_ledger()
    with tr_mod.trace("turn", run_id=run_id, sid=sid) as tr:
        with tr_mod.span("gate.decide", "m"):
            pass
    tr.emit()
    return tr_mod, tr


def test_day_ledger_surfaces_trace_rows(day):
    module, ctx, sid = day
    _tr_mod, tr = _seed_run(module, sid)
    out = _dispatch(ctx, "day-ledger", sid)
    assert out["ok"] is True
    assert "trace" in out, "day-ledger must surface the trace sidecar"
    assert any(r.get("trace_id") == tr.trace_id for r in out["trace"])
    assert out["gate"] == [] and out["ctxscore"] == []   # additive, not a swap


def test_day_trace_command_lists_and_renders_one_run(day):
    module, ctx, sid = day
    _tr_mod, tr = _seed_run(module, sid)
    listing = _dispatch(ctx, "day-trace")
    assert listing["ok"] is True
    assert any(t["run_id"] == "run-9" for t in listing["traces"])
    one = _dispatch(ctx, "day-trace", "run-9")
    assert one["ok"] is True
    assert one["trace_id"] == tr.trace_id
    assert "gate.decide m" in one["text"]
    assert one["text"].splitlines()[0].startswith("trace turn")
    assert _dispatch(ctx, "day-trace", "no-such-run")["ok"] is False
