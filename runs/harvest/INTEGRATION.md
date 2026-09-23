# Harvest integration record — six lanes → `feat/harvest-integration`

Branch cut from `main` = `8c8843f`. Six lane branches, merged one at a time, full
suite (`python3 -m pytest tests -q`; baseline **269 passed** on main) run after
**every** merge.

## Merge order and suite count

| # | lane | how | commit | suite after |
|---|------|-----|--------|-------------|
| 1 | `feat/lane-durable` | fast-forward | `8c84e08` | **286 passed** (+17) |
| 2 | `feat/lane-approvals` | auto-merge (ort) | `d17bb6b` | **334 passed** (+48) |
| 3 | `feat/lane-trace` | **conflict**, resolved | `09a63e1` | **364 passed** (+30) |
| 4 | `feat/lane-context` | auto-merge (ort) | `8b8dd50` | **374 passed** (+10) |
| 5 | `feat/lane-orchestration` | auto-merge (ort) | `c3bde56` | **392 passed** (+18) |
| 6 | `feat/lane-interop` | auto-merge (ort) | `ac159e1` | **470 passed** (+78) |

`470 == 269 + 17 + 48 + 30 + 10 + 18 + 78` — every lane's tests arrived, none lost.

## Conflicts

Exactly one, in `__init__.py`:

- **`register()`** — `lane-approvals` and `lane-trace` both appended a
  `ctx.register_command(...)` block at the same anchor (the `day-ledger`
  registration). **Resolution:** kept **both**, in merge order — `day-queue`
  (approvals) then `day-trace` (trace). Purely additive; neither block edited.

Everything else merged cleanly because `ort` resolved the `__init__.py` edits as
disjoint regions: approvals' widened `_gate_log` key list (`interrupt`, `status`,
`action_hash`, `blast_radius`, `capabilities`, `durable`, `responder`, `prompt`,
`rail`, `before_hash`, `after_hash`, `verdict`, `guardrails`), trace's additive
`trace` key in `_cmd_ledger`, and durable's sink factory sit in separate regions
from the command registrations.

No two lanes defined the same private helper name: a top-level AST scan of the
merged `__init__.py` reports **202 defs, 0 duplicates**; the command registry has
**46 commands, 0 duplicate registrations**.

## Wiring completeness (all four hooks present and exercised)

- **durable** — `_durable_sink` → `rec["gate"]` through the gate's own
  `_append` / `_persist` / `_LOCK`.
- **approvals** — `_approvals_ledger` → `_gate_log`; `_ApprovalsStateStore` →
  `ctx.state`; `day-queue` registered.
- **trace** — span sidecar rows surfaced in `_cmd_ledger["trace"]`; `day-trace`
  registered.
- **widened `_gate_log` keys** (above).

Every writer to `rec["gate"]` lives in `__init__.py` (3 sites); no lane module
writes a second gate ledger.

## Duplicate-store / duplicate-mechanism audit (one ledger, one store)

- **UNIFIED — duplicate sibling-module loader (split module state).** The durable
  lane's `_load_hday_durable()` loaded `hday_durable.py` under a private
  `hday_durable_runtime` name while the host's generic `_hday()` loaded siblings
  under their own names: two module objects for one file. It now delegates to
  `_hday()`. Doing that exposed a **pre-existing defect in `_hday()` itself**
  (commit `d659f45`, main): a sibling module with a module-level `@dataclass`
  cannot be exec'd from a bare spec, because CPython's `dataclasses` resolves
  `cls.__module__` through `sys.modules` — the load raised
  `AttributeError: 'NoneType' object has no attribute '__dict__'`, `_hday`
  swallowed it and cached `None`. Measured on a fresh interpreter (the production
  path: the plugin host loads `__init__.py` as a package and never puts the plugin
  dir on `sys.path`, so path-loading is the only route) **3 of 11 sibling modules
  loaded, 8 returned `None`** — `hday_ctxscore`, `hday_trace`, `hday_approvals`,
  `hday_router`, `hday_durable`, `hday_context_hygiene`, `hday_orchestration`,
  `hday_interop` — identically under system `python3` **3.14.4** and the deployed
  hermes-agent venv **3.11.15**. The suite never saw it because each lane's test
  file imports its module plainly first, so `_hday`'s reuse branch found it in
  `sys.modules`. Fixed in `3abef4d` (register before `exec_module`, evict on
  failure, only when the name is free — the same pattern the host's own plugin
  loader uses). Three of the six lanes wire through `_hday`, so without this the
  union's wiring is inert on the live path.
- **LEGITIMATE, kept — trace's `LEDGER`** is a span sidecar keyed by run id, not a
  decision ledger: surfaced **additively** as `_cmd_ledger["trace"]` alongside
  `gate` and `ctxscore`, never replacing either.
- **LEGITIMATE, kept — approvals' `ApprovalQueue`** persists into the host's own
  state store (`ctx.state`, key `hday_approvals` — the same one-key-per-module
  convention as `_SECTION_STATE_KEY`) and its rows go to the one gate ledger via
  `_approvals_ledger`; `configure()` is re-applied on every call, so the module's
  `MemoryStore` / `FileStore` defaults are only standalone/test fallbacks. Not a
  shadow store.
- **LEGITIMATE, kept, reported — the host's pre-existing `_PENDING_APPROVALS` /
  `rec["approvals"]`** (an in-memory observer feed of approval prompts the *host*
  raised; not persisted, not resumable) coexists with the lane's durable interrupt
  queue (resume tokens, TTL, action hashes, persisted). Two pending-approval
  collections, but different sources of truth and different questions
  (`day-attention` / `day-approvals` read the feed; `day-queue` reads the queue),
  so neither is a shadow of the other.
- **LEGITIMATE, kept — durable's `Journal`** is run state (optionally
  file-backed), explicitly *not* a ledger; its rows go through the injected sink.
- **LEGITIMATE, kept — `hday_orchestration`'s and `hday_context_hygiene`'s
  `LEDGER` deques** are module-domain ledgers, the repo's existing convention
  (`hday_ctxscore` has had one since main). No host counterpart to unify to.
- **NONE — `hday_interop`** holds no ledger/store/queue/observer: only constant
  documents and per-instance transports.
- **Reported gap (not changed)** — `hday_context_hygiene`, `hday_orchestration`
  and `hday_interop` are standalone by design (no `__init__.py` edit), so their
  ledgers/archives are in-process only and no host hook consumes them yet; they
  are exercised by their own tests and by the lane smoke check.

## Verification on the integration head

- Full suite **470 passed** under system `python3` 3.14.4 **and** under the
  deployed hermes-agent venv `python3` 3.11.15.
- `python3 _demo_interop.py` → exit 0, all end-to-end interop checks ok.
- Fresh-interpreter lane smoke driving each lane through the plugin's own seams:
  **24/24 checks pass** — durable rows land on the gate ledger; `day-trace` +
  `_cmd_ledger["trace"]`; `day-queue` + `pause()` interrupt row + queue store is
  the host state store; orchestration child attribution; context-hygiene
  record/recall, externalize/rehydrate, pinned refusal; interop loads.
- Same probe after the loader fix: **11 of 11** sibling modules load on both
  interpreters.

## Housekeeping

- No stray `hd-wt/` directory existed in the main checkout (verified with `find`
  and `git status --porcelain --ignored`: the tree is clean) — nothing to move.
- The leftover `feat/int-*` branches and worktrees from earlier work were left
  untouched.
- Not pushed — the parent handles push and deploy.
