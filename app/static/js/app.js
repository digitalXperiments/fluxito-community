/* Fluxito — shared client JS */
(function () {
  'use strict';

  window.Fluxito = window.Fluxito || {};

  // ---------- Mobile nav ----------
  function initNav() {
    const toggle = document.querySelector('.nav-toggle');
    const links = document.querySelector('.nav-links');
    if (!toggle || !links) return;
    toggle.addEventListener('click', () => {
      links.classList.toggle('is-open');
      const expanded = links.classList.contains('is-open');
      toggle.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    });
    document.addEventListener('click', (e) => {
      if (!links.contains(e.target) && !toggle.contains(e.target)) {
        links.classList.remove('is-open');
      }
    });
  }

  // ---------- Toasts (improved — top-right, with icon, longer duration) ----------
  var _svgCheck = '<svg class="toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
  var _svgX = '<svg class="toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>';

  function toast(msg, kind) {
    let host = document.querySelector('.toast-host');
    if (!host) {
      host = document.createElement('div');
      host.className = 'toast-host';
      document.body.appendChild(host);
    }
    const el = document.createElement('div');
    el.className = 'toast ' + (kind === 'error' ? 'is-error' : 'is-success');
    el.innerHTML = (kind === 'error' ? _svgX : _svgCheck) + '<span>' + _escHtml(msg) + '</span>';
    host.appendChild(el);
    setTimeout(() => {
      el.style.transition = 'opacity .25s, transform .25s';
      el.style.opacity = '0';
      el.style.transform = 'translateX(24px)';
      setTimeout(() => el.remove(), 250);
    }, 4000);
  }

  function _escHtml(s) {
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }
  window.Fluxito.toast = toast;

  // ---------- Copy to clipboard ----------
  window.Fluxito.copy = async function (text, label) {
    try {
      await navigator.clipboard.writeText(text);
      toast((label || 'Copied') + ' to clipboard');
    } catch (e) {
      toast('Copy failed', 'error');
    }
  };

  // ---------- Prompt Box Copy ----------
  window.copyHubPrompt = function (btn, customText) {
    var box = btn.closest('.lh-prompt-box') || btn.closest('.fx-prompt-box') || btn.closest('.fx-slab');
    var promptEl = box ? box.querySelector('.fx-q') : null;
    var rawText = customText || (promptEl ? promptEl.innerText : '');
    var text = rawText.replace(/^[\s“"]+|[\s”"]+$/g, '').trim();
    if (!text) return;

    var labelEl = btn.querySelector('[data-label]');
    var original = labelEl ? labelEl.textContent : btn.textContent;

    function reset() {
      if (labelEl) labelEl.textContent = original;
      else btn.textContent = original;
    }

    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function() {
        if (labelEl) labelEl.textContent = 'Copied';
        else btn.textContent = 'Copied';
        if (window.Fluxito && window.Fluxito.toast) {
          window.Fluxito.toast('Prompt copied — ready for your AI agent');
        }
        setTimeout(reset, 2000);
      }).catch(function() {
        if (window.Fluxito && window.Fluxito.copy) {
          window.Fluxito.copy(text, 'Prompt');
        }
      });
    } else {
      if (window.Fluxito && window.Fluxito.copy) {
        window.Fluxito.copy(text, 'Prompt');
      }
    }
  };

  // ---------- Kebab (three-dot) menu ----------
  window.Fluxito.toggleKebab = function (e) {
    e.preventDefault();
    e.stopPropagation();
    var menu = e.currentTarget.nextElementSibling;
    var isOpen = menu.classList.contains('is-open');
    // close any other open menus first
    document.querySelectorAll('.kebab-menu.is-open').forEach(function (m) { m.classList.remove('is-open'); });
    if (!isOpen) menu.classList.add('is-open');
  };
  // close menus on outside click
  document.addEventListener('click', function (e) {
    if (!e.target.closest('.kebab-wrap')) {
      document.querySelectorAll('.kebab-menu.is-open').forEach(function (m) { m.classList.remove('is-open'); });
    }
  });

  // ---------- Confirm delete ----------
  window.Fluxito.confirmDelete = async function (url, msg, onOk) {
    if (!window.confirm(msg || 'Are you sure you want to delete this?')) return;
    try {
      const res = await fetch(url, { method: 'DELETE', credentials: 'same-origin' });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        toast(err.error || 'Delete failed', 'error');
        return;
      }
      toast('Deleted');
      if (typeof onOk === 'function') onOk();
      else setTimeout(() => window.location.reload(), 400);
    } catch (e) {
      toast('Network error', 'error');
    }
  };

  // ---------- Time ago ----------
  window.Fluxito.timeAgo = function (iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    const s = Math.floor((Date.now() - d.getTime()) / 1000);
    if (s < 60) return 'just now';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ago';
    if (s < 604800) return Math.floor(s / 86400) + 'd ago';
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  };

  // ---------- Auto refresh dashboards ----------
  window.Fluxito.autoRefresh = function (config) {
    const intervalMs = (config.intervalSec || 60) * 1000;
    const endpoint = config.endpoint;
    const onData = config.onData;
    if (!endpoint || !onData) return;

    async function tick() {
      try {
        const res = await fetch(endpoint, { credentials: 'same-origin' });
        if (res.ok) {
          const data = await res.json();
          onData(data);
        }
      } catch (e) { /* silent */ }
    }
    const id = setInterval(tick, intervalMs);
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) clearInterval(id);
    });
    return id;
  };

  // ---------- Notifications ----------
  var _notifCategoryIcons = {
    connection: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>',
    dashboard: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
    billing: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="1" y="4" width="22" height="16" rx="2"/><line x1="1" y1="10" x2="23" y2="10"/></svg>',
    system: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>'
  };

  // Notifications popover — markup lives in base.html next to the sidebar
  // bell (#notifGroup / .sidebar-bell). Open/close + fixed positioning
  // (anchored above the bell) is handled by the shared [data-nav-group]
  // script in base.html; this only loads/renders data and wires
  // mark-read behavior. We watch the group's class via MutationObserver
  // instead of hooking the toggle button's click directly, so this stays
  // correct regardless of listener registration order between the two
  // inline scripts.
  function initNotifications() {
    var group = document.getElementById('notifGroup');
    var dot = document.getElementById('notifDot');
    var list = document.getElementById('notifList');
    var markAllBtn = document.getElementById('notifMarkAll');
    if (!group || !list) return;

    var loadedOnce = false;

    // Load notifications (called each time the popover opens)
    async function loadNotifications() {
      try {
        var res = await fetch('/api/notifications?limit=8', { credentials: 'same-origin' });
        if (!res.ok) return;
        var data = await res.json();
        _updateDot(data.unread_count);
        _renderNotifications(data.notifications || []);
        loadedOnce = true;
      } catch (e) { /* silent */ }
    }

    function _renderNotifications(notifs) {
      if (!notifs || notifs.length === 0) {
        list.innerHTML = '<div class="notif-empty">'
          + '<div class="notif-empty-icon"><svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="color:var(--muted)"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg></div>'
          + '<p class="notif-empty-title">All caught up</p>'
          + '<p class="notif-empty-sub">Test-flow alerts and system events will appear here.</p>'
          + '</div>';
        return;
      }
      var html = '';
      for (var i = 0; i < notifs.length; i++) {
        var n = notifs[i];
        var icon = _notifCategoryIcons[n.category] || _notifCategoryIcons.system;
        var cls = n.is_read ? '' : ' is-unread';
        var dotEl = n.is_read ? '' : '<div class="notif-item-dot"></div>';
        html += '<' + (n.action_url ? 'a href="' + _escHtml(n.action_url) + '"' : 'div') + ' class="notif-item' + cls + '" data-id="' + n.id + '">'
             + '<div class="notif-item-icon is-' + n.severity + '">' + icon + '</div>'
             + '<div class="notif-item-body">'
             + '<div class="notif-item-title"><strong>' + _escHtml(n.title) + '</strong> — ' + _escHtml(n.message) + '</div>'
             + '<div class="notif-item-time">' + Fluxito.timeAgo(n.created_at) + '</div>'
             + '</div>' + dotEl + '</' + (n.action_url ? 'a' : 'div') + '>';
      }
      list.innerHTML = html;

      // Mark-as-read on click (fire-and-forget; navigation via the anchor's
      // own href happens natively, no need to intercept it).
      list.querySelectorAll('.notif-item.is-unread').forEach(function (el) {
        el.addEventListener('click', function () {
          var id = el.getAttribute('data-id');
          el.classList.remove('is-unread');
          var d = el.querySelector('.notif-item-dot');
          if (d) d.remove();
          fetch('/api/notifications/' + id + '/read', { method: 'POST', credentials: 'same-origin' }).catch(function () {});
          _refreshDotFromDom();
        }, { once: true });
      });
    }

    function _refreshDotFromDom() {
      var remaining = list.querySelectorAll('.notif-item.is-unread').length;
      _updateDot(remaining);
    }

    function _updateDot(count) {
      if (!dot) return;
      dot.style.display = count > 0 ? '' : 'none';
    }

    if (markAllBtn) {
      markAllBtn.addEventListener('click', async function () {
        try {
          await fetch('/api/notifications/read-all', { method: 'POST', credentials: 'same-origin' });
          list.querySelectorAll('.notif-item.is-unread').forEach(function (el) {
            el.classList.remove('is-unread');
            var d = el.querySelector('.notif-item-dot');
            if (d) d.remove();
          });
          _updateDot(0);
          toast('All notifications marked as read');
        } catch (e) { /* silent */ }
      });
    }

    // Reload every time the popover is opened (cheap — limit=8)
    var mo = new MutationObserver(function () {
      if (group.classList.contains('is-open')) loadNotifications();
    });
    mo.observe(group, { attributes: true, attributeFilter: ['class'] });

    // Initial unread-count check (dot only, no fetch of full list)
    (async function () {
      try {
        var res = await fetch('/api/notifications/count', { credentials: 'same-origin' });
        if (res.ok) {
          var data = await res.json();
          _updateDot(data.unread_count);
        }
      } catch (e) { /* silent */ }
    })();

    // Poll for new notifications every 60s (dot only; full list reloads on open)
    setInterval(async function () {
      if (document.hidden) return;
      try {
        var res = await fetch('/api/notifications/count', { credentials: 'same-origin' });
        if (res.ok) {
          var data = await res.json();
          _updateDot(data.unread_count);
        }
      } catch (e) { /* silent */ }
    }, 60000);
  }

  // ---------- Avatar dropdown ----------
  function initAvatarDropdown() {
    var toggle = document.getElementById('avatarToggle');
    var dropdown = document.getElementById('avatarDropdown');
    if (!toggle || !dropdown) return;

    toggle.addEventListener('click', function (e) {
      e.stopPropagation();
      // Close notification dropdown if open
      var notifDd = document.getElementById('notifDropdown');
      if (notifDd) notifDd.classList.remove('is-open');
      dropdown.classList.toggle('is-open');
    });

    document.addEventListener('click', function (e) {
      if (!e.target.closest('.avatar-wrap')) {
        dropdown.classList.remove('is-open');
      }
    });
  }

  // ---------- init ----------
  function init() {
    initNav();
    initNotifications();
    initAvatarDropdown();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

/* ── Collapsible sidebar: toggle, persist, and label tooltips ─────────── */
(function () {
  var btn = document.getElementById('sidebarCollapse');
  if (!btn) return;
  var root = document.documentElement;

  // In collapsed mode the labels are hidden; expose them as native tooltips.
  function syncTitles() {
    document.querySelectorAll('.sidebar-item').forEach(function (el) {
      // Exclude badge counters (e.g. the tasks count) from the
      // label text so the tooltip/collapsed-mode title stays a clean label
      // instead of "Label\n  5".
      var badge = el.querySelector('.sidebar-badge');
      var t;
      if (badge) {
        var clone = el.cloneNode(true);
        var badgeClone = clone.querySelector('.sidebar-badge');
        if (badgeClone) badgeClone.remove();
        t = (clone.textContent || '').trim();
      } else {
        t = (el.textContent || '').trim();
      }
      t = t.replace(/\s+/g, ' ');
      if (!t) return;
      // data-label feeds the collapsed-rail CSS tooltip only — no `title`
      // attribute, so the expanded sidebar doesn't grow stray native tooltips.
      if (!el.getAttribute('data-label')) el.setAttribute('data-label', t);
    });
  }
  syncTitles();

  function label(collapsed) {
    btn.setAttribute('aria-label', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
    btn.setAttribute('title', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
  }
  label(root.classList.contains('sidebar-collapsed'));

  btn.addEventListener('click', function () {
    var collapsed = root.classList.toggle('sidebar-collapsed');
    try { localStorage.setItem('fx-sidebar-collapsed', collapsed ? '1' : '0'); } catch (e) {}
    label(collapsed);
  });
})();

/* ⌘K / Ctrl+K is handled by the command palette (partials/command_palette.html). */

/* ── Shared UI helpers for the redesigned pages (ui-* components) ─────────
   Fluxito.ui.drawer(html)  → right-hand drawer; returns the drawer element
   Fluxito.ui.modal(html)   → centred modal; returns the modal element
   Fluxito.ui.menu(anchor, html) → small popover menu under/over `anchor`
   Fluxito.ui.close()       → closes whatever is open (also Esc / scrim click)
   Elements with [data-ui-close] inside any of them close it on click.
   Elements with [data-copy="text"] anywhere copy their text on click.   */
(function () {
  window.Fluxito = window.Fluxito || {};
  var scrim, drawer, modal, menu;
  function ensure() {
    if (scrim) return;
    scrim = document.createElement('div'); scrim.className = 'ui-scrim'; scrim.hidden = true;
    drawer = document.createElement('aside'); drawer.className = 'ui-drawer'; drawer.setAttribute('role', 'dialog'); drawer.setAttribute('aria-modal', 'true'); drawer.hidden = true;
    modal = document.createElement('div'); modal.className = 'ui-modal'; modal.setAttribute('role', 'dialog'); modal.setAttribute('aria-modal', 'true'); modal.hidden = true;
    menu = document.createElement('div'); menu.className = 'ui-menu'; menu.setAttribute('role', 'menu'); menu.hidden = true;
    document.body.appendChild(scrim); document.body.appendChild(drawer); document.body.appendChild(modal); document.body.appendChild(menu);
    scrim.addEventListener('click', close);
    [drawer, modal, menu].forEach(function (el) {
      el.addEventListener('click', function (e) { if (e.target.closest('[data-ui-close]')) close(); });
    });
  }
  function close() {
    if (!scrim) return;
    scrim.hidden = true; drawer.hidden = true; modal.hidden = true; menu.hidden = true;
    scrim.classList.remove('is-clear');
    document.documentElement.classList.remove('ui-locked');
  }
  function openIn(el, html, lock) {
    ensure(); close();
    el.innerHTML = html; el.hidden = false; scrim.hidden = false;
    if (lock) document.documentElement.classList.add('ui-locked');
    var f = el.querySelector('[autofocus], input, select, textarea, button');
    if (f) setTimeout(function () { try { f.focus(); } catch (e) {} }, 30);
    return el;
  }
  function openMenu(anchor, html) {
    ensure(); close();
    menu.innerHTML = html; menu.hidden = false; scrim.hidden = false; scrim.classList.add('is-clear');
    var r = anchor.getBoundingClientRect(), w = menu.offsetWidth, h = menu.offsetHeight;
    var left = Math.min(Math.max(8, r.right - w), window.innerWidth - w - 8);
    var top = r.bottom + 6 + h > window.innerHeight ? Math.max(8, r.top - h - 6) : r.bottom + 6;
    menu.style.left = left + 'px'; menu.style.top = top + 'px';
    return menu;
  }
  window.Fluxito.ui = {
    drawer: function (html) { return openIn(drawer || (ensure(), drawer), html, true); },
    modal: function (html) { return openIn(modal || (ensure(), modal), html, true); },
    menu: openMenu,
    close: close
  };
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });
  document.addEventListener('click', function (e) {
    var c = e.target.closest('[data-copy]');
    if (!c || !window.Fluxito.copy || c.closest('.lp')) return;  // landing.js handles its own copy buttons
    e.preventDefault();
    window.Fluxito.copy(c.getAttribute('data-copy'), c.getAttribute('data-copy-label') || 'Copied');
  });
})();
