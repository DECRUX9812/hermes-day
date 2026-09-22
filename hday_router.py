"""hday_router — tool-group selection (Mandate 10 / Phase 3).

Two-stage ranker over a deferred-tool catalog, ported from
``typesafe_lab/router.py`` (rank_wide / rerank / suggest) and retargeted from
skills to tool groups. State = task text + a catalog of ``group -> {tool:
one-liner}``; the question is one choice over groups.

The default judge is deterministic BM25-style lexical scoring — the
pre_llm_call hot path can never block on a network call. A scored judge is
pluggable: any callable ``(task, {option: text}) -> {option: probability}``
works, including a ``SystemOneClient`` Choice adapter.

Hidden is never deleted: this module only RANKS groups. In the Hermes Day
wiring the result is an advisory line behind ``gate.tool_router`` (default
OFF); deferred tools stay reachable through ``tool_search`` either way.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

log = logging.getLogger("hermes-day.router")

SHORTLIST = 3      # groups carried from the wide rank into the rerank
TOP_GROUPS = 2     # groups surfaced by suggest()
_SCORE_FLOOR = 0.0  # a group scoring at/below this never gets suggested

_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Judge protocol: (task_text, {option_name: option_text}) -> {option_name: prob}
Judge = Callable[[str, Mapping[str, str]], Mapping[str, float]]


def _tokens(text: object) -> List[str]:
    """Lowercase alphanumeric tokens, plus a crude singular form so
    ``playlists`` still reaches ``playlist``."""
    out: List[str] = []
    for tok in _TOKEN_RE.findall(str(text).lower()):
        out.append(tok)
        if tok.endswith("s") and len(tok) > 3:
            out.append(tok[:-1])
    return out


def _group_text(group: str, tools: Mapping[str, str]) -> str:
    """The document a group is judged on: its name + tool names + one-liners."""
    parts = [group.replace("-", " ").replace("_", " ")]
    for name, desc in tools.items():
        parts.append(str(name).replace("_", " "))
        parts.append(str(desc))
    return " ".join(parts)


def lexical_judge(task: str, options: Mapping[str, str]) -> Dict[str, float]:
    """Default judge — BM25-lite over each option's text, normalized to a
    probability-like spread. Deterministic, offline, dependency-free."""
    q = set(_tokens(task))
    if not q or not options:
        return {name: 0.0 for name in options}
    docs = {name: _tokens(text) for name, text in options.items()}
    df = Counter(t for toks in docs.values() for t in set(toks))
    n = len(docs)
    raw: Dict[str, float] = {}
    for name, toks in docs.items():
        tf = Counter(toks)
        score = 0.0
        for t in q & tf.keys():
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            score += idf * tf[t] * 2.5 / (tf[t] + 1.5)
        raw[name] = score
    total = sum(raw.values())
    if total <= 0:
        return {name: 0.0 for name in options}
    return {name: v / total for name, v in raw.items()}


# -- request 1 -----------------------------------------------------------------


def rank_wide(task: str, catalog: Mapping[str, Mapping[str, str]], *,
              judge: Optional[Judge] = None) -> List[Tuple[str, float]]:
    """Score every group once. Returns ``[(group, prob)]`` best-first;
    deterministic tie-break on the name."""
    judge = judge or lexical_judge
    options = {g: _group_text(g, tools) for g, tools in catalog.items()}
    try:
        probs = dict(judge(task, options) or {})
    except Exception as exc:  # fail-open, loudly
        log.warning("router: wide judge failed: %s", exc)
        probs = {g: 0.0 for g in options}
    for g in options:  # a partial judge must not silently drop a group
        probs.setdefault(g, 0.0)
    return sorted(probs.items(), key=lambda kv: (-kv[1], kv[0]))


# -- request 2 -----------------------------------------------------------------


def rerank(task: str, names: Sequence[str],
           catalog: Mapping[str, Mapping[str, str]], *,
           judge: Optional[Judge] = None) -> Dict[str, Any]:
    """Re-judge the shortlist against the same group text. With the default
    judge this is one lexical pass over a smaller option set; with a scored
    judge it is the second, narrower question."""
    judge = judge or lexical_judge
    options = {g: _group_text(g, catalog[g]) for g in names if g in catalog}
    try:
        fits = dict(judge(task, options) or {})
    except Exception as exc:  # fail-open, loudly
        log.warning("router: rerank judge failed: %s", exc)
        fits = {g: 0.0 for g in options}
    winner, best = (None, 0.0)
    for name, score in sorted(fits.items()):
        if score > best:
            winner, best = name, score
    return {"winner": winner, "fits": fits}


# -- the whole recipe ----------------------------------------------------------


@dataclass
class Pick:
    """The selected groups plus the evidence that produced them."""

    groups: Tuple[str, ...]
    task: str
    reason: str = ""
    ranked: List[Tuple[str, float]] = field(default_factory=list)
    fits: Dict[str, float] = field(default_factory=dict)

    @property
    def group(self) -> Optional[str]:
        return self.groups[0] if self.groups else None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "task": self.task,
            "groups": list(self.groups),
            "reason": self.reason,
            "ranked": [[g, round(p, 5)] for g, p in self.ranked[:12]],
            "fits": {k: round(v, 4) for k, v in self.fits.items()},
        }


def suggest(task: str, catalog: Mapping[str, Mapping[str, str]], *,
            top: int = TOP_GROUPS, shortlist: int = SHORTLIST,
            judge: Optional[Judge] = None) -> Pick:
    """Two stages, one floor, at most ``top`` groups. Never raises."""
    try:
        catalog = {str(g): dict(t) for g, t in (catalog or {}).items() if t}
        result = Pick(groups=(), task=str(task or ""))
        if not result.task.strip():
            result.reason = "empty task"
            return result
        if not catalog:
            result.reason = "empty catalog"
            return result
        ranked = rank_wide(result.task, catalog, judge=judge)
        result.ranked = ranked
        names = [g for g, s in ranked[:shortlist] if s > _SCORE_FLOOR]
        if not names:
            result.reason = "no group scored above the floor"
            return result
        second = rerank(result.task, names, catalog, judge=judge)
        result.fits = {k: float(v) for k, v in second["fits"].items()}
        if not second["winner"]:
            result.reason = "rerank found no fitting group"
            return result
        ordered = [g for g, s in sorted(result.fits.items(),
                                        key=lambda kv: (-kv[1], kv[0]))
                   if s > _SCORE_FLOOR]
        result.groups = tuple(ordered[: max(1, top)])
        result.reason = (f"winner {second['winner']} "
                         f"(fits {result.fits.get(second['winner'], 0.0):.2f})")
        return result
    except Exception as exc:  # fail-open, loudly
        log.warning("router: suggest failed: %s", exc)
        return Pick(groups=(), task=str(task or ""), reason=f"error: {exc}")
