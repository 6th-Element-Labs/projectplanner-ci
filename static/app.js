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
        this.renderTasks();
        this.renderTables();
        this.renderExec();
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

    filtered() {
        const q = (document.getElementById('f-search').value || '').trim().toLowerCase();
        const ws = document.getElementById('f-ws').value;
        const owner = document.getElementById('f-owner').value;
        const ownerPerson = document.getElementById('f-assignee').value;
        const risk = document.getElementById('f-risk').value;
        const blocking = document.getElementById('f-blocking').checked;
        return this.tasks.filter((t) => {
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

    renderTasks() {
        const el = document.getElementById('tasks-content');
        if (!el) return;
        // When the owner filter is set to one person, show ONLY their section
        // (co-owned tasks otherwise leak into every co-owner's group).
        const sel = document.getElementById('f-assignee');
        const only = sel ? sel.value : '';
        const groups = {};
        this.filtered().forEach((t) => {
            this._peopleOf(t).forEach((p) => {
                if (only && p !== only) return;
                (groups[p] || (groups[p] = [])).push(t);
            });
        });
        const names = Object.keys(groups).filter((n) => n !== 'Unassigned')
            .sort((a, b) => groups[b].length - groups[a].length || a.localeCompare(b));
        if (groups['Unassigned']) names.push('Unassigned');
        if (!names.length) {
            el.innerHTML = `<div class="card"><div class="empty">
                <div class="empty-icon"><i class="ti ti-checklist"></i></div>
                <p class="empty-title">Nothing to show</p>
                <p class="empty-subtitle text-secondary">No tasks match the current filters.</p></div></div>`;
            return;
        }
        const rank = (s) => (s === 'Done' ? 1 : 0);
        el.innerHTML = names.map((name) => {
            const list = groups[name].slice().sort((a, b) =>
                rank(a.status) - rank(b.status) ||
                ((a.finish_date || '9999') < (b.finish_date || '9999') ? -1 : 1));
            const done = list.filter((t) => t.status === 'Done').length;
            const isU = name === 'Unassigned';
            const avatar = isU
                ? `<span class="avatar avatar-sm avatar-rounded me-2 bg-secondary-lt"><i class="ti ti-user-question"></i></span>`
                : `<span class="avatar avatar-sm avatar-rounded me-2">${this.esc(this.initials(name))}</span>`;
            const nextUp = (this.signals && !isU && (this.signals.by_owner_next || {})[name]) || [];
            const nextHtml = nextUp.length ? `<div class="mb-2 ms-1 small">
                <span class="text-secondary me-1"><i class="ti ti-player-track-next-filled"></i> Next up:</span>
                ${nextUp.map((n) => `<a href="#" class="text-reset fw-medium me-3" data-task="${this.esc(n.task_id)}"><span class="status-dot bg-${this.STATUS_COLOR[n.status] || 'secondary'} me-1"></span>${this.esc(n.task_id)} · ${this.esc((n.title || '').slice(0, 42))}</a>`).join('')}
            </div>` : '';
            return `
                <div class="mb-4">
                    <div class="d-flex align-items-center mb-2">
                        ${avatar}
                        <span class="h3 m-0">${this.esc(name)}</span>
                        <span class="badge bg-secondary-lt ms-2">${list.length}</span>
                        <span class="ms-auto text-secondary small">${done}/${list.length} done</span>
                    </div>
                    ${nextHtml}
                    <div class="card">
                        <div class="list-group list-group-flush">
                            ${list.map((t) => this.taskRow(t)).join('')}
                        </div>
                    </div>
                </div>`;
        }).join('');
    },

    taskRow(t) {
        const done = t.status === 'Done';
        const wc = this.WS_COLOR[t._wsId] || 'secondary';
        const due = this.fmtDue(t.finish_date, done);
        const id = this.esc(t.task_id);
        const titleCls = done ? 'text-decoration-line-through text-secondary' : 'text-body';
        return `
            <div class="list-group-item d-flex align-items-start gap-2 py-2" data-task-row="${id}">
                <input class="form-check-input rounded-circle mt-1 flex-shrink-0" type="checkbox" data-check="${id}"${done ? ' checked' : ''} title="Mark done"/>
                <div class="flex-fill">
                    <a href="#" class="d-block fw-medium text-reset ${titleCls}" data-task="${id}">${this.esc(t.title)}</a>
                    <div class="d-flex flex-wrap align-items-center gap-2 mt-1 small">
                        ${due.text ? `<span class="${due.cls}"><i class="ti ti-calendar-event me-1"></i>${due.text}</span>` : ''}
                        ${t.risk_level === 'High' ? '<span class="text-red" title="High risk"><i class="ti ti-flag-filled"></i></span>' : ''}
                        ${t.is_blocking ? '<span class="text-red" title="Blocking"><i class="ti ti-alert-triangle-filled"></i></span>' : ''}
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
                    <button id="edit-dispatch" class="btn btn-outline-primary btn-sm ms-auto" title="Hand this task to Claude Code — opens a PR on a claude/ branch (never main), watchable in the Claude Code app"><i class="ti ti-robot me-1"></i>Dispatch to Claude Code</button>
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
            this.renderTasks();
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
            this.renderTasks();
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
                return `<div class="mb-2 text-end"><span class="badge bg-blue-lt">you</span> ${this.esc(m.content)}</div>`;
            const sources = (m.payload && m.payload.sources) || [];
            const src = sources.length
                ? `<div class="text-secondary small mt-1">sources: ${sources.map((s) => this.esc(s)).join(', ')}</div>` : '';
            return `<div class="mb-2"><span class="badge bg-green-lt">Maxwell</span> ${this.esc(m.content)}${src}</div>`;
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
        log.insertAdjacentHTML('beforeend', `<div class="mb-2 text-end"><span class="badge bg-blue-lt">you</span> ${this.esc(msg)}</div>`);
        log.insertAdjacentHTML('beforeend', `<div id="ask-thinking" class="mb-2 text-secondary small"><span class="spinner-border spinner-border-sm me-1"></span>Maxwell is reading the plan…</div>`);
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
                log.insertAdjacentHTML('beforeend', `<div class="mb-2"><span class="badge bg-red-lt">error</span> ${this.esc(data.detail || ('HTTP ' + res.status))}</div>`);
                return;
            }
            const sources = data.sources || [];
            const src = sources.length
                ? `<div class="text-secondary small mt-1">sources: ${sources.map((s) => this.esc(s)).join(', ')}</div>` : '';
            log.insertAdjacentHTML('beforeend', `<div class="mb-2"><span class="badge bg-green-lt">Maxwell</span> ${this.esc(data.answer)}${src}</div>`);
            const props = (data.proposals && data.proposals.length) ? data.proposals : (data.proposal ? [data.proposal] : []);
            if (props.length === 1) this.renderAskProposal(props[0]);
            else if (props.length > 1) this.renderAskProposals(props);
            this._askScroll();
        } catch (e) {
            const think = document.getElementById('ask-thinking');
            if (think) think.remove();
            log.insertAdjacentHTML('beforeend', `<div class="mb-2"><span class="badge bg-red-lt">error</span> ${this.esc(e.message)}</div>`);
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

    _propChips(p) {
        const fields = Object.keys(p).filter((k) => !['rationale', 'task_id'].includes(k) && p[k] != null && p[k] !== '');
        return fields.map((k) => `<span class="badge bg-azure-lt me-1">${this.esc(k)}: ${this.esc(String(p[k]))}</span>`).join('')
            || '<span class="text-secondary small">no change</span>';
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
        log.insertAdjacentHTML('beforeend', `<div class="mb-2 text-end"><span class="badge bg-blue-lt">intake</span> ${this.esc(kind)}${title ? ' · ' + this.esc(title) : ''}</div>`);
        this._askScroll();
        try {
            const res = await fetch('api/intake', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind, title, text }) });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || ('HTTP ' + res.status));
            if (flash) flash.textContent = `ingested ${data.ingested_chunks} chunk(s) into the corpus`;
            const src = (data.sources || []).length ? `<div class="text-secondary small mt-1">sources: ${data.sources.map((s) => this.esc(s)).join(', ')}</div>` : '';
            log.insertAdjacentHTML('beforeend', `<div class="mb-2"><span class="badge bg-green-lt">Maxwell</span> ${this.esc(data.summary)}${src}</div>`);
            const props = data.proposals || [];
            if (props.length === 1) this.renderAskProposal(props[0]);
            else if (props.length > 1) this.renderAskProposals(props);
            if ((data.new_tasks || []).length) this.renderAskNewTasks(data.new_tasks);
            document.getElementById('intake-text').value = '';
            this._askScroll();
        } catch (e) {
            if (flash) flash.textContent = '';
            log.insertAdjacentHTML('beforeend', `<div class="mb-2"><span class="badge bg-red-lt">error</span> ${this.esc(e.message)}</div>`);
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
    mdLite(text) {
        return (text || '').split('\n').map((line) => {
            const l = this.esc(line).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
            const m = l.match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/);
            if (m) return `<div class="ms-3">• ${m[1]}</div>`;
            if (!l.trim()) return '<div class="mb-2"></div>';
            return `<div>${l}</div>`;
        }).join('');
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
        try {
            const data = await (await fetch('api/digests')).json();
            const ds = data.digests || [];
            if (ds.length) {
                this.renderDigest(ds[0], document.getElementById('digest-latest'));
                this.renderDigestHistory(ds.slice(1));
            }
        } catch (e) { /* keep the empty hint */ }
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

    // ---- Inbox (Live Inbox: email-triaged review queue) -----------------
    async initInbox() {
        try {
            const data = await (await fetch('api/inbox')).json();
            this._renderInboxBadge(data.pending || 0);
            this.renderInbox(data.items || []);
        } catch (e) { /* leave hint */ }
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

    renderInbox(items) {
        const el = document.getElementById('inbox-content');
        if (!el) return;
        if (!items.length) {
            el.classList.add('text-secondary');
            el.innerHTML = 'Nothing yet. Email <strong>plan@taikunai.com</strong> (once connected) — the agent acts on it autonomously and replies, and a log of what it did shows here. Use <em>Simulate email</em> to try it now.';
            return;
        }
        el.classList.remove('text-secondary');
        el.innerHTML = items.map((it) => this._inboxItemHtml(it)).join('');
        items.forEach((it) => this._wireInboxItem(it));
    },

    _inboxItemHtml(it) {
        const tri = it.triage || {};
        const when = it.received_at ? new Date(it.received_at * 1000).toLocaleString() : '';
        const chip = it.status === 'applied' ? '<span class="badge bg-green-lt">Acted</span>'
            : it.status === 'pending' ? '<span class="badge bg-yellow-lt">Pending</span>'
                : `<span class="badge bg-secondary-lt">${this.esc(it.status)}</span>`;
        let bodyHtml;
        if (it.status === 'pending') {
            const propRows = (tri.proposals || []).map((p, idx) => `<div class="d-flex align-items-center gap-2 py-1" data-iprow="${idx}">
                    <span class="fw-medium font-monospace">${this.esc(p.task_id)}</span>
                    <div class="flex-fill">${this._propChips(p)}</div>
                    <button class="btn btn-sm btn-ghost-secondary p-1" data-ipdrop="${idx}" title="Drop"><i class="ti ti-x"></i></button>
                </div>`).join('');
            const ntRows = (tri.new_tasks || []).map((t, idx) => `<div class="d-flex align-items-center gap-2 py-1" data-introw="${idx}">
                    <span class="badge bg-green-lt">new</span><span class="badge bg-azure-lt">${this.esc(t.workstream_id)}</span>
                    <span class="flex-fill fw-medium">${this.esc(t.title)}</span>
                    <button class="btn btn-sm btn-ghost-secondary p-1" data-intdrop="${idx}" title="Drop"><i class="ti ti-x"></i></button>
                </div>`).join('');
            bodyHtml = `${(propRows || ntRows) ? `<div class="my-2">${propRows}${ntRows}</div>` : '<div class="text-secondary small my-2">No proposed changes.</div>'}
                <div><button class="btn btn-primary btn-sm" data-iconfirm><i class="ti ti-checks me-1"></i>Confirm selected</button>
                <button class="btn btn-sm" data-idismiss>Dismiss</button>
                <span class="small text-secondary ms-2" data-istatus></span></div>`;
        } else {
            const a = tri.applied || {};
            const u = a.updated || [], c = a.created || [];
            const rep = tri.reply;
            const reply = rep ? (rep.sent ? 'replied to sender' : (rep.dry_run ? 'reply: dry-run (SMTP off)' : (rep.error ? 'reply failed' : ''))) : '';
            const did = (u.length || c.length)
                ? `${u.length ? 'updated ' + u.map((x) => this.esc(x)).join(', ') : ''}${(u.length && c.length) ? ' · ' : ''}${c.length ? 'created ' + c.map((x) => this.esc(x)).join(', ') : ''}`
                : 'no task change — answered / ingested for reference';
            bodyHtml = `<div class="small text-secondary mt-2"><i class="ti ti-checks me-1 text-green"></i>${did}${reply ? ' · ' + reply : ''}</div>`;
        }
        return `<div class="card card-sm mb-2" data-inbox="${it.id}">
            <div class="card-status-start bg-azure"></div>
            <div class="card-body">
                <div class="d-flex align-items-center gap-2">
                    <i class="ti ti-mail"></i><strong>${this.esc(it.subject || '(no subject)')}</strong>
                    <span class="text-secondary small">${this.esc(it.sender || '')}</span>
                    ${chip}
                    <span class="ms-auto text-secondary small">${this.esc(when)}</span>
                </div>
                <div class="small mt-1">${this.mdLite(it.summary || '')}</div>
                ${bodyHtml}
            </div></div>`;
    },

    _wireInboxItem(it) {
        if (it.status !== 'pending') return;
        const card = document.querySelector(`[data-inbox="${it.id}"]`);
        if (!card) return;
        const tri = it.triage || {};
        const props = (tri.proposals || []).map((p) => Object.assign({}, p));
        const nts = (tri.new_tasks || []).map((t) => Object.assign({}, t));
        card.querySelectorAll('[data-ipdrop]').forEach((b) => b.addEventListener('click', () => {
            const idx = parseInt(b.getAttribute('data-ipdrop'), 10); props[idx] = null;
            const r = card.querySelector(`[data-iprow="${idx}"]`); if (r) r.remove();
        }));
        card.querySelectorAll('[data-intdrop]').forEach((b) => b.addEventListener('click', () => {
            const idx = parseInt(b.getAttribute('data-intdrop'), 10); nts[idx] = null;
            const r = card.querySelector(`[data-introw="${idx}"]`); if (r) r.remove();
        }));
        card.querySelector('[data-idismiss]').addEventListener('click', () => this.dismissInbox(it.id));
        card.querySelector('[data-iconfirm]').addEventListener('click', () => this.confirmInbox(it.id, props.filter(Boolean), nts.filter(Boolean), card));
    },

    async confirmInbox(id, proposals, new_tasks, card) {
        const st = card ? card.querySelector('[data-istatus]') : null;
        if (st) st.textContent = 'Applying…';
        try {
            const res = await fetch(`api/inbox/${id}/confirm`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ proposals, new_tasks }),
            });
            const data = await res.json();
            const a = data.applied || {};
            if (card) card.querySelector('.card-body').innerHTML = `<span class="text-green"><i class="ti ti-checks me-1"></i>Applied — updated ${(a.updated || []).length}, created ${(a.created || []).length}${(a.failed || []).length ? ', failed ' + a.failed.length : ''}</span>`;
            await this._reloadBoardData();
            this.initInbox();
        } catch (e) { if (st) st.textContent = 'failed: ' + e.message; }
    },

    async dismissInbox(id) {
        try { await fetch(`api/inbox/${id}/dismiss`, { method: 'POST' }); this.initInbox(); } catch (e) { /* noop */ }
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
        set('f-ws', 'workstream'); set('f-owner', 'owner'); set('f-assignee', 'person'); set('f-risk', 'risk'); set('f-search', 'q');
        if (document.getElementById('f-blocking').checked) p.set('blocking', '1');
        const qs = p.toString();
        return `api/export.${kind}` + (qs ? `?${qs}` : '');
    },

    // ---- events ----------------------------------------------------------
    wireEvents() {
        ['f-search', 'f-ws', 'f-owner', 'f-assignee', 'f-risk', 'f-blocking'].forEach((id) => {
            const el = document.getElementById(id);
            const ev = (id === 'f-search') ? 'input' : 'change';
            el.addEventListener(ev, () => { this.renderBoard(); this.renderTasks(); if (this.isGanttVisible()) this.renderGantt(); });
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
