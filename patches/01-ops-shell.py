"""Lane 01 - Ops Shell: ``/day-exec`` command surface (splice-ready patch).

Registers exactly ONE command, ``day-exec``, via ``register(ctx) -> None``.
No hooks: the plugin's only fail-closed gate stays ``pre_tool_call``
(BRIEF constraint 4), so nothing here can wedge the tool loop.

JSON request/response shapes follow ``lanes/01-ops-shell.md`` section 3:
  3.1 request ``arg``  - {op, session, host, cmd, dry, cwd, q, limit}
                         CLI fallback: [-n|--dry] [--last] [--failed]
                         [--history] [<session_key>] -- <command...>
  3.2 successful run   - {"ok": true, "run": {...}}
  3.3 dry-run          - dry/executed/exit_code/ms as specified, print only
  3.4 guard blocked    - {"ok": false, "error": "guard_blocked", reason, rule, run}
  3.5 history          - {"ok": true, "runs": [...], count, limit, q}
  3.6 rerun-last/-failed - server-side resolution under lock, then 3.2 shape
                         with ``replayed_from`` on the run.

Safety invariants:
* dry-run NEVER spawns a subprocess (section 3.3).
* every command passes the plugin's REAL honesty guard (``_command_violation``)
  BEFORE any spawn (sections 1.5 / 3.7); if the guard cannot be located or
  raises, the command is BLOCKED (fail-closed) - a cockpit that executes
  unchecked is the exact failure this plugin exists to prevent.
* guard-blocked runs are still appended to the ledger (auditable record of
  what the cockpit refused, section 3.4).
* execution timeout clamped to 8 s, under RPC_TIMEOUT (9000 ms, plugin.js:76).
* every compound read-modify-write of the run ledger happens under THIS
  module's own ``threading.RLock`` (the shared ``_LOCK`` in ``__init__`` is
  not importable from a patch module, per lane contract).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

# Own module-level lock (do NOT import _LOCK from the plugin package).
_LOCK = threading.RLock()

_EXEC_KEY = "day_exec_runs"     # ctx.state key (mirrors the _INDEX_KEY precedent)
_EXEC_MAX = 200                 # runbook cap (mirrors _MAX_SESSIONS = 60)
_EXEC_TIMEOUT_S = 8             # must stay < RPC_TIMEOUT (9000 ms)
_EXEC_TAIL = 8000               # chars kept per stream (tail clip)

_EXEC_RUNS = []                 # flat list of run dicts, newest last
_EXEC_SEQ = 0                   # id counter -> "x_000001"
_CTX = None

_ARGS_HINT = "[-n|--dry] [--last] [--failed] [--history] [<session_key>] -- <command>"
_USAGE = {"ok": False, "error": "usage", "args_hint": _ARGS_HINT}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _json(obj):
    """Serialize a response dict to the JSON string every path returns."""
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return json.dumps({"ok": False, "error": "serialize_failed"})


def _now_iso():
    try:
        return datetime.now(timezone.utc).isoformat()
    except Exception:
        return ""


def _next_id():
    global _EXEC_SEQ
    with _LOCK:
        _EXEC_SEQ += 1
        return "x_%06d" % _EXEC_SEQ


def _persist_exec():
    if _CTX is None:
        return
    try:
        _CTX.state.set(_EXEC_KEY, list(_EXEC_RUNS))
    except Exception:
        pass


def _hydrate_exec():
    """Load the runbook from ctx.state on register (same shape, same key)."""
    global _EXEC_RUNS, _EXEC_SEQ
    if _CTX is None:
        return
    stored = None
    try:
        state = getattr(_CTX, "state", None)
        if state is None:
            return
        getter = getattr(state, "get", None)
        if callable(getter):
            try:
                stored = getter(_EXEC_KEY)
            except TypeError:
                stored = getter(_EXEC_KEY, None)
        elif isinstance(state, dict):
            stored = state.get(_EXEC_KEY)
    except Exception:
        stored = None
    if not isinstance(stored, list):
        return
    rows = [r for r in stored if isinstance(r, dict)][-_EXEC_MAX:]
    max_seq = 0
    for row in rows:
        match = re.match(r"^x_(\d+)$", str(row.get("id") or ""))
        if match:
            try:
                max_seq = max(max_seq, int(match.group(1)))
            except Exception:
                pass
    with _LOCK:
        _EXEC_RUNS[:] = rows
        if max_seq > _EXEC_SEQ:
            _EXEC_SEQ = max_seq


def _record(run):
    """Append one run to the ledger, cap it, persist - all under _LOCK."""
    with _LOCK:
        _EXEC_RUNS.append(run)
        overflow = len(_EXEC_RUNS) - _EXEC_MAX
        if overflow > 0:
            del _EXEC_RUNS[:overflow]
        _persist_exec()


def _as_text(data):
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return str(data)


def _clip(text):
    """Tail-clip a stream to _EXEC_TAIL; report whether it was truncated."""
    text = _as_text(text)
    return text[-_EXEC_TAIL:], len(text) > _EXEC_TAIL


# --------------------------------------------------------------------------
# honesty guard (spec 1.5 / 3.4 / 3.7)
# --------------------------------------------------------------------------

def _guard_violation(cmd):
    """Run `cmd` through the plugin's REAL ``_command_violation`` predicate.

    Returns ``(reason, rule)`` when the command must be BLOCKED, else
    ``(None, None)``. Fail-closed: when the guard cannot be located or it
    raises, the command is blocked rather than executed unchecked.
    """
    fn = None
    try:
        root_name = (__package__ or "").split(".")[0]
        root = sys.modules.get(root_name) if root_name else None
        fn = getattr(root, "_command_violation", None)
    except Exception:
        fn = None
    if not callable(fn):
        try:
            for mod in list(sys.modules.values()):
                cand = getattr(mod, "_command_violation", None)
                if callable(cand):
                    fn = cand
                    break
        except Exception:
            fn = None
    if not callable(fn):
        return ("honesty guard unavailable in this plugin load", "guard_unavailable")
    try:
        violation = fn(cmd)
    except Exception as exc:
        return ("honesty guard raised: %s" % exc, "guard_error")
    if not violation:
        return (None, None)
    try:
        reason, rule = violation[0], violation[1]
    except Exception:
        return ("honesty guard returned an unreadable violation", "guard_unknown")
    return (str(reason), str(rule))


# --------------------------------------------------------------------------
# run records
# --------------------------------------------------------------------------

def _base_run(op, cmd, session, host, cwd, dry, replayed_from=None):
    run = {
        "id": _next_id(),
        "op": op,
        "cmd": cmd,
        "session": session,
        "host": host,
        "cwd": cwd,
        "dry": bool(dry),
        "blocked": False,
        "executed": False,
        "exit_code": None,
        "timed_out": False,
        "ms": 0,
        "stdout": "",
        "stderr": "",
        "truncated": False,
        "at": _now_iso(),
    }
    if replayed_from:
        run["replayed_from"] = replayed_from
    return run


def _execute(req, op, replayed_from=None):
    """Guard -> dry-run echo -> real run. Returns the response dict (3.2-3.4)."""
    cmd = str(req.get("cmd") or "")
    session = str(req.get("session") or "")
    host = str(req.get("host") or "local") or "local"
    dry = bool(req.get("dry"))
    cwd = str(req.get("cwd") or "") or os.getcwd()

    # 1) honesty guard ALWAYS comes before any spawn (spec 1.5 / 3.7).
    reason, rule = _guard_violation(cmd)
    if reason is not None:
        run = _base_run(op, cmd, session, host, cwd, dry, replayed_from)
        run["blocked"] = True
        run["executed"] = False
        run["exit_code"] = None
        run["reason"] = reason
        run["rule"] = rule
        _record(run)
        return {
            "ok": False,
            "error": "guard_blocked",
            "reason": reason,
            "rule": rule,
            "run": run,
        }

    # 2) dry-run: print only - NO subprocess is ever spawned (spec 3.3).
    if dry:
        note = "" if os.path.isdir(cwd) else " [cwd missing]"
        stdout = (
            "$ " + cmd
            + "\n(dry-run: not executed — target host=" + host
            + " cwd=" + cwd + note + ")"
        )
        run = _base_run(op, cmd, session, host, cwd, True, replayed_from)
        run["dry"] = True
        run["executed"] = False
        run["exit_code"] = None
        run["timed_out"] = False
        run["ms"] = 0
        run["stdout"] = stdout
        run["stderr"] = ""
        run["truncated"] = False
        _record(run)
        return {"ok": True, "run": run}

    # 3) real run (spec 3.7)
    if not os.path.isdir(cwd):
        return {"ok": False, "error": "bad_cwd", "cwd": cwd}
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            executable="/bin/bash",
            capture_output=True,
            text=True,
            timeout=_EXEC_TIMEOUT_S,
            cwd=cwd,
        )
        code = int(proc.returncode)
        if code < 0:
            # signalled child: the shell convention 128+N keeps exit_code >= 0
            # (spec 3.2) without claiming a success that did not happen.
            code = 128 + abs(code)
        exit_code = code
        out_text = proc.stdout or ""
        err_text = proc.stderr or ""
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        timed_out = True
        out_text = _as_text(getattr(exc, "stdout", None))
        err_text = _as_text(getattr(exc, "stderr", None))
    except OSError as exc:
        return {"ok": False, "error": "exec_failed", "detail": str(exc)}
    ms = int(round((time.time() - started) * 1000))
    out_tail, out_trunc = _clip(out_text)
    err_tail, err_trunc = _clip(err_text)
    run = _base_run(op, cmd, session, host, cwd, False, replayed_from)
    run["executed"] = True
    run["exit_code"] = exit_code
    run["timed_out"] = bool(timed_out)
    run["ms"] = ms
    run["stdout"] = out_tail
    run["stderr"] = err_tail
    run["truncated"] = bool(out_trunc or err_trunc)
    _record(run)
    return {"ok": True, "run": run}


# --------------------------------------------------------------------------
# history + re-runs (spec 3.5 / 3.6)
# --------------------------------------------------------------------------

def _op_history(req):
    raw_limit = req.get("limit")
    try:
        limit = 50 if raw_limit is None else int(raw_limit)
    except Exception:
        limit = 50
    if limit < 0:
        limit = 0
    if limit > _EXEC_MAX:
        limit = _EXEC_MAX
    q = str(req.get("q") or "")
    session = str(req.get("session") or "")
    with _LOCK:
        rows = list(_EXEC_RUNS)
    if session:
        rows = [r for r in rows if str(r.get("session") or "") == session]
    if q:
        needle = q.lower()

        def _hit(row):
            hay = (
                str(row.get("cmd") or "")
                + "\n" + str(row.get("stdout") or "")
                + "\n" + str(row.get("stderr") or "")
            ).lower()
            return needle in hay

        rows = [r for r in rows if _hit(r)]
    rows = list(reversed(rows))          # newest-first
    count = len(rows)
    return {"ok": True, "runs": rows[:limit], "count": count, "limit": limit, "q": q}


def _resolve_rerun(op, session):
    """Resolve the source run server-side, under _LOCK (spec 3.6)."""
    with _LOCK:
        rows = list(_EXEC_RUNS)
    if session:
        rows = [r for r in rows if str(r.get("session") or "") == session]
    if op == "rerun-last":
        cands = [r for r in rows if not r.get("dry")]
        if not cands:
            return None, "no_prior_run"
        return cands[-1], None

    def _failed(row):
        if row.get("dry") or row.get("blocked"):
            return False
        code = row.get("exit_code")
        if code is None:
            return False
        try:
            return int(code) != 0
        except Exception:
            return False

    cands = [r for r in rows if _failed(r)]
    if not cands:
        return None, "no_failed_run"
    return cands[-1], None


# --------------------------------------------------------------------------
# request parsing (spec 3.1)
# --------------------------------------------------------------------------

def _parse_arg(arg):
    """Normalize the dispatch `arg` (JSON object or human CLI) -> request dict.

    Returns {"_usage": True} when the request does not parse (spec 3.1).
    """
    if arg is None:
        arg = ""
    if not isinstance(arg, str):
        arg = str(arg)
    text = arg.strip()

    if text.startswith("{"):
        try:
            req = json.loads(text)
        except Exception:
            return {"_usage": True}
        if not isinstance(req, dict):
            return {"_usage": True}
        op = str(req.get("op") or "").strip()
        cmd = str(req.get("cmd") or "")
        if not op:
            op = "run" if cmd.strip() else "history"
        if op == "run" and not cmd.strip():
            return {"_usage": True}
        return {
            "op": op,
            "session": str(req.get("session") or ""),
            "host": str(req.get("host") or "local") or "local",
            "cmd": cmd,
            "dry": bool(req.get("dry")),
            "cwd": str(req.get("cwd") or ""),
            "q": str(req.get("q") or ""),
            "limit": req.get("limit"),
        }

    # ---- human CLI fallback: [-n|--dry] [--last] [--failed] [--history]
    #      [<session_key>] -- <command...> ----
    parts = re.split(r"(?:(?<=\s)|^)--(?=\s|$)", text, maxsplit=1)
    head = parts[0].strip()
    raw_cmd = parts[1].strip() if len(parts) > 1 else None
    try:
        tokens = shlex.split(head)
    except ValueError:
        return {"_usage": True}

    dry = False
    op = None
    pre = []
    for tok in tokens:
        if tok in ("-n", "--dry"):
            dry = True
        elif tok == "--last":
            op = "rerun-last"
        elif tok == "--failed":
            op = "rerun-failed"
        elif tok == "--history":
            op = "history"
        elif tok.startswith("-") and tok != "-":
            return {"_usage": True}      # unknown flag
        else:
            pre.append(tok)

    if raw_cmd is not None:
        if op is not None:
            return {"_usage": True}      # contradictory: mode flag + "-- cmd"
        if not raw_cmd:
            return {"_usage": True}
        if len(pre) > 1:
            return {"_usage": True}
        return {
            "op": "run",
            "session": pre[0] if pre else "",
            "host": "local",
            "cmd": raw_cmd,
            "dry": dry,
            "cwd": "",
            "q": "",
            "limit": None,
        }

    if op is None:
        if pre:
            # bare text without "--" cannot be a run (spec 3.1)
            return {"_usage": True}
        op = "history"
    if len(pre) > 1:
        return {"_usage": True}
    return {
        "op": op,
        "session": pre[0] if pre else "",
        "host": "local",
        "cmd": "",
        "dry": dry,
        "cwd": "",
        "q": "",
        "limit": None,
    }


# --------------------------------------------------------------------------
# the command
# --------------------------------------------------------------------------

def _cmd_exec(arg: str = "") -> str:
    """/day-exec -> JSON string on every path (same contract as day-evidence)."""
    try:
        req = _parse_arg(arg)
        if req.get("_usage"):
            return _json(dict(_USAGE))
        op = str(req.get("op") or "history")

        if op == "history":
            return _json(_op_history(req))

        if op in ("rerun-last", "rerun-failed"):
            src, err = _resolve_rerun(op, str(req.get("session") or ""))
            if err:
                return _json({"ok": False, "error": err})
            merged = dict(req)
            merged["cmd"] = str(src.get("cmd") or "")
            if not merged.get("session"):
                merged["session"] = str(src.get("session") or "")
            if not merged.get("cwd"):
                merged["cwd"] = str(src.get("cwd") or "")
            return _json(
                _execute(merged, op, replayed_from=str(src.get("id") or ""))
            )

        if op == "run":
            if not str(req.get("cmd") or "").strip():
                return _json(dict(_USAGE))
            return _json(_execute(req, "run"))

        return _json(
            {"ok": False, "error": "unknown_op", "op": op, "args_hint": _ARGS_HINT}
        )
    except Exception as exc:
        # a bug degrades to an error payload, never a raised exception
        return _json({"ok": False, "error": "exec_error", "detail": str(exc)})


def register(ctx) -> None:
    """Plugin-load entrypoint: register the ``day-exec`` command. No hooks."""
    global _CTX
    try:
        _CTX = ctx
        _hydrate_exec()
    except Exception:
        pass
    try:
        ctx.register_command(
            "day-exec", _cmd_exec,
            description="Run/dry-run a shell command for a session; runbook history as JSON.",
            args_hint="[-n|--dry] [--last] [--failed] [--history] [<session_key>] -- <command>",
        )
    except Exception:
        pass
