"""Lane 05 — Session Surgery: fork / branch / diff / merge bookkeeping.

Loaded standalone by ``patches.register_all()`` — commands only (no hooks),
every entry point wrapped in try/except, and its OWN module-level
``threading.RLock`` (never the host plugin's ``_LOCK``).

Commands
    day-fork    <from_key> <to_key>
    day-branch  <parent_key> <child_key> <count>
    day-diff    <keyA> <keyB> [shaA] [shaB]
    day-merge   <from_key> <to_key> <via> <chars>
    day-surgery [key] | teleport <from_key> <to_key>

Gateway ground truth the desktop half drives (contracts/sessions.py), used
exactly as contracted:
    session.branch   fork-with-history; ``count`` truncates ``history[:count]``
                     → count is MESSAGES, never turns (turn addressing BLOCKED).
    session.activate attach-frontend teleport primitive (it does NOT move
                     desktop focus — no backend command can).
    session.history  durable display transcript; session-scoped, so a live
                     runtime id is needed first (session.resume on the STORED
                     id) before it will answer.

BLOCKED surfaces, reported honestly instead of faked: turn-addressed branch,
native ``session.merge``, server-side conversation diff, backend focus moves.
Snapshot SHAs are read ONLY from ``rec["snaps"]`` — never constructed from a
session key (key rotation makes that a wrong-object ref), and mixed-root diffs
are refused instead of guessing.
"""

import copy
import json
import os
import re
import subprocess
import sys
import threading
import time

# Own module-level lock. Deliberately NOT the host plugin's _LOCK: this file is
# executed standalone (module_from_spec + exec_module) and must never depend on
# another module's private surface. It serialises this lane's read-modify-write
# sequences against itself.
_LOCK = threading.RLock()

_STATE_KEY = "sessions"   # host's ctx.state key for the evidence map (E-A)
_HOST = None              # host module globals, captured in register()
_CTX = None               # PluginContext, captured in register()

_MAX_FORKS = 20
_MAX_BRANCHES = 20
_MAX_MERGES = 20
_MAX_TELEPORTS = 20
_MAX_SESSIONS = 60        # mirrors the host _rec() eviction cap
_GIT_TIMEOUT = 20

_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_VIA_RE = re.compile(r"^[a-z_]{1,24}$")

# Fields cloned onto a forked child's evidence record. Lists are deep-copied so
# a later append on the parent never leaks into the child (or vice versa).
_FORK_COPY = ("files", "snaps", "runs", "blocks", "verdict", "detail",
              "receipt", "approvals", "attn", "tamper", "_root")


class SurgeryError(Exception):
    """Raised inside this lane; always turned into a JSON error payload."""


# ---------------------------------------------------------------------------
# small payload helpers
# ---------------------------------------------------------------------------

def _out(payload: dict) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        return '{"ok": false, "error": "payload not serialisable"}'


def _bad(msg, **extra) -> str:
    extra["ok"] = False
    extra["error"] = str(msg)[:400]
    return _out(extra)


def _good(payload: dict) -> str:
    body = dict(payload)
    body["ok"] = True
    return _out(body)


def _tokens(arg) -> list:
    """Accept the host's raw-args string, but tolerate a list (defensive)."""
    if arg is None:
        return []
    if isinstance(arg, (list, tuple)):
        return [str(a).strip() for a in arg if str(a).strip()]
    return str(arg).split()


def _append(lst, item: dict, cap: int) -> None:
    try:
        lst.append(item)
        del lst[: max(0, len(lst) - cap)]
    except Exception:
        pass


# ---------------------------------------------------------------------------
# state access: prefer the LIVE host _SESSIONS, fall back to ctx.state
# ---------------------------------------------------------------------------

def _looks_like_host(g) -> bool:
    try:
        return (g is not globals()
                and g.get("_STATE_KEY") == _STATE_KEY
                and isinstance(g.get("_SESSIONS"), dict)
                and callable(g.get("_persist")))
    except Exception:
        return False


def _capture_host() -> None:
    """Find the host plugin's module globals.

    register(ctx) is called from patches.register_all(), which is called from
    the host plugin's own register(), so the caller chain carries the live
    ``_SESSIONS`` dict one frame up from register_all. Walking frames keeps the
    reference live; a sys.modules scan is the fallback. If neither matches,
    commands degrade to a best-effort ctx.state read/write (reported as
    ``state_channel`` in every payload).
    """
    global _HOST
    try:
        frame = sys._getframe(1)
    except Exception:
        frame = None
    while frame is not None:
        if _looks_like_host(frame.f_globals):
            _HOST = frame.f_globals
            return
        frame = frame.f_back
    try:
        for mod in list(sys.modules.values()):
            if mod is None:
                continue
            g = getattr(mod, "__dict__", None)
            if isinstance(g, dict) and _looks_like_host(g):
                _HOST = g
                return
    except Exception:
        pass
    # keep whatever we had; a stale None just selects the ctx.state channel


def _sessions() -> tuple:
    """(sessions mapping, host globals or None)."""
    host = _HOST if isinstance(_HOST, dict) else None
    if host is not None:
        live = host.get("_SESSIONS")
        if isinstance(live, dict):
            return live, host
    if _CTX is not None:
        try:
            stored = _CTX.state.get(_STATE_KEY)
        except Exception:
            stored = None
        if isinstance(stored, dict):
            return stored, None
    return {}, None


def _channel(host) -> str:
    if isinstance(host, dict):
        return "live"
    return "ctx.state" if _CTX is not None else "none"


def _save(sessions: dict, host) -> str:
    """Persist after a mutation, through the channel we read from."""
    try:
        if isinstance(host, dict) and callable(host.get("_persist")):
            host["_persist"]()          # writes the live dict we just mutated
            return "live"
        if _CTX is not None:
            _CTX.state.set(_STATE_KEY, sessions)
            return "ctx.state"
    except Exception:
        return "none"
    return "none"


def _new_rec() -> dict:
    """Mirror of the host's default evidence record (lane spec, E-A).

    patches cannot import the host module, so the default shape is duplicated
    here rather than invented — keys match ``_new_rec()`` in ``__init__.py``.
    """
    return {"files": [], "runs": [], "blocks": [], "verdict": None,
            "detail": "", "at": 0.0, "nudges": 0, "snaps": [],
            "approvals": [], "attn": [], "inst_epoch": 0.0,
            "test_digest": {}, "tamper": [], "receipt": {}}


def _locate(sessions: dict, key: str):
    """Exact key first, then the host's own fuzzy rule (ids rotate)."""
    if not key:
        return None
    rec = sessions.get(key)
    if isinstance(rec, dict):
        return rec
    for k, v in sessions.items():
        if not isinstance(v, dict):
            continue
        ks = str(k)
        if ks.endswith(key) or key in ks:
            return v
    return None


def _evict(sessions: dict) -> None:
    try:
        while len(sessions) > _MAX_SESSIONS:
            oldest = min(sessions, key=lambda k: (sessions[k] or {}).get("at") or 0)
            sessions.pop(oldest, None)
    except Exception:
        pass


def _ensure(sessions: dict, key: str) -> dict:
    rec = _locate(sessions, key)
    if rec is None:
        rec = _new_rec()
        rec["at"] = time.time()
        sessions[key] = rec
        _evict(sessions)
    return rec


# ---------------------------------------------------------------------------
# git (array argv only — never a shell string, never --force, 20s timeout)
# ---------------------------------------------------------------------------

def _git(root: str, *args: str, timeout: int = _GIT_TIMEOUT):
    if not isinstance(root, str) or not root.strip():
        raise SurgeryError("no repo root recorded on that snapshot")
    root = root.strip()
    if not os.path.isdir(root):
        raise SurgeryError(f"repo root does not exist: {root}")
    argv = ["git", "-C", root] + [str(a) for a in args]
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    try:
        proc = subprocess.run(argv, shell=False, capture_output=True, text=True,
                              timeout=timeout, env=env, stdin=subprocess.DEVNULL,
                              check=False)
    except subprocess.TimeoutExpired:
        raise SurgeryError(f"git timed out after {timeout}s")
    except FileNotFoundError:
        raise SurgeryError("git executable not found")
    except OSError as exc:
        raise SurgeryError(f"git failed to launch: {exc}")
    return proc


def _pick_snap(snaps: list, explicit: str, key: str):
    """SHAs resolve ONLY from rec['snaps'] — never from a constructed ref."""
    if explicit:
        if not _SHA_RE.match(explicit):
            raise SurgeryError(f"not a hex sha: {explicit!r}")
        for s in snaps:
            if str(s.get("sha")) == explicit:
                return s
        raise SurgeryError(
            f"sha {explicit[:10]} is not in rec['snaps'] for {key} — snapshot "
            "SHAs resolve only from recorded snapshots")
    if not snaps:
        raise SurgeryError(
            f"no snapshots recorded for {key} — day-diff needs at least one "
            "(run a turn so a tsnap exists)")
    return snaps[-1]                      # latest snapshot


def _snap_view(key: str, snap: dict) -> dict:
    return {"key": key, "n": snap.get("n"), "sha": snap.get("sha"),
            "label": snap.get("label") or "", "ts": snap.get("ts")}


# ---------------------------------------------------------------------------
# day-fork
# ---------------------------------------------------------------------------

def _do_fork(arg) -> str:
    parts = _tokens(arg)
    if len(parts) < 2:
        return _bad("usage: day-fork <from_key> <to_key>",
                    note="pair with session.branch: <to_key> is the branch "
                         "result's stored_session_id")
    src_key, dst_key = parts[0], parts[1]
    if src_key == dst_key:
        return _bad("from_key and to_key are identical")
    with _LOCK:
        sessions, host = _sessions()
        if dst_key in sessions:
            return _bad(f"{dst_key} already has an evidence record — refusing "
                        "to overwrite it", to=dst_key,
                        state_channel=_channel(host))
        src = _locate(sessions, src_key)
        parent_created = src is None
        if src is None:
            # no parent evidence yet: create the shell so the fork link itself
            # is durable, and say so (nothing to clone below).
            src = _new_rec()
            src["at"] = time.time()
            sessions[src_key] = src
        child = _new_rec()
        for field in _FORK_COPY:
            if field in src:
                try:
                    child[field] = copy.deepcopy(src[field])
                except Exception:
                    child[field] = src[field]
        child["at"] = time.time()
        child["fork_of"] = src_key
        snaps = [s for s in (child.get("snaps") or []) if isinstance(s, dict)]
        child["fork_at_snaps"] = len(snaps)
        sessions[dst_key] = child
        _evict(sessions)
        _append(src.setdefault("forks", []),
                {"to": dst_key, "at": time.time(), "snaps_n": len(snaps)},
                _MAX_FORKS)
        channel = _save(sessions, host)
    return _good({"from": src_key, "to": dst_key,
                  "cloned": not parent_created,
                  "parent_record_created": parent_created,
                  "snaps_copied": len(snaps),
                  "fork_at_snaps": child["fork_at_snaps"],
                  "state_channel": channel,
                  "note": "evidence record cloned plugin-side "
                          "(files/snaps/runs/blocks/receipt) — session.branch "
                          "copies messages only; there is no gateway "
                          "state-clone RPC"})


# ---------------------------------------------------------------------------
# day-branch — bookkeeping for a message-granular session.branch cut
# ---------------------------------------------------------------------------

def _do_branch(arg) -> str:
    parts = _tokens(arg)
    if len(parts) < 3:
        return _bad("usage: day-branch <parent_key> <child_key> <count>",
                    note="count = MESSAGES kept by session.branch "
                         "(history[:count]); there is no turn-addressed "
                         "branch RPC")
    parent_key, child_key, raw = parts[0], parts[1], parts[2]
    try:
        count = int(raw)
    except Exception:
        return _bad(f"count must be an integer number of messages, got {raw!r}")
    if count < 1:
        return _bad("count must be >= 1 message")
    with _LOCK:
        sessions, host = _sessions()
        parent = _ensure(sessions, parent_key)
        _append(parent.setdefault("branches", []),
                {"child": child_key, "count": count, "at": time.time()},
                _MAX_BRANCHES)
        channel = _save(sessions, host)
    return _good({"parent": parent_key, "child": child_key, "count": count,
                  "state_channel": channel,
                  "note": "recorded a message-granular branch: session.branch "
                          "keeps history[:count] — count is MESSAGES, not "
                          "turns"})


# ---------------------------------------------------------------------------
# day-diff — worktree diff between two snapshot SHAs (read-only, no refs)
# ---------------------------------------------------------------------------

def _do_diff(arg) -> str:
    parts = _tokens(arg)
    if len(parts) < 2:
        return _bad("usage: day-diff <keyA> <keyB> [shaA] [shaB]",
                    note="SHAs default to each record's latest rec['snaps'] "
                         "entry; explicit SHAs must also appear in that "
                         "record's snaps")
    key_a, key_b = parts[0], parts[1]
    sha_a_arg = parts[2] if len(parts) > 2 else ""
    sha_b_arg = parts[3] if len(parts) > 3 else ""
    with _LOCK:
        sessions, host = _sessions()
        channel = _channel(host)
        rec_a = _locate(sessions, key_a)
        if rec_a is None:
            return _bad(f"no evidence record for {key_a}", state_channel=channel)
        rec_b = _locate(sessions, key_b)
        if rec_b is None:
            return _bad(f"no evidence record for {key_b}", state_channel=channel)
        snaps_a = [s for s in (rec_a.get("snaps") or [])
                   if isinstance(s, dict) and s.get("sha")]
        snaps_b = [s for s in (rec_b.get("snaps") or [])
                   if isinstance(s, dict) and s.get("sha")]
        try:
            snap_a = _pick_snap(snaps_a, sha_a_arg, key_a)
            snap_b = _pick_snap(snaps_b, sha_b_arg, key_b)
        except SurgeryError as exc:
            return _bad(str(exc), state_channel=channel)
        root_a = str(snap_a.get("root") or rec_a.get("_root") or "").strip()
        root_b = str(snap_b.get("root") or rec_b.get("_root") or "").strip()
        sha_a = str(snap_a.get("sha"))
        sha_b = str(snap_b.get("sha"))
    if not root_a or not root_b:
        return _bad("no repo root recorded on one of those snapshots",
                    a=root_a or None, b=root_b or None, state_channel=channel)
    if os.path.realpath(root_a) != os.path.realpath(root_b):
        # refuse instead of guessing which repo the diff should run in
        return _bad("mixed-root diff refused — snapshots belong to different "
                    "repos", a_root=root_a, b_root=root_b,
                    state_channel=channel)
    root = root_a
    # subprocess outside the lock: git can take up to _GIT_TIMEOUT seconds
    try:
        stat = _git(root, "diff", "--stat", "--no-ext-diff", sha_a, sha_b)
        if stat.returncode != 0:
            return _bad("git diff --stat failed: "
                        + (stat.stderr or stat.stdout or "").strip()[:300],
                        root=root, a=sha_a, b=sha_b, state_channel=channel)
        names = _git(root, "diff", "--name-only", "--no-ext-diff", sha_a, sha_b)
        if names.returncode != 0:
            return _bad("git diff --name-only failed: "
                        + (names.stderr or names.stdout or "").strip()[:300],
                        root=root, a=sha_a, b=sha_b, state_channel=channel)
    except SurgeryError as exc:
        return _bad(str(exc), root=root, a=sha_a, b=sha_b,
                    state_channel=channel)
    stat_lines = [ln for ln in (stat.stdout or "").splitlines() if ln.strip()]
    file_list = [ln for ln in (names.stdout or "").splitlines() if ln.strip()]
    return _good({"a": _snap_view(key_a, snap_a),
                  "b": _snap_view(key_b, snap_b),
                  "same_sha": sha_a == sha_b,
                  "root": root,
                  "stat": stat_lines[:80],
                  "files": file_list[:200],
                  "state_channel": channel,
                  "note": "SHAs resolved only from each record's rec['snaps'] "
                          "(never from the session key); mixed roots refused"})


# ---------------------------------------------------------------------------
# day-merge — record an EMULATED merge (there is no session.merge RPC)
# ---------------------------------------------------------------------------

def _do_merge(arg) -> str:
    parts = _tokens(arg)
    if len(parts) < 4:
        return _bad("usage: day-merge <from_key> <to_key> <via> <chars>",
                    note="via is the route actually used "
                         "(user_message/steer/redirect); there is no "
                         "session.merge RPC — this records an emulated merge")
    from_key, to_key, via, raw_chars = parts[0], parts[1], parts[2], parts[3]
    if not _VIA_RE.match(via):
        return _bad(f"via must be a lowercase token such as user_message/"
                    f"steer/redirect, got {via!r}")
    try:
        chars = int(raw_chars)
    except Exception:
        return _bad(f"chars must be an integer, got {raw_chars!r}")
    if chars < 0 or chars > 200000:
        return _bad("chars must be between 0 and 200000")
    if from_key == to_key:
        return _bad("from_key and to_key are identical")
    with _LOCK:
        sessions, host = _sessions()
        target = _ensure(sessions, to_key)
        _append(target.setdefault("merges", []),
                {"from": from_key, "via": via, "chars": chars,
                 "at": time.time()},
                _MAX_MERGES)
        source = _ensure(sessions, from_key)
        source["merged_into"] = to_key
        channel = _save(sessions, host)
    return _good({"from": from_key, "to": to_key, "via": via, "chars": chars,
                  "state_channel": channel,
                  "note": "emulated merge recorded: no session.merge RPC "
                          "exists, so the desktop delivered a digest "
                          "(prompt.submit / steer / redirect) and this command "
                          "only records it in the target's ledger"})


# ---------------------------------------------------------------------------
# day-surgery — ledger payload + teleport bookkeeping
# ---------------------------------------------------------------------------

def _block(key: str, rec: dict, brief: bool = False) -> dict:
    snaps = [s for s in (rec.get("snaps") or []) if isinstance(s, dict)]
    forks = list(rec.get("forks") or [])
    branches = list(rec.get("branches") or [])
    merges = list(rec.get("merges") or [])
    teleports = list(rec.get("teleports") or [])
    if brief:
        return {"key": key, "at": rec.get("at") or 0.0,
                "verdict": rec.get("verdict"),
                "detail": str(rec.get("detail") or "")[:80],
                "snaps": len(snaps), "forks": len(forks),
                "branches": len(branches), "merges": len(merges),
                "teleports": len(teleports),
                "merged_into": rec.get("merged_into"),
                "fork_of": rec.get("fork_of")}
    return {"key": key,
            "at": rec.get("at") or 0.0,
            "verdict": rec.get("verdict"),
            "detail": str(rec.get("detail") or ""),
            "fork_of": rec.get("fork_of"),
            "merged_into": rec.get("merged_into"),
            "fork_at_snaps": rec.get("fork_at_snaps"),
            "root": rec.get("_root") or (snaps[-1].get("root") if snaps else None),
            "snaps": [{"n": s.get("n"), "sha": s.get("sha"),
                       "label": s.get("label") or "", "root": s.get("root"),
                       "ts": s.get("ts")} for s in snaps],
            "forks": forks[-8:],
            "branches": branches[-8:],
            "merges": merges[-8:],
            "teleports": teleports[-8:],
            "receipt": rec.get("receipt") or {}}


def _do_surgery(arg) -> str:
    parts = _tokens(arg)
    if parts and parts[0] == "teleport":
        if len(parts) < 3:
            return _bad("usage: day-surgery teleport <from_key> <to_key>",
                        note="bookkeeping only — a backend command cannot "
                             "move desktop focus")
        from_key, to_key = parts[1], parts[2]
        with _LOCK:
            sessions, host = _sessions()
            rec = _ensure(sessions, from_key)
            _append(rec.setdefault("teleports", []),
                    {"to": to_key, "at": time.time()}, _MAX_TELEPORTS)
            channel = _save(sessions, host)
        return _good({"from": from_key, "to": to_key,
                      "state_channel": channel,
                      "note": "teleport bookkeeping only — the gateway side of "
                              "a teleport is session.activate (attach; it does "
                              "not move focus) and a backend command cannot "
                              "move desktop focus; the focus move is "
                              "desktop-side (host.openSession)"})
    with _LOCK:
        sessions, host = _sessions()
        channel = _channel(host)
        if parts:
            key = parts[0]
            rec = _locate(sessions, key)
            if rec is None:
                return _bad(f"no evidence record for {key}",
                            state_channel=channel)
            return _good({"state_channel": channel,
                          "session": _block(key, rec)})
        blocks = [_block(k, r, brief=True) for k, r in sessions.items()
                  if isinstance(r, dict)]
        blocks.sort(key=lambda b: b.get("at") or 0.0, reverse=True)
        return _good({"state_channel": channel, "count": len(blocks),
                      "sessions": blocks[:60]})


# ---------------------------------------------------------------------------
# registration (commands only)
# ---------------------------------------------------------------------------

def _cmd_fork(arg="") -> str:
    try:
        return _do_fork(arg)
    except Exception as exc:                       # never take the plugin down
        return _bad(f"day-fork failed: {type(exc).__name__}: {exc}")


def _cmd_branch(arg="") -> str:
    try:
        return _do_branch(arg)
    except Exception as exc:
        return _bad(f"day-branch failed: {type(exc).__name__}: {exc}")


def _cmd_diff(arg="") -> str:
    try:
        return _do_diff(arg)
    except Exception as exc:
        return _bad(f"day-diff failed: {type(exc).__name__}: {exc}")


def _cmd_merge(arg="") -> str:
    try:
        return _do_merge(arg)
    except Exception as exc:
        return _bad(f"day-merge failed: {type(exc).__name__}: {exc}")


def _cmd_surgery(arg="") -> str:
    try:
        return _do_surgery(arg)
    except Exception as exc:
        return _bad(f"day-surgery failed: {type(exc).__name__}: {exc}")


_COMMANDS = (
    ("day-fork", _cmd_fork,
     "clone a session's evidence record after session.branch",
     "<from_key> <to_key>"),
    ("day-branch", _cmd_branch,
     "record a message-granular session.branch cut (count = MESSAGES)",
     "<parent_key> <child_key> <count>"),
    ("day-diff", _cmd_diff,
     "git diff between two rec['snaps'] SHAs (mixed roots refused)",
     "<keyA> <keyB> [shaA] [shaB]"),
    ("day-merge", _cmd_merge,
     "record an emulated merge (no session.merge RPC exists)",
     "<from_key> <to_key> <via> <chars>"),
    ("day-surgery", _cmd_surgery,
     "surgery ledger payload; 'teleport <from> <to>' records a teleport",
     "[key] | teleport <from_key> <to_key>"),
)


def register(ctx) -> None:
    """Called once at plugin load (patches.register_all). Commands only."""
    global _CTX
    try:
        _CTX = ctx
    except Exception:
        _CTX = None
    try:
        _capture_host()
    except Exception:
        pass
    for name, fn, desc, hint in _COMMANDS:
        try:
            ctx.register_command(name, fn, description=desc, args_hint=hint)
        except Exception:
            continue                               # isolated per command
