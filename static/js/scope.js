/* UI-28/UI-30: the Scope page — the gated kickoff ladder, server-recorded.
 * ============================================================================
 * Vision → PRD → Architecture → Operating rules → Scope breakdown. Approving
 * one unlocks the next; revising an upstream artifact marks dependents stale;
 * the completeness projection derives from all five.
 *
 * UI-30: state lives in the kickoff record (GET /api/kickoff; approve/revise
 * POSTs) — durable, attributed, shared across the team. Kickoff approvals are
 * advisory planning history and never gate claims or merges.
 * Self-contained: wires only inside #tab-scope, no app.js changes.
 */
(function () {
    'use strict';

    const LADDER = [
        { id: 'vision', name: 'Vision / POV' },
        { id: 'prd', name: 'PRD' },
        { id: 'arch', name: 'Architecture' },
        { id: 'rules', name: 'Operating rules' },
        { id: 'scope', name: 'Scope breakdown' },
    ];
    const SHORT = { vision: 'Vision', prd: 'PRD', arch: 'Architecture', rules: 'Operating rules', scope: 'Scope breakdown' };

    let state = null;          // last GET /api/kickoff payload
    let curSec = 'vision';

    function el(id) { return document.getElementById(id); }
    function proj() { return window.PM_PROJECT || 'maxwell'; }
    function gate(id) { return (state && state.gates || []).find((g) => g.gate === id) || { s: 'wait', version: 0 }; }
    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    }
    function flash(msg, cls) {
        const f = el('scope-flash');
        if (!f) return;
        f.className = 'alert py-2 px-3 ' + (cls || 'alert-info');
        f.textContent = msg;
        f.classList.remove('d-none');
        clearTimeout(flash._t);
        flash._t = setTimeout(() => f.classList.add('d-none'), 4000);
    }

    async function load() {
        try {
            const res = await fetch(`api/kickoff?project=${encodeURIComponent(proj())}`, { cache: 'no-store' });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
            state = data;
        } catch (e) {
            flash('Could not load the kickoff record: ' + e.message, 'alert-danger');
            state = { gates: LADDER.map((a, i) => ({ gate: a.id, order: i, s: i ? 'wait' : 'now', version: 0 })), frontier: 'vision', build_authorized: false };
        }
        render();
    }

    async function post(action, gid) {
        try {
            const res = await fetch(`api/kickoff/${encodeURIComponent(gid)}/${action}?project=${encodeURIComponent(proj())}`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
            state = data;
            render();
            if (action === 'approve') {
                flash('Approved: ' + SHORT[gid] + ' — recorded on the project.'
                    + (state.build_authorized ? ' All 5 gates green.' : ''), 'alert-success');
            } else {
                flash(SHORT[gid] + ' revised — downstream approvals are stale until re-approved.', 'alert-warning');
            }
        } catch (e) {
            flash(action + ' failed: ' + e.message, 'alert-danger');
        }
    }

    function fmtWhen(ts) {
        if (!ts) return '';
        try { return new Date(ts * 1000).toLocaleDateString(); } catch (e) { return ''; }
    }

    function render() {
        if (!state) return;
        const fr = state.frontier;
        el('scope-rail').innerHTML = LADDER.map((a, i) => {
            const g = gate(a.id);
            let cls, node, meta;
            if (g.s === 'ok') { cls = 'done'; node = '<i class="ti ti-check"></i>'; meta = 'Approved v' + g.version + (g.approved_by ? ' · ' + esc(g.approved_by) : ''); }
            else if (a.id === fr && g.s !== 'stale') { cls = 'now'; node = String(i + 1); meta = 'Ready to approve'; }
            else if (g.s === 'stale') { cls = 'stale'; node = '<i class="ti ti-alert-triangle"></i>'; meta = 'Stale — re-approve'; }
            else { cls = 'blocked'; node = '<i class="ti ti-lock" style="font-size:.625rem"></i>'; meta = 'Locked'; }
            return `<button type="button" class="rail-step ${cls}${a.id === curSec ? ' active' : ''}" data-gate="${a.id}">
                <span class="rn">${node}</span><span class="rt">${SHORT[a.id]}</span><span class="rm">${meta}</span></button>`;
        }).join('');
        el('scope-rail').querySelectorAll('[data-gate]').forEach((b) =>
            b.addEventListener('click', () => showSection(b.dataset.gate)));

        ['vision', 'prd', 'arch', 'rules'].forEach((id) => {
            const b = document.querySelector('#scope-switch button[data-sec="' + id + '"]');
            if (!b) return;
            const g = gate(id); const i = LADDER.findIndex((a) => a.id === id); const stEl = b.querySelector('.st');
            b.classList.remove('done', 'now', 'stale');
            if (g.s === 'ok') { b.classList.add('done'); stEl.innerHTML = '<i class="ti ti-check"></i>'; }
            else if (id === fr && g.s !== 'stale') { b.classList.add('now'); stEl.textContent = String(i + 1); }
            else if (g.s === 'stale') { b.classList.add('stale'); stEl.textContent = '!'; }
            else stEl.innerHTML = '<i class="ti ti-lock" style="font-size:.5rem"></i>';
        });

        ['vision', 'prd', 'arch', 'rules'].forEach((id) => {
            const f = document.querySelector('.scope-foot[data-foot="' + id + '"]');
            if (!f) return;
            const g = gate(id); const i = LADDER.findIndex((a) => a.id === id);
            f.className = 'scope-foot';
            if (g.s === 'ok') {
                f.classList.add('done');
                f.innerHTML = `<i class="ti ti-circle-check"></i><span>Approved v${g.version}${g.approved_by ? ' · ' + esc(g.approved_by) : ''}${g.approved_at ? ' · ' + fmtWhen(g.approved_at) : ''}</span>
                    <a href="#" class="ms-2" data-revise="${id}">revise</a>`;
            } else if (id === fr && g.s !== 'stale') {
                f.innerHTML = `<span class="text-secondary flex-fill">Reviewed it? Approve to unlock the next artifact.</span>
                    <button type="button" class="btn btn-sm btn-primary" data-approve="${id}">Approve</button>`;
            } else if (g.s === 'stale') {
                f.innerHTML = `<span class="text-secondary flex-fill">Superseded upstream — re-approve to restore the chain.</span>
                    <button type="button" class="btn btn-sm btn-primary" data-approve="${id}">Re-approve</button>`;
            } else {
                f.classList.add('blocked');
                f.innerHTML = `<i class="ti ti-lock"></i><span>Unlocks after ${LADDER[i - 1].name} is approved.</span>`;
            }
        });
        document.querySelectorAll('.scope-foot [data-approve]').forEach((b) =>
            b.addEventListener('click', () => post('approve', b.dataset.approve)));
        document.querySelectorAll('.scope-foot [data-revise]').forEach((b) =>
            b.addEventListener('click', (e) => { e.preventDefault(); post('revise', b.dataset.revise); }));

        // verdict + the fifth gate
        const authed = !!state.build_authorized;
        const remaining = (state.gates || []).filter((g) => g.s !== 'ok').length;
        const v = el('scope-verdict'); const vi = el('scope-verdict-icon');
        const vt = el('scope-verdict-title'); const vs = el('scope-verdict-sub');
        v.className = 'scope-verdict' + (authed ? ' ok' : '');
        if (authed) {
            vi.className = 'ti ti-lock-open'; vi.style.color = '#2fb344';
            vt.textContent = 'Kickoff record: complete (advisory)';
            vs.innerHTML = 'All 5 gates green, on the record. Kickoff approvals are advisory and do not gate Autopilot.';
        } else {
            vi.className = 'ti ti-notes'; vi.style.color = '#f76707';
            vt.textContent = 'Kickoff record: incomplete (advisory)';
            vs.innerHTML = remaining + ' gate' + (remaining > 1 ? 's' : '') + ' open — next: <strong>'
                + (fr ? SHORT[fr] : '') + '</strong>. Recorded on the project; claims and merges remain available.';
        }
        const sg = el('scope-gate-row');
        if (sg) {
            const g = gate('scope');
            if (g.s === 'ok') sg.innerHTML = '<span class="stx"><span class="sdot" style="background:#2fb344"></span>Scope breakdown approved v' + g.version + '.</span>';
            else if (fr === 'scope') sg.innerHTML = '<span class="stx"><span class="sdot" style="background:#f76707"></span>Scope breakdown is next — review the task graph in Plan, then</span> <button type="button" class="btn btn-sm btn-primary ms-2" data-approve-scope>Approve breakdown</button>';
            else if (g.s === 'stale') sg.innerHTML = '<span class="stx"><span class="sdot" style="background:#f76707"></span>Scope breakdown is stale — re-approve after the upstream revision.</span> <button type="button" class="btn btn-sm btn-primary ms-2" data-approve-scope>Re-approve</button>';
            else sg.innerHTML = '<span class="stx"><span class="sdot" style="background:#8b95a5"></span>Scope breakdown — the fifth gate; locked until the four artifacts are approved.</span>';
            const ab = sg.querySelector('[data-approve-scope]');
            if (ab) ab.addEventListener('click', () => post('approve', 'scope'));
        }
    }

    function showSection(id) {
        if (id === 'scope') { flash('The scope breakdown is the task graph — review it in Plan; approve it from the gate line below the rail.'); return; }
        curSec = id;
        document.querySelectorAll('.scope-sec').forEach((s) => s.classList.toggle('on', s.id === 'scope-sec-' + id));
        document.querySelectorAll('#scope-switch button').forEach((b) => b.classList.toggle('on', b.dataset.sec === id));
        document.querySelectorAll('#scope-rail .rail-step').forEach((r) => r.classList.toggle('active', r.dataset.gate === id));
    }

    function init() {
        if (init._wired) { load(); return; }
        init._wired = true;
        document.querySelectorAll('#scope-switch button').forEach((b) =>
            b.addEventListener('click', () => showSection(b.dataset.sec)));
        const refresh = el('scope-refresh');
        if (refresh) refresh.addEventListener('click', (e) => { e.preventDefault(); load(); });
        load(); showSection(curSec);
    }

    const tab = document.getElementById('toptab-scope');
    if (tab) tab.addEventListener('shown.bs.tab', init);
})();
