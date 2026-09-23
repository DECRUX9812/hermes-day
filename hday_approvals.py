"""Hermes Day — HITL pause/resume + capability safety (typed policy, guardrails).

Import-safe standalone module: no PluginContext, no hooks, no I/O at import
time. Like ``hday_gate.py`` it deliberately does NOT import ``__init__`` — the
integrator wires the ledger sink, the durable store and the escalation probe in
through ``configure()`` (see ``__init__._approvals``).

Four mechanisms, each taken from the harvest (``runs/harvest/*.json``):

1. **Human-in-the-loop as a pair of ordinary functions.**
   ``pause(action, payload, ...) -> token`` persists a *pending decision* to the
   ONE existing ledger (the session's ``gate`` rows) and parks the interrupt in
   the durable queue; ``resume(token, decision)`` continues deterministically
   from the recorded payload. Spec: LangGraph's ``interrupt`` /
   ``Command(resume=...)`` ("the pause point lives in the run, not in a side
   channel"), LlamaIndex's ``input_required``/``human_response`` pair ("a second
   response for the same prompt id is rejected as already-answered (typed, no
   duplicate continuation)"), AG-UI's ``Interrupt`` ("a denial round-trips as
   ``payload{approved:false}`` and never as ``status:'cancelled'``") and OWASP's
   human-approval-gates ("an approval for action hash A cannot authorize action
   hash B"; "unreachable rows never become approvals"; "a stale approval row
   cannot be replayed after TTL").

2. **Capability-based permissions.** Every tool declares the capabilities it
   needs (``fs.write``, ``net.egress``, ``proc.spawn``, …); args add derived
   capabilities (``git push`` -> ``net.egress``, ``sudo`` -> ``proc.privilege``,
   a credential path -> ``secret.read``). The gate consults
   ``verdict_for() -> PolicyVerdict`` — a typed allow/escalate/deny, never a
   boolean (``bool(verdict)`` raises, by design). Spec: CaMeL's capability table
   ("a capability grant for one call does not persist to the next (no ambient
   authority)") and Progent's per-task allowlist ("a policy that permits nothing
   denies everything (fail-closed control)"; "a call inside it is allowed
   without invoking the judge").

3. **Task guardrails as a ``(passed, value)`` contract.** A rail returns
   ``(True, payload)`` — where the payload may be *rewritten* — or
   ``(False, reason)``. Spec: NeMo's ``rail_action in {allow, deny, alter}``
   ("an alter rail records before/after hashes plus the reason as a ledger row
   (never a silent rewrite)") and ADK callbacks ("one returning a truthy rewrite
   replaces the args").

4. **A durable approval queue.** ``ApprovalQueue`` reads and writes its store on
   every mutation, so a pending approval survives a process restart; the plugin
   wires the store to the host's persisted state, standalone callers can pass a
   JSON path. A store that cannot be written is reported (``durable: false`` on
   the row) rather than silently claimed.

Failure policy: two directions, deliberately.

- A **rail that raises fails open, loudly** — it contributes nothing, the
  payload continues unaltered and the step is marked ``unjudged`` (NeMo's rule,
  and the repo's own failure policy).
- An **approval that cannot reach a human never becomes an approval** — the row
  is recorded ``unreachable`` and the decision is fail-closed, except for
  read-only actions under a policy that says ``unreachable="record"``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    # vocabulary / capability table
    "CAPABILITIES", "TOOL_CAPABILITIES", "declare_capabilities",
    "capabilities_for", "action_hash", "payload_hash",
    # typed policy
    "Policy", "PolicyVerdict", "verdict_for",
    # guardrails
    "GuardrailOutcome", "run_guardrails", "scrub_secrets", "clamp_timeout",
    "capability_rail",
    # HITL
    "Interrupt", "ResumeOutcome", "AuthOutcome",
    "pause", "resume", "authorize", "interrupt", "pending",
    # durable queue + stores
    "ApprovalQueue", "MemoryStore", "FileStore", "store_for",
    # gate entry point / config
    "decide", "configure", "reset", "ApprovalError",
    "DEFAULT_TTL", "QUEUE_CAP",
]

# ---------------------------------------------------------------------------
# Capability vocabulary + declared tool table (mechanism 2)
# ---------------------------------------------------------------------------

FS_READ = "fs.read"
FS_WRITE = "fs.write"
NET_READ = "net.read"
NET_EGRESS = "net.egress"
PROC_SPAWN = "proc.spawn"
PROC_PRIVILEGE = "proc.privilege"
PROC_SIGNAL = "proc.signal"
SECRET_READ = "secret.read"

CAPABILITIES = frozenset({
    FS_READ, FS_WRITE, NET_READ, NET_EGRESS,
    PROC_SPAWN, PROC_PRIVILEGE, PROC_SIGNAL, SECRET_READ,
})

#: Read-only capabilities — the only set a policy may let through when no human
#: gate is reachable (``unreachable="record"``).
_READ_ONLY = frozenset({FS_READ, NET_READ})

#: What each tool declares up front. Args add derived capabilities on top
#: (``capabilities_for``) — a declaration is a floor, never a ceiling.
_BASE_TOOL_CAPABILITIES: Dict[str, Tuple[str, ...]] = {
    "read_file": (FS_READ,),
    "search_files": (FS_READ,),
    "write_file": (FS_WRITE,),
    "patch": (FS_READ, FS_WRITE),
    "terminal": (PROC_SPAWN,),
    "execute_code": (PROC_SPAWN,),
    "process_manage": (PROC_SIGNAL,),
    "delegate_task": (PROC_SPAWN,),
    "browser_exec": (NET_EGRESS,),
    "web_search": (NET_READ,),
    "web_extract": (NET_READ,),
}
TOOL_CAPABILITIES: Dict[str, Tuple[str, ...]] = dict(_BASE_TOOL_CAPABILITIES)

#: Args that name a file — mirrors the gate's path keys.
_PATH_KEYS = ("path", "file_path", "target_file", "filePath", "notebook_path",
              "directory", "dir", "dest", "destination", "target",
              "target_path", "output", "output_path")

#: Mirrored from ``__init__._GATE_CMD_PATTERNS`` (NOT imported: separate file,
#: same parallel-wave discipline as ``hday_gate``; drift is caught by tests).
_DERIVED_CMD_PATTERNS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (NET_EGRESS, (
        r"\bgit\s+push\b", r"\bgh\s+(?:pr|release|gist)\b",
        r"\bcurl\b[^|;]*(?:-X\s*(?:POST|PUT|PATCH)|--data\b|-d\s|--upload-file|-T\s|--form|-F\s|@)",
        r"\bwget\b[^|;]*--post", r"\bscp\s+\S", r"\brsync\b[^|;]*\S+:",
        r"\b(?:npm|pip|pip3|uv)\s+(?:publish|upload)\b",
        r"\baws\s+s3\s+(?:cp|sync|rm)\b", r"\bs3cmd\b",
        r"\bgcloud\s+(?:storage|compute|secrets)\b",
        r"\b(?:vercel|netlify|flyctl|heroku|wrangler)\b",
        r"\bssh\s+\S", r"\bmosh\s+\S", r"\bsocat\b",
    )),
    (NET_READ, (r"\bcurl\b", r"\bwget\b", r"\bgit\s+fetch\b", r"\bgit\s+pull\b")),
    (PROC_PRIVILEGE, (
        r"\bsudo\b", r"\bsu\s+-", r"\bdoas\b",
        r"\bchmod\s+(?:-[a-zA-Z]+=\S+\s+)*(?:-[a-zA-Z]+\s+)*777\b",
        r"\bchown\s+-R\b", r"\bsetcap\b", r"\bvisudo\b",
    )),
    (PROC_SIGNAL, (
        r"\bkill\s+-9\b", r"\bpkill\b",
        r"\bsystemctl\s+(?:stop|disable|mask|restart|kill)\b",
        r"\b(?:shutdown|reboot|halt|poweroff)\b",
        r"\biptables\b", r"\bnft\b", r"\bufw\b", r"\b(?:mount|umount)\b",
        r"\bcrontab\b", r"\blaunchctl\b",
    )),
    (FS_WRITE, (
        r"\brm\s+(?:-[a-zA-Z]+=\S+\s+)*-[a-zA-Z]*[rf][a-zA-Z]*(?:\s|$)",
        r"\bmkfs(?:\.\w+)?\b", r"\bdd\s+[^|;]*\bof=", r"\bshred\b",
        r"\bfind\b[^|;]*\s-delete\b", r"\btruncate\s+-s\s*0\b",
        r"\bgit\s+reset\s+--hard\b", r"\bgit\s+clean\s+-[a-z]*[fdx]",
        r"\b(?:mkdir|touch|tee)\b", r"\bsed\s+-i\b", r"\bmv\s+\S", r"\bcp\s+\S",
        r"[^|;<>]\s>{1,2}\s*\S",
    )),
    (SECRET_READ, (
        r"\.ssh", r"\.aws", r"\.gnupg", r"\.kube", r"\bhimalaya\b", r"keychains",
        r"(?:^|[\s/'\"])(?:\.env|id_rsa|id_dsa|id_ecdsa|id_ed25519|authorized_keys|"
        r"credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc|kubeconfig|shadow|passwd)\b",
    )),
)

#: Capabilities a file-ish arg implies when the path itself is sensitive.
_SECRET_PATH_RE = re.compile(
    r"\.ssh|\.aws|\.gnupg|\.kube|\.docker/config|himalaya|keychains", re.I)
_CREDENTIAL_NAMES = frozenset({
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "authorized_keys",
    "credentials", ".env", ".netrc", ".pgpass", ".npmrc", ".pypirc",
    "kubeconfig", "shadow", "passwd",
})


def declare_capabilities(tool: str, capabilities: Iterable[str]) -> None:
    """Register (or extend) a tool's declared capability set.

    A declaration is a *floor*: args can still add derived capabilities. Unknown
    names are kept — ``verdict_for`` denies them fail-closed rather than letting
    a typo widen the grant.
    """
    tool = str(tool or "").strip()
    if not tool:
        return
    declared = set(TOOL_CAPABILITIES.get(tool) or ())
    declared.update(str(c) for c in (capabilities or ()))
    TOOL_CAPABILITIES[tool] = tuple(sorted(declared))


def _command_of(args: Dict[str, Any]) -> str:
    for key in ("command", "cmd", "code"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _arg_paths(args: Dict[str, Any]) -> List[str]:
    out = []
    for key in _PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            out.append(value)
    return out


def _path_is_secret(path: str) -> bool:
    text = str(path or "").replace("\\", "/")
    if _SECRET_PATH_RE.search(text):
        return True
    base = os.path.basename(text.rstrip("/"))
    return base in _CREDENTIAL_NAMES or base.startswith(".env")


def capabilities_for(tool_name: str, args: Optional[Dict[str, Any]] = None) -> frozenset:
    """Declared capabilities for ``tool_name`` plus the ones its args imply."""
    args = args if isinstance(args, dict) else {}
    caps = set(TOOL_CAPABILITIES.get(str(tool_name or "")) or ())
    cmd = _command_of(args)
    if cmd:
        for cap, patterns in _DERIVED_CMD_PATTERNS:
            if any(re.search(p, cmd, re.I) for p in patterns):
                caps.add(cap)
    paths = _arg_paths(args)
    if paths:
        if str(tool_name or "") in ("write_file", "patch", "edit_file", "multi_edit"):
            caps.add(FS_WRITE)
        if any(_path_is_secret(p) for p in paths):
            caps.add(SECRET_READ)
        if _SECRET_PATH_RE.search(" ".join(paths)):
            caps.add(SECRET_READ)
    # a file-editing tool always carries the write capability
    if str(tool_name or "") in ("write_file", "patch", "edit_file", "multi_edit"):
        caps.add(FS_WRITE)
    return frozenset(caps)


def payload_hash(payload: Any) -> str:
    """Stable hash of a payload (used for alter/deny rails: never a silent edit)."""
    try:
        blob = json.dumps(payload, sort_keys=True, default=str)
    except Exception:
        blob = repr(payload)
    return hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[:16]


def action_hash(action: str, payload: Any) -> str:
    """Bind an approval to one exact action — hash of (action, payload)."""
    try:
        blob = json.dumps(payload, sort_keys=True, default=str)
    except Exception:
        blob = repr(payload)
    return hashlib.sha1(f"{action}|{blob}".encode("utf-8", "replace")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Typed policy verdict (mechanism 2) — allow | escalate | deny, never a bool
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PolicyVerdict:
    """The gate's typed answer for a capability request.

    Deliberately NOT a boolean: ``bool(verdict)`` raises, so a caller must branch
    on ``.decision`` (``allow`` | ``escalate`` | ``deny``) and the three-way
    outcome cannot be silently collapsed into true/false.
    """

    decision: str
    rule: str
    reason: str
    capabilities: Tuple[str, ...] = ()
    missing: Tuple[str, ...] = ()
    escalate: Tuple[str, ...] = ()
    unknown: Tuple[str, ...] = ()
    policy: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - the raise IS the contract
        raise TypeError(
            "PolicyVerdict is typed, not a boolean — branch on .decision "
            f"(allow|escalate|deny), got {self.decision!r}")

    @property
    def allow(self) -> bool:
        """Explicit accessor for callers that really do want a bool."""
        return self.decision == "allow"

    def as_row(self) -> Dict[str, Any]:
        return {"decision": self.decision, "rule": self.rule,
                "capabilities": list(self.capabilities),
                "missing": list(self.missing), "escalate": list(self.escalate),
                "unknown": list(self.unknown), "policy": self.policy}


@dataclass(frozen=True)
class Policy:
    """A per-task allowlist over the capability vocabulary.

    ``grants``   — capabilities allowed outright.
    ``escalate`` — capabilities that may proceed only with a human approval.
    ``deny``     — capabilities refused outright (wins over ``grants``).
    ``unreachable`` — what to do when no human gate is reachable at all:
        ``"deny"`` (default, fail-closed) or ``"record"`` (allow-plus-record,
        and only for read-only actions — OWASP's rule for the unreachable case).
    """

    name: str = "default"
    grants: Tuple[str, ...] = ()
    escalate: Tuple[str, ...] = ()
    deny: Tuple[str, ...] = ()
    unreachable: str = "deny"

    def __post_init__(self) -> None:
        object.__setattr__(self, "grants", tuple(sorted(str(c) for c in self.grants or ())))
        object.__setattr__(self, "escalate", tuple(sorted(str(c) for c in self.escalate or ())))
        object.__setattr__(self, "deny", tuple(sorted(str(c) for c in self.deny or ())))
        if self.unreachable not in ("deny", "record"):
            object.__setattr__(self, "unreachable", "deny")

    @classmethod
    def default(cls) -> "Policy":
        """Permissive default: every known capability granted, nothing escalated.

        Used when a caller passes no policy, so adding the capability layer is
        additive — it never blocks work the existing gate would have allowed.
        """
        return cls(name="default", grants=tuple(sorted(CAPABILITIES)))

    @classmethod
    def read_only(cls) -> "Policy":
        return cls(name="read-only", grants=(FS_READ, NET_READ))


def verdict_for(capabilities: Iterable[str], policy: Optional[Policy] = None) -> PolicyVerdict:
    """Type a capability request against a policy. Never a boolean."""
    policy = policy if isinstance(policy, Policy) else Policy.default()
    requested = tuple(sorted({str(c) for c in (capabilities or ())}))
    unknown = tuple(c for c in requested if c not in CAPABILITIES)
    if unknown:
        return PolicyVerdict(
            "deny", "capability.unknown",
            f"unknown capability {', '.join(unknown)} — fail-closed",
            capabilities=requested, unknown=unknown, policy=policy.name)
    denied = tuple(c for c in requested if c in policy.deny)
    if denied:
        return PolicyVerdict(
            "deny", "capability.denied",
            f"policy {policy.name!r} denies {', '.join(denied)}",
            capabilities=requested, missing=denied, policy=policy.name)
    missing = tuple(c for c in requested if c not in policy.grants)
    if not missing:
        return PolicyVerdict(
            "allow", "capability.granted",
            f"policy {policy.name!r} grants {', '.join(requested) or 'nothing needed'}",
            capabilities=requested, policy=policy.name)
    escalatable = tuple(c for c in missing if c in policy.escalate)
    if escalatable:
        return PolicyVerdict(
            "escalate", "capability.escalate",
            f"policy {policy.name!r} requires a human for {', '.join(escalatable)}",
            capabilities=requested, missing=missing, escalate=escalatable,
            policy=policy.name)
    return PolicyVerdict(
        "deny", "capability.denied",
        f"policy {policy.name!r} does not grant {', '.join(missing)}",
        capabilities=requested, missing=missing, policy=policy.name)


# ---------------------------------------------------------------------------
# Guardrails — (passed, value) contract that can rewrite the payload (3)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GuardrailOutcome:
    """``(passed, value)`` with an audit trail.

    ``value`` is always the payload as it stood after the last successful rail —
    on a block, the reason lives in ``reason``, never in ``value``.
    """

    passed: bool
    value: Any
    rewrites: Tuple[str, ...] = ()
    blocked_by: str = ""
    reason: str = ""
    trace: Tuple[Dict[str, Any], ...] = ()

    def as_row(self) -> Dict[str, Any]:
        return {"passed": self.passed, "rewrites": list(self.rewrites),
                "blocked_by": self.blocked_by, "reason": self.reason}


_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(api[_-]?key|apikey|token|secret|password|passwd|authorization|bearer)"
    r"(\s*[:=]\s*|\s+)(['\"]?)([^\s'\"]{4,})")
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}")


def _scrub_text(text: str) -> str:
    out = _SECRET_VALUE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}***", text)
    return _SK_RE.sub("sk-***", out)


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, dict):
        return {k: _scrub_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_value(v) for v in value]
    return value


def scrub_secrets(action: str, payload: Any) -> Tuple[bool, Any]:
    """Alter rail: redact credential-looking values instead of blocking the turn.

    Spec (NeMo's ``alter``): the rewrite is recorded with before/after hashes and
    a reason, so a human can always see that the payload was changed.
    """
    return True, _scrub_value(payload)


def clamp_timeout(limit: float = 30.0) -> Callable[[str, Any], Tuple[bool, Any]]:
    """Alter rail factory: cap a payload's numeric ``timeout`` at ``limit``."""
    def clamp_timeout(action: str, payload: Any) -> Tuple[bool, Any]:  # noqa: F811
        if isinstance(payload, dict):
            value = payload.get("timeout")
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > limit:
                new = dict(payload)
                new["timeout"] = limit
                return True, new
        return True, payload
    clamp_timeout.__name__ = "clamp_timeout"
    return clamp_timeout


def capability_rail(policy: Optional[Policy] = None) -> Callable[[str, Any], Tuple[bool, Any]]:
    """Deny rail factory: block when the args need a capability the policy denies."""
    def capability_rail(action: str, payload: Any) -> Tuple[bool, Any]:  # noqa: F811
        args = payload if isinstance(payload, dict) else {}
        verdict = verdict_for(capabilities_for(action, args), policy)
        if verdict.decision == "deny":
            return False, verdict.reason
        return True, payload
    capability_rail.__name__ = "capability_rail"
    return capability_rail


def _rail_name(rail: Any) -> str:
    return str(getattr(rail, "__name__", "") or type(rail).__name__)


def run_guardrails(action: str, payload: Any, guardrails: Sequence[Any],
                   ledger: Optional[Callable[[Dict[str, Any]], None]] = None,
                   now: Any = None) -> GuardrailOutcome:
    """Run rails in order over ``payload``; the first block wins.

    Contract: ``rail(action, payload) -> (True, payload)`` continues with the
    (possibly rewritten) payload; ``(False, reason)`` blocks. A rail that raises,
    or that returns something other than a 2-tuple, contributes nothing and is
    recorded ``unjudged`` (fail-open, loudly — NeMo's rule for a broken rail).
    """
    value = payload
    rewrites: List[str] = []
    trace: List[Dict[str, Any]] = []
    steps: List[Dict[str, Any]] = []

    def _emit(step: Dict[str, Any]) -> None:
        trace.append(step)
        if step.get("action") != "allow":       # allow steps are not noise
            steps.append(step)

    for rail in guardrails or ():
        name = _rail_name(rail)
        before_hash = payload_hash(value)
        try:
            result = rail(action, value)
            if not (isinstance(result, tuple) and len(result) == 2):
                raise TypeError(f"rail returned {type(result).__name__}, expected (passed, value)")
            passed, new_value = bool(result[0]), result[1]
        except Exception as exc:                # fail open, loudly
            _emit({"rail": name, "action": "unjudged", "reason":
                   f"rail raised {type(exc).__name__}: {exc}", "before_hash": before_hash})
            continue
        if not passed:
            _emit({"rail": name, "action": "deny", "reason": str(new_value)[:200],
                   "before_hash": before_hash})
            outcome = GuardrailOutcome(False, value, tuple(rewrites), name,
                                       str(new_value), tuple(trace))
            _write_ledger(ledger, action, steps, outcome, now)
            return outcome
        if new_value != value:
            rewrites.append(name)
            value = new_value
            _emit({"rail": name, "action": "alter",
                   "reason": "rail rewrote the payload",
                   "before_hash": before_hash, "after_hash": payload_hash(value)})
        else:
            _emit({"rail": name, "action": "allow", "before_hash": before_hash})

    outcome = GuardrailOutcome(True, value, tuple(rewrites), "", "", tuple(trace))
    _write_ledger(ledger, action, steps, outcome, now)
    return outcome


def _write_ledger(ledger: Optional[Callable[[Dict[str, Any]], None]], action: str,
                  steps: List[Dict[str, Any]], outcome: GuardrailOutcome,
                  now: Any) -> None:
    """One row per non-allow rail step: never a silent rewrite."""
    if ledger is None or not steps:
        return
    for step in steps:
        row = {"kind": "judged", "lane": "local", "ms": 0.0, "cost": 0.0,
               "ts": _now(now), "tool": action, "call": "",
               "allow": False, "directive": "none",
               "rule": "Guardrail", "rail": step["rail"],
               "reason": _brief(step.get("reason") or "", 240),
               "before_hash": step.get("before_hash")}
        if step.get("after_hash"):
            row["after_hash"] = step["after_hash"]
        _sink(ledger, row)


# ---------------------------------------------------------------------------
# Durable stores (mechanism 4) — the queue is only as durable as its store
# ---------------------------------------------------------------------------

QUEUE_CAP = 64
_QUEUE_KEY = "hday_approvals"


class MemoryStore:
    """Process-local store. Explicitly NOT durable — rows say ``durable: false``."""

    durable = False

    def __init__(self) -> None:
        self.data: Dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value


class FileStore:
    """JSON file store — survives a process restart (and a new interpreter)."""

    durable = True

    def __init__(self, path: Any) -> None:
        self.path = os.path.abspath(os.path.expanduser(str(path)))

    def _read(self) -> Dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._read().get(key, default)

    def set(self, key: str, value: Any) -> None:
        data = self._read()
        data[key] = value
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, default=str)
        os.replace(tmp, self.path)


def store_for(store: Any) -> Any:
    """Coerce a store argument: None -> the configured default, path -> FileStore."""
    if store is None:
        return _STORE
    if isinstance(store, (str, os.PathLike)):
        return FileStore(store)
    if hasattr(store, "get") and hasattr(store, "set"):
        return store
    raise ApprovalError(f"not a store: {store!r}")


# ---------------------------------------------------------------------------
# Interrupt / outcomes (mechanism 1)
# ---------------------------------------------------------------------------

class ApprovalError(Exception):
    """Typed refusal: an unknown token, an unknown decision, a broken store."""


DEFAULT_TTL = 900.0
_RESOLVED = ("approved", "denied", "expired")
_DECISIONS = {"approve": "approve", "approved": "approve",
              "deny": "deny", "denied": "deny",
              "expire": "expire", "expired": "expire"}


@dataclass(frozen=True)
class Interrupt:
    """A parked decision (AG-UI's Interrupt: id, reason, message, payload, ttl)."""

    id: str
    action: str
    action_hash: str
    payload: Any = None
    prompt: str = ""
    reason: str = ""
    blast_radius: str = "local"
    capabilities: Tuple[str, ...] = ()
    status: str = "pending"          # pending | approved | denied | expired | unreachable
    created_at: float = 0.0
    expires_at: float = 0.0
    ttl_s: float = DEFAULT_TTL
    responder: str = ""
    resolved_at: float = 0.0
    decision: str = ""
    consumed_at: float = 0.0
    durable: bool = False
    unreachable_policy: str = "deny"
    session: str = ""

    def as_row(self) -> Dict[str, Any]:
        row = dict(self.__dict__)
        row["capabilities"] = list(self.capabilities)
        return row

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "Interrupt":
        fields = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        clean = {k: v for k, v in (row or {}).items() if k in fields}
        clean["capabilities"] = tuple(clean.get("capabilities") or ())
        return cls(**clean)  # type: ignore[arg-type]

    def expired(self, at: Optional[float] = None) -> bool:
        return self.expires_at > 0 and (at if at is not None else time.time()) >= self.expires_at


@dataclass(frozen=True)
class ResumeOutcome:
    """The deterministic continuation of a paused decision."""

    token: str
    status: str
    allow: bool
    action: str = ""
    payload: Any = None
    action_hash: str = ""
    fresh: bool = True
    reason: str = ""
    rule: str = ""
    responder: str = ""

    def __bool__(self) -> bool:
        return bool(self.allow)


@dataclass(frozen=True)
class AuthOutcome:
    """Does this resolved approval authorize THIS exact action, right now?"""

    allow: bool
    rule: str
    reason: str
    token: str = ""
    action_hash: str = ""
    status: str = ""

    def __bool__(self) -> bool:
        return bool(self.allow)


class ApprovalQueue:
    """The durable pending/resolved queue. Every mutation hits the store."""

    def __init__(self, store: Any = None, cap: int = QUEUE_CAP) -> None:
        self.store = store_for(store)
        self.cap = max(1, int(cap))
        self._cache: List[Dict[str, Any]] = []   # last rows seen/written

    # -- storage ----------------------------------------------------------
    @property
    def durable(self) -> bool:
        return bool(getattr(self.store, "durable", False))

    def _rows(self) -> List[Dict[str, Any]]:
        """Rows from the store; the in-process cache when the store is silent.

        A store that cannot be read (or has never been written) must not lose a
        decision that was already accepted — the cache is the honest fallback and
        the row carries ``durable: false``.
        """
        try:
            rows = self.store.get(_QUEUE_KEY, []) or []
            rows = [r for r in rows if isinstance(r, dict) and r.get("id")]
            if rows:
                self._cache = list(rows)
                return list(rows)
        except Exception:
            pass
        return list(self._cache)

    def _write(self, rows: List[Dict[str, Any]]) -> bool:
        rows = rows[-self.cap:]
        self._cache = list(rows)
        try:
            self.store.set(_QUEUE_KEY, rows)
            return True
        except Exception:
            return False

    # -- public surface ---------------------------------------------------
    def put(self, it: Interrupt) -> bool:
        """Append an interrupt. Returns whether the store actually took it."""
        rows = [r for r in self._rows() if r.get("id") != it.id]
        rows.append(it.as_row())
        ok = self._write(rows)
        return ok and self.durable

    def get(self, token: str) -> Optional[Interrupt]:
        for row in self._rows():
            if row.get("id") == token:
                return Interrupt.from_row(row)
        return None

    def pending(self, at: Optional[float] = None) -> Tuple[Interrupt, ...]:
        out = []
        for row in self._rows():
            it = Interrupt.from_row(row)
            if it.status == "pending" and not it.expired(at):
                out.append(it)
        return tuple(out)

    def recent(self, limit: int = 12) -> List[Dict[str, Any]]:
        resolved = [r for r in self._rows() if r.get("status") in _RESOLVED
                    or r.get("status") == "unreachable"]
        return resolved[-max(1, int(limit)):]

    def replace(self, it: Interrupt) -> bool:
        rows = [r for r in self._rows() if r.get("id") != it.id]
        rows.append(it.as_row())
        return self._write(rows)


# ---------------------------------------------------------------------------
# Module state + config seams
# ---------------------------------------------------------------------------

_STORE: Any = MemoryStore()
_LEDGER: Optional[Callable[[Dict[str, Any]], None]] = None
_ESCALATION: Optional[Callable[[], bool]] = None
_NOW: Optional[Callable[[], float]] = None
_GATE_MOD: Any = None
_GATE_MOD_TRIED = False


def configure(ledger: Optional[Callable[[Dict[str, Any]], None]] = None,
              store: Any = None,
              escalation_available: Optional[Callable[[], bool]] = None,
              now: Optional[Callable[[], float]] = None) -> None:
    """Wire the module into its host (called by ``__init__._approvals``).

    ``ledger`` is the ONE existing ledger sink: ``sink(row: dict) -> None``.
    ``store`` is the durable store (host state, a JSON path, or a dict-like).
    """
    global _STORE, _LEDGER, _ESCALATION, _NOW
    if store is not None:
        _STORE = store_for(store)
    if ledger is not None:
        _LEDGER = ledger
    if escalation_available is not None:
        _ESCALATION = escalation_available
    if now is not None:
        _NOW = now


def reset() -> None:
    """Drop every in-process cache (used by tests and by a fresh host wiring)."""
    global _STORE, _LEDGER, _ESCALATION, _NOW, _GATE_MOD, _GATE_MOD_TRIED
    _STORE = MemoryStore()
    _LEDGER = None
    _ESCALATION = None
    _NOW = None
    _GATE_MOD = None
    _GATE_MOD_TRIED = False
    TOOL_CAPABILITIES.clear()
    TOOL_CAPABILITIES.update(_BASE_TOOL_CAPABILITIES)


def _now(now: Any = None) -> float:
    source = now if now is not None else _NOW
    if callable(source):
        try:
            return float(source())
        except Exception:
            return time.time()
    if isinstance(source, (int, float)):
        return float(source)
    return time.time()


def _brief(text: Any, n: int = 300) -> str:
    return " ".join(str(text or "").split())[:n]


def _preview(payload: Any) -> str:
    if isinstance(payload, dict):
        cmd = _command_of(payload)
        if cmd:
            return _brief(cmd, 300)
        for key in _PATH_KEYS:
            if isinstance(payload.get(key), str) and payload[key].strip():
                return _brief(f"{payload.get('tool') or ''} {payload[key]}", 300)
    try:
        return _brief(json.dumps(payload, default=str), 300)
    except Exception:
        return _brief(repr(payload), 300)


def _sink(ledger: Optional[Callable[[Dict[str, Any]], None]], row: Dict[str, Any]) -> None:
    """Hand a row to the ledger sink. Never raises, never wedges the caller."""
    target = ledger if ledger is not None else _LEDGER
    if target is None:
        return
    try:
        target(row)
    except Exception:
        pass


def _escalation_available(escalation_available: Any = None) -> bool:
    probe = escalation_available if escalation_available is not None else _ESCALATION
    if probe is None:
        return True
    try:
        return bool(probe())
    except Exception:
        return True


def _queue(store: Any = None) -> ApprovalQueue:
    return ApprovalQueue(store=store)


def _new_token() -> str:
    return "ap-" + uuid.uuid4().hex[:12]


def _interrupt_row(it: Interrupt, allow: bool, directive: str, rule: str,
                   reason: str, **extra: Any) -> Dict[str, Any]:
    row = {"kind": "interrupt", "lane": "local", "ms": 0.0, "cost": 0.0,
           "ts": it.created_at or time.time(), "tool": it.action,
           "call": _preview(it.payload), "allow": bool(allow),
           "directive": directive, "rule": rule, "reason": _brief(reason, 240),
           "interrupt": it.id, "status": it.status,
           "action_hash": it.action_hash, "blast_radius": it.blast_radius,
           "capabilities": list(it.capabilities), "durable": bool(it.durable),
           "session": it.session}
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# Mechanism 1 — pause() / resume() / authorize()
# ---------------------------------------------------------------------------

def pause(action: str, payload: Any = None, *, prompt: str = "", reason: str = "",
          blast_radius: str = "local", capabilities: Optional[Iterable[str]] = None,
          policy: Optional[Policy] = None, ttl_s: float = DEFAULT_TTL,
          session: str = "", store: Any = None, ledger: Optional[Callable] = None,
          now: Any = None, escalation_available: Any = None) -> str:
    """Park a decision that needs a human. Returns the interrupt token.

    The pending decision is written to the ONE existing ledger (one ``interrupt``
    row, ``status: pending``) and to the durable queue, so a process restart does
    not lose it. When no human gate is reachable the row is recorded
    ``unreachable`` instead — never a silent approval.
    """
    action = str(action or "")
    policy = policy if isinstance(policy, Policy) else Policy.default()
    caps = tuple(sorted({str(c) for c in (capabilities if capabilities is not None
                                          else capabilities_for(action, payload if isinstance(payload, dict) else {}))}))
    at = _now(now)
    reachable = _escalation_available(escalation_available)
    it = Interrupt(id=_new_token(), action=action,
                   action_hash=action_hash(action, payload), payload=payload,
                   prompt=_brief(prompt, 300), reason=_brief(reason, 300),
                   blast_radius=str(blast_radius or "local"), capabilities=caps,
                   status="pending" if reachable else "unreachable",
                   created_at=at, expires_at=at + float(ttl_s), ttl_s=float(ttl_s),
                   unreachable_policy=policy.unreachable, session=str(session or ""))
    queue = _queue(store)
    durable = queue.put(it)
    it = replace(it, durable=durable)
    queue.replace(it)
    if reachable:
        note = (f"paused for human approval: {it.prompt or action} "
                f"(expires in {int(it.ttl_s)}s)")
        rule = "Interrupt"
    else:
        note = ("no human approval gate is reachable — recorded unreachable, "
                "never a silent approval")
        rule = "Interrupt Unreachable"
    _sink(ledger, _interrupt_row(it, False, "approve" if reachable else "block",
                                 rule, note, prompt=it.prompt, detail=it.reason))
    return it.id


def resume(token: str, decision: str, *, responder: str = "", store: Any = None,
           ledger: Optional[Callable] = None, now: Any = None) -> ResumeOutcome:
    """Continue a parked decision deterministically.

    ``decision`` is ``approve`` | ``deny`` | ``expire``. Resuming an already
    resolved token is refused as ``already_answered`` (no duplicate
    continuation, no second ledger row); an expired token is ``expired`` and the
    next matching call re-escalates under a new token.
    """
    token = str(token or "")
    want = _DECISIONS.get(str(decision or "").strip().lower())
    if want is None:
        raise ApprovalError(
            f"unknown decision {decision!r} — expected one of "
            f"{sorted(set(_DECISIONS))}")
    queue = _queue(store)
    it = queue.get(token)
    if it is None:
        raise ApprovalError(f"no such approval token: {token!r}")
    at = _now(now)

    if it.status in _RESOLVED:
        return ResumeOutcome(token=it.id, status="already_answered",
                             allow=it.status == "approved", action=it.action,
                             payload=it.payload, action_hash=it.action_hash,
                             fresh=False, responder=it.responder,
                             rule="approval.already_answered",
                             reason=f"already answered ({it.status}) by "
                                    f"{it.responder or 'unknown'} — no duplicate "
                                    "continuation")

    if it.status == "unreachable":
        record = (it.unreachable_policy == "record"
                  and set(it.capabilities) <= _READ_ONLY)
        allow = bool(record) and want == "approve"
        rule = "approval.unreachable_recorded" if allow else "approval.unreachable"
        reason = ("no human gate was reachable; read-only action allowed and "
                  "recorded" if allow else
                  "no human gate was reachable — fail-closed (never a silent approval)")
        done = replace(it, decision=want, responder=str(responder or "unreachable"),
                       resolved_at=at)
        queue.replace(done)
        _sink(ledger, _interrupt_row(done, allow, "none", rule, reason,
                                     responder=done.responder))
        return ResumeOutcome(token=it.id, status="unreachable", allow=allow,
                             action=it.action, payload=it.payload,
                             action_hash=it.action_hash, reason=reason, rule=rule,
                             responder=done.responder)

    if it.expired(at):
        done = replace(it, status="expired", decision="expire", resolved_at=at)
        queue.replace(done)
        _sink(ledger, _interrupt_row(done, False, "none", "approval.expired",
                                     f"approval expired after {int(it.ttl_s)}s "
                                     "— the next matching call re-escalates"))
        return ResumeOutcome(token=it.id, status="expired", allow=False,
                             action=it.action, payload=it.payload,
                             action_hash=it.action_hash,
                             rule="approval.expired",
                             reason="approval expired before it was answered")

    status = {"approve": "approved", "deny": "denied", "expire": "expired"}[want]
    allow = want == "approve"
    done = replace(it, status=status, decision=want, responder=str(responder or ""),
                   resolved_at=at)
    queue.replace(done)
    rule = {"approve": "approval.approved", "deny": "approval.denied",
            "expire": "approval.expired"}[want]
    _sink(ledger, _interrupt_row(done, allow, "none", rule,
                                 f"{it.action} {status} by {done.responder or 'unknown'}",
                                 responder=done.responder))
    return ResumeOutcome(token=it.id, status=status, allow=allow, action=it.action,
                         payload=it.payload, action_hash=it.action_hash,
                         reason=f"{it.action} {status}", rule=rule,
                         responder=done.responder)


def authorize(token: str, action: str, payload: Any = None, *, store: Any = None,
              now: Any = None, consume: bool = True) -> AuthOutcome:
    """Does this approval authorize THIS exact action, right now?

    Bound to the action hash (an approval for A never authorizes B) and spent on
    use (``consume=True``): a capability grant for one call does not persist to
    the next — no ambient authority.
    """
    queue = _queue(store)
    it = queue.get(str(token or ""))
    if it is None:
        return AuthOutcome(False, "approval.unknown", "no such approval token",
                           token=str(token or ""))
    at = _now(now)
    if it.expired(at):
        return AuthOutcome(False, "approval.expired",
                           "approval expired — re-escalate", it.id, it.action_hash,
                           it.status)
    if it.status != "approved":
        return AuthOutcome(False, f"approval.{it.status}",
                           f"approval is {it.status}, not approved", it.id,
                           it.action_hash, it.status)
    if it.consumed_at and action_hash(action, payload) == it.action_hash:
        return AuthOutcome(False, "approval.consumed",
                           "approval already consumed — no ambient authority", it.id,
                           it.action_hash, it.status)
    if action_hash(action, payload) != it.action_hash:
        # checked before the consumed flag: a mismatched action must be refused
        # with the reason that names the real problem (OWASP: an approval for
        # hash A cannot authorize hash B).
        return AuthOutcome(False, "approval.action_mismatch",
                           "this approval authorizes a different action hash", it.id,
                           it.action_hash, it.status)
    if consume:
        queue.replace(replace(it, consumed_at=at))
    return AuthOutcome(True, "approval.authorized",
                       "approved by a human for this exact action", it.id,
                       it.action_hash, it.status)


def interrupt(token: str, *, store: Any = None) -> Optional[Interrupt]:
    """The typed Interrupt behind a token (None when unknown)."""
    return _queue(store).get(str(token or ""))


def pending(*, store: Any = None, now: Any = None) -> Tuple[Interrupt, ...]:
    """Every unresolved interrupt, newest last."""
    return _queue(store).pending(_now(now) if now is not None else None)


def recent(*, store: Any = None, limit: int = 12) -> List[Dict[str, Any]]:
    return _queue(store).recent(limit)


# ---------------------------------------------------------------------------
# The gate entry point — regex honesty guard, then guardrails, then verdict
# ---------------------------------------------------------------------------

def _gate_module() -> Any:
    """Load the sibling ``hday_gate`` by path (no sys.path assumption)."""
    global _GATE_MOD, _GATE_MOD_TRIED
    if _GATE_MOD_TRIED:
        return _GATE_MOD
    _GATE_MOD_TRIED = True
    try:
        import sys
        import importlib.util as ilu
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hday_gate.py")
        existing = sys.modules.get("hday_gate")
        if existing is not None and os.path.abspath(
                getattr(existing, "__file__", "") or "") == path:
            _GATE_MOD = existing
        else:
            spec = ilu.spec_from_file_location("hday_gate", path)
            if spec is not None and spec.loader is not None:
                mod = ilu.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _GATE_MOD = mod
    except Exception:
        _GATE_MOD = None
    return _GATE_MOD


def decide(action: str = "", user_request: str = "", context: Optional[Dict[str, Any]] = None,
           *, policy: Optional[Policy] = None, judge: Optional[Callable] = None,
           guardrails: Optional[Sequence[Any]] = None, store: Any = None,
           ledger: Optional[Callable] = None, now: Any = None,
           session: str = "", **_kw: Any) -> Dict[str, Any]:
    """Capability-aware gate: the existing gate first, then rails, then policy.

    Order is load-bearing. The regex honesty guard (``hday_gate.decide``) runs
    first and its deny is returned untouched — this lane ADDS enforcement, it
    never relaxes what the free deny-list already blocks. Only then do the
    ``(passed, value)`` rails get to rewrite the payload, and only then is the
    capability verdict consulted. A typed ``escalate`` verdict pauses for a human
    (``pause``) and returns the token.
    """
    ctx = dict(context) if isinstance(context, dict) else {}
    policy = policy if isinstance(policy, Policy) else Policy.default()
    action = str(action or "")

    gate = _gate_module()
    if gate is None:
        base: Dict[str, Any] = {
            "allow": True, "kind": "unjudged",
            "reason": "gate module hday_gate.py unavailable — fail-open (unjudged)",
            "row": {"kind": "unjudged", "lane": "local", "ms": 0.0, "cost": 0.0,
                    "rule": "GateUnavailable"},
        }
    else:
        try:
            base = gate.decide(action, user_request, ctx, judge=judge)
        except Exception as exc:      # decide() never raises by contract; belt anyway
            base = {"allow": True, "kind": "unjudged",
                    "reason": f"gate decide raised {type(exc).__name__}: {exc} — fail-open",
                    "row": {"kind": "unjudged", "lane": "local", "ms": 0.0, "cost": 0.0}}
    if not base.get("allow"):
        _sink(ledger, _gate_row(action, ctx, base.get("row") or {}, False,
                                base.get("reason") or "", session))
        return base

    outcome = run_guardrails(action, ctx, guardrails or (), ledger=ledger, now=now)
    if not outcome.passed:
        row = {"kind": "judged", "lane": "local", "ms": 0.0, "cost": 0.0,
               "rule": "Guardrail", "rail": outcome.blocked_by,
               "detail": outcome.reason,
               "guardrails": outcome.as_row()}
        reason = f"blocked by guardrail {outcome.blocked_by!r}: {outcome.reason}"
        _sink(ledger, _gate_row(action, ctx, row, False, reason, session))
        return {"allow": False, "kind": "judged", "reason": reason, "row": row,
                "guardrails": outcome.as_row()}
    if isinstance(outcome.value, dict):
        ctx = outcome.value

    caps = capabilities_for(action, ctx)
    verdict = verdict_for(caps, policy)
    row: Dict[str, Any] = {"kind": "judged", "lane": "local", "ms": 0.0, "cost": 0.0,
                           "rule": verdict.rule, "verdict": verdict.as_row(),
                           "capabilities": list(verdict.capabilities),
                           "policy": verdict.policy,
                           "guardrails": outcome.as_row()}
    if verdict.decision == "deny":
        _sink(ledger, _gate_row(action, ctx, row, False, verdict.reason, session))
        return {"allow": False, "kind": "judged", "reason": verdict.reason,
                "row": row, "verdict": verdict, "guardrails": outcome.as_row()}
    if verdict.decision == "escalate":
        token = pause(action, ctx, prompt=f"approve {action}?", reason=verdict.reason,
                      capabilities=caps, policy=policy, session=session, store=store,
                      ledger=ledger, now=now)
        reachable = _escalation_available(None)
        reason = f"escalated: {verdict.reason} (token {token})"
        return {"allow": False, "kind": "judged", "reason": reason, "row": row,
                "verdict": verdict, "token": token,
                "directive": "approve" if reachable else "block",
                "guardrails": outcome.as_row()}
    _sink(ledger, _gate_row(action, ctx, row, True, verdict.reason, session))
    return {"allow": True, "kind": "judged", "reason": verdict.reason, "row": row,
            "verdict": verdict, "guardrails": outcome.as_row()}


def _gate_row(action: str, ctx: Dict[str, Any], row: Dict[str, Any], allow: bool,
              reason: str, session: str = "") -> Dict[str, Any]:
    out = dict(row or {})
    out.setdefault("kind", "judged")
    out.setdefault("lane", "local")
    out.setdefault("ms", 0.0)
    out.setdefault("cost", 0.0)
    out["ts"] = _now(None)
    out["tool"] = action
    out["call"] = _preview(ctx)
    out["allow"] = bool(allow)
    out["directive"] = "none"
    out["reason"] = _brief(reason, 240)
    out["session"] = session
    return out
