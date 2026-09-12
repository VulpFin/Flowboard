/* SPDX-License-Identifier: AGPL-3.0-or-later  Copyright (C) 2025-2026 TG11 */
(function () {
  const csrf = document.body.dataset.csrf;

  function boardSlug() {
    const b = document.getElementById('board');
    return b ? b.dataset.slug : null;
  }

  function swapBoard(html) {
    const board = document.getElementById('board');
    if (!board) return;
    board.outerHTML = html;
    htmx.process(document.getElementById('board'));
    init();
  }

  function postJSON(url, payload) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'HX-Request': 'true', 'X-CSRF-Token': csrf },
      body: JSON.stringify(payload),
      credentials: 'same-origin',
    }).then((r) => r.text());
  }

  // --- drag & drop ------------------------------------------------------
  function initSortable() {
    if (typeof Sortable === 'undefined') return;
    document.querySelectorAll('.dcol').forEach(function (el) {
      if (el._sortable_init) return;
      el._sortable_init = true;
      new Sortable(el, {
        group: 'flowboard',
        animation: 150,
        onEnd: function (evt) {
          const ul = evt.to;
          const ctx = ul.closest('.col').dataset.context;
          const order = Array.from(ul.querySelectorAll('.cardwrap')).map((li) => li.dataset.task);
          postJSON('/boards/' + boardSlug() + '/tasks/reorder', { context: ctx, order: order }).then(swapBoard);
        },
      });
    });
  }

  // --- collapsible columns & expandable cards ---------------------------
  function storeKey(kind) { return 'fb:' + kind + ':' + boardSlug(); }
  function loadSet(kind) {
    try { return new Set(JSON.parse(localStorage.getItem(storeKey(kind)) || '[]')); } catch (e) { return new Set(); }
  }
  function saveSet(kind, set) {
    try { localStorage.setItem(storeKey(kind), JSON.stringify(Array.from(set))); } catch (e) {}
  }
  function initCollapse() {
    const board = document.getElementById('board');
    if (!board || board._collapse_init) return;
    board._collapse_init = true;
    const collapsed = loadSet('cols');
    const expanded = loadSet('cards');
    const forceExpand = board.dataset.expand;
    if (forceExpand) { expanded.add(forceExpand); saveSet('cards', expanded); }
    board.querySelectorAll('.col').forEach(function (col) {
      const ctx = col.dataset.context;
      if (collapsed.has(ctx)) col.classList.add('collapsed');
      const head = col.querySelector('.colhead');
      function toggle() {
        col.classList.toggle('collapsed');
        if (col.classList.contains('collapsed')) collapsed.add(ctx); else collapsed.delete(ctx);
        saveSet('cols', collapsed);
      }
      head.addEventListener('click', toggle);
      head.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } });
    });
    board.querySelectorAll('.cardwrap').forEach(function (li) {
      const id = li.dataset.task;
      if (expanded.has(id)) li.classList.add('open');
      const head = li.querySelector('.cardhead');
      if (!head) return;
      function toggle(e) {
        if (e && e.target.closest('a, button, form, input, select')) return;
        li.classList.toggle('open');
        if (li.classList.contains('open')) expanded.add(id); else expanded.delete(id);
        saveSet('cards', expanded);
      }
      head.addEventListener('click', toggle);
      head.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } });
    });
    board.querySelectorAll('[data-cols]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        const collapse = btn.dataset.cols === 'collapse';
        board.querySelectorAll('.col').forEach(function (col) {
          col.classList.toggle('collapsed', collapse);
          if (collapse) collapsed.add(col.dataset.context); else collapsed.delete(col.dataset.context);
        });
        saveSet('cols', collapsed);
      });
    });
    if (forceExpand) {
      const el = board.querySelector('.cardwrap[data-task="' + forceExpand + '"]');
      if (el) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
  }

  // --- reflection modal: Esc closes (= skip) ------------------------------
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    const m = document.querySelector('.reflectmodal');
    if (m) { const skip = m.querySelector('button[name=skip]'); if (skip) skip.click(); }
  });
  function initFlashes() {
    document.querySelectorAll('.flash.autohide').forEach(function (f) {
      if (f._t) return; f._t = setTimeout(function () { f.remove(); }, 6000);
    });
  }

  // --- focus mode -------------------------------------------------------
  let modal;
  function ensureModal() {
    if (modal) return modal;
    modal = document.createElement('div');
    modal.id = 'focus-modal';
    modal.className = 'focusmodal';
    modal.style.display = 'none';
    modal.innerHTML = '<div class="focuscard"><h3 id="focus-title"></h3><p>Timer: <span id="focus-remaining"></span></p><button id="focus-stop">Stop</button></div>';
    document.body.appendChild(modal);
    return modal;
  }
  function initFocus() {
    document.querySelectorAll('.focusbtn').forEach(function (btn) {
      if (btn._focus_init) return;
      btn._focus_init = true;
      btn.addEventListener('click', function () {
        const m = ensureModal();
        const est = parseInt(btn.dataset.est || '25', 10) * 60;
        const taskId = btn.dataset.id;
        const endTs = Date.now() + est * 1000;
        m.querySelector('#focus-title').textContent = btn.dataset.title;
        m.style.display = 'flex';
        const remain = m.querySelector('#focus-remaining');
        let timer = null;
        function finish() {
          clearInterval(timer);
          const actual = prompt('How many minutes did this actually take?', '');
          m.style.display = 'none';
          const n = parseInt(actual || '0', 10);
          if (!n) return;
          postJSON('/boards/' + boardSlug() + '/tasks/' + taskId + '/actual', { actual_min: n }).then(swapBoard);
        }
        function tick() {
          const s = Math.max(0, Math.floor((endTs - Date.now()) / 1000));
          remain.textContent = Math.floor(s / 60) + 'm ' + (s % 60) + 's';
          if (s <= 0) finish();
        }
        tick();
        timer = setInterval(tick, 1000);
        m.querySelector('#focus-stop').onclick = finish;
      });
    });
  }

  // --- assistant --------------------------------------------------------
  function initAssistant() {
    const form = document.getElementById('ask-form');
    if (!form || form._init) return;
    form._init = true;
    const ta = form.querySelector('textarea[name=prompt]');
    form.querySelectorAll('.chip').forEach(function (chip) {
      chip.addEventListener('click', function () {
        ta.value = chip.dataset.prompt;
        ta.focus();
      });
    });
    // keep a short conversational history for follow-ups
    form.addEventListener('htmx:afterRequest', function () {
      const out = document.getElementById('assistant-out');
      const hist = [];
      Array.from(out.querySelectorAll('.changeset')).slice(0, 4).reverse().forEach(function (cs) {
        const q = cs.querySelector('.cs-q');
        const a = cs.querySelector('.cs-msg');
        if (q) hist.push({ role: 'user', content: q.textContent.replace(/^You:\s*/, '') });
        if (a) hist.push({ role: 'assistant', content: a.textContent });
      });
      document.getElementById('ai-history').value = JSON.stringify(hist);
      ta.value = '';
    });
    // model picker: grouped by provider/family, only configured providers
    const picker = document.getElementById('model-picker');
    if (picker) {
      fetch('/api/ai/models', { credentials: 'same-origin' }).then((r) => r.json()).then(function (data) {
        (data.providers || []).forEach(function (p) {
          (p.groups || []).forEach(function (g) {
            const og = document.createElement('optgroup');
            og.label = p.provider_name + ' · ' + g.family;
            g.models.forEach(function (m) {
              const o = document.createElement('option');
              o.value = m.ref;
              o.textContent = m.name + (m.tags && m.tags.length ? ' (' + m.tags.join(', ') + ')' : '');
              og.appendChild(o);
            });
            picker.appendChild(og);
          });
        });
        const manage = document.createElement('option');
        manage.value = '__manage';
        manage.textContent = '⚙ Manage providers…';
        picker.appendChild(manage);
        picker.addEventListener('change', function () {
          if (picker.value === '__manage') window.location.href = '/settings/ai-providers';
        });
      }).catch(function () {});
    }
  }

  function init() {
    initSortable();
    initCollapse();
    initFocus();
    initAssistant();
    initFlashes();
  }
  document.addEventListener('DOMContentLoaded', init);
  document.body.addEventListener('htmx:afterSwap', init);
  document.body.addEventListener('htmx:responseError', function (e) {
    const status = e.detail.xhr.status;
    if (status === 403) alert('Request rejected (403). Reload the page and try again.');
  });
})();
