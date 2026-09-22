"""hday_ctxscore — query-aware context chunk scorer (meta-attention).

The essay's centerpiece: context is not static. At vacuum/compaction time every
candidate chunk gets a relevance judgment, and a disposition in
``{whole, long, small, hide}`` decides what stays in the prompt:

    whole  keep verbatim            long/small  keep a bounded excerpt placeholder
    hide   archive-only marker      (never silent: every drop leaves a line behind)

Design rules enforced here (see BRIEF):

* **Batch rule** — many questions, ONE request per state. ``score`` builds one
  noul (relevance) + one 4-way choice (disposition) question *per chunk* and
  hands the whole dict to the injected ``judge`` exactly once. Fan out
  questions, not requests.
* **Single call site** — the only place a judge/network call can happen is the
  one ``judge(state, questions)`` line inside ``score``. The integrator
  redirects it by injecting ``judge=client.ask`` (the SystemOneClient shape) or
  any callable with the same signature.
* **Fail-open, loudly** — judge missing/crashing/partial answers never lose
  context: affected chunks get disposition ``whole`` AND a ledger row with
  ``status="unjudged"`` and a ``reason``. ``score`` never raises out of the hot
  path; problems are logged and marked, never swallowed silently.
* **Sync-cheap pre-filter** — the judge is only reached when the dump is
  >40 lines AND splits into >1 chunk (nothing to discriminate below that), so
  the hot path stays local-cheap. Skips are recorded ``unjudged`` too
  (reason ``pre-filter`` / ``no-judge``).
* **Combination rule** — thresholds are priors. ``noul`` (relevance) acts as a
  guard rail over the ``choice`` answer: a chunk the goal needs is never
  ``hide``-d (downgraded to ``small``, reason ``noul-guard``), so a needle
  cannot be destroyed by a single axis.

Question dicts match the typesafe wire format (``primitives.noul`` /
``primitives.choice``) but are built inline on purpose: this module is
import-safe standalone — stdlib only, no env/file reads at import time — so the
integrator can wire it into ``__init__.py`` as-is.

Typical use::

    chunks = chunker(tool_output)                 # blank/section boundaries
    res = score(chunks, goal, judge=client.ask)   # one batched request
    compact = apply_dispositions(chunks, res)     # needle survives if `whole`
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Iterable, List, Mapping, Optional, Sequence, Union

__all__ = [
    "DISPOSITIONS",
    "LANES",
    "LEDGER",
    "ScoreResult",
    "apply_dispositions",
    "chunker",
    "reset_ledger",
    "score",
]

DISPOSITIONS = ("whole", "long", "small", "hide")
LANES = ("local", "classifier", "hosted")

#: prior: at/above this noul the chunk counts as relevant to the goal
RELEVANT_NOUL = 0.5

#: pre-filter defaults: judge only when >40 lines AND >1 chunk
DEFAULT_MIN_LINES = 40
DEFAULT_MIN_CHUNKS = 1

#: excerpt windows (head, tail lines) for the `long` / `small` placeholders
_EXCERPT = {"long": (12, 6), "small": (4, 2)}

_log = logging.getLogger("hermes_day.ctxscore")

#: module ledger (bounded). Every score() row lands here for the integrator's
#: /day inspector; ``reset_ledger()`` clears it (tests, session rollover).
LEDGER: deque = deque(maxlen=500)

# Section boundaries for tool dumps: markdown headings, pure rule lines
# (=== or ---), `=== title ===` banners, and bare column-0 "Label:" lines.
# Conservative on purpose: "step 0: compiling widget" is content, not a border.
_SECTION_RE = re.compile(
    r"^\#{1,6}\s+\S"
    r"|^(?:={3,}|-{3,}|\*{3,}|_{3,})\s*$"
    r"|^={2,}\s*\S.*\s*={2,}\s*$"
    r"|^\S[^:]*:\s*$"
)


# ---------------------------------------------------------------------------
# result + ledger row shapes
# ---------------------------------------------------------------------------


@dataclass
class ScoreResult:
    """Per-chunk dispositions plus the honest ledger rows behind them.

    Indexing/iteration yields the disposition directly, so
    ``score(chunks, goal)[i]`` is a disposition in ``DISPOSITIONS``.
    """

    dispositions: List[str]
    rows: List[dict]
    requests: int = 0
    ok: bool = False

    def __getitem__(self, index: int) -> str:
        return self.dispositions[index]

    def __iter__(self):
        return iter(self.dispositions)

    def __len__(self) -> int:
        return len(self.dispositions)


def reset_ledger() -> None:
    """Clear the module ledger (tests / session rollover)."""
    LEDGER.clear()


def _row(
    chunk_i: int,
    disposition: str,
    *,
    status: str,
    lane: str,
    ms: float,
    cost: float,
    noul: Optional[float],
    choice: Optional[str],
    reason: str,
) -> dict:
    return {
        "chunk": chunk_i,
        "status": status,          # judged | cached | unjudged (regex_hit is the gate's)
        "lane": lane,              # local | classifier | hosted (local when nothing ran)
        "ms": ms,
        "cost": cost,
        "noul": noul,
        "choice": choice,
        "disposition": disposition,
        "reason": reason,          # "" when fully judged; why-not when unjudged
    }


def _emit(rows: Sequence[dict], ledger: Optional[Callable[[dict], None]]) -> None:
    """Record rows in the module ledger and an optional injected sink.

    Never raises: a broken sink is logged, the rows still exist in the result.
    """
    for row in rows:
        try:
            LEDGER.append(row)
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("ctxscore ledger append failed: %s", exc)
        if callable(ledger):
            try:
                ledger(row)
            except Exception as exc:
                _log.warning("ctxscore ledger sink failed: %s", exc)


# ---------------------------------------------------------------------------
# chunker
# ---------------------------------------------------------------------------


def chunker(text: Optional[str], max_lines: int = 40) -> List[str]:
    """Split a tool dump on blank-line / section boundaries, <= ``max_lines``.

    Blocks are cut at blank lines and at section borders (headings, rule lines,
    bare ``Label:`` lines); any block still over ``max_lines`` is hard-split on
    line boundaries. No content line is ever dropped — only blank separator
    lines are, so ``"\\n".join(chunker(t)).split() == t.split()``.
    """
    if text is None:
        return []
    if not isinstance(text, str):
        text = str(text)
    if not text.strip():
        return []
    try:
        max_lines = int(max_lines)
    except (TypeError, ValueError):
        max_lines = DEFAULT_MIN_LINES
    if max_lines < 1:
        max_lines = DEFAULT_MIN_LINES

    blocks: List[List[str]] = []
    cur: List[str] = []
    for line in text.splitlines():
        if not line.strip():
            if cur:
                blocks.append(cur)
                cur = []
            continue
        if cur and _SECTION_RE.match(line):
            blocks.append(cur)
            cur = [line]
        else:
            cur.append(line)
    if cur:
        blocks.append(cur)

    chunks: List[str] = []
    for block in blocks:
        for i in range(0, len(block), max_lines):
            chunks.append("\n".join(block[i : i + max_lines]))
    return chunks


# ---------------------------------------------------------------------------
# judge plumbing — the single network seam
# ---------------------------------------------------------------------------


def _build_state(goal: str, chunks: Sequence[str]) -> dict:
    return {
        "goal": goal,
        "chunks": [
            {"id": f"c{i}", "lines": len(text.splitlines()), "text": text}
            for i, text in enumerate(chunks)
        ],
    }


def _build_questions(goal: str, n: int) -> dict:
    """noul relevance + 4-way choice per chunk — two questions, one batch."""
    questions: dict = {}
    for i in range(n):
        cid = f"c{i}"
        questions[f"r:{cid}"] = {
            "type": "noul",
            "instructions": (
                f"Current goal: {goal}\n"
                f"Context chunk {cid}: is it relevant to that goal?"
            ),
            "criteria": {
                "true": "the chunk carries facts, code, or output the goal depends on",
                "false": "the chunk is unrelated noise, boilerplate, or output for another task",
            },
        }
        questions[f"d:{cid}"] = {
            "type": "choice",
            "instructions": (
                f"Current goal: {goal}\n"
                f"Context chunk {cid}: how much of it should stay in the prompt?"
            ),
            "criteria": {
                "whole": "keep the chunk verbatim; it holds facts the goal needs",
                "long": "keep a long excerpt; mostly relevant, some lines can drop",
                "small": "keep only a short excerpt; marginally relevant context",
                "hide": "drop the chunk; irrelevant to the goal, archive only",
            },
        }
    return questions


def _get_answer(response: Any, qid: str) -> Any:
    """Pull one answer out of a Response-like object, a dict, or None.

    Tolerates the shapes seen in the wild: typesafe ``Response`` (``.answers``,
    ``.get``, ``[qid]``), a bare ``{qid: answer}`` mapping, or garbage/None.
    """
    if response is None:
        return None
    try:
        answers = getattr(response, "answers", None)
        if isinstance(answers, Mapping) and qid in answers:
            return answers[qid]
        if isinstance(response, Mapping):
            if qid in response:
                return response[qid]
            inner = response.get("answers")
            if isinstance(inner, Mapping):
                return inner.get(qid)
            return None
        getter = getattr(response, "get", None)
        if callable(getter):
            try:
                found = getter(qid)
                if found is not None:
                    return found
            except Exception:
                pass
        return response[qid]
    except Exception:
        return None


def _field(answer: Any, name: str) -> Any:
    """Read one field from an answer: attribute, dict key, or raw dict key."""
    if answer is None:
        return None
    try:
        value = getattr(answer, name, None)   # Answer.noul raises if absent
    except Exception:
        value = None
    if value is None and isinstance(answer, Mapping):
        value = answer.get(name)
        if value is None and isinstance(answer.get("raw"), Mapping):
            value = answer["raw"].get(name)
    return value


def _noul_of(answer: Any) -> Optional[float]:
    raw = _field(answer, "noul")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _choice_of(answer: Any) -> Optional[str]:
    raw = _field(answer, "choice")
    if isinstance(raw, str):
        norm = raw.strip().lower()
        if norm in DISPOSITIONS:
            return norm
    probs = _field(answer, "probabilities")
    if isinstance(probs, Mapping) and probs:
        try:
            best = max(probs.items(), key=lambda kv: float(kv[1]))[0]
        except Exception:
            best = None
        if isinstance(best, str) and best.strip().lower() in DISPOSITIONS:
            return best.strip().lower()
    return None


def _combine(noul_v: Optional[float], choice_v: Optional[str]) -> tuple:
    """Merge the two axes: choice is primary, noul is the guard rail.

    Returns ``(disposition, reason)`` — reason is "" for a clean judgment.
    """
    if choice_v is None:
        if noul_v is None:
            # answer objects existed but neither axis parsed → fail open
            return "whole", "empty-answer"
        return ("whole" if noul_v >= RELEVANT_NOUL else "hide"), "missing-choice"
    if choice_v == "hide" and noul_v is not None and noul_v >= RELEVANT_NOUL:
        # combination rule: never destroy a chunk the goal demonstrably needs
        return "small", "noul-guard"
    return choice_v, ""


def _cost_of(response: Any) -> float:
    if response is None:
        return 0.0
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return 0.0
        fn = getattr(usage, "cost_usd", None)
        if callable(fn):
            return max(0.0, float(fn() or 0.0))
        if isinstance(usage, Mapping):
            return max(0.0, float(usage.get("cost_usd", 0.0) or 0.0))
    except Exception:
        return 0.0
    return 0.0


def _resolve_lane(judge: Any, lane: Optional[str], *, invoked: bool) -> str:
    """Explicit ``lane=`` wins, then ``judge.lane`` (or its bound owner)."""
    if isinstance(lane, str) and lane in LANES:
        return lane
    candidates: List[Any] = [judge]
    if judge is not None:
        try:
            candidates.append(getattr(judge, "__self__", None))
        except Exception:
            candidates.append(None)
    for obj in candidates:
        if obj is None:
            continue
        try:
            value = getattr(obj, "lane", None)
        except Exception:
            value = None
        if isinstance(value, str) and value in LANES:
            return value
    # nothing declared: a judge that ran is assumed hosted (say lane= to fix),
    # a skipped judge means the local heuristic decided.
    return "hosted" if invoked else "local"


def _normalize_chunks(context_chunks: Any) -> List[str]:
    if context_chunks is None:
        return []
    if isinstance(context_chunks, str):
        return chunker(context_chunks)
    try:
        items = list(context_chunks)
    except TypeError:
        items = [context_chunks]
    out: List[str] = []
    for item in items:
        if isinstance(item, str):
            out.append(item)
            continue
        try:
            out.append(str(item))
        except Exception:
            _log.warning("ctxscore could not stringify a chunk; storing empty")
            out.append("")
    return out


def _ms_since(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000.0, 3)


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# score — the hot path
# ---------------------------------------------------------------------------


def score(
    context_chunks: Union[Sequence[str], str, None],
    goal: Any,
    *,
    judge: Optional[Callable[..., Any]] = None,
    lane: Optional[str] = None,
    ledger: Optional[Callable[[dict], None]] = None,
    min_lines: int = DEFAULT_MIN_LINES,
    min_chunks: int = DEFAULT_MIN_CHUNKS,
) -> ScoreResult:
    """Score each chunk against ``goal`` → per-chunk disposition.

    One batched request (``judge(state, questions)``) at most; never raises.
    ``judge=None`` (or any judge failure) fails open: every chunk stays
    ``whole`` with an ``unjudged`` row explaining why. Rows are also appended
    to ``LEDGER`` and to the optional per-row ``ledger`` sink.
    """
    try:
        chunks = _normalize_chunks(context_chunks)
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("ctxscore input normalization failed: %s: %s", type(exc).__name__, exc)
        chunks = []
    try:
        return _score(
            chunks,
            goal,
            judge=judge,
            lane=lane,
            ledger=ledger,
            min_lines=min_lines,
            min_chunks=min_chunks,
        )
    except Exception as exc:  # hot path never raises — keep context, mark it
        _log.warning("ctxscore failed open: %s: %s", type(exc).__name__, exc)
        rows = [
            _row(
                i, "whole",
                status="unjudged", lane="local", ms=0.0, cost=0.0,
                noul=None, choice=None, reason=f"error: {type(exc).__name__}",
            )
            for i in range(len(chunks))
        ]
        _emit(rows, ledger)
        return ScoreResult(dispositions=["whole"] * len(chunks), rows=rows, requests=0, ok=False)


def _score(
    chunks: Sequence[str],
    goal: Any,
    *,
    judge: Optional[Callable[..., Any]],
    lane: Optional[str],
    ledger: Optional[Callable[[dict], None]],
    min_lines: int,
    min_chunks: int,
) -> ScoreResult:
    t0 = time.perf_counter()
    n = len(chunks)
    if n == 0:
        return ScoreResult(dispositions=[], rows=[], requests=0, ok=True)

    goal_text = "" if goal is None else str(goal)

    # -- heuristic pre-filter: judge only when it can discriminate ------------
    skip_reason: Optional[str] = None
    if judge is None:
        skip_reason = "no-judge"
    else:
        total_lines = sum(len(c.splitlines()) for c in chunks)
        if not (
            total_lines > _int_or(min_lines, DEFAULT_MIN_LINES)
            and n > _int_or(min_chunks, DEFAULT_MIN_CHUNKS)
        ):
            skip_reason = "pre-filter"
    if skip_reason is not None:
        ms = _ms_since(t0)
        skip_lane = _resolve_lane(judge, lane, invoked=False)
        rows = [
            _row(
                i, "whole",
                status="unjudged", lane=skip_lane, ms=ms, cost=0.0,
                noul=None, choice=None, reason=skip_reason,
            )
            for i in range(n)
        ]
        _emit(rows, ledger)
        return ScoreResult(dispositions=["whole"] * n, rows=rows, requests=0, ok=False)

    # -- ONE request: fan out questions, not requests -------------------------
    state = _build_state(goal_text, chunks)
    questions = _build_questions(goal_text, n)
    response: Any = None
    error: Optional[BaseException] = None
    try:
        response = judge(state, questions)      # <<< the single call site
    except Exception as exc:
        error = exc
        _log.warning("ctxscore judge failed (%s): %s", type(exc).__name__, exc)
    ms = _ms_since(t0)
    used_lane = _resolve_lane(judge, lane, invoked=True)

    if error is not None:
        reason = f"judge-error: {type(error).__name__}"
        rows = [
            _row(
                i, "whole",
                status="unjudged", lane=used_lane, ms=ms, cost=0.0,
                noul=None, choice=None, reason=reason,
            )
            for i in range(n)
        ]
        _emit(rows, ledger)
        return ScoreResult(dispositions=["whole"] * n, rows=rows, requests=1, ok=False)

    if response is None:
        rows = [
            _row(
                i, "whole",
                status="unjudged", lane=used_lane, ms=ms, cost=0.0,
                noul=None, choice=None, reason="no-response",
            )
            for i in range(n)
        ]
        _emit(rows, ledger)
        return ScoreResult(dispositions=["whole"] * n, rows=rows, requests=1, ok=False)

    cached = bool(getattr(response, "cached", False))
    cost_each = _cost_of(response) / n
    status_if_ok = "cached" if cached else "judged"

    dispositions: List[str] = []
    rows: List[dict] = []
    for i, _text in enumerate(chunks):
        cid = f"c{i}"
        ans_rel = _get_answer(response, f"r:{cid}")
        ans_disp = _get_answer(response, f"d:{cid}")
        if ans_rel is None and ans_disp is None:
            dispositions.append("whole")
            rows.append(_row(
                i, "whole",
                status="unjudged", lane=used_lane, ms=ms, cost=cost_each,
                noul=None, choice=None, reason="missing-answer",
            ))
            continue
        noul_v = _noul_of(ans_rel)
        choice_v = _choice_of(ans_disp)
        # tolerate a judge that parks both fields on either answer
        if noul_v is None:
            noul_v = _noul_of(ans_disp)
        if choice_v is None:
            choice_v = _choice_of(ans_rel)
        disp, note = _combine(noul_v, choice_v)
        status = "unjudged" if note == "empty-answer" else status_if_ok
        dispositions.append(disp)
        rows.append(_row(
            i, disp,
            status=status, lane=used_lane, ms=ms, cost=cost_each,
            noul=noul_v, choice=choice_v, reason=note,
        ))

    _emit(rows, ledger)
    ok = all(r["status"] in ("judged", "cached") for r in rows)
    return ScoreResult(dispositions=dispositions, rows=rows, requests=1, ok=ok)


# ---------------------------------------------------------------------------
# apply_dispositions — the actual keep / trim / drop step
# ---------------------------------------------------------------------------


def _disposition_at(dispositions: Any, i: int) -> str:
    """Read one disposition; anything unusable → ``whole`` (fail-open)."""
    if dispositions is None:
        return "whole"
    try:
        if i >= len(dispositions):
            return "whole"
        value = dispositions[i]
    except Exception:
        return "whole"
    return value if isinstance(value, str) and value in DISPOSITIONS else "whole"


def _excerpt(text: str, head: int, tail: int, label: str) -> str:
    """Bounded stand-in for a real summary: head + tail lines, marked."""
    lines = text.splitlines()
    if len(lines) <= head + tail + 1:
        return text
    kept = lines[:head] + ["..."] + lines[-tail:]
    marker = f"[ctxscore {label}: {len(lines)} lines -> {head}+{tail}]"
    return marker + "\n" + "\n".join(kept)


def apply_dispositions(
    chunks: Union[Sequence[str], str, None],
    dispositions: Union[Sequence[str], Any, None],
) -> str:
    """Materialize dispositions into the text that stays in context.

    ``whole`` keeps the chunk verbatim (needle survives), ``long``/``small``
    become marked head/tail excerpts, ``hide`` becomes a one-line marker so a
    drop is auditable. Unknown/missing dispositions keep the chunk — this
    function never loses data either.
    """
    if chunks is None:
        return ""
    if isinstance(chunks, str):
        chunks = [chunks]
    elif not isinstance(chunks, (list, tuple)):
        try:
            chunks = list(chunks)
        except TypeError:
            chunks = [chunks]

    parts: List[str] = []
    for i, text in enumerate(chunks):
        if not isinstance(text, str):
            text = str(text)
        disp = _disposition_at(dispositions, i)
        if disp == "hide":
            parts.append(f"[ctxscore: hidden {len(text.splitlines())} lines]")
        elif disp in _EXCERPT:
            head, tail = _EXCERPT[disp]
            parts.append(_excerpt(text, head, tail, f"{disp} summary"))
        else:  # whole / unknown → keep verbatim (fail-open)
            parts.append(text)
    return "\n\n".join(parts)
