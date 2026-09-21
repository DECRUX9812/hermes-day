"""Hermes Day — agent half: evidence-locked completion gate + honesty hooks.

Companion to the desktop cockpit (``desktop/plugin.js``). Registers:

- ``pre_tool_call`` — honesty guard. Blocks tool calls that fake a pass:
  deleting/reverting/in-place-rewriting test files, gutting assertions via
  file edits, ``--passWithNoTests``-style flags, and ``|| true`` exit
  laundering on test/check commands.
- ``post_tool_call`` — observes terminal runs; records test/lint/typecheck
  executions (command + exit status) as completion evidence.
- ``pre_verify`` — completion gate. When the agent edited files and is about
  to finish, requires a passing check run in this session; returns
  ``{"action": "continue"}`` nudges while evidence is missing (self-throttled
  to two nudges, inside the framework's ``agent.max_verify_nudges`` bound).
- ``on_session_finalize`` — settles the session's verdict on teardown.
- Turn snapshots — after each file-mutating tool call, commits the
  workspace to a private git ref (``refs/hday/tsnap/<sid>/<n>``) via
  commit-tree plumbing on a throwaway index; the worktree and the user's
  real index are never touched. Powers the inspector's turn scrubber.
- ``/day-evidence`` — JSON verdict map (verdicts, runs, trap ledger,
  snapshots) the desktop half polls via ``command.dispatch``.
- ``/day-rollback`` — ``<session_key> <sha>``: restores the worktree to a
  turn snapshot (``git restore --source``).
- ``/day-impact`` — ``<session_key>``: blast-radius scan — symbols touched,
  downstream dependents, and which dependents lack test coverage.

State: per-session records in ``ctx.state`` under the ``sessions`` key
(capped, trimmed), plus an in-memory mirror for hot-path updates.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from typing import Any, Dict, Optional

_STATE_KEY = "sessions"
_MAX_SESSIONS = 60
_MAX_RUNS = 20
_MAX_BLOCKS = 20
_MAX_SNAPS = 25
_MAX_VERIFY_NUDGES = 2  # self-throttle; framework caps at agent.max_verify_nudges

_CTX = None  # PluginContext, set in register()
_LOCK = threading.RLock()
_SESSIONS: Dict[str, Dict[str, Any]] = {}

# ---------------------------------------------------------------------------
# Detection tables
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

# ---------------------------------------------------------------------------
# Session records
# ---------------------------------------------------------------------------


def _new_rec() -> Dict[str, Any]:
    return {"files": [], "runs": [], "blocks": [], "verdict": None,
            "detail": "", "at": 0.0, "nudges": 0, "snaps": []}


def _rec(sid: str) -> Dict[str, Any]:
    if not sid:
        sid = "_unknown"
    rec = _SESSIONS.get(sid)
    if rec is None:
        rec = _new_rec()
        _SESSIONS[sid] = rec
        while len(_SESSIONS) > _MAX_SESSIONS:  # evict oldest
            oldest = min(_SESSIONS, key=lambda k: _SESSIONS[k].get("at") or 0)
            _SESSIONS.pop(oldest, None)
    return rec


def _append(lst: list, item: Dict[str, Any], cap: int) -> None:
    lst.append(item)
    del lst[: max(0, len(lst) - cap)]


def _persist() -> None:
    if _CTX is None:
        return
    try:
        _CTX.state.set(_STATE_KEY, _SESSIONS)
    except Exception:
        pass


def _load_persisted() -> None:
    if _CTX is None:
        return
    try:
        stored = _CTX.state.get(_STATE_KEY) or {}
    except Exception:
        return
    if isinstance(stored, dict):
        for sid, rec in stored.items():
            if isinstance(rec, dict) and sid not in _SESSIONS:
                merged = _new_rec()
                merged.update({k: rec.get(k, merged[k]) for k in merged})
                _SESSIONS[sid] = merged


# ---------------------------------------------------------------------------
# Honesty guard
# ---------------------------------------------------------------------------


def _command_violation(cmd: str) -> Optional[tuple]:
    """(reason, rule) for a shell command that fakes a pass, else None."""
    for pattern, reason, rule in _COMMAND_VIOLATIONS:
        if pattern.search(cmd):
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


def _edit_violation(path: str, args: Dict[str, Any]) -> Optional[str]:
    if not path or not _TEST_PATH_RE.search(path):
        return None
    edits = args.get("edits")
    if isinstance(edits, list) and edits:
        pairs = [(str(e.get("old_string") or e.get("old_str") or e.get("old") or ""),
                  str(e.get("new_string") or e.get("new_str") or e.get("new") or ""))
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


def _call_preview(tool_name: str, args: Dict[str, Any]) -> str:
    """Short monospace-friendly rendering of the attempted tool call."""
    if tool_name == "terminal":
        return str(args.get("command") or args.get("code") or "")[:300]
    path = next((str(args.get(k) or "") for k in _PATH_KEYS if args.get(k)), "")
    return f"{tool_name} {path}"[:300]


def _block(sid: str, tool_name: str, reason: str, rule: str = "Honesty Guard",
           args: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    message = (
        f"Hermes Day honesty guard BLOCKED this call — it {reason}. Tests and checks are "
        "evidence, not decoration: fix the implementation or ask the human, do not modify, "
        "delete, or launder the test suite to force a pass."
    )
    with _LOCK:
        rec = _rec(sid)
        _append(rec["blocks"], {"reason": reason, "rule": rule, "tool": tool_name,
                                "call": _call_preview(tool_name, args or {}),
                                "intervention": message, "ts": time.time()},
                _MAX_BLOCKS)
        rec["verdict"] = "flagged"
        rec["detail"] = f"honesty guard x{len(rec['blocks'])}"
        rec["at"] = time.time()
        _persist()
    return {"action": "block", "message": message}


def _pre_tool_call(tool_name: str = "", args: Optional[Dict[str, Any]] = None,
                   session_id: str = "", **_kw: Any) -> Optional[Dict[str, str]]:
    try:
        if not _enabled("honesty"):
            return None
        args = args if isinstance(args, dict) else {}
        hit = None
        if tool_name == "terminal":
            cmd = str(args.get("command") or args.get("code") or "")
            hit = _command_violation(cmd) if cmd else None
        elif tool_name in _FILE_EDIT_TOOLS:
            path = next((str(args.get(k) or "") for k in _PATH_KEYS if args.get(k)), "")
            hit = _edit_violation(path, args)
        if not hit:
            return None
        reason, rule = hit
        return _block(session_id, tool_name, reason, rule, args)
    except Exception:
        return None  # never let the guard wedge the tool loop


# ---------------------------------------------------------------------------
# Evidence observer
# ---------------------------------------------------------------------------


def _exit_code(result: Any, status: Optional[str]) -> Optional[int]:
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (ValueError, TypeError):
            text = result.lower()
            if "exit code" in text or "exit_code" in text:
                m = re.search(r"exit[_ ]code[\"'\s:=]+(-?\d+)", text)
                if m:
                    return int(m.group(1))
            return None
    if isinstance(result, dict):
        for key in ("exit_code", "exitCode", "exit", "returncode", "return_code"):
            value = result.get(key)
            if isinstance(value, int):
                return value
        inner = result.get("result")
        if isinstance(inner, dict):
            return _exit_code(inner, status)
    if status == "error":
        return 1
    return None


def _post_tool_call(tool_name: str = "", args: Optional[Dict[str, Any]] = None,
                    result: Any = None, status: Optional[str] = None,
                    session_id: str = "", **_kw: Any) -> None:
    try:
        if not _enabled("observe"):
            return
        args = args if isinstance(args, dict) else {}
        if isinstance(result, str) and "honesty guard BLOCKED" in result:
            return  # vetoed calls echo back as tool results — not evidence
        if status == "error":
            failed_edit = tool_name in _FILE_EDIT_TOOLS
        else:
            failed_edit = False
        with _LOCK:
            rec = _rec(session_id)
            if tool_name in _FILE_EDIT_TOOLS:
                path = next((str(args.get(k) or "") for k in _PATH_KEYS if args.get(k)), "")
                if path and path not in rec["files"]:
                    rec["files"].append(path)
                    del rec["files"][: max(0, len(rec["files"]) - 40)]
                if not failed_edit and _enabled("snaps"):
                    _snapshot(rec, session_id, path, f"{tool_name} {os.path.basename(path)}")
                    _persist()
                return
            if tool_name != "terminal":
                return
            cmd = str(args.get("command") or args.get("code") or "")
            if cmd and _WRITE_CMD_RE.search(cmd) and _enabled("snaps"):
                _snapshot(rec, session_id, "", f"$ {cmd[:50]}")
            if not cmd or not _CHECK_RE.search(cmd):
                return
            code = _exit_code(result, status)
            _append(rec["runs"], {"cmd": cmd[:240], "exit": code,
                                  "ok": code == 0, "ts": time.time()}, _MAX_RUNS)
            _persist()
    except Exception:
        return


# ---------------------------------------------------------------------------
# Turn snapshots — shadow-git plumbing on a throwaway index
# ---------------------------------------------------------------------------

# terminal commands that plausibly mutate the workspace (snapshot after them)
_WRITE_CMD_RE = re.compile(
    r"(?:>>?[^&|]|sed\s+-i|\btee\b|\b(?:mv|cp|rm|mkdir|touch|truncate|dd)\b|"
    r"\b(?:npm|pnpm|yarn|pip|uv|poetry|cargo|go)\s+(?:install|add|update|remove)|"
    r"\bmake\b|\bapply_patch\b|\bchown|\bchmod)",
    re.I,
)


def _git(root: str, *args: str, env: Optional[Dict[str, str]] = None,
         timeout: int = 15) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env["GIT_OPTIONAL_LOCKS"] = "0"
    if env:
        full_env.update(env)
    return subprocess.run(
        ["git", "-C", root, *args], capture_output=True, text=True,
        env=full_env, timeout=timeout)


def _repo_root(path: str) -> Optional[str]:
    """Git toplevel containing ``path``, or None when it isn't in a repo."""
    if not path or not os.path.isabs(path):
        return None
    start = path if os.path.isdir(path) else os.path.dirname(path)
    if not start or not os.path.isdir(start):
        return None
    try:
        out = _git(start, "rev-parse", "--show-toplevel", timeout=5)
    except Exception:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _snapshot(rec: Dict[str, Any], sid: str, hint_path: str, label: str) -> None:
    """Commit the worktree (incl. untracked) to a private ref via a temp index.

    Never touches the user's real index or the working tree:
    GIT_INDEX_FILE points at a scratch file, commit-tree writes an object.
    Caller holds _LOCK.
    """
    root = _repo_root(hint_path or "") or rec.get("_root")
    if not root:
        return
    rec["_root"] = root  # remember the workspace root for later snaps/impact
    try:
        head_tree = _git(root, "rev-parse", "HEAD^{tree}", timeout=5)
        fd, idx = tempfile.mkstemp(prefix="hday-idx-", dir=os.path.join(root, ".git"))
        os.close(fd)
        try:
            env = {"GIT_INDEX_FILE": idx}
            _git(root, "read-tree", "HEAD", env=env)
            _git(root, "add", "-A", env=env, timeout=30)
            tree = _git(root, "write-tree", env=env).stdout.strip()
        finally:
            try:
                os.unlink(idx)
            except OSError:
                pass
        if not tree or (head_tree.returncode == 0 and tree == head_tree.stdout.strip()):
            return  # nothing changed vs HEAD — no snap needed
        snaps = rec["snaps"]
        if snaps and snaps[-1].get("tree") == tree:
            return  # identical to the previous snap
        n = len(snaps) + 1
        msg = f"[hday-turnsnap] {sid} turn-snapshot {n}: {label[:60]}"
        sha = _git(root, "commit-tree", tree, "-p", "HEAD", "-m", msg).stdout.strip()
        if not sha:
            return
        _git(root, "update-ref", f"refs/hday/tsnap/{sid}/{n}", sha)
        _append(snaps, {"n": n, "sha": sha, "tree": tree, "root": root,
                        "label": label[:80], "ts": time.time()}, _MAX_SNAPS)
    except Exception:
        return  # snapshots are best-effort — never wedge the tool loop


# ---------------------------------------------------------------------------
# Completion gate
# ---------------------------------------------------------------------------


def _settle_verdict(rec: Dict[str, Any]) -> None:
    """Set the durable verdict once the gate stops nudging (or passes)."""
    runs, blocks = rec["runs"], rec["blocks"]
    last = runs[-1] if runs else None
    if last and last.get("ok") and not blocks:
        rec["verdict"] = "verified"
        rec["detail"] = f"exit 0 · {last.get('cmd', '')[:80]}"
    elif blocks:
        rec["verdict"] = "flagged"
        tail = f" · later check passed" if (last and last.get("ok")) else ""
        rec["detail"] = f"honesty guard x{len(blocks)}{tail}"
    elif last and not last.get("ok"):
        rec["verdict"] = "failed"
        rec["detail"] = f"exit {last.get('exit')} · {last.get('cmd', '')[:80]}"
    else:
        rec["verdict"] = "missing"
        rec["detail"] = f"{len(rec['files'])} file(s) changed · no test/check run"
    rec["at"] = time.time()
    _persist()


def _pre_verify(session_id: str = "", coding: bool = False, attempt: int = 0,
                final_response: str = "", changed_paths: Optional[list] = None,
                **_kw: Any) -> Optional[Dict[str, str]]:
    try:
        if not _enabled("gate"):
            return None
        with _LOCK:
            rec = _rec(session_id)
            for path in changed_paths or []:
                if path and path not in rec["files"]:
                    rec["files"].append(str(path))
            runs, blocks = rec["runs"], rec["blocks"]
            last = runs[-1] if runs else None

            missing_artifacts = [
                p for p in (changed_paths or [])
                if isinstance(p, str) and os.path.isabs(p) and not os.path.exists(p)
            ]

            if last and last.get("ok") and not missing_artifacts:
                _settle_verdict(rec)
                return None  # verified (or flagged if it cheated) — let the turn finish

            if attempt >= _MAX_VERIFY_NUDGES:
                _settle_verdict(rec)
                return None  # out of nudges — record the verdict and finish

            rec["nudges"] += 1
            n = len(rec["files"])
            if blocks:
                why = f"the honesty guard already blocked {len(blocks)} attempt(s) to weaken the test suite"
            elif last and not last.get("ok"):
                why = f"your last check `{last.get('cmd', '')[:60]}` exited {last.get('exit')}"
            elif missing_artifacts:
                why = f"declared artifact(s) missing on disk: {', '.join(missing_artifacts[:3])}"
            else:
                why = f"you changed {n} file(s) but no test/check command has run this session"
            message = (
                f"[Hermes Day evidence gate] Completion refused: {why}. Run the project's "
                "test suite or a deterministic check (pytest/jest/tsc/ruff/…) and get a real "
                "exit-0 before marking this done. Claimed success without evidence will be "
                "flagged in the cockpit."
            )
            return {"action": "continue", "message": message}
    except Exception:
        return None


def _on_session_finalize(session_id: str = "", **_kw: Any) -> None:
    try:
        with _LOCK:
            rec = _SESSIONS.get(session_id)
            if rec is not None and rec["verdict"] is None and (rec["files"] or rec["runs"] or rec["blocks"]):
                _settle_verdict(rec)
    except Exception:
        return


# ---------------------------------------------------------------------------
# /day-evidence — desktop polling surface
# ---------------------------------------------------------------------------


def _public_rec(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "verdict": rec.get("verdict"),
        "detail": rec.get("detail") or "",
        "at": rec.get("at") or 0,
        "blocks": len(rec.get("blocks") or []),
        "runs": len(rec.get("runs") or []),
        "files": (rec.get("files") or [])[-8:],
        "nudges": rec.get("nudges") or 0,
        "run_list": [{"cmd": r.get("cmd"), "exit": r.get("exit"), "ok": r.get("ok"),
                      "ts": r.get("ts")} for r in (rec.get("runs") or [])[-8:]],
        "block_list": [{"reason": b.get("reason"), "rule": b.get("rule"),
                        "tool": b.get("tool"), "call": b.get("call") or "",
                        "intervention": b.get("intervention") or "",
                        "ts": b.get("ts")} for b in (rec.get("blocks") or [])[-10:]],
        "snaps": [{"n": s.get("n"), "sha": s.get("sha"), "label": s.get("label"),
                   "root": s.get("root"), "ts": s.get("ts")}
                  for s in (rec.get("snaps") or [])],
    }


def _cmd_evidence(arg: str = "") -> str:
    with _LOCK:
        sessions = {sid: _public_rec(r) for sid, r in _SESSIONS.items()
                    if r.get("verdict") or r.get("runs") or r.get("blocks")}
    sid = (arg or "").strip()
    if sid:
        return json.dumps({"ok": True, "sessions": {sid: sessions.get(sid)}})
    return json.dumps({"ok": True, "sessions": sessions})


def _find_rec(sid: str) -> Optional[Dict[str, Any]]:
    """Look a session record up by exact id or by stored-key suffix match."""
    with _LOCK:
        if sid in _SESSIONS:
            return _SESSIONS[sid]
        for k, rec in _SESSIONS.items():
            if sid and (k == sid or k.endswith(sid) or sid in k):
                return rec
    return None


def _cmd_rollback(arg: str = "") -> str:
    """``<session_key> <sha>`` — restore the worktree to a turn snapshot."""
    parts = (arg or "").split()
    if len(parts) < 2:
        return json.dumps({"ok": False, "error": "usage: day-rollback <session_key> <sha>"})
    sid, sha = parts[0], parts[1]
    rec = _find_rec(sid)
    if not rec:
        return json.dumps({"ok": False, "error": f"no evidence record for {sid}"})
    snap = next((s for s in rec.get("snaps") or [] if s.get("sha") == sha), None)
    if not snap:
        return json.dumps({"ok": False, "error": f"snapshot {sha[:10]} not found for {sid}"})
    root = snap.get("root") or rec.get("_root")
    try:
        # tracked + staged content restored; files created after the snap stay
        out = _git(root, "restore", f"--source={sha}", "--worktree", "--staged", "--",
                   ":/", timeout=30)
        if out.returncode != 0:
            return json.dumps({"ok": False, "error": out.stderr.strip()[:300]})
        return json.dumps({
            "ok": True, "sha": sha, "n": snap.get("n"), "root": root,
            "note": "tracked files restored to the snapshot; files created after "
                    "it remain (git clean them if unwanted)"})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)[:200]})


# ---------------------------------------------------------------------------
# /day-impact — blast-radius scan over touched files
# ---------------------------------------------------------------------------

_PY_DEF_RE = re.compile(r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_]\w*)", re.M)
_JS_DEF_RE = re.compile(
    r"(?:export\s+)?(?:async\s+)?(?:function\*?|class)\s+([A-Za-z_$][\w$]*)|"
    r"(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?"
    r"(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>",
    re.M,
)
_IMPORT_RE = {
    "py": re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.M),
    "js": re.compile(r"(?:import|require)\s*\(?\s*['\"]([^'\"]+)['\"]", re.M),
}
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist",
              "build", ".next", "target", "vendor", ".tox", "coverage"}


def _symbols_of(path: str) -> list:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(400_000)
    except OSError:
        return []
    if path.endswith(".py"):
        return list(dict.fromkeys(_PY_DEF_RE.findall(text)))[:24]
    if re.search(r"\.(jsx?|tsx?|mjs|cjs)$", path):
        flat = []
        for a, b in _JS_DEF_RE.findall(text):
            flat.append(a or b)
        return list(dict.fromkeys(flat))[:24]
    return []


def _module_tokens(path: str, root: str) -> list:
    """Import-addressable tokens for a file: dotted module + basename stems."""
    rel = os.path.relpath(path, root)
    stem, _ext = os.path.splitext(rel)
    toks = []
    if path.endswith(".py"):
        dotted = stem.replace(os.sep, ".").replace(".__init__", "")
        toks.append(dotted)
        toks.append(os.path.basename(stem))
    else:
        base = os.path.basename(stem)
        toks.append(stem)        # ./src/foo/bar import style
        toks.append(base)
    return [t for t in toks if t and t not in ("__init__", "index")]


def _tracked_files(root: str) -> list:
    try:
        out = _git(root, "ls-files", timeout=15)
        if out.returncode == 0:
            return [os.path.join(root, l) for l in out.stdout.splitlines() if l.strip()]
    except Exception:
        pass
    out = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            out.append(os.path.join(dirpath, name))
        if len(out) > 20000:
            break
    return out


def _is_test_file(path: str) -> bool:
    return bool(_TEST_PATH_RE.search(path))


def _test_cover_for(dep_path: str, root: str, tests: list) -> bool:
    """Does any test file plausibly exercise this dependent? Name + import heuristics."""
    stem = os.path.splitext(os.path.basename(dep_path))[0]
    for t in tests:
        tb = os.path.basename(t)
        if stem and stem in tb:
            return True
        try:
            with open(t, encoding="utf-8", errors="replace") as fh:
                if stem and re.search(r"\b" + re.escape(stem) + r"\b", fh.read(300_000)):
                    return True
        except OSError:
            continue
    return False


def _impact_scan(rec: Dict[str, Any]) -> Dict[str, Any]:
    files = [f for f in (rec.get("files") or []) if isinstance(f, str)]
    roots = {}
    for f in files:
        root = _repo_root(f)
        if root:
            roots.setdefault(root, []).append(f)
    if not roots:
        root = rec.get("_root")
        if root:
            roots[root] = []
    out_roots = []
    for root, touched in roots.items():
        tracked = _tracked_files(root)
        tests = [t for t in tracked if _is_test_file(t)]
        touched_rows = []
        dependents: Dict[str, set] = {}
        for f in touched:
            symbols = _symbols_of(f)
            touched_rows.append({"path": os.path.relpath(f, root), "symbols": symbols})
            tokens = _module_tokens(f, root) + symbols[:12]
            for other in tracked:
                if other == f or not re.search(r"\.(py|jsx?|tsx?|mjs|cjs)$", other):
                    continue
                try:
                    with open(other, encoding="utf-8", errors="replace") as fh:
                        body = fh.read(400_000)
                except OSError:
                    continue
                hits = {tok for tok in tokens
                        if len(tok) >= 3 and re.search(r"\b" + re.escape(tok) + r"\b", body)}
                if hits:
                    dependents.setdefault(other, set()).update(hits)
        dep_rows = [{
            "path": os.path.relpath(p, root),
            "calls": sorted(c for c in calls if not os.sep in c)[:10],
            "is_test": _is_test_file(p),
            "covered": _is_test_file(p) or _test_cover_for(p, root, tests),
        } for p, calls in sorted(dependents.items())]
        out_roots.append({"root": root, "touched": touched_rows, "dependents": dep_rows})
    return {"roots": out_roots}


def _cmd_impact(arg: str = "") -> str:
    sid = (arg or "").strip()
    if not sid:
        return json.dumps({"ok": False, "error": "usage: day-impact <session_key>"})
    rec = _find_rec(sid)
    if not rec:
        return json.dumps({"ok": False, "error": f"no evidence record for {sid}"})
    try:
        result = _impact_scan(rec)
        return json.dumps({"ok": True, **result})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)[:200]})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _enabled(section: str) -> bool:
    if _CTX is None:
        return True
    try:
        cfg = _CTX.get_config("gate")
        if isinstance(cfg, dict) and section in cfg:
            return bool(cfg[section])
    except Exception:
        pass
    return True


def register(ctx: Any) -> None:
    global _CTX
    _CTX = ctx
    _load_persisted()
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("pre_verify", _pre_verify)
    ctx.register_hook("on_session_finalize", _on_session_finalize)
    ctx.register_command(
        "day-evidence", _cmd_evidence,
        description="Evidence-gate verdicts as JSON (arg: optional session key).",
        args_hint="[session_key]",
    )
    ctx.register_command(
        "day-rollback", _cmd_rollback,
        description="Restore the worktree to a Hermes Day turn snapshot.",
        args_hint="<session_key> <sha>",
    )
    ctx.register_command(
        "day-impact", _cmd_impact,
        description="Blast-radius scan: touched symbols, dependents, coverage.",
        args_hint="<session_key>",
    )
