# Vendored frontend assets (BUG-110)

Pinned copies of third-party browser assets previously loaded from jsDelivr.
Served from `/vendor/...` via the SPA static mount (same pattern as ApexCharts).

| Package | Version | Path |
|---------|---------|------|
| @xterm/xterm | 5.5.0 | `xterm/` |
| @xterm/addon-fit | 0.10.0 | `xterm/addon-fit.js` |
| mermaid | 11 (resolved dist) | `mermaid/mermaid.min.js` |
| @mermaid-js/layout-elk | 0.2.2 | `mermaid/mermaid-layout-elk.esm.min.mjs` + chunks |
| @tabler/core | 1.4.0 | `tabler/css/tabler.min.css` |
| @tabler/icons-webfont | 3.44.0 | `tabler/css/tabler-icons.min.css` + `tabler/fonts/` |
| bootstrap | 5.3.3 | `bootstrap/bootstrap.bundle.min.js` |
| apexcharts | (pre-existing) | `apexcharts/` |

Do not reload these from CDN in product HTML/JS. Docs mockups under `docs/` may still use CDN.
