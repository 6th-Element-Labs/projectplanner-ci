/* NARRATE-13: operator narration dock — queue exceptions only.
   Quiet when the queue is healthy. Receipt-window rates (fallback/failure %) live in
   Settings → Narration; they must not dump a card onto every page. */
(function () {
    const POLL_MS = 20000;
    let collapsed = true;

    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    function project() {
        try {
            return (window.PM_PROJECT
                || new URLSearchParams(location.search).get('project')
                || 'switchboard');
        } catch (e) { return 'switchboard'; }
    }

    /** Queue problems an operator can act on now — not historical receipt stats. */
    function queueExceptions(body) {
        const q = body.queue || {};
        const alerts = body.alerts || {};
        const flags = [];
        if (alerts.dead_letters_present || (q.dead_letter || 0) > 0) flags.push('dead_letters');
        if (alerts.expired_leases_present || (q.expired_leases || 0) > 0) flags.push('expired_leases');
        if (alerts.queue_age_over_threshold) flags.push('queue_age');
        return flags;
    }

    function openSettings() {
        window.location.hash = '#tab-settings/narration';
        if (window.TAIKUN_showTab) window.TAIKUN_showTab('#tab-settings');
    }

    function render(body) {
        const host = document.getElementById('narration-ops-dock');
        if (!host) return;
        const flags = queueExceptions(body);
        if (!flags.length) {
            host.innerHTML = '';
            return;
        }

        const q = body.queue || {};
        const color = 'warning';
        // Position is owned by .tk-left-status-stack so Narration and Box
        // Pressure can never render into the same fixed coordinates.
        const anchor = '';
        const summary = flags.join(', ');

        if (collapsed) {
            host.innerHTML =
                '<button id="narration-ops-pill" type="button" class="btn btn-sm btn-' + color +
                ' shadow-sm" style="' + anchor + 'border-radius:999px;">' +
                '<i class="ti ti-broadcast me-1"></i>Narration · ' + esc(summary) +
                '</button>';
            const pill = document.getElementById('narration-ops-pill');
            if (pill) pill.addEventListener('click', function () {
                collapsed = false;
                render(body);
            });
            return;
        }

        const rows = [
            ['Queued', (q.pending || 0) + ' pending / ' + (q.retry_wait || 0) + ' retry'],
            ['Running', (q.claimed || 0) + ' claimed (' + (q.expired_leases || 0) + ' lease-expired)'],
            ['Dead letters', q.dead_letter || 0],
            ['Oldest pending', Math.round((body.freshness || {}).oldest_pending_age_seconds || 0) + 's'],
        ];
        host.innerHTML =
            '<div class="card shadow-sm border-' + color + '" style="' + anchor +
            'max-width:360px;">' +
            '<div class="card-header py-2 d-flex align-items-center justify-content-between">' +
            '<div class="fw-semibold text-' + color + '">' +
            '<i class="ti ti-broadcast me-1"></i>Narration queue</div>' +
            '<div>' +
            '<button id="narration-ops-settings" type="button" ' +
            'class="btn btn-sm btn-ghost-secondary p-1" title="Open Settings">' +
            '<i class="ti ti-settings"></i></button>' +
            '<button id="narration-ops-min" type="button" ' +
            'class="btn btn-sm btn-ghost-secondary p-1" title="Collapse">' +
            '<i class="ti ti-chevron-down"></i></button>' +
            '</div></div>' +
            '<div class="card-body p-2 small">' +
            '<div class="text-secondary mb-2">alerts: ' + esc(summary) + '</div>' +
            '<table class="table table-sm mb-0"><tbody>' +
            rows.map(function (kv) {
                return '<tr><td class="text-secondary">' + esc(kv[0]) +
                    '</td><td>' + esc(kv[1]) + '</td></tr>';
            }).join('') +
            '</tbody></table></div></div>';

        const min = document.getElementById('narration-ops-min');
        const settings = document.getElementById('narration-ops-settings');
        if (min) min.addEventListener('click', function () {
            collapsed = true;
            render(body);
        });
        if (settings) settings.addEventListener('click', openSettings);
    }

    async function poll(force) {
        if (document.hidden && !force) return;
        try {
            const resp = await fetch(
                '/api/narration/health?project=' + encodeURIComponent(project()),
                { credentials: 'same-origin', cache: 'no-store' });
            if (resp.ok) render(await resp.json());
        } catch (e) { /* transient — try again next tick */ }
    }

    function start() {
        poll(true);
        setInterval(function () { poll(false); }, POLL_MS);
        document.addEventListener('visibilitychange', function () {
            if (!document.hidden) poll(true);
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})();
