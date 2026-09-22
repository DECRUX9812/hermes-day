"""TDD tests for hday_ctxscore — query-aware context chunk scorer (meta-attention).

The essay's centerpiece: context is not static. Each chunk gets a relevance
judgment (noul) plus a 4-way choice disposition — {whole, long, small, hide} —
batched as ONE request per state (fan out questions, not requests).

Discipline under test:
- needle-recall: an anchor fact embedded in a tool dump survives dispositioning;
- fail-open: judge=None / judge crash / missing answers never lose context, and
  every such row is recorded `unjudged` in the ledger (marked, never silent);
- sync-cheap hot path: the (potentially networked) judge is only reached when
  the dump is >40 lines AND split into >1 chunk, behind a single call site.

No network: every judge here is an in-process stub.
"""

from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

import hday_ctxscore as cs


ANCHOR = "needle fact NEEDLE-7734 lives in this chunk"


def make_dump() -> str:
    """A tool dump >40 lines: one needle section plus two junk sections."""
    needle = ["## build output", ANCHOR] + [f"step {i}: compiling widget_{i}" for i in range(30)]
    junk_cache = ["=== cache ==="] + [f"cache entry {i}: item {i} of many" for i in range(15)]
    junk_warn = ["## warnings"] + [f"warning {i}: deprecated widget_{i}" for i in range(12)]
    return "\n\n".join("\n".join(s) for s in (needle, junk_cache, junk_warn))


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


def needle_index(chunks) -> int:
    return next(i for i, c in enumerate(chunks) if ANCHOR in c)


# ---------------------------------------------------------------------------
# chunker — splits tool dumps on blank-line / section boundaries, <= max_lines
# ---------------------------------------------------------------------------


def test_chunker_splits_on_blank_lines_and_sections():
    dump = make_dump()
    chunks = cs.chunker(dump)
    assert len(chunks) >= 3
    assert all(len(c.splitlines()) <= 40 for c in chunks)
    # no line lost (blank lines aside) and order preserved
    assert "\n".join(chunks).split() == dump.split()
    # the needle lands whole inside exactly one chunk
    assert sum(1 for c in chunks if ANCHOR in c) == 1


def test_chunker_hard_splits_oversized_blocks():
    text = "\n".join(f"noise line {i}" for i in range(90))
    chunks = cs.chunker(text, max_lines=40)
    assert [len(c.splitlines()) for c in chunks] == [40, 40, 10]
    assert "\n".join(chunks).split() == text.split()


def test_chunker_handles_empty_and_non_string():
    assert cs.chunker("") == []
    assert cs.chunker("   \n\n  ") == []
    assert cs.chunker(None) == []


# ---------------------------------------------------------------------------
# score — noul relevance + 4-way choice, ONE batched request per state
# ---------------------------------------------------------------------------


def test_needle_scores_whole_junk_hides():
    chunks = cs.chunker(make_dump())
    judge = StubJudge()
    res = cs.score(chunks, "find the needle fact", judge=judge)

    assert res.ok is True
    assert res.requests == 1
    i = needle_index(chunks)
    assert res[i] == "whole"                    # needle chunk kept verbatim
    for j in range(len(chunks)):
        if j != i:
            assert res[j] == "hide"             # junk collapses
    assert all(r["status"] == "judged" for r in res.rows)
    assert all(r["lane"] == "local" for r in res.rows)   # StubJudge.lane
    assert res.rows[i]["noul"] == pytest.approx(0.97)
    assert res.rows[i]["choice"] == "whole"


def test_one_request_per_state_batches_every_question():
    chunks = cs.chunker(make_dump())
    judge = StubJudge()
    cs.score(chunks, "find the needle fact", judge=judge)

    assert len(judge.calls) == 1                # fan out questions, not requests
    state = judge.calls[0]["state"]
    questions = judge.calls[0]["questions"]
    assert state["goal"] == "find the needle fact"
    assert {c["id"] for c in state["chunks"]} == {f"c{i}" for i in range(len(chunks))}
    assert len(questions) == 2 * len(chunks)    # one noul + one choice per chunk
    assert sum(1 for q in questions.values() if q["type"] == "noul") == len(chunks)
    assert sum(1 for q in questions.values() if q["type"] == "choice") == len(chunks)
    choice_q = next(q for q in questions.values() if q["type"] == "choice")
    assert set(choice_q["criteria"]) == {"whole", "long", "small", "hide"}
    noul_q = next(q for q in questions.values() if q["type"] == "noul")
    assert set(noul_q["criteria"]) == {"true", "false"}


def test_score_accepts_raw_text():
    dump = make_dump()
    chunks = cs.chunker(dump)               # what the raw string chunks into
    res = cs.score(dump, "find the needle fact", judge=StubJudge())
    assert len(res) == len(chunks)          # a str is chunked internally
    assert len(res) > 1
    assert res[needle_index(chunks)] == "whole"


def test_noul_vetoes_hide_for_a_relevant_chunk():
    """Combination rule: noul says needle-relevant, choice says hide → never destroy data."""

    def judge(state, questions):
        texts = {c["id"]: c["text"] for c in state["chunks"]}
        out = {}
        for qid, q in questions.items():
            _kind, cid = qid.split(":", 1)
            relevant = ANCHOR in texts[cid]
            if q["type"] == "noul":
                out[qid] = SimpleNamespace(kind="noul", noul=0.96 if relevant else 0.02)
            else:
                out[qid] = SimpleNamespace(kind="choice", choice="hide")  # hostile choice
        return out

    chunks = cs.chunker(make_dump())
    res = cs.score(chunks, "find the needle fact", judge=judge)
    i = needle_index(chunks)
    assert res[i] == "small"                    # hide downgraded, not applied
    assert res.rows[i]["reason"] == "noul-guard"
    for j in range(len(chunks)):
        if j != i:
            assert res[j] == "hide"


# ---------------------------------------------------------------------------
# fail-open, loudly — no judge / crashing judge / partial answers
# ---------------------------------------------------------------------------


def test_judge_none_fails_open_all_whole_and_unjudged():
    chunks = cs.chunker(make_dump())
    res = cs.score(chunks, "find the needle fact")           # judge defaults to None

    assert res.dispositions == ["whole"] * len(chunks)     # context never lost
    assert res.requests == 0
    assert res.ok is False
    for row in res.rows:
        assert row["status"] == "unjudged"
        assert row["disposition"] == "whole"
        assert row["reason"] == "no-judge"
        assert row["cost"] == 0.0
        assert row["lane"] in cs.LANES


def test_judge_failure_fails_open_all_whole():
    def boom(state, questions):
        raise RuntimeError("upstream 500")

    chunks = cs.chunker(make_dump())
    res = cs.score(chunks, "find the needle fact", judge=boom)   # must not raise

    assert res.dispositions == ["whole"] * len(chunks)
    assert res.ok is False
    assert res.requests == 1
    for row in res.rows:
        assert row["status"] == "unjudged"
        assert row["reason"].startswith("judge-error: RuntimeError")


def test_missing_answers_fail_open_per_chunk():
    def judge(state, questions):
        # only chunk c0 comes back answered; the rest of the batch is empty
        out = {}
        for qid, q in questions.items():
            if not qid.endswith(":c0"):
                continue
            out[qid] = {"noul": 0.9} if q["type"] == "noul" else {"choice": "whole"}
        return out

    chunks = cs.chunker(make_dump())
    res = cs.score(chunks, "find the needle fact", judge=judge)

    assert res.dispositions[0] == "whole"
    assert res.rows[0]["status"] == "judged"
    for row in res.rows[1:]:
        assert row["status"] == "unjudged"
        assert row["reason"] == "missing-answer"
        assert row["disposition"] == "whole"
    assert res.ok is False


def test_no_response_fails_open():
    chunks = cs.chunker(make_dump())
    res = cs.score(chunks, "find the needle fact", judge=lambda state, questions: None)
    assert res.dispositions == ["whole"] * len(chunks)
    assert all(r["status"] == "unjudged" and r["reason"] == "no-response" for r in res.rows)


# ---------------------------------------------------------------------------
# heuristic pre-filter — judge only runs when >40 lines AND >1 chunk
# ---------------------------------------------------------------------------


def test_prefilter_skips_judge_for_small_results():
    chunks = ["line one of output", "another short line"]      # 2 lines, 2 chunks
    judge = StubJudge()
    res = cs.score(chunks, "anything", judge=judge)

    assert judge.calls == []                    # sync-cheap: nothing over 40 lines
    assert res.dispositions == ["whole", "whole"]
    assert res.requests == 0
    assert all(r["status"] == "unjudged" and r["reason"] == "pre-filter" for r in res.rows)


def test_prefilter_skips_judge_for_a_single_chunk():
    chunks = ["\n".join(f"line {i}" for i in range(45))]        # >40 lines but ONE chunk
    judge = StubJudge()
    res = cs.score(chunks, "anything", judge=judge)

    assert judge.calls == []                    # nothing to discriminate → keep whole
    assert res.dispositions == ["whole"]
    assert res.requests == 0
    assert res.rows[0]["reason"] == "pre-filter"


# ---------------------------------------------------------------------------
# apply_dispositions — the actual keep/trim/drop step (needle-recall)
# ---------------------------------------------------------------------------


def test_needle_recall_after_dispositions():
    chunks = cs.chunker(make_dump())
    res = cs.score(chunks, "find the needle fact", judge=StubJudge())
    kept = cs.apply_dispositions(chunks, res)

    assert ANCHOR in kept                       # the needle survived the disposition
    assert "cache entry 7:" not in kept         # junk really dropped
    assert "[ctxscore" in kept                  # drop is marked, never silent


def test_apply_dispositions_shapes():
    text = "\n".join(f"line {i}" for i in range(30))
    kept_long = cs.apply_dispositions([text], ["long"])
    kept_small = cs.apply_dispositions([text], ["small"])
    kept_hide = cs.apply_dispositions([text], ["hide"])

    assert len(kept_small) < len(kept_long) < len(text)
    assert "long summary" in kept_long and "small summary" in kept_small
    assert "line 3" not in kept_hide and "hidden" in kept_hide
    assert cs.apply_dispositions([], []) == ""


def test_apply_dispositions_unknown_and_missing_fail_open():
    text = "precious data"
    assert cs.apply_dispositions([text], ["bogus"]) == text   # unknown → keep whole
    assert cs.apply_dispositions([text], []) == text          # missing → keep whole
    assert cs.apply_dispositions([text], None) == text


# ---------------------------------------------------------------------------
# ledger contract — judged vs cached vs unjudged recorded separately
# ---------------------------------------------------------------------------


def test_response_like_cached_rows_record_cost():
    class Usage:
        def cost_usd(self):
            return 0.00042

    class Resp:
        cached = True
        usage = Usage()

        def __init__(self, answers):
            self.answers = answers

    chunks = cs.chunker(make_dump())
    answers = {}
    for i in range(len(chunks)):
        answers[f"r:c{i}"] = {"noul": 0.1}
        answers[f"d:c{i}"] = {"choice": "hide"}
    resp = Resp(answers)
    res = cs.score(chunks, "find the needle fact", judge=lambda s, q: resp)

    assert all(r["status"] == "cached" for r in res.rows)     # cached ≠ judged
    assert all(r["cost"] > 0 for r in res.rows)
    assert all(r["lane"] in cs.LANES for r in res.rows)
    assert res.dispositions == ["hide"] * len(chunks)
    assert res.ok is True


def test_row_schema_matches_ledger_contract():
    chunks = cs.chunker(make_dump())
    res = cs.score(chunks, "find the needle fact", judge=StubJudge())
    for row in res.rows:
        assert set(row) >= {"chunk", "status", "lane", "ms", "cost",
                            "noul", "choice", "disposition", "reason"}
        assert row["status"] in ("regex_hit", "judged", "cached", "unjudged")
        assert row["lane"] in cs.LANES
        assert row["disposition"] in cs.DISPOSITIONS
        assert row["ms"] >= 0
        assert row["cost"] >= 0


def test_ledger_records_judged_and_unjudged_rows():
    cs.reset_ledger()
    chunks = cs.chunker(make_dump())
    cs.score(chunks, "find the needle fact", judge=StubJudge())
    judged = list(cs.LEDGER)
    assert len(judged) == len(chunks)
    assert all(r["status"] == "judged" for r in judged)

    cs.reset_ledger()
    cs.score(chunks, "find the needle fact")                   # no judge
    unjudged = list(cs.LEDGER)
    assert len(unjudged) == len(chunks)
    assert all(r["status"] == "unjudged" for r in unjudged)
    cs.reset_ledger()


def test_empty_and_none_inputs_never_raise():
    assert cs.score([], "goal").dispositions == []
    assert cs.score(None, "goal").dispositions == []
    assert cs.score([], "goal").requests == 0


def test_import_is_standalone():
    """Import-safe: works from the plugin root with no env, no side effects."""
    out = subprocess.run(
        [sys.executable, "-c", "import hday_ctxscore; print('ok')"],
        cwd=_PLUGIN_ROOT, capture_output=True, text=True, timeout=60,
        env={"PATH": os.environ.get("PATH", "")},
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"
