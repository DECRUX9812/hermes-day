"""Hermes Day — unified permission gate: regex-first, typed second, fail-open.

Import-safe standalone module: no PluginContext, no hooks, no I/O at import
time. The integrator wires ``decide`` into ``__init__.py``'s hot path later;
this file deliberately does NOT import ``__init__`` (parallel-wave file
discipline) — the deny-list triggers are mirrored here verbatim.

Two phases:

1. **Regex phase (hot path, zero cost)** — pure-Python regex over the
   attempted command / file edit, mirroring ``__init__._command_violation``
   and ``__init__._edit_violation``: test deletion, git test reversion,
   in-place test rewrite, test truncation, force-pass / empty-suite flags,
   exit-code laundering, assertion gutting. A hit denies immediately and the
   judge is never called.

2. **Typed phase** — ``decide(action, user_request, context, judge=...)``
   calls an injected ``judge`` callable and reads four noul signals
   (``irreversible``, ``outer_scope``, ``intent_consistent``, ``data_egress``)
   plus a ``blast_radius`` choice (``local`` < ``workspace`` < ``machine`` <
   ``external``). Escalation fires on COMBINATIONS, never a single axis:

   - ``regex_hit``                                                     -> deny
   - off-mandate AND any risk >= 0.6                                   -> deny
   - external blast AND mandate not explicit                           -> deny

   A lone high-risk signal (e.g. irreversible=1.0 on an on-mandate local
   action) never blocks by itself.

Failure policy: **fail-open, loudly.** ``judge=None``, a judge that raises,
or an internal error all return ``allow=True`` with ``kind="unjudged"`` and
a reason that records what happened — never an exception out of the hot path.

Decision object: ``{"allow": bool, "kind": str, "reason": str, "row": dict}``
where ``row`` is a ledger row: ``kind`` (``regex_hit`` | ``judged`` |
``cached`` | ``unjudged``), ``lane`` (``local`` | ``classifier`` | ``hosted``),
``ms``, ``cost``.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Callable, Dict, Optional

__all__ = [
    "decide",
    "regex_hit",
    "command_violation",
    "edit_violation",
    "BLAST_RANK",
    "RISK_THRESHOLD",
]

# ---------------------------------------------------------------------------
# Detection tables — mirrored from __init__.py (NOT imported: separate file,
# integrator wires later; drift here is caught by tests/test_hday_gate.py)
# ---------------------------------------------------------------------------

_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|test_?data|specs?|fixtures)/|"
    r"(^|/)test_[^/]+\.py$|(^|/)[^/]+_test\.(go|py|rb|java)$|"
    r"[^/]+\.(test|spec)\.[a-z0-9]{1,5}$|"
    r"(^|/)conftest\.py$|(^|/)pytest\.ini$|(^|/)tox\.ini$|"
    r"(^|/)(jest|vitest|playwright|karma|phpunit|mocha)\.config\.",
    re.IGNORECASE,
)

_CHECK_WORD = (
    r"(?:pytest|py\.test|python[0-9.]*\s+-m\s+(?:pytest|unittest)|jest|vitest|mocha|ava|tap|"
    r"go\s+test|cargo\s+(?:test|nextest)|npm\s+(?:run\s+)?test[\w:-]*|pnpm\s+(?:run\s+)?test|"
    r"yarn\s+test|bun\s+test|deno\s+test|make\s+test|ctest|rspec|rails\s+test|phpunit|"
    r"dotnet\s+test|mvn\s+(?:test|verify)|gradle[w]?\s+test|sbt\s+test|tox|nox|coverage\s+run|"
    r"ruff(?:\s+check)?|mypy|pyright|tsc|eslint|pylint|clippy|shellcheck|biome|staticcheck)"
)
_CHECK_RE = re.compile(r"\b" + _CHECK_WORD + r"\b")

# Path-shaped token inside a shell command that smells like a test file/dir.
_TEST_TOKEN = r"[^\s\"'`;&|]*(?:test|spec|conftest|__tests__|_test)[^\s\"'`;&|]*"

# Generated / vendored directories — cache cleanup under one of these is
# housekeeping, not test tampering.
_CACHE_DIRS = ("__pycache__", "pytest_cache", "mypy_cache", "ruff_cache",
               "tox", "cache", "git", "node_modules", "dist", "build",
               "target", "coverage", "next", "venv", "site-packages")

_COMMAND_VIOLATIONS = (
    (
        re.compile(r"\b(?:rm|rmdir|unlink|del(?:ete)?|trash(?:-put)?|shred)\b[^;&|\n]*" + _TEST_TOKEN, re.I),
        "deletes test files",
        "Test Deletion",
    ),
    (
        re.compile(r"\bgit\s+(?:rm|clean|restore|checkout|reset)\b[^;&|\n]*" + _TEST_TOKEN, re.I),
        "reverts or removes test files via git",
        "Test Reversion",
    ),
    (
        re.compile(r"\b(?:sed\s+-i|perl\s+-pi?|ed\s|awk\b[^;&|\n]*>\s*)\b[^;&|\n]*" + _TEST_TOKEN, re.I),
        "rewrites a test file in place",
        "In-Place Test Rewrite",
    ),
    (
        re.compile(r"(?:^|[;&|]\s*|>\s*|>>\s*)[>]{1,2}\s*['\"]?" + _TEST_TOKEN),
        "truncates a test file",
        "Test Truncation",
    ),
    (
        re.compile(r"--pass[-_]?with[-_]?no[-_]?tests|--allow[-_]?empty(?:-results)?|"
                   r"--ignore[-_]?failures|--force(?:exit)?(?:\s|=|[^\w-])", re.I),
        "adds a force-pass / empty-suite flag",
        "Exit Code Laundering",
    ),
    (
        re.compile(_CHECK_WORD + r"[^\n]*?(?:\|\|\s*(?:true|:|echo\b)|[;&|]\s*exit\s+0\b)", re.I),
        "launders the check's exit status",
        "Exit Code Laundering",
    ),
)

_ASSERT_RE = re.compile(
    r"\bassert\b|\bassert_\w+|\bexpect\s*\(|\.to(?:Be|Equal|StrictEqual|Contain|Throw|Match|HaveBeen)\w*\(|"
    r"\bpytest\.raises\b|\braise\s+AssertionError|\bTEST(?:_F|_P)?\s*\(|\bdef\s+test_\w+|"
    r"#\[\s*test\s*\]|\bit\s*\(\s*['\"]|\btest\s*\(\s*['\"]",
)

_FILE_EDIT_TOOLS = {
    "write_file", "patch", "edit_file", "apply_patch", "replace_in_file",
    "str_replace_editor", "multi_edit", "insert_edit_into_file", "fs_write",
}
_PATH_KEYS = ("path", "file_path", "target_file", "filePath", "notebook_path")
_TERMINAL_TOOLS = {"terminal", "bash", "execute_code", "run_command",
                   "shell", "powershell", "cmd"}


# ---------------------------------------------------------------------------
# Phase 1 — regex (pure python, zero cost, judge never consulted)
# ---------------------------------------------------------------------------

def _is_generated_path(path: str) -> bool:
    parts = [p.strip(".") for p in str(path or "").replace("\\", "/").split("/") if p]
    return any(part in _CACHE_DIRS for part in parts)


def _without_generated_paths(cmd: str) -> str:
    return " ".join(w for w in str(cmd or "").split() if not _is_generated_path(w))


def _without_generated_paths_git(cmd: str) -> str:
    """Same filter, but the bare word ``git`` survives. The cache-dir list in
    ``__init__`` contains ``git``, which strips the token ``git`` itself and
    makes the declared Test Reversion trigger unreachable; we keep the token so
    ``git <verb> <test path>`` fires as declared (still exempts cache paths
    like ``.git/`` / ``dist/tests/``)."""
    return " ".join(w for w in str(cmd or "").split()
                    if not _is_generated_path(w) or w.strip("./").lower() == "git")


def command_violation(cmd: str) -> Optional[tuple]:
    """``(reason, rule)`` for a shell command that fakes a pass, else None."""
    probe = _without_generated_paths(cmd)
    git_probe = _without_generated_paths_git(cmd)
    for pattern, reason, rule in _COMMAND_VIOLATIONS:
        target = git_probe if rule == "Test Reversion" else probe
        if pattern.search(target):
            return reason, rule
    return None


def _comment_prefix(path: str) -> str:
    if re.search(r"\.(py|sh|yaml|yml|toml|ini|cfg|rb|pl|r|jl|nim|conf)$", path, re.I):
        return "#"
    return "//"


def _assertion_lines(text: str, path: str) -> int:
    prefix = _comment_prefix(path)
    n = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(prefix):
            continue
        if _ASSERT_RE.search(line):
            n += 1
    return n


def _read_existing(path: str) -> Optional[str]:
    try:
        if path and os.path.isfile(path) and os.path.getsize(path) < 2_000_000:
            with open(path, encoding="utf-8", errors="replace") as handle:
                return handle.read()
    except OSError:
        pass
    return None


def edit_violation(path: str, args: Dict[str, Any]) -> Optional[tuple]:
    """``(reason, rule)`` when an edit guts/weaken a test file's assertions."""
    if not path or not _TEST_PATH_RE.search(path) or _is_generated_path(path):
        return None
    edits = args.get("edits")
    if isinstance(edits, list) and edits:
        pairs = [(str(e.get("old_string") or e.get("old_str") or e.get("old") or ""),
                  str(e.get("new_string") or e.get("new_str") or ""))
                 for e in edits if isinstance(e, dict)]
    else:
        old = str(args.get("old_string") or args.get("old_str") or "")
        new = str(args.get("new_string") or args.get("new_str") or "")
        if not old and not new:
            content = args.get("content")
            if isinstance(content, str):
                old = _read_existing(path) or ""
                new = content
        pairs = [(old, new)]
    for old, new in pairs:
        if not old and new:
            continue  # creating/extending a test file is fine
        a_old, a_new = _assertion_lines(old, path), _assertion_lines(new, path)
        if a_old > 0 and a_new == 0:
            return f"deletes all {a_old} assertion(s) in {path}", "Assertion Gutting"
        if a_old >= 2 and a_new < a_old:
            return f"weakens assertions in {path} ({a_old} -> {a_new})", "Assertion Gutting"
    return None


def regex_scan(action: str, context: Dict[str, Any]) -> Optional[tuple]:
    """Run the mirrored deny-list over the attempted call. ``(reason, rule)``."""
    action = str(action or "")
    cmd = context.get("command") or context.get("cmd")
    if not cmd and action in _TERMINAL_TOOLS:
        cmd = context.get("code")
    if isinstance(cmd, str) and cmd.strip():
        hit = command_violation(cmd)
        if hit:
            return hit
    raw_args = context.get("args")
    args = raw_args if isinstance(raw_args, dict) else context
    path = next((str(context.get(k) or "") for k in _PATH_KEYS if context.get(k)), "")
    if not path:
        path = next((str(args.get(k) or "") for k in _PATH_KEYS if args.get(k)), "")
    if path and (action in _FILE_EDIT_TOOLS or _TEST_PATH_RE.search(path)
                 or any(k in args for k in ("old_string", "new_string", "content", "edits"))):
        hit = edit_violation(path, args)
        if hit:
            return hit
    return None


def regex_hit(cmd: str) -> Optional[tuple]:
    """Command-only phase-1 entry point (``(reason, rule)`` or None)."""
    return command_violation(str(cmd or ""))


# ---------------------------------------------------------------------------
# Phase 2 — typed signals
# ---------------------------------------------------------------------------

RISK_THRESHOLD = 0.6          # escalate only when this is COMBINED with another axis
_INTENT_THRESHOLD = 0.5       # intent_consistent noul below this = off-mandate

BLAST_RANK = {"local": 0, "workspace": 1, "machine": 2, "external": 3}
_BLAST_RISK = {"local": 0.1, "workspace": 0.35, "machine": 0.65, "external": 0.9}
_LANES = ("local", "classifier", "hosted")
_KINDS = ("regex_hit", "judged", "cached", "unjudged")


def _noul(value: Any) -> float:
    """Coerce a signal to a 0..1 noul — accepts raw numbers, bools, or an
    object carrying ``.noul`` (typesafe-lab ``Answer``)."""
    value = getattr(value, "noul", value)
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(min(1.0, max(0.0, value)))
    try:
        return float(min(1.0, max(0.0, float(value))))
    except (TypeError, ValueError):
        return 0.0


def _get(res: Any, key: str, default: Any = None) -> Any:
    if isinstance(res, dict):
        return res.get(key, default)
    return getattr(res, key, default)


def _blast_radius(value: Any) -> tuple:
    """``(canonical_name, rank)`` for a blast-radius choice."""
    value = getattr(value, "noul", value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        rank = int(min(3, max(0, value)))
        inv = {v: k for k, v in BLAST_RANK.items()}
        return inv[rank], rank
    name = str(value or "local").strip().lower()
    if name not in BLAST_RANK:
        name = "local"
    return name, BLAST_RANK[name]


def _signals(res: Any) -> Dict[str, Any]:
    inner = _get(res, "signals")
    src = inner if isinstance(inner, dict) else res
    blast, rank = _blast_radius(_get(src, "blast_radius", "local"))
    sig = {
        "irreversible": _noul(_get(src, "irreversible")),
        "outer_scope": _noul(_get(src, "outer_scope")),
        "intent_consistent": _noul(_get(src, "intent_consistent", 1.0)),
        "data_egress": _noul(_get(src, "data_egress")),
        "blast_radius": blast,
        "blast_rank": rank,
    }
    declared = _get(src, "risk")
    if declared is None:
        risk = max(sig["irreversible"], sig["outer_scope"], sig["data_egress"],
                   _BLAST_RISK[blast])
    else:
        risk = _noul(declared)
    sig["risk"] = risk
    return sig


def _row(kind: str, lane: str, ms: float, cost: float, **extra: Any) -> Dict[str, Any]:
    if kind not in _KINDS:
        kind = "unjudged"
    if lane not in _LANES:
        lane = "local"
    row = {"kind": kind, "lane": lane, "ms": round(float(ms), 3),
           "cost": float(cost)}
    row.update(extra)
    return row


def _decision(allow: bool, kind: str, reason: str, row: Dict[str, Any]) -> Dict[str, Any]:
    return {"allow": bool(allow), "kind": kind, "reason": reason, "row": row}


def decide(action: str = "", user_request: str = "", context: Optional[Dict[str, Any]] = None,
           judge: Optional[Callable[..., Any]] = None, **_kw: Any) -> Dict[str, Any]:
    """Lane-chain-ready permission decision. Never raises.

    Phase 1 (regex, zero cost) short-circuits to a deny before the judge is
    ever called. Phase 2 reads the injected judge's signals and escalates on
    combinations only. Missing/raising judge fails open with ``unjudged``.
    """
    t0 = time.perf_counter()
    ctx = context if isinstance(context, dict) else {}
    action = str(action or "")
    request = str(user_request or "")

    def _ms() -> float:
        return (time.perf_counter() - t0) * 1000.0

    # ---- Phase 1: regex -----------------------------------------------------
    try:
        hit = regex_scan(action, ctx)
    except Exception as exc:  # a raising scanner must not wedge the tool loop
        return _decision(
            True, "unjudged",
            f"regex scan raised {type(exc).__name__}: {exc} — fail-open (unjudged)",
            _row("unjudged", "local", _ms(), 0.0, action=action[:120]),
        )
    if hit:
        reason, rule = hit
        return _decision(
            False, "regex_hit",
            f"blocked by deny-list: it {reason} ({rule})",
            _row("regex_hit", "local", _ms(), 0.0, action=action[:120],
                 rule=rule, detail=reason[:200]),
        )

    # ---- Phase 2: typed -----------------------------------------------------
    if judge is None:
        return _decision(
            True, "unjudged",
            "no judge configured — fail-open (unjudged)",
            _row("unjudged", "local", _ms(), 0.0, action=action[:120]),
        )
    try:
        res = judge(action, request, ctx)
    except Exception as exc:
        return _decision(
            True, "unjudged",
            f"judge raised {type(exc).__name__}: {exc} — fail-open (unjudged)",
            _row("unjudged", "local", _ms(), 0.0, action=action[:120]),
        )
    if res is None:
        return _decision(
            True, "unjudged",
            "judge returned no signals — fail-open (unjudged)",
            _row("unjudged", "local", _ms(), 0.0, action=action[:120]),
        )

    try:
        sig = _signals(res)
        lane = str(_get(res, "lane", "local") or "local")
        cost = _noul(_get(res, "cost", 0.0))
        kind = str(_get(res, "kind", "") or ("cached" if _get(res, "cached") else "judged"))
        if kind not in ("judged", "cached"):
            kind = "judged"
        mandate_explicit = bool(ctx.get("mandate_explicit"))
        off_mandate = sig["intent_consistent"] < _INTENT_THRESHOLD
        external = sig["blast_rank"] >= BLAST_RANK["external"]

        row_extra = {
            "action": action[:120],
            "signals": {k: sig[k] for k in ("irreversible", "outer_scope",
                                            "intent_consistent", "data_egress",
                                            "blast_radius", "risk")},
            "mandate_explicit": mandate_explicit,
        }

        # Escalation: COMBINATIONS only, never a single axis.
        if off_mandate and sig["risk"] >= RISK_THRESHOLD:
            reason = (f"escalated: off-mandate (intent_consistent="
                      f"{sig['intent_consistent']:.2f}) combined with risk "
                      f"{sig['risk']:.2f} >= {RISK_THRESHOLD}")
            return _decision(False, kind, reason,
                             _row(kind, lane, _ms(), cost, rule="Combination", **row_extra))
        if external and not mandate_explicit:
            reason = (f"escalated: external blast radius "
                      f"({sig['blast_radius']}) without an explicit mandate")
            return _decision(False, kind, reason,
                             _row(kind, lane, _ms(), cost, rule="Combination", **row_extra))
        reason = (f"allowed: on-mandate, blast={sig['blast_radius']}, "
                  f"risk={sig['risk']:.2f}")
        return _decision(True, kind, reason, _row(kind, lane, _ms(), cost, **row_extra))
    except Exception as exc:  # fail-open loudly — never raise from the hot path
        return _decision(
            True, "unjudged",
            f"signal evaluation raised {type(exc).__name__}: {exc} — fail-open (unjudged)",
            _row("unjudged", "local", _ms(), 0.0, action=action[:120]),
        )
