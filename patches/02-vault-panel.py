"""Lane 02 — Vault & Redaction Panel.

Commands (registered at plugin load — observer/commands only, NO hooks):
  * ``day-vault``   — read: per-session credential-exposure ledger (labels only)
  * ``day-redact``  — in-place scrub of stored session records, reusing the
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

Labels used (identical to the host's ``_secret_signs``): "private-key
block", "api-key", "bearer header", "jwt".
=============================================================================
"""

import json
import sys
import threading
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
                for item in attn:
                    if not isinstance(item, dict):
                        continue
                    if item.get("kind") != "secret_exposure":
                        continue
                    # signs[] are the host's four labels — never bytes
                    signs = [str(s) for s in (item.get("signs") or [])]
                    try:
                        ts = float(item.get("ts") or 0.0)
                    except Exception:
                        ts = 0.0
                    exposures.append({
                        "tool": str(item.get("tool") or ""),
                        "ts": ts,
                        "signs": signs,
                    })
                if sid or exposures:
                    rows.append((key, exposures, len(attn)))
        rows.sort(
            key=lambda r: (max([e["ts"] for e in r[1]] or [0.0]), r[0]),
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


def _cmd_redact(arg: str = "") -> str:
    """``[session_key]`` — in-place scrub; empty arg targets every session.

    Reuses the plugin's four real regexes. Redact-before-store: strings are
    rewritten in place FIRST, only then is the host's ``_persist()`` called.
    Output carries labels and counts — never a matched byte.
    """
    try:
        host = _resolve_host()
        if host is None:
            return _seal({"ok": False, "error": "host-state-unavailable"}, None)
        sid = (arg or "").strip()
        results = []
        with VAULT_LOCK:
            try:
                items = [(k, r) for k, r in host["_SESSIONS"].items()
                         if isinstance(r, dict)]
            except Exception:
                items = []
            if sid:
                items = [(k, r) for k, r in items if _sid_match(k, sid)]
                if not items:
                    return _seal({"ok": False, "error": "no-session-record",
                                  "session": sid}, host)
            for key, rec in items:
                stats = {"scanned": 0, "changed": 0, "labels": {}}
                _scrub_value(rec, host, stats)  # in place, redact first
                results.append({
                    "session": key,
                    "strings": stats["scanned"],
                    "changed": stats["changed"],
                    "scrubbed": stats["labels"],
                })
            # redact-before-store: the records are already clean above —
            # only now hand them to the host's persistence path.
            try:
                host["_persist"]()
            except Exception:
                pass
        payload = {
            "ok": True,
            "sessions": results,
            "note": ("scrubbed with the plugin's four credential regexes; "
                     "only labels and counts are reported — matched bytes "
                     "are dropped in place, never stored, logged or returned"),
        }
        return _seal(payload, host)
    except Exception as exc:
        return _seal({"ok": False, "error": type(exc).__name__}, None)


# Static guidance only — this module never executes shell for rotation.
_ROTATE_BY_SIGN = (
    {
        "sign": "api-key",
        "why": "a provider token appeared in a tool result",
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
                     "provider's console or CLI"),
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
         "Scrub credential-shaped strings from stored session records "
         "in place (labels only; never stores secret bytes).",
         "[session_key]"),
        ("day-rotate", _cmd_rotate,
         "STATIC credential-rotation guidance as JSON — never executes "
         "shell.",
         "[sign]"),
    ):
        try:
            ctx.register_command(name, fn, description=desc, args_hint=hint)
        except Exception:
            pass
