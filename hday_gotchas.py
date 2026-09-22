"""hday_gotchas — conditional per-directory context loader (Mandate 9).

Compaction-proof standing context: every `**/GOTCHAS.md` under a repo root is
discovered once, then injected into the prompt ONLY for directories actually
touched by paths currently in context. Sections are pruned to a hard 60-line
cap and truncation is always recorded, never silent.

Precedence (higher wins on conflict): instincts > gotchas > skills.

Design notes:
- Pure, import-safe module: no I/O, no hook registration, no state at import.
- Idempotent: same input -> byte-identical output (deterministic ordering:
  shallowest directory first, then alphabetical).
- Fail-open, loudly: every public function catches exceptions, logs a warning,
  and returns the empty/default result instead of raising on the hot path.
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

log = logging.getLogger("hermes-day.gotchas")

#: Hard cap on the injected block, lines including header + truncation marker.
MAX_SECTION_LINES = 60

#: Directory names never descended into during discovery.
IGNORED_DIRS = frozenset({
    "node_modules",
    ".git",
    "venvs",
    ".venv",
    "venv",
    "__pycache__",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
})

GOTCHAS_FILENAME = "GOTCHAS.md"

# First line of every emitted block; carries the precedence note.
_HEADER = ("[Hermes Day — standing repo context. "
           "Precedence: instincts > gotchas > skills.]")


# --------------------------------------------------------------------- util


def _normalize(path: object) -> str:
    """Normalize a context path to a repo-relative posix string."""
    s = str(path).replace("\\", "/").strip()
    while s.startswith("./"):
        s = s[2:]
    return s.rstrip("/")


def _candidates(path: object) -> List[str]:
    """Strings a discovered directory may legitimately be a prefix of.

    Relative paths are matched as-is. Absolute paths additionally yield their
    component suffixes, so `/repo/src/foo/main.py` still activates `src/foo`
    when the session hands us an absolute path.
    """
    s = _normalize(path)
    if not s:
        return []
    out: List[str] = [s]
    if s.startswith("/"):
        parts = [p for p in s.split("/") if p]
        out.append("/".join(parts))
        for i in range(1, len(parts)):
            out.append("/".join(parts[i:]))
    # de-dupe, keep order
    seen = set()
    uniq = []
    for c in out:
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def _is_prefix(directory: str, candidate: str) -> bool:
    """Path-boundary prefix test: `src/foo` matches `src/foo/x.py` and
    `src/foo`, but never `src/foobar` or `src/bar`. Empty dir = repo root,
    a prefix of everything."""
    if directory == "":
        return True
    return candidate == directory or candidate.startswith(directory + "/")


def _depth(directory: str) -> int:
    return 0 if directory == "" else directory.count("/") + 1


# ---------------------------------------------------------------- discovery


def discover(repo_root: str) -> Dict[str, str]:
    """Walk `repo_root` for `**/GOTCHAS.md`, skipping ignored directories.

    Returns `{relpath_dir: gotchas_text}` where the repo root itself maps to
    `""`. Missing/unreadable roots fail open to `{}` (logged, not raised).
    """
    found: Dict[str, str] = {}
    try:
        root = os.path.abspath(str(repo_root))
        if not os.path.isdir(root):
            return found
        for dirpath, dirnames, filenames in os.walk(root):
            # deterministic walk + prune ignorable subtrees everywhere
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
            if GOTCHAS_FILENAME not in filenames:
                continue
            rel = os.path.relpath(dirpath, root)
            if rel == os.curdir:
                rel = ""
            else:
                rel = rel.replace(os.sep, "/")
            try:
                with open(os.path.join(dirpath, GOTCHAS_FILENAME),
                          "r", encoding="utf-8", errors="replace") as fh:
                    found[rel] = fh.read()
            except OSError as exc:
                log.warning("gotchas: cannot read %s/%s: %s",
                            dirpath, GOTCHAS_FILENAME, exc)
    except Exception as exc:  # fail-open, loudly
        log.warning("gotchas: discovery failed for %r: %s", repo_root, exc)
    return found


# ---------------------------------------------------------- prefix matching


def sections_for(paths_in_context: Sequence[object],
                 discovered: Dict[str, str]) -> List[Tuple[str, str]]:
    """Ordered `(dir, text)` pairs whose `dir` prefixes ANY context path.

    Only directories actually touched get injected. Ordering is deterministic:
    shallowest first, then alphabetical.
    """
    try:
        if not discovered:
            return []
        paths = [p for p in (paths_in_context or []) if str(p).strip()]
        cand_cache = [_candidates(p) for p in paths]
        selected: List[Tuple[str, str]] = []
        for directory, text in discovered.items():
            for cands in cand_cache:
                if any(_is_prefix(directory, c) for c in cands):
                    selected.append((directory, text))
                    break
        selected.sort(key=lambda kv: (_depth(kv[0]), kv[0]))
        return selected
    except Exception as exc:  # fail-open, loudly
        log.warning("gotchas: sections_for failed: %s", exc)
        return []


# ------------------------------------------------------------ build + cap


def _render(sections: Sequence[Tuple[str, str]], truncated: int) -> List[str]:
    lines: List[str] = [_HEADER]
    for directory, text in sections:
        label = directory if directory else os.curdir  # "./GOTCHAS.md" at root
        lines.append(f"In {label} ({GOTCHAS_FILENAME}):")
        body = str(text).splitlines()
        while body and not body[-1].strip():
            body.pop()
        lines.extend(body)
    if truncated:
        lines.append(f"# gotchas: truncated {truncated} section(s) to fit the "
                     f"{MAX_SECTION_LINES}-line cap")
    return lines


def build_section(sections: Sequence[Tuple[str, str]],
                  max_lines: int = MAX_SECTION_LINES) -> Optional[str]:
    """Single prompt block of at most `max_lines` lines (default 60).

    Empty input -> `None`. Overflow is truncated oldest/lowest-relevance
    first (deepest sections drop before shallowest; body keeps its head),
    and the dropped count is always recorded in a trailing `#` comment —
    never a silent drop. Deterministic: same input -> same output.
    """
    try:
        if not sections:
            return None
        chosen: List[Tuple[str, str]] = list(sections)
        truncated = 0
        lines = _render(chosen, truncated)
        # 1) drop whole sections from the END (lowest relevance = deepest)
        while len(lines) > max_lines and len(chosen) > 1:
            chosen.pop()
            truncated += 1
            lines = _render(chosen, truncated)
        # 2) a single section still too big: keep its head, mark the tail gone
        if len(lines) > max_lines:
            truncated += 1
            directory, text = chosen[0]
            overhead = len(_render([(directory, "")], truncated))
            keep = max(0, max_lines - overhead)
            head = "\n".join(str(text).splitlines()[:keep])
            lines = _render([(directory, head)], truncated)
            if len(lines) > max_lines:  # pathological label length
                lines = lines[:max_lines]
        return "\n".join(lines)
    except Exception as exc:  # fail-open, loudly
        log.warning("gotchas: build_section failed: %s", exc)
        return None
