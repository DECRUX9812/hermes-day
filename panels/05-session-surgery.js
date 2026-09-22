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
