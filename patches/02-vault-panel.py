"""Lane 02 — Vault & Redaction Panel.

Commands (registered at plugin load — observer/commands only, NO hooks):
  * ``day-vault``   — read: per-session credential-exposure ledger (labels only)
  * ``day-redact``  — in-place scrub of ONE session record — the session key
                      is REQUIRED (a missing key is a usage error, never a
                      silent mutate-everything), scope-limited via
                      ``[attn|runs|receipt|instincts|all] [index]``, with a
                      label-only audit row appended per call; reuses the
                      plugin's four real regexes (_PRIVATE_KEY_RE,
                      _SECRET_SIGNS_RE, _BEARER_RE, _JWT_RE)
  * ``day-rotate``  — STATIC rotation guidance; this module NEVER executes
                      shell, subprocess, or any provider API call.

=============================================================================
SAFETY INVARIANT — read before changing anything below:

    ONLY LABELS ARE EVER PERSISTED, NEVER SECRET BYTES.

A matched credential ("sk-ant-…", a bearer token, a JWT, a PEM body) may
exist in memory only as the *input* to ``_scrub``; it must never reach
state, JSON output, or a log line. The design makes that hard to violate:

  1. ``_scrub()`` is the single choke point. It returns
     ``(clean_text, {label: count})`` — never a match object, never a
     captured group, never a span. Callers physically cannot obtain the
     matched bytes from its return value.
  2. Everything STORED is redacted FIRST: ``/day-redact`` rewrites strings
     in place (redact-before-store — the original is overwritten and
     dropped, not copied anywhere) and only then calls the host's
     ``_persist()``.
  3. Everything RETURNED is sealed: ``_seal()`` runs the same four regexes
     over the serialized JSON, so even an accidentally-included secret
     leaves as ``[REDACTED:<label>]`` before it is ever handed back.
  4. No print/log/traceback in this module includes record text; failures
     report only the exception type name.
  5. Audit rows (``day-redact`` → ``rec["redactions"]``) carry only ``ts``,
     ``scope``, label names and replacement COUNTS — never matched text.

Honesty rule (gatemark 4): a missing or failed probe is reported as null /
unavailable, never as a confident ``0``. A missing timestamp leaves this
module as ``ts: null`` (never epoch ``0.0``); the panel likewise renders an
unavailable state instead of ``0/40``.

Labels used (identical to the host's ``_secret_signs``): "private-key
block", "api-key", "bearer header", "jwt".
=============================================================================
"""

import datetime
import json
import sys
import threading
import time
from typing import Any, Dict, Optional, Tuple

# Own module-level lock for this lane (never import the plugin _LOCK).
VAULT_LOCK = threading.RLock()

# The live host module's globals must expose all four real regexes plus the
# session store before this lane will touch any state.
_HOST_MARKERS = (
    "_SECRET_SIGNS_RE",
    "_PRIVATE_KEY_RE",
    "_BEARER_RE",
    "_JWT_RE",
    "_SESSIONS",
)

_HOST: Optional[Dict[str, Any]] = None  # cached host globals (fail-soft)

_MAX_SESSIONS_OUT = 20        # sessions returned by day-vault
_MAX_EXPOSURES_OUT = 40       # exposures per session (== the attn window)
_REDACT_FMT = "[REDACTED:%s]"  # placeholder carries a LABEL, never bytes

# day-redact contract (spec 3.2): <session_key> [scope] [index]. A missing
# session key is a USAGE ERROR — never a silent "mutate every session".
_REDACT_SCOPES = ("attn", "runs", "receipt", "instincts", "all")
_REDACT_USAGE = ("usage: day-redact <session_key> "
                 "[attn|runs|receipt|instincts|all] [index]")
_MAX_REDACTIONS = 40          # audit rows kept per session (labels only)
_TEXT_BOUND = 300             # exposure brief bound, mirrors host _brief()


# ---------------------------------------------------------------------------
# Host resolution — patches are loaded by path, so the host module is found
# structurally, never imported (and _LOCK in particular is never imported).
# ---------------------------------------------------------------------------


def _resolve_host() -> Optional[Dict[str, Any]]:
    """Return the live host module's globals dict, or None (degrade).

    Two fail-soft strategies:
      1. walk the caller frames — ``patches.register_all()`` is invoked from
        the host's own ``register()``, so the markers are a few frames up.
        This also works under the test harness, which execs the host
        without ever inserting it into ``sys.modules``.
      2. scan ``sys.modules`` for an imported copy carrying the markers.
    """
    global _HOST
    if _HOST is not None:
        return _HOST
    try:
        frame = sys._getframe(1)
        depth = 0
        while frame is not None and depth < 20:
            g = frame.f_globals
            if isinstance(g, dict) and all(k in g for k in _HOST_MARKERS):
                _HOST = g
                return _HOST
            frame = frame.f_back
            depth += 1
    except Exception:
        pass
    try:
        for mod in tuple(sys.modules.values()):
            g = getattr(mod, "__dict__", None)
            if isinstance(g, dict) and all(k in g for k in _HOST_MARKERS):
                _HOST = g
                return _HOST
    except Exception:
        pass
    return None


def _sid_match(key: str, sid: str) -> bool:
    """Mirror the host's exact/suffix/substring session-key lookup."""
    if not sid:
        return True
    return bool(key) and (key == sid or key.endswith(sid) or sid in key)


def _iso(ts: Optional[float]) -> Optional[str]:
    """RFC3339-UTC rendering of *ts*, else None.

    A missing / non-numeric timestamp stays ``None`` end to end — it must
    never become epoch 0.0 (gatemark 4: a failed probe is not a value).
    """
    if ts is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(
            float(ts), datetime.timezone.utc
        ).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Redaction choke point — the ONLY place matched bytes exist past the regexes
# ---------------------------------------------------------------------------


def _scrub(text: str, host: Dict[str, Any],
           extend_pem: bool = True) -> Tuple[str, Dict[str, int]]:
    """Redact credential-shaped spans from *text* using the host's regexes.

    Returns ``(clean_text, {label: count})``. SAFETY: by construction no
    match object, group, or span leaves this function — only the rewritten
    string and label counts — so callers cannot leak the bytes by accident.
    """
    if not isinstance(text, str) or not text:
        return text, {}
    counts: Dict[str, int] = {}

    def bump(label: str, n: int = 1) -> None:
        counts[label] = counts.get(label, 0) + n

    out = text
    # 1) private-key blocks: the host regex flags the BEGIN line; extend the
    #    cut through the matching END marker so the key BODY leaves too.
    try:
        pk_re = host["_PRIVATE_KEY_RE"]
        while True:
            m = pk_re.search(out)
            if not m:
                break
            placeholder = _REDACT_FMT % "private-key block"
            if not extend_pem:
                # serialized-JSON context: replace exactly the match so the
                # surrounding structure stays intact (defense-in-depth pass)
                out = out[:m.start()] + placeholder + out[m.end():]
                bump("private-key block")
                continue
            end_at = out.find("-----END", m.end())
            stop = None
            if end_at != -1:
                nl = out.find("\n", end_at)
                stop = (nl + 1) if nl != -1 else len(out)
            if stop is None:
                nl = out.find("\n", m.end())
                if nl != -1:
                    stop = nl
                else:
                    esc = out.find("\\n", m.end())  # escaped newline in text
                    stop = (esc + 2) if esc != -1 else len(out)
            # over-redaction is safe, under-redaction is not: each iteration
            # always consumes the match, so this loop always terminates.
            out = out[:m.start()] + placeholder + out[stop:]
            bump("private-key block")
    except Exception:
        pass

    for key, label in (
        ("_SECRET_SIGNS_RE", "api-key"),
        ("_BEARER_RE", "bearer header"),
        ("_JWT_RE", "jwt"),
    ):
        try:
            out, n = host[key].subn(_REDACT_FMT % label, out)
            if n:
                bump(label, n)
        except Exception:
            pass
    return out, counts


def _seal(payload: Dict[str, Any], host: Optional[Dict[str, Any]]) -> str:
    """Serialize command output THROUGH the scrubber (belt and braces).

    Even if a payload somehow carried a secret, it leaves this function as
    ``[REDACTED:<label>]`` — sealed before it is ever returned.
    """
    try:
        text = json.dumps(payload, default=str)
    except Exception:
        text = json.dumps({"ok": False, "error": "unserializable-output"})
    if host:
        try:
            text, _ = _scrub(text, host, extend_pem=False)
        except Exception:
            pass
    return text


def _clean_str(text: str, host: Dict[str, Any], stats: Dict[str, Any]) -> str:
    """Scrub one stored string and tally LABELS ONLY into *stats*."""
    clean, counts = _scrub(text, host, extend_pem=True)
    stats["scanned"] += 1
    if clean != text:
        stats["changed"] += 1
        for label, n in counts.items():
            stats["labels"][label] = stats["labels"].get(label, 0) + n
    return clean


def _scrub_value(node: Any, host: Dict[str, Any],
                 stats: Dict[str, Any]) -> Any:
    """Redact every string VALUE reachable from *node*, redact-before-store.

    Dicts and lists are mutated in place: the clean string OVERWRITES the
    original before ``_persist()`` is ever called, and the original bytes
    are dropped — never copied to another field, buffer or log. Strings come
    back cleaned (callers assign the result). Dict keys are fixed structural
    labels written by the host and are never scrubbed or reported.
    """
    if isinstance(node, str):
        return _clean_str(node, host, stats)
    if isinstance(node, dict):
        for key in list(node.keys()):
            node[key] = _scrub_value(node[key], host, stats)
        return node
    if isinstance(node, list):
        for i in range(len(node)):
            node[i] = _scrub_value(node[i], host, stats)
        return node
    return node


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _cmd_vault(arg: str = "") -> str:
    """``[session_key]`` — read the exposure ledger as JSON (labels only)."""
    try:
        host = _resolve_host()
        if host is None:
            return _seal({"ok": False, "error": "host-state-unavailable"}, None)
        sid = (arg or "").strip()
        try:
            window = int(host.get("_MAX_ATTN") or 40)
        except Exception:
            window = 40
        rows = []
        with VAULT_LOCK:
            try:
                snapshot = dict(host["_SESSIONS"])
            except Exception:
                snapshot = {}
            for key, rec in snapshot.items():
                if not isinstance(rec, dict):
                    continue
                if sid and not _sid_match(key, sid):
                    continue
                attn = rec.get("attn")
                attn = list(attn) if isinstance(attn, list) else []
                exposures = []
                for idx, item in enumerate(attn):
                    if not isinstance(item, dict):
                        continue
                    if item.get("kind") != "secret_exposure":
                        continue
                    # signs[] are the host's four labels — never bytes
                    signs = [str(s) for s in (item.get("signs") or [])]
                    raw_ts = item.get("ts")
                    # null, never 0.0: a missing/failed timestamp probe must
                    # not render as 1970 (gatemark 4 — no confident zero)
                    ts = (raw_ts
                          if isinstance(raw_ts, (int, float))
                          and not isinstance(raw_ts, bool)
                          else None)
                    brief = str(item.get("text") or "")
                    if len(brief) > _TEXT_BOUND:
                        brief = brief[:_TEXT_BOUND]
                    exposures.append({
                        "tool": str(item.get("tool") or ""),
                        "ts": ts,
                        "iso": _iso(ts),
                        "signs": signs,
                        "text": brief,
                        "index": idx,      # stable handle for a row redact
                        "redacted": bool(item.get("redacted")),
                    })
                if sid or exposures:
                    rows.append((key, exposures, len(attn)))
        rows.sort(
            key=lambda r: (
                max([e["ts"] for e in r[1] if e["ts"] is not None] or [0.0]),
                r[0]),
            reverse=True,
        )
        sessions = [
            {
                "session": key,
                "attn_window": n_attn,
                "in_window": len(exps),
                "exposures": exps[:_MAX_EXPOSURES_OUT],
            }
            for key, exps, n_attn in rows[:_MAX_SESSIONS_OUT]
        ]
        payload = {
            "ok": True,
            "window": window,
            "window_note": (
                "counts are secret_exposure entries currently inside the "
                "per-session _MAX_ATTN=%d attention ledger — a live window, "
                "not lifetime exposure" % window
            ),
            "sessions": sessions,
            "totals": {
                "sessions_with_exposure": sum(1 for r in rows if r[1]),
                "in_window": sum(len(r[1]) for r in rows),
            },
        }
        return _seal(payload, host)
    except Exception as exc:  # never surface record text — type name only
        return _seal({"ok": False, "error": type(exc).__name__}, None)


# ---------------------------------------------------------------------------
# Redaction helpers — scope targeting, audit rows, instinct ledgers
# ---------------------------------------------------------------------------


def _audit_row(host: Dict[str, Any], rec: Dict[str, Any],
               row: Dict[str, Any]) -> None:
    """Append one label-only audit row to ``rec['redactions']`` (cap 40).

    Row shape (spec 3.2): ``{ts, scope, signs, replacements}`` — ``signs``
    is a sorted list of LABEL names and ``replacements`` a COUNT. No matched
    text can enter this structure.
    """
    try:
        lst = rec.get("redactions")
        if not isinstance(lst, list):
            lst = []
            rec["redactions"] = lst
        ap = host.get("_append")
        if callable(ap):
            try:
                ap(lst, row, _MAX_REDACTIONS)  # host: _append(lst, item, cap)
                return
            except Exception:
                pass
        lst.append(row)
        del lst[: max(0, len(lst) - _MAX_REDACTIONS)]
    except Exception:
        pass


def _redact_instincts(host: Dict[str, Any], rec: Dict[str, Any],
                      stats: Dict[str, Any]) -> None:
    """Scrub this session's repo instinct ledger (spec 3.3 step 3).

    The empty-``_root`` guard mirrors ``_digest_tests``: no root → no read,
    no write. Only the host's own load/save path is used, and the file is
    rewritten ONLY when the scrub actually changed something. The host's
    in-memory cache is refreshed alongside so a stale copy can never
    re-save the bytes later. Labels in, labels out.
    """
    root = rec.get("_root")
    if not isinstance(root, str) or not root.strip():
        return
    load = host.get("_load_instincts")
    save = host.get("_save_instincts")
    if not callable(load) or not callable(save):
        return
    try:
        items = load(root)
    except Exception:
        return
    if not isinstance(items, list) or not items:
        return
    try:
        before = json.dumps(items, default=str)
    except Exception:
        return
    _scrub_value(items, host, stats)   # in place — redact before store
    try:
        after = json.dumps(items, default=str)
    except Exception:
        return
    if after == before:
        return                          # nothing changed → never rewrite
    try:
        save(root, items)
        cache = host.get("_INSTINCT_CACHE")
        if isinstance(cache, dict):
            cache[root] = items         # never let a stale copy re-save bytes
    except Exception:
        pass


def _redact_in_scope(rec: Dict[str, Any], scope: str, idx: Optional[int],
                     host: Dict[str, Any], stats: Dict[str, Any]) -> str:
    """Scrub only the in-scope subtree, in place. Returns '' or an error code.

    Ordering is always redact-FIRST: every in-scope string is rewritten here
    before the caller is allowed to call ``_persist()``.
    """
    if scope == "instincts":
        _redact_instincts(host, rec, stats)
        return ""
    if scope == "attn":
        attn = rec.get("attn")
        attn = attn if isinstance(attn, list) else []
        if idx is not None:
            if idx < 0 or idx >= len(attn) or not isinstance(attn[idx], dict):
                return "index-out-of-range"
            target: Any = [attn[idx]]   # same dict object → mutates in place
            mark = [attn[idx]]
        else:
            target = attn
            mark = [i for i in attn if isinstance(i, dict)]
        _scrub_value(target, host, stats)
        for item in mark:
            if item.get("kind") == "secret_exposure":
                item["redacted"] = True  # state marker, not proof of anything
        return ""
    if scope == "runs":
        target = rec.get("runs")
    elif scope == "receipt":
        target = rec.get("receipt")
    else:  # "all"
        target = rec
    if target is None:
        return ""
    _scrub_value(target, host, stats)
    if scope == "all":
        attn = rec.get("attn")
        if isinstance(attn, list):
            for item in attn:
                if (isinstance(item, dict)
                        and item.get("kind") == "secret_exposure"):
                    item["redacted"] = True
    return ""


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _cmd_redact(arg: str = "") -> str:
    """``<session_key> [attn|runs|receipt|instincts|all] [index]`` — scrub.

    Contract (spec 3.2): the session key is REQUIRED — a missing or unknown
    key is a usage error, never a silent mutation of every session. Scope
    defaults to ``all``; an explicit ``index`` narrows the scrub to that one
    ``attn`` row (``day-redact <key> 7`` ⇒ ``<key> attn 7``). Every
    successful call appends a label-only audit row to ``rec['redactions']``.
    Redact-before-store: strings are rewritten in place FIRST, only then is
    ``_persist()`` called. Output carries labels and counts — never a byte.
    """
    try:
        host = _resolve_host()
        if host is None:
            return _seal({"ok": False, "error": "host-state-unavailable"}, None)
        parts = (arg or "").split()
        sid = parts[0].strip() if parts else ""
        scope = "all"
        idx = None
        for tok in parts[1:]:
            if tok in _REDACT_SCOPES and idx is None and scope == "all":
                scope = tok
            elif tok.isdigit() and idx is None:
                idx = int(tok)
            else:
                return _seal({"ok": False, "error": _REDACT_USAGE,
                              "token": str(tok)[:40]}, host)
        if not sid:
            return _seal({"ok": False, "error": _REDACT_USAGE}, host)
        if idx is not None and scope == "all":
            scope = "attn"      # a bare row index addresses an attn entry
        if idx is not None and scope != "attn":
            return _seal({"ok": False, "error": _REDACT_USAGE,
                          "note": "index applies to the attn scope only"},
                         host)
        results = []
        with VAULT_LOCK:
            try:
                items = [(k, r) for k, r in host["_SESSIONS"].items()
                         if isinstance(r, dict) and _sid_match(k, sid)]
            except Exception:
                items = []
            if not items:
                return _seal({"ok": False, "error": "no-session-record",
                              "session": sid}, host)
            for key, rec in items:
                stats = {"scanned": 0, "changed": 0, "labels": {}}
                err = _redact_in_scope(rec, scope, idx, host, stats)
                if err:
                    results.append({"session": key, "scope": scope,
                                    "error": err})
                    continue
                # audit row — labels and counts ONLY (spec 3.2 step 4)
                _audit_row(host, rec, {
                    "ts": time.time(),
                    "scope": scope,
                    "signs": sorted(stats["labels"]),
                    "replacements": stats["changed"],
                })
                results.append({
                    "session": key,
                    "scope": scope,
                    "strings": stats["scanned"],
                    "changed": stats["changed"],
                    "scrubbed": stats["labels"],
                })
            # redact-before-store: every in-scope record is already clean —
            # only now hand it to the host's persistence path.
            try:
                host["_persist"]()
            except Exception:
                pass
        payload = {
            "ok": True,
            "scope": scope,
            "sessions": results,
            "note": ("scrubbed with the plugin's four credential regexes; "
                     "only labels, counts and audit rows are reported — "
                     "matched bytes are dropped in place, never stored, "
                     "logged or returned"),
        }
        return _seal(payload, host)
    except Exception as exc:
        return _seal({"ok": False, "error": type(exc).__name__}, None)


# Static guidance only — this module never executes shell for rotation.
_ROTATE_BY_SIGN = (
    {
        "sign": "api-key",
        "why": "a provider token appeared in a tool result",
        "candidates": [
            {"env": "OPENAI_API_KEY (or your provider's *_API_KEY)",
             "file": "process env / ~/.zshrc / repo .env",
             "change": "revoke in the provider console, then restart "
                       "the session",
             "verify": "test -n \"$OPENAI_API_KEY\" && echo set",
             "unverified": True},
        ],
        "steps": [
            "revoke the leaked key in the provider console FIRST — an old "
            "copy keeps working until you do",
            "mint a replacement scoped to the least privilege you need",
            "store it in the Hermes vault / environment — never in a file, "
            "command line, or chat",
            "re-run the call once to confirm the new key works, then delete "
            "the old entry",
        ],
    },
    {
        "sign": "private-key block",
        "why": "a private key's BEGIN block appeared in output",
        "candidates": [
            {"env": "(none — the key lives in a file)",
             "file": "the leaked key path from this session's files list "
                     "(never guessed here)",
             "change": "publish the new public key everywhere, then destroy "
                       "the old private key",
             "verify": "test -f \"$KEY_PATH\" && ssh-keygen -l -f "
                       "\"$KEY_PATH\"",
             "unverified": True},
        ],
        "steps": [
            "generate a NEW keypair on a machine that never saw the leak",
            "publish the new public key everywhere the old one is trusted "
            "(git host, servers, CI)",
            "remove the old public key, THEN destroy the old private key",
            "if it was committed, scrub history only AFTER the new key is "
            "in place — rotation first, history second",
        ],
    },
    {
        "sign": "bearer header",
        "why": "an Authorization: Bearer token was echoed",
        "candidates": [
            {"env": "(none — sent per request, not stored in env)",
             "file": "provider console → active sessions / tokens",
             "change": "invalidate the session server-side, then fetch a "
                       "fresh token through the Hermes vault",
             "verify": "echo run the provider status CLI, e.g. gh auth "
                       "status, and confirm the old session is revoked",
             "unverified": True},
        ],
        "steps": [
            "log out / invalidate that session server-side — bearer tokens "
            "stay valid until revoked",
            "fetch a fresh token through the Hermes vault, not by hand",
            "check the provider's session/audit list for other live copies",
        ],
    },
    {
        "sign": "jwt",
        "why": "a JWT appeared in output",
        "candidates": [
            {"env": "(none — session token, not an env var)",
             "file": "provider console → sessions / signing secret",
             "change": "end the session the JWT asserts, or rotate the "
                       "signing secret behind it",
             "verify": "echo run the provider status CLI and confirm that "
                       "session no longer resolves",
             "unverified": True},
        ],
        "steps": [
            "rotating a JWT means ending the session it asserts, or "
            "rotating the signing secret behind it",
            "re-issue the session for the legitimate consumer",
            "shorten expiry / enable revocation so the next leak expires",
        ],
    },
)


def _cmd_rotate(arg: str = "") -> str:
    """``[sign]`` — STATIC guidance JSON. NEVER executes shell — by design."""
    try:
        want = (arg or "").strip().lower()
        if want:
            groups = tuple(g for g in _ROTATE_BY_SIGN if want in g["sign"])
            if not groups:
                return _seal({
                    "ok": False,
                    "error": "unknown-sign",
                    "known": [g["sign"] for g in _ROTATE_BY_SIGN],
                }, _resolve_host())
        else:
            groups = _ROTATE_BY_SIGN
        payload = {
            "ok": True,
            "executed": False,
            "action": "rotate",
            "note": ("static guidance only — hermes-day never executes shell "
                     "for rotation; run these steps yourself, in the "
                     "provider's console or CLI. Every candidate carries a "
                     "copyable verify snippet marked unverified — nothing "
                     "here has been run for you"),
            "general": [
                "assume the exposed credential is already public: rotate it, "
                "do not merely delete it",
                "revoke before you replace, so the leaked copy dies first",
                "update every consumer (Hermes vault, .env files, CI secrets) "
                "to the new value",
                "check the provider's audit log for use of the leaked "
                "credential since it appeared",
            ],
            "by_sign": list(groups),
        }
        return _seal(payload, _resolve_host())
    except Exception as exc:
        return _seal({"ok": False, "error": type(exc).__name__}, None)


def register(ctx: Any) -> None:
    """Wire the three commands. COMMANDS ONLY — no hooks, no manifest edits.

    Every step is individually guarded: a bug here degrades to "that
    feature is missing", never a plugin crash.
    """
    try:
        _resolve_host()
    except Exception:
        pass
    for name, fn, desc, hint in (
        ("day-vault", _cmd_vault,
         "Credential-exposure ledger as JSON (labels only; counts are a "
         "_MAX_ATTN window, not lifetime).",
         "[session_key]"),
        ("day-redact", _cmd_redact,
         "Scrub credential-shaped strings from a session's stored records "
         "in place, scope-limited and audit-logged (labels only; never "
         "stores secret bytes).",
         "<session_key> [attn|runs|receipt|instincts|all] [index]"),
        ("day-rotate", _cmd_rotate,
         "STATIC credential-rotation guidance as JSON — never executes "
         "shell.",
         "[sign]"),
    ):
        try:
            ctx.register_command(name, fn, description=desc, args_hint=hint)
        except Exception:
            pass
