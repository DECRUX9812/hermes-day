// Lane 06 — Git Forge panel (splice-ready snippet; NOT a module).
// No import/export: this file must parse as a plain script (`node --check`).
// Tabs: PRs | Issues | Diff | Release. Polling is throttled on purpose:
// staleTime 60s, no refetch interval, refetch only when the window regains
// focus — the panel never spawns `gh` in a loop. Every colour comes from the
// `C` token object or a theme var; there is not one literal colour here.

function hdayForgeEmpty(reason) {
  return {
    ok: true, empty: true, reason: reason || '', generated_at: 0,
    repo: null, root: null, default_branch: null, head: null,
    auth: { ok: false, login: null },
    prs: [], issues: [], checks: null, diff: null, runs: [],
    release: { tags: [], head: null, next_tag: 'v0.1.1', dirty: false,
               can_tag: false, can_push: false, last: null },
    actions: [], error: null, performed: null
  };
}

function hdayForgeParse(disp) {
  try {
    var out = '';
    if (typeof disp === 'string') out = disp;
    else if (disp) out = disp.output || disp.text || '';
    if (out && String(out).trim().charAt(0) === '{') return JSON.parse(out);
    return hdayForgeEmpty(out ? '' : 'no response from the agent half');
  } catch (e) {
    return hdayForgeEmpty(String(e && e.message ? e.message : e));
  }
}

function hdayForgeDispatch(route, arg) {
  try {
    var p = rpc(route, 'command.dispatch', { name: 'day-forge', arg: String(arg) });
    if (!p || typeof p.then !== 'function') {
      return Promise.resolve(hdayForgeEmpty('no response from the agent half'));
    }
    return p.then(function (d) { return hdayForgeParse(d); },
                  function (e) { return hdayForgeEmpty(String(e && e.message ? e.message : e)); });
  } catch (e) {
    return Promise.resolve(hdayForgeEmpty(String(e)));
  }
}

// UI state lives in atoms cached on a stable module-scope object, so renders
// never re-create them (this file may only declare top-level functions).
function hdayForgeAtoms() {
  function build() {
    return {
      tab: atom('prs'), sel: atom(null), mode: atom('none'),
      verb: atom('comment'), text: atom(''), arm: atom(null),
      tag: atom(''), busy: atom(null), strip: atom(null)
    };
  }
  try {
    if (typeof host !== 'undefined' && host && !host.__hdayForge06) host.__hdayForge06 = build();
    if (typeof host !== 'undefined' && host && host.__hdayForge06) return host.__hdayForge06;
  } catch (e) { /* fall through */ }
  try {
    if (typeof globalThis !== 'undefined' && !globalThis.__hdayForge06) globalThis.__hdayForge06 = build();
    if (typeof globalThis !== 'undefined' && globalThis.__hdayForge06) return globalThis.__hdayForge06;
  } catch (e) { /* fall through */ }
  return build();
}

function hdayForgeWhen(u) {
  try {
    var ms = Date.parse(u || '');
    return isFinite(ms) && ms ? relativeTime(ms) : '';
  } catch (e) { return ''; }
}

function hdayForgeStamp(generatedAt) {
  try {
    if (!generatedAt) return '';
    return hdayForgeWhen(new Date(generatedAt * 1000).toISOString());
  } catch (e) { return ''; }
}

function hdayForgeLines(text) {
  try { return String(text || '').split('\n').length; } catch (e) { return 0; }
}

// §3.1 check pill states: ✓ n/n · ✗ k/n · ◐ running · – no checks · … loading
function hdayForgePill(checks, loading) {
  if (loading) return { text: '…', color: C.faint, title: 'fetching checks' };
  if (!checks) return null;
  if (checks.state === 'PENDING') {
    return { text: '◐ ' + (checks.done || 0) + '/' + (checks.total || 0),
             color: C.amber, title: 'running' };
  }
  if (checks.state === 'FAILURE') {
    return { text: '✗ ' + (checks.failed || 0) + '/' + (checks.total || 0),
             color: C.red, title: 'failing' };
  }
  if (checks.state === 'SUCCESS') {
    return { text: '✓ ' + (checks.total || 0) + '/' + (checks.total || 0),
             color: C.emerald, title: 'all green' };
  }
  if (checks.state === 'NONE') return { text: '– no checks', color: C.faint, title: '' };
  return { text: '…', color: C.faint, title: '' };
}

function hdayForgePillView(pill) {
  if (!pill) return null;
  return jsx('span', {
    className: 'hday-chip tabular-nums',
    style: { color: pill.color },
    title: pill.title,
    children: pill.text
  });
}

function hdayForgeHead(props) {
  var data = props.data;
  var authed = Boolean(data.auth && data.auth.ok);
  var repoLine = data.repo
    ? data.repo + ' · ' + (data.head || '?') + (data.default_branch ? ' · ' + data.default_branch : '')
    : 'no origin remote — not a GitHub checkout';
  return jsxs('div', { className: 'hday-panel-head', children: [
    jsx(Badge, { tone: 'info', children: 'Git Forge' }),
    jsx('span', { className: 'hday-muted', style: { fontFamily: 'inherit' }, children: repoLine }),
    jsx(Badge, { tone: authed ? 'info' : 'warn',
                 children: authed ? ('gh: ' + data.auth.login) : 'gh: unauthenticated' })
  ] });
}

function hdayForgeTabs(props) {
  var tabs = [['prs', 'PRs'], ['issues', 'Issues'], ['diff', 'Diff'], ['release', 'Release']];
  return jsx('div', { className: 'hday-chips', children: tabs.map(function (t) {
    var on = props.tab === t[0];
    return jsx('button', {
      type: 'button',
      className: cn('hday-chip', on && 'is-active'),
      style: { color: on ? C.mono : C.faint,
               borderBottom: on ? '1px solid ' + C.mono : '1px solid transparent' },
      onClick: function () { props.onTab(t[0]); },
      children: t[1],
      key: t[0]
    });
  }) });
}

// §3.4 — the mandatory empty state, never an exception.
function hdayForgeEmptyState(props) {
  var data = props.data;
  var open = (data.prs || []).length + (data.issues || []).length;
  var tags = ((data.release && data.release.tags) || []).length;
  var reason = data.reason || (data.auth && data.auth.ok ? '' : 'gh not authenticated');
  return jsxs('div', { className: 'hday-empty', children: [
    jsxs('div', { children: [
      jsx('span', { style: { color: C.text, fontWeight: 600 },
                    children: open + ' OPEN WORK' }),
      jsx('span', { style: { color: C.faint }, children: ' · ' }),
      jsx('span', { style: { color: C.text, fontWeight: 600 },
                    children: tags + ' TAGS' })
    ] }),
    jsx('p', { style: { color: C.muted },
               children: 'No open pull requests. No open issues.' }),
    reason ? jsx('p', { style: { color: C.faint }, children: reason }) : null,
    jsxs('div', { className: 'hday-actions', children: [
      jsx(Button, { variant: 'secondary', onClick: props.onRescan,
                    children: 'Re-scan' }),
      jsx(Button, { variant: 'ghost', onClick: function () { props.onTab('release'); },
                    children: 'Release lane →' })
    ] }),
    jsxs(Tip, { children: [
      'Checks run only when a row is selected. Polling is throttled: 60s stale, ',
      'refetch on focus only — the panel does not spawn gh in a loop.'
    ] })
  ] });
}

function hdayForgePrTable(props) {
  var prs = props.prs;
  var sel = props.sel;
  return jsxs('div', { children: [
    jsxs('div', { className: 'hday-row', style: { color: C.faint },
                  children: [
                    jsx('span', { style: { width: '3rem' }, children: 'pr' }),
                    jsx('span', { style: { flex: 1 }, children: 'title' }),
                    jsx('span', { style: { width: '9rem' }, children: 'head' }),
                    jsx('span', { style: { width: '7rem' }, children: 'rev' }),
                    jsx('span', { style: { width: '6rem' }, children: 'upd' }),
                    jsx('span', { style: { width: '7rem' }, children: 'checks' })
                  ] }),
    prs.map(function (pr, i) {
      var on = sel === pr.number;
      var pill = on ? hdayForgePill(props.checks, props.checksLoading) : null;
      return jsx('div', {
        className: cn('hday-row', on && 'hday-sel'),
        onClick: function () { props.onSelect(pr.number); },
        children: [
          jsx('span', { style: { width: '3rem', color: C.mono },
                        children: '#' + pr.number }),
          jsx('span', { style: { flex: 1, color: C.text },
                        children: pr.title }),
          jsx('span', { style: { width: '9rem', color: C.slate },
                        children: pr.head || '' }),
          jsx('span', { style: { width: '7rem' },
                        children: jsx(Badge, {
                          tone: String(pr.review || '').toUpperCase() === 'APPROVED' ? 'info' : 'muted',
                          children: pr.review || pr.state || ''
                        }) }),
          jsx('span', { style: { width: '6rem', color: C.faint },
                        children: hdayForgeWhen(pr.updated) }),
          jsx('span', { style: { width: '7rem' }, children: hdayForgePillView(pill) })
        ],
        key: 'pr-' + (pr.number || i)
      });
    })
  ] });
}

function hdayForgeIssueTable(props) {
  var issues = props.issues;
  var sel = props.sel;
  if (!issues.length) {
    return jsx('p', { className: 'hday-empty', children: 'No open issues.' });
  }
  return jsx('div', { children: issues.map(function (it, i) {
    var on = sel === it.number;
    return jsx('div', {
      className: cn('hday-row', on && 'hday-sel'),
      onClick: function () { props.onSelect(it.number); },
      children: [
        jsx('span', { style: { width: '3rem', color: C.mono },
                      children: '#' + it.number }),
        jsx('span', { style: { flex: 1, color: C.text }, children: it.title }),
        jsx('span', { children: (it.labels || []).map(function (l, j) {
          return jsx(Badge, { tone: 'muted', children: l, key: 'l' + j });
        }) }),
        jsx('span', { style: { width: '6rem', color: C.faint },
                      children: hdayForgeWhen(it.updated) })
      ],
      key: 'is-' + (it.number || i)
    });
  }) });
}

function hdayForgeDiff(props) {
  var diff = props.diff;
  if (!props.prNumber) {
    return jsx('p', { className: 'hday-muted',
                      children: 'Select a PR first — press d with a row chosen.' });
  }
  if (props.loading) {
    return jsx('p', { className: 'hday-muted', children: 'fetching diff…' });
  }
  if (!diff || !diff.text) {
    return jsx('p', { className: 'hday-muted',
                      children: 'No diff returned for #' + props.prNumber +
                        (props.reason ? ' — ' + props.reason : '') });
  }
  return jsxs('div', { children: [
    jsxs('div', { className: 'hday-receipt-row', style: { color: C.faint },
                  children: [
                    jsx('span', { children: 'pr #' + diff.number + ' · ' +
                      hdayForgeLines(diff.text) + ' shown / ' + (diff.total || 0) + ' lines' }),
                    diff.truncated ? jsx('span', { children: 'head-capped at 400 lines' }) : null
                  ] }),
    jsx('pre', { className: 'hday-pre',
                 style: { color: C.text, whiteSpace: 'pre-wrap', maxHeight: '24rem',
                          overflow: 'auto' },
                 children: diff.text }),
    props.url ? jsx('div', { style: { color: C.faint, fontSize: '0.66rem',
                                      wordBreak: 'break-all' },
                             children: props.url }) : null
  ] });
}

function hdayForgeRelease(props) {
  var rel = props.release || {};
  var tags = rel.tags || [];
  var next = props.tagValue || rel.next_tag || 'v0.1.1';
  return jsxs('div', { className: 'hday-receipt', children: [
    jsxs('div', { className: 'hday-receipt-head', children: [
      'release lane',
      jsx(Badge, { tone: rel.dirty ? 'warn' : 'info',
                   children: rel.dirty ? 'dirty' : 'clean' })
    ] }),
    jsxs('div', { className: 'hday-receipt-row', children: [
      jsx('span', { children: 'head' }),
      jsx('span', { style: { color: C.mono }, children: rel.head || '?' })
    ] }),
    jsxs('div', { className: 'hday-receipt-row', children: [
      jsx('span', { children: 'tags' }),
      jsx('span', { style: { color: tags.length ? C.text : C.faint },
                    children: String(tags.length) + ' (' +
                      (tags.length ? tags[tags.length - 1] : 'none') + ')' })
    ] }),
    jsxs('div', { className: 'hday-receipt-row', children: [
      jsx('span', { children: 'next' }),
      jsx('span', { style: { color: C.emerald }, children: next })
    ] }),
    jsx('div', { className: 'hday-fields', children: jsx('label', { children: [
      jsx('span', { children: 'tag (semver, vMAJOR.MINOR.PATCH)' }),
      jsx('input', {
        type: 'text',
        value: next,
        onChange: function (e) { props.onTag(e.target.value); }
      })
    ] }) }),
    jsxs('div', { className: 'hday-actions', children: [
      jsx(Button, { variant: 'ghost', disabled: Boolean(props.busy) || !rel.can_tag,
                    onClick: function () { props.onRelease('local'); },
                    children: 'Tag' }),
      jsx(Button, { variant: 'secondary', disabled: Boolean(props.busy),
                    onClick: function () { props.onRelease('push'); },
                    children: 'Tag & Push' }),
      jsx(Button, { variant: 'primary', disabled: Boolean(props.busy),
                    onClick: function () { props.onRelease('gh'); },
                    children: 'Create GitHub Release' })
    ] }),
    jsxs('div', { className: 'hday-receipt-row', style: { color: C.faint },
                  children: [
                    jsx('span', { children: 'workflow runs' }),
                    jsx('span', { children: (props.runs || []).length
                      ? (props.runs[0].workflow || 'run') + ' · ' +
                        (props.runs[0].conclusion || props.runs[0].status || '?')
                      : '0' })
                  ] }),
    jsx('div', { children: jsx(Tip, { children:
      'A dirty worktree refuses to tag. Push is never forced: a rejected push is reported exactly as git said it.' }) })
  ] });
}

function hdayPanel06(props) {
  props = props || {};
  var route = props.route || null;
  var A = hdayForgeAtoms();

  var tab = useValue(A.tab);
  var sel = useValue(A.sel);
  var mode = useValue(A.mode);
  var verb = useValue(A.verb);
  var text = useValue(A.text);
  var arm = useValue(A.arm);
  var tagValue = useValue(A.tag);
  var busy = useValue(A.busy);
  var strip = useValue(A.strip);

  // Throttled list query: 60s stale, no interval, refetch on focus only.
  var forgeQ = useQuery({
    queryKey: ['hday-forge', 'list'],
    queryFn: function () { return hdayForgeDispatch(route, 'list'); },
    staleTime: 60000,
    refetchInterval: false,
    refetchOnWindowFocus: true,
    refetchOnMount: false,
    refetchOnReconnect: false,
    retry: false
  });

  var data = forgeQ.data || hdayForgeEmpty('');
  var prs = data.prs || [];
  var issues = data.issues || [];
  // checks/diff attach to a PR number only; an issue number never fetches them
  var prSel = null;
  for (var i = 0; i < prs.length; i++) {
    if (prs[i].number === sel) { prSel = sel; break; }
  }

  var checksQ = useQuery({
    queryKey: ['hday-forge', 'checks', prSel],
    queryFn: function () { return hdayForgeDispatch(route, 'checks ' + prSel); },
    enabled: Boolean(prSel) && (tab === 'prs' || tab === 'diff'),
    staleTime: 60000,
    refetchInterval: false,
    refetchOnWindowFocus: true,
    refetchOnMount: false,
    refetchOnReconnect: false,
    retry: false
  });

  var diffQ = useQuery({
    queryKey: ['hday-forge', 'diff', prSel],
    queryFn: function () { return hdayForgeDispatch(route, 'diff ' + prSel); },
    enabled: Boolean(prSel) && tab === 'diff',
    staleTime: 60000,
    refetchInterval: false,
    refetchOnWindowFocus: true,
    refetchOnMount: false,
    refetchOnReconnect: false,
    retry: false
  });

  function rescan() {
    try { if (forgeQ && forgeQ.refetch) forgeQ.refetch(); } catch (e) { /* ignore */ }
  }

  // one command dispatch + one strip line; never throws to the render path
  function act(arg, okLine) {
    A.busy.set(arg);
    return hdayForgeDispatch(route, arg).then(function (env) {
      A.busy.set(null);
      var line = okLine || 'ok';
      if (env.performed) {
        var p = env.performed;
        line = p.action + ' ' + (p.tag || p.target || '') +
          (p.pushed ? ' · pushed' : '') + (p.released ? ' · released' : '');
        if (p.error) line += ' · ' + p.error;
      } else if (env.error) {
        line = env.error;
      } else if (env.empty && env.reason) {
        line = env.reason;
      }
      A.strip.set({ kind: env.error || (env.performed && env.performed.error) || !env.ok ? 'err' : 'ok',
                    line: line });
      rescan();
      return env;
    });
  }

  function submit() {
    if (mode === 'comment') {
      if (!text || !String(text).trim()) return;
      var cArg = 'comment ' + prSel + ' ' + text;
      A.mode.set('none');
      A.text.set('');
      act(cArg, 'comment sent');
      return;
    }
    if (mode === 'review') {
      var rArg = 'review ' + prSel + ' ' + verb + (text && String(text).trim() ? ' ' + text : '');
      A.mode.set('none');
      A.text.set('');
      act(rArg, 'review posted');
      return;
    }
    if (tab === 'release') {
      var t = tagValue || (data.release && data.release.next_tag) || 'v0.1.1';
      act('release ' + t, 'tagged');
    }
  }

  function fireArmed() {
    if (!arm) return;
    var parts = String(arm).split(':');
    if (parts[0] === 'approve' && parts[1]) {
      var arg = 'review ' + parts[1] + ' approve' +
        (text && String(text).trim() ? ' ' + text : '');
      A.arm.set(null);
      A.text.set('');
      act(arg, 'approved');
    }
  }

  function move(delta) {
    var rows = tab === 'issues' ? issues : prs;
    if (!rows.length) return;
    var idx = -1;
    for (var i = 0; i < rows.length; i++) if (rows[i].number === sel) idx = i;
    var next = idx < 0 ? 0 : idx + delta;
    if (next < 0) next = 0;
    if (next > rows.length - 1) next = rows.length - 1;
    A.sel.set(rows[next].number);
  }

  function onKey(e) {
    try {
      var k = e && e.key;
      var tgt = e && e.target ? e.target : null;
      var tagName = tgt && tgt.tagName ? String(tgt.tagName).toUpperCase() : '';
      var typing = tagName === 'INPUT' || tagName === 'TEXTAREA';
      if (k === 'Escape') {
        A.mode.set('none'); A.text.set(''); A.arm.set(null);
        return;
      }
      if (k === 'Enter') {
        if (typing) { e.preventDefault(); submit(); return; }
        fireArmed();
        return;
      }
      if (typing) return;
      if (k === 'ArrowDown') { e.preventDefault(); move(1); return; }
      if (k === 'ArrowUp') { e.preventDefault(); move(-1); return; }
      var ch = k && k.length === 1 ? String(k).toLowerCase() : '';
      if (ch === 'd') {
        if (!prSel && prs.length) A.sel.set(prs[0].number);
        A.tab.set('diff');
      } else if (ch === 'c') {
        if (prSel) { A.mode.set('comment'); A.text.set(''); A.arm.set(null); }
      } else if (ch === 'r') {
        if (prSel) { A.mode.set('review'); A.verb.set('comment'); A.arm.set(null); }
      } else if (ch === 'a') {
        if (prSel) A.arm.set('approve:' + prSel);
      } else if (ch === 't') {
        A.tab.set('release');
      }
    } catch (err) { /* never wedge the panel on a key */ }
  }

  var emptyNow = Boolean(data.empty) || (prs.length + issues.length === 0);
  var checksData = checksQ.data || null;
  var diffData = diffQ.data && diffQ.data.diff ? diffQ.data.diff : null;

  var body;
  if (emptyNow && tab !== 'release') {
    body = jsx(hdayForgeEmptyState, {
      data: data, onRescan: rescan, onTab: function (t) { A.tab.set(t); }, key: 'empty'
    });
  } else if (tab === 'prs') {
    body = prs.length
      ? jsx(hdayForgePrTable, { prs: prs, sel: prSel, checks: checksData && checksData.checks,
                                checksLoading: Boolean(prSel) && checksQ.isLoading,
                                onSelect: function (n) { A.sel.set(n); A.mode.set('none'); A.arm.set(null); },
                                key: 'prs' })
      : jsx('p', { className: 'hday-empty', children: 'No open pull requests.' });
  } else if (tab === 'issues') {
    body = jsx(hdayForgeIssueTable, { issues: issues, sel: sel,
                                      onSelect: function (n) { A.sel.set(n); }, key: 'issues' });
  } else if (tab === 'diff') {
    body = jsx(hdayForgeDiff, { diff: diffData, prNumber: prSel,
                                loading: Boolean(prSel) && diffQ.isLoading,
                                reason: (diffQ.data && diffQ.data.reason) || '',
                                url: prSel && prs.length
                                  ? (function () {
                                      for (var i = 0; i < prs.length; i++) {
                                        if (prs[i].number === prSel) return prs[i].url;
                                      }
                                      return '';
                                    })()
                                  : '',
                                key: 'diff' });
  } else {
    body = jsx(hdayForgeRelease, {
      release: data.release, runs: data.runs, tagValue: tagValue, busy: busy,
      onTag: function (v) { A.tag.set(v); },
      onRelease: function (m) {
        var t = tagValue || (data.release && data.release.next_tag) || 'v0.1.1';
        var arg = 'release ' + t + (m === 'gh' ? ' gh ' + t : m === 'local' ? ' local' : '');
        act(arg, m === 'local' ? 'tagged' : 'tagged & pushed');
      },
      key: 'release'
    });
  }

  var composer = null;
  if (prSel && (mode === 'comment' || mode === 'review')) {
    composer = jsxs('div', { className: 'hday-actions', children: [
      jsx('input', {
        type: 'text',
        value: text,
        placeholder: mode === 'comment'
          ? ('comment on #' + prSel + ' — Enter sends, Esc closes')
          : ('review #' + prSel + ' (' + verb + ') — Enter posts'),
        onChange: function (e) { A.text.set(e.target.value); },
        key: 'input'
      }),
      mode === 'review' ? ['approve', 'request-changes', 'comment'].map(function (v) {
        return jsx(Button, {
          variant: v === verb ? 'primary' : 'ghost',
          onClick: function () { A.verb.set(v); },
          children: v,
          key: 'v-' + v
        });
      }) : null,
      jsx(Button, { variant: 'ghost', onClick: function () { A.mode.set('none'); A.text.set(''); },
                    children: 'Cancel', key: 'cancel' })
    ] });
  } else if (arm) {
    composer = jsx('div', { className: 'hday-actions', children: [
      jsx('span', { style: { color: C.amber },
                    children: 'armed: approve ' + String(arm).split(':')[1] +
                      ' — press Enter (Esc cancels)' })
    ] });
  }

  var stripView = null;
  if (strip) {
    stripView = jsx('div', {
      className: 'hday-receipt',
      style: { color: strip.kind === 'err' ? C.red : C.emerald },
      children: strip.line
    });
  }

  return jsx('div', {
    className: cn('hday-panel', 'hday-forge', props.className),
    tabIndex: 0,
    onKeyDown: onKey,
    children: jsxs('div', { children: [
      jsx(hdayForgeHead, { data: data, key: 'head' }),
      jsx(hdayForgeTabs, { tab: tab, onTab: function (t) {
        A.tab.set(t); A.mode.set('none'); A.arm.set(null);
      }, key: 'tabs' }),
      body,
      composer,
      stripView,
      jsx('div', { className: 'hday-row', style: { color: C.faint, fontSize: '0.62rem' },
                   children: busy ? ('running: ' + busy)
                     : 'keys: ↑/↓ select · c comment · r review · a approve (Enter fires) · d diff · t release · Esc closes' }),
      jsx('div', { style: { color: C.faint, fontSize: '0.6rem' },
                   children: 'generated ' + hdayForgeStamp(data.generated_at) })
    ] })
  });
}
