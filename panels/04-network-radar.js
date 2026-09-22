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
