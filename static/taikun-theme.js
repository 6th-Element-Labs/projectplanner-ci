/* ============================================================================
   taikun-theme.js  —  Taikun brand + color-scheme switcher (the Settings cog).
   ----------------------------------------------------------------------------
   Self-contained. On load it applies the saved brand color + light/dark scheme;
   it renders a Tabler offcanvas "Settings" panel opened from a floating cog
   (like preview.tabler.io). Picking a brand swaps --tblr-primary (+ -rgb) on
   :root, so every button / link / active tab / bg-primary / text-primary
   recolors instantly, app-wide. --tblr-danger (alarm red) is NEVER touched, so
   choosing a non-red brand automatically frees red for alarms. Persisted in
   localStorage (per browser). Loaded once by js/navbar.js, so it's on every page.
   ============================================================================ */
(function () {
  var BRANDS = [
    { name: 'Taikun Red', hex: '#c0392b', rgb: '192,57,43' },
    { name: 'Blue',       hex: '#3b82f6', rgb: '59,130,246' },
    { name: 'Black',      hex: '#0b1020', rgb: '11,16,32' },
    { name: 'Teal',       hex: '#0f6e56', rgb: '15,110,86' },
    { name: 'Indigo',     hex: '#4263eb', rgb: '66,99,235' },
    { name: 'Amber',      hex: '#d97706', rgb: '217,119,6' }
  ];
  var LS_BRAND = 'tk-brand', LS_SCHEME = 'tk-scheme';
  var root = document.documentElement;
  var DEFAULT_BRAND = '#c0392b';

  function get(k, d) { try { return localStorage.getItem(k) || d; } catch (e) { return d; } }
  function set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }

  function rgbFor(hex) {
    var b = BRANDS.filter(function (x) { return x.hex.toLowerCase() === (hex || '').toLowerCase(); })[0];
    if (b) return b.rgb;
    var h = (hex || '').replace('#', '');
    if (h.length === 6) return parseInt(h.substr(0,2),16) + ',' + parseInt(h.substr(2,2),16) + ',' + parseInt(h.substr(4,2),16);
    return '192,57,43';
  }
  function applyBrand(hex) {
    if (!hex) return;
    root.style.setProperty('--tblr-primary', hex);
    root.style.setProperty('--tblr-primary-rgb', rgbFor(hex));
  }
  function resolveScheme(mode) {
    if (mode === 'auto') return (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
    return mode || 'light';
  }
  function applyScheme(mode) { root.setAttribute('data-bs-theme', resolveScheme(mode)); }
  function applyDensity(mode) { if (document.body) document.body.classList.toggle('tk-density-control', mode === 'compact'); }

  /* 1 · apply saved prefs immediately (minimise flash) */
  var savedBrand = get(LS_BRAND, null);
  if (savedBrand) applyBrand(savedBrand);
  applyScheme(get(LS_SCHEME, 'light'));
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () {
      if (get(LS_SCHEME, 'light') === 'auto') applyScheme('auto');
    });
  }

  /* 2 · build the panel + cog when the DOM is ready */
  function build() {
    if (document.getElementById('tk-settings')) return;

    var oc = document.createElement('div');
    oc.className = 'offcanvas offcanvas-end';
    oc.tabIndex = -1; oc.id = 'tk-settings'; oc.style.maxWidth = '320px';
    oc.innerHTML =
      '<div class="offcanvas-header"><h2 class="offcanvas-title">Settings</h2>' +
        '<button class="btn-close" data-bs-dismiss="offcanvas" aria-label="Close"></button></div>' +
      '<div class="offcanvas-body">' +
        '<div class="mb-4"><div class="subheader mb-2">Color scheme</div>' +
          '<div class="btn-group w-100" role="group" id="tk-scheme">' +
            '<button type="button" class="btn" data-scheme="light"><i class="ti ti-sun me-1"></i>Light</button>' +
            '<button type="button" class="btn" data-scheme="dark"><i class="ti ti-moon me-1"></i>Dark</button>' +
            '<button type="button" class="btn" data-scheme="auto"><i class="ti ti-device-desktop me-1"></i>Auto</button>' +
          '</div></div>' +
        '<div><div class="subheader mb-2">Brand color</div>' +
          '<div class="row g-2" id="tk-brands"></div>' +
          '<div class="text-secondary small mt-2">Recolors buttons, links &amp; tabs everywhere. Alarm red stays reserved.</div>' +
        '</div>' +
        '<div class="mt-4"><div class="subheader mb-2">Density</div>' +
          '<div class="btn-group w-100" role="group" id="tk-density">' +
            '<button type="button" class="btn" data-density="comfortable">Comfortable</button>' +
            '<button type="button" class="btn" data-density="compact"><i class="ti ti-layout-rows me-1"></i>Compact</button>' +
          '</div></div>' +
      '</div>';
    document.body.appendChild(oc);

    var wrap = oc.querySelector('#tk-brands');
    BRANDS.forEach(function (b) {
      var col = document.createElement('div');
      col.className = 'col-4';
      col.innerHTML =
        '<button type="button" class="btn btn-outline-secondary w-100 d-flex flex-column align-items-center gap-1 tk-swatch" data-hex="' + b.hex + '" style="padding:.5rem .25rem">' +
          '<span style="width:22px;height:22px;border-radius:50%;background:' + b.hex + '"></span>' +
          '<span class="small">' + b.name + '</span></button>';
      wrap.appendChild(col);
    });

    var cog = document.createElement('button');
    cog.type = 'button';
    cog.className = 'btn btn-icon btn-primary shadow d-print-none';
    cog.setAttribute('data-bs-toggle', 'offcanvas');
    cog.setAttribute('data-bs-target', '#tk-settings');
    cog.setAttribute('aria-label', 'Theme settings');
    cog.title = 'Theme settings';
    cog.style.cssText = 'position:fixed;right:1rem;bottom:1rem;z-index:1030;width:44px;height:44px;border-radius:50%;padding:0';
    cog.innerHTML = '<i class="ti ti-settings fs-2"></i>';
    document.body.appendChild(cog);

    function markActive() {
      var sc = get(LS_SCHEME, 'light');
      oc.querySelectorAll('#tk-scheme .btn').forEach(function (btn) {
        var on = btn.getAttribute('data-scheme') === sc;
        btn.classList.toggle('btn-primary', on);
        btn.classList.toggle('btn-outline-secondary', !on);
      });
      var br = get(LS_BRAND, DEFAULT_BRAND);
      oc.querySelectorAll('.tk-swatch').forEach(function (btn) {
        var on = btn.getAttribute('data-hex').toLowerCase() === br.toLowerCase();
        btn.style.outline = on ? '2px solid var(--tblr-primary)' : '';
        btn.style.outlineOffset = '2px';
      });
      var de = get('tk-density', 'comfortable');
      oc.querySelectorAll('#tk-density .btn').forEach(function (btn) {
        var on = btn.getAttribute('data-density') === de;
        btn.classList.toggle('btn-primary', on);
        btn.classList.toggle('btn-outline-secondary', !on);
      });
    }
    applyDensity(get('tk-density', 'comfortable'));
    oc.querySelector('#tk-scheme').addEventListener('click', function (e) {
      var btn = e.target.closest('[data-scheme]'); if (!btn) return;
      set(LS_SCHEME, btn.getAttribute('data-scheme')); applyScheme(btn.getAttribute('data-scheme')); markActive();
    });
    oc.querySelector('#tk-density').addEventListener('click', function (e) {
      var btn = e.target.closest('[data-density]'); if (!btn) return;
      set('tk-density', btn.getAttribute('data-density')); applyDensity(btn.getAttribute('data-density')); markActive();
    });
    wrap.addEventListener('click', function (e) {
      var btn = e.target.closest('.tk-swatch'); if (!btn) return;
      set(LS_BRAND, btn.getAttribute('data-hex')); applyBrand(btn.getAttribute('data-hex')); markActive();
    });
    oc.addEventListener('show.bs.offcanvas', markActive);
    markActive();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', build);
  else build();
})();
