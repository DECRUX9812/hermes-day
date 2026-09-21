/**
 * hermes-day — a full-screen day cockpit for Hermes Desktop.
 *
 * One screen for your whole Hermes day: everything waiting on you (approvals,
 * clarifies, input prompts) across every profile and connection, what Hermes
 * is running right now, what it finished while you were away, and what's
 * scheduled next. Act inline or jump straight into the session.
 *
 * Single self-contained ESM bundle — no JSX, no build step. Renders through
 * jsx()/jsxs() against the desktop SDK surface only.
 */

import {
  Badge,
  Button,
  Codicon,
  ErrorState,
  Input,
  Kbd,
  Loader,
  Popover,
  PopoverContent,
  PopoverTrigger,
  ScrollArea,
  SearchField,
  SessionStatusDot,
  Tip,
  KEYBINDS_AREA,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  atom,
  cn,
  haptic,
  host,
  isSubmitEnter,
  markSessionUnreadFinished,
  ackStoredSessionId,
  nextRunOverdueMs,
  queryClient,
  relativeTime,
  useQuery,
  useValue
} from '@hermes/plugin-sdk'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

// ---------------------------------------------------------------------------
// tiny element helper — jsx() ergonomics without a transpiler
// ---------------------------------------------------------------------------

const e = (tag, props, ...kids) => {
  const flat = []
  const push = c => {
    if (Array.isArray(c)) c.forEach(push)
    else if (c === 0 || c === '' || Boolean(c)) flat.push(c)
  }
  kids.forEach(push)
  const p = props ? { ...props } : {}
  if (flat.length === 1) p.children = flat[0]
  else if (flat.length > 1) p.children = flat
  return flat.length > 1 ? jsxs(tag, p) : jsx(tag, p)
}

// ---------------------------------------------------------------------------
// model
// ---------------------------------------------------------------------------

const DAY_PATH = '/day'
const SCAN_QK = ['hermes-day', 'scan']
const CRON_QK = ['hermes-day', 'cron']
const STORE_KEY = 'finished.v1'
const MAX_SEQ = Number.MAX_SAFE_INTEGER
const FINISHED_TTL = 36 * 60 * 60 * 1000
const RPC_TIMEOUT = 9000

/** runtime statuses that mean the session is actively producing work */
const FLIGHT = new Set(['working', 'streaming', 'starting', 'resuming'])
/** request methods the user answers with a choice, a string, or in-session */
const REQUEST_KIND = {
  approval: 'approval',
  clarify: 'clarify',
  sudo: 'input',
  secret: 'input',
  vault: 'input',
  mcp_setup: 'input',
  read_terminal: 'other',
  tour: 'other',
  platform_followup: 'other'
}

const KIND_RANK = { approval: 0, clarify: 1, input: 2, other: 3 }

/** first time this scan saw an open request — requests carry no timestamp */
const firstSeen = new Map()
/** runtime session id -> { storedId, title, route } from the last scan */
const lastLive = new Map()
/** source.key -> recent stored session rows ({id, title, started_at}) for
 *  bridging finished entries whose runtime id never got a session_key. */
const $recentStored = atom({})
/** ctx.storage / ctx.os, captured in register() for module-level use */
let storageRef = null
let notifyRef = null
/** finished-but-unreviewed sessions, persisted across reloads */
const $finished = atom([])
/** muted sources — hidden from counts until unmuted (Sources rail toggle) */
const $muted = atom({})
/** snoozed requests — requestId -> unix ms until which the card stays hidden */
const $snoozed = atom({})
/** user prefs — { notify: boolean } */
const $prefs = atom({ notify: true })
/** pinned sessions — storedId -> { storedId, title, route, profile, at } */
const $pinned = atom({})
/** daily triage counter — { date: 'YYYY-MM-DD', count } */
const $triage = atom({ date: '', count: 0 })
/** saved quick-prompt presets under the task bar */
const $prompts = atom(null)
/** last dismissed finished item — powers the undo chip */
const $undo = atom(null)

// evidence gate verdicts from the agent half (pre_tool_call/post_tool_call/
// pre_verify); key: `${sourceKey}#${session_key}`, value: {verdict, detail, ...}
const $evidence = atom({})
/** sessionKey -> recent latest_seq samples — the in-flight heartbeat */
const seqHist = new Map()
const SPARK_LEN = 14
const STALE_MS = 10 * 60 * 1000
/** request ids already notified about this app session */
const notified = new Set()
/** first scan seeds `notified` silently — no burst on app start */
let notifyArmed = false

const SNOOZE_MS = 15 * 60 * 1000

const MUTED_KEY = 'muted.v1'
const SNOOZE_KEY = 'snoozed.v1'
const PREFS_KEY = 'prefs.v1'
const PINNED_KEY = 'pinned.v1'
const TRIAGE_KEY = 'triage.v1'
const PROMPTS_KEY = 'prompts.v1'
const DEFAULT_PROMPTS = ['Summarize overnight logs', 'Review my open PRs', 'Check cron results']

let stylesInjected = false
function ensureDayStyles() {
  if (stylesInjected || typeof document === 'undefined') return
  stylesInjected = true
  const el = document.createElement('style')
  el.textContent =
    '@keyframes hday-confetti{0%{transform:translateY(-10vh) rotate(0)}100%{transform:translateY(110vh) rotate(720deg)}}' +
    '@keyframes hday-stale{0%,100%{box-shadow:0 0 0 0 rgba(239,68,68,0)}50%{box-shadow:0 0 0 2px rgba(239,68,68,.4)}}' +
    '.hday-card{transition:box-shadow .15s ease,border-color .15s ease}' +
    '.hday-card:hover{border-color:#333a47}' +
    '.hday-stat{transition:border-color .12s ease,background .12s ease}' +
    '.hday-stat:hover{background:#1a1d26;border-color:#333a47}' +
    '.hday-row{transition:background .12s ease}' +
    '.hday-row:hover{background:#1a1d26}' +
    '.hday-chip{transition:border-color .12s ease,color .12s ease}' +
    '.hday-chip:hover{border-color:#333a47;color:#e2e8f0}' +
    // the app skin is light — pin our cockpit palette inside the page
    '.hday-root [class*="text-muted-foreground"]{color:#94a3b8}' +
    '.hday-root .text-destructive{color:#ef4444}' +
    '.hday-root .text-amber-500{color:#f59e0b}' +
    '.hday-root .text-emerald-500{color:#34d399}' +
    '.hday-root .bg-emerald-500{background:#34d399}' +
    '.hday-root .text-red-400{color:#f87171}' +
    // inspector feed rows + tabs
    '.hday-feedrow{cursor:pointer}' +
    '.hday-feedrow.hday-sel{background:#1a1d26;box-shadow:inset 2px 0 0 #38bdf8}' +
    '.hday-tab{cursor:pointer;border-bottom:1px solid transparent}' +
    '.hday-tab:hover{color:#f3f4f6}' +
    '.hday-tab.hday-tab-on{color:#f3f4f6;border-bottom-color:#38bdf8}' +
    '.hday-diff-add{color:#34d399;background:rgba(52,211,153,.07)}' +
    '.hday-diff-del{color:#ef4444;background:rgba(239,68,68,.07)}' +
    '.hday-diff-hunk{color:#38bdf8}' +
    // while /day is foreground, the host shell follows our dark canvas —
    // sidebar + floating notices stop fighting the palette
    'body:has(.hday-root) [class*="sidebar-wrapper"]{background:#0b0c10!important;color:#f3f4f6;'
      + '--ui-bg-primary:#0b0c10;--ui-bg-secondary:#161922;--ui-bg-quaternary:#1a1d26;'
      + '--ui-bg-sidebar:#0b0c10;--sidebar:#0b0c10;--sidebar-accent:#1a1d26;'
      + '--ui-text-primary:#f3f4f6;--ui-text-secondary:#94a3b8;--ui-text-tertiary:#64748b;'
      + '--ui-stroke-secondary:#262a33;--stroke-nous:#262a33}' +
    'body:has(.hday-root) [class*="sidebar-wrapper"] [class*="text-muted-foreground"]{color:#94a3b8}' +
    // dock the floating approval/notice modal: bottom-right tray, dark, never over the KPIs
    'body:has(.hday-root) div[class*="over-modal"]{top:auto!important;bottom:14px!important;'
      + 'left:auto!important;right:14px!important;transform:none!important;'
      + 'width:min(370px,92vw)!important}' +
    'body:has(.hday-root) div[class*="over-modal"] [class*="bg-popover"]{'
      + 'background:#161922!important;color:#f3f4f6!important;border-color:#262a33!important;'
      + 'backdrop-filter:none!important;box-shadow:0 8px 28px rgba(0,0,0,.55)!important}' +
    'body:has(.hday-root) div[class*="over-modal"] [class*="text-muted-foreground"]{color:#94a3b8!important}'
  document.head.appendChild(el)
}

// design tokens — true dark cockpit, colors reserved for actionable state
const C = {
  canvas: '#0b0c10',
  surface: '#161922',
  surfaceHover: '#1a1d26',
  border: '#262a33',
  borderHover: '#333a47',
  text: '#f3f4f6',
  muted: '#94a3b8',
  faint: '#64748b',
  mono: '#38bdf8',
  amber: '#f59e0b',
  emerald: '#34d399',
  red: '#ef4444',
  slate: '#8b949e'
}

const dayStamp = () => new Date().toISOString().slice(0, 10)
const triageToday = () => {
  const t = $triage.get()
  return t.date === dayStamp() ? t.count : 0
}
const bumpTriage = () => {
  const t = $triage.get()
  const next = { date: dayStamp(), count: (t.date === dayStamp() ? t.count : 0) + 1 }
  $triage.set(next)
  try {
    storageRef && storageRef.set(TRIAGE_KEY, next)
  } catch {}
}

/** kind -> accent color + icon + label — the card's identity at a glance */
const KIND_STYLE = {
  approval: { icon: 'shield', label: 'Approval', color: C.amber },
  clarify: { icon: 'comment-discussion', label: 'Question', color: C.mono },
  input: { icon: 'keyboard', label: 'Input', color: '#c084fc' },
  other: { icon: 'question', label: 'Request', color: C.slate }
}

const routeKey = route =>
  route ? `${route.connectionId ?? ''}/${route.profile ?? ''}/${route.targetProfile ?? ''}` : 'local'

const sourceName = route => {
  if (!route) return 'Local'
  const target = String(route.targetProfile ?? '').trim()
  const via = String(route.profile ?? '').trim()
  return target && via && target !== via ? `${via} · ${target}` : target || via || 'Local'
}

const rpc = (route, method, params, timeoutMs = RPC_TIMEOUT) =>
  route ? host.requestProfile(route, method, params, timeoutMs) : host.request(method, params, timeoutMs)

const errMsg = err => String((err && err.message) || err || 'unreachable')

const epochMs = v => {
  const n = Number(v)
  if (!Number.isFinite(n) || n <= 0) return null
  return n < 1e12 ? n * 1000 : n
}

// ---------------------------------------------------------------------------
// scans — everything keyed off live session + open-request snapshots
// ---------------------------------------------------------------------------

async function scanRoute(route) {
  const source = { key: routeKey(route), label: sourceName(route), route, unreachable: null }
  const needs = []
  const flight = []
  const waiting = []

  let live = null
  try {
    live = await rpc(route, 'session.active_list', {})
  } catch (err) {
    source.unreachable = errMsg(err)
    return { source, needs, flight, waiting }
  }

  const sessions = Array.isArray(live && live.sessions) ? live.sessions : []
  const seenKeys = new Set()

  await Promise.all(
    sessions.map(async s => {
      lastLive.set(s.id, { storedId: s.session_key || null, title: s.title || '', route })
      const hkey = `${source.key}#${s.id}`
      seenKeys.add(hkey)
      let open = []
      try {
        // last_seen past every seq -> empty event page, but the open-request
        // snapshot always ships — the cheap "what is this session asking" probe.
        const snap = await rpc(route, 'session.events.since', { session_id: s.id, last_seen: MAX_SEQ })
        open = snap && Array.isArray(snap.open_requests) ? snap.open_requests : []
        if (snap && Number.isFinite(snap.latest_seq)) {
          const h = seqHist.get(hkey) || []
          h.push(snap.latest_seq)
          while (h.length > SPARK_LEN + 1) h.shift()
          seqHist.set(hkey, h)
        }
      } catch {}

      for (const r of open) {
        if (!r || !r.id) continue
        if (!firstSeen.has(r.id)) firstSeen.set(r.id, Date.now())
        needs.push({
          key: `${source.key}#${r.id}`,
          requestId: r.id,
          method: String(r.method || ''),
          kind: REQUEST_KIND[r.method] || 'other',
          params: r.params && typeof r.params === 'object' ? r.params : {},
          route,
          sourceKey: source.key,
          sourceLabel: source.label,
          sessionId: s.id,
          storedId: s.session_key || null,
          title: s.title || 'Session',
          preview: s.preview || '',
          model: s.model || '',
          firstSeenAt: firstSeen.get(r.id)
        })
      }

      if (open.length === 0) {
        const row = { route, sourceLabel: source.label, sourceKey: source.key, session: s, spark: seqHist.get(hkey) || [] }
        if (s.status === 'waiting') waiting.push(row)
        else if (FLIGHT.has(s.status)) flight.push(row)
      }
    })
  )

  for (const k of seqHist.keys()) if (k.startsWith(source.key + '#') && !seenKeys.has(k)) seqHist.delete(k)

  // Evidence-gate verdicts from the agent half, if installed — one plugin
  // command dispatch per route, keyed by durable session_key.
  try {
    const disp = await rpc(route, 'command.dispatch', { name: 'day-evidence', arg: '' })
    const out = disp && (disp.output || disp.text || '')
    const parsed = out && out.trim().startsWith('{') ? JSON.parse(out) : null
    const sessions = parsed && parsed.sessions
    if (sessions && typeof sessions === 'object') {
      const ev = { ...$evidence.get() }
      for (const [k, v] of Object.entries(sessions)) {
        if (v && (v.verdict || v.runs || v.blocks || v.vacuum || (v.snaps && v.snaps.length))) ev[`${source.key}#${k}`] = v
      }
      $evidence.set(ev)
    }
  } catch {}

  // recent stored rows — bridges finished entries that never hit active_list
  try {
    const list = await rpc(route, 'session.list', { limit: 40 })
    const rows = Array.isArray(list && list.sessions) ? list.sessions : []
    $recentStored.set({ ...$recentStored.get(), [source.key]: rows })
  } catch {}

  // forget request ids that resolved between scans so re-asks re-stamp
  if (firstSeen.size > 400) {
    const liveIds = new Set(needs.map(n => n.requestId))
    for (const id of firstSeen.keys()) if (!liveIds.has(id)) firstSeen.delete(id)
  }

  return { source, needs, flight, waiting }
}

async function scanInbox() {
  let routes = null
  try {
    routes = await host.profileRoutes()
  } catch {}
  const list = Array.isArray(routes) && routes.length > 0 ? routes : [null]
  const parts = await Promise.all(list.map(scanRoute))

  const sources = []
  const needs = []
  const flight = []
  const waiting = []
  for (const p of parts) {
    sources.push(p.source)
    needs.push(...p.needs)
    flight.push(...p.flight)
    waiting.push(...p.waiting)
  }
  needs.sort((a, b) => KIND_RANK[a.kind] - KIND_RANK[b.kind] || a.firstSeenAt - b.firstSeenAt)
  flight.sort((a, b) => (epochMs(b.session.last_active) ?? 0) - (epochMs(a.session.last_active) ?? 0))

  // mute + snooze are presentation-layer filters on top of the raw truth
  const muted = $muted.get()
  const snoozed = $snoozed.get()
  const now = Date.now()
  for (const n of needs) {
    n.muted = Boolean(muted[n.sourceKey])
    n.snoozedUntil = snoozed[n.requestId] || 0
  }
  const visible = needs.filter(n => !n.muted && n.snoozedUntil <= now)

  // notify on genuinely NEW waiting items — never on the boot scan
  if (notifyRef && $prefs.get().notify) {
    if (notifyArmed) {
      for (const n of visible) {
        if (notified.has(n.requestId)) continue
        notified.add(n.requestId)
        const title = n.kind === 'approval' ? 'Approval needed' : n.kind === 'clarify' ? 'Question from Hermes' : 'Input needed'
        const body = `${n.title}: ${n.params.command || n.params.question || n.params.description || n.method}`.slice(0, 140)
        try {
          notifyRef.notify({ title, body })
        } catch {}
      }
    } else {
      for (const n of visible) notified.add(n.requestId)
      notifyArmed = true
    }
  }

  return { scannedAt: Date.now(), sources, needs: visible, hiddenCount: needs.length - visible.length, flight, waiting }
}

async function scanCron() {
  let routes = null
  try {
    routes = await host.profileRoutes()
  } catch {}
  const list = Array.isArray(routes) && routes.length > 0 ? routes : [null]
  const out = []
  await Promise.all(
    list.map(async route => {
      let res = null
      try {
        res = await rpc(route, 'cron.manage', { action: 'list' })
      } catch {
        return
      }
      for (const job of (res && res.jobs) || []) {
        out.push({ key: `${routeKey(route)}#${job.job_id || job.name}`, route, sourceLabel: sourceName(route), job })
      }
    })
  )
  const nextMs = j => {
    const t = Date.parse(j.next_run_at || '')
    return Number.isNaN(t) ? Number.MAX_SAFE_INTEGER : t
  }
  out.sort((a, b) => {
    const ea = a.job.enabled === false ? 1 : 0
    const eb = b.job.enabled === false ? 1 : 0
    return ea - eb || nextMs(a.job) - nextMs(b.job)
  })
  return { scannedAt: Date.now(), jobs: out }
}

// ---------------------------------------------------------------------------
// finished-but-unreviewed — event driven, persisted via ctx.storage
// ---------------------------------------------------------------------------

const loadMap = key => {
  try {
    const v = storageRef ? storageRef.get(key, {}) : {}
    return v && typeof v === 'object' && !Array.isArray(v) ? v : {}
  } catch {
    return {}
  }
}

const persistMap = (key, map) => {
  try {
    storageRef && storageRef.set(key, map)
  } catch {}
}

const toggleMute = key => {
  const next = { ...$muted.get() }
  if (next[key]) delete next[key]
  else next[key] = true
  $muted.set(next)
  persistMap(MUTED_KEY, next)
}

const snoozeRequest = (requestId, ms = SNOOZE_MS) => {
  const next = { ...$snoozed.get(), [requestId]: Date.now() + ms }
  $snoozed.set(next)
  persistMap(SNOOZE_KEY, next)
  invalidate()
}

const snoozePresets = () => {
  const nine = new Date()
  nine.setHours(9, 0, 0, 0)
  if (nine.getTime() <= Date.now()) nine.setDate(nine.getDate() + 1)
  return [
    { label: '15 minutes', ms: 15 * 60 * 1000 },
    { label: '1 hour', ms: 60 * 60 * 1000 },
    { label: 'Tomorrow, 9 am', ms: nine.getTime() - Date.now() }
  ]
}

const togglePin = (storedId, info) => {
  const next = { ...$pinned.get() }
  if (next[storedId]) delete next[storedId]
  else next[storedId] = { storedId, title: info.title || 'Session', route: info.route || null, profile: info.profile || null, at: Date.now() }
  $pinned.set(next)
  persistMap(PINNED_KEY, next)
}

const setNotifyPref = on => {
  const next = { ...$prefs.get(), notify: on }
  $prefs.set(next)
  persistMap(PREFS_KEY, next)
}

const loadFinished = () => {
  try {
    const v = storageRef ? storageRef.get(STORE_KEY, []) : []
    return Array.isArray(v) ? v.filter(f => f && f.at && Date.now() - f.at < FINISHED_TTL) : []
  } catch {
    return []
  }
}

const persistFinished = list => {
  try {
    storageRef && storageRef.set(STORE_KEY, list.slice(0, 60))
  } catch {}
}

const dismissFinished = key => {
  const item = $finished.get().find(f => f.key === key)
  const next = $finished.get().filter(f => f.key !== key)
  $finished.set(next)
  persistFinished(next)
  if (item) {
    $undo.set({ f: item })
    const f = item
    setTimeout(() => {
      const u = $undo.get()
      if (u && u.f === f) $undo.set(null)
    }, 6000)
    bumpTriage()
  }
}

const undoDismiss = () => {
  const u = $undo.get()
  if (!u) return
  $undo.set(null)
  const next = [u.f, ...$finished.get().filter(f => f.key !== u.f.key)]
  $finished.set(next)
  persistFinished(next)
}

/** Best-effort runtime→stored-id bridge for sessions that finished between
 *  scans (never seen in active_list, so lastLive missed them). `session.list`
 *  row `id` IS the stored session_key; match on title when known, else the
 *  single closest `started_at` inside a 90s window. */
function resolveStoredId(f) {
  if (!f) return null
  if (f.storedId) return f.storedId
  const stored = $recentStored.get()
  const sourceKey = f.sourceKey || (f.route ? routeKey(f.route) : 'local')
  const rows = stored[sourceKey] || Object.values(stored).flat()
  if (!rows.length) return null
  let best = null
  let bestDt = Infinity
  for (const r of rows) {
    if (!r || !r.id) continue
    if (f.title && f.title !== 'Session' && r.title && r.title !== f.title) continue
    const started = epochMs(r.started_at)
    if (!started || started > f.at + 5000) continue
    const dt = f.at - started
    if (dt < bestDt) { bestDt = dt; best = r }
  }
  return best && bestDt < 90000 ? best.id : null
}

function recordFinished(ev, kind) {
  const runtimeId = ev && ev.session_id
  if (!runtimeId || ev.replayed) return

  const known = lastLive.get(runtimeId) || {}
  const key = `${ev.connectionId || ''}/${ev.profile || ''}/${runtimeId}`
  const entry = {
    key,
    runtimeId,
    storedId: known.storedId || null,
    title: known.title || 'Session',
    profile: ev.profile || (known.route && known.route.targetProfile) || null,
    route: known.route || null,
    connectionId: ev.connectionId || null,
    at: Date.now(),
    kind
  }

  const rest = $finished.get().filter(f => f.key !== key)
  const next = [entry, ...rest].filter(f => Date.now() - f.at < FINISHED_TTL).slice(0, 60)
  $finished.set(next)
  persistFinished(next)

  // light the sidebar's own unread dot too — same store core paints from
  if (entry.storedId) {
    try {
      markSessionUnreadFinished(entry.storedId, entry.profile || undefined)
    } catch {}
  }
}

// ---------------------------------------------------------------------------
// actions — same answer paths the desktop's own cards take
// ---------------------------------------------------------------------------

async function respondApproval(item, choice) {
  try {
    await rpc(item.route, 'request.answer', { id: item.requestId, result: { choice } })
  } catch {
    await rpc(item.route, 'approval.respond', {
      session_id: item.sessionId,
      request_id: item.params.request_id,
      choice
    })
  }
  bumpTriage()
}

async function respondClarify(item, answer) {
  await rpc(item.route, 'request.answer', { id: item.requestId, result: { answer: answer ?? '' } })
  bumpTriage()
}

async function respondClarifyBatch(item, answersByQid) {
  const questions = Array.isArray(item.params.questions) ? item.params.questions : []
  for (let i = 0; i < questions.length; i += 1) {
    const qid = questions[i] && (questions[i].id || questions[i].question_id) ? questions[i].id || questions[i].question_id : `q${i}`
    const answer = answersByQid[qid]
    if (answer == null) continue
    await rpc(item.route, 'clarify.lock', { request_id: item.requestId, question_id: qid, answer })
  }
  bumpTriage()
}

/** Kick off a brand-new task on the active profile — create + first prompt. */
async function startTask(text) {
  const title = text.length > 60 ? `${text.slice(0, 57)}…` : text
  const created = await host.request('session.create', { title, source: 'desktop' })
  const sessionId = created && created.session_id
  if (!sessionId) throw new Error('session.create returned no session')
  await host.request('prompt.submit', { session_id: sessionId, text })
  return created
}

/** Resolve every pending approval in one session at once. */
async function respondAllForSession(item, choice) {
  await rpc(item.route, 'approval.respond', {
    session_id: item.sessionId,
    choice,
    all: true
  })
}

/** Stop a running turn in place. */
async function interruptSession(route, sessionId) {
  await rpc(route, 'session.interrupt', { session_id: sessionId })
}

/** Send a follow-up prompt to a live session without leaving the board. */
async function nudgeSession(route, sessionId, text) {
  await rpc(route, 'prompt.submit', { session_id: sessionId, text })
}

async function openItemSession(item) {
  const target = item.storedId || item.sessionId
  const opts = {
    awaitHydration: true,
    forceResume: true,
    retryHydrationTimeoutOnce: true
  }
  if (item.route) {
    opts.route = item.route
    opts.profile = item.route.targetProfile
  }
  await host.openSession(target, opts)
}

const invalidate = () => {
  try {
    queryClient.invalidateQueries({ queryKey: ['hermes-day'] })
  } catch {}
}

// ---------------------------------------------------------------------------
// small shared bits
// ---------------------------------------------------------------------------

function SectionLabel({ icon, title, count, color, action }) {
  return jsxs('div', {
    className: 'mb-2 flex items-center gap-2 px-1',
    children: [
      jsx('span', {
        className: 'inline-flex size-5 items-center justify-center rounded',
        style: { color: color || C.faint, background: C.surface, border: `1px solid ${C.border}` },
        children: jsx(Codicon, { name: icon, size: 12 })
      }),
      jsx('span', {
        className: 'text-[0.7rem] font-semibold uppercase tracking-[0.12em]',
        style: { color: C.muted },
        children: title
      }),
      typeof count === 'number'
        ? jsx('span', {
            className: 'rounded px-1.5 py-px text-[0.62rem] font-semibold tabular-nums',
            style: { color: color || C.muted, background: color ? `${color}1a` : C.surface, border: `1px solid ${C.border}` },
            children: String(count)
          })
        : null,
      jsx('span', { className: 'mx-1 h-px flex-1', style: { background: C.border } }),
      action || null
    ]
  })
}

/** flat KPI tile — bold count, muted label, color only on the state dot/icon */
function StatTile({ icon, label, n, color, scrollTo }) {
  return jsxs('button', {
    type: 'button',
    className: 'hday-stat flex min-w-0 items-center gap-3 rounded-lg px-3.5 py-2.5 text-left',
    style: { background: C.surface, border: `1px solid ${C.border}` },
    onClick: () => {
      if (scrollTo) {
        try {
          document.getElementById(scrollTo)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
        } catch {}
      }
      haptic('selection')
    },
    children: [
      jsx('span', {
        className: 'size-2 shrink-0 rounded-full',
        style: { background: color }
      }),
      jsxs('span', {
        className: 'flex min-w-0 items-baseline gap-2',
        children: [
          jsx('span', { className: 'text-xl font-bold leading-6 tabular-nums', style: { color: C.text }, children: String(n) }),
          jsx('span', { className: 'truncate text-[0.68rem] font-medium uppercase tracking-wider', style: { color: C.faint }, children: label })
        ]
      })
    ]
  })
}

function SourcePill({ label }) {
  return jsx('span', {
    className: 'inline-flex shrink-0 items-center gap-1 rounded px-1.5 py-px text-[0.62rem] font-medium',
    style: { background: C.surfaceHover, color: C.faint, border: `1px solid ${C.border}` },
    children: label
  })
}

function AgoText({ ms }) {
  if (!ms) return null
  return jsx('span', {
    className: 'shrink-0 text-[0.68rem] tabular-nums',
    style: { color: C.faint },
    children: relativeTime(ms)
  })
}

const SPIN = { className: 'animate-spin' }
const spinIcon = name => jsx(Codicon, { name, spinning: true, className: 'text-[0.8rem]' })

// ---------------------------------------------------------------------------
// needs-you cards
// ---------------------------------------------------------------------------

function SnoozeMenu({ requestId, busy }) {
  const [openMenu, setOpenMenu] = useState(false)
  return jsxs(Popover, {
    open: openMenu,
    onOpenChange: setOpenMenu,
    children: [
      jsx(PopoverTrigger, {
        children: jsx(Tip, {
          label: 'Snooze',
          children: jsx(Button, {
            size: 'icon-xs',
            variant: 'ghost',
            disabled: Boolean(busy),
            children: jsx(Codicon, { name: 'snooze' })
          })
        })
      }),
      jsx(PopoverContent, {
        align: 'end',
        className: 'w-40 p-1',
        children: snoozePresets().map(p =>
          jsx('button', {
            type: 'button',
            className:
              'flex w-full items-center gap-2 rounded-[0.25rem] px-2 py-1.5 text-left text-[0.75rem] hover:bg-(--chrome-action-hover)',
            onClick: () => {
              snoozeRequest(requestId, p.ms)
              setOpenMenu(false)
            },
            children: p.label
          }, p.label)
        )
      })
    ]
  })
}

/** tiny throughput sparkline — deltas of latest_seq between polls */
function Spark({ seqs }) {
  const diffs = []
  for (let i = 1; i < seqs.length; i += 1) diffs.push(Math.max(0, seqs[i] - seqs[i - 1]))
  if (diffs.length < 2) return null
  const max = Math.max(1, ...diffs)
  const w = 46
  const h = 14
  const step = w / (diffs.length - 1)
  const pts = diffs.map((d, i) => `${(i * step).toFixed(1)},${(h - 2 - (d / max) * (h - 4)).toFixed(1)}`)
  return jsxs('svg', {
    width: w,
    height: h,
    'aria-hidden': true,
    className: 'shrink-0 opacity-70',
    children: jsx('polyline', {
      points: pts.join(' '),
      fill: 'none',
      stroke: '#58a6ff',
      strokeWidth: 1.5,
      strokeLinecap: 'round',
      strokeLinejoin: 'round'
    })
  })
}

function NeedsYouCard({ item, selected, longest }) {
  const [busy, setBusy] = useState('')
  const [failed, setFailed] = useState('')

  const run = useCallback(
    async (tag, fn) => {
      setBusy(tag)
      setFailed('')
      try {
        await fn()
        haptic('submit')
        invalidate()
      } catch (err) {
        setFailed(errMsg(err))
        haptic('cancel')
      } finally {
        setBusy('')
      }
    },
    [item]
  )

  const open = () =>
    run('open', async () => {
      await openItemSession(item)
    })

  const accent = KIND_STYLE[item.kind] || KIND_STYLE.other
  const stale = Date.now() - item.firstSeenAt > STALE_MS

  const header = jsxs('div', {
    className: 'flex min-w-0 items-center gap-2',
    children: [
      item.storedId ? jsx(SessionStatusDot, { storedSessionId: item.storedId }) : jsx(Codicon, { name: 'comment', className: 'text-muted-foreground' }),
      jsxs('span', {
        className: 'inline-flex shrink-0 items-center gap-1 rounded-[3px] px-1.5 py-px text-[0.62rem] font-semibold',
        style: { color: accent.color, background: `${accent.color}1f` },
        children: [jsx(Codicon, { name: accent.icon, size: 11 }), accent.label]
      }),
      jsx('span', { className: 'min-w-0 flex-1 truncate text-[0.82rem] font-semibold', style: { color: C.text }, children: item.title }),
      longest
        ? jsxs('span', {
            className: 'inline-flex shrink-0 items-center gap-1 rounded-[3px] px-1.5 py-px text-[0.62rem] font-semibold text-red-400',
            style: { background: '#ef44441f' },
            children: [jsx(Codicon, { name: 'flame', size: 11 }), 'waiting longest']
          })
        : null,
      jsx(SourcePill, { label: item.sourceLabel }),
      jsx(AgoText, { ms: item.firstSeenAt }),
      jsx(SnoozeMenu, { requestId: item.requestId, busy })
    ]
  })

  let body = null
  if (item.kind === 'approval') body = jsx(ApprovalBody, { item, busy, run, open })
  else if (item.kind === 'clarify') body = jsx(ClarifyBody, { item, busy, run, open })
  else body = jsx(GenericRequestBody, { item, busy, open })

  return jsxs('div', {
    className: 'hday-card rounded-lg p-3.5',
    style: {
      background: C.surface,
      border: `1px solid ${C.border}`,
      borderLeft: `2px solid ${accent.color}`,
      ...(stale ? { animation: 'hday-stale 2.4s ease-in-out infinite' } : null),
      ...(selected ? { boxShadow: `0 0 0 1px ${accent.color}66` } : null)
    },
    children: [
      header,
      body,
      selected
        ? jsxs('div', {
            className: 'mt-2 flex items-center gap-2.5 text-[0.65rem] text-muted-foreground/80',
            children: [
              jsxs('span', { className: 'flex items-center gap-1', children: [jsx(Kbd, { children: 'j/k' }), 'move'] }),
              item.kind === 'approval' ? jsxs('span', { className: 'flex items-center gap-1', children: [jsx(Kbd, { children: 'a' }), 'allow once'] }) : null,
              item.kind === 'approval' ? jsxs('span', { className: 'flex items-center gap-1', children: [jsx(Kbd, { children: 'd' }), 'deny'] }) : null,
              jsxs('span', { className: 'flex items-center gap-1', children: [jsx(Kbd, { children: 's' }), 'snooze'] }),
              jsxs('span', { className: 'flex items-center gap-1', children: [jsx(Kbd, { children: 'o' }), 'open'] })
            ]
          })
        : null,
      failed
        ? jsxs('div', {
            className: 'mt-2 flex items-center gap-1.5 text-[0.7rem] text-destructive',
            children: [jsx(Codicon, { name: 'warning' }), jsx('span', { children: failed })]
          })
        : null
    ]
  })
}

const APPROVAL_LABELS = { once: 'Allow once', session: 'Allow this session', always: 'Always allow', deny: 'Deny' }

function ApprovalBody({ item, busy, run, open }) {
  const p = item.params
  const base = Array.isArray(p.choices) && p.choices.length ? p.choices : p.smart_denied ? ['once', 'deny'] : ['once', 'session', 'always', 'deny']
  const choices = p.allow_permanent === false ? base.filter(c => c !== 'always') : base

  return jsxs('div', {
    className: 'mt-2',
    children: [
      p.description
        ? jsx('div', { className: 'mb-1 text-[0.75rem] text-(--ui-text-secondary)', children: String(p.description) })
        : null,
      p.command
        ? jsx('pre', {
            className: 'mb-2 max-h-32 overflow-auto whitespace-pre-wrap break-all rounded-lg p-2.5 font-mono text-[0.74rem] leading-5',
            style: { background: '#0b0c10', color: C.mono, border: `1px solid ${C.border}` },
            children: String(p.command)
          })
        : null,
      p.tool_name
        ? jsxs('div', {
            className: 'mb-2 flex items-center gap-1.5 text-[0.68rem] text-muted-foreground',
            children: [jsx(Codicon, { name: 'tools' }), jsx('span', { children: String(p.tool_name) })]
          })
        : null,
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-1.5',
        children: [
          choices.map(choice =>
            jsx(
              Button,
              {
                size: 'xs',
                variant: choice === 'deny' ? 'outline' : choice === 'once' ? 'default' : 'secondary',
                disabled: Boolean(busy),
                onClick: () => run(`c:${choice}`, () => respondApproval(item, choice)),
                children: busy === `c:${choice}` ? spinIcon('sync') : APPROVAL_LABELS[choice] || choice
              },
              choice
            )
          ),
          jsx(
            Button,
            { size: 'xs', variant: 'ghost', disabled: Boolean(busy), onClick: open, children: 'Open session' },
            'open'
          )
        ]
      })
    ]
  })
}

function ClarifyBody({ item, busy, run, open }) {
  const p = item.params
  const questions = Array.isArray(p.questions) ? p.questions : null
  const [text, setText] = useState('')
  const [picked, setPicked] = useState({})

  if (questions && questions.length > 0) {
    return jsx(BatchClarify, { item, questions, busy, run, open, picked, setPicked, text, setText })
  }

  const choices = Array.isArray(p.choices) ? p.choices.filter(c => typeof c === 'string') : []
  const multi = p.multi_select === true

  const submit = value =>
    run('send', () => respondClarify(item, value))

  const submitMulti = () => {
    const vals = Object.keys(picked).filter(k => picked[k])
    return submit(vals.join(', '))
  }

  return jsxs('div', {
    className: 'mt-2',
    children: [
      jsx('div', { className: 'mb-2 text-[0.78rem] leading-5 text-(--ui-text-primary)', children: String(p.question || 'Input requested') }),
      choices.length > 0 && !multi
        ? jsx('div', {
            className: 'mb-2 flex flex-wrap gap-1.5',
            children: choices.map(c =>
              jsx(
                Button,
                { size: 'xs', variant: 'secondary', disabled: Boolean(busy), onClick: () => submit(c), children: c },
                c
              )
            )
          })
        : null,
      choices.length > 0 && multi
        ? jsxs('div', {
            className: 'mb-2 flex flex-wrap gap-1.5',
            children: [
              choices.map(c =>
                jsx(
                  Button,
                  {
                    size: 'xs',
                    variant: picked[c] ? 'default' : 'outline',
                    disabled: Boolean(busy),
                    onClick: () => setPicked(prev => ({ ...prev, [c]: !prev[c] })),
                    children: c
                  },
                  c
                )
              ),
              jsx(
                Button,
                {
                  size: 'xs',
                  variant: 'secondary',
                  disabled: Boolean(busy) || !Object.values(picked).some(Boolean),
                  onClick: submitMulti,
                  children: busy === 'send' ? spinIcon('sync') : 'Send selection'
                },
                'send-multi'
              )
            ]
          })
        : null,
      jsxs('div', {
        className: 'flex items-center gap-1.5',
        children: [
          jsx(Input, {
            value: text,
            placeholder: choices.length ? 'Or type an answer…' : 'Type an answer…',
            disabled: Boolean(busy),
            onChange: ev => setText(ev.target.value),
            onKeyDown: ev => {
              if (isSubmitEnter(ev) && text.trim()) submit(text.trim())
            },
            className: 'h-7 flex-1 text-[0.78rem]'
          }),
          jsx(Button, {
            size: 'xs',
            variant: 'secondary',
            disabled: Boolean(busy) || !text.trim(),
            onClick: () => submit(text.trim()),
            children: busy === 'send' ? spinIcon('sync') : 'Send'
          }),
          jsx(Button, { size: 'xs', variant: 'ghost', disabled: Boolean(busy), onClick: open, children: 'Open' })
        ]
      })
    ]
  })
}

function BatchClarify({ item, questions, busy, run, open, picked, setPicked, text, setText }) {
  const answers = {}
  questions.forEach((q, i) => {
    const qid = q && (q.id || q.question_id) ? q.id || q.question_id : `q${i}`
    if (picked[qid]) answers[qid] = picked[qid]
  })

  const submitAll = () => run('send', () => respondClarifyBatch(item, answers))

  return jsxs('div', {
    className: 'mt-2 flex flex-col gap-3',
    children: [
      questions.map((q, i) => {
        const qid = q && (q.id || q.question_id) ? q.id || q.question_id : `q${i}`
        const qChoices = q && Array.isArray(q.choices) ? q.choices.filter(c => typeof c === 'string') : []
        const locked = q && q.locked === true
        return jsxs(
          'div',
          {
            className: 'rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-primary) p-2',
            children: [
              jsxs('div', {
                className: 'mb-1.5 flex items-start gap-1.5 text-[0.75rem] font-medium',
                children: [
                  jsx('span', { className: 'shrink-0 text-muted-foreground', children: `${i + 1}.` }),
                  jsx('span', { children: String((q && q.question) || '') }),
                  locked ? jsx(Badge, { size: 'xs', variant: 'success', children: 'locked' }) : null
                ]
              }),
              qChoices.length
                ? jsx('div', {
                    className: 'flex flex-wrap gap-1.5',
                    children: qChoices.map(c =>
                      jsx(
                        Button,
                        {
                          size: 'xs',
                          variant: picked[qid] === c ? 'default' : 'outline',
                          disabled: Boolean(busy) || locked,
                          onClick: () => setPicked(prev => ({ ...prev, [qid]: c })),
                          children: c
                        },
                        c
                      )
                    )
                  })
                : jsx(Input, {
                    value: picked[qid] ?? '',
                    placeholder: 'Type an answer…',
                    disabled: Boolean(busy) || locked,
                    onChange: ev => setPicked(prev => ({ ...prev, [qid]: ev.target.value })),
                    className: 'h-7 text-[0.75rem]'
                  })
            ]
          },
          qid
        )
      }),
      jsxs('div', {
        className: 'flex items-center gap-1.5',
        children: [
          jsx(
            Button,
            { size: 'xs', variant: 'secondary', disabled: Boolean(busy), onClick: submitAll, children: busy === 'send' ? spinIcon('sync') : 'Send answers' },
            'send'
          ),
          jsx(Button, { size: 'xs', variant: 'ghost', disabled: Boolean(busy), onClick: open, children: 'Open session' }, 'open')
        ]
      })
    ]
  })
}

const METHOD_ICON = {
  sudo: 'shield',
  secret: 'key',
  vault: 'lock',
  mcp_setup: 'plug',
  read_terminal: 'terminal',
  tour: 'compass',
  platform_followup: 'comment-discussion'
}

function GenericRequestBody({ item, busy, open }) {
  const p = item.params
  const prompt = p.prompt || p.question || p.description || p.message || ''
  return jsxs('div', {
    className: 'mt-2',
    children: [
      jsxs('div', {
        className: 'mb-2 flex items-center gap-1.5 text-[0.75rem] text-(--ui-text-secondary)',
        children: [
          jsx(Codicon, { name: METHOD_ICON[item.method] || 'question', className: 'shrink-0' }),
          jsx('span', { className: 'min-w-0 flex-1 truncate', children: String(prompt || `Hermes is asking for ${item.method}`) })
        ]
      }),
      jsxs('div', {
        className: 'flex items-center gap-1.5',
        children: [
          jsx(Button, { size: 'xs', variant: 'secondary', disabled: Boolean(busy), onClick: open, children: busy === 'open' ? spinIcon('sync') : 'Answer in session' }),
          jsx(Badge, { size: 'xs', variant: 'outline', children: item.method })
        ]
      })
    ]
  })
}

// ---------------------------------------------------------------------------
// flight / finished / cron rows
// ---------------------------------------------------------------------------

function FlightRow({ row }) {
  const s = row.session
  const [busy, setBusy] = useState(false)
  const [nudging, setNudging] = useState(false)
  const [nudgeText, setNudgeText] = useState('')
  const [note, setNote] = useState('')
  const pinnedMap = useValue($pinned)
  const pinned = Boolean(s.session_key && pinnedMap[s.session_key])
  const running = FLIGHT.has(s.status)

  const open = async () => {
    setBusy(true)
    try {
      await openItemSession({ storedId: s.session_key, sessionId: s.id, route: row.route })
    } catch {} finally {
      setBusy(false)
    }
  }

  const nudge = async () => {
    const t = nudgeText.trim()
    if (!t) return
    setNote('')
    try {
      await nudgeSession(row.route, s.id, t)
      setNudgeText('')
      setNudging(false)
      setNote('Sent')
      haptic('submit')
      invalidate()
    } catch (err) {
      setNote(errMsg(err))
      haptic('cancel')
    }
  }

  const stop = async ev => {
    ev.stopPropagation()
    setNote('')
    try {
      await interruptSession(row.route, s.id)
      haptic('cancel')
      invalidate()
    } catch (err) {
      setNote(errMsg(err))
    }
  }

  return jsxs('div', {
    className: 'hday-row group rounded-md border border-transparent transition-colors',
    children: [
      jsxs('div', {
        className: 'flex w-full cursor-pointer items-center gap-2.5 px-2 py-1',
        onClick: open,
        children: [
          s.session_key
            ? jsx(SessionStatusDot, { storedSessionId: s.session_key })
            : jsx('span', { className: 'size-1.5 rounded-full bg-emerald-500' }),
          jsxs('div', {
            className: 'min-w-0 flex-1',
            children: [
              jsx('div', { className: 'truncate text-[0.78rem] font-medium', style: { color: C.text }, children: s.title || 'Session' }),
              s.preview
                ? jsx('div', { className: 'truncate font-mono text-[0.66rem]', style: { color: C.faint }, children: s.preview })
                : null
            ]
          }),
          jsx(SourcePill, { label: row.sourceLabel }),
          running ? jsx(Spark, { seqs: row.spark || [] }) : null,
          jsx(AgoText, { ms: epochMs(s.last_active) }),
          s.session_key
            ? jsx(Tip, {
                label: pinned ? 'Unpin' : 'Pin to Watching',
                children: jsx(Button, {
                  size: 'icon-xs',
                  variant: 'ghost',
                  className: pinned ? '' : 'opacity-0 group-hover:opacity-100',
                  onClick: ev => {
                    ev.stopPropagation()
                    togglePin(s.session_key, { title: s.title, route: row.route })
                    haptic('selection')
                  },
                  children: jsx(Codicon, { name: pinned ? 'pinned' : 'pin' })
                })
              })
            : null,
          jsx(Tip, {
            label: 'Send a follow-up',
            children: jsx(Button, {
              size: 'icon-xs',
              variant: 'ghost',
              className: nudging ? '' : 'opacity-0 group-hover:opacity-100',
              onClick: ev => {
                ev.stopPropagation()
                setNudging(v => !v)
              },
              children: jsx(Codicon, { name: 'comment' })
            })
          }),
          running
            ? jsx(Tip, {
                label: 'Stop the running turn',
                children: jsx(Button, {
                  size: 'icon-xs',
                  variant: 'ghost',
                  className: 'opacity-0 group-hover:opacity-100',
                  onClick: stop,
                  children: jsx(Codicon, { name: 'debug-stop' })
                })
              })
            : null,
          busy ? spinIcon('sync') : jsx(Codicon, { name: 'arrow-right', className: 'text-muted-foreground/40 group-hover:text-muted-foreground' })
        ]
      }),
      nudging
        ? jsxs('div', {
            className: 'flex items-center gap-1.5 px-2 pb-2 pl-8',
            children: [
              jsx(Input, {
                value: nudgeText,
                placeholder: 'Reply without opening the session…',
                onChange: ev => setNudgeText(ev.target.value),
                onKeyDown: ev => {
                  ev.stopPropagation()
                  if (isSubmitEnter(ev)) nudge()
                },
                className: 'h-7 flex-1 text-[0.78rem]'
              }),
              jsx(Button, { size: 'xs', variant: 'secondary', disabled: !nudgeText.trim(), onClick: nudge, children: 'Send' })
            ]
          })
        : null,
      note ? jsx('div', { className: 'px-2 pb-1.5 pl-8 text-[0.68rem] text-muted-foreground', children: note }) : null
    ]
  })
}

const EVIDENCE_BADGE = {
  verified: { icon: 'shield', color: () => C.emerald, label: () => 'verified' },
  missing: { icon: 'question', color: () => C.amber, label: () => 'no evidence' },
  failed: { icon: 'flame', color: () => C.red, label: () => 'checks failed' },
  flagged: { icon: 'warning', color: () => C.red, label: ev => `guard x${ev.blocks || 1}` }
}

function EvidenceBadge({ ev }) {
  const m = EVIDENCE_BADGE[ev && ev.verdict]
  if (!m) return null
  const color = m.color(ev)
  const label = m.label(ev)
  return jsx(Tip, {
    label: `Evidence gate — ${ev.detail || label}`,
    children: jsxs('span', {
      className: 'inline-flex shrink-0 items-center gap-1 rounded border px-1 py-px text-[0.62rem] font-mono',
      style: { color, borderColor: `${color}44`, background: `${color}14` },
      children: [jsx(Codicon, { name: m.icon, className: 'text-[0.66rem]' }), label]
    })
  })
}

function evidenceFor(f, map) {
  const sid = resolveStoredId(f)
  if (!sid) return null
  const scoped = `${f.route ? routeKey(f.route) : 'local'}#${sid}`
  if (map[scoped] || map[sid]) return map[scoped] || map[sid]
  const suf = `#${sid}`
  const k = Object.keys(map).find(key => key.endsWith(suf))
  return k ? map[k] : null
}

function FinishedRow({ f }) {
  const [busy, setBusy] = useState(false)
  const evMap = useValue($evidence)
  const [nudging, setNudging] = useState(false)
  const [nudgeText, setNudgeText] = useState('')
  const [note, setNote] = useState('')
  const pinnedMap = useValue($pinned)
  const pinned = Boolean(f.storedId && pinnedMap[f.storedId])

  const review = async () => {
    setBusy(true)
    try {
      if (f.storedId) {
        await host.openSession(f.storedId, {
          ...(f.route ? { route: f.route, profile: f.route.targetProfile } : {}),
          awaitHydration: true,
          retryHydrationTimeoutOnce: true
        })
        try {
          ackStoredSessionId(f.storedId, f.profile || undefined)
        } catch {}
      }
      dismissFinished(f.key)
    } catch {} finally {
      setBusy(false)
    }
  }

  const nudge = async () => {
    const t = nudgeText.trim()
    if (!t || !f.runtimeId) return
    setNote('')
    try {
      await nudgeSession(f.route, f.runtimeId, t)
      setNudgeText('')
      setNudging(false)
      setNote('Sent — session resumed')
      haptic('submit')
      invalidate()
    } catch (err) {
      setNote(errMsg(err))
      haptic('cancel')
    }
  }

  return jsxs('div', {
    className: 'hday-row group rounded-md',
    children: [
      jsxs('div', {
        className: 'flex w-full items-center gap-2.5 px-2 py-1',
        children: [
          jsx(Codicon, {
            name: f.kind === 'error' ? 'error' : 'pass-filled',
            className: cn('shrink-0 text-[0.8rem]', f.kind === 'error' ? 'text-destructive' : 'text-emerald-500')
          }),
          jsxs('div', {
            className: 'min-w-0 flex-1',
            children: [
              jsx('div', { className: 'truncate text-[0.78rem] font-medium', style: { color: C.text }, children: f.title || 'Session' }),
              jsx('div', {
                className: 'truncate text-[0.68rem] text-muted-foreground',
                children: f.kind === 'error' ? 'Ended with an error' : 'Turn finished'
              })
            ]
          }),
          f.profile ? jsx(SourcePill, { label: f.profile }) : null,
          jsx(EvidenceBadge, { ev: evidenceFor(f, evMap) }),
          jsx(AgoText, { ms: f.at }),
          f.storedId
            ? jsx(Tip, {
                label: pinned ? 'Unpin' : 'Pin to Watching',
                children: jsx(Button, {
                  size: 'icon-xs',
                  variant: 'ghost',
                  className: pinned ? '' : 'opacity-0 group-hover:opacity-100',
                  onClick: () => {
                    togglePin(f.storedId, { title: f.title, route: f.route, profile: f.profile })
                    haptic('selection')
                  },
                  children: jsx(Codicon, { name: pinned ? 'pinned' : 'pin' })
                })
              })
            : null,
          f.runtimeId
            ? jsx(Tip, {
                label: 'Send a follow-up',
                children: jsx(Button, {
                  size: 'icon-xs',
                  variant: 'ghost',
                  className: nudging ? '' : 'opacity-0 group-hover:opacity-100',
                  onClick: () => setNudging(v => !v),
                  children: jsx(Codicon, { name: 'comment' })
                })
              })
            : null,
          jsx(Button, { size: 'xs', variant: 'outline', disabled: busy, onClick: review, children: busy ? spinIcon('sync') : 'Review' }),
          jsx(Tip, {
            label: 'Dismiss',
            children: jsx(Button, {
              size: 'icon-xs',
              variant: 'ghost',
              onClick: () => dismissFinished(f.key),
              children: jsx(Codicon, { name: 'close' })
            })
          })
        ]
      }),
      nudging
        ? jsxs('div', {
            className: 'flex items-center gap-1.5 px-2 pb-2 pl-8',
            children: [
              jsx(Input, {
                value: nudgeText,
                placeholder: 'Follow up — resumes this session…',
                onChange: ev => setNudgeText(ev.target.value),
                onKeyDown: ev => {
                  ev.stopPropagation()
                  if (isSubmitEnter(ev)) nudge()
                },
                className: 'h-7 flex-1 text-[0.78rem]'
              }),
              jsx(Button, { size: 'xs', variant: 'secondary', disabled: !nudgeText.trim(), onClick: nudge, children: 'Send' })
            ]
          })
        : null,
      note ? jsx('div', { className: 'px-2 pb-1.5 pl-8 text-[0.68rem] text-muted-foreground', children: note }) : null
    ]
  })
}

function CronRow({ entry }) {
  const j = entry.job
  const overdueMs = nextRunOverdueMs(j)
  const nextAt = Date.parse(j.next_run_at || '')
  const failed = j.last_status === 'error' || j.last_status === 'failed' || Boolean(j.last_error)
  return jsxs('div', {
    className: 'hday-row flex w-full items-center gap-2.5 rounded-md px-2 py-1',
    children: [
      jsx(Codicon, {
        name: j.enabled === false ? 'circle-slash' : 'history',
        className: cn('shrink-0 text-[0.8rem]', j.enabled === false ? 'text-muted-foreground/50' : 'text-muted-foreground')
      }),
      jsxs('div', {
        className: 'min-w-0 flex-1',
        children: [
          jsx('div', { className: 'truncate text-[0.78rem] font-medium text-(--ui-text-primary)', children: j.name || j.job_id || 'Job' }),
          jsx('div', {
            className: 'truncate text-[0.68rem] text-muted-foreground',
            children: j.prompt_preview || j.schedule || ''
          })
        ]
      }),
      failed
        ? jsx(Tip, { label: j.last_error || 'Last run failed', children: jsx(Codicon, { name: 'warning', className: 'text-amber-500' }) })
        : null,
      overdueMs != null
        ? jsx(Badge, { size: 'xs', variant: 'warn', children: 'overdue' })
        : j.enabled !== false && !Number.isNaN(nextAt)
          ? jsx('span', { className: 'shrink-0 text-[0.68rem] tabular-nums text-muted-foreground/70', children: relativeTime(nextAt) })
          : jsx(Badge, { size: 'xs', variant: 'muted', children: j.enabled === false ? 'paused' : '—' })
    ]
  })
}

// ---------------------------------------------------------------------------
// quick-task bar — type a task, Hermes starts it
// ---------------------------------------------------------------------------

function QuickTaskBar() {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')

  const submit = async () => {
    const t = text.trim()
    if (!t || busy) return
    setBusy(true)
    setNote('')
    try {
      const created = await startTask(t)
      setText('')
      setNote(`Started “${(created && created.info && created.info.title) || t.slice(0, 40)}”`)
      haptic('submit')
      invalidate()
    } catch (err) {
      setNote(`Couldn't start: ${errMsg(err)}`)
      haptic('cancel')
    } finally {
      setBusy(false)
    }
  }

  const prompts = useValue($prompts) || DEFAULT_PROMPTS
  const [adding, setAdding] = useState(false)
  const [draft, setDraft] = useState('')

  const savePrompts = list => {
    $prompts.set(list)
    try {
      storageRef && storageRef.set(PROMPTS_KEY, list)
    } catch {}
  }

  return jsxs('div', {
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2.5 rounded-lg px-4 py-2.5',
        style: { background: C.surface, border: `1px solid ${C.border}` },
        children: [
          jsx('span', {
            className: 'inline-flex size-7 shrink-0 items-center justify-center rounded',
            style: { color: C.mono, background: C.surfaceHover, border: `1px solid ${C.border}` },
            children: jsx(Codicon, { name: 'sparkle', size: 14 })
          }),
          jsx(Input, {
            value: text,
            placeholder: 'Start something — “review my PRs”, “summarize overnight logs”…',
            disabled: busy,
            onChange: ev => setText(ev.target.value),
            onKeyDown: ev => {
              if (isSubmitEnter(ev)) submit()
            },
            className: 'h-8 flex-1 border-none bg-transparent px-0 text-[0.9rem] shadow-none focus-visible:ring-0'
          }),
          note ? jsx('span', { className: 'shrink-0 truncate text-[0.68rem] text-muted-foreground', children: note }) : null,
          jsx(Button, {
            size: 'sm',
            variant: 'default',
            disabled: busy || !text.trim(),
            onClick: submit,
            children: busy ? spinIcon('sync') : 'Start'
          })
        ]
      }),
      jsxs('div', {
        className: 'mt-1.5 flex flex-wrap items-center gap-1.5 px-1',
        children: [
          prompts.map(p =>
            jsxs(
              'span',
              {
                className: 'hday-chip group/chip inline-flex items-center gap-1 rounded px-2.5 py-0.5 text-[0.68rem]',
                style: { border: `1px solid ${C.border}`, color: C.muted },
                children: [
                  jsx('button', {
                    type: 'button',
                    className: 'max-w-48 truncate hover:text-(--ui-text-primary)',
                    onClick: () => setText(p),
                    children: p
                  }),
                  jsx('button', {
                    type: 'button',
                    className: 'opacity-0 group-hover/chip:opacity-100',
                    onClick: () => savePrompts(prompts.filter(x => x !== p)),
                    children: jsx(Codicon, { name: 'close', size: 10 })
                  })
                ]
              },
              p
            )
          ),
          adding
            ? jsxs('span', {
                className: 'inline-flex items-center gap-1',
                children: [
                  jsx(Input, {
                    value: draft,
                    placeholder: 'New preset…',
                    onChange: ev => setDraft(ev.target.value),
                    onKeyDown: ev => {
                      if (isSubmitEnter(ev) && draft.trim()) {
                        savePrompts([...prompts, draft.trim()])
                        setDraft('')
                        setAdding(false)
                      } else if (ev.key === 'Escape') setAdding(false)
                    },
                    className: 'h-6 w-40 text-[0.68rem]'
                  })
                ]
              })
            : jsxs('button', {
                type: 'button',
                className: 'inline-flex items-center gap-1 rounded px-2 py-0.5 text-[0.66rem]',
                style: { color: C.faint },
                onClick: () => setAdding(true),
                children: [jsx(Codicon, { name: 'add', size: 10 }), 'save a prompt']
              })
        ]
      })
    ]
  })
}

/** One approval flood per session collapses to a single resolve-all bar. */
function ApproveAllBar({ sessionId, title, items }) {
  const [busy, setBusy] = useState(false)
  const sample = items[0]
  const approveAll = async () => {
    setBusy(true)
    try {
      await respondAllForSession(sample, 'once')
      haptic('submit')
      invalidate()
    } catch {} finally {
      setBusy(false)
    }
  }
  return jsxs('div', {
    className: 'flex items-center gap-2 rounded-md px-3 py-1.5 text-[0.72rem]',
    style: { background: C.surface, border: `1px solid rgba(245,158,11,.4)`, color: C.muted },
    children: [
      jsx(Codicon, { name: 'check-all', className: 'text-amber-500' }),
      jsxs('span', {
        className: 'min-w-0 flex-1 truncate text-(--ui-text-secondary)',
        children: [`${items.length} approvals waiting in `, jsx('span', { className: 'font-medium', children: title })]
      }),
      jsx(Button, { size: 'xs', variant: 'secondary', disabled: busy, onClick: approveAll, children: busy ? spinIcon('sync') : 'Allow all' })
    ]
  })
}

// ---------------------------------------------------------------------------
// page
// ---------------------------------------------------------------------------

const greeting = () => {
  const h = new Date().getHours()
  if (h < 5) return 'Up late?'
  if (h < 12) return 'Good morning'
  if (h < 17) return 'Good afternoon'
  return 'Good evening'
}



// ---------------------------------------------------------------------------
// day-arc hero + inbox-zero confetti
// ---------------------------------------------------------------------------

/** Sun position along the day arc — 6 am rises left, 6 pm sets right. */
function DayArc() {
  const now = new Date()
  const mins = now.getHours() * 60 + now.getMinutes()
  const t = Math.min(1, Math.max(0, (mins - 360) / 720))
  const x = 12 + 176 * t
  const y = 46 - Math.sin(t * Math.PI) * 34
  return jsxs('svg', {
    width: 200,
    height: 52,
    viewBox: '0 0 200 52',
    className: 'shrink-0 opacity-90',
    'aria-hidden': true,
    children: [
      jsx('path', { d: 'M12 46 Q100 -16 188 46', fill: 'none', stroke: 'var(--ui-stroke-secondary)', strokeWidth: 1.5 }),
      jsx('line', { x1: 4, y1: 46, x2: 196, y2: 46, stroke: 'var(--ui-stroke-secondary)', strokeWidth: 1 }),
      jsx('circle', { cx: x, cy: y, r: 10, fill: '#f59e0b', fillOpacity: 0.15 }),
      jsx('circle', { cx: x, cy: y, r: 4.5, fill: '#f59e0b' }),
      jsx('text', { x: 12, y: 12, fontSize: 8, fill: 'var(--ui-text-tertiary)', children: '6a' }),
      jsx('text', { x: 96, y: 4, fontSize: 8, fill: 'var(--ui-text-tertiary)', children: '12p' }),
      jsx('text', { x: 182, y: 12, fontSize: 8, fill: 'var(--ui-text-tertiary)', children: '6p' })
    ]
  })
}

const CONFETTI_COLORS = ['#f59e0b', '#10b981', '#3b82f6', '#a855f7', '#ec4899']

function Confetti({ onDone }) {
  const pieces = useMemo(
    () =>
      Array.from({ length: 64 }, (_, i) => ({
        left: 2 + Math.random() * 96,
        delay: Math.random() * 0.5,
        dur: 1.5 + Math.random() * 0.9,
        size: 5 + Math.random() * 6,
        color: CONFETTI_COLORS[i % CONFETTI_COLORS.length],
        round: Math.random() > 0.5
      })),
    []
  )
  useEffect(() => {
    const t = setTimeout(onDone, 2600)
    return () => clearTimeout(t)
  }, [])
  return jsxs('div', {
    className: 'pointer-events-none fixed inset-0 z-50 overflow-hidden',
    children: [
      jsx('style', {
        children:
          '@keyframes hday-confetti{0%{transform:translateY(-6vh) rotate(0);opacity:1}85%{opacity:1}100%{transform:translateY(105vh) rotate(680deg);opacity:0}}'
      }),
      pieces.map((p, i) =>
        jsx('div', {
          style: {
            position: 'absolute',
            top: '-6vh',
            left: `${p.left}%`,
            width: p.size,
            height: p.size * (p.round ? 1 : 0.6),
            background: p.color,
            borderRadius: p.round ? '50%' : '1.5px',
            animation: `hday-confetti ${p.dur}s cubic-bezier(.2,.7,.4,1) ${p.delay}s forwards`
          }
        }, i)
      )
    ]
  })
}

// ---------------------------------------------------------------------------
// watching — pinned sessions rail
// ---------------------------------------------------------------------------

function WatchingRow({ entry }) {
  const [busy, setBusy] = useState(false)
  const open = async () => {
    setBusy(true)
    try {
      await host.openSession(entry.storedId, {
        ...(entry.route ? { route: entry.route, profile: entry.route.targetProfile } : {}),
        awaitHydration: true,
        retryHydrationTimeoutOnce: true
      })
    } catch {} finally {
      setBusy(false)
    }
  }
  return jsxs('div', {
    className: 'hday-row group flex w-full cursor-pointer items-center gap-2 rounded-md px-2 py-1 text-[0.72rem]',
    onClick: open,
    children: [
      jsx(SessionStatusDot, { storedSessionId: entry.storedId }),
      jsx('span', { className: 'min-w-0 flex-1 truncate text-(--ui-text-secondary)', children: entry.title }),
      busy ? spinIcon('sync') : null,
      jsx(Button, {
        size: 'icon-xs',
        variant: 'ghost',
        className: 'opacity-0 group-hover:opacity-100',
        onClick: ev => {
          ev.stopPropagation()
          togglePin(entry.storedId, {})
        },
        children: jsx(Codicon, { name: 'pinned' })
      })
    ]
  })
}

// ---------------------------------------------------------------------------
// workbench — unified feed row + live inspector
// ---------------------------------------------------------------------------

const FEED_ICON = {
  need: (e) => KIND_STYLE[e.item.kind] || KIND_STYLE.other,
  waiting: () => ({ icon: 'watch', color: C.amber }),
  flight: () => ({ icon: 'rocket', color: C.emerald }),
  finished: (e) => ({ icon: e.f.kind === 'error' ? 'error' : 'pass-filled', color: e.f.kind === 'error' ? C.red : C.emerald })
}

function feedTitle(e) {
  if (e.type === 'need') return e.item.title || 'Session'
  if (e.type === 'finished') return e.f.title || 'Session'
  return e.row.session.title || 'Session'
}

function feedSub(e) {
  if (e.type === 'need') {
    const p = e.item.params || {}
    if (e.item.kind === 'approval') return p.command || p.description || e.item.method
    if (e.item.kind === 'clarify') {
      const qs = (p.questions || []).map(x => x.question || x).join(' · ')
      return qs || e.item.method
    }
    return p.prompt || p.message || e.item.method
  }
  if (e.type === 'finished') return e.f.kind === 'error' ? 'Ended with an error' : 'Turn finished'
  return e.row.session.preview || ''
}

function feedAt(e) {
  if (e.type === 'need') return e.item.firstSeenAt
  if (e.type === 'finished') return e.f.at
  return (epochMs(e.row.session.last_active) || Date.now())
}

function feedEvidence(e, evMap) {
  if (e.type === 'finished') return evidenceFor(e.f, evMap)
  const storedId = e.type === 'need' ? e.item.storedId : e.row.session.session_key
  if (!storedId) return null
  const sk = `${e.type === 'need' ? e.item.sourceKey : e.row.sourceKey}#${storedId}`
  return evMap[sk] || evMap[storedId] || null
}

function FeedRow({ e, selected, idx, evMap, onSelect }) {
  const st = FEED_ICON[e.type](e)
  const ev = feedEvidence(e, evMap)
  return jsxs('div', {
    className: cn('hday-feedrow hday-row group flex w-full items-center gap-2 px-2.5 py-1.5', selected && 'hday-sel'),
    'data-hday-idx': idx,
    onClick: onSelect,
    children: [
      jsx(Codicon, { name: st.icon, className: 'shrink-0 text-[0.78rem]', style: { color: st.color } }),
      jsxs('div', {
        className: 'min-w-0 flex-1',
        children: [
          jsx('div', { className: 'truncate text-[0.78rem] font-medium', style: { color: C.text }, children: feedTitle(e) }),
          jsx('div', {
            className: cn('truncate text-[0.66rem]', e.type === 'need' && e.item.kind === 'approval' && 'font-mono'),
            style: { color: e.type === 'need' && e.item.kind === 'approval' ? C.mono : C.faint },
            children: feedSub(e)
          })
        ]
      }),
      e.type === 'need'
        ? jsxs('span', {
            className: 'inline-flex shrink-0 items-center rounded-[3px] px-1 py-px text-[0.6rem] font-semibold',
            style: { color: st.color, background: `${st.color}1f` },
            children: (KIND_STYLE[e.item.kind] || KIND_STYLE.other).label
          })
        : null,
      jsx(EvidenceBadge, { ev }),
      jsx(AgoText, { ms: feedAt(e) })
    ]
  })
}

// session event replay for the inspector's Diff + Trace tabs
const INSP_QK = ['hday-insp']

async function fetchEventsFor(e) {
  const runtimeId = e.type === 'finished' ? e.f.runtimeId
    : e.type === 'need' ? e.item.sessionId
    : e.row.session.id
  const route = e.type === 'finished' ? e.f.route
    : e.type === 'need' ? e.item.route
    : e.row.route
  if (!runtimeId) return { events: [], unavailable: true }
  try {
    const snap = await rpc(route, 'session.events.since', { session_id: runtimeId, last_seen: 0 })
    return { events: snap && Array.isArray(snap.events) ? snap.events : [], truncated: snap && snap.truncated }
  } catch (err) {
    return { events: [], error: errMsg(err) }
  }
}

const evType = e => String(e.type || e.event || '')
const evSeq = e => (typeof e.seq === 'number' ? e.seq : -1)

// turn boundaries from the replay stream: message.start opens, message.complete seals
function turnsFromEvents(events) {
  const turns = []
  let open = null
  const labelFor = evs => {
    let blocked = false, edited = null, check = null, tools = 0
    for (const e of evs) {
      const t = evType(e)
      if (t === 'tool.start' || t === 'tool.complete') {
        tools++
        const res = String(e.result_text || e.summary || '')
        if (/honesty guard BLOCKED/i.test(res)) blocked = true
        if (t === 'tool.complete' && e.inline_diff) {
          edited = (e.args && (e.args.path || e.args.file_path)) || edited
          if (!edited) edited = e.name || 'file'
        }
        if (t === 'tool.complete' && (e.name === 'terminal' || e.name === 'bash')) {
          const c = (e.args && (e.args.command || e.args.code)) || ''
          if (/\b(pytest|jest|vitest|go test|cargo test|npm (run )?test|unittest|ruff|mypy|tsc|eslint)\b/.test(c))
            check = e
        }
      }
    }
    if (blocked) return '⚠ guard'
    if (edited) return `edit ${String(edited).split('/').pop()}`
    if (check) return 'tests'
    if (tools) return `${tools} tool${tools > 1 ? 's' : ''}`
    return 'reply'
  }
  for (const e of events) {
    const t = evType(e)
    if (t === 'message.start') open = { startSeq: evSeq(e), evs: [] }
    if (open) open.evs.push(e)
    if (t === 'message.complete' && open) {
      turns.push({ n: turns.length + 1, startSeq: open.startSeq, endSeq: evSeq(e), ts: e.ts || 0,
                   status: e.status || 'complete', label: labelFor(open.evs) })
      open = null
    }
  }
  if (open) turns.push({ n: turns.length + 1, startSeq: open.startSeq, endSeq: MAX_SEQ,
                         ts: Date.now() / 1000, status: 'live', label: `${labelFor(open.evs)}…` })
  return turns
}

function TurnScrubber({ turns, scrub, onScrub, onRevert, reverting, revertNote, snaps }) {
  if (!turns.length) return null
  // replay frames carry seq but no ts — snaps are numbered in turn order, so
  // turn N maps to the latest snap numbered ≤ N (approximate across multi-snap turns)
  const snapForTurn = t => {
    let best = null
    for (const s of snaps || []) if ((s.n || 0) <= t.n) best = s
    return best
  }
  return jsxs('div', {
    className: 'flex shrink-0 items-center gap-1 overflow-x-auto px-3 pb-2 pt-1',
    children: [
      jsxs('span', { className: 'mr-1 shrink-0 text-[0.6rem] uppercase tracking-wider', style: { color: C.faint }, children: ['turns'] }),
      turns.map(t => {
        const active = scrub === t.endSeq || (scrub === null && t.endSeq === MAX_SEQ)
        const bad = t.label.includes('⚠')
        return jsx('button', {
          className: 'hday-chip shrink-0 rounded border px-1.5 py-0.5 font-mono text-[0.6rem]',
          style: {
            borderColor: active ? C.mono : C.border,
            color: bad ? C.red : active ? C.text : C.muted,
            background: active ? '#1a1d26' : 'transparent'
          },
          onClick: () => { onScrub(t.endSeq === MAX_SEQ ? null : t.endSeq); haptic('selection') },
          children: `T${t.n} ${t.label}`
        }, t.n)
      }),
      scrub !== null
        ? jsx('button', {
            className: 'hday-chip shrink-0 rounded border px-1.5 py-0.5 font-mono text-[0.6rem]',
            style: { borderColor: C.border, color: C.muted },
            onClick: () => { onScrub(null); haptic('selection') },
            children: '⟶ latest'
          })
        : null,
      (() => {
        if (scrub === null) return null
        const t = turns.find(x => x.endSeq === scrub)
        const snap = t && snapForTurn(t)
        return snap
          ? jsx(Button, {
              size: 'xs', variant: 'secondary', className: 'ml-auto h-5 shrink-0 text-[0.62rem]',
              disabled: Boolean(reverting),
              onClick: () => onRevert(snap),
              children: reverting ? spinIcon('sync') : `⚡ revert to T${snap.n}`
            })
          : null
      })(),
      revertNote ? jsx('span', { className: 'ml-auto shrink-0 text-[0.62rem]', style: { color: revertNote.startsWith('!') ? C.red : C.emerald }, children: revertNote.replace(/^!/, '') }) : null
    ]
  })
}

// Context X-Ray — rough token split of the session's context, estimated from
// replay frames (chars/4). sys = prompt base + message bodies, files =
// read/write/diff tool traffic, dumps = stdout/stderr tool results.
const XRAY_BASE = 6000
const XRAY_FILE_TOOLS = new Set([
  'write_file', 'edit_file', 'multi_edit', 'patch', 'apply_patch',
  'read_file', 'view_file', 'str_replace_editor'
])

function xrayFromEvents(events) {
  let sys = XRAY_BASE * 4, files = 0, dumps = 0
  for (const ev of events || []) {
    const t = evType(ev)
    if (t === 'message.start' || t === 'message.complete') {
      sys += String(ev.text || ev.final || '').length
    } else if (t === 'tool.start' || t === 'tool.complete') {
      const argChars = String(ev.args_text || (ev.args ? JSON.stringify(ev.args) : '')).length
      const resChars = String(ev.result_text || ev.summary || (typeof ev.result === 'string' ? ev.result : '')).length
      if (XRAY_FILE_TOOLS.has(ev.name) || ev.inline_diff)
        files += argChars + resChars + String(ev.inline_diff || '').length
      else dumps += argChars + resChars
    }
  }
  return { sys: Math.round(sys / 4), files: Math.round(files / 4), dumps: Math.round(dumps / 4) }
}

const fmtTok = n => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`)

function XrayBar({ events, ev, storedId, route }) {
  const seg = xrayFromEvents(events)
  const total = seg.sys + seg.files + seg.dumps || 1
  const vac = (ev && ev.vacuum) || null
  const savedTok = vac && vac.saved_chars ? Math.round(vac.saved_chars / 4) : 0
  const dumpBase = seg.dumps + savedTok
  const recoveredPct = savedTok && dumpBase ? Math.min(99, Math.round((savedTok / dumpBase) * 100)) : 0
  const [arming, setArming] = useState(false)
  const arm = async () => {
    if (!storedId) return
    setArming(true)
    try {
      await rpc(route, 'command.dispatch', { name: 'day-vacuum', arg: storedId })
      invalidate()
    } catch {} finally {
      setArming(false)
    }
  }
  const segRow = (label, v, color) =>
    jsxs('span', {
      className: 'flex items-center gap-1 whitespace-nowrap',
      children: [
        jsx('span', { className: 'size-1.5 rounded-sm', style: { background: color } }),
        jsx('span', { style: { color: C.faint }, children: label }),
        jsx('span', { className: 'font-mono', style: { color: C.muted }, children: `~${fmtTok(v)}` })
      ]
    })
  return jsxs('div', {
    className: 'shrink-0 px-3 pb-1.5 pt-1',
    children: [
      jsxs('div', {
        className: 'flex h-1 overflow-hidden rounded-full',
        style: { background: C.border },
        children: [
          jsx('span', { style: { width: `${Math.max(3, (seg.sys / total) * 100)}%`, background: '#64748b' } }),
          jsx('span', { style: { width: `${Math.max(0, (seg.files / total) * 100)}%`, background: C.mono } }),
          jsx('span', { style: { width: `${Math.max(0, (seg.dumps / total) * 100)}%`, background: C.amber } })
        ]
      }),
      jsxs('div', {
        className: 'mt-1 flex items-center gap-2.5 text-[0.58rem]',
        children: [
          jsx('span', { className: 'uppercase tracking-wider', style: { color: C.faint }, children: 'ctx' }),
          segRow('sys', seg.sys, '#64748b'),
          segRow('files', seg.files, C.mono),
          segRow('dumps', seg.dumps, C.amber),
          recoveredPct
            ? jsx('span', { className: 'font-mono', style: { color: C.emerald }, children: `▼ ${recoveredPct}% recovered` })
            : null,
          jsx('div', { className: 'flex-1' }),
          vac && vac.trimmed
            ? jsx('span', { className: 'font-mono', style: { color: C.emerald }, children: `${vac.trimmed} trimmed` })
            : null,
          jsx('button', {
            className: 'hday-chip rounded border px-1.5 py-px font-mono text-[0.6rem]',
            style: {
              borderColor: vac && vac.armed ? C.emerald : C.border,
              color: vac && vac.armed ? C.emerald : C.muted
            },
            disabled: !storedId || arming || (vac && vac.armed),
            onClick: arm,
            children: arming ? 'arming…' : vac && vac.armed ? 'vacuum armed' : '⌀ vacuum ctx'
          })
        ]
      })
    ]
  })
}

// Negative instincts — per-repo ledger of blocked/failed approaches, injected
// into the agent's first-turn context by the backend half.
function InstinctsDrawer({ open, onClose, route }) {
  const instQ = useQuery({
    queryKey: ['hday-instincts'],
    queryFn: async () => {
      const disp = await rpc(route, 'command.dispatch', { name: 'day-instincts', arg: '' })
      const out = disp && (disp.output || disp.text || '')
      return out && out.trim().startsWith('{') ? JSON.parse(out) : { ok: false }
    },
    enabled: open,
    refetchInterval: 10000,
    staleTime: 4000
  })
  const act = async (name, arg) => {
    try {
      await rpc(route, 'command.dispatch', { name, arg })
      queryClient.invalidateQueries({ queryKey: ['hday-instincts'] })
    } catch {}
  }
  if (!open) return null
  const repos = (instQ.data && instQ.data.repos) || {}
  const roots = Object.keys(repos).sort()
  const totalEnabled = roots.reduce((n, r) => n + repos[r].filter(i => i.enabled !== false).length, 0)
  return jsxs('div', {
    className: 'absolute right-0 top-0 z-30 flex h-full w-[380px] flex-col',
    style: { background: C.canvas, borderLeft: `1px solid ${C.border}` },
    children: [
      jsxs('div', {
        className: 'flex shrink-0 items-center gap-2 px-3 py-2.5',
        style: { borderBottom: `1px solid ${C.border}` },
        children: [
          jsx(Codicon, { name: 'lightbulb', style: { color: C.amber } }),
          jsx('span', { className: 'flex-1 truncate text-[0.78rem] font-semibold', style: { color: C.text }, children: 'Repo instincts' }),
          totalEnabled ? jsx(Badge, { variant: 'warn', children: `${totalEnabled} active` }) : null,
          jsx(Button, { size: 'icon-sm', variant: 'ghost', onClick: onClose, children: jsx(Codicon, { name: 'close' }) })
        ]
      }),
      jsx('div', { className: 'px-3 py-2 text-[0.64rem] leading-4', style: { color: C.faint, borderBottom: `1px solid ${C.border}` },
        children: 'Approaches already disproven in each repo — injected into new sessions so Hermes never repeats them. Toggling takes effect on the next session.' }),
      jsx(ScrollArea, {
        className: 'min-h-0 flex-1',
        children: jsxs('div', {
          className: 'flex flex-col gap-3 p-3',
          children: [
            instQ.isLoading ? jsxs('div', { className: 'flex items-center gap-2 py-4', style: { color: C.faint }, children: [jsx(Loader, {}), jsx('span', { className: 'text-[0.72rem]', children: 'Loading ledgers…' })] }) : null,
            !instQ.isLoading && !roots.length
              ? jsx('div', { className: 'py-6 text-center text-[0.72rem]', style: { color: C.faint }, children: 'No instincts yet — blocked traps and failed checks promote themselves here automatically.' })
              : null,
            roots.map(root => jsxs('div', {
              className: 'flex flex-col gap-1.5',
              children: [
                jsx('div', { className: 'truncate font-mono text-[0.6rem]', style: { color: C.faint }, children: root }),
                ...repos[root].map(i => jsxs('div', {
                  className: 'rounded border px-2 py-1.5',
                  style: { borderColor: C.border, background: C.surface, opacity: i.enabled === false ? 0.45 : 1 },
                  children: [
                    jsxs('div', {
                      className: 'flex items-start gap-2',
                      children: [
                        jsx('button', {
                          className: 'mt-px shrink-0 rounded border',
                          style: { width: 12, height: 12, borderColor: i.enabled === false ? C.border : C.amber, background: i.enabled === false ? 'transparent' : C.amber },
                          onClick: () => act('day-instinct-set', `${root} ${i.id} ${i.enabled === false ? 1 : 0}`)
                        }),
                        jsx('div', {
                          className: 'min-w-0 flex-1',
                          children: [
                            jsx('div', { className: 'break-all font-mono text-[0.66rem]', style: { color: C.text }, children: i.trigger_pattern }),
                            jsx('div', { className: 'break-all font-mono text-[0.6rem]', style: { color: C.mono }, children: i.disproven_approach }),
                            jsx('div', { className: 'mt-0.5 text-[0.62rem]', style: { color: C.muted }, children: i.reason })
                          ]
                        }),
                        i.hits > 1 ? jsx('span', { className: 'shrink-0 rounded border px-1 font-mono text-[0.58rem]', style: { borderColor: C.border, color: C.amber }, children: `x${i.hits}` }) : null,
                        jsx(Button, {
                          size: 'icon-xs', variant: 'ghost', className: 'shrink-0',
                          onClick: () => act('day-instinct-del', `${root} ${i.id}`),
                          children: jsx(Codicon, { name: 'trash', className: 'text-[0.6rem]', style: { color: C.faint } })
                        })
                      ]
                    })
                  ]
                }, i.id))
              ]
            }, root))
          ]
        })
      })
    ]
  })
}

function ImpactView({ data, loading }) {
  if (loading) return jsxs('div', { className: 'flex items-center gap-2 px-3 py-6', style: { color: C.faint }, children: [jsx(Loader, {}), jsx('span', { className: 'text-[0.72rem]', children: 'Scanning blast radius…' })] })
  const roots = (data && data.roots) || []
  if (!roots.length) return jsx('div', { className: 'px-3 py-6 text-[0.72rem]', style: { color: C.faint }, children: 'No repo-backed file changes recorded for this session.' })
  return jsx('div', {
    className: 'flex flex-col gap-3 p-3',
    children: roots.map((r, i) => jsxs('div', {
      className: 'flex flex-col gap-2',
      children: [
        jsx('div', { className: 'truncate font-mono text-[0.62rem]', style: { color: C.faint }, children: r.root }),
        r.touched.length
          ? jsxs('div', {
              children: [
                jsx('div', { className: 'mb-1 text-[0.62rem] font-semibold uppercase tracking-wider', style: { color: C.faint }, children: 'Touched symbols' }),
                jsx('div', {
                  className: 'flex flex-col gap-1',
                  children: r.touched.map((t, j) => jsxs('div', {
                    className: 'flex flex-col gap-0.5',
                    children: [
                      jsx('div', { className: 'truncate font-mono text-[0.66rem]', style: { color: C.mono }, children: t.path }),
                      t.symbols.length
                        ? jsx('div', {
                            className: 'flex flex-wrap gap-1',
                            children: t.symbols.map((s, k) => jsx('span', {
                              className: 'rounded border px-1 py-px font-mono text-[0.6rem]',
                              style: { borderColor: C.border, color: C.muted }, children: s
                            }, k))
                          })
                        : jsx('div', { className: 'text-[0.62rem]', style: { color: C.faint }, children: '(no defs parsed)' })
                    ]
                  }, j))
                })
              ]
            })
          : jsx('div', { className: 'text-[0.68rem]', style: { color: C.faint }, children: 'No files recorded as touched.' }),
        jsxs('div', {
          children: [
            jsx('div', { className: 'mb-1 text-[0.62rem] font-semibold uppercase tracking-wider', style: { color: C.faint }, children: `Downstream dependents (${r.dependents.length})` }),
            r.dependents.length
              ? jsx('div', {
                  className: 'flex flex-col gap-0.5',
                  children: r.dependents.map((d, j) => jsxs('div', {
                    className: 'flex items-center gap-2 text-[0.68rem]',
                    children: [
                      jsx(Codicon, {
                        name: d.is_test ? 'beaker' : d.covered ? 'pass-filled' : 'warning',
                        className: 'shrink-0 text-[0.66rem]',
                        style: { color: d.is_test ? C.muted : d.covered ? C.emerald : C.amber }
                      }),
                      jsx('span', { className: 'min-w-0 flex-1 truncate font-mono', style: { color: C.text }, children: d.path }),
                      d.calls && d.calls.length
                        ? jsx('span', { className: 'truncate text-[0.6rem]', style: { color: C.faint }, children: `uses ${d.calls.slice(0, 3).join(', ')}` })
                        : null,
                      !d.is_test && !d.covered
                        ? jsx('span', { className: 'shrink-0 rounded px-1 py-px text-[0.58rem] font-semibold', style: { color: C.red, background: '#ef44441c' }, children: 'NO TEST COVER' })
                        : null
                    ]
                  }, j))
                })
              : jsx('div', { className: 'text-[0.68rem]', style: { color: C.faint }, children: 'Nothing imports or references the touched files.' })
          ]
        })
      ]
    }, i))
  })
}

function TrapCard({ b }) {
  return jsxs('div', {
    className: 'flex flex-col gap-1.5 rounded border px-2 py-2',
    style: { borderColor: C.border, background: C.surface },
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx(Codicon, { name: 'shield', className: 'shrink-0 text-[0.7rem]', style: { color: C.red } }),
          jsx('span', { className: 'rounded px-1 py-px text-[0.6rem] font-semibold', style: { color: C.amber, background: '#f59e0b1c' }, children: b.rule || 'Honesty Guard' }),
          jsx('span', { className: 'ml-auto shrink-0 text-[0.6rem]', style: { color: C.faint }, children: b.ts ? relativeTime(b.ts * 1000) : '' })
        ]
      }),
      b.call
        ? jsx('pre', {
            className: 'overflow-x-auto whitespace-pre-wrap break-all rounded px-2 py-1 font-mono text-[0.64rem]',
            style: { background: '#0b0c10', color: C.mono, border: `1px solid ${C.border}` },
            children: b.call
          })
        : null,
      jsx('div', { className: 'text-[0.66rem]', style: { color: C.muted }, children: `${b.tool} — ${b.reason}` }),
      b.intervention
        ? jsxs('details', {
            className: 'group',
            children: [
              jsxs('summary', {
                className: 'flex cursor-pointer list-none items-center gap-1.5 text-[0.64rem]',
                style: { color: C.faint },
                children: [jsx(Codicon, { name: 'chevron-right', className: 'text-[0.62rem] transition-transform group-open:rotate-90' }), 'Intervention prompt sent back to the agent']
              }),
              jsx('pre', {
                className: 'mt-1 whitespace-pre-wrap break-all rounded border px-2 py-1.5 font-mono text-[0.62rem]',
                style: { borderColor: C.border, color: C.muted, background: '#0b0c10' },
                children: b.intervention
              })
            ]
          })
        : null
    ]
  })
}

const jsonShort = v => {
  try {
    const s = typeof v === 'string' ? v : JSON.stringify(v)
    return s && s !== '{}' && s !== 'null' ? s.slice(0, 400) : ''
  } catch {
    return ''
  }
}

function TraceView({ events }) {
  const rows = []
  const starts = new Map()
  for (const e of events) {
    const t = evType(e)
    if (t === 'tool.start') starts.set(e.tool_id, e)
    else if (t === 'tool.complete') {
      const st = starts.get(e.tool_id)
      rows.push({
        key: e.seq || rows.length, name: e.name || 'tool', ok: !(e.error_type || e.error_message),
        at: e.ts || 0, dur: e.duration_s, args: (st && st.args_text) || e.args_text || jsonShort(e.args),
        out: e.summary || e.result_text || jsonShort(e.result)
      })
    } else if (t === 'message.complete') {
      rows.push({ key: e.seq || rows.length, name: 'answer', ok: true, at: e.ts || 0, args: '', out: String(e.text || e.final || '').slice(0, 400) })
    } else if (t === 'status.update' && e.kind === 'process') {
      rows.push({ key: e.seq || rows.length, name: 'status', ok: true, at: e.ts || 0, args: '', out: e.text || '' })
    }
  }
  if (!rows.length) return jsx('div', { className: 'px-3 py-6 text-[0.72rem]', style: { color: C.faint }, children: 'No tool calls in this session’s replay window.' })
  return jsx('div', {
    className: 'flex flex-col px-2 py-1',
    children: rows.map(r => jsxs('details', {
      className: 'group border-b py-1.5',
      style: { borderColor: C.border },
      children: [
        jsxs('summary', {
          className: 'flex cursor-pointer list-none items-center gap-2 text-[0.72rem]',
          children: [
            jsx(Codicon, { name: 'chevron-right', className: 'text-[0.7rem] transition-transform group-open:rotate-90', style: { color: C.faint } }),
            jsx(Codicon, { name: r.ok ? 'check' : 'error', className: 'text-[0.7rem]', style: { color: r.ok ? C.emerald : C.red } }),
            jsx('span', { className: 'font-mono', style: { color: C.mono }, children: r.name }),
            r.dur != null ? jsx('span', { className: 'text-[0.62rem]', style: { color: C.faint }, children: `${r.dur.toFixed ? r.dur.toFixed(1) : r.dur}s` }) : null
          ]
        }),
        jsxs('div', {
          className: 'mt-1.5 flex flex-col gap-1 pl-6',
          children: [
            r.args ? jsx('pre', { className: 'overflow-x-auto whitespace-pre-wrap break-all rounded border px-2 py-1.5 font-mono text-[0.66rem]', style: { background: '#0b0c10', borderColor: C.border, color: C.mono }, children: r.args }) : null,
            r.out ? jsx('pre', { className: 'max-h-48 overflow-y-auto whitespace-pre-wrap break-all rounded border px-2 py-1.5 font-mono text-[0.66rem]', style: { background: '#0b0c10', borderColor: C.border, color: C.muted }, children: String(r.out).slice(0, 3000) }) : null
          ]
        })
      ]
    }, r.key))
  })
}

function DiffView({ events }) {
  const diffs = []
  for (const e of events) {
    if (evType(e) === 'tool.complete' && e.inline_diff) {
      diffs.push({ key: e.seq || diffs.length, name: (e.args && (e.args.path || e.args.file_path)) || e.name || 'file', diff: e.inline_diff })
    }
  }
  if (!diffs.length) return jsx('div', { className: 'px-3 py-6 text-[0.72rem]', style: { color: C.faint }, children: 'No file diffs in this session’s replay window.' })
  return jsx('div', {
    className: 'flex flex-col gap-2 p-2',
    children: diffs.map(d => jsxs('div', {
      className: 'overflow-hidden rounded border',
      style: { borderColor: C.border },
      children: [
        jsx('div', { className: 'border-b px-2 py-1 font-mono text-[0.66rem]', style: { borderColor: C.border, color: C.muted, background: C.surface }, children: d.name }),
        jsx('pre', {
          className: 'max-h-72 overflow-auto p-1 font-mono text-[0.66rem] leading-4',
          style: { background: '#0b0c10', color: C.muted },
          children: String(d.diff).split('\n').map((line, i) => jsx('div', {
            className: line.startsWith('+') ? 'hday-diff-add' : line.startsWith('-') ? 'hday-diff-del' : line.startsWith('@@') ? 'hday-diff-hunk' : '',
            children: line || ' '
          }, i))
        })
      ]
    }, d.key))
  })
}

function EvidenceView({ ev }) {
  if (!ev) return jsx('div', { className: 'px-3 py-6 text-[0.72rem]', style: { color: C.faint }, children: 'No evidence-gate record for this session.' })
  return jsxs('div', {
    className: 'flex flex-col gap-3 p-3',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [jsx(EvidenceBadge, { ev }), jsx('span', { className: 'text-[0.72rem]', style: { color: C.muted }, children: ev.detail || '' })]
      }),
      ev.run_list && ev.run_list.length
        ? jsxs('div', {
            children: [
              jsx('div', { className: 'mb-1 text-[0.62rem] font-semibold uppercase tracking-wider', style: { color: C.faint }, children: 'Check runs' }),
              jsx('div', {
                className: 'flex flex-col gap-0.5',
                children: ev.run_list.map((r, i) => jsxs('div', {
                  className: 'flex items-center gap-2 font-mono text-[0.66rem]',
                  children: [
                    jsx('span', { style: { color: r.ok ? C.emerald : C.red }, children: `exit ${r.exit ?? '?'}` }),
                    jsx('span', { className: 'truncate', style: { color: C.mono }, children: r.cmd }),
                    jsx('span', { className: 'ml-auto shrink-0', style: { color: C.faint }, children: r.ts ? relativeTime(r.ts * 1000) : '' })
                  ]
                }, i))
              })
            ]
          })
        : null,
      ev.block_list && ev.block_list.length
        ? jsxs('div', {
            children: [
              jsx('div', { className: 'mb-1 text-[0.62rem] font-semibold uppercase tracking-wider', style: { color: C.red }, children: `Trap ledger (${ev.block_list.length})` }),
              jsx('div', {
                className: 'flex flex-col gap-1.5',
                children: ev.block_list.map((b, i) => jsx(TrapCard, { b }, i))
              })
            ]
          })
        : null,
      ev.snaps && ev.snaps.length
        ? jsxs('div', {
            children: [
              jsx('div', { className: 'mb-1 text-[0.62rem] font-semibold uppercase tracking-wider', style: { color: C.faint }, children: `Turn snapshots (${ev.snaps.length})` }),
              jsx('div', {
                className: 'flex flex-col gap-0.5',
                children: ev.snaps.map((s, i) => jsxs('div', {
                  className: 'flex items-center gap-2 font-mono text-[0.64rem]',
                  children: [
                    jsx('span', { style: { color: C.mono }, children: `#${s.n}` }),
                    jsx('span', { className: 'truncate', style: { color: C.muted }, children: s.label }),
                    jsx('span', { className: 'ml-auto shrink-0', style: { color: C.faint }, children: (s.sha || '').slice(0, 8) })
                  ]
                }, i))
              })
            ]
          })
        : null,
      ev.files && ev.files.length
        ? jsxs('div', {
            children: [
              jsx('div', { className: 'mb-1 text-[0.62rem] font-semibold uppercase tracking-wider', style: { color: C.faint }, children: 'Files touched' }),
              jsx('div', {
                className: 'flex flex-col gap-0.5',
                children: ev.files.map((f, i) => jsx('div', { className: 'truncate font-mono text-[0.66rem]', style: { color: C.muted }, children: f }, i))
              })
            ]
          })
        : null
    ]
  })
}

function NeedInspectorBody({ item }) {
  const [busy, setBusy] = useState('')
  const [failed, setFailed] = useState('')
  const run = useCallback(async (tag, fn) => {
    setBusy(tag)
    setFailed('')
    try {
      await fn()
      haptic('submit')
      invalidate()
    } catch (err) {
      setFailed(errMsg(err))
      haptic('cancel')
    } finally {
      setBusy('')
    }
  }, [item])
  const open = () => run('open', () => openItemSession(item))
  const body = item.kind === 'approval'
    ? jsx(ApprovalBody, { item, busy, run, open })
    : item.kind === 'clarify'
      ? jsx(ClarifyBody, { item, busy, run, open })
      : jsx(GenericRequestBody, { item, busy, open })
  return jsxs('div', {
    className: 'flex flex-col gap-2 p-3',
    children: [body, failed ? jsx('div', { className: 'text-[0.68rem]', style: { color: C.red }, children: failed }) : null]
  })
}

function SessInspectorBody({ e }) {
  // details/actions for waiting, in-flight and finished feed entries
  const [busy, setBusy] = useState('')
  const [nudging, setNudging] = useState(false)
  const [nudgeText, setNudgeText] = useState('')
  const [note, setNote] = useState('')
  const pinnedMap = useValue($pinned)
  const sess = e.type === 'finished' ? null : e.row.session
  const f = e.type === 'finished' ? e.f : null
  const storedId = sess ? sess.session_key : resolveStoredId(f)
  const runtimeId = sess ? sess.id : f.runtimeId
  const route = sess ? e.row.route : f.route
  const title = sess ? sess.title : f.title
  const pinned = Boolean(storedId && pinnedMap[storedId])

  const act = async (tag, fn) => {
    setBusy(tag)
    try {
      await fn()
      haptic('submit')
      invalidate()
    } catch {
      haptic('cancel')
    } finally {
      setBusy('')
    }
  }

  return jsxs('div', {
    className: 'flex flex-col gap-3 p-3',
    children: [
      jsxs('div', {
        className: 'flex flex-col gap-1',
        children: [
          jsx('div', { className: 'text-[0.82rem] font-semibold', style: { color: C.text }, children: title || 'Session' }),
          jsx('div', {
            className: 'text-[0.68rem]',
            style: { color: C.faint },
            children: sess
              ? `${sess.status || 'unknown'} · ${sess.model || ''} · ${sess.message_count ?? 0} messages`
              : f.kind === 'error' ? 'Ended with an error' : 'Turn finished'
          }),
          sess && sess.preview
            ? jsx('div', { className: 'mt-1 line-clamp-3 text-[0.72rem]', style: { color: C.muted }, children: sess.preview })
            : null
        ]
      }),
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-1.5',
        children: [
          jsx(Button, {
            size: 'xs', variant: 'default', disabled: Boolean(busy),
            onClick: () => act('open', () => sess
              ? openItemSession({ storedId, sessionId: runtimeId, route })
              : host.openSession(storedId, { ...(route ? { route, profile: route.targetProfile } : {}), awaitHydration: true })),
            children: busy === 'open' ? spinIcon('sync') : 'Open session'
          }),
          storedId
            ? jsx(Button, {
                size: 'xs', variant: 'outline', disabled: Boolean(busy),
                onClick: () => act('pin', () => togglePin(storedId, { title, route })),
                children: pinned ? 'Unpin' : 'Pin to Watching'
              })
            : null,
          runtimeId
            ? jsx(Button, {
                size: 'xs', variant: 'outline',
                onClick: () => setNudging(v => !v),
                children: 'Follow up'
              })
            : null,
          sess && e.type === 'flight'
            ? jsx(Button, {
                size: 'xs', variant: 'outline', disabled: Boolean(busy),
                onClick: () => act('stop', () => interruptSession(route, runtimeId)),
                children: 'Stop'
              })
            : null,
          f
            ? jsx(Button, {
                size: 'xs', variant: 'ghost', disabled: Boolean(busy),
                onClick: () => act('dismiss', () => dismissFinished(f.key)),
                children: 'Dismiss'
              })
            : null
        ]
      }),
      nudging && runtimeId
        ? jsxs('div', {
            className: 'flex items-center gap-1.5',
            children: [
              jsx(Input, {
                value: nudgeText,
                placeholder: 'Follow up — resumes this session…',
                onChange: ev => setNudgeText(ev.target.value),
                onKeyDown: ev => {
                  ev.stopPropagation()
                  if (isSubmitEnter(ev) && nudgeText.trim()) {
                    act('nudge', async () => {
                      await nudgeSession(route, runtimeId, nudgeText.trim())
                      setNudgeText('')
                      setNudging(false)
                      setNote('Sent — session resumed')
                    })
                  }
                },
                className: 'h-7 flex-1 text-[0.78rem]'
              }),
              jsx(Button, {
                size: 'xs', variant: 'secondary', disabled: !nudgeText.trim() || Boolean(busy),
                onClick: () => act('nudge', async () => {
                  await nudgeSession(route, runtimeId, nudgeText.trim())
                  setNudgeText('')
                  setNudging(false)
                  setNote('Sent — session resumed')
                }),
                children: 'Send'
              })
            ]
          })
        : null,
      note ? jsx('div', { className: 'text-[0.68rem]', style: { color: C.emerald }, children: note }) : null
    ]
  })
}

function GhostDock({ e, route, runtimeId, storedId }) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState('')
  const [note, setNote] = useState('')
  const isLive = Boolean(runtimeId) && e.type !== 'finished'

  const steer = async () => {
    const t = text.trim()
    if (!t) return
    setBusy('steer')
    try {
      const res = await rpc(route, 'session.steer', { session_id: runtimeId, text: t })
      setNote(res && res.status === 'rejected' ? 'steer rejected — session may be mid-build' : `steered — ${res && res.status || 'ok'}`)
      if (res && res.status !== 'rejected') setText('')
      haptic('submit')
    } catch (err) {
      setNote(`steer failed: ${errMsg(err)}`)
      haptic('cancel')
    } finally {
      setBusy('')
    }
  }

  const branch = async () => {
    setBusy('branch')
    try {
      const res = await rpc(route, 'session.resume', { session_id: storedId, omit_messages: true })
      const liveSid = res && res.session_id
      const br = await rpc(route, 'session.branch', { session_id: liveSid || storedId })
      if (br && br.stored_session_id) {
        setNote(`branched — ${br.title || br.stored_session_id}`)
        host.openSession(br.stored_session_id, {}).catch(() => {})
        haptic('submit')
      } else setNote('branch failed')
    } catch (err) {
      setNote(`branch failed: ${errMsg(err)}`)
      haptic('cancel')
    } finally {
      setBusy('')
    }
  }

  if (!isLive && !storedId) return null
  return jsxs('div', {
    className: 'flex shrink-0 items-center gap-1.5 px-3 py-2',
    style: { borderTop: `1px solid ${C.border}`, background: C.surface },
    children: [
      jsx(Codicon, { name: 'comment-discussion', className: 'shrink-0 text-[0.72rem]', style: { color: C.mono } }),
      isLive
        ? jsxs('div', {
            className: 'flex min-w-0 flex-1 items-center gap-1.5',
            children: [
              jsx(Input, {
                value: text,
                placeholder: 'Ghost-steer — injected mid-run, no restart…',
                onChange: ev => setText(ev.target.value),
                onKeyDown: ev => {
                  ev.stopPropagation()
                  if (isSubmitEnter(ev)) steer()
                },
                className: 'h-7 flex-1 text-[0.76rem]'
              }),
              jsx(Button, { size: 'xs', variant: 'secondary', disabled: !text.trim() || Boolean(busy), onClick: steer, children: busy === 'steer' ? spinIcon('sync') : 'Steer' })
            ]
          })
        : jsx(Button, {
            size: 'xs', variant: 'secondary', disabled: Boolean(busy), onClick: branch,
            children: busy === 'branch' ? spinIcon('sync') : '⑂ Branch from here'
          }),
      note ? jsx('span', { className: 'shrink-0 text-[0.62rem]', style: { color: note.includes('fail') || note.includes('reject') ? C.red : C.emerald }, children: note }) : null
    ]
  })
}

const INSP_TABS = [['details', 'Details'], ['evidence', 'Evidence'], ['impact', 'Impact'], ['diff', 'Diff'], ['trace', 'Trace']]

function Inspector({ e, evMap }) {
  const [tab, setTab] = useState('details')
  const [scrub, setScrub] = useState(null)
  const [reverting, setReverting] = useState(false)
  const [revertNote, setRevertNote] = useState('')
  const selKey = e ? e.key : ''
  const runtimeId = !e ? null : e.type === 'finished' ? e.f.runtimeId : e.type === 'need' ? e.item.sessionId : e.row.session.id
  const route = !e ? null : e.type === 'finished' ? e.f.route : e.type === 'need' ? e.item.route : e.row.route
  const storedId = !e ? null : e.type === 'finished' ? resolveStoredId(e.f) : e.type === 'need' ? e.item.storedId : e.row.session.session_key
  const eventsQ = useQuery({
    queryKey: [...INSP_QK, selKey],
    queryFn: () => fetchEventsFor(e),
    enabled: Boolean(e),
    refetchInterval: 3000,
    staleTime: 1200
  })
  const impactQ = useQuery({
    queryKey: ['hday-impact', selKey],
    queryFn: async () => {
      const disp = await rpc(route, 'command.dispatch', { name: 'day-impact', arg: storedId || '' })
      const out = disp && (disp.output || disp.text || '')
      return out && out.trim().startsWith('{') ? JSON.parse(out) : { ok: false, error: 'no response' }
    },
    enabled: Boolean(e && storedId),
    staleTime: 15000
  })
  const ev = e ? feedEvidence(e, evMap) : null

  useEffect(() => {
    setTab('details')
    setScrub(null)
    setRevertNote('')
  }, [selKey])

  if (!e) {
    return jsxs('div', {
      className: 'flex h-full flex-col items-center justify-center gap-2',
      children: [
        jsx(Codicon, { name: 'inspect', className: 'text-2xl', style: { color: C.faint } }),
        jsx('div', { className: 'text-[0.78rem]', style: { color: C.muted }, children: 'Select a row to inspect it' }),
        jsxs('div', {
          className: 'flex items-center gap-2 text-[0.66rem]',
          style: { color: C.faint },
          children: [jsx(Kbd, { children: 'j/k' }), 'move', jsx('span', { children: '·' }), jsx(Kbd, { children: 'o' }), 'open session']
        })
      ]
    })
  }

  const title = feedTitle(e)
  const st = FEED_ICON[e.type](e)
  const allEvents = (eventsQ.data && eventsQ.data.events) || []
  const turns = turnsFromEvents(allEvents)
  const events = scrub === null ? allEvents : allEvents.filter(x => evSeq(x) <= scrub)
  const tabs = INSP_TABS.filter(([k]) => k !== 'evidence' || Boolean(ev))

  const revertTo = async snap => {
    setReverting(true)
    try {
      const disp = await rpc(route, 'command.dispatch', { name: 'day-rollback', arg: `${storedId || ''} ${snap.sha}` })
      const out = disp && (disp.output || disp.text || '')
      const res = out && out.trim().startsWith('{') ? JSON.parse(out) : null
      setRevertNote(res && res.ok ? `reverted to snap ${snap.n}` : `!${(res && res.error) || 'rollback failed'}`)
      haptic(res && res.ok ? 'submit' : 'cancel')
    } catch (err) {
      setRevertNote(`!${errMsg(err)}`)
      haptic('cancel')
    } finally {
      setReverting(false)
    }
  }

  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col',
    children: [
      jsxs('div', {
        className: 'flex shrink-0 items-center gap-2 px-3 py-2',
        style: { borderBottom: `1px solid ${C.border}` },
        children: [
          jsx(Codicon, { name: st.icon, className: 'text-[0.8rem]', style: { color: st.color } }),
          jsx('span', { className: 'min-w-0 flex-1 truncate text-[0.82rem] font-semibold', style: { color: C.text }, children: title }),
          jsx(AgoText, { ms: feedAt(e) }),
          jsx(Button, {
            size: 'xs', variant: 'outline',
            onClick: () => {
              if (e.type === 'need') openItemSession(e.item).catch(() => {})
              else if (e.type === 'finished') {
                if (e.f.storedId) host.openSession(e.f.storedId, { ...(e.f.route ? { route: e.f.route, profile: e.f.route.targetProfile } : {}), awaitHydration: true }).catch(() => {})
              } else openItemSession({ storedId: e.row.session.session_key, sessionId: e.row.session.id, route: e.row.route }).catch(() => {})
            },
            children: 'Open'
          })
        ]
      }),
      jsx(XrayBar, { events: allEvents, ev, storedId, route }),
      jsx(TurnScrubber, {
        turns, scrub, snaps: ev && ev.snaps, reverting, revertNote,
        onScrub: setScrub, onRevert: revertTo
      }),
      jsxs('div', {
        className: 'flex shrink-0 items-center gap-3 px-3 pt-1',
        children: tabs.map(([k, label]) => jsx('button', {
          className: cn('hday-tab px-0.5 py-1.5 text-[0.68rem] font-medium', tab === k && 'hday-tab-on'),
          style: { color: tab === k ? C.text : C.faint },
          onClick: () => { setTab(k); haptic('selection') },
          children: label
        }, k))
      }),
      jsx(ScrollArea, {
        className: 'min-h-0 flex-1',
        children: tab === 'details'
          ? e.type === 'need'
            ? jsx(NeedInspectorBody, { item: e.item })
            : jsx(SessInspectorBody, { e })
          : tab === 'evidence'
            ? jsx(EvidenceView, { ev })
            : tab === 'impact'
              ? jsx(ImpactView, { data: impactQ.data, loading: impactQ.isLoading })
              : tab === 'diff'
                ? jsx(DiffView, { events })
                : jsx(TraceView, { events })
      }),
      eventsQ.data && eventsQ.data.truncated
        ? jsx('div', { className: 'shrink-0 px-3 py-1 text-[0.62rem]', style: { color: C.faint, borderTop: `1px solid ${C.border}` }, children: 'replay window truncated — open the session for the full history' })
        : null,
      jsx(GhostDock, { e, route, runtimeId, storedId })
    ]
  })
}

function DayPage() {
  ensureDayStyles()
  const scan = useQuery({ queryKey: SCAN_QK, queryFn: scanInbox, refetchInterval: 5000, staleTime: 1500, refetchOnWindowFocus: true })
  const cron = useQuery({ queryKey: CRON_QK, queryFn: scanCron, refetchInterval: 30000, staleTime: 10000, refetchOnWindowFocus: true })
  const finishedAll = useValue($finished)
  const [filter, setFilter] = useState('')
  const [sel, setSel] = useState(0)
  const [celebrate, setCelebrate] = useState(false)
  const [focus, setFocus] = useState(false)
  const [instOpen, setInstOpen] = useState(false)
  const prevNeeds = useRef(null)
  const pinnedMap = useValue($pinned)
  const undo = useValue($undo)
  useValue($triage)

  const data = scan.data
  const q = filter.trim().toLowerCase()
  const match = t => !q || String(t || '').toLowerCase().includes(q)
  const needs = ((data && data.needs) || []).filter(n => match(n.title) || match(n.preview) || match(n.method))
  const flight = ((data && data.flight) || []).filter(r => match(r.session.title) || match(r.session.preview))
  const waiting = ((data && data.waiting) || []).filter(r => match(r.session.title) || match(r.session.preview))
  const finished = finishedAll.filter(f => match(f.title))
  const sources = (data && data.sources) || []
  const jobs = (cron.data && cron.data.jobs) || []
  const hiddenCount = (data && data.hiddenCount) || 0
  const mutedMap = useValue($muted)
  const prefs = useValue($prefs)
  const watching = Object.values(pinnedMap).sort((a, b) => b.at - a.at)
  const evMap = useValue($evidence)

  // unified action feed — needs, waiting, in-flight, finished in triage order
  const feed = []
  for (const n of needs) feed.push({ type: 'need', key: `need:${n.key}`, item: n })
  if (!focus) for (const r of waiting) feed.push({ type: 'waiting', key: `sess:${r.sourceKey}#${r.session.id}`, row: r })
  for (const r of flight) feed.push({ type: 'flight', key: `sess:${r.sourceKey}#${r.session.id}`, row: r })
  if (!focus) for (const f of finished) feed.push({ type: 'finished', key: `fin:${f.key}`, f })
  const feedIndex = new Map(feed.map((e, i) => [e.key, i]))
  const selEntry = feed[sel] || null

  // clamp keyboard selection to the visible feed
  useEffect(() => {
    if (sel >= feed.length) setSel(Math.max(0, feed.length - 1))
  }, [feed.length])

  // inbox-zero confetti — only on a real transition, never on first paint
  useEffect(() => {
    if (prevNeeds.current !== null && prevNeeds.current > 0 && needs.length === 0) {
      setCelebrate(true)
      haptic('submit')
    }
    prevNeeds.current = needs.length
  }, [needs.length])

  // j/k/a/d/s/o triage — live only while this route is foreground and not typing
  useEffect(() => {
    const onKey = ev => {
      if (!location.hash.includes(DAY_PATH)) return
      const t = ev.target
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
      if (ev.metaKey || ev.ctrlKey || ev.altKey) return
      const entry = feed[sel]
      if (ev.key === 'j' || ev.key === 'ArrowDown') {
        setSel(s => Math.min(feed.length - 1, s + 1))
        ev.preventDefault()
      } else if (ev.key === 'k' || ev.key === 'ArrowUp') {
        setSel(s => Math.max(0, s - 1))
        ev.preventDefault()
      } else if (!entry) return
      else if (entry.type === 'need' && ev.key === 'a' && entry.item.kind === 'approval') {
        respondApproval(entry.item, 'once').then(invalidate).catch(() => {})
      } else if (entry.type === 'need' && ev.key === 'd' && entry.item.kind === 'approval') {
        respondApproval(entry.item, 'deny').then(invalidate).catch(() => {})
      } else if (entry.type === 'need' && ev.key === 's') {
        snoozeRequest(entry.item.requestId)
      } else if (ev.key === 'o' || ev.key === 'Enter') {
        if (entry.type === 'need') openItemSession(entry.item).catch(() => {})
        else if (entry.type === 'finished') {
          if (entry.f.storedId) host.openSession(entry.f.storedId, {}).catch(() => {})
        } else openItemSession({ storedId: entry.row.session.session_key, sessionId: entry.row.session.id, route: entry.row.route }).catch(() => {})
      } else return
      haptic('selection')
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [feed, sel])

  // keep the selected feed row scrolled into view
  useEffect(() => {
    const el = document.querySelector(`[data-hday-idx="${sel}"]`)
    if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest' })
  }, [sel])

  // group approvals by session for the resolve-all bar
  const approvalsBySession = {}
  for (const n of needs) {
    if (n.kind !== 'approval') continue
    ;(approvalsBySession[n.sessionId] = approvalsBySession[n.sessionId] || []).push(n)
  }
  const floodSessions = Object.entries(approvalsBySession).filter(([, items]) => items.length > 1)

  const dateStr = new Date().toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })
  const nextJob = jobs.find(j => j.enabled !== false && j.next_run_at && !Number.isNaN(Date.parse(j.next_run_at)))

  const refresh = () => invalidate()

  const empty =
    data && needs.length === 0 && flight.length === 0 && waiting.length === 0 && finished.length === 0 && jobs.length === 0

  const feedSection = (id, icon, title, count, color, type, extra) =>
    jsxs('section', {
      id,
      children: [
        jsx(SectionLabel, { icon, title, count, color, action: extra }),
        jsx('div', {
          className: 'flex flex-col',
          children: feed.filter(e => e.type === type).map(e =>
            jsx(FeedRow, { e, idx: feedIndex.get(e.key), selected: feedIndex.get(e.key) === sel, evMap, onSelect: () => setSel(feedIndex.get(e.key) ?? 0) }, e.key))
        })
      ]
    })

  return jsxs('div', {
    className: 'hday-root flex h-full min-h-0 flex-col',
    style: {
      background: C.canvas,
      color: C.text,
      // force the dark cockpit palette — every (--ui-*) utility resolves to our tokens
      '--ui-bg-primary': C.canvas,
      '--ui-bg-secondary': C.surface,
      '--ui-bg-quaternary': C.surfaceHover,
      '--ui-stroke-secondary': C.border,
      '--ui-text-primary': C.text,
      '--ui-text-secondary': C.muted,
      '--ui-text-tertiary': C.faint
    },
    children: [
      celebrate ? jsx(Confetti, { onDone: () => setCelebrate(false) }) : null,
      jsxs('header', {
        className: 'flex shrink-0 items-center justify-between gap-4 px-6 py-4',
        style: { borderBottom: `1px solid ${C.border}` },
        children: [
          jsxs('div', {
            className: 'flex min-w-0 items-baseline gap-3',
            children: [
              jsx('h1', { className: 'text-lg font-semibold tracking-tight', children: `${greeting()}` }),
              jsx('span', { className: 'truncate text-[0.75rem] text-muted-foreground', children: dateStr })
            ]
          }),
          jsx(DayArc, {}),
          jsxs('div', {
            className: 'flex shrink-0 items-center gap-2',
            children: [
              needs.length
                ? jsxs(Badge, {
                    variant: 'warn',
                    className: 'gap-1',
                    children: [jsx(Codicon, { name: 'bell-dot' }), `${needs.length} waiting on you`]
                  })
                : jsxs(Badge, {
                    variant: 'success',
                    className: 'gap-1',
                    children: [jsx(Codicon, { name: 'check' }), 'caught up']
                  }),
              triageToday()
                ? jsxs(Badge, {
                    variant: 'outline',
                    className: 'gap-1',
                    children: [jsx(Codicon, { name: 'checklist' }), `${triageToday()} triaged`]
                  })
                : null,
              nextJob
                ? jsxs(Badge, {
                    variant: 'outline',
                    className: 'gap-1',
                    children: [
                      jsx(Codicon, { name: 'calendar' }),
                      `next: ${nextJob.name || 'job'} · ${relativeTime(Date.parse(nextJob.next_run_at))}`
                    ]
                  })
                : null,
              jsx(Tip, {
                label: 'Repo instincts — disproven approaches Hermes remembers',
                children: jsx(Button, {
                  size: 'icon-sm',
                  variant: 'ghost',
                  onClick: () => { setInstOpen(v => !v); haptic('selection') },
                  children: jsx(Codicon, { name: 'lightbulb', className: instOpen ? '' : 'text-muted-foreground' })
                })
              }),
              jsx(Tip, {
                label: prefs.notify ? 'Notifications on — click to mute' : 'Notifications off — click to enable',
                children: jsx(Button, {
                  size: 'icon-sm',
                  variant: 'ghost',
                  onClick: () => setNotifyPref(!prefs.notify),
                  children: jsx(Codicon, { name: prefs.notify ? 'bell' : 'bell-slash', className: prefs.notify ? '' : 'text-muted-foreground/50' })
                })
              }),
              jsx(Tip, {
                label: focus ? 'Exit focus — show the whole board' : 'Focus — only what needs you and what is running',
                children: jsx(Button, {
                  size: 'icon-sm',
                  variant: 'ghost',
                  onClick: () => {
                    setFocus(v => !v)
                    haptic('selection')
                  },
                  children: jsx(Codicon, { name: focus ? 'eye-closed' : 'eye', className: focus ? '' : 'text-muted-foreground' })
                })
              }),
              jsx(Tip, {
                label: 'Refresh',
                children: jsx(Button, {
                  size: 'icon-sm',
                  variant: 'ghost',
                  onClick: refresh,
                  children: scan.isFetching ? spinIcon('sync') : jsx(Codicon, { name: 'refresh' })
                })
              })
            ]
          })
        ]
      }),
      jsxs('div', {
        className: 'flex min-h-0 flex-1',
        children: [
          // LEFT — the action feed
          jsxs('section', {
            className: 'flex w-[38%] min-w-[300px] max-w-[480px] shrink-0 flex-col',
            style: { borderRight: `1px solid ${C.border}` },
            children: [
              jsxs('div', {
                className: 'shrink-0 px-3 pt-3',
                children: [
                  jsxs('div', {
                    className: 'mb-2 grid grid-cols-2 gap-1.5 xl:grid-cols-4',
                    children: [
                      jsx(StatTile, { icon: 'bell-dot', label: 'Waiting', n: needs.length, color: '#f59e0b', scrollTo: 'hday-needs' }),
                      jsx(StatTile, { icon: 'rocket', label: 'In flight', n: flight.length, color: '#58a6ff', scrollTo: 'hday-flight' }),
                      jsx(StatTile, { icon: 'pass', label: 'Review', n: finished.length, color: '#34d399', scrollTo: 'hday-finished' }),
                      jsx(StatTile, { icon: 'calendar', label: 'Scheduled', n: jobs.length, color: '#a855f7', scrollTo: 'hday-sched' })
                    ]
                  }),
                  jsx(QuickTaskBar, {}),
                  jsxs('div', {
                    className: 'mt-2 flex items-center gap-3 px-1 pb-1',
                    children: [
                      jsx(SearchField, {
                        placeholder: 'Filter…',
                        value: filter,
                        onChange: setFilter,
                        containerClassName: 'w-40'
                      }),
                      jsxs('span', {
                        className: 'hidden items-center gap-2 text-[0.62rem] text-muted-foreground/70 xl:flex',
                        children: [
                          jsxs('span', { className: 'flex items-center gap-1', children: [jsx(Kbd, { children: 'j/k' }), 'move'] }),
                          jsxs('span', { className: 'flex items-center gap-1', children: [jsx(Kbd, { children: 'a' }), 'allow'] }),
                          jsxs('span', { className: 'flex items-center gap-1', children: [jsx(Kbd, { children: 'd' }), 'deny'] }),
                          jsxs('span', { className: 'flex items-center gap-1', children: [jsx(Kbd, { children: 's' }), 'snooze'] }),
                          jsxs('span', { className: 'flex items-center gap-1', children: [jsx(Kbd, { children: 'o' }), 'open'] })
                        ]
                      })
                    ]
                  })
                ]
              }),
              jsx(ScrollArea, {
                className: 'min-h-0 flex-1',
                children: jsxs('div', {
                  className: 'flex flex-col gap-4 px-1 py-2',
                  children: [
                    scan.isLoading
                      ? jsxs('div', {
                          className: 'flex items-center gap-2 py-8 text-muted-foreground',
                          children: [jsx(Loader, {}), jsx('span', { className: 'text-[0.8rem]', children: 'Scanning…' })]
                        })
                      : null,
                    scan.isError ? jsx(ErrorState, { title: 'Scan failed', description: errMsg(scan.error) }) : null,
                    needs.length
                      ? jsxs('div', {
                          children: [
                            feedSection('hday-needs', 'bell-dot', 'Needs you', needs.length, '#f59e0b', 'need',
                              floodSessions.length
                                ? jsx('span', {
                                    className: 'text-[0.62rem]',
                                    style: { color: C.amber },
                                    children: `${floodSessions.length} flood`
                                  })
                                : null),
                            floodSessions.length
                              ? jsx('div', {
                                  className: 'mb-2 flex flex-col gap-1.5',
                                  children: floodSessions.map(([sid, items]) =>
                                    jsx(ApproveAllBar, { sessionId: sid, title: items[0].title, items }, `flood-${sid}`)
                                  )
                                })
                              : null
                          ]
                        })
                      : null,
                    waiting.length ? feedSection(null, 'watch', 'Waiting on input', waiting.length, '#f59e0b', 'waiting') : null,
                    flight.length ? feedSection('hday-flight', 'rocket', 'In flight', flight.length, '#34d399', 'flight') : null,
                    finished.length
                      ? feedSection('hday-finished', 'pass', 'Finished — review', finished.length, '#34d399', 'finished',
                          jsx(Button, {
                            size: 'xs',
                            variant: 'ghost',
                            className: 'h-5 px-1.5 text-[0.62rem] text-muted-foreground',
                            onClick: () => {
                              $finished.get().forEach(f => bumpTriage())
                              $finished.set([])
                              persistFinished([])
                              haptic('selection')
                            },
                            children: 'Clear all'
                          }))
                      : null,
                    hiddenCount
                      ? jsxs('div', {
                          className: 'px-2 text-[0.68rem] text-muted-foreground/60',
                          children: [`${hiddenCount} item${hiddenCount === 1 ? '' : 's'} snoozed or muted`]
                        })
                      : null,
                    empty
                      ? jsxs('div', {
                          className: 'flex flex-col items-center gap-3 py-12 text-center',
                          children: [
                            jsx('div', { className: 'text-[0.85rem] font-medium', style: { color: C.text }, children: 'Nothing on the board' }),
                            jsx('div', {
                              className: 'max-w-xs text-[0.72rem] leading-5 text-muted-foreground',
                              children: 'No approvals waiting, nothing running, nothing scheduled. Start something up top and run the whole day from here.'
                            })
                          ]
                        })
                      : null,
                    !focus && watching.length
                      ? jsxs('section', {
                          children: [
                            jsx(SectionLabel, { icon: 'pinned', title: 'Watching', count: watching.length, color: '#58a6ff' }),
                            jsx('div', { className: 'flex flex-col', children: watching.map(w => jsx(WatchingRow, { entry: w }, w.storedId)) })
                          ]
                        })
                      : null,
                    !focus
                      ? jsxs('section', {
                          id: 'hday-sched',
                          children: [
                            jsx(SectionLabel, { icon: 'calendar', title: 'Scheduled', count: jobs.length, color: '#a855f7' }),
                            jobs.length
                              ? jsx('div', { className: 'flex flex-col', children: jobs.slice(0, 20).map(j2 => jsx(CronRow, { entry: j2 }, j2.key)) })
                              : jsx('div', { className: 'px-2 py-2 text-[0.72rem] text-muted-foreground', children: 'No scheduled jobs.' })
                          ]
                        })
                      : null,
                    !focus
                      ? jsxs('section', {
                          children: [
                            jsx(SectionLabel, { icon: 'server-environment', title: 'Sources', count: sources.length }),
                            jsx('div', {
                              className: 'flex flex-col gap-1',
                              children: sources.map(s => {
                                const muted = Boolean(mutedMap[s.key])
                                return jsxs(
                                  'div',
                                  {
                                    className: cn('flex items-center gap-2 px-2 py-1 text-[0.72rem]', muted && 'opacity-50'),
                                    children: [
                                      jsx('span', {
                                        className: cn('size-1.5 rounded-full', s.unreachable ? 'bg-destructive' : 'bg-emerald-500')
                                      }),
                                      jsx('span', { className: 'min-w-0 flex-1 truncate text-(--ui-text-secondary)', children: s.label }),
                                      s.unreachable ? jsx(Tip, { label: s.unreachable, children: jsx(Codicon, { name: 'warning', className: 'text-amber-500' }) }) : null,
                                      jsx(Tip, {
                                        label: muted ? 'Unmute' : 'Mute source',
                                        children: jsx(Button, {
                                          size: 'icon-xs',
                                          variant: 'ghost',
                                          onClick: () => {
                                            toggleMute(s.key)
                                            invalidate()
                                          },
                                          children: jsx(Codicon, { name: muted ? 'bell-slash' : 'bell' })
                                        })
                                      })
                                    ]
                                  },
                                  s.key
                                )
                              })
                            })
                          ]
                        })
                      : null,
                    jsx('div', {
                      className: 'px-2 pt-2 text-[0.68rem]',
                      style: { borderTop: `1px solid ${C.border}`, color: C.faint },
                      children: `Today · ${finishedAll.filter(f => Date.now() - f.at < 24 * 60 * 60 * 1000).length} finished · ${triageToday()} triaged`
                    })
                  ]
                })
              })
            ]
          }),
          // RIGHT — the live inspector
          jsxs('section', {
            className: 'relative flex min-w-0 flex-1 flex-col',
            style: { background: '#0e1117' },
            children: [
              jsx(Inspector, { e: selEntry, evMap }),
              jsx(InstinctsDrawer, {
                open: instOpen,
                onClose: () => setInstOpen(false),
                route: sources.length ? sources[0].route : null
              })
            ]
          })
        ]
      }),
      undo
        ? jsxs('div', {
            className: 'fixed bottom-5 left-1/2 z-40 flex -translate-x-1/2 items-center gap-3 rounded-lg px-3 py-2 shadow-lg',
            style: { background: C.surface, border: `1px solid ${C.border}` },
            children: [
              jsxs('span', { className: 'text-[0.75rem] text-muted-foreground', children: ['Dismissed “', undo.f.title || 'session', '”'] }),
              jsx(Button, {
                size: 'xs',
                variant: 'secondary',
                onClick: () => {
                  undoDismiss()
                  haptic('selection')
                },
                children: 'Undo'
              })
            ]
          })
        : null
    ]
  })
}

// ---------------------------------------------------------------------------
// statusbar chip
// ---------------------------------------------------------------------------

function DayChip() {
  const scan = useQuery({ queryKey: SCAN_QK, queryFn: scanInbox, refetchInterval: 5000, staleTime: 1500 })
  const n = (scan.data && scan.data.needs.length) || 0
  const waiting = (scan.data && scan.data.waiting.length) || 0
  const total = n + waiting

  return jsx(Tip, {
    label: total ? `${total} waiting on you — open Day` : 'Day — all caught up',
    children: jsxs('button', {
      type: 'button',
      onClick: () => host.navigate(DAY_PATH),
      className: 'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) transition-colors hover:text-(--ui-text-primary)',
      children: [
        jsx(Codicon, { name: 'calendar' }),
        total
          ? jsx(Badge, { size: 'xs', variant: 'warn', children: String(total) })
          : jsx('span', { className: 'text-muted-foreground/60', children: 'day' })
      ]
    })
  })
}

// ---------------------------------------------------------------------------
// registration
// ---------------------------------------------------------------------------

export default {
  id: 'hermes-day',
  name: 'Hermes Day',
  defaultEnabled: true,

  register(ctx) {
    storageRef = ctx.storage
    notifyRef = (ctx.os && typeof ctx.os.notify === 'function') ? ctx.os : null
    $finished.set(loadFinished())
    $muted.set(loadMap(MUTED_KEY))
    $snoozed.set(loadMap(SNOOZE_KEY))
    $pinned.set(loadMap(PINNED_KEY))
    const savedPrompts = storageRef ? storageRef.get(PROMPTS_KEY, null) : null
    $prompts.set(Array.isArray(savedPrompts) && savedPrompts.length ? savedPrompts : DEFAULT_PROMPTS)
    $prefs.set({ notify: true, ...loadMap(PREFS_KEY) })

    ctx.onEvent('message.complete', ev => recordFinished(ev, 'done'))
    ctx.onEvent('error', ev => recordFinished(ev, 'error'))
    for (const type of ['sessions.changed', 'request.cancel', 'cron.changed', 'message.complete', 'error', 'session.info']) {
      ctx.onEvent(type, () => invalidate())
    }

    ctx.registerMany([
      {
        id: 'route',
        area: ROUTES_AREA,
        title: 'Day',
        data: { path: DAY_PATH },
        render: () => jsx(DayPage, {})
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        title: 'Day',
        data: { path: DAY_PATH, label: 'Day', codicon: 'calendar' }
      },
      {
        id: 'chip',
        area: STATUSBAR_AREAS.right,
        title: 'Day',
        order: 40,
        render: () => jsx(DayChip, {})
      },
      {
        id: 'palette.open',
        area: PALETTE_AREA,
        title: 'Open Day',
        data: {
          id: 'hermes-day.open',
          label: 'Open Day',
          action: 'hermes-day.open',
          keywords: ['inbox', 'triage', 'approvals', 'today', 'agenda'],
          run: () => host.navigate(DAY_PATH)
        }
      },
      {
        id: 'keybind.open',
        area: KEYBINDS_AREA,
        title: 'Open Day',
        data: {
          id: 'hermes-day.open',
          label: 'Open Day',
          category: 'navigation',
          defaults: ['mod+shift+i'],
          run: () => host.navigate(DAY_PATH)
        }
      },
      {
        id: 'palette.notify',
        area: PALETTE_AREA,
        title: 'Day: Toggle notifications',
        data: {
          id: 'hermes-day.notify',
          label: 'Day: Toggle notifications',
          keywords: ['day', 'notify', 'quiet', 'alerts'],
          run: () => setNotifyPref(!$prefs.get().notify)
        }
      }
    ])
  }
}
