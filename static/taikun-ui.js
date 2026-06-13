/* ============================================================================
   taikun-ui.js  ·  Taikun UI behaviors (v0.1)
   ----------------------------------------------------------------------------
   Load AFTER @tabler/core's JS bundle:

     <script src=".../@tabler/core/dist/js/tabler.min.js"></script>
     <script src="taikun-ui.js"></script>

   Provides:
     1) A Bootstrap-API alias + data-attribute wiring so the SAME markup
        (data-bs-toggle="tab|modal|collapse") is interactive across every
        @tabler/core build and drops 1:1 into the product.
     2) A collapsible sidebar: click any [data-tk-sidebar-toggle] to collapse /
        expand the vertical navbar. State persists in localStorage and DEFAULTS
        TO COLLAPSED. For no flash-of-expanded, also put
        class="tk-sidebar-collapsed" on <body> in your HTML.
   ============================================================================ */
(function () {
  'use strict';

  /* ── 1 · Bootstrap API alias + data-attr delegation ──────────────────────
     Only needed for @tabler/core builds that don't auto-wire data-bs-*. Apps
     that already load the full bootstrap.bundle (which auto-wires) set
     window.TAIKUN_NO_BS_WIRE = true before this script to avoid double-firing. */
  var bs = window.bootstrap || (window.tabler && window.tabler.bootstrap);
  if (bs && !window.TAIKUN_NO_BS_WIRE) {
    window.bootstrap = bs;
    document.querySelectorAll('[data-bs-toggle="tab"]').forEach(function (el) {
      el.addEventListener('click', function (e) { e.preventDefault(); bs.Tab.getOrCreateInstance(el).show(); });
    });
    document.querySelectorAll('[data-bs-toggle="modal"]').forEach(function (el) {
      el.addEventListener('click', function (e) {
        e.preventDefault();
        var target = document.querySelector(el.getAttribute('data-bs-target'));
        if (target) bs.Modal.getOrCreateInstance(target).show();
      });
    });
    document.querySelectorAll('[data-bs-dismiss="modal"]').forEach(function (el) {
      el.addEventListener('click', function () {
        var m = el.closest('.modal');
        if (m) bs.Modal.getOrCreateInstance(m).hide();
      });
    });
    document.querySelectorAll('[data-bs-toggle="collapse"]').forEach(function (el) {
      el.addEventListener('click', function (e) {
        e.preventDefault();
        var sel = el.getAttribute('data-bs-target') || el.getAttribute('href');
        if (sel) bs.Collapse.getOrCreateInstance(document.querySelector(sel)).toggle();
      });
    });
  }

  /* ── 2 · Collapsible sidebar (default collapsed, persisted) ───────────── */
  var KEY = 'taikun.sidebar.collapsed';
  function read() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function write(v) { try { localStorage.setItem(KEY, v ? '1' : '0'); } catch (e) {} }

  var stored = read();
  var collapsed = (stored === null) ? true : (stored === '1'); // DEFAULT: collapsed

  function render() {
    if (!document.body) return;
    document.body.classList.toggle('tk-sidebar-collapsed', collapsed);
    document.querySelectorAll('[data-tk-sidebar-toggle]').forEach(function (btn) {
      btn.setAttribute('title', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
      btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      var i = btn.querySelector('i');
      if (i) i.className = 'ti ' + (collapsed ? 'ti-layout-sidebar-left-expand' : 'ti-layout-sidebar-left-collapse');
    });
  }

  function toggle() { collapsed = !collapsed; write(collapsed); render(); }

  document.addEventListener('click', function (e) {
    var t = e.target.closest('[data-tk-sidebar-toggle]');
    if (t) { e.preventDefault(); toggle(); }
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', render);
  } else {
    render();
  }
})();
