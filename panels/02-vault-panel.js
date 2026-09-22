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
