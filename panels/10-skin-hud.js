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
// Every binding here is verified present in desktop/plugin.js. Two trace
// fields per row:
//   symbol  what the SOURCE column PRINTS — a stable name, because plugin.js
//           line numbers drift on every splice (the old 3036/3039/3043/3045/
//           3047/3049/911/3120 cites were stale post-splice and 911/3120
//           never pointed at the binding at all), and F1-F3's fixes to
//           plugin.js will shift them again.
//   line    a SNAPSHOT of the verified site, kept as build evidence only —
//           plugin.js is still being edited by its lane owner, so numbers
//           move (they already did: 9864 -> 9880 between two greps a minute
//           apart). Re-grep before trusting it. Last verified snapshot
//           (working tree, HEAD b63b256): Day keydown handler j/ArrowDown
//           9880, k/ArrowUp 9883, a 9887, d 9889, s 9891, o/Enter 9893
//           (route-gated to /day, meta/ctrl/alt abort, skipped while focus is
//           in an input; shiftKey is NOT filtered); keybind.open id 10359 with
//           defaults ['mod+shift+i'] 10366; click via FeedRow onSelect 1873
//           (wired to setSel at 9935).
// ---------------------------------------------------------------------------
function hdayKeymapRows() {
  return [
    { keys: ['j', '\u2193'], action: 'Next row', note: 'feed select + 1', gate: 'always', gateLabel: '—', symbol: 'DayPage keydown', line: 'plugin.js:9880' },
    { keys: ['k', '\u2191'], action: 'Previous row', note: 'feed select - 1', gate: 'always', gateLabel: '—', symbol: 'DayPage keydown', line: 'plugin.js:9883' },
    { keys: ['a'], action: 'Approve selected approval', note: 'deny stays on d', gate: 'approval', gateLabel: 'selected approval', symbol: 'DayPage keydown', line: 'plugin.js:9887' },
    { keys: ['d'], action: 'Deny selected approval', note: 'once-per-request', gate: 'approval', gateLabel: 'selected approval', symbol: 'DayPage keydown', line: 'plugin.js:9889' },
    { keys: ['s'], action: 'Snooze selected request', note: 'hides the card until later', gate: 'need', gateLabel: 'selected need', symbol: 'DayPage keydown', line: 'plugin.js:9891' },
    { keys: ['o', 'Enter'], action: 'Open selected session', note: 'mouse equivalent is the row click', gate: 'row', gateLabel: 'a row is selected', symbol: 'DayPage keydown', line: 'plugin.js:9893' },
    { keys: ['mod', 'shift', 'i'], action: 'Toggle the day panel', note: 'host chrome keybind', gate: 'global', gateLabel: 'global', symbol: 'keybind.open', line: 'plugin.js:10366' },
    { keys: ['click'], action: 'Select row', note: 'mouse equivalent of j/k', gate: 'row', gateLabel: 'a row is selected', symbol: 'FeedRow onClick', line: 'plugin.js:1873' }
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
  // GATEMARK: needs === null/undefined means the needs probe never ran.
  // That state renders IDLE (all blocks off, count '\u2014'), never a calm 0 —
  // a failed/absent probe must not read as "measured zero". A real number,
  // including a real 0, renders as measured.
  var known = typeof p.needs === 'number' && isFinite(p.needs);
  var raw = known ? p.needs : 0;
  var n = known && raw > 0 ? Math.floor(raw) : 0;
  var cap = 12;
  var blocks = known ? (n > cap ? cap : n) : 0;
  var kids = [];
  for (var i = 0; i < cap; i++) {
    kids.push(jsx('span', { className: 'hday-sig-block', 'data-on': i < blocks ? '1' : '0', key: 'b' + i }));
  }
  return jsxs(
    'div',
    {
      className: 'hday-hud-signal',
      'data-known': known ? '1' : '0',
      'aria-label': known
        ? n + ' need' + (n === 1 ? '' : 's') + ' waiting'
        : 'attention unknown — no scan yet',
      children: [
        jsxs(
          'div',
          {
            className: 'hday-sig-track',
            'data-hot': known && n > 0 ? '1' : '0',
            'data-calm': known && n === 0 ? '1' : '0',
            'data-idle': known ? '0' : '1',
            key: 'track'
          },
          kids
        ),
        known
          ? (n > 0 ? jsx('span', { className: 'hday-sig-count', key: 'count' }, String(n)) : null)
          : jsx('span', { className: 'hday-sig-count hday-sig-idle', key: 'count' }, '\u2014')
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
          jsx('span', { className: 'hday-keymap-src', key: 'src' }, (active ? 'ready · ' : 'idle · ') + (row.symbol || row.line))
        ]
      )
    );
  });
  kids.push(
    jsxs('div', { className: 'hday-keymap-foot', key: 'foot' }, [
      // Spec §2 requires all three footnotes in the panel:
      // (1) ignored while focus is in an input, (2) meta/ctrl/alt abort,
      // (3) the real quirk — shift is NOT filtered today (0 shiftKey hits).
      jsx(Tip, { key: 'tip' },
        'ignored while typing \u00b7 meta/ctrl/alt abort \u00b7 shift is not filtered today'),
      jsx('div', { className: 'hday-keymap-note', key: 'skins' },
        'skins recolor tokens, not layout.')
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
  // GATEMARK: a probe that was never run (prop absent AND absent from the scan
  // payload) is UNKNOWN — it renders an em-dash / idle state, never a
  // confident 0. An explicitly supplied 0 or an empty array IS a measurement
  // and prints 0 honestly. Each cell reports only what actually arrived.
  var scan = p.scan || p.data || null;
  function pick(prop, scanKey) {
    var v = p[prop];
    if ((v === undefined || v === null) && scan && scan[scanKey] !== undefined && scan[scanKey] !== null) {
      v = scan[scanKey];
    }
    return v;
  }
  function countOf(v) {
    if (Array.isArray(v)) return v.length;
    if (typeof v === 'number' && isFinite(v)) return v;
    return null; // absent probe -> unknown
  }
  function numOf(v) {
    return typeof v === 'number' && isFinite(v) ? v : null;
  }
  var needsLen = countOf(pick('needs', 'needs'));
  var flightLen = countOf(pick('flight', 'flight'));
  var waitingLen = countOf(pick('waiting', 'waiting'));
  var jobsLen = countOf(pick('jobs', 'jobs'));
  var hidden = numOf(pick('hiddenCount', 'hiddenCount'));
  var scannedAt = numOf(pick('scannedAt', 'scannedAt'));
  var age = scannedAt === null ? '\u2014' : hdayAgeLabel(scannedAt, now);
  var fresh = scannedAt !== null && scannedAt > 0 && now - (scannedAt < 1e12 ? scannedAt * 1000 : scannedAt) < 15000;
  var feedLength = typeof p.feedLength === 'number' ? p.feedLength : 0;
  var selectedEntry = p.selectedEntry || p.selEntry || null;

  // null = no probe -> em-dash; a real number (including 0) prints as measured
  function cell(v) {
    return v === null ? '\u2014' : v;
  }
  var lines = [
    ['NEEDS', cell(needsLen), needsLen === null ? 'faint' : needsLen > 0 ? 'danger' : 'ok'],
    ['HIDDEN', cell(hidden), hidden === null ? 'faint' : hidden > 0 ? 'warn' : 'faint'],
    ['FLIGHT', cell(flightLen), flightLen === null ? 'faint' : flightLen > 0 ? 'accent' : 'faint'],
    ['WAIT', cell(waitingLen), waitingLen === null ? 'faint' : waitingLen > 0 ? 'warn' : 'faint'],
    ['JOBS', cell(jobsLen), jobsLen === null ? 'faint' : jobsLen > 0 ? 'accent' : 'faint'],
    // scannedAt of 0 / null means no probe has ever run: faint em-dash, not a
    // stale-looking warn (a failed probe is never a measured value).
    ['SCAN', age, scannedAt !== null && scannedAt > 0 ? (fresh ? 'ok' : 'warn') : 'faint'],
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
      + 'animation:hday-hud-scan-sweep 7.5s linear infinite}',
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
    // un-probed signal state renders visibly idle instead of a calm full row
    '.hday-root .hday-hud-signal[data-known="0"] .hday-sig-track{opacity:.45}',
    '.hday-root .hday-sig-count.hday-sig-idle{color:var(--hday-ink-3,var(--ui-text-tertiary))}',
    '.hday-root .hday-sig-block{width:13px;height:4px;border-radius:2px;'
      + 'background:var(--hday-line,var(--ui-stroke-secondary));opacity:.5}',
    '.hday-root .hday-sig-block[data-on="1"]{opacity:1;background:var(--hday-danger,var(--ui-accent));'
      + 'box-shadow:0 0 5px var(--hday-glow,var(--ui-accent))}',
    '.hday-root .hday-sig-track[data-calm="1"] .hday-sig-block[data-on="1"]{background:var(--hday-ok,var(--ui-accent));'
      + 'box-shadow:none}',
    '.hday-root .hday-sig-track[data-hot="1"]{animation:hday-hud-sig-pulse 1.8s ease-in-out infinite}',
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
      + 'animation:hday-hud-cheat-in .16s ease-out both}',
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
    '@keyframes hday-hud-scan-sweep{0%{transform:translateY(-120%)}100%{transform:translateY(760%)}}',
    '@keyframes hday-hud-sig-pulse{0%,100%{opacity:1}50%{opacity:.55}}',
    '@keyframes hday-hud-cheat-in{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}',
    '@keyframes hday-hud-boot{0%{transform:translateY(-60%);opacity:0}30%{opacity:1}'
      + '100%{transform:translateY(320%);opacity:0}}',
    '@keyframes hday-boot-in{from{opacity:0;transform:translateY(7px)}to{opacity:1;transform:none}}',
    // LAST, before the closing: prefers-reduced-motion disables every animation.
    // Explicit selectors are repeated so each beats its own earlier rule by
    // order at equal specificity — an importance override flag is never used.
    '@media (prefers-reduced-motion:reduce){',
    '.hday-root *{animation-duration:.001s;animation-delay:0s;animation-iteration-count:1;'
      + 'transition-duration:.001s;transition-delay:0s}',
    // The universal rule above is (0,1,0) and loses to the core
    // '.hday-shield.hday-degraded' rule (0,2,0), so the degraded shield kept
    // pulsing under reduced motion. Repeat it scoped under .hday-root: equal
    // specificity (0,3,0) + later source order wins — no importance flag.
    '.hday-root .hday-shield.hday-degraded{animation:none}',
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
