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

    const SRC = {
        agent: ['#c0392b', 'ti-robot', 'Agent'],
        inbox: ['#4299e1', 'ti-mail', 'Inbound'],
        provider: ['#ae3ec9', 'ti-plug', 'Provider'],
        mission: ['#f76707', 'ti-target-arrow', 'Mission'],
        decision: ['#f59f00', 'ti-help-circle', 'Decision'],
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

    async function load() {
        const list = el('needs-list');
        if (!list) return;
        try {
            const res = await fetch(`api/attention?project=${encodeURIComponent(proj())}`, { cache: 'no-store' });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
            items = data.items || [];
        } catch (e) {
            list.innerHTML = `<div class="text-secondary p-3"><i class="ti ti-plug-connected-x me-1"></i>Could not load api/attention (${esc(e.message)})</div>`;
            return;
        }
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
            return `<div class="card mb-2 ${i.attention_id === sel ? 'border-primary' : ''}" data-nid="${esc(i.attention_id)}" style="cursor:pointer">
                <div class="card-body p-2">
                    <div class="d-flex align-items-center gap-2 mb-1">
                        <span class="sdot" style="background:${sc[0]}"></span>
                        ${i.task_id ? `<span class="tk-mono small text-secondary">${esc(i.task_id)}</span>` : ''}
                        <span class="ms-auto text-secondary small">${age(i.age_s)} ago</span>
                    </div>
                    <div style="font-weight:500;line-height:1.4">${esc(i.title)}</div>
                    <div class="text-secondary small mt-1">${sc[2]}${i.from ? ' · ' + esc(i.from) : ''}</div>
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

    function renderDetail(detail, it) {
        const sc = SRC[it.source] || ['#8b95a5', 'ti-point', ''];
        const isAgent = it.source === 'agent';
        const isInbox = it.source === 'inbox';
        detail.innerHTML = `
            <div class="d-flex align-items-start gap-2 mb-2">
                <div class="flex-fill">
                    <div class="d-flex align-items-center gap-2 flex-wrap">
                        <span class="tk-mono small text-secondary">${esc(it.attention_id)}</span>
                        <span class="stx small"><span class="sdot" style="background:${sc[0]}"></span>${sc[2]}</span>
                        ${it.from ? `<span class="text-secondary small">· from ${esc(it.from)}</span>` : ''}
                    </div>
                    ${it.task_id ? `<div class="d-flex align-items-baseline gap-2 mt-1"><span class="h3 mb-0 tk-mono">${esc(it.task_id)}</span></div>` : ''}
                </div>
            </div>
            <div class="mb-3" style="font-weight:600">${esc(it.summary || it.title)}</div>
            <div class="tk-eyebrow mb-1">Details</div>
            <div class="card card-sm mb-3"><div class="card-body p-3">${datagrid(it.payload)}</div></div>
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
                </div>` : `
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
        }
    }

    // lazy init + refresh whenever the sub-tab shows (matches the app's idiom)
    const tab = document.querySelector('a[href="#tab-needs"]');
    if (tab) tab.addEventListener('shown.bs.tab', load);
    // and when the Inbox hub itself opens with Needs-you already active
    const hub = document.getElementById('toptab-inbox');
    if (hub) hub.addEventListener('shown.bs.tab', () => {
        const active = document.querySelector('#tab-inbox-hub .tk-subnav .nav-link.active');
        if (active && active.getAttribute('href') === '#tab-needs') load();
    });
})();
