/* ==========================================================
   Fluxito — Audit runs (beta redesign)
   Serves /audits (Runs tab) and /audits/run/{id}:
     - "Run audit" / "Ask AI to fix" → Ask-your-AI prompt drawer
       (audits run from the user's AI client over MCP; Fluxito
       can't start one server-side)
     - finding rows → drawer with expected vs actual, rule id,
       how to fix, a copyable fix prompt and triage actions
       (Mark resolved / Snooze 7d · 30d · until next run / Reopen →
       POST /api/audits/findings/{id}/{resolve|snooze|reopen})
     - run detail: resolved & snoozed findings hidden behind a
       "Show resolved & snoozed (N)" toggle; counts are open-only
     - clickable past-run rows
     - run detail: severity / platform filters, CSV / JSON export
   Uses the shared Fluxito.ui drawer + [data-copy] helpers (app.js).
   ========================================================== */
(function () {
  'use strict';

  function esc(s) {
    return (s == null ? '' : String(s))
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function toast(msg, kind) {
    if (window.Fluxito && window.Fluxito.toast) window.Fluxito.toast(msg, kind);
  }
  function drawer(html) {
    if (window.Fluxito && window.Fluxito.ui) return window.Fluxito.ui.drawer(html);
    return null;
  }
  function sevCls(sev) {
    return sev === 'critical' ? 'bad' : sev === 'warning' ? 'warn' : 'info';
  }
  var PLAT = { ga4: 'GA4', gtm: 'GTM', ua: 'UA', meta: 'Meta', meta_pixel: 'Meta Pixel', meta_capi: 'Meta CAPI', google_ads: 'Google Ads', tiktok: 'TikTok', tiktok_pixel: 'TikTok Pixel', linkedin: 'LinkedIn', linkedin_insight: 'LinkedIn Insight', x_pixel: 'X Pixel', bing_uet: 'Bing UET', microsoft_uet: 'Microsoft UET', sdr: 'SDR', seo: 'SEO' };
  function titleCase(s) {
    if (PLAT[s]) return PLAT[s];
    return String(s || '').replace(/_/g, ' ').replace(/\b\w/g, function (c) { return c.toUpperCase(); });
  }
  function pretty(v) {
    if (v == null) return '—';
    if (typeof v === 'string') return v;
    try { return JSON.stringify(v, null, 2); } catch (e) { return String(v); }
  }
  function closeBtn() {
    return '<button type="button" class="ui-btn is-quiet au-x" data-ui-close aria-label="Close">✕</button>';
  }

  var PAGE = window.__AUDIT_PAGE__ || {};
  var RUN = window.__AUDIT_RUN__ || null;
  var CAN_TRIAGE = !!((RUN && RUN.canTriage) || (!RUN && PAGE.canTriage));

  function fmtDay(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: d.getFullYear() === new Date().getFullYear() ? undefined : 'numeric' });
  }
  function triageStatus(f) { return (f && !f.passed && f.triage_status) || 'open'; }
  function triageLabel(f) {
    var t = f.triage || {};
    var st = triageStatus(f);
    if (st === 'resolved') return 'Resolved' + (t.resolved_at ? ' on ' + fmtDay(t.resolved_at) : '');
    if (st === 'snoozed') {
      return t.snooze_until_next_run ? 'Snoozed until the next run' : 'Snoozed until ' + fmtDay(t.snoozed_until);
    }
    return '';
  }

  /* ── Ask-your-AI drawer ─────────────────────────────── */
  function openAsk(kind) {
    var prompts = (PAGE.prompts && PAGE.prompts[kind]) || (PAGE.prompts && PAGE.prompts.run) || [];
    var title = kind === 'fix' ? 'Ask your AI to fix' : 'Run an audit from your AI';
    var html = '<div class="ui-drawer-h"><h3>' + esc(title) + '</h3>' + closeBtn() + '</div>' +
      '<div class="ui-drawer-b">' +
        '<p class="au-drawer-lead">Fluxito doesn’t run audits on its own — your AI does the work over MCP, and every audit it runs is saved here with its findings. Copy a prompt into your client.</p>' +
        '<div class="au-ask-list">' + prompts.map(function (p) {
          return '<div class="ui-say"><q>' + esc(p) + '</q>' +
            '<button type="button" class="ui-btn is-quiet" data-copy="' + esc(p) + '" data-copy-label="Prompt copied">Copy</button></div>';
        }).join('') + '</div>' +
        '<div class="ui-label">Open your client</div>' +
        '<a class="ui-btn" href="/ai">Add Fluxito to your AI client →</a>' +
      '</div>';
    drawer(html);
  }
  document.querySelectorAll('[data-audit-ask]').forEach(function (btn) {
    btn.addEventListener('click', function () { openAsk(btn.getAttribute('data-audit-ask')); });
  });

  /* ── Finding drawer ─────────────────────────────────── */
  function fixPrompt(f) {
    var trim = function (t) { return String(t || '').replace(/[.\s]+$/, ''); };
    return 'Fix this ' + (f.platform ? titleCase(f.platform) + ' ' : '') + 'finding' +
      (f.rule_id ? ' (' + f.rule_id + ')' : '') + ': ' + trim(f.message) +
      (f.remediation ? '. Suggested fix: ' + trim(f.remediation) : '') +
      '. Draft the change in a new workspace and show me before publishing.';
  }
  /* ── Triage (resolve / snooze / reopen) ──────────────── */
  var SNOOZES = [['7d', '7 days'], ['30d', '30 days'], ['next_run', 'Until next run']];
  function triageBlock(f) {
    if (f.passed || !f.id) return '';
    var st = triageStatus(f);
    var t = f.triage || {};
    if (st !== 'open') {
      return '<div class="ui-label">Status</div>' +
        '<div class="au-triage is-' + esc(st) + '">' +
          '<div class="au-triage-state"><span class="ui-chip no-dot au-tri-chip">' + esc(st) + '</span><b>' + esc(triageLabel(f)) + '</b></div>' +
          (t.note ? '<p class="au-triage-memo">' + esc(t.note) + '</p>' : '') +
          '<p class="ui-muted au-triage-help">Hidden from open counts' + (st === 'resolved' ? ' in this and later runs until reopened.' : ' until the snooze ends.') + '</p>' +
          (CAN_TRIAGE ? '<button type="button" class="ui-btn" data-triage-act="reopen">Reopen</button>' : '') +
        '</div>';
    }
    if (!CAN_TRIAGE) return '';
    return '<div class="ui-label">Triage</div>' +
      '<div class="au-triage">' +
        '<label class="ui-field au-triage-field"><span>Note <small class="ui-muted">(optional)</small></span>' +
          '<textarea class="ui-input au-triage-input" data-triage-note rows="2" maxlength="2000" placeholder="Why is this resolved or snoozed?"></textarea></label>' +
        '<div class="au-triage-acts">' +
          '<button type="button" class="ui-btn is-primary" data-triage-act="resolve">Mark resolved</button>' +
          '<span class="au-snooze-l">Snooze</span>' +
          '<div class="ui-seg au-snooze" role="group" aria-label="Snooze for">' +
            SNOOZES.map(function (o) { return '<button type="button" data-triage-act="snooze" data-snooze="' + o[0] + '">' + esc(o[1]) + '</button>'; }).join('') +
          '</div>' +
        '</div>' +
        '<p class="ui-muted au-triage-help">Resolved and snoozed findings drop out of the open counts and stay hidden in later runs; the recorded score doesn’t change.</p>' +
      '</div>';
  }

  function postTriage(f, action, snooze, note) {
    var body = {};
    if (snooze) body.snooze = snooze;
    if (note) body.note = note;
    return fetch('/api/audits/findings/' + encodeURIComponent(f.id) + '/' + action, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (!r.ok || d.error) throw new Error(d.message || d.detail || ('HTTP ' + r.status));
        return d;
      });
    });
  }

  function wireTriage(el, f, runId, onChange) {
    if (!el) return;
    el.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-triage-act]');
      if (!btn || btn.disabled) return;
      var action = btn.getAttribute('data-triage-act');
      var snooze = btn.getAttribute('data-snooze');
      var noteEl = el.querySelector('[data-triage-note]');
      var note = noteEl ? noteEl.value.trim() : '';
      var before = triageStatus(f);
      el.querySelectorAll('[data-triage-act]').forEach(function (b) { b.disabled = true; });
      postTriage(f, action, snooze, note).then(function (d) {
        f.triage_status = d.triage_status;
        f.triage = d.triage;
        toast(action === 'reopen' ? 'Finding reopened' : triageLabel(f));
        if (onChange) onChange(f, before);
        openFinding(f, runId, onChange);
      }).catch(function (err) {
        toast('Couldn’t update finding: ' + err.message, 'error');
        el.querySelectorAll('[data-triage-act]').forEach(function (b) { b.disabled = false; });
      });
    });
  }

  function openFinding(f, runId, onChange) {
    if (!f) return;
    var sev = f.passed ? 'pass' : (f.severity || 'info');
    var chip = f.passed
      ? '<span class="ui-chip no-dot is-good">pass</span>'
      : '<span class="ui-chip no-dot is-' + sevCls(sev) + '">' + esc(sev) + '</span>';
    var meta = [f.rule_id, f.platform ? titleCase(f.platform) + (f.source === 'custom' ? ' · custom rule' : ' rule book') : null, f.event]
      .filter(Boolean).map(esc).join(' · ');
    var hasDiff = f.expected != null || f.actual != null;
    var html = '<div class="ui-drawer-h">' + chip + '<h3>' + esc(f.message || f.rule_id || 'Finding') + '</h3>' + closeBtn() + '</div>' +
      '<div class="ui-drawer-b">' +
        (meta ? '<div class="ui-mono ui-muted au-drawer-meta">' + meta + '</div>' : '') +
        (hasDiff
          ? '<div class="ui-label">Expected vs actual</div><div class="au-diff">' +
              '<div class="au-diff-exp"><small>EXPECTED</small><pre>' + esc(pretty(f.expected)) + '</pre></div>' +
              '<div class="au-diff-act"><small>ACTUAL</small><pre>' + esc(pretty(f.actual)) + '</pre></div>' +
            '</div>'
          : '') +
        (f.passed ? '' :
          '<div class="ui-label">How to fix</div>' +
          '<p class="au-drawer-p">' + (f.remediation ? esc(f.remediation) : '<span class="ui-muted">No remediation recorded for this rule — ask your AI to investigate.</span>') + '</p>' +
          '<div class="ui-say"><div class="ui-say-l">Copy fix prompt for your AI</div><q>' + esc(fixPrompt(f)) + '</q>' +
            '<button type="button" class="ui-btn is-quiet" data-copy="' + esc(fixPrompt(f)) + '" data-copy-label="Fix prompt copied">Copy</button></div>') +
        triageBlock(f) +
      '</div>' +
      (runId ? '<div class="ui-drawer-f"><a class="ui-btn" href="/audits/run/' + esc(runId) + '">Open full report →</a></div>' : '');
    wireTriage(drawer(html), f, runId, onChange);
  }

  function wireFindings(root, list, runId, onChange) {
    if (!root) return;
    function fire(row) {
      var idx = parseInt(row.getAttribute('data-finding-idx'), 10);
      if (!isNaN(idx)) openFinding(list[idx], runId, onChange);
    }
    root.addEventListener('click', function (e) {
      if (e.target.closest('button, a')) return;
      var row = e.target.closest('[data-finding-idx]');
      if (row) fire(row);
    });
    root.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      var row = e.target.closest('[data-finding-idx]');
      if (row && row === e.target) { e.preventDefault(); fire(row); }
    });
  }

  function setText(id, v) { var el = document.getElementById(id); if (el) el.textContent = String(v); }
  function setIssues(n) {
    setText('auIssuesN', n);
    setText('auIssuesWord', n === 1 ? 'issue' : 'issues');
  }
  function sevKey(f) { return f.severity === 'critical' || f.severity === 'warning' ? f.severity : 'info'; }

  // Runs page: latest-run top findings. Triage re-renders the top three from
  // the open list and nudges the latest-run counts.
  var topList = document.getElementById('openFindingsList');
  if (topList && !RUN) {
    var pageFindings = PAGE.findings || [];
    var chipId = { critical: 'auChipCritical', warning: 'auChipWarning', info: 'auChipInfo' };
    var num = function (id) { var el = document.getElementById(id); return el ? parseInt(el.textContent, 10) || 0 : 0; };
    var renderTop = function () {
      var open = [];
      pageFindings.forEach(function (f, i) { if (triageStatus(f) === 'open') open.push([f, i]); });
      if (!open.length) {
        topList.innerHTML = '<div class="ui-finding"><span class="ui-sev is-good"></span><div class="ui-grow"><b>No open findings on the latest run.</b><p>Everything left is resolved or snoozed.</p></div></div>';
        return;
      }
      topList.innerHTML = open.slice(0, 3).map(function (p) {
        var f = p[0];
        var sub = [f.platform ? titleCase(f.platform) : null, f.event, f.remediation].filter(Boolean).join(' · ');
        if (sub.length > 140) sub = sub.slice(0, 137) + '...';
        return '<div class="ui-finding is-click" data-severity="' + esc(f.severity || 'info') + '" data-finding-idx="' + p[1] + '" role="button" tabindex="0">' +
          '<span class="ui-sev is-' + sevCls(f.severity) + '"></span>' +
          '<div class="ui-grow"><b>' + esc(f.message || f.rule_id || 'Finding') + '</b><p>' + esc(sub) + '</p></div>' +
          '<span class="ui-chip no-dot is-' + sevCls(f.severity) + '">' + esc(f.severity || 'info') + '</span></div>';
      }).join('');
    };
    var onTopChange = function (f, before) {
      var after = triageStatus(f);
      var delta = before === 'open' && after !== 'open' ? -1 : (before !== 'open' && after === 'open' ? 1 : 0);
      if (delta) {
        setText(chipId[sevKey(f)], Math.max(0, num(chipId[sevKey(f)]) + delta));
        setIssues(Math.max(0, num('auIssuesN') + delta));
        var hidden = Math.max(0, num('auTriageNoteN') - delta);
        setText('auTriageNoteN', hidden);
        var note = document.getElementById('auTriageNote');
        if (note) note.hidden = !hidden;
      }
      renderTop();
    };
    wireFindings(topList, pageFindings, PAGE.latestRunId, onTopChange);
  }

  // Past-run rows are clickable
  document.querySelectorAll('tr.is-click[data-href]').forEach(function (tr) {
    tr.addEventListener('click', function (e) {
      if (e.target.closest('a, button')) return;
      window.location.href = tr.getAttribute('data-href');
    });
  });

  /* ── Run detail ─────────────────────────────────────── */
  var findingsList = document.getElementById('findingsList');
  if (findingsList && RUN) {
    var rows = Array.prototype.slice.call(findingsList.querySelectorAll('.finding-row'));
    var chips = document.querySelectorAll('#findingFilters .ff-chip:not(.ff-chip--plat)');
    var platChips = document.querySelectorAll('#findingFilters .ff-chip--plat');
    var activeFilter = 'all';
    var activePlatform = null;
    var showTriaged = false;
    var emptyHint = document.getElementById('findingsFilteredEmpty');
    var triToggle = document.getElementById('auTriagedToggle');

    var apply = function () {
      var shown = 0;
      rows.forEach(function (row) {
        var sev = row.getAttribute('data-severity');
        var plat = row.getAttribute('data-platform');
        var passed = row.getAttribute('data-passed') === 'true';
        var triOk = showTriaged || (row.getAttribute('data-triage') || 'open') === 'open';
        var sevOk = activeFilter === 'all' ||
          (activeFilter === 'pass' && passed) ||
          (!passed && activeFilter === sev);
        var platOk = !activePlatform || plat === activePlatform;
        var ok = triOk && sevOk && platOk;
        row.style.display = ok ? '' : 'none';
        if (ok) shown++;
      });
      if (emptyHint) emptyHint.hidden = !(rows.length && !shown);
    };

    // Open-only counts, recomputed from the run's finding list after triage.
    var recount = function () {
      var c = { critical: 0, warning: 0, info: 0, pass: 0, triaged: 0 };
      (RUN.findings || []).forEach(function (f) {
        if (f.passed) { c.pass++; return; }
        if (triageStatus(f) !== 'open') { c.triaged++; return; }
        c[sevKey(f)]++;
      });
      var issues = c.critical + c.warning + c.info;
      setText('countAll', issues + c.pass);
      setText('countCritical', c.critical); setText('countWarning', c.warning); setText('countInfo', c.info);
      setText('auChipCritical', c.critical); setText('auChipWarning', c.warning); setText('auChipInfo', c.info);
      setIssues(issues);
      setText('auTriagedN', c.triaged);
      setText('auTriageNoteN', c.triaged);
      var note = document.getElementById('auTriageNote');
      if (note) note.hidden = !c.triaged;
      if (triToggle) {
        if (!c.triaged && !showTriaged) triToggle.hidden = true;
        else if (c.triaged) triToggle.hidden = false;
      }
    };

    var onRowChange = function (f) {
      var idx = (RUN.findings || []).indexOf(f);
      var row = idx >= 0 ? findingsList.querySelector('[data-finding-idx="' + idx + '"]') : null;
      if (row) {
        var st = triageStatus(f);
        row.setAttribute('data-triage', st);
        row.classList.toggle('is-triaged', st !== 'open');
        var side = row.querySelector('.au-f-side');
        var chip = row.querySelector('[data-tri-chip]');
        if (st === 'open') {
          if (chip) chip.remove();
        } else if (side) {
          if (!chip) {
            chip = document.createElement('span');
            chip.className = 'ui-chip no-dot au-tri-chip';
            chip.setAttribute('data-tri-chip', '');
            side.insertBefore(chip, side.firstChild);
          }
          chip.textContent = st;
        }
        var copy = row.querySelector('.au-f-copy');
        if (copy) copy.hidden = st !== 'open';  // .au-f-copy[hidden] is display:none (auditing.css)
      }
      recount();
      apply();
    };

    wireFindings(findingsList, RUN.findings || [], null, onRowChange);

    if (triToggle) {
      triToggle.addEventListener('click', function () {
        showTriaged = !showTriaged;
        triToggle.setAttribute('aria-pressed', showTriaged ? 'true' : 'false');
        triToggle.classList.toggle('is-on', showTriaged);
        var n = document.getElementById('auTriagedN');
        triToggle.innerHTML = (showTriaged ? 'Hide resolved &amp; snoozed (' : 'Show resolved &amp; snoozed (') +
          '<span id="auTriagedN">' + esc(n ? n.textContent : '0') + '</span>)';
        recount();
        apply();
      });
    }

    chips.forEach(function (c) {
      c.addEventListener('click', function () {
        chips.forEach(function (x) { x.classList.remove('is-active'); });
        c.classList.add('is-active');
        activeFilter = c.getAttribute('data-filter');
        apply();
      });
    });
    platChips.forEach(function (c) {
      c.addEventListener('click', function () {
        var p = c.getAttribute('data-filter-platform');
        platChips.forEach(function (x) { x.classList.remove('is-active'); });
        if (activePlatform === p) {
          activePlatform = null;
        } else {
          activePlatform = p;
          c.classList.add('is-active');
        }
        apply();
      });
    });

    var exportRun = function (fmt, btn) {
      var runId = btn ? btn.getAttribute('data-run-id') : '';
      if (!runId) return;
      btn.disabled = true;
      fetch('/api/audits/' + runId + '/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ format: fmt })
      }).then(function (r) {
        if (!r.ok) throw new Error('Export failed');
        return r.blob();
      }).then(function (blob) {
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url;
        a.download = 'audit_' + runId.slice(0, 8) + '.' + fmt;
        document.body.appendChild(a);
        a.click();
        URL.revokeObjectURL(url);
        a.remove();
        toast('Exported audit.' + fmt);
      }).catch(function (e) {
        toast('Export failed: ' + e.message, 'error');
      }).finally(function () { btn.disabled = false; });
    };
    var csvBtn = document.getElementById('exportCsvBtn');
    var jsonBtn = document.getElementById('exportJsonBtn');
    if (csvBtn) csvBtn.addEventListener('click', function () { exportRun('csv', csvBtn); });
    if (jsonBtn) jsonBtn.addEventListener('click', function () { exportRun('json', jsonBtn); });
  }
})();
