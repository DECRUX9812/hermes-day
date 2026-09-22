"""Lane 08 — Instinct Studio: preview / edit / status / loadout commands.

Observer-only lane: registers plain commands — never a hook, never a gate,
`plugin.yaml` untouched. Every preview byte comes from the plugin's own
renderer, `_instincts_block` (the exact callable `_instincts_section` returns
at injection time, L431–445), so preview == injected cannot drift. The staged
(as-if-saved) preview runs that same code object with only the ledger read
overlaid — no format string from §1.3 is re-implemented in this file.

Spec: hday-v2/lanes/08-instinct-studio.md
"""

import inspect
import json
import os
import re
import sys
import threading
import time
import types

# --- constraints (all read from source; see spec §1) -------------------------
_MAX_INSTINCTS = 60                      # __init__.py L97  — per-repo ledger cap
_MAX_ITEMS_PER_REPO = 8                  # L420 — items[:8] reach the prompt
_MAX_BLOCK_LINES = 16                    # L427 — lines[:16]; repo headers count
_CAP_TRIGGER = 160                       # L397 — promote-time write cap (read-only in v1)
_CAP_APPROACH = 240                      # L398 — dedupe-key half, never edited in v1
_CAP_REASON = 240                        # L399 — the only field v1 edits, via _brief
MAX_SYSTEM_PROMPT_SECTION_CHARS = 4000   # plugins_dispatch.py L58 — skip, not truncate (L509-511)

_LOADOUT_FILE = "instinct-loadouts.json"
_LOADOUT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
_FIELDS = ("reason",)                    # v1: the only free-text field

_MARKER_START = "<!-- hermes-plugin-sections:start -->"
_MARKER_END = "<!-- hermes-plugin-sections:end -->"
_MARKER_END_TAIL = ":end -->"            # spec §2.2.2 — edit rejection predicate

# Lane-owned lock. The BRIEF forbids patches importing the main module's _LOCK,
# so every mutation this lane makes serialises here instead (see Risks).
_LOCK = threading.RLock()

# Main hermes-day module namespace (its __dict__), discovered per patch
# instance — never imported by module name, so host and test fixtures both work.
_MAIN = None


# ---------------------------------------------------------------------------
# Core-module discovery + shared primitives (all delegated, never re-implemented)
# ---------------------------------------------------------------------------

def _discover():
    """Locate the main hermes-day module namespace.

    Preferred: walk the call stack (our register <- patches.register_all <-
    the plugin's register) which yields the exact module instance that wired
    this patch — including the fresh instance each test fixture builds.
    Fallback: scan sys.modules for a module exposing the renderer. Returns the
    module dict, or None when the core is unreachable.
    """
    global _MAIN
    if isinstance(_MAIN, dict) and callable(_MAIN.get("_instincts_block")):
        return _MAIN
    try:
        f = inspect.currentframe()
        for _ in range(8):
            if f is None:
                break
            g = f.f_globals
            if (isinstance(g, dict) and callable(g.get("_instincts_block"))
                    and callable(g.get("_load_instincts"))
                    and callable(g.get("_save_instincts"))):
                _MAIN = g
                return _MAIN
            f = f.f_back
    except Exception:
        pass
    try:
        for mod in list(sys.modules.values()):
            g = getattr(mod, "__dict__", None)
            if (isinstance(g, dict) and callable(g.get("_instincts_block"))
                    and callable(g.get("_load_instincts"))
                    and callable(g.get("_save_instincts"))):
                _MAIN = g
                return _MAIN
    except Exception:
        pass
    return None


def _err(msg):
    return json.dumps({"ok": False, "error": str(msg)})


def _usage(cmd, hint):
    return _err("usage: %s %s" % (cmd, hint))


def _arg_str(arg):
    """Command arg to text — the host passes str; tolerate a pre-split list."""
    if arg is None:
        return ""
    if isinstance(arg, str):
        return arg
    if isinstance(arg, (list, tuple)):
        return " ".join(str(a) for a in arg)
    return str(arg)


def _enabled_gate(g):
    """Mirror of the gate check inside _instincts_section (L433): an exception
    there makes the section return '', so an exception here means 'off'."""
    try:
        fn = g.get("_enabled")
        if not callable(fn):
            return False
        return bool(fn("instincts"))
    except Exception:
        return False


def _render_block(g, cwd):
    """Live bytes: the plugin's own `_instincts_block(cwd)`, gated exactly like
    `_instincts_section`. Any render exception mirrors the section's
    `except Exception: return ''` so preview stays byte-identical even then."""
    if not _enabled_gate(g):
        return ""
    fn = g.get("_instincts_block")
    if not callable(fn):
        raise RuntimeError("core renderer (_instincts_block) not reachable")
    try:
        return str(fn(cwd) or "")
    except Exception:
        return ""


def _render_staged(g, cwd, target_root, staged_items):
    """(block, applied): as-if-saved bytes. Runs the SAME `_instincts_block`
    code object with two overlays — the ledger read for `target_root` returns
    the staged copy, and the root index gains `target_root` (post-edit the
    write sequence puts it in `_INSTINCT_CACHE`, L1241). Nothing about the
    format is duplicated here, so staged bytes cannot drift either.
    `applied` is False only if the renderer stopped consulting the ledger
    (shape change) — callers must then refuse rather than show live bytes."""
    if not _enabled_gate(g):
        return "", True
    fn0 = g.get("_instincts_block")
    real_load = g.get("_load_instincts")
    real_roots = g.get("_instinct_roots")
    if not (callable(fn0) and callable(real_load) and callable(real_roots)):
        raise RuntimeError("core renderer not reachable")

    called = []

    def overlay_load(root):
        called.append(str(root))
        if str(root) == str(target_root):
            return [dict(i) for i in staged_items]
        return real_load(root)

    def overlay_roots():
        try:
            out = list(real_roots())
        except Exception:
            out = []
        if str(target_root) not in [str(r) for r in out]:
            out.append(str(target_root))
        return out

    ns = dict(g)
    ns["_load_instincts"] = overlay_load
    ns["_instinct_roots"] = overlay_roots
    try:
        fn = types.FunctionType(fn0.__code__, ns,
                                getattr(fn0, "__name__", "_instincts_block"),
                                fn0.__defaults__, fn0.__closure__)
        block = str(fn(cwd) or "")
    except Exception:
        # Never overlay the LIVE module namespace: the core renderer does not
        # take this lane's _LOCK, so a concurrent render could observe the
        # overlaid ledger. Refuse instead; _cmd_instinct_preview's except
        # turns this into ok:false (near-impossible path anyway: a module-level
        # def builds without a closure, so FunctionType binding cannot fail).
        raise RuntimeError("staged render unavailable (FunctionType bind failed)")
    return block, str(target_root) in called


def _scope_roots(g, cwd, extra=None):
    """§1.3 step 1 scoping, via the core's own helpers. Metadata only — the
    rendered bytes never come from here."""
    try:
        roots = list(g["_instinct_roots"]())
    except Exception:
        roots = []
    if extra is not None and str(extra) not in [str(r) for r in roots]:
        roots.append(str(extra))
    try:
        scoped = g["_repo_root"](cwd) if cwd else None
    except Exception:
        scoped = None
    if scoped and scoped in roots:
        roots = [scoped]
    return roots


def _safe_load(g, root):
    fn = g.get("_load_instincts")
    if not callable(fn):
        return []
    try:
        return list(fn(root) or [])
    except Exception:
        return []


def _brief(g, value):
    """The core's own sanitiser (L1308-1309): whitespace-collapse + cap."""
    fn = g.get("_brief")
    if not callable(fn):
        raise RuntimeError("core _brief not reachable")
    return str(fn(value, _CAP_REASON))


def _dispatch_markers(text):
    """Would dispatch skip this section? plugins_dispatch L506-508."""
    start, end = _MARKER_START, _MARKER_END
    try:
        from hermes_cli.plugins import PLUGIN_SECTIONS_START, PLUGIN_SECTIONS_END
        start, end = PLUGIN_SECTIONS_START, PLUGIN_SECTIONS_END
    except Exception:
        pass
    return start in text or end in text


def _edit_markers(value):
    """Edit-rejection predicate, spec §2.2.2: START literal or ':end -->'."""
    start = _MARKER_START
    try:
        from hermes_cli.plugins import PLUGIN_SECTIONS_START
        start = PLUGIN_SECTIONS_START
    except Exception:
        pass
    return start in value or _MARKER_END_TAIL in value


def _ambient_cwd():
    try:
        return os.getcwd()
    except Exception:
        return ""


def _find_root_for_id(g, iid):
    try:
        roots = list(g["_instinct_roots"]())
    except Exception:
        return []
    for r in roots:
        for i in _safe_load(g, r):
            if str(i.get("id")) == str(iid):
                return r
    return None


def _shadowed_ids(block, scope_roots, items_by_root):
    """Ids in the ledger but not in the prompt: index >= items[:8] (L420) or
    cut by lines[:16] counting repo headers (L427). Membership is read back
    from the rendered bytes — the renderer itself stays untouched. If headers
    cannot be attributed back to the scope, degrade to [] rather than lie."""
    try:
        enabled_of = {}
        for r in scope_roots:
            enabled_of[r] = [i for i in (items_by_root.get(r) or [])
                             if i.get("enabled", True)]
        stripped = (block or "").strip()
        if not stripped:
            out = []
            for r in scope_roots:
                out.extend(str(i.get("id")) for i in enabled_of.get(r, []))
            return out
        content = stripped.split("\n")[1:-1]        # drop wrapper header + footer
        rendered = {}
        parsed = []
        current = None
        for ln in content[:_MAX_BLOCK_LINES]:
            if ln.startswith("- NEVER "):
                if current is not None:
                    rendered[current] = rendered.get(current, 0) + 1
            elif ln.startswith("In ") and ln.endswith(":") and " (" in ln:
                inner = ln[3:-1].rsplit(" (", 1)    # "basename (root)" -> [base, "root)"]
                current = inner[1] if len(inner) == 2 else None
                if current and current.endswith(")"):
                    current = current[:-1]          # drop the header's closing paren
                parsed.append(current)
                if current is not None:
                    rendered.setdefault(current, 0)
        if not parsed or any(p is None or p not in scope_roots for p in parsed):
            return []
        out = []
        for r in scope_roots:
            n = min(rendered.get(r, 0), _MAX_ITEMS_PER_REPO)
            out.extend(str(i.get("id")) for i in enabled_of.get(r, [])[n:])
        return out
    except Exception:
        return []


# ---------------------------------------------------------------------------
# /day-instinct-preview
# ---------------------------------------------------------------------------

def _cmd_instinct_preview(arg=""):
    """Byte-exact preview of the instincts system-prompt section.

    Forms (spec §2.2.1):
        <empty>                        ambient-cwd block (what a session here gets)
        <repo_root>                    block scoped as that repo's sessions get
        <instinct_id> <field> <v...>   as-if-saved block; repo found from the id
        <repo_root> <id> <field> <v..> as-if-saved block in an explicit repo

    Output: {ok, block, chars (len after .strip(), the value dispatch measures),
    lines (content lines toward lines[:16]), over (chars > 4000), shadowed
    (ids not reaching the prompt), markers (reserved marker present)}.
    """
    try:
        g = _discover()
        if not g:
            return _err("hermes-day core not reachable")
        a = _arg_str(arg)
        tokens = a.split()
        cwd = ""
        staged = None                      # (root, instinct_id, value)
        if not tokens:
            cwd = _ambient_cwd()
        elif len(tokens) == 1:
            cwd = tokens[0]
        elif tokens[1] in _FIELDS:
            p = a.split(None, 2)
            if len(p) < 3:
                return _err("field '%s' needs a value: <instinct_id> %s <value...>"
                            % (tokens[1], tokens[1]))
            root = _find_root_for_id(g, p[0])
            if not root:
                return _err("unknown instinct id '%s'" % p[0])
            staged, cwd = (root, p[0], p[2]), root
        elif len(tokens) >= 3 and tokens[2] in _FIELDS:
            p = a.split(None, 3)
            if len(p) < 4:
                return _err("field '%s' needs a value: <repo_root> <instinct_id> %s <value...>"
                            % (tokens[2], tokens[2]))
            staged, cwd = (p[0], p[1], p[3]), p[0]
        else:
            return _usage("day-instinct-preview",
                          "[repo_root] [instinct_id %s <value...>]"
                          % "|".join(_FIELDS))

        if staged is None:
            block = _render_block(g, cwd)
            scope = _scope_roots(g, cwd)
            items_by_root = dict((r, _safe_load(g, r)) for r in scope)
        else:
            root, iid, value = staged
            items = _safe_load(g, root)
            if not any(str(i.get("id")) == str(iid) for i in items):
                return _err("instinct '%s' not found in %s" % (iid, root))
            v = _brief(g, value)
            if not v:
                return _err("empty value after sanitisation (_brief, 240)")
            if _edit_markers(v):
                return _err("reserved section marker rejected — dispatch would "
                            "skip the whole section (plugins_dispatch L506-508)")
            staged_items = [dict(i) for i in items]
            for i in staged_items:
                if str(i.get("id")) == str(iid):
                    i["reason"] = v          # id / hits / created / enabled untouched
            block, applied = _render_staged(g, root, root, staged_items)
            if not applied:
                return _err("staged render did not consult the ledger "
                            "(renderer shape changed?) — refusing to show live bytes")
            scope = _scope_roots(g, root, extra=root)
            items_by_root = dict(
                (r, staged_items if r == root else _safe_load(g, r)) for r in scope)

        stripped = block.strip()
        chars = len(stripped)
        lines = 0 if not stripped else max(0, len(stripped.split("\n")) - 2)
        return json.dumps({
            "ok": True,
            "gate": _enabled_gate(g),       # real gate state — never inferred from chars
            "block": block,                 # raw section bytes, un-stripped (L443)
            "chars": chars,                 # len(block.strip()) — dispatch L503/L509
            "lines": lines,                 # content lines; wrapper header/footer excluded
            "over": chars > MAX_SYSTEM_PROMPT_SECTION_CHARS,
            "shadowed": _shadowed_ids(block, scope, items_by_root),
            "markers": _dispatch_markers(stripped),
        })
    except Exception as exc:
        return _err("%s: %s" % (type(exc).__name__, exc))


# ---------------------------------------------------------------------------
# /day-instinct-edit
# ---------------------------------------------------------------------------

def _cmd_instinct_edit(arg=""):
    """``<repo_root> <instinct_id> <field> <value...>`` — v1 field: reason.

    Sanitises with the core's own `_brief(value, 240)` (collapse + cap = the
    L399 write cap), rejects reserved section markers, then performs the exact
    write sequence of /day-instinct-set (L1241-1242) under this lane's lock,
    followed by a refetch — `_save_instincts` swallows OSError (L337-338), so
    the refetch, not the optimistic state, is truth (spec §3.4).
    id / hits / created / enabled are never touched: `created > inst_epoch`
    stays false, so the edit reaches NO running session — next session only.
    """
    try:
        g = _discover()
        if not g:
            return _err("hermes-day core not reachable")
        parts = _arg_str(arg).split(None, 3)
        if len(parts) < 4:
            return _usage("day-instinct-edit",
                          "<repo_root> <instinct_id> <field> <value...>")
        root, iid, field, value = parts
        if field not in _FIELDS:
            return _err("unknown field '%s' in v1 (editable: %s)"
                        % (field, ", ".join(_FIELDS)))
        if not os.path.isdir(root):
            return _err("repo_root is not a directory: %s" % root)
        v = _brief(g, value)
        if not v:
            return _err("empty value after sanitisation (_brief, 240)")
        if _edit_markers(v):
            return _err("reserved section marker rejected — dispatch would skip "
                        "the whole section (plugins_dispatch L506-508)")
        with _LOCK:
            save = g.get("_save_instincts")
            cache = g.get("_INSTINCT_CACHE")
            if not callable(save) or not isinstance(cache, dict):
                return _err("core ledger writer not reachable")
            items = _safe_load(g, root)
            item = next((i for i in items if str(i.get("id")) == str(iid)), None)
            if item is None:
                return _err("instinct %s not found in %s" % (iid, root))
            item["reason"] = v
            cache[root] = items
            save(root, items)
            again = next((i for i in _safe_load(g, root)
                          if str(i.get("id")) == str(iid)), None)
            if again is None or str(again.get("reason", "")) != v:
                return _err("write failed (refetch shows an unchanged reason)")
        return json.dumps({"ok": True, "root": root, "id": iid,
                           "field": "reason", "value": v, "chars": len(v)})
    except Exception as exc:
        return _err("%s: %s" % (type(exc).__name__, exc))


# ---------------------------------------------------------------------------
# /day-instinct-status
# ---------------------------------------------------------------------------

def _cmd_instinct_status(arg=""):
    """`{sessions:[{id, cwd, inst_epoch}], block_chars}` — how many sessions
    hold a frozen section (inst_epoch > 0 means the section was stamped for
    them at prompt build) plus the ambient block size after .strip(), the very
    value dispatch measures (L509). Read-only."""
    try:
        g = _discover()
        if not g:
            return _err("hermes-day core not reachable")
        recs = g.get("_SESSIONS")
        rows = []
        with _LOCK:
            # Lane lock per BRIEF (main's _LOCK is off-limits to patches);
            # snapshot defensively — a RuntimeError here means another writer
            # mutated _SESSIONS mid-iteration, so we report no rows rather
            # than a torn view. No retry: the result would be identical.
            try:
                pairs = list(recs.items()) if isinstance(recs, dict) else []
            except RuntimeError:
                pairs = []
        for sid, rec in pairs:
            try:
                if not isinstance(rec, dict):
                    continue
                rows.append({
                    "id": str(sid),
                    "cwd": str(rec.get("cwd") or rec.get("_root") or ""),
                    "inst_epoch": float(rec.get("inst_epoch") or 0.0),
                })
            except Exception:
                continue
        block = _render_block(g, _ambient_cwd())
        return json.dumps({"ok": True, "sessions": rows,
                           "block_chars": len(block.strip())})
    except Exception as exc:
        return _err("%s: %s" % (type(exc).__name__, exc))


# ---------------------------------------------------------------------------
# Loadouts — reference-based named bundles of (id, enabled) flags
# File: <root>/.hermes/instinct-loadouts.json — atomic tmp + os.replace,
# try/except OSError (mirrors _save_instincts L330-338), under this lane's lock.
# v1 refuses unknown top-level shapes: {"loadouts": ...} only, never guess.
# ---------------------------------------------------------------------------

def _loadouts_path(root):
    return os.path.join(root, ".hermes", _LOADOUT_FILE)


def _read_loadouts(path):
    """(loadouts, error). Missing file -> ({}, None); unreadable/corrupt or an
    unknown shape -> (None, reason) — v1 refuses instead of guessing."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}, None
    except OSError:
        return {}, None
    except ValueError:
        return None, "corrupt loadout JSON (fix or delete by hand): %s" % path
    if not isinstance(data, dict) or "loadouts" not in data \
            or any(k != "loadouts" for k in data):
        return None, 'unknown loadout shape in %s: expected {"loadouts": {...}}' % path
    lds = data["loadouts"]
    if not isinstance(lds, dict):
        return None, 'unknown loadout shape in %s: "loadouts" must be an object' % path
    for name, entry in lds.items():
        if not isinstance(entry, dict) \
                or not isinstance(entry.get("created"), (int, float)) \
                or isinstance(entry.get("created"), bool) \
                or not isinstance(entry.get("instincts"), list):
            return None, 'malformed loadout "%s" in %s (need created + instincts[])' % (name, path)
        for ref in entry["instincts"]:
            if not isinstance(ref, dict) or not isinstance(ref.get("id"), str) \
                    or not isinstance(ref.get("enabled"), bool):
                return None, ('malformed loadout "%s" in %s: refs are '
                              '(id, enabled) pairs only') % (name, path)
    return lds, None


def _write_loadouts(path, lds):
    """Atomic write; returns False on OSError (swallowed, mirroring the core —
    callers confirm via refetch, spec §3.4)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"loadouts": lds}, fh, indent=1)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _loadout_target(arg, cmd, hint):
    """Shared arg parse: <repo_root> <name>. Returns (root, name, err_json)."""
    parts = _arg_str(arg).split()
    if len(parts) != 2:
        return None, None, _usage(cmd, hint)
    root, name = parts[0], parts[1]
    if not _LOADOUT_NAME_RE.match(name):
        return None, None, _err('invalid loadout name "%s" (allowed: [A-Za-z0-9._-]{1,40})' % name)
    if not os.path.isdir(root):
        return None, None, _err("repo_root is not a directory: %s" % root)
    return root, name, None


def _cmd_loadout_list(arg=""):
    """``<repo_root>`` -> {ok, loadouts: {name: {created, count, active,
    instincts: [{id, enabled}]}}} — refs included so the UI can diff and badge
    ACTIVE (exact (id,enabled) set match, spec §2.4) without a second command."""
    try:
        g = _discover()
        if not g:
            return _err("hermes-day core not reachable")
        parts = _arg_str(arg).split()
        if len(parts) != 1:
            return _usage("day-loadout-list", "<repo_root>")
        root = parts[0]
        if not os.path.isdir(root):
            return _err("repo_root is not a directory: %s" % root)
        lds, err = _read_loadouts(_loadouts_path(root))
        if err:
            return _err(err)
        cur = set()
        for i in _safe_load(g, root):
            cur.add((str(i.get("id")), bool(i.get("enabled", True))))
        out = {}
        for name, entry in lds.items():
            refs = [{"id": r["id"], "enabled": r["enabled"]}
                    for r in entry["instincts"]]
            want = set((r["id"], r["enabled"]) for r in refs)
            out[name] = {"created": entry["created"], "count": len(refs),
                         "active": want == cur, "instincts": refs}
        return json.dumps({"ok": True, "loadouts": out})
    except Exception as exc:
        return _err("%s: %s" % (type(exc).__name__, exc))


def _cmd_loadout_save(arg=""):
    """``<repo_root> <name>`` — snapshot the current (id, enabled) flags.
    Reference-based: ids and flags only, no copied reason text (spec §2.4)."""
    try:
        g = _discover()
        if not g:
            return _err("hermes-day core not reachable")
        root, name, err = _loadout_target(
            arg, "day-loadout-save", "<repo_root> <name>")
        if err:
            return err
        path = _loadouts_path(root)
        with _LOCK:
            lds, rerr = _read_loadouts(path)
            if rerr:
                return _err(rerr)          # never clobber an unknown shape
            replaced = name in lds
            refs = [{"id": str(i.get("id")),
                     "enabled": bool(i.get("enabled", True))}
                    for i in _safe_load(g, root)][-_MAX_INSTINCTS:]
            lds[name] = {"created": time.time(), "instincts": refs}
            _write_loadouts(path, lds)
        again, rerr2 = _read_loadouts(path)
        if rerr2 or not isinstance(again, dict) or name not in again:
            return _err("write failed (refetch shows no change)")
        entry = again[name]
        return json.dumps({"ok": True, "name": name,
                           "count": len(entry["instincts"]),
                           "created": entry["created"], "replaced": replaced})
    except Exception as exc:
        return _err("%s: %s" % (type(exc).__name__, exc))


def _cmd_loadout_apply(arg=""):
    """``<repo_root> <name>`` -> {ok, applied, missing, disabled}.

    applied = refs found in the ledger (n + k of the UI diff), disabled = those
    targeting off (k), missing = ids absent (m) — deleted instincts are never
    resurrected (spec §2.4). Flags flip under this lane's lock, the ledger is
    written via the core's _save_instincts (cache + atomic replace, L1241-1242)
    and verified by refetch."""
    try:
        g = _discover()
        if not g:
            return _err("hermes-day core not reachable")
        root, name, err = _loadout_target(
            arg, "day-loadout-apply", "<repo_root> <name>")
        if err:
            return err
        path = _loadouts_path(root)
        with _LOCK:
            lds, rerr = _read_loadouts(path)
            if rerr:
                return _err(rerr)
            if name not in lds:
                return _err('loadout "%s" not found in %s' % (name, path))
            refs = lds[name]["instincts"]
            items = _safe_load(g, root)
            by_id = dict((str(i.get("id")), i) for i in items)
            missing, applied, disabled, changed = [], 0, 0, 0
            for r in refs:
                item = by_id.get(r["id"])
                if item is None:
                    missing.append(r["id"])
                    continue
                applied += 1
                if not r["enabled"]:
                    disabled += 1
                if bool(item.get("enabled", True)) != r["enabled"]:
                    item["enabled"] = r["enabled"]
                    changed += 1
            if changed:
                save = g.get("_save_instincts")
                cache = g.get("_INSTINCT_CACHE")
                if not callable(save) or not isinstance(cache, dict):
                    return _err("core ledger writer not reachable")
                cache[root] = items
                save(root, items)
                again = dict((str(i.get("id")), i) for i in _safe_load(g, root))
                for r in refs:
                    it = again.get(r["id"])
                    if it is not None and bool(it.get("enabled", True)) != r["enabled"]:
                        return json.dumps({
                            "ok": False,
                            "error": "write failed (refetch shows unchanged flags)",
                            "applied": applied, "missing": missing,
                            "disabled": disabled})
        return json.dumps({"ok": True, "applied": applied,
                           "missing": missing, "disabled": disabled})
    except Exception as exc:
        return _err("%s: %s" % (type(exc).__name__, exc))


def _cmd_loadout_del(arg=""):
    """``<repo_root> <name>`` — remove a loadout (never touches the ledger)."""
    try:
        root, name, err = _loadout_target(
            arg, "day-loadout-del", "<repo_root> <name>")
        if err:
            return err
        path = _loadouts_path(root)
        with _LOCK:
            lds, rerr = _read_loadouts(path)
            if rerr:
                return _err(rerr)
            if name not in lds:
                return _err('loadout "%s" not found in %s' % (name, path))
            lds.pop(name, None)
            _write_loadouts(path, lds)
        again, rerr2 = _read_loadouts(path)
        if rerr2:
            return _err(rerr2)
        if name in (again or {}):
            return _err("write failed (refetch shows the loadout still present)")
        return json.dumps({"ok": True, "name": name, "removed": True})
    except Exception as exc:
        return _err("%s: %s" % (type(exc).__name__, exc))


# ---------------------------------------------------------------------------
# Registration — commands only, no hooks, no manifest edits
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    try:
        _discover()
    except Exception:
        pass
    specs = (
        ("day-instinct-preview", _cmd_instinct_preview,
         "[repo_root] [instinct_id reason <value...>]",
         "Byte-exact instincts system-prompt block: exactly what a new session gets."),
        ("day-instinct-edit", _cmd_instinct_edit,
         "<repo_root> <instinct_id> reason <value...>",
         "Edit an instinct reason (240 chars, one line). Lands NEXT session only."),
        ("day-instinct-status", _cmd_instinct_status, "",
         "Frozen-section status: sessions holding old bytes + ambient block size."),
        ("day-loadout-list", _cmd_loadout_list, "<repo_root>",
         "Named reference-based loadouts (id + enabled flags only) for a repo."),
        ("day-loadout-save", _cmd_loadout_save, "<repo_root> <name>",
         "Snapshot current (id, enabled) flags as a loadout."),
        ("day-loadout-apply", _cmd_loadout_apply, "<repo_root> <name>",
         "Apply a loadout: flip flags on existing ids, report missing ids."),
        ("day-loadout-del", _cmd_loadout_del, "<repo_root> <name>",
         "Delete a loadout."),
    )
    for name, fn, hint, desc in specs:
        try:
            ctx.register_command(name, fn, description=desc, args_hint=hint)
        except Exception:
            pass
