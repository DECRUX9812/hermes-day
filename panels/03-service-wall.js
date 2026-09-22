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
