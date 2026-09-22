"""Lane 09 — Escalation Matrix (commands only, observer, never throws).

Built strictly on mechanisms that exist in the real source:

  * ``_ATTN_KINDS`` — exactly the six kinds, nothing more.
  * Attention items written by ``_attn_add``: ``{kind, text, ts, ...extras}``.
  * Approval ledger entries ``{ts, choice, ms}`` — ``ms == -1`` means still
    unanswered; ``ms >= 0`` is a real human latency sample. The spec-named
    real samples 400 / 1100 / 6200 ms define the three latency_factor bands.
  * Persistence mirrors ``_persist()`` exactly: ``ctx.state.set("escalation",
    blob)`` inside ``try/except`` — a save can never throw.
  * Delivery mirrors the desktop's guarded call: every notify sits inside
    ``try/except``, body capped at 140 chars, and a failed delivery never
    mutates the underlying ``rec["attn"]`` ledger (the ledger is evidence,
    the matrix is only routing).

Attention debt (spec §5):
    latency_factor(ms):  ms < 0     -> (now - ts)/1000 pending age, linear
                          0..1100    -> 1.0        (samples 400, 1100)
                          1100..6200 -> 1.0 + 0.3*(ms-1100)/5100
                          >= 6200    -> 1.3        (sample 6200)
    debt_item = sev_w[kind] * (1 + unack_age_min / 5) * latency_factor
    session_debt = SUM over open coalesced rows (count multiplies: x count)

Coalescing: one row per ``(kind, session, fingerprint)`` key — a flood
collapses into that single row with an incremented count, never one row per
event. MAX_GROUPS = 40 (== _MAX_ATTN); overflow folds into one ``+N more``
row so nothing is ever dropped silently.
"""

from __future__ import annotations

import json
import threading
import time

# Own module-level lock for this lane — never the plugin's _LOCK.
ESC_LOCK = threading.RLock()

_CTX = None
_STATE_KEY = "escalation"          # sibling of _STATE_KEY / _INDEX_KEY

_ATTN_KINDS = (
    "secret_exposure",
    "test_tamper",
    "approval_stall",
    "subagent_failed",
    "provider_error",
    "interrupted",
)

_MAX_GROUPS = 40                   # == _MAX_ATTN (per-session mirror cap)
_BODY_CAP = 140                    # same notify body slice as the desktop
_MS_BANDS = (400, 1100, 6200)      # real approval-ledger samples (spec §0/§5)
_DEFAULT_QUIET = (22 * 60, 7 * 60)  # 22:00–07:00 local (spec §4)
_DELIVERIES = ("silent", "badge", "notify")
_QUIET_CHOICES = ("never", "badge", "silent")

_SEV_W = {
    "secret_exposure": 5.0,
    "test_tamper": 5.0,
    "approval_stall": 3.0,
    "subagent_failed": 2.0,
    "provider_error": 1.0,
    "interrupted": 0.5,
}
_SEV_LABEL = {
    "secret_exposure": "P0",
    "test_tamper": "P0",
    "approval_stall": "P1",
    "subagent_failed": "P1",
    "provider_error": "P2",
    "interrupted": "P3",
}


def _lad(*pairs):
    return [[int(m), str(a)] for m, a in pairs]


# Per-kind routing defaults — spec §1 table (delivery, coalesce window,
# quiet-hours suppression, escalation ladder in minutes unacked).
_MATRIX_DEFAULT = {
    "secret_exposure": {
        "sev": "P0", "delivery": "notify", "coalesce_s": 60, "quiet": "never",
        "escalate_after": None,
        "ladder": _lad((0, "notify"), (2, "renotify"), (5, "pin"),
                       (10, "notify-critical")),
    },
    "test_tamper": {
        "sev": "P0", "delivery": "notify", "coalesce_s": 60, "quiet": "never",
        "escalate_after": None,
        "ladder": _lad((0, "notify"), (3, "renotify"), (10, "pin")),
    },
    "approval_stall": {
        "sev": "P1", "delivery": "badge", "coalesce_s": 300, "quiet": "badge",
        "escalate_after": 2,
        "ladder": _lad((0, "badge"), (2, "notify"), (10, "renotify"),
                       (30, "notify")),
    },
    "subagent_failed": {
        "sev": "P1", "delivery": "badge", "coalesce_s": 300, "quiet": "badge",
        "escalate_after": 5,
        "ladder": _lad((0, "badge"), (5, "notify"), (15, "renotify")),
    },
    "provider_error": {
        "sev": "P2", "delivery": "badge", "coalesce_s": 600, "quiet": "silent",
        "escalate_after": None,
        "ladder": _lad((0, "badge"), (10, "notify"), (30, "badge")),
    },
    "interrupted": {
        "sev": "P3", "delivery": "silent", "coalesce_s": 600, "quiet": "silent",
        "escalate_after": None,
        "ladder": [],
    },
}

# Flood mirror for live/observed events: coalesce key -> single row.
_OBSERVED = {}
# In-memory notification bookkeeping (same idea as the desktop's `notified` set).
_NOTIFIED = {}
# Fallback persistence when ctx has no state store.
_MEM_ESC = {"matrix": {}, "debt": {}, "acked": {}}


def _now():
    try:
        return time.time()
    except Exception:
        return 0.0


# ---------------------------------------------------------------- state


def _state_store():
    try:
        st = getattr(_CTX, "state", None)
        if st is not None and hasattr(st, "get"):
            return st
    except Exception:
        pass
    return None


def _load_esc():
    """Read {"matrix","debt","acked"} — memory overlay, never raises."""
    with ESC_LOCK:
        blob = None
        st = _state_store()
        if st is not None:
            try:
                blob = st.get(_STATE_KEY)
            except Exception:
                blob = None
        if not isinstance(blob, dict):
            blob = _MEM_ESC
        matrix = blob.get("matrix")
        debt = blob.get("debt")
        acked = blob.get("acked")
        return (
            dict(matrix) if isinstance(matrix, dict) else {},
            dict(debt) if isinstance(debt, dict) else {},
            dict(acked) if isinstance(acked, dict) else {},
        )


def _persist_esc(matrix, debt, acked):
    """Mirror of _persist(): ctx.state.set inside try/except. Never raises."""
    with ESC_LOCK:
        blob = {
            "matrix": dict(matrix or {}),
            "debt": dict(debt or {}),
            "acked": dict(acked or {}),
        }
        try:
            _MEM_ESC.clear()
            _MEM_ESC.update(blob)
        except Exception:
            pass
        st = _state_store()
        if st is None:
            return False
        try:
            st.set(_STATE_KEY, blob)
            return True
        except Exception:
            return False


# -------------------------------------------------------------- matrix


def matrix_get():
    """Stored matrix merged over the defaults for exactly the six kinds."""
    with ESC_LOCK:
        try:
            stored, _debt, _acked = _load_esc()
        except Exception:
            stored = {}
        out = {}
        for kind in _ATTN_KINDS:
            entry = dict(_MATRIX_DEFAULT[kind])
            override = stored.get(kind)
            if isinstance(override, dict):
                for field, value in override.items():
                    if field in entry:
                        entry[field] = value
            out[kind] = entry
        return out


def matrix_set(kind, field, value):
    """Update one routing cell. Returns (ok, payload-or-error). Never raises."""
    try:
        if kind not in _ATTN_KINDS:
            return False, "unknown kind: %s (kinds: %s)" % (
                kind, ", ".join(_ATTN_KINDS))
        with ESC_LOCK:
            stored, debt, acked = _load_esc()
            entry = dict(stored.get(kind) or {})
            entry.setdefault("sev", _SEV_LABEL[kind])
            entry.setdefault("delivery", _MATRIX_DEFAULT[kind]["delivery"])
            entry.setdefault("coalesce_s", _MATRIX_DEFAULT[kind]["coalesce_s"])
            entry.setdefault("quiet", _MATRIX_DEFAULT[kind]["quiet"])
            entry.setdefault("escalate_after",
                             _MATRIX_DEFAULT[kind]["escalate_after"])
            entry.setdefault("ladder", _MATRIX_DEFAULT[kind]["ladder"])
            if field == "delivery":
                if str(value) not in _DELIVERIES:
                    return False, "delivery must be silent|badge|notify"
                entry["delivery"] = str(value)
            elif field in ("coalesce", "coalesce_s"):
                seconds = int(value)
                if seconds < 0:
                    return False, "coalesce must be seconds (0 = never)"
                entry["coalesce_s"] = seconds
            elif field == "quiet":
                if str(value) not in _QUIET_CHOICES:
                    return False, "quiet must be never|badge|silent"
                entry["quiet"] = str(value)
            elif field in ("escalate", "escalate_after"):
                entry["escalate_after"] = None if value in (None, "", "none") \
                    else int(value)
            elif field == "ladder":
                ladder = _parse_ladder(value)
                if ladder is None:
                    return False, ("ladder format: 0->badge,2->notify,"
                                   "10->renotify")
                entry["ladder"] = ladder
            else:
                return False, "unknown field: %s" % field
            stored[kind] = entry
            _persist_esc(stored, debt, acked)
            return True, entry
    except Exception as exc:
        return False, "day-matrix-set failed: %s" % exc


def reset_matrix():
    """Restore the spec-default routing table. Never raises."""
    try:
        with ESC_LOCK:
            _stored, debt, acked = _load_esc()
            fresh = {}
            for kind in _ATTN_KINDS:
                fresh[kind] = json.loads(json.dumps(_MATRIX_DEFAULT[kind]))
            _persist_esc(fresh, debt, acked)
            return fresh
    except Exception:
        return {}


def _parse_ladder(value):
    try:
        if isinstance(value, (list, tuple)):
            raw_parts = list(value)
            out = []
            for part in raw_parts:
                if isinstance(part, (list, tuple)) and len(part) == 2:
                    out.append([int(part[0]), str(part[1])])
                else:
                    return None
            out.sort(key=lambda s: s[0])
            return out
        text = str(value).strip()
        if not text:
            return []
        out = []
        for chunk in text.replace(";", ",").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "->" not in chunk:
                return None
            minutes, action = chunk.split("->", 1)
            out.append([int(minutes), action.strip()])
        out.sort(key=lambda s: s[0])
        return out
    except Exception:
        return None


def _coalesce_text(seconds):
    try:
        seconds = int(seconds)
        if seconds <= 0:
            return "never"
        if seconds < 60:
            return "%ds" % seconds
        return "%sm" % (seconds // 60)
    except Exception:
        return "?"


def _ladder_text(ladder):
    try:
        if not ladder:
            return "none"
        return "/".join("%dm" % int(step[0]) for step in ladder)
    except Exception:
        return "?"


# ------------------------------------------------------- real ledger reads


def _sessions():
    """Real session records from ctx.state["sessions"]. Never raises."""
    try:
        st = _state_store()
        if st is None:
            return {}
        sessions = st.get("sessions")
        return sessions if isinstance(sessions, dict) else {}
    except Exception:
        return {}


def ledger_ms_samples(limit=12):
    """REAL approval-latency samples (ms) from the persisted ledger.

    Falls back to the spec-named samples 400/1100/6200 (which define the
    bands) when no answered approval exists yet. Never raises.
    """
    try:
        pairs = []
        for _sk, rec in _sessions().items():
            try:
                approvals = rec.get("approvals") or []
            except Exception:
                continue
            for entry in approvals:
                try:
                    if not isinstance(entry, dict):
                        continue
                    ms = entry.get("ms")
                    if ms is None:
                        continue
                    ms = float(ms)
                    if ms < 0:                     # ms == -1 -> unanswered
                        continue
                    pairs.append((float(entry.get("ts") or 0.0), ms))
                except Exception:
                    continue
        pairs.sort()
        samples = [m for _ts, m in pairs[-limit:]]
        return samples or [float(v) for v in _MS_BANDS]
    except Exception:
        return [float(v) for v in _MS_BANDS]


def _quiet_window():
    try:
        getter = getattr(_CTX, "get_config", None)
        if callable(getter):
            cfg = getter("day") or {}
            quiet = (cfg or {}).get("quiet") or {}
            start = int(quiet.get("start", _DEFAULT_QUIET[0]))
            end = int(quiet.get("end", _DEFAULT_QUIET[1]))
            return start % 1440, end % 1440
    except Exception:
        pass
    return _DEFAULT_QUIET


def _quiet_active(now=None):
    try:
        start, end = _quiet_window()
        if start == end:
            return False
        lt = time.localtime(now if now is not None else _now())
        minute = lt.tm_hour * 60 + lt.tm_min
        if start > end:
            return minute >= start or minute < end
        return start <= minute < end
    except Exception:
        return False


# ------------------------------------------------------------ coalescing


def _fingerprint(kind, item):
    """Per-kind coalesce fingerprint from the REAL extras the writers store."""
    try:
        if kind == "approval_stall":
            return str(item.get("key") or item.get("pattern_key") or "")
        if kind == "secret_exposure":
            return str(item.get("sign") or (item.get("text") or "")[:40])
        if kind == "test_tamper":
            files = item.get("files")
            if isinstance(files, list) and files:
                return str(files[0])
            return str(item.get("file") or (item.get("text") or "")[:40])
        if kind == "provider_error":
            return "%s|%s" % (item.get("provider") or "",
                              item.get("status") or "")
        if kind == "subagent_failed":
            return "%s|%s" % (item.get("role") or "",
                              item.get("status") or "")
        if kind == "interrupted":
            return str(item.get("platform") or item.get("reason") or "")
    except Exception:
        pass
    return str((item.get("text") or "")[:40])


def _key(kind, session_key, fingerprint):
    return "%s|%s|%s" % (kind, session_key or "", fingerprint or "")


def observe(kind, session_key="", fingerprint="", text="", ms=None):
    """Record one attention event.

    Floods of the same key collapse into ONE row with an incremented count —
    never one row per event. Returns {"row": …, "delivery": …}; never raises.
    """
    try:
        if kind not in _ATTN_KINDS:
            return {"error": "unknown kind: %s" % kind}
        matrix = matrix_get()
        entry = matrix.get(kind) or dict(_MATRIX_DEFAULT[kind])
        window = float(entry.get("coalesce_s") or 0)
        now = _now()
        fp = str(fingerprint or (text or "")[:40] or "default")
        key = _key(kind, session_key, fp)
        try:
            ms_val = None if ms is None else float(ms)
        except Exception:
            ms_val = None

        with ESC_LOCK:
            _m, _d, acked = _load_esc()
            ack_ts = float(acked.get(key) or 0.0)
            row = _OBSERVED.get(key)
            gap = 0.0 if row is None else (
                now - float(row.get("lastTs") or now))
            # After an ack: a repeat INSIDE the coalesce window merges into
            # the same row (a still-running flood keeps its count and the row
            # re-opens because lastTs > ack_ts); only a repeat BEYOND the
            # window starts a fresh cycle. Never a new row per event.
            fresh_after_ack = (
                row is not None and ack_ts > 0
                and float(row.get("lastTs") or 0) <= ack_ts
                and now > ack_ts
                and gap > window
            )
            created = False
            if row is None or fresh_after_ack:
                created = True
                row = {
                    "key": key,
                    "kind": kind,
                    "sessionKey": str(session_key or ""),
                    "count": 1,
                    "firstTs": now,
                    "lastTs": now,
                    "text": str(text or ("%s event" % kind)),
                    "entries": [{"text": str(text or kind), "ts": now}],
                    "ms": ms_val,
                    "ts": now,
                    "observed": True,
                }
                _OBSERVED[key] = row
            else:
                # merge into the single open row — count increments, text
                # tracks the latest event, window expiry does not fork it.
                row["count"] = int(row.get("count") or 1) + 1
                row["lastTs"] = now
                if text:
                    row["text"] = str(text)
                entries = list(row.get("entries") or [])
                if len(entries) < 5:
                    entries.append({"text": str(text or kind), "ts": now})
                row["entries"] = entries
                if ms_val is not None:
                    row["ms"] = ms_val
                    if ms_val >= 0:
                        row["ts"] = now
            # bound the mirror: prune ACKED rows first (unacked never drop)
            try:
                if len(_OBSERVED) > 400:
                    acked_keys = [k for k, row2 in _OBSERVED.items()
                                  if acked.get(k)]
                    for old in sorted(
                        acked_keys,
                        key=lambda k: _OBSERVED[k].get("lastTs") or 0.0,
                    )[: max(0, len(_OBSERVED) - 400)]:
                        _OBSERVED.pop(old, None)
            except Exception:
                pass

        delivery = _annotate_and_deliver(row, entry, now, created)
        return {"row": dict(row), "delivery": delivery}
    except Exception as exc:
        return {"error": "observe failed: %s" % exc}


def ack(key="*"):
    """Acknowledge one row, or every row when key is '*' / ''.

    '*' walks the UNCAPPED row set so groups hidden behind the '+N more'
    fold are acknowledged too — nothing is left half-acked. Never raises.
    """
    try:
        now = _now()
        with ESC_LOCK:
            matrix, debt, acked = _load_esc()
            if key in (None, "", "*"):
                rows = collect_rows(now, cap=False)
                targets = [r.get("key") for r in rows]
            else:
                targets = [key]
            for target in targets:
                if target:
                    acked[str(target)] = now
            # bound the acked map (oldest drops first)
            try:
                if len(acked) > 400:
                    for old in sorted(acked, key=lambda k: acked[k]) \
                            [: len(acked) - 400]:
                        acked.pop(old, None)
            except Exception:
                pass
            _persist_esc(matrix, debt, acked)
        return [t for t in targets if t]
    except Exception:
        return []


# ----------------------------------------------------------- debt math


def _latency_factor(ms, pending_age_s=0.0):
    """Bands from the real ledger samples 400/1100/6200 ms (spec §5)."""
    try:
        if ms is None:
            return 1.0                       # not approval-backed: no invention
        ms = float(ms)
        if ms < 0:
            # unanswered: (now - ts)/1000 — pending age, grows linearly.
            return max(1.0, float(pending_age_s))
        if ms <= _MS_BANDS[1]:
            return 1.0                       # 0..1100 (samples 400, 1100)
        if ms < _MS_BANDS[2]:
            return 1.0 + 0.3 * (ms - _MS_BANDS[1]) / (_MS_BANDS[2] - _MS_BANDS[1])
        return 1.3                           # >= 6200 (slow human)
    except Exception:
        return 1.0


def _debt_item(kind, age_min, factor, count=1):
    try:
        weight = float(_SEV_W.get(kind, 1.0))
        item = weight * (1.0 + float(age_min) / 5.0) * float(factor)
        return item * max(1, int(count or 1))       # count multiplies
    except Exception:
        return 0.0


# ------------------------------------------------------- ladder / delivery


def _rung(entry, age_min, acked):
    if acked:
        return -1, None
    rung, action = -1, None
    try:
        for idx, step in enumerate(entry.get("ladder") or []):
            if age_min >= float(step[0]):
                rung, action = idx, step[1]
    except Exception:
        return -1, None
    return rung, action


def _cadence_s(entry, age_min, debt):
    """Rung cadence = max(base-to-next-rung, 60s / min(debt, 10)) (spec §5)."""
    try:
        future = [s for s in (entry.get("ladder") or [])
                  if float(s[0]) > age_min]
        base = max(30.0, (float(future[0][0]) - age_min) * 60.0) \
            if future else 60.0
        d = max(float(debt or 0.0), 1.0)
        return round(max(base, 60.0 / min(d, 10.0)), 1)
    except Exception:
        return 60.0


def _effective_delivery(entry, row, now=None):
    """Base delivery + escalate_after + ladder rung + quiet hours. Never raises.

    Quiet hours downgrade notify for P1–P3 only; P0 and rung >= 3 bypass
    quiet hours. Result is an intention — the actual OS call always goes
    through the guarded ``_notify``.
    """
    try:
        base = entry.get("delivery", "badge")
        level = _DELIVERIES.index(base) if base in _DELIVERIES else 1
        age_min = float(row.get("age_min") or 0.0)
        escalate_after = entry.get("escalate_after")
        if escalate_after is not None and age_min >= float(escalate_after) \
                and level < 2:
            level = 2
        action = row.get("action")
        if action in ("notify", "renotify", "notify-critical"):
            level = 2
        elif action in ("badge", "pin") and level < 1:
            level = 1
        rung = int(row.get("rung") or -1)
        if level == 2 and entry.get("sev") != "P0" and rung < 3 \
                and _quiet_active(now):
            target = entry.get("quiet") or "never"
            if target == "badge" and level > 1:
                level = 1
            elif target == "silent":
                level = 0
        return _DELIVERIES[level]
    except Exception:
        return "silent"


def _notify(title, body):
    """The ONE guarded OS delivery path. Returns bool; never raises."""
    try:
        payload = {
            "title": str(title)[:80],
            "body": str(body)[:_BODY_CAP],
        }
        os_mod = getattr(_CTX, "os", None)
        fn = getattr(os_mod, "notify", None) if os_mod is not None else None
        if not callable(fn):
            fn = getattr(_CTX, "notify", None)
        if not callable(fn):
            return False
        try:
            fn(payload)                       # same guarded shape as L417–419
            return True
        except Exception:
            return False
    except Exception:
        return False


def _annotate_and_deliver(row, entry, now, created=False):
    """Annotate one row, then run the one-notify-per-key-per-window rule.

    Repeats inside the window are absorbed (badge only); window expiry with
    a still-unacked row re-notifies ONCE at the next ladder rung. The
    underlying ledger row is never mutated by a delivery attempt. Never raises.
    """
    try:
        age_min = max(0.0, (now - float(row.get("firstTs") or now)) / 60.0)
        row["age_min"] = round(age_min, 2)
        rung, action = _rung(entry, age_min, False)
        row["rung"], row["action"] = rung, action
        row["effective"] = _effective_delivery(entry, row, now)
        window = float(entry.get("coalesce_s") or 60) or 60.0
        if row.get("effective") != "notify":
            return row.get("effective")
        with ESC_LOCK:
            book = _NOTIFIED.get(row.get("key")) or {}
            notified_at = float(book.get("at") or 0.0)
            notified_rung = int(book.get("rung") or -1)
        if notified_at:
            if (now - notified_at) < window:
                return "absorbed"             # one notify per key per window
            if rung <= notified_rung and not created:
                return "suppressed"           # re-notify only at the next rung
        title = "%s ×%d" % (row.get("kind") or "attention",
                            int(row.get("count") or 1))
        ok = _notify(title, str(row.get("text") or ""))
        with ESC_LOCK:
            _NOTIFIED[row.get("key")] = {"at": now, "rung": rung}
        return "notified" if ok else "no-notify-path"
    except Exception:
        return "silent"


def ladder_tick(now=None):
    """Walk open rows and deliver at their current rung. Never raises."""
    try:
        now = now if now is not None else _now()
        matrix = matrix_get()
        rows = collect_rows(now, cap=False)
        results = []
        for row in rows:
            try:
                if row.get("acked") or row.get("kind") not in _ATTN_KINDS:
                    continue
                entry = matrix.get(row.get("kind")) or {}
                outcome = _annotate_and_deliver(row, entry, now, False)
                results.append({"key": row.get("key"),
                                "rung": row.get("rung"),
                                "result": outcome})
            except Exception:
                continue
        return results
    except Exception:
        return []


# ------------------------------------------------------------ collect


def _cap_groups(rows):
    """MAX_GROUPS = 40; overflow folds into ONE '+N more' row (never drop)."""
    try:
        if len(rows) <= _MAX_GROUPS:
            return rows
        ordered = sorted(
            rows,
            key=lambda r: (-float(r.get("debt") or 0.0),
                           float(r.get("firstTs") or 0.0)),
        )
        keep = ordered[: _MAX_GROUPS - 1]
        fold = ordered[_MAX_GROUPS - 1:]
        folded_debt = sum(float(r.get("debt") or 0.0) for r in fold)
        firsts = [float(r.get("firstTs") or 0.0) for r in fold]
        lasts = [float(r.get("lastTs") or 0.0) for r in fold]
        # the fold reflects what it hides: acked only when EVERY folded row
        # is acked, debt is the sum of folded (acked rows contribute 0).
        keep.append({
            "key": "+more",
            "kind": "other",
            "sessionKey": "",
            "count": len(fold),
            "firstTs": min(firsts) if firsts else 0.0,
            "lastTs": max(lasts) if lasts else 0.0,
            "text": "+%d more groups" % len(fold),
            "entries": [],
            "ms": None,
            "acked": all(bool(r.get("acked")) for r in fold),
            "sev": "P3",
            "age_min": 0.0,
            "debt": round(folded_debt, 3),
            "rung": -1,
            "action": None,
            "effective": "silent",
            "overflow": True,
        })
        return keep
    except Exception:
        return rows


def collect_rows(now=None, include_observed=True, cap=True):
    """All open rows: real ledger + pending approvals + observed floods.

    Real rows are grouped by coalesce key (kind|session|fingerprint); acked
    state comes from the persisted acked map and a real event newer than the
    ack re-opens the row. ``cap=False`` returns the UNCAPPED set (used by
    ack/tick/debt so rows hidden behind the +N more fold are never silently
    skipped). Never raises.
    """
    try:
        now = now if now is not None else _now()
        matrix = matrix_get()
        _m, _debt, acked = _load_esc()
        rows = {}

        for session_key, rec in _sessions().items():
            try:
                items = [i for i in (rec.get("attn") or [])
                         if isinstance(i, dict)
                         and i.get("kind") in _ATTN_KINDS]
                approvals = [a for a in (rec.get("approvals") or [])
                             if isinstance(a, dict)]
            except Exception:
                continue

            # newest approval entry per pattern key -> real ms latency
            ms_by_key = {}
            for entry in approvals:
                try:
                    ms_by_key[str(entry.get("key") or "")] = entry
                except Exception:
                    continue

            for item in items:
                try:
                    kind = item.get("kind")
                    fp = _fingerprint(kind, item)
                    key = _key(kind, session_key, fp)
                    ts = float(item.get("ts") or now)
                    row = rows.get(key)
                    if row is None:
                        row = {
                            "key": key,
                            "kind": kind,
                            "sessionKey": str(session_key or ""),
                            "count": 0,
                            "firstTs": ts,
                            "lastTs": ts,
                            "text": item.get("text") or kind,
                            "entries": [],
                            "ms": None,
                            "ts": ts,
                        }
                        rows[key] = row
                    row["count"] = int(row.get("count") or 0) + 1
                    row["firstTs"] = min(float(row["firstTs"]), ts)
                    row["lastTs"] = max(float(row["lastTs"]), ts)
                    row["text"] = item.get("text") or row.get("text")
                    entries = list(row.get("entries") or [])
                    if len(entries) < 5:
                        entries.append({"text": item.get("text") or "",
                                        "ts": ts})
                    row["entries"] = entries
                    if kind == "approval_stall":
                        hit = ms_by_key.get(fp)
                        if hit is not None:
                            try:
                                row["ms"] = float(hit.get("ms", -1))
                            except Exception:
                                pass
                            row["ts"] = float(hit.get("ts") or ts)
                except Exception:
                    continue

            # pending approvals (ms == -1) are open approval_stall rows
            for entry in approvals:
                try:
                    raw_ms = entry.get("ms", -1)
                    if raw_ms is None or float(raw_ms) >= 0:
                        continue
                    fp = str(entry.get("key") or "")
                    key = _key("approval_stall", session_key, fp)
                    if key in rows:
                        continue          # the stall writer already surfaced it
                    ts = float(entry.get("ts") or now)
                    rows[key] = {
                        "key": key,
                        "kind": "approval_stall",
                        "sessionKey": str(session_key or ""),
                        "count": 1,
                        "firstTs": ts,
                        "lastTs": ts,
                        "text": "approval pending: %s" % (
                            entry.get("cmd") or entry.get("desc") or ""),
                        "entries": [{"text": entry.get("cmd") or "", "ts": ts}],
                        "ms": -1.0,
                        "ts": ts,
                    }
                except Exception:
                    continue

        # observed flood mirror (already coalesced to one row per key)
        if include_observed:
            with ESC_LOCK:
                for key, row in _OBSERVED.items():
                    copy = dict(row)
                    copy["entries"] = list(row.get("entries") or [])
                    rows[key] = copy

        out = []
        for key, row in rows.items():
            try:
                kind = row.get("kind")
                if kind not in _ATTN_KINDS:
                    continue
                entry = matrix.get(kind) or dict(_MATRIX_DEFAULT[kind])
                ack_ts = float(acked.get(key) or 0.0)
                last_ts = float(row.get("lastTs") or 0.0)
                # acked only if nothing arrived after the ack
                row["acked"] = bool(ack_ts) and last_ts <= ack_ts
                row["sev"] = entry.get("sev") or _SEV_LABEL[kind]
                age_min = max(0.0, (now - float(row.get("firstTs") or now))
                              / 60.0)
                row["age_min"] = round(age_min, 2)
                pending_age = max(0.0, now - float(row.get("ts")
                                                   or row.get("firstTs")
                                                   or now))
                factor = _latency_factor(row.get("ms"), pending_age)
                row["latency_factor"] = round(float(factor), 3)
                count = int(row.get("count") or 1)
                row["debt"] = 0.0 if row["acked"] else round(
                    _debt_item(kind, age_min, factor, count), 3)
                rung, action = _rung(entry, age_min, row["acked"])
                row["rung"], row["action"] = rung, action
                row["effective"] = _effective_delivery(entry, row, now)
                row["next_rung_s"] = (None if row["acked"]
                                      else _cadence_s(entry, age_min,
                                                      row["debt"]))
                out.append(row)
            except Exception:
                continue

        if cap:
            out = _cap_groups(out)
        out.sort(key=lambda r: (-float(r.get("debt") or 0.0),
                                float(r.get("firstTs") or 0.0)))
        return out
    except Exception:
        return []


def attention_debt(now=None):
    """Fleet + per-session + per-kind attention debt from REAL data. Never raises."""
    try:
        now = now if now is not None else _now()
        rows = collect_rows(now, cap=False)   # exact per-session attribution
        total = 0.0
        by_session = {}
        by_kind = {}
        open_rows = 0
        unacked = 0
        for row in rows:
            debt = float(row.get("debt") or 0.0)
            total += debt
            sk = row.get("sessionKey") or ""
            by_session[sk] = round(by_session.get(sk, 0.0) + debt, 3)
            kind = row.get("kind")
            if kind:
                by_kind[kind] = round(by_kind.get(kind, 0.0) + debt, 3)
            if not row.get("acked"):
                unacked += 1
            open_rows += 1
        return {
            "total": round(total, 3),
            "by_session": by_session,
            "by_kind": by_kind,
            "open": open_rows,
            "unacked": unacked,
        }
    except Exception:
        return {"total": 0.0, "by_session": {}, "by_kind": {},
                "open": 0, "unacked": 0}


def _counts_by_kind(rows):
    counts = {}
    try:
        for kind in _ATTN_KINDS:
            counts[kind] = 0
        for row in rows:
            if row.get("acked"):
                continue
            kind = row.get("kind")
            if kind in counts:
                counts[kind] += int(row.get("count") or 1)
    except Exception:
        return {}
    return counts


def snapshot(now=None):
    """Everything /day-matrix reports. Never raises."""
    try:
        now = now if now is not None else _now()
        matrix = matrix_get()
        rows = collect_rows(now)
        debt = attention_debt(now)
        try:
            start, end = _quiet_window()
        except Exception:
            start, end = _DEFAULT_QUIET
        return {
            "ok": True,
            "kinds": list(_ATTN_KINDS),
            "matrix": matrix,
            "rows": rows,
            "counts": _counts_by_kind(rows),
            "debt": debt,
            "quiet": {"start": start, "end": end,
                      "active": _quiet_active(now)},
            "ms_samples": ledger_ms_samples(),
            "bands_ms": list(_MS_BANDS),
            "max_groups": _MAX_GROUPS,
            "generated_at": now,
        }
    except Exception as exc:
        return {"ok": False, "kinds": list(_ATTN_KINDS), "matrix": {},
                "rows": [], "counts": {}, "debt": {},
                "error": "%s" % exc}


def _persist_debt(debt):
    try:
        with ESC_LOCK:
            matrix, _old, acked = _load_esc()
            _persist_esc(matrix, debt.get("by_session") or {}, acked)
    except Exception:
        pass


def _fmt_clock(minute):
    try:
        minute = int(minute) % 1440
        return "%02d:%02d" % (minute // 60, minute % 60)
    except Exception:
        return "??"


def _table_text(payload):
    """The §6 routing table as plain text (chat command view). Never raises."""
    try:
        lines = []
        debt = payload.get("debt") or {}
        quiet = payload.get("quiet") or {}
        lines.append("ROUTING — escalation matrix    debt %.1f" %
                     float(debt.get("total") or 0.0))
        lines.append("quiet hours %s–%s local  %s" % (
            _fmt_clock(quiet.get("start", _DEFAULT_QUIET[0])),
            _fmt_clock(quiet.get("end", _DEFAULT_QUIET[1])),
            "ACTIVE" if quiet.get("active") else "inactive"))
        lines.append("%-16s %-3s  %-7s  %-8s  %-6s  %-16s  %s" % (
            "kind", "sev", "delivery", "coalesce", "quiet",
            "escalate after", "count"))
        matrix = payload.get("matrix") or {}
        counts = payload.get("counts") or {}
        for kind in payload.get("kinds") or _ATTN_KINDS:
            entry = matrix.get(kind) or {}
            count = int(counts.get(kind, 0) or 0)
            lines.append("%-16s %-3s  %-7s  %-8s  %-6s  %-16s  %s" % (
                kind,
                entry.get("sev", "-"),
                entry.get("delivery", "-"),
                _coalesce_text(entry.get("coalesce_s", 0)),
                entry.get("quiet", "-"),
                _ladder_text(entry.get("ladder") or []),
                ("%d ×" % count) if count else "0"))
        rows = payload.get("rows") or []
        lines.append("")
        lines.append("open coalesced rows: %d (unacked %d)" % (
            len(rows), int((debt.get("unacked")) or 0)))
        for row in rows[:12]:
            lines.append("  %-46s ×%-3d %-9s debt %8.1f  %s" % (
                str(row.get("key"))[:46],
                int(row.get("count") or 1),
                str(row.get("effective") or "-"),
                float(row.get("debt") or 0.0),
                "acked" if row.get("acked")
                else "unacked %sm" % row.get("age_min")))
        samples = payload.get("ms_samples") or []
        lines.append("ledger ms: %s" % "/".join(
            str(int(s)) for s in samples))
        lines.append("edit: day-matrix-set <kind> <delivery|coalesce|quiet|"
                     "escalate|ladder> <value>  ·  ack: day-attn-ack <*|key>")
        return "\n".join(lines)
    except Exception:
        try:
            return json.dumps(payload, default=str)
        except Exception:
            return "day-matrix: table render failed"


# ------------------------------------------------------------- commands


def _is_json_arg(arg):
    return str(arg or "").strip().lower() in ("json", "-j", "--json")


def cmd_matrix(arg="", **_kw):
    """Routing table + coalesced rows + debt. Runs the ladder tick first."""
    try:
        now = _now()
        try:
            ladder_tick(now)              # delivery attempts, all guarded
        except Exception:
            pass
        payload = snapshot(now)
        _persist_debt(payload.get("debt") or {})
        if _is_json_arg(arg):
            return json.dumps(payload, sort_keys=True, default=str)
        return _table_text(payload)
    except Exception as exc:
        return "day-matrix failed: %s" % exc


def cmd_matrix_set(arg="", **_kw):
    try:
        parts = str(arg or "").split()
        if len(parts) < 3:
            return ("usage: day-matrix-set <kind> "
                    "<delivery|coalesce|quiet|escalate|ladder> <value>")
        kind = parts[0]
        field = parts[1]
        raw = " ".join(parts[2:])
        value = raw
        if field in ("coalesce", "coalesce_s", "escalate", "escalate_after"):
            lowered = raw.strip().lower()
            if lowered in ("none", "never", "off"):
                value = None if field.startswith("escalate") else 0
            else:
                try:
                    value = int(raw)
                except Exception:
                    return ("usage: day-matrix-set %s <seconds|none>"
                            % field)
        ok, payload = matrix_set(kind, field, value)
        if not ok:
            return str(payload)
        return json.dumps({"ok": True, "kind": kind,
                           "entry": payload}, sort_keys=True, default=str)
    except Exception as exc:
        return "day-matrix-set failed: %s" % exc


def cmd_matrix_reset(arg="", **_kw):
    try:
        fresh = reset_matrix()
        return json.dumps({"ok": True, "reset": list(fresh.keys()) or
                           list(_ATTN_KINDS)}, sort_keys=True)
    except Exception as exc:
        return "day-matrix-reset failed: %s" % exc


def cmd_observe(arg="", **_kw):
    """Record one event; floods coalesce into a single counted row."""
    try:
        parts = str(arg or "").split()
        if not parts or parts[0] not in _ATTN_KINDS:
            return ("usage: day-attn-observe <kind> [session] "
                    "[fingerprint] [ms] [text]  kinds: %s"
                    % ", ".join(_ATTN_KINDS))
        kind = parts[0]
        session = parts[1] if len(parts) > 1 else ""
        fingerprint = parts[2] if len(parts) > 2 else ""
        ms = None
        rest_start = 3
        if len(parts) > 3:
            try:
                ms = float(parts[3])
                rest_start = 4
            except Exception:
                ms = None
                rest_start = 3
        text = " ".join(parts[rest_start:]) or ("%s event" % kind)
        result = observe(kind, session, fingerprint, text, ms)
        return json.dumps({"ok": "error" not in result, "result": result},
                          sort_keys=True, default=str)
    except Exception as exc:
        return "day-attn-observe failed: %s" % exc


def cmd_ack(arg="", **_kw):
    try:
        key = str(arg or "").strip() or "*"
        targets = ack(key)
        return json.dumps({"ok": True, "acked": targets}, sort_keys=True)
    except Exception as exc:
        return "day-attn-ack failed: %s" % exc


def cmd_debt(arg="", **_kw):
    try:
        now = _now()
        debt = attention_debt(now)
        samples = ledger_ms_samples()
        if _is_json_arg(arg):
            return json.dumps({
                "ok": True,
                "debt": debt,
                "ms_samples": samples,
                "bands_ms": list(_MS_BANDS),
                "generated_at": now,
            }, sort_keys=True, default=str)
        lines = [
            "attention debt = sev_w × (1 + unack_age_min/5) × latency_factor"
            " × count",
            "  fleet total: %.1f   open groups: %d   unacked: %d" % (
                float(debt.get("total") or 0.0),
                int(debt.get("open") or 0),
                int(debt.get("unacked") or 0)),
            "  ledger ms samples: %s" % ("/".join(
                str(int(s)) for s in samples)),
        ]
        for sk, value in sorted(debt.get("by_session", {}).items(),
                                key=lambda kv: -kv[1]):
            lines.append("  %-44s %8.1f" % (sk or "(local)", value))
        for kind in _ATTN_KINDS:
            value = (debt.get("by_kind") or {}).get(kind) or 0.0
            if value:
                lines.append("  %-44s %8.1f" % (kind, value))
        return "\n".join(lines)
    except Exception as exc:
        return "day-attn-debt failed: %s" % exc


def _register_one(ctx, name, fn, description, args_hint=""):
    """Register one command; a failure here is swallowed, never propagated."""
    try:
        ctx.register_command(name, fn, description=description,
                             args_hint=args_hint)
        return True
    except TypeError:
        pass
    except Exception:
        pass
    try:
        ctx.register_command(name, fn)
        return True
    except Exception:
        return False


def register(ctx) -> None:
    """Called at plugin load. Commands only — no hooks. Never raises."""
    global _CTX
    try:
        _CTX = ctx
    except Exception:
        pass
    try:
        matrix_get()                      # warm the merged matrix once
    except Exception:
        pass
    commands = (
        ("day-matrix", cmd_matrix,
         "Escalation matrix: routing table, coalesced rows, attention debt.",
         "[json]"),
        ("day-matrix-set", cmd_matrix_set,
         "Edit one routing cell of the escalation matrix.",
         "<kind> <delivery|coalesce|quiet|escalate|ladder> <value>"),
        ("day-matrix-reset", cmd_matrix_reset,
         "Restore the default escalation matrix.", ""),
        ("day-attn-observe", cmd_observe,
         "Record one attention event (floods coalesce to one counted row).",
         "<kind> [session] [fingerprint] [ms] [text]"),
        ("day-attn-ack", cmd_ack,
         "Acknowledge attention rows ('*' = all).", "<key|*>"),
        ("day-attn-debt", cmd_debt,
         "Attention debt from ledger ms + unacknowledged age.", "[json]"),
    )
    for name, fn, description, hint in commands:
        _register_one(ctx, name, fn, description, hint)
    return None
