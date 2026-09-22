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
