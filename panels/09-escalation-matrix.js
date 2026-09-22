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
