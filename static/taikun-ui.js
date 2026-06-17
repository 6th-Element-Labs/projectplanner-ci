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

  var DESKTOP = '(min-width: 992px)';
  function render() {
    if (!document.body) return;
    document.body.classList.toggle('tk-sidebar-collapsed', collapsed);
    document.querySelectorAll('[data-tk-sidebar-toggle]').forEach(function (btn) {
      btn.setAttribute('title', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
      btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      var i = btn.querySelector('i');
      if (i) i.className = 'ti ' + (collapsed ? 'ti-layout-sidebar-left-expand' : 'ti-layout-sidebar-left-collapse');
    });
    // Reflow the content beside the icon-rail. Tabler hardcodes the page-wrapper's
    // margin-inline-start (= sidebar width) with !important in its cross-origin sheet;
    // only an inline !important on the SAME logical property reliably wins. Always set
    // it explicitly (never removeProperty — logical-prop removal is flaky): mobile = 0,
    // desktop collapsed = the 4.25rem rail, desktop expanded = Tabler's 15rem width.
    var wrap = document.querySelector('.page-wrapper');
    var aside = document.querySelector('.navbar-vertical');
    if (wrap && aside) {
      // Only the desktop fixed sidebar reserves inline-start space. On mobile Tabler
      // turns the navbar into a normal top bar (position != fixed) with no offset, so
      // detect by the aside's actual layout rather than a media query.
      var rail = getComputedStyle(aside).position === 'fixed';
      var v = !rail ? '0' : (collapsed ? '4.25rem' : '15rem');
      wrap.style.setProperty('margin-inline-start', v, 'important');
    }
  }

  function toggle() { collapsed = !collapsed; write(collapsed); render(); }

  document.addEventListener('click', function (e) {
    var t = e.target.closest('[data-tk-sidebar-toggle]');
    if (t) { e.preventDefault(); toggle(); }
  });

  // Re-apply the desktop margin when the viewport crosses the breakpoint.
  var _rt;
  window.addEventListener('resize', function () { clearTimeout(_rt); _rt = setTimeout(render, 120); });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', render);
  } else {
    render();
  }
})();

// Load the Taikun theme switcher (brand color + light/dark Settings cog).
(function () {
  if (document.getElementById('tk-theme-js')) return;
  var s = document.createElement('script'); s.id = 'tk-theme-js'; s.src = 'taikun-theme.js?v=1';
  document.head.appendChild(s);
})();
