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
// honesty-guard liveness reported by the agent half: {ok, degraded, failed[], cases[]}
const $guard = atom({})
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
// lane 10 palette - sentinel block copied verbatim from patches/10-skin-hud.py
const SKIN_COLOR_CSS = '/* == hday-skin:COLORS BEGIN == */\n/* --- base tokens: skin "current" (default). Values are the plugin\'s own C\n       palette, so pre-existing UI and skin "current" agree exactly. */\n.hday-root{--hday-canvas:#0b0c10;--hday-surface:#161922;--hday-surface-2:#1a1d26;--hday-line:#262a33;\n--hday-line-hot:#333a47;--hday-ink:#f3f4f6;--hday-ink-2:#94a3b8;--hday-ink-3:#64748b;\n--hday-accent:#38bdf8;--hday-ok:#34d399;--hday-warn:#f59e0b;--hday-danger:#ef4444;\n--hday-slate:#8b949e;--hday-kind-input:#c084fc;--hday-glow:#38bdf8;--hday-scan-alpha:18%}\n\n/* --- skin "current" re-declared under its attribute selector, same values:\n       an explicit choice must be identical to the base, never a drift. */\n.hday-root[data-hday-skin="current"]{--hday-canvas:#0b0c10;--hday-surface:#161922;--hday-surface-2:#1a1d26;--hday-line:#262a33;\n--hday-line-hot:#333a47;--hday-ink:#f3f4f6;--hday-ink-2:#94a3b8;--hday-ink-3:#64748b;\n--hday-accent:#38bdf8;--hday-ok:#34d399;--hday-warn:#f59e0b;--hday-danger:#ef4444;\n--hday-slate:#8b949e;--hday-kind-input:#c084fc;--hday-glow:#38bdf8;--hday-scan-alpha:18%}\n\n/* --- skin "phosphor": CRT green. Only hexes from the phosphor candidate\n       list (accents + danger). Surfaces, warn, slate, ink-3 inherit base —\n       re-declaring them would need a hex outside the candidate list. */\n.hday-root[data-hday-skin="phosphor"]{--hday-ink:#8fffce;--hday-ink-2:#7dffbc;--hday-accent:#33ff77;--hday-glow:#33ff77;\n--hday-ok:#4dff9e;--hday-danger:#ff5252;--hday-kind-input:#7dffbc;--hday-scan-alpha:30%}\n\n/* --- skin "oxide": amber/red rust. Surfaces + accents from the oxide\n       candidate list; ok/slate/ink/ink-3 inherit base. */\n.hday-root[data-hday-skin="oxide"]{--hday-canvas:#140d0b;--hday-surface:#1c1210;--hday-surface-2:#241714;--hday-line:#38231c;\n--hday-line-hot:#4a2f25;--hday-accent:#ff8c42;--hday-glow:#ff8c42;--hday-warn:#ffab70;\n--hday-danger:#e2483f;--hday-ink-2:#ffb98a;--hday-kind-input:#ff9a5c;--hday-scan-alpha:22%}\n/* == hday-skin:COLORS END == */'

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
    'body:has(.hday-root) div[class*="over-modal"] [class*="text-muted-foreground"]{color:#94a3b8!important}' +
    // guard shield — the gate's own liveness, never silent
    '@keyframes hday-pulse{0%,100%{box-shadow:0 0 0 0 rgba(239,68,68,0)}50%{box-shadow:0 0 0 3px rgba(239,68,68,.35)}}' +
    '.hday-shield{display:inline-flex;align-items:center;gap:.3rem;border:1px solid;border-radius:999px;' +
      'padding:.1rem .45rem;font-size:.6rem;font-weight:600;letter-spacing:.03em;text-transform:uppercase}' +
    '.hday-shield.hday-degraded{animation:hday-pulse 1.6s ease-in-out infinite}' +
    // tamper hatch — a verdict that was bought rather than earned
    '.hday-hatch{background-image:repeating-linear-gradient(45deg,rgba(239,68,68,.13) 0 6px,transparent 6px 12px)}' +
    // attention rows carry a left stripe in their kind's colour
    '.hday-attn{border-left:2px solid transparent}' +
    '.hday-attn:hover{background:#171a22}' +
    // receipt — evidence rendered like a till slip
    '.hday-receipt{border:1px dashed currentColor;border-radius:4px;padding:.5rem .6rem;' +
      'font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.66rem;line-height:1.55}' +
    '.hday-receipt-row{display:flex;gap:.5rem;justify-content:space-between}' +
    '.hday-receipt-sep{border-top:1px dashed currentColor;opacity:.35;margin:.35rem 0}'
  // F1: hand the sentinel palette to the CSS text builder (hoisted decl)
  if (typeof SKIN_COLOR_CSS === 'string' && SKIN_COLOR_CSS) {
    const skin = hdaySkinCss(SKIN_COLOR_CSS)
    if (skin) el.textContent += skin
  }
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
    if (parsed && parsed.guard && typeof parsed.guard === 'object') $guard.set(parsed.guard)
    const sessions = parsed && parsed.sessions
    if (sessions && typeof sessions === 'object') {
      const ev = { ...$evidence.get() }
      for (const [k, v] of Object.entries(sessions)) {
        if (v && (v.verdict || v.runs || v.blocks || v.vacuum || (v.snaps && v.snaps.length)
                  || (v.approvals && v.approvals.length) || (v.attn && v.attn.length)
                  || (v.tamper && v.tamper.length))) ev[`${source.key}#${k}`] = v
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
  const reducedRM = hdayUseReducedMotion()  // F3: unconditional hook call

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
      ...(stale && !reducedRM ? { animation: 'hday-stale 2.4s ease-in-out infinite' } : null),
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
      jsx(ReceiptCard, { receipt: ev.receipt, tamper: ev.tamper }),
      ev.tamper && ev.tamper.length
        ? jsxs('div', {
            children: [
              jsx('div', {
                className: 'mb-1 text-[0.62rem] font-semibold uppercase tracking-wider',
                style: { color: C.red },
                children: 'Suite integrity'
              }),
              jsx('div', {
                className: 'flex flex-col gap-0.5',
                children: ev.tamper.map((t, i) =>
                  jsx('div', { className: 'font-mono text-[0.64rem]', style: { color: C.red }, children: t }, i))
              })
            ]
          })
        : null,
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

// ---------------------------------------------------------------------------
// Guard shield — the honesty gate's own liveness, surfaced rather than assumed
// ---------------------------------------------------------------------------

function guardState(g) {
  if (!g || Object.keys(g).length === 0) return 'unknown'
  return g.degraded ? 'degraded' : 'ok'
}

function GuardShield() {
  const g = useValue($guard)
  const state = guardState(g)
  const tone = state === 'degraded' ? C.red : state === 'ok' ? C.emerald : C.slate
  const failed = Array.isArray(g && g.failed) ? g.failed : []
  const cases = Array.isArray(g && g.cases) ? g.cases : []
  const label = state === 'degraded' ? 'guard degraded' : state === 'ok' ? 'guard live' : 'guard unproven'
  const tip = state === 'degraded'
    ? `Honesty guard self-test is failing: ${failed.join(', ') || 'unknown case'}. Blocked-action protection cannot be trusted until this is fixed.`
    : state === 'ok'
      ? `Guard self-test passing (${cases.length} cases). Blocks test deletion, assertion gutting and exit laundering; deliberately allows cache cleanup.`
      : 'Guard has not reported yet this session.'
  return jsx(Tip, {
    label: tip,
    children: jsxs('span', {
      className: `hday-shield${state === 'degraded' ? ' hday-degraded' : ''}`,
      style: { color: tone, borderColor: tone },
      children: [jsx(Codicon, { name: 'shield', size: 11 }), label]
    })
  })
}

// ---------------------------------------------------------------------------
// Attention strip — what went wrong while you were not looking
// ---------------------------------------------------------------------------

const ATTN_STYLE = {
  interrupted: { icon: 'debug-stop', tone: () => C.amber },
  provider_error: { icon: 'cloud-offline', tone: () => C.red },
  subagent_failed: { icon: 'error', tone: () => C.red },
  approval_stall: { icon: 'watch', tone: () => C.amber },
  secret_exposure: { icon: 'key', tone: () => C.red },
  test_tamper: { icon: 'beaker', tone: () => C.red }
}

function AttentionRow({ item }) {
  const cfg = ATTN_STYLE[item.kind] || { icon: 'info', tone: () => C.slate }
  const tone = cfg.tone()
  return jsxs('div', {
    className: 'hday-attn flex items-start gap-2 px-2 py-1',
    style: { borderLeftColor: tone },
    children: [
      jsx(Codicon, { name: cfg.icon, size: 11, style: { color: tone, marginTop: '2px' } }),
      jsxs('div', {
        className: 'min-w-0 flex-1',
        children: [
          jsx('div', { className: 'truncate text-[0.68rem]', style: { color: C.text }, children: item.text || item.kind }),
          jsx('div', { className: 'text-[0.58rem]', style: { color: C.faint }, children: item.ts ? relativeTime(item.ts * 1000) : '' })
        ]
      })
    ]
  })
}

function AttentionStrip() {
  const evMap = useValue($evidence)
  const items = useMemo(() => {
    const all = []
    for (const [key, rec] of Object.entries(evMap || {})) {
      for (const it of (rec && rec.attn) || []) all.push({ ...it, key })
    }
    return all.sort((a, b) => (b.ts || 0) - (a.ts || 0)).slice(0, 4)
  }, [evMap])
  if (!items.length) return null
  return jsxs('div', {
    className: 'mb-2 overflow-hidden rounded border',
    style: { borderColor: C.border, background: C.surface },
    children: [
      jsxs('div', {
        className: 'flex items-center gap-1.5 px-2 py-1 text-[0.6rem] font-semibold uppercase tracking-wider',
        style: { color: C.amber },
        children: [jsx(Codicon, { name: 'alert', size: 10 }), `Attention · ${items.length}`]
      }),
      ...items.map((it, i) => jsx(AttentionRow, { item: it }, i))
    ]
  })
}

// ---------------------------------------------------------------------------
// Receipt — a verdict printed like a till slip
// ---------------------------------------------------------------------------

function ReceiptCard({ receipt, tamper }) {
  if (!receipt || !receipt.verdict) return null
  const flagged = receipt.verdict === 'flagged'
  const tone = flagged ? C.red : receipt.verdict === 'verified' ? C.emerald : C.amber
  const rows = [
    ['verdict', receipt.verdict],
    ['command', receipt.cmd ? String(receipt.cmd).slice(0, 46) : '—'],
    ['exit', receipt.exit === null || receipt.exit === undefined ? '—' : String(receipt.exit)],
    ['tree', receipt.tree ? String(receipt.tree).slice(0, 10) : '—'],
    ['checks run', String(receipt.runs == null ? 0 : receipt.runs)],
    ['guard blocks', String(receipt.blocks == null ? 0 : receipt.blocks)],
    ['suite damage', String(receipt.tamper == null ? 0 : receipt.tamper)]
  ]
  return jsxs('div', {
    className: `hday-receipt${flagged ? ' hday-hatch' : ''}`,
    style: { color: tone },
    children: [
      jsx('div', { className: 'mb-1 text-center font-semibold uppercase tracking-widest', children: 'evidence receipt' }),
      jsx('div', { className: 'hday-receipt-sep' }),
      ...rows.map(([k, v], i) => jsxs('div', {
        className: 'hday-receipt-row',
        children: [
          jsx('span', { style: { opacity: 0.7 }, children: k }),
          jsx('span', { className: 'truncate text-right', children: v })
        ]
      }, i)),
      jsx('div', { className: 'hday-receipt-sep' }),
      jsx('div', {
        className: 'text-center text-[0.6rem]',
        style: { opacity: 0.8 },
        children: receipt.ts ? new Date(receipt.ts * 1000).toLocaleString() : ''
      }),
      tamper && tamper.length
        ? jsx('div', {
            className: 'mt-1 text-center text-[0.6rem]',
            style: { color: C.red },
            children: tamper[0]
          })
        : null
    ]
  })
}

// HDAY-INTEGRATED: lane panel snippets spliced below (regenerated by integrate.py)


// ---- lane 01: ops-shell ----
// Lane 01 - Ops Shell: pinned command strip for the #/day screen.
// Splice-ready snippet: plain script (node --check clean), NO import/export,
// top-level function declarations only. Assumes the desktop plugin's module
// scope already provides: jsx, jsxs, Badge, Button, Input, Kbd, Loader,
// SearchField, cn, relativeTime, rpc, queryClient, isSubmitEnter, C,
// useQuery, the React hooks (useState/useEffect/useRef), and sourceName
// (plugin.js:253 route label helper).
// Colors: theme vars (var(--ui-*)) and the C token object ONLY - no hex/rgb/hsl.
// Reuses the injected .hday-* classes (card, row, chip, stat, receipt, shield,
// attn, hatch). Badge variants are the SDK's real set: default, muted,
// success, warn, destructive, outline, solid (badge.tsx).

// ---- exit-code badge (spec 4.1 table) -------------------------------------
function hdayExitBadge(run, busy) {
  if (busy) {
    return jsxs(Badge, { size: 'xs', variant: 'outline', children: [jsx(Loader, {}), 'running'] })
  }
  if (!run) return null
  // guard-blocked renders as a BLOCK (warn), never as a failure (destructive)
  if (run.blocked) return jsx(Badge, { size: 'xs', variant: 'warn', children: 'blocked' })
  if (run.dry) return jsx(Badge, { size: 'xs', variant: 'muted', children: 'dry-run' })
  if (run.timed_out) return jsx(Badge, { size: 'xs', variant: 'destructive', children: 'T/O 124' })
  var code = run.exit_code
  if (code === null || code === undefined) return jsx(Badge, { size: 'xs', variant: 'muted', children: 'no exit' })
  if (code === 0) return jsx(Badge, { size: 'xs', variant: 'muted', children: 'exit 0' })
  return jsx(Badge, { size: 'xs', variant: 'destructive', children: 'exit ' + String(code) })
}

// ---- route label (sourceName at plugin.js:253, with a safe fallback) ------
function hdayRouteLabel(route) {
  try {
    if (typeof sourceName === 'function') return String(sourceName(route))
  } catch (err) {
    // fall through to the local label
  }
  if (!route) return 'local'
  try {
    return String(route.targetProfile || route.profile || route.label || 'local')
  } catch (err2) {
    return 'local'
  }
}

function hdaySessionKey(row) {
  if (!row) return ''
  if (typeof row === 'string') return row
  try {
    return String(row.session_key || row.key || row.id || '')
  } catch (err) {
    return ''
  }
}

// ---- ExecPane: pure terminal-like output block (XrayBar pattern) ----------
function ExecPane(props) {
  var p = props || {}
  var run = p.run || null
  var busy = !!p.busy
  var outRef = useRef(null)
  useEffect(function () {
    try {
      var node = outRef.current
      if (node) node.scrollTop = node.scrollHeight
    } catch (err) {
      // scrolling is cosmetic; never let it throw
    }
  })
  if (!run && !busy) return null
  var echo = run ? '$ ' + String(run.cmd || '') : '$ ' + String(p.pendingCmd || '')
  var meta = run
    ? [run.host || 'local', run.session || '-', run.cwd || '-',
       (typeof run.ms === 'number' ? run.ms + 'ms' : '-'), run.at || ''].join('  \u00b7  ')
    : ''
  return jsxs('div', {
    className: 'hday-receipt',
    style: {
      background: (C && C.surface) || 'var(--ui-bg-primary)',
      border: '1px solid var(--ui-stroke-secondary)',
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
      padding: '6px',
      display: 'flex',
      flexDirection: 'column',
      gap: '4px'
    },
    children: [
      jsxs('div', { className: 'hday-row', style: { justifyContent: 'space-between' }, children: [
        jsxs('code', { style: { color: 'var(--ui-accent)' }, children: [echo] }),
        hdayExitBadge(run, busy)
      ] }),
      run && run.blocked
        ? jsxs('div', { className: 'hday-shield hday-row', children: [
            jsxs('span', { children: ['blocked by honesty guard'] }),
            jsx('span', { className: 'hday-chip', children: [String(run.reason || run.rule || '')] })
          ] })
        : null,
      jsxs('div', {
        ref: outRef,
        className: 'hday-stat',
        style: {
          maxHeight: '220px',
          overflow: 'auto',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
          color: 'var(--ui-text-primary)',
          fontFamily: 'inherit'
        },
        children: [
          run && run.stdout ? String(run.stdout) : '',
          run && run.stderr ? (run.stdout ? '\n' : '') + String(run.stderr) : '',
          !run && busy ? 'running\u2026' : ''
        ]
      }),
      meta
        ? jsxs('div', { className: 'hday-row', style: { color: 'var(--ui-text-tertiary)' }, children: [meta] })
        : null
    ]
  })
}

// ---- RunbookList: searchable history (SearchField) ------------------------
function RunbookList(props) {
  var p = props || {}
  var runs = p.runs || []
  var q = p.q || ''
  var onQuery = p.onQuery || function () {}
  var onPick = p.onPick || function () {}
  var wrapRef = p.wrapRef || null
  var pickedId = p.pickedId || ''
  return jsxs('div', { className: 'hday-card', style: { background: 'var(--ui-bg-primary)', border: '1px solid var(--ui-stroke-secondary)', padding: '6px', display: 'flex', flexDirection: 'column', gap: '4px' }, children: [
    jsxs('div', { className: 'hday-row', children: [
      jsx('span', { style: { color: 'var(--ui-text-tertiary)' }, children: ['runbook'] }),
      jsx('div', { ref: wrapRef, style: { flex: 1 }, children: jsx(SearchField, { value: q, onChange: onQuery, placeholder: 'search runs (/)' }) })
    ] }),
    runs.length === 0
      ? jsx('div', { className: 'hday-row hday-hatch hday-chip', children: ['no runs yet \u2014 run something'] })
      : jsxs('div', { children: runs.map(function (run) {
          var at = 0
          try { at = Date.parse(run.at) || 0 } catch (err) { at = 0 }
          var when = ''
          if (at > 0) {
            try { when = relativeTime(at) } catch (err2) { when = '' }
          }
          var cls = 'hday-row' + (run.id === pickedId ? ' hday-attn' : '')
          return jsxs('button', {
            className: cls,
            onClick: function () { onPick(run) },
            children: [
              jsxs('span', { style: { color: 'var(--ui-text-tertiary)' }, children: [when] }),
              jsxs('span', { style: { color: 'var(--ui-text-secondary)' }, children: [(run.host || 'local') + (run.session ? '\u00b7' + run.session : '')] }),
              jsxs('span', { style: { flex: 1, color: 'var(--ui-text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', textAlign: 'left' }, children: [String(run.cmd || '')] }),
              hdayExitBadge(run, false)
            ]
          }, String(run.id))
        }) })
  ] })
}

// ---- ExecShell: the lane root (target picker, input, modes, actions) ------
function ExecShell(props) {
  var p = props || {}
  var routes = p.routes || []
  var sessions = p.sessions || []
  var routeIdx = p.routeIdx || 0
  var sessionKey = p.sessionKey || ''
  var cmd = p.cmd || ''
  var dry = !!p.dry
  var busy = !!p.busy
  var notice = p.notice || null
  var execQ = p.execQ
  var runs = (execQ && execQ.data && execQ.data.runs) || []
  var inputWrapRef = p.inputWrapRef || null
  var searchWrapRef = p.searchWrapRef || null
  var selStyle = {
    background: (C && C.surface) || 'var(--ui-bg-primary)',
    color: 'var(--ui-text-primary)',
    border: '1px solid var(--ui-stroke-secondary)',
    borderRadius: '4px',
    padding: '2px 6px'
  }
  return jsxs('div', {
    className: 'hday-card',
    style: { background: 'var(--ui-bg-primary)', border: '1px solid var(--ui-stroke-secondary)', display: 'flex', flexDirection: 'column', gap: '6px', padding: '8px' },
    children: [
      jsxs('div', { className: 'hday-row', style: { justifyContent: 'space-between' }, children: [
        jsxs('span', { style: { color: 'var(--ui-text-secondary)', letterSpacing: '.08em' }, children: ['OPS SHELL'] }),
        jsxs('span', { className: 'hday-row', style: { color: 'var(--ui-text-tertiary)' }, children: [
          jsx(Kbd, { children: 'e' }), 'focus ',
          jsx(Kbd, { children: 'p' }), 'dry ',
          jsx(Kbd, { children: 'n' }), 'last ',
          jsx(Kbd, { children: 'f' }), 'failed ',
          jsx(Kbd, { children: '/' }), 'search'
        ] })
      ] }),
      jsxs('div', { className: 'hday-row', children: [
        jsx('select', {
          className: 'hday-chip',
          value: String(routeIdx),
          style: selStyle,
          onChange: function (ev) { p.onPickRoute && p.onPickRoute(Number(ev.target.value)) },
          children: routes.length === 0
            ? jsx('option', { value: '0', children: ['local'] })
            : routes.map(function (r, i) { return jsx('option', { value: String(i), children: [hdayRouteLabel(r)] }, String(i)) })
        }),
        jsx('select', {
          className: 'hday-chip',
          value: sessionKey,
          style: selStyle,
          onChange: function (ev) { p.onPickSession && p.onPickSession(ev.target.value) },
          children: sessions.length === 0
            ? jsx('option', { value: '', children: ['any session'] })
            : sessions.map(function (s, i) {
                var key = hdaySessionKey(s)
                var title = typeof s === 'string' ? s : String((s && (s.title || s.name)) || key)
                return jsx('option', { value: key, children: [title] }, String(i))
              })
        }),
        dry
          ? jsx('span', { className: 'hday-chip hday-attn', children: ['DRY'] })
          : jsx('span', { className: 'hday-chip', children: ['RUN'] })
      ] }),
      jsxs('div', { className: 'hday-row', children: [
        jsx('div', { ref: inputWrapRef, style: { flex: 1 }, children: jsx(Input, {
          value: cmd,
          onChange: function (ev) { p.onCmdChange && p.onCmdChange(ev && ev.target ? ev.target.value : '') },
          onKeyDown: function (ev) {
            try {
              if (isSubmitEnter && isSubmitEnter(ev)) {
                ev.preventDefault()
                p.onRun && p.onRun()
                return
              }
              if (ev.key === 'Escape') {
                ev.stopPropagation()
                if (ev.target && ev.target.blur) ev.target.blur()
                p.onCollapse && p.onCollapse()
              }
            } catch (err) {
              // never wedge the input
            }
          },
          placeholder: 'command\u2026  (Enter runs, Esc collapses)',
          disabled: !!busy
        }) }),
        jsx(Button, { size: 'xs', variant: 'ghost', onClick: function () { p.onToggleDry && p.onToggleDry() }, children: dry ? 'dry-run: on' : 'dry-run: off' }),
        jsx(Button, { size: 'xs', variant: 'secondary', onClick: function () { p.onRun && p.onRun() }, disabled: !!busy, children: 'Run' }),
        jsx(Button, { size: 'xs', variant: 'ghost', onClick: function () { p.onRerunLast && p.onRerunLast() }, disabled: !!busy, children: 'Re-run last' }),
        jsx(Button, { size: 'xs', variant: 'ghost', onClick: function () { p.onRerunFailed && p.onRerunFailed() }, disabled: !!busy, children: 'Re-run failed' })
      ] }),
      notice
        ? jsxs('div', { className: notice.kind === 'blocked' ? 'hday-shield' : 'hday-attn', children: [String(notice.text)] })
        : null,
      jsx(ExecPane, { run: p.run, busy: busy, pendingCmd: cmd }),
      jsx(RunbookList, {
        runs: runs,
        q: p.q || '',
        onQuery: p.onQuery,
        onPick: p.onPick,
        wrapRef: searchWrapRef,
        pickedId: p.run ? String(p.run.id) : ''
      })
    ]
  })
}

// ---- hdayPanel01: state, dispatch, keybindings, render --------------------
function hdayPanel01(props) {
  var p = props || {}
  var routes = p.routes || []
  var activeRoute = p.route || p.activeRoute || (routes.length ? routes[0] : null)
  var inputWrapRef = useRef(null)
  var searchWrapRef = useRef(null)
  var actionsRef = useRef(null)
  var [routeIdx, setRouteIdx] = useState(0)
  var [sessionKey, setSessionKey] = useState('')
  var [cmd, setCmd] = useState('')
  var [dry, setDry] = useState(false)
  var [busy, setBusy] = useState(false)
  var [run, setRun] = useState(null)
  var [q, setQ] = useState('')
  var [notice, setNotice] = useState(null)

  var targetRoute = routes.length ? (routes[routeIdx] || activeRoute) : activeRoute
  var hostLabel = hdayRouteLabel(targetRoute)

  // history query - InstinctsDrawer pattern (plugin.js:2109-2118)
  var execQ = useQuery({
    queryKey: ['hermes-day', 'exec', String(q), String(sessionKey)],
    queryFn: async function () {
      try {
        var arg = JSON.stringify({ op: 'history', session: sessionKey, host: hostLabel, q: q, limit: 50 })
        var disp = await rpc(targetRoute, 'command.dispatch', { name: 'day-exec', arg: arg })
        var out = disp && (disp.output || disp.text || '')
        if (out && String(out).trim().startsWith('{')) {
          var parsed = JSON.parse(out)
          if (parsed && parsed.ok) return parsed
        }
      } catch (err) {
        // fall through to the empty payload below
      }
      return { ok: false, runs: [], count: 0, limit: 50, q: String(q) }
    },
    refetchInterval: 5000,
    staleTime: 1500
  })

  var send = function (payload) {
    setBusy(true)
    return (async function () {
      try {
        var disp = await rpc(targetRoute, 'command.dispatch', { name: 'day-exec', arg: JSON.stringify(payload) })
        var out = disp && (disp.output || disp.text || '')
        var res = null
        try {
          res = out && String(out).trim().startsWith('{') ? JSON.parse(out) : null
        } catch (err) {
          res = null
        }
        if (res && res.run) {
          // includes guard-blocked: ok=false BUT a run object -> render as block
          setRun(res.run)
          if (res.error === 'guard_blocked') {
            setNotice({ kind: 'blocked', text: 'blocked: ' + String(res.reason || '') + (res.rule ? ' (' + String(res.rule) + ')' : '') })
          } else {
            setNotice(null)
          }
        } else {
          setNotice({ kind: 'error', text: res ? String(res.error || res.detail || 'error') : 'dispatch failed' })
        }
        try {
          if (queryClient && queryClient.invalidateQueries) queryClient.invalidateQueries({ queryKey: ['hermes-day', 'exec'] })
        } catch (err2) {
          // cache refresh is best-effort
        }
      } catch (err) {
        setNotice({ kind: 'error', text: String((err && err.message) || err) })
      } finally {
        setBusy(false)
      }
    })()
  }

  var baseReq = function (op, extra) {
    var req = { op: op, session: sessionKey, host: hostLabel, cmd: '', dry: !!dry, cwd: '', q: '', limit: 50 }
    if (extra) { for (var k in extra) { if (Object.prototype.hasOwnProperty.call(extra, k)) req[k] = extra[k] } }
    return req
  }

  var focusInput = function () {
    try {
      var node = inputWrapRef.current && inputWrapRef.current.querySelector ? inputWrapRef.current.querySelector('input') : null
      if (node && node.focus) node.focus()
    } catch (err) {
      // focus is cosmetic
    }
  }
  var focusSearch = function () {
    try {
      var node = searchWrapRef.current && searchWrapRef.current.querySelector ? searchWrapRef.current.querySelector('input') : null
      if (node && node.focus) node.focus()
    } catch (err) {
      // focus is cosmetic
    }
  }
  var runNow = function () {
    if (busy) return
    if (!String(cmd).trim()) {
      setNotice({ kind: 'error', text: 'type a command first (e to focus)' })
      return
    }
    setNotice(null)
    send(baseReq('run', { cmd: String(cmd) }))
  }
  var rerunLast = function () {
    if (busy) return
    setNotice(null)
    send(baseReq('rerun-last'))
  }
  var rerunFailed = function () {
    if (busy) return
    setNotice(null)
    send(baseReq('rerun-failed'))
  }
  var toggleDry = function () {
    setDry(function (was) { return !was })
  }

  // keep the key handler's closures fresh without re-binding every render
  useEffect(function () {
    actionsRef.current = {
      focusInput: focusInput,
      focusSearch: focusSearch,
      runNow: runNow,
      rerunLast: rerunLast,
      rerunFailed: rerunFailed,
      toggleDry: toggleDry
    }
  })

  // e / n / f / p / '/' - verified free against plugin.js:3032-3052
  // (bound today: j, k, ArrowDown, ArrowUp, a, d, s, o, Enter, Escape).
  // Same input guard as the global handler, so nothing fires while typing.
  useEffect(function () {
    if (typeof window === 'undefined' || !window.addEventListener) return undefined
    var handler = function (ev) {
      try {
        if (ev.defaultPrevented) return
        if (ev.metaKey || ev.ctrlKey || ev.altKey) return
        var t = ev.target
        var tag = t && t.tagName ? String(t.tagName).toUpperCase() : ''
        if (tag === 'INPUT' || tag === 'TEXTAREA' || (t && t.isContentEditable)) return
        var act = actionsRef.current
        if (!act) return
        var k = ev.key
        if (k === 'e') { ev.preventDefault(); act.focusInput() }
        else if (k === 'p') { ev.preventDefault(); act.toggleDry() }
        else if (k === 'n') { ev.preventDefault(); act.rerunLast() }
        else if (k === 'f') { ev.preventDefault(); act.rerunFailed() }
        else if (k === '/') { ev.preventDefault(); ev.stopPropagation(); act.focusSearch() }
      } catch (err) {
        // a key handler must never wedge the app
      }
    }
    window.addEventListener('keydown', handler)
    return function () {
      try { window.removeEventListener('keydown', handler) } catch (err) { /* ignore */ }
    }
  }, [])

  return jsxs('div', {
    className: 'hday-card',
    style: { background: 'var(--ui-bg-primary)', border: '1px solid var(--ui-stroke-secondary)', padding: '6px' },
    children: [
      jsx(ExecShell, {
        routes: routes,
        sessions: p.sessions || [],
        routeIdx: routeIdx,
        sessionKey: sessionKey,
        cmd: cmd,
        dry: dry,
        busy: busy,
        run: run,
        q: q,
        notice: notice,
        execQ: execQ,
        inputWrapRef: inputWrapRef,
        searchWrapRef: searchWrapRef,
        onPickRoute: function (idx) { setRouteIdx(idx) },
        onPickSession: function (key) { setSessionKey(key) },
        onCmdChange: function (val) { setCmd(val) },
        onToggleDry: toggleDry,
        onRun: runNow,
        onRerunLast: rerunLast,
        onRerunFailed: rerunFailed,
        onCollapse: function () { setRun(null) },
        onQuery: function (val) { setQ(val) },
        onPick: function (picked) { if (picked) setRun(picked) }
      })
    ]
  })
}

// ---- lane 02: vault-panel ----
// Lane 02 — Vault & Redaction panel (first draft, splice-ready snippet).
// SAFETY: this half only ever renders LABELS and counts. The backend seals
// every /day-vault and /day-redact payload through its four credential
// regexes, so no secret byte can appear here even if a payload tried to
// carry one; nothing in this panel writes to state or disk.

function hdayPanel02(props) {
  const route = (props && props.route) || ''
  const sid = (props && props.sessionId) || ''
  const [filter, setFilter] = useState('')
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState('')
  const [rotate, setRotate] = useState(null)

  // live read: exposure ledger per session (labels only)
  const vaultQ = useQuery({
    queryKey: ['hday-vault', sid],
    queryFn: async function () {
      try {
        const disp = await rpc(route, 'command.dispatch',
          { name: 'day-vault', arg: sid })
        const out = disp && (disp.output || disp.text || '')
        if (out && String(out).trim().charAt(0) === '{') return JSON.parse(out)
        return { ok: false, error: 'no response' }
      } catch (e) {
        return { ok: false, error: String((e && e.message) || e) }
      }
    },
    refetchInterval: 5000,
    staleTime: 2000
  })

  const data = (vaultQ && vaultQ.data) || null
  const windowN = (data && data.window) || 40
  const inWindow = (data && data.totals && data.totals.in_window) || 0
  const allSessions = (data && data.sessions) || []
  const needle = String(filter || '').trim().toLowerCase()

  const sessions = allSessions
    .map(function (s) {
      const exps = (s.exposures || []).filter(function (x) {
        if (!needle) return true
        const hay = (String(x.tool || '') + ' ' +
          (x.signs || []).join(' ') + ' ' +
          String(s.session || '')).toLowerCase()
        return hay.indexOf(needle) !== -1
      })
      return {
        session: String(s.session || ''),
        in_window: s.in_window || 0,
        attn_window: s.attn_window || 0,
        exposures: exps
      }
    })
    .filter(function (s) { return !needle || s.exposures.length > 0 })

  async function dispatch(name, arg) {
    try {
      const disp = await rpc(route, 'command.dispatch',
        { name: name, arg: arg || '' })
      const out = disp && (disp.output || disp.text || '')
      if (out && String(out).trim().charAt(0) === '{') return JSON.parse(out)
      return { ok: false, error: 'no response' }
    } catch (e) {
      return { ok: false, error: String((e && e.message) || e) }
    }
  }

  // in-place scrub; the receipt below shows labels and counts only
  async function redact(sKey) {
    setBusy('day-redact')
    setStatus('scrubbing ' + (sKey || 'all sessions') + '…')
    const res = await dispatch('day-redact', sKey)
    setBusy('')
    if (res && res.ok) {
      const parts = (res.sessions || []).map(function (r) {
        const labels = Object.keys(r.scrubbed || {}).map(function (k) {
          return k + '×' + r.scrubbed[k]
        })
        return String(r.session) + ': ' + (labels.length ? labels.join(', ') : 'clean')
      })
      setStatus('redacted (labels only) — ' +
        (parts.join(' | ') || 'nothing stored'))
      try { queryClient.invalidateQueries({ queryKey: ['hday-vault'] }) } catch (e) {}
    } else {
      setStatus('redact failed: ' + ((res && res.error) || 'unknown'))
    }
  }

  // one-key rotation guidance — the backend never executes shell for this
  async function rotateNow() {
    setBusy('day-rotate')
    const res = await dispatch('day-rotate', '')
    setBusy('')
    if (res && res.ok) {
      setRotate(res)
      setStatus('rotation guidance loaded — nothing was executed')
    } else {
      setStatus('rotate guidance failed: ' + ((res && res.error) || 'unknown'))
    }
  }

  const shieldLabel = 'secret_exposure entries currently inside the per-session ' +
    '_MAX_ATTN=' + windowN + ' attention ledger — a live window, not a lifetime count'

  return jsx('div', {
    className: 'flex flex-col gap-2',
    children: [

      // ---- header: live secrets-in-context counter (a 40-entry window) ----
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        key: 'head',
        children: [
          jsx(Tip, {
            key: 'tip',
            label: shieldLabel,
            children: jsx('span', {
              className: inWindow > 0 ? 'hday-shield text-destructive' : 'hday-shield',
              style: inWindow > 0 ? null : { color: C.faint, borderColor: C.border },
              children: [
                jsx(Codicon, { name: 'shield', size: 11, key: 'i' }),
                'secrets in context: ' + inWindow + '/' + windowN + ' window'
              ]
            })
          }),
          jsx(Badge, { key: 'b', tone: 'info' }, 'vault'),
          jsx('span', {
            key: 'wl',
            className: 'text-[0.58rem]',
            style: { color: C.faint },
            children: 'window ≠ lifetime · labels only'
          }),
          jsx('span', { key: 'sp', className: 'ml-auto' }),
          jsx(SearchField, {
            key: 'f',
            placeholder: 'Filter tool or sign…',
            value: filter,
            onChange: setFilter,
            containerClassName: 'w-40'
          })
        ]
      }),

      // ---- loading / error ----
      !data
        ? jsx('div', {
            key: 'load',
            className: 'hday-receipt',
            style: { color: C.faint },
            children: (vaultQ && vaultQ.error)
              ? 'vault read failed — retrying'
              : 'scanning the attention ledger…'
          })
        : data.ok === false
          ? jsx('div', {
              key: 'err',
              className: 'hday-receipt hday-hatch',
              style: { color: C.faint },
              children: 'vault unavailable: ' + (data.error || 'unknown')
            })
          : sessions.length === 0
            ? jsx('div', {
                key: 'empty',
                className: 'hday-receipt',
                style: { color: C.faint },
                children: needle
                  ? 'no exposure matches "' + filter + '"'
                  : 'no credential-shaped strings in any session window'
              })
            : sessions.map(function (s) {
                return jsxs('div', {
                  className: 'hday-card p-2 flex flex-col gap-1',
                  style: { background: C.surface, border: '1px solid ' + C.border },
                  children: [
                    // session header + per-session redact action
                    jsxs('div', {
                      className: 'hday-row flex items-center gap-2 px-1',
                      key: 'h',
                      children: [
                        jsx(Codicon, { name: 'shield', size: 11, key: 'ic' }),
                        jsx('span', {
                          className: 'font-mono text-[0.62rem] truncate',
                          style: { color: C.muted },
                          children: s.session
                        }, 'k'),
                        jsx(Badge, { key: 'w', tone: 'info' },
                          String(s.in_window) + ' in window'),
                        jsx('span', {
                          key: 'lw',
                          className: 'ml-auto text-[0.55rem] tabular-nums',
                          style: { color: C.faint },
                          children: 'ledger ' + s.attn_window + '/' + windowN
                        }),
                        jsx(Button, {
                          key: 'r',
                          onClick: function () { redact(s.session) },
                          disabled: busy === 'day-redact'
                        }, 'Redact')
                      ]
                    }),
                    // exposure rows: tool · time · sign labels
                    s.exposures.length === 0
                      ? jsx('div', {
                          key: 'z',
                          className: 'px-1 text-[0.6rem]',
                          style: { color: C.faint },
                          children: 'no exposures match the filter'
                        })
                      : s.exposures.map(function (x, i) {
                          return jsxs('div', {
                            className: 'hday-attn hday-hatch flex items-center gap-2 px-2 py-1',
                            key: 'x' + i,
                            children: [
                              jsx(Codicon, { name: 'shield', size: 11, key: 'i' }),
                              jsx('span', {
                                className: 'font-mono text-[0.62rem] truncate',
                                style: { color: C.muted },
                                children: x.tool || 'unknown tool'
                              }, 't'),
                              (x.signs || []).map(function (sg, j) {
                                return jsx('span', {
                                  className: 'hday-chip rounded border px-1.5 py-px font-mono text-[0.6rem]',
                                  style: { borderColor: C.border, color: C.muted },
                                  children: sg
                                }, 's' + j)
                              }),
                              jsx('span', {
                                key: 'ts',
                                className: 'ml-auto shrink-0 text-[0.58rem] tabular-nums',
                                style: { color: C.faint },
                                children: x.ts ? relativeTime(x.ts * 1000) : ''
                              })
                            ]
                          })
                        })
                  ]
                }, 'sess-' + s.session)
              }),

      // ---- actions: redact + one-key rotate guidance ----
      jsxs('div', {
        className: 'hday-row flex flex-wrap items-center gap-2 px-1',
        key: 'act',
        children: [
          jsx(Button, {
            key: 'ra',
            onClick: function () { redact(sid) },
            disabled: busy === 'day-redact'
          }, sid ? 'Redact this session' : 'Redact all sessions'),
          jsx(Button, {
            key: 'rg',
            onClick: rotateNow,
            disabled: busy === 'day-rotate'
          }, 'Rotation guidance (one key)'),
          rotate
            ? jsx(Button, {
                key: 'rh',
                onClick: function () { setRotate(null) }
              }, 'Hide guidance')
            : null,
          jsx('span', {
            key: 'bs',
            className: 'ml-auto text-[0.58rem]',
            style: { color: C.faint },
            children: busy ? busy + '…' : ''
          })
        ]
      }),

      // ---- action receipt: labels and counts only ----
      status
        ? jsx('div', {
            key: 'st',
            className: 'hday-receipt',
            style: { color: C.faint },
            children: status
          })
        : null,

      // ---- rotation guidance receipt (static, nothing executed) ----
      rotate
        ? jsxs('div', {
            key: 'rot',
            className: 'hday-receipt',
            children: [
              jsx('div', {
                className: 'mb-1 text-center font-semibold uppercase tracking-widest',
                children: 'rotate — manual, nothing executed'
              }),
              jsx('div', { className: 'hday-receipt-sep' }),
              (rotate.general || []).map(function (g, i) {
                return jsxs('div', {
                  className: 'hday-receipt-row',
                  key: 'g' + i,
                  children: [
                    jsx('span', { style: { opacity: 0.7 }, children: 'general' }),
                    jsx('span', { className: 'text-right', children: g })
                  ]
                })
              }),
              jsx('div', { className: 'hday-receipt-sep' }),
              (rotate.by_sign || []).map(function (grp, i) {
                return jsxs('div', {
                  className: 'mb-1',
                  key: 'b' + i,
                  children: [
                    jsx('div', {
                      className: 'font-semibold uppercase',
                      children: grp.sign
                    }),
                    (grp.steps || []).map(function (step, j) {
                      return jsxs('div', {
                        className: 'hday-receipt-row',
                        key: 'st' + j,
                        children: [
                          jsx('span', { style: { opacity: 0.7 }, children: 'step' }),
                          jsx('span', { className: 'text-right', children: step })
                        ]
                      })
                    })
                  ]
                })
              }),
              jsx('div', { className: 'hday-receipt-sep' }),
              jsx('div', {
                className: 'text-center text-[0.6rem]',
                style: { opacity: 0.8 },
                children: rotate.note || ''
              })
            ]
          })
        : null
    ]
  })
}

// ---- lane 03: service-wall ----
// ---------------------------------------------------------------------------
// lane 03 — Service Wall: live watchdog over the systemd --user units Hermes runs
// splice-ready snippet: plain script (no import/export), top-level functions only,
// jsx()/jsxs() calls, theme tokens from `C` only — never a hardcoded colour.
// ---------------------------------------------------------------------------

/** degraded-but-complete envelope — every key, services:[] (spec §4) */
function wallDegraded(reason) {
  return {
    ok: true,
    available: false,
    scanned_at: Date.now() / 1000,
    probe: { bin: null, scope: '--user', systemd: null, elapsed_ms: 0, reason: reason || null },
    watch: [],
    services: [],
    alerts: [],
    counts: { ok: 0, warn: 0, crit: 0, off: 0 },
    restart: null
  }
}

/** read: command.dispatch -> JSON envelope; any failure degrades, never throws */
async function wallLoad(route) {
  try {
    const disp = await rpc(route, 'command.dispatch', { name: 'day-services', arg: '' })
    const out = disp && (disp.output || disp.text || '')
    const parsed = out && String(out).trim().charAt(0) === '{' ? JSON.parse(out) : null
    if (parsed && Array.isArray(parsed.services)) return parsed
    return wallDegraded('unparseable_response')
  } catch (e) {
    return wallDegraded('dispatch_failed')
  }
}

/** mutate: allowlisted restart -> receipt envelope; any failure degrades, never throws */
async function wallRestart(route, unit) {
  try {
    const disp = await rpc(route, 'command.dispatch', { name: 'day-service-restart', arg: unit })
    const out = disp && (disp.output || disp.text || '')
    const parsed = out && String(out).trim().charAt(0) === '{' ? JSON.parse(out) : null
    return parsed || { ok: false, reason: 'unparseable_response' }
  } catch (e) {
    return { ok: false, reason: String((e && e.message) || e) }
  }
}

function wallSev(verdict) {
  if (verdict === 'failed' || verdict === 'down') return 'crit'
  if (verdict === 'restarting' || verdict === 'drifted' || verdict === 'degraded' || verdict === 'unknown') return 'warn'
  if (verdict === 'off') return 'off'
  return 'ok'
}

function wallOrder(sev) {
  if (sev === 'crit') return 0
  if (sev === 'warn') return 1
  if (sev === 'off') return 2
  return 3
}

/** tones come from the token object C — no literal colours in this file */
function wallTone(sev) {
  if (sev === 'crit') return C.red
  if (sev === 'warn') return C.amber
  if (sev === 'off') return C.slate
  return C.emerald
}

function wallIcon(sev) {
  if (sev === 'crit') return 'error'
  if (sev === 'warn') return 'warning'
  if (sev === 'off') return 'circle-slash'
  return 'check'
}

function wallUptime(sec) {
  if (sec === null || sec === undefined || !isFinite(sec) || sec < 0) return '—'
  const s = Math.floor(sec)
  if (s < 60) return s + 's'
  const m = Math.floor(s / 60)
  if (m < 60) return m + 'm'
  const h = Math.floor(m / 60)
  if (h < 24) return h + 'h ' + String(m % 60).padStart(2, '0') + 'm'
  return Math.floor(h / 24) + 'd ' + (h % 24) + 'h'
}

function wallGrid() {
  return {
    display: 'grid',
    gridTemplateColumns: 'minmax(0, 2.6fr) 8.5rem 4.5rem 5.5rem minmax(0, 1fr) 6rem',
    gap: '0.6rem',
    alignItems: 'center'
  }
}

function wallHead(text) {
  return jsx('span', {
    className: 'truncate text-[0.6rem] font-semibold uppercase tracking-[0.12em]',
    style: { color: C.faint },
    children: text
  })
}

/** step 2 of the two-step inline confirm (no modal, no window.confirm) */
function wallConfirmBits(unit, busy, onGo, onCancel) {
  return jsxs('span', {
    className: 'inline-flex shrink-0 items-center gap-1.5',
    children: [
      jsx('span', {
        className: 'text-[0.7rem]',
        style: { color: C.muted },
        children: 'Restart ' + String(unit) + '?'
      }),
      jsx(Button, {
        size: 'xs',
        variant: 'default',
        disabled: Boolean(busy),
        onClick: () => onGo(unit),
        children: busy ? '…' : 'Yes'
      }),
      jsx(Button, {
        size: 'xs',
        variant: 'ghost',
        disabled: Boolean(busy),
        autoFocus: true,
        onClick: () => onCancel(),
        children: 'No'
      }),
      jsxs('span', {
        className: 'flex items-center gap-1 text-[0.66rem]',
        style: { color: C.faint },
        children: [jsx(Kbd, { children: 'Esc' }), 'cancel']
      })
    ]
  })
}

/** pinned alert row — drifted/restarting/failed, only watchlisted units are actionable */
function WallAlerts03(p) {
  const alerts = p.alerts || []
  if (!alerts.length) return null
  const crit = alerts.some(a => (a.sev || 'warn') === 'crit')
  const tone = crit ? C.red : C.amber
  return jsxs('div', {
    className: 'hday-attn mb-2 rounded-md px-2.5 py-1.5',
    style: { background: C.surface, border: '1px solid ' + C.border, borderLeft: '2px solid ' + tone },
    children: [
      jsxs('div', {
        className: 'mb-1 flex items-center gap-2 text-[0.7rem] font-semibold uppercase tracking-[0.1em]',
        style: { color: tone },
        children: [jsx(Codicon, { name: 'warning', size: 12 }), String(alerts.length) + ' need attention']
      }),
      alerts.map((a, i) => {
        const watched = (p.watch || []).indexOf(a.unit) >= 0
        const confirming = p.confirming === a.unit
        const note = p.note && p.note.unit === a.unit ? p.note : null
        return jsxs('div', {
          className: 'flex min-w-0 items-center gap-2 py-0.5',
          onKeyDown: confirming
            ? ev => {
                if (ev.key === 'Escape') {
                  ev.stopPropagation()
                  p.onCancel()
                }
              }
            : undefined,
          children: [
            jsx('span', {
              className: 'min-w-0 flex-1 truncate text-[0.72rem]',
              style: { color: C.text },
              title: String(a.unit || ''),
              children: a.unit
            }),
            jsx('span', {
              className: 'hidden shrink-0 text-[0.68rem] sm:inline',
              style: { color: (a.sev || 'warn') === 'crit' ? C.red : C.amber },
              title: String(a.kind || ''),
              children: a.text
            }),
            note
              ? jsx('span', {
                  className: 'shrink-0 text-[0.68rem]',
                  style: { color: note.bad ? C.red : C.emerald },
                  children: note.text
                })
              : null,
            watched
              ? confirming
                ? wallConfirmBits(a.unit, p.busy, p.onGo, p.onCancel)
                : jsx(Button, {
                    size: 'xs',
                    variant: 'outline',
                    disabled: Boolean(p.busy),
                    onClick: () => p.onAsk(a.unit),
                    children: 'Restart'
                  })
              : jsx('span', {
                  className: 'shrink-0 text-[0.66rem]',
                  style: { color: C.faint },
                  children: 'not watchlisted: no action'
                })
          ]
        }, String(a.unit) + '#' + i)
      })
    ]
  })
}

/** one unit: dot+name · verdict chip · uptime · restarts+spark · since · action */
function ServiceRow03(p) {
  const s = p.s || {}
  const sev = wallSev(s.verdict)
  const tone = wallTone(sev)
  const watched = Boolean(p.watched)
  const busy = p.busy === s.unit
  const confirming = p.confirming === s.unit
  const note = p.note && p.note.unit === s.unit ? p.note : null
  const startedMs =
    typeof s.uptime_s === 'number'
      ? Date.now() - s.uptime_s * 1000
      : typeof s.last_fire_ago_s === 'number'
        ? Date.now() - s.last_fire_ago_s * 1000
        : 0
  const action = confirming
    ? null
    : watched && !busy
      ? jsx(Button, {
          size: 'xs',
          variant: 'outline',
          disabled: Boolean(p.busy),
          onClick: () => p.onAsk(s.unit),
          children: 'Restart'
        })
      : busy
        ? jsx('span', { className: 'text-[0.68rem]', style: { color: C.faint }, children: 'restarting…' })
        : null

  return jsxs('div', {
    className: 'hday-row',
    style: p.idx ? { borderTop: '1px solid ' + C.border } : undefined,
    children: [
      jsxs('div', {
        className: 'px-3 py-1.5',
        style: wallGrid(),
        children: [
          jsxs('span', {
            className: 'flex min-w-0 items-center gap-2',
            children: [
              jsx('span', {
                className: 'size-1.5 shrink-0 rounded-full',
                style: { background: tone }
              }),
              jsx('span', {
                className: 'truncate text-[0.78rem] font-medium',
                style: { color: C.text },
                title: String(s.unit || ''),
                children: s.unit || '—'
              })
            ]
          }),
          jsx('span', {
            className: 'hday-chip inline-flex w-max items-center gap-1 rounded-[3px] px-1.5 py-px text-[0.62rem] font-semibold uppercase tracking-wide',
            style: { color: tone, background: tone + '1f', border: '1px solid ' + tone + '55' },
            title: String(s.verdict_why || s.verdict || ''),
            children: [jsx(Codicon, { name: wallIcon(sev), size: 11 }), String(s.verdict || 'unknown')]
          }),
          jsx('span', {
            className: 'truncate text-[0.72rem] tabular-nums',
            style: { color: C.muted },
            title: s.uptime_s == null ? (s.timer ? 'idle — ' + s.timer : 'not running') : 'uptime',
            children: wallUptime(s.uptime_s)
          }),
          jsxs('span', {
            className: 'flex items-center gap-1.5',
            title: 'NRestarts' + (s.timer ? ' · timer ' + s.timer : ''),
            children: [
              jsx('span', {
                className: 'text-[0.72rem] tabular-nums',
                style: { color: (s.n_restarts || 0) > 0 ? C.amber : C.muted },
                children: s.n_restarts == null ? '—' : String(s.n_restarts)
              }),
              jsx(Spark, { seqs: s.spark || [] })
            ]
          }),
          jsx('span', {
            className: 'truncate text-[0.68rem] tabular-nums',
            style: { color: C.faint },
            title: String(s.started_at || ''),
            children: startedMs ? relativeTime(startedMs) : '—'
          }),
          jsx('span', { className: 'flex items-center justify-end', children: action })
        ]
      }),
      confirming
        ? jsxs('div', {
            className: 'flex flex-wrap items-center gap-2 px-3 pb-1.5 pt-0.5',
            style: { borderTop: '1px solid ' + C.border },
            onKeyDown: ev => {
              if (ev.key === 'Escape') {
                ev.stopPropagation()
                p.onCancel()
              }
            },
            children: [
              jsx(Codicon, { name: 'warning', size: 12, style: { color: C.amber } }),
              wallConfirmBits(s.unit, p.busy, p.onGo, p.onCancel)
            ]
          })
        : null,
      note
        ? jsx('div', {
            className: 'px-3 pb-1.5 text-[0.68rem]',
            style: { color: note.bad ? C.red : C.emerald },
            children: note.text
          })
        : null
    ]
  })
}

/** Service Wall section — render between hday-flight and hday-finished (spec §3.0) */
function hdayPanel03(props) {
  const route = props && props.route ? props.route : null
  const [busy, setBusy] = useState('')
  const [confirming, setConfirming] = useState('')
  const [note, setNote] = useState(null)

  const wallQ = useQuery({
    queryKey: ['hermes-day', 'services'],
    queryFn: () => wallLoad(route),
    refetchInterval: 15000,
    staleTime: 5000,
    retry: false,
    refetchOnWindowFocus: true
  })

  // mirror of the existing cron query (same key/shape as DayPage's CRON_QK)
  const cronQ = useQuery({
    queryKey: ['hermes-day', 'cron'],
    queryFn: scanCron,
    enabled: !(props && props.cron),
    refetchInterval: 30000,
    staleTime: 10000,
    retry: false
  })

  const data = (wallQ && wallQ.data) || wallDegraded('loading')
  const services = Array.isArray(data.services) ? data.services : []
  const alerts = Array.isArray(data.alerts) ? data.alerts : []
  const watch = Array.isArray(data.watch) ? data.watch : []
  const counts = data.counts || { ok: 0, warn: 0, crit: 0, off: 0 }
  const probe = data.probe || { reason: null }
  const total = services.length

  let cronData = null
  if (props && props.cron) {
    const pc = props.cron
    if (pc && Array.isArray(pc.jobs)) cronData = pc
    else if (pc && pc.data && Array.isArray(pc.data.jobs)) cronData = pc.data
  }
  const jobs = (cronData || (cronQ && cronQ.data) || { jobs: [] }).jobs || []

  const rows = services.slice().sort((a, b) => {
    const oa = wallOrder(wallSev(a.verdict))
    const ob = wallOrder(wallSev(b.verdict))
    if (oa !== ob) return oa - ob
    return String(a.unit || '').localeCompare(String(b.unit || ''))
  })

  const headerTone = counts.crit ? C.red : counts.warn ? C.amber : C.emerald

  const ask = unit => {
    setConfirming(unit)
    setNote(null)
  }
  const cancel = () => setConfirming('')

  const go = async unit => {
    if (busy) return
    setBusy(unit)
    setConfirming('')
    const rec = await wallRestart(route, unit)
    if (rec && rec.ok) {
      const ms = typeof rec.elapsed_ms === 'number' ? rec.elapsed_ms : null
      setNote({
        unit,
        text: ms != null ? 'restarted in ' + (ms / 1000).toFixed(1) + ' s' : 'restarted',
        bad: false
      })
    } else {
      const why = (rec && rec.reason) || 'failed'
      const detail = rec && rec.detail ? ' — ' + rec.detail : ''
      setNote({ unit, text: why + detail, bad: true })
    }
    setBusy('')
    try {
      queryClient.invalidateQueries({ queryKey: ['hermes-day'] })
    } catch (e) {}
    try {
      if (wallQ && wallQ.refetch) wallQ.refetch()
    } catch (e) {}
    setTimeout(() => {
      setNote(cur => (cur && cur.unit === unit ? null : cur))
    }, 8000)
  }

  return jsx('div', {
    id: 'hday-wall',
    className: 'hday-wall mb-4',
    children: [
      jsx(SectionLabel, { icon: 'watch', title: 'Service Wall', count: total, color: headerTone }),

      probe.reason && total
        ? jsx('div', {
            className: 'px-1 pb-1.5 text-[0.66rem]',
            style: { color: C.amber },
            children: 'partial data — ' + String(probe.reason)
          })
        : null,

      alerts.length
        ? jsx(WallAlerts03, {
            alerts,
            watch,
            busy,
            confirming,
            note,
            onAsk: ask,
            onCancel: cancel,
            onGo: go
          })
        : null,

      total
        ? jsxs('div', {
            className: 'overflow-hidden rounded-lg',
            style: { background: C.surface, border: '1px solid ' + C.border },
            children: [
              jsxs('div', {
                className: 'px-3 py-1.5',
                style: { ...wallGrid(), borderBottom: '1px solid ' + C.border },
                children: [
                  wallHead('Unit'),
                  wallHead('State'),
                  wallHead('Uptime'),
                  wallHead('Restarts'),
                  wallHead('Since'),
                  wallHead('Action')
                ]
              }),
              rows.map((s, i) =>
                jsx(
                  ServiceRow03,
                  {
                    s,
                    idx: i,
                    watched: watch.indexOf(s.unit) >= 0,
                    busy,
                    confirming,
                    note,
                    onAsk: ask,
                    onCancel: cancel,
                    onGo: go
                  },
                  String(s.unit || i)
                )
              )
            ]
          })
        : jsxs('div', {
            className: 'rounded-lg px-3 py-5 text-center',
            style: { background: C.surface, border: '1px solid ' + C.border },
            children: [
              jsx('div', {
                className: 'text-[0.78rem] font-medium',
                style: { color: C.muted },
                children: 'Service data unavailable on this host.'
              }),
              probe.reason
                ? jsx('div', {
                    className: 'mt-1 text-[0.68rem]',
                    style: { color: C.faint },
                    children: String(probe.reason)
                  })
                : null
            ]
          }),

      jsxs('div', {
        className: 'mt-3',
        children: [
          jsx(SectionLabel, { icon: 'calendar', title: 'Next scheduled' }),
          jobs.length
            ? jsx(CronRow, { entry: jobs[0] }, String((jobs[0] && jobs[0].key) || 'next'))
            : jsx('div', {
                className: 'px-2 py-1 text-[0.72rem]',
                style: { color: C.faint },
                children: 'No scheduled jobs.'
              })
        ]
      })
    ]
  })
}

// ---- lane 04: network-radar ----
/**
 * Lane 04 — Network Radar panel (splice-ready snippet, NOT a module).
 *
 * Splice into desktop/plugin.js. No import/export: it parses as a plain script
 * and leans on symbols already in module scope (jsx/jsxs, Badge, Button,
 * Codicon, Kbd, Tip, atom, useValue, useQuery, cn, relativeTime, host, rpc, C)
 * plus the React hooks the bundle already imports.
 *
 * Data comes from `/day-net` through the existing poll pattern:
 *   rpc(route, 'command.dispatch', { name: 'day-net', arg })
 *
 * Honesty rules rendered here (they mirror the backend contract):
 *   - ms === null renders '—', never 0, never a last-known number.
 *   - status 'unknown' and 'down' are first-class; nothing says "up" without a
 *     probe behind it.
 *   - sparkline nulls are gaps (polyline restarts), never interpolated.
 *   - probe kinds (tcp_connect / http / tailscale_ping) are labelled and never
 *     mixed on one axis.
 * Styling: C tokens + theme vars only — no hardcoded colours.
 */

/** transport class -> badge colour (spec §2.1: four real transports) */
function hdayNetTransport(t) {
  if (t === 'tailscale') return { label: 'tailscale', color: C.emerald }
  if (t === 'cloudflared') return { label: 'cloudflared', color: C.amber }
  if (t === 'localhost') return { label: 'localhost', color: C.slate }
  if (t === 'any') return { label: 'any', color: C.faint }
  return { label: 'unknown', color: C.faint }
}

/** status -> dot colour + label; unknown/stale is faint, never optimistic */
function hdayNetStatus(status, stale) {
  if (stale && status === 'up') return { label: 'stale', color: C.faint }
  if (status === 'up') return { label: 'up', color: C.emerald }
  if (status === 'down') return { label: 'down', color: C.red }
  return { label: 'unknown', color: C.faint }
}

/** latency cell — null renders an em dash, always */
function hdayNetMs(ms) {
  const v = typeof ms === 'number' && isFinite(ms) && ms >= 0 ? ms : null
  if (v === null) return '—'
  if (v >= 100) return v.toFixed(0) + ' ms'
  if (v >= 10) return v.toFixed(1) + ' ms'
  if (v >= 1) return v.toFixed(2) + ' ms'
  return v.toFixed(3) + ' ms'
}

/** 'checked_at' (ISO) -> relativeTime; null -> '—' */
function hdayNetAgo(iso) {
  if (!iso) return '—'
  try {
    const t = Date.parse(iso)
    if (!isFinite(t) || t <= 0) return '—'
    return relativeTime(t)
  } catch {
    return '—'
  }
}

/**
 * Latency sparkline — plots raw samples and BREAKS the polyline on every null
 * (multiple segments = real gaps). Deliberately not Spark({seqs}): that one
 * diffs latest_seq (throughput), drops out under 2 samples and hardcodes hex.
 */
function hdayNetSpark(history, status, stale) {
  const vals = Array.isArray(history) ? history : []
  const nums = vals.filter(v => typeof v === 'number' && isFinite(v) && v >= 0)
  const w = 56
  const h = 14
  if (!nums.length) {
    return jsxs('span', {
      className: 'shrink-0 font-mono text-[0.62rem]',
      style: { color: C.faint },
      title: 'no samples yet — a failed probe is a gap, not a zero',
      children: '···'
    })
  }
  const max = Math.max.apply(null, nums)
  const top = max > 0 ? max : 1
  const n = Math.max(vals.length - 1, 1)
  const step = w / n
  const segments = []
  let cur = []
  for (let i = 0; i < vals.length; i += 1) {
    const v = vals[i]
    if (typeof v !== 'number' || !isFinite(v) || v < 0) {
      if (cur.length) segments.push(cur)
      cur = []
      continue
    }
    const x = Math.min(w, i * step)
    const y = h - 2 - (v / top) * (h - 4)
    cur.push([x, y])
  }
  if (cur.length) segments.push(cur)
  const stroke = status === 'down' ? C.red : stale ? C.faint : C.mono
  const kids = []
  for (let s = 0; s < segments.length; s += 1) {
    const seg = segments[s]
    if (seg.length === 1) {
      kids.push(jsx('circle', {
        cx: seg[0][0].toFixed(1), cy: seg[0][1].toFixed(1), r: 1.6,
        fill: stroke, 'aria-hidden': true
      }, 'p' + s))
    } else {
      kids.push(jsx('polyline', {
        points: seg.map(pt => pt[0].toFixed(1) + ',' + pt[1].toFixed(1)).join(' '),
        fill: 'none', stroke: stroke, strokeWidth: 1.5,
        strokeLinecap: 'round', strokeLinejoin: 'round'
      }, 's' + s))
    }
  }
  return jsxs('svg', {
    width: w, height: h, 'aria-hidden': true, className: 'shrink-0 opacity-80',
    children: kids
  })
}

/** one endpoint row: identity, transport, spark, status, latency, checked-at */
function hdayNetRow(e) {
  const tr = hdayNetTransport(e.transport)
  const st = hdayNetStatus(e.status, e.stale)
  const id = String(e.id || (e.host || '?') + ':' + e.port)
  return jsxs('div', {
    className: 'hday-row grid grid-cols-[minmax(0,1.6fr)_7rem_64px_5.5rem_6rem_5rem] items-center gap-2 rounded-md px-2 py-1.5',
    key: id,
    title: e.err ? String(e.err) : (e.bind ? 'bound ' + e.bind : 'bind unknown'),
    children: [
      jsxs('div', {
        className: 'flex min-w-0 items-center gap-2',
        children: [
          jsx('span', {
            className: 'truncate font-mono text-[0.7rem]',
            style: { color: C.mono },
            children: id
          }),
          e.default ? jsx(Badge, { size: 'xs', variant: 'secondary', children: 'brief' }) : null,
          e.stale ? jsx(Badge, { size: 'xs', variant: 'outline', children: 'stale' }) : null
        ]
      }),
      jsxs('span', {
        className: 'inline-flex items-center gap-1 truncate text-[0.66rem]',
        style: { color: tr.color },
        children: [
          jsx('span', {
            className: 'size-1.5 shrink-0 rounded-full',
            style: { background: tr.color }
          }),
          tr.label
        ]
      }),
      hdayNetSpark(e.history, e.status, e.stale),
      jsxs('span', {
        className: 'inline-flex items-center gap-1.5 text-[0.66rem]',
        style: { color: st.color },
        children: [
          jsx('span', {
            className: 'size-2 shrink-0 rounded-full',
            style: { background: st.color },
            'aria-hidden': true
          }),
          st.label
        ]
      }),
      jsx('span', {
        className: 'text-right font-mono text-[0.68rem] tabular-nums',
        style: { color: typeof e.ms === 'number' ? (e.stale ? C.faint : C.text) : C.faint },
        title: e.err ? String(e.err) : (e.probe ? 'probe: ' + e.probe : ''),
        children: hdayNetMs(e.ms)
      }),
      jsx('span', {
        className: 'truncate text-right text-[0.62rem] tabular-nums',
        style: { color: C.faint },
        children: hdayNetAgo(e.checked_at)
      })
    ]
  })
}

/** peer strip cell: online dot + host + ip + last-seen + latency */
function hdayNetPeer(p) {
  const online = Boolean(p.online)
  const color = online ? C.emerald : C.red
  const label = online ? 'up' : 'down'
  const shown = p.pinged ? p.ms : null     // no ping taken -> no number, ever
  return jsxs('div', {
    className: 'hday-row flex items-center gap-2 rounded-md px-2 py-1',
    key: String(p.ip || p.host),
    title: (p.err ? String(p.err) + ' · ' : '') + (p.last_seen_text || ''),
    children: [
      jsx('span', {
        className: 'size-2 shrink-0 rounded-full',
        style: { background: color },
        'aria-hidden': true
      }),
      jsx('span', {
        className: 'shrink-0 text-[0.7rem]',
        style: { color: C.text },
        children: String(p.host || '?')
      }),
      jsx('span', {
        className: 'shrink-0 font-mono text-[0.62rem]',
        style: { color: C.faint },
        children: String(p.ip || '—')
      }),
      jsx('span', {
        className: 'min-w-0 flex-1 truncate text-[0.62rem]',
        style: { color: C.faint },
        children: String(p.last_seen_text || '')
      }),
      jsx('span', {
        className: 'w-14 shrink-0 text-right font-mono text-[0.66rem] tabular-nums',
        style: { color: shown === null ? C.faint : C.text },
        children: shown === null ? '—' : hdayNetMs(shown)
      }),
      jsx('span', {
        className: 'w-8 shrink-0 text-[0.62rem]',
        style: { color: color },
        children: label
      }),
      jsx('span', {
        className: 'w-14 shrink-0 text-right text-[0.6rem] tabular-nums',
        style: { color: C.faint },
        children: p.pinged ? hdayNetAgo(p.checked_at) : hdayNetAgo(p.last_seen)
      })
    ]
  })
}

/** tunnel row — live trycloudflare URL, copy button, edge/origin sub-states */
function hdayNetTunnel(tun, copied, onCopy) {
  if (!tun) return null
  const url = tun.url || null
  const st = hdayNetStatus(tun.status, tun.stale)
  let statusLine = st.label
  if (tun.status === 'degraded') {
    const sub = tun.substates || {}
    statusLine = sub.origin === 'down' ? 'tunnel up / origin down' : 'tunnel degraded'
  }
  const statusColor = tun.status === 'degraded' ? C.amber : st.color
  return jsxs('div', {
    className: 'hday-card flex flex-col gap-2 rounded-lg p-3.5',
    style: { background: C.surface, border: '1px solid ' + C.border },
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsx(Badge, { size: 'xs', variant: 'secondary', children: 'cloudflared' }),
          jsxs('span', {
            className: 'inline-flex items-center gap-1.5 text-[0.7rem]',
            style: { color: statusColor },
            title: tun.last_log_error ? String(tun.last_log_error) : (tun.err ? String(tun.err) : ''),
            children: [
              jsx('span', {
                className: 'size-2 rounded-full',
                style: { background: statusColor },
                'aria-hidden': true
              }),
              statusLine
            ]
          }),
          jsx('span', {
            className: 'font-mono text-[0.68rem] tabular-nums',
            style: { color: tun.ms === null ? C.faint : C.text },
            title: tun.probe ? 'probe: ' + tun.probe : '',
            children: hdayNetMs(tun.ms)
          }),
          tun.http_status ? jsx(Badge, {
            size: 'xs',
            variant: 'outline',
            children: 'http ' + String(tun.http_status)
          }) : null,
          jsx('span', {
            className: 'text-[0.62rem] tabular-nums',
            style: { color: C.faint },
            children: hdayNetAgo(tun.checked_at)
          })
        ]
      }),
      url
        ? jsxs('div', {
            className: 'flex min-w-0 items-center gap-2',
            children: [
              jsx('code', {
                className: 'min-w-0 flex-1 truncate font-mono text-[0.68rem]',
                style: { color: C.mono },
                children: url
              }),
              jsx(Button, {
                size: 'xs',
                variant: copied ? 'secondary' : 'ghost',
                onClick: function () { onCopy(url) },
                children: copied
                  ? jsxs('span', { className: 'inline-flex items-center gap-1', children: [jsx(Codicon, { name: 'check' }), 'copied'] })
                  : jsxs('span', { className: 'inline-flex items-center gap-1', children: [jsx(Codicon, { name: 'copy' }), 'copy'] })
              })
            ]
          })
        : jsx('div', {
            className: 'text-[0.68rem]',
            style: { color: C.faint },
            children: 'no tunnel URL found'
          }),
      jsxs('div', {
        className: 'flex flex-wrap gap-3 text-[0.6rem]',
        style: { color: C.faint },
        children: [
          jsx('span', { children: 'pid ' + (tun.pid ? String(tun.pid) : '—') }),
          jsx('span', { children: 'origin ' + (tun.origin ? String(tun.origin) : '—') }),
          jsx('span', {
            className: 'min-w-0 truncate',
            children: 'log ' + (tun.log ? String(tun.log) : '—')
          })
        ]
      }),
      tun.last_log_error
        ? jsxs('div', {
            className: 'flex items-start gap-1.5 text-[0.62rem]',
            style: { color: C.amber },
            children: [jsx(Codicon, { name: 'warning', size: 12 }), jsx('span', { children: String(tun.last_log_error) })]
          })
        : null
    ]
  })
}

/** degraded banner — one amber line naming every reason, plus cache age */
function hdayNetBanner(data) {
  const reasons = (data && data.degraded) || []
  if (!reasons.length) return null
  const cache = (data && data.cache) || {}
  const age = typeof cache.age_s === 'number' ? Math.round(cache.age_s) + 's' : 'n/a'
  return jsxs('div', {
    className: 'flex items-center gap-2 rounded-md px-2.5 py-1.5 text-[0.66rem]',
    style: { background: C.surface, border: '1px solid ' + C.border, color: C.amber },
    children: [
      jsx(Codicon, { name: 'warning', size: 12 }),
      jsx('span', {
        children: reasons.join(', ') + ' — showing ' + (cache.fresh ? 'cached' : 'stale/empty') + ' values (age ' + age + ')'
      })
    ]
  })
}

/** empty state — cache unwritable / no data yet; never a wall of zeroes */
function hdayNetEmpty(onProbe) {
  return jsxs('div', {
    className: 'flex flex-col items-center gap-2 rounded-lg p-6 text-center',
    style: { background: C.surface, border: '1px dashed ' + C.border },
    children: [
      jsx(Codicon, { name: 'globe', size: 20 }),
      jsx('div', {
        className: 'text-[0.72rem]',
        style: { color: C.muted },
        children: 'no radar data yet'
      }),
      jsx('div', {
        className: 'max-w-md text-[0.64rem]',
        style: { color: C.faint },
        children: 'the cache is empty or unwritable — probe to measure real latency. nothing below is guessed: a failed probe shows as unknown, never 0.'
      }),
      jsx(Button, { size: 'xs', variant: 'secondary', onClick: onProbe, children: 'Probe now' })
    ]
  })
}

/** the panel — poll /day-net, render tunnel + endpoints + peers + cache strip */
function hdayPanel04(props) {
  const p = props || {}
  const route = p.route || null
  const [nonce, setNonce] = useState(0)
  const [deep, setDeep] = useState(false)
  const [cacheOnly, setCacheOnly] = useState(false)
  const [showAll, setShowAll] = useState(false)
  const [copied, setCopied] = useState('')
  const refreshRef = useRef(false)
  const copyTimer = useRef(null)

  useEffect(function () {
    return function () {
      if (copyTimer.current) clearTimeout(copyTimer.current)
    }
  }, [])

  const routeKey = route
    ? String(route.connectionId || '') + '/' + String(route.profile || '') + '/' + String(route.targetProfile || '')
    : 'local'

  const netQ = useQuery({
    queryKey: ['hday-netradar', routeKey, nonce, deep, cacheOnly],
    queryFn: function () {
      const bits = []
      let forced = false
      try {
        if (refreshRef.current) {
          refreshRef.current = false
          bits.push('refresh')
          forced = true
        } else if (cacheOnly) {
          bits.push('cache')
        }
        if (deep) bits.push('deep')
        const arg = bits.join(' ')
        return Promise.resolve(rpc(route, 'command.dispatch', { name: 'day-net', arg: arg }))
          .then(function (disp) {
            const out = disp && (disp.output || disp.text || '')
            if (out && String(out).trim().startsWith('{')) {
              const parsed = JSON.parse(out)
              if (parsed && parsed.ok) return parsed
            }
            return null
          })
          .catch(function () { return null })   // missing backend -> degrade, never throw
      } catch (e) {
        if (forced) refreshRef.current = false
        return Promise.resolve(null)
      }
    },
    refetchInterval: 5000,
    staleTime: 2000,
    refetchOnWindowFocus: true,
    retry: false
  })

  const data = netQ && netQ.data ? netQ.data : null

  const onProbe = function () {
    refreshRef.current = true
    setNonce(function (n) { return n + 1 })
  }

  const onCopy = function (text) {
    try {
      if (typeof navigator !== 'undefined' && navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text)
      }
    } catch {}
    setCopied(text)
    if (copyTimer.current) clearTimeout(copyTimer.current)
    copyTimer.current = setTimeout(function () { setCopied('') }, 1400)
  }

  const allEndpoints = (data && Array.isArray(data.endpoints)) ? data.endpoints : []
  const endpoints = showAll
    ? allEndpoints
    : allEndpoints.filter(function (e) { return e.default !== false })
  const peers = (data && Array.isArray(data.peers)) ? data.peers : []
  const totals = (data && data.totals) || { endpoints: 0, up: 0, down: 0, unknown: 0 }
  const selfInfo = (data && data.self) || {}
  const cache = (data && data.cache) || {}
  const empty = !data || !Array.isArray(data.endpoints) || data.endpoints.length === 0

  const cell = function (label, value, color) {
    return jsxs('div', {
      className: 'flex min-w-0 flex-col gap-0.5',
      children: [
        jsx('span', {
          className: 'text-[0.58rem] uppercase tracking-wider',
          style: { color: C.faint },
          children: label
        }),
        jsx('span', {
          className: 'truncate font-mono text-[0.78rem] tabular-nums',
          style: { color: color || C.text },
          children: String(value)
        })
      ]
    })
  }

  return jsx('div', {
    className: 'flex h-full min-h-0 flex-col gap-3 overflow-auto p-3',
    children: [
      // header: identity + totals + actions
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-3',
        children: [
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx(Codicon, { name: 'server', size: 16 }),
              jsx('span', {
                className: 'text-[0.86rem] font-semibold uppercase tracking-wider',
                style: { color: C.text },
                children: 'Network Radar'
              })
            ]
          }),
          jsx('span', {
            className: 'font-mono text-[0.66rem]',
            style: { color: C.faint },
            children: String(selfInfo.host || '—') + (selfInfo.tailscale_ip ? ' · ' + selfInfo.tailscale_ip : '')
          }),
          jsxs('div', {
            className: 'ml-auto flex items-center gap-2',
            children: [
              jsx(Badge, {
                size: 'xs',
                variant: 'success',
                children: totals.up + ' up'
              }),
              jsx(Badge, {
                size: 'xs',
                variant: totals.down ? 'destructive' : 'secondary',
                children: totals.down + ' down'
              }),
              jsx(Badge, { size: 'xs', variant: 'secondary', children: totals.unknown + ' unknown' }),
              jsx(Badge, {
                size: 'xs',
                variant: 'outline',
                children: String(totals.endpoints) + ' endpoints'
              }),
              jsx(Button, {
                size: 'xs',
                variant: 'ghost',
                onClick: onProbe,
                children: jsxs('span', {
                  className: 'inline-flex items-center gap-1',
                  children: [jsx(Codicon, { name: 'sync' }), 'Probe now']
                })
              }),
              jsx(Button, {
                size: 'xs',
                variant: cacheOnly ? 'secondary' : 'ghost',
                onClick: function () { setCacheOnly(function (v) { return !v }) },
                children: jsxs('span', {
                  className: 'inline-flex items-center gap-1',
                  children: [jsx(Codicon, { name: 'history' }), 'cache only']
                })
              }),
              jsx(Button, {
                size: 'xs',
                variant: deep ? 'secondary' : 'ghost',
                onClick: function () {
                  // deep = "measure more now", so it forces a real probe —
                  // a fresh cache has no deep fields to serve
                  setDeep(function (v) { return !v })
                  refreshRef.current = true
                  setNonce(function (n) { return n + 1 })
                },
                children: jsxs('span', {
                  className: 'inline-flex items-center gap-1',
                  children: [jsx(Codicon, { name: 'zoom-in' }), 'deep']
                })
              }),
              jsx(Button, {
                size: 'xs',
                variant: showAll ? 'secondary' : 'ghost',
                onClick: function () { setShowAll(function (v) { return !v }) },
                children: jsxs('span', {
                  className: 'inline-flex items-center gap-1',
                  children: [jsx(Codicon, { name: 'list-flat' }), showAll ? 'brief ports' : 'all listeners']
                })
              })
            ]
          })
        ]
      }),

      hdayNetBanner(data),

      data
        ? hdayNetTunnel(data.tunnel, Boolean(copied && data.tunnel && data.tunnel.copy === copied), onCopy)
        : null,

      empty
        ? hdayNetEmpty(onProbe)
        : jsxs('div', {
            className: 'flex flex-col gap-1 rounded-lg p-2',
            style: { background: C.surface, border: '1px solid ' + C.border },
            children: [
              jsxs('div', {
                className: 'grid grid-cols-[minmax(0,1.6fr)_7rem_64px_5.5rem_6rem_5rem] items-center gap-2 px-2 pb-1 text-[0.58rem] uppercase tracking-wider',
                style: { color: C.faint },
                children: [
                  jsx('span', { children: 'endpoint' }),
                  jsx('span', { children: 'transport' }),
                  jsx('span', { children: 'latency' }),
                  jsx('span', { children: 'status' }),
                  jsx('span', { className: 'text-right', children: 'ms' }),
                  jsx('span', { className: 'text-right', children: 'checked' })
                ]
              }),
              endpoints.length
                ? endpoints.map(hdayNetRow)
                : jsx('div', {
                    className: 'px-2 py-2 text-[0.66rem]',
                    style: { color: C.faint },
                    children: 'no listeners discovered'
                  })
            ]
          }),

      peers.length
        ? jsxs('div', {
            className: 'flex flex-col gap-1 rounded-lg p-2',
            style: { background: C.surface, border: '1px solid ' + C.border },
            children: [
              jsxs('div', {
                className: 'flex items-center gap-2 px-2 pb-1 text-[0.58rem] uppercase tracking-wider',
                style: { color: C.faint },
                children: [
                  jsx('span', { children: 'tailnet peers' }),
                  deep ? jsx(Kbd, { children: 'deep ping' }) : null,
                  jsx('span', {
                    className: 'ml-auto normal-case tracking-normal',
                    children: 'latency only when a ping was actually taken'
                  })
                ]
              }),
              peers.map(hdayNetPeer)
            ]
          })
        : null,

      // cache strip — provenance for everything above
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-3 px-1 text-[0.6rem]',
        style: { color: C.faint },
        children: [
          jsx('span', {
            className: 'min-w-0 max-w-full truncate font-mono',
            children: 'cache ' + String(cache.path || '—')
          }),
          jsx('span', { children: 'age ' + (typeof cache.age_s === 'number' ? Math.round(cache.age_s) + 's' : '—') }),
          jsx('span', { children: 'ttl ' + String(cache.ttl_s == null ? '—' : cache.ttl_s) + 's' }),
          jsx('span', {
            style: { color: cache.fresh ? C.emerald : C.amber },
            children: cache.fresh ? 'fresh' : 'stale'
          }),
          jsx('span', { children: 'generated ' + hdayNetAgo(data && data.generated_at) }),
          jsx('span', { className: 'inline-flex items-center gap-1', children: [jsx(Kbd, { children: 'tcp' }), 'connect'] }),
          jsx('span', { className: 'inline-flex items-center gap-1', children: [jsx(Kbd, { children: 'http' }), deep ? 'on' : 'tunnel only'] }),
          netQ && netQ.isFetching ? jsx(Codicon, { name: 'sync', spinning: true, className: 'text-[0.7rem]' }) : null
        ]
      })
    ]
  })
}

// ---- lane 05: session-surgery ----
// Lane 05 — Session Surgery (splice-ready snippet; NOT a module).
//
// Parses as a plain script (`node --check`): no import/export, top-level
// function declarations only, every helper prefixed `hday05` so it cannot
// collide with another lane's snippet or the host bundle.
//
// Gateway contracts used AS IS (tui_gateway/contracts/sessions.py):
//   session.branch   fork-with-history; `count` truncates history[:count]
//                    → count is MESSAGES, never turns (turn addressing BLOCKED)
//   session.activate attach the frontend to a live session without closing
//                    the current one — the teleport primitive (no focus move)
//   session.history  durable display transcript; session-scoped, so every use
//                    below is preceded by session.resume on the STORED id
// BLOCKED (surfaced in copy, never faked): turn-addressed branch, native
// session.merge, server-side conversation diff, backend desktop-focus moves.
// The worktree half of diff runs in the backend and resolves SHAs ONLY from
// rec['snaps'] — never from a session key.
//
// Host bundle provides: jsx, jsxs, Badge, Button, Codicon, Kbd, Tip, Input,
// atom, useValue, useQuery, cn, relativeTime, host, rpc, haptic, errMsg,
// useState, useEffect, C (theme token object). Theme tokens only — no hex.

function hday05Ms(ts) {
  var n = Number(ts) || 0
  if (n <= 0) return 0
  return n < 1e12 ? n * 1000 : n
}

function hday05When(ts) {
  var ms = hday05Ms(ts)
  try { return ms ? relativeTime(ms) : '—' } catch (e) { return '—' }
}

function hday05Err(e) {
  try { return errMsg(e) || String(e) } catch (x) { return String(e) }
}

function hday05Text(m) {
  return String((m && m.text) || '')
}

function hday05Flat(s) {
  return hday05Text(s).replace(/\s+/g, ' ').trim()
}

/** Turn boundary = a user_message row. Legacy untyped user rows count too;
 *  system scaffolding (auto_continue notes, hidden rows) never does. */
function hday05IsTurn(m) {
  if (!m || m.role !== 'user') return false
  var kind = m.display_kind || ''
  if (kind === 'user_message') return true
  if (kind) return false
  var t = hday05Text(m)
  if (!t || t.indexOf('[System note:') === 0) return false
  return true
}

function hday05TurnsOf(msgs) {
  var out = []
  var n = 0
  for (var i = 0; i < msgs.length; i++) {
    var m = msgs[i] || {}
    if (!hday05IsTurn(m)) continue
    n += 1
    out.push({ n: n, index: i, keep: i + 1, preview: hday05Flat(m).slice(0, 90) })
  }
  return out
}

function hday05Norm(m) {
  return (m && m.role || '') + '\u0001' + hday05Flat(m) + '\u0001' + ((m && m.name) || '')
}

/** Client-side conversation diff — there is no server-side session.diff /
 *  session.compare RPC (BLOCKED), so the divergence is computed here from two
 *  session.history reads. Equality is exact on role+text+tool-name. */
function hday05ConvDiff(a, b) {
  var i = 0
  var n = Math.min(a.length, b.length)
  while (i < n && hday05Norm(a[i]) === hday05Norm(b[i])) i++
  function tools(list) {
    var out = {}
    for (var k = 0; k < list.length; k++) {
      var m = list[k] || {}
      if (m.name && m.role !== 'user') out[m.name] = (out[m.name] || 0) + 1
    }
    return out
  }
  var ta = tools(a)
  var tb = tools(b)
  var onlyA = []
  var onlyB = []
  var k
  for (k in ta) if (Object.prototype.hasOwnProperty.call(tb, k) === false) onlyA.push(k)
  for (k in tb) if (Object.prototype.hasOwnProperty.call(ta, k) === false) onlyB.push(k)
  return {
    shared: i,
    aLen: a.length,
    bLen: b.length,
    divergeAt: i,
    identical: i >= n && a.length === b.length,
    aHere: i < a.length ? a[i] : null,
    bHere: i < b.length ? b[i] : null,
    toolOnlyA: onlyA.sort(),
    toolOnlyB: onlyB.sort()
  }
}

function hday05ToolTail(msgs, limit) {
  var seen = []
  for (var i = msgs.length - 1; i >= 0 && seen.length < limit; i--) {
    var m = msgs[i] || {}
    if (!m.name || m.role === 'user') continue
    if (seen.indexOf(m.name) < 0) seen.push(m.name)
  }
  return seen.reverse()
}

/** Gateway RPC against the LOCAL route (this board view carries no route
 *  prop). Rejects on a gateway error envelope as well as a transport error. */
async function hday05Rpc(method, params) {
  var res = await rpc(null, method, params)
  if (res && typeof res === 'object' && res.error) {
    var e = res.error
    var msg = typeof e === 'string'
      ? e
      : (e && (e.message || e.msg)) || (e && e.code != null ? 'error ' + e.code : 'rpc error')
    throw new Error(method + ': ' + msg)
  }
  return res
}

/** One plugin command dispatch → parsed JSON (the day-* backend contract). */
async function hday05Dispatch(name, arg) {
  var disp = await hday05Rpc('command.dispatch', { name: name, arg: arg || '' })
  var out = disp && (disp.output || disp.text || '')
  if (out && typeof out === 'object') return out
  var text = String(out || '').trim()
  if (!text) return { ok: false, error: name + ' returned no output' }
  if (text.charAt(0) === '{') {
    try {
      var parsed = JSON.parse(text)
      if (parsed && typeof parsed === 'object') return parsed
    } catch (e) { /* fall through to the raw report */ }
  }
  return { ok: false, error: text.slice(0, 300) }
}

/** session.resume is always step one: every session-scoped RPC (branch,
 *  history, steer, prompt.submit, activate) is keyed by the RUNTIME id and
 *  answers 4001 for a cold session. */
async function hday05Resume(storedKey, lazy) {
  var params = { session_id: storedKey }
  if (lazy) params.lazy = true
  var res = await hday05Rpc('session.resume', params)
  var sid = res && res.session_id
  if (!sid) throw new Error('session.resume returned no runtime id for ' + storedKey)
  return { sid: sid, snap: res || {} }
}

async function hday05History(storedKey) {
  var live = await hday05Resume(storedKey, true)
  var res = await hday05Rpc('session.history', { session_id: live.sid })
  var msgs = (res && res.messages) || []
  return { messages: msgs, count: Number((res && res.count)) || msgs.length, sid: live.sid }
}

function hday05Rows(listQ, liveQ) {
  var map = {}
  var list = (listQ && listQ.data && listQ.data.sessions) || []
  var live = (liveQ && liveQ.data && liveQ.data.sessions) || []
  var i
  for (i = 0; i < list.length; i++) {
    var r = list[i] || {}
    if (!r.id) continue
    map[r.id] = {
      key: String(r.id),
      title: r.title || 'Session',
      msgs: Number(r.message_count) || 0,
      at: hday05Ms(r.started_at),
      live: false,
      status: ''
    }
  }
  for (i = 0; i < live.length; i++) {
    var s = live[i] || {}
    var key = s.session_key || s.id
    if (!key) continue
    var ent = map[key]
    if (!ent) {
      ent = { key: String(key), title: s.title || 'Session', msgs: 0, at: hday05Ms(s.started_at), live: false, status: '' }
      map[key] = ent
    }
    ent.live = true
    ent.status = String(s.status || '')
    if (s.title) ent.title = s.title
    if (Number(s.message_count)) ent.msgs = Number(s.message_count)
    if (hday05Ms(s.started_at)) ent.at = hday05Ms(s.started_at)
  }
  var out = []
  for (var k in map) {
    if (Object.prototype.hasOwnProperty.call(map, k)) out.push(map[k])
  }
  out.sort(function (a, b) { return (b.at || 0) - (a.at || 0) })
  return out
}

function hday05Find(rows, key) {
  if (!key) return null
  for (var i = 0; i < rows.length; i++) {
    if (rows[i].key === key) return rows[i]
  }
  return null
}

// ---------------------------------------------------------------------------

function hdayPanel05(props) {
  var listQ = useQuery({
    queryKey: ['hday-surgery-sessions'],
    queryFn: function () { return hday05Rpc('session.list', { limit: 40 }) },
    staleTime: 20000, refetchInterval: 60000, retry: false
  })
  var liveQ = useQuery({
    queryKey: ['hday-surgery-live'],
    queryFn: function () { return hday05Rpc('session.active_list', {}) },
    staleTime: 10000, refetchInterval: 30000, retry: false
  })

  var [tab, setTab] = useState('fork')
  var [fromKey, setFromKey] = useState('')
  var [toKey, setToKey] = useState('')
  var [busy, setBusy] = useState('')
  var [note, setNote] = useState('')
  var [turns, setTurns] = useState([])
  var [msgTotal, setMsgTotal] = useState(0)
  var [keepN, setKeepN] = useState(0)
  var [rawCount, setRawCount] = useState('')
  var [wtDiff, setWtDiff] = useState(null)
  var [convDiff, setConvDiff] = useState(null)
  var [digest, setDigest] = useState('')
  var [delivery, setDelivery] = useState('auto')

  var ledgerQ = useQuery({
    queryKey: ['hday-surgery-ledger', fromKey],
    queryFn: function () { return hday05Dispatch('day-surgery', fromKey) },
    enabled: !!fromKey,
    staleTime: 5000, refetchInterval: 30000, retry: false
  })

  var rows = hday05Rows(listQ, liveQ)
  var fromRow = hday05Find(rows, fromKey)
  var toRow = hday05Find(rows, toKey)
  var isBusy = !!busy

  // default the two pickers once the first rows land
  useEffect(function () {
    if (!rows.length) return
    if (!hday05Find(rows, fromKey)) setFromKey(rows[0].key)
    if (!hday05Find(rows, toKey)) setToKey((rows[1] || rows[0]).key)
  })

  // (re)load the source transcript whenever the source picker changes
  useEffect(function () {
    setTurns([])
    setMsgTotal(0)
    setKeepN(0)
    setRawCount('')
    setWtDiff(null)
    setConvDiff(null)
    setDigest('')
    if (!fromKey) return undefined
    var dead = false
    hday05History(fromKey).then(function (h) {
      if (dead) return
      var t = hday05TurnsOf(h.messages)
      setTurns(t)
      setMsgTotal(h.count)
      setKeepN(t.length ? t[0].keep : 0)
    }).catch(function (e) {
      if (!dead) setNote('!history read failed: ' + hday05Err(e))
    })
    return function () { dead = true }
  }, [fromKey])

  function keepNow() {
    var raw = String(rawCount || '').trim()
    if (raw) {
      var n = Number(raw)
      if (!isFinite(n) || n < 1) return 0
      return Math.floor(n)
    }
    return Number(keepN) || 0
  }

  // -------------------------------------------------------------- actions

  async function doFork() {
    if (!fromKey || isBusy) return
    setBusy('fork')
    setNote('')
    try {
      var live = await hday05Resume(fromKey, false)
      var br = await hday05Rpc('session.branch', { session_id: live.sid })
      var child = br && br.stored_session_id
      if (!child) throw new Error('session.branch returned no stored_session_id')
      var rec = await hday05Dispatch('day-fork', fromKey + ' ' + child)
      try {
        Promise.resolve(host.openSession(child, { awaitHydration: true, forceResume: true })).catch(function () {})
      } catch (eOpen) { /* focus move is best effort */ }
      haptic('submit')
      setNote('forked → "' + ((br.title || 'child') + '" [' + child + '] · '
        + (Number(br.message_count) || 0) + ' messages copied (full history)'
        + (rec && rec.ok
          ? ' · evidence record cloned (' + rec.snaps_copied + ' snaps'
            + (rec.parent_record_created ? ', parent had no record' : '') + ')'
          : ' · ledger write failed: ' + ((rec && rec.error) || 'no output'))))
    } catch (e) {
      haptic('cancel')
      setNote('!fork failed: ' + hday05Err(e))
    } finally {
      setBusy('')
    }
  }

  async function doBranch() {
    var keep = keepNow()
    if (!fromKey || !keep || isBusy) return
    setBusy('branch')
    setNote('')
    try {
      if (msgTotal && keep > msgTotal) {
        throw new Error('count ' + keep + ' exceeds the transcript (' + msgTotal + ' messages)')
      }
      var live = await hday05Resume(fromKey, false)
      var br = await hday05Rpc('session.branch', {
        session_id: live.sid,
        count: keep,
        name: 'branch @msg ' + keep
      })
      var child = br && br.stored_session_id
      if (!child) throw new Error('session.branch returned no stored_session_id')
      var rec = await hday05Dispatch('day-branch', fromKey + ' ' + child + ' ' + keep)
      try {
        Promise.resolve(host.openSession(child, { awaitHydration: true, forceResume: true })).catch(function () {})
      } catch (eOpen) { /* focus move is best effort */ }
      haptic('submit')
      setNote('branched at message ' + keep + (msgTotal ? ' of ' + msgTotal : '')
        + ' → "' + ((br.title || child) + '" · gateway kept '
          + (br.message_count != null ? br.message_count : '?') + ' messages'
        + ' (count is MESSAGE-granular — no turn-addressed branch RPC)'
        + (rec && rec.ok ? ' · bookkeeping recorded' : ' · ledger: ' + ((rec && rec.error) || 'no output'))))
    } catch (e) {
      haptic('cancel')
      setNote('!branch failed: ' + hday05Err(e))
    } finally {
      setBusy('')
    }
  }

  async function doDiff() {
    if (!fromKey || !toKey || isBusy) return
    setBusy('diff')
    setNote('')
    try {
      var wt = await hday05Dispatch('day-diff', fromKey + ' ' + toKey)
      setWtDiff(wt)
      var ha = await hday05History(fromKey)
      var hb = await hday05History(toKey)
      setConvDiff(hday05ConvDiff(ha.messages, hb.messages))
      haptic('submit')
      if (wt && wt.ok) {
        setNote('diff done — worktree ' + ((wt.files || []).length) + ' file(s); transcript divergence at message '
          + (convAt(ha.messages, hb.messages)))
      } else {
        setNote('!worktree half unavailable: ' + ((wt && wt.error) || 'day-diff failed')
          + ' — the transcript half below still ran (client-side)')
      }
    } catch (e) {
      haptic('cancel')
      setNote('!diff failed: ' + hday05Err(e))
    } finally {
      setBusy('')
    }
  }

  function convAt(a, b) {
    var d = hday05ConvDiff(a, b)
    return d.shared
  }

  async function buildDigest() {
    setBusy('digest')
    setNote('')
    try {
      var h = await hday05History(fromKey)
      var msgs = h.messages
      var userTurns = 0
      var lastAssistant = ''
      for (var i = 0; i < msgs.length; i++) {
        var m = msgs[i] || {}
        if (hday05IsTurn(m)) userTurns += 1
        if (m.role === 'assistant' && hday05Text(m)) lastAssistant = hday05Text(m)
      }
      var toolTail = hday05ToolTail(msgs, 8)
      var wt = await hday05Dispatch('day-diff', fromKey + ' ' + toKey)
      var delta
      if (wt && wt.ok) {
        var fs = wt.files || []
        delta = fs.length
          ? fs.length + ' file(s): ' + fs.slice(0, 10).join(', ') + (fs.length > 10 ? ' …' : '')
          : 'no file changes between the two latest snapshots'
      } else {
        delta = 'unavailable — ' + ((wt && wt.error) || 'day-diff failed')
      }
      var lines = [
        'Session-surgery merge: "' + ((fromRow && fromRow.title) || fromKey) + '" [' + fromKey + '] → ["'
          + ((toRow && toRow.title) || toKey) + '"] [' + toKey + ']',
        'transcript: ' + h.count + ' messages · ' + userTurns + ' user turns (turn = user_message row)',
        'worktree delta vs target (latest rec[\'snaps\'] SHAs): ' + delta,
        'tools in the tail: ' + (toolTail.length ? toolTail.join(', ') : 'none'),
        '',
        lastAssistant
          ? 'Last assistant message (tail): ' + lastAssistant.slice(-700)
          : 'Last assistant message: (none yet)'
      ]
      var text = lines.join('\n')
      setDigest(text)
      haptic('submit')
      setNote('digest built from session.history + day-diff (' + text.length + ' chars)')
      return text
    } catch (e) {
      haptic('cancel')
      setNote('!digest build failed: ' + hday05Err(e))
      return ''
    } finally {
      setBusy('')
    }
  }

  async function doMerge() {
    if (!fromKey || !toKey || isBusy) return
    if (fromKey === toKey) {
      setNote('!source and target are the same session')
      return
    }
    try {
      var text = digest && digest.trim() ? digest : await buildDigest()
      if (!text || !text.trim()) return
      setBusy('merge')
      setNote('')
      var live = await hday05Resume(toKey, true)
      var want = delivery
      if (want === 'auto') {
        var st = toRow && toRow.status ? String(toRow.status) : ''
        want = ['working', 'streaming', 'starting', 'resuming'].indexOf(st) >= 0 ? 'steer' : 'user_message'
      }
      var attempts = [want]
      if (want !== 'steer') attempts.push('steer')
      if (want !== 'user_message') attempts.push('user_message')
      var used = ''
      var why = ''
      for (var i = 0; i < attempts.length && !used; i++) {
        try {
          if (attempts[i] === 'user_message') {
            await hday05Rpc('prompt.submit', { session_id: live.sid, text: text })
            used = 'user_message'
          } else if (attempts[i] === 'steer') {
            var sr = await hday05Rpc('session.steer', { session_id: live.sid, text: text })
            if (sr && String(sr.status) === 'rejected') {
              why = why || 'steer rejected (agent has no steer)'
            } else {
              used = 'steer'
            }
          } else {
            var rr = await hday05Rpc('session.redirect', { session_id: live.sid, text: text })
            if (rr && String(rr.status) === 'rejected') {
              why = why || 'redirect rejected (agent lacks active-turn redirect)'
            } else {
              used = 'redirect'
            }
          }
        } catch (eAtt) {
          why = why || attempts[i] + ': ' + hday05Err(eAtt)
        }
      }
      if (!used) throw new Error('no delivery route worked — ' + (why || 'unknown'))
      var rec = await hday05Dispatch('day-merge', [fromKey, toKey, used, String(text.length)].join(' '))
      haptic('submit')
      setNote('merge delivered via ' + used + ' (' + text.length + ' chars) → recorded on "'
        + ((toRow && toRow.title) || toKey) + '"'
        + (rec && rec.ok ? '' : ' · ledger write failed: ' + ((rec && rec.error) || 'no output')))
    } catch (e) {
      haptic('cancel')
      setNote('!merge failed: ' + hday05Err(e))
    } finally {
      setBusy('')
    }
  }

  async function doTeleport() {
    if (!toKey || isBusy) return
    setBusy('teleport')
    setNote('')
    try {
      var live = await hday05Resume(toKey, true)
      var activated = false
      var activateErr = ''
      try {
        await hday05Rpc('session.activate', { session_id: live.sid, omit_messages: true })
        activated = true
      } catch (eAct) {
        activateErr = hday05Err(eAct)
      }
      try {
        Promise.resolve(host.openSession(toKey, { awaitHydration: true, forceResume: true })).catch(function () {})
      } catch (eOpen) { /* focus move is best effort */ }
      var bk = null
      if (fromKey && fromKey !== toKey) {
        bk = await hday05Dispatch('day-surgery', 'teleport ' + fromKey + ' ' + toKey)
      }
      haptic('submit')
      setNote('teleport → "' + ((toRow && toRow.title) || toKey) + '"'
        + (activated ? ' · gateway attached (session.activate)' : ' · session.activate failed (' + activateErr + ')')
        + ' · this window focused the session via host.openSession — a backend command cannot move desktop focus'
        + (bk && bk.ok ? ' · bookkeeping recorded' : (bk ? ' · ledger: ' + bk.error : '')))
    } catch (e) {
      haptic('cancel')
      setNote('!teleport failed: ' + hday05Err(e))
    } finally {
      setBusy('')
    }
  }

  // ------------------------------------------------------------- rendering

  function title(text) {
    return jsx('div', {
      className: 'mb-1 text-[0.62rem] font-semibold uppercase tracking-wider',
      style: { color: C.faint },
      children: text
    })
  }

  function blocked(text) {
    return jsxs('div', {
      className: 'flex items-start gap-1.5 rounded border px-2 py-1.5 text-[0.66rem] leading-snug',
      style: { borderColor: C.border, color: C.muted, background: C.surface },
      children: [
        jsx(Codicon, { name: 'info', style: { color: C.amber, marginTop: '1px' } }),
        jsx('span', { children: text })
      ]
    })
  }

  function chip(text, color, key) {
    return jsx('span', {
      className: 'hday-chip shrink-0 rounded border px-1.5 py-0.5 font-mono text-[0.6rem]',
      style: { color: color || C.muted, borderColor: C.border },
      children: text
    }, key)
  }

  function picker(label, value, onChange) {
    return jsxs('label', {
      className: 'flex items-center gap-1.5 text-[0.62rem] uppercase tracking-wider',
      style: { color: C.faint },
      children: [
        label,
        jsx('select', {
          className: 'max-w-[210px] truncate rounded px-1.5 py-1 font-mono text-[0.68rem]',
          style: { background: C.surface, color: C.text, border: '1px solid ' + C.border },
          value: value,
          disabled: isBusy,
          onChange: function (e) { onChange(e.target.value) },
          children: rows.map(function (r) {
            var label2 = r.title + ' · ' + r.msgs + ' msg' + (r.live ? ' · live' : '')
            return jsx('option', { value: r.key, children: label2 }, r.key)
          })
        }),
        fromRow && toRow && value === fromKey && fromKey === toKey
          ? jsx('span', { style: { color: C.amber }, children: 'same' })
          : null
      ]
    })
  }

  function forkBody() {
    return jsxs('div', {
      className: 'flex flex-col gap-2',
      children: [
        jsx('div', {
          className: 'text-[0.68rem] leading-relaxed',
          style: { color: C.muted },
          children: 'Fork with history: session.resume → session.branch (a new stored child holding the '
            + 'display transcript so far) → open the child → day-fork clones the evidence record '
            + '(snaps, runs, blocks, receipt) and links forks[] on both sides.'
        }),
        blocked('BLOCKED: no gateway RPC clones plugin state — session.branch copies messages only; '
          + 'the record clone happens plugin-side in day-fork, and the parent record is created '
          + 'empty if this session had no evidence yet (reported as parent_record_created).'),
        jsxs('div', {
          className: 'flex items-center gap-2',
          children: [
            jsx(Button, {
              variant: 'secondary',
              disabled: !fromKey || isBusy,
              onClick: doFork,
              children: busy === 'fork' ? 'Forking…' : 'Fork this session'
            }),
            jsx('span', {
              className: 'text-[0.64rem]',
              style: { color: C.faint },
              children: 'child: new stored session (opens in this window)'
            })
          ]
        })
      ]
    })
  }

  function branchBody() {
    var keep = keepNow()
    return jsxs('div', {
      className: 'flex flex-col gap-2',
      children: [
        blocked('BLOCKED: no turn-addressed branch RPC — session.branch only takes '
          + 'count = messages kept (history[:count]). It cannot be given a turn number.'),
        jsxs('div', {
          className: 'flex items-start gap-1.5 text-[0.66rem] leading-snug',
          style: { color: C.muted },
          children: [
            jsx(Codicon, { name: 'git-branch', style: { color: C.amber, marginTop: '1px' } }),
            jsx('span', { children: 'Turns below are derived HERE from session.history — turn = a '
              + 'user_message row, system notes excluded — so the cut is labelled by message index, '
              + 'not by any gateway turn number.' })
          ]
        }),
        jsxs('div', {
          className: 'flex flex-wrap items-center gap-2',
          children: [
            jsx('label', {
              className: 'flex items-center gap-1.5 text-[0.62rem] uppercase tracking-wider',
              style: { color: C.faint },
              children: [
                'turn',
                jsx('select', {
                  className: 'max-w-[320px] truncate rounded px-1.5 py-1 font-mono text-[0.68rem]',
                  style: { background: C.surface, color: C.text, border: '1px solid ' + C.border },
                  value: String(keepN || ''),
                  disabled: isBusy || !turns.length,
                  onChange: function (e) {
                    var v = Number(e.target.value) || 0
                    setKeepN(v)
                    setRawCount('')
                  },
                  children: turns.map(function (t) {
                    var lab = 'turn ' + t.n + ' · keep ' + t.keep + ' msg' + (t.preview ? ' · ' + t.preview : '')
                    return jsx('option', { value: String(t.keep), children: lab }, String(t.keep))
                  })
                })
              ]
            }),
            jsx('label', {
              className: 'flex items-center gap-1.5 text-[0.62rem] uppercase tracking-wider',
              style: { color: C.faint },
              children: [
                'or',
                jsx(Input, {
                  value: rawCount,
                  placeholder: 'message count',
                  disabled: isBusy,
                  className: 'w-32',
                  onChange: function (e) { setRawCount(e.target.value) }
                }),
                jsx(Kbd, { children: 'count' })
              ]
            }),
            jsx('span', {
              className: 'font-mono text-[0.64rem]',
              style: { color: keep ? C.emerald : C.faint },
              children: keep
                ? 'keep[' + keep + '] of ' + (msgTotal || '?') + ' msgs'
                : 'pick a turn or type a count'
            })
          ]
        }),
        jsxs('div', {
          className: 'flex items-center gap-2',
          children: [
            jsx(Button, {
              variant: 'secondary',
              disabled: !fromKey || !keep || isBusy,
              onClick: doBranch,
              children: busy === 'branch' ? 'Branching…' : 'Branch at message ' + (keep || '?')
            }),
            jsx('span', {
              className: 'text-[0.64rem]',
              style: { color: C.faint },
              children: 'keeps messages[0..' + Math.max(0, keep - 1) + '], drops the rest; records the cut on the parent'
            })
          ]
        })
      ]
    })
  }

  function previewLine(m) {
    if (!m) return '(end of transcript)'
    return (m.role || '?') + (m.name ? '/' + m.name : '') + ' · ' + hday05Flat(m).slice(0, 140)
  }

  function diffBody() {
    return jsxs('div', {
      className: 'flex flex-col gap-2',
      children: [
        blocked('BLOCKED: no server-side conversation diff (no session.diff / session.compare RPC) — '
          + 'the transcript half is computed HERE from two session.history reads. Worktree half runs '
          + 'in the backend: SHAs resolve only from rec[\'snaps\'], mixed roots are refused, and no ref '
          + 'is ever constructed from a session key.'),
        jsxs('div', {
          className: 'flex items-center gap-2',
          children: [
            jsx(Button, {
              variant: 'secondary',
              disabled: !fromKey || !toKey || isBusy,
              onClick: doDiff,
              children: busy === 'diff' ? 'Diffing…' : 'Run diff (worktree + transcript)'
            }),
            jsx('span', {
              className: 'text-[0.64rem]',
              style: { color: C.faint },
              children: fromKey && toKey && fromKey === toKey ? 'source and target are the same session'
                : 'latest snapshot on each side'
            })
          ]
        }),
        wtDiff ? wtSection() : null,
        convDiff ? convSection() : null
      ]
    })
  }

  function wtSection() {
    if (!wtDiff.ok) {
      return jsxs('div', {
        className: 'flex flex-col gap-1',
        children: [
          title('worktree (backend day-diff)'),
          jsx('div', {
            className: 'text-[0.66rem]',
            style: { color: C.red },
            children: String(wtDiff.error || 'day-diff failed')
          }),
          jsx('div', {
            className: 'text-[0.64rem]',
            style: { color: C.faint },
            children: 'day-diff only resolves SHAs from rec[\'snaps\'] — take a turn on both sides first.'
          })
        ]
      })
    }
    var a = wtDiff.a || {}
    var b = wtDiff.b || {}
    var files = wtDiff.files || []
    return jsxs('div', {
      className: 'flex flex-col gap-1.5',
      children: [
        title('worktree (backend day-diff)'),
        jsxs('div', {
          className: 'flex flex-wrap items-center gap-1.5',
          children: [
            chip('A #' + (a.n != null ? a.n : '?') + ' ' + String(a.sha || '').slice(0, 8) + ' ' + (a.label || ''), C.mono, 'sa'),
            jsx(Codicon, { name: 'arrow-right', style: { color: C.faint } }, 'ar'),
            chip('B #' + (b.n != null ? b.n : '?') + ' ' + String(b.sha || '').slice(0, 8) + ' ' + (b.label || ''), C.mono, 'sb'),
            wtDiff.same_sha ? chip('identical snapshots', C.emerald, 'same') : null,
            Array.isArray(wtDiff.stat) && wtDiff.stat.length
              ? chip(String(wtDiff.stat[wtDiff.stat.length - 1] || '').trim(), C.amber, 'statsum')
              : chip('no changes', C.emerald, 'nostat')
          ]
        }),
        files.length
          ? jsx('div', {
              className: 'flex flex-wrap gap-1',
              children: files.slice(0, 24).map(function (f, i) {
                return chip(f, C.muted, f + '-' + i)
              })
            })
          : null,
        wtDiff.note
          ? jsx('div', {
              className: 'text-[0.64rem]',
              style: { color: C.faint },
              children: wtDiff.note + (wtDiff.state_channel ? ' · channel: ' + wtDiff.state_channel : '')
            })
          : null
      ]
    })
  }

  function convSection() {
    var d = convDiff
    return jsxs('div', {
      className: 'flex flex-col gap-1.5',
      children: [
        title('transcript (client-side, computed in this panel)'),
        jsxs('div', {
          className: 'flex flex-wrap items-center gap-1.5',
          children: [
            chip('shared prefix: ' + d.shared + ' msgs', d.identical ? C.emerald : C.muted, 'shared'),
            d.identical ? chip('transcripts identical', C.emerald, 'ident')
              : chip('diverges at #' + (d.divergeAt + 1), C.amber, 'diverge'),
            chip('A ' + d.aLen + ' · B ' + d.bLen, C.muted, 'lens')
          ]
        }),
        d.identical ? null : jsxs('div', {
          className: 'flex flex-col gap-0.5 font-mono text-[0.64rem]',
          children: [
            jsx('div', { style: { color: C.muted }, children: 'A: ' + previewLine(d.aHere) }),
            jsx('div', { style: { color: C.muted }, children: 'B: ' + previewLine(d.bHere) })
          ]
        }),
        d.toolOnlyA.length || d.toolOnlyB.length
          ? jsxs('div', {
              className: 'flex flex-wrap items-center gap-1.5',
              children: [
                d.toolOnlyA.map(function (n) { return chip('only A: ' + n, C.amber, 'a-' + n) }),
                d.toolOnlyB.map(function (n) { return chip('only B: ' + n, C.emerald, 'b-' + n) })
              ]
            })
          : null
      ]
    })
  }

  function mergeBody() {
    var deliveries = [
      { v: 'auto', label: 'auto (user message when idle, steer mid-run)' },
      { v: 'user_message', label: 'new user message — prompt.submit' },
      { v: 'steer', label: 'inject mid-run — session.steer' },
      { v: 'redirect', label: 'replace active turn — session.redirect' }
    ]
    return jsxs('div', {
      className: 'flex flex-col gap-2',
      children: [
        blocked('BLOCKED: no session.merge RPC — nothing is spliced server-side. The digest below is '
          + 'delivered to the target as a user message (prompt.submit), injected into the next tool '
          + 'result (session.steer), or queued as an active-turn redirect (session.redirect); '
          + 'day-merge only records which route actually carried it.'),
        jsxs('div', {
          className: 'flex flex-wrap items-center gap-2',
          children: [
            jsx('label', {
              className: 'flex items-center gap-1.5 text-[0.62rem] uppercase tracking-wider',
              style: { color: C.faint },
              children: [
                'delivery',
                jsx('select', {
                  className: 'max-w-[300px] truncate rounded px-1.5 py-1 text-[0.68rem]',
                  style: { background: C.surface, color: C.text, border: '1px solid ' + C.border },
                  value: delivery,
                  disabled: isBusy,
                  onChange: function (e) { setDelivery(e.target.value) },
                  children: deliveries.map(function (d) {
                    return jsx('option', { value: d.v, children: d.label }, d.v)
                  })
                })
              ]
            }),
            jsx(Button, {
              variant: 'ghost',
              size: 'xs',
              disabled: !fromKey || isBusy,
              onClick: function () { buildDigest() },
              children: busy === 'digest' ? 'Building…' : 'Build digest'
            }),
            jsx(Button, {
              variant: 'secondary',
              disabled: !fromKey || !toKey || isBusy,
              onClick: doMerge,
              children: busy === 'merge' ? 'Sending…' : 'Merge into target'
            }),
            jsx('span', {
              className: 'font-mono text-[0.64rem]',
              style: { color: C.faint },
              children: digest ? digest.length + ' chars' : 'digest auto-builds on send'
            })
          ]
        }),
        jsx('textarea', {
          className: 'h-24 w-full resize-y rounded px-2 py-1.5 font-mono text-[0.66rem] leading-relaxed',
          style: { background: C.surface, color: C.text, border: '1px solid ' + C.border },
          value: digest,
          disabled: isBusy,
          placeholder: 'Digest: branch title-ish header, turn count, worktree delta from day-diff, key tool outcomes, last findings…',
          onChange: function (e) { setDigest(e.target.value) }
        }),
        jsx('div', {
          className: 'text-[0.64rem]',
          style: { color: C.faint },
          children: 'Delivered as plain text by the route above; recorded on the target as from/via/chars/… (day-merge).'
        })
      ]
    })
  }

  function teleportBody() {
    return jsxs('div', {
      className: 'flex flex-col gap-2',
      children: [
        blocked('BLOCKED: backend commands run agent-side and CANNOT move desktop focus. The gateway '
          + 'half of a teleport is session.activate (attach the frontend to the live target without '
          + 'closing the current one) — it does not focus anything. Focus happens here, desktop-side, '
          + 'via host.openSession; day-surgery teleports only records the bookkeeping.'),
        jsxs('div', {
          className: 'flex items-center gap-2',
          children: [
            jsx(Button, {
              variant: 'secondary',
              disabled: !toKey || isBusy,
              onClick: doTeleport,
              children: busy === 'teleport' ? 'Teleporting…' : 'Teleport to target'
            }),
            jsx('span', {
              className: 'text-[0.64rem]',
              style: { color: C.faint },
              children: toRow && toRow.live
                ? 'target is live (' + (toRow.status || 'idle') + ') — resume reuses it'
                : 'target is cold — resumed lazily first, then activated'
            })
          ]
        }),
        jsx('div', {
          className: 'text-[0.64rem]',
          style: { color: C.faint },
          children: 'sequence: session.resume (lazy) → session.activate → host.openSession → day-surgery teleport bookkeeping'
        })
      ]
    })
  }

  function ledgerBody() {
    if (!fromKey) return null
    if (ledgerQ.isLoading) {
      return jsx('div', {
        className: 'text-[0.66rem]',
        style: { color: C.faint },
        children: 'reading evidence ledger…'
      })
    }
    var data = ledgerQ.data
    if (!data) {
      return jsx('div', {
        className: 'text-[0.66rem]',
        style: { color: C.faint },
        children: 'no evidence ledger for this session yet — day-fork / day-branch / day-merge / '
          + 'day-surgery teleport write it.'
      })
    }
    if (!data.ok || !data.session) {
      return jsx('div', {
        className: 'text-[0.66rem]',
        style: { color: C.red },
        children: 'ledger: ' + (data.error || 'no record')
      })
    }
    var b = data.session
    var snaps = b.snaps || []
    var forks = b.forks || []
    var branches = b.branches || []
    var merges = b.merges || []
    var teleports = b.teleports || []
    var verdict = b.verdict || 'none'
    var vVariant = verdict === 'flagged' ? 'warn' : (verdict === 'ok' ? 'success' : 'muted')
    var items = []
    forks.slice(-3).forEach(function (f) {
      items.push({ k: 'fork → ' + f.to + ' · ' + (f.snaps_n != null ? f.snaps_n : '?') + ' snaps', at: f.at, c: C.amber })
    })
    branches.slice(-3).forEach(function (r) {
      items.push({ k: 'branch → ' + r.child + ' · count ' + r.count + ' msgs', at: r.at, c: C.amber })
    })
    merges.slice(-3).forEach(function (m) {
      items.push({ k: 'merge ← ' + m.from + ' via ' + m.via + ' · ' + m.chars + ' chars', at: m.at, c: C.emerald })
    })
    teleports.slice(-3).forEach(function (t) {
      items.push({ k: 'teleport → ' + t.to, at: t.at, c: C.emerald })
    })
    return jsxs('div', {
      className: 'flex flex-col gap-1.5',
      children: [
        jsxs('div', {
          className: 'flex items-center gap-2',
          children: [
            title('evidence ledger · ' + fromKey),
            jsx('div', { className: 'ml-auto', children: jsx(Badge, { size: 'xs', variant: vVariant, children: 'verdict: ' + verdict }) }),
            data.state_channel && data.state_channel !== 'live'
              ? jsx(Tip, {
                  label: 'Best-effort channel: the host plugin\'s in-memory map was not reachable, so reads/writes go through ctx.state and a later host persistence may overwrite them.',
                  children: jsx(Codicon, { name: 'warning', style: { color: C.amber } })
                })
              : null
          ]
        }),
        jsxs('div', {
          className: 'flex flex-wrap items-center gap-1.5',
          children: [
            chip('snaps ' + snaps.length, C.mono, 'snaps'),
            chip('forks ' + forks.length, C.muted, 'forks'),
            chip('branches ' + branches.length, C.muted, 'branches'),
            chip('merges ' + merges.length, C.muted, 'merges'),
            chip('teleports ' + teleports.length, C.muted, 'teleports'),
            b.fork_of ? chip('fork of ' + b.fork_of, C.amber, 'forkof') : null,
            b.merged_into ? chip('merged into ' + b.merged_into, C.emerald, 'mergedinto') : null
          ]
        }),
        snaps.length
          ? jsx('div', {
              className: 'flex flex-col gap-0.5',
              children: snaps.slice(-4).map(function (s, i) {
                return jsxs('div', {
                  className: 'flex items-center gap-2 font-mono text-[0.64rem]',
                  children: [
                    jsx('span', { style: { color: C.mono }, children: '#' + (s.n != null ? s.n : i) }),
                    jsx('span', { className: 'truncate', style: { color: C.muted }, children: s.label || '' }),
                    jsx('span', { style: { color: C.faint }, children: hday05When(s.ts) }),
                    jsx('span', { className: 'ml-auto shrink-0', style: { color: C.faint }, children: String(s.sha || '').slice(0, 8) })
                          ]
                        }, 'snap-' + String(s.n) + '-' + i)
              })
            })
          : null,
        items.length
          ? jsx('div', {
              className: 'flex flex-col gap-0.5',
              children: items.slice(-6).map(function (it, i) {
                return jsxs('div', {
                  className: 'flex items-center gap-2 text-[0.64rem]',
                  key: 'item-' + i + '-' + it.k,
                  children: [
                    jsx('span', { className: 'truncate', style: { color: it.c }, children: it.k }),
                    jsx('span', { className: 'ml-auto shrink-0', style: { color: C.faint }, children: hday05When(it.at) })
                  ]
                })
              })
            })
          : null,
        b.receipt && b.receipt.tree
          ? jsx('div', {
              className: 'text-[0.64rem] font-mono',
              style: { color: C.faint },
              children: 'receipt: ' + (b.receipt.verdict || '—') + ' · tree ' + String(b.receipt.tree).slice(0, 8)
                + ' · runs ' + (b.receipt.runs != null ? b.receipt.runs : '?')
                + ' · blocks ' + (b.receipt.blocks != null ? b.receipt.blocks : '?')
            })
          : null
      ]
    })
  }

  var TABS = [
    { id: 'fork', label: 'fork', icon: 'repo-forking' },
    { id: 'branch', label: 'branch at…', icon: 'git-branch' },
    { id: 'diff', label: 'diff', icon: 'diff' },
    { id: 'merge', label: 'merge', icon: 'git-merge' },
    { id: 'teleport', label: 'teleport', icon: 'link' }
  ]
  var body = tab === 'fork' ? forkBody()
    : tab === 'branch' ? branchBody()
    : tab === 'diff' ? diffBody()
    : tab === 'merge' ? mergeBody()
    : teleportBody()

  return jsxs('div', {
    className: 'flex flex-col gap-3',
    children: [
      // pickers
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsx(Codicon, { name: 'git-branch', style: { color: C.amber } }),
          jsx('span', {
            className: 'text-[0.7rem] font-semibold',
            style: { color: C.text },
            children: 'surgery'
          }),
          picker('source', fromKey, setFromKey),
          jsx(Codicon, { name: 'arrow-right', style: { color: C.faint } }),
          picker('target', toKey, setToKey),
          jsx('span', {
            className: 'ml-auto font-mono text-[0.6rem]',
            style: { color: C.faint },
            children: rows.length ? rows.length + ' sessions · gateway: local route'
              : (listQ.isLoading ? 'loading sessions…' : 'no sessions found')
          })
        ]
      }),
      // tabs (GhostDock / INSP_TABS pattern)
      jsxs('div', {
        className: 'flex items-center gap-1',
        children: TABS.map(function (t) {
          return jsx('button', {
            type: 'button',
            disabled: isBusy,
            onClick: function () { setTab(t.id) },
            className: cn('hday-tab flex items-center gap-1 rounded-t px-2 py-1.5 text-[0.68rem] font-medium', tab === t.id && 'hday-tab-on'),
            style: tab === t.id ? { color: C.text } : { color: C.muted },
            children: [
              jsx(Codicon, { name: t.icon, style: { color: tab === t.id ? C.amber : C.faint } }),
              t.label
            ]
          }, t.id)
        })
      }),
      body,
      ledgerBody(),
      note
        ? jsx('div', {
            className: 'text-[0.68rem]',
            style: { color: note.charAt(0) === '!' ? C.red : C.emerald },
            children: note.charAt(0) === '!' ? note.slice(1) : note
          })
        : null
    ]
  })
}

// ---- lane 06: git-forge ----
// Lane 06 — Git Forge panel (splice-ready snippet; NOT a module).
// No import/export: this file must parse as a plain script (`node --check`).
// Tabs: PRs | Issues | Diff | Release. Polling is throttled on purpose:
// staleTime 60s, no refetch interval, refetch only when the window regains
// focus — the panel never spawns `gh` in a loop. Every colour comes from the
// `C` token object or a theme var; there is not one literal colour here.

function hdayForgeEmpty(reason) {
  return {
    ok: true, empty: true, reason: reason || '', generated_at: 0,
    repo: null, root: null, default_branch: null, head: null,
    auth: { ok: false, login: null },
    prs: [], issues: [], checks: null, diff: null, runs: [],
    release: { tags: [], head: null, next_tag: 'v0.1.1', dirty: false,
               can_tag: false, can_push: false, last: null },
    actions: [], error: null, performed: null
  };
}

function hdayForgeParse(disp) {
  try {
    var out = '';
    if (typeof disp === 'string') out = disp;
    else if (disp) out = disp.output || disp.text || '';
    if (out && String(out).trim().charAt(0) === '{') return JSON.parse(out);
    return hdayForgeEmpty(out ? '' : 'no response from the agent half');
  } catch (e) {
    return hdayForgeEmpty(String(e && e.message ? e.message : e));
  }
}

function hdayForgeDispatch(route, arg) {
  try {
    var p = rpc(route, 'command.dispatch', { name: 'day-forge', arg: String(arg) });
    if (!p || typeof p.then !== 'function') {
      return Promise.resolve(hdayForgeEmpty('no response from the agent half'));
    }
    return p.then(function (d) { return hdayForgeParse(d); },
                  function (e) { return hdayForgeEmpty(String(e && e.message ? e.message : e)); });
  } catch (e) {
    return Promise.resolve(hdayForgeEmpty(String(e)));
  }
}

// UI state lives in atoms cached on a stable module-scope object, so renders
// never re-create them (this file may only declare top-level functions).
function hdayForgeAtoms() {
  function build() {
    return {
      tab: atom('prs'), sel: atom(null), mode: atom('none'),
      verb: atom('comment'), text: atom(''), arm: atom(null),
      tag: atom(''), busy: atom(null), strip: atom(null)
    };
  }
  try {
    if (typeof host !== 'undefined' && host && !host.__hdayForge06) host.__hdayForge06 = build();
    if (typeof host !== 'undefined' && host && host.__hdayForge06) return host.__hdayForge06;
  } catch (e) { /* fall through */ }
  try {
    if (typeof globalThis !== 'undefined' && !globalThis.__hdayForge06) globalThis.__hdayForge06 = build();
    if (typeof globalThis !== 'undefined' && globalThis.__hdayForge06) return globalThis.__hdayForge06;
  } catch (e) { /* fall through */ }
  return build();
}

function hdayForgeWhen(u) {
  try {
    var ms = Date.parse(u || '');
    return isFinite(ms) && ms ? relativeTime(ms) : '';
  } catch (e) { return ''; }
}

function hdayForgeStamp(generatedAt) {
  try {
    if (!generatedAt) return '';
    return hdayForgeWhen(new Date(generatedAt * 1000).toISOString());
  } catch (e) { return ''; }
}

function hdayForgeLines(text) {
  try { return String(text || '').split('\n').length; } catch (e) { return 0; }
}

// §3.1 check pill states: ✓ n/n · ✗ k/n · ◐ running · – no checks · … loading
function hdayForgePill(checks, loading) {
  if (loading) return { text: '…', color: C.faint, title: 'fetching checks' };
  if (!checks) return null;
  if (checks.state === 'PENDING') {
    return { text: '◐ ' + (checks.done || 0) + '/' + (checks.total || 0),
             color: C.amber, title: 'running' };
  }
  if (checks.state === 'FAILURE') {
    return { text: '✗ ' + (checks.failed || 0) + '/' + (checks.total || 0),
             color: C.red, title: 'failing' };
  }
  if (checks.state === 'SUCCESS') {
    return { text: '✓ ' + (checks.total || 0) + '/' + (checks.total || 0),
             color: C.emerald, title: 'all green' };
  }
  if (checks.state === 'NONE') return { text: '– no checks', color: C.faint, title: '' };
  return { text: '…', color: C.faint, title: '' };
}

function hdayForgePillView(pill) {
  if (!pill) return null;
  return jsx('span', {
    className: 'hday-chip tabular-nums',
    style: { color: pill.color },
    title: pill.title,
    children: pill.text
  });
}

function hdayForgeHead(props) {
  var data = props.data;
  var authed = Boolean(data.auth && data.auth.ok);
  var repoLine = data.repo
    ? data.repo + ' · ' + (data.head || '?') + (data.default_branch ? ' · ' + data.default_branch : '')
    : 'no origin remote — not a GitHub checkout';
  return jsxs('div', { className: 'hday-panel-head', children: [
    jsx(Badge, { tone: 'info', children: 'Git Forge' }),
    jsx('span', { className: 'hday-muted', style: { fontFamily: 'inherit' }, children: repoLine }),
    jsx(Badge, { tone: authed ? 'info' : 'warn',
                 children: authed ? ('gh: ' + data.auth.login) : 'gh: unauthenticated' })
  ] });
}

function hdayForgeTabs(props) {
  var tabs = [['prs', 'PRs'], ['issues', 'Issues'], ['diff', 'Diff'], ['release', 'Release']];
  return jsx('div', { className: 'hday-chips', children: tabs.map(function (t) {
    var on = props.tab === t[0];
    return jsx('button', {
      type: 'button',
      className: cn('hday-chip', on && 'is-active'),
      style: { color: on ? C.mono : C.faint,
               borderBottom: on ? '1px solid ' + C.mono : '1px solid transparent' },
      onClick: function () { props.onTab(t[0]); },
      children: t[1],
      key: t[0]
    });
  }) });
}

// §3.4 — the mandatory empty state, never an exception.
function hdayForgeEmptyState(props) {
  var data = props.data;
  var open = (data.prs || []).length + (data.issues || []).length;
  var tags = ((data.release && data.release.tags) || []).length;
  var reason = data.reason || (data.auth && data.auth.ok ? '' : 'gh not authenticated');
  return jsxs('div', { className: 'hday-empty', children: [
    jsxs('div', { children: [
      jsx('span', { style: { color: C.text, fontWeight: 600 },
                    children: open + ' OPEN WORK' }),
      jsx('span', { style: { color: C.faint }, children: ' · ' }),
      jsx('span', { style: { color: C.text, fontWeight: 600 },
                    children: tags + ' TAGS' })
    ] }),
    jsx('p', { style: { color: C.muted },
               children: 'No open pull requests. No open issues.' }),
    reason ? jsx('p', { style: { color: C.faint }, children: reason }) : null,
    jsxs('div', { className: 'hday-actions', children: [
      jsx(Button, { variant: 'secondary', onClick: props.onRescan,
                    children: 'Re-scan' }),
      jsx(Button, { variant: 'ghost', onClick: function () { props.onTab('release'); },
                    children: 'Release lane →' })
    ] }),
    jsxs(Tip, { children: [
      'Checks run only when a row is selected. Polling is throttled: 60s stale, ',
      'refetch on focus only — the panel does not spawn gh in a loop.'
    ] })
  ] });
}

function hdayForgePrTable(props) {
  var prs = props.prs;
  var sel = props.sel;
  return jsxs('div', { children: [
    jsxs('div', { className: 'hday-row', style: { color: C.faint },
                  children: [
                    jsx('span', { style: { width: '3rem' }, children: 'pr' }),
                    jsx('span', { style: { flex: 1 }, children: 'title' }),
                    jsx('span', { style: { width: '9rem' }, children: 'head' }),
                    jsx('span', { style: { width: '7rem' }, children: 'rev' }),
                    jsx('span', { style: { width: '6rem' }, children: 'upd' }),
                    jsx('span', { style: { width: '7rem' }, children: 'checks' })
                  ] }),
    prs.map(function (pr, i) {
      var on = sel === pr.number;
      var pill = on ? hdayForgePill(props.checks, props.checksLoading) : null;
      return jsx('div', {
        className: cn('hday-row', on && 'hday-sel'),
        onClick: function () { props.onSelect(pr.number); },
        children: [
          jsx('span', { style: { width: '3rem', color: C.mono },
                        children: '#' + pr.number }),
          jsx('span', { style: { flex: 1, color: C.text },
                        children: pr.title }),
          jsx('span', { style: { width: '9rem', color: C.slate },
                        children: pr.head || '' }),
          jsx('span', { style: { width: '7rem' },
                        children: jsx(Badge, {
                          tone: String(pr.review || '').toUpperCase() === 'APPROVED' ? 'info' : 'muted',
                          children: pr.review || pr.state || ''
                        }) }),
          jsx('span', { style: { width: '6rem', color: C.faint },
                        children: hdayForgeWhen(pr.updated) }),
          jsx('span', { style: { width: '7rem' }, children: hdayForgePillView(pill) })
        ],
        key: 'pr-' + (pr.number || i)
      });
    })
  ] });
}

function hdayForgeIssueTable(props) {
  var issues = props.issues;
  var sel = props.sel;
  if (!issues.length) {
    return jsx('p', { className: 'hday-empty', children: 'No open issues.' });
  }
  return jsx('div', { children: issues.map(function (it, i) {
    var on = sel === it.number;
    return jsx('div', {
      className: cn('hday-row', on && 'hday-sel'),
      onClick: function () { props.onSelect(it.number); },
      children: [
        jsx('span', { style: { width: '3rem', color: C.mono },
                      children: '#' + it.number }),
        jsx('span', { style: { flex: 1, color: C.text }, children: it.title }),
        jsx('span', { children: (it.labels || []).map(function (l, j) {
          return jsx(Badge, { tone: 'muted', children: l, key: 'l' + j });
        }) }),
        jsx('span', { style: { width: '6rem', color: C.faint },
                      children: hdayForgeWhen(it.updated) })
      ],
      key: 'is-' + (it.number || i)
    });
  }) });
}

function hdayForgeDiff(props) {
  var diff = props.diff;
  if (!props.prNumber) {
    return jsx('p', { className: 'hday-muted',
                      children: 'Select a PR first — press d with a row chosen.' });
  }
  if (props.loading) {
    return jsx('p', { className: 'hday-muted', children: 'fetching diff…' });
  }
  if (!diff || !diff.text) {
    return jsx('p', { className: 'hday-muted',
                      children: 'No diff returned for #' + props.prNumber +
                        (props.reason ? ' — ' + props.reason : '') });
  }
  return jsxs('div', { children: [
    jsxs('div', { className: 'hday-receipt-row', style: { color: C.faint },
                  children: [
                    jsx('span', { children: 'pr #' + diff.number + ' · ' +
                      hdayForgeLines(diff.text) + ' shown / ' + (diff.total || 0) + ' lines' }),
                    diff.truncated ? jsx('span', { children: 'head-capped at 400 lines' }) : null
                  ] }),
    jsx('pre', { className: 'hday-pre',
                 style: { color: C.text, whiteSpace: 'pre-wrap', maxHeight: '24rem',
                          overflow: 'auto' },
                 children: diff.text }),
    props.url ? jsx('div', { style: { color: C.faint, fontSize: '0.66rem',
                                      wordBreak: 'break-all' },
                             children: props.url }) : null
  ] });
}

function hdayForgeRelease(props) {
  var rel = props.release || {};
  var tags = rel.tags || [];
  var next = props.tagValue || rel.next_tag || 'v0.1.1';
  return jsxs('div', { className: 'hday-receipt', children: [
    jsxs('div', { className: 'hday-receipt-head', children: [
      'release lane',
      jsx(Badge, { tone: rel.dirty ? 'warn' : 'info',
                   children: rel.dirty ? 'dirty' : 'clean' })
    ] }),
    jsxs('div', { className: 'hday-receipt-row', children: [
      jsx('span', { children: 'head' }),
      jsx('span', { style: { color: C.mono }, children: rel.head || '?' })
    ] }),
    jsxs('div', { className: 'hday-receipt-row', children: [
      jsx('span', { children: 'tags' }),
      jsx('span', { style: { color: tags.length ? C.text : C.faint },
                    children: String(tags.length) + ' (' +
                      (tags.length ? tags[tags.length - 1] : 'none') + ')' })
    ] }),
    jsxs('div', { className: 'hday-receipt-row', children: [
      jsx('span', { children: 'next' }),
      jsx('span', { style: { color: C.emerald }, children: next })
    ] }),
    jsx('div', { className: 'hday-fields', children: jsx('label', { children: [
      jsx('span', { children: 'tag (semver, vMAJOR.MINOR.PATCH)' }),
      jsx('input', {
        type: 'text',
        value: next,
        onChange: function (e) { props.onTag(e.target.value); }
      })
    ] }) }),
    jsxs('div', { className: 'hday-actions', children: [
      jsx(Button, { variant: 'ghost', disabled: Boolean(props.busy) || !rel.can_tag,
                    onClick: function () { props.onRelease('local'); },
                    children: 'Tag' }),
      jsx(Button, { variant: 'secondary', disabled: Boolean(props.busy),
                    onClick: function () { props.onRelease('push'); },
                    children: 'Tag & Push' }),
      jsx(Button, { variant: 'primary', disabled: Boolean(props.busy),
                    onClick: function () { props.onRelease('gh'); },
                    children: 'Create GitHub Release' })
    ] }),
    jsxs('div', { className: 'hday-receipt-row', style: { color: C.faint },
                  children: [
                    jsx('span', { children: 'workflow runs' }),
                    jsx('span', { children: (props.runs || []).length
                      ? (props.runs[0].workflow || 'run') + ' · ' +
                        (props.runs[0].conclusion || props.runs[0].status || '?')
                      : '0' })
                  ] }),
    jsx('div', { children: jsx(Tip, { children:
      'A dirty worktree refuses to tag. Push is never forced: a rejected push is reported exactly as git said it.' }) })
  ] });
}

function hdayPanel06(props) {
  props = props || {};
  var route = props.route || null;
  var A = hdayForgeAtoms();

  var tab = useValue(A.tab);
  var sel = useValue(A.sel);
  var mode = useValue(A.mode);
  var verb = useValue(A.verb);
  var text = useValue(A.text);
  var arm = useValue(A.arm);
  var tagValue = useValue(A.tag);
  var busy = useValue(A.busy);
  var strip = useValue(A.strip);

  // Throttled list query: 60s stale, no interval, refetch on focus only.
  var forgeQ = useQuery({
    queryKey: ['hday-forge', 'list'],
    queryFn: function () { return hdayForgeDispatch(route, 'list'); },
    staleTime: 60000,
    refetchInterval: false,
    refetchOnWindowFocus: true,
    refetchOnMount: false,
    refetchOnReconnect: false,
    retry: false
  });

  var data = forgeQ.data || hdayForgeEmpty('');
  var prs = data.prs || [];
  var issues = data.issues || [];
  // checks/diff attach to a PR number only; an issue number never fetches them
  var prSel = null;
  for (var i = 0; i < prs.length; i++) {
    if (prs[i].number === sel) { prSel = sel; break; }
  }

  var checksQ = useQuery({
    queryKey: ['hday-forge', 'checks', prSel],
    queryFn: function () { return hdayForgeDispatch(route, 'checks ' + prSel); },
    enabled: Boolean(prSel) && (tab === 'prs' || tab === 'diff'),
    staleTime: 60000,
    refetchInterval: false,
    refetchOnWindowFocus: true,
    refetchOnMount: false,
    refetchOnReconnect: false,
    retry: false
  });

  var diffQ = useQuery({
    queryKey: ['hday-forge', 'diff', prSel],
    queryFn: function () { return hdayForgeDispatch(route, 'diff ' + prSel); },
    enabled: Boolean(prSel) && tab === 'diff',
    staleTime: 60000,
    refetchInterval: false,
    refetchOnWindowFocus: true,
    refetchOnMount: false,
    refetchOnReconnect: false,
    retry: false
  });

  function rescan() {
    try { if (forgeQ && forgeQ.refetch) forgeQ.refetch(); } catch (e) { /* ignore */ }
  }

  // one command dispatch + one strip line; never throws to the render path
  function act(arg, okLine) {
    A.busy.set(arg);
    return hdayForgeDispatch(route, arg).then(function (env) {
      A.busy.set(null);
      var line = okLine || 'ok';
      if (env.performed) {
        var p = env.performed;
        line = p.action + ' ' + (p.tag || p.target || '') +
          (p.pushed ? ' · pushed' : '') + (p.released ? ' · released' : '');
        if (p.error) line += ' · ' + p.error;
      } else if (env.error) {
        line = env.error;
      } else if (env.empty && env.reason) {
        line = env.reason;
      }
      A.strip.set({ kind: env.error || (env.performed && env.performed.error) || !env.ok ? 'err' : 'ok',
                    line: line });
      rescan();
      return env;
    });
  }

  function submit() {
    if (mode === 'comment') {
      if (!text || !String(text).trim()) return;
      var cArg = 'comment ' + prSel + ' ' + text;
      A.mode.set('none');
      A.text.set('');
      act(cArg, 'comment sent');
      return;
    }
    if (mode === 'review') {
      var rArg = 'review ' + prSel + ' ' + verb + (text && String(text).trim() ? ' ' + text : '');
      A.mode.set('none');
      A.text.set('');
      act(rArg, 'review posted');
      return;
    }
    if (tab === 'release') {
      var t = tagValue || (data.release && data.release.next_tag) || 'v0.1.1';
      act('release ' + t, 'tagged');
    }
  }

  function fireArmed() {
    if (!arm) return;
    var parts = String(arm).split(':');
    if (parts[0] === 'approve' && parts[1]) {
      var arg = 'review ' + parts[1] + ' approve' +
        (text && String(text).trim() ? ' ' + text : '');
      A.arm.set(null);
      A.text.set('');
      act(arg, 'approved');
    }
  }

  function move(delta) {
    var rows = tab === 'issues' ? issues : prs;
    if (!rows.length) return;
    var idx = -1;
    for (var i = 0; i < rows.length; i++) if (rows[i].number === sel) idx = i;
    var next = idx < 0 ? 0 : idx + delta;
    if (next < 0) next = 0;
    if (next > rows.length - 1) next = rows.length - 1;
    A.sel.set(rows[next].number);
  }

  function onKey(e) {
    try {
      var k = e && e.key;
      var tgt = e && e.target ? e.target : null;
      var tagName = tgt && tgt.tagName ? String(tgt.tagName).toUpperCase() : '';
      var typing = tagName === 'INPUT' || tagName === 'TEXTAREA';
      if (k === 'Escape') {
        A.mode.set('none'); A.text.set(''); A.arm.set(null);
        return;
      }
      if (k === 'Enter') {
        if (typing) { e.preventDefault(); submit(); return; }
        fireArmed();
        return;
      }
      if (typing) return;
      if (k === 'ArrowDown') { e.preventDefault(); move(1); return; }
      if (k === 'ArrowUp') { e.preventDefault(); move(-1); return; }
      var ch = k && k.length === 1 ? String(k).toLowerCase() : '';
      if (ch === 'd') {
        if (!prSel && prs.length) A.sel.set(prs[0].number);
        A.tab.set('diff');
      } else if (ch === 'c') {
        if (prSel) { A.mode.set('comment'); A.text.set(''); A.arm.set(null); }
      } else if (ch === 'r') {
        if (prSel) { A.mode.set('review'); A.verb.set('comment'); A.arm.set(null); }
      } else if (ch === 'a') {
        if (prSel) A.arm.set('approve:' + prSel);
      } else if (ch === 't') {
        A.tab.set('release');
      }
    } catch (err) { /* never wedge the panel on a key */ }
  }

  var emptyNow = Boolean(data.empty) || (prs.length + issues.length === 0);
  var checksData = checksQ.data || null;
  var diffData = diffQ.data && diffQ.data.diff ? diffQ.data.diff : null;

  var body;
  if (emptyNow && tab !== 'release') {
    body = jsx(hdayForgeEmptyState, {
      data: data, onRescan: rescan, onTab: function (t) { A.tab.set(t); }, key: 'empty'
    });
  } else if (tab === 'prs') {
    body = prs.length
      ? jsx(hdayForgePrTable, { prs: prs, sel: prSel, checks: checksData && checksData.checks,
                                checksLoading: Boolean(prSel) && checksQ.isLoading,
                                onSelect: function (n) { A.sel.set(n); A.mode.set('none'); A.arm.set(null); },
                                key: 'prs' })
      : jsx('p', { className: 'hday-empty', children: 'No open pull requests.' });
  } else if (tab === 'issues') {
    body = jsx(hdayForgeIssueTable, { issues: issues, sel: sel,
                                      onSelect: function (n) { A.sel.set(n); }, key: 'issues' });
  } else if (tab === 'diff') {
    body = jsx(hdayForgeDiff, { diff: diffData, prNumber: prSel,
                                loading: Boolean(prSel) && diffQ.isLoading,
                                reason: (diffQ.data && diffQ.data.reason) || '',
                                url: prSel && prs.length
                                  ? (function () {
                                      for (var i = 0; i < prs.length; i++) {
                                        if (prs[i].number === prSel) return prs[i].url;
                                      }
                                      return '';
                                    })()
                                  : '',
                                key: 'diff' });
  } else {
    body = jsx(hdayForgeRelease, {
      release: data.release, runs: data.runs, tagValue: tagValue, busy: busy,
      onTag: function (v) { A.tag.set(v); },
      onRelease: function (m) {
        var t = tagValue || (data.release && data.release.next_tag) || 'v0.1.1';
        var arg = 'release ' + t + (m === 'gh' ? ' gh ' + t : m === 'local' ? ' local' : '');
        act(arg, m === 'local' ? 'tagged' : 'tagged & pushed');
      },
      key: 'release'
    });
  }

  var composer = null;
  if (prSel && (mode === 'comment' || mode === 'review')) {
    composer = jsxs('div', { className: 'hday-actions', children: [
      jsx('input', {
        type: 'text',
        value: text,
        placeholder: mode === 'comment'
          ? ('comment on #' + prSel + ' — Enter sends, Esc closes')
          : ('review #' + prSel + ' (' + verb + ') — Enter posts'),
        onChange: function (e) { A.text.set(e.target.value); },
        key: 'input'
      }),
      mode === 'review' ? ['approve', 'request-changes', 'comment'].map(function (v) {
        return jsx(Button, {
          variant: v === verb ? 'primary' : 'ghost',
          onClick: function () { A.verb.set(v); },
          children: v,
          key: 'v-' + v
        });
      }) : null,
      jsx(Button, { variant: 'ghost', onClick: function () { A.mode.set('none'); A.text.set(''); },
                    children: 'Cancel', key: 'cancel' })
    ] });
  } else if (arm) {
    composer = jsx('div', { className: 'hday-actions', children: [
      jsx('span', { style: { color: C.amber },
                    children: 'armed: approve ' + String(arm).split(':')[1] +
                      ' — press Enter (Esc cancels)' })
    ] });
  }

  var stripView = null;
  if (strip) {
    stripView = jsx('div', {
      className: 'hday-receipt',
      style: { color: strip.kind === 'err' ? C.red : C.emerald },
      children: strip.line
    });
  }

  return jsx('div', {
    className: cn('hday-panel', 'hday-forge', props.className),
    tabIndex: 0,
    onKeyDown: onKey,
    children: jsxs('div', { children: [
      jsx(hdayForgeHead, { data: data, key: 'head' }),
      jsx(hdayForgeTabs, { tab: tab, onTab: function (t) {
        A.tab.set(t); A.mode.set('none'); A.arm.set(null);
      }, key: 'tabs' }),
      body,
      composer,
      stripView,
      jsx('div', { className: 'hday-row', style: { color: C.faint, fontSize: '0.62rem' },
                   children: busy ? ('running: ' + busy)
                     : 'keys: ↑/↓ select · c comment · r review · a approve (Enter fires) · d diff · t release · Esc closes' }),
      jsx('div', { style: { color: C.faint, fontSize: '0.6rem' },
                   children: 'generated ' + hdayForgeStamp(data.generated_at) })
    ] })
  });
}

// ---- lane 07: log-terminal ----
// Lane 07 — Live Log Terminal (splice-ready snippet: NO imports, NO exports,
// top-level function declarations only; parses as a plain script).
//
// The backend reads only bytes appended since the last poll (byte-offset
// cursor, 64 KiB page cap — patches/07-log-terminal.py), so this panel
// ACCUMULATES pages into a ring buffer, filters with a live regex + match
// badge, highlights ERROR/WARN, groups consecutive repeats with a count, and
// offers pause/resume + jump-to-tail. Only lines the backend actually
// returned are ever rendered — no synthetic rows.

/** Singleton store, held on the function object: the snippet may not declare
 *  top-level `const`, and atoms must survive re-renders. */
function hdayPanel07Store() {
  if (!hdayPanel07Store._s) {
    hdayPanel07Store._s = {
      path: atom(''),
      regex: atom(''),
      paused: atom(false),
      follow: atom(true),
      wantReset: false,
      buf: [],
      bufPath: null,
      lastSeq: 0
    };
  }
  return hdayPanel07Store._s;
}

/** Read an atom whether useValue hands back the atom or the raw value. */
function hdayPanel07Read(v, fallback) {
  if (v && typeof v === 'object' && typeof v.get === 'function') return v.get();
  return v === undefined || v === null ? fallback : v;
}

function hdayPanel07RouteKey(route) {
  if (!route) return 'local'
  return String(route.connectionId || '') + '/' + String(route.profile || '') +
    '/' + String(route.targetProfile || '')
}

/** Client-side fallback when the backend line carries no level. */
function hdayPanel07Level(text) {
  var t = String(text == null ? '' : text)
  if (/\b(ERROR|FATAL|CRITICAL|PANIC)\b/i.test(t)) return 'error'
  if (/\bWARN(ING)?\b/i.test(t)) return 'warn'
  return 'info'
}

function hdayPanel07Color(level) {
  if (level === 'error') return C.red
  if (level === 'warn') return C.amber
  return C.muted
}

function hdayPanel07Num(n) {
  return typeof n === 'number' && isFinite(n) ? String(n) : '—'
}

function hdayPanel07Compile(src) {
  if (!src) return { re: null, bad: null }
  try { return { re: new RegExp(src, 'i'), bad: null } } catch (e) {
    return { re: null, bad: String((e && e.message) || e) }
  }
}

/** Group consecutive identical lines AFTER filtering → one row + ×count. */
function hdayPanel07Group(rows) {
  var out = []
  for (var i = 0; i < rows.length; i++) {
    var last = out.length ? out[out.length - 1] : null
    if (last && String(last.row.text) === String(rows[i].text)) last.count += 1
    else out.push({ row: rows[i], count: 1 })
  }
  return out
}

function hdayPanel07ScrollTail() {
  try {
    var el = typeof document !== 'undefined'
      ? document.querySelector('.hday-log07-body') : null
    if (el) el.scrollTop = el.scrollHeight
  } catch (e) {}
}

function hdayPanel07Fail(store, reason) {
  return {
    ok: false, reason: reason, error: reason, empty: true, lines: [],
    buf: store.buf.slice(), at: Date.now(), polled: 0
  }
}

/** One poll: dispatch /day-logs, merge new lines into the buffer. */
function hdayPanel07QueryFn(route, path, store) {
  return function poll() {
    var doReset = !!store.wantReset
    store.wantReset = false
    if (doReset || store.bufPath !== path) {
      store.buf = []
      store.lastSeq = 0
      store.bufPath = path
    }
    var arg = JSON.stringify({ path: path, reset: doReset ? true : undefined })
    return Promise.resolve(
      rpc(route, 'command.dispatch', { name: 'day-logs', arg: arg })
    ).then(function (disp) {
      var env = null
      if (disp && typeof disp === 'object' &&
          ('output' in disp || 'text' in disp)) {
        var out = disp.output || disp.text || ''
        if (out && typeof out === 'object') env = out
        else if (typeof out === 'string' && out.trim().charAt(0) === '{') {
          try { env = JSON.parse(out) } catch (e) { env = null }
        }
      } else if (disp && typeof disp === 'object') {
        env = disp
      }
      if (!env || typeof env !== 'object') {
        return hdayPanel07Fail(store, 'no JSON envelope from /day-logs')
      }
      var got = Array.isArray(env.lines) ? env.lines : []
      // backend cursor restarted (fresh seq 1) — rebuild instead of dropping
      if (got.length && Number(got[0].seq) === 1 && store.lastSeq > 1) {
        store.buf = []
        store.lastSeq = 0
      }
      for (var i = 0; i < got.length; i++) {
        var ln = got[i]
        if (!ln || typeof ln !== 'object') continue
        var seq = Number(ln.seq)
        if (!isFinite(seq) || seq <= 0) seq = store.lastSeq + 1
        if (seq <= store.lastSeq) continue
        store.buf.push({
          seq: seq,
          text: typeof ln.text === 'string'
            ? ln.text
            : (ln.text === undefined || ln.text === null ? '' : String(ln.text)),
          level: ln.level === 'error' || ln.level === 'warn' ? ln.level : null,
          partial: !!ln.partial
        })
        store.lastSeq = seq
      }
      if (store.buf.length > 5000) {
        store.buf = store.buf.slice(store.buf.length - 5000)
      }
      if (hdayPanel07Read(store.follow, true)) {
        try { setTimeout(hdayPanel07ScrollTail, 80) } catch (e) {}
      }
      return {
        ok: env.ok !== false,
        reason: env.reason || null,
        error: env.error || null,
        // empty state: backend said so, or this poll produced no lines at all
        // (buf stays authoritative for what is on screen)
        empty: !!env.empty || got.length === 0,
        path: env.path || path,
        buf: store.buf.slice(),
        offset: env.offset,
        size: env.size,
        pending: !!env.pending,
        truncated: !!env.truncated,
        omittedBefore: env.omitted_before || 0,
        dropped: env.dropped || 0,
        rotated: !!env.rotated,
        partial: !!env.partial,
        polled: got.length,
        at: Date.now()
      }
    })
  }
}

/** ErrorState for transport failures when the host bundle provides it (§3.5),
 *  otherwise an inline equivalent — the snippet must not crash bare. */
function hdayPanel07ErrState(props) {
  var title = (props && props.title) || 'Log poll failed'
  var description = (props && props.description) || ''
  try {
    if (typeof ErrorState !== 'undefined' && ErrorState) {
      return jsx(ErrorState, { title: title, description: description })
    }
  } catch (e) {}
  return jsxs('div', {
    className: 'hday-receipt',
    style: { color: C.red },
    children: [
      jsxs('div', {
        key: 'h',
        style: { display: 'flex', alignItems: 'center', gap: '.4rem' },
        children: [
          jsx(Codicon, { key: 'i', name: 'warning', style: { color: C.red } }),
          jsx('span', { key: 't', children: title })
        ]
      }),
      jsx('div', { key: 'd', style: { color: C.muted, marginTop: '.25rem' },
        children: description })
    ]
  })
}

function hdayPanel07(props) {
  var route = (props && props.route) || null
  var store = hdayPanel07Store()

  // reactive atoms — called unconditionally, in a stable order
  var pathV = String(hdayPanel07Read(useValue(store.path), ''))
  var regexV = String(hdayPanel07Read(useValue(store.regex), ''))
  var paused = !!hdayPanel07Read(useValue(store.paused), false)
  var follow = !!hdayPanel07Read(useValue(store.follow), true)

  var q = useQuery({
    queryKey: ['hday-log07', hdayPanel07RouteKey(route), pathV],
    queryFn: hdayPanel07QueryFn(route, pathV, store),
    enabled: !paused && pathV.trim() !== '',
    refetchInterval: paused ? false : 2000,
    refetchOnWindowFocus: false,
    staleTime: 750
  })

  var data = q && q.data ? q.data : null
  var buf = data && data.buf ? data.buf : store.buf.slice()

  // ---- filter + match badge ---------------------------------------------
  var comp = hdayPanel07Compile(regexV)
  var filtered = []
  for (var bi = 0; bi < buf.length; bi++) {
    if (!comp.re || comp.re.test(buf[bi].text)) filtered.push(buf[bi])
  }
  var groups = hdayPanel07Group(filtered)

  var transportErr = q && q.isError
    ? String((q.error && q.error.message) || q.error || 'transport failure')
    : null
  var backendErr = data && data.ok === false
    ? (data.reason || data.error || 'backend reported failure')
    : null

  var emptyReason = null
  if (!pathV.trim()) emptyReason = 'No path set — /day-logs <path> [tail|reset]'
  else if (!transportErr && !backendErr) {
    if (data && data.empty && !buf.length) {
      emptyReason = data.reason || 'no lines yet'
    } else if (!buf.length && q && q.isLoading) emptyReason = 'polling…'
    else if (!buf.length && paused) emptyReason = 'polling paused — no lines buffered yet'
  }

  // ---- handlers (imperative, never inside the render body) ---------------
  function setPath(v) {
    store.path.set(v)
    store.buf = []
    store.lastSeq = 0
    store.bufPath = v
  }
  function refetch() {
    try { if (q && q.refetch) q.refetch() } catch (e) {}
  }
  function togglePause() {
    var next = !paused
    store.paused.set(next)
    if (!next) refetch()
  }
  function jumpTail() {
    try { store.follow.set(true) } catch (e) {}
    hdayPanel07ScrollTail()
  }
  function tailFromEnd() {
    try {
      store.wantReset = true
      store.buf = []
      store.lastSeq = 0
      store.bufPath = pathV
    } catch (e) {}
    refetch()
    hdayPanel07ScrollTail()
  }
  function onBodyScroll(ev) {
    try {
      var el = ev.currentTarget
      var atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24
      if (hdayPanel07Read(store.follow, true) !== atBottom) {
        store.follow.set(atBottom)
      }
    } catch (e) {}
  }
  function onKey(ev) {
    try {
      var t = ev.target
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' ||
                t.isContentEditable)) return
      var k = String(ev.key || '').toLowerCase()
      if (k === 'p') { ev.preventDefault(); togglePause() }
      else if (k === 't') { ev.preventDefault(); jumpTail() }
      else if (k === 'r') { ev.preventDefault(); refetch() }
    } catch (e) {}
  }

  // ---- body --------------------------------------------------------------
  var bodyKids
  if (transportErr) {
    bodyKids = [jsx(hdayPanel07ErrState, {
      key: 'e', title: 'Log poll failed', description: transportErr
    })]
  } else if (backendErr) {
    bodyKids = [jsx('div', {
      key: 'e', className: 'hday-receipt', style: { color: C.red },
      children: backendErr
    })]
  } else if (emptyReason) {
    bodyKids = [jsxs('div', {
      key: 'e',
      className: 'hday-receipt',
      style: { color: C.faint },
      children: [
        jsx(Codicon, { key: 'i', name: 'terminal', style: { color: C.faint } }),
        jsx('span', { key: 't', children: ' ' + emptyReason })
      ]
    })]
  } else if (buf.length && !filtered.length) {
    bodyKids = [jsx('div', {
      key: 'z', className: 'hday-receipt',
      style: { color: C.amber },
      children: '0 of ' + String(buf.length) + ' buffered lines match /' +
        regexV + '/' + (comp.bad ? ' (invalid regex)' : '')
    })]
  } else {
    bodyKids = groups.map(function (g, gi) {
      var row = g.row
      var lvl = row.level || hdayPanel07Level(row.text)
      var col = hdayPanel07Color(lvl)
      return jsxs('div', {
        key: String(row.seq) + ':' + String(gi),
        className: cn('hday-row', 'hday-receipt'),
        style: {
          color: col,
          padding: '.14rem .4rem',
          borderLeft: '2px solid ' + (lvl === 'info' ? C.border : col)
        },
        children: [
          jsx('span', {
            key: 's',
            style: { color: C.faint, opacity: '.75', marginRight: '.55rem' },
            children: String(row.seq)
          }),
          jsx('span', {
            key: 't',
            style: { flex: '1 1 auto', minWidth: 0, whiteSpace: 'pre-wrap',
              wordBreak: 'break-word' },
            children: row.text
          }),
          row.partial
            ? jsx('span', {
                key: 'p', style: { color: C.faint, fontSize: '.58rem' },
                children: '· partial'
              })
            : null,
          g.count > 1
            ? jsx('span', {
                key: 'c', style: { color: C.mono, marginLeft: '.4rem' },
                children: '×' + String(g.count)
              })
            : null
        ]
      })
    })
  }

  return jsx('div', {
    className: cn('hday-panel', 'hday-log07', 'flex h-full min-h-0 flex-col gap-2'),
    style: { color: C.text },
    tabIndex: 0,
    onKeyDown: onKey,
    children: [
      // ---- head: badge + path -------------------------------------------
      jsxs('div', {
        key: 'head',
        className: 'hday-row flex items-center gap-2 px-1',
        children: [
          jsx(Codicon, { key: 'i', name: 'terminal', style: { color: C.mono } }),
          jsx(Badge, {
            key: 'b', variant: 'default',
            children: 'Live Log Terminal'
          }),
          jsx('input', {
            key: 'p',
            className: 'min-w-0 flex-1 rounded border px-2 py-1 outline-none',
            style: {
              borderColor: C.border,
              color: C.text,
              background: 'transparent',
              fontSize: '.72rem',
              fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace'
            },
            value: pathV,
            spellCheck: false,
            autoCorrect: 'off',
            autoCapitalize: 'off',
            placeholder: 'path to log — e.g. ~/.hermes/logs/agent.log',
            onChange: function (ev) { setPath(ev.target.value) },
            onKeyDown: function (ev) {
              if (ev.key === 'Enter') { ev.preventDefault(); refetch() }
            }
          }),
          paused
            ? jsx(Badge, {
                key: 'z', variant: 'warn', children: 'PAUSED'
              })
            : null
        ]
      }),

      // ---- toolbar: regex + match badge + legend + controls --------------
      jsxs('div', {
        key: 'tools',
        className: 'hday-row flex flex-wrap items-center gap-2 px-1',
        children: [
          jsx(SearchField, {
            key: 'f',
            placeholder: 'regex filter…',
            value: regexV,
            onChange: function (v) {
              store.regex.set(String(v == null ? '' : v))
            },
            containerClassName: 'w-44'
          }),
          jsx(Badge, {
            key: 'm',
            variant: comp.bad ? 'warn' : 'muted',
            children: comp.bad
              ? 'bad regex'
              : String(filtered.length) + '/' + String(buf.length)
          }),
          comp.bad
            ? jsx('span', {
                key: 'be', style: { color: C.red, fontSize: '.62rem' },
                children: comp.bad
              })
            : null,
          jsx('span', {
            key: 'le', style: { color: C.red, fontSize: '.6rem',
              letterSpacing: '.06em' }, children: 'ERROR'
          }),
          jsx('span', {
            key: 'lw', style: { color: C.amber, fontSize: '.6rem',
              letterSpacing: '.06em' }, children: 'WARN'
          }),
          jsx('span', {
            key: 'fo', style: { color: follow ? C.emerald : C.faint,
              fontSize: '.6rem' },
            children: follow ? 'following' : 'held'
          }),
          jsx('span', { key: 'sp', className: 'flex-1' }),
          jsx(Tip, {
            key: 'bp',
            label: paused ? 'Resume polling — catch up on missed bytes'
              : 'Pause polling — the buffer freezes, nothing is lost',
            children: jsx(Button, {
              size: 'xs', variant: 'secondary', onClick: togglePause,
              children: paused
                ? [jsx(Codicon, { key: 'i', name: 'play' }), ' Resume']
                : [jsx(Codicon, { key: 'i', name: 'debug-pause' }), ' Pause']
            })
          }),
          jsx(Tip, {
            key: 'bt',
            label: 'Scroll to the newest line and re-arm follow',
            children: jsx(Button, {
              size: 'xs', variant: 'ghost', onClick: jumpTail,
              children: [jsx(Codicon, { key: 'i', name: 'arrow-down' }),
                ' Jump to tail']
            })
          }),
          jsx(Tip, {
            key: 'br',
            label: 'Drop the buffer and re-read the file tail from the backend',
            children: jsx(Button, {
              size: 'xs', variant: 'ghost', onClick: tailFromEnd,
              children: [jsx(Codicon, { key: 'i', name: 'history' }),
                ' Re-tail']
            })
          }),
          jsx(Tip, {
            key: 'bf',
            label: 'Poll now (reads only new bytes since the cursor)',
            children: jsx(Button, {
              size: 'xs', variant: 'ghost', onClick: refetch,
              children: [jsx(Codicon, { key: 'i', name: 'refresh' }),
                ' Refresh']
            })
          })
        ]
      }),

      // ---- byte-cursor status strip --------------------------------------
      data
        ? jsxs('div', {
            key: 'st',
            className: 'hday-receipt flex flex-wrap items-center gap-x-3',
            style: { color: C.faint },
            children: [
              jsx('span', {
                key: 'o',
                children: 'offset ' + hdayPanel07Num(data.offset) + '/' +
                  hdayPanel07Num(data.size)
              }),
              data.pending
                ? jsx('span', {
                    key: 'pd', style: { color: C.mono },
                    children: 'more pending'
                  })
                : null,
              data.truncated
                ? jsx('span', {
                    key: 'tr',
                    children: 'tail window — earlier ' +
                      hdayPanel07Num(data.omittedBefore) + ' bytes omitted'
                  })
                : null,
              data.dropped
                ? jsx('span', {
                    key: 'dr', style: { color: C.amber },
                    children: hdayPanel07Num(data.dropped) +
                      ' lines dropped (page cap)'
                  })
                : null,
              data.rotated
                ? jsx('span', {
                    key: 'ro', style: { color: C.red },
                    children: 'file rotated — re-tailed'
                  })
                : null,
              data.at
                ? jsx('span', {
                    key: 'at', style: { marginLeft: 'auto' },
                    children: 'polled ' + relativeTime(data.at)
                  })
                : null
            ]
          })
        : null,

      // ---- terminal body --------------------------------------------------
      jsx('div', {
        key: 'body',
        className: 'hday-log07-body min-h-0 flex-1 overflow-y-auto',
        style: { color: C.muted },
        onScroll: onBodyScroll,
        children: bodyKids
      }),

      // ---- footer: honest controls ---------------------------------------
      jsxs('div', {
        key: 'foot',
        className: 'flex flex-wrap items-center gap-3 px-1',
        style: { fontSize: '.62rem', color: C.faint },
        children: [
          jsxs('span', {
            key: 'k1', style: { display: 'flex', alignItems: 'center',
              gap: '.25rem' },
            children: [jsx(Kbd, { key: 'a', children: 'p' }),
              paused ? 'resume' : 'pause']
          }),
          jsxs('span', {
            key: 'k2', style: { display: 'flex', alignItems: 'center',
              gap: '.25rem' },
            children: [jsx(Kbd, { key: 'b', children: 't' }), 'jump to tail']
          }),
          jsxs('span', {
            key: 'k3', style: { display: 'flex', alignItems: 'center',
              gap: '.25rem' },
            children: [jsx(Kbd, { key: 'c', children: 'r' }), 'refresh']
          }),
          jsx('span', {
            key: 'n', style: { marginLeft: 'auto' },
            children: 'keys apply when the panel is focused · each poll reads ' +
              'only new bytes (64 KiB page cap)'
          })
        ]
      })
    ]
  })
}

// ---- lane 08: instinct-studio ----
// Lane 08 — Instinct Studio: 3-column HUD for the negative-instincts system.
//   col 1 — repo rail (N/60 meters) + loadouts (save as / diff-first activate)
//   col 2 — ledger table: toggle, hits, age, inline reason editor, badges
//   col 3 — byte-exact prompt preview: SAVED vs STAGED, chars/4000, lines/16
// Splice-ready snippet: no import/export statements, no top-level statements —
// integrate.py drops it before `function DayPage()`, where jsx, jsxs, Badge,
// Button, Codicon, Kbd, Tip, Input, useState, useQuery, cn, relativeTime, rpc
// and the C token object are already in module scope.
//
// Backend: /day-instincts, /day-instinct-set, /day-instinct-del (existing),
// plus /day-instinct-preview, /day-instinct-edit, /day-instinct-status and
// /day-loadout-{list,save,apply,del} from patches/08-instinct-studio.py.
// Every color is a C token or var(--ui-*) — zero hardcoded hex/rgb/hsl.

function hdayPanel08Dispatch(route, name, arg) {
  return Promise.resolve()
    .then(function () { return rpc(route, 'command.dispatch', { name: name, arg: arg || '' }) })
    .then(function (disp) {
      var out = disp && (disp.output || disp.text || '')
      out = typeof out === 'string' ? out.trim() : out
      if (!out) return { ok: false, error: 'empty response from ' + name }
      try { return JSON.parse(out) } catch (e) { return { ok: false, error: String(out).slice(0, 300) } }
    })
    .catch(function (e) { return { ok: false, error: String((e && e.message) || e) } })
}

// Client mirror of the core's _brief — used ONLY to verify a save landed
// (refetch-is-truth, spec §3.4); preview bytes always come from the backend.
function hdayPanel08Brief(text) {
  return String(text == null ? '' : text).split(/\s+/).filter(Boolean).join(' ').slice(0, 240)
}

function hdayPanel08NameOk(name) {
  return /^[A-Za-z0-9._-]{1,40}$/.test(name)
}

function hdayPanel08Find(items, id) {
  var list = items || []
  for (var i = 0; i < list.length; i++) if (list[i] && list[i].id === id) return list[i]
  return null
}

// Diff of a loadout against the current ledger: n on, k off, m missing (§2.4).
function hdayPanel08Diff(refs, items) {
  var by = {}
  ;(items || []).forEach(function (it) { if (it && it.id) by[it.id] = it })
  var on = 0, off = 0, missing = []
  ;(refs || []).forEach(function (r) {
    if (!r || !by[r.id]) missing.push(r && r.id)
    else if (r.enabled === false) off++
    else on++
  })
  return { on: on, off: off, missing: missing }
}

function hdayPanel08Base(root) {
  var p = String(root || '').split('/')
  return p[p.length - 1] || root
}

function hdayPanel08Age(sec) {
  if (!sec) return '—'
  try { return relativeTime(Number(sec) * 1000) } catch (e) { return '—' }
}

function hdayPanel08Ref(q) {
  try { return q && q.refetch ? Promise.resolve(q.refetch()).catch(function () { return null }) : Promise.resolve(null) }
  catch (e) { return Promise.resolve(null) }
}

// ---------------------------------------------------------------------------
// Cache-safety banner — always visible (spec §3, copy verbatim in spirit)
// ---------------------------------------------------------------------------
function hdayPanel08Banner(props) {
  var frozen = props.frozen
  return jsxs('div', {
    className: 'rounded border px-2.5 py-2',
    style: { borderColor: C.border, background: C.surface },
    children: [
      jsxs('div', {
        className: 'flex items-start gap-1.5 text-[0.68rem] leading-4',
        style: { color: C.text },
        children: [
          jsx(Codicon, { name: 'lightbulb', className: 'mt-px shrink-0 text-[0.68rem]', style: { color: C.amber } }),
          jsxs('span', {
            children: [
              'The section renders once per session and is frozen: an EDIT takes effect in the ',
              jsx('span', { style: { color: C.amber }, children: 'NEXT session' }),
              ' — a NEWLY created instinct can still reach the running session, once, via the user message.'
            ]
          })
        ]
      }),
      jsx('div', {
        className: 'mt-1 text-[0.66rem]',
        style: { color: C.muted },
        children: 'Applies to NEW sessions only. ' + (frozen === null || frozen === undefined ? '…' : frozen) +
          ' running session(s) keep their frozen copy.'
      }),
      jsx('div', {
        className: 'mt-0.5 text-[0.62rem]',
        style: { color: C.faint },
        children: 'New instincts learned mid-session reach the running agent once via the user message; edits to existing ones never do.'
      })
    ]
  })
}

// ---------------------------------------------------------------------------
// Column 1a — repo rail
// ---------------------------------------------------------------------------
function hdayPanel08Repos(props) {
  var roots = props.roots || []
  var repos = props.repos || {}
  var selRoot = props.selRoot
  var onPick = props.onPick
  if (!roots.length) {
    return jsx('div', {
      className: 'py-3 text-[0.66rem]',
      style: { color: C.faint },
      children: props.isLoading
        ? 'Loading ledgers…'
        : 'No instincts yet — blocked traps and failed checks promote themselves here automatically.'
    })
  }
  return jsxs('div', {
    className: 'flex flex-col gap-1',
    children: roots.map(function (root) {
      var items = repos[root] || []
      var n = items.length
      var meter = n >= 55 ? C.amber : C.faint
      var on = root === selRoot
      return jsxs('button', {
        className: 'hday-row flex items-center gap-2 rounded border px-2 py-1.5 text-left',
        style: {
          borderColor: on ? C.mono : C.border,
          background: on ? C.surface : 'transparent'
        },
        onClick: function () { onPick(root) },
        children: [
          jsx(Codicon, { name: 'repo', className: 'shrink-0 text-[0.66rem]', style: { color: on ? C.mono : C.faint } }),
          jsx('span', {
            className: 'min-w-0 flex-1 truncate font-mono text-[0.64rem]',
            style: { color: on ? C.text : C.muted },
            children: hdayPanel08Base(root)
          }),
          jsx('span', {
            className: 'shrink-0 font-mono text-[0.6rem]',
            style: { color: meter },
            children: n + '/60'
          })
        ]
      }, root)
    })
  })
}

// ---------------------------------------------------------------------------
// Column 1b — loadouts rail (save / diff-first activate / delete)
// ---------------------------------------------------------------------------
function hdayPanel08Loadouts(props) {
  var entries = props.entries || []
  var name = props.name
  var onName = props.onName
  var onSave = props.onSave
  var pending = props.pending
  var diff = props.diff
  var onActivate = props.onActivate
  var onConfirm = props.onConfirm
  var onCancel = props.onCancel
  var onDelete = props.onDelete
  var busy = props.busy
  var failed = props.failed || {}
  var hasRoot = Boolean(props.root)
  var nameOk = hdayPanel08NameOk(name)
  return jsxs('div', {
    className: 'mt-2 flex flex-col gap-1.5',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-1.5',
        children: [
          jsx(Codicon, { name: 'archive', className: 'text-[0.66rem]', style: { color: C.faint } }),
          jsx('span', {
            className: 'text-[0.6rem] font-semibold uppercase tracking-wider',
            style: { color: C.faint },
            children: 'loadouts'
          })
        ]
      }),
      !entries.length
        ? jsx('div', {
            className: 'text-[0.62rem]',
            style: { color: C.faint },
            children: 'No loadouts — snapshot current flags with “Save as…”.'
          })
        : null,
      entries.map(function (e) {
        var key = (props.root || '') + '#' + e.name
        var fkey = 'loadout#' + key
        var showDiff = pending && pending.name === e.name
        return jsxs('div', {
          className: 'rounded border px-2 py-1.5',
          style: { borderColor: showDiff ? C.mono : C.border, background: C.surface },
          children: [
            jsxs('div', {
              className: 'flex items-center gap-1.5',
              children: [
                jsx('span', {
                  className: 'min-w-0 flex-1 truncate font-mono text-[0.66rem]',
                  style: { color: C.text },
                  children: e.name
                }),
                e.active ? jsx(Badge, { size: 'xs', variant: 'success', children: 'ACTIVE' }) : null,
                jsx('span', {
                  className: 'shrink-0 text-[0.58rem]',
                  style: { color: C.faint },
                  children: hdayPanel08Age(e.created)
                }),
                failed[fkey]
                  ? jsx('span', {
                      className: 'shrink-0 rounded border px-1 font-mono text-[0.56rem]',
                      style: { borderColor: C.red, color: C.red },
                      children: 'WRITE FAILED'
                    })
                  : null
              ]
            }),
            jsxs('div', {
              className: 'mt-1 flex items-center gap-1.5',
              children: [
                jsx(Button, {
                  size: 'xs',
                  variant: 'secondary',
                  disabled: Boolean(busy),
                  onClick: function () { onActivate(e) },
                  children: 'Activate'
                }),
                jsx('span', {
                  className: 'font-mono text-[0.58rem]',
                  style: { color: C.faint },
                  children: e.count + ' refs'
                }),
                jsx('span', { className: 'flex-1' }),
                jsx(Button, {
                  size: 'icon-xs',
                  variant: 'ghost',
                  disabled: Boolean(busy),
                  onClick: function () { onDelete(e.name) },
                  children: jsx(Codicon, { name: 'trash', className: 'text-[0.6rem]', style: { color: C.faint } })
                })
              ]
            }),
            showDiff
              ? jsxs('div', {
                  className: 'mt-1.5 rounded border px-1.5 py-1',
                  style: { borderColor: C.mono, background: C.canvas },
                  children: [
                    jsx('div', {
                      className: 'font-mono text-[0.6rem]',
                      style: { color: C.mono },
                      children: diff.on + ' on · ' + diff.off + ' off · ' + diff.missing.length + ' missing'
                    }),
                    diff.missing.length
                      ? jsx('div', {
                          className: 'mt-0.5 break-all font-mono text-[0.56rem]',
                          style: { color: C.amber },
                          children: 'missing (never resurrected): ' + diff.missing.slice(0, 6).join(', ') +
                            (diff.missing.length > 6 ? ' +' + (diff.missing.length - 6) + ' more' : '')
                        })
                      : null,
                    jsxs('div', {
                      className: 'mt-1 flex items-center gap-1.5',
                      children: [
                        jsx(Button, {
                          size: 'xs',
                          variant: 'primary',
                          disabled: Boolean(busy),
                          onClick: function () { onConfirm(e.name) },
                          children: busy === 'apply:' + e.name ? 'Applying…' : 'Apply'
                        }),
                        jsx(Button, { size: 'xs', variant: 'ghost', onClick: onCancel, children: 'Cancel' })
                      ]
                    })
                  ]
                })
              : null
          ]
        }, e.name)
      }),
      jsxs('div', {
        className: 'flex items-center gap-1.5',
        children: [
          jsx(Input, {
            value: name,
            placeholder: 'save as…',
            disabled: !hasRoot || Boolean(busy),
            className: 'h-6 min-w-0 flex-1 font-mono text-[0.64rem]',
            onChange: function (ev) { onName(ev.target.value) }
          }),
          jsx(Button, {
            size: 'xs',
            variant: 'secondary',
            disabled: !hasRoot || !nameOk || Boolean(busy),
            onClick: onSave,
            children: jsx(Codicon, { name: 'save', className: 'text-[0.6rem]' })
          })
        ]
      }),
      name && !nameOk && hasRoot
        ? jsx('div', {
            className: 'text-[0.58rem]',
            style: { color: C.amber },
            children: 'names: letters, digits, . _ - (1–40 chars), no spaces'
          })
        : null
    ]
  })
}

// ---------------------------------------------------------------------------
// Column 2 — ledger table with inline reason editor
// ---------------------------------------------------------------------------
function hdayPanel08Editor(props) {
  var editing = props.editing
  var onDraft = props.onDraft
  var onSave = props.onSave
  var onCancel = props.onCancel
  var onStage = props.onStage
  var busy = props.busy
  var staged = props.staged
  var lastError = props.lastError
  var len = hdayPanel08Brief(editing.draft).length
  var stagedStale = Boolean(staged && staged.value !== hdayPanel08Brief(editing.draft))
  return jsxs('div', {
    className: 'mt-1 rounded border px-1.5 py-1.5',
    style: { borderColor: C.mono, background: C.canvas },
    children: [
      jsxs('div', {
        className: 'flex items-center gap-1.5',
        children: [
          jsx('span', {
            className: 'text-[0.58rem] font-semibold uppercase tracking-wider',
            style: { color: C.faint },
            children: 'reason'
          }),
          jsx('span', {
            className: 'font-mono text-[0.58rem]',
            style: { color: len >= 240 ? C.red : len > 200 ? C.amber : C.faint },
            children: len + '/240'
          }),
          jsx('span', {
            className: 'text-[0.56rem]',
            style: { color: C.faint },
            children: 'one line · next session only'
          })
        ]
      }),
      jsx(Input, {
        value: editing.draft,
        disabled: busy === 'edit:' + editing.root + '#' + editing.id,
        maxLength: 240,
        className: 'mt-1 h-6 w-full font-mono text-[0.64rem]',
        onChange: function (ev) { onDraft(ev.target.value) }
      }),
      jsxs('div', {
        className: 'mt-1.5 flex items-center gap-1.5',
        children: [
          jsx(Button, {
            size: 'xs',
            variant: 'primary',
            disabled: busy === 'edit:' + editing.root + '#' + editing.id || !hdayPanel08Brief(editing.draft),
            onClick: onSave,
            children: busy === 'edit:' + editing.root + '#' + editing.id ? 'Saving…' : 'Save'
          }),
          jsx(Button, {
            size: 'xs',
            variant: 'secondary',
            disabled: !hdayPanel08Brief(editing.draft),
            onClick: onStage,
            children: 'Preview staged'
          }),
          jsx(Button, { size: 'xs', variant: 'ghost', onClick: onCancel, children: 'Cancel' })
        ]
      }),
      staged
        ? jsxs('div', {
            className: 'mt-1 text-[0.58rem]',
            style: { color: stagedStale ? C.amber : C.faint },
            children: [
              stagedStale
                ? 'Draft changed since this staged preview — run “Preview staged” again.'
                : 'Staged preview showing exact post-save bytes — not yet saved.'
            ]
          })
        : null,
      lastError
        ? jsx('div', {
            className: 'mt-1 break-all text-[0.58rem]',
            style: { color: C.red },
            children: lastError
          })
        : null
    ]
  })
}

function hdayPanel08Ledger(props) {
  var items = props.items || []
  var root = props.root
  var shadowed = props.shadowed || []
  var failed = props.failed || {}
  var editing = props.editing
  var busy = props.busy
  if (!root) {
    return jsx('div', {
      className: 'py-4 text-center text-[0.66rem]',
      style: { color: C.faint },
      children: 'Select a repo on the left.'
    })
  }
  if (!items.length) {
    return jsx('div', {
      className: 'py-4 text-center text-[0.66rem]',
      style: { color: C.faint },
      children: 'No instincts in this ledger — blocked traps and failed checks promote themselves here automatically.'
    })
  }
  return jsxs('div', {
    className: 'flex flex-col gap-1.5',
    children: items.map(function (item) {
      var off = item.enabled === false
      var isShadow = shadowed.indexOf(item.id) >= 0
      var fkey = root + '#' + item.id
      var rowBusy = busy === fkey
      var isEditing = editing && editing.root === root && editing.id === item.id
      return jsxs('div', {
        className: 'hday-row rounded border px-2 py-1.5',
        style: { borderColor: C.border, background: C.surface, opacity: off ? 0.5 : 1 },
        children: [
          jsxs('div', {
            className: 'flex items-start gap-2',
            children: [
              jsx('button', {
                className: 'mt-px shrink-0 rounded border',
                title: off ? 'Enable' : 'Disable',
                style: {
                  width: 12,
                  height: 12,
                  borderColor: off ? C.border : C.amber,
                  background: off ? 'transparent' : C.amber
                },
                disabled: Boolean(rowBusy),
                onClick: function () { props.onToggle(item, !off) }
              }),
              jsxs('div', {
                className: 'min-w-0 flex-1',
                children: [
                  jsxs('div', {
                    className: 'flex flex-wrap items-center gap-1.5',
                    children: [
                      jsx('span', {
                        className: 'break-all font-mono text-[0.66rem]',
                        style: { color: C.text },
                        children: item.trigger_pattern
                      }),
                      jsx(Tip, {
                        label: 'locked (dedupe key) — trigger_pattern is not editable in v1',
                        children: jsx(Codicon, { name: 'lock', className: 'text-[0.6rem]', style: { color: C.faint } })
                      }),
                      item.hits > 1
                        ? jsx('span', {
                            className: 'shrink-0 rounded border px-1 font-mono text-[0.58rem]',
                            style: { borderColor: C.border, color: C.amber },
                            children: 'x' + item.hits
                          })
                        : null,
                      jsx('span', {
                        className: 'shrink-0 font-mono text-[0.58rem]',
                        style: { color: C.faint },
                        children: hdayPanel08Age(item.created)
                      })
                    ]
                  }),
                  isEditing
                    ? jsx(hdayPanel08Editor, {
                        editing: editing,
                        staged: props.staged,
                        lastError: props.lastError,
                        busy: busy,
                        onDraft: props.onDraft,
                        onSave: props.onSaveReason,
                        onCancel: props.onCancelEdit,
                        onStage: props.onStage
                      })
                    : jsx('button', {
                        className: 'mt-0.5 block w-full break-all text-left font-mono text-[0.6rem]',
                        style: { color: C.muted },
                        title: 'Edit reason',
                        onClick: function () { props.onEdit(item) },
                        children: item.reason || 'previously failed'
                      }),
                  off || isShadow || failed[fkey]
                    ? jsxs('div', {
                        className: 'mt-1 flex flex-wrap items-center gap-1.5',
                        children: [
                          off ? jsx(Badge, { size: 'xs', variant: 'muted', children: 'OFF' }) : null,
                          isShadow
                            ? jsx(Tip, {
                                label: 'In the ledger, not in the prompt — beyond items[:8] or past lines[:16] (repo headers count).',
                                children: jsx(Badge, { size: 'xs', variant: 'warn', children: 'SHADOWED' })
                              })
                            : null,
                          failed[fkey]
                            ? jsx('span', {
                                className: 'rounded border px-1 font-mono text-[0.56rem]',
                                style: { borderColor: C.red, color: C.red },
                                children: 'WRITE FAILED'
                              })
                            : null
                        ]
                      })
                    : null
                ]
              }),
              jsx(Button, {
                size: 'icon-xs',
                variant: 'ghost',
                className: 'shrink-0',
                disabled: Boolean(rowBusy),
                onClick: function () { props.onDelete(item) },
                children: jsx(Codicon, { name: 'trash', className: 'text-[0.6rem]', style: { color: C.faint } })
              })
            ]
          })
        ]
      }, item.id)
    })
  })
}

// ---------------------------------------------------------------------------
// Column 3 — byte-exact preview pane (the receipt)
// ---------------------------------------------------------------------------
function hdayPanel08MeterRow(props) {
  return jsxs('div', {
    className: 'hday-receipt-row',
    children: [
      jsx('span', { children: props.label }),
      jsx('span', { style: { color: props.color }, children: props.value })
    ]
  })
}

function hdayPanel08Preview(props) {
  var view = props.view
  var data = view && view.data
  var stagedMode = Boolean(view && view.mode === 'STAGED')
  var block = (data && data.block) || ''
  var chars = (data && data.chars) || 0
  var lines = (data && data.lines) || 0
  var over = Boolean(data && data.over)
  var markers = Boolean(data && data.markers)
  var stagedStale = Boolean(view && view.stale)
  var loading = props.loading
  var error = props.error
  var enabledInScope = props.enabledInScope
  var charsColor = over ? C.red : chars > 3200 ? C.amber : C.emerald
  var linesColor = lines >= 16 ? C.amber : C.emerald
  return jsxs('div', {
    className: 'flex flex-col gap-1.5',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-1.5',
        children: [
          jsx(Codicon, { name: 'eye', className: 'text-[0.68rem]', style: { color: stagedMode ? C.amber : C.emerald } }),
          jsx('span', {
            className: 'flex-1 text-[0.6rem] font-semibold uppercase tracking-wider',
            style: { color: C.faint },
            children: 'preview'
          }),
          stagedMode
            ? jsx(Badge, { size: 'xs', variant: 'warn', children: 'STAGED — not yet saved' })
            : jsx(Badge, { size: 'xs', variant: 'success', children: 'SAVED' })
        ]
      }),
      jsx('div', {
        className: 'text-[0.58rem]',
        style: { color: C.faint },
        children: 'EXACT bytes injected into new sessions — rendered by _instincts_block'
      }),
      jsx('div', {
        className: 'hday-receipt',
        style: { color: stagedMode ? C.amber : C.text, background: C.canvas },
        children: jsxs('div', {
          children: [
            jsx('pre', {
              style: {
                margin: 0,
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-all',
                fontFamily: 'inherit',
                fontSize: 'inherit',
                lineHeight: 'inherit'
              },
              children: loading ? 'rendering…' : (block || '(empty — nothing would be injected)')
            }),
            jsx('div', { className: 'hday-receipt-sep' }),
            jsx(hdayPanel08MeterRow, {
              label: 'chars',
              value: chars + ' / 4000 chars',
              color: charsColor
            }),
            jsx(hdayPanel08MeterRow, {
              label: 'lines',
              value: lines + ' / 16 lines',
              color: linesColor
            }),
            jsx(hdayPanel08MeterRow, {
              label: 'shadowed',
              value: String(((data && data.shadowed) || []).length) + ' id(s)',
              color: ((data && data.shadowed) || []).length ? C.amber : C.emerald
            })
          ]
        })
      }),
      over
        ? jsx('div', {
            className: 'text-[0.6rem]',
            style: { color: C.red },
            children: '> 4000 chars — Hermes will SKIP this section entirely (plugins_dispatch L509). Disable entries until it fits.'
          })
        : null,
      markers
        ? jsx('div', {
            className: 'text-[0.6rem]',
            style: { color: C.red },
            children: 'SECTION MARKERS PRESENT — dispatch skips it (reserved persistence marker).'
          })
        : null,
      !over && !markers && chars === 0 && !loading
        ? jsx('div', {
            className: 'text-[0.6rem]',
            style: { color: enabledInScope ? C.amber : C.faint },
            children: enabledInScope
              ? 'SECTION DISABLED (gate.instincts) — nothing is injected while the gate is off.'
              : 'No enabled instincts — nothing to inject.'
          })
        : null,
      stagedMode && stagedStale
        ? jsx('div', {
            className: 'text-[0.6rem]',
            style: { color: C.amber },
            children: 'Draft changed since this preview — the bytes below are from the last “Preview staged” run.'
          })
        : null,
      error
        ? jsx('div', {
            className: 'break-all text-[0.6rem]',
            style: { color: C.red },
            children: error
          })
        : null,
      jsxs('div', {
        className: 'mt-1 flex flex-wrap items-center gap-1.5',
        children: [
          jsx(Button, {
            size: 'xs',
            variant: 'ghost',
            onClick: props.onRefresh,
            children: jsx(Codicon, { name: 'sync', className: 'text-[0.6rem]' })
          }),
          jsx(Kbd, { children: '/day-instinct-preview' }),
          jsx(Kbd, { children: '/day-instinct-edit' }),
          jsx(Kbd, { children: '/day-instinct-status' })
        ]
      }),
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-1.5',
        children: [
          jsx(Kbd, { children: '/day-loadout-list' }),
          jsx(Kbd, { children: '/day-loadout-save' }),
          jsx(Kbd, { children: '/day-loadout-apply' }),
          jsx(Kbd, { children: '/day-loadout-del' })
        ]
      })
    ]
  })
}

// ---------------------------------------------------------------------------
// Panel shell — state, queries, mutations, 3-column layout
// ---------------------------------------------------------------------------
function hdayPanel08(props) {
  var route = (props && props.route) || null
  var sel = useState(null)                 // selected repo root (null = first)
  var selRoot = sel[0], setSelRoot = sel[1]
  var ed = useState(null)                  // inline editor {root, id, draft, saved}
  var editing = ed[0], setEditing = ed[1]
  var st = useState(null)                  // staged preview {root, id, value, arg}
  var staged = st[0], setStaged = st[1]
  var nm = useState('')                    // loadout name being typed
  var loadoutName = nm[0], setLoadoutName = nm[1]
  var pa = useState(null)                  // diff-first confirm {name, diff}
  var pending = pa[0], setPending = pa[1]
  var ff = useState({})                    // refetch-proven write failures
  var failed = ff[0], setFailed = ff[1]
  var bs = useState(null)                  // in-flight mutation key
  var busy = bs[0], setBusy = bs[1]
  var er = useState('')                    // last dispatch error (editor area)
  var lastError = er[0], setLastError = er[1]

  var ledgersQ = useQuery({
    queryKey: ['hday-instincts'],
    queryFn: function () { return hdayPanel08Dispatch(route, 'day-instincts', '') },
    refetchInterval: 10000,
    staleTime: 4000,
    retry: false
  })
  var statusQ = useQuery({
    queryKey: ['hday-instinct-studio', 'status'],
    queryFn: function () { return hdayPanel08Dispatch(route, 'day-instinct-status', '') },
    refetchInterval: 15000,
    staleTime: 5000,
    retry: false
  })

  var repos = (ledgersQ.data && ledgersQ.data.repos) || {}
  var roots = Object.keys(repos).sort()
  var root = (selRoot && roots.indexOf(selRoot) >= 0) ? selRoot : (roots[0] || '')
  var items = (root && repos[root]) || []
  var enabledCount = items.filter(function (i) { return i && i.enabled !== false }).length
  var enabledAll = roots.reduce(function (n, r) {
    return n + (repos[r] || []).filter(function (i) { return i && i.enabled !== false }).length
  }, 0)

  var loadoutsQ = useQuery({
    queryKey: ['hday-instinct-studio', 'loadouts', root],
    queryFn: function () { return hdayPanel08Dispatch(route, 'day-loadout-list', root) },
    enabled: Boolean(root),
    staleTime: 3000,
    retry: false
  })
  var loadoutEntries = (loadoutsQ.data && loadoutsQ.data.loadouts)
    ? Object.keys(loadoutsQ.data.loadouts).sort().map(function (name) {
        var e = loadoutsQ.data.loadouts[name] || {}
        return {
          name: name,
          created: e.created,
          count: e.count,
          active: Boolean(e.active),
          instincts: e.instincts || []
        }
      })
    : []

  // SAVED preview: scoped to the selected repo (what a session there gets).
  var liveQ = useQuery({
    queryKey: ['hday-instinct-studio', 'preview', root],
    queryFn: function () { return hdayPanel08Dispatch(route, 'day-instinct-preview', root) },
    staleTime: 3000,
    retry: false
  })
  // STAGED preview: same backend renderer, as-if-saved arg (spec §2.2.1).
  var stagedArg = staged ? staged.arg : ''
  var stagedQ = useQuery({
    queryKey: ['hday-instinct-studio', 'preview-staged', stagedArg],
    queryFn: function () { return hdayPanel08Dispatch(route, 'day-instinct-preview', stagedArg) },
    enabled: Boolean(stagedArg),
    staleTime: 3000,
    retry: false
  })

  var frozen = null
  if (statusQ.data && statusQ.data.sessions) {
    frozen = statusQ.data.sessions.filter(function (s) { return Number(s.inst_epoch) > 0 }).length
  }

  var shadowed = []
  var view = null
  var viewLoading = liveQ.isLoading
  var viewError = liveQ.error ? 'preview query failed' : null
  if (stagedArg && stagedQ.data && stagedQ.data.ok !== false) {
    view = {
      mode: 'STAGED',
      data: stagedQ.data,
      stale: Boolean(editing && staged && staged.value !== hdayPanel08Brief(editing.draft))
    }
    viewLoading = stagedQ.isLoading
    viewError = stagedQ.data && stagedQ.data.ok === false ? stagedQ.data.error : null
  } else if (liveQ.data && liveQ.data.ok !== false) {
    view = { mode: 'SAVED', data: liveQ.data, stale: false }
    viewError = liveQ.data && liveQ.data.ok === false ? liveQ.data.error : null
  }
  if (view && view.data) shadowed = view.data.shadowed || []

  function setFailedKey(key, bad) {
    setFailed(function (m) {
      var n = {}
      for (var k in m) n[k] = m[k]
      if (bad) n[key] = true
      else delete n[key]
      return n
    })
  }

  // Dispatch -> refetch (refetch is truth, §3.4) -> verify -> WRITE FAILED.
  function runMutation(key, name, arg, verify) {
    setBusy(key)
    setLastError('')
    return hdayPanel08Dispatch(route, name, arg)
      .then(function (res) {
        return Promise.all([
          hdayPanel08Ref(ledgersQ),
          hdayPanel08Ref(liveQ),
          hdayPanel08Ref(loadoutsQ),
          hdayPanel08Ref(statusQ)
        ]).then(function (results) { return { res: res, results: results } })
      })
      .then(function (o) {
        var bad = !o.res || o.res.ok === false
        if (!bad && verify) {
          try { bad = !verify(o) } catch (e) { bad = true }
        }
        setFailedKey(key, bad)
        if (o.res && o.res.ok === false && o.res.error) setLastError(o.res.error)
        setBusy(null)
        return { res: o.res, bad: bad }
      })
      .catch(function (e) {
        setFailedKey(key, true)
        setLastError(String((e && e.message) || e))
        setBusy(null)
        return { res: { ok: false }, bad: true }
      })
  }

  function refetchedRepos(o) {
    var d = o.results[0] && o.results[0].data
    return (d && d.repos) || {}
  }

  function onToggle(item, next) {
    var key = root + '#' + item.id
    runMutation(key, 'day-instinct-set', root + ' ' + item.id + ' ' + (next ? '1' : '0'),
      function (o) {
        var it = hdayPanel08Find(refetchedRepos(o)[root] || [], item.id)
        return Boolean(it && (it.enabled !== false) === next)
      })
  }

  function onDelete(item) {
    var key = root + '#' + item.id
    runMutation(key, 'day-instinct-del', root + ' ' + item.id, function (o) {
      return hdayPanel08Find(refetchedRepos(o)[root] || [], item.id) === null
    })
  }

  function onEdit(item) {
    setEditing({ root: root, id: item.id, draft: item.reason || '', saved: item.reason || '' })
    setStaged(null)
    setLastError('')
  }

  function onDraft(v) {
    setEditing(function (e) { return e ? { root: e.root, id: e.id, draft: v, saved: e.saved } : e })
  }

  function onCancelEdit() {
    setEditing(null)
    setStaged(null)
    setLastError('')
  }

  function onStage() {
    if (!editing) return
    var v = hdayPanel08Brief(editing.draft)
    if (!v) return
    setStaged({ root: editing.root, id: editing.id, value: v,
                arg: editing.root + ' ' + editing.id + ' reason ' + editing.draft })
  }

  function onSaveReason() {
    if (!editing) return
    var r = editing.root, id = editing.id, draft = editing.draft
    var key = r + '#' + id
    setStaged(null)
    runMutation(key, 'day-instinct-edit', r + ' ' + id + ' reason ' + draft, function (o) {
      var it = hdayPanel08Find(refetchedRepos(o)[r] || [], id)
      return Boolean(it && it.reason === hdayPanel08Brief(draft))
    }).then(function (o) {
      if (o && o.res && o.res.ok !== false && !o.bad) setEditing(null)
    })
  }

  function onSaveLoadout() {
    var name = loadoutName.trim()
    if (!hdayPanel08NameOk(name)) return
    var key = 'loadout#' + root + '#' + name
    runMutation(key, 'day-loadout-save', root + ' ' + name, function (o) {
      var d = o.results[2] && o.results[2].data
      return Boolean(d && d.loadouts && d.loadouts[name])
    }).then(function (o) {
      if (o && o.res && o.res.ok !== false && !o.bad) setLoadoutName('')
    })
  }

  function onActivate(entry) {
    var refs = entry.instincts || []
    setPending({ name: entry.name, diff: hdayPanel08Diff(refs, items) })
  }

  function onConfirmApply(name) {
    var entry = null
    for (var i = 0; i < loadoutEntries.length; i++) {
      if (loadoutEntries[i].name === name) entry = loadoutEntries[i]
    }
    var refs = entry ? entry.instincts : []
    var key = 'loadout#' + root + '#' + name
    runMutation(key, 'day-loadout-apply', root + ' ' + name, function (o) {
      var led = refetchedRepos(o)[root] || []
      var by = {}
      led.forEach(function (it) { if (it && it.id) by[it.id] = it })
      for (var j = 0; j < refs.length; j++) {
        var it = by[refs[j].id]
        if (it && (it.enabled !== false) !== refs[j].enabled) return false
      }
      return true
    }).then(function () { setPending(null) })
  }

  function onDeleteLoadout(name) {
    if (pending && pending.name === name) setPending(null)
    var key = 'loadout#' + root + '#' + name
    runMutation(key, 'day-loadout-del', root + ' ' + name, function (o) {
      var d = o.results[2] && o.results[2].data
      return Boolean(d && d.loadouts && !d.loadouts[name])
    })
  }

  function onRefresh() {
    hdayPanel08Ref(ledgersQ)
    hdayPanel08Ref(liveQ)
    hdayPanel08Ref(loadoutsQ)
    hdayPanel08Ref(statusQ)
    if (stagedArg) hdayPanel08Ref(stagedQ)
  }

  return jsx('div', {
    className: 'flex flex-col gap-2',
    children: [
      jsx(hdayPanel08Banner, { frozen: frozen }),
      jsxs('div', {
        style: {
          display: 'grid',
          gridTemplateColumns: 'minmax(150px, 0.9fr) minmax(0, 1.7fr) minmax(0, 1.2fr)',
          gap: '0.6rem',
          alignItems: 'start'
        },
        children: [
          // col 1 — repos + loadouts
          jsxs('div', {
            className: 'flex flex-col gap-1',
            children: [
              jsxs('div', {
                className: 'flex items-center gap-1.5',
                children: [
                  jsx('span', {
                    className: 'text-[0.6rem] font-semibold uppercase tracking-wider',
                    style: { color: C.faint },
                    children: 'repos'
                  }),
                  jsx('span', {
                    className: 'font-mono text-[0.58rem]',
                    style: { color: C.faint },
                    children: roots.length ? enabledAll + ' on' : ''
                  })
                ]
              }),
              jsx(hdayPanel08Repos, {
                roots: roots,
                repos: repos,
                selRoot: root,
                isLoading: ledgersQ.isLoading,
                onPick: function (r) { setSelRoot(r); setPending(null); setStaged(null) }
              }),
              jsx(hdayPanel08Loadouts, {
                root: root,
                entries: loadoutEntries,
                name: loadoutName,
                onName: setLoadoutName,
                onSave: onSaveLoadout,
                pending: pending,
                diff: pending ? pending.diff : null,
                onActivate: onActivate,
                onConfirm: onConfirmApply,
                onCancel: function () { setPending(null) },
                onDelete: onDeleteLoadout,
                busy: busy,
                failed: failed
              })
            ]
          }),
          // col 2 — ledger
          jsxs('div', {
            className: 'flex flex-col gap-1.5',
            children: [
              jsxs('div', {
                className: 'flex items-center gap-1.5',
                children: [
                  jsx('span', {
                    className: 'min-w-0 flex-1 truncate font-mono text-[0.6rem]',
                    style: { color: C.muted },
                    children: root || '—'
                  }),
                  jsx(Badge, {
                    size: 'xs',
                    variant: items.length >= 55 ? 'warn' : 'outline',
                    children: items.length + '/60'
                  }),
                  lastError && !editing
                    ? jsx('span', {
                        className: 'max-w-[14rem] truncate text-[0.58rem]',
                        style: { color: C.red },
                        children: lastError
                      })
                    : null
                ]
              }),
              jsx(hdayPanel08Ledger, {
                root: root,
                items: items,
                shadowed: shadowed,
                failed: failed,
                editing: editing,
                staged: staged,
                lastError: lastError,
                busy: busy,
                onEdit: onEdit,
                onDraft: onDraft,
                onSaveReason: onSaveReason,
                onCancelEdit: onCancelEdit,
                onStage: onStage,
                onToggle: onToggle,
                onDelete: onDelete
              })
            ]
          }),
          // col 3 — preview + status
          jsxs('div', {
            className: 'flex flex-col gap-1.5',
            children: [
              jsx(hdayPanel08Preview, {
                view: view,
                loading: viewLoading,
                error: viewError,
                enabledInScope: root ? enabledCount : enabledAll,
                onRefresh: onRefresh
              }),
              jsx('div', {
                className: 'rounded border px-2 py-1.5',
                style: { borderColor: C.border, background: C.surface },
                children: jsxs('div', {
                  className: 'hday-receipt',
                  style: { color: C.faint, border: 'none', padding: 0 },
                  children: [
                    jsxs('div', {
                      className: 'hday-receipt-row',
                      children: [
                        jsx('span', { children: 'ambient block' }),
                        jsx('span', {
                          style: { color: statusQ.data && statusQ.data.block_chars > 3200 ? C.amber : C.muted },
                          children: (statusQ.data ? statusQ.data.block_chars : '—') + ' / 4000 chars'
                        })
                      ]
                    }),
                    jsxs('div', {
                      className: 'hday-receipt-row',
                      children: [
                        jsx('span', { children: 'frozen sessions' }),
                        jsx('span', { style: { color: C.muted }, children: frozen === null ? '…' : String(frozen) })
                      ]
                    }),
                    jsxs('div', {
                      className: 'hday-receipt-row',
                      children: [
                        jsx('span', { children: 'gate.instincts' }),
                        jsx('span', {
                          style: { color: view && view.data && view.data.chars > 0 ? C.emerald : C.amber },
                          children: view && view.data && view.data.chars > 0 ? 'on' : 'off / empty'
                        })
                      ]
                    })
                  ]
                })
              })
            ]
          })
        ]
      }),
      jsx('div', {
        className: 'text-[0.6rem] font-semibold uppercase tracking-wider',
        style: { color: C.faint },
        children: 'FROZEN FOR RUNNING SESSIONS — applies to NEW sessions only (' +
          (frozen === null ? '?' : frozen) + ' running keep old bytes)'
      })
    ]
  })
}

// ---- lane 09: escalation-matrix ----
// Lane 09 — Escalation Matrix: routing-table panel (splice-ready snippet).
// NOT a module: no import/export, top-level function declarations only.
// Module-scope symbols assumed in scope: jsx, jsxs, Badge, Button, Codicon,
// Kbd, Tip, atom, useValue, useQuery, cn, relativeTime, host, rpc, C.
// Styling: C tokens + theme vars only — no hardcoded hex/rgb/hsl.

function hday09Kinds() {
  return [
    'secret_exposure',
    'test_tamper',
    'approval_stall',
    'subagent_failed',
    'provider_error',
    'interrupted'
  ];
}

function hday09Icons() {
  return {
    secret_exposure: 'key',
    test_tamper: 'beaker',
    approval_stall: 'watch',
    subagent_failed: 'error',
    provider_error: 'cloud-offline',
    interrupted: 'debug-stop'
  };
}

// Mirrors _MATRIX_DEFAULT in patches/09-escalation-matrix.py (spec §1 table).
function hday09DefaultMatrix() {
  return {
    secret_exposure: { sev: 'P0', delivery: 'notify', coalesce_s: 60, quiet: 'never', escalate_after: null,
      ladder: [[0, 'notify'], [2, 'renotify'], [5, 'pin'], [10, 'notify-critical']] },
    test_tamper: { sev: 'P0', delivery: 'notify', coalesce_s: 60, quiet: 'never', escalate_after: null,
      ladder: [[0, 'notify'], [3, 'renotify'], [10, 'pin']] },
    approval_stall: { sev: 'P1', delivery: 'badge', coalesce_s: 300, quiet: 'badge', escalate_after: 2,
      ladder: [[0, 'badge'], [2, 'notify'], [10, 'renotify'], [30, 'notify']] },
    subagent_failed: { sev: 'P1', delivery: 'badge', coalesce_s: 300, quiet: 'badge', escalate_after: 5,
      ladder: [[0, 'badge'], [5, 'notify'], [15, 'renotify']] },
    provider_error: { sev: 'P2', delivery: 'badge', coalesce_s: 600, quiet: 'silent', escalate_after: null,
      ladder: [[0, 'badge'], [10, 'notify'], [30, 'badge']] },
    interrupted: { sev: 'P3', delivery: 'silent', coalesce_s: 600, quiet: 'silent', escalate_after: null,
      ladder: [] }
  };
}

function hday09DeliveryCycle() { return ['silent', 'badge', 'notify']; }
function hday09CoalesceCycle() { return [30, 60, 300, 600, 0]; } // 0 = never
function hday09QuietCycle() { return ['never', 'badge', 'silent']; }

function hday09LadderPresets() {
  return [
    { id: 'none', ladder: [] },
    { id: '0m/2m/5m/10m', ladder: [[0, 'notify'], [2, 'renotify'], [5, 'pin'], [10, 'notify-critical']] },
    { id: '0m/2m/10m/30m', ladder: [[0, 'badge'], [2, 'notify'], [10, 'renotify'], [30, 'notify']] },
    { id: '0m/5m/15m', ladder: [[0, 'badge'], [5, 'notify'], [15, 'renotify']] },
    { id: '0m/10m/30m', ladder: [[0, 'badge'], [10, 'notify'], [30, 'badge']] }
  ];
}

function hday09CoalesceText(seconds) {
  try {
    var n = Number(seconds) || 0;
    if (n <= 0) return 'never';
    if (n < 60) return n + 's';
    return (n / 60) + 'm';
  } catch (e) {
    return '?';
  }
}

function hday09LadderId(ladder) {
  try {
    return (ladder || [])
      .map(function (step) { return String(step[0]) + '->' + String(step[1]); })
      .join(',');
  } catch (e) {
    return '';
  }
}

function hday09LadderText(ladder) {
  try {
    if (!ladder || !ladder.length) return 'none';
    return ladder
      .map(function (step) { return String(step[0]) + 'm'; })
      .join('/');
  } catch (e) {
    return 'none';
  }
}

function hday09LadderDetail(ladder) {
  try {
    if (!ladder || !ladder.length) return 'no escalation (informational)';
    return ladder
      .map(function (step) { return step[0] + 'm ' + step[1]; })
      .join('  →  ');
  } catch (e) {
    return '';
  }
}

function hday09SevColor(sev) {
  try {
    if (sev === 'P0') return C.red;
    if (sev === 'P1') return C.amber;
    if (sev === 'P2') return C.mono;
    return C.slate;
  } catch (e) {
    return C && C.muted ? C.muted : 'inherit';
  }
}

function hday09DeliveryColor(delivery) {
  try {
    if (delivery === 'notify') return C.emerald;
    if (delivery === 'badge') return C.amber;
    return C.faint;
  } catch (e) {
    return 'inherit';
  }
}

function hday09QuietColor(quiet) {
  try {
    if (quiet === 'never') return C.emerald;
    if (quiet === 'badge') return C.amber;
    return C.slate;
  } catch (e) {
    return 'inherit';
  }
}

function hday09Next(list, current) {
  try {
    var index = -1;
    for (var i = 0; i < list.length; i++) {
      if (list[i] === current) { index = i; break; }
    }
    return list[(index + 1) % list.length];
  } catch (e) {
    return current;
  }
}

function hday09NextLadder(currentLadder) {
  try {
    var presets = hday09LadderPresets();
    var currentId = hday09LadderId(currentLadder);
    for (var i = 0; i < presets.length; i++) {
      if (hday09LadderId(presets[i].ladder) === currentId) {
        return presets[(i + 1) % presets.length];
      }
    }
    return presets[0];
  } catch (e) {
    return hday09LadderPresets()[0];
  }
}

function hday09LadderArg(ladder) {
  try {
    return (ladder || [])
      .map(function (step) { return step[0] + '->' + step[1]; })
      .join(',');
  } catch (e) {
    return '';
  }
}

function hday09GridStyle() {
  return {
    display: 'grid',
    gridTemplateColumns: '1.45fr 0.4fr 0.9fr 0.75fr 0.75fr 1.15fr 0.55fr',
    gap: '8px',
    alignItems: 'center'
  };
}

function hday09GridCellStyle(color) {
  return { color: color || C.text, cursor: 'pointer', userSelect: 'none' };
}

// ------------------------------------------------------------- data I/O

function hday09Dispatch(route, name, arg, refetch) {
  var done = function () {
    try { if (refetch) refetch(); } catch (e) { /* never throw */ }
  };
  try {
    var pending = rpc(route, 'command.dispatch', { name: name, arg: arg || '' });
    if (pending && typeof pending.then === 'function') {
      pending.then(done, function () { done(); });
    } else {
      done();
    }
  } catch (e) {
    done();
  }
}

function hday09SetCell(route, refetch, kind, field, value) {
  hday09Dispatch(route, 'day-matrix-set', kind + ' ' + field + ' ' + value, refetch);
}

function hday09Ack(route, refetch, key) {
  hday09Dispatch(route, 'day-attn-ack', key || '*', refetch);
}

function hday09Reset(route, refetch) {
  hday09Dispatch(route, 'day-matrix-reset', '', refetch);
}

function hday09Data(query) {
  try {
    var raw = (query && query.data) || {};
    var kinds = hday09Kinds();
    var defaults = hday09DefaultMatrix();
    var stored = raw.matrix || {};
    var matrix = {};
    kinds.forEach(function (kind) {
      var entry = {};
      var base = defaults[kind] || {};
      Object.keys(base).forEach(function (field) { entry[field] = base[field]; });
      var override = stored[kind] || {};
      Object.keys(entry).forEach(function (field) {
        if (override[field] !== undefined && override[field] !== null) {
          entry[field] = override[field];
        }
      });
      matrix[kind] = entry;
    });
    var rows = Array.isArray(raw.rows) ? raw.rows : [];
    var counts = {};
    kinds.forEach(function (kind) { counts[kind] = 0; });
    var open = [];
    rows.forEach(function (row) {
      if (!row || row.acked) return;
      if (counts[row.kind] !== undefined) {
        counts[row.kind] += Number(row.count) || 1;
      }
      open.push(row);
    });
    var otherRows = rows.filter(function (row) {
      return row && (row.kind === 'other' || kinds.indexOf(row.kind) < 0);
    });
    var otherCount = otherRows.reduce(function (sum, row) {
      return sum + (Number(row.count) || 1);
    }, 0);
    var unknownKinds = Object.keys(stored).filter(function (kind) {
      return kinds.indexOf(kind) < 0;
    });
    var debt = raw.debt || {};
    return {
      matrix: matrix,
      rows: rows,
      open: open,
      counts: counts,
      otherRows: otherRows,
      otherCount: otherCount,
      unknownKinds: unknownKinds,
      total: Number(debt.total) || 0,
      unacked: Number(debt.unacked) || 0,
      quiet: raw.quiet || null,
      samples: raw.ms_samples || [],
      lastFetch: raw.generated_at || null
    };
  } catch (e) {
    return {
      matrix: hday09DefaultMatrix(),
      rows: [],
      open: [],
      counts: {},
      otherRows: [],
      otherCount: 0,
      unknownKinds: [],
      total: 0,
      unacked: 0,
      quiet: null,
      samples: [],
      lastFetch: null
    };
  }
}

// Expand/collapse state for coalesced rows (component body stays pure).
function hday09ExpandAtom() {
  if (!hday09ExpandAtom._atom) {
    try {
      hday09ExpandAtom._atom = atom({});
    } catch (e) {
      hday09ExpandAtom._atom = null;
    }
  }
  return hday09ExpandAtom._atom;
}

function hday09ToggleExpand(key) {
  try {
    var target = hday09ExpandAtom();
    if (!target) return;
    var current = target.get() || {};
    var next = {};
    Object.keys(current).forEach(function (k) { next[k] = current[k]; });
    next[key] = !current[key];
    target.set(next);
  } catch (e) {
    /* never throw */
  }
}

function hday09Clock(minute, fallback) {
  try {
    var m = Number(minute);
    if (!isFinite(m)) return fallback;
    m = ((m % 1440) + 1440) % 1440;
    var hh = Math.floor(m / 60);
    var mm = m % 60;
    return (hh < 10 ? '0' : '') + hh + ':' + (mm < 10 ? '0' : '') + mm;
  } catch (e) {
    return fallback;
  }
}

// ------------------------------------------------------------ table rows

function hday09CycleCell(props) {
  return jsx('span', {
    className: 'hday-chip',
    role: 'button',
    tabIndex: 0,
    title: props.title || '',
    style: hday09GridCellStyle(props.color),
    onClick: props.onClick,
    children: props.label
  });
}

function hday09KindRow(props) {
  var kind = props.kind;
  var entry = props.entry;
  var icons = hday09Icons();
  var count = props.count || 0;
  var sevColor = hday09SevColor(entry.sev);
  var nextDelivery = hday09Next(hday09DeliveryCycle(), entry.delivery);
  var nextCoalesce = hday09Next(hday09CoalesceCycle(), Number(entry.coalesce_s) || 0);
  var nextQuiet = hday09Next(hday09QuietCycle(), entry.quiet);
  var nextPreset = hday09NextLadder(entry.ladder);
  return jsxs('div', {
    className: 'hday-row',
    key: kind,
    style: Object.assign(hday09GridStyle(), {
      padding: '6px 8px',
      borderBottom: '1px solid var(--ui-stroke-secondary)'
    }),
    children: [
      jsxs('span', {
        style: { display: 'flex', alignItems: 'center', gap: '6px', minWidth: 0 },
        children: [
          jsx(Codicon, { name: icons[kind] || 'bell', size: 12, style: { color: sevColor } }),
          jsx('span', {
            className: 'truncate text-[0.7rem]',
            style: { color: C.text, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' },
            children: kind
          })
        ]
      }),
      jsx('span', {
        className: 'text-[0.62rem] tabular-nums',
        style: { color: sevColor, fontWeight: 600 },
        children: entry.sev
      }),
      hday09CycleCell({
        label: entry.delivery,
        color: hday09DeliveryColor(entry.delivery),
        title: 'Click to cycle delivery: silent → badge → notify',
        onClick: function () {
          props.onSet(kind, 'delivery', nextDelivery);
        }
      }),
      hday09CycleCell({
        label: hday09CoalesceText(entry.coalesce_s),
        color: C.text,
        title: 'Click to cycle coalesce window: 30s → 1m → 5m → 10m → never',
        onClick: function () {
          props.onSet(kind, 'coalesce', String(nextCoalesce));
        }
      }),
      hday09CycleCell({
        label: entry.quiet,
        color: hday09QuietColor(entry.quiet),
        title: 'Quiet hours suppression: never / downgrade to badge / downgrade to silent',
        onClick: function () {
          props.onSet(kind, 'quiet', nextQuiet);
        }
      }),
      hday09CycleCell({
        label: hday09LadderText(entry.ladder),
        color: C.text,
        title: hday09LadderDetail(entry.ladder),
        onClick: function () {
          props.onSet(kind, 'ladder', hday09LadderArg(nextPreset.ladder));
        }
      }),
      jsx('span', {
        className: 'text-[0.65rem] tabular-nums',
        style: { color: count > 0 ? C.amber : C.faint, textAlign: 'right' },
        children: count > 0 ? count + ' ×' : '0'
      })
    ]
  });
}

function hday09TableHead() {
  var columns = ['kind', 'sev', 'delivery', 'coalesce', 'quiet', 'escalate after', 'count'];
  return jsxs('div', {
    className: 'hday-row',
    key: 'head',
    style: Object.assign(hday09GridStyle(), {
      padding: '4px 8px',
      borderBottom: '1px solid var(--ui-stroke-secondary)'
    }),
    children: columns.map(function (label) {
      return jsx('span', {
        key: label,
        className: 'text-[0.58rem] uppercase tracking-wider',
        style: {
          color: C.faint,
          textAlign: label === 'count' ? 'right' : 'left'
        },
        children: label
      });
    })
  });
}

// ------------------------------------------------- coalesced open rows

function hday09OpenRow(props) {
  var row = props.row;
  var icons = hday09Icons();
  var sevColor = hday09SevColor(row.sev || 'P2');
  var count = Number(row.count) || 1;
  var expanded = !!props.expanded;
  var entries = Array.isArray(row.entries) ? row.entries : [];
  return jsxs('div', {
    className: 'hday-attn',
    key: row.key,
    style: {
      borderLeftColor: sevColor,
      padding: '6px 8px',
      borderBottom: '1px solid var(--ui-stroke-secondary)'
    },
    children: [
      jsxs('div', {
        style: { display: 'flex', alignItems: 'center', gap: '8px', minWidth: 0 },
        children: [
          jsx(Codicon, {
            name: icons[row.kind] || 'bell',
            size: 12,
            style: { color: sevColor, marginTop: '2px' }
          }),
          jsx('span', {
            className: 'truncate text-[0.68rem]',
            style: { color: C.text },
            children: row.text || row.kind
          }),
          count > 1
            ? jsx(Badge, {
                tone: 'warn',
                children: '×' + count
              }, 'count')
            : null,
          jsx('span', {
            className: 'text-[0.58rem]',
            style: { color: C.faint },
            children: row.sessionKey ? String(row.sessionKey) : 'local'
          }),
          jsx('span', {
            className: 'text-[0.58rem] tabular-nums',
            style: { color: C.faint },
            children: row.lastTs ? relativeTime(row.lastTs * 1000) : ''
          }),
          jsx('span', {
            className: 'text-[0.58rem] tabular-nums',
            style: { color: row.acked ? C.faint : C.amber },
            children: row.acked ? 'acked' : 'unacked ' + String(row.age_min) + 'm'
          }),
          jsx('span', {
            className: 'text-[0.6rem] tabular-nums',
            style: { color: C.muted },
            children: 'debt ' + (Number(row.debt) || 0).toFixed(1)
          }),
          jsx('span', {
            className: 'text-[0.58rem]',
            style: { color: hday09DeliveryColor(row.effective) },
            children: String(row.effective || '-')
          }),
          row.rung >= 0
            ? jsx('span', {
                className: 'text-[0.56rem]',
                style: { color: C.mono },
                children: 'rung ' + String(row.rung) + ' · ' + String(row.action || '')
              }, 'rung')
            : null,
          jsx('span', { style: { marginLeft: 'auto', display: 'flex', gap: '6px' } },
            jsx(Button, {
              size: 'sm',
              variant: 'ghost',
              onClick: function () {
                hday09ToggleExpand(row.key);
              },
              children: expanded ? 'Hide source' : 'Source'
            })),
          jsx(Button, {
            size: 'sm',
            variant: 'ghost',
            onClick: function () {
              props.onAck(row.key);
            },
            children: 'Ack'
          })
        ]
      }),
      expanded && entries.length
        ? jsxs('div', {
            className: 'hday-receipt',
            style: { marginTop: '6px', color: C.muted },
            children: [
              jsx('div', { className: 'hday-receipt-row', children: 'source ledger (rec["attn"])' }),
              jsx('div', { className: 'hday-receipt-sep' }),
              entries.map(function (entry, i) {
                return jsxs('div', {
                  className: 'hday-receipt-row',
                  key: String(i),
                  children: [
                    jsx('span', { className: 'truncate', children: String(entry.text || '') }),
                    jsx('span', { children: entry.ts ? relativeTime(entry.ts * 1000) : '' })
                  ]
                });
              })
            ]
          })
        : null
    ]
  });
}

function hday09OtherRow(props) {
  if (!props.count && !props.rows.length) return null;
  return jsxs('div', {
    className: 'hday-row',
    key: 'other',
    style: {
      padding: '5px 8px',
      borderBottom: '1px solid var(--ui-stroke-secondary)',
      display: 'flex',
      alignItems: 'center',
      gap: '8px'
    },
    children: [
      jsx(Codicon, { name: 'inbox', size: 12, style: { color: C.slate } }),
      jsx('span', {
        className: 'text-[0.65rem]',
        style: { color: C.slate },
        children: 'other (unknown kinds / overflow)'
      }),
      jsx(Badge, { tone: 'muted', children: '+' + props.count + ' more' }),
      jsx('span', {
        className: 'text-[0.58rem]',
        style: { color: C.faint },
        children: props.rows
          .map(function (row) { return String(row.kind) + ' ×' + String(row.count || 1); })
          .slice(0, 4)
          .join(' · ')
      })
    ]
  });
}

// ------------------------------------------------------------- panel

function hdayPanel09(props) {
  props = props || {};
  var route = props.route || null;

  var query = useQuery({
    queryKey: ['hday-matrix'],
    queryFn: function () {
      try {
        var pending = rpc(route, 'command.dispatch', { name: 'day-matrix', arg: 'json' });
        var parse = function (disp) {
          try {
            var out = disp && (disp.output || disp.text || '');
            return out && String(out).trim().charAt(0) === '{'
              ? JSON.parse(out)
              : { matrix: {}, rows: [], debt: {}, quiet: null };
          } catch (e) {
            return { matrix: {}, rows: [], debt: {}, quiet: null };
          }
        };
        if (pending && typeof pending.then === 'function') {
          return pending.then(parse, function () {
            return { matrix: {}, rows: [], debt: {}, quiet: null };
          });
        }
        return parse(pending);
      } catch (e) {
        return { matrix: {}, rows: [], debt: {}, quiet: null };
      }
    },
    staleTime: 4000,
    refetchInterval: 20000,
    refetchOnWindowFocus: true,
    retry: false
  });

  var expandAtom = hday09ExpandAtom();
  var expanded = (expandAtom && useValue(expandAtom)) || {};

  var data = hday09Data(query);
  var kinds = hday09Kinds();
  var refetch = function () {
    try {
      if (query && query.refetch) query.refetch();
    } catch (e) {
      /* never throw */
    }
  };
  var onSet = function (kind, field, value) {
    hday09SetCell(route, refetch, kind, field, value);
  };
  var onAck = function (key) {
    hday09Ack(route, refetch, key);
  };

  var quiet = data.quiet || {};
  var quietLabel = hday09Clock(quiet.start, '22:00') + '–' + hday09Clock(quiet.end, '07:00');
  var debtColor = data.total > 0 ? C.amber : C.emerald;

  return jsx('div', {
    className: cn('hday-panel hday-matrix', props.className),
    children: jsxs('div', {
      style: { display: 'flex', flexDirection: 'column', gap: '8px' },
      children: [
        // header — spec §6: "ROUTING                    debt 42 ▸"
        jsxs('div', {
          className: 'hday-card',
          key: 'head',
          style: {
            display: 'flex',
            alignItems: 'center',
            gap: '10px',
            padding: '8px 10px',
            border: '1px solid var(--ui-stroke-secondary)',
            borderRadius: '6px'
          },
          children: [
            jsx(Codicon, { name: 'list-flat', size: 14, style: { color: C.mono } }),
            jsx('span', {
              className: 'text-[0.7rem] uppercase tracking-widest',
              style: { color: C.text, fontWeight: 700 },
              children: 'Routing'
            }),
            jsx('span', {
              className: 'text-[0.6rem]',
              style: { color: C.faint },
              children: 'quiet ' + quietLabel + (quiet.active ? ' · ACTIVE' : '')
            }),
            jsx('span', {
              className: 'text-[0.56rem]',
              style: { color: C.faint },
              children: data.samples.length
                ? 'ledger ms ' + data.samples.map(function (s) { return String(Math.round(s)); }).join('/')
                : ''
            }),
            jsx('span', {
              style: { marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: '8px' },
              children: [
                jsx('span', {
                  className: 'text-[0.72rem] tabular-nums',
                  style: { color: debtColor, fontWeight: 700 },
                  children: 'debt ' + data.total.toFixed(1)
                }),
                jsx(Codicon, { name: 'chevron-right', size: 12, style: { color: debtColor } }),
                jsx('span', {
                  className: 'text-[0.58rem] tabular-nums',
                  style: { color: C.faint },
                  children: data.unacked + ' unacked'
                }),
                jsx(Button, {
                  size: 'sm',
                  variant: 'ghost',
                  onClick: function () {
                    hday09Reset(route, refetch);
                  },
                  children: 'Reset defaults'
                }),
                jsx(Kbd, { children: '/day-matrix' })
              ]
            })
          ]
        }),

        // routing table — six rows, one per _ATTN_KINDS entry
        jsxs('div', {
          className: 'hday-card',
          key: 'table',
          style: {
            border: '1px solid var(--ui-stroke-secondary)',
            borderRadius: '6px',
            overflow: 'hidden'
          },
          children: [
            hday09TableHead(),
            kinds.map(function (kind) {
              return jsx(hday09KindRow, {
                kind: kind,
                entry: data.matrix[kind],
                count: data.counts[kind] || 0,
                onSet: onSet,
                key: kind
              });
            }),
            data.unknownKinds.length
              ? hday09OtherRow({ count: data.otherCount, rows: data.otherRows })
              : null
          ]
        }),

        // coalesced flood rows — one row per key with ×N, never one per event
        jsxs('div', {
          className: 'hday-card',
          key: 'open',
          style: {
            border: '1px solid var(--ui-stroke-secondary)',
            borderRadius: '6px',
            padding: '6px 0'
          },
          children: [
            jsxs('div', {
              style: { display: 'flex', alignItems: 'center', gap: '8px', padding: '2px 10px 6px' },
              children: [
                jsx('span', {
                  className: 'text-[0.6rem] uppercase tracking-wider',
                  style: { color: C.faint },
                  children: 'open attention (coalesced)'
                }),
                jsx(Badge, { tone: data.open.length ? 'warn' : 'muted',
                  children: data.open.length + ' rows' }),
                jsx('span', { style: { marginLeft: 'auto' } },
                  jsx(Tip, {
                    children: 'Floods collapse to ONE row with a count (MAX_GROUPS ' +
                      '40, overflow folds into a +N more row) — never one row per event.'
                  }))
              ]
            }),
            data.open.length === 0 && !data.otherRows.length
              ? jsx('div', {
                  className: 'hday-empty',
                  children: jsx('div', {
                    className: 'hday-empty-body',
                    children: 'No open attention. Repeats inside a window are absorbed into a single counted row.'
                  })
                })
              : null,
            data.open.map(function (row) {
              return jsx(hday09OpenRow, {
                row: row,
                expanded: !!expanded[row.key],
                onAck: onAck,
                key: row.key
              });
            }),
            hday09OtherRow({ count: data.otherCount, rows: data.otherRows }),
            jsxs('div', {
              className: 'hday-actions',
              key: 'actions',
              style: { display: 'flex', gap: '8px', padding: '8px 10px 2px' },
              children: [
                jsx(Button, {
                  size: 'sm',
                  variant: 'ghost',
                  onClick: function () {
                    hday09Ack(route, refetch, '*');
                  },
                  children: 'Ack all'
                }),
                jsx(Button, {
                  size: 'sm',
                  variant: 'ghost',
                  onClick: refetch,
                  children: 'Refresh'
                }),
                jsx(Tip, {
                  children: 'edit: day-matrix-set <kind> <delivery|coalesce|quiet|ladder> <value> · ack: day-attn-ack <*|key>'
                })
              ]
            })
          ]
        })
      ]
    })
  });
}

// ---- lane 10: skin-hud ----
// Lane 10 — Skin & HUD Engine (splice-ready snippet, NOT a module).
// No import/export: parses with `node --check` as a plain script.
// Top-level declarations: functions only.
//
// Module-scope symbols assumed in scope (already imported by desktop/plugin.js):
//   jsx, jsxs, Badge, Button, Codicon, Kbd, Tip, atom, useValue, cn,
//   relativeTime, host, rpc, C, useState, useEffect, useRef, haptic
//
// Exports:
//   hdayPanel10(props)      — HUD layer; render it as a child of .hday-root
//   hdaySkinCss(palette)    — returns the structural CSS text (a string; no DOM writes)
//   hdayKeymapRows()        — KEYMAP_ROWS, the single source of truth for the cheatsheet
//   hdayUseReducedMotion()  — prefers-reduced-motion guard hook
//
// COLOUR RULE: this file carries ZERO colour literals (gate: grep for
// hex/rgb/hsl must count 0). Skin palettes live in the ONE sanctioned
// sentinel block `SKIN_COLOR_CSS` in patches/10-skin-hud.py — pass it to
// hdaySkinCss(paletteCss), or splice it once into ensureDayStyles(); if a
// `SKIN_COLOR_CSS` binding is already in scope at splice time it is picked
// up automatically. Every structural rule below only consumes var(--hday-*)
// with --ui-* fallbacks, stays under .hday-root, and never uses an importance
// override flag.
//
// props (all optional, every one passed straight through from the Day scan):
//   needs, flight, waiting, jobs      arrays (counts come from .length)
//   hiddenCount, scannedAt            numbers (scannedAt = epoch ms, Date.now())
//   scan | data                       whole scanInbox payload, used as fallback
//   skin                              'current' | 'phosphor' | 'oxide'
//   onSetSkin(name)                   parent hook when it owns the selection
//   feedLength, selectedEntry         live feed[sel] state -> gate dimming

// ---------------------------------------------------------------------------
// prefers-reduced-motion — one shared guard for every animation in this lane
// ---------------------------------------------------------------------------
function hdayUseReducedMotion() {
  var pair = useState(false);
  var setReduced = pair[1];
  useEffect(function () {
    try {
      if (typeof window === 'undefined' || !window.matchMedia) return undefined;
      var mq = window.matchMedia('(prefers-reduced-motion: reduce)');
      var apply = function () {
        try {
          setReduced(!!mq.matches);
        } catch (e) {
          /* never throw out of an effect */
        }
      };
      apply();
      if (mq.addEventListener) mq.addEventListener('change', apply);
      else if (mq.addListener) mq.addListener(apply);
      return function () {
        try {
          if (mq.removeEventListener) mq.removeEventListener('change', apply);
          else if (mq.removeListener) mq.removeListener(apply);
        } catch (e) {
          /* teardown is best-effort */
        }
      };
    } catch (e) {
      return undefined;
    }
  }, []);
  return !!pair[0];
}

// ---------------------------------------------------------------------------
// KEYMAP_ROWS — the cheatsheet UI and its source trace read this ONE array.
// Every binding here is verified present in desktop/plugin.js:
//   j/k/a/d/s/o -> the triage keydown handler, 'mod+shift+i' -> host keybinds.
// ---------------------------------------------------------------------------
function hdayKeymapRows() {
  return [
    { keys: ['j'], action: 'Next row', note: 'feed select + 1', gate: 'always', gateLabel: '—', line: 'plugin.js:3036' },
    { keys: ['k'], action: 'Previous row', note: 'feed select - 1', gate: 'always', gateLabel: '—', line: 'plugin.js:3039' },
    { keys: ['a'], action: 'Approve selected approval', note: 'deny stays on d', gate: 'approval', gateLabel: 'selected approval', line: 'plugin.js:3043' },
    { keys: ['d'], action: 'Deny selected approval', note: 'once-per-request', gate: 'approval', gateLabel: 'selected approval', line: 'plugin.js:3045' },
    { keys: ['s'], action: 'Snooze selected request', note: 'hides the card until later', gate: 'need', gateLabel: 'selected need', line: 'plugin.js:3047' },
    { keys: ['o'], action: 'Open selected session', note: 'Enter does the same', gate: 'row', gateLabel: 'a row is selected', line: 'plugin.js:3049' },
    { keys: ['mod', 'shift', 'i'], action: 'Toggle the day panel', note: 'host chrome keybind', gate: 'global', gateLabel: 'global', line: 'plugin.js:911' },
    { keys: ['click'], action: 'Select row', note: 'mouse equivalent of j/k', gate: 'row', gateLabel: 'a row is selected', line: 'plugin.js:3120' }
  ];
}

// Gate satisfaction for one keymap row: dim when the binding cannot fire.
function hdayRowActive(row, selectedEntry, feedLength) {
  try {
    var gate = row && row.gate;
    if (gate === 'always' || gate === 'global') return true;
    if (gate === 'row') return feedLength > 0;
    if (!selectedEntry || selectedEntry.type !== 'need') return false;
    if (gate === 'need') return true;
    if (gate === 'approval') {
      var item = selectedEntry.item || {};
      return item.kind === 'approval';
    }
    return true;
  } catch (e) {
    return true;
  }
}

function hdaySkinLabel(name) {
  var n = String(name || 'current');
  if (n === 'phosphor') return 'PHOSPHOR';
  if (n === 'oxide') return 'OXIDE';
  return 'CURRENT';
}

function hdaySkinOrder() {
  return ['current', 'phosphor', 'oxide'];
}

// Real age of the last scan: seconds since scannedAt (epoch ms or seconds).
function hdayAgeLabel(ts, nowMs) {
  var t = Number(ts);
  if (!Number.isFinite(t) || t <= 0) return '—';
  if (t < 1e12) t = t * 1000;
  var s = Math.floor((nowMs - t) / 1000);
  if (!Number.isFinite(s) || s < 0) s = 0;
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm' + String(s % 60) + 's';
  return Math.floor(s / 3600) + 'h' + Math.floor((s % 3600) / 60) + 'm';
}

// ---------------------------------------------------------------------------
// corner telemetry — bottom-left, brackets, values come from real counts
// ---------------------------------------------------------------------------
function hdayTelemetry(props) {
  var lines = (props && props.lines) || [];
  var kids = [jsx('div', { className: 'hday-hud-tele-title', key: 'title' }, 'HERMES-DAY HUD')];
  lines.forEach(function (line) {
    kids.push(
      jsxs('div', { className: 'hday-hud-tele-line', 'data-tone': line[2], key: line[0] }, [
        jsx('span', { className: 'hday-hud-tele-k', key: 'k' }, line[0]),
        jsx('span', { className: 'hday-hud-tele-v', key: 'v' }, String(line[1]))
      ])
    );
  });
  return jsx('div', { className: 'hday-hud-tele', children: kids });
}

// ---------------------------------------------------------------------------
// live signal bar — 0..12 blocks, always prints the exact needs count > 0
// ---------------------------------------------------------------------------
function hdaySignalBar(props) {
  var p = props || {};
  var raw = typeof p.needs === 'number' ? p.needs : 0;
  var n = raw > 0 ? Math.floor(raw) : 0;
  var cap = 12;
  var blocks = n > cap ? cap : n;
  var kids = [];
  for (var i = 0; i < cap; i++) {
    kids.push(jsx('span', { className: 'hday-sig-block', 'data-on': i < blocks ? '1' : '0', key: 'b' + i }));
  }
  return jsxs(
    'div',
    {
      className: 'hday-hud-signal',
      'aria-label': n + ' need' + (n === 1 ? '' : 's') + ' waiting',
      children: [
        jsxs(
          'div',
          { className: 'hday-sig-track', 'data-hot': n > 0 ? '1' : '0', 'data-calm': n === 0 ? '1' : '0', key: 'track' },
          kids
        ),
        n > 0 ? jsx('span', { className: 'hday-sig-count', key: 'count' }, String(n)) : null
      ]
    }
  );
}

// ---------------------------------------------------------------------------
// in-cockpit keymap cheatsheet (click-to-toggle, no keybinding of its own)
// ---------------------------------------------------------------------------
function hdayKeymap(props) {
  var p = props || {};
  var rows = p.rows && p.rows.length ? p.rows : hdayKeymapRows();
  var kids = [
    jsxs('div', { className: 'hday-keymap-title', key: 'title' }, [
      jsx('span', { key: 'a' }, 'IN-COCKPIT KEYMAP'),
      jsx('span', { className: 'hday-keymap-scope', key: 'b' }, '/day foreground')
    ]),
    jsxs('div', { className: 'hday-keymap-head', key: 'head' }, [
      jsx('span', { key: 'k' }, 'KEYS'),
      jsx('span', { key: 'a' }, 'ACTION'),
      jsx('span', { key: 'g' }, 'GATE'),
      jsx('span', { key: 's' }, 'SOURCE')
    ])
  ];
  rows.forEach(function (row, i) {
    var active = hdayRowActive(row, p.selectedEntry || null, p.feedLength || 0);
    var keys = (row.keys || []).map(function (k, j) {
      return jsx(Kbd, { key: 'k' + j }, k);
    });
    kids.push(
      jsxs(
        'div',
        { className: 'hday-keymap-row', 'data-active': active ? '1' : '0', key: 'r' + i },
        [
          jsxs('span', { className: 'hday-keymap-keys', key: 'keys' }, keys),
          jsxs('span', { className: 'hday-keymap-action', key: 'action' }, [
            row.action,
            row.note ? jsx('span', { className: 'hday-keymap-note', key: 'n' }, ' ' + row.note) : null
          ]),
          jsx('span', { className: 'hday-keymap-gate', key: 'gate' }, row.gateLabel || '—'),
          jsx('span', { className: 'hday-keymap-src', key: 'src' }, (active ? 'ready · ' : 'idle · ') + row.line)
        ]
      )
    );
  });
  kids.push(
    jsx('div', { className: 'hday-keymap-foot', key: 'foot' }, [
      jsx(Tip, { key: 'tip' }, 'skins recolor tokens, not layout.')
    ])
  );
  return jsxs('div', { className: 'hday-keymap', role: 'dialog', 'aria-label': 'In-cockpit keymap' }, kids);
}

// ---------------------------------------------------------------------------
// hdayPanel10 — the HUD layer: scanlines + vignette, corner telemetry,
// boot sweep on first open, live signal bar, keymap chip + cheatsheet,
// skin cycler. Pure component: state via hooks, handlers read imperatively.
// ---------------------------------------------------------------------------
function hdayPanel10(props) {
  var p = props || {};
  var reduced = hdayUseReducedMotion();
  var KEYMAP_ROWS = hdayKeymapRows();

  var skinPair = useState(typeof p.skin === 'string' && p.skin ? p.skin : 'current');
  var skin = typeof p.skin === 'string' && p.skin ? p.skin : skinPair[0];
  var bootPair = useState(function () {
    try {
      return typeof sessionStorage !== 'undefined' && !sessionStorage.getItem('hday.hud-booted');
    } catch (e) {
      return false;
    }
  });
  var boot = !!bootPair[0];
  var cheatPair = useState(false);
  var nowPair = useState(function () {
    return Date.now();
  });
  var now = Number(nowPair[0]) || Date.now();

  // clock: keeps the SCAN age honest without faking a value
  useEffect(function () {
    var id = setInterval(function () {
      try {
        nowPair[1](Date.now());
      } catch (e) {
        /* ignore */
      }
    }, 1000);
    return function () {
      clearInterval(id);
    };
  }, []);

  // apply the skin as an attribute on .hday-root (base block stays "current")
  useEffect(function () {
    var root = null;
    try {
      root = document.querySelector('.hday-root');
    } catch (e) {
      root = null;
    }
    if (!root) return undefined;
    try {
      root.setAttribute('data-hday-skin', skin);
    } catch (e) {
      /* never throw out of an effect */
    }
    return function () {
      try {
        root.removeAttribute('data-hday-skin');
      } catch (e) {
        /* teardown is best-effort */
      }
    };
  }, [skin]);

  // boot sequence: armed once per browser session, skipped entirely when the
  // user asked for reduced motion
  useEffect(function () {
    var root = null;
    try {
      root = document.querySelector('.hday-root');
    } catch (e) {
      root = null;
    }
    if (reduced) {
      try {
        if (typeof sessionStorage !== 'undefined') sessionStorage.setItem('hday.hud-booted', '1');
      } catch (e) {
        /* ignore */
      }
      if (boot) bootPair[1](false);
      return undefined;
    }
    if (!boot) return undefined;
    try {
      if (root) root.setAttribute('data-hday-boot', '1');
    } catch (e) {
      /* ignore */
    }
    var id = setTimeout(function () {
      try {
        if (root) root.removeAttribute('data-hday-boot');
      } catch (e) {
        /* ignore */
      }
      try {
        if (typeof sessionStorage !== 'undefined') sessionStorage.setItem('hday.hud-booted', '1');
      } catch (e) {
        /* ignore */
      }
      bootPair[1](false);
    }, 900);
    return function () {
      clearTimeout(id);
      try {
        if (root) root.removeAttribute('data-hday-boot');
      } catch (e) {
        /* ignore */
      }
    };
  }, [reduced, boot]);

  // ---- real counts only: array lengths and scan payload numbers ----
  var scan = p.scan || p.data || null;
  var needs = p.needs || (scan && scan.needs) || [];
  var flight = p.flight || (scan && scan.flight) || [];
  var waiting = p.waiting || (scan && scan.waiting) || [];
  var jobs = p.jobs || [];
  var needsLen = Array.isArray(needs) ? needs.length : typeof needs === 'number' ? needs : 0;
  var flightLen = Array.isArray(flight) ? flight.length : typeof flight === 'number' ? flight : 0;
  var waitingLen = Array.isArray(waiting) ? waiting.length : typeof waiting === 'number' ? waiting : 0;
  var jobsLen = Array.isArray(jobs) ? jobs.length : typeof jobs === 'number' ? jobs : 0;
  var hidden = typeof p.hiddenCount === 'number' ? p.hiddenCount : scan && typeof scan.hiddenCount === 'number' ? scan.hiddenCount : 0;
  var scannedAt = typeof p.scannedAt === 'number' ? p.scannedAt : scan && typeof scan.scannedAt === 'number' ? scan.scannedAt : 0;
  var age = hdayAgeLabel(scannedAt, now);
  var fresh = scannedAt > 0 && now - (scannedAt < 1e12 ? scannedAt * 1000 : scannedAt) < 15000;
  var feedLength = typeof p.feedLength === 'number' ? p.feedLength : 0;
  var selectedEntry = p.selectedEntry || p.selEntry || null;

  var lines = [
    ['NEEDS', needsLen, needsLen > 0 ? 'danger' : 'ok'],
    ['HIDDEN', hidden, hidden > 0 ? 'warn' : 'faint'],
    ['FLIGHT', flightLen, flightLen > 0 ? 'accent' : 'faint'],
    ['WAIT', waitingLen, waitingLen > 0 ? 'warn' : 'faint'],
    ['JOBS', jobsLen, jobsLen > 0 ? 'accent' : 'faint'],
    ['SCAN', age, fresh ? 'ok' : 'warn'],
    ['SKIN', hdaySkinLabel(skin), 'accent']
  ];

  // handlers read state imperatively — never mutate atoms during render
  function cycleSkin() {
    var order = hdaySkinOrder();
    var next = order[(order.indexOf(skin) + 1) % order.length] || order[0];
    try {
      skinPair[1](next);
    } catch (e) {
      /* ignore */
    }
    if (typeof p.onSetSkin === 'function') {
      try {
        p.onSetSkin(next);
      } catch (e) {
        /* parent hook is optional */
      }
    }
  }

  function toggleKeymap() {
    try {
      cheatPair[1](!cheatPair[0]);
    } catch (e) {
      /* ignore */
    }
  }

  var kids = [];
  // scanlines (static grid) + a slow sweep band
  kids.push(
    jsx('div', { className: 'hday-hud-scan', key: 'scan' }, [
      jsx('div', { className: 'hday-hud-band', 'data-still': reduced ? '1' : '0', key: 'band' })
    ])
  );
  kids.push(jsx('div', { className: 'hday-hud-vignette', key: 'vignette' }));
  // first-open boot sweep
  if (boot && !reduced) kids.push(jsx('div', { className: 'hday-boot-sweep', key: 'boot' }));
  // corner telemetry, 16px above the KEYMAP chip
  kids.push(jsx(hdayTelemetry, { key: 'telemetry', lines: lines }));
  // live signal bar along the bottom edge
  kids.push(jsx(hdaySignalBar, { key: 'signal', needs: needsLen }));
  // keymap cheatsheet (opens upward from its chip)
  if (cheatPair[0]) {
    kids.push(
      jsx(hdayKeymap, {
        key: 'keymap',
        rows: KEYMAP_ROWS,
        feedLength: feedLength,
        selectedEntry: selectedEntry
      })
    );
  }
  // chips last so they stack above the panel
  kids.push(
    jsxs(
      'button',
      {
        type: 'button',
        className: 'hday-keymap-chip',
        key: 'keymap-chip',
        'aria-expanded': cheatPair[0] ? 'true' : 'false',
        onClick: toggleKeymap,
        children: [
          jsx(Codicon, { name: 'keyboard', key: 'icon' }),
          jsx('span', { key: 'label' }, 'KEYMAP'),
          jsx(Kbd, { key: 'kbd' }, 'mod+shift+i')
        ]
      }
    )
  );
  kids.push(
    jsxs(
      'button',
      {
        type: 'button',
        className: 'hday-skin-chip',
        key: 'skin-chip',
        title: 'Cycle cockpit skin (persisted as skin.v1)',
        onClick: cycleSkin,
        children: [
          jsx(Codicon, { name: 'color-mode', key: 'icon' }),
          jsx('span', { key: 'label' }, 'SKIN · ' + hdaySkinLabel(skin))
        ]
      }
    )
  );

  return jsx('div', {
    className: 'hday-hud',
    'data-hday-skin': skin,
    'data-reduced': reduced ? '1' : '0',
    children: kids
  });
}

// ---------------------------------------------------------------------------
// hdaySkinCss(paletteCss) — CSS TEXT BUILDER. Returns a string; never touches
// the DOM. Pass the sanctioned block from patches/10-skin-hud.py (it is also
// auto-detected when a `SKIN_COLOR_CSS` binding is already in scope).
// structure only: no colour literals, every selector under .hday-root, and no
// importance override flag anywhere. The prefers-reduced-motion block is LAST
// so it wins by source order.
// ---------------------------------------------------------------------------
function hdaySkinCss(paletteCss) {
  // Palette argument wins when provided; a call with NO argument auto-detects
  // an in-scope SKIN_COLOR_CSS binding (splice convenience). An explicit empty
  // string means structure only — that path is what the colour gate exercises.
  var palette = typeof paletteCss === 'string' ? paletteCss : '';
  try {
    if (!palette && typeof paletteCss === 'undefined'
      && typeof SKIN_COLOR_CSS === 'string' && SKIN_COLOR_CSS) {
      palette = SKIN_COLOR_CSS;
    }
  } catch (e) {
    palette = '';
  }

  var structure = [
    '/* lane 10 structure — consumes var(--hday-*) from the sanctioned palette block; '
      + 'every selector stays under .hday-root, no importance override flag anywhere */',
    '.hday-root{position:relative}',
    '.hday-root .hday-hud{position:absolute;inset:0;z-index:6;pointer-events:none;'
      + 'color:var(--hday-ink,var(--ui-text-primary));'
      + 'font-family:ui-monospace,SFMono-Regular,Menlo,monospace}',
    // scanlines: static grid, opacity from the skin token (kept <= 35 percent)
    '.hday-root .hday-hud-scan{position:absolute;inset:0;pointer-events:none;overflow:hidden;'
      + 'opacity:var(--hday-scan-alpha,18%);color:var(--hday-canvas,var(--ui-bg-primary));'
      + 'background-image:repeating-linear-gradient(to bottom,transparent 0 2px,currentColor 2px 3px)}',
    '.hday-root .hday-hud-band{position:absolute;left:0;right:0;top:0;height:16%;pointer-events:none;'
      + 'background:linear-gradient(to bottom,transparent,'
      + 'color-mix(in srgb,var(--hday-accent,var(--ui-accent)) 10%,transparent),transparent);'
      + 'animation:hday-scan-sweep 7.5s linear infinite}',
    '.hday-root .hday-hud-band[data-still="1"]{display:none}',
    // vignette
    '.hday-root .hday-hud-vignette{position:absolute;inset:0;pointer-events:none;'
      + 'background:radial-gradient(ellipse at center,transparent 52%,'
      + 'color-mix(in srgb,var(--hday-canvas,var(--ui-bg-primary)) 78%,transparent) 100%)}',
    // corner telemetry, bottom-left, 16px above the KEYMAP chip
    '.hday-root .hday-hud-tele{position:absolute;left:16px;bottom:66px;width:198px;pointer-events:none;'
      + 'border:1px solid var(--hday-line,var(--ui-stroke-secondary));'
      + 'background:color-mix(in srgb,var(--hday-canvas,var(--ui-bg-primary)) 86%,transparent);'
      + 'border-radius:5px;padding:.5rem .6rem;font-size:.62rem;line-height:1.6;letter-spacing:.03em}',
    '.hday-root .hday-hud-tele-title{font-size:.55rem;letter-spacing:.14em;color:var(--hday-ink-3,var(--ui-text-tertiary));'
      + 'margin-bottom:.35rem}',
    '.hday-root .hday-hud-tele-title::before{content:"["}',
    '.hday-root .hday-hud-tele-title::after{content:"]"}',
    '.hday-root .hday-hud-tele-line{display:flex;justify-content:space-between;gap:.6rem}',
    '.hday-root .hday-hud-tele-k{color:var(--hday-ink-3,var(--ui-text-tertiary))}',
    '.hday-root .hday-hud-tele-v{font-variant-numeric:tabular-nums}',
    '.hday-root .hday-hud-tele-line[data-tone="faint"] .hday-hud-tele-v{color:var(--hday-ink-3,var(--ui-text-tertiary))}',
    '.hday-root .hday-hud-tele-line[data-tone="ok"] .hday-hud-tele-v{color:var(--hday-ok,var(--ui-accent))}',
    '.hday-root .hday-hud-tele-line[data-tone="warn"] .hday-hud-tele-v{color:var(--hday-warn,var(--ui-accent))}',
    '.hday-root .hday-hud-tele-line[data-tone="accent"] .hday-hud-tele-v{color:var(--hday-accent,var(--ui-accent))}',
    '.hday-root .hday-hud-tele-line[data-tone="danger"] .hday-hud-tele-v{color:var(--hday-danger,var(--ui-accent))}',
    // live signal bar on the bottom edge, left-anchored
    '.hday-root .hday-hud-signal{position:absolute;left:16px;bottom:5px;display:flex;align-items:center;'
      + 'gap:.5rem;pointer-events:none}',
    '.hday-root .hday-sig-track{display:flex;gap:3px}',
    '.hday-root .hday-sig-block{width:13px;height:4px;border-radius:2px;'
      + 'background:var(--hday-line,var(--ui-stroke-secondary));opacity:.5}',
    '.hday-root .hday-sig-block[data-on="1"]{opacity:1;background:var(--hday-danger,var(--ui-accent));'
      + 'box-shadow:0 0 5px var(--hday-glow,var(--ui-accent))}',
    '.hday-root .hday-sig-track[data-calm="1"] .hday-sig-block[data-on="1"]{background:var(--hday-ok,var(--ui-accent));'
      + 'box-shadow:none}',
    '.hday-root .hday-sig-track[data-hot="1"]{animation:hday-sig-pulse 1.8s ease-in-out infinite}',
    '.hday-root .hday-sig-count{font-size:.62rem;letter-spacing:.08em;font-variant-numeric:tabular-nums;'
      + 'color:var(--hday-ink-2,var(--ui-text-secondary))}',
    // chips (shared look): KEYMAP bottom-left, SKIN bottom-right
    '.hday-root .hday-keymap-chip,.hday-root .hday-skin-chip{position:absolute;bottom:24px;'
      + 'pointer-events:auto;cursor:pointer;display:inline-flex;align-items:center;gap:.45rem;'
      + 'border:1px solid var(--hday-line,var(--ui-stroke-secondary));border-radius:999px;'
      + 'padding:.24rem .6rem;'
      + 'background:color-mix(in srgb,var(--hday-canvas,var(--ui-bg-primary)) 90%,transparent);'
      + 'color:var(--hday-ink-2,var(--ui-text-secondary));font-family:inherit;font-size:.6rem;'
      + 'letter-spacing:.08em;text-transform:uppercase;transition:border-color .12s ease,color .12s ease}',
    '.hday-root .hday-keymap-chip{left:16px}',
    '.hday-root .hday-skin-chip{right:16px}',
    '.hday-root .hday-keymap-chip:hover,.hday-root .hday-skin-chip:hover{'
      + 'border-color:var(--hday-line-hot,var(--ui-stroke-secondary));color:var(--hday-ink,var(--ui-text-primary))}',
    '.hday-root .hday-keymap-chip[aria-expanded="true"]{color:var(--hday-accent,var(--ui-accent));'
      + 'border-color:var(--hday-accent,var(--ui-accent))}',
    // keymap cheatsheet
    '.hday-root .hday-keymap{position:absolute;left:16px;bottom:58px;pointer-events:auto;'
      + 'width:min(640px,calc(100vw - 32px));border:1px solid var(--hday-line,var(--ui-stroke-secondary));'
      + 'border-radius:6px;padding:.7rem .8rem .75rem;'
      + 'background:color-mix(in srgb,var(--hday-canvas,var(--ui-bg-primary)) 95%,transparent);'
      + 'box-shadow:0 12px 30px color-mix(in srgb,var(--hday-ink,var(--ui-text-primary)) 16%,transparent);'
      + 'animation:hday-cheat-in .16s ease-out both}',
    '.hday-root .hday-keymap-title{display:flex;justify-content:space-between;gap:.6rem;font-size:.58rem;'
      + 'letter-spacing:.12em;text-transform:uppercase;color:var(--hday-ink-3,var(--ui-text-tertiary));'
      + 'margin-bottom:.5rem}',
    '.hday-root .hday-keymap-head,.hday-root .hday-keymap-row{display:grid;'
      + 'grid-template-columns:136px minmax(0,1fr) 108px 92px;gap:.6rem;align-items:center}',
    '.hday-root .hday-keymap-head{font-size:.55rem;letter-spacing:.1em;text-transform:uppercase;'
      + 'color:var(--hday-ink-3,var(--ui-text-tertiary));padding-bottom:.3rem;'
      + 'border-bottom:1px solid var(--hday-line,var(--ui-stroke-secondary))}',
    '.hday-root .hday-keymap-row{padding:.3rem 0;font-size:.68rem;opacity:.3;filter:saturate(.25);'
      + 'transition:opacity .12s ease,filter .12s ease}',
    '.hday-root .hday-keymap-row[data-active="1"]{opacity:1;filter:none}',
    '.hday-root .hday-keymap-keys{display:flex;gap:.25rem;flex-wrap:wrap;align-items:center}',
    '.hday-root .hday-keymap-note{color:var(--hday-ink-3,var(--ui-text-tertiary));font-size:.62rem}',
    '.hday-root .hday-keymap-gate,.hday-root .hday-keymap-src{font-size:.55rem;letter-spacing:.04em;'
      + 'color:var(--hday-ink-3,var(--ui-text-tertiary));font-variant-numeric:tabular-nums}',
    '.hday-root .hday-keymap-foot{margin-top:.5rem;font-size:.6rem;'
      + 'color:var(--hday-ink-3,var(--ui-text-tertiary))}',
    // boot sequence — sweep + staggered header/stat tiles while data-hday-boot="1"
    '.hday-root .hday-boot-sweep{position:absolute;left:0;right:0;top:0;height:34%;pointer-events:none;'
      + 'background:linear-gradient(to bottom,transparent,'
      + 'color-mix(in srgb,var(--hday-accent,var(--ui-accent)) 16%,transparent),transparent);'
      + 'animation:hday-hud-boot .8s ease-out both}',
    '.hday-root[data-hday-boot="1"] header{animation:hday-boot-in .4s ease-out both}',
    '.hday-root[data-hday-boot="1"] .hday-stat{animation:hday-boot-in .4s ease-out both}',
    '.hday-root[data-hday-boot="1"] .hday-stat:nth-child(1){animation-delay:0ms}',
    '.hday-root[data-hday-boot="1"] .hday-stat:nth-child(2){animation-delay:60ms}',
    '.hday-root[data-hday-boot="1"] .hday-stat:nth-child(3){animation-delay:120ms}',
    '.hday-root[data-hday-boot="1"] .hday-stat:nth-child(4){animation-delay:180ms}',
    '.hday-root[data-hday-boot="1"] .hday-stat:nth-child(5){animation-delay:240ms}',
    '.hday-root[data-hday-boot="1"] .hday-stat:nth-child(6){animation-delay:300ms}',
    '.hday-root[data-hday-boot="1"] .hday-stat:nth-child(7){animation-delay:360ms}',
    '.hday-root[data-hday-boot="1"] .hday-stat:nth-child(8){animation-delay:420ms}',
    '@keyframes hday-scan-sweep{0%{transform:translateY(-120%)}100%{transform:translateY(760%)}}',
    '@keyframes hday-sig-pulse{0%,100%{opacity:1}50%{opacity:.55}}',
    '@keyframes hday-cheat-in{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}',
    '@keyframes hday-hud-boot{0%{transform:translateY(-60%);opacity:0}30%{opacity:1}'
      + '100%{transform:translateY(320%);opacity:0}}',
    '@keyframes hday-boot-in{from{opacity:0;transform:translateY(7px)}to{opacity:1;transform:none}}',
    // LAST, before the closing: prefers-reduced-motion disables every animation.
    // Explicit selectors are repeated so each beats its own earlier rule by
    // order at equal specificity — an importance override flag is never used.
    '@media (prefers-reduced-motion:reduce){',
    '.hday-root *{animation-duration:.001s;animation-delay:0s;animation-iteration-count:1;'
      + 'transition-duration:.001s;transition-delay:0s}',
    '.hday-root .hday-hud-band,.hday-root .hday-boot-sweep{display:none}',
    '.hday-root .hday-keymap{animation:none}',
    '.hday-root .hday-sig-track{animation:none}',
    '.hday-root .hday-keymap-row,.hday-root .hday-keymap-chip,.hday-root .hday-skin-chip{transition:none}',
    '.hday-root[data-hday-boot="1"] header,.hday-root[data-hday-boot="1"] .hday-stat,'
      + '.hday-root[data-hday-boot="1"] .hday-stat:nth-child(1),'
      + '.hday-root[data-hday-boot="1"] .hday-stat:nth-child(2),'
      + '.hday-root[data-hday-boot="1"] .hday-stat:nth-child(3),'
      + '.hday-root[data-hday-boot="1"] .hday-stat:nth-child(4),'
      + '.hday-root[data-hday-boot="1"] .hday-stat:nth-child(5),'
      + '.hday-root[data-hday-boot="1"] .hday-stat:nth-child(6),'
      + '.hday-root[data-hday-boot="1"] .hday-stat:nth-child(7),'
      + '.hday-root[data-hday-boot="1"] .hday-stat:nth-child(8){animation:none}',
    '}'
  ].join('\n');

  return palette ? palette + '\n' + structure : structure;
}


/** Assembled from lanes/*.js — name, render fn. Regenerated, do not edit. */
const HDAY_PANELS = [
  ['ops-shell', hdayPanel01],
  ['vault-panel', hdayPanel02],
  ['service-wall', hdayPanel03],
  ['network-radar', hdayPanel04],
  ['session-surgery', hdayPanel05],
  ['git-forge', hdayPanel06],
  ['log-terminal', hdayPanel07],
  ['instinct-studio', hdayPanel08],
  ['escalation-matrix', hdayPanel09],
  ['skin-hud', hdayPanel10],
];

/** The toolkit band: every lane's panel, one row per feature. */
function ToolkitGrid({ scan, cronJobs }) {
  if (!HDAY_PANELS.length) return null;
  return jsxs('div', { className: 'flex flex-col gap-4',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx(Codicon, { name: 'tools', size: 12,
                        style: { color: C.mono } }),
          jsx('span', {
            className: 'text-[0.62rem] font-semibold uppercase tracking-wider',
            style: { color: C.faint }, children: 'toolkit' }),
        ],
      }),
      ...HDAY_PANELS.map(([name, Comp]) =>
        jsxs('div', { className: 'hday-card p-3',
          children: [
            jsx('div', {
              className: 'mb-1.5 text-[0.6rem] font-semibold uppercase',
              style: { color: C.faint }, children: name }),
            // F2: the HUD is the only panel that reports counts - hand it the
            // REAL payloads (null when the query has not landed) so a failed
            // probe reads as 'no data', never as a confident zero.
            jsx(Comp, Comp === hdayPanel10
              ? { scan, jobs: cronJobs, hiddenCount: scan ? (scan.hiddenCount || 0) : null,
                  scannedAt: Date.now() }
              : {}),
          ],
        }, name)),
    ],
  });
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
  const reducedRM = hdayUseReducedMotion()  // F3
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
      celebrate && !reducedRM ? jsx(Confetti, { onDone: () => setCelebrate(false) }) : null,
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
                  jsxs('div', {
                    className: 'mb-1.5 flex items-center gap-2',
                    children: [
                      jsx(GuardShield, {}),
                      jsx('span', {
                        className: 'text-[0.58rem] uppercase tracking-wider',
                        style: { color: C.faint },
                        children: 'honesty gate'
                      })
                    ]
                  }),
                  jsx(AttentionStrip, {}),
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
                    jsx(ToolkitGrid, { scan: scan.data || null, cronJobs: cron.data ? cron.data.jobs : null }),
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
