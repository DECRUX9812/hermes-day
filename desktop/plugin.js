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
  EmptyState,
  Input,
  Loader,
  ScrollArea,
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
/** request ids already notified about this app session */
const notified = new Set()
/** first scan seeds `notified` silently — no burst on app start */
let notifyArmed = false

const SNOOZE_MS = 15 * 60 * 1000

const MUTED_KEY = 'muted.v1'
const SNOOZE_KEY = 'snoozed.v1'
const PREFS_KEY = 'prefs.v1'

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

  await Promise.all(
    sessions.map(async s => {
      lastLive.set(s.id, { storedId: s.session_key || null, title: s.title || '', route })
      let open = []
      try {
        // last_seen past every seq -> empty event page, but the open-request
        // snapshot always ships — the cheap "what is this session asking" probe.
        const snap = await rpc(route, 'session.events.since', { session_id: s.id, last_seen: MAX_SEQ })
        open = snap && Array.isArray(snap.open_requests) ? snap.open_requests : []
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
        const row = { route, sourceLabel: source.label, sourceKey: source.key, session: s }
        if (s.status === 'waiting') waiting.push(row)
        else if (FLIGHT.has(s.status)) flight.push(row)
      }
    })
  )

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

const snoozeRequest = requestId => {
  const next = { ...$snoozed.get(), [requestId]: Date.now() + SNOOZE_MS }
  $snoozed.set(next)
  persistMap(SNOOZE_KEY, next)
  invalidate()
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
  const next = $finished.get().filter(f => f.key !== key)
  $finished.set(next)
  persistFinished(next)
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
}

async function respondClarify(item, answer) {
  await rpc(item.route, 'request.answer', { id: item.requestId, result: { answer: answer ?? '' } })
}

async function respondClarifyBatch(item, answersByQid) {
  const questions = Array.isArray(item.params.questions) ? item.params.questions : []
  for (let i = 0; i < questions.length; i += 1) {
    const qid = questions[i] && (questions[i].id || questions[i].question_id) ? questions[i].id || questions[i].question_id : `q${i}`
    const answer = answersByQid[qid]
    if (answer == null) continue
    await rpc(item.route, 'clarify.lock', { request_id: item.requestId, question_id: qid, answer })
  }
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

function SectionLabel({ icon, title, count, tone }) {
  return jsxs('div', {
    className: 'mb-2 flex items-center gap-2 px-1',
    children: [
      jsx(Codicon, { name: icon, className: cn('text-[0.8rem]', tone || 'text-muted-foreground') }),
      jsx('span', {
        className: 'text-[0.72rem] font-semibold uppercase tracking-wider text-muted-foreground',
        children: title
      }),
      typeof count === 'number'
        ? jsx(Badge, { size: 'xs', variant: tone === 'text-amber-500' ? 'warn' : 'muted', children: String(count) })
        : null
    ]
  })
}

function SourcePill({ label }) {
  return jsx('span', {
    className: 'inline-flex shrink-0 items-center gap-1 rounded-[3px] bg-(--ui-bg-quaternary) px-1.5 py-px text-[0.62rem] font-medium text-muted-foreground',
    children: label
  })
}

function AgoText({ ms }) {
  if (!ms) return null
  return jsx('span', {
    className: 'shrink-0 text-[0.68rem] tabular-nums text-muted-foreground/70',
    children: relativeTime(ms)
  })
}

const SPIN = { className: 'animate-spin' }
const spinIcon = name => jsx(Codicon, { name, spinning: true, className: 'text-[0.8rem]' })

// ---------------------------------------------------------------------------
// needs-you cards
// ---------------------------------------------------------------------------

function NeedsYouCard({ item }) {
  const [busy, setBusy] = useState('')
  const [failed, setFailed] = useState('')
  const [, forceTick] = useState(0)

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

  const header = jsxs('div', {
    className: 'flex min-w-0 items-center gap-2',
    children: [
      item.storedId ? jsx(SessionStatusDot, { storedSessionId: item.storedId }) : jsx(Codicon, { name: 'comment', className: 'text-muted-foreground' }),
      jsx('span', { className: 'min-w-0 flex-1 truncate text-[0.82rem] font-semibold', children: item.title }),
      jsx(SourcePill, { label: item.sourceLabel }),
      jsx(AgoText, { ms: item.firstSeenAt }),
      jsx(Tip, {
        label: 'Snooze 15 min',
        children: jsx(Button, {
          size: 'icon-xs',
          variant: 'ghost',
          disabled: Boolean(busy),
          onClick: () => {
            snoozeRequest(item.requestId)
            forceTick(t => t + 1)
          },
          children: jsx(Codicon, { name: 'snooze' })
        })
      })
    ]
  })

  let body = null
  if (item.kind === 'approval') body = jsx(ApprovalBody, { item, busy, run, open })
  else if (item.kind === 'clarify') body = jsx(ClarifyBody, { item, busy, run, open })
  else body = jsx(GenericRequestBody, { item, busy, open })

  return jsxs('div', {
    className: 'rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) p-3 shadow-sm',
    children: [
      header,
      body,
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
            className:
              'mb-2 max-h-32 overflow-auto whitespace-pre-wrap break-all rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-primary) p-2 font-mono text-[0.72rem] leading-5 text-(--ui-text-primary)',
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
                variant: choice === 'deny' ? 'outline' : 'secondary',
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
  const open = async () => {
    setBusy(true)
    try {
      await openItemSession({ storedId: s.session_key, sessionId: s.id, route: row.route })
    } catch {} finally {
      setBusy(false)
    }
  }
  return jsxs('button', {
    type: 'button',
    onClick: open,
    className:
      'group flex w-full items-center gap-2.5 rounded-md border border-transparent px-2 py-1.5 text-left transition-colors hover:border-(--ui-stroke-secondary) hover:bg-(--ui-bg-secondary)',
    children: [
      s.session_key
        ? jsx(SessionStatusDot, { storedSessionId: s.session_key })
        : jsx('span', { className: 'size-1.5 rounded-full bg-emerald-500' }),
      jsxs('div', {
        className: 'min-w-0 flex-1',
        children: [
          jsx('div', { className: 'truncate text-[0.78rem] font-medium text-(--ui-text-primary)', children: s.title || 'Session' }),
          s.preview
            ? jsx('div', { className: 'truncate text-[0.68rem] text-muted-foreground', children: s.preview })
            : null
        ]
      }),
      jsx(SourcePill, { label: row.sourceLabel }),
      jsx(AgoText, { ms: epochMs(s.last_active) }),
      busy ? spinIcon('sync') : jsx(Codicon, { name: 'arrow-right', className: 'text-muted-foreground/40 group-hover:text-muted-foreground' })
    ]
  })
}

function FinishedRow({ f }) {
  const [busy, setBusy] = useState(false)
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
  return jsxs('div', {
    className: 'flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 hover:bg-(--ui-bg-secondary)',
    children: [
      jsx(Codicon, {
        name: f.kind === 'error' ? 'error' : 'pass-filled',
        className: cn('shrink-0 text-[0.8rem]', f.kind === 'error' ? 'text-destructive' : 'text-emerald-500')
      }),
      jsxs('div', {
        className: 'min-w-0 flex-1',
        children: [
          jsx('div', { className: 'truncate text-[0.78rem] font-medium text-(--ui-text-primary)', children: f.title || 'Session' }),
          jsx('div', {
            className: 'truncate text-[0.68rem] text-muted-foreground',
            children: f.kind === 'error' ? 'Ended with an error' : 'Turn finished'
          })
        ]
      }),
      f.profile ? jsx(SourcePill, { label: f.profile }) : null,
      jsx(AgoText, { ms: f.at }),
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
  })
}

function CronRow({ entry }) {
  const j = entry.job
  const overdueMs = nextRunOverdueMs(j)
  const nextAt = Date.parse(j.next_run_at || '')
  const failed = j.last_status === 'error' || j.last_status === 'failed' || Boolean(j.last_error)
  return jsxs('div', {
    className: 'flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 hover:bg-(--ui-bg-secondary)',
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

  return jsxs('div', {
    className:
      'flex items-center gap-2 rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) px-3 py-2',
    children: [
      jsx(Codicon, { name: 'sparkle', className: 'shrink-0 text-[0.85rem] text-primary/80' }),
      jsx(Input, {
        value: text,
        placeholder: 'Start something — “review my PRs”, “summarize overnight logs”…',
        disabled: busy,
        onChange: ev => setText(ev.target.value),
        onKeyDown: ev => {
          if (isSubmitEnter(ev)) submit()
        },
        className: 'h-7 flex-1 border-none bg-transparent px-0 text-[0.82rem] shadow-none focus-visible:ring-0'
      }),
      note
        ? jsx('span', { className: 'shrink-0 truncate text-[0.68rem] text-muted-foreground', children: note })
        : null,
      jsx(Button, {
        size: 'xs',
        variant: 'secondary',
        disabled: busy || !text.trim(),
        onClick: submit,
        children: busy ? spinIcon('sync') : 'Start'
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
    className:
      'flex items-center gap-2 rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-1.5 text-[0.72rem]',
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

function DayPage() {
  const scan = useQuery({ queryKey: SCAN_QK, queryFn: scanInbox, refetchInterval: 5000, staleTime: 1500, refetchOnWindowFocus: true })
  const cron = useQuery({ queryKey: CRON_QK, queryFn: scanCron, refetchInterval: 30000, staleTime: 10000, refetchOnWindowFocus: true })
  const finished = useValue($finished)

  const data = scan.data
  const needs = (data && data.needs) || []
  const flight = (data && data.flight) || []
  const waiting = (data && data.waiting) || []
  const sources = (data && data.sources) || []
  const jobs = (cron.data && cron.data.jobs) || []
  const hiddenCount = (data && data.hiddenCount) || 0
  const mutedMap = useValue($muted)
  const prefs = useValue($prefs)

  // group approvals by session for the resolve-all bar
  const approvalsBySession = {}
  for (const n of needs) {
    if (n.kind !== 'approval') continue
    ;(approvalsBySession[n.sessionId] = approvalsBySession[n.sessionId] || []).push(n)
  }
  const floodSessions = Object.entries(approvalsBySession).filter(([, items]) => items.length > 1)

  const dateStr = new Date().toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })

  const refresh = () => invalidate()

  const empty =
    data && needs.length === 0 && flight.length === 0 && waiting.length === 0 && finished.length === 0 && jobs.length === 0

  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col bg-(--ui-bg-primary) text-(--ui-text-primary)',
    children: [
      jsxs('header', {
        className: 'flex shrink-0 items-center justify-between gap-4 border-b border-(--ui-stroke-secondary) px-6 py-4',
        children: [
          jsxs('div', {
            className: 'flex min-w-0 items-baseline gap-3',
            children: [
              jsx('h1', { className: 'text-lg font-semibold tracking-tight', children: `${greeting()}` }),
              jsx('span', { className: 'truncate text-[0.75rem] text-muted-foreground', children: dateStr })
            ]
          }),
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
              flight.length ? jsx(Badge, { variant: 'muted', children: `${flight.length} in flight` }) : null,
              finished.length ? jsx(Badge, { variant: 'muted', children: `${finished.length} to review` }) : null,
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
        className: 'mx-auto w-full max-w-6xl shrink-0 px-6 pt-4',
        children: [jsx(QuickTaskBar, {})]
      }),
      jsx(ScrollArea, {
        className: 'min-h-0 flex-1',
        children: jsxs('div', {
          className: 'mx-auto grid w-full max-w-6xl grid-cols-1 gap-x-8 gap-y-6 px-6 py-5 xl:grid-cols-[minmax(0,1fr)_320px]',
          children: [
            jsxs('main', {
              className: 'flex min-w-0 flex-col gap-6',
              children: [
                scan.isLoading
                  ? jsxs('div', {
                      className: 'flex items-center gap-2 py-8 text-muted-foreground',
                      children: [jsx(Loader, {}), jsx('span', { className: 'text-[0.8rem]', children: 'Scanning every profile and connection…' })]
                    })
                  : null,
                scan.isError
                  ? jsx(ErrorState, { title: 'Scan failed', description: errMsg(scan.error) })
                  : null,
                needs.length
                  ? jsxs('section', {
                      children: [
                        jsx(SectionLabel, { icon: 'bell-dot', title: 'Needs you', count: needs.length, tone: 'text-amber-500' }),
                        floodSessions.length
                          ? jsx('div', {
                              className: 'mb-2 flex flex-col gap-1.5',
                              children: floodSessions.map(([sid, items]) =>
                                jsx(ApproveAllBar, { sessionId: sid, title: items[0].title, items }, `flood-${sid}`)
                              )
                            })
                          : null,
                        jsx('div', {
                          className: 'flex flex-col gap-2.5',
                          children: needs.map(item => jsx(NeedsYouCard, { item }, item.key))
                        }),
                        hiddenCount
                          ? jsxs('div', {
                              className: 'mt-2 px-1 text-[0.68rem] text-muted-foreground/60',
                              children: [`${hiddenCount} item${hiddenCount === 1 ? '' : 's'} snoozed or from muted sources`]
                            })
                          : null
                      ]
                    })
                  : hiddenCount
                    ? jsxs('div', {
                        className: 'px-1 text-[0.68rem] text-muted-foreground/60',
                        children: [`${hiddenCount} item${hiddenCount === 1 ? '' : 's'} snoozed or from muted sources`]
                      })
                    : null,
                waiting.length
                  ? jsxs('section', {
                      children: [
                        jsx(SectionLabel, { icon: 'watch', title: 'Waiting on input', count: waiting.length, tone: 'text-amber-500' }),
                        jsx('div', { className: 'flex flex-col', children: waiting.map(r => jsx(FlightRow, { row: r }, `${r.sourceKey}#${r.session.id}`)) })
                      ]
                    })
                  : null,
                flight.length
                  ? jsxs('section', {
                      children: [
                        jsx(SectionLabel, { icon: 'rocket', title: 'In flight', count: flight.length }),
                        jsx('div', { className: 'flex flex-col', children: flight.map(r => jsx(FlightRow, { row: r }, `${r.sourceKey}#${r.session.id}`)) })
                      ]
                    })
                  : null,
                finished.length
                  ? jsxs('section', {
                      children: [
                        jsx(SectionLabel, { icon: 'pass', title: 'Finished — review', count: finished.length }),
                        jsx('div', { className: 'flex flex-col', children: finished.map(f => jsx(FinishedRow, { f }, f.key)) })
                      ]
                    })
                  : null,
                empty
                  ? jsx(EmptyState, {
                      title: 'Nothing on the board',
                      description: 'No approvals waiting, nothing running, nothing scheduled. Hermes is idle — start something and manage it from here.'
                    })
                  : null
              ]
            }),
            jsxs('aside', {
              className: 'flex min-w-0 flex-col gap-6 xl:border-l xl:border-(--ui-stroke-secondary) xl:pl-8',
              children: [
                jsxs('section', {
                  children: [
                    jsx(SectionLabel, { icon: 'calendar', title: 'Scheduled', count: jobs.length }),
                    jobs.length
                      ? jsx('div', { className: 'flex flex-col', children: jobs.slice(0, 20).map(j2 => jsx(CronRow, { entry: j2 }, j2.key)) })
                      : jsx('div', { className: 'px-2 py-3 text-[0.72rem] text-muted-foreground', children: 'No scheduled jobs.' })
                  ]
                }),
                jsxs('section', {
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
                                label: muted ? 'Unmute — show its waiting items again' : 'Mute — hide its waiting items',
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
              ]
            })
          ]
        })
      })
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
      }
    ])
  }
}
