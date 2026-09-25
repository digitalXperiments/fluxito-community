/* integrations.js — OAuth app credentials management UI
   Vanilla JS, no framework. Relies on Fluxito.toast() from app.js and
   the CSRF auto-header injected by base.html.

   Drives two pages. An element with data-intg-api picks the API and mode:
     Admin → OAuth Apps (install-wide):  default, /api/integrations
     Project settings → OAuth apps:      data-intg-mode="project",
                                         data-intg-api="/api/project/<slug>/oauth-apps"
   In project mode "own" credentials have source 'project'; 'db' means the
   project falls back to the install's app. */

(function () {
  'use strict';

  // Logo + display name per platform (mirrors platform_meta in integrations.html)
  var META = {
    google:    { logo: 'ga4.svg',       name: 'Google' },
    meta:      { logo: 'meta.svg',      name: 'Meta' },
    tiktok:    { logo: 'tiktok.svg',    name: 'TikTok' },
    snap:      { logo: 'snapchat.svg',  name: 'Snapchat' },
    linkedin:  { logo: 'linkedin.svg',  name: 'LinkedIn' },
    pinterest: { logo: 'pinterest.svg', name: 'Pinterest' },
    reddit:    { logo: 'reddit.svg',    name: 'Reddit' },
    x:         { logo: 'x.svg',         name: 'X' },
    bing:      { logo: 'microsoft.svg', name: 'Microsoft' },
    apple:     { logo: 'apple.svg',     name: 'Apple' },
  };

  var ROOT = document.querySelector('[data-intg-api]');
  var API = (ROOT && ROOT.getAttribute('data-intg-api')) || '/api/integrations';
  var PROJECT = !!(ROOT && ROOT.getAttribute('data-intg-mode') === 'project');
  var OWN = PROJECT ? 'project' : 'db';  // source value of credentials this page manages

  var _currentPlatform = null;
  var _currentSource = null;

  // ── Refresh card state from API ─────────────────────────────────────────

  function loadIntegrations() {
    fetch(API, { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var items = data.items || [];
        items.forEach(function (item) { _updateCard(item); });
      })
      .catch(function (err) {
        console.warn('integrations refresh failed', err);
      });
  }

  function _updateCard(item) {
    var card = document.getElementById('intg-card-' + item.platform);
    if (!card) return;
    card.setAttribute('data-source', item.source || 'unconfigured');

    var badge = card.querySelector('.intg-status-row');
    if (badge) {
      badge.innerHTML = _statusBadge(item.source);
    }
    var idRow = card.querySelector('.intg-id-row');
    if (idRow) {
      if (item.client_id_masked) {
        idRow.innerHTML = '<span class="intg-id-label">Client ID</span><span class="intg-id-value">' +
          _esc(item.client_id_masked) + '</span>';
        idRow.style.display = '';
      } else {
        idRow.style.display = 'none';
      }
    }
    var updated = card.querySelector('.intg-updated');
    if (updated) {
      updated.textContent = (item.source === OWN && item.updated_at)
        ? 'Updated ' + _fmtDate(item.updated_at)
        : ' ';
    }
    // Remove only applies to credentials this page owns; the primary action
    // reads "Edit" once configured, "Add credentials" / "Use your own app" otherwise.
    var removeBtn = card.querySelector('.intg-remove-btn');
    if (removeBtn) removeBtn.style.display = item.source === OWN ? '' : 'none';
    var editBtn = card.querySelector('.intg-edit-btn');
    if (editBtn) {
      var configured = PROJECT ? item.source === OWN : (item.source === 'db' || item.source === 'env');
      editBtn.textContent = configured ? 'Edit' : (PROJECT ? 'Use your own app' : 'Add credentials');
      editBtn.classList.toggle('is-quiet', configured);
    }
  }

  function _statusBadge(source) {
    if (PROJECT) {
      if (source === 'project') return '<span class="ui-chip is-good intg-badge">Your app</span>';
      if (source === 'db') return '<span class="ui-chip is-info intg-badge">Fluxito\'s app</span>';
      return '<span class="ui-chip intg-badge intg-badge-none">Not available</span>';
    }
    if (source === 'db') return '<span class="ui-chip is-good intg-badge intg-badge-db">Configured (DB)</span>';
    if (source === 'env') return '<span class="ui-chip is-info intg-badge intg-badge-env">Configured (env)</span>';
    return '<span class="ui-chip intg-badge intg-badge-none">Not configured</span>';
  }

  // ── Modal open/close ────────────────────────────────────────────────────

  function openModal(platform) {
    _currentPlatform = platform;
    _currentSource = null;

    // Reset form
    var form = document.getElementById('intgForm');
    if (form) form.reset();
    _setTestResult(null);

    // Show google dev token field only for google
    var devWrap = document.getElementById('intgDevTokenWrap');
    if (devWrap) devWrap.style.display = platform === 'google' ? '' : 'none';

    var m = META[platform] || { logo: '', name: _capitalize(platform) };
    var badgeEl = document.getElementById('intgModalBadge');
    if (badgeEl) {
      badgeEl.innerHTML = m.logo ? '<img src="/static/img/logos/' + m.logo + '" alt="">' : '';
    }
    var titleEl = document.getElementById('intgModalTitle');
    if (titleEl) titleEl.textContent = m.name + (PROJECT ? ' · your own OAuth app' : ' · OAuth app credentials');

    // Fetch platform detail and populate redirect URIs
    fetch(API + '/' + platform, { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        _currentSource = data.source || 'unconfigured';

        // Redirect URI list
        var uriEl = document.getElementById('intgRedirectUris');
        if (uriEl) {
          uriEl.innerHTML = (data.redirect_uris || []).map(function (u) {
            return '<div class="intg-uri-row">' +
              '<code class="intg-uri-code">' + _esc(u) + '</code>' +
              '<button type="button" class="intg-copy-btn" onclick="Fluxito.copy(' + JSON.stringify(u) + ',\'URI\')" aria-label="Copy URI">' +
              '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>' +
              '</button></div>';
          }).join('');
        }

        // Console link
        var consoleLink = document.getElementById('intgConsoleLink');
        if (consoleLink) consoleLink.href = data.dev_console_url || '#';

        // Setup guide link
        var guideLink = document.getElementById('intgGuideLink');
        var guideAnchor = document.getElementById('intgGuideAnchor');
        var slugs = data.tutorial_slugs || [];
        if (guideLink && guideAnchor && slugs.length) {
          guideAnchor.href = '/tutorials/' + slugs[0];
          guideLink.style.display = '';
        } else if (guideLink) {
          guideLink.style.display = 'none';
        }

        // Remove button in footer
        var removeBtn = document.getElementById('intgRemoveBtn');
        if (removeBtn) removeBtn.style.display = _currentSource === OWN ? '' : 'none';
      })
      .catch(function (err) {
        Fluxito.toast('Could not load platform details', 'error');
        console.error(err);
      });

    // Show modal
    var backdrop = document.getElementById('intgModalBackdrop');
    if (backdrop) backdrop.classList.add('is-open');
    document.body.style.overflow = 'hidden';
  }

  function closeModal() {
    var backdrop = document.getElementById('intgModalBackdrop');
    if (backdrop) backdrop.classList.remove('is-open');
    document.body.style.overflow = '';
    _currentPlatform = null;
    _currentSource = null;
  }

  // ── Save ────────────────────────────────────────────────────────────────

  function handleSave(event) {
    event.preventDefault();
    if (!_currentPlatform) return;
    var form = event.target;
    var clientId = (form.elements.client_id && form.elements.client_id.value || '').trim();
    var clientSecret = (form.elements.client_secret && form.elements.client_secret.value || '').trim();
    var devToken = (form.elements.developer_token && form.elements.developer_token.value || '').trim();

    var body = { client_id: clientId, client_secret: clientSecret };
    if (_currentPlatform === 'google' && devToken) {
      body.extra = { developer_token: devToken };
    }

    var saveBtn = document.getElementById('intgSaveBtn');
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = 'Saving…'; }

    fetch(API + '/' + _currentPlatform, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (e) { throw new Error(e.detail || 'Save failed'); });
        return r.json();
      })
      .then(function (data) {
        Fluxito.toast(_capitalize(_currentPlatform) + ' credentials saved' + _reconnectNote(data));
        closeModal();
        loadIntegrations();
      })
      .catch(function (err) {
        Fluxito.toast(err.message || 'Save failed', 'error');
      })
      .finally(function () {
        if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = 'Save'; }
      });
  }

  // ── Test ────────────────────────────────────────────────────────────────

  function testCredentials() {
    if (!_currentPlatform) return;
    _setTestResult({ loading: true });

    fetch(API + '/' + _currentPlatform + '/test', {
      method: 'POST',
      credentials: 'same-origin',
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        _setTestResult({ ok: data.ok, issues: data.issues || [] });
      })
      .catch(function (err) {
        _setTestResult({ ok: false, issues: [err.message || 'Test request failed'] });
      });
  }

  function _setTestResult(result) {
    var el = document.getElementById('intgTestResult');
    if (!el) return;
    if (!result) { el.style.display = 'none'; el.innerHTML = ''; return; }
    el.style.display = '';
    if (result.loading) {
      el.className = 'intg-test-result intg-test-loading';
      el.textContent = 'Testing…';
      return;
    }
    el.className = 'intg-test-result ' + (result.ok ? 'intg-test-ok' : 'intg-test-fail');
    var lines = result.issues && result.issues.length
      ? result.issues.map(function (i) { return '<li>' + _esc(i) + '</li>'; }).join('')
      : '';
    el.innerHTML = (result.ok ? '&#10003; Credentials look valid' : '&#10007; Issues found') +
      (lines ? '<ul class="intg-test-issues">' + lines + '</ul>' : '');
  }

  // ── Remove ──────────────────────────────────────────────────────────────

  function _removePrompt(platform) {
    return PROJECT
      ? 'Stop using your own ' + _capitalize(platform) + ' app? This project goes back to Fluxito\'s app, and existing ' +
        _capitalize(platform) + ' connections here will need to be reconnected.'
      : 'Remove ' + _capitalize(platform) + ' credentials from the database? The env fallback will resume if set.';
  }

  function _reconnectNote(data) {
    var n = data && data.reconnect_count;
    return n ? ' · ' + n + ' connection' + (n === 1 ? '' : 's') + ' need reconnecting' : '';
  }

  function removeCredentials(platform) {
    if (!window.confirm(_removePrompt(platform))) return;
    _doRemove(platform, false);
  }

  function removeFromModal() {
    if (!_currentPlatform) return;
    if (!window.confirm(_removePrompt(_currentPlatform))) return;
    _doRemove(_currentPlatform, true);
  }

  function _doRemove(platform, closeAfter) {
    fetch(API + '/' + platform, { method: 'DELETE', credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (e) { throw new Error(e.detail || 'Remove failed'); });
        return r.json();
      })
      .then(function (data) {
        Fluxito.toast(_capitalize(platform) + ' credentials removed' + _reconnectNote(data));
        if (closeAfter) closeModal();
        loadIntegrations();
      })
      .catch(function (err) {
        Fluxito.toast(err.message || 'Remove failed', 'error');
      });
  }

  // ── Helpers ─────────────────────────────────────────────────────────────

  function toggleSecret() {
    var inp = document.getElementById('intgClientSecret');
    if (!inp) return;
    inp.type = inp.type === 'password' ? 'text' : 'password';
  }

  function _capitalize(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : s; }

  function _esc(s) {
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  function _fmtDate(iso) {
    try {
      var d = new Date(iso);
      return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
    } catch (_) { return iso; }
  }

  // Close modal on backdrop click
  document.addEventListener('click', function (e) {
    var backdrop = document.getElementById('intgModalBackdrop');
    if (backdrop && e.target === backdrop) closeModal();
  });

  // Close on Escape
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeModal();
  });

  // Refresh on load
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', loadIntegrations);
  } else {
    loadIntegrations();
  }

  // Public API
  window.IntgUI = {
    openModal: openModal,
    closeModal: closeModal,
    handleSave: handleSave,
    testCredentials: testCredentials,
    removeCredentials: removeCredentials,
    removeFromModal: removeFromModal,
    toggleSecret: toggleSecret,
  };
})();
