/* NARRATE-13: operator narration dock — queue depth, freshness, success/failure/fallback rates,
   spend, and alert flags from /api/narration/health. Quiet when healthy; expands on any alert. */
(function () {
    const POLL_MS = 20000;

    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    function project() {
        try {
            return new URLSearchParams(location.search).get('project') || 'switchboard';
        } catch (e) { return 'switchboard'; }
    }

    function render(body) {
        const host = document.getElementById('narration-ops-dock');
        if (!host) return;
        const q = body.queue || {};
        const r = body.receipts || {};
        const alerts = body.alerts || {};
        const active = Object.keys(alerts).filter(function (k) { return alerts[k]; });
        if (!body.alerting && (q.actionable || 0) === 0 && (q.dead_letter || 0) === 0) {
            host.innerHTML = '';  // quiet when nothing needs attention
            return;
        }
        const color = body.alerting ? 'warning' : 'secondary';
        const rows = [
            ['Queued', (q.pending || 0) + ' pending / ' + (q.retry_wait || 0) + ' retry'],
            ['Running', (q.claimed || 0) + ' claimed (' + (q.expired_leases || 0) + ' lease-expired)'],
            ['Dead letters', q.dead_letter || 0],
            ['Oldest pending', Math.round((body.freshness || {}).oldest_pending_age_seconds || 0) + 's'],
            ['Outcomes', (r.delivered || 0) + ' ok / ' + (r.failed || 0) + ' err / ' + (r.fallback || 0) + ' fallback'],
            ['Failure rate', Math.round((r.failure_rate || 0) * 100) + '%'],
            ['Spend (window)', '$' + ((body.cost || {}).total_cost_usd || 0).toFixed(4) +
                ' / ' + ((body.cost || {}).total_tokens || 0) + ' tok'],
        ];
        host.innerHTML =
            '<div class="card border-' + color + ' mb-2"><div class="card-body p-2">' +
            '<div class="fw-semibold text-' + color + ' mb-1">Narration queue' +
            (active.length ? ' — alerts: ' + esc(active.join(', ')) : '') + '</div>' +
            '<table class="table table-sm mb-0"><tbody>' +
            rows.map(function (kv) {
                return '<tr><td class="text-secondary">' + esc(kv[0]) + '</td><td>' + esc(kv[1]) + '</td></tr>';
            }).join('') +
            '</tbody></table></div></div>';
    }

    async function poll() {
        try {
            const resp = await fetch('/api/narration/health?project=' + encodeURIComponent(project()),
                { credentials: 'same-origin' });
            if (resp.ok) render(await resp.json());
        } catch (e) { /* transient — try again next tick */ }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () { poll(); setInterval(poll, POLL_MS); });
    } else { poll(); setInterval(poll, POLL_MS); }
})();
