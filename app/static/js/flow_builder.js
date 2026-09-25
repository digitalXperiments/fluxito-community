/* ============================================================
   Automated Test Flows — stateful flow builder.

   State is a single plain object (`STATE`) rendered functionally.
   Simple top-level fields (name/base_url/device/desc/cron/tz/enabled)
   live in the DOM and are read at save time; the dynamic structures
   (steps + their assertions, groups, notify selections) live in STATE.
   Before any structural re-render we call syncSelectedStepFromDOM() so
   in-progress text edits are never lost.
   ============================================================ */
(function () {
  'use strict';

  var OPS = ['equals', 'contains', 'regex', 'exists', 'not_empty'];
  var ACTIONS = ['navigate', 'click', 'type', 'wait'];

  function toast(msg, kind) {
    if (window.Fluxito && window.Fluxito.toast) window.Fluxito.toast(msg, kind);
  }
  function esc(s) {
    return (s == null ? '' : String(s))
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function el(id) { return document.getElementById(id); }
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

  var root = el('afBuilder');
  if (!root) return;
  var PROJECT_SLUG = root.getAttribute('data-project-slug') || '';
  var VENDORS = window.__VENDORS__ || [];
  var VENDOR_BY_ID = {};
  VENDORS.forEach(function (v) { VENDOR_BY_ID[v.id] = v; });
  var FLOW = window.__FLOW__ || null;

  // ── State ────────────────────────────────────────────────
  var STATE = {
    id: FLOW ? FLOW.id : null,
    device: FLOW ? (FLOW.device || 'desktop') : 'desktop',
    steps: normalizeSteps(FLOW ? FLOW.steps : []),
    selected: 0,
    groups: FLOW ? (FLOW.groups || []).slice() : [],
    notify: FLOW ? (FLOW.notify || {}) : {}
  };
  var WEBHOOKS = null;   // null = not loaded; [] = loaded/empty; false = fetch failed
  var SENDERS = null;

  function normalizeSteps(steps) {
    return (steps || []).map(function (s) {
      var a = s.assertions || {};
      return {
        action: s.action || 'navigate',
        label: s.label || '',
        url: s.url || '',
        selector: s.selector || '',
        text: s.text || '',
        ms: s.ms != null ? s.ms : 1000,
        assertions: {
          datalayer_events: (a.datalayer_events || []).map(function (e) {
            return {
              event: e.event || '',
              mode: e.mode || 'must',
              when: e.when || 'anytime',
              fields: (e.fields || []).map(cloneCheck)
            };
          }),
          vendor_requests: (a.vendor_requests || []).map(function (v) {
            return {
              vendor_id: v.vendor_id || '',
              mode: v.mode || 'must',
              when: v.when || 'anytime',
              params: (v.params || []).map(cloneCheck)
            };
          })
        }
      };
    });
  }
  function cloneCheck(c) {
    return { key: c.key || '', op: c.op || 'equals', value: c.value != null ? c.value : '' };
  }
  function emptyStep() {
    return {
      action: 'navigate', label: '', url: '', selector: '', text: '', ms: 1000,
      assertions: { datalayer_events: [], vendor_requests: [] }
    };
  }
  function assertCount(s) {
    var a = s.assertions || {};
    return (a.datalayer_events || []).length + (a.vendor_requests || []).length;
  }
  function stepHasAssert(s) {
    var a = s.assertions || {};
    return (a.datalayer_events || []).length > 0 || (a.vendor_requests || []).length > 0;
  }

  /* ==========================================================
     Steps rail
     ========================================================== */
  function renderRail() {
    var rail = el('afStepsRail');
    var html = STATE.steps.map(function (s, i) {
      return '<button type="button" class="af-step-chip' +
        (i === STATE.selected ? ' is-active' : '') + (stepHasAssert(s) ? ' has-assert' : '') +
        '" data-idx="' + i + '">' +
        '<span class="af-step-num">' + (i + 1) + '</span>' +
        '<span class="ui-tool af-step-tool">' + esc(s.action || 'step') + '</span>' +
        '<span class="af-step-label">' + esc(s.label || s.action) + '</span>' +
        (stepHasAssert(s) ? '<span class="af-step-checks">' + assertCount(s) + ' check' + (assertCount(s) === 1 ? '' : 's') + '</span>' : '') +
        '</button>';
    }).join('');
    html += '<button type="button" class="af-step-add" id="afAddStep">+ Add step</button>';
    rail.innerHTML = html;
  }

  /* ==========================================================
     Step editor
     ========================================================== */
  function actionFields(s) {
    if (s.action === 'navigate') {
      return field('URL', '<input class="af-input mono af-sf" data-field="url" value="' + esc(s.url) + '" placeholder="https://… or /relative (optional)"/>');
    }
    if (s.action === 'click') {
      return field('Selector', '<input class="af-input mono af-sf" data-field="selector" value="' + esc(s.selector) + '" placeholder="button.checkout"/>');
    }
    if (s.action === 'type') {
      return field('Selector', '<input class="af-input mono af-sf" data-field="selector" value="' + esc(s.selector) + '" placeholder="#email"/>') +
        field('Text', '<input class="af-input af-sf" data-field="text" value="' + esc(s.text) + '" placeholder="alice@example.com"/>');
    }
    if (s.action === 'wait') {
      return field('Wait (ms)', '<input class="af-input mono af-sf" data-field="ms" type="number" min="0" max="30000" value="' + esc(s.ms) + '"/>');
    }
    return '';
  }
  function field(label, inner) {
    return '<div class="af-field"><label class="af-label">' + esc(label) + '</label>' + inner + '</div>';
  }
  function opOptions(sel) {
    return OPS.map(function (o) {
      return '<option value="' + o + '"' + (o === sel ? ' selected' : '') + '>' + o + '</option>';
    }).join('');
  }
  function modeWhenToggle(cls, mode, when) {
    return '<div class="af-seg-toggle ' + cls + '-mode" data-val="' + esc(mode) + '">' +
        '<button type="button" data-mode="must" class="' + (mode !== 'must_not' ? 'is-active' : '') + '">must</button>' +
        '<button type="button" data-mode="must_not" class="' + (mode === 'must_not' ? 'is-active' : '') + '">must not</button>' +
      '</div>' +
      '<div class="af-seg-toggle ' + cls + '-when" data-val="' + esc(when) + '">' +
        '<button type="button" data-when="anytime" class="' + (when !== 'at_step' ? 'is-active' : '') + '">anytime</button>' +
        '<button type="button" data-when="at_step" class="' + (when === 'at_step' ? 'is-active' : '') + '">at step</button>' +
      '</div>';
  }
  function checkRow(cls, c) {
    return '<div class="af-check-row ' + cls + '-check">' +
      '<input class="af-input af-input-sm chk-key" value="' + esc(c.key) + '" placeholder="key"/>' +
      '<select class="af-select af-input-sm chk-op">' + opOptions(c.op) + '</select>' +
      '<input class="af-input af-input-sm chk-value" value="' + esc(c.value) + '" placeholder="value"/>' +
      '<button type="button" class="af-remove-x chk-remove" title="Remove">&times;</button>' +
    '</div>';
  }

  function renderEditor() {
    var wrap = el('afStepEditor');
    if (!STATE.steps.length) {
      wrap.innerHTML = '<div class="af-empty-hint">No steps yet. Add a step to begin — the first is usually a <strong>navigate</strong>.</div>';
      return;
    }
    var i = STATE.selected;
    var s = STATE.steps[i];
    var a = s.assertions;

    var actionSel = '<select class="af-select" id="afStepAction">' + ACTIONS.map(function (act) {
      return '<option value="' + act + '"' + (act === s.action ? ' selected' : '') + '>' + act + '</option>';
    }).join('') + '</select>';

    // dataLayer event blocks
    var dlHtml = (a.datalayer_events || []).map(function (e, j) {
      return '<div class="af-assert-block dl-block" data-j="' + j + '">' +
        '<div class="af-assert-block-head">' +
          '<input class="af-input dl-event" value="' + esc(e.event) + '" placeholder="event name e.g. purchase"/>' +
          modeWhenToggle('dl', e.mode, e.when) +
          '<button type="button" class="af-remove-x dl-remove" title="Remove event check">&times;</button>' +
        '</div>' +
        (e.fields || []).map(function (f) { return checkRow('dl', f); }).join('') +
        '<button type="button" class="af-mini-add dl-addfield">+ Add field check</button>' +
      '</div>';
    }).join('');

    // vendor request blocks
    var vrHtml = (a.vendor_requests || []).map(function (v, j) {
      var vendorOpts = '<option value="">— select vendor —</option>' + VENDORS.map(function (vn) {
        return '<option value="' + esc(vn.id) + '"' + (vn.id === v.vendor_id ? ' selected' : '') + '>' + esc(vn.name) + '</option>';
      }).join('');
      var picks = '';
      var vendor = VENDOR_BY_ID[v.vendor_id];
      if (vendor && (vendor.params || []).length) {
        picks = '<div class="af-param-picks">' + vendor.params.map(function (p) {
          return '<button type="button" class="af-param-pick vr-pick" data-key="' + esc(p.key) +
            '" data-default="' + esc(p.default || '') + '" title="' + esc(p.hint || '') + '">' + esc(p.label || p.key) + '</button>';
        }).join('') + '</div>';
      }
      return '<div class="af-assert-block vr-block" data-j="' + j + '">' +
        '<div class="af-assert-block-head">' +
          '<select class="af-select vr-vendor">' + vendorOpts + '</select>' +
          modeWhenToggle('vr', v.mode, v.when) +
          '<button type="button" class="af-remove-x vr-remove" title="Remove vendor check">&times;</button>' +
        '</div>' +
        picks +
        (v.params || []).map(function (p) { return checkRow('vr', p); }).join('') +
        '<button type="button" class="af-mini-add vr-addparam">+ Add param check</button>' +
      '</div>';
    }).join('');

    var vendorsNote = VENDORS.length ? '' :
      '<div class="af-empty-hint">No vendors defined yet. <a href="/audits/vendors">Add vendors</a> to assert on beacons.</div>';

    wrap.innerHTML =
      '<div class="af-step-editor-head">' +
        '<span class="af-mono">Step ' + (i + 1) + '</span>' +
        '<div class="af-step-reorder">' +
          '<button type="button" class="af-iconbtn" id="afStepUp" title="Move up"' + (i === 0 ? ' disabled' : '') + '>&uarr;</button>' +
          '<button type="button" class="af-iconbtn" id="afStepDown" title="Move down"' + (i === STATE.steps.length - 1 ? ' disabled' : '') + '>&darr;</button>' +
          '<button type="button" class="af-iconbtn" id="afStepRemove" title="Remove step">&times;</button>' +
        '</div>' +
      '</div>' +
      field('Action', actionSel) +
      field('Label', '<input class="af-input af-sf" data-field="label" value="' + esc(s.label) + '" placeholder="Human-readable step name"/>') +
      actionFields(s) +
      '<div class="af-assert-section">' +
        '<div class="af-card-title">DataLayer events</div>' +
        '<div id="afDlList">' + dlHtml + '</div>' +
        '<button type="button" class="af-mini-add" id="afAddDl">+ Add event check</button>' +
      '</div>' +
      '<div class="af-assert-section">' +
        '<div class="af-card-title">Vendor requests</div>' +
        vendorsNote +
        '<div id="afVrList">' + vrHtml + '</div>' +
        (VENDORS.length ? '<button type="button" class="af-mini-add" id="afAddVr">+ Add vendor check</button>' : '') +
      '</div>';
  }

  /* ==========================================================
     Sync DOM → STATE for the selected step
     ========================================================== */
  function syncSelectedStepFromDOM() {
    if (!STATE.steps.length) return;
    var wrap = el('afStepEditor');
    var s = STATE.steps[STATE.selected];
    var actionEl = wrap.querySelector('#afStepAction');
    if (actionEl) s.action = actionEl.value;
    wrap.querySelectorAll('.af-sf').forEach(function (inp) {
      var f = inp.getAttribute('data-field');
      if (f === 'ms') s.ms = parseInt(inp.value, 10) || 0;
      else s[f] = inp.value;
    });
    // dataLayer events
    var dl = [];
    wrap.querySelectorAll('.dl-block').forEach(function (blk) {
      var ev = {
        event: (blk.querySelector('.dl-event').value || '').trim(),
        mode: blk.querySelector('.dl-mode').getAttribute('data-val') || 'must',
        when: blk.querySelector('.dl-when').getAttribute('data-val') || 'anytime',
        fields: readChecks(blk)
      };
      dl.push(ev);
    });
    // vendor requests
    var vr = [];
    wrap.querySelectorAll('.vr-block').forEach(function (blk) {
      var v = {
        vendor_id: blk.querySelector('.vr-vendor').value || '',
        mode: blk.querySelector('.vr-mode').getAttribute('data-val') || 'must',
        when: blk.querySelector('.vr-when').getAttribute('data-val') || 'anytime',
        params: readChecks(blk)
      };
      vr.push(v);
    });
    s.assertions = { datalayer_events: dl, vendor_requests: vr };
  }
  function readChecks(blk) {
    var out = [];
    blk.querySelectorAll('.af-check-row').forEach(function (row) {
      out.push({
        key: (row.querySelector('.chk-key').value || '').trim(),
        op: row.querySelector('.chk-op').value,
        value: row.querySelector('.chk-value').value
      });
    });
    return out;
  }

  function rerenderStep() { renderRail(); renderEditor(); }

  /* ==========================================================
     Event wiring — steps rail + editor (delegated)
     ========================================================== */
  el('afStepsRail').addEventListener('click', function (ev) {
    var add = ev.target.closest('#afAddStep');
    if (add) {
      syncSelectedStepFromDOM();
      STATE.steps.push(emptyStep());
      STATE.selected = STATE.steps.length - 1;
      rerenderStep();
      return;
    }
    var chip = ev.target.closest('.af-step-chip');
    if (chip) {
      syncSelectedStepFromDOM();
      STATE.selected = parseInt(chip.getAttribute('data-idx'), 10);
      rerenderStep();
    }
  });

  var editor = el('afStepEditor');

  // Keep chip label live as you type the label field.
  editor.addEventListener('input', function (ev) {
    if (ev.target.matches('.af-sf[data-field="label"]')) {
      var chip = el('afStepsRail').querySelector('.af-step-chip[data-idx="' + STATE.selected + '"]');
      if (chip) {
        var val = ev.target.value || STATE.steps[STATE.selected].action;
        chip.lastChild.textContent = val;
      }
    }
  });

  editor.addEventListener('change', function (ev) {
    if (ev.target.id === 'afStepAction') {
      syncSelectedStepFromDOM();
      renderEditor();
      renderRail();
    } else if (ev.target.matches('.vr-vendor')) {
      // Re-render so the vendor's param quick-picks appear.
      syncSelectedStepFromDOM();
      renderEditor();
    }
  });

  editor.addEventListener('click', function (ev) {
    var t = ev.target;

    // Reorder / remove step
    if (t.closest('#afStepUp')) { moveStep(-1); return; }
    if (t.closest('#afStepDown')) { moveStep(1); return; }
    if (t.closest('#afStepRemove')) { removeStep(); return; }

    // mode / when segmented toggles
    var mBtn = t.closest('.af-seg-toggle button');
    if (mBtn) {
      var group = mBtn.parentElement;
      group.querySelectorAll('button').forEach(function (b) { b.classList.remove('is-active'); });
      mBtn.classList.add('is-active');
      group.setAttribute('data-val', mBtn.getAttribute('data-mode') || mBtn.getAttribute('data-when'));
      return;
    }

    // Add / remove dataLayer event
    if (t.closest('#afAddDl')) {
      syncSelectedStepFromDOM();
      STATE.steps[STATE.selected].assertions.datalayer_events.push({ event: '', mode: 'must', when: 'anytime', fields: [] });
      renderEditor(); renderRail(); return;
    }
    if (t.closest('.dl-remove')) {
      syncSelectedStepFromDOM();
      var dj = parseInt(t.closest('.dl-block').getAttribute('data-j'), 10);
      STATE.steps[STATE.selected].assertions.datalayer_events.splice(dj, 1);
      renderEditor(); renderRail(); return;
    }
    if (t.closest('.dl-addfield')) {
      syncSelectedStepFromDOM();
      var dblk = parseInt(t.closest('.dl-block').getAttribute('data-j'), 10);
      STATE.steps[STATE.selected].assertions.datalayer_events[dblk].fields.push({ key: '', op: 'equals', value: '' });
      renderEditor(); return;
    }

    // Add / remove vendor request
    if (t.closest('#afAddVr')) {
      syncSelectedStepFromDOM();
      STATE.steps[STATE.selected].assertions.vendor_requests.push({ vendor_id: '', mode: 'must', when: 'anytime', params: [] });
      renderEditor(); renderRail(); return;
    }
    if (t.closest('.vr-remove')) {
      syncSelectedStepFromDOM();
      var vj = parseInt(t.closest('.vr-block').getAttribute('data-j'), 10);
      STATE.steps[STATE.selected].assertions.vendor_requests.splice(vj, 1);
      renderEditor(); renderRail(); return;
    }
    if (t.closest('.vr-addparam')) {
      syncSelectedStepFromDOM();
      var vblk = parseInt(t.closest('.vr-block').getAttribute('data-j'), 10);
      STATE.steps[STATE.selected].assertions.vendor_requests[vblk].params.push({ key: '', op: 'equals', value: '' });
      renderEditor(); return;
    }

    // Vendor param quick-pick
    var pick = t.closest('.vr-pick');
    if (pick) {
      syncSelectedStepFromDOM();
      var vb = parseInt(pick.closest('.vr-block').getAttribute('data-j'), 10);
      STATE.steps[STATE.selected].assertions.vendor_requests[vb].params.push({
        key: pick.getAttribute('data-key'),
        op: pick.getAttribute('data-default') ? 'equals' : 'exists',
        value: pick.getAttribute('data-default') || ''
      });
      renderEditor(); return;
    }

    // Remove a field/param check row
    if (t.closest('.chk-remove')) {
      var row = t.closest('.af-check-row');
      row.parentElement.removeChild(row);
      return;
    }
  });

  function moveStep(delta) {
    syncSelectedStepFromDOM();
    var i = STATE.selected, j = i + delta;
    if (j < 0 || j >= STATE.steps.length) return;
    var tmp = STATE.steps[i]; STATE.steps[i] = STATE.steps[j]; STATE.steps[j] = tmp;
    STATE.selected = j;
    rerenderStep();
  }
  function removeStep() {
    STATE.steps.splice(STATE.selected, 1);
    if (STATE.selected >= STATE.steps.length) STATE.selected = Math.max(0, STATE.steps.length - 1);
    rerenderStep();
  }

  /* ==========================================================
     Top-level fields
     ========================================================== */
  function initTopFields() {
    if (FLOW) {
      el('afName').value = FLOW.name || '';
      el('afBaseUrl').value = FLOW.base_url || '';
      el('afDesc').value = FLOW.description || '';
      el('afCron').value = FLOW.schedule_cron || '';
      el('afTz').value = FLOW.timezone || 'UTC';
      el('afEnabled').checked = !!FLOW.enabled;
    }
    // Device segmented
    el('afDevice').querySelectorAll('button').forEach(function (b) {
      b.classList.toggle('is-active', b.getAttribute('data-device') === STATE.device);
      b.addEventListener('click', function () {
        STATE.device = b.getAttribute('data-device');
        el('afDevice').querySelectorAll('button').forEach(function (x) {
          x.classList.toggle('is-active', x === b);
        });
      });
    });
    // Schedule presets
    el('afPresets').addEventListener('click', function (ev) {
      var p = ev.target.closest('.af-preset');
      if (!p) return;
      el('afCron').value = p.getAttribute('data-cron');
      highlightPreset();
    });
    el('afCron').addEventListener('input', highlightPreset);
    el('afClearCron').addEventListener('click', function () { el('afCron').value = ''; highlightPreset(); });
    highlightPreset();
    initGroups();
    initNotify();
  }
  function highlightPreset() {
    var cur = el('afCron').value.trim();
    el('afPresets').querySelectorAll('.af-preset').forEach(function (p) {
      p.classList.toggle('is-active', p.getAttribute('data-cron') === cur);
    });
  }

  // Groups tag input
  function renderGroups() {
    el('afGroups').innerHTML = STATE.groups.map(function (g, i) {
      return '<span class="af-chip">' + esc(g) + '<button type="button" data-i="' + i + '">&times;</button></span>';
    }).join('') || '<span class="af-dim" style="font-size:12px;">No groups</span>';
  }
  function initGroups() {
    renderGroups();
    el('afGroups').addEventListener('click', function (ev) {
      var b = ev.target.closest('button[data-i]');
      if (!b) return;
      STATE.groups.splice(parseInt(b.getAttribute('data-i'), 10), 1);
      renderGroups();
    });
    el('afGroupInput').addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ',') {
        ev.preventDefault();
        var v = this.value.trim().replace(/,$/, '').trim();
        if (v && STATE.groups.indexOf(v) === -1) { STATE.groups.push(v); renderGroups(); }
        this.value = '';
      }
    });
  }

  // Notifications
  function initNotify() {
    var slackOn = el('afSlackOn'), emailOn = el('afEmailOn');
    var initSlack = (STATE.notify.slack_webhook_ids || []);
    var initSenderId = STATE.notify.email_sender_id || '';
    var initRecipients = STATE.notify.recipients || [];

    slackOn.checked = initSlack.length > 0;
    emailOn.checked = !!initSenderId;
    el('afSlackWrap').hidden = !slackOn.checked;
    el('afEmailWrap').hidden = !emailOn.checked;
    el('afRecipients').value = initRecipients.join(', ');

    slackOn.addEventListener('change', function () {
      el('afSlackWrap').hidden = !this.checked;
      if (this.checked && WEBHOOKS === null) loadWebhooks();
    });
    emailOn.addEventListener('change', function () {
      el('afEmailWrap').hidden = !this.checked;
      if (this.checked && SENDERS === null) loadSenders();
    });

    if (slackOn.checked) loadWebhooks();
    if (emailOn.checked) loadSenders();
  }

  function loadWebhooks() {
    if (!PROJECT_SLUG) { renderSlackFallback(); return; }
    jfetch('/api/project/' + encodeURIComponent(PROJECT_SLUG) + '/slack-webhooks')
      .then(function (data) { WEBHOOKS = (data && data.webhooks) || []; renderSlackList(); })
      .catch(function () { WEBHOOKS = false; renderSlackFallback(); });
  }
  function renderSlackList() {
    var sel = STATE.notify.slack_webhook_ids || [];
    if (!WEBHOOKS.length) {
      el('afSlackList').innerHTML = '<div class="af-empty-hint">No Slack webhooks configured. Add them in Project settings → Notifications.</div>';
      el('afSlackHint').textContent = '';
      return;
    }
    el('afSlackList').innerHTML = WEBHOOKS.map(function (w) {
      return '<label style="display:flex;align-items:center;gap:8px;margin:6px 0;font-size:13px;">' +
        '<input type="checkbox" class="af-wh" value="' + esc(w.id) + '"' + (sel.indexOf(w.id) !== -1 ? ' checked' : '') + '/>' +
        esc(w.label || w.id) + '</label>';
    }).join('');
    el('afSlackHint').textContent = 'Notified on failing/error and on recovery.';
  }
  function renderSlackFallback() {
    var sel = STATE.notify.slack_webhook_ids || [];
    el('afSlackList').innerHTML = '<input type="text" id="afSlackFree" class="af-input af-input-sm mono" value="' +
      esc(sel.join(', ')) + '" placeholder="webhook id, webhook id"/>';
    el('afSlackHint').textContent = 'Enter configured Slack webhook IDs, comma-separated.';
  }

  function loadSenders() {
    if (!PROJECT_SLUG) { renderSenderFallback(); return; }
    jfetch('/api/project/' + encodeURIComponent(PROJECT_SLUG) + '/email-senders')
      .then(function (data) { SENDERS = (data && data.senders) || []; renderSenderSelect(); })
      .catch(function () { SENDERS = false; renderSenderFallback(); });
  }
  function renderSenderSelect() {
    var sel = el('afEmailSender');
    var cur = STATE.notify.email_sender_id || '';
    if (!SENDERS.length) {
      sel.outerHTML = '<div class="af-empty-hint" id="afEmailSender">No email senders configured. Add one in Project settings → Notifications.</div>';
      return;
    }
    sel.innerHTML = '<option value="">— select sender —</option>' + SENDERS.map(function (s) {
      var label = (s.label || s.from_address || s.id);
      return '<option value="' + esc(s.id) + '"' + (s.id === cur ? ' selected' : '') + '>' + esc(label) + '</option>';
    }).join('');
  }
  function renderSenderFallback() {
    var sel = el('afEmailSender');
    var cur = STATE.notify.email_sender_id || '';
    sel.outerHTML = '<input type="text" id="afEmailSender" class="af-input mono" value="' + esc(cur) + '" placeholder="email sender id"/>';
  }

  function collectNotify() {
    var notify = {};
    if (el('afSlackOn').checked) {
      var ids = [];
      if (WEBHOOKS && WEBHOOKS.length) {
        el('afSlackList').querySelectorAll('.af-wh:checked').forEach(function (c) { ids.push(c.value); });
      } else {
        var free = el('afSlackFree');
        if (free) ids = free.value.split(',').map(function (x) { return x.trim(); }).filter(Boolean);
        else ids = (STATE.notify.slack_webhook_ids || []);
      }
      if (ids.length) notify.slack_webhook_ids = ids;
    }
    if (el('afEmailOn').checked) {
      var senderEl = el('afEmailSender');
      var senderId = senderEl && senderEl.value ? senderEl.value.trim() : '';
      var recipients = el('afRecipients').value.split(',').map(function (x) { return x.trim(); }).filter(Boolean);
      if (senderId) notify.email_sender_id = senderId;
      if (recipients.length) notify.recipients = recipients;
    }
    return notify;
  }

  /* ==========================================================
     Serialize + save
     ========================================================== */
  function isHttp(u) { return /^https?:\/\//i.test(u); }

  function serialize() {
    syncSelectedStepFromDOM();
    var name = el('afName').value.trim();
    var base_url = el('afBaseUrl').value.trim();
    var errs = [];
    if (!name) errs.push('Name is required.');
    if (!isHttp(base_url)) errs.push('Base URL must be an http(s) URL.');

    var steps = STATE.steps.map(function (s, i) {
      var out = { action: s.action };
      if (s.label && s.label.trim()) out.label = s.label.trim();
      if (s.action === 'navigate') {
        var u = (s.url || '').trim();
        if (u) {
          if (/^[a-z][a-z0-9+.-]*:\/\//i.test(u) && !isHttp(u)) errs.push('Step ' + (i + 1) + ': navigate URL must be http(s).');
          out.url = u;
        } else if (!base_url) {
          errs.push('Step ' + (i + 1) + ': navigate needs a URL when there is no base URL.');
        }
      } else if (s.action === 'click') {
        if (!s.selector.trim()) errs.push('Step ' + (i + 1) + ': click needs a selector.');
        out.selector = s.selector.trim();
      } else if (s.action === 'type') {
        if (!s.selector.trim()) errs.push('Step ' + (i + 1) + ': type needs a selector.');
        out.selector = s.selector.trim();
        out.text = s.text || '';
      } else if (s.action === 'wait') {
        var ms = parseInt(s.ms, 10);
        if (isNaN(ms) || ms < 0) errs.push('Step ' + (i + 1) + ': wait ms must be >= 0.');
        out.ms = Math.min(Math.max(ms || 0, 0), 30000);
      }
      var assertions = serializeAssertions(s.assertions, i, errs);
      if (assertions) out.assertions = assertions;
      return out;
    });

    var payload = {
      name: name,
      description: el('afDesc').value.trim() || null,
      device: STATE.device,
      base_url: base_url,
      steps: steps,
      schedule_cron: el('afCron').value.trim() || null,
      timezone: el('afTz').value.trim() || 'UTC',
      notify: collectNotify(),
      groups: STATE.groups,
      enabled: el('afEnabled').checked
    };
    return { payload: payload, errs: errs };
  }

  function serializeAssertions(a, stepIdx, errs) {
    var out = {};
    var dl = (a.datalayer_events || []).filter(function (e) { return (e.event || '').trim(); }).map(function (e) {
      return {
        event: e.event.trim(), mode: e.mode, when: e.when,
        fields: cleanChecks(e.fields)
      };
    });
    // Flag events left blank.
    (a.datalayer_events || []).forEach(function (e) {
      if (!(e.event || '').trim() && (e.fields || []).length) errs.push('Step ' + (stepIdx + 1) + ': a dataLayer check is missing an event name.');
    });
    var vr = (a.vendor_requests || []).filter(function (v) { return v.vendor_id; }).map(function (v) {
      return {
        vendor_id: v.vendor_id, mode: v.mode, when: v.when,
        params: cleanChecks(v.params)
      };
    });
    (a.vendor_requests || []).forEach(function (v) {
      if (!v.vendor_id) errs.push('Step ' + (stepIdx + 1) + ': a vendor check has no vendor selected.');
    });
    if (dl.length) out.datalayer_events = dl;
    if (vr.length) out.vendor_requests = vr;
    return (dl.length || vr.length) ? out : null;
  }
  function cleanChecks(checks) {
    return (checks || []).filter(function (c) { return (c.key || '').trim(); }).map(function (c) {
      var out = { key: c.key.trim(), op: c.op || 'equals' };
      if (c.value !== '' && c.value != null) out.value = c.value;
      return out;
    });
  }

  el('afSaveBtn').addEventListener('click', function () {
    var r = serialize();
    if (r.errs.length) { toast(r.errs[0], 'error'); return; }
    var btn = this;
    btn.disabled = true;
    var editing = !!STATE.id;
    var url = editing ? '/api/audit/flows/' + STATE.id : '/api/audit/flows';
    jfetch(url, {
      method: editing ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(r.payload)
    }).then(function () {
      toast('Flow saved');
      window.location.href = '/audits/flows';
    }).catch(function (e) {
      btn.disabled = false;
      toast(e.message, 'error');
    });
  });

  /* ==========================================================
     Boot
     ========================================================== */
  if (!STATE.steps.length) STATE.steps.push(emptyStep());
  renderRail();
  renderEditor();
  initTopFields();
})();
