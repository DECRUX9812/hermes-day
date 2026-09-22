"""hday_manifest — the ONE shared repo-state walk for N background tasks.

`build_manifest(repo_root, max_files=2000)` walks a repo exactly once and
returns a JSON-serializable dict that any number of read-only background
tasks consume instead of re-walking:

    {
      "root":        absolute repo root,
      "built_at":    float epoch seconds,
      "files":       [{"path", "loc", "mtime", "sha1_10"}, ...],  # "/"-separated relpaths
      "symbols":     {"<relpath>": ["name", ...]},  # from ^\\s*(?:def|class)\\s+(\\w+)
      "recent_diff": [{"hash","author","date","subject"}, ...],  # last 20 git log entries, [] if not a repo / git missing
      "total_loc":   int,
    }

`persist(manifest, path)` / `load(path, stale_after_s=...)` roundtrip it to
disk; `load` returns `(None, reason)` where reason is one of
"missing" | "unreadable" | "malformed" | "stale".

Failure policy: fail-open, loudly. No function raises on a hot path — errors
are logged via the ``hday.manifest`` logger and degrade to empty/None results.

Import-safe: module import performs no I/O, builds nothing, spawns nothing.
Pure stdlib.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time

log = logging.getLogger("hday.manifest")

# Directories never walked (repo VCS state, deps, caches).
SKIP_DIRS = frozenset({".git", "node_modules", "venv", ".venv", "__pycache__"})

DEFAULT_MAX_FILES = 2000
DEFAULT_STALE_AFTER_S = 6 * 3600.0  # 6 h
_GIT_TIMEOUT_S = 10

# Per spec: parse 'def'/'class' definition lines only — no ast dependency.
_SYMBOL_RE = re.compile(r"^[ \t]*(?:def|class)[ \t]+(\w+)", re.MULTILINE)

_PATH_LIKE = (str, bytes, os.PathLike)


def _warn(msg: str, *args) -> None:
    log.warning("hday_manifest: " + msg, *args)


def _git_recent(root: str) -> list:
    """Last 20 `git log` entries as dicts; [] on any failure (fail-open)."""
    if not shutil.which("git"):
        _warn("git not found on PATH; recent_diff=[]")
        return []
    if not os.path.exists(os.path.join(root, ".git")):
        return []  # not a repo (or a worktree whose .git lives elsewhere): not an error
    try:
        top = subprocess.run(
            ["git", "-C", root, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S,
        )
        if top.returncode != 0:
            _warn("git rev-parse failed (%s); recent_diff=[]", top.stderr.strip()[:200])
            return []
        toplevel = os.path.realpath(top.stdout.strip())
        if toplevel != os.path.realpath(root):
            # fake/incomplete .git dir; git walked up to an ancestor repo
            _warn("git toplevel %r != root %r; recent_diff=[]", toplevel, root)
            return []
        log_run = subprocess.run(
            ["git", "-C", root, "log", "-n", "20",
             "--pretty=format:%H%x09%an%x09%aI%x09%s"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S,
        )
        if log_run.returncode != 0:
            _warn("git log failed (%s); recent_diff=[]", log_run.stderr.strip()[:200])
            return []
        return _parse_git_log(log_run.stdout)
    except Exception as exc:  # noqa: BLE001 — fail-open, loudly
        _warn("git log raised %s; recent_diff=[]", exc)
        return []


def _parse_git_log(stdout: str) -> list:
    entries = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 3)
        if len(parts) != 4:
            _warn("skipping malformed git log line: %r" % line[:80])
            continue
        h, author, date, subject = parts
        entries.append({"hash": h, "author": author, "date": date, "subject": subject})
    return entries


def build_manifest(repo_root, max_files: int = DEFAULT_MAX_FILES) -> dict:
    """Walk `repo_root` once and return the shared state-find manifest.

    Respects `max_files` (file cap) and never enters SKIP_DIRS. Logs and
    skips individual unreadable files rather than raising.
    """
    root = os.path.abspath(os.path.expanduser(os.fspath(repo_root)))
    files: list = []
    symbols: dict = {}
    total_loc = 0

    if max_files < 0:
        max_files = 0

    try:
        walker = os.walk(root)
        for dirpath, dirnames, filenames in walker:
            # prune in-place (topdown) + deterministic order
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                if len(files) >= max_files:
                    break
                fp = os.path.join(dirpath, name)
                try:
                    st = os.stat(fp)
                    if not os.path.isfile(fp):
                        continue
                    with open(fp, "rb") as fh:
                        data = fh.read()
                except OSError as exc:
                    _warn("skipping %s: %s", fp, exc)
                    continue
                rel = os.path.relpath(fp, root).replace(os.sep, "/")
                text = data.decode("utf-8", errors="replace")
                loc = len(text.splitlines())
                files.append({
                    "path": rel,
                    "loc": loc,
                    "mtime": st.st_mtime,
                    "sha1_10": hashlib.sha1(data).hexdigest()[:10],
                })
                total_loc += loc
                names = _SYMBOL_RE.findall(text)
                if names:
                    symbols[rel] = names
            else:
                continue
            break  # max_files hit during inner loop
    except Exception as exc:  # noqa: BLE001 — fail-open; keep what we have
        _warn("walk failed after %d files: %s", len(files), exc)

    return {
        "root": root,
        "built_at": time.time(),
        "files": files,
        "symbols": symbols,
        "recent_diff": _git_recent(root),
        "total_loc": total_loc,
    }


def persist(a, b=None) -> bool:
    """Write a manifest to JSON at `path`. Fail-open: returns False, never raises.

    Accepted call shapes:
      persist(manifest, path)   — write an existing manifest
      persist(path, manifest)   — same, args swapped
      persist(path)             — build a fresh manifest rooted at dirname(path) and write it
    """
    manifest = None
    path = None
    if isinstance(a, _PATH_LIKE) and b is None:
        path, manifest = a, None
    elif isinstance(a, _PATH_LIKE) and isinstance(b, dict):
        path, manifest = a, b
    elif isinstance(a, dict) and isinstance(b, _PATH_LIKE):
        manifest, path = a, b
    else:
        _warn("persist(manifest, path) called with invalid args: %r, %r" % (type(a), type(b)))
        return False

    path = os.path.abspath(os.path.expanduser(os.fspath(path)))
    try:
        if manifest is None:
            manifest = build_manifest(os.path.dirname(path) or ".")
        if not isinstance(manifest, dict) or "built_at" not in manifest:
            _warn("persist refusing non-manifest payload for %s", path)
            return False
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, separators=(",", ":"), sort_keys=False)
        os.replace(tmp, path)  # atomic on POSIX
        return True
    except Exception as exc:  # noqa: BLE001 — fail-open
        _warn("persist to %s failed: %s", path, exc)
        return False


def load(path, stale_after_s: float = DEFAULT_STALE_AFTER_S):
    """Load a persisted manifest.

    Returns ``(manifest, None)`` on success or ``(None, reason)`` where reason
    is "missing" | "unreadable" | "malformed" | "stale". A manifest older than
    `stale_after_s` (measured from its `built_at`) is stale: `(None, "stale")`.
    Pass `stale_after_s=None` to disable the staleness policy.
    """
    path = os.path.abspath(os.path.expanduser(os.fspath(path)))
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return None, "missing"
    except OSError as exc:
        _warn("load %s unreadable: %s", path, exc)
        return None, "unreadable"

    try:
        manifest = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        _warn("load %s malformed JSON: %s", path, exc)
        return None, "malformed"

    if (not isinstance(manifest, dict)
            or not isinstance(manifest.get("built_at"), (int, float))
            or "files" not in manifest
            or "total_loc" not in manifest):
        _warn("load %s malformed shape (keys=%r)", path, sorted(manifest)[:10] if isinstance(manifest, dict) else type(manifest))
        return None, "malformed"

    if stale_after_s is not None:
        age = time.time() - float(manifest["built_at"])
        if age > stale_after_s:
            return None, "stale"
    return manifest, None
