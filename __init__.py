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
  snapshots, vacuum stats) the desktop half polls via ``command.dispatch``.
- ``/day-rollback`` — ``<session_key> <sha>``: restores the worktree to a
  turn snapshot (``git restore --source``).
- ``/day-impact`` — ``<session_key>``: blast-radius scan — symbols touched,
  downstream dependents, and which dependents lack test coverage.
- Negative instincts — guard blocks and non-zero-exit commands are promoted
  into ``<repo>/.hermes/instincts.json``; a ``hermes_day.instincts`` system
  prompt section injects the enabled set into every new session prompt
  (frozen at session start, byte-stable thereafter) so past mistakes are not
  repeated across days. Managed via ``/day-instincts``,
  ``/day-instinct-set`` (toggle), ``/day-instinct-del``.
- ``transform_tool_result`` — context vacuum. When a session is armed via
  ``/day-vacuum <session_key>`` (or the cockpit button), verbose terminal
  outputs are archived to disk and replaced with a compact placeholder
  before they enter context.
- ``pre_approval_request`` / ``post_approval_response`` — observer-only
  (they cannot veto), but they expose the REAL approval stream: the command,
  the pattern that fired, which surface asked (cli/gateway/smart), whether a
  human or the smart-approval aux LLM decided (``decided_by``), and how long
  the human took. A deliberate human ``deny`` promotes into the repo's
  negative-instinct ledger, so a refusal today is a constraint tomorrow;
  ``timeout``/``cancelled`` are booked as approval debt instead, since those
  mean the prompt never reached a human.
- ``agent_loop_stopped`` — a running turn was interrupted (``/stop``, the
  ``/new`` fast-path, or the desktop ``session.interrupt`` path).
- ``subagent_stop`` — delegation outcomes tallied per parent session; only
  ``failed``/``error``/``interrupted`` children raise an attention item.
- ``api_request_error`` — failed provider attempts, identity-deduped over 60s
  so a retry storm cannot flood the feed.
- ``pre_llm_call`` — mid-session instinct re-assertion. The system-prompt
  section renders once and is frozen for the life of the conversation, so a
  trap learned mid-session could not otherwise reach the running agent; this
  returns the newly-promoted set into the turn's user message (never the
  system prompt, which would break the prompt cache) and only when something
  genuinely new landed.
- ``/day-approvals`` — the approval ledger (counts, stalls, denies, latency).
- ``/day-attention`` — interruptions, provider errors, failed subagents.
- ``/day-guard`` — guard self-test. Replays known-bad and known-benign commands
  through the real predicates so a silently-dead gate (or one that has started
  over-blocking) surfaces as DEGRADED instead of an absence of protection.
  ``/day-evidence`` carries the same under ``guard``.
- Rollback insurance — ``/day-rollback`` snapshots the current state first, so
  a rollback is itself reversible rather than a one-way door.

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
_INDEX_KEY = "instinct_index"   # ctx.state key: {"repos": [repo_root, ...]}
_MAX_SESSIONS = 60
_MAX_RUNS = 20
_MAX_BLOCKS = 20
_MAX_SNAPS = 25
_MAX_INSTINCTS = 60             # per repo ledger
_MAX_VERIFY_NUDGES = 2          # self-throttle; framework caps at agent.max_verify_nudges
_MAX_APPROVALS = 40              # per-session approval ledger
_MAX_PENDING_APPROVALS = 60      # in-flight request/response correlation set
_MAX_ATTN = 40                   # per-session attention ledger
_ATTN_KINDS = ("interrupted", "provider_error", "subagent_failed", "approval_stall")
_VACUUM_MIN_LINES = 40           # results longer than this get trimmed when armed
_VACUUM_HEAD = 6
_VACUUM_TAIL = 3

_CTX = None  # PluginContext, set in register()
_LOCK = threading.RLock()
_SESSIONS: Dict[str, Dict[str, Any]] = {}
_VACUUM_ARMED: set = set()      # session_ids with context vacuum armed
_INSTINCT_CACHE: Dict[str, list] = {}  # repo_root -> instincts (enabled ones first)

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

# Shell segments that set up the real command but are never the thing worth
# retrying. Promoting them yields nonsense constraints like "NEVER cd".
_NAV_SEGMENTS = {"cd", "pushd", "popd", "source", ".", "set", "export", "unset",
                 "unsetenv", "true", ":", "echo", "printf", "sudo", "env",
                 "command", "nohup", "nice", "time", "wait", "mkdir", "cp", "mv"}
_SHELL_SPLIT_RE = re.compile(r"&&|\|\||;|\n|\|")
_GENERIC_TRIGGER = "this command"   # neutral label; must not be a _NAV_SEGMENTS member


def _trigger_of(cmd: str) -> str:
    """Derive the retry-trigger for a command, not merely its first token.

    ``cd repo && python -m pytest -q`` must yield ``python -m pytest`` — the
    leading token of a compound command is almost always navigation or setup,
    and promoting it produces useless "NEVER cd"-style constraints that then
    get injected into future prompts.
    """
    text = str(cmd or "").strip()
    if not text:
        return _GENERIC_TRIGGER
    for raw in _SHELL_SPLIT_RE.split(text):
        seg = raw.strip()
        while seg:
            head, _, tail = seg.partition(" ")
            # drop leading VAR=value assignments and wrapper verbs
            if ("=" in head and not head.startswith("-")) or head in _NAV_SEGMENTS:
                seg = tail.strip()
                continue
            break
        if not seg or not _CHECK_RE.search(seg):
            continue
        head, _, tail = seg.partition(" ")
        base = os.path.basename(head) or head
        if base in _NAV_SEGMENTS:
            continue
        # `env FOO=1 python -m pytest` already unwrapped; keep module targets
        rest = tail.split()
        if len(rest) >= 2 and rest[0] == "-m":
            return f"{base} -m {rest[1]}"[:80]
        if rest and not rest[0].startswith("-") and "/" not in rest[0] \
                and not rest[0].endswith((".py", ".js", ".ts", ".sh")):
            return f"{base} {rest[0]}"[:80]
        return base[:80]
    # Nothing check-like was identifiable; keep the historical fallback, but
    # never hand back a navigation token — that is the poison this guards.
    fallback = text.split(" ", 1)[0][:80]
    return _GENERIC_TRIGGER if fallback in _NAV_SEGMENTS else fallback

# Path-shaped token inside a shell command that smells like a test file/dir.
_TEST_TOKEN = r"[^\s\"'`;&|]*(?:test|spec|conftest|__tests__|_test)[^\s\"'`;&|]*"

# Generated / vendored directories. Cache cleanup under one of these is
# housekeeping, not test tampering: blocking it teaches the operator to route
# around the guard, which is worse than the false positive itself.
_CACHE_DIRS = ("__pycache__", "pytest_cache", "mypy_cache", "ruff_cache",
               "tox", "cache", "git", "node_modules", "dist", "build",
               "target", "coverage", "next", "venv", "site-packages")


def _is_generated_path(path: str) -> bool:
    """True when any component is a generated or vendored directory."""
    parts = [p.strip(".") for p in str(path or "").replace("\\", "/").split("/") if p]
    return any(part in _CACHE_DIRS for part in parts)


def _without_generated_paths(cmd: str) -> str:
    """Drop words living under a generated directory before guard matching.

    `rm -rf __pycache__ tests/__pycache__` is housekeeping, not test tampering,
    and blocking it trains the operator to route around the guard.
    """
    return " ".join(w for w in str(cmd or "").split()
                    if not _is_generated_path(w))


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
            "detail": "", "at": 0.0, "nudges": 0, "snaps": [],
            "approvals": [], "attn": [], "inst_epoch": 0.0}


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
# Negative instincts — persistent per-repo ledger of disproven approaches
# ---------------------------------------------------------------------------


def _instincts_path(root: str) -> str:
    return os.path.join(root, ".hermes", "instincts.json")


def _load_instincts(root: str) -> list:
    try:
        with open(_instincts_path(root), encoding="utf-8") as fh:
            data = json.load(fh)
        items = data.get("instincts") if isinstance(data, dict) else data
        return [i for i in (items or []) if isinstance(i, dict)]
    except (OSError, ValueError):
        return []


def _save_instincts(root: str, items: list) -> None:
    try:
        os.makedirs(os.path.join(root, ".hermes"), exist_ok=True)
        tmp = _instincts_path(root) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"instincts": items[-_MAX_INSTINCTS:]}, fh, indent=1)
        os.replace(tmp, _instincts_path(root))
    except OSError:
        pass


def _instinct_roots() -> list:
    """All repo roots with known ledgers — in-memory cache + persisted index."""
    roots = set(_INSTINCT_CACHE)
    if _CTX is not None:
        try:
            idx = _CTX.state.get(_INDEX_KEY) or {}
            roots.update(r for r in idx.get("repos", []) if isinstance(r, str))
        except Exception:
            pass
    return sorted(roots)


def _remember_root(root: str) -> None:
    if not root:
        return
    _INSTINCT_CACHE.setdefault(root, [])
    if _CTX is None:
        return
    try:
        idx = _CTX.state.get(_INDEX_KEY) or {"repos": []}
        repos = idx.get("repos") or []
        if root not in repos:
            repos.append(root)
            _CTX.state.set(_INDEX_KEY, {"repos": repos[-40:]})
    except Exception:
        pass


def _promote_instinct(rec: Dict[str, Any], root: Optional[str], trigger: str,
                      approach: str, reason: str) -> None:
    """Promote a trap/failed command into the repo's instincts ledger.

    Dedupes on (trigger, approach); bumps `hits` on repeats. Caller holds _LOCK.
    """
    if not root:
        root = rec.get("_root") or next(
            (_repo_root(f) for f in reversed(rec.get("files") or []) if _repo_root(f)), None)
    if not root:
        try:  # ambient session cwd — covers sessions whose edits were all blocked
            from agent.runtime_cwd import resolve_context_cwd
            c = resolve_context_cwd()
            root = _repo_root(str(c)) if c else None
        except Exception:
            root = None
    if not root:
        return
    _remember_root(root)
    items = _load_instincts(root)
    hit = next((i for i in items if i.get("trigger_pattern") == trigger
                and i.get("disproven_approach") == approach), None)
    if hit:
        hit["hits"] = int(hit.get("hits") or 1) + 1
        hit["reason"] = reason or hit.get("reason", "")
    else:
        items.append({
            "id": f"i{int(time.time() * 1000) % 10**9:x}",
            "trigger_pattern": trigger[:160],
            "disproven_approach": approach[:240],
            "reason": reason[:240],
            "enabled": True, "hits": 1, "created": time.time(),
        })
    _INSTINCT_CACHE[root] = items
    _save_instincts(root, items)


def _instincts_block(cwd: str = "") -> str:
    """Constraint block from instinct ledgers, scoped to the cwd's repo when it
    resolves (else every known ledger). Frozen into each new system prompt via
    register_system_prompt_section — byte-stable for the conversation's life."""
    roots = _instinct_roots()
    scoped = _repo_root(cwd) if cwd else None
    if scoped and scoped in roots:
        roots = [scoped]
    lines = []
    for root in roots:
        items = [i for i in _load_instincts(root) if i.get("enabled", True)]
        if not items:
            continue
        lines.append(f"In {os.path.basename(root) or root} ({root}):")
        for i in items[:8]:
            lines.append(f"- NEVER {i.get('trigger_pattern', '')} — "
                         f"{i.get('reason', 'previously failed')} "
                         f"(approach tried: `{i.get('disproven_approach', '')[:70]}`)")
    if not lines:
        return ""
    return ("[Hermes Day — negative instincts from this repo's history]\n" +
            "\n".join(lines[:16]) +
            "\nDo not retry these; they were already blocked or failed here.")


def _instincts_section(session_info: Dict[str, Any]) -> str:
    try:
        if not _enabled("instincts"):
            return ""
        info = session_info or {}
        # Stamp the session: everything promoted from here on is "new" and gets
        # re-asserted mid-conversation by _pre_llm_call, since these very bytes
        # are frozen for the life of the conversation.
        sid = str(info.get("session_id") or "")
        if sid:
            with _LOCK:
                _rec(sid)["inst_epoch"] = time.time()
        return _instincts_block(str(info.get("cwd") or ""))
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Context vacuum — transform_tool_result trims verbose dumps once armed
# ---------------------------------------------------------------------------

_VACUUM_TOOLS = {"terminal", "bash", "execute_code", "run_command",
                 "shell", "powershell", "cmd"}


def _vacuum_dir(sid: str) -> str:
    try:
        from hermes_constants import get_hermes_home
        base = os.path.join(get_hermes_home(), "plugin-data", "hermes-day", "vacuum")
    except Exception:
        base = os.path.join(os.path.expanduser("~"), ".hermes", "plugin-data",
                            "hermes-day", "vacuum")
    return os.path.join(base, sid or "_unknown")


_DUMP_FIELDS = ("stdout", "stderr", "output", "result", "text", "log")


def _transform_tool_result(tool_name: str = "", args: Optional[Dict[str, Any]] = None,
                           result: Any = None, session_id: str = "",
                           **_kw: Any) -> Optional[str]:
    """Replace verbose tool dumps with a pointer to the on-disk archive.

    Results usually arrive as a JSON envelope (``{"stdout": ..., "exit": 0}``)
    — one physical line — so the line test runs on the biggest string field
    inside the payload, and the field is replaced while the envelope survives.
    """
    try:
        if session_id not in _VACUUM_ARMED or not _enabled("vacuum"):
            return None
        if tool_name not in _VACUUM_TOOLS or not isinstance(result, str):
            return None
        body = None
        try:
            parsed = json.loads(result)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            field = max((f for f in _DUMP_FIELDS if isinstance(parsed.get(f), str)),
                        key=lambda f: len(parsed[f]), default=None)
            body = parsed.get(field) if field else None
        else:
            field = None
            body = result
        if not isinstance(body, str):
            return None
        lines = body.splitlines()
        if len(lines) <= _VACUUM_MIN_LINES:
            return None
        root = _vacuum_dir(session_id)
        os.makedirs(root, exist_ok=True)
        n = int(time.time() * 1000) % 10**8
        path = os.path.join(root, f"dump-{n}.log")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(result if isinstance(result, str) else str(result))
        kept = lines[:_VACUUM_HEAD] + lines[-_VACUUM_TAIL:]
        code = parsed.get("exit_code") if isinstance(parsed, dict) else None
        marker = (f"\n[Trimmed stdout: exit {code if code is not None else '?'}, "
                  f"{len(lines)} lines cached at {path}]")
        trimmed = "\n".join(kept) + marker
        out = trimmed if field is None else json.dumps({**parsed, field: trimmed})
        with _LOCK:
            rec = _rec(session_id)
            vac = rec.setdefault("vacuum", {"armed": True, "trimmed": 0, "saved_chars": 0})
            vac["trimmed"] += 1
            vac["saved_chars"] += max(0, len(body) - len(trimmed))
            _persist()
        return out
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Honesty guard
# ---------------------------------------------------------------------------


def _command_violation(cmd: str) -> Optional[tuple]:
    """(reason, rule) for a shell command that fakes a pass, else None."""
    probe = _without_generated_paths(cmd)
    for pattern, reason, rule in _COMMAND_VIOLATIONS:
        if pattern.search(probe):
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
    if not path or not _TEST_PATH_RE.search(path) or _is_generated_path(path):
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
        # promote the trap into the repo's negative-instincts ledger
        path = next((str((args or {}).get(k) or "") for k in _PATH_KEYS
                     if (args or {}).get(k)), "")
        _promote_instinct(rec, _repo_root(path), rule or reason,
                          _call_preview(tool_name, args or {}), reason)
        _persist()
    return {"action": "block", "message": message}


# (label, command, must_block) — replayed through the real predicates.
_CANARY_COMMANDS = (
    ("deletes tests", "rm -rf tests/", True),
    ("git rm a test", "git rm tests/test_x.py", True),
    ("truncates a test", "> tests/test_x.py", True),
    ("launders exit", "pytest -q || true", True),
    ("empty-suite flag", "pytest --passWithNoTests", True),
    ("cache cleanup", "rm -rf __pycache__ tests/__pycache__", False),
    ("runs the suite", "pytest tests/ -q", False),
    ("inspects state", "git status --short", False),
)
_CANARY_TTL = 300.0
_CANARY: Dict[str, Any] = {}
_CANARY_AT = 0.0


def _canary(force: bool = False) -> Dict[str, Any]:
    """Prove the honesty guard is enforcing — and not over-blocking.

    Both failure modes matter. A guard that is silently dead is worse than no
    guard at all (a mistyped path or a swallowed import leaves the gate open),
    and one that trips on cache cleanup trains the operator to route around it,
    which is worse still. Replaying a fixed table of known-bad and known-benign
    cases through the real predicates makes both visible as a DEGRADED state in
    the cockpit instead of a silent absence of protection.
    """
    global _CANARY, _CANARY_AT
    now = time.time()
    if _CANARY and not force and (now - _CANARY_AT) < _CANARY_TTL:
        return _CANARY
    cases = []
    for label, cmd, must_block in _CANARY_COMMANDS:
        try:
            hit = _command_violation(cmd)
            blocked, rule = bool(hit), (hit[1] if hit else "")
        except Exception as exc:                      # a raising guard is a dead guard
            blocked, rule = False, f"raised {type(exc).__name__}"
        cases.append({"case": label, "blocked": blocked, "rule": rule,
                      "expect": "blocked" if must_block else "allowed",
                      "ok": blocked == must_block})
    try:
        edit = _edit_violation("tests/test_x.py",
                               {"old_string": "assert a == 1\nassert b == 2\n",
                                "new_string": "pass\n"})
        cases.append({"case": "guts assertions", "blocked": bool(edit),
                      "rule": edit or "", "expect": "blocked", "ok": bool(edit)})
    except Exception as exc:
        cases.append({"case": "guts assertions", "blocked": False,
                      "rule": f"raised {type(exc).__name__}", "expect": "blocked",
                      "ok": False})
    failed = [c["case"] for c in cases if not c["ok"]]
    _CANARY = {"ok": not failed, "degraded": bool(failed), "failed": failed,
               "cases": cases, "checked_at": now}
    _CANARY_AT = now
    return _CANARY


def _pre_tool_call(tool_name: str = "", args: Optional[Dict[str, Any]] = None,
                   session_id: str = "", **_kw: Any) -> Optional[Dict[str, str]]:
    try:
        if not _enabled("honesty"):
            return None
        _canary()  # TTL-cached liveness probe for the gate itself
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
            # non-zero exits become negative instincts for the repo — but only
            # for check/build-ish commands a future turn might blindly retry
            if code not in (None, 0) and _CHECK_RE.search(cmd):
                _promote_instinct(rec, None, _trigger_of(cmd),
                                  cmd[:240], f"exited {code}")
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
        "vacuum": dict(rec.get("vacuum") or {}),
        "approvals": (rec.get("approvals") or [])[-12:],
        "attn": (rec.get("attn") or [])[-12:],
        "subagents": dict(rec.get("subagents") or {}),
        "last_subagent": rec.get("sub_last") or {},
    }


def _cmd_evidence(arg: str = "") -> str:
    with _LOCK:
        sessions = {sid: _public_rec(r) for sid, r in _SESSIONS.items()
                    if r.get("verdict") or r.get("runs") or r.get("blocks")
                    or r.get("vacuum") or (r.get("snaps") or [])
                    or r.get("approvals") or r.get("attn")}
    sid = (arg or "").strip()
    if sid:
        return json.dumps({"ok": True, "guard": _canary(),
                           "sessions": {sid: sessions.get(sid)}})
    return json.dumps({"ok": True, "guard": _canary(), "sessions": sessions})


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
    # Rollback insurance: capture the CURRENT state before restoring, so this
    # restore is itself reversible. Cascade's and Roo's reverts are explicitly
    # irreversible, which is the trap this avoids.
    insurance = None
    with _LOCK:
        _snapshot(rec, sid, root or "", "pre-rollback")
        if rec.get("snaps"):
            insurance = rec["snaps"][-1].get("sha")
        _persist()
    try:
        # tracked + staged content restored; files created after the snap stay
        out = _git(root, "restore", f"--source={sha}", "--worktree", "--staged", "--",
                   ":/", timeout=30)
        if out.returncode != 0:
            return json.dumps({"ok": False, "error": out.stderr.strip()[:300],
                               "insurance": insurance})
        return json.dumps({
            "ok": True, "sha": sha, "n": snap.get("n"), "root": root,
            "insurance": insurance,
            "note": "tracked files restored to the snapshot; files created after "
                    "it remain (git clean them if unwanted)"
                    + (f"; {insurance[:10]} undoes this restore" if insurance else "")})
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
# /day-instincts, /day-instinct-set, /day-instinct-del, /day-vacuum
# ---------------------------------------------------------------------------


def _cmd_instincts(arg: str = "") -> str:
    """JSON map of every repo's instinct ledger (enabled + disabled)."""
    repos = {}
    for root in _instinct_roots():
        items = _load_instincts(root)
        if items:
            repos[root] = items
    return json.dumps({"ok": True, "repos": repos})


def _cmd_instinct_set(arg: str = "") -> str:
    """``<repo_root> <instinct_id> <0|1>`` — toggle an instinct on/off."""
    parts = (arg or "").split()
    if len(parts) < 3:
        return json.dumps({"ok": False, "error": "usage: day-instinct-set <root> <id> <0|1>"})
    root, iid, flag = parts[0], parts[1], parts[2] == "1"
    items = _load_instincts(root)
    item = next((i for i in items if i.get("id") == iid), None)
    if not item:
        return json.dumps({"ok": False, "error": f"instinct {iid} not found in {root}"})
    item["enabled"] = flag
    _INSTINCT_CACHE[root] = items
    _save_instincts(root, items)
    return json.dumps({"ok": True, "id": iid, "enabled": flag})


def _cmd_instinct_del(arg: str = "") -> str:
    """``<repo_root> <instinct_id>`` — delete an instinct."""
    parts = (arg or "").split()
    if len(parts) < 2:
        return json.dumps({"ok": False, "error": "usage: day-instinct-del <root> <id>"})
    root, iid = parts[0], parts[1]
    items = _load_instincts(root)
    kept = [i for i in items if i.get("id") != iid]
    if len(kept) == len(items):
        return json.dumps({"ok": False, "error": f"instinct {iid} not found in {root}"})
    _INSTINCT_CACHE[root] = kept
    _save_instincts(root, kept)
    return json.dumps({"ok": True, "id": iid, "removed": True})


def _cmd_vacuum(arg: str = "") -> str:
    """``<session_key>`` — arm context vacuum for a session's tool dumps."""
    sid = (arg or "").strip()
    if not sid:
        return json.dumps({"ok": False, "error": "usage: day-vacuum <session_key>"})
    rec = _find_rec(sid)
    _VACUUM_ARMED.add(sid)
    # arm every key spelling too — hook sees the agent/session key
    if rec is not None:
        for k in _SESSIONS:
            if _SESSIONS[k] is rec:
                _VACUUM_ARMED.add(k)
    with _LOCK:
        if rec is not None:
            vac = rec.setdefault("vacuum", {"armed": True, "trimmed": 0, "saved_chars": 0})
            vac["armed"] = True
            _persist()
    return json.dumps({"ok": True, "session": sid, "armed": True,
                       "note": f"tool dumps >{_VACUUM_MIN_LINES} lines now archive to disk "
                               "and enter context as a compact placeholder"})


# ---------------------------------------------------------------------------
# Approval lifecycle, attention ledger, mid-session instincts (observer hooks)
#
# ``pre_approval_request`` / ``post_approval_response`` are observer-only (they
# cannot veto), but they hand the cockpit the REAL approval stream instead of an
# inferred one: which surface asked, which pattern fired, whether a human or the
# smart-approval aux LLM decided, and how long the human took to answer. A
# deliberate human ``deny`` is the highest-signal trap there is, so it promotes
# straight into the repo's negative-instinct ledger — the same ledger the honesty
# guard feeds. ``timeout``/``cancelled`` are NOT denials (docs: the prompt never
# reached a human), so they are booked as approval debt instead of instincts.
#
# ``pre_llm_call`` closes the staleness gap in the system-prompt section: that
# section renders once per new session and its bytes are frozen, so an instinct
# learned MID-session cannot reach the running conversation. Anything injected
# mid-conversation must ride a user message (never the system prompt) to keep the
# prompt cache intact — which is exactly what this hook returns.
# ---------------------------------------------------------------------------

_PENDING_APPROVALS: list = []    # unresolved pre_approval_request entries
_PROV_ERR_TS: Dict[str, float] = {}   # dedupe identical provider errors (60s)
_DENY_CHOICES = {"deny"}
_STALL_CHOICES = {"timeout", "cancelled", "notify_failed"}


def _brief(text: Any, n: int = 300) -> str:
    return " ".join(str(text or "").split())[:n]


def _attn_add(rec: Dict[str, Any], kind: str, text: str, **extra: Any) -> None:
    """Append one attention item. Caller holds _LOCK."""
    item = {"kind": kind, "text": _brief(text), "ts": time.time()}
    item.update(extra)
    _append(rec["attn"], item, _MAX_ATTN)


def _pre_approval_request(command: str = "", description: str = "",
                          pattern_key: str = "", pattern_keys: Optional[list] = None,
                          session_key: str = "", surface: str = "",
                          session_id: str = "", turn_id: str = "",
                          tool_call_id: str = "", **_kw: Any) -> None:
    """Record an approval request the moment it is raised."""
    try:
        entry = {"cmd": _brief(command), "desc": _brief(description, 200),
                 "key": pattern_key or "", "keys": [str(k) for k in (pattern_keys or [])],
                 "surface": surface or "", "sk": session_key or "",
                 "turn": turn_id or "", "tcid": tool_call_id or "",
                 "ts": time.time(), "choice": "", "by": "", "ms": -1}
        with _LOCK:
            rec = _rec(session_id or session_key or "")
            _append(rec["approvals"], entry, _MAX_APPROVALS)
            _PENDING_APPROVALS.append(entry)
            del _PENDING_APPROVALS[:-_MAX_PENDING_APPROVALS]  # bound in-flight set
            rec["at"] = entry["ts"]
        _persist()
    except Exception:
        pass


def _post_approval_response(command: str = "", description: str = "",
                            pattern_key: str = "", pattern_keys: Optional[list] = None,
                            session_key: str = "", surface: str = "",
                            session_id: str = "", turn_id: str = "",
                            tool_call_id: str = "", choice: str = "",
                            decided_by: str = "", **_kw: Any) -> None:
    """Resolve the matching request, then book the outcome."""
    try:
        now = time.time()
        cmd = _brief(command)
        want = pattern_key or ""
        choice = choice or ""
        with _LOCK:
            hit = None
            for e in reversed(_PENDING_APPROVALS):     # newest unresolved match
                if e.get("choice") or e.get("cmd") != cmd or e.get("key") != want:
                    continue
                if session_key and e.get("sk") and e["sk"] != session_key:
                    continue
                hit = e
                break
            rec = _rec(session_id or session_key or "")
            if hit is not None:
                hit["choice"] = choice
                hit["by"] = decided_by or ""
                hit["ms"] = max(0, int((now - float(hit.get("ts") or now)) * 1000))
                if hit.get("surface"):
                    surface = hit["surface"]
                try:
                    _PENDING_APPROVALS.remove(hit)
                except ValueError:
                    pass
            else:                                       # response with no seen request
                entry = {"cmd": cmd, "desc": _brief(description, 200), "key": want,
                         "keys": [str(k) for k in (pattern_keys or [])],
                         "surface": surface or "", "sk": session_key or "",
                         "turn": turn_id or "", "tcid": tool_call_id or "",
                         "ts": now, "choice": choice, "by": decided_by or "", "ms": -1}
                _append(rec["approvals"], entry, _MAX_APPROVALS)

            if choice in _DENY_CHOICES:
                _promote_instinct(rec, None, want or "approval",
                                  cmd, "you denied this command: "
                                       + _brief(description, 140) or "refused")
            elif choice in _STALL_CHOICES:
                _attn_add(rec, "approval_stall",
                          f"approval never answered ({choice}) — agent waited on you",
                          key=want, surface=surface or "")
            rec["at"] = now
        _persist()
    except Exception:
        pass


def _agent_loop_stopped(session_key: str = "", platform: str = "",
                        reason: str = "", invalidation_reason: str = "",
                        **_kw: Any) -> None:
    """A running turn was interrupted (/stop, /new fast-path, desktop interrupt)."""
    try:
        with _LOCK:
            rec = _rec(session_key)
            _attn_add(rec, "interrupted",
                      f"turn interrupted: {reason or 'stop'}", platform=platform or "",
                      reason=reason or "", detail=invalidation_reason or "")
            rec["at"] = time.time()
        _persist()
    except Exception:
        pass


def _subagent_stop(parent_session_id: str = "", child_role: Optional[str] = None,
                   child_summary: Optional[str] = None, child_status: str = "",
                   tool_call_history: Optional[list] = None,
                   duration_ms: int = 0, **_kw: Any) -> None:
    """Delegation finished — tally it, and surface only the bad endings.

    Fires many times per turn under heavy fan-out, so the common (successful)
    path stays in memory and skips the persist.
    """
    try:
        status = child_status or "unknown"
        dur = int(duration_ms or 0)
        with _LOCK:
            rec = _rec(parent_session_id)
            tally = rec.setdefault("subagents", {})
            tally[status] = int(tally.get(status) or 0) + 1
            rec["sub_last"] = {"role": child_role or "", "status": status, "ms": dur,
                               "tools": len(tool_call_history or []), "ts": time.time()}
            if status in ("failed", "error", "interrupted"):
                _attn_add(rec, "subagent_failed",
                          f"{child_role or 'subagent'} {status}: "
                          f"{_brief(child_summary, 160)}",
                          status=status, role=child_role or "", ms=dur)
            else:
                return
            rec["at"] = time.time()
        _persist()
    except Exception:
        pass


def _api_request_error(session_id: str = "", provider: str = "", model: str = "",
                       status_code: Optional[int] = None, retryable: Optional[bool] = None,
                       reason: Optional[str] = None, error: Any = None,
                       **_kw: Any) -> None:
    """Provider attempt failed — a real triage signal (identity-deduped, 60s)."""
    try:
        if isinstance(error, dict):
            msg = error.get("message") or error.get("type") or ""
        else:
            msg = str(error or "")
        msg = _brief(msg or reason or "provider error", 200)
        sig = f"{provider}|{status_code}|{msg}"
        with _LOCK:
            now = time.time()
            last = _PROV_ERR_TS.get(sig)
            if last is not None and now - last < 60:
                return
            _PROV_ERR_TS[sig] = now
            if len(_PROV_ERR_TS) > 200:
                for k in sorted(_PROV_ERR_TS, key=lambda k: _PROV_ERR_TS[k])[:100]:
                    _PROV_ERR_TS.pop(k, None)
            rec = _rec(session_id)
            _attn_add(rec, "provider_error", msg, provider=provider or "",
                      status=status_code, retryable=retryable, reason=reason or "")
            rec["at"] = now
        _persist()
    except Exception:
        pass


def _pre_llm_call(session_id: str = "", user_message: str = "",
                  conversation_history: Optional[list] = None,
                  is_first_turn: bool = False, platform: str = "",
                  **_kw: Any) -> Optional[str]:
    """Surface instincts promoted AFTER this session's prompt was frozen.

    The system-prompt section is rendered once and byte-stable for the life of
    the conversation, so a trap learned mid-session never reaches the running
    agent. Returning text here appends it to the turn's user message — the
    sanctioned, cache-safe channel. Silent unless something genuinely new landed.
    """
    try:
        if not _enabled("instincts"):
            return None
        with _LOCK:
            rec = _SESSIONS.get(session_id or "")
            if rec is None:
                return None
            since = float(rec.get("inst_epoch") or 0.0)
            if since <= 0:
                return None
            fresh: list = []
            for root in _instinct_roots():
                for i in _load_instincts(root):
                    if i.get("enabled", True) and float(i.get("created") or 0) > since:
                        fresh.append((root, i))
            if not fresh:
                return None
            rec["inst_epoch"] = time.time()          # surface each one exactly once
        fresh.sort(key=lambda p: float(p[1].get("created") or 0))
        lines = ["[Hermes Day — new negative instincts learned this session]"]
        for root, i in fresh[:6]:
            lines.append(f"- In {os.path.basename(root) or root}: NEVER "
                         f"{_brief(i.get('trigger_pattern'), 120)} — "
                         f"{_brief(i.get('reason'), 140)}")
        lines.append("These were blocked or refused earlier in this conversation. "
                     "Do not retry them.")
        block = "\n".join(lines)
        return block if len(block) < 1200 else block[:1200]
    except Exception:
        return None


def _approval_digest(limit: int = 12) -> Dict[str, Any]:
    """Rolling approval ledger across sessions — the cockpit's approvals view."""
    with _LOCK:
        rows: list = []
        by_choice: Dict[str, int] = {}
        stalls = denies = 0
        for sid, rec in _SESSIONS.items():
            for a in rec.get("approvals") or []:
                by_choice[a["choice"] or "pending"] = \
                    by_choice.get(a["choice"] or "pending", 0) + 1
                if a.get("choice") in _STALL_CHOICES:
                    stalls += 1
                elif a.get("choice") in _DENY_CHOICES:
                    denies += 1
                rows.append({**a, "sid": sid})
        rows.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return {"ok": True, "counts": by_choice, "stalls": stalls, "denies": denies,
            "pending": len(_PENDING_APPROVALS), "recent": rows[:limit]}


def _cmd_approvals(arg: str = "") -> str:
    try:
        limit = int((arg or "").strip() or 12)
    except ValueError:
        limit = 12
    return json.dumps(_approval_digest(max(1, min(limit, 60))))


def _cmd_attention(arg: str = "") -> str:
    """Session-attention feed: interruptions, provider errors, failed children."""
    with _LOCK:
        out = {}
        for sid, rec in _SESSIONS.items():
            items = rec.get("attn") or []
            if not items:
                continue
            out[sid] = {"items": items[-12:], "subagents": dict(rec.get("subagents") or {}),
                        "last_subagent": rec.get("sub_last") or {}}
    return json.dumps({"ok": True, "sessions": out})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _cmd_guard(arg: str = "") -> str:
    """``[force]`` — guard liveness + over-blocking self-test."""
    return json.dumps(_canary(force=bool((arg or "").strip())))


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
    ctx.register_hook("transform_tool_result", _transform_tool_result)
    # Observer-only surfaces: the real approval stream, stall/error attention,
    # delegation outcomes, and mid-session instinct re-assertion.
    for _hook, _fn in (
        ("pre_approval_request", _pre_approval_request),
        ("post_approval_response", _post_approval_response),
        ("agent_loop_stopped", _agent_loop_stopped),
        ("subagent_stop", _subagent_stop),
        ("api_request_error", _api_request_error),
        ("pre_llm_call", _pre_llm_call),
    ):
        try:
            ctx.register_hook(_hook, _fn)
        except Exception:
            pass
    try:
        ctx.register_system_prompt_section(
            "hermes_day.instincts", _instincts_section, position="after_memory")
    except Exception:
        pass
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
    ctx.register_command(
        "day-instincts", _cmd_instincts,
        description="Repo negative-instinct ledgers as JSON.",
    )
    ctx.register_command(
        "day-instinct-set", _cmd_instinct_set,
        description="Toggle a repo instinct on/off.",
        args_hint="<repo_root> <instinct_id> <0|1>",
    )
    ctx.register_command(
        "day-instinct-del", _cmd_instinct_del,
        description="Delete a repo instinct.",
        args_hint="<repo_root> <instinct_id>",
    )
    ctx.register_command(
        "day-vacuum", _cmd_vacuum,
        description="Arm context vacuum: verbose tool dumps archive to disk.",
        args_hint="<session_key>",
    )
    ctx.register_command(
        "day-approvals", _cmd_approvals,
        description="Real approval ledger: surface, pattern, who decided, latency.",
        args_hint="[limit]",
    )
    ctx.register_command(
        "day-attention", _cmd_attention,
        description="Attention feed: interruptions, provider errors, failed subagents.",
    )
    ctx.register_command(
        "day-guard", _cmd_guard,
        description="Honesty-guard self-test: proves the gate blocks and is not over-blocking.",
        args_hint="[force]",
    )
