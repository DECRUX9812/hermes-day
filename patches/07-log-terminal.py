"""Lane 07 — Live Log Terminal (``/day-logs``).

Design: a BYTE-OFFSET cursor per log path. Every poll reads only the bytes
appended since the previous poll — hard-capped at ``_MAX_BYTES_PER_POLL``
bytes (and ``_MAX_LINES_PER_POLL`` lines) per page — and the cursor is
persisted under this module's own ``threading.RLock`` (never the plugin's
``_LOCK``).

A missing or unreadable log yields an EMPTY state with a machine-readable
``reason``: never an exception, never invented lines. Every line this command
returns comes from a real ``open()``/``read()`` of the file.

Command: ``/day-logs <path> [tail|reset]``
Also accepts a structured arg from the desktop panel:
``{"path": "...", "reset": true}``

Response (JSON): the full envelope the panel renders —
``ok, path, lines[{seq,text,level,partial}], empty, reason, offset, size,
bytes_read, pending, truncated, omitted_before, dropped, rotated, partial,
first_seq, last_seq, ts``.
"""

import json
import os
import re
import shlex
import threading
import time
import traceback

# Own module-level lock for this lane (never import the plugin _LOCK).
LOG_LOCK = threading.RLock()

_CURSOR_KEY = "log_cursors"
# Hard caps: bytes read (and therefore returned) per poll, lines per poll,
# and persisted cursors (typing a path char-by-char must not grow state).
_MAX_BYTES_PER_POLL = 65536
_MAX_LINES_PER_POLL = 1000
_MAX_CURSORS = 64
_USAGE = ('usage: day-logs <path> [tail|reset]   '
          '(or {"path": "...", "reset": true})')

_ERR_RE = re.compile(r"\b(ERROR|FATAL|CRITICAL|PANIC)\b", re.IGNORECASE)
_WARN_RE = re.compile(r"\bWARN(ING)?\b", re.IGNORECASE)

_CTX = None
# path -> {"offset", "next_seq", "at", "ino", "truncated", "omitted_before"}
_CURSORS = {}


def _level(text):
    """Classify a line for the UI. Derived metadata only — text is verbatim."""
    try:
        if _ERR_RE.search(text):
            return "error"
        if _WARN_RE.search(text):
            return "warn"
        return "info"
    except Exception:
        return "info"


def _persist():
    """Best-effort cursor write-through. Caller holds LOG_LOCK."""
    try:
        if _CTX is not None:
            _CTX.state.set(_CURSOR_KEY, dict(_CURSORS))
    except Exception:
        pass


def _evict_locked():
    """Cap the cursor map. Caller holds LOG_LOCK."""
    try:
        while len(_CURSORS) > _MAX_CURSORS:
            oldest = None
            for k, v in _CURSORS.items():
                if oldest is None or v.get("at", 0) < _CURSORS[oldest].get("at", 0):
                    oldest = k
            if oldest is None:
                break
            _CURSORS.pop(oldest, None)
    except Exception:
        pass


def _snapshot(path):
    with LOG_LOCK:
        cur = _CURSORS.get(path)
        return dict(cur) if isinstance(cur, dict) else {}


def _store_cursor(path, offset, next_seq, ino, truncated, omitted_before):
    with LOG_LOCK:
        _CURSORS[path] = {
            "offset": int(offset),
            "next_seq": int(next_seq),
            "at": time.time(),
            "ino": int(ino) if ino else 0,
            "truncated": bool(truncated),
            "omitted_before": int(omitted_before) if omitted_before else 0,
        }
        _evict_locked()
        _persist()


def _empty(path, reason, cur=None, size=None):
    """Uniform empty state: ok + reason, zero lines, real cursor/size facts."""
    cur = cur if isinstance(cur, dict) else {}
    return {
        "ok": True,
        "path": path,
        "lines": [],
        "empty": True,
        "reason": reason,
        "offset": int(cur.get("offset", 0) or 0),
        "size": size,
        "bytes_read": 0,
        "first_seq": None,
        "last_seq": None,
        "dropped": 0,
        "truncated": bool(cur.get("truncated", False)),
        "omitted_before": int(cur.get("omitted_before", 0) or 0),
        "pending": False,
        "rotated": False,
        "partial": False,
        "ts": time.time(),
    }


def _parse_arg(arg):
    """Return {"path", "reset"} or a usage/error STRING (never raises)."""
    raw = (arg or "").strip() if isinstance(arg, str) else ""
    if not raw:
        return _USAGE
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except Exception as exc:
            return "invalid JSON argument: %s: %s" % (type(exc).__name__, exc)
        if not isinstance(obj, dict):
            return "JSON argument must be an object — %s" % _USAGE
        path = str(obj.get("path") or "").strip()
        if not path:
            return _USAGE
        return {"path": path, "reset": bool(obj.get("reset") or obj.get("tail"))}
    try:
        toks = shlex.split(raw)
    except ValueError as exc:
        return "cannot parse argument: %s" % exc
    if not toks:
        return _USAGE
    reset = False
    for tok in toks[1:]:
        if tok.lower() in ("tail", "reset"):
            reset = True
        else:
            return "unknown option: %s — %s" % (tok, _USAGE)
    return {"path": toks[0], "reset": reset}


def _poll(path, reset=False):
    """Read only the bytes appended since the cursor. Never raises."""
    try:
        path = os.path.abspath(os.path.expanduser(path))
    except Exception as exc:
        return _empty(str(path), "cannot resolve log path (%s: %s)"
                      % (type(exc).__name__, exc))

    cur = {} if reset else _snapshot(path)

    # --- stat: missing / unreadable -> empty state with a reason -----------
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return _empty(path, "log file does not exist", cur)
    except PermissionError:
        return _empty(path, "log file is not readable (PermissionError)", cur)
    except OSError as exc:
        return _empty(path, "cannot stat log file (%s: %s)"
                      % (type(exc).__name__, exc), cur)
    except Exception as exc:
        return _empty(path, "cannot open log path (%s: %s)"
                      % (type(exc).__name__, exc), cur)
    if os.path.isdir(path):
        return _empty(path, "log path is a directory, not a file", cur,
                      size=int(st.st_size))

    size = int(st.st_size)
    ino = int(getattr(st, "st_ino", 0) or 0)
    tail_start = max(0, size - _MAX_BYTES_PER_POLL)

    rotated = False
    if not cur:
        start = tail_start
    elif cur.get("ino") and int(cur.get("ino")) != ino:
        # same path, new file (logrotate replace) — restart at the new tail
        rotated = True
        start = tail_start
    elif int(cur.get("offset", 0) or 0) > size:
        # truncated in place (logrotate copytruncate)
        rotated = True
        start = tail_start
    else:
        start = int(cur.get("offset", 0) or 0)

    truncated = start > 0
    to_read = min(size - start, _MAX_BYTES_PER_POLL)
    if to_read <= 0:
        if size == 0:
            return _empty(path, "log file is empty", cur, size=size)
        return _empty(path, "no new bytes since offset %d" % start, cur,
                      size=size)

    # --- read: hard byte cap ----------------------------------------------
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            chunk = fh.read(to_read)
    except PermissionError:
        return _empty(path, "log file is not readable (PermissionError)", cur,
                      size=size)
    except OSError as exc:
        return _empty(path, "cannot read log file (%s: %s)"
                      % (type(exc).__name__, exc), cur, size=size)
    except Exception as exc:
        return _empty(path, "cannot read log file (%s: %s)"
                      % (type(exc).__name__, exc), cur, size=size)

    if not chunk:
        return _empty(path, "no new bytes since offset %d" % start, cur,
                      size=size)

    # --- split on complete lines only (never a half line, except at the
    #     byte cap where a single line outlives the whole page) -------------
    last_nl = chunk.rfind(b"\n")
    partial_tail = False
    if last_nl >= 0:
        raw = chunk[: last_nl + 1]
        new_offset = start + len(raw)
    else:
        raw = chunk
        new_offset = start + len(chunk)
        partial_tail = True

    try:
        text = raw.decode("utf-8", "replace")
    except Exception:
        text = raw.decode("utf-8", "ignore")
    parts = text.split("\n")
    if parts and parts[-1] == "":
        parts.pop()

    dropped = 0
    if len(parts) > _MAX_LINES_PER_POLL:
        dropped = len(parts) - _MAX_LINES_PER_POLL
        parts = parts[-_MAX_LINES_PER_POLL:]

    with LOG_LOCK:
        first_seq = int(cur.get("next_seq", 1) or 1)

    lines = []
    for i, t in enumerate(parts):
        lines.append({"seq": first_seq + i, "text": t, "level": _level(t)})
    if partial_tail and lines:
        lines[-1]["partial"] = True

    next_seq = first_seq + len(lines)
    _store_cursor(path, new_offset, next_seq, ino, truncated,
                  start if truncated else 0)

    return {
        "ok": True,
        "path": path,
        "lines": lines,
        "first_seq": lines[0]["seq"] if lines else None,
        "last_seq": lines[-1]["seq"] if lines else None,
        "empty": not lines,
        "reason": None if lines else "no complete lines in this page",
        "offset": int(new_offset),
        "size": size,
        "bytes_read": len(chunk),
        "pending": size > new_offset,
        "truncated": bool(truncated),
        "omitted_before": int(start) if truncated else 0,
        "dropped": int(dropped),
        "rotated": bool(rotated),
        "partial": bool(partial_tail),
        "ts": time.time(),
    }


def cmd_day_logs(arg: str = "") -> str:
    """/day-logs — byte-offset poll of a log file. Returns JSON; never raises."""
    try:
        spec = _parse_arg(arg)
        if isinstance(spec, str):
            return json.dumps({
                "ok": False, "lines": [], "empty": True,
                "reason": spec, "error": spec,
            })
        return json.dumps(_poll(spec["path"], bool(spec.get("reset"))))
    except Exception:
        return json.dumps({
            "ok": False, "lines": [], "empty": True,
            "reason": "internal error (see error)",
            "error": "day-logs failed: %s" % traceback.format_exc(limit=4),
        })


def register(ctx) -> None:
    """Wire /day-logs only — commands, never hooks. Never raises."""
    global _CTX
    try:
        _CTX = ctx
        try:
            stored = ctx.state.get(_CURSOR_KEY)
        except Exception:
            stored = None
        if isinstance(stored, dict):
            with LOG_LOCK:
                for key, val in stored.items():
                    if not isinstance(val, dict):
                        continue
                    try:
                        _CURSORS[str(key)] = {
                            "offset": int(val.get("offset", 0) or 0),
                            "next_seq": int(val.get("next_seq", 1) or 1),
                            "at": float(val.get("at", 0) or 0),
                            "ino": int(val.get("ino", 0) or 0),
                            "truncated": bool(val.get("truncated", False)),
                            "omitted_before": int(val.get("omitted_before", 0) or 0),
                        }
                    except Exception:
                        continue
                _evict_locked()
        ctx.register_command(
            "day-logs", cmd_day_logs,
            description="Live log terminal: byte-offset poll of a log file (JSON).",
            args_hint="<path> [tail|reset]",
        )
    except Exception:
        traceback.print_exc()
