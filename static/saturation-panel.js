/* PERF-7: operator saturation dock — PSI + lock-wait + inbox depth + SLO alerts.
   Quiet when healthy; expands when /health/saturation reports warnings/critical. */
(function () {
    const POLL_MS = 15000;
    let timer = null;
    let collapsed = true;
    let lastSig = '';

    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
    }

    function severityColor(status) {
        if (status === 'critical') return 'danger';
        if (status === 'warning') return 'warning';
        if (status === 'info') return 'info';
        return 'success';
    }

    function signature(body) {
        return JSON.stringify([
            body.status,
            body.alert_count,
            body.sqlite_lock_waits,
            body.webhook_inbox_pending,
            body.slos_ok,
            body.load_shed,
        ]);
    }

    function updateMobileStatus(status, alertCount) {
        const badge = document.getElementById('mobile-system-health-badge');
        const icon = document.getElementById('mobile-system-health-icon');
        if (!badge || !icon) return;
        const visible = status !== 'healthy';
        const tone = status === 'critical' ? 'danger' : (status === 'warning' ? 'warning' : 'info');
        badge.textContent = status === 'critical' ? 'Critical' : (alertCount ? String(alertCount) : status);
        badge.className = `badge bg-${tone}-lt ms-auto${visible ? '' : ' d-none'}`;
        icon.className = `ti ti-activity-heartbeat me-2${visible ? ` text-${tone}` : ''}`;
    }

    function render(body) {
        const host = document.getElementById('saturation-dock');
        if (!host) return;
        const status = body.status || 'healthy';
        const color = severityColor(status);
        const alerts = body.alerts || [];
        const quiet = status === 'healthy' && alerts.length === 0;
        updateMobileStatus(status, Number(body.alert_count || alerts.length || 0));

        // Mobile navigation owns the lower safe area. Warnings remain passive in
        // More -> System health; only a critical system condition interrupts the
        // page, and it does so at the top where it cannot cover navigation.
        if (window.matchMedia && window.matchMedia('(max-width: 991.98px)').matches) {
            if (status !== 'critical') {
                host.innerHTML = '';
                return;
            }
            const message = alerts.length ? alerts[0].message : 'This box is under critical pressure.';
            host.innerHTML = `<button id="saturation-critical-banner" type="button" class="alert alert-danger tk-pressure-banner text-start w-100" role="alert">
                <span class="d-flex align-items-center gap-2"><i class="ti ti-alert-triangle"></i><span class="flex-fill"><strong>System pressure is critical.</strong> ${esc(message)}</span><i class="ti ti-chevron-right"></i></span>
            </button>`;
            document.getElementById('saturation-critical-banner').addEventListener('click', () => {
                window.location.hash = '#tab-settings/capacity';
                if (window.TAIKUN_showTab) window.TAIKUN_showTab('#tab-settings');
            });
            return;
        }

        if (quiet) {
            host.innerHTML = '';
            return;
        }

        if (collapsed) {
            host.innerHTML = `<button id="saturation-dock-pill" class="btn btn-sm btn-${color} shadow-sm"
                style="position:fixed;bottom:16px;left:16px;z-index:1040;border-radius:999px;">
                <i class="ti ti-activity-heartbeat me-1"></i>Box pressure
            </button>`;
            const pill = document.getElementById('saturation-dock-pill');
            if (pill) pill.addEventListener('click', () => { collapsed = false; render(body); });
            return;
        }

        const rows = alerts.map((a) => `<li class="mb-1"><span class="badge bg-${severityColor(a.severity)}-lt me-1">${esc(a.severity)}</span>${esc(a.message)}</li>`).join('')
            || '<li class="text-secondary">No active alerts</li>';

        host.innerHTML = `<div class="card shadow-sm" style="position:fixed;bottom:16px;left:16px;z-index:1040;max-width:360px;">
            <div class="card-header py-2 d-flex align-items-center justify-content-between">
                <div class="fw-semibold"><i class="ti ti-activity-heartbeat me-1"></i>Box saturation</div>
                <div>
                    <button id="saturation-dock-refresh" class="btn btn-sm btn-ghost-secondary p-1" title="Refresh"><i class="ti ti-refresh"></i></button>
                    <button id="saturation-dock-min" class="btn btn-sm btn-ghost-secondary p-1" title="Collapse"><i class="ti ti-chevron-down"></i></button>
                </div>
            </div>
            <div class="card-body py-2 small">
                <div class="mb-2">Status: <span class="badge bg-${color}-lt">${esc(status)}</span>
                    ${body.load_shed ? '<span class="badge bg-danger-lt ms-1">shedding</span>' : ''}
                </div>
                <div class="text-secondary mb-2">
                    lock-wait ${esc(body.sqlite_lock_waits || 0)}
                    · inbox ${esc(body.webhook_inbox_pending || 0)}
                    · SLO ${body.slos_ok === false ? '<span class="text-danger">fail</span>' : '<span class="text-success">ok</span>'}
                </div>
                <ul class="mb-0 ps-3">${rows}</ul>
            </div>
        </div>`;

        const refresh = document.getElementById('saturation-dock-refresh');
        const min = document.getElementById('saturation-dock-min');
        if (refresh) refresh.addEventListener('click', () => poll(true));
        if (min) min.addEventListener('click', () => { collapsed = true; render(body); });
    }

    async function poll(force) {
        if (document.hidden && !force) return;
        const project = window.PM_PROJECT || 'switchboard';
        try {
            const res = await fetch(`/health/saturation?project=${encodeURIComponent(project)}`, { cache: 'no-store' });
            if (!res.ok) return;
            const body = await res.json();
            const sig = signature(body);
            if (!force && sig === lastSig) return;
            lastSig = sig;
            render(body);
        } catch (e) {
            /* leave last good render */
        }
    }

    function start() {
        if (timer) return;
        poll(true);
        timer = window.setInterval(() => poll(false), POLL_MS);
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) poll(true);
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})();
