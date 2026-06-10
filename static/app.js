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
    wsMeta: {},         // workstream_id -> {name, lead_org}
    gantt: null,        // ApexCharts instance
    ganttMode: 'task',  // default 'task' (per-task detail) · 'workstream' = 12-bar overview

    PHASES: ['Kickoff', 'Bootstrap', 'Build', 'Cutover', 'Operate'],
    PHASE_COLOR: { Kickoff: 'azure', Bootstrap: 'purple', Build: 'blue', Cutover: 'orange', Operate: 'green' },
    PHASE_HEX: { Kickoff: '#4299e1', Bootstrap: '#ae3ec9', Build: '#066fd1', Cutover: '#f76707', Operate: '#2fb344' },
    OWNER_COLOR: { 'Taikun': 'blue', 'TEEP': 'teal', 'Sensirion/Nubo': 'orange', 'IFS Merrick': 'purple', 'Joint': 'cyan' },
    RISK_COLOR: { Low: 'green', Medium: 'yellow', High: 'red' },
    STATUS_COLOR: { 'Not Started': 'secondary', 'In Progress': 'blue', 'Blocked': 'red', 'Done': 'green' },
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
        { k: 'status', label: 'Status', type: 'select', opts: ['Not Started', 'In Progress', 'Blocked', 'Done'], col: 'col-6 col-md-3' },
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
        try {
            const res = await fetch('api/board');
            if (!res.ok) throw new Error(`HTTP ${res.status} loading the board`);
            this.plan = await res.json();
            try { this.people = (await (await fetch('api/people')).json()).people || []; } catch (e) { this.people = []; }
        } catch (err) {
            this.showError(err.message);
            return;
        }
        this.flatten();
        this.renderGenerated();
        this.renderStats();
        this.renderAbout();
        this.buildFilters();
        const dl = document.getElementById('people-list');
        if (dl) dl.innerHTML = (this.people || []).map((p) => `<option value="${this.esc(p)}"></option>`).join('');
        this.renderBoard();
        this.renderTables();
        this.renderExec();
        this.wireEvents();
        this.setupGantt();
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

    // ---- small helpers ---------------------------------------------------
    esc(s) {
        if (s === null || s === undefined) return '';
        return String(s).replace(/[&<>"']/g, (c) => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
        ));
    },
    badge(text, color, light) {
        const cls = light === false ? `bg-${color}` : `bg-${color}-lt`;
        return `<span class="badge ${cls}">${this.esc(text)}</span>`;
    },
    initials(name) {
        if (!name) return '';
        return String(name).trim().split(/\s+/).slice(0, 2)
            .map((p) => (p[0] || '').toUpperCase()).join('');
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
        document.getElementById('f-owner').innerHTML = `<option value="">All owners</option>` +
            owners.map((o) => `<option value="${this.esc(o)}">${this.esc(o)}</option>`).join('');
        this.refreshAssignees();
        document.getElementById('f-risk').innerHTML = `<option value="">All risk</option>` +
            ['High', 'Medium', 'Low'].map((x) => `<option value="${x}">${x} risk</option>`).join('');
    },

    refreshAssignees() {
        const sel = document.getElementById('f-assignee');
        if (!sel) return;
        const cur = sel.value;
        const names = [...new Set([...this.tasks.map((t) => t.assignee).filter(Boolean), ...(this.people || [])])].sort();
        sel.innerHTML = `<option value="">All users</option>` +
            names.map((a) => `<option value="${this.esc(a)}"${a === cur ? ' selected' : ''}>${this.esc(a)}</option>`).join('');
    },

    filtered() {
        const q = (document.getElementById('f-search').value || '').trim().toLowerCase();
        const ws = document.getElementById('f-ws').value;
        const owner = document.getElementById('f-owner').value;
        const assignee = document.getElementById('f-assignee').value;
        const risk = document.getElementById('f-risk').value;
        const blocking = document.getElementById('f-blocking').checked;
        return this.tasks.filter((t) => {
            if (ws && t._wsId !== ws) return false;
            if (owner && t.owner_org !== owner) return false;
            if (assignee && t.assignee !== assignee) return false;
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
                        <span class="subheader">${this.esc(phase)}</span>
                        <span class="badge bg-secondary-lt ms-2">${col.length}</span>
                        <span class="ms-auto text-secondary small">${Math.round(days)}d</span>
                    </div>
                    <div>${cards}</div>
                </div>`;
        }).join('');
    },

    taskCard(t) {
        const wc = this.WS_COLOR[t._wsId] || 'secondary';
        const sc = this.STATUS_COLOR[t.status] || 'secondary';
        const deps = (t.depends_on || []).length;
        const meta = [];
        if (t.owner_org) meta.push(this.esc(t.owner_org));
        if (t.effort_days != null) meta.push(this.esc(t.effort_days) + 'd');
        if (deps) meta.push(`<i class="ti ti-link"></i>${deps}`);
        return `
            <a href="#" class="d-block text-reset" data-task="${this.esc(t.task_id)}">
                <div class="card card-sm mb-2">
                    <div class="card-status-start bg-${wc}"></div>
                    <div class="card-body">
                        <div class="d-flex align-items-center gap-2 mb-1">
                            <span class="status-dot bg-${sc}" title="${this.esc(t.status || '')}"></span>
                            <span class="text-secondary small fw-medium text-uppercase">${this.esc(t._wsId)}</span>
                            <span class="ms-auto text-secondary small font-monospace">${this.esc(t.task_id)}</span>
                        </div>
                        <div class="fw-semibold lh-sm text-body">${this.esc(t.title)}</div>
                        <div class="d-flex align-items-center gap-2 mt-2 text-secondary small">
                            <span>${meta.join(' · ')}</span>
                            ${t.risk_level === 'High' ? '<span class="badge badge-outline text-red">High risk</span>' : ''}
                            ${t.is_blocking ? '<span class="text-red lh-1" title="Blocking"><i class="ti ti-alert-triangle-filled"></i></span>' : ''}
                            ${t.assignee ? `<span class="avatar avatar-xs ms-auto" title="${this.esc(t.assignee)}">${this.esc(this.initials(t.assignee))}</span>` : ''}
                        </div>
                    </div>
                </div>
            </a>`;
    },

    // ---- Gantt (ApexCharts rangeBar) ------------------------------------
    isGanttVisible() {
        const p = document.getElementById('tab-gantt');
        return !!p && p.classList.contains('active');
    },

    setupGantt() {
        const note = document.getElementById('gantt-note');
        if (note) note.textContent = (this.plan.schedule_note || '') + ' Tip: switch By workstream / By task; click a bar to drill in.';
        const tab = document.querySelector('a[href="#tab-gantt"]');
        if (tab) tab.addEventListener('shown.bs.tab', () => this.renderGantt());
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
            plotOptions: { bar: { horizontal: true, borderRadius: 2, barHeight: this.ganttMode === 'workstream' ? '60%' : '72%' } },
            dataLabels: { enabled: false },
            xaxis: { type: 'datetime' },
            yaxis: { labels: { style: { fontSize: '11px' } } },
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
        const wc = this.WS_COLOR[t._wsId] || 'secondary';
        const oc = this.OWNER_COLOR[t.owner_org] || 'secondary';
        const rc = this.RISK_COLOR[t.risk_level] || 'secondary';
        const pc = this.PHASE_COLOR[t.phase] || 'secondary';
        const deps = (t.depends_on || []).map((d) => `<span class="badge bg-secondary-lt me-1">${this.esc(d)}</span>`).join('') || '<span class="text-secondary">none</span>';
        document.getElementById('task-modal-title').innerHTML =
            `<span class="me-2">${this.esc(t.task_id)}</span>${this.esc(t.title)}`;
        document.getElementById('task-modal-body').innerHTML = `
            <div class="d-flex flex-wrap align-items-center gap-2 mb-2">
                ${this.badge(t._wsId, wc)}<span class="text-secondary small">${this.esc(t._wsName)}</span>
                <span class="ms-auto small text-secondary">depends on: ${deps}</span>
            </div>
            <div class="card mb-3"><div class="card-body">
                ${this._taskFormHtml(t, 'edit-')}
                <div class="d-flex align-items-center gap-2 mt-3">
                    <button id="edit-save" class="btn btn-primary btn-sm"><i class="ti ti-device-floppy me-1"></i>Save</button>
                    <button id="edit-delete" class="btn btn-outline-danger btn-sm"><i class="ti ti-trash me-1"></i>Delete</button>
                    <span id="edit-flash" class="small text-secondary"></span>
                </div>
            </div></div>
            <div class="d-flex align-items-center mb-1"><strong>Ask Taikun · this task</strong>
                <span class="badge bg-green-lt ms-2">RAG over plan docs · propose-to-confirm</span></div>
            <div id="chat-log" class="border rounded p-2 mb-2"></div>
            <div class="input-group input-group-sm">
                <input id="chat-input" class="form-control" placeholder="Ask how to push this task ahead…" autocomplete="off"/>
                <button id="chat-send" class="btn btn-primary"><i class="ti ti-send"></i></button>
            </div>`;
        this._renderActivity(t);
        document.getElementById('edit-delete').addEventListener('click', () => this.deleteTask(t.task_id));
        document.getElementById('edit-save').addEventListener('click', () => this.saveTask(t.task_id));
        document.getElementById('chat-send').addEventListener('click', () => this.sendChat(t.task_id));
        document.getElementById('chat-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') this.sendChat(t.task_id); });
        window.bootstrap.Modal.getOrCreateInstance(document.getElementById('task-modal')).show();
    },

    _renderActivity(t) {
        const log = document.getElementById('chat-log');
        if (!log) return;
        const acts = (t.activity || []).filter((a) => a.kind === 'comment' || a.kind === 'chat');
        log.innerHTML = acts.length
            ? acts.map((a) => `<div class="mb-1"><span class="badge bg-secondary-lt me-1">${this.esc(a.actor)}</span>${this.esc((a.payload && a.payload.text) || '')}</div>`).join('')
            : '<div class="text-secondary small">No messages yet — ask the agent how to move this task forward.</div>';
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
            if (this.isGanttVisible()) this.renderGantt();
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
            if (this.isGanttVisible()) this.renderGantt();
        } catch (e) {
            const el = document.getElementById('edit-flash');
            if (el) { el.textContent = 'Delete failed: ' + e.message; el.className = 'small text-danger'; }
        }
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
        log.innerHTML += `<div class="mb-1 text-end"><span class="badge bg-blue-lt">you</span> ${this.esc(msg)}</div>`;
        log.insertAdjacentHTML('beforeend', `<div id="chat-thinking" class="mb-1 text-secondary small"><span class="spinner-border spinner-border-sm me-1"></span>Maxwell is reading the plan…</div>`);
        log.scrollTop = log.scrollHeight;
        try {
            const res = await fetch(`api/tasks/${encodeURIComponent(id)}/chat`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: msg }),
            });
            const data = await res.json().catch(() => ({}));
            const think = document.getElementById('chat-thinking'); if (think) think.remove();
            if (!res.ok) {
                log.innerHTML += `<div class="mb-1"><span class="badge bg-red-lt">error</span> ${this.esc(data.detail || ('HTTP ' + res.status))}</div>`;
                return;
            }
            const src = (data.sources || []).length
                ? `<div class="text-secondary small mt-1">sources: ${data.sources.map((s) => this.esc(s)).join(', ')}</div>` : '';
            log.innerHTML += `<div class="mb-2"><span class="badge bg-green-lt">Maxwell</span> ${this.esc(data.answer)}${src}</div>`;
            if (data.proposal) this.renderProposal(id, data.proposal);
            log.scrollTop = log.scrollHeight;
        } catch (e) {
            const think = document.getElementById('chat-thinking'); if (think) think.remove();
            log.innerHTML += `<div class="mb-1"><span class="badge bg-red-lt">error</span> ${this.esc(e.message)}</div>`;
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
            if (this.isGanttVisible()) this.renderGantt();
        } catch (e) {
            const card = document.getElementById(pid);
            if (card) card.querySelector('.card-body').insertAdjacentHTML('beforeend', `<div class="text-danger small mt-1">${this.esc(e.message)}</div>`);
        }
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

    // ---- exec summary (shareable) ---------------------------------------
    renderExec() {
        const el = document.getElementById('exec-content');
        if (!el) return;
        const r = this.plan.rollups || {};
        const t = this.tasks;
        const endDate = t.map((x) => x.finish_date).filter(Boolean).sort().slice(-1)[0] || '—';
        const blocking = t.filter((x) => x.is_blocking).length;
        const stats = [
            { l: 'Workstreams', v: r.total_workstreams }, { l: 'Tasks', v: r.total_tasks },
            { l: 'Effort (person-days)', v: r.total_effort_days }, { l: 'Target finish', v: endDate },
            { l: 'Milestones', v: (this.plan.milestones || []).length }, { l: 'Blocking tasks', v: blocking },
        ];
        const win = (arr) => {
            const s = arr.map((x) => x.start_date).filter(Boolean).sort()[0];
            const f = arr.map((x) => x.finish_date).filter(Boolean).sort().slice(-1)[0];
            return (s || '—') + ' → ' + (f || '—');
        };
        const phRows = this.PHASES.map((p) => {
            const pt = t.filter((x) => x.phase === p);
            return pt.length ? [this.badge(p, this.PHASE_COLOR[p]), pt.length,
                Math.round(pt.reduce((a, x) => a + (x.effort_days || 0), 0)) + 'd', win(pt)] : null;
        }).filter(Boolean);
        const wsRows = (this.plan.workstreams || []).map((w) => [
            this.badge(w.workstream_id, this.WS_COLOR[w.workstream_id] || 'secondary') + ' ' + this.esc(w.name),
            (w.tasks || []).length,
            Math.round((w.tasks || []).reduce((a, x) => a + (x.effort_days || 0), 0)) + 'd', win(w.tasks || []),
        ]);
        const card = (s) => `<div class="col-6 col-sm-4 col-xl-2"><div class="card card-sm"><div class="card-body"><div class="subheader">${this.esc(s.l)}</div><div class="h2 mb-0 mt-1">${this.esc(s.v)}</div></div></div></div>`;
        el.innerHTML = `
            <div class="row row-cards mb-3">${stats.map(card).join('')}</div>
            <div class="card mb-3"><div class="card-body">
                ${(this.plan.executive_summary || '').split('\n').filter(Boolean).slice(0, 2).map((p) => `<p>${this.esc(p)}</p>`).join('')}
                <p class="text-secondary small mb-0">${this.esc(this.plan.timeline_note || '')}</p>
            </div></div>
            <div class="row g-3">
                <div class="col-12 col-lg-5"><div class="card"><div class="card-header"><h3 class="card-title">Phases</h3></div>${this.table(['Phase', 'Tasks', 'Effort', 'Window'], phRows)}</div></div>
                <div class="col-12 col-lg-7"><div class="card"><div class="card-header"><h3 class="card-title">Milestones</h3></div>${this.table(['Milestone', 'Target', 'Gate'], (this.plan.milestones || []).map((m) => [this.esc(m.name), this.badge(m.target_week, 'azure'), this.esc(m.gate_criteria)]))}</div></div>
            </div>
            <div class="card mt-3"><div class="card-header"><h3 class="card-title">Workstream summary</h3></div>${this.table(['Workstream', 'Tasks', 'Effort', 'Window'], wsRows)}</div>`;
    },

    exportUrl(kind) {
        const p = new URLSearchParams();
        const set = (id, key) => { const v = (document.getElementById(id).value || '').trim(); if (v) p.set(key, v); };
        set('f-ws', 'workstream'); set('f-owner', 'owner'); set('f-assignee', 'assignee'); set('f-risk', 'risk'); set('f-search', 'q');
        if (document.getElementById('f-blocking').checked) p.set('blocking', '1');
        const qs = p.toString();
        return `api/export.${kind}` + (qs ? `?${qs}` : '');
    },

    // ---- events ----------------------------------------------------------
    wireEvents() {
        ['f-search', 'f-ws', 'f-owner', 'f-assignee', 'f-risk', 'f-blocking'].forEach((id) => {
            const el = document.getElementById(id);
            const ev = (id === 'f-search') ? 'input' : 'change';
            el.addEventListener(ev, () => { this.renderBoard(); if (this.isGanttVisible()) this.renderGantt(); });
        });
        document.getElementById('board').addEventListener('click', (e) => {
            const a = e.target.closest('a[data-task]');
            if (!a) return;
            e.preventDefault();
            this.openTask(a.getAttribute('data-task'));
        });
        ['xlsx', 'xml'].forEach((kind) => {
            const btn = document.getElementById('dl-' + kind);
            if (btn) btn.addEventListener('click', (e) => { e.preventDefault(); window.location.href = this.exportUrl(kind); });
        });
        const nb = document.getElementById('btn-new-task');
        if (nb) nb.addEventListener('click', () => this.openCreate());
    },
};

document.addEventListener('DOMContentLoaded', () => TeepPlan.init());
