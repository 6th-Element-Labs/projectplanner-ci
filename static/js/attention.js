/* UI-29: the Needs-you queue — one list of everything awaiting a human.
 * ============================================================================
 * Reads GET /api/attention, a read-only projection over provider requests,
 * messages, inbound triage, mission actions, and open plan decisions.
 * Self-contained: wires only inside #tab-needs; no app.js changes. The list/
 * detail layout reuses ActionEngine's approval-gate shape (agent-approval.js)
 * in the house status language (dots + words, Tabler type ladder).
 */
(function () {
    'use strict';

    let items = [];
    let sel = null;
    let filter = 'all';
    let tracked = null;
    let delivering = false;

    const SRC = {
        agent: ['#c0392b', 'ti-robot', 'Agent'],
        inbox: ['#4299e1', 'ti-mail', 'Inbound'],
        provider: ['#ae3ec9', 'ti-plug', 'Provider'],
        mission: ['#f76707', 'ti-target-arrow', 'Mission'],
        decision: ['#f59f00', 'ti-help-circle', 'Decision'],
    };
    const STATE = {
        pending: ['bg-yellow-lt', 'Needs decision'],
        decision_recorded: ['bg-azure-lt', 'Resuming'],
        delivering: ['bg-azure-lt', 'Resuming'],
        resolved: ['bg-green-lt', 'Resumed'],
        failed: ['bg-red-lt', 'Failed'],
        expired: ['bg-orange-lt', 'Expired'],
        cancelled: ['bg-secondary-lt', 'Cancelled'],
        orphaned: ['bg-red-lt', 'Orphaned'],
    };

    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    }
    function el(id) { return document.getElementById(id); }
    function age(s) {
        if (s < 60) return s + 's';
        if (s < 3600) return Math.floor(s / 60) + 'm';
        if (s < 86400) return Math.floor(s / 3600) + 'h';
        return Math.floor(s / 86400) + 'd';
    }
    function proj() { return window.PM_PROJECT || 'maxwell'; }
    function apiError(data, status) {
        const detail = data && data.detail;
        if (typeof detail === 'string') return detail;
        if (detail && typeof detail === 'object') {
            return detail.message || detail.error || JSON.stringify(detail);
        }
        return (data && (data.message || data.error)) || `HTTP ${status}`;
    }

    function setCount(n) {
        const count = Number(n) || 0;
        const badge = el('ack-inbox-count');
        if (badge) {
            badge.style.display = count ? '' : 'none';
            badge.textContent = count > 99 ? '99+' : String(count);
            badge.setAttribute('aria-label', `${count} items need you`);
        }
        const cnt = el('needs-count');
        if (cnt) cnt.textContent = String(count);
    }

    async function load(options) {
        const renderQueue = !(options && options.render === false);
        const list = el('needs-list');
        if (!list) return;
        try {
            const res = await fetch(`api/attention?project=${encodeURIComponent(proj())}`, { cache: 'no-store' });
            const data = await res.json();
            if (!res.ok) throw new Error(apiError(data, res.status));
            items = data.items || [];
            setCount(data.count);
        } catch (e) {
            if (renderQueue) list.innerHTML = `<div class="text-secondary p-3"><i class="ti ti-plug-connected-x me-1"></i>Could not load api/attention (${esc(e.message)})</div>`;
            return;
        }
        if (!renderQueue && !document.querySelector('#tab-needs.active')) return;
        if (sel && !items.some((i) => i.attention_id === sel)) sel = null;
        if (!sel && items.length) sel = items[0].attention_id;
        render();
    }

    function match(i) { return filter === 'all' || i.source === filter; }

    function render() {
        const list = el('needs-list'); const detail = el('needs-detail');
        const fw = el('needs-filters'); const cnt = el('needs-count');
        if (!list || !detail) return;
        if (cnt) cnt.textContent = String(items.length);

        const counts = { all: items.length };
        Object.keys(SRC).forEach((k) => { counts[k] = items.filter((i) => i.source === k).length; });
        fw.innerHTML = [['all', 'ti-inbox', 'All']].concat(Object.keys(SRC).map((k) => [k, SRC[k][1], SRC[k][2]]))
            .map((c) => `<button type="button" class="btn btn-sm ${filter === c[0] ? 'btn-primary' : 'btn-outline-secondary'}" data-nf="${c[0]}">
                <i class="ti ${c[1]} me-1"></i>${c[2]} <span class="tk-mono ms-1">${counts[c[0]] || 0}</span></button>`).join('');
        fw.querySelectorAll('[data-nf]').forEach((b) => b.addEventListener('click', () => {
            filter = b.dataset.nf;
            const vis = items.filter(match);
            sel = vis.length ? vis[0].attention_id : null;
            render();
        }));

        const shown = items.filter(match);
        if (!shown.length) {
            list.innerHTML = '';
            detail.innerHTML = `<div class="empty py-5"><div class="empty-icon"><i class="ti ti-checks text-green"></i></div>
                <p class="empty-title">No one is waiting on you</p>
                <p class="empty-subtitle text-secondary">Agent questions and inbound proposals land here the moment they need a human.</p></div>`;
            return;
        }
        list.innerHTML = shown.map((i) => {
            const sc = SRC[i.source] || ['#8b95a5', 'ti-point', ''];
            const p = i.payload || {};
            const completion = i.kind === 'completion_human';
            const status = STATE[p.status] || STATE.pending;
            return `<div class="card mb-2 ${i.attention_id === sel ? 'border-primary' : ''}" data-nid="${esc(i.attention_id)}" style="cursor:pointer">
                <div class="card-body p-2">
                    <div class="d-flex align-items-center gap-2 mb-1">
                        <span class="sdot" style="background:${sc[0]}"></span>
                        ${i.task_id ? `<span class="tk-mono small text-secondary">${esc(i.task_id)}</span>` : ''}
                        ${completion ? '<span class="badge bg-blue-lt">Human handoff</span>' : ''}
                        <span class="badge ${status[0]}">${status[1]}</span>
                        <span class="ms-auto text-secondary small">${age(i.age_s)} ago</span>
                    </div>
                    <div style="font-weight:500;line-height:1.4">${esc(i.title)}</div>
                    <div class="text-secondary small mt-1">${sc[2]}${i.from ? ' · ' + esc(i.from) : ''}
                        ${p.deliverable_id ? ` · ${esc(p.deliverable_id)}` : ''}
                        ${p.pr_number ? ` · PR #${esc(p.pr_number)}` : ''}
                        ${p.head_sha ? ` · ${esc(String(p.head_sha).slice(0, 8))}` : ''}
                        ${i.delivery_impact ? ` · ${esc(i.delivery_impact)}` : ''}</div>
                    ${p.reason_code ? `<div class="small mt-1">${esc(p.reason_code)}</div>` : ''}
                </div></div>`;
        }).join('');
        list.querySelectorAll('[data-nid]').forEach((c) => c.addEventListener('click', () => { sel = c.dataset.nid; render(); }));

        const it = items.find((i) => i.attention_id === sel);
        if (!it) { detail.innerHTML = ''; return; }
        renderDetail(detail, it);
    }

    function datagrid(obj) {
        const entries = Object.entries(obj || {}).filter(([, v]) => v != null && v !== '');
        if (!entries.length) return '<div class="text-secondary small">—</div>';
        return `<div class="datagrid">${entries.map(([k, v]) => `
            <div class="datagrid-item"><div class="datagrid-title">${esc(k)}</div>
            <div class="datagrid-content ${/id|task|touches/.test(k) ? 'font-monospace' : ''}">${esc(Array.isArray(v) ? v.join(', ') : (typeof v === 'object' ? JSON.stringify(v) : v))}</div></div>`).join('')}</div>`;
    }

    function section(title, value) {
        if (value == null || value === '' || (Array.isArray(value) && !value.length)) return '';
        const body = typeof value === 'object' ? datagrid(value) : `<div>${esc(value)}</div>`;
        return `<div class="tk-eyebrow mb-1">${esc(title)}</div><div class="card card-sm mb-3"><div class="card-body p-3">${body}</div></div>`;
    }

    function linkButtons(links) {
        const l = links || {};
        const buttons = [];
        if (l.task) buttons.push(`<a class="btn btn-sm btn-outline-secondary" href="${esc(l.task)}"><i class="ti ti-list-check me-1"></i>Task</a>`);
        if (l.deliverable || l.mission) {
            const id = l.deliverable || l.mission;
            buttons.push(`<a class="btn btn-sm btn-outline-secondary" href="#mission/${encodeURIComponent(id)}"><i class="ti ti-target-arrow me-1"></i>Deliverable</a>`);
        }
        if (l.session) buttons.push(`<button type="button" class="btn btn-sm btn-outline-secondary" data-open-session="${esc(l.session)}"><i class="ti ti-terminal-2 me-1"></i>Open session</button>`);
        if (l.provider) buttons.push(`<span class="badge bg-purple-lt">${esc(l.provider)}</span>`);
        return buttons.join('');
    }

    function renderDetail(detail, it) {
        const sc = SRC[it.source] || ['#8b95a5', 'ti-point', ''];
        const isAgent = it.source === 'agent';
        const isInbox = it.source === 'inbox';
        const isProvider = it.source === 'provider' && Array.isArray(it.payload && it.payload.choices) && it.payload.choices.length;
        const p = it.payload || {};
        const completion = it.kind === 'completion_human';
        const wake = p.completion_wake || {};
        let state = STATE[p.status] || STATE.pending;
        if (completion && p.status === 'decision_recorded' && wake.status === 'failed') {
            state = ['bg-red-lt', 'Wake retrying'];
        } else if (completion && p.status === 'decision_recorded' && wake.status === 'pending') {
            state = ['bg-yellow-lt', 'Wake queued'];
        }
        const choiceButtons = isProvider ? (it.payload.choices || []).map((c) => {
            const id = (c && c.id) || c;
            const label = (c && (c.label || c.id)) || String(c);
            const primary = it.payload.recommended_default
                && ((it.payload.recommended_default.id || it.payload.recommended_default) === id);
            const description = c && (c.description || c.help);
            return `<button type="button" class="btn text-start flex-column align-items-start ${primary ? 'btn-primary' : 'btn-outline-secondary'}" data-choice="${esc(id)}" ${delivering ? 'disabled' : ''}><span>${esc(label)}</span>${description ? `<span class="small opacity-75">${esc(description)}</span>` : ''}</button>`;
        }).join('') : '';
        detail.innerHTML = `
            <div class="d-flex align-items-start gap-2 mb-2">
                <div class="flex-fill">
                    <div class="d-flex align-items-center gap-2 flex-wrap">
                        <span class="tk-mono small text-secondary">${esc(it.attention_id)}</span>
                        <span class="stx small"><span class="sdot" style="background:${sc[0]}"></span>${sc[2]}</span>
                        <span class="badge ${state[0]}" id="needs-state">${state[1]}</span>
                        ${it.from ? `<span class="text-secondary small">· from ${esc(it.from)}</span>` : ''}
                    </div>
                    ${it.task_id ? `<div class="d-flex align-items-baseline gap-2 mt-1"><span class="h3 mb-0 tk-mono">${esc(it.task_id)}</span></div>` : ''}
                </div>
            </div>
            <div class="mb-3" style="font-weight:600">${esc(it.summary || it.title)}</div>
            ${completion ? '<div class="alert alert-info py-2"><strong>Implementation complete, human action required.</strong> This is a controlled handoff, not an implementation failure.</div>' : ''}
            <div class="btn-list mb-3">${linkButtons(it.links)}</div>
            ${completion ? `
                ${section('Completed work', p.completed_work_summary)}
                ${section('Evidence', p.evidence)}
                ${section('Why automation stopped', p.why_automation_stopped || p.reason_code)}
                ${section('What you need to do', p.what_you_need_to_do || it.summary)}
                ${section('Resume condition', p.resume_condition)}
                ${section('Next automatic step', p.next_automatic_action)}
                ${section('Blast radius', p.blast_radius)}
                <details class="mb-3"><summary class="tk-eyebrow" style="cursor:pointer">Frozen payload</summary><div class="card card-sm mt-2"><div class="card-body p-3">${datagrid(p.frozen_payload)}</div></div></details>` :
                `${section('Decision type and blast radius', { decision_type: it.kind, blast_radius: p.blast_radius })}
                 ${section('Evidence', p.evidence)}
                 ${section('Frozen payload', p.frozen_payload || p)}`}
            ${isAgent ? `
                <div class="tk-eyebrow mb-2">Your answer</div>
                <div class="d-flex gap-2 mb-2">
                    <input class="form-control" id="needs-reply" placeholder="Answer — recorded as the ack response; the sender's monitor resolves"/>
                    <button type="button" class="btn btn-primary" id="needs-ack"><i class="ti ti-send me-1"></i>Answer &amp; ack</button>
                </div>` : isInbox ? `
                <div class="tk-eyebrow mb-2">Decide</div>
                <div class="btn-list mb-2">
                    <button type="button" class="btn btn-primary" id="needs-confirm"><i class="ti ti-checks me-1"></i>Confirm — apply proposals</button>
                    <button type="button" class="btn btn-outline-secondary" id="needs-open"><i class="ti ti-list-details me-1"></i>Review in Action Queue</button>
                    <button type="button" class="btn btn-ghost-danger" id="needs-dismiss">Dismiss</button>
                </div>` : isProvider ? `
                <div class="tk-eyebrow mb-2">Decide</div>
                <div class="btn-list mb-2">${choiceButtons}</div>
                <div class="text-secondary small mb-2">Only the frozen choices above are authorized. Policy, permission, and blast-radius changes require a new audited request.</div>
                <div class="text-secondary small mb-2">Resuming means the decision was accepted. Resumed appears only after a bound delivery/execution receipt.</div>
                ${['failed', 'expired', 'cancelled', 'orphaned'].includes(p.status) ? '<button type="button" class="btn btn-outline-danger" id="needs-recover"><i class="ti ti-refresh me-1"></i>Refresh recovery state</button>' : ''}` : `
                <div class="tk-eyebrow mb-2">Authoritative source</div>
                <div class="card card-sm mb-2"><div class="card-body p-2">${datagrid(it.links)}</div></div>
                <div class="text-secondary small">Resolve this item through its owning provider, mission, or plan-decision workflow.</div>`}
            <div id="needs-flash" class="small text-secondary"></div>
            <div class="text-secondary small mt-3 pt-2 border-top">Decisions route to the store that owns the item — this queue adds no new write path.</div>`;

        const flash = (m, cls) => { const f = el('needs-flash'); if (f) { f.className = 'small ' + (cls || 'text-secondary'); f.textContent = m; } };
        if (isAgent) {
            const send = async () => {
                const resp = (el('needs-reply').value || '').trim();
                flash('Sending…');
                try {
                    const res = await fetch(`api/agent_messages/ack?project=${encodeURIComponent(proj())}`, {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ message_id: it.payload.message_id, response: resp }),
                    });
                    const data = await res.json().catch(() => ({}));
                    if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
                    flash('Answered — the sender resumes on your reply.', 'text-green');
                    setTimeout(load, 600);
                } catch (e) { flash('Ack failed: ' + e.message, 'text-danger'); }
            };
            el('needs-ack').addEventListener('click', send);
            el('needs-reply').addEventListener('keydown', (e) => { if (e.key === 'Enter') send(); });
        } else if (isInbox) {
            el('needs-confirm').addEventListener('click', async () => {
                flash('Applying…');
                try {
                    const res = await fetch(`api/inbox/${it.payload.inbox_id}/confirm?project=${encodeURIComponent(proj())}`, {
                        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
                    });
                    const data = await res.json().catch(() => ({}));
                    if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
                    flash('Confirmed — proposals applied.', 'text-green');
                    setTimeout(load, 600);
                } catch (e) { flash('Confirm failed: ' + e.message, 'text-danger'); }
            });
            el('needs-dismiss').addEventListener('click', async () => {
                flash('Dismissing…');
                try {
                    const res = await fetch(`api/inbox/${it.payload.inbox_id}/dismiss?project=${encodeURIComponent(proj())}`, { method: 'POST' });
                    if (!res.ok) throw new Error(`HTTP ${res.status}`);
                    flash('Dismissed.', 'text-green');
                    setTimeout(load, 600);
                } catch (e) { flash('Dismiss failed: ' + e.message, 'text-danger'); }
            });
            el('needs-open').addEventListener('click', () => {
                const t = document.querySelector('a[href="#tab-inbox"]');
                if (t && window.bootstrap) window.bootstrap.Tab.getOrCreateInstance(t).show();
            });
        } else if (isProvider) {
            const decide = async (choice) => {
                if (delivering) return;
                delivering = true;
                detail.querySelectorAll('[data-choice]').forEach((node) => { node.disabled = true; });
                flash('Delivering decision…');
                try {
                    const body = {
                        expected_version: it.payload.version,
                        choice,
                        idempotency_key: `operator-decide:${it.payload.request_id}:${JSON.stringify(choice)}`,
                    };
                    const res = await fetch(`${it.decide.path}?project=${encodeURIComponent(proj())}`, {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(body),
                    });
                    const data = await res.json().catch(() => ({}));
                    if (!res.ok) throw new Error(apiError(data, res.status));
                    tracked = Object.assign({}, it, {
                        payload: Object.assign({}, it.payload, {
                            status: (data.request && data.request.status) || data.status || 'decision_recorded',
                            version: (data.request && data.request.version) || it.payload.version,
                            delivery_receipt: (data.request && data.request.delivery_receipt) || null,
                            completion_wake: data.completion_wake || null,
                        }),
                    });
                    renderDetail(detail, tracked);
                    const wake = data.completion_wake || {};
                    if (completion && wake.status === 'failed') {
                        flash(`Decision recorded — wake retry queued${wake.last_error ? ': ' + wake.last_error : '.'}`, 'text-danger');
                        pollRequest(it.payload.request_id);
                    } else if (completion && wake.status === 'pending') {
                        flash('Decision recorded — completion wake is durably queued.', 'text-orange');
                        pollRequest(it.payload.request_id);
                    } else if (completion && wake.status === 'accepted') {
                        flash('Resuming — the fenced completion owner accepted the wake.', 'text-azure');
                        pollRequest(it.payload.request_id);
                    } else if (completion && data.request && data.request.status === 'resolved') {
                        delivering = false;
                        flash('Decision recorded — task remains blocked.', 'text-secondary');
                        setTimeout(() => load(), 900);
                    } else {
                        flash('Decision recorded — awaiting the bound provider receipt.', 'text-azure');
                        pollRequest(it.payload.request_id);
                    }
                } catch (e) {
                    delivering = false;
                    renderDetail(detail, it);
                    flash('Decide failed: ' + e.message, 'text-danger');
                }
            };
            detail.querySelectorAll('[data-choice]').forEach((btn) => btn.addEventListener('click', () => decide({ id: btn.dataset.choice })));
            const recover = el('needs-recover');
            if (recover) recover.addEventListener('click', () => pollRequest(p.request_id));
        }
        detail.querySelectorAll('[data-open-session]').forEach((button) => button.addEventListener('click', () => {
            window.dispatchEvent(new CustomEvent('switchboard:open-session', {
                detail: { session_id: button.dataset.openSession, task_id: it.task_id },
            }));
        }));
    }

    async function pollRequest(requestId) {
        try {
            const res = await fetch(`api/attention/requests/${encodeURIComponent(requestId)}?project=${encodeURIComponent(proj())}`, { cache: 'no-store' });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || data.message || `HTTP ${res.status}`);
            const request = data.request || data;
            if (tracked && tracked.payload.request_id === requestId) {
                tracked.payload.status = request.status;
                tracked.payload.version = request.version;
                tracked.payload.delivery_receipt = request.delivery_receipt;
                tracked.payload.completion_wake = request.completion_wake || null;
                tracked.payload.terminal_reason = request.terminal_reason;
                const detail = el('needs-detail');
                if (detail) renderDetail(detail, tracked);
            }
            const receipt = request.delivery_receipt || {};
            const completionReceipt = (
                tracked && tracked.kind === 'completion_human'
                && receipt.schema === 'switchboard.completion_resume_receipt.v1'
                && receipt.effect === 'resume_assessment'
                && receipt.verified === true
            );
            const providerReceipt = (
                tracked && tracked.kind !== 'completion_human'
                && request.status === 'resolved' && request.delivery_receipt
            );
            if (request.status === 'resolved' && (completionReceipt || providerReceipt)) {
                delivering = false;
                const detail = el('needs-detail');
                if (detail && tracked) renderDetail(detail, tracked);
                const flash = el('needs-flash');
                if (flash) { flash.className = 'small text-green'; flash.textContent = 'Resumed — provider receipt verified.'; }
                setTimeout(() => load(), 900);
            } else if (
                request.status === 'resolved'
                && tracked && tracked.kind === 'completion_human'
                && receipt.effect === 'remain_blocked'
            ) {
                delivering = false;
                const flash = el('needs-flash');
                if (flash) { flash.className = 'small text-secondary'; flash.textContent = 'Decision recorded — task remains blocked.'; }
                setTimeout(() => load(), 900);
            } else if (['failed', 'expired', 'cancelled', 'orphaned'].includes(request.status)) {
                delivering = false;
                const detail = el('needs-detail');
                if (detail && tracked) renderDetail(detail, tracked);
            } else {
                setTimeout(() => pollRequest(requestId), 1500);
            }
        } catch (e) {
            delivering = false;
            const flash = el('needs-flash');
            if (flash) { flash.className = 'small text-danger'; flash.textContent = 'Recovery check failed: ' + e.message; }
        }
    }

    function syncNarrowLayout() {
        const wrap = document.querySelector('.page-wrapper');
        const aside = document.querySelector('.navbar-vertical');
        if (!wrap || !aside) return;
        const needsActive = Boolean(document.querySelector('#tab-needs.active'));
        if (window.matchMedia('(max-width: 640px)').matches && needsActive) {
            wrap.style.setProperty('margin-inline-start', '0', 'important');
            wrap.style.setProperty('margin-left', '0', 'important');
            wrap.style.setProperty('width', '100%', 'important');
            return;
        }
        const rail = getComputedStyle(aside).position === 'fixed';
        const offset = !rail ? '0' : (document.body.classList.contains('tk-sidebar-collapsed') ? '4.25rem' : '15rem');
        wrap.style.setProperty('margin-inline-start', offset, 'important');
        wrap.style.removeProperty('margin-left');
        wrap.style.removeProperty('width');
    }

    // lazy init + refresh whenever the sub-tab shows (matches the app's idiom)
    const tab = document.querySelector('a[href="#tab-needs"]');
    if (tab) tab.addEventListener('shown.bs.tab', () => { syncNarrowLayout(); load(); });
    // and when the Inbox hub itself opens with Needs-you already active
    const hub = document.getElementById('toptab-inbox');
    if (hub) hub.addEventListener('shown.bs.tab', () => {
        syncNarrowLayout();
        const active = document.querySelector('#tab-inbox-hub .tk-subnav .nav-link.active');
        if (active && active.getAttribute('href') === '#tab-needs') load();
    });
    window.addEventListener('resize', syncNarrowLayout);
    window.PMAttention = { load };
})();
