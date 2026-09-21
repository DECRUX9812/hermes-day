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
- ``/day-evidence`` — plugin command returning the JSON verdict map the
  desktop half polls via ``command.dispatch`` for its Evidence badges.

State: per-session records in ``ctx.state`` under the ``sessions`` key
(capped, trimmed), plus an in-memory mirror for hot-path updates.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Dict, Optional

_STATE_KEY = "sessions"
_MAX_SESSIONS = 60
_MAX_RUNS = 20
_MAX_BLOCKS = 20
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
    ),
    (
        re.compile(r"\bgit\s+(?:rm|clean|restore|checkout|reset)\b[^;&|\n]*" + _TEST_TOKEN, re.I),
        "reverts or removes test files via git",
    ),
    (
        re.compile(r"\b(?:sed\s+-i|perl\s+-pi?|ed\s|awk\b[^;&|\n]*>\s*)\b[^;&|\n]*" + _TEST_TOKEN, re.I),
        "rewrites a test file in place",
    ),
    (
        re.compile(r"(?:^|[;&|]\s*|>\s*|>>\s*)[>]{1,2}\s*['\"]?" + _TEST_TOKEN),
        "truncates a test file",
    ),
    (
        re.compile(r"--pass[-_]?with[-_]?no[-_]?tests|--allow[-_]?empty(?:-results)?|"
                   r"--ignore[-_]?failures|--force(?:exit)?(?:\s|=|[^\w-])", re.I),
        "adds a force-pass / empty-suite flag",
    ),
    (
        re.compile(_CHECK_WORD + r"[^\n]*?(?:\|\|\s*(?:true|:|echo\b)|[;&|]\s*exit\s+0\b)", re.I),
        "launders the check's exit status",
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
            "detail": "", "at": 0.0, "nudges": 0}


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


def _command_violation(cmd: str) -> Optional[str]:
    for pattern, reason in _COMMAND_VIOLATIONS:
        if pattern.search(cmd):
            return reason
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
            return f"deletes all {a_old} assertion(s) in {path}"
        if a_old >= 2 and a_new < a_old:
            return f"weakens assertions in {path} ({a_old} -> {a_new})"
    return None


def _block(sid: str, tool_name: str, reason: str) -> Dict[str, str]:
    with _LOCK:
        rec = _rec(sid)
        _append(rec["blocks"], {"reason": reason, "tool": tool_name, "ts": time.time()},
                _MAX_BLOCKS)
        rec["verdict"] = "flagged"
        rec["detail"] = f"honesty guard x{len(rec['blocks'])}"
        rec["at"] = time.time()
        _persist()
    message = (
        f"Hermes Day honesty guard BLOCKED this call — it {reason}. Tests and checks are "
        "evidence, not decoration: fix the implementation or ask the human, do not modify, "
        "delete, or launder the test suite to force a pass."
    )
    return {"action": "block", "message": message}


def _pre_tool_call(tool_name: str = "", args: Optional[Dict[str, Any]] = None,
                   session_id: str = "", **_kw: Any) -> Optional[Dict[str, str]]:
    try:
        if not _enabled("honesty"):
            return None
        args = args if isinstance(args, dict) else {}
        if tool_name == "terminal":
            cmd = str(args.get("command") or args.get("code") or "")
            reason = _command_violation(cmd) if cmd else None
        elif tool_name in _FILE_EDIT_TOOLS:
            path = next((str(args.get(k) or "") for k in _PATH_KEYS if args.get(k)), "")
            reason = _edit_violation(path, args)
        else:
            return None
        return _block(session_id, tool_name, reason) if reason else None
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
        with _LOCK:
            rec = _rec(session_id)
            if tool_name in _FILE_EDIT_TOOLS:
                path = next((str(args.get(k) or "") for k in _PATH_KEYS if args.get(k)), "")
                if path and path not in rec["files"]:
                    rec["files"].append(path)
                    del rec["files"][: max(0, len(rec["files"]) - 40)]
                return
            if tool_name != "terminal":
                return
            cmd = str(args.get("command") or args.get("code") or "")
            if not cmd or not _CHECK_RE.search(cmd):
                return
            if isinstance(result, str) and "honesty guard BLOCKED" in result:
                return  # vetoed calls echo back as tool results — not evidence
            code = _exit_code(result, status)
            _append(rec["runs"], {"cmd": cmd[:240], "exit": code,
                                  "ok": code == 0, "ts": time.time()}, _MAX_RUNS)
            _persist()
    except Exception:
        return


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
        "block_list": [{"reason": b.get("reason"), "tool": b.get("tool"),
                        "ts": b.get("ts")} for b in (rec.get("blocks") or [])[-8:]],
    }


def _cmd_evidence(arg: str = "") -> str:
    with _LOCK:
        sessions = {sid: _public_rec(r) for sid, r in _SESSIONS.items()
                    if r.get("verdict") or r.get("runs") or r.get("blocks")}
    sid = (arg or "").strip()
    if sid:
        return json.dumps({"ok": True, "sessions": {sid: sessions.get(sid)}})
    return json.dumps({"ok": True, "sessions": sessions})


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
