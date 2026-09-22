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
