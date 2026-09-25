/* ============================================================
   Automated Test Flows — list page + vendors registry editor.
   One file serves both /audits/flows and /audits/vendors; each
   half no-ops when its root element is absent.
   ============================================================ */
(function () {
  'use strict';

  function toast(msg, kind) {
    if (window.Fluxito && window.Fluxito.toast) window.Fluxito.toast(msg, kind);
  }
  function esc(s) {
    return (s == null ? '' : String(s))
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function sevChip(sev) {
    var s = sev || 'info';
    var cls = s === 'critical' ? 'is-bad' : s === 'warning' ? 'is-warn' : 'is-info';
    return '<span class="ui-chip no-dot ' + cls + '">' + esc(s) + '</span>';
  }
  function fmtDateTime(iso) {
    if (!iso) return '—';
    var d = new Date(iso);
    if (isNaN(d)) return '—';
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) +
      ', ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  }
  async function jfetch(url, opts) {
    var res = await fetch(url, opts);
    var data = null;
    try { data = await res.json(); } catch (e) { /* ignore */ }
    if (!res.ok) {
      var msg = (data && (data.message || data.detail)) || ('Request failed (' + res.status + ')');
      throw new Error(msg);
    }
    return data;
  }

  /* ==========================================================
     FLOWS LIST
     ========================================================== */
  function initFlows() {
    var mount = document.getElementById('afFlowsMount');
    if (!mount) return;

    var FLOWS = (window.__AUDIT_FLOWS__ || []).slice();
    var byId = {};
    FLOWS.forEach(function (f) { byId[f.id] = f; });

    var search = '';
    var statusFilter = 'all';

    var DEVICE_ICON = {
      desktop: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/></svg>',
      mobile_web: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="2" width="14" height="20" rx="2"/><path d="M12 18h.01"/></svg>'
    };

    var STATUS_CLS = { passing: 'is-good', failing: 'is-bad', error: 'is-warn', running: 'is-accent', never_run: '' };
    var DEVICE_LABEL = { desktop: 'Desktop', mobile_web: 'Mobile web' };

    function statusPill(status) {
      var s = status || 'never_run';
      var label = s === 'never_run' ? 'never run' : s;
      return '<span class="ui-chip af-pill af-pill--' + esc(s) + ' ' + (STATUS_CLS[s] || '') + '">' + esc(label) + '</span>';
    }

    function checksText(f) {
      var lr = f.latest_run;
      if (!lr || lr.assertions_total == null || !lr.assertions_total) return '';
      var n = lr.assertions_passed || 0, m = lr.assertions_total || 0;
      var cls = n === m ? 'af-assert--pass' : 'af-assert--fail';
      return ' <span class="af-assert ' + cls + '">· ' + n + '/' + m + ' checks</span>';
    }

    function lastRunCell(f) {
      var lr = f.latest_run;
      var when = (lr && lr.started_at) || f.last_run_at;
      if (!when) return '<span class="af-dim">never</span>';
      return esc(fmtDateTime(when)) + checksText(f);
    }

    function scheduleCell(f) {
      if (!f.schedule_cron) return '<span class="af-dim">manual</span>';
      return '<span class="af-cron">' + esc(f.schedule_cron) + '</span>' +
        (f.enabled && f.next_run_at ? '<div class="af-sub">next ' + esc(fmtDateTime(f.next_run_at)) + '</div>' : '');
    }

    function groupsCell(f) {
      var g = f.groups || [];
      if (!g.length) return '';
      return '<span class="af-chip-row">' + g.map(function (x) {
        return '<span class="af-chip">' + esc(x) + '</span>';
      }).join('') + '</span>';
    }

    function latestRunLink(f) {
      var lr = f.latest_run;
      if (!lr || !lr.id) return '';
      return '<a class="ui-btn is-quiet af-view" href="/audits/flows/' + esc(f.id) + '/runs/' + esc(lr.id) + '">View</a>';
    }

    function matchesFilter(f) {
      if (statusFilter !== 'all' && (f.last_status || 'never_run') !== statusFilter) return false;
      if (search) {
        var hay = ((f.name || '') + ' ' + (f.description || '') + ' ' + (f.base_url || '') + ' ' + (f.groups || []).join(' ')).toLowerCase();
        if (hay.indexOf(search) === -1) return false;
      }
      return true;
    }

    function render() {
      if (!FLOWS.length) {
        var tpl = document.getElementById('afEmptyTpl');
        mount.innerHTML = '';
        if (tpl) mount.appendChild(tpl.content.cloneNode(true));
        return;
      }
      var rows = FLOWS.filter(matchesFilter);
      if (!rows.length) {
        mount.innerHTML = '<div class="ui-empty">No flows match this filter.</div>';
        return;
      }
      var html = '<div class="af-table-wrap"><table class="ui-table af-table"><thead><tr>' +
        '<th>Flow</th><th class="ui-hide-sm">Device</th><th class="ui-hide-sm">Steps</th><th class="ui-hide-sm">Schedule</th>' +
        '<th class="ui-hide-sm">Last run</th><th>Status</th><th class="ui-hide-sm">Enabled</th><th></th>' +
        '</tr></thead><tbody>';
      rows.forEach(function (f) {
        var steps = (f.steps || []).length;
        html += '<tr data-fid="' + esc(f.id) + '">' +
          '<td class="af-flow-cell"><a class="af-flow-name" href="/audits/flows/' + esc(f.id) + '">' + esc(f.name) + '</a>' +
            '<div class="af-flow-url">' + esc(f.base_url || '') + '</div>' +
            (f.description ? '<div class="af-flow-desc">' + esc(f.description) + '</div>' : '') +
            groupsCell(f) + '</td>' +
          '<td class="ui-hide-sm"><span class="af-device" title="' + esc(f.device) + '">' + (DEVICE_ICON[f.device] || '') + esc(DEVICE_LABEL[f.device] || f.device || '') + '</span></td>' +
          '<td class="ui-hide-sm">' + steps + '</td>' +
          '<td class="ui-hide-sm af-sched-cell">' + scheduleCell(f) + '</td>' +
          '<td class="ui-hide-sm af-last-cell">' + lastRunCell(f) + '</td>' +
          '<td class="af-status-cell">' + statusPill(f.last_status) + '</td>' +
          '<td class="ui-hide-sm"><label class="af-switch" title="Enable scheduled runs"><input type="checkbox" class="af-toggle"' + (f.enabled ? ' checked' : '') + ' aria-label="Enabled"><span class="af-slider"></span></label></td>' +
          '<td class="af-actions-cell"><span class="af-row-actions">' +
            '<button type="button" class="ui-btn af-run">Run now</button>' +
            latestRunLink(f) +
          '</span></td>' +
        '</tr>';
      });
      html += '</tbody></table></div>';
      mount.innerHTML = html;
    }

    function updateRow(f) {
      var tr = mount.querySelector('tr[data-fid="' + f.id + '"]');
      if (!tr) { render(); return; }
      tr.querySelector('.af-status-cell').innerHTML = statusPill(f.last_status);
      tr.querySelector('.af-last-cell').innerHTML = lastRunCell(f);
      var actions = tr.querySelector('.af-row-actions');
      var runBtn = actions.querySelector('.af-run');
      // Rebuild the latest-run link (id may have changed).
      var existingLink = actions.querySelector('a.af-view');
      if (existingLink) existingLink.remove();
      if (f.latest_run && f.latest_run.id) {
        actions.insertAdjacentHTML('beforeend', latestRunLink(f));
      }
      runBtn.disabled = false;
    }

    // ── Toggle enable/disable ──
    mount.addEventListener('change', function (ev) {
      var input = ev.target.closest('.af-toggle');
      if (!input) return;
      var tr = input.closest('tr');
      var fid = tr.getAttribute('data-fid');
      var enabled = input.checked;
      input.disabled = true;
      jfetch('/api/audit/flows/' + fid + '/toggle', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: enabled })
      }).then(function (flow) {
        if (flow && !flow.latest_run && byId[fid]) flow.latest_run = byId[fid].latest_run;
        byId[fid] = flow;
        for (var i = 0; i < FLOWS.length; i++) if (FLOWS[i].id === fid) FLOWS[i] = flow;
        input.disabled = false;
        input.checked = flow.enabled;
        // Refresh the schedule / next-run cell.
        tr.querySelector('.af-sched-cell').innerHTML = scheduleCell(flow);
        toast(enabled ? 'Flow enabled' : 'Flow disabled');
      }).catch(function (e) {
        input.disabled = false;
        input.checked = !enabled;
        toast(e.message, 'error');
      });
    });

    // ── Run now ──
    mount.addEventListener('click', function (ev) {
      var btn = ev.target.closest('.af-run');
      if (!btn) return;
      var tr = btn.closest('tr');
      var fid = tr.getAttribute('data-fid');
      var f = byId[fid];
      var priorRunId = f && f.latest_run ? f.latest_run.id : null;
      btn.disabled = true;
      tr.querySelector('.af-status-cell').innerHTML = statusPill('running');
      jfetch('/api/audit/flows/' + fid + '/run', { method: 'POST' })
        .then(function () {
          toast('Run started — results in about a minute');
          pollForRun(fid, priorRunId, btn);
        })
        .catch(function (e) {
          btn.disabled = false;
          tr.querySelector('.af-status-cell').innerHTML = statusPill(f ? f.last_status : 'never_run');
          toast(e.message, 'error');
        });
    });

    function pollForRun(fid, priorRunId, btn) {
      var tries = 0;
      var MAX = 100; // ~5 min at 3s
      var timer = setInterval(function () {
        tries++;
        jfetch('/api/audit/flows/' + fid + '/runs').then(function (data) {
          var runs = (data && data.runs) || [];
          var latest = runs[0];
          if (latest && latest.id !== priorRunId && latest.status !== 'running') {
            clearInterval(timer);
            var f = byId[fid];
            if (f) {
              f.last_status = latest.status;
              f.latest_run = {
                id: latest.id,
                status: latest.status,
                assertions_total: latest.assertions_total,
                assertions_passed: latest.assertions_passed,
                started_at: latest.started_at
              };
              updateRow(f);
            }
            toast('Run ' + latest.status);
          } else if (tries >= MAX) {
            clearInterval(timer);
            if (btn) btn.disabled = false;
            toast('Run is taking longer than expected; check back shortly.', 'error');
          }
        }).catch(function () {
          if (tries >= MAX) { clearInterval(timer); if (btn) btn.disabled = false; }
        });
      }, 3000);
    }

    // ── Controls ──
    var searchEl = document.getElementById('afSearch');
    if (searchEl) searchEl.addEventListener('input', function () {
      search = this.value.trim().toLowerCase();
      render();
    });
    var chips = document.getElementById('afStatusFilter');
    if (chips) chips.addEventListener('click', function (ev) {
      var chip = ev.target.closest('.af-fchip');
      if (!chip) return;
      chips.querySelectorAll('.af-fchip').forEach(function (c) { c.classList.remove('is-active'); });
      chip.classList.add('is-active');
      statusFilter = chip.getAttribute('data-status');
      render();
    });

    render();

    // Enrich with latest_run summaries from the API.
    jfetch('/api/audit/flows').then(function (data) {
      var fresh = (data && data.flows) || [];
      FLOWS = fresh;
      byId = {};
      FLOWS.forEach(function (f) { byId[f.id] = f; });
      render();
    }).catch(function () { /* keep seeded render */ });
  }

  /* ==========================================================
     VENDORS — audit setup: pick platforms, review rule books,
     layer custom rules. Primary path is one-click enable from
     the rule-book catalog (not the raw connector list).
     ========================================================== */
  function initVendors() {
    var root = document.getElementById('afVendors');
    if (!root) return;

    var VENDORS = (window.__VENDORS__ || []).slice();
    var PLATFORMS = [];
    var PLATFORM_INDEX = {};
    var RB_CACHE = {};
    var CUSTOM_RULES = null;
    var current = null;       // selected vendor, or null
    var mode = 'editor';       // editor | empty
    var pickerQuery = '';
    var enabling = null;      // platform slug being created

    var els = {
      list: document.getElementById('avList'),
      railCount: document.getElementById('avRailCount'),
      picker: document.getElementById('avPicker'),
      editor: document.getElementById('avEditor'),
      empty: document.getElementById('avEmpty'),
      grid: document.getElementById('avPlatformGrid'),
      search: document.getElementById('avPickerSearch'),
      title: document.getElementById('avFormTitle'),
      kicker: document.getElementById('avEditKicker'),
      badges: document.getElementById('avDetailBadges'),
      tabs: document.getElementById('avTabs'),
      name: document.getElementById('avName'),
      slug: document.getElementById('avSlug'),
      platform: document.getElementById('avPlatform'),
      pattern: document.getElementById('avPattern'),
      desc: document.getElementById('avDesc'),
      params: document.getElementById('avParams'),
      seed: document.getElementById('avSeedBtn'),
      del: document.getElementById('avDeleteBtn'),
      rbPane: document.getElementById('avRulebook'),
      rbCount: document.getElementById('avRbCount'),
      crCount: document.getElementById('avCrCount'),
      crList: document.getElementById('avCustomList')
    };

    // Main stage is either the editor or the empty-state card.
    // Platform picker is a fixed modal — never in the layout flow, so it
    // cannot leave a blank white panel under/beside the editor.
    function showStage(m) {
      mode = m;
      var isEditor = m === 'editor';
      var isEmpty = m === 'empty';
      if (els.editor) {
        els.editor.hidden = !isEditor;
        els.editor.style.display = isEditor ? '' : 'none';
      }
      if (els.empty) {
        els.empty.hidden = !isEmpty;
        els.empty.style.display = isEmpty ? '' : 'none';
      }
    }

    function setPickerOpen(open) {
      if (!els.picker) return;
      els.picker.hidden = !open;
      // Modal uses display:flex when open; [hidden] alone can lose to that.
      els.picker.style.display = open ? 'flex' : 'none';
      document.body.style.overflow = open ? 'hidden' : '';
      if (open) {
        renderPlatformGrid();
        if (els.search) {
          els.search.value = pickerQuery;
          setTimeout(function () { try { els.search.focus(); } catch (e) {} }, 0);
        }
      }
    }

    function setTab(name) {
      root.querySelectorAll('[data-vpane]').forEach(function (p) {
        p.hidden = p.getAttribute('data-vpane') !== name;
      });
      els.tabs.querySelectorAll('.av-tab').forEach(function (t) {
        t.classList.toggle('is-active', t.getAttribute('data-vtab') === name);
      });
      if (name === 'rulebook') renderRulebookPane();
      if (name === 'custom') renderCustomPane();
    }
    els.tabs.addEventListener('click', function (ev) {
      var t = ev.target.closest('.av-tab');
      if (t) setTab(t.getAttribute('data-vtab'));
    });

    function vendorPlatform(v) {
      if (!v) return null;
      if (v.catalog_slug && PLATFORM_INDEX[v.catalog_slug]) return v.catalog_slug;
      if (v.slug && PLATFORM_INDEX[v.slug]) return v.slug;
      return null;
    }
    function selectedPlatform() { return (els.platform.value || '').trim() || null; }

    function fetchRb(platform) {
      if (RB_CACHE[platform]) return Promise.resolve(RB_CACHE[platform]);
      return jfetch('/api/tag-rulebook/platforms/' + encodeURIComponent(platform)).then(function (rb) {
        RB_CACHE[platform] = rb;
        return rb;
      });
    }

    function populatePlatformSelect() {
      var opts = ['<option value="">— none (custom only) —</option>'].concat(PLATFORMS.map(function (p) {
        return '<option value="' + esc(p.platform) + '">' + esc(p.display_name) +
          ' · ' + p.event_count + ' events · ' + p.global_rule_count + ' rules</option>';
      }));
      var cur = els.platform.value;
      els.platform.innerHTML = opts.join('');
      els.platform.value = cur || '';
    }

    function slugify(s) {
      return (s || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
    }

    function uniqueSlug(base) {
      var s = slugify(base) || 'vendor';
      var taken = {};
      VENDORS.forEach(function (v) {
        if (!current || v.id !== current.id) taken[v.slug] = true;
      });
      if (!taken[s]) return s;
      var i = 2;
      while (taken[s + '_' + i]) i++;
      return s + '_' + i;
    }

    function seedParamsFromRb(rb, existing) {
      var have = {};
      (existing || []).forEach(function (p) { if (p && p.key) have[p.key] = true; });
      var out = (existing || []).slice();
      var added = 0;
      (rb.events || []).forEach(function (ev) {
        (ev.required_params || []).forEach(function (p) {
          if (have[p.name] || added >= 15) return;
          have[p.name] = true;
          added++;
          out.push({ label: p.name, key: p.name, source: 'query', hint: p.type || '' });
        });
      });
      return out;
    }

    // ── Rail list ──
    function renderList() {
      var n = VENDORS.length;
      if (els.railCount) {
        els.railCount.textContent = n ? (n + ' configured') : 'None yet';
      }
      if (!n) {
        els.list.innerHTML =
          '<div class="av-list-empty"><strong>No tags yet</strong>' +
          'Add the pixels you fire so Fluxito knows what to validate.</div>';
        return;
      }
      els.list.innerHTML = VENDORS.map(function (v) {
        var platform = vendorPlatform(v);
        var pInfo = platform && PLATFORM_INDEX[platform];
        var ready = !!pInfo;
        var pc = (v.params || []).length;
        return '<button type="button" class="av-item' +
          (current && current.id === v.id ? ' is-active' : '') +
          '" data-vid="' + esc(v.id) + '"' + (current && current.id === v.id ? ' aria-current="true"' : '') + '>' +
          '<div class="av-item-top">' +
            '<span class="av-item-name">' + esc(v.name) + '</span>' +
            '<span class="av-item-ready' + (ready ? '' : ' is-custom') + '">' +
              (ready ? 'rule book' : 'custom') +
            '</span>' +
          '</div>' +
          '<div class="av-item-sub">' + esc(v.url_pattern || '—') + '</div>' +
          '<div class="av-item-meta">' +
            (pInfo ? '<span class="av-chip">' + esc(pInfo.display_name) + '</span>' : '') +
            '<span class="av-chip">' + pc + ' param' + (pc === 1 ? '' : 's') + '</span>' +
          '</div>' +
        '</button>';
      }).join('');
    }

    // ── Platform picker grid (rule books only) ──
    function addedPlatformSlugs() {
      var set = {};
      VENDORS.forEach(function (v) {
        var p = vendorPlatform(v);
        if (p) set[p] = v;
        if (v.slug) set[v.slug] = v;
        if (v.catalog_slug) set[v.catalog_slug] = v;
      });
      return set;
    }

    function renderPlatformGrid() {
      var q = (pickerQuery || '').trim().toLowerCase();
      var added = addedPlatformSlugs();
      var rows = PLATFORMS.filter(function (p) {
        if (!q) return true;
        return (p.display_name || '').toLowerCase().indexOf(q) >= 0 ||
          (p.platform || '').toLowerCase().indexOf(q) >= 0;
      });
      if (!rows.length) {
        els.grid.innerHTML = '<div class="af-empty-inline">No platforms match “' + esc(pickerQuery) + '”.</div>';
        return;
      }
      els.grid.innerHTML = rows.map(function (p) {
        var isAdded = !!added[p.platform];
        var busy = enabling === p.platform;
        return '<button type="button" class="av-plat' + (isAdded ? ' is-added' : '') + '"' +
          ' data-platform="' + esc(p.platform) + '"' +
          (busy ? ' disabled' : '') + '>' +
          '<div class="av-plat-name">' + esc(p.display_name) + '</div>' +
          '<div class="av-plat-meta">' + (p.event_count || 0) + ' events · ' +
            (p.global_rule_count || 0) + ' global rules</div>' +
          '<div class="av-plat-foot">' +
            '<span>' + (busy ? 'Enabling…' : isAdded ? 'Open' : 'Enable') + '</span>' +
            (p.spec_version ? '<span class="av-plat-ver">' + esc(p.spec_version) + '</span>' : '') +
          '</div>' +
        '</button>';
      }).join('');
    }

    function openPicker() {
      setPickerOpen(true);
    }

    function closePicker() {
      setPickerOpen(false);
    }

    function showEmptyOrFirst() {
      current = null;
      renderList();
      if (VENDORS.length) loadForm(VENDORS[0], { tab: 'rulebook' });
      else showStage('empty');
    }

    // ── Editor ──
    function renderBadges() {
      var platform = selectedPlatform();
      var bits = [];
      if (platform && PLATFORM_INDEX[platform]) {
        var p = PLATFORM_INDEX[platform];
        bits.push('<span class="ui-chip is-good">Checked in audits</span>');
        bits.push('<span class="ui-chip no-dot">' + esc(p.display_name) + '</span>');
        if (p.spec_version) bits.push('<span class="ui-chip no-dot av-ver">' + esc(p.spec_version) + '</span>');
        els.rbCount.hidden = false;
        els.rbCount.textContent = String((p.event_count || 0) + (p.global_rule_count || 0));
      } else {
        bits.push('<span class="ui-chip no-dot">No rule book · custom only</span>');
        els.rbCount.hidden = true;
      }
      els.badges.innerHTML = bits.join('');
      els.seed.hidden = !platform;
      var filtered = filteredCustomRules();
      els.crCount.hidden = filtered === null || !filtered.length;
      if (filtered && filtered.length) els.crCount.textContent = String(filtered.length);
    }

    function paramRow(p) {
      p = p || {};
      var srcQuery = (p.source || 'query') === 'query';
      return '<tr>' +
        '<td><input class="af-input af-input-sm p-label" value="' + esc(p.label || '') + '" placeholder="Label"/></td>' +
        '<td><input class="af-input af-input-sm mono p-key" value="' + esc(p.key || '') + '" placeholder="key"/></td>' +
        '<td><select class="af-select af-input-sm p-source">' +
          '<option value="query"' + (srcQuery ? ' selected' : '') + '>query</option>' +
          '<option value="auto"' + (!srcQuery ? ' selected' : '') + '>auto</option>' +
        '</select></td>' +
        '<td><input class="af-input af-input-sm p-default" value="' + esc(p.default || '') + '" placeholder="—"/></td>' +
        '<td><input class="af-input af-input-sm p-hint" value="' + esc(p.hint || '') + '" placeholder="type"/></td>' +
        '<td><button type="button" class="af-remove-x p-remove" title="Remove">&times;</button></td>' +
      '</tr>';
    }

    function renderParams(params) {
      els.params.innerHTML = (params || []).map(paramRow).join('');
    }

    function collectParams() {
      var out = [];
      els.params.querySelectorAll('tr').forEach(function (tr) {
        var key = tr.querySelector('.p-key').value.trim();
        if (!key) return;
        var p = {
          label: tr.querySelector('.p-label').value.trim(),
          key: key,
          source: tr.querySelector('.p-source').value
        };
        var def = tr.querySelector('.p-default').value;
        var hint = tr.querySelector('.p-hint').value.trim();
        if (def !== '') p.default = def;
        if (hint) p.hint = hint;
        out.push(p);
      });
      return out;
    }

    function loadForm(v, opts) {
      opts = opts || {};
      current = v;
      showStage('editor');
      closePicker();
      els.kicker.textContent = v && v.id ? 'Configured tag' : 'New custom vendor';
      els.title.textContent = v ? v.name : 'Custom vendor';
      els.name.value = v ? v.name : '';
      els.slug.value = v ? v.slug : '';
      els.platform.value = (v && vendorPlatform(v)) || (v && v.catalog_slug) || '';
      if (els.platform.value && !PLATFORM_INDEX[els.platform.value]) els.platform.value = '';
      els.pattern.value = v ? (v.url_pattern || '') : '';
      els.desc.value = v ? (v.description || '') : '';
      renderParams(v ? v.params : []);
      els.del.hidden = !(v && v.id);
      renderList();
      renderBadges();
      setTab(opts.tab || (v && v.id ? 'rulebook' : 'advanced'));
    }

    // ── Rule book pane ──
    function renderRulebookPane() {
      var platform = selectedPlatform();
      if (!platform) {
        els.rbPane.innerHTML =
          '<div class="af-empty-inline">No rule book linked. Pick one in the essentials bar above, ' +
          'or enable a platform from <button type="button" class="av-link-btn" id="avRbAddTag">Add tag</button>.</div>';
        var btn = document.getElementById('avRbAddTag');
        if (btn) btn.addEventListener('click', openPicker);
        return;
      }
      els.rbPane.innerHTML = '<div class="af-empty-inline">Loading rule book…</div>';
      fetchRb(platform).then(function (rb) {
        if (selectedPlatform() !== platform) return;
        var html = '';
        html += '<div class="af-rb-head">' +
          '<div><div class="af-rb-title">' + esc(rb.display_name) + '</div>' +
          '<div class="af-rb-sub">' + esc(rb.spec_version || '') +
            (rb.docs_url ? ' · <a href="' + esc(rb.docs_url) + '" target="_blank" rel="noopener">Docs ↗</a>' : '') +
          '</div></div>' +
          '<div class="af-rb-stats">' + (rb.event_count || 0) + ' events · ' +
            (rb.global_rules || []).length + ' global rules</div>' +
        '</div>';
        if ((rb.detection_patterns || []).length) {
          html += '<div class="af-rb-section">How this tag is detected</div>' +
            '<div class="af-rb-patterns">' + rb.detection_patterns.map(function (p) {
              return '<code>' + esc(p) + '</code>';
            }).join(' ') + '</div>';
        }
        if ((rb.global_rules || []).length) {
          html += '<div class="af-rb-section">Global rules</div>';
          html += '<div class="af-rb-rules">' + rb.global_rules.map(function (r) {
            return '<div class="af-rb-rule" data-sev="' + esc(r.severity || 'info') + '">' +
              sevChip(r.severity) +
              '<div class="af-rb-rule-main"><div class="af-rb-rule-desc">' + esc(r.description || '') + '</div>' +
              (r.remediation ? '<div class="af-rb-rule-fix">Fix: ' + esc(r.remediation) + '</div>' : '') + '</div>' +
              '<code class="af-rb-rule-id">' + esc(r.rule_id) + '</code>' +
            '</div>';
          }).join('') + '</div>';
        }
        if ((rb.events || []).length) {
          html += '<div class="af-rb-section">Event specs</div>';
          html += rb.events.map(function (ev) {
            var req = (ev.required_params || []).map(function (p) { return p.name; });
            var rec = (ev.recommended_params || []).map(function (p) { return p.name; });
            return '<details class="af-rb-event"><summary><code>' + esc(ev.event_name) + '</code>' +
              '<span class="af-rb-event-meta">' + req.length + ' required · ' + rec.length + ' recommended</span></summary>' +
              (req.length ? '<div class="af-rb-plist"><strong>Required:</strong> ' + req.map(esc).join(', ') + '</div>' : '') +
              (rec.length ? '<div class="af-rb-plist"><strong>Recommended:</strong> ' + rec.map(esc).join(', ') + '</div>' : '') +
              (ev.notes ? '<div class="af-rb-plist">' + esc(ev.notes) + '</div>' : '') +
            '</details>';
          }).join('');
        }
        els.rbPane.innerHTML = html;
      }).catch(function () {
        els.rbPane.innerHTML = '<div class="af-empty-inline">Could not load the rule book.</div>';
      });
    }

    function applySeed(rb, opts) {
      opts = opts || {};
      if (opts.forcePattern || !els.pattern.value.trim()) {
        if ((rb.detection_patterns || []).length) {
          els.pattern.value = rb.detection_patterns[0];
        }
      }
      var next = seedParamsFromRb(rb, opts.replaceParams ? [] : collectParams());
      renderParams(next);
    }

    els.seed.addEventListener('click', function () {
      var platform = selectedPlatform();
      if (!platform) return;
      fetchRb(platform).then(function (rb) {
        applySeed(rb, { forcePattern: true });
        toast('Re-seeded detection + params from ' + rb.display_name);
      }).catch(function () { toast('Could not load the rule book', 'error'); });
    });

    // ── Custom rules ──
    function filteredCustomRules() {
      if (CUSTOM_RULES === null) return null;
      var platform = selectedPlatform();
      var vslug = current ? current.slug : null;
      return CUSTOM_RULES.filter(function (r) {
        return (platform && r.platform === platform) || (vslug && r.platform === vslug) || r.platform === '*';
      });
    }

    function loadCustomRules() {
      return jfetch('/api/custom-rules').then(function (data) {
        CUSTOM_RULES = data.rules || [];
        renderBadges();
      }).catch(function () { CUSTOM_RULES = []; });
    }

    function renderCustomPane() {
      var run = (CUSTOM_RULES === null) ? loadCustomRules() : Promise.resolve();
      els.crList.innerHTML = '<div class="af-empty-inline">Loading…</div>';
      run.then(function () {
        var rules = filteredCustomRules() || [];
        if (!rules.length) {
          els.crList.innerHTML =
            '<div class="af-empty-inline">No custom rules yet — add one below when the platform book is not enough.</div>';
          return;
        }
        els.crList.innerHTML = rules.map(function (r) {
          var req = (r.required_params || []).join(', ');
          var forb = (r.forbidden_params || []).join(', ');
          return '<div class="af-cr-row" data-sev="' + esc(r.severity || 'warning') + '">' +
            sevChip(r.severity || 'warning') +
            '<div class="af-cr-main">' +
              '<div class="af-cr-name">' + esc(r.name || r.rule_id) +
                (r.platform === '*' ? '<span class="ui-chip no-dot">all platforms</span>' : '') +
              '</div>' +
              '<div class="af-cr-meta mono">' + esc(r.platform) + ' · ' + esc(r.event || '*') +
                (req ? ' · requires: ' + esc(req) : '') + (forb ? ' · forbids: ' + esc(forb) : '') + '</div>' +
              (r.remediation ? '<div class="af-cr-fix">Fix: ' + esc(r.remediation) + '</div>' : '') +
            '</div>' +
            '<button type="button" class="af-remove-x af-cr-delete" data-rule-id="' + esc(r.rule_id) + '" title="Delete rule" aria-label="Delete rule ' + esc(r.name || r.rule_id) + '">&times;</button>' +
          '</div>';
        }).join('');
      });
    }

    els.crList.addEventListener('click', function (ev) {
      var btn = ev.target.closest('.af-cr-delete');
      if (!btn) return;
      var ruleId = btn.getAttribute('data-rule-id');
      if (!confirm('Delete this custom rule? Every audit and flow using it stops enforcing it.')) return;
      jfetch('/api/custom-rules/' + encodeURIComponent(ruleId), { method: 'DELETE' }).then(function () {
        CUSTOM_RULES = (CUSTOM_RULES || []).filter(function (r) { return r.rule_id !== ruleId; });
        renderCustomPane();
        renderBadges();
        toast('Custom rule deleted');
      }).catch(function (e) { toast(e.message, 'error'); });
    });

    document.getElementById('avCrSaveBtn').addEventListener('click', function () {
      var name = document.getElementById('avCrName').value.trim();
      if (!name) { toast('Rule name is required', 'error'); return; }
      var platform = selectedPlatform() || (current && current.slug) || '*';
      var csv = function (id) {
        return document.getElementById(id).value.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
      };
      var body = {
        rule_id: 'custom.' + slugify(name),
        platform: platform,
        event: document.getElementById('avCrEvent').value.trim() || '*',
        name: name,
        severity: document.getElementById('avCrSeverity').value,
        required_params: csv('avCrRequired'),
        forbidden_params: csv('avCrForbidden'),
        param_assertions: [],
        remediation: document.getElementById('avCrRemediation').value.trim() || null
      };
      jfetch('/api/custom-rules', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      }).then(function () {
        ['avCrName', 'avCrEvent', 'avCrRequired', 'avCrForbidden', 'avCrRemediation'].forEach(function (id) {
          document.getElementById(id).value = '';
        });
        return loadCustomRules();
      }).then(function () {
        renderCustomPane();
        toast('Custom rule saved');
      }).catch(function (e) { toast(e.message, 'error'); });
    });

    // ── Persist vendor ──
    function upsertLocal(v) {
      var found = false;
      for (var i = 0; i < VENDORS.length; i++) {
        if (VENDORS[i].id === v.id) { VENDORS[i] = v; found = true; break; }
      }
      if (!found) VENDORS.push(v);
      VENDORS.sort(function (a, b) { return (a.name || '').localeCompare(b.name || ''); });
    }

    function saveVendorBody() {
      return {
        name: els.name.value.trim(),
        slug: els.slug.value.trim().toLowerCase(),
        url_pattern: els.pattern.value.trim(),
        description: els.desc.value.trim() || null,
        params: collectParams(),
        catalog_slug: selectedPlatform()
      };
    }

    document.getElementById('avSaveBtn').addEventListener('click', function () {
      var body = saveVendorBody();
      if (!body.name) { toast('Name is required', 'error'); return; }
      if (!body.slug) { toast('Slug is required (Detection & params tab)', 'error'); setTab('advanced'); return; }
      if (!body.url_pattern) { toast('URL pattern is required (Detection & params tab)', 'error'); setTab('advanced'); return; }
      var editing = current && current.id;
      var url = editing ? '/api/audit/vendors/' + current.id : '/api/audit/vendors';
      jfetch(url, {
        method: editing ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      }).then(function (v) {
        upsertLocal(v);
        loadForm(v, { tab: 'rulebook' });
        toast(editing ? 'Tag updated' : 'Tag saved');
      }).catch(function (e) { toast(e.message, 'error'); });
    });

    els.del.addEventListener('click', function () {
      if (!current || !current.id) return;
      if (!confirm('Remove "' + current.name + '" from this project? Audits and flows stop using it.')) return;
      jfetch('/api/audit/vendors/' + current.id, { method: 'DELETE' }).then(function () {
        VENDORS = VENDORS.filter(function (v) { return v.id !== current.id; });
        toast('Tag removed');
        showEmptyOrFirst();
      }).catch(function (e) { toast(e.message, 'error'); });
    });

    // ── One-click enable from rule-book platform ──
    function enablePlatform(platformSlug) {
      var existing = addedPlatformSlugs()[platformSlug];
      if (existing && existing.id) {
        loadForm(existing, { tab: 'rulebook' });
        return;
      }
      var summary = PLATFORM_INDEX[platformSlug];
      if (!summary) { toast('Unknown platform', 'error'); return; }
      enabling = platformSlug;
      renderPlatformGrid();
      fetchRb(platformSlug).then(function (rb) {
        var pattern = ((rb.detection_patterns || [])[0]) || platformSlug;
        var params = seedParamsFromRb(rb, []);
        var body = {
          name: rb.display_name || summary.display_name || platformSlug,
          slug: uniqueSlug(platformSlug),
          url_pattern: pattern,
          description: null,
          params: params,
          catalog_slug: platformSlug
        };
        return jfetch('/api/audit/vendors', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        });
      }).then(function (v) {
        upsertLocal(v);
        enabling = null;
        loadForm(v, { tab: 'rulebook' });
        toast(v.name + ' enabled — review the rule book below');
      }).catch(function (e) {
        enabling = null;
        renderPlatformGrid();
        toast(e.message || 'Could not enable platform', 'error');
      });
    }

    els.grid.addEventListener('click', function (ev) {
      var card = ev.target.closest('.av-plat');
      if (!card || card.disabled) return;
      enablePlatform(card.getAttribute('data-platform'));
    });

    if (els.search) {
      els.search.addEventListener('input', function () {
        pickerQuery = els.search.value || '';
        renderPlatformGrid();
      });
    }

    // ── Custom vendor (blank form) ──
    function startCustom() {
      current = null;
      loadForm({
        name: '',
        slug: '',
        url_pattern: '',
        description: '',
        params: [],
        catalog_slug: null
      }, { tab: 'advanced' });
      els.kicker.textContent = 'Custom vendor';
      els.title.textContent = 'New custom vendor';
      els.del.hidden = true;
      els.slug.value = '';
      els.pattern.focus();
    }

    // ── Wiring ──
    function wireAdd(id) {
      var el = document.getElementById(id);
      if (el) el.addEventListener('click', openPicker);
    }
    wireAdd('avAddOpen');
    wireAdd('avAddOpenRail');
    wireAdd('avEmptyAdd');
    if (els.picker) {
      els.picker.querySelectorAll('[data-av-close]').forEach(function (el) {
        el.addEventListener('click', closePicker);
      });
      document.addEventListener('keydown', function (ev) {
        if (ev.key === 'Escape' && els.picker && !els.picker.hidden) closePicker();
      });
    }
    var customBtn = document.getElementById('avCustomBtn');
    if (customBtn) customBtn.addEventListener('click', function () {
      closePicker();
      startCustom();
    });

    document.getElementById('avAddParam').addEventListener('click', function () {
      els.params.insertAdjacentHTML('beforeend', paramRow({}));
    });
    els.params.addEventListener('click', function (ev) {
      var rm = ev.target.closest('.p-remove');
      if (rm) rm.closest('tr').remove();
    });

    els.list.addEventListener('click', function (ev) {
      var item = ev.target.closest('.av-item');
      if (!item) return;
      var vid = item.getAttribute('data-vid');
      var v = VENDORS.filter(function (x) { return x.id === vid; })[0];
      if (v) loadForm(v, { tab: 'rulebook' });
    });

    els.platform.addEventListener('change', function () {
      renderBadges();
      var platform = selectedPlatform();
      if (!platform) return;
      // Auto-seed empty detection fields when linking a book
      fetchRb(platform).then(function (rb) {
        if (!els.pattern.value.trim() || !collectParams().length) {
          applySeed(rb, { forcePattern: !els.pattern.value.trim() });
        }
        if (mode === 'editor') renderRulebookPane();
      }).catch(function () {});
    });

    // ── Boot ──
    jfetch('/api/tag-rulebook/platforms').then(function (data) {
      PLATFORMS = data.platforms || [];
      PLATFORMS.forEach(function (p) { PLATFORM_INDEX[p.platform] = p; });
      populatePlatformSelect();
      renderList();
      if (!VENDORS.length) {
        showStage('empty');
        openPicker();
      } else {
        loadForm(VENDORS[0], { tab: 'rulebook' });
      }
    }).catch(function () {
      renderList();
      if (!VENDORS.length) showStage('empty');
      else loadForm(VENDORS[0], { tab: 'advanced' });
    });

    loadCustomRules();
    renderList();
  }

  function boot() { initFlows(); initVendors(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
