/* === Multi-project routing (added) ========================================================
   Resolve the active project ONCE (URL ?project= → localStorage → 'maxwell'), then tag every
   relative api/* request with it. Reads default to Maxwell server-side; writes are fail-closed
   (require an explicit project), so a stale selection can never write into the wrong board. */
(function () {
    var fromUrl = null;
    try { fromUrl = new URL(window.location.href).searchParams.get('project'); } catch (e) {}
    var stored = null;
    try { stored = localStorage.getItem('pm_project'); } catch (e) {}
    var proj = ((fromUrl || stored || 'maxwell') + '').trim() || 'maxwell';
    try { localStorage.setItem('pm_project', proj); } catch (e) {}
    window.PM_PROJECT = proj;
    var _fetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
        try {
            if (typeof input === 'string' && /^\/?api\//.test(input) && !/[?&]project=/.test(input)) {
                input += (input.indexOf('?') >= 0 ? '&' : '?') + 'project=' + encodeURIComponent(window.PM_PROJECT);
            }
        } catch (e) {}
        return _fetch(input, init);
    };
})();

/**
 * Project Maxwell — TEEP Barnett Phase-1 Pilot Plan board
 * ============================================================================
 * Thin client. Fetches the static plan artifact (data/teep-project-plan.json)
 * and renders it into Tabler components as a Monday-style Kanban board grouped
 * by lifecycle phase, plus tables for milestones, critical path, risks, and
 * open decisions.
 *
 * No domain logic. No fabricated data. Filtering is presentation-only (UX).
 * If the artifact cannot be loaded, a Tabler alert is shown.
 */
const TeepPlan = {
    plan: null,
    tasks: [],          // flattened: every task + _wsId / _wsName
    tally: null,        // project-level spend/outcome/KPI rollup
    wsMeta: {},         // workstream_id -> {name, lead_org}
    gantt: null,        // ApexCharts instance
    ganttMode: 'task',  // default 'task' (per-task detail) · 'workstream' = 12-bar overview

    PHASES: ['Kickoff', 'Bootstrap', 'Build', 'Cutover', 'Operate'],
    PHASE_COLOR: { Kickoff: 'azure', Bootstrap: 'purple', Build: 'blue', Cutover: 'orange', Operate: 'green',
                   'Wave 1': 'azure', 'Wave 2': 'blue', 'Wave 3': 'orange', 'Wave 4': 'green' },
    PHASE_HEX: { Kickoff: '#4299e1', Bootstrap: '#ae3ec9', Build: '#066fd1', Cutover: '#f76707', Operate: '#2fb344',
                 'Wave 1': '#4299e1', 'Wave 2': '#066fd1', 'Wave 3': '#f76707', 'Wave 4': '#2fb344' },
    OWNER_COLOR: { 'Taikun': 'blue', 'TEEP': 'teal', 'Sensirion/Nubo': 'orange', 'IFS Merrick': 'purple', 'Joint': 'cyan' },
    RISK_COLOR: { Low: 'green', Medium: 'yellow', High: 'red' },
    STATUS_COLOR: { 'Not Started': 'secondary', 'In Progress': 'blue', 'In Review': 'azure', 'Blocked': 'red', 'Done': 'green' },
    WS_COLOR: {
        SEN: 'azure', FMP: 'blue', SCADA: 'cyan', IFS: 'teal', SSO: 'indigo', BEDROCK: 'purple',
        GW: 'pink', REG: 'lime', AGENT: 'orange', REPORT: 'yellow', DATA: 'green', CUTOVER: 'red'
    },
    WS_HEX: {
        SEN: '#4299e1', FMP: '#066fd1', SCADA: '#17a2b8', IFS: '#0ca678', SSO: '#4263eb', BEDROCK: '#ae3ec9',
        GW: '#d6336c', REG: '#74b816', AGENT: '#f76707', REPORT: '#f59f00', DATA: '#2fb344', CUTOVER: '#d63939'
    },
    OWNER_ORGS: ['Taikun', 'TEEP', 'Sensirion/Nubo', 'IFS Merrick', 'Joint'],
    // Drives the edit + create forms, reading, and applying agent proposals.
    EDIT_FIELDS: [
        { k: 'title', label: 'Title', type: 'text', col: 'col-12' },
        { k: 'description', label: 'Description', type: 'textarea', col: 'col-12' },
        { k: 'phase', label: 'Phase', type: 'select', opts: ['Kickoff', 'Bootstrap', 'Build', 'Cutover', 'Operate'], col: 'col-6 col-md-3' },
        { k: 'status', label: 'Status', type: 'select', opts: ['Not Started', 'In Progress', 'In Review', 'Blocked', 'Done'], col: 'col-6 col-md-3' },
        { k: 'risk_level', label: 'Risk', type: 'select', opts: ['Low', 'Medium', 'High'], col: 'col-6 col-md-3' },
        { k: 'is_blocking', label: 'Blocking', type: 'switch', col: 'col-6 col-md-3' },
        { k: 'owner_org', label: 'Owner org', type: 'select', opts: ['Taikun', 'TEEP', 'Sensirion/Nubo', 'IFS Merrick', 'Joint'], col: 'col-6 col-md-4' },
        { k: 'owner_person_or_role', label: 'Owner', type: 'text', col: 'col-6 col-md-4' },
        { k: 'assignee', label: 'Assignee', type: 'people', col: 'col-6 col-md-4' },
        { k: 'effort_days', label: 'Effort (d)', type: 'number', col: 'col-4' },
        { k: 'start_date', label: 'Start', type: 'date', col: 'col-4' },
        { k: 'finish_date', label: 'Finish', type: 'date', col: 'col-4' },
        { k: 'entry_criteria', label: 'Entry criteria', type: 'textarea', col: 'col-12' },
        { k: 'exit_criteria', label: 'Exit criteria', type: 'textarea', col: 'col-12' },
        { k: 'deliverable', label: 'Deliverable', type: 'textarea', col: 'col-12' },
    ],

    _fieldHtml(f, val, prefix) {
        const id = prefix + f.k;
        const v = val == null ? '' : val;
        if (f.type === 'textarea')
            return `<label class="form-label small mb-1">${f.label}</label><textarea id="${id}" class="form-control form-control-sm" rows="2">${this.esc(v)}</textarea>`;
        if (f.type === 'select')
            return `<label class="form-label small mb-1">${f.label}</label><select id="${id}" class="form-select form-select-sm">`
                + `<option value=""></option>` + f.opts.map((o) => `<option${o === v ? ' selected' : ''}>${this.esc(o)}</option>`).join('') + `</select>`;
        if (f.type === 'switch')
            return `<label class="form-label small d-block mb-1">${f.label}</label><label class="form-check form-switch m-0"><input id="${id}" class="form-check-input" type="checkbox"${val ? ' checked' : ''}/></label>`;
        const itype = f.type === 'people' ? 'text' : f.type;
        const extra = f.type === 'people' ? ' list="people-list"' : (f.type === 'number' ? ' step="0.5" min="0"' : '');
        return `<label class="form-label small mb-1">${f.label}</label><input id="${id}" type="${itype}" class="form-control form-control-sm" value="${this.esc(v)}"${extra}/>`;
    },

    _taskFormHtml(t, prefix) {
        return `<div class="row g-2">` + this.EDIT_FIELDS.map((f) =>
            `<div class="${f.col}">${this._fieldHtml(f, t[f.k], prefix)}</div>`).join('') + `</div>`;
    },

    _readForm(prefix) {
        const out = {};
        this.EDIT_FIELDS.forEach((f) => {
            const el = document.getElementById(prefix + f.k);
            if (!el) return;
            if (f.type === 'switch') out[f.k] = el.checked;
            else if (f.type === 'number') { const n = parseFloat(el.value); out[f.k] = isNaN(n) ? null : n; }
            else out[f.k] = el.value === '' ? null : el.value;
        });
        return out;
    },

    async init() {
        try { await this.applyProject(); } catch (e) { /* switcher is best-effort */ }
        try {
            const res = await fetch('api/board');
            if (!res.ok) throw new Error(`HTTP ${res.status} loading the board`);
            this.plan = await res.json();
            try { this.people = (await (await fetch('api/people')).json()).people || []; } catch (e) { this.people = []; }
            try { this.tally = await (await fetch(`tally/v1/project?project=${encodeURIComponent(window.PM_PROJECT || 'maxwell')}`)).json(); } catch (e) { this.tally = null; }
        } catch (err) {
            this.showError(err.message);
            return;
        }
        this.flatten();
        this.PHASES = this.derivePhases();
        this.renderGenerated();
        this.renderStats();
        this.renderAbout();
        this.buildFilters();
        const dl = document.getElementById('people-list');
        if (dl) dl.innerHTML = (this.people || []).map((p) => `<option value="${this.esc(p)}"></option>`).join('');
        this.renderBoard();
        this.renderTasks();
        this.renderEpics();
        this.renderTables();
        this.renderExec();
        this.renderTallyPulse();
        this.wireEvents();
        this.setupGantt();
        this.loadSignals();
        this.initInbox();
        const ds = document.getElementById('data-status');
        if (ds) { ds.className = 'badge bg-green-lt'; ds.textContent = `${this.tasks.length} tasks`; }
    },

    flatten() {
        this.tasks = [];
        (this.plan.workstreams || []).forEach((w) => {
            this.wsMeta[w.workstream_id] = { name: w.name, lead_org: w.lead_org };
            (w.tasks || []).forEach((t) => {
                this.tasks.push(Object.assign({}, t, { _wsId: w.workstream_id, _wsName: w.name }));
            });
        });
    },

    // ---- multi-project: populate the switcher, drive the header -----------
    async applyProject() {
        const cur = window.PM_PROJECT || 'maxwell';
        let list = [{ id: 'maxwell', label: 'Project Maxwell', pretitle: '' }];
        try { list = (await (await fetch('api/projects')).json()).projects || list; } catch (e) { /* offline */ }
        const sel = document.getElementById('project-switcher');
        if (sel) {
            sel.innerHTML = list.map((p) =>
                `<option value="${this.esc(p.id)}"${p.id === cur ? ' selected' : ''}>${this.esc(p.label)}</option>`).join('');
            if (!sel._wired) {
                sel._wired = true;
                sel.addEventListener('change', () => {
                    const id = sel.value || 'maxwell';
                    try { localStorage.setItem('pm_project', id); } catch (e) {}
                    const u = new URL(window.location.href);
                    u.searchParams.set('project', id);
                    window.location.href = u.toString();   // full reload re-renders everything for the picked project
                });
            }
        }
        // Data-drive the header for non-default projects; leave Maxwell's static header pixel-identical.
        const meta = list.find((p) => p.id === cur);
        if (meta && cur !== 'maxwell') {
            const t = document.querySelector('.page-title'); if (t) t.textContent = meta.label;
            const pt = document.querySelector('.page-pretitle'); if (pt && meta.pretitle) pt.textContent = meta.pretitle;
            document.title = `${meta.label} | Taikun Atlas`;
        }
    },

    // Board/overview columns come from the phases actually present: Maxwell's 5 lifecycle phases,
    // or Helm's "Wave 1..4". Falls back to the canonical 5 when a plan has no/standard phases.
    derivePhases() {
        const canon = ['Kickoff', 'Bootstrap', 'Build', 'Cutover', 'Operate'];
        const present = [];
        ((this.plan && this.plan.workstreams) || []).forEach((w) => {
            (w.tasks || []).forEach((t) => { if (t.phase && present.indexOf(t.phase) < 0) present.push(t.phase); });
        });
        if (!present.length) return canon;
        if (present.every((p) => canon.indexOf(p) >= 0)) return canon;
        return present.sort((a, b) => String(a).localeCompare(String(b), undefined, { numeric: true }));
    },

    // ---- small helpers ---------------------------------------------------
    esc(s) {
        if (s === null || s === undefined) return '';
        return String(s).replace(/[&<>"']/g, (c) => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
        ));
    },
    money(v) {
        if (v === null || v === undefined || v === '') return '—';
        const n = Number(v);
        if (!isFinite(n)) return '—';
        return '$' + n.toLocaleString(undefined, { maximumFractionDigits: n >= 100 ? 0 : 2 });
    },
    compact(v) {
        const n = Number(v || 0);
        return n.toLocaleString(undefined, { maximumFractionDigits: 1 });
    },
    taskTally(id) {
        const rows = (this.tally && this.tally.by_task) || [];
        return rows.find((x) => x.task_id === id) || null;
    },
    tallyMini(tally) {
        if (!tally) return '';
        const spend = tally.spend || {};
        const outcomes = tally.outcomes || {};
        const cost = Number(spend.cost_usd || 0);
        const verified = Number(outcomes.verified || 0);
        if (!cost && !verified) return '';
        const bits = [];
        if (cost) bits.push(`<span title="Tally spend"><i class="ti ti-cash me-1"></i>${this.money(cost)}</span>`);
        if (verified) bits.push(`<span title="Verified outcomes"><i class="ti ti-target-arrow me-1"></i>${this.compact(verified)}</span>`);
        const cpo = tally.unit_cost && tally.unit_cost.cost_per_verified_outcome;
        if (cpo != null) bits.push(`<span title="Cost per verified outcome">${this.money(cpo)}/outcome</span>`);
        return bits.join(' · ');
    },
    tallyDetailHtml(tally) {
        if (!tally) return `<div class="text-secondary small mb-3">No spend or outcomes recorded.</div>`;
        const spend = tally.spend || {};
        const outcomes = tally.outcomes || {};
        const unit = tally.unit_cost || {};
        const kpis = tally.kpis || [];
        const metric = (label, value, sub, icon) => `<div class="col-6 col-lg-3"><div class="card card-sm"><div class="card-body p-3">
            <div class="subheader text-secondary"><i class="ti ti-${icon} me-1"></i>${label}</div>
            <div class="h2 mb-0">${value}</div>
            <div class="text-secondary small">${sub}</div>
        </div></div></div>`;
        const kpiLine = kpis.length ? `<div class="small text-secondary mt-2">${kpis.map((k) =>
            `${this.esc(k.name || k.kpi_id || 'KPI')}: ${this.compact(k.verified_contribution || 0)} ${this.esc(k.unit || '')}`).join(' · ')}</div>` : '';
        return `<div class="row g-2 mb-2">
            ${metric('Spend', this.money(spend.cost_usd || 0), `${this.compact(spend.total_tokens || 0)} tokens`, 'cash')}
            ${metric('Verified', this.compact(outcomes.verified || 0), `${this.compact(outcomes.proposed || 0)} proposed`, 'target-arrow')}
            ${metric('Cost / outcome', this.money(unit.cost_per_verified_outcome), 'verified only', 'receipt-2')}
            ${metric('KPI movement', this.compact(tally.verified_kpi_contribution || 0), `${kpis.length} linked KPI${kpis.length === 1 ? '' : 's'}`, 'chart-arrows-vertical')}
        </div>${kpiLine}`;
    },
    controlTruthHtml(t) {
        const dep = (t && t.dependency_state) || {};
        const rat = (t && t.rationale_state) || {};
        const ident = (t && t.identity) || {};
        const terminal = (t && t.terminal_state) || {};
        const depStatus = dep.satisfied
            ? (dep.ready ? 'Ready' : 'Dependencies satisfied')
            : `${dep.blocked_by_count || 0} blocking`;
        const depClass = dep.satisfied ? 'green' : 'red';
        const rationaleStatus = terminal.terminal
            ? 'Terminal truth wins'
            : (rat.stale ? 'Stale rationale ignored' : 'Rationale current');
        const rationaleClass = rat.stale && !terminal.terminal ? 'yellow' : 'green';
        const identityStatus = terminal.terminal
            ? 'Terminal Done'
            : (ident.status && ident.status !== 'clear'
            ? (ident.takeover_safe === false ? 'Identity takeover risk' : this.esc(ident.status))
            : 'Identity clear');
        const identityClass = (!terminal.terminal && ident.status && ident.status !== 'clear') ? 'red' : 'green';
        const deps = (dep.dependencies || []).map((d) => {
            const cls = d.done ? 'green' : (d.missing ? 'red' : 'yellow');
            return `<span class="badge bg-${cls}-lt me-1">${this.esc(d.task_id)}${d.status ? ` · ${this.esc(d.status)}` : ''}</span>`;
        }).join('') || '<span class="text-secondary">none</span>';
        const flags = (rat.flags || []).map((f) => `<span class="badge bg-yellow-lt me-1">${this.esc(f)}</span>`).join('');
        const suppressed = terminal.suppressed_derived
            ? `<div class="col-12 small text-secondary">Historical derived state suppressed: ${this.esc(Object.keys(terminal.suppressed_derived).join(', '))}</div>`
            : '';
        return `<div class="card mb-3" id="control-truth-panel">
            <div class="card-header py-2">
                <div class="d-flex align-items-center gap-2">
                    <i class="ti ti-shield-check text-green"></i>
                    <span class="fw-semibold">Board truth</span>
                </div>
            </div>
            <div class="card-body py-3">
                <div class="row g-2">
                    <div class="col-md-4"><span class="badge bg-${depClass}-lt">${this.esc(depStatus)}</span></div>
                    <div class="col-md-4"><span class="badge bg-${rationaleClass}-lt">${this.esc(rationaleStatus)}</span></div>
                    <div class="col-md-4"><span class="badge bg-${identityClass}-lt">${identityStatus}</span></div>
                    <div class="col-12 small">${deps}</div>
                    ${flags ? `<div class="col-12 small">${flags}</div>` : ''}
                    ${suppressed}
                </div>
            </div>
        </div>`;
    },
    monitorControlHtml(t) {
        return `<div class="card mb-3" id="task-monitor-panel" data-task-id="${this.esc(t.task_id)}">
            <div class="card-header py-2">
                <div class="d-flex align-items-center gap-2">
                    <i class="ti ti-radar text-orange"></i>
                    <span class="fw-semibold">Monitors</span>
                    <span id="task-monitor-count" class="badge bg-secondary-lt">loading</span>
                    <span id="task-monitor-flash" class="small text-secondary ms-auto"></span>
                </div>
            </div>
            <div id="task-monitor-body" class="card-body py-3">
                <div class="text-secondary small">Loading monitors…</div>
            </div>
        </div>`;
    },
    claimControlHtml(t) {
        const claims = (t.active_claims || []);
        if (!claims.length) {
            return `<div class="alert alert-secondary d-flex align-items-center py-2 mb-3">
                <i class="ti ti-lock-open me-2"></i><span class="small">No active task claim.</span>
            </div>`;
        }
        const rows = claims.map((c) => {
            const exp = c.expires_at ? new Date(c.expires_at * 1000).toLocaleString() : '—';
            return `<tr>
                <td><span class="font-monospace">${this.esc(c.claim_id)}</span></td>
                <td>${this.esc(c.agent_id)}</td>
                <td class="text-secondary">${this.esc(exp)}</td>
            </tr>`;
        }).join('');
        const primary = claims[0];
        return `<div class="card mb-3" id="claim-control" data-claim-id="${this.esc(primary.claim_id)}">
            <div class="card-header py-2">
                <div class="d-flex align-items-center gap-2">
                    <i class="ti ti-hand-stop text-red"></i>
                    <span class="fw-semibold">Claim control</span>
                    <span class="badge bg-red-lt">${claims.length} active</span>
                </div>
            </div>
            <div class="table-responsive"><table class="table table-sm card-table mb-0">
                <thead><tr><th>Claim</th><th>Holder</th><th>Expires</th></tr></thead><tbody>${rows}</tbody>
            </table></div>
            <div class="card-body border-top py-3">
                <div class="row g-2">
                    <div class="col-12 col-md-5"><label class="form-label small mb-1">Reason</label><input id="claim-revoke-reason" class="form-control form-control-sm" value="operator override"></div>
                    <div class="col-6 col-md-4"><label class="form-label small mb-1">Redirect agent</label><input id="claim-revoke-reassign" class="form-control form-control-sm" placeholder="codex/DISPATCH-3"></div>
                    <div class="col-6 col-md-3"><label class="form-label small mb-1">Sort order</label><input id="claim-revoke-sort" class="form-control form-control-sm" type="number" min="1" step="1" value="${this.esc(t.sort_order || '')}"></div>
                    <div class="col-12"><label class="form-label small mb-1">Partial evidence</label><textarea id="claim-revoke-evidence" class="form-control form-control-sm" rows="2" placeholder='{"branch":"...","head_sha":"..."}'></textarea></div>
                </div>
                <div class="btn-list mt-3">
                    <button id="claim-revoke-btn" class="btn btn-danger btn-sm"><i class="ti ti-ban me-1"></i>Revoke claim</button>
                    <span id="claim-revoke-flash" class="small text-secondary"></span>
                </div>
            </div>
        </div>`;
    },
    runnerControlHtml(t) {
        return `<div class="card mb-3" id="runner-control-panel" data-task-id="${this.esc(t.task_id)}">
            <div class="card-header py-2">
                <div class="d-flex align-items-center gap-2">
                    <i class="ti ti-player-play text-azure"></i>
                    <span class="fw-semibold">Runner sessions</span>
                    <span id="runner-control-count" class="badge bg-secondary-lt">loading</span>
                    <span id="runner-control-flash" class="small text-secondary ms-auto"></span>
                </div>
            </div>
            <div id="runner-control-body" class="card-body py-3">
                <div class="text-secondary small">Loading runner sessions…</div>
            </div>
        </div>`;
    },
    badge(text, color, light) {
        const cls = light === false ? `bg-${color}` : `bg-${color}-lt`;
        return `<span class="badge ${cls}">${this.esc(text)}</span>`;
    },
    provenanceBadge(t) {
        const p = (t && t.provenance) || {};
        if (p.type !== 'offline_evidence') return '';
        const title = [
            p.verifier ? `verified by ${p.verifier}` : 'verified offline',
            p.evidence_hash ? `hash ${String(p.evidence_hash).slice(0, 12)}` : '',
        ].filter(Boolean).join(' · ');
        return `<span class="badge bg-teal-lt" title="${this.esc(title)}"><i class="ti ti-clipboard-check me-1"></i>offline evidence</span>`;
    },
    provenanceDetail(t) {
        const p = (t && t.provenance) || {};
        if (!p.type) return '<span class="text-secondary">none</span>';
        if (p.type === 'offline_evidence') {
            const parts = [this.provenanceBadge(t)];
            if (p.verifier) parts.push(`<span class="text-secondary small">by ${this.esc(p.verifier)}</span>`);
            if (p.artifact_url) parts.push(`<a class="small" href="${this.esc(p.artifact_url)}" target="_blank" rel="noopener">artifact</a>`);
            if (p.evidence_hash) parts.push(`<span class="font-monospace small">${this.esc(String(p.evidence_hash).slice(0, 16))}</span>`);
            return parts.join(' ');
        }
        if (p.type === 'github_pr_merged') return `<span class="badge bg-green-lt"><i class="ti ti-git-merge me-1"></i>PR merged</span>`;
        if (p.type === 'default_branch_commit') return `<span class="badge bg-green-lt"><i class="ti ti-git-commit me-1"></i>default branch</span>`;
        if (p.type === 'github_pr_open') return `<span class="badge bg-azure-lt"><i class="ti ti-git-pull-request me-1"></i>PR evidence</span>`;
        return `<span class="badge bg-secondary-lt">${this.esc(p.label || p.type)}</span>`;
    },
    initials(name) {
        if (!name) return '';
        // drop "(TEEP)"-style org tags and any word without a letter (e.g. "+", "·")
        const words = String(name).replace(/\([^)]*\)/g, ' ').split(/\s+/)
            .filter((w) => /[A-Za-z]/.test(w));
        return words.slice(0, 2).map((p) => (p.match(/[A-Za-z]/) || [''])[0].toUpperCase()).join('');
    },

    renderGenerated() {
        const el = document.getElementById('plan-generated');
        if (el && this.plan.generated) el.textContent = `Generated ${this.plan.generated}`;
    },

    showError(msg) {
        const ds = document.getElementById('data-status');
        if (ds) { ds.className = 'badge bg-red-lt'; ds.textContent = 'Load failed'; }
        const box = document.getElementById('plan-error');
        if (box) {
            box.innerHTML = `<div class="alert alert-danger" role="alert">
                <div class="d-flex"><div><i class="ti ti-alert-circle me-2"></i></div>
                <div><h4 class="alert-title">Could not load the project plan</h4>
                <div class="text-secondary">${this.esc(msg)} — the taikun-pm API (<code>api/board</code>) is unreachable.</div></div></div></div>`;
        }
    },

    // ---- rollup stats ----------------------------------------------------
    renderStats() {
        const r = this.plan.rollups || {};
        const cards = [
            { label: 'Workstreams', value: r.total_workstreams, icon: 'ti-stack-2' },
            { label: 'Tasks', value: r.total_tasks, icon: 'ti-checklist' },
            { label: 'Effort (person-days)', value: r.total_effort_days, icon: 'ti-clock-hour-4' },
            { label: 'Critical-path tasks', value: (this.plan.critical_path || []).length, icon: 'ti-route' },
            { label: 'Milestones', value: (this.plan.milestones || []).length, icon: 'ti-flag' },
            { label: 'Open decisions', value: (this.plan.consolidated_decisions || []).length, icon: 'ti-help-circle' },
        ];
        document.getElementById('plan-stats').innerHTML = cards.map((c) => `
            <div class="col-6 col-sm-4 col-xl-2">
                <div class="card card-sm">
                    <div class="card-body">
                        <div class="d-flex align-items-center">
                            <div class="subheader">${this.esc(c.label)}</div>
                            <div class="ms-auto text-secondary"><i class="ti ${c.icon}"></i></div>
                        </div>
                        <div class="h1 mb-0 mt-1">${this.esc(c.value)}</div>
                    </div>
                </div>
            </div>`).join('');
    },

    renderAbout() {
        const el = document.getElementById('about-content');
        if (!el) return;
        const exec = (this.plan.executive_summary || '').split('\n').filter(Boolean);
        const tl = this.plan.timeline_note || '';
        el.innerHTML =
            exec.map((p) => `<p>${this.esc(p)}</p>`).join('') +
            (tl ? `<h4 class="mt-3">Timeline &amp; realistic duration</h4><p class="text-secondary">${this.esc(tl)}</p>` : '');
    },

    // ---- filters ---------------------------------------------------------
    buildFilters() {
        const ws = document.getElementById('f-ws');
        ws.innerHTML = `<option value="">All workstreams</option>` +
            (this.plan.workstreams || []).map((w) =>
                `<option value="${this.esc(w.workstream_id)}">${this.esc(w.workstream_id)} — ${this.esc(w.name)}</option>`).join('');
        const owners = (this.plan.owner_orgs || []).slice();
        document.getElementById('f-owner').innerHTML = `<option value="">All orgs</option>` +
            owners.map((o) => `<option value="${this.esc(o)}">${this.esc(o)}</option>`).join('');
        this.refreshAssignees();
        document.getElementById('f-risk').innerHTML = `<option value="">All risk</option>` +
            ['High', 'Medium', 'Low'].map((x) => `<option value="${x}">${x} risk</option>`).join('');
    },

    refreshAssignees() {
        const sel = document.getElementById('f-assignee');
        if (!sel) return;
        const cur = sel.value;
        const names = [...new Set(this.tasks.flatMap((t) => this._peopleOf(t)).filter((n) => n !== 'Unassigned'))].sort();
        sel.innerHTML = `<option value="">All owners</option>` +
            names.map((a) => `<option value="${this.esc(a)}"${a === cur ? ' selected' : ''}>${this.esc(a)}</option>`).join('');
    },

    isHideDone() { const hd = document.getElementById('f-hidedone'); return !!(hd && hd.checked); },

    filtered(includeDone) {
        const q = (document.getElementById('f-search').value || '').trim().toLowerCase();
        const ws = document.getElementById('f-ws').value;
        const owner = document.getElementById('f-owner').value;
        const ownerPerson = document.getElementById('f-assignee').value;
        const risk = document.getElementById('f-risk').value;
        const blocking = document.getElementById('f-blocking').checked;
        const hideDone = !includeDone && this.isHideDone();
        return this.tasks.filter((t) => {
            if (hideDone && t.status === 'Done') return false;
            if (ws && t._wsId !== ws) return false;
            if (owner && t.owner_org !== owner) return false;
            if (ownerPerson && !this._peopleOf(t).includes(ownerPerson)) return false;
            if (risk && t.risk_level !== risk) return false;
            if (blocking && !t.is_blocking) return false;
            if (q) {
                const hay = `${t.task_id} ${t.title} ${t.description} ${t.owner_person_or_role} ${t._wsName}`.toLowerCase();
                if (!hay.includes(q)) return false;
            }
            return true;
        });
    },

    // ---- board -----------------------------------------------------------
    renderBoard() {
        const tasks = this.filtered();
        const board = document.getElementById('board');
        board.innerHTML = this.PHASES.map((phase) => {
            const col = tasks.filter((t) => t.phase === phase);
            const days = col.reduce((s, t) => s + (t.effort_days || 0), 0);
            const color = this.PHASE_COLOR[phase] || 'secondary';
            const cards = col.length
                ? col.map((t) => this.taskCard(t)).join('')
                : `<div class="text-secondary text-center py-4 small">—</div>`;
            return `
                <div class="col-12 col-lg">
                    <div class="d-flex align-items-center mb-3 px-1">
                        <span class="status-dot bg-${color} me-2"></span>
                        <span class="h3 m-0">${this.esc(phase)}</span>
                        <span class="badge bg-secondary-lt ms-2">${col.length}</span>
                        <span class="ms-auto text-secondary small">${Math.round(days)}d</span>
                    </div>
                    <div>${cards}</div>
                </div>`;
        }).join('');
    },

    taskCard(t) {
        const done = t.status === 'Done';
        const sc = this.STATUS_COLOR[t.status] || 'secondary';
        const deps = (t.depends_on || []).length;
        const tally = this.taskTally(t.task_id);
        const econ = this.tallyMini(tally);
        const provenance = this.provenanceBadge(t);
        const meta = [];
        if (t.owner_org) meta.push(this.esc(t.owner_org));
        if (t.effort_days != null) meta.push(this.esc(t.effort_days) + 'd');
        if (deps) meta.push(`<i class="ti ti-link"></i>${deps}`);
        return `
            <a href="#" class="d-block text-reset" data-task="${this.esc(t.task_id)}">
                <div class="card card-sm mb-2"${done ? ' style="opacity:.55"' : ''}>
                    <div class="card-status-start bg-${sc}"></div>
                    <div class="card-body">
                        <div class="d-flex align-items-center gap-2 mb-1">
                            <span class="status-dot bg-${sc}" title="${this.esc(t.status || '')}"></span>
                            <span class="text-secondary small fw-medium text-uppercase">${this.esc(t._wsId)}</span>
                            <span class="ms-auto text-secondary small font-monospace">${this.esc(t.task_id)}</span>
                        </div>
                        <div class="fw-semibold lh-sm ${done ? 'text-decoration-line-through text-secondary' : 'text-body'}">${this.esc(t.title)}</div>
                        <div class="d-flex align-items-center gap-2 mt-2 text-secondary small">
                            <span>${meta.join(' · ')}</span>
                            ${t.risk_level === 'High' ? '<span class="badge badge-outline text-red">High risk</span>' : ''}
                            ${t.is_blocking ? '<span class="text-red lh-1" title="Blocking"><i class="ti ti-alert-triangle-filled"></i></span>' : ''}
                            ${provenance}
                            ${t.assignee ? `<span class="avatar avatar-xs ms-auto" title="${this.esc(t.assignee)}">${this.esc(this.initials(t.assignee))}</span>` : ''}
                        </div>
                        ${econ ? `<div class="text-secondary small mt-2 border-top pt-2">${econ}</div>` : ''}
                    </div>
                </div>
            </a>`;
    },

    // ---- Tasks (Todoist-style "by person" lens) -------------------------
    // Same data, regrouped per person. assignee wins; else match the known
    // people list against owner_person_or_role (a task with two owners shows
    // under each). Pure presentation — no new data.
    // Group by OWNER (the person/role in owner_person_or_role) for now — NOT the
    // assignee/user field (which is empty across the plan). Match the canonical
    // people list against the owner string; a task with two owners shows under each.
    _peopleOf(t) {
        const owner = (t.owner_person_or_role || '').toLowerCase();
        if (!owner) return ['Unassigned'];
        const matched = (this.people || []).filter((p) => owner.includes(p.toLowerCase()));
        return matched.length ? matched : ['Unassigned'];
    },

    fmtDue(dateStr, done) {
        if (!dateStr) return { text: '', cls: '' };
        const d = new Date(dateStr + 'T00:00:00');
        if (isNaN(d.getTime())) return { text: this.esc(dateStr), cls: 'text-secondary' };
        const today = new Date(); today.setHours(0, 0, 0, 0);
        const diff = Math.round((d - today) / 86400000);
        const M = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
        let text = M[d.getMonth()] + ' ' + d.getDate();
        if (diff === 0) text = 'Today';
        else if (diff === 1) text = 'Tomorrow';
        else if (diff === -1) text = 'Yesterday';
        let cls = 'text-secondary';
        if (!done && diff < 0) cls = 'text-red';
        else if (!done && diff <= 1) cls = 'text-orange';
        return { text, cls };
    },

    async loadSignals() {
        try { this.signals = await (await fetch('api/signals')).json(); }
        catch (e) { this.signals = null; }
        this.renderTasks();
    },

    // Back-compat shim: the old flat "ToDo" per-person tab is merged into the
    // grouped Tasks view (toggle to "Assignee"). Existing callers (signals,
    // CRUD handlers, filters) keep working and refresh the unified view.
    renderTasks() { this.renderEpics(); },

    // ---- Tasks (one collapsible grouped lens) --------------------------
    // Group by WORKSTREAM (→ phase → tasks) or by ASSIGNEE (→ workstream →
    // tasks) via the in-view toggle; each group collapses to one row
    // (count · who/where · progress) and expands. Replaces the old flat
    // "ToDo" per-person tab. Pure presentation; toggle persists per browser.
    groupModeKey() {
        return `pm_group_mode:${window.PM_PROJECT || 'maxwell'}`;
    },
    groupMode() {
        try { return localStorage.getItem(this.groupModeKey()) === 'assignee' ? 'assignee' : 'workstream'; }
        catch (e) { return 'workstream'; }
    },
    setGroupMode(m) {
        try { localStorage.setItem(this.groupModeKey(), m === 'assignee' ? 'assignee' : 'workstream'); } catch (e) {}
        this.renderEpics();
    },
    renderEpics() {
        const el = document.getElementById('epics-content');
        if (!el) return;
        // Keep groups the user has opened expanded across re-renders (e.g. after
        // checking a task done) — only an explicit click should collapse one.
        const openIds = new Set(Array.from(el.querySelectorAll('.collapse.show')).map((c) => c.id));
        const hideDone = this.isHideDone();
        const tasks = this.filtered(true);
        const mode = this.groupMode();                 // 'workstream' | 'assignee'

        const groups = {};
        tasks.forEach((t) => {
            (mode === 'assignee' ? this._peopleOf(t) : [t._wsId]).forEach((k) => {
                (groups[k] || (groups[k] = [])).push(t);
            });
        });
        let keys;
        if (mode === 'assignee') {
            keys = Object.keys(groups).filter((n) => n !== 'Unassigned')
                .sort((a, b) => groups[b].length - groups[a].length || a.localeCompare(b));
            if (groups['Unassigned']) keys.push('Unassigned');
        } else {
            keys = (this.plan.workstreams || []).map((w) => w.workstream_id).filter((id) => groups[id]);
        }

        let tTotal = 0, tDone = 0;
        const cards = keys.map((key, idx) => {
            const list = groups[key];
            const done = list.filter((t) => t.status === 'Done').length;
            const total = list.length;
            const visN = hideDone ? (total - done) : total;
            tDone += done; tTotal += total;
            const cid = 'epic-' + mode + '-' + idx;
            const isU = key === 'Unassigned';
            const open = openIds.has(cid);

            let dotColor, titleHtml, rightHtml;
            if (mode === 'assignee') {
                dotColor = isU ? 'secondary' : 'azure';
                titleHtml = `<span class="h3 m-0">${this.esc(key)}</span>`;
                rightHtml = '';
            } else {
                dotColor = this.WS_COLOR[key] || 'secondary';
                titleHtml = `<span class="h3 m-0">${this.esc(key)}</span>
                    <span class="text-secondary ms-2 d-none d-md-inline">${this.esc((this.wsMeta[key] || {}).name || key)}</span>`;
                const ppl = [...new Set(list.flatMap((t) => this._peopleOf(t)).filter((p) => p !== 'Unassigned'))];
                rightHtml = `<div class="avatar-list avatar-list-stacked d-none d-sm-flex">${ppl.slice(0, 6).map((p) =>
                    `<span class="avatar avatar-xs" title="${this.esc(p)}">${this.esc(this.initials(p))}</span>`).join('')}</div>`;
            }

            let body;
            if (mode === 'assignee') {
                const innerOrder = (this.plan.workstreams || []).map((w) => w.workstream_id);
                const byWs = {};
                list.forEach((t) => { (byWs[t._wsId] || (byWs[t._wsId] = [])).push(t); });
                body = innerOrder.filter((w) => byWs[w]).map((w) => {
                    const items = byWs[w].filter((t) => !hideDone || t.status !== 'Done');
                    if (!items.length) return '';
                    return `<div class="d-flex align-items-center mt-2 mb-1">
                            <span class="status-dot bg-${this.WS_COLOR[w] || 'secondary'} me-2"></span>
                            <span class="text-uppercase small fw-medium text-secondary">${this.esc(w)}</span>
                            <span class="text-secondary ms-2 small d-none d-md-inline">${this.esc((this.wsMeta[w] || {}).name || '')}</span>
                            <span class="badge bg-secondary-lt ms-2">${items.length}</span>
                        </div>
                        <div class="card"><div class="list-group list-group-flush">${items.map((t) => this.taskRow(t)).join('')}</div></div>`;
                }).join('');
            } else {
                body = this.PHASES.map((phase) => {
                    const ph = list.filter((t) => t.phase === phase && (!hideDone || t.status !== 'Done'));
                    if (!ph.length) return '';
                    return `<div class="d-flex align-items-center mt-2 mb-1">
                            <span class="status-dot bg-${this.PHASE_COLOR[phase] || 'secondary'} me-2"></span>
                            <span class="text-uppercase small fw-medium text-secondary">${this.esc(phase)}</span>
                            <span class="badge bg-secondary-lt ms-2">${ph.length}</span>
                        </div>
                        <div class="card"><div class="list-group list-group-flush">${ph.map((t) => this.taskRow(t)).join('')}</div></div>`;
                }).join('');
            }

            let nextHtml = '';
            if (mode === 'assignee' && !isU) {
                const nx = (this.signals && (this.signals.by_owner_next || {})[key]) || [];
                if (nx.length) nextHtml = `<div class="mb-2 ms-1 small">
                    <span class="text-secondary me-1"><i class="ti ti-player-track-next-filled"></i> Next up:</span>
                    ${nx.map((n) => `<a href="#" class="text-reset fw-medium me-3" data-task="${this.esc(n.task_id)}"><span class="status-dot bg-${this.STATUS_COLOR[n.status] || 'secondary'} me-1"></span>${this.esc(n.task_id)} · ${this.esc((n.title || '').slice(0, 42))}</a>`).join('')}
                </div>`;
            }

            const emptyNote = (!body && hideDone) ? `<div class="text-secondary small px-1 py-2"><i class="ti ti-check me-1"></i>All ${total} task${total !== 1 ? 's' : ''} complete.</div>` : '';
            return `
                <div class="card mb-2">
                    <div class="card-header epic-head d-flex align-items-center" role="button" data-bs-toggle="collapse" data-bs-target="#${cid}" aria-expanded="${open ? 'true' : 'false'}" aria-controls="${cid}">
                        <span class="status-dot bg-${dotColor} me-2"></span>
                        ${titleHtml}
                        <span class="badge bg-secondary-lt ms-2">${visN} task${visN !== 1 ? 's' : ''}</span>
                        ${(total - done) === 0 ? '<span class="badge bg-green-lt ms-1">done</span>' : ''}
                        <div class="ms-auto d-flex align-items-center gap-3">
                            ${rightHtml}
                            <span class="text-secondary small">${done}/${total}</span>
                            <i class="ti ti-chevron-down epic-chev text-secondary"></i>
                        </div>
                    </div>
                    <div class="collapse${open ? ' show' : ''}" id="${cid}">
                        <div class="card-body py-2">${nextHtml}${body}${emptyNote}</div>
                    </div>
                </div>`;
        }).join('');

        const hint = (hideDone && tDone) ? ` · hiding ${tDone} done` : '';
        const head = `<div class="d-flex flex-wrap align-items-center mb-3 gap-2">
                <span class="text-secondary">${keys.length} ${mode === 'assignee' ? 'people' : 'workstreams'} · ${tTotal} tasks · ${tDone} done${hint}</span>
                <label class="form-check form-switch m-0 ms-2">
                    <input id="gmode-switch" class="form-check-input" type="checkbox"${mode === 'assignee' ? ' checked' : ''}/>
                    <span class="form-check-label">Group by assignee</span>
                </label>
                <div class="ms-auto btn-list">
                    <button class="btn btn-sm" id="epic-expand"><i class="ti ti-chevrons-down me-1"></i>Expand all</button>
                    <button class="btn btn-sm" id="epic-collapse"><i class="ti ti-chevrons-up me-1"></i>Collapse all</button>
                </div>
            </div>`;
        el.innerHTML = keys.length
            ? (head + cards)
            : `<div class="card"><div class="empty"><p class="empty-title">No tasks match the filters</p></div></div>`;
        const gs = document.getElementById('gmode-switch');
        if (gs) gs.onchange = () => this.setGroupMode(gs.checked ? 'assignee' : 'workstream');
        const setAll = (show) => el.querySelectorAll('.collapse').forEach((c) => {
            const inst = window.bootstrap.Collapse.getOrCreateInstance(c, { toggle: false });
            show ? inst.show() : inst.hide();
        });
        const eb = document.getElementById('epic-expand'); if (eb) eb.onclick = () => setAll(true);
        const cb = document.getElementById('epic-collapse'); if (cb) cb.onclick = () => setAll(false);
    },

    taskRow(t) {
        const done = t.status === 'Done';
        const wc = this.WS_COLOR[t._wsId] || 'secondary';
        const due = this.fmtDue(t.finish_date, done);
        const id = this.esc(t.task_id);
        const titleCls = done ? 'text-decoration-line-through text-secondary' : 'text-body';
        const provenance = this.provenanceBadge(t);
        return `
            <div class="list-group-item d-flex align-items-start gap-2 py-2" data-task-row="${id}">
                <input class="form-check-input rounded-circle mt-1 flex-shrink-0" type="checkbox" data-check="${id}"${done ? ' checked' : ''} title="Mark done"/>
                <div class="flex-fill">
                    <a href="#" class="d-block fw-medium text-reset ${titleCls}" data-task="${id}">${this.esc(t.title)}</a>
                    <div class="d-flex flex-wrap align-items-center gap-2 mt-1 small">
                        ${due.text ? `<span class="${due.cls}"><i class="ti ti-calendar-event me-1"></i>${due.text}</span>` : ''}
                        ${t.risk_level === 'High' ? '<span class="text-red" title="High risk"><i class="ti ti-flag-filled"></i></span>' : ''}
                        ${t.is_blocking ? '<span class="text-red" title="Blocking"><i class="ti ti-alert-triangle-filled"></i></span>' : ''}
                        ${provenance}
                        <span class="text-secondary ms-auto"><span class="status-dot bg-${wc} me-1"></span>${id} · ${this.esc(t._wsId)}</span>
                    </div>
                </div>
            </div>`;
    },

    async toggleDone(id, checked) {
        const status = checked ? 'Done' : 'In Progress';
        try {
            const res = await fetch(`api/tasks/${encodeURIComponent(id)}`, {
                method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ status, _actor: 'checkbox' }),
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const updated = await res.json();
            const i = this.tasks.findIndex((x) => x.task_id === id);
            if (i >= 0) this.tasks[i] = Object.assign({}, this.tasks[i], updated);
            // in-place: strike the title + sync any duplicate rows (same task, multiple owners)
            document.querySelectorAll(`#tasks-content a[data-task="${id}"]`).forEach((a) => {
                a.classList.toggle('text-decoration-line-through', checked);
                a.classList.toggle('text-secondary', checked);
                a.classList.toggle('text-body', !checked);
            });
            document.querySelectorAll(`#tasks-content input[data-check="${id}"]`).forEach((cb) => { cb.checked = checked; });
            this.renderBoard();
            if (this.isGanttVisible()) this.renderGantt();
        } catch (e) {
            document.querySelectorAll(`#tasks-content input[data-check="${id}"]`).forEach((cb) => { cb.checked = !checked; });
        }
    },

    // ---- Gantt (ApexCharts rangeBar) ------------------------------------
    isGanttVisible() {
        const p = document.getElementById('tab-gantt');
        return !!p && p.classList.contains('active');
    },

    setupGantt() {
        const note = document.getElementById('gantt-note');
        if (note) note.textContent = (this.plan.schedule_note || '') + ' Tip: switch By workstream / By task; click a bar to drill in.';
        // The Gantt is reachable from BOTH the sidebar and the nav-tabs. Listen on
        // EVERY #tab-gantt trigger — querySelector caught only the first (the
        // sidebar), so showing the tab from the nav-tabs left the chart blank until
        // a mode toggle. shown.bs.tab fires after the pane is visible + laid out,
        // so a direct render is safe.
        document.querySelectorAll('a[href="#tab-gantt"]').forEach((tab) =>
            tab.addEventListener('shown.bs.tab', () => this.renderGantt()));
        ['gm-ws', 'gm-task'].forEach((id) => {
            const r = document.getElementById(id);
            if (r) r.addEventListener('change', () => { if (r.checked) { this.ganttMode = r.value; this.renderGantt(); } });
        });
    },

    setGanttMode(mode) {
        this.ganttMode = mode;
        const r = document.getElementById(mode === 'task' ? 'gm-task' : 'gm-ws');
        if (r) r.checked = true;
        this.renderGantt();
    },

    renderGantt() {
        const el = document.getElementById('gantt');
        if (!el || !window.ApexCharts) return;
        const tasks = this.filtered();
        let data, height;
        if (this.ganttMode === 'workstream') {
            const g = {};
            tasks.forEach((t) => {
                const w = g[t._wsId] || (g[t._wsId] = { id: t._wsId, name: t._wsName, starts: [], fins: [], effort: 0, count: 0 });
                if (t.start_date) w.starts.push(t.start_date);
                if (t.finish_date) w.fins.push(t.finish_date);
                w.effort += (t.effort_days || 0); w.count += 1;
            });
            const arr = Object.values(g)
                .map((w) => ({ ...w, s: w.starts.slice().sort()[0], f: w.fins.slice().sort().slice(-1)[0] }))
                .filter((w) => w.s && w.f)
                .sort((a, b) => (a.s < b.s ? -1 : a.s > b.s ? 1 : 0));
            data = arr.map((w) => ({
                x: w.id,
                y: [new Date(w.s).getTime(), new Date(w.f).getTime() + 86400000],
                fillColor: this.WS_HEX[w.id] || '#868e96',
                meta: { ws: w.id, name: w.name, count: w.count, effort: Math.round(w.effort), start: w.s, finish: w.f },
            }));
            height = Math.max(280, data.length * 36 + 90);
        } else {
            const sorted = tasks.filter((t) => t.start_date && t.finish_date).sort((a, b) => a._wsId.localeCompare(b._wsId) || ((a.start_day || 0) - (b.start_day || 0)));
            data = sorted.map((t) => ({
                x: t.task_id,
                y: [new Date(t.start_date).getTime(), new Date(t.finish_date).getTime() + 86400000],
                fillColor: this.PHASE_HEX[t.phase] || '#868e96',
                meta: { id: t.task_id, title: t.title, owner: t.owner_org + ' · ' + t.owner_person_or_role, ws: t._wsId, phase: t.phase, start: t.start_date, finish: t.finish_date, dur: t.duration_days },
            }));
            height = Math.max(320, Math.min(6000, data.length * 22 + 90));
        }
        const opts = {
            chart: {
                type: 'rangeBar', height, fontFamily: 'inherit', animations: { enabled: false },
                events: {
                    dataPointSelection: (e, c, cfg) => {
                        const d = cfg.w.config.series[0].data[cfg.dataPointIndex];
                        if (!d || !d.meta) return;
                        if (this.ganttMode === 'workstream') {
                            const sel = document.getElementById('f-ws');
                            if (sel) sel.value = d.meta.ws;
                            this.renderBoard();
                            this.setGanttMode('task');   // drill: filter to the workstream + show its tasks
                        } else {
                            this.openTask(d.meta.id);
                        }
                    },
                },
            },
            series: [{ data }],
            plotOptions: { bar: { horizontal: true, borderRadius: 3, barHeight: this.ganttMode === 'workstream' ? '55%' : '78%',
                dataLabels: { hideOverflowingLabels: true, position: 'center' } } },
            dataLabels: {
                enabled: true,
                formatter: (val, opts) => {
                    const d = opts.w.config.series[0].data[opts.dataPointIndex];
                    return this.ganttMode === 'workstream' ? `${d.meta.ws} · ${d.meta.count} tasks` : d.meta.id;
                },
                style: { fontSize: '10px', fontWeight: 600, colors: ['#fff'], fontFamily: 'inherit' },
            },
            xaxis: { type: 'datetime', axisBorder: { show: true, color: '#e6e7e9' }, axisTicks: { show: true } },
            // left pane: task / workstream names
            yaxis: { labels: { style: { fontSize: '11px', fontFamily: 'inherit' } } },
            // alternating row stripes + vertical grid lines (the "lines"), Tabler-toned
            grid: {
                borderColor: '#eef0f3', strokeDashArray: 0,
                row: { colors: ['rgba(15,23,42,.025)', 'transparent'], opacity: 1 },
                xaxis: { lines: { show: true } }, yaxis: { lines: { show: false } },
                padding: { left: 8, right: 16 },
            },
            legend: { show: false },
            tooltip: {
                custom: ({ dataPointIndex, w }) => {
                    const m = w.config.series[0].data[dataPointIndex].meta;
                    if (m.count !== undefined) {
                        return `<div class="p-2"><div class="fw-bold">${this.esc(m.ws)} · ${this.esc(m.name)}</div>`
                            + `<div class="text-secondary small">${m.count} tasks · ${m.effort}d effort</div>`
                            + `<div class="small">${this.esc(m.start)} → ${this.esc(m.finish)}</div></div>`;
                    }
                    return `<div class="p-2"><div class="fw-bold">${this.esc(m.id)} · ${this.esc(m.title)}</div>`
                        + `<div class="text-secondary small">${this.esc(m.ws)} · ${this.esc(m.phase)} · ${this.esc(m.owner)}</div>`
                        + `<div class="small">${this.esc(m.start)} → ${this.esc(m.finish)} (${this.esc(m.dur)}d)</div></div>`;
                },
            },
        };
        if (this.gantt) { this.gantt.destroy(); }
        this.gantt = new window.ApexCharts(el, opts);
        this.gantt.render();
    },

    async openTask(id) {
        let t = this.tasks.find((x) => x.task_id === id);
        if (!t) return;
        try {
            const fresh = await (await fetch(`api/tasks/${encodeURIComponent(id)}`)).json();
            if (fresh && fresh.task_id) t = Object.assign({}, t, fresh);
        } catch (e) { /* fall back to in-memory task */ }
        const meta = (label, val) => `<div class="col-6 mb-2"><div class="text-secondary" style="font-size:12px">${label}</div><div>${val}</div></div>`;
        const owner = this.esc(t.owner_org || '—') + (t.owner_person_or_role ? ' · ' + this.esc(t.owner_person_or_role) : '');
        const dates = `${this.esc(t.start_date || '?')} – ${this.esc(t.finish_date || '?')}`;
        const risk = this.esc(t.risk_level || '—') + (t.is_blocking ? ' · blocking' : '');
        const depsText = (t.depends_on || []).map((d) => this.esc(d)).join(', ') || 'none';
        const statusOpts = ['Not Started', 'In Progress', 'In Review', 'Blocked', 'Done'].map((s) =>
            `<option ${s === t.status ? 'selected' : ''}>${s}</option>`).join('');
        const prose = (v) => `<div style="white-space:pre-wrap">${this.esc(v || '—')}</div>`;
        const sc = this.STATUS_COLOR[t.status] || 'secondary';
        document.getElementById('task-modal-title').innerHTML =
            `<span class="status-dot bg-${sc} me-2" title="${this.esc(t.status || '')}"></span><span class="text-secondary fw-normal me-2">${this.esc(t.task_id)}</span>${this.esc(t.title)}`;
        const assignee = this.esc(t.assignee || '—');
        const blocks = this.tasks.filter((x) => (x.depends_on || []).includes(t.task_id)).map((x) => this.esc(x.task_id)).join(', ') || 'none';
        const wsLabel = this.esc(t._wsId || '') + (t._wsName ? ' · ' + this.esc(t._wsName) : '');
        const effort = (t.effort_days != null) ? this.esc(t.effort_days) + 'd' : '—';
        const dg = (label, val) => `<div class="datagrid-item"><div class="datagrid-title">${label}</div><div class="datagrid-content">${val}</div></div>`;
        const gateCard = (icon, label, val, full, accent) => `<div class="col-${full ? '12' : 'md-6'}"><div class="card card-sm">${accent ? '<div class="card-status-start bg-primary"></div>' : ''}<div class="card-body">
            <div class="subheader text-secondary mb-1"><i class="ti ti-${icon} me-1"></i>${label}</div>
            <div class="small" style="white-space:pre-wrap">${this.esc(val || '—')}</div>
        </div></div></div>`;
        const PCT = { 'Not Started': 0, 'In Progress': 50, 'Blocked': 20, 'In Review': 80, 'Done': 100 };
        const pct = PCT[t.status] != null ? PCT[t.status] : 0;
        const av = (name) => name ? `<span class="avatar avatar-xs rounded-circle bg-secondary-lt me-1">${this.esc(this.initials(name))}</span>` : '';
        const badgeList = (arr, icon) => (arr && arr.length) ? arr.map((x) => `<span class="badge bg-secondary-lt me-1"><i class="ti ti-${icon} me-1"></i>${this.esc(x)}</span>`).join('') : '<span class="text-secondary">none</span>';
        const blockArr = this.tasks.filter((x) => (x.depends_on || []).includes(t.task_id)).map((x) => x.task_id);
        const riskHtml = t.risk_level === 'High' ? '<span class="badge badge-outline text-red">High</span>' : (t.risk_level ? this.esc(t.risk_level) : '—');
        const tally = this.taskTally(t.task_id);
        const provenanceHtml = this.provenanceDetail(t);
        document.getElementById('task-modal-title').innerHTML =
            `<span class="status-dot bg-${sc} me-2"></span><span class="text-secondary font-monospace fw-normal me-2">${this.esc(t.task_id)}</span>${this.esc(t.title)}${t.is_blocking ? ' <span class="badge bg-red-lt ms-2"><i class="ti ti-alert-triangle me-1"></i>Blocking</span>' : ''}`;
        document.getElementById('task-modal-body').innerHTML = `
            <ul class="nav nav-tabs" role="tablist">
                <li class="nav-item" role="presentation"><a class="nav-link active" data-bs-toggle="tab" href="#m-details" role="tab"><i class="ti ti-info-circle me-1"></i>Details</a></li>
                <li class="nav-item" role="presentation"><a class="nav-link" data-bs-toggle="tab" href="#m-edit" role="tab"><i class="ti ti-pencil me-1"></i>Edit</a></li>
                <li class="nav-item" role="presentation"><a class="nav-link" data-bs-toggle="tab" href="#m-dev" role="tab"><i class="ti ti-terminal-2 me-1"></i>Dev</a></li>
                <li class="nav-item" role="presentation"><a class="nav-link" data-bs-toggle="tab" href="#m-activity" role="tab"><i class="ti ti-history me-1"></i>Activity</a></li>
            </ul>
            <div class="tab-content mt-3">
                <div class="tab-pane fade show active" id="m-details" role="tabpanel">
                    <div class="progress progress-sm mb-3"><div class="progress-bar bg-${sc}" style="width:${pct}%"></div></div>
                    <div class="text-secondary small mb-3 d-flex align-items-center"><span class="status-dot bg-${sc} me-2"></span>${this.esc(t.status || '—')} · ${pct}% complete</div>
                    <div class="subheader mb-2">Economics</div>
                    ${this.tallyDetailHtml(tally)}
                    <div class="subheader mb-2">Properties</div>
                    <div class="datagrid mb-3">
                        <div class="datagrid-item"><div class="datagrid-title">Status</div>
                            <div class="datagrid-content"><select id="details-status" class="form-select form-select-sm" style="max-width:200px">${statusOpts}</select></div></div>
                        ${dg('Done provenance', provenanceHtml)}
                        ${dg('Owner', av(t.owner_person_or_role || t.owner_org) + owner)}
                        ${dg('Assignee', t.assignee ? av(t.assignee) + this.esc(t.assignee) : '—')}
                        ${dg('Phase', this.esc(t.phase || '—'))}
                        ${dg('Workstream', `<span class="text-uppercase">${this.esc(t._wsId || '—')}</span>${t._wsName ? ' · ' + this.esc(t._wsName) : ''}`)}
                        ${dg('Timeline', dates)}
                        ${dg('Effort', effort)}
                        ${dg('Risk', riskHtml)}
                        ${dg('Depends on', badgeList(t.depends_on, 'link'))}
                        ${dg('Blocks', badgeList(blockArr, 'arrow-bar-to-right'))}
                    </div>
                    <div class="subheader mb-2">Description</div>
                    <p class="text-secondary" style="white-space:pre-wrap">${this.esc(t.description || '—')}</p>
                    <div class="subheader mb-2 mt-3">Gates</div>
                    <div class="row g-2">
                        ${gateCard('login', 'Entry criteria', t.entry_criteria, false, false)}
                        ${gateCard('logout', 'Exit criteria', t.exit_criteria, false, false)}
                        ${gateCard('package', 'Deliverable', t.deliverable, true, true)}
                    </div>
                    <div class="subheader mb-2 mt-4 d-flex align-items-center">Recent activity
                        <a class="ms-auto small fw-normal text-reset" data-bs-toggle="tab" href="#m-activity" role="tab">View all <i class="ti ti-arrow-right"></i></a></div>
                    <div id="details-activity"></div>
                </div>
                <div class="tab-pane fade" id="m-edit" role="tabpanel">
                    ${this._taskFormHtml(t, 'edit-')}
                    <div class="btn-list mt-3 pt-3 border-top">
                        <button id="edit-save" class="btn btn-primary"><i class="ti ti-device-floppy me-1"></i>Save changes</button>
                        <button id="edit-delete" class="btn btn-ghost-danger ms-auto"><i class="ti ti-trash me-1"></i>Delete task</button>
                        <span id="edit-flash" class="small text-secondary"></span>
                    </div>
                </div>
                <div class="tab-pane fade" id="m-dev" role="tabpanel">
                    <p class="text-secondary">Dispatch hands this task to a Claude Code agent in an isolated worktree. It drafts the change, opens a PR, and reports back here — it never merges or writes to your systems on its own.</p>
                    ${t.is_blocking ? `<div class="alert alert-warning d-flex" role="alert"><i class="ti ti-shield-lock me-2 mt-1"></i><div><span class="fw-bold">Human-gated.</span> This task is blocking — a maintainer must approve both the dispatch and the resulting PR before anything merges.</div></div>` : ''}
                    ${this.controlTruthHtml(t)}
                    ${this.monitorControlHtml(t)}
                    ${this.runnerControlHtml(t)}
                    ${this.claimControlHtml(t)}
                    <button id="edit-dispatch" class="btn btn-primary mb-3"><i class="ti ti-robot me-1"></i>Dispatch to Claude Code</button>
                    <div id="dispatch-panel"></div>
                    <span id="edit-flash-dev" class="small text-secondary"></span>
                </div>
                <div class="tab-pane fade" id="m-activity" role="tabpanel">
                    <div id="activity-log" class="list-group list-group-flush mb-3"></div>
                    <div class="d-flex align-items-center mb-2 pt-2 border-top">
                        <span class="avatar avatar-xs rounded bg-primary-lt text-primary me-2"><i class="ti ti-sparkles"></i></span>
                        <span class="fw-semibold">Ask Taikun · this task</span>
                        <span class="text-secondary small ms-2">grounded in the plan docs · proposes changes you confirm</span>
                    </div>
                    <div id="chat-log" class="mb-2"></div>
                    <div class="input-group">
                        <input id="chat-input" class="form-control" placeholder="Ask how to push this task ahead…" autocomplete="off"/>
                        <button id="chat-send" class="btn btn-primary"><i class="ti ti-send me-1"></i>Send</button>
                    </div>
                </div>
            </div>`;
        this._renderActivity(t);
        this._loadTaskMonitors(t.task_id);
        this._loadRunnerSessions(t.task_id);
        this._loadDispatch(t.task_id);
        document.getElementById('details-status').addEventListener('change', (e) => this.quickStatus(t.task_id, e.target.value));
        document.getElementById('edit-delete').addEventListener('click', () => this.deleteTask(t.task_id));
        document.getElementById('edit-save').addEventListener('click', () => this.saveTask(t.task_id));
        document.getElementById('edit-dispatch').addEventListener('click', () => this.dispatchTask(t.task_id));
        const revokeBtn = document.getElementById('claim-revoke-btn');
        if (revokeBtn) revokeBtn.addEventListener('click', () => this.revokeClaim(t.task_id));
        document.getElementById('chat-send').addEventListener('click', () => this.sendChat(t.task_id));
        document.getElementById('chat-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') this.sendChat(t.task_id); });
        window.bootstrap.Modal.getOrCreateInstance(document.getElementById('task-modal')).show();
    },

    async quickStatus(id, status) {
        try {
            const res = await fetch(`api/tasks/${encodeURIComponent(id)}`, {
                method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status }),
            });
            if (!res.ok) return;
            const updated = await res.json();
            const i = this.tasks.findIndex((x) => x.task_id === id);
            if (i >= 0) this.tasks[i] = Object.assign({}, this.tasks[i], updated);
            this.renderBoard();
            this.renderTasks();
            if (this.isGanttVisible()) this.renderGantt();
            this._renderActivity(i >= 0 ? this.tasks[i] : updated);   // reflect this change in the Activity timeline + Details glance
        } catch (e) { /* ignore */ }
    },

    _FIELD_LABELS: { status: 'Status', start_date: 'Start', finish_date: 'Finish', assignee: 'Assignee',
        owner_person_or_role: 'Owner', owner_org: 'Org', is_blocking: 'Blocking', depends_on: 'Depends on',
        risk_level: 'Risk', phase: 'Phase', effort_days: 'Effort', duration_days: 'Duration', title: 'Title',
        description: 'Description', deliverable: 'Deliverable', entry_criteria: 'Entry criteria', exit_criteria: 'Exit criteria' },

    _actorMeta(actor) {
        const a = (actor || '').toLowerCase();
        const agent = /maxwell|mcp|agent|claude/.test(a);
        if (agent) return { agent: true, icon: 'ti-robot', cls: 'bg-azure-lt text-azure', label: actor || 'Agent' };
        return { agent: false, icon: 'ti-user', cls: 'bg-secondary-lt', label: (a === 'user' || !actor) ? 'You' : actor };
    },

    // Render an `edit` payload ({field: newValue, ...}) as "Field → value" chips.
    // Inline, badge-free change summary: muted label + emphasized value; status carries a dot.
    _fmtEditPayload(payload) {
        if (!payload || typeof payload !== 'object') return '';
        return Object.keys(payload).map((k) => {
            let v = payload[k];
            if (k === 'is_blocking') v = v ? 'blocking' : 'not blocking';
            else if (k === 'depends_on') { try { v = (Array.isArray(v) ? v : JSON.parse(v || '[]')).join(', ') || 'none'; } catch (e) { /* leave as-is */ } }
            v = String(v);
            if (v.length > 2000) v = v.slice(0, 2000) + '…';   // DOM safety cap only — display is clamped + click-to-expand
            const label = this._FIELD_LABELS[k] || k;
            const dot = (k === 'status') ? `<span class="status-dot bg-${this.STATUS_COLOR[v] || 'secondary'} me-1"></span>` : '';
            // No text-nowrap — value flows inline and wraps; full value kept so the row can expand.
            return `<span class="me-3"><span class="text-secondary">${this.esc(label)}</span> ${dot}<span class="text-body fw-medium">${this.esc(v)}</span></span>`;
        }).join(' ');
    },

    // One timeline entry — actor shown by the avatar (no badge); changes as clean inline text.
    _activityRow(a) {
        const m = this._actorMeta(a.actor);
        const when = this._relAge(a.created_at);
        let body;
        if (a.kind === 'edit') {
            body = this._fmtEditPayload(a.payload) || '<span class="text-secondary">updated</span>';
        } else if (a.kind === 'create') {
            body = '<span class="text-secondary">Created this task</span>';
        } else if (a.kind === 'dispatch') {
            body = '<span class="text-secondary">Dispatched to Claude Code</span>';
        } else if (a.kind === 'chat') {
            body = `<div class="markdown text-body">${this.md((a.payload && a.payload.text) || '')}</div>`;
        } else { // comment / note
            body = `<span class="text-body">${this._linkify(this.esc((a.payload && a.payload.text) || ''))}</span>`;
        }
        return `<div class="list-group-item list-group-item-action tk-act-item px-0 py-2">
            <div class="d-flex gap-2 align-items-start">
                <span class="avatar avatar-xs rounded-circle ${m.cls} flex-shrink-0 mt-1"><i class="ti ${m.icon}"></i></span>
                <div class="flex-fill" style="min-width:0">
                    <div class="d-flex align-items-baseline gap-2">
                        <span class="fw-medium text-truncate" style="min-width:0">${this.esc(m.label)}</span>
                        <span class="text-secondary small ms-auto flex-shrink-0">${when} ago</span>
                    </div>
                    <div class="tk-act-body tk-clamp small mt-1">${body}</div>
                    <div class="tk-act-toggle mt-1">Show more</div>
                </div>
            </div>
        </div>`;
    },

    // Unified activity timeline — newest first. Agent edits, your edits, notes, chats all land here.
    _renderActivity(t) {
        const acts = (t.activity || []).slice().reverse();
        // Long updates clamp to a few lines; click the row (or "Show more") to
        // expand to the full text and back. The click ALWAYS toggles the clamp,
        // so it works even in the initially-hidden Activity tab; the "Show more"
        // hint is shown only for rows that actually overflow.
        const detect = (c) => { if (!c) return; c.querySelectorAll('.tk-act-item').forEach((item) => {
            const b = item.querySelector('.tk-act-body');
            item.classList.toggle('tk-expandable', !!(b && b.scrollHeight - b.clientHeight > 2));
        }); };
        const wire = (c) => {
            if (!c) return;
            c.onclick = (e) => {
                if (e.target.closest('a')) return;                  // let real links work
                const item = e.target.closest('.tk-act-item'); if (!item || !c.contains(item)) return;
                const b = item.querySelector('.tk-act-body'); if (!b) return;
                const clamped = b.classList.toggle('tk-clamp');     // true = now clamped, false = now full
                item.classList.add('tk-expandable');                // keep the affordance after interaction
                const tog = item.querySelector('.tk-act-toggle');
                if (tog) tog.textContent = clamped ? 'Show more' : 'Show less';
            };
            detect(c);
        };
        const full = document.getElementById('activity-log');
        if (full) { full.innerHTML = acts.length ? acts.map((a) => this._activityRow(a)).join('') : '<div class="text-secondary small">No activity yet — agent updates, your edits, notes and dispatch events will appear here.</div>'; wire(full); }
        const glance = document.getElementById('details-activity');
        if (glance) { glance.className = acts.length ? 'list-group list-group-flush' : ''; glance.innerHTML = acts.length ? acts.slice(0, 3).map((a) => this._activityRow(a)).join('') : '<div class="text-secondary small">No activity yet.</div>'; wire(glance); }
        // The Activity tab is hidden at modal-open (0 height → overflow can't be
        // measured), so re-detect when it's shown.
        const atab = document.querySelector('a[href="#m-activity"]');
        if (atab && !atab._actWired) { atab._actWired = true; atab.addEventListener('shown.bs.tab', () => detect(document.getElementById('activity-log'))); }
    },

    async saveTask(id) {
        const flash = (msg, cls) => { const el = document.getElementById('edit-flash'); if (el) { el.textContent = msg; el.className = 'small text-' + (cls || 'secondary'); } };
        const body = this._readForm('edit-');
        if (!body.title) { flash('Title is required', 'danger'); return; }
        flash('Saving…');
        try {
            const res = await fetch(`api/tasks/${encodeURIComponent(id)}`, {
                method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const updated = await res.json();
            const i = this.tasks.findIndex((x) => x.task_id === id);
            if (i >= 0) this.tasks[i] = Object.assign({}, this.tasks[i], updated);
            document.getElementById('task-modal-title').innerHTML = `<span class="me-2">${this.esc(updated.task_id)}</span>${this.esc(updated.title)}`;
            flash('Saved', 'green');
            this.renderBoard();
            this.renderTasks();
            if (this.isGanttVisible()) this.renderGantt();
            this._renderActivity(i >= 0 ? this.tasks[i] : updated);   // your edit shows in the same Activity timeline
        } catch (e) { flash(e.message, 'danger'); }
    },

    async deleteTask(id) {
        if (!window.confirm(`Delete task ${id}? This cannot be undone.`)) return;
        try {
            const res = await fetch(`api/tasks/${encodeURIComponent(id)}`, { method: 'DELETE' });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            this.tasks = this.tasks.filter((x) => x.task_id !== id);
            (this.plan.workstreams || []).forEach((w) => { w.tasks = (w.tasks || []).filter((x) => x.task_id !== id); });
            window.bootstrap.Modal.getOrCreateInstance(document.getElementById('task-modal')).hide();
            this.renderBoard();
            this.renderTasks();
            if (this.isGanttVisible()) this.renderGantt();
        } catch (e) {
            const el = document.getElementById('edit-flash');
            if (el) { el.textContent = 'Delete failed: ' + e.message; el.className = 'small text-danger'; }
        }
    },

    async dispatchTask(id) {
        const flash = (msg, cls) => { const el = document.getElementById('edit-flash-dev'); if (el) { el.textContent = msg; el.className = 'small text-' + (cls || 'secondary'); } };
        if (!window.confirm(`Dispatch ${id} to the Claude Code runner? It builds the change on a claude/ branch and posts a PR link to this task — it never touches main.`)) return;
        flash('Dispatching to Claude Code…');
        let data;
        try {
            const res = await fetch(`api/tasks/${encodeURIComponent(id)}/dispatch`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
            data = await res.json();
        } catch (e) { return flash('Dispatch failed: ' + e.message, 'danger'); }
        if (data.disabled) return flash(data.reason || 'Runner not configured', 'warning');
        if (!data.dispatched) return flash('Dispatch failed: ' + (data.error || 'unknown'), 'danger');
        flash(`Dispatched (job ${data.job_id}) — Claude Code is building it now.`, 'green');
        this._loadDispatch(id);   // render the live panel (status -> Open PR); it self-refreshes
    },

    async revokeClaim(taskId) {
        const root = document.getElementById('claim-control');
        const claimId = root ? root.getAttribute('data-claim-id') : '';
        const flash = (msg, cls) => { const el = document.getElementById('claim-revoke-flash'); if (el) { el.textContent = msg; el.className = 'small text-' + (cls || 'secondary'); } };
        if (!claimId) return;
        const reason = (document.getElementById('claim-revoke-reason') || {}).value || 'operator override';
        const reassign = (document.getElementById('claim-revoke-reassign') || {}).value || '';
        const sortVal = (document.getElementById('claim-revoke-sort') || {}).value || '';
        const evidenceRaw = ((document.getElementById('claim-revoke-evidence') || {}).value || '').trim();
        let partial_evidence = {};
        if (evidenceRaw) {
            try { partial_evidence = JSON.parse(evidenceRaw); }
            catch (e) { partial_evidence = { note: evidenceRaw }; }
        }
        if (!window.confirm(`Revoke claim ${claimId}?`)) return;
        flash('Revoking…');
        try {
            const body = {
                reason,
                reassign_to: reassign.trim(),
                sort_order: sortVal ? parseInt(sortVal, 10) : null,
                partial_evidence,
                notify: true,
            };
            const res = await fetch(`api/tasks/${encodeURIComponent(taskId)}/claims/${encodeURIComponent(claimId)}/revoke`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
            });
            const data = await res.json();
            if (!res.ok || data.error) throw new Error(data.error || data.detail || `HTTP ${res.status}`);
            const updated = data.task;
            const i = this.tasks.findIndex((x) => x.task_id === taskId);
            if (i >= 0 && updated) this.tasks[i] = Object.assign({}, this.tasks[i], updated);
            flash('Revoked', 'green');
            this.renderBoard();
            this.renderTasks();
            if (this.isGanttVisible()) this.renderGantt();
            await this.openTask(taskId);
        } catch (e) { flash('Revoke failed: ' + e.message, 'danger'); }
    },

    async _loadTaskMonitors(taskId) {
        const body = document.getElementById('task-monitor-body');
        const count = document.getElementById('task-monitor-count');
        if (!body) return;
        let data;
        try {
            const q = `project=${encodeURIComponent(window.PM_PROJECT || 'maxwell')}&task_id=${encodeURIComponent(taskId)}`;
            data = await (await fetch(`/ixp/v1/monitors?${q}`)).json();
        } catch (e) {
            body.innerHTML = `<div class="text-danger small">Monitors unavailable: ${this.esc(e.message)}</div>`;
            if (count) { count.className = 'badge bg-red-lt'; count.textContent = 'error'; }
            return;
        }
        const monitors = data.monitors || [];
        if (count) {
            count.className = monitors.length ? 'badge bg-orange-lt' : 'badge bg-secondary-lt';
            count.textContent = `${monitors.length}`;
        }
        if (!monitors.length) {
            body.innerHTML = `<div class="text-secondary small">No monitors are registered for this task.</div>`;
            return;
        }
        const rows = monitors.slice(0, 8).map((m) => {
            const statusCls = m.status === 'pending' ? 'yellow' : (m.status === 'fired' ? 'red' : 'green');
            const deadline = m.deadline ? new Date(m.deadline * 1000).toLocaleString() : 'none';
            return `<tr>
                <td><span class="badge bg-${statusCls}-lt">${this.esc(m.status || 'unknown')}</span></td>
                <td>${this.esc(m.kind || 'monitor')}</td>
                <td class="font-monospace small">${this.esc(m.id || '')}</td>
                <td class="text-secondary small">${this.esc(deadline)}</td>
            </tr>`;
        }).join('');
        body.innerHTML = `<div class="table-responsive"><table class="table table-sm card-table mb-0">
            <thead><tr><th>Status</th><th>Kind</th><th>ID</th><th>Deadline</th></tr></thead><tbody>${rows}</tbody>
        </table></div>`;
    },

    async _loadRunnerSessions(taskId) {
        const body = document.getElementById('runner-control-body');
        const count = document.getElementById('runner-control-count');
        if (!body) return;
        let data;
        try {
            const q = `project=${encodeURIComponent(window.PM_PROJECT || 'maxwell')}&task_id=${encodeURIComponent(taskId)}&include_stale=true`;
            data = await (await fetch(`/ixp/v1/runner_sessions?${q}`)).json();
        } catch (e) {
            body.innerHTML = `<div class="text-danger small">Runner sessions unavailable: ${this.esc(e.message)}</div>`;
            if (count) { count.className = 'badge bg-red-lt'; count.textContent = 'error'; }
            return;
        }
        const sessions = data.sessions || [];
        if (count) {
            count.className = sessions.length ? 'badge bg-azure-lt' : 'badge bg-secondary-lt';
            count.textContent = `${sessions.length}`;
        }
        if (!sessions.length) {
            body.innerHTML = `<div class="text-secondary small">No runner sessions are registered for this task.</div>`;
            return;
        }
        body.innerHTML = `<div class="table-responsive"><table class="table table-sm mb-0 align-middle">
            <thead><tr><th>Session</th><th>Host</th><th>Runtime</th><th>Claim</th><th>Fidelity</th><th>Environment</th><th>Snapshot</th><th class="text-end">Actions</th></tr></thead>
            <tbody>${sessions.map((s) => this._runnerSessionRow(s)).join('')}</tbody>
        </table></div>`;
        body.querySelectorAll('[data-runner-action]').forEach((btn) => {
            btn.addEventListener('click', () => this.requestRunnerControl(
                btn.getAttribute('data-runner-id'),
                btn.getAttribute('data-runner-action'),
                taskId,
            ));
        });
    },

    _runnerSessionRow(s) {
        const actions = s.available_actions || [];
        const canSnap = actions.includes('snapshot');
        const canHealth = actions.includes('health');
        const canLogs = actions.includes('logs');
        const canKill = actions.includes('kill');
        const snap = s.last_snapshot || {};
        const env = s.environment || {};
        const snapText = snap.captured_at ? new Date(snap.captured_at * 1000).toLocaleTimeString() : '—';
        const statusColor = s.stale ? 'yellow' : (s.status === 'running' ? 'green' : 'secondary');
        const ctrl = s.control || {};
        const fidelity = ctrl.runner_kill ? 'T3 runner kill' : (ctrl.managed_process ? 'Managed' : 'Advisory');
        const uptime = env.uptime_seconds == null ? '' : `${Math.round(env.uptime_seconds / 60)}m`;
        const logTail = env.log_tail ? `<div class="text-secondary small text-truncate" style="max-width:220px" title="${this.esc(env.log_tail)}">${this.esc(env.log_tail.split('\n').slice(-1)[0])}</div>` : '';
        const failure = env.failure_reason ? `<div class="text-danger small">${this.esc(env.failure_reason)}</div>` : '';
        const btn = (action, icon, label, color, disabled) =>
            `<button class="btn btn-sm btn-${color}" data-runner-id="${this.esc(s.runner_session_id)}" data-runner-action="${action}"${disabled ? ' disabled' : ''} title="${this.esc(label)}"><i class="ti ti-${icon}"></i></button>`;
        return `<tr>
            <td><div class="font-monospace small">${this.esc(s.runner_session_id)}</div><span class="badge bg-${statusColor}-lt">${this.esc(s.status || 'unknown')}${s.stale ? ' · stale' : ''}</span></td>
            <td>${this.esc(s.host_id || '—')}</td>
            <td>${this.esc(s.runtime || '—')}<div class="text-secondary small">${this.esc(s.agent_id || '')}</div></td>
            <td class="font-monospace small">${this.esc(s.claim_id || '—')}</td>
            <td>${this.esc(fidelity)}</td>
            <td><span class="badge bg-${statusColor}-lt">${this.esc(env.status || s.status || 'unknown')}</span>${uptime ? `<span class="text-secondary small ms-1">${this.esc(uptime)}</span>` : ''}${failure}${logTail}</td>
            <td class="text-secondary small">${this.esc(snapText)}</td>
            <td class="text-end"><div class="btn-list justify-content-end flex-nowrap">
                ${btn('health', 'activity-heartbeat', 'Health', 'outline-secondary', !canHealth)}
                ${btn('logs', 'file-text', 'Logs', 'outline-secondary', !canLogs)}
                ${btn('snapshot', 'camera', 'Snapshot', 'outline-secondary', !canSnap)}
                ${btn('kill', 'square', 'Kill', 'outline-danger', !canKill)}
            </div></td>
        </tr>`;
    },

    async requestRunnerControl(runnerId, action, taskId) {
        const flash = (msg, cls) => {
            const el = document.getElementById('runner-control-flash');
            if (el) { el.textContent = msg; el.className = 'small text-' + (cls || 'secondary') + ' ms-auto'; }
        };
        if (!runnerId || !action) return;
        if (action === 'kill' && !window.confirm(`Request runner kill for ${runnerId}?`)) return;
        flash(action === 'kill' ? 'Requesting kill…' : `Requesting ${action}…`);
        const endpoints = {
            kill: '/ixp/v1/request_runner_kill',
            snapshot: '/ixp/v1/request_runner_snapshot',
            health: '/ixp/v1/request_runner_health',
            logs: '/ixp/v1/request_runner_logs',
            open: '/ixp/v1/request_runner_open',
        };
        const endpoint = endpoints[action] || '/ixp/v1/request_runner_snapshot';
        try {
            const res = await fetch(endpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    project: window.PM_PROJECT || 'maxwell',
                    runner_session_id: runnerId,
                    reason: `operator ${action} from task ${taskId}`,
                }),
            });
            const data = await res.json();
            if (!res.ok || data.error) throw new Error(data.error || data.detail || `HTTP ${res.status}`);
            flash(data.requested === false ? `${action} refused` : `${action} requested`, data.requested === false ? 'warning' : 'green');
            await this._loadRunnerSessions(taskId);
        } catch (e) {
            flash(`${action} failed: ${e.message}`, 'danger');
        }
    },

    async _loadDispatch(id) {
        const el = document.getElementById('dispatch-panel');
        if (!el) return;
        this._dispatchPollId = id;
        let d;
        try { d = await (await fetch(`api/tasks/${encodeURIComponent(id)}/dispatch/latest`)).json(); } catch (e) { return; }
        if (!d || !d.job_id) { el.innerHTML = ''; return; }
        const st = d.status || 'running';
        const M = { running: ['Building…', 'azure'], pushed: ['PR ready', 'green'], no_changes: ['No changes', 'yellow'], push_failed: ['Push failed', 'red'], failed_branch: ['Failed', 'red'], no_repo: ['Failed', 'red'] };
        const [label, color] = M[st] || [st, 'secondary'];
        const pr = d.pr_url ? `<a href="${this.esc(d.pr_url)}" target="_blank" class="btn btn-success btn-sm"><i class="ti ti-git-pull-request me-1"></i>Open PR ↗</a>` : '';
        el.innerHTML = `
            <div class="card"><div class="card-body py-2">
                <div class="d-flex align-items-center gap-2 flex-wrap">
                    <i class="ti ti-robot text-azure"></i><strong>Claude Code dev run</strong>
                    <span class="badge bg-${color}-lt">${this.esc(label)}</span>
                    ${st === 'running' ? '<span class="spinner-border spinner-border-sm text-azure"></span>' : ''}
                    <span class="ms-auto"></span>${pr}
                    <button class="btn btn-sm btn-outline-secondary" id="dispatch-log-btn"><i class="ti ti-file-text me-1"></i>View run log</button>
                </div>
                ${st === 'pushed' ? '<div class="small text-secondary mt-1">Next: open the PR, review the diff, and merge it on GitHub (or comment back here).</div>' : ''}
                ${st === 'running' ? '<div class="small text-secondary mt-1">Building now — the Open PR button appears here when it finishes.</div>' : ''}
                ${st === 'no_changes' ? '<div class="small text-secondary mt-1">The run produced no code changes — open the log to see why.</div>' : ''}
                <div id="dispatch-log" class="mt-2" style="display:none"></div>
            </div></div>`;
        const lb = document.getElementById('dispatch-log-btn');
        if (lb) lb.addEventListener('click', () => {
            const box = document.getElementById('dispatch-log');
            if (!box) return;
            if (box.style.display === 'none') { box.style.display = 'block'; box.innerHTML = `<pre class="bg-dark text-light p-2 rounded small" style="max-height:260px;overflow:auto;white-space:pre-wrap">${this.esc(d.log_tail || '(no log captured yet)')}</pre>`; }
            else { box.style.display = 'none'; }
        });
        if (st === 'running') setTimeout(() => { if (this._dispatchPollId === id) this._loadDispatch(id); }, 7000);
    },

    _linkify(s) {
        return (s || '').replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank">$1</a>');
    },

    // ---- Markdown rendering (XSS-safe: HTML-escapes first, emits only known tags)
    // Turns an agent answer into rich, Tabler-styled HTML (wrapped in .markdown).
    md(src) {
        if (src === null || src === undefined) return '';
        const s = this.esc(String(src)).replace(/\r\n?/g, '\n');
        const lines = s.split('\n');
        return '<div class="markdown">' + this._mdBlocks(lines, 0, lines.length) + '</div>';
    },
    _mdItem(line) {
        const m = /^(\s*)(?:([-*+])|(\d+)[.)])\s+(.*)$/.exec(line);
        if (!m) return null;
        return { indent: m[1].length, ordered: m[3] !== undefined, content: m[4] };
    },
    _mdInline(text) {
        // text is already HTML-escaped; stash code/links behind a sentinel so emphasis
        // and autolink rules can't touch them, then restore. Sentinel = U+FFF9/U+FFFA
        // (interlinear annotation controls — never present in real chat text).
        const stash = [];
        const hide = (html) => { stash.push(html); return '￹' + (stash.length - 1) + '￺'; };
        let t = String(text);
        t = t.replace(/`([^`]+)`/g, (m, c) => hide('<code>' + c + '</code>'));
        t = t.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+&quot;[^"]*&quot;)?\)/g, (m, label, url) => {
            const safe = /^(https?:|mailto:|\/|#|\.\/|\.\.\/)/i.test(url) ? url : '#';
            return hide('<a href="' + safe + '" target="_blank" rel="noopener">' + label + '</a>');
        });
        t = t.replace(/(https?:\/\/[^\s<]+[^\s<.,;:!?)])/g, (m, url) => hide('<a href="' + url + '" target="_blank" rel="noopener">' + url + '</a>'));
        t = t.replace(/\*\*([^*]+?)\*\*/g, '<strong>$1</strong>')
             .replace(/__([^_]+?)__/g, '<strong>$1</strong>')
             .replace(/(^|[^*])\*(?!\s)([^*]+?)\*/g, '$1<em>$2</em>')
             .replace(/~~([^~]+?)~~/g, '<del>$1</del>');
        t = t.replace(/￹(\d+)￺/g, (m, i) => stash[+i]);
        return t;
    },
    _mdBlocks(lines, start, end) {
        const out = [];
        let i = start;
        while (i < end) {
            const line = lines[i];
            if (/^\s*$/.test(line)) { i++; continue; }
            const fence = /^\s*(```|~~~)/.exec(line);
            if (fence) {
                const marker = fence[1];
                const buf = [];
                i++;
                while (i < end && lines[i].trim().slice(0, 3) !== marker) { buf.push(lines[i]); i++; }
                i++;
                out.push('<pre class="tk-code"><code>' + buf.join('\n') + '</code></pre>');
                continue;
            }
            const h = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line);
            if (h) { const lvl = h[1].length; out.push('<h' + lvl + '>' + this._mdInline(h[2]) + '</h' + lvl + '>'); i++; continue; }
            if (/^\s{0,3}([-*_])\s*(\1\s*){2,}$/.test(line)) { out.push('<hr>'); i++; continue; }
            if (/^\s{0,3}&gt;\s?/.test(line)) {
                const buf = [];
                while (i < end && /^\s{0,3}&gt;\s?/.test(lines[i])) { buf.push(lines[i].replace(/^\s{0,3}&gt;\s?/, '')); i++; }
                out.push('<blockquote>' + this._mdBlocks(buf, 0, buf.length) + '</blockquote>');
                continue;
            }
            if (line.indexOf('|') !== -1 && i + 1 < end &&
                /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(lines[i + 1]) && lines[i + 1].indexOf('-') !== -1) {
                const tbl = this._mdTable(lines, i, end);
                if (tbl) { out.push(tbl.html); i = tbl.next; continue; }
            }
            if (this._mdItem(line)) {
                const lst = this._mdList(lines, i, end);
                out.push(lst.html); i = lst.next; continue;
            }
            const buf = [];
            while (i < end) {
                const l = lines[i];
                if (/^\s*$/.test(l) || /^\s*(```|~~~)/.test(l) || /^\s{0,3}#{1,6}\s/.test(l) ||
                    /^\s{0,3}&gt;\s?/.test(l) || this._mdItem(l) ||
                    /^\s{0,3}([-*_])\s*(\1\s*){2,}$/.test(l)) break;
                buf.push(l.trim());
                i++;
            }
            out.push('<p>' + this._mdInline(buf.join(' ')) + '</p>');
        }
        return out.join('');
    },
    _mdList(lines, start, end) {
        const first = this._mdItem(lines[start]);
        const baseIndent = first.indent;
        const ordered = first.ordered;
        const items = [];
        let i = start;
        while (i < end) {
            if (/^\s*$/.test(lines[i])) {
                let j = i + 1; while (j < end && /^\s*$/.test(lines[j])) j++;
                const nm = j < end ? this._mdItem(lines[j]) : null;
                if (nm && nm.indent >= baseIndent) { i = j; continue; }
                break;
            }
            const m = this._mdItem(lines[i]);
            if (!m || m.indent !== baseIndent) break;
            const body = [m.content];
            i++;
            while (i < end) {
                if (/^\s*$/.test(lines[i])) {
                    let j = i + 1; while (j < end && /^\s*$/.test(lines[j])) j++;
                    if (j < end) {
                        const ind = lines[j].search(/\S/);
                        const jm = this._mdItem(lines[j]);
                        if (ind > baseIndent || (jm && jm.indent > baseIndent)) { body.push(''); i = j; continue; }
                    }
                    break;
                }
                const cm = this._mdItem(lines[i]);
                const ind = lines[i].search(/\S/);
                if (cm && cm.indent <= baseIndent) break;
                if (!cm && ind <= baseIndent) break;
                body.push(lines[i]);
                i++;
            }
            items.push(this._mdListItem(body));
        }
        const tag = ordered ? 'ol' : 'ul';
        return { html: '<' + tag + '>' + items.join('') + '</' + tag + '>', next: i };
    },
    _mdListItem(body) {
        const head = body[0];
        const rest = body.slice(1);
        let min = Infinity;
        rest.forEach((l) => { if (l.trim()) min = Math.min(min, l.search(/\S/)); });
        let inner = '';
        if (rest.length && min !== Infinity) {
            const ded = rest.map((l) => (l.trim() ? l.slice(min) : ''));
            inner = this._mdBlocks(ded, 0, ded.length);
        }
        return '<li>' + this._mdInline(head.trim()) + inner + '</li>';
    },
    _mdTable(lines, start, end) {
        const splitRow = (l) => l.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
        const header = splitRow(lines[start]);
        const align = splitRow(lines[start + 1]).map((c) => {
            const l = c.startsWith(':'), r = c.endsWith(':');
            return l && r ? 'center' : r ? 'right' : l ? 'left' : '';
        });
        let i = start + 2;
        const rows = [];
        while (i < end && lines[i].indexOf('|') !== -1 && lines[i].trim() && !this._mdItem(lines[i])) {
            rows.push(splitRow(lines[i])); i++;
        }
        const sty = (idx) => (align[idx] ? ' style="text-align:' + align[idx] + '"' : '');
        const th = header.map((c, idx) => '<th' + sty(idx) + '>' + this._mdInline(c) + '</th>').join('');
        const trs = rows.map((r) => '<tr>' + header.map((u, idx) => '<td' + sty(idx) + '>' + this._mdInline(r[idx] || '') + '</td>').join('') + '</tr>').join('');
        return { html: '<div class="table-responsive"><table class="table table-sm table-bordered tk-md-table"><thead><tr>' + th + '</tr></thead><tbody>' + trs + '</tbody></table></div>', next: i };
    },

    // ---- Chat bubbles (shared by the task-modal chat and plan-wide Ask Taikun) --
    _bubble(role, html, sourcesHtml) {
        if (role === 'user')
            return '<div class="tk-msg tk-msg-user"><div class="tk-bubble tk-bubble-user">' + html + '</div></div>';
        if (role === 'error')
            return '<div class="tk-msg tk-msg-bot"><span class="avatar avatar-sm rounded-circle bg-red-lt text-red"><i class="ti ti-alert-triangle"></i></span><div class="tk-bubble tk-bubble-error">' + html + '</div></div>';
        return '<div class="tk-msg tk-msg-bot"><span class="avatar avatar-sm rounded-circle bg-primary-lt text-primary"><i class="ti ti-sparkles"></i></span><div class="tk-bubble">' + html + (sourcesHtml || '') + '</div></div>';
    },
    _thinking(id) {
        return '<div id="' + id + '" class="tk-msg tk-msg-bot"><span class="avatar avatar-sm rounded-circle bg-primary-lt text-primary"><i class="ti ti-sparkles"></i></span>'
            + '<div class="tk-bubble text-secondary d-flex align-items-center"><span class="spinner-border spinner-border-sm me-2"></span>Maxwell is reading the plan…</div></div>';
    },

    openCreate() {
        const wsOpts = (this.plan.workstreams || []).map((w) =>
            `<option value="${this.esc(w.workstream_id)}">${this.esc(w.workstream_id)} — ${this.esc(w.name)}</option>`).join('');
        document.getElementById('create-modal-body').innerHTML = `
            <div class="mb-2"><label class="form-label small mb-1">Workstream</label>
                <select id="create-ws" class="form-select form-select-sm">${wsOpts}</select></div>
            ${this._taskFormHtml({ phase: 'Build', status: 'Not Started', risk_level: 'Medium' }, 'new-')}`;
        const flash = document.getElementById('create-flash'); if (flash) flash.textContent = '';
        document.getElementById('create-save').onclick = () => this.createTask();
        window.bootstrap.Modal.getOrCreateInstance(document.getElementById('create-modal')).show();
    },

    async createTask() {
        const flash = (msg, cls) => { const el = document.getElementById('create-flash'); if (el) { el.textContent = msg; el.className = 'small me-auto text-' + (cls || 'secondary'); } };
        const body = this._readForm('new-');
        body.workstream_id = document.getElementById('create-ws').value;
        if (!body.workstream_id) { flash('Pick a workstream', 'danger'); return; }
        if (!body.title) { flash('Title is required', 'danger'); return; }
        flash('Creating…');
        try {
            const res = await fetch('api/tasks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
            if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || `HTTP ${res.status}`); }
            const created = await res.json();
            this.tasks.push(created);
            const w = (this.plan.workstreams || []).find((x) => x.workstream_id === created._wsId);
            if (w) w.tasks.push(created);
            flash('Created ' + created.task_id, 'green');
            this.renderBoard();
            this.renderTasks();
            if (this.isGanttVisible()) this.renderGantt();
            setTimeout(() => window.bootstrap.Modal.getOrCreateInstance(document.getElementById('create-modal')).hide(), 700);
        } catch (e) { flash(e.message, 'danger'); }
    },

    async sendChat(id) {
        const input = document.getElementById('chat-input');
        const log = document.getElementById('chat-log');
        const msg = (input.value || '').trim();
        if (!msg) return;
        input.value = '';
        log.insertAdjacentHTML('beforeend', this._bubble('user', this.esc(msg)));
        log.insertAdjacentHTML('beforeend', this._thinking('chat-thinking'));
        log.scrollTop = log.scrollHeight;
        try {
            const res = await fetch(`api/tasks/${encodeURIComponent(id)}/chat`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: msg }),
            });
            const data = await res.json().catch(() => ({}));
            const think = document.getElementById('chat-thinking'); if (think) think.remove();
            if (!res.ok) {
                log.insertAdjacentHTML('beforeend', this._bubble('error', this.esc(data.detail || ('HTTP ' + res.status))));
                return;
            }
            const src = (data.sources || []).length
                ? `<div class="tk-sources">sources: ${data.sources.map((s) => this.esc(s)).join(', ')}</div>` : '';
            log.insertAdjacentHTML('beforeend', this._bubble('assistant', this.md(data.answer), src));
            if (data.proposal) this.renderProposal(id, data.proposal);
            log.scrollTop = log.scrollHeight;
        } catch (e) {
            const think = document.getElementById('chat-thinking'); if (think) think.remove();
            log.insertAdjacentHTML('beforeend', this._bubble('error', this.esc(e.message)));
        }
    },

    renderProposal(id, p) {
        const log = document.getElementById('chat-log');
        const chips = Object.keys(p).filter((k) => k !== 'rationale' && p[k] != null && p[k] !== '')
            .map((k) => `<span class="badge bg-azure-lt me-1">${this.esc(k)}: ${this.esc(String(p[k]))}</span>`).join('') || '<span class="text-secondary">no fields</span>';
        const pid = 'prop-' + Math.random().toString(36).slice(2, 9);
        log.insertAdjacentHTML('beforeend', `<div id="${pid}" class="card card-sm mb-2">
            <div class="card-status-start bg-azure"></div>
            <div class="card-body">
                <div class="small text-secondary mb-1"><i class="ti ti-robot me-1"></i>Proposed change${p.rationale ? ' — ' + this.esc(p.rationale) : ''}</div>
                <div class="mb-2">${chips}</div>
                <button class="btn btn-primary btn-sm" data-confirm><i class="ti ti-check me-1"></i>Confirm</button>
                <button class="btn btn-sm" data-dismiss>Dismiss</button>
            </div></div>`);
        const card = document.getElementById(pid);
        card.querySelector('[data-confirm]').addEventListener('click', () => this.applyProposal(id, p, pid));
        card.querySelector('[data-dismiss]').addEventListener('click', () => card.remove());
        log.scrollTop = log.scrollHeight;
    },

    async applyProposal(id, p, pid) {
        const body = { _actor: 'Maxwell (confirmed)' };
        Object.keys(p).forEach((k) => { if (k !== 'rationale' && p[k] != null && p[k] !== '') body[k] = p[k]; });
        try {
            const res = await fetch(`api/tasks/${encodeURIComponent(id)}`, {
                method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const updated = await res.json();
            const i = this.tasks.findIndex((x) => x.task_id === id);
            if (i >= 0) this.tasks[i] = Object.assign({}, this.tasks[i], updated);
            const card = document.getElementById(pid);
            if (card) card.querySelector('.card-body').innerHTML = '<span class="text-green"><i class="ti ti-check me-1"></i>Applied</span>';
            // reflect applied fields back into the open edit form
            Object.keys(body).forEach((k) => {
                if (k === '_actor') return;
                const el = document.getElementById('edit-' + k);
                if (el) { if (el.type === 'checkbox') el.checked = !!body[k]; else el.value = body[k]; }
            });
            if (body.title) document.getElementById('task-modal-title').innerHTML = `<span class="me-2">${this.esc(updated.task_id)}</span>${this.esc(updated.title)}`;
            this.renderBoard();
            this.renderTasks();
            if (this.isGanttVisible()) this.renderGantt();
        } catch (e) {
            const card = document.getElementById(pid);
            if (card) card.querySelector('.card-body').insertAdjacentHTML('beforeend', `<div class="text-danger small mt-1">${this.esc(e.message)}</div>`);
        }
    },

    // ---- Ask Taikun (plan-wide agent) -----------------------------------
    async initAsk() {
        if (this._askLoaded) return;
        this._askLoaded = true;
        try {
            const data = await (await fetch('api/chat/history?session=plan')).json();
            if ((data.messages || []).length) {
                const empty = document.getElementById('ask-empty');
                if (empty) empty.remove();
                this.renderAskMessages(data.messages);
                this._askScroll();
            }
        } catch (e) { /* leave the empty hint */ }
    },

    renderAskMessages(messages) {
        const log = document.getElementById('ask-log');
        if (!log) return;
        log.innerHTML = messages.map((m) => {
            if (m.role === 'user')
                return this._bubble('user', this.esc(m.content));
            const sources = (m.payload && m.payload.sources) || [];
            const src = sources.length
                ? `<div class="tk-sources">sources: ${sources.map((s) => this.esc(s)).join(', ')}</div>` : '';
            return this._bubble('assistant', this.md(m.content), src);
        }).join('');
    },

    _askScroll() {
        const log = document.getElementById('ask-log');
        if (log && log.lastElementChild) log.lastElementChild.scrollIntoView({ block: 'nearest' });
    },

    async clearAsk() {
        try { await fetch('api/chat?session=plan', { method: 'DELETE' }); } catch (e) { /* noop */ }
        const log = document.getElementById('ask-log');
        if (log) log.innerHTML = '<div class="text-secondary small">Cleared. Ask about the whole plan below.</div>';
    },

    async sendAsk() {
        const input = document.getElementById('ask-input');
        const log = document.getElementById('ask-log');
        const msg = (input.value || '').trim();
        if (!msg) return;
        input.value = '';
        const empty = document.getElementById('ask-empty');
        if (empty) empty.remove();
        log.insertAdjacentHTML('beforeend', this._bubble('user', this.esc(msg)));
        log.insertAdjacentHTML('beforeend', this._thinking('ask-thinking'));
        this._askScroll();
        try {
            const res = await fetch('api/chat', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: msg, session: 'plan' }),
            });
            const data = await res.json().catch(() => ({}));
            const think = document.getElementById('ask-thinking');
            if (think) think.remove();
            if (!res.ok) {
                log.insertAdjacentHTML('beforeend', this._bubble('error', this.esc(data.detail || ('HTTP ' + res.status))));
                return;
            }
            const sources = data.sources || [];
            const src = sources.length
                ? `<div class="tk-sources">sources: ${sources.map((s) => this.esc(s)).join(', ')}</div>` : '';
            log.insertAdjacentHTML('beforeend', this._bubble('assistant', this.md(data.answer), src));
            const props = (data.proposals && data.proposals.length) ? data.proposals : (data.proposal ? [data.proposal] : []);
            if (props.length === 1) this.renderAskProposal(props[0]);
            else if (props.length > 1) this.renderAskProposals(props);
            this._askScroll();
        } catch (e) {
            const think = document.getElementById('ask-thinking');
            if (think) think.remove();
            log.insertAdjacentHTML('beforeend', this._bubble('error', this.esc(e.message)));
        }
    },

    renderAskProposal(p) {
        const log = document.getElementById('ask-log');
        const fields = Object.keys(p).filter((k) => k !== 'rationale' && k !== 'task_id' && p[k] != null && p[k] !== '');
        const chips = fields.map((k) => `<span class="badge bg-azure-lt me-1">${this.esc(k)}: ${this.esc(String(p[k]))}</span>`).join('') || '<span class="text-secondary">no fields</span>';
        const pid = 'aprop-' + Math.random().toString(36).slice(2, 9);
        log.insertAdjacentHTML('beforeend', `<div id="${pid}" class="card card-sm mb-2">
            <div class="card-status-start bg-azure"></div>
            <div class="card-body">
                <div class="small text-secondary mb-1"><i class="ti ti-robot me-1"></i>Proposed change to <strong>${this.esc(p.task_id)}</strong>${p.rationale ? ' — ' + this.esc(p.rationale) : ''}</div>
                <div class="mb-2">${chips}</div>
                <button class="btn btn-primary btn-sm" data-confirm><i class="ti ti-check me-1"></i>Confirm</button>
                <button class="btn btn-sm" data-dismiss>Dismiss</button>
            </div></div>`);
        const card = document.getElementById(pid);
        card.querySelector('[data-confirm]').addEventListener('click', () => this.applyAskProposal(p, pid));
        card.querySelector('[data-dismiss]').addEventListener('click', () => card.remove());
        this._askScroll();
    },

    async applyAskProposal(p, pid) {
        const body = { _actor: 'Maxwell (confirmed)' };
        Object.keys(p).forEach((k) => { if (!['rationale', 'task_id'].includes(k) && p[k] != null && p[k] !== '') body[k] = p[k]; });
        try {
            const res = await fetch(`api/tasks/${encodeURIComponent(p.task_id)}`, {
                method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const updated = await res.json();
            const i = this.tasks.findIndex((x) => x.task_id === p.task_id);
            if (i >= 0) this.tasks[i] = Object.assign({}, this.tasks[i], updated);
            const card = document.getElementById(pid);
            if (card) card.querySelector('.card-body').innerHTML = `<span class="text-green"><i class="ti ti-check me-1"></i>Applied to ${this.esc(p.task_id)}</span>`;
            this.renderBoard();
            this.renderTasks();
            if (this.isGanttVisible()) this.renderGantt();
        } catch (e) {
            const card = document.getElementById(pid);
            if (card) card.querySelector('.card-body').insertAdjacentHTML('beforeend', `<div class="text-danger small mt-1">${this.esc(e.message)}</div>`);
        }
    },

    _STATUS_TONE: { 'In Progress': 'blue', 'In Review': 'azure', 'Done': 'green', 'Blocked': 'red', 'Not Started': 'secondary' },

    // Compact, low-badge change summary: ONE colored status pill + quiet label→value text
    // (description truncated). Replaces the old badge-per-field dump.
    _propChips(p) {
        const out = [];
        if (p.status) out.push(`<span class="badge bg-${this._STATUS_TONE[p.status] || 'secondary'}-lt">→ ${this.esc(p.status)}</span>`);
        const txt = [];
        if (p.start_date || p.finish_date) txt.push('dates ' + this.esc(p.start_date || '…') + ' → ' + this.esc(p.finish_date || '…'));
        if (p.assignee || p.owner_person_or_role) txt.push('owner ' + this.esc(p.assignee || p.owner_person_or_role));
        if (p.owner_org) txt.push('org ' + this.esc(p.owner_org));
        if (p.title) txt.push('title “' + this.esc(p.title) + '”');
        if (p.description) txt.push('desc “' + this.esc(p.description.slice(0, 60)) + (p.description.length > 60 ? '…' : '') + '”');
        if (p.risk_level) txt.push('risk ' + this.esc(p.risk_level));
        if (p.phase) txt.push('phase ' + this.esc(p.phase));
        if (txt.length) out.push(`<span class="text-secondary small">${txt.join(' · ')}</span>`);
        return out.join(' ') || '<span class="text-secondary small">no field change</span>';
    },

    renderAskProposals(proposals) {
        const log = document.getElementById('ask-log');
        const pid = 'abulk-' + Math.random().toString(36).slice(2, 9);
        const working = proposals.map((p) => Object.assign({}, p));
        const rationale = (proposals.find((p) => p.rationale) || {}).rationale || '';
        const rows = working.map((p, idx) => `
            <div class="d-flex align-items-center gap-2 py-1" data-prow="${idx}">
                <span class="fw-medium font-monospace">${this.esc(p.task_id)}</span>
                <div class="flex-fill">${this._propChips(p)}</div>
                <button class="btn btn-sm btn-ghost-secondary p-1" data-drop="${idx}" title="Drop"><i class="ti ti-x"></i></button>
            </div>`).join('');
        log.insertAdjacentHTML('beforeend', `<div id="${pid}" class="card card-sm mb-2">
            <div class="card-status-start bg-azure"></div>
            <div class="card-body">
                <div class="small text-secondary mb-2"><i class="ti ti-robot me-1"></i>Proposed changes to <strong>${working.length}</strong> tasks${rationale ? ' — ' + this.esc(rationale) : ''}</div>
                <div data-rows>${rows}</div>
                <div class="mt-2">
                    <button class="btn btn-primary btn-sm" data-confirm-all><i class="ti ti-checks me-1"></i>Confirm all</button>
                    <button class="btn btn-sm" data-dismiss-all>Dismiss</button>
                    <span class="small text-secondary ms-2" data-bulk-status></span>
                </div>
            </div></div>`);
        const card = document.getElementById(pid);
        card.querySelectorAll('[data-drop]').forEach((btn) => btn.addEventListener('click', () => {
            const idx = parseInt(btn.getAttribute('data-drop'), 10);
            working[idx] = null;
            const row = card.querySelector(`[data-prow="${idx}"]`);
            if (row) row.remove();
            if (!working.some(Boolean)) card.remove();
        }));
        card.querySelector('[data-dismiss-all]').addEventListener('click', () => card.remove());
        card.querySelector('[data-confirm-all]').addEventListener('click', () => this.applyAskBulk(pid, working.filter(Boolean)));
        this._askScroll();
    },

    async applyAskBulk(pid, working) {
        const card = document.getElementById(pid);
        const statusEl = card ? card.querySelector('[data-bulk-status]') : null;
        const btn = card ? card.querySelector('[data-confirm-all]') : null;
        if (btn) btn.disabled = true;
        let ok = 0, fail = 0;
        for (const p of working) {
            const body = { _actor: 'Maxwell (confirmed)' };
            Object.keys(p).forEach((k) => { if (!['rationale', 'task_id'].includes(k) && p[k] != null && p[k] !== '') body[k] = p[k]; });
            if (Object.keys(body).length <= 1) continue;
            try {
                const res = await fetch(`api/tasks/${encodeURIComponent(p.task_id)}`, {
                    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
                });
                if (!res.ok) throw new Error();
                const updated = await res.json();
                const i = this.tasks.findIndex((x) => x.task_id === p.task_id);
                if (i >= 0) this.tasks[i] = Object.assign({}, this.tasks[i], updated);
                ok++;
            } catch (e) { fail++; }
            if (statusEl) statusEl.textContent = `applied ${ok}${fail ? ' · ' + fail + ' failed' : ''}…`;
        }
        if (card) card.querySelector('.card-body').innerHTML = `<span class="text-green"><i class="ti ti-checks me-1"></i>Applied ${ok} change${ok === 1 ? '' : 's'}${fail ? ' · ' + fail + ' failed' : ''}</span>`;
        this.renderBoard();
        this.renderTasks();
        if (this.isGanttVisible()) this.renderGantt();
    },

    // ---- Intake (ingest + triage an artifact) ---------------------------
    async submitIntake() {
        const kind = document.getElementById('intake-kind').value;
        const title = (document.getElementById('intake-title').value || '').trim();
        const text = (document.getElementById('intake-text').value || '').trim();
        const flash = document.getElementById('intake-flash');
        if (!text) { if (flash) flash.textContent = 'Paste some text first.'; return; }
        if (flash) flash.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Ingesting + triaging…';
        const log = document.getElementById('ask-log');
        const empty = document.getElementById('ask-empty'); if (empty) empty.remove();
        log.insertAdjacentHTML('beforeend', this._bubble('user', `<span class="badge bg-white text-blue me-1">intake</span>${this.esc(kind)}${title ? ' · ' + this.esc(title) : ''}`));
        this._askScroll();
        try {
            const res = await fetch('api/intake', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind, title, text }) });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || ('HTTP ' + res.status));
            if (flash) flash.textContent = `ingested ${data.ingested_chunks} chunk(s) into the corpus`;
            const src = (data.sources || []).length ? `<div class="tk-sources">sources: ${data.sources.map((s) => this.esc(s)).join(', ')}</div>` : '';
            log.insertAdjacentHTML('beforeend', this._bubble('assistant', this.md(data.summary), src));
            const props = data.proposals || [];
            if (props.length === 1) this.renderAskProposal(props[0]);
            else if (props.length > 1) this.renderAskProposals(props);
            if ((data.new_tasks || []).length) this.renderAskNewTasks(data.new_tasks);
            document.getElementById('intake-text').value = '';
            this._askScroll();
        } catch (e) {
            if (flash) flash.textContent = '';
            log.insertAdjacentHTML('beforeend', this._bubble('error', this.esc(e.message)));
        }
    },

    async submitIntakeUpload(f) {
        const flash = document.getElementById('intake-flash');
        const kind = (document.getElementById('intake-kind') || {}).value || 'document';
        const isMedia = /\.(m4a|mp3|mp4|wav|webm|mov|m4v|aac|ogg|oga|flac|mpeg|mpga|amr)$/i.test(f.name);
        const log = document.getElementById('ask-log');
        const empty = document.getElementById('ask-empty'); if (empty) empty.remove();
        log.insertAdjacentHTML('beforeend', this._bubble('user', `<span class="badge bg-white text-blue me-1">${isMedia ? 'media' : 'file'}</span>${this.esc(f.name)}`));
        if (flash) flash.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>' + (isMedia ? 'Transcribing &amp; ingesting…' : 'Extracting &amp; ingesting…');
        this._askScroll();
        try {
            const fd = new FormData();
            fd.append('file', f);
            fd.append('kind', isMedia ? 'transcript' : kind);
            fd.append('title', f.name);
            const res = await fetch('api/intake/upload', { method: 'POST', body: fd });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || ('HTTP ' + res.status));
            if (flash) flash.textContent = `${data.transcribed ? 'transcribed + ' : ''}ingested ${data.ingested_chunks} chunk(s) into the corpus`;
            const src = (data.sources || []).length ? `<div class="tk-sources">sources: ${data.sources.map((s) => this.esc(s)).join(', ')}</div>` : '';
            log.insertAdjacentHTML('beforeend', this._bubble('assistant', this.md(data.summary), src));
            const props = data.proposals || [];
            if (props.length === 1) this.renderAskProposal(props[0]);
            else if (props.length > 1) this.renderAskProposals(props);
            if ((data.new_tasks || []).length) this.renderAskNewTasks(data.new_tasks);
            this._askScroll();
        } catch (e) {
            if (flash) flash.textContent = '';
            log.insertAdjacentHTML('beforeend', this._bubble('error', this.esc(e.message)));
        }
    },

    // ---- Exec-tab corpus upload (drop a doc -> ingest -> agent reacts) ----
    _readFilesThenIngest(files) {
        if (!files || !files.length) return;
        const f = files[0];
        if (/\.(m4a|mp3|mp4|wav|webm|mov|m4v|aac|ogg|oga|flac|mpeg|mpga|amr|pdf|docx|pptx)$/i.test(f.name)) {
            this.submitExecFile(f);
            return;
        }
        const reader = new FileReader();
        reader.onload = () => this.submitExecUpload(String(reader.result || ''), f.name);
        reader.onerror = () => { const fl = document.getElementById('exec-upload-flash'); if (fl) fl.textContent = 'Could not read that file.'; };
        reader.readAsText(f);
    },

    async submitExecUpload(text, title) {
        const flash = document.getElementById('exec-upload-flash');
        if (!text || !text.trim()) { if (flash) flash.textContent = 'Drop a file or paste text first.'; return; }
        await this._execIngest(() => fetch('api/intake', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind: 'document', title: title || '', text }) }), false);
        const paste = document.getElementById('exec-paste'); if (paste) paste.value = '';
    },

    async submitExecFile(f) {
        const isMedia = /\.(m4a|mp3|mp4|wav|webm|mov|m4v|aac|ogg|oga|flac|mpeg|mpga|amr)$/i.test(f.name);
        await this._execIngest(() => {
            const fd = new FormData();
            fd.append('file', f);
            fd.append('kind', isMedia ? 'transcript' : 'document');
            fd.append('title', f.name);
            return fetch('api/intake/upload', { method: 'POST', body: fd });
        }, isMedia);
    },

    async _execIngest(doFetch, isMedia) {
        const flash = document.getElementById('exec-upload-flash');
        const out = document.getElementById('exec-upload-result');
        if (flash) flash.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>' + (isMedia ? 'Transcribing &amp; ingesting… (long audio takes a few minutes)' : 'Ingesting + reacting…');
        if (out) out.innerHTML = '';
        try {
            const res = await doFetch();
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || ('HTTP ' + res.status));
            if (flash) flash.textContent = `${data.transcribed ? 'transcribed + ' : ''}ingested ${data.ingested_chunks || 0} chunk(s) into the corpus`;
            const props = data.proposals || [];
            const newTasks = data.new_tasks || [];
            let html = '<div class="card card-sm"><div class="card-body">'
                + '<div class="d-flex align-items-center mb-2"><span class="avatar avatar-xs rounded bg-primary-lt text-primary me-2"><i class="ti ti-sparkles"></i></span><span class="fw-semibold">What this changes</span></div>';
            if (data.summary) html += `<div class="mb-2">${this.md(data.summary)}</div>`;
            if (props.length) {
                html += '<div class="subheader mb-1">Proposed task changes</div>';
                html += props.map((p) => {
                    const id = p.task_id || p.id || '';
                    const act = p.action || p.change || p.status || p.summary || 'update';
                    const done = /done|close|complete|resolve/i.test(String(act));
                    return `<div class="d-flex align-items-start mb-1"><span class="status-dot bg-${done ? 'green' : 'orange'} mt-1 me-2"></span><div class="small">${id ? `<span class="fw-medium">${this.esc(id)}</span> ` : ''}${this.esc(act)}</div></div>`;
                }).join('');
            }
            if (newTasks.length) {
                html += '<div class="subheader mb-1 mt-2">Suggested new tasks</div>';
                html += newTasks.map((t) => `<div class="d-flex align-items-start mb-1"><i class="ti ti-plus text-green mt-1 me-2"></i><div class="small">${this.esc(t.title || t.summary || t.task || '')}</div></div>`).join('');
            }
            if (!props.length && !newTasks.length) html += '<div class="text-secondary small">No task changes detected — ingested into the corpus for reference.</div>';
            html += '<div class="mt-2"><a href="#tab-inbox" data-bs-toggle="tab" class="btn btn-sm btn-primary"><i class="ti ti-checklist me-1"></i>Review &amp; confirm in the Action Queue</a></div>';
            html += '</div></div>';
            if (out) out.innerHTML = html;
            const paste = document.getElementById('exec-paste'); if (paste) paste.value = '';
            if (this.loadSignals) this.loadSignals(); // refresh inbox so new triage shows up
        } catch (e) {
            if (flash) flash.textContent = '';
            if (out) out.innerHTML = `<div class="alert alert-danger py-2 px-3 small mb-0">${this.esc(e.message)}</div>`;
        }
    },

    renderAskNewTasks(newTasks) {
        const log = document.getElementById('ask-log');
        const pid = 'anew-' + Math.random().toString(36).slice(2, 9);
        const working = newTasks.map((t) => Object.assign({}, t));
        const rows = working.map((t, idx) => `<div class="d-flex align-items-center gap-2 py-1" data-nrow="${idx}">
                <span class="badge bg-azure-lt">${this.esc(t.workstream_id)}</span>
                <span class="flex-fill fw-medium">${this.esc(t.title)}</span>
                <span class="text-secondary small">${this.esc(t.owner_person_or_role || '')}</span>
                <button class="btn btn-sm btn-ghost-secondary p-1" data-ndrop="${idx}" title="Drop"><i class="ti ti-x"></i></button>
            </div>`).join('');
        log.insertAdjacentHTML('beforeend', `<div id="${pid}" class="card card-sm mb-2">
            <div class="card-status-start bg-green"></div>
            <div class="card-body">
                <div class="small text-secondary mb-2"><i class="ti ti-plus me-1"></i>Proposed <strong>${working.length}</strong> new task(s)</div>
                <div data-nrows>${rows}</div>
                <div class="mt-2">
                    <button class="btn btn-primary btn-sm" data-create-all><i class="ti ti-checks me-1"></i>Create all</button>
                    <button class="btn btn-sm" data-dismiss-all>Dismiss</button>
                    <span class="small text-secondary ms-2" data-new-status></span>
                </div>
            </div></div>`);
        const card = document.getElementById(pid);
        card.querySelectorAll('[data-ndrop]').forEach((b) => b.addEventListener('click', () => {
            const idx = parseInt(b.getAttribute('data-ndrop'), 10);
            working[idx] = null;
            const row = card.querySelector(`[data-nrow="${idx}"]`);
            if (row) row.remove();
            if (!working.some(Boolean)) card.remove();
        }));
        card.querySelector('[data-dismiss-all]').addEventListener('click', () => card.remove());
        card.querySelector('[data-create-all]').addEventListener('click', () => this.applyAskNewTasks(pid, working.filter(Boolean)));
        this._askScroll();
    },

    async applyAskNewTasks(pid, working) {
        const card = document.getElementById(pid);
        const statusEl = card ? card.querySelector('[data-new-status]') : null;
        let ok = 0, fail = 0;
        for (const t of working) {
            const body = Object.assign({ _actor: 'Maxwell (confirmed)' }, t);
            delete body.rationale;
            try {
                const res = await fetch('api/tasks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
                if (!res.ok) throw new Error();
                const created = await res.json();
                this.tasks.push(created);
                const w = (this.plan.workstreams || []).find((x) => x.workstream_id === created._wsId);
                if (w) w.tasks.push(created);
                ok++;
            } catch (e) { fail++; }
            if (statusEl) statusEl.textContent = `created ${ok}${fail ? ' · ' + fail + ' failed' : ''}…`;
        }
        if (card) card.querySelector('.card-body').innerHTML = `<span class="text-green"><i class="ti ti-checks me-1"></i>Created ${ok} task${ok === 1 ? '' : 's'}${fail ? ' · ' + fail + ' failed' : ''}</span>`;
        this.renderBoard();
        this.renderTasks();
    },

    // ---- Pulse (weekly digest) ------------------------------------------
    // Kept for back-compat — digests, digest history and inbox summaries now use
    // the full rich renderer (nested lists, headings, code, tables, links).
    mdLite(text) {
        return this.md(text);
    },

    renderDigest(d, el) {
        if (!el) return;
        const when = d.created_at ? new Date(d.created_at * 1000).toLocaleString() : '';
        const c = (d.meta && d.meta.counts) || d.counts || {};
        const chips = ['overdue', 'critical_slip', 'ready', 'due_soon'].filter((k) => c[k] != null)
            .map((k) => `<span class="badge bg-secondary-lt me-1">${k.replace('_', ' ')}: ${c[k]}</span>`).join('');
        const ns = this.notifyStatus || {};
        const channels = [ns.slack && 'Slack', ns.email && 'Email'].filter(Boolean).join(' + ');
        const sendLabel = channels ? `Send · ${channels}` : 'Send · dry-run';
        el.classList.remove('text-secondary');
        el.innerHTML = `<div class="d-flex flex-wrap align-items-center gap-2 mb-2">
                <span class="text-secondary small">${this.esc(when)}</span>${chips}
                <button class="btn btn-sm btn-outline-primary ms-auto" data-send-digest="${d.id}"><i class="ti ti-send me-1"></i>${sendLabel}</button>
            </div>
            <div>${this.mdLite(d.content)}</div>
            <div class="small text-secondary mt-2" data-send-result></div>`;
        const btn = el.querySelector('[data-send-digest]');
        if (btn) btn.addEventListener('click', () => this.sendDigest(d.id, el));
    },

    async sendDigest(id, el) {
        const out = el ? el.querySelector('[data-send-result]') : null;
        if (out) out.textContent = 'Sending…';
        try {
            const data = await (await fetch(`api/digest/${id}/send`, { method: 'POST' })).json();
            const parts = (data.results || []).map((x) => `${x.channel}: ${x.sent ? 'sent' : (x.dry_run ? 'dry-run (not configured)' : ('failed' + (x.error ? ' — ' + x.error : '')))}`);
            if (out) out.textContent = parts.join('  ·  ');
        } catch (e) {
            if (out) out.textContent = 'send failed: ' + e.message;
        }
    },

    renderDigestHistory(list) {
        const el = document.getElementById('digest-history');
        if (!el) return;
        el.innerHTML = (list || []).map((d) => {
            const when = d.created_at ? new Date(d.created_at * 1000).toLocaleString() : '';
            const id = 'dg-' + d.id;
            return `<div class="card card-sm mb-2">
                <div class="card-header py-2">
                    <a class="text-reset" data-bs-toggle="collapse" href="#${id}"><i class="ti ti-chevron-down me-1"></i>Digest · ${this.esc(when)}</a>
                </div>
                <div class="collapse" id="${id}"><div class="card-body">${this.mdLite(d.content)}</div></div>
            </div>`;
        }).join('');
    },

    async initPulse() {
        try { this.notifyStatus = await (await fetch('api/notify/status')).json(); } catch (e) { /* dry-run */ }
        this.renderTallyPulse();
        try {
            const data = await (await fetch('api/digests')).json();
            const ds = data.digests || [];
            if (ds.length) {
                this.renderDigest(ds[0], document.getElementById('digest-latest'));
                this.renderDigestHistory(ds.slice(1));
            }
        } catch (e) { /* keep the empty hint */ }
    },

    renderTallyPulse() {
        const el = document.getElementById('tally-pulse');
        if (!el) return;
        const tally = this.tally || {};
        const totals = tally.totals || {};
        const spend = totals.spend || {};
        const unit = totals.unit_cost || {};
        const kpiUnitCost = unit.cost_per_kpi_contribution_unit != null ? this.money(unit.cost_per_kpi_contribution_unit) + ' / unit' : '—';
        const ws = (tally.by_workstream || []).filter((w) => (w.spend || {}).cost_usd || w.verified_outcomes).slice(0, 6);
        const kpis = (tally.kpis || []).filter((k) => k.verified_contribution || ((k.spend || {}).cost_usd)).slice(0, 5);
        const metric = (label, value, sub, icon) => `<div class="col-6 col-lg-3"><div class="card"><div class="card-body p-3">
            <div class="subheader"><i class="ti ti-${icon} me-1"></i>${label}</div>
            <div class="h1 mb-0 mt-1">${value}</div>
            <div class="text-secondary small">${sub}</div>
        </div></div></div>`;
        const wsRows = ws.length ? ws.map((w) => `<tr>
            <td><span class="fw-semibold">${this.esc(w.workstream_id)}</span><div class="text-secondary small">${this.esc(w.name || '')}</div></td>
            <td class="text-end">${this.money((w.spend || {}).cost_usd || 0)}</td>
            <td class="text-end">${this.compact(w.verified_outcomes || 0)}</td>
            <td class="text-end">${this.money((w.unit_cost || {}).cost_per_verified_outcome)}</td>
        </tr>`).join('') : `<tr><td colspan="4" class="text-secondary text-center py-3">No Tally records yet.</td></tr>`;
        const kpiRows = kpis.length ? kpis.map((k) => `<tr>
            <td><span class="fw-semibold">${this.esc((k.kpi || {}).name || 'KPI')}</span><div class="text-secondary small">${this.esc((k.kpi || {}).unit || '')}</div></td>
            <td class="text-end">${this.compact(k.verified_contribution || 0)}</td>
            <td class="text-end">${this.money((k.spend || {}).cost_usd || 0)}</td>
            <td class="text-end">${this.money((k.unit_cost || {}).cost_per_contribution_unit)}</td>
        </tr>`).join('') : `<tr><td colspan="4" class="text-secondary text-center py-3">No KPI movement yet.</td></tr>`;
        el.innerHTML = `<div class="row row-cards mt-3 mb-3">
            ${metric('Spend', this.money(spend.cost_usd || 0), `${this.compact(spend.total_tokens || 0)} tokens`, 'cash')}
            ${metric('Verified outcomes', this.compact(totals.verified_outcomes || 0), `${this.compact(totals.proposed_outcomes || 0)} proposed`, 'target-arrow')}
            ${metric('Cost / outcome', this.money(unit.cost_per_verified_outcome), 'verified only', 'receipt-2')}
            ${metric('KPI movement', this.compact(totals.verified_kpi_contribution || 0), kpiUnitCost, 'chart-arrows-vertical')}
        </div>
        <div class="row row-cards mb-3">
            <div class="col-lg-6"><div class="card">
                <div class="card-header"><h3 class="card-title"><i class="ti ti-stack-2 me-2"></i>Workstream economics</h3></div>
                <div class="table-responsive"><table class="table table-vcenter mb-0">
                    <thead><tr><th>Workstream</th><th class="text-end">Spend</th><th class="text-end">Verified</th><th class="text-end">Cost / outcome</th></tr></thead>
                    <tbody>${wsRows}</tbody>
                </table></div>
            </div></div>
            <div class="col-lg-6"><div class="card">
                <div class="card-header"><h3 class="card-title"><i class="ti ti-chart-arrows-vertical me-2"></i>KPI economics</h3></div>
                <div class="table-responsive"><table class="table table-vcenter mb-0">
                    <thead><tr><th>KPI</th><th class="text-end">Movement</th><th class="text-end">Spend</th><th class="text-end">Cost / unit</th></tr></thead>
                    <tbody>${kpiRows}</tbody>
                </table></div>
            </div></div>
        </div>`;
    },

    async genDigest() {
        const el = document.getElementById('digest-latest');
        const btn = document.getElementById('digest-gen');
        if (btn) btn.disabled = true;
        if (el) { el.classList.add('text-secondary'); el.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Maxwell is writing the brief…'; }
        try {
            const d = await (await fetch('api/digest', { method: 'POST' })).json();
            if (d.detail) throw new Error(d.detail);
            this.renderDigest(d, el);
            const data = await (await fetch('api/digests')).json();
            this.renderDigestHistory((data.digests || []).slice(1));
        } catch (e) {
            if (el) el.innerHTML = `<span class="text-danger">Digest failed: ${this.esc(e.message)}</span>`;
        } finally {
            if (btn) btn.disabled = false;
        }
    },

    // ---- Action Queue (universal review queue — every triage source lands here) -----
    async initInbox() {
        try {
            const data = await (await fetch('api/inbox')).json();
            this.inboxItems = data.items || [];
            this._renderInboxBadge(data.pending || 0);
            this.renderInbox(this.inboxItems);
            if (document.getElementById('exec-content')) this.renderExec();   // refresh the front-page Action Queue box
        } catch (e) {
            const el = document.getElementById('inbox-content');
            if (el) el.innerHTML = '<div class="text-secondary small">Queue unavailable — the plan API is unreachable.</div>';
        }
    },

    _renderInboxBadge(n) {
        const tab = document.querySelector('a[href="#tab-inbox"]');
        if (!tab) return;
        let b = tab.querySelector('.badge');
        if (n > 0) {
            if (!b) { b = document.createElement('span'); b.className = 'badge bg-red ms-1'; tab.appendChild(b); }
            b.textContent = n;
        } else if (b) { b.remove(); }
    },

    QUEUE_FILTERS: [
        { key: 'all', label: 'All', icon: 'ti-list-details' },
        { key: 'evidence', label: 'Needs evidence', icon: 'ti-alert-triangle' },
        { key: 'status', label: 'Status', icon: 'ti-progress-check' },
        { key: 'dates', label: 'Dates', icon: 'ti-calendar-event' },
        { key: 'new', label: 'New tasks', icon: 'ti-plus' },
    ],

    // A proposal "needs evidence" if it would CLOSE a task (status -> Done): direction can be
    // confirmed in bulk, but closing needs acceptance evidence, so we hold those separately.
    _needsEvidence(p) { return !!(p && p.status === 'Done'); },

    _propMatchesFilter(p, f) {
        switch (f) {
            case 'evidence': return this._needsEvidence(p);
            case 'dates': return !!(p.start_date || p.finish_date);
            case 'status': return !!p.status && !this._needsEvidence(p);
            case 'new': return false;
            default: return true;   // 'all'
        }
    },

    _srcMeta(source) {
        const s = (source || '').toLowerCase();
        if (s.indexOf('transcript') >= 0) return { icon: 'ti-microphone', label: 'Call' };
        if (s.indexOf('email') >= 0) return { icon: 'ti-mail', label: 'Email' };
        if (s.indexOf('note') >= 0 || s.indexOf('paste') >= 0) return { icon: 'ti-clipboard-text', label: 'Note' };
        if (s.indexOf('upload') >= 0 || s.indexOf('document') >= 0) return { icon: 'ti-file-text', label: 'Upload' };
        return { icon: 'ti-inbox', label: source || 'Item' };
    },

    _queueCounts() {
        let items = 0, props = 0, evidence = 0, news = 0;
        (this.inboxItems || []).filter((it) => it.status === 'pending').forEach((it) => {
            const tri = it.triage || {}; items++;
            (tri.proposals || []).forEach((p) => { props++; if (this._needsEvidence(p)) evidence++; });
            news += (tri.new_tasks || []).length;
        });
        return { items, props, evidence, news, actions: props + news, safe: props + news - evidence };
    },

    // Universal Operator Queue — KPI strip + filter bar + queue table + preview modal,
    // matching the canonical concept-queue.html on demo.taikunai.com.
    renderInbox(items) {
        const el = document.getElementById('inbox-content');
        if (!el) return;
        el.classList.remove('text-secondary');
        this._ensureQueueModal();
        const all = items || this.inboxItems || [];
        const c = this._queueCounts();
        const confirmedCount = all.filter((it) => it.status === 'confirmed' || it.status === 'applied').length;
        const f = this.queueFilter || 'all';
        const src = this.queueSource || 'all';
        const hideDone = this.queueHideConfirmed !== false;   // default: hide the audit log

        // ---- KPI strip (mirrors the live console KPIs) ----
        const kpi = `<div class="row row-cards mb-3">
            ${this._kpiCard('Awaiting review', c.items, 'red', 'items in the queue')}
            ${this._kpiCard('Pending actions', c.actions, 'orange', 'changes + new tasks')}
            ${this._kpiCard('Needs evidence', c.evidence, 'red', 'status → Done, held by default', c.evidence > 0)}
            ${this._kpiCard('Confirmed', confirmedCount, 'green', 'applied from the queue')}
        </div>`;

        // ---- universal filter bar ----
        const typeOpts = this.QUEUE_FILTERS.map((q) =>
            `<option value="${q.key}"${q.key === f ? ' selected' : ''}>${q.key === 'all' ? 'Type · any' : q.label}</option>`).join('');
        const srcOpts = ['all', 'call', 'email', 'upload', 'note'].map((s) =>
            `<option value="${s}"${s === src ? ' selected' : ''}>${s === 'all' ? 'Source · any' : s[0].toUpperCase() + s.slice(1)}</option>`).join('');
        const filterBar = `<div class="card mb-3"><div class="card-body py-2"><div class="row g-2 align-items-center">
            <div class="col-12 col-md"><div class="input-icon">
                <span class="input-icon-addon"><i class="ti ti-search"></i></span>
                <input id="q-search" type="text" class="form-control" placeholder="Search subject, task id, or text…" autocomplete="off"/>
            </div></div>
            <div class="col-6 col-md-auto"><select id="q-type" class="form-select">${typeOpts}</select></div>
            <div class="col-6 col-md-auto"><select id="q-source" class="form-select">${srcOpts}</select></div>
            <div class="col-auto"><label class="form-check form-switch m-0">
                <input id="q-hide" class="form-check-input" type="checkbox"${hideDone ? ' checked' : ''}/>
                <span class="form-check-label">Hide confirmed</span></label></div>
        </div></div></div>`;

        // ---- bulk action bar ----
        const bulk = c.actions ? `<div class="d-flex flex-wrap align-items-center gap-2 mb-3">
            <button class="btn btn-primary" data-confirm-safe><i class="ti ti-checks me-1"></i>Confirm all safe
                <span class="badge bg-white text-primary ms-1">${c.safe}</span></button>
            <button class="btn btn-outline-primary" data-confirm-all>Confirm everything (${c.actions})</button>
            <span class="text-secondary small ms-1">${c.items} item${c.items !== 1 ? 's' : ''} · ${c.actions} pending action${c.actions !== 1 ? 's' : ''}${c.evidence ? ` · <span class="text-orange fw-medium">${c.evidence} need evidence</span>` : ''}</span>
        </div>` : '';

        // ---- rows (source + type filtered) ----
        const typeMatch = (it) => {
            if (f === 'all') return true;
            const tri = it.triage || {};
            if (f === 'new') return (tri.new_tasks || []).length > 0;
            return (tri.proposals || []).some((p) => this._propMatchesFilter(p, f));
        };
        const visible = all.filter((it) => it.status === 'pending'
            && (src === 'all' || this._srcKey(it.source) === src) && typeMatch(it));
        const logItems = all.filter((it) => it.status !== 'pending'
            && (src === 'all' || this._srcKey(it.source) === src)).slice(0, 15);

        const rows = visible.map((it) => this._queueRow(it)).join('')
            || `<tr><td colspan="6" class="text-secondary text-center py-4">${c.actions ? 'No items match the filters.' : 'Queue is clear — upload a call, paste notes, or forward an email and proposals land here.'}</td></tr>`;
        const tableCard = `<div class="card"><div class="table-responsive">
            <table class="table table-vcenter card-table table-hover">
                <thead><tr><th class="w-1"></th><th>Item</th><th>Source</th><th>Proposed changes</th><th>Age</th><th class="w-1"></th></tr></thead>
                <tbody>${rows}</tbody>
            </table></div></div>`;

        const audit = (!hideDone && logItems.length) ? `<div class="hr-text text-secondary mt-4 mb-2">Recently actioned</div>
            <div class="card"><div class="table-responsive"><table class="table table-vcenter card-table">
                <tbody>${logItems.map((it) => this._queueLogRow(it)).join('')}</tbody></table></div></div>` : '';

        const flash = this._queueFlash
            ? `<div class="alert alert-success py-2 px-3 small mb-3"><i class="ti ti-checks me-1"></i>${this.esc(this._queueFlash)}</div>` : '';
        this._queueFlash = null;

        el.innerHTML = flash + kpi + filterBar + bulk + tableCard + audit;

        const sb = el.querySelector('[data-confirm-safe]'); if (sb) sb.addEventListener('click', () => this.confirmAll(true));
        const ab = el.querySelector('[data-confirm-all]'); if (ab) ab.addEventListener('click', () => this.confirmAll(false));
        const qt = el.querySelector('#q-type'); if (qt) qt.addEventListener('change', () => { this.queueFilter = qt.value; this.renderInbox(this.inboxItems); });
        const qsrc = el.querySelector('#q-source'); if (qsrc) qsrc.addEventListener('change', () => { this.queueSource = qsrc.value; this.renderInbox(this.inboxItems); });
        const qh = el.querySelector('#q-hide'); if (qh) qh.addEventListener('change', () => { this.queueHideConfirmed = qh.checked; this.renderInbox(this.inboxItems); });
        const qsearch = el.querySelector('#q-search');   // in-place filter — no re-render (keeps focus)
        if (qsearch) qsearch.addEventListener('input', () => {
            const v = qsearch.value.toLowerCase();
            el.querySelectorAll('tbody tr[data-qrow]').forEach((tr) => {
                tr.style.display = (!v || (tr.getAttribute('data-hay') || '').indexOf(v) >= 0) ? '' : 'none';
            });
        });
        el.querySelectorAll('[data-qopen]').forEach((r) => r.addEventListener('click', () => this.openQueueItem(r.getAttribute('data-qopen'))));
    },

    _kpiCard(title, value, tone, sub, animated) {
        return `<div class="col-sm-6 col-lg-3"><div class="card"><div class="card-body">
            <div class="d-flex align-items-center"><div class="subheader">${title}</div>
                <div class="ms-auto"><span class="status-dot ${animated ? 'status-dot-animated ' : ''}bg-${tone}"></span></div></div>
            <div class="h1 mb-0 mt-1">${value}</div>
            <div class="text-secondary small">${sub}</div></div></div></div>`;
    },

    _relAge(ts) {
        if (!ts) return '';
        const s = Math.max(0, Date.now() / 1000 - ts);
        if (s < 60) return Math.floor(s) + 's';
        if (s < 3600) return Math.floor(s / 60) + 'm';
        if (s < 86400) return Math.floor(s / 3600) + 'h';
        return Math.floor(s / 86400) + 'd';
    },

    _srcKey(source) {
        const s = (source || '').toLowerCase();
        if (s.indexOf('transcript') >= 0) return 'call';
        if (s.indexOf('email') >= 0) return 'email';
        if (s.indexOf('note') >= 0 || s.indexOf('paste') >= 0) return 'note';
        return 'upload';
    },

    _queueRow(it) {
        const tri = it.triage || {};
        const sm = this._srcMeta(it.source);
        const props = tri.proposals || [];
        const nts = tri.new_tasks || [];
        const nAct = props.length + nts.length;
        const nEv = props.filter((p) => this._needsEvidence(p)).length;
        const hay = ((it.subject || '') + ' ' + props.map((p) => p.task_id).join(' ') + ' ' + (it.summary || '')).toLowerCase();
        const ids = props.slice(0, 4).map((p) => this.esc(p.task_id)).join(', ') + (nAct > 4 ? `, +${nAct - 4}` : '');
        return `<tr data-qrow data-qopen="${it.id}" data-hay="${this.esc(hay)}" style="cursor:pointer">
            <td><span class="status-dot bg-${nEv ? 'orange' : 'green'}"></span></td>
            <td><div class="fw-bold text-truncate" style="max-width:420px">${this.esc(it.subject || sm.label)}</div>
                <div class="text-secondary small">${this.esc(sm.label)} · ${this._relAge(it.received_at)} ago</div></td>
            <td><span class="text-secondary"><i class="ti ${sm.icon} me-1"></i>${this.esc(sm.label)}</span></td>
            <td><span class="badge bg-secondary-lt">${nAct} action${nAct !== 1 ? 's' : ''}</span>${nEv ? ` <span class="text-orange small">· ${nEv} need evidence</span>` : ''}
                <div class="text-secondary small font-monospace text-truncate" style="max-width:300px">${ids}</div></td>
            <td class="text-secondary">${this._relAge(it.received_at)}</td>
            <td class="text-end"><a href="#" class="btn btn-primary btn-sm" onclick="return false">Review</a></td>
        </tr>`;
    },

    _queueLogRow(it) {
        const tri = it.triage || {};
        const sm = this._srcMeta(it.source);
        const a = tri.applied || {};
        const u = a.updated || [], cr = a.created || [];
        const did = (u.length || cr.length)
            ? `${u.length ? 'updated ' + u.map((x) => this.esc(x)).join(', ') : ''}${(u.length && cr.length) ? ' · ' : ''}${cr.length ? 'created ' + cr.map((x) => this.esc(x)).join(', ') : ''}`
            : 'no task change — ingested for reference';
        const dc = it.status === 'dismissed' ? 'secondary' : 'green';
        return `<tr data-qopen="${it.id}" style="cursor:pointer">
            <td class="w-1"><span class="status-dot bg-${dc}"></span></td>
            <td><span class="fw-medium">${this.esc(it.subject || sm.label)}</span>
                <span class="text-secondary small">· ${did}</span></td>
            <td class="text-end"><span class="badge bg-${dc}-lt">${this.esc(it.status)}</span> <i class="ti ti-chevron-right text-secondary"></i></td>
        </tr>`;
    },

    _ensureQueueModal() {
        if (document.getElementById('queue-modal')) return;
        const wrap = document.createElement('div');
        wrap.innerHTML = `<div class="modal modal-blur fade" id="queue-modal" tabindex="-1" aria-hidden="true">
            <div class="modal-dialog modal-lg modal-dialog-centered modal-dialog-scrollable">
                <div class="modal-content" id="queue-modal-content"></div>
            </div></div>`;
        document.body.appendChild(wrap.firstElementChild);
    },

    _hideQueueModal() {
        const m = document.getElementById('queue-modal');
        if (m && window.bootstrap) { const inst = window.bootstrap.Modal.getInstance(m); if (inst) inst.hide(); }
    },

    // Open one queue item in the preview modal — proposals shown grouped, each editable/droppable,
    // with Confirm safe / Confirm incl. closes / Dismiss. Working copies so edits/drops are local
    // until Confirm.
    openQueueItem(id) {
        const it = (this.inboxItems || []).find((x) => String(x.id) === String(id));
        if (!it) return;
        const tri = it.triage || {};
        const sm = this._srcMeta(it.source);
        const props = (tri.proposals || []).map((p) => Object.assign({}, p));
        const nts = (tri.new_tasks || []).map((t) => Object.assign({}, t));
        const nEv = props.filter((p) => this._needsEvidence(p)).length;
        const nSafe = props.length - nEv;
        const nAct = props.length + nts.length;
        const when = it.received_at ? new Date(it.received_at * 1000).toLocaleString() : '';
        // Confirmed/dismissed items open read-only: show what was applied, no edit/drop/confirm.
        const isPast = !!(it.status && it.status !== 'pending');
        const dismissed = it.status === 'dismissed';
        const ap = tri.applied || {};
        const apUpd = ap.updated || [], apCr = ap.created || [];

        // change row (evidence-table style): marker avatar | id | change pill + rationale + inline editor | hover edit/drop
        const propRow = (p, idx) => {
            const ev = this._needsEvidence(p);
            const mk = isPast ? (dismissed ? { c: 'secondary', i: 'minus' } : { c: 'green', i: 'check' }) : (ev ? { c: 'orange', i: 'lock' } : { c: 'green', i: 'check' });
            return `<tr class="tk-evrow" data-iprow="${idx}">
                <td class="w-1"><span class="avatar avatar-xs bg-${mk.c}-lt text-${mk.c} rounded-circle"><i class="ti ti-${mk.i}"></i></span></td>
                <td style="width:104px"><span class="fw-bold font-monospace">${this.esc(p.task_id)}</span></td>
                <td>
                    <div class="tk-change" data-pchips="${idx}">${this._propChips(p)}</div>
                    ${p.rationale ? `<div class="text-secondary small mt-1">${this.esc(p.rationale)}</div>` : ''}
                    ${isPast ? '' : `<div class="mt-2 d-none" data-peditor="${idx}"></div>`}
                </td>
                ${isPast ? '' : `<td class="text-end w-1"><span class="tk-rowctl btn-list">
                    <a href="#" class="btn btn-ghost-secondary btn-icon btn-sm" data-pedit="${idx}" onclick="return false" aria-label="Edit"><i class="ti ti-pencil"></i></a>
                    <a href="#" class="btn btn-ghost-secondary btn-icon btn-sm" data-ipdrop="${idx}" onclick="return false" aria-label="Drop"><i class="ti ti-x"></i></a>
                </span></td>`}
            </tr>`;
        };
        const ntRow = (t, idx) => `<tr class="tk-evrow" data-introw="${idx}">
                <td class="w-1"><span class="avatar avatar-xs bg-azure-lt text-azure rounded-circle"><i class="ti ti-plus"></i></span></td>
                <td style="width:104px"><span class="fw-bold font-monospace">${this.esc(t.workstream_id || 'NEW')}</span></td>
                <td><div class="tk-change"><span class="badge bg-azure-lt"><i class="ti ti-plus me-1"></i>new task</span> <span class="fw-medium">${this.esc(t.title)}</span></div>
                    ${t.rationale ? `<div class="text-secondary small mt-1">${this.esc(t.rationale)}</div>` : ''}</td>
                ${isPast ? '' : `<td class="text-end w-1"><span class="tk-rowctl btn-list">
                    <a href="#" class="btn btn-ghost-secondary btn-icon btn-sm" data-intdrop="${idx}" onclick="return false" aria-label="Drop"><i class="ti ti-x"></i></a>
                </span></td>`}
            </tr>`;

        const tableWrap = (rows) => `<div class="table-responsive mb-4"><table class="table table-vcenter mb-0"><tbody>${rows}</tbody></table></div>`;
        const safeRows = props.map((p, i) => (!this._needsEvidence(p) ? propRow(p, i) : '')).join('');
        const evRows = props.map((p, i) => (this._needsEvidence(p) ? propRow(p, i) : '')).join('');

        const safeSection = nSafe ? `<div class="d-flex align-items-center justify-content-between mb-2">
                <div class="d-flex align-items-center gap-2"><span class="status-dot bg-green"></span>
                    <span class="subheader text-uppercase fw-bold text-secondary">Safe to apply</span>
                    <span class="badge bg-green-lt">${nSafe} change${nSafe !== 1 ? 's' : ''}</span></div>
                <div class="text-secondary small d-none d-sm-block">status moves · date shifts · field updates</div>
            </div>${tableWrap(safeRows)}` : '';
        const evSection = nEv ? `<div class="d-flex align-items-center justify-content-between mb-2">
                <div class="d-flex align-items-center gap-2"><span class="status-dot bg-orange"></span>
                    <span class="subheader text-uppercase fw-bold text-secondary">Needs evidence</span>
                    <span class="badge bg-orange-lt">held by default</span></div>
                <div class="text-secondary small d-none d-sm-block">closes (→ Done) apply only with acceptance evidence</div>
            </div>${tableWrap(evRows)}` : '';
        const ntSection = nts.length ? `<div class="d-flex align-items-center gap-2 mb-2"><span class="status-dot bg-azure"></span>
                <span class="subheader text-uppercase fw-bold text-secondary">New task proposed</span></div>${tableWrap(nts.map((t, i) => ntRow(t, i)).join(''))}` : '';
        const heroSummary = it.summary ? `<div class="bg-primary-lt rounded-3 p-3 mb-4"><div class="d-flex gap-3">
                <span class="avatar bg-primary text-white rounded-3 flex-shrink-0"><i class="ti ti-robot fs-2"></i></span>
                <div class="flex-fill"><div class="subheader text-primary mb-1">Maxwell · agent summary</div>
                    <div class="markdown small">${this.md(it.summary)}</div></div></div></div>` : '';
        const safety = `<div class="bg-azure-lt rounded-3 p-3 d-flex gap-3">
                <span class="avatar avatar-sm bg-azure-lt text-azure rounded-3 flex-shrink-0"><i class="ti ti-info-circle"></i></span>
                <div class="small text-secondary">Nothing is applied until you confirm. <strong class="text-body">→ Done</strong> closes are held until you confirm them with evidence.</div></div>`;

        // read-only "past" view — what was applied when this item was confirmed/dismissed
        const appliedBanner = dismissed
            ? `<div class="bg-secondary-lt rounded-3 p-3 mb-4 small text-secondary"><i class="ti ti-x me-1"></i>Dismissed — no changes were applied.</div>`
            : ((apUpd.length || apCr.length)
                ? `<div class="bg-green-lt rounded-3 p-3 mb-4 d-flex gap-3"><span class="avatar avatar-sm bg-green-lt text-green rounded-3 flex-shrink-0"><i class="ti ti-checks"></i></span>
                    <div class="small text-secondary">${apUpd.length ? `Updated <strong class="text-body font-monospace">${apUpd.map((x) => this.esc(x)).join(', ')}</strong>` : ''}${(apUpd.length && apCr.length) ? ' · ' : ''}${apCr.length ? `Created <strong class="text-body font-monospace">${apCr.map((x) => this.esc(x)).join(', ')}</strong>` : ''}</div></div>`
                : `<div class="bg-secondary-lt rounded-3 p-3 mb-4 small text-secondary">Ingested for reference — no task changes.</div>`);
        const pcTone = dismissed ? 'secondary' : 'green';
        const pastChanges = props.length ? `<div class="d-flex align-items-center gap-2 mb-2"><span class="status-dot bg-${pcTone}"></span>
                <span class="subheader text-uppercase fw-bold text-secondary">${dismissed ? 'Proposed — dismissed' : 'Changes applied'}</span>
                <span class="badge bg-${pcTone}-lt">${props.length} change${props.length !== 1 ? 's' : ''}</span></div>${tableWrap(props.map((p, i) => propRow(p, i)).join(''))}` : '';
        const pastNew = nts.length ? `<div class="d-flex align-items-center gap-2 mb-2"><span class="status-dot bg-azure"></span>
                <span class="subheader text-uppercase fw-bold text-secondary">Tasks created</span></div>${tableWrap(nts.map((t, i) => ntRow(t, i)).join(''))}` : '';
        const bodyInner = isPast ? (heroSummary + appliedBanner + pastChanges + pastNew) : (heroSummary + safeSection + evSection + ntSection + safety);
        const statusBadge = isPast ? `<span class="badge bg-${dismissed ? 'secondary' : 'green'}-lt text-${dismissed ? 'secondary' : 'green'}"><i class="ti ti-${dismissed ? 'x' : 'checks'} me-1"></i>${dismissed ? 'Dismissed' : 'Confirmed'}</span>` : '';
        const topTone = isPast ? (dismissed ? 'secondary' : 'green') : (nEv ? 'orange' : 'primary');

        const root = document.getElementById('queue-modal-content');
        root.innerHTML = `<div class="card-status-top bg-${topTone}"></div>
            <div class="modal-header align-items-start">
                <span class="avatar avatar-lg bg-primary-lt text-primary rounded-3 me-3"><i class="ti ${sm.icon} fs-2"></i></span>
                <div class="flex-fill">
                    <div class="d-flex flex-wrap align-items-center gap-2 mb-1">
                        <span class="badge bg-primary text-white">QUEUE-${this.esc(String(it.id))}</span>
                        ${statusBadge}
                        <span class="badge bg-secondary-lt">${this.esc(sm.label)}</span>
                        <span class="badge bg-azure-lt"><i class="ti ti-robot me-1"></i>Triaged by Maxwell</span>
                        <span class="text-secondary small">${nAct} action${nAct !== 1 ? 's' : ''}${(!isPast && nEv) ? ` · <span class="text-orange">${nEv} needs evidence</span>` : ''}</span>
                    </div>
                    <h3 class="modal-title mb-1">${this.esc(it.subject || sm.label)}</h3>
                    <div class="text-secondary small"><i class="ti ${sm.icon} me-1"></i>${this.esc(sm.label)}<span class="mx-2">·</span><i class="ti ti-clock me-1"></i>${this.esc(when)}<span class="mx-2">·</span>${nAct} proposed change${nAct !== 1 ? 's' : ''}</div>
                </div>
                <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
            </div>
            <div class="modal-body">${bodyInner}</div>
            <div class="modal-footer">
                ${isPast
                    ? `<span class="small text-secondary me-auto"><i class="ti ti-clock me-1"></i>${this.esc(it.status)} · ${this.esc(when)}</span>
                <button type="button" class="btn btn-primary btn-pill px-4" data-bs-dismiss="modal">Close</button>`
                    : `<span class="small text-secondary me-auto" data-istatus></span>
                <a href="#" class="btn btn-link text-secondary px-2" data-idismiss onclick="return false"><i class="ti ti-trash me-1"></i>Dismiss</a>
                ${nEv ? '<button type="button" class="btn btn-outline-danger btn-pill px-3" data-iconfirm-all><i class="ti ti-lock-open me-1"></i>Confirm incl. closes</button>' : ''}
                <button type="button" class="btn btn-primary btn-pill px-4" data-iconfirm-safe><i class="ti ti-circle-check me-1"></i>Confirm safe</button>`}
            </div>`;

        if (!isPast) {
            root.querySelectorAll('[data-ipdrop]').forEach((b) => b.addEventListener('click', () => {
                const idx = parseInt(b.getAttribute('data-ipdrop'), 10); props[idx] = null;
                const r = root.querySelector(`[data-iprow="${idx}"]`); if (r) r.remove();
            }));
            root.querySelectorAll('[data-intdrop]').forEach((b) => b.addEventListener('click', () => {
                const idx = parseInt(b.getAttribute('data-intdrop'), 10); nts[idx] = null;
                const r = root.querySelector(`[data-introw="${idx}"]`); if (r) r.remove();
            }));
            root.querySelectorAll('[data-pedit]').forEach((b) => b.addEventListener('click', () =>
                this._togglePropEditor(root, parseInt(b.getAttribute('data-pedit'), 10), props)));
            const confirm = (includeCloses) => {
                const live = props.filter(Boolean);
                const apply = includeCloses ? live : live.filter((p) => !this._needsEvidence(p));
                const keep = includeCloses ? [] : live.filter((p) => this._needsEvidence(p));
                this.confirmInbox(it.id, apply, nts.filter(Boolean), keep, root);
            };
            const cs = root.querySelector('[data-iconfirm-safe]'); if (cs) cs.addEventListener('click', () => confirm(false));
            const ca = root.querySelector('[data-iconfirm-all]'); if (ca) ca.addEventListener('click', () => confirm(true));
            root.querySelector('[data-idismiss]').addEventListener('click', () => this.dismissInbox(it.id));
        }
        window.bootstrap.Modal.getOrCreateInstance(document.getElementById('queue-modal')).show();
    },

    _PROP_STATUSES: ['', 'Not Started', 'In Progress', 'In Review', 'Blocked', 'Done'],

    // Inline edit of a queued proposal — mutates the in-memory working copy `props[idx]`; the
    // edited values are what get sent on Confirm (backend applies whatever fields are present).
    _togglePropEditor(card, idx, props) {
        const box = card.querySelector(`[data-peditor="${idx}"]`);
        if (!box) return;
        if (!box.classList.contains('d-none')) { box.classList.add('d-none'); box.innerHTML = ''; return; }
        const p = props[idx] || {};
        const opts = this._PROP_STATUSES.map((s) => `<option value="${s}"${(p.status || '') === s ? ' selected' : ''}>${s || '— status —'}</option>`).join('');
        box.innerHTML = `<div class="row g-2">
            <div class="col-12 col-md-3"><select class="form-select form-select-sm" data-ef="status">${opts}</select></div>
            <div class="col-6 col-md-3"><input class="form-control form-control-sm" type="date" data-ef="start_date" value="${this.esc(p.start_date || '')}" title="start"></div>
            <div class="col-6 col-md-3"><input class="form-control form-control-sm" type="date" data-ef="finish_date" value="${this.esc(p.finish_date || '')}" title="finish"></div>
            <div class="col-12 col-md-3"><input class="form-control form-control-sm" data-ef="assignee" value="${this.esc(p.assignee || '')}" placeholder="owner"></div>
            <div class="col-12"><input class="form-control form-control-sm" data-ef="title" value="${this.esc(p.title || '')}" placeholder="title (optional)"></div>
            <div class="col-12"><input class="form-control form-control-sm" data-ef="rationale" value="${this.esc(p.rationale || '')}" placeholder="rationale (optional)"></div>
            <div class="col-12"><button class="btn btn-sm btn-primary" data-esave><i class="ti ti-check me-1"></i>Save</button>
                <button class="btn btn-sm" data-ecancel>Cancel</button></div>
        </div>`;
        box.classList.remove('d-none');
        box.querySelector('[data-ecancel]').addEventListener('click', () => { box.classList.add('d-none'); box.innerHTML = ''; });
        box.querySelector('[data-esave]').addEventListener('click', () => {
            const tid = props[idx].task_id;
            box.querySelectorAll('[data-ef]').forEach((inp) => {
                const k = inp.getAttribute('data-ef'); const v = (inp.value || '').trim();
                if (v) props[idx][k] = v; else delete props[idx][k];
            });
            props[idx].task_id = tid;
            const chips = card.querySelector(`[data-pchips="${idx}"]`); if (chips) chips.innerHTML = this._propChips(props[idx]);
            const dot = card.querySelector(`[data-iprow="${idx}"] .status-dot`);
            if (dot) dot.className = `status-dot bg-${this._needsEvidence(props[idx]) ? 'orange' : 'green'} flex-shrink-0`;
            box.classList.add('d-none'); box.innerHTML = '';
        });
    },

    async confirmInbox(id, proposals, new_tasks, keep, card) {
        const st = card ? card.querySelector('[data-istatus]') : null;
        if (st) st.textContent = 'Applying…';
        try {
            const res = await fetch(`api/inbox/${id}/confirm`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ proposals, new_tasks, keep_proposals: keep || [] }),
            });
            const data = await res.json();
            const a = data.applied || {};
            const held = data.remaining || 0;
            const n = (a.updated || []).length + (a.created || []).length;
            this._queueFlash = `Applied ${n} change${n !== 1 ? 's' : ''}${held ? ` · ${held} close(s) held for evidence` : ''}.`;
            this._hideQueueModal();
            await this._reloadBoardData();
            this.initInbox();
        } catch (e) { if (st) st.textContent = 'failed: ' + e.message; }
    },

    async confirmAll(safeOnly) {
        try {
            const res = await fetch('api/inbox/confirm_all', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ safe_only: !!safeOnly }),
            });
            const d = await res.json();
            this._queueFlash = `Applied ${d.updated || 0} update(s) + ${d.created || 0} new task(s) across ${d.items || 0} item(s)${d.held ? ` · ${d.held} close(s) held for evidence` : ''}.`;
            await this._reloadBoardData();
            this.initInbox();
        } catch (e) { /* noop */ }
    },

    async dismissInbox(id) {
        try { await fetch(`api/inbox/${id}/dismiss`, { method: 'POST' }); this._hideQueueModal(); this.initInbox(); } catch (e) { /* noop */ }
    },

    async simulateInbox() {
        const subject = (document.getElementById('inbox-sim-subject').value || '').trim();
        const text = (document.getElementById('inbox-sim-text').value || '').trim();
        const flash = document.getElementById('inbox-sim-flash');
        if (!text) { if (flash) flash.textContent = 'Paste some email text.'; return; }
        if (flash) flash.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>triaging…';
        try {
            await fetch('api/inbox/simulate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ subject, text }) });
            if (flash) flash.textContent = 'queued';
            document.getElementById('inbox-sim-text').value = '';
            this.initInbox();
        } catch (e) { if (flash) flash.textContent = 'failed: ' + e.message; }
    },

    async _reloadBoardData() {
        try {
            this.plan = await (await fetch('api/board')).json();
            this.flatten();
            this.renderBoard();
            this.renderTasks();
            this.loadSignals();
        } catch (e) { /* noop */ }
    },

    // ---- tables (milestones / critical path / risks / decisions) ---------
    table(headers, rows) {
        return `<div class="table-responsive"><table class="table table-vcenter card-table">
            <thead><tr>${headers.map((h) => `<th>${this.esc(h)}</th>`).join('')}</tr></thead>
            <tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody>
        </table></div>`;
    },

    renderTables() {
        // Milestones
        document.getElementById('milestones-table').innerHTML = this.table(
            ['Milestone', 'Target', 'Gate criteria'],
            (this.plan.milestones || []).map((m) => [this.esc(m.name), this.badge(m.target_week, 'azure'), this.esc(m.gate_criteria)])
        );
        // Critical path
        document.getElementById('path-table').innerHTML = this.table(
            ['#', 'Task', 'Workstream', 'Why on the critical path'],
            (this.plan.critical_path || []).map((c, i) => [
                String(i + 1),
                `<code>${this.esc(c.task_id)}</code>`,
                this.badge(c.workstream, this.WS_COLOR[c.workstream] || 'secondary'),
                this.esc(c.why)
            ])
        );
        // Risks
        const rl = (x) => this.badge(x, this.RISK_COLOR[x] || 'secondary');
        document.getElementById('risks-table').innerHTML = this.table(
            ['Risk', 'L', 'I', 'Mitigation', 'Owner', 'Workstream'],
            (this.plan.consolidated_risks || []).map((r) => [
                this.esc(r.risk), rl(r.likelihood), rl(r.impact), this.esc(r.mitigation),
                this.esc(r.owner), this.esc(r.workstream)
            ])
        );
        // Decisions
        document.getElementById('decisions-table').innerHTML = this.table(
            ['Question', 'Owner', 'Recommended default', 'Needed by', 'Workstream'],
            (this.plan.consolidated_decisions || []).map((d) => [
                this.esc(d.question), this.esc(d.owner), this.esc(d.recommended_default),
                this.badge(d.needed_by, 'orange'), this.esc(d.workstream)
            ])
        );
    },

    // ---- exec summary (A-exec: "My work" + Inbox + Pulse) ---------------
    // Is this task mine? (owned-by / assigned-to Steve Ridder / SR)
    _isMine(t) {
        const hay = `${t.assignee || ''} ${t.owner_person_or_role || ''}`.toLowerCase();
        return hay.includes('steve ridder') || /\bsr\b/.test(hay);
    },

    renderExec() {
        const el = document.getElementById('exec-content');
        if (!el) return;
        this._ensureQueueModal();
        const r = this.plan.rollups || {};
        const t = this.tasks;
        const mine = t.filter((x) => this._isMine(x));
        const blocking = t.filter((x) => x.is_blocking).length;
        const qitems = this.inboxItems || [];
        const qActive = qitems.filter((it) => it.status === 'pending');
        const qHist = qitems.filter((it) => it.status && it.status !== 'pending').slice(0, 8);
        const inboxN = qActive.length;
        const all = this.filtered();
        const tt = (this.tally && this.tally.totals) || {};
        const spend = tt.spend || {};
        const unit = tt.unit_cost || {};

        // --- KPI strip ----------------------------------------------------
        const kpi = (label, value, sub, red) => `
            <div class="col-6 col-lg">
                <div class="card"><div class="card-body p-3">
                    <div class="subheader">${this.esc(label)}</div>
                    <div class="h1 mb-0 mt-1${red ? ' text-red' : ''}">${this.esc(value)}</div>
                    <div class="text-secondary small">${this.esc(sub)}</div>
                </div></div>
            </div>`;
        const kpiStrip = `<div class="row row-cards mb-3">
            ${kpi('Workstreams', r.total_workstreams, 'across the plan')}
            ${kpi('Tasks', r.total_tasks, (r.total_effort_days != null ? r.total_effort_days + ' effort-days' : ''))}
            ${kpi('My open work', mine.length, 'SR · Taikun')}
            ${kpi('Blocking', blocking, 'gating other work', true)}
            ${kpi('Spend', this.money(spend.cost_usd || 0), `${this.compact(tt.verified_outcomes || 0)} verified outcomes`)}
            ${kpi('Cost / outcome', this.money(unit.cost_per_verified_outcome), 'verified denominator')}
            ${kpi('Inbox to triage', inboxN, inboxN ? 'awaiting confirm' : 'all clear')}
        </div>`;

        // --- LEFT: my work, grouped by phase (blocking-first) -------------
        const rank = (x) => (x.is_blocking ? 0 : 1);
        // Overview = remaining work; never list completed tasks here regardless of
        // the Hide-done toggle (Done lives on the Board / Tasks tabs).
        const openWork = all.filter((x) => x.status !== 'Done');
        const groups = this.PHASES.map((phase) => {
            const list = openWork.filter((x) => x.phase === phase)
                .sort((a, b) => rank(a) - rank(b) ||
                    ((a.finish_date || '9999') < (b.finish_date || '9999') ? -1 : 1));
            return { phase, list };
        }).filter((g) => g.list.length);

        const row = (x) => {
            const sc = this.STATUS_COLOR[x.status] || 'secondary';
            const live = x.status === 'In Progress';
            const due = this.fmtDue(x.finish_date, x.status === 'Done');
            const who = x.assignee || x.owner_person_or_role || '';
            const av = who
                ? `<span class="avatar avatar-xs rounded-circle bg-secondary-lt" title="${this.esc(who)}">${this.esc(this.initials(who))}</span>`
                : '<span class="text-secondary small">—</span>';
            return `
                <tr data-task="${this.esc(x.task_id)}" style="cursor:pointer">
                    <td class="w-1"><span class="status-dot${live ? ' status-dot-animated' : ''} bg-${sc}" title="${this.esc(x.status || '')}"></span></td>
                    <td>
                        <div class="fw-semibold text-body">${this.esc(x.title)}</div>
                        ${x.description ? `<div class="text-secondary small">${this.esc(x.description)}</div>` : ''}
                    </td>
                    <td><span class="text-secondary small text-uppercase">${this.esc(x._wsId)}</span></td>
                    <td>${av}</td>
                    <td>${x.risk_level === 'High' ? '<span class="badge badge-outline text-red">High</span>' : '<span class="text-secondary small">—</span>'}</td>
                    <td>${x.is_blocking ? '<span class="d-inline-flex align-items-center"><span class="status-dot bg-red me-1"></span><span class="text-secondary small">Blocking</span></span>' : '<span class="text-secondary small">—</span>'}</td>
                    <td class="text-secondary small text-nowrap ${due.cls}">${due.text || '—'}</td>
                </tr>`;
        };

        const groupHtml = groups.map((g) => `
            <div class="card-body bg-light py-2 border-bottom border-top">
                <div class="row align-items-center">
                    <div class="col"><span class="subheader text-body">${this.esc(g.phase)}</span></div>
                    <div class="col-auto text-secondary small">${g.list.length} task${g.list.length > 1 ? 's' : ''}</div>
                </div>
            </div>
            <div class="table-responsive">
                <table class="table table-vcenter table-borderless mb-0">
                    <tbody>${g.list.map(row).join('')}</tbody>
                </table>
            </div>`).join('');

        const myWork = `
            <div class="col-lg-8">
                <div class="card">
                    <div class="card-header">
                        <h3 class="card-title">Work, grouped by phase</h3>
                        <div class="card-actions d-flex align-items-center">
                            <span class="text-secondary small me-3">Sorted: blocking first</span>
                            <span class="badge bg-secondary-lt">${openWork.length} task${openWork.length === 1 ? '' : 's'}</span>
                        </div>
                    </div>
                    ${groups.length ? groupHtml : `
                    <div class="empty">
                        <div class="empty-icon"><i class="ti ti-check"></i></div>
                        <p class="empty-title">Nothing assigned to you</p>
                        <p class="empty-subtitle text-secondary">Open the Board to see all ${this.esc(r.total_tasks)} tasks.</p>
                    </div>`}
                    <div class="card-footer text-secondary small">
                        Grouped by phase · <a href="#tab-board" data-bs-toggle="tab" class="text-reset fw-bold">open the board</a> for the kanban
                    </div>
                </div>
            </div>`;

        // --- RIGHT: Action Queue (active + historical) + Latest Pulse ----
        const execQRow = (it) => {
            const sm = this._srcMeta(it.source);
            const tri = it.triage || {};
            const past = !!(it.status && it.status !== 'pending');
            const isDis = it.status === 'dismissed';
            const nA = (tri.proposals || []).length + (tri.new_tasks || []).length;
            const a2 = tri.applied || {}; const done = (a2.updated || []).length + (a2.created || []).length;
            const nEv2 = (tri.proposals || []).filter((p) => this._needsEvidence(p)).length;
            const dot = past ? (isDis ? 'secondary' : 'green') : (nEv2 ? 'orange' : 'red');
            const right = past
                ? `<span class="badge bg-${isDis ? 'secondary' : 'green'}-lt">${this.esc(it.status)}</span>${done ? ` <span class="text-secondary small">${done} change${done !== 1 ? 's' : ''}</span>` : ''}`
                : `<span class="badge bg-secondary-lt">${nA} action${nA !== 1 ? 's' : ''}</span>${nEv2 ? ` <span class="text-orange small">· ${nEv2} evidence</span>` : ''}`;
            return `<div class="list-group-item list-group-item-action py-2" data-qopen="${it.id}" style="cursor:pointer">
                    <div class="row align-items-center g-2">
                        <div class="col-auto"><span class="status-dot bg-${dot}"></span></div>
                        <div class="col text-truncate">
                            <div class="fw-semibold text-body text-truncate">${this.esc(it.subject || sm.label)}</div>
                            <div class="text-secondary small"><i class="ti ${sm.icon} me-1"></i>${this.esc(sm.label)} · ${this._relAge(it.received_at)} ago</div>
                        </div>
                        <div class="col-auto text-end">${right}</div>
                    </div>
                </div>`;
        };
        const qSection = (title, items) => items.length
            ? `<div class="card-body py-1 px-3 bg-light border-top"><span class="subheader text-secondary">${title}</span></div>
               <div class="list-group list-group-flush">${items.map(execQRow).join('')}</div>` : '';
        const inboxBody = (qActive.length || qHist.length)
            ? `${qSection('Active', qActive)}${qSection('History', qHist)}
               <div class="card-footer text-center py-2"><a href="#tab-inbox" data-bs-toggle="tab" class="text-reset small fw-bold">Open the Action Queue <i class="ti ti-arrow-right ms-1"></i></a></div>`
            : `<div class="empty py-4">
                   <div class="empty-icon"><i class="ti ti-inbox"></i></div>
                   <p class="empty-title">Queue is clear</p>
                   <p class="empty-subtitle text-secondary">Forward an email or transcript and the agent triages it here.</p>
                   <div class="empty-action"><a href="#tab-inbox" data-bs-toggle="tab" class="btn btn-outline-secondary"><i class="ti ti-checklist me-1"></i>Open the Action Queue</a></div>
               </div>`;

        const rightRail = `
            <div class="col-lg-4">
                <div class="card mb-3">
                    <div class="card-header">
                        <h3 class="card-title"><i class="ti ti-cloud-upload me-2"></i>Add to corpus</h3>
                        <div class="card-actions"><span class="badge bg-primary-lt">RAG · agent reacts</span></div>
                    </div>
                    <div class="card-body">
                        <div id="exec-drop" class="border border-2 border-dashed rounded p-3 text-center" style="cursor:pointer">
                            <div class="text-secondary"><i class="ti ti-file-upload" style="font-size:1.5rem"></i></div>
                            <div class="fw-semibold mt-1">Drop a doc, email, transcript or media file</div>
                            <div class="text-secondary small">or <span class="text-primary">browse</span> — audio/video is transcribed, then the agent ingests it and tells you what changes</div>
                            <input id="exec-file" type="file" accept=".txt,.md,.vtt,.eml,.csv,.json,.pdf,.docx,.pptx,.m4a,.mp3,.mp4,.wav,.webm,.mov,.m4v,.aac,.ogg,.flac,audio/*,video/*" class="d-none" multiple>
                        </div>
                        <textarea id="exec-paste" class="form-control form-control-sm mt-2" rows="2" placeholder="…or paste text"></textarea>
                        <div class="mt-2 d-flex align-items-center">
                            <button id="exec-ingest" class="btn btn-primary btn-sm"><i class="ti ti-sparkles me-1"></i>Ingest &amp; react</button>
                            <span id="exec-upload-flash" class="small text-secondary ms-2"></span>
                        </div>
                        <div id="exec-upload-result" class="mt-2"></div>
                    </div>
                </div>

                <div class="card mb-3">
                    <div class="card-header">
                        <h3 class="card-title"><i class="ti ti-checklist me-2"></i>Action Queue</h3>
                        <div class="card-actions"><span class="badge bg-${inboxN ? 'red' : 'secondary'}-lt">${inboxN} active</span></div>
                    </div>
                    ${inboxBody}
                </div>

                <div class="card">
                    <div class="card-status-top bg-primary"></div>
                    <div class="card-header">
                        <span class="avatar avatar-sm rounded bg-primary-lt text-primary me-2"><i class="ti ti-broadcast"></i></span>
                        <div>
                            <h3 class="card-title mb-0">Latest Pulse</h3>
                            <div class="text-secondary small">Weekly chief-of-staff digest</div>
                        </div>
                    </div>
                    <div class="card-body">
                        <div class="empty py-4">
                            <div class="empty-icon"><i class="ti ti-activity-heartbeat"></i></div>
                            <p class="empty-title">No digest yet</p>
                            <p class="empty-subtitle text-secondary">Generate a chief-of-staff brief: what changed, what's slipping, and what to pick up next.</p>
                            <div class="empty-action"><a href="#tab-pulse" data-bs-toggle="tab" class="btn btn-primary"><i class="ti ti-sparkles me-1"></i>Generate digest</a></div>
                        </div>
                    </div>
                </div>
            </div>`;

        el.innerHTML = kpiStrip + `<div class="row row-cards">${myWork}${rightRail}</div>`;

        // Open the task modal from a clicked row. wireEvents() does not delegate
        // exec-content, so bind once here (re-renders reuse the same element).
        if (!this._execWired) {
            this._execWired = true;
            el.addEventListener('click', (e) => {
                if (e.target.closest('#exec-ingest')) { e.preventDefault(); const ta = document.getElementById('exec-paste'); this.submitExecUpload((ta && ta.value) || '', 'Pasted note'); return; }
                if (e.target.closest('#exec-drop')) { const fi = document.getElementById('exec-file'); if (fi) fi.click(); return; }
                const q = e.target.closest('[data-qopen]');
                if (q && el.contains(q)) { this.openQueueItem(q.getAttribute('data-qopen')); return; }
                const trg = e.target.closest('[data-task]');
                if (!trg || !el.contains(trg)) return;
                this.openTask(trg.getAttribute('data-task'));
            });
            el.addEventListener('change', (e) => {
                if (e.target && e.target.id === 'exec-file' && e.target.files && e.target.files.length) this._readFilesThenIngest(e.target.files);
            });
            el.addEventListener('dragover', (e) => { const z = e.target.closest('#exec-drop'); if (z) { e.preventDefault(); z.classList.add('bg-primary-lt', 'border-primary'); } });
            el.addEventListener('dragleave', (e) => { const z = e.target.closest('#exec-drop'); if (z) z.classList.remove('bg-primary-lt', 'border-primary'); });
            el.addEventListener('drop', (e) => { const z = e.target.closest('#exec-drop'); if (z) { e.preventDefault(); z.classList.remove('bg-primary-lt', 'border-primary'); if (e.dataTransfer && e.dataTransfer.files.length) this._readFilesThenIngest(e.dataTransfer.files); } });
        }
    },

    exportUrl(kind) {
        const p = new URLSearchParams();
        const set = (id, key) => { const v = (document.getElementById(id).value || '').trim(); if (v) p.set(key, v); };
        set('f-ws', 'workstream'); set('f-owner', 'owner'); set('f-assignee', 'person'); set('f-risk', 'risk'); set('f-search', 'q');
        if (document.getElementById('f-blocking').checked) p.set('blocking', '1');
        p.set('project', window.PM_PROJECT || 'maxwell');
        const qs = p.toString();
        return `api/export.${kind}` + (qs ? `?${qs}` : '');
    },

    // ---- events ----------------------------------------------------------
    wireEvents() {
        ['f-search', 'f-ws', 'f-owner', 'f-assignee', 'f-risk', 'f-blocking', 'f-hidedone'].forEach((id) => {
            const el = document.getElementById(id);
            const ev = (id === 'f-search') ? 'input' : 'change';
            el.addEventListener(ev, () => { this.renderExec(); this.renderBoard(); this.renderTasks(); this.renderEpics(); if (this.isGanttVisible()) this.renderGantt(); });
        });
        document.getElementById('board').addEventListener('click', (e) => {
            const a = e.target.closest('a[data-task]');
            if (!a) return;
            e.preventDefault();
            this.openTask(a.getAttribute('data-task'));
        });
        const tc = document.getElementById('tasks-content');
        if (tc) {
            tc.addEventListener('change', (e) => {
                const cb = e.target.closest('input[data-check]');
                if (cb) this.toggleDone(cb.getAttribute('data-check'), cb.checked);
            });
            tc.addEventListener('click', (e) => {
                const a = e.target.closest('a[data-task]');
                if (!a) return;
                e.preventDefault();
                this.openTask(a.getAttribute('data-task'));
            });
        }
        const tt = document.querySelector('a[href="#tab-tasks"]');
        if (tt) tt.addEventListener('shown.bs.tab', () => this.renderTasks());
        const ec = document.getElementById('epics-content');
        if (ec) {
            ec.addEventListener('click', (e) => {
                const a = e.target.closest('a[data-task]');
                if (!a) return;
                e.preventDefault();
                this.openTask(a.getAttribute('data-task'));
            });
            ec.addEventListener('change', async (e) => {
                const cb = e.target.closest('input[data-check]');
                if (cb) { await this.toggleDone(cb.getAttribute('data-check'), cb.checked); this.renderEpics(); }
            });
        }
        const epicsTab = document.querySelector('a[href="#tab-epics"]');
        if (epicsTab) epicsTab.addEventListener('shown.bs.tab', () => this.renderEpics());
        ['xlsx', 'xml'].forEach((kind) => {
            const btn = document.getElementById('dl-' + kind);
            if (btn) btn.addEventListener('click', (e) => { e.preventDefault(); window.location.href = this.exportUrl(kind); });
        });
        const nb = document.getElementById('btn-new-task');
        if (nb) nb.addEventListener('click', () => this.openCreate());
        // Ask Taikun (plan-wide chat)
        const askSend = document.getElementById('ask-send');
        if (askSend) askSend.addEventListener('click', () => this.sendAsk());
        const askInput = document.getElementById('ask-input');
        if (askInput) askInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') this.sendAsk(); });
        const askTab = document.querySelector('a[href="#tab-ask"]');
        if (askTab) askTab.addEventListener('shown.bs.tab', () => this.initAsk());
        const askClear = document.getElementById('ask-clear');
        if (askClear) askClear.addEventListener('click', () => this.clearAsk());
        const intakeGo = document.getElementById('intake-go');
        if (intakeGo) intakeGo.addEventListener('click', () => this.submitIntake());
        const intakeFile = document.getElementById('intake-file');
        if (intakeFile) intakeFile.addEventListener('change', (e) => {
            const f = e.target.files[0];
            if (!f) return;
            if (/\.(m4a|mp3|mp4|wav|webm|mov|m4v|aac|ogg|oga|flac|mpeg|mpga|amr|pdf|docx|pptx)$/i.test(f.name)) {
                this.submitIntakeUpload(f);
                e.target.value = '';
                return;
            }
            const r = new FileReader();
            r.onload = () => {
                document.getElementById('intake-text').value = r.result || '';
                const ti = document.getElementById('intake-title');
                if (ti && !ti.value) ti.value = f.name;
            };
            r.readAsText(f);
        });
        const tasksTab = document.querySelector('a[href="#tab-tasks"]');
        if (tasksTab) tasksTab.addEventListener('shown.bs.tab', () => this.loadSignals());
        const digestGen = document.getElementById('digest-gen');
        if (digestGen) digestGen.addEventListener('click', () => this.genDigest());
        const pulseTab = document.querySelector('a[href="#tab-pulse"]');
        if (pulseTab) pulseTab.addEventListener('shown.bs.tab', () => this.initPulse());
        const inboxTab = document.querySelector('a[href="#tab-inbox"]');
        if (inboxTab) inboxTab.addEventListener('shown.bs.tab', () => this.initInbox());
        const inboxRefresh = document.getElementById('inbox-refresh');
        if (inboxRefresh) inboxRefresh.addEventListener('click', () => this.initInbox());
        const inboxSim = document.getElementById('inbox-sim');
        if (inboxSim) inboxSim.addEventListener('click', () => { const box = document.getElementById('inbox-sim-box'); if (window.bootstrap) window.bootstrap.Collapse.getOrCreateInstance(box).toggle(); });
        const inboxSimGo = document.getElementById('inbox-sim-go');
        if (inboxSimGo) inboxSimGo.addEventListener('click', () => this.simulateInbox());
    },
};

document.addEventListener('DOMContentLoaded', () => TeepPlan.init());
