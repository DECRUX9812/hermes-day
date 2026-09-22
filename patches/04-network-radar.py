"""Lane 04 — Network Radar (``/day-net``).

One command that answers "every endpoint this fleet depends on, with the real
latency behind each answer". Loaded by ``patches/__init__.py::register_all`` at
plugin load; registers **commands only** (never a hook, never a fail-closed
gate), so it structurally cannot sit on — or delay — the tool loop.

Honesty rules this module is built around (they override everything else):

* a failed, refused, timed-out or unreachable probe returns ``ms: None``.
  There is no code path that writes ``0``, a pseudo-latency or a last-known
  number for a probe that did not produce one. The only place a previous real
  sample is carried forward is the deadline path, and it is marked
  ``stale: true`` with its original ``checked_at``.
* the tunnel hostname is resolved **live** from the running cloudflared pid:
  ``/proc/<pid>/fd/1`` (or ``fd/2``) -> log tail -> last
  ``https://<x>.trycloudflare.com``. Never a glob of /tmp or scratch: those
  contain dead tunnels (``/tmp/cf-clean/cf.log`` holds a stale hostname today).
* probe kind is always labelled (``tcp_connect`` / ``http`` /
  ``tailscale_ping``) so no UI can draw one axis across mixed metrics.
* "up" for an endpoint or the tunnel is only ever produced by a probe that
  succeeded. For peers, "up"/"down" comes from a real ``tailscale status --json``
  read (``Online``/``LastSeen``) and ``ms`` is ``None`` unless a
  ``tailscale_ping`` sample was actually taken — so the invariant
  ``ms is None iff status != "up"`` holds for ``endpoints[]`` and ``tunnel``,
  and for ``peers[]`` whenever a latency probe ran. No number is ever invented
  to make it hold.

Cache: ``<hermes home>/plugin-data/hermes-day/netradar.json``, TTL 30 s by
default (``netradar.ttl_s`` via ``ctx.get_config``), atomic write. If the cache
directory is unwritable the cache layer degrades to an *empty* state
(``degraded: ["cache_unwritable"]``, ``cache.fresh=false``, no in-memory mirror
is kept) rather than serving state it cannot persist or verify.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# Module-owned state. Deliberately NOT the main module's _LOCK: patch modules
# are loaded by path and must not reach into the monolith's internals.
_CTX: Any = None
_LOCK = threading.RLock()
_NET: Dict[str, Any] = {}

_TTL_DEFAULT_S = 30.0          # spec §3.4: netradar.ttl_s, default 30 s
_TTL_MIN_S = 1.0
_TTL_MAX_S = 600.0
_SOFT_BUDGET_MS = 3000         # spec §3.3 soft budget (RPC_TIMEOUT is 9000)
_HISTORY_MAX = 40              # spec §3.2: history ring of <=40
_PROBE_TIMEOUT_S = 0.5         # socket.settimeout per spec §3.3
_SS_TIMEOUT_S = 1.0
_TS_JSON_TIMEOUT_S = 2.0
_TS_TEXT_TIMEOUT_S = 1.0
_TUNNEL_CURL_TIMEOUT_S = 1.5
_DEEP_CURL_TIMEOUT_S = 1.0
_DEEP_PING_TIMEOUT_S = 2.0
_PING_MIN_REMAINING_S = 0.6    # do not start a stage with less budget left
_ERR_MAX_AGE_S = 900.0         # surface a tunnel log error only if it is recent
_LOG_TAIL_BYTES = 262144       # read the tail, never the whole growing log
_CURL_BINARY = "curl"          # http probe binary (§2.3: curl -m 1.5 / -m 2)

# The 7 ports the brief names — always present in the payload, even when ss
# cannot see them (then bind/transport stay null rather than being guessed).
_BRIEF_PORTS: Tuple[int, ...] = (8611, 8787, 8790, 8899, 9119, 9130, 8099)

_TC_RE = re.compile(r'users:\(\("([^"]+)",pid=(\d+)')
_TS_PONG_RE = re.compile(r"\bin\s+(\d+(?:\.\d+)?)\s*ms\b")
_CF_URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")
_CF_ERR_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T[0-9:.+\-Z]+)\s+ERR\s+(.*)$")
_CF_REFUSED_RE = re.compile(r"dial tcp [^\r\n\"']*connection refused")
_TS_TXRX_RE = re.compile(r",\s*tx \d+ rx \d+\s*$")
_OS_PREFIX_RE = re.compile(
    r"^(?:linux|windows|macos|ios|android|docker|chromeos|openwrt|freebsd"
    r"|unknown)\s+", re.IGNORECASE)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _iso(ts: Optional[float] = None) -> str:
    """UTC ISO-8601 with milliseconds and a Z suffix."""
    try:
        dt = (datetime.fromtimestamp(ts, tz=timezone.utc) if ts is not None
              else datetime.now(timezone.utc))
        return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except Exception:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _epoch(value: Any) -> Optional[float]:
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _hostname() -> str:
    try:
        return socket.gethostname() or "unknown"
    except Exception:
        return "unknown"


def _run(cmd: List[str], timeout: float) -> Optional[subprocess.CompletedProcess]:
    """Bounded subprocess. ``None`` = never ran / failed to start (not rc!=0)."""
    try:
        return subprocess.run(
            list(cmd), timeout=timeout, capture_output=True,
            text=True, errors="replace",
        )
    except Exception:
        return None


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        if f == f and f >= 0:          # not NaN, non-negative
            return f
    return None


def _clamp_ms(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Enforce: status == 'up' requires a real number, anything else -> None."""
    status = rec.get("status")
    ms = _num(rec.get("ms"))
    if status == "up":
        if ms is None:
            rec["status"] = "unknown"
            rec["ms"] = None
        else:
            rec["ms"] = round(ms, 3)
    else:
        rec["ms"] = None
    return rec


# ---------------------------------------------------------------------------
# cache layer
# ---------------------------------------------------------------------------

def _cache_path() -> str:
    try:
        from hermes_constants import get_hermes_home
        base = os.path.join(get_hermes_home(), "plugin-data", "hermes-day")
    except Exception:
        base = os.path.join(os.path.expanduser("~"), ".hermes", "plugin-data",
                            "hermes-day")
    return os.path.join(base, "netradar.json")


def _cache_dir_writable(path: str) -> bool:
    """Can the cache dir be written (created if its parent allows)?"""
    try:
        d = os.path.dirname(path)
        if os.path.isdir(d):
            return os.access(d, os.W_OK)
        parent = os.path.dirname(d)
        return os.path.isdir(parent) and os.access(parent, os.W_OK)
    except Exception:
        return False


def _read_cache_file(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("endpoints"), list):
            return data
    except Exception:
        return None
    return None


def _write_cache_file(path: str, payload: Dict[str, Any]) -> bool:
    """Atomic write (mkstemp in the target dir + os.replace). False on any
    failure — including an unwritable cache dir — so the caller can degrade."""
    tmp = None
    try:
        d = os.path.dirname(path)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".netradar-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"))
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except Exception:
                pass
        os.replace(tmp, path)
        tmp = None
        return True
    except Exception:
        return False
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass


def _load_memory() -> None:
    """Cold start: seed the in-memory mirror from the durable file (spec §3.4)."""
    try:
        data = _read_cache_file(_cache_path())
        if data:
            with _LOCK:
                _NET.clear()
                _NET.update(data)
    except Exception:
        pass


def _ttl_s() -> float:
    try:
        cfg = None
        if _CTX is not None:
            try:
                cfg = _CTX.get_config("netradar")
            except Exception:
                cfg = None
        raw = None
        if isinstance(cfg, dict):
            raw = cfg.get("ttl_s", cfg.get("ttl"))
        elif isinstance(cfg, (int, float)) and not isinstance(cfg, bool):
            raw = cfg
        if raw is not None:
            f = float(raw)
            if f == f and f > 0:
                return max(_TTL_MIN_S, min(_TTL_MAX_S, f))
    except Exception:
        pass
    return _TTL_DEFAULT_S


def _memory_get() -> Optional[Dict[str, Any]]:
    with _LOCK:
        if _NET:
            return dict(_NET)
    return None


def _memory_put(payload: Dict[str, Any]) -> None:
    with _LOCK:
        _NET.clear()
        _NET.update(payload)


def _memory_clear() -> None:
    with _LOCK:
        _NET.clear()


# ---------------------------------------------------------------------------
# discovery: listeners, transports, tailscale
# ---------------------------------------------------------------------------

def _split_local(addr: str) -> Tuple[Optional[str], Optional[int]]:
    """'0.0.0.0:8611' / '[::]:80' / '127.0.0.1:631' -> (host, port)."""
    try:
        if addr.startswith("["):
            close = addr.find("]")
            if close < 0 or close + 1 >= len(addr) or addr[close + 1] != ":":
                return None, None
            host = addr[1:close]
            port_s = addr[close + 2:]
        else:
            if addr.count(":") != 1:
                return None, None
            host, port_s = addr.split(":", 1)
        if not port_s.isdigit():
            return None, None
        host = host.strip()
        if host in ("", "*"):
            host = "0.0.0.0"
        return host, int(port_s)
    except Exception:
        return None, None


def _classify(bind: Optional[str], ts_ip: Optional[str]) -> Optional[str]:
    """Four real transport classes (spec §2.1). ``None`` when the bind is
    genuinely unknown — the UI renders that as 'unknown', never a guess."""
    if not bind:
        return None
    if bind in ("0.0.0.0", "::"):
        return "any"
    if bind.startswith("127.") or bind == "::1":
        return "localhost"
    if ts_ip and bind == ts_ip:
        return "tailscale"
    # concrete non-loopback address that is not the tailnet addr: reachable
    # beyond loopback, so 'any' is the honest bucket of the four.
    return "any"


def _probe_host(bind: Optional[str], ts_ip: Optional[str]) -> str:
    """Where to connect to measure this listener.

    loopback bind -> that loopback addr; tailnet bind -> tailscale ip;
    wildcard bind -> tailscale ip when known else 127.0.0.1 (both are real
    local routes; a refused/filtered connect still yields ms: None).
    Unknown bind (ss unavailable) -> tailscale ip if known else 127.0.0.1:
    every brief port except the loopback ones is reachable via the tailnet addr.
    """
    if bind and (bind.startswith("127.") or bind == "::1"):
        return bind
    if bind and ts_ip and bind == ts_ip:
        return ts_ip
    if bind and bind not in ("0.0.0.0", "::"):
        return bind
    return ts_ip or "127.0.0.1"


def _ss_listeners() -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Returns (rows, degraded_reason). rows == [] with reason set = ss failed."""
    for cmd, to in ((["ss", "-ltnp"], _SS_TIMEOUT_S), (["ss", "-ltn"], _SS_TIMEOUT_S)):
        cp = _run(cmd, timeout=to)
        if cp is None or cp.returncode != 0:
            continue
        rows: List[Dict[str, Any]] = []
        for line in (cp.stdout or "").splitlines()[1:]:
            parts = line.split()
            if len(parts) < 4:
                continue
            bind, port = _split_local(parts[3])
            if port is None:
                continue
            proc, pid = None, None
            m = _TC_RE.search(line)
            if m:
                proc, pid = m.group(1), int(m.group(2))
            rows.append({"bind": bind, "port": port, "process": proc, "pid": pid})
        if rows:
            # -ltnp carried process info or not, the listener set is what we
            # need; a second run with -ltn only matters if the first failed.
            return rows, None
    return [], "ss_unavailable"


def _tailscale_ip() -> Optional[str]:
    cp = _run(["tailscale", "ip", "-4"], timeout=_SS_TIMEOUT_S)
    if cp is None or cp.returncode != 0:
        return None
    for line in (cp.stdout or "").splitlines():
        s = line.strip()
        if s and ":" not in s:
            return s
    return None


def _tailscale_peers(ts_ip: Optional[str]
                     ) -> Tuple[Optional[str], Optional[str], List[Dict[str, Any]], List[str]]:
    """(self_host, ts_ip, peers, degraded). One structured read + one text read
    for the verbatim 'offline, last seen 16d ago' strings. No ping here, ever.
    Also back-fills the tailscale IP from Self when `tailscale ip -4` failed."""
    degraded: List[str] = []
    cp = _run(["tailscale", "status", "--json"], timeout=_TS_JSON_TIMEOUT_S)
    if cp is None or cp.returncode != 0:
        return None, ts_ip, [], ["tailscale_status_failed"]
    try:
        data = json.loads(cp.stdout or "{}")
    except Exception:
        return None, ts_ip, [], ["tailscale_status_failed"]
    if not isinstance(data, dict):
        return None, ts_ip, [], ["tailscale_status_failed"]

    self_host = None
    try:
        me = data.get("Self") or {}
        if isinstance(me, dict):
            self_host = me.get("HostName") or None
            if not ts_ip:
                ips = me.get("TailscaleIPs") or []
                for ip in ips:
                    if isinstance(ip, str) and ":" not in ip:
                        ts_ip = ip
                        break
    except Exception:
        pass

    # text table: gives the human 'last seen' strings verbatim
    text_by_ip: Dict[str, str] = {}
    cp2 = _run(["tailscale", "status"], timeout=_TS_TEXT_TIMEOUT_S)
    if cp2 is None or cp2.returncode != 0:
        degraded.append("tailscale_status_text_failed")
    else:
        for line in (cp2.stdout or "").splitlines():
            cols = line.split(None, 3)
            if len(cols) < 4 or not re.match(r"^\d+\.\d+\.\d+\.\d+$", cols[0]):
                continue
            # columns are: ip  hostname  user@  os  <activity> — keep only the
            # activity, so 'linux    offline, last seen 16d ago' loses 'linux'
            # but a plain '-' status survives untouched.
            raw = _TS_TXRX_RE.sub("", cols[3]).strip()
            body = _OS_PREFIX_RE.sub("", raw).strip()
            text_by_ip[cols[0]] = body or raw

    peers: List[Dict[str, Any]] = []
    raw_peers = data.get("Peer")
    if isinstance(raw_peers, dict):
        for key, p in raw_peers.items():
            if not isinstance(p, dict):
                continue
            ip = None
            for cand in (p.get("TailscaleIPs") or []):
                if isinstance(cand, str) and ":" not in cand:
                    ip = cand
                    break
            online = bool(p.get("Online"))
            last_seen = str(p.get("LastSeen") or "")
            name = str(p.get("HostName") or key)
            if ip in text_by_ip:
                last_seen_text = text_by_ip[ip]
            elif online:
                last_seen_text = "active"
            else:
                # Go zero value means "unknown", not "now" (spec §2.4)
                le = _epoch(last_seen)
                if not le or le < 946684800:      # < 2000-01-01 => zero value
                    last_seen_text = "offline, last seen unknown"
                else:
                    secs = max(0.0, time.time() - le)
                    if secs < 3600:
                        txt = "%dm ago" % int(secs // 60)
                    elif secs < 86400:
                        txt = "%dh ago" % int(secs // 3600)
                    else:
                        txt = "%dd ago" % int(secs // 86400)
                    last_seen_text = "offline, last seen " + txt
            peers.append({
                "host": name,
                "ip": ip,
                "online": online,
                "last_seen": last_seen or None,
                "last_seen_text": last_seen_text,
            })
    peers.sort(key=lambda r: (r.get("host") or "").lower())
    return self_host, ts_ip, peers, degraded


# ---------------------------------------------------------------------------
# the cloudflared trycloudflare tunnel — resolved from the LIVE pid
# ---------------------------------------------------------------------------

def _readlink(path: str) -> Optional[str]:
    try:
        target = os.readlink(path)
        return target if target and target.startswith("/") else None
    except Exception:
        return None


def _cmdline(pid: int) -> List[str]:
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            raw = fh.read()
        return [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]
    except Exception:
        return []


def _find_cloudflared() -> Optional[int]:
    """Live pid of the trycloudflare tunnel process. Never a stale record."""
    cp = _run(["pgrep", "-f", "cloudflared"], timeout=1.0)
    if cp is None or cp.returncode != 0:
        return None
    for tok in (cp.stdout or "").split():
        if not tok.strip().isdigit():
            continue
        pid = int(tok)
        args = _cmdline(pid)
        joined = " ".join(args)
        if "cloudflared" in joined and "tunnel" in joined:
            return pid
    return None


def _tail_bytes(path: str, size: int = _LOG_TAIL_BYTES) -> str:
    """Last `size` bytes of the tunnel log (it grows; never read it whole)."""
    try:
        with open(path, "rb") as fh:
            try:
                fh.seek(0, os.SEEK_END)
                total = fh.tell()
                start = max(0, total - size)
                fh.seek(start)
            except Exception:
                start = 0
            blob = fh.read()
        text = blob.decode("utf-8", "replace")
        return text, start > 0
    except Exception:
        return "", False


def _last_cf_url(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    text, truncated = _tail_bytes(path)
    if not text:
        return None
    matches = list(_CF_URL_RE.finditer(text))
    if not matches:
        return None
    # a match at offset 0 of a truncated tail may be cut in half — drop it
    if truncated and matches[0].start() == 0 and len(matches) > 1:
        return matches[-1].group(0)
    return matches[-1].group(0)


def _last_tunnel_error(path: Optional[str]) -> Optional[str]:
    """Most recent *recent* ERR line (spec §2.2), e.g. the origin-refused dial.
    A stale error from hours ago is not reported as if it were live."""
    if not path:
        return None
    text, _ = _tail_bytes(path)
    if not text:
        return None
    refused = None
    generic = None
    for line in reversed(text.splitlines()):
        m = _CF_ERR_RE.match(line.strip())
        if not m:
            continue
        ts = _epoch(m.group(1))
        if ts is None or (time.time() - ts) > _ERR_MAX_AGE_S:
            continue
        body = m.group(2).strip()
        if "ERR" in body:
            body = body.split("ERR", 1)[-1].strip()
        if generic is None and body:
            generic = body
        if refused is None and _CF_REFUSED_RE.search(body):
            refused = _CF_REFUSED_RE.search(body).group(0)
            break
    return refused or generic


def _tunnel_info() -> Dict[str, Any]:
    """Everything about the tunnel except the HTTP probe (that is timed)."""
    out: Dict[str, Any] = {
        "pid": None, "origin": None, "log": None, "url": None,
        "last_log_error": None, "err": None,
    }
    pid = _find_cloudflared()
    if pid is None:
        out["err"] = "cloudflared: no live process"
        return out
    out["pid"] = pid

    args = _cmdline(pid)
    for i, a in enumerate(args):
        if a == "--url" and i + 1 < len(args):
            out["origin"] = args[i + 1]
            break
        if a.startswith("--url="):
            out["origin"] = a.split("=", 1)[1]
            break

    # stdout first, stderr as the equivalent fallback — live, per pid
    log = _readlink("/proc/%d/fd/1" % pid) or _readlink("/proc/%d/fd/2" % pid)
    out["log"] = log
    if not log:
        out["err"] = "cloudflared: no readable stdout log"
        return out

    url = _last_cf_url(log)
    out["url"] = url
    if not url:
        out["err"] = "cloudflared: no trycloudflare URL in live log"
        return out
    out["last_log_error"] = _last_tunnel_error(log)
    return out


# ---------------------------------------------------------------------------
# probes
# ---------------------------------------------------------------------------

def _tcp_probe(host: str, port: int,
               timeout: float = _PROBE_TIMEOUT_S) -> Dict[str, Any]:
    """TCP-connect latency. Success -> real ms. ANY failure -> ms: None.

    Zone suffixes ('127.0.0.53%lo') are stripped for connect and an IPv6
    literal gets an AF_INET6 socket — otherwise the probe itself fails with
    gaierror and would report a *tool* failure as if the service were down.
    """
    target = str(host or "").split("%", 1)[0].strip()
    t0 = time.perf_counter()
    try:
        sock = socket.socket(socket.AF_INET6 if ":" in target else socket.AF_INET,
                             socket.SOCK_STREAM)
    except Exception as exc:
        return {"status": "unknown", "ms": None,
                "err": "%s: %s" % (type(exc).__name__, exc)}
    sock.settimeout(timeout)
    try:
        sock.connect((target, port))
        ms = round((time.perf_counter() - t0) * 1000, 3)
        return {"status": "up", "ms": ms, "err": None}
    except Exception as exc:
        return {"status": "down", "ms": None,
                "err": "%s: %s" % (type(exc).__name__, exc)}
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _curl_probe(url: str, timeout_s: float) -> Dict[str, Any]:
    """HTTP latency. curl prints time_total even when it failed — that number
    is only reported when curl exited 0 AND the status line is a real code."""
    cp = _run([
        _CURL_BINARY, "-sS", "-o", "/dev/null",
        "-m", "%.2f" % timeout_s,
        "-w", "%{http_code} %{time_total}", url,
    ], timeout=timeout_s + 0.5)
    if cp is None:
        return {"ms": None, "http_status": None, "err": "curl: unavailable"}
    parts = (cp.stdout or "").split()
    code = None
    total = None
    try:
        if parts and parts[0].isdigit():
            code = int(parts[0])
        if len(parts) > 1:
            total = float(parts[1])
    except Exception:
        pass
    if cp.returncode != 0 or code in (None, 0):
        err_lines = [l for l in (cp.stderr or "").splitlines() if l.strip()]
        err = err_lines[-1].strip() if err_lines else "curl exit %d" % cp.returncode
        return {"ms": None, "http_status": code, "err": err}
    if total is None:
        return {"ms": None, "http_status": code, "err": "curl: no timing"}
    return {"ms": round(total * 1000, 3), "http_status": code, "err": None}


def _tailscale_ping(ip: str) -> Dict[str, Any]:
    """Opt-in (``deep``) peer latency. GNU timeout hard-kills it. A timeout or
    'timed out' line yields ms: None — never a parsed or derived number."""
    cmd = ["timeout", "%.1f" % _DEEP_PING_TIMEOUT_S,
           "tailscale", "ping", "-c", "1", ip]
    cp = _run(cmd, timeout=_DEEP_PING_TIMEOUT_S + 0.5)
    if cp is None:
        return {"ms": None, "err": "tailscale ping: unavailable"}
    text = ((cp.stdout or "") + "\n" + (cp.stderr or "")).strip()
    m = _TS_PONG_RE.search(text) if cp.returncode == 0 else None
    if m:
        return {"ms": float(m.group(1)), "err": None}
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return {"ms": None, "err": lines[-1] if lines else "tailscale ping: failed"}


def _push_history(prev: Any, sample: Optional[float]) -> List[Optional[float]]:
    ring: List[Optional[float]] = []
    if isinstance(prev, list):
        for v in prev[-(_HISTORY_MAX - 1):]:
            ring.append(_num(v))       # non-numeric history entries become gaps
    if sample is None:
        ring.append(None)
    else:
        ring.append(_num(sample))
    return ring[-_HISTORY_MAX:]


# ---------------------------------------------------------------------------
# the probe cycle
# ---------------------------------------------------------------------------

def _endpoint_rows(listeners: List[Dict[str, Any]],
                   ts_ip: Optional[str]) -> List[Dict[str, Any]]:
    """Union of discovered listeners and the 7 brief ports (dedup by bind:port).
    A brief port with no listener row still appears — as an unbound row whose
    transport stays null instead of being guessed."""
    rows: List[Dict[str, Any]] = []
    seen_ports = set()
    for r in listeners:
        bind = r.get("bind")
        port = r.get("port")
        if port is None:
            continue
        rows.append({
            "id": "%s:%d" % (bind or "?", port),
            "host": _probe_host(bind, ts_ip),
            "port": port,
            "bind": bind,
            "transport": _classify(bind, ts_ip),
            "process": r.get("process"),
            "pid": r.get("pid"),
            "default": port in _BRIEF_PORTS,
        })
        seen_ports.add(port)
    for port in _BRIEF_PORTS:
        if port in seen_ports:
            continue
        rows.append({
            "id": "?:%d" % port,
            "host": ts_ip or "127.0.0.1",
            "port": port,
            "bind": None,
            "transport": None,
            "process": None,
            "pid": None,
            "default": True,
        })
    # stable, brief-ports-first ordering
    rows.sort(key=lambda r: (0 if r["default"] else 1, r["port"], r["id"]))
    return rows


def _probe_cycle(deep: bool, prev: Optional[Dict[str, Any]],
                 path: str, ttl: float) -> Dict[str, Any]:
    degraded: List[str] = []
    t_start = time.monotonic()
    deadline = t_start + _SOFT_BUDGET_MS / 1000.0

    def remaining() -> float:
        return deadline - time.monotonic()

    prev_eps = {}
    prev_peers = {}
    prev_tunnel = None
    if isinstance(prev, dict):
        for e in (prev.get("endpoints") or []):
            if isinstance(e, dict) and e.get("id"):
                prev_eps[e["id"]] = e
        for p in (prev.get("peers") or []):
            if isinstance(p, dict) and p.get("ip"):
                prev_peers[p["ip"]] = p
        if isinstance(prev.get("tunnel"), dict):
            prev_tunnel = prev["tunnel"]

    # --- stage 1: discovery -------------------------------------------------
    ts_ip = _tailscale_ip() if remaining() > 0.2 else None
    listeners, ss_reason = _ss_listeners()
    if ss_reason:
        degraded.append(ss_reason)
    self_host = None
    peers: List[Dict[str, Any]] = []
    if remaining() > 0.5:
        self_host, ts_ip_found, peers, peer_reasons = _tailscale_peers(ts_ip)
        ts_ip = ts_ip or ts_ip_found       # Self.TailscaleIPs as the fallback
        degraded.extend(peer_reasons)
    else:
        degraded.append("probe_deadline")
    if ts_ip is None and not any("tailscale" in d for d in degraded):
        degraded.append("tailscale_ip_failed")

    rows = _endpoint_rows(listeners, ts_ip)

    # --- stage 2: TCP-connect latency (parallel, bounded) -------------------
    probed: Dict[str, Dict[str, Any]] = {}
    now_stamp = _iso()
    budget_left = remaining()
    if budget_left > 0.3 and rows:
        futures = {}
        executor = ThreadPoolExecutor(max_workers=16)
        try:
            for row in rows:
                futures[executor.submit(_tcp_probe, row["host"], row["port"])] = row["id"]
            pending = list(futures.keys())
            done, not_done = __import__("concurrent.futures", fromlist=["wait"]).wait(
                pending, timeout=max(0.0, budget_left - 0.1))
            for fut in done:
                try:
                    probed[futures[fut]] = fut.result()
                except Exception:
                    probed[futures[fut]] = {"status": "unknown", "ms": None,
                                            "err": "probe: internal error"}
            if not_done:
                degraded.append("probe_deadline")
        except Exception:
            degraded.append("probe_error")
        finally:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
    elif rows:
        degraded.append("probe_deadline")

    # --- stage 3: tunnel (URL live from the pid, then one HTTP probe) -------
    tun = _tunnel_info()
    tunnel_prev = prev_tunnel or {}
    tunnel: Dict[str, Any] = {
        "transport": "cloudflared",
        "url": tun.get("url"),
        "copy": tun.get("url"),
        "pid": tun.get("pid"),
        "origin": tun.get("origin"),
        "log": tun.get("log"),
        "status": "unknown",
        "substates": {"edge": "unknown", "origin": "unknown"},
        "http_status": None,
        "ms": None,
        "probe": None,
        "checked_at": None,
        "stale": False,
        "err": tun.get("err"),
        "last_log_error": tun.get("last_log_error"),
    }
    if tunnel["url"] is None:
        tunnel["status"] = "unknown"
        tunnel["ms"] = None
    elif remaining() < 0.4:
        # deadline: keep the previous real sample, clearly stale — never a new one
        degraded.append("probe_deadline")
        tunnel["status"] = "unknown"
        tunnel["ms"] = None
        if tunnel_prev:
            pm = _num(tunnel_prev.get("ms"))
            if pm is not None and tunnel_prev.get("status") == "up":
                tunnel["ms"] = pm
                tunnel["http_status"] = tunnel_prev.get("http_status")
                tunnel["probe"] = tunnel_prev.get("probe") or "http"
                tunnel["checked_at"] = tunnel_prev.get("checked_at")
                tunnel["status"] = "up"
                tunnel["stale"] = True
    else:
        res = _curl_probe(tunnel["url"], _TUNNEL_CURL_TIMEOUT_S)
        tunnel["probe"] = "http"
        tunnel["http_status"] = res.get("http_status")
        tunnel["err"] = res.get("err")
        if res.get("ms") is None:
            # unreachable edge (or curl missing) -> no number, ever
            if res.get("err") == "curl: unavailable":
                tunnel["status"] = "unknown"
            else:
                tunnel["status"] = "down"
                tunnel["substates"] = {"edge": "down", "origin": "unknown"}
            tunnel["ms"] = None
        else:
            tunnel["ms"] = res.get("ms")
            tunnel["checked_at"] = _iso()
            code = res.get("http_status")
            origin_down = (code is not None and code >= 500) or bool(
                tunnel["last_log_error"]
                and "refused" in str(tunnel["last_log_error"]))
            if origin_down:
                tunnel["status"] = "degraded"   # tunnel up / origin down
                tunnel["substates"] = {"edge": "up", "origin": "down"}
            else:
                tunnel["status"] = "up"
                tunnel["substates"] = {"edge": "up", "origin": "up"}
        _clamp_ms(tunnel)
    if tunnel["status"] != "up" and tunnel["url"] is not None and tunnel["checked_at"] is None:
        tunnel["checked_at"] = _iso()
        _clamp_ms(tunnel)

    # --- stage 4: endpoints (+ history merge) -------------------------------
    endpoints: List[Dict[str, Any]] = []
    for row in rows:
        rid = row["id"]
        result = probed.get(rid)
        prev_rec = prev_eps.get(rid) or {}
        prev_hist = prev_rec.get("history")
        if result is not None:
            hist = _push_history(prev_hist, result.get("ms"))
            rec = {
                **row,
                "status": result.get("status"),
                "probe": "tcp_connect",
                "ms": result.get("ms"),
                "err": result.get("err"),
                "checked_at": now_stamp,
                "stale": False,
                "history": hist,
            }
        elif prev_rec:
            # missed probe: carry the previous REAL sample forward, marked stale
            rec = {
                **row,
                "status": prev_rec.get("status") or "unknown",
                "probe": prev_rec.get("probe"),
                "ms": _num(prev_rec.get("ms")),
                "err": prev_rec.get("err"),
                "checked_at": prev_rec.get("checked_at"),
                "stale": True,
                "history": prev_hist if isinstance(prev_hist, list) else [None],
            }
        else:
            rec = {
                **row,
                "status": "unknown",
                "probe": None,
                "ms": None,
                "err": "probe skipped (deadline)",
                "checked_at": None,
                "stale": True,
                "history": [None],
            }
        endpoints.append(_clamp_ms(rec))

    # --- stage 5: deep only — HTTP per endpoint + tailscale ping per peer ---
    if deep:
        if remaining() > 0.3:
            http_targets = [e for e in endpoints if e.get("url") is None and e.get("host")]
            futures = {}
            executor = ThreadPoolExecutor(max_workers=16)
            try:
                for e in http_targets:
                    scheme = "https" if e["port"] in (443, 47984, 47989, 47990) else "http"
                    url = "%s://%s:%d/" % (scheme, e["host"], e["port"])
                    futures[executor.submit(_curl_probe, url, _DEEP_CURL_TIMEOUT_S)] = e["id"]
                done, not_done = __import__("concurrent.futures", fromlist=["wait"]).wait(
                    list(futures.keys()), timeout=max(0.0, remaining() - 0.1))
                by_id = {e["id"]: e for e in endpoints}
                for fut in done:
                    e = by_id.get(futures[fut])
                    if e is None:
                        continue
                    try:
                        r = fut.result()
                    except Exception:
                        r = {"ms": None, "http_status": None, "err": "probe: internal error"}
                    # a separate, separately-labelled metric — never history
                    e["http_ms"] = r.get("ms")
                    e["http_status"] = r.get("http_status")
                    e["http_err"] = r.get("err")
                if not_done:
                    degraded.append("probe_deadline")
            except Exception:
                degraded.append("probe_error")
            finally:
                try:
                    executor.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
        else:
            degraded.append("probe_deadline")

        for peer in peers:
            if not peer.get("online") or not peer.get("ip"):
                continue
            if remaining() < _PING_MIN_REMAINING_S:
                degraded.append("probe_deadline")
                peer["ms"] = None
                peer["probe"] = "tailscale_status"
                peer["pinged"] = False
                peer["err"] = None
                peer["checked_at"] = _iso()
                peer["history"] = _push_history(
                    (prev_peers.get(peer["ip"]) or {}).get("history"), None)
                continue
            res = _tailscale_ping(peer["ip"])
            peer["pinged"] = True
            peer["ms"] = res.get("ms")
            peer["err"] = res.get("err")
            peer["probe"] = "tailscale_ping"
            peer["checked_at"] = _iso()
            peer["history"] = _push_history(
                (prev_peers.get(peer["ip"]) or {}).get("history"), res.get("ms"))

    # default path: status-only peers — up/down is authoritative, ms stays null
    for peer in peers:
        peer.setdefault("pinged", False)
        if "ms" not in peer:
            peer["ms"] = None
        peer.setdefault("err", None)
        peer.setdefault("checked_at", None)
        if peer.get("online"):
            peer["status"] = "up"
            peer.setdefault("probe", "tailscale_status")
        else:
            peer["status"] = "down"
            peer["ms"] = None
            peer["probe"] = None
            peer["err"] = "tailscale: offline"
        pr = prev_peers.get(peer.get("ip")) or {}
        peer["history"] = _push_history(
            pr.get("history"), peer.get("ms") if peer.get("pinged") else None) \
            if peer.get("pinged") else (pr.get("history") if isinstance(pr.get("history"), list)
                                        else [None])
        if peer["status"] != "up":
            peer["ms"] = None

    # --- totals -------------------------------------------------------------
    up = sum(1 for e in endpoints if e["status"] == "up")
    down = sum(1 for e in endpoints if e["status"] == "down")
    unknown = len(endpoints) - up - down

    if time.monotonic() > deadline and "probe_deadline" not in degraded:
        degraded.append("probe_deadline")

    payload = {
        "ok": True,
        "generated_at": _iso(),
        "cache": {"path": path, "age_s": 0, "ttl_s": ttl, "fresh": True},
        "self": {"host": self_host or _hostname(), "tailscale_ip": ts_ip},
        "totals": {"endpoints": len(endpoints), "up": up, "down": down,
                   "unknown": unknown, "degraded": bool(degraded)},
        "tunnel": tunnel,
        "endpoints": endpoints,
        "peers": peers,
        "degraded": degraded,
    }
    return payload


# ---------------------------------------------------------------------------
# command surface
# ---------------------------------------------------------------------------

def _empty_payload(path: str, ttl: float, degraded: List[str]) -> Dict[str, Any]:
    """Empty state — zero endpoints, every status 'unknown', ms null everywhere.
    Nothing here is a guess; it is what we know when the cache is unusable."""
    return {
        "ok": True,
        "generated_at": None,
        "cache": {"path": path, "age_s": None, "ttl_s": ttl, "fresh": False},
        "self": {"host": _hostname(), "tailscale_ip": None},
        "totals": {"endpoints": 0, "up": 0, "down": 0, "unknown": 0,
                   "degraded": True},
        "tunnel": {
            "transport": "cloudflared", "url": None, "copy": None,
            "pid": None, "origin": None, "log": None, "status": "unknown",
            "substates": {"edge": "unknown", "origin": "unknown"},
            "http_status": None, "ms": None, "probe": None,
            "checked_at": None, "stale": False,
            "err": None, "last_log_error": None,
        },
        "endpoints": [],
        "peers": [],
        "degraded": degraded,
    }


def _with_cache_block(payload: Dict[str, Any], path: str, ttl: float,
                      generated: Optional[str], fresh: bool) -> Dict[str, Any]:
    age = None
    gen_epoch = _epoch(generated)
    if gen_epoch is not None:
        age = round(max(0.0, time.time() - gen_epoch), 1)
    payload["cache"] = {"path": path, "age_s": age, "ttl_s": ttl,
                        "fresh": bool(fresh and age is not None and age <= ttl)}
    return payload


def _radar(mode: str, deep: bool) -> Dict[str, Any]:
    path = _cache_path()
    ttl = _ttl_s()
    writable = _cache_dir_writable(path)

    with _LOCK:
        mem = dict(_NET) if _NET else None
    cached = mem or _read_cache_file(path)

    age = None
    if isinstance(cached, dict):
        gen = _epoch(cached.get("generated_at"))
        if gen is not None:
            age = round(max(0.0, time.time() - gen), 1)
    cache_fresh = bool(cached) and age is not None and age <= ttl

    if mode == "cache":
        # never probe: serve what is stored, or the empty state if there is
        # nothing (or the cache dir is unwritable — degraded to empty).
        if not cached:
            reasons = ["cache_unavailable"]
            if not writable:
                reasons.insert(0, "cache_unwritable")
            return _empty_payload(path, ttl, reasons)
        payload = json.loads(json.dumps(cached))      # deep copy, no aliasing
        payload["ok"] = True
        return _with_cache_block(payload, path, ttl, payload.get("generated_at"),
                                 fresh=False)

    # `deep` is an explicit request for extra measurements a cached payload
    # does not contain, so a fresh cache never satisfies it (spec §3.1 auto
    # mode stays literal for every non-deep read).
    if mode == "auto" and cached and cache_fresh and not deep:
        payload = json.loads(json.dumps(cached))
        payload["ok"] = True
        return _with_cache_block(payload, path, ttl, payload.get("generated_at"),
                                 fresh=True)

    payload = _probe_cycle(bool(deep), cached if isinstance(cached, dict) else None,
                           path, ttl)

    if _write_cache_file(path, payload):
        _memory_put(payload)
        payload = _with_cache_block(payload, path, ttl, payload["generated_at"], True)
    else:
        # cache dir unwritable: degrade the cache layer to empty rather than
        # serve state we cannot persist — live probe data is still returned.
        _memory_clear()
        reasons = payload.get("degraded") or []
        if "cache_unwritable" not in reasons:
            reasons = list(reasons) + ["cache_unwritable"]
        payload["degraded"] = reasons
        payload["totals"]["degraded"] = True
        payload = _with_cache_block(payload, path, ttl, payload.get("generated_at"),
                                    fresh=False)
    return payload


def _cmd_net(arg: str = "") -> str:
    """/day-net [refresh|cache] [deep] -> JSON (same contract as day-evidence)."""
    try:
        tokens = [t.lower() for t in str(arg or "").split() if t]
        deep = "deep" in tokens
        if "cache" in tokens:
            mode = "cache"
        elif "refresh" in tokens:
            mode = "refresh"
        else:
            mode = "auto"
        return json.dumps(_radar(mode, deep))
    except Exception as exc:
        # honest failure: no payload, no invented rows
        return json.dumps({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})


def register(ctx: Any) -> None:
    """Patch entrypoint — commands only, never a hook (brief rule 4)."""
    global _CTX
    try:
        _CTX = ctx
        _load_memory()
        ctx.register_command(
            "day-net",
            _cmd_net,
            description="Network radar: endpoints, transports, real latency, tunnel URL.",
            args_hint="[refresh|cache] [deep]",
        )
    except Exception:
        pass
