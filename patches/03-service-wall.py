"""Lane 03 — Service Wall: live watchdog over the systemd --user units Hermes runs.

Registers exactly two commands (commands only — no hooks, no plugin.yaml edit):

    /day-services            read  -> JSON wall: verdict, uptime, NRestarts, alerts
    /day-service-restart <u> mutate -> allowlisted restart receipt

Binding rules from the lane spec:
  * every subprocess is an argv list with shell=False and a hard timeout;
  * the unit allowlist (^hermes- plus the spec's fixed Hermes-adjacent set)
    rejects anything else BEFORE any subprocess is spawned
    -> {"ok": false, "reason": "not_allowlisted"};
  * oneshot units paired with a fresh timer count as ok, never "down";
  * uptime = time.CLOCK_MONOTONIC vs ActiveEnterTimestampMonotonic — locale
    date strings are echoed for display only, never parsed;
  * systemctl missing or timing out degrades the envelope to services:[] with
    probe.reason set — an empty state, never an exception.

State (NRestarts sample ring + last restart receipt) lives in module-level
containers guarded by this patch's own threading.RLock (never the main
module's `_LOCK` — patches are loaded standalone).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

# --- module state (this patch owns its own lock) -----------------------------
_LOCK = threading.RLock()
_SAMPLES: Dict[str, List[int]] = {}
_LAST_RESTART: Optional[Dict[str, Any]] = None

# C locale so systemd prints stable English tokens ("1min 34s ago", "Mon …")
_ENV: Dict[str, str] = dict(os.environ)
_ENV["LC_ALL"] = "C"
_ENV["LANG"] = "C"

# spec §1.5 watchlist: every ^hermes- unit plus this fixed adjacent set
_FIXED_UNITS: Tuple[str, ...] = (
    "omniroute.service",
    "cliproxyapi.service",
    "local-embed.service",
    "camofox-browser.service",
    "cua-driver.service",
    "squad-preview.service",
)
_FIXED = frozenset(_FIXED_UNITS)

# spec §2.3: shape gate, applied before anything is spawned
_UNIT_RE = re.compile(r"[a-z0-9][a-z0-9@._-]*\.service")

_SHOW_PROPS = (
    "Id,Description,LoadState,ActiveState,SubState,UnitFileState,NRestarts,"
    "ActiveEnterTimestampMonotonic,InactiveExitTimestampMonotonic,"
    "ActiveExitTimestampMonotonic,ExecMainStartTimestamp,Result,Type"
)

_T_LIST = 6.0        # list-units
_T_SHOW = 5.0        # per show call
_T_TIMERS = 6.0      # list-timers
_T_RESTART = 30.0    # spec §2.3 restart timeout
_BUDGET = 8.0        # hard budget for the per-unit sweep (spec §2.1.3)
_SPARK_CAP = 30      # rolling NRestarts ring for Spark({seqs}) (spec §3.1)
_TIMER_FALLBACK_FRESH_S = 900.0   # freshness window when the interval is unreadable
_MAX_INTERVAL_S = 31 * 86400.0    # sanity bound for a parsed timer interval

_WEEKDAYS = frozenset({"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"})
_DUR_TOKEN_RE = re.compile(r"^(\d+)(ms|s|min|h|day|days|week|weeks|month|months)$")
_UNIT_WORD_RE = re.compile(r"^(ms|s|min|h|day|days|week|weeks|month|months)$")
_MULT = {
    "ms": 0.001, "s": 1.0, "min": 60.0, "h": 3600.0,
    "day": 86400.0, "days": 86400.0, "week": 604800.0, "weeks": 604800.0,
    "month": 2592000.0, "months": 2592000.0,
}

# spec §2.4 verdicts -> severity bucket
_SEV = {
    "ok": "ok",
    "off": "off",
    "degraded": "warn",
    "restarting": "warn",
    "drifted": "warn",
    "unknown": "warn",
    "failed": "crit",
    "down": "crit",
}
_SEV_ORDER = {"crit": 0, "warn": 1, "off": 2, "ok": 3}


# --- tiny helpers ------------------------------------------------------------
def _ms_since(t0: float) -> int:
    return int((time.time() - t0) * 1000)


def _int_or(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _run(argv: Sequence[str], timeout: float) -> Tuple[Optional[int], str, str, str]:
    """One argv-list subprocess (shell=False). status: ok|timeout|missing|error."""
    try:
        proc = subprocess.run(
            list(argv), shell=False, capture_output=True, text=True,
            timeout=timeout, env=_ENV,
        )
    except subprocess.TimeoutExpired:
        return None, "", "", "timeout"
    except FileNotFoundError:
        return None, "", "", "missing"
    except Exception as exc:                       # decode errors, EAGAIN, …
        return None, "", "%s: %s" % (type(exc).__name__, exc), "error"
    return proc.returncode, proc.stdout or "", proc.stderr or "", "ok"


def _now_us() -> int:
    """CLOCK_MONOTONIC microseconds — the same clock systemd stamps *Monotonic with."""
    try:
        return int(time.clock_gettime(time.CLOCK_MONOTONIC) * 1e6)
    except Exception:
        return 0


def _bus_down(text: str) -> bool:
    low = (text or "").lower()
    for marker in ("failed to connect to bus", "no medium found",
                   "no user session", "dbus: ", "access denied"):
        if marker in low:
            return True
    return False


def _fail_reason(status: str, rc: Optional[int], text: str) -> str:
    if status == "missing":
        return "systemctl_not_on_PATH"
    if status == "timeout":
        return "timeout"
    if _bus_down(text):
        return "user_bus_unavailable"
    return "exit_%d" % rc if rc is not None else "exit_-1"


def _systemd_version(bin_path: str) -> Optional[str]:
    rc, out, _, status = _run([bin_path, "--version"], 5.0)
    if status != "ok" or rc != 0:
        return None
    first = (out.splitlines() or [""])[0].split()
    if len(first) >= 2 and first[0] == "systemd":
        return " ".join(first[1:])
    return None


# --- parsers (all token-based: no locale date parsing anywhere) --------------
def _parse_props(text: str) -> List[Dict[str, str]]:
    """systemctl show output -> one dict per unit (blank line = new block)."""
    recs: List[Dict[str, str]] = []
    cur: Dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            if cur:
                recs.append(cur)
                cur = {}
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        if key == "Id" and cur:          # batched `show a.service b.service`
            recs.append(cur)
            cur = {}
        cur[key] = val
    if cur:
        recs.append(cur)
    return recs


def _parse_units(text: str) -> Dict[str, Dict[str, str]]:
    """list-units rows -> unit -> {load, active, sub, desc} (marker column tolerant)."""
    rows: Dict[str, Dict[str, str]] = {}
    for line in (text or "").splitlines():
        toks = line.split()
        idx = next((i for i, tok in enumerate(toks) if tok.endswith(".service")), None)
        if idx is None or len(toks) < idx + 4:
            continue
        rows[toks[idx]] = {
            "unit": toks[idx],
            "load": toks[idx + 1],
            "active": toks[idx + 2],
            "sub": toks[idx + 3],
            "desc": " ".join(toks[idx + 4:]),
        }
    return rows


def _dur_seconds(tokens: Sequence[str]) -> Optional[float]:
    """'25s' / '1min 34s' / '4 weeks 1 day' -> seconds. C locale only, no dates."""
    total = 0.0
    seen = False
    pending: Optional[int] = None
    for tok in tokens:
        if tok in ("ago", "-", ""):
            continue
        m = _DUR_TOKEN_RE.match(tok)
        if m:
            total += int(m.group(1)) * _MULT[m.group(2)]
            seen = True
            pending = None
            continue
        if tok.isdigit():
            pending = int(tok)
            continue
        if pending is not None and _UNIT_WORD_RE.match(tok):
            total += pending * _MULT[tok]
            seen = True
            pending = None
            continue
        return None                      # unknown token — refuse, never guess
    if pending is not None:
        total += pending                 # bare "5" with no unit: seconds
        seen = True
    return total if seen else None


def _parse_timers(text: str) -> Tuple[Dict[str, str], Dict[str, float]]:
    """list-timers -> ({service: timer}, {service: seconds until next fire}).

    Column order (verified live): NEXT LEFT LAST ELAPSED TIMER SERVICE.
    Dates are never parsed — only the relative LEFT token group is measured.
    """
    pairs: Dict[str, str] = {}
    lefts: Dict[str, float] = {}
    for line in (text or "").splitlines():
        toks = line.split()
        ti = next((i for i, tok in enumerate(toks) if tok.endswith(".timer")), None)
        if ti is None:
            continue
        timer = toks[ti]
        si = next((i for i, tok in enumerate(toks) if i > ti and tok.endswith(".service")), None)
        service = toks[si] if si is not None else timer[:-6] + ".service"
        pairs[service] = timer
        start = 1 if toks and toks[0] == "-" else 4
        if start >= ti:
            continue
        last_start = next(
            (i for i in range(start, ti) if toks[i] in _WEEKDAYS or toks[i] == "-"),
            None,
        )
        if last_start is None:
            continue
        left_toks = toks[start:last_start]
        if not left_toks or "ago" in left_toks:
            continue
        secs = _dur_seconds(left_toks)
        if secs is not None and 0 < secs < _MAX_INTERVAL_S:
            lefts[service] = secs
    return pairs, lefts


# --- allowlist ---------------------------------------------------------------
def _allowlisted(unit: str) -> bool:
    """Spec §2.3 — checked BEFORE any subprocess is spawned."""
    if not unit or unit.startswith("-"):
        return False
    if not _UNIT_RE.fullmatch(unit):
        return False
    return unit.startswith("hermes-") or unit in _FIXED


def _watchlist(rows: Dict[str, Dict[str, str]]) -> List[str]:
    """spec §1.5: every ^hermes- unit present, plus the fixed set that is present."""
    watch = {u for u in rows if u.startswith("hermes-")}
    watch |= {u for u in _FIXED if u in rows}
    return sorted(watch)


# --- metric helpers ----------------------------------------------------------
def _uptime_s(rec: Dict[str, str], now_us: int) -> Optional[float]:
    """Monotonic math only — locale dates are never parsed (spec §2.1 step 5)."""
    active = (rec.get("ActiveState") or "").lower()
    if active not in ("active", "reloading"):
        return None
    start_us = _int_or(rec.get("ActiveEnterTimestampMonotonic"), 0)
    if start_us <= 0 or now_us <= 0:
        return None
    return round(max(0.0, (now_us - start_us) / 1e6), 1)


def _last_run_us(rec: Dict[str, str]) -> int:
    """Most recent monotonic 'left inactive / left active' stamp (oneshot last fire)."""
    vals = [
        _int_or(rec.get(key), 0)
        for key in ("InactiveExitTimestampMonotonic", "ActiveExitTimestampMonotonic",
                    "ActiveEnterTimestampMonotonic")
    ]
    vals = [v for v in vals if v > 0]
    return max(vals) if vals else 0


def _simple_verdict(active: str, sub: str) -> str:
    """Verdict for units we only saw in list-units (no show sweep): spec §2.4 order."""
    state = (active or "").lower()
    if state == "failed":
        return "failed"
    if state in ("activating", "deactivating"):
        return "restarting"
    if state == "active":
        return "ok"
    if state == "inactive":
        return "off"
    return "unknown"


def _verdict(rec: Dict[str, str], *, watched: bool, timer: Optional[str],
             fresh: bool, age: Optional[float], interval: Optional[float],
             prev_n: Optional[int], uptime: Optional[float]) -> Tuple[str, str]:
    """Spec §2.4 — deterministic, evaluated strictly in this order."""
    active = (rec.get("ActiveState") or "").lower()
    sub = rec.get("SubState") or ""
    unit_file = (rec.get("UnitFileState") or "").lower()
    is_oneshot = (rec.get("Type") or "").lower() == "oneshot"
    n_restarts = _int_or(rec.get("NRestarts"), 0)

    # 1. failed
    if active == "failed":
        result = rec.get("Result") or ""
        if result and result not in ("failed", "success"):
            return "failed", "failed (%s)" % result
        return "failed", "failed"

    # 2. oneshot + fresh timer -> ok (keeps hermes-plugin-health green).
    #    A oneshot still in `activating` must not be swallowed here, or a stuck
    #    start would hide behind its own fresh timer (spec's rag-autocrawler case).
    if is_oneshot and timer and fresh and active not in ("activating", "deactivating"):
        return "ok", "oneshot; last fire %ds ago" % int(age or 0)

    # 3. restarting
    if active in ("activating", "deactivating"):
        if active == "activating":
            return "restarting", "activating (%s)" % (sub or "start")
        return "restarting", "deactivating"
    if prev_n is not None and n_restarts > prev_n and uptime is not None and uptime < 120:
        return "restarting", "restart #%d, %ds ago" % (n_restarts, int(uptime))

    # 4. drift
    if unit_file == "enabled" and active != "active":
        why = "enabled but %s" % (active or "unknown")
        if is_oneshot and timer:
            if age is None:
                why += ", timer last fire unknown"
            else:
                why += ", timer stale (%ds ago)" % int(age)
        return "drifted", why
    if unit_file == "disabled" and active == "active":
        return "drifted", "disabled but active"

    # 5. inactive
    if active == "inactive":
        if timer:
            if is_oneshot:
                return "off", "oneshot idle between fires"
            return "off", "scheduled by %s" % timer
        if watched and not is_oneshot:
            return "down", "inactive; no timer"
        return "off", "inactive"

    # 6. everything else
    if active == "active":
        return "ok", "active/%s" % (sub or "-")
    return "unknown", "state %s/%s" % (active or "?", sub or "-")


# --- envelope ----------------------------------------------------------------
def _envelope(*, ok: bool = True, available: bool = True,
              reason: Optional[str] = None, error: Optional[str] = None,
              bin_path: Optional[str] = None, systemd: Optional[str] = None,
              elapsed_ms: int = 0, watch: Sequence[str] = (),
              services: Sequence[Dict[str, Any]] = (),
              alerts: Sequence[Dict[str, Any]] = (),
              tally: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Every envelope carries every key (spec §4) — the JS branches on services.length."""
    counts = {"ok": 0, "warn": 0, "crit": 0, "off": 0}
    if tally is None:
        tally = [s.get("verdict") for s in services if isinstance(s, dict)]
    for verdict in tally:
        sev = _SEV.get(verdict, "warn")
        counts[sev] = counts.get(sev, 0) + 1
    env: Dict[str, Any] = {
        "ok": bool(ok),
        "available": bool(available),
        "scanned_at": time.time(),
        "probe": {
            "bin": bin_path,
            "scope": "--user",
            "systemd": systemd,
            "elapsed_ms": int(elapsed_ms),
            "reason": reason,
        },
        "watch": list(watch),
        "services": list(services),
        "alerts": list(alerts),
        "counts": counts,
        "restart": None,
    }
    if error is not None:
        env["error"] = error
    try:
        with _LOCK:
            env["restart"] = _LAST_RESTART
    except Exception:
        env["restart"] = None
    return env


# --- the probe ---------------------------------------------------------------
def _service_wall() -> Dict[str, Any]:
    t0 = time.time()
    bin_path = shutil.which("systemctl")
    if not bin_path:                                   # 1. missing -> empty state
        return _envelope(available=False, reason="systemctl_not_on_PATH",
                         elapsed_ms=_ms_since(t0))
    systemd = _systemd_version(bin_path)

    # 2. the unit table
    rc, out, err, status = _run(
        [bin_path, "--user", "list-units", "--type=service", "--all",
         "--no-legend", "--plain"], _T_LIST)
    if status != "ok" or rc != 0:
        available = status != "timeout"                # timeout = never answered
        return _envelope(available=False, reason=_fail_reason(status, rc, err or out),
                         bin_path=bin_path, systemd=systemd, elapsed_ms=_ms_since(t0)) \
            if not available else \
            _envelope(available=True, reason="timeout", bin_path=bin_path,
                      systemd=systemd, elapsed_ms=_ms_since(t0))
    rows = _parse_units(out)
    watch = _watchlist(rows)

    # 3. timers (pairing + seconds-to-next, best effort)
    reason: Optional[str] = None
    rc_t, out_t, err_t, status_t = _run(
        [bin_path, "--user", "list-timers", "--no-legend", "--no-pager"], _T_TIMERS)
    timer_map: Dict[str, str] = {}
    left_map: Dict[str, float] = {}
    if status_t == "ok" and rc_t == 0:
        timer_map, left_map = _parse_timers(out_t)
    elif status_t == "timeout":
        reason = "timeout"

    # 4. batched `show` sweep, hard budget, partial results kept (spec §2.1.3)
    recs: Dict[str, Dict[str, str]] = {}
    if watch:
        deadline = time.monotonic() + _BUDGET
        first = max(0.5, min(_T_SHOW, deadline - time.monotonic()))
        rc_s, out_s, _, status_s = _run(
            [bin_path, "--user", "show"] + list(watch) +
            ["-p", _SHOW_PROPS, "--no-pager"], first)
        if status_s == "ok" and rc_s == 0:
            for rec in _parse_props(out_s):
                uid = rec.get("Id")
                if uid:
                    recs[uid] = rec
        if len(recs) < len(watch):                     # batch unsupported/partial
            for unit in watch:
                if unit in recs:
                    continue
                left = deadline - time.monotonic()
                if left <= 0:
                    reason = "timeout"
                    break
                rc_u, out_u, _, status_u = _run(
                    [bin_path, "--user", "show", unit, "-p", _SHOW_PROPS, "--no-pager"],
                    min(_T_SHOW, left))
                if status_u == "timeout":
                    reason = "timeout"
                    break
                if status_u == "ok" and rc_u == 0:
                    for rec in _parse_props(out_u):
                        uid = rec.get("Id")
                        if uid:
                            recs[uid] = rec
        if len(recs) < len(watch) and reason is None:
            reason = "timeout"

    # 5. build rows (list-units data always; show props when they came back)
    now_us = _now_us()
    services: List[Dict[str, Any]] = []
    with _LOCK:
        prev_n = {unit: (samples[-1] if samples else None)
                  for unit, samples in _SAMPLES.items()}
        for unit in watch:
            rec = dict(recs.get(unit) or {})
            row = rows.get(unit) or {}
            if not rec:                                # show dropped this unit
                rec = {
                    "Id": unit,
                    "LoadState": row.get("load", ""),
                    "ActiveState": row.get("active", ""),
                    "SubState": row.get("sub", ""),
                    "Description": row.get("desc", ""),
                }
            for key, fallback in (("LoadState", "load"), ("ActiveState", "active"),
                                  ("SubState", "sub"), ("Description", "desc")):
                if not rec.get(key) and row.get(fallback):
                    rec[key] = row[fallback]

            uptime = _uptime_s(rec, now_us)
            timer = timer_map.get(unit)
            is_oneshot = (rec.get("Type") or "").lower() == "oneshot"
            age: Optional[float] = None
            interval: Optional[float] = None
            fresh = False
            if is_oneshot and timer:
                last_us = _last_run_us(rec)
                if last_us > 0 and now_us > 0:
                    age = round(max(0.0, (now_us - last_us) / 1e6), 1)
                left = left_map.get(unit)
                if age is not None and left is not None:
                    interval = round(age + left, 1)
                threshold = (max(60.0, 3.0 * interval) if interval
                             else _TIMER_FALLBACK_FRESH_S)
                fresh = age is not None and age <= threshold

            n_val: Optional[int] = None
            if "NRestarts" in rec:
                n_val = _int_or(rec.get("NRestarts"), 0)
            ring = _SAMPLES.setdefault(unit, [])
            if n_val is not None:
                ring.append(n_val)
                del ring[:-_SPARK_CAP]
            spark = list(ring)

            verdict, why = _verdict(
                rec, watched=True, timer=timer, fresh=fresh, age=age,
                interval=interval, prev_n=prev_n.get(unit), uptime=uptime)

            services.append({
                "unit": unit,
                "desc": rec.get("Description") or "",
                "load": rec.get("LoadState") or "",
                "active": rec.get("ActiveState") or "",
                "sub": rec.get("SubState") or "",
                "unit_file": rec.get("UnitFileState") or "",
                "type": rec.get("Type") or "",
                "n_restarts": n_val,
                "spark": spark,
                "uptime_s": uptime,
                "started_at": rec.get("ExecMainStartTimestamp") or "",
                "result": rec.get("Result") or "",
                "timer": timer,
                "last_fire_ago_s": age,
                "watched": True,
                "verdict": verdict,
                "verdict_why": why,
            })

    services.sort(key=lambda s: (_SEV_ORDER.get(_SEV.get(s["verdict"], "warn"), 4),
                                 s["unit"]))

    # 6. alerts: every non-ok watchlisted unit + every failed/restarting strays
    alerts: List[Dict[str, Any]] = []
    for svc in services:
        sev = _SEV.get(svc["verdict"], "warn")
        if sev in ("warn", "crit"):
            alerts.append({"unit": svc["unit"], "kind": svc["verdict"],
                           "sev": sev, "text": svc["verdict_why"]})
    tally: List[str] = [svc["verdict"] for svc in services]
    for unit, row in rows.items():
        if unit in set(watch):
            continue
        state = _simple_verdict(row.get("active", ""), row.get("sub", ""))
        tally.append(state)
        if state == "failed":
            alerts.append({"unit": unit, "kind": "failed", "sev": "crit", "text": "failed"})
        elif state == "restarting":
            alerts.append({"unit": unit, "kind": "restarting", "sev": "warn",
                           "text": "activating (%s)" % (row.get("sub") or "start")})
    alerts.sort(key=lambda a: (0 if a["sev"] == "crit" else 1, a["unit"]))

    return _envelope(available=True, reason=reason, bin_path=bin_path,
                     systemd=systemd, elapsed_ms=_ms_since(t0), watch=watch,
                     services=services, alerts=alerts, tally=tally)


# --- commands ----------------------------------------------------------------
def _cmd_services(arg: str = "") -> str:
    """Read-only wall probe. Always a JSON envelope; never raises (spec §2.1)."""
    try:
        return json.dumps(_service_wall())
    except Exception as exc:
        return json.dumps(_envelope(
            ok=False, available=False, reason="error",
            error="%s: %s" % (type(exc).__name__, exc), elapsed_ms=0))


def _active_state_after(bin_path: str, unit: str) -> Optional[str]:
    rc, out, _, status = _run(
        [bin_path, "--user", "show", unit, "-p", "ActiveState,SubState", "--no-pager"], 5.0)
    if status != "ok" or rc != 0:
        return None
    recs = _parse_props(out)
    return recs[0].get("ActiveState") or None if recs else None


def _cmd_service_restart(arg: str = "") -> str:
    """Allowlisted one-key restart. The allowlist gate runs before any spawn."""
    global _LAST_RESTART
    unit = (arg or "").strip()
    t0 = time.time()
    try:
        if not _allowlisted(unit):                     # NO subprocess past this point
            return json.dumps({"ok": False, "reason": "not_allowlisted"})
        bin_path = shutil.which("systemctl")
        if not bin_path:
            return json.dumps({
                "ok": False, "unit": unit, "action": "restart",
                "reason": "systemctl_not_on_PATH", "detail": "",
                "elapsed_ms": _ms_since(t0)})
        rc, out, err, status = _run(
            [bin_path, "--user", "restart", unit], _T_RESTART)
        elapsed = _ms_since(t0)
        if status == "timeout":
            receipt: Dict[str, Any] = {
                "ok": False, "unit": unit, "action": "restart", "reason": "timeout",
                "detail": "systemctl --user restart timed out after 30s",
                "elapsed_ms": elapsed}
        elif status == "missing":
            receipt = {
                "ok": False, "unit": unit, "action": "restart",
                "reason": "systemctl_not_on_PATH", "detail": "",
                "elapsed_ms": elapsed}
        elif status != "ok" or rc != 0:
            lines = [ln for ln in ((err or out) or "").splitlines() if ln.strip()]
            receipt = {
                "ok": False, "unit": unit, "action": "restart",
                "reason": ("exit_%d" % rc) if rc is not None else "exit_-1",
                "detail": lines[0] if lines else "", "elapsed_ms": elapsed}
        else:
            receipt = {
                "ok": True, "unit": unit, "action": "restart", "exit": 0,
                "elapsed_ms": elapsed,
                "verdict_after": _active_state_after(bin_path, unit),
                "scanned_at": time.time()}
        with _LOCK:
            _LAST_RESTART = {
                "unit": unit, "ok": bool(receipt.get("ok")),
                "reason": receipt.get("reason"),
                "at": time.time(), "elapsed_ms": receipt.get("elapsed_ms")}
        return json.dumps(receipt)
    except Exception as exc:
        return json.dumps({
            "ok": False, "unit": unit, "action": "restart",
            "reason": type(exc).__name__, "detail": str(exc),
            "elapsed_ms": _ms_since(t0)})


def register(ctx: Any) -> None:
    """Called once at plugin load: commands only, each isolated by try/except."""
    try:
        ctx.register_command(
            "day-services", _cmd_services,
            description="Service Wall: systemd user-unit states, uptime, restarts, verdicts.",
            args_hint="[list]",
        )
    except Exception:
        pass
    try:
        ctx.register_command(
            "day-service-restart", _cmd_service_restart,
            description="Restart one allowlisted Hermes unit (returns JSON receipt).",
            args_hint="<unit>",
        )
    except Exception:
        pass
