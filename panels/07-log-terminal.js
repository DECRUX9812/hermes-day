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
