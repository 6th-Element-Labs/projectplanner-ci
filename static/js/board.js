/* ARCH-MS-21: board filters, cards, and summary rendering. */
(function (global) {
    'use strict';
    const methods = {
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
        const ps = document.getElementById('plan-stats');
        if (!ps) return;   // the hidden rollup strip was removed in the sidebar redesign
        ps.innerHTML = cards.map((c) => `
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
            (this.projectContext ? this.projectContextHtml(this.projectContext) : '') +
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
    // Column axis is adaptive: group by lifecycle phase only when every phase is a
    // canonical stage (Maxwell stays great); otherwise group by workflow status so
    // plans with ad-hoc/mixed phase labels (Helm's Wave/P0/P1 soup) get a clean,
    // consistent kanban instead of a dozen stray columns.
    _boardGrouping() {
        const canon = ['Kickoff', 'Bootstrap', 'Build', 'Cutover', 'Operate'];
        const present = new Set();
        (this.tasks || []).forEach((t) => { if (t.phase) present.add(t.phase); });
        if (present.size && [...present].every((p) => canon.indexOf(p) >= 0)) return 'phase';
        return 'status';
    },

    _boardColumns(mode) {
        if (mode === 'phase') return this.PHASES;
        // Status kanban: canonical order, only columns that have tasks, unknowns last.
        const order = ['Not Started', 'In Progress', 'In Review', 'Blocked', 'Done'];
        const present = new Set((this.tasks || []).map((t) => t.status).filter(Boolean));
        const cols = order.filter((s) => present.has(s));
        [...present].forEach((s) => { if (order.indexOf(s) < 0) cols.push(s); });
        return cols.length ? cols : order;
    },

    renderBoard() {
        const tasks = this.filtered();
        const board = document.getElementById('board');
        if (!board) return;
        const mode = this._boardGrouping();
        const cols = this._boardColumns(mode);
        const colorMap = mode === 'phase' ? this.PHASE_COLOR : this.STATUS_COLOR;
        board.innerHTML = cols.map((colName) => {
            const col = tasks.filter((t) => (mode === 'phase' ? t.phase : t.status) === colName);
            const days = col.reduce((s, t) => s + (t.effort_days || 0), 0);
            const color = colorMap[colName] || 'secondary';
            const cards = col.length
                ? col.map((t) => this.taskCard(t)).join('')
                : `<div class="text-secondary text-center py-4 small">—</div>`;
            return `
                <div class="tk-board-col">
                    <div class="d-flex align-items-center mb-3 px-1">
                        <span class="status-dot bg-${color} me-2"></span>
                        <span class="h3 m-0">${this.esc(colName)}</span>
                        <span class="badge bg-secondary-lt ms-2">${col.length}</span>
                        <span class="ms-auto text-secondary small">${Math.round(days)}d</span>
                    </div>
                    <div>${cards}</div>
                </div>`;
        }).join('');
        this.renderFleetDock();
    },

    taskCard(t) {
        const done = t.status === 'Done';
        const honest = t.honest_display || {};
        // SIMPLIFY-3: prefer TaskSession label over raw workflow status.
        const displayLabel = honest.label || t.status || '';
        const sc = honest.lifecycle_phase === 'start_failed_retry'
            ? 'orange'
            : (this.STATUS_COLOR[t.status] || 'secondary');
        const deps = (t.depends_on || []).length;
        const tally = this.taskTally(t.task_id);
        const econ = this.tallyMini(tally);
        const provenance = this.provenanceBadge(t);
        const meta = [];
        if (t.owner_org) meta.push(this.esc(t.owner_org));
        if (t.effort_days != null) meta.push(this.esc(t.effort_days) + 'd');
        if (deps) meta.push(`<i class="ti ti-link"></i>${deps}`);
        const honestBadge = honest.lifecycle_phase === 'start_failed_retry'
            ? `<span class="badge bg-orange-lt text-orange" title="${this.esc(honest.reason || honest.message || displayLabel)}">${this.esc(displayLabel)}</span>`
            : '';
        return `
            <a href="#" class="d-block text-reset" data-task="${this.esc(t.task_id)}">
                <div class="card card-sm mb-2"${done ? ' style="opacity:.55"' : ''}>
                    <div class="card-status-start bg-${sc}"></div>
                    <div class="card-body">
                        <div class="d-flex align-items-center gap-2 mb-1">
                            <span class="status-dot bg-${sc}" title="${this.esc(displayLabel)}"></span>
                            <span class="text-secondary small fw-medium text-uppercase">${this.esc(t._wsId)}</span>
                            <span class="ms-auto text-secondary small font-monospace">${this.esc(t.task_id)}</span>
                        </div>
                        <div class="fw-semibold lh-sm ${done ? 'text-decoration-line-through text-secondary' : 'text-body'}">${this.esc(t.title)}</div>
                        ${honestBadge ? `<div class="mt-1">${honestBadge}</div>` : ''}
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

    };
    global.SwitchboardBoard = Object.freeze({ methods });
})(window);
