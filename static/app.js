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
    projectContext: null, // project hierarchy + repo role guide from api/board
    deliverables: [],
    missionStatus: null,
    selectedDeliverableId: '',
    missionKpis: [],        // UI-2: project KPIs with rollup (tiles)
    missionOutcomes: [],    // UI-2: outcomes-to-verify queue
    missionGraph: null,
    _missionDagRenderId: 0,
    _missionPollMs: 5000,   // live cockpit poll interval (ms) — snappy now the graph is ~0.2s
    _missionLiveTimer: null,
    _missionSig: null,
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
    DELIVERABLE_STATUS_COLOR: {
        proposed: 'secondary', approved: 'azure', in_progress: 'blue', blocked: 'red',
        in_review: 'yellow', done: 'green', archived: 'secondary',
    },
    MILESTONE_STATUS_COLOR: {
        not_started: 'secondary', in_progress: 'blue', blocked: 'red',
        in_review: 'azure', done: 'green', skipped: 'secondary',
    },
    // UI-1: authoring vocab — kept in sync with store.py (DELIVERABLE_MILESTONE_STATUSES
    // and link_task_to_deliverable's role auto-classifier).
    MILESTONE_STATUSES: ['not_started', 'in_progress', 'blocked', 'in_review', 'done', 'skipped'],
    DELIVERABLE_LINK_ROLES: ['contributes', 'implementation', 'acceptance', 'foundation', 'parked'],
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
        if (this._noProjects) { this.renderNoProjects(); return; }
        try {
            // HARDEN-38: fire board/people/tally concurrently — they're independent
            // once the project is known, so the critical path isn't 3 serial round-trips.
            // HARDEN-35: project_context is no longer bundled in /api/board; fetch it
            // in parallel from its own (browser-cached) endpoint.
            const boardReq = fetch('api/board');
            const peopleReq = fetch('api/people').then((r) => r.json()).then((d) => d.people || []).catch(() => []);
            const tallyReq = fetch(`tally/v1/project?project=${encodeURIComponent(window.PM_PROJECT || 'maxwell')}`)
                .then((r) => r.json()).catch(() => null);
            const ctxReq = this.fetchProjectContext();
            const res = await boardReq;
            if (!res.ok) throw new Error(`HTTP ${res.status} loading the board`);
            this.plan = await res.json();
            this.people = await peopleReq;
            this.tally = await tallyReq;
            this.projectContext = await ctxReq;
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
        // HARDEN-39: render only the active (Exec) tab now; the heavier off-screen
        // tabs (Board, Tasks, tables, Pulse) render the first time they're shown and
        // on every show, so nothing goes stale. Mutations/filters still re-render the
        // visible tab via their existing render calls.
        this.renderExec();
        this.wireEvents();
        this.setupGantt();
        this._wireLazyTabs();
        this.loadSignals();
        this.initInbox();
        this.renderTallyPulse();   // Pulse (tally strip + digest) lives in the default Overview tab now
        this.initPulse();
        this._missionDeliverableFromUrl();
        await this._preloadDeliverableDefault();
        await this.initHeaderDeliverableSwitcher();
        this._renderActiveTop();   // deep-linked URLs land on a rendered tab, not a blank one
        const ds = document.getElementById('data-status');
        if (ds) { ds.className = 'badge bg-green-lt'; ds.textContent = `${this.tasks.length} tasks`; }
    },

    // HARDEN-35: project_context (repo roles, hierarchy, policy profiles) is a
    // near-static ~9KB blob the board + task list never render — only the
    // task-detail "Project context" card falls back to it (task detail carries
    // its own per-task project_context from get_task). It's fetched from its own
    // endpoint, which sets ETag + max-age so refocus/reload reuse the cache.
    async fetchProjectContext() {
        const proj = window.PM_PROJECT || 'maxwell';
        try {
            const r = await fetch(`api/projects/${encodeURIComponent(proj)}/context`);
            return r.ok ? await r.json() : null;
        } catch (e) { return null; }
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
        let cur = window.PM_PROJECT || 'maxwell';
        let list = [{ id: 'maxwell', label: 'Project Maxwell', pretitle: '' }];
        try { list = (await (await fetch('api/projects')).json()).projects || list; } catch (e) { /* offline */ }
        // Global auth: the list is filtered to the projects this user can access.
        // Fall back to the first accessible one if the stored project isn't in it;
        // if there are none, flag an empty workspace so init() shows a message.
        this._noProjects = list.length === 0;
        if (list.length && !list.some((p) => p.id === cur)) {
            cur = list[0].id;
            window.PM_PROJECT = cur;
            try { localStorage.setItem('pm_project', cur); } catch (e) {}
        }
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

    // Global auth: a signed-in user with no project grants sees a friendly empty
    // workspace instead of a failed board load.
    renderNoProjects() {
        const host = document.querySelector('.page-body .container-xl') || document.body;
        host.innerHTML = `<div class="empty py-6">
            <div class="empty-icon"><i class="ti ti-folder-off"></i></div>
            <p class="empty-title">No projects yet</p>
            <p class="empty-subtitle text-secondary">Your account doesn't have access to any projects yet.
            Ask an owner to grant you access, then refresh.</p>
            <div class="empty-action"><a href="/login" class="btn" id="np-signout"><i class="ti ti-logout me-1"></i>Sign out</a></div>
        </div>`;
        const btn = document.getElementById('np-signout');
        if (btn) btn.addEventListener('click', (e) => {
            e.preventDefault();
            fetch('/api/auth/logout', { method: 'POST' }).finally(() => { window.location.href = '/login'; });
        });
        const ds = document.getElementById('data-status');
        if (ds) { ds.className = 'badge bg-secondary-lt'; ds.textContent = 'no access'; }
    },

    // ACCESS-14/15: create a project from the web (contributors and up). Private by default.
    openNewProject() {
        const flash = document.getElementById('np-flash');
        if (flash) { flash.textContent = ''; flash.className = 'small text-secondary me-auto'; }
        const name = document.getElementById('np-name');
        if (name) name.value = '';
        const priv = document.querySelector('input[name="np-visibility"][value="private"]');
        if (priv) priv.checked = true;
        window.bootstrap.Modal.getOrCreateInstance(document.getElementById('new-project-modal')).show();
        setTimeout(() => name && name.focus(), 200);
    },

    async submitNewProject() {
        const flash = document.getElementById('np-flash');
        const btn = document.getElementById('np-create');
        const setFlash = (msg, cls) => { if (flash) { flash.textContent = msg; flash.className = `small ${cls} me-auto`; } };
        const name = (document.getElementById('np-name')?.value || '').trim();
        const visibility = document.querySelector('input[name="np-visibility"]:checked')?.value || 'private';
        const github_repo = (document.getElementById('np-repo')?.value || '').trim();
        if (!name) { setFlash('Enter a project name.', 'text-danger'); return; }
        if (btn) btn.disabled = true;
        setFlash('Creating…', 'text-secondary');
        try {
            const body = { name, visibility };
            if (github_repo) body.github_repo = github_repo;
            const res = await fetch('api/projects', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                throw new Error(res.status === 403
                    ? 'You don’t have permission to create projects.'
                    : (data.detail || data.error || `Failed (${res.status})`));
            }
            const id = (data.project && data.project.id) || '';
            try { localStorage.setItem('pm_project', id); } catch (e) {}
            // If they named a repo, hand off to the guided webhook-wiring panel before
            // switching boards — the ?project= pin is only useful once they install it.
            if (github_repo) {
                setFlash('Created — wire the webhook…', 'text-success');
                window.bootstrap.Modal.getOrCreateInstance(document.getElementById('new-project-modal')).hide();
                this.openGithubAssoc(id, { switchTo: id });
                if (btn) btn.disabled = false;
                return;
            }
            setFlash('Created — switching…', 'text-success');
            const u = new URL(window.location.href);
            u.searchParams.set('project', id);
            window.location.href = u.toString();   // reload into the new project
        } catch (e) {
            setFlash(e.message || 'Failed to create project.', 'text-danger');
            if (btn) btn.disabled = false;
        }
    },

    // ---- UI-15: connect a GitHub repo + guided webhook wiring -------------
    // Opens the shared association modal for `projectId`. opts.switchTo, when set, shows a
    // "Go to project" button that reloads into that board (used right after New Project).
    openGithubAssoc(projectId, opts) {
        const proj = projectId || window.PM_PROJECT || 'maxwell';
        this._gaProject = proj;
        this._gaSwitchTo = (opts && opts.switchTo) || '';
        const label = document.getElementById('ga-project-label');
        if (label) label.textContent = proj;
        const repo = document.getElementById('ga-repo'); if (repo) repo.value = '';
        const flash = document.getElementById('ga-repo-flash'); if (flash) { flash.textContent = ''; flash.className = 'small mt-1 text-secondary'; }
        const vflash = document.getElementById('ga-verify-flash'); if (vflash) vflash.textContent = '';
        const panel = document.getElementById('ga-panel'); if (panel) panel.style.display = 'none';
        const goto = document.getElementById('ga-goto');
        if (goto) goto.style.display = this._gaSwitchTo ? '' : 'none';
        window.bootstrap.Modal.getOrCreateInstance(document.getElementById('github-assoc-modal')).show();
        this.loadGithubAssoc();
    },

    async loadGithubAssoc(check) {
        const proj = this._gaProject;
        if (!proj) return;
        try {
            const url = `api/projects/${encodeURIComponent(proj)}/github_association${check ? '?check=1' : ''}`;
            const res = await fetch(url);
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || data.error || `Failed (${res.status})`);
            this.renderGithubAssoc(data);
        } catch (e) {
            const flash = document.getElementById('ga-repo-flash');
            if (flash) { flash.textContent = e.message || 'Failed to load.'; flash.className = 'small mt-1 text-danger'; }
        }
    },

    renderGithubAssoc(data) {
        const repoInput = document.getElementById('ga-repo');
        if (repoInput && data.repo && !repoInput.value) repoInput.value = data.repo;
        const panel = document.getElementById('ga-panel');
        const verify = document.getElementById('ga-verify');
        if (!data.repo_configured) {
            if (panel) panel.style.display = 'none';
            if (verify) verify.style.display = 'none';
            return;
        }
        if (panel) panel.style.display = '';
        if (verify) verify.style.display = '';
        const wh = data.webhook || {};
        const set = (id, val) => { const el = document.getElementById(id); if (el) el.value = val || ''; };
        set('ga-url', wh.payload_url);
        set('ga-secret', wh.secret_env);
        set('ga-gh', wh.gh_command);
        const name = document.getElementById('ga-repo-name'); if (name) name.textContent = data.repo;
        const warn = document.getElementById('ga-secret-warn');
        if (warn) warn.style.display = wh.secret_configured ? 'none' : '';
        // Verification badge — flips green on the first real delivery.
        const v = data.verification || {};
        const badge = document.getElementById('ga-status');
        if (badge) {
            let cls = 'badge ms-2 ', txt = '';
            if (v.status === 'connected') {
                cls += 'bg-green-lt'; txt = 'Connected';
                if (v.delivery_count) txt += ` · ${v.delivery_count} deliver${v.delivery_count === 1 ? 'y' : 'ies'}`;
            } else if (v.repo_reachable === false) {
                cls += 'bg-red-lt'; txt = 'Repo unreachable';
            } else if (v.repo_reachable === true) {
                cls += 'bg-yellow-lt'; txt = 'Repo found · awaiting first delivery';
            } else {
                cls += 'bg-yellow-lt'; txt = 'Awaiting first delivery';
            }
            badge.className = cls;
            badge.textContent = txt;
        }
    },

    async saveGithubRepo() {
        const proj = this._gaProject;
        const flash = document.getElementById('ga-repo-flash');
        const setFlash = (msg, cls) => { if (flash) { flash.textContent = msg; flash.className = `small mt-1 ${cls}`; } };
        const github_repo = (document.getElementById('ga-repo')?.value || '').trim();
        if (!github_repo) { setFlash('Enter a repo as owner/name.', 'text-danger'); return; }
        const btn = document.getElementById('ga-save');
        if (btn) btn.disabled = true;
        setFlash('Saving…', 'text-secondary');
        try {
            const res = await fetch(`api/projects/${encodeURIComponent(proj)}/github_repo`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ github_repo }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                throw new Error(res.status === 403
                    ? 'You need admin on this project to set its repo.'
                    : (data.detail || data.error || `Failed (${res.status})`));
            }
            setFlash('Saved — now install the webhook below.', 'text-success');
            await this.loadGithubAssoc();
        } catch (e) {
            setFlash(e.message || 'Failed to save repo.', 'text-danger');
        } finally {
            if (btn) btn.disabled = false;
        }
    },

    async verifyGithubConnection() {
        const btn = document.getElementById('ga-verify');
        const flash = document.getElementById('ga-verify-flash');
        if (flash) { flash.textContent = 'Checking…'; flash.className = 'small text-secondary me-auto'; }
        if (btn) btn.disabled = true;
        await this.loadGithubAssoc(true);
        const badge = document.getElementById('ga-status');
        const connected = badge && /Connected/.test(badge.textContent || '');
        if (flash) {
            flash.textContent = connected
                ? 'Delivery received — you’re connected.'
                : 'No delivery yet. Push a commit or merge a PR, then verify again.';
            flash.className = `small me-auto ${connected ? 'text-success' : 'text-secondary'}`;
        }
        if (btn) btn.disabled = false;
    },

    // ---- UI-14: Settings → Communications ---------------------------------
    // Inbound domain associations (the editable UI-13 routing map) + per-project outbound
    // recipients/cadence. Admin-gated writes; anyone who can read the project can view.
    openComms(projectId) {
        const proj = projectId || window.PM_PROJECT || 'maxwell';
        this._commsProject = proj;
        this._comms = { domains: [], notify: [], digest: [] };
        this._commsAdmin = true;
        ['comms-project-label', 'comms-dom-proj'].forEach((id) => {
            const el = document.getElementById(id); if (el) el.textContent = proj;
        });
        const flash = document.getElementById('comms-flash'); if (flash) flash.textContent = '';
        const load = document.getElementById('comms-load-flash');
        if (load) { load.style.display = ''; load.textContent = 'Loading…'; load.className = 'small text-secondary'; }
        const body = document.getElementById('comms-body'); if (body) body.style.display = 'none';
        window.bootstrap.Modal.getOrCreateInstance(document.getElementById('comms-modal')).show();
        this.loadComms();
    },

    async loadComms() {
        const proj = this._commsProject;
        const load = document.getElementById('comms-load-flash');
        try {
            const res = await fetch(`api/projects/${encodeURIComponent(proj)}/comms`);
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || data.error || `Failed (${res.status})`);
            this.renderComms(data);
            if (load) load.style.display = 'none';
            const body = document.getElementById('comms-body'); if (body) body.style.display = '';
        } catch (e) {
            if (load) { load.style.display = ''; load.textContent = e.message || 'Failed to load.'; load.className = 'small text-danger'; }
        }
    },

    renderComms(data) {
        const inb = data.inbound || {}, out = data.outbound || {}, fb = data.global_fallback || {};
        if (typeof data.can_edit === 'boolean') this._commsAdmin = data.can_edit;
        this._comms = {
            domains: (inb.domains || []).slice(),
            notify: (out.notify_recipients || []).slice(),
            digest: (out.digest_recipients || []).slice(),
        };
        const plus = document.getElementById('comms-plus'); if (plus) plus.value = inb.plus_address || '';
        const fbEl = document.getElementById('comms-fallback');
        if (fbEl) fbEl.textContent = fb.configured ? (fb.notify_to || []).join(', ') : '(none configured)';
        // Cadence select
        const sel = document.getElementById('comms-cadence');
        if (sel) {
            sel.innerHTML = (out.cadence_options || ['off', 'daily', 'weekly', 'monthly'])
                .map((c) => `<option value="${c}">${c}</option>`).join('');
            sel.value = out.cadence || 'weekly';
        }
        this._renderCommsChips();
    },

    _renderCommsChips() {
        const admin = this._commsAdmin;
        const chip = (val, kind) => {
            const x = admin ? `<button type="button" class="btn-close btn-close-sm ms-1" data-rm-kind="${kind}" data-rm-val="${this.esc(val)}" aria-label="Remove"></button>` : '';
            return `<span class="badge bg-blue-lt d-inline-flex align-items-center">${this.esc(val)}${x}</span>`;
        };
        const put = (id, list, kind, empty) => {
            const el = document.getElementById(id); if (!el) return;
            el.innerHTML = list.length ? list.map((v) => chip(v, kind)).join('')
                : `<span class="text-secondary small">${empty}</span>`;
        };
        put('comms-domains', this._comms.domains, 'domains', 'No domains associated — plus-address still works.');
        put('comms-notify', this._comms.notify, 'notify', 'Falls back to the global list.');
        put('comms-digest', this._comms.digest, 'digest', 'Falls back to the global list.');
        // Remove-chip handlers
        document.querySelectorAll('#comms-body [data-rm-kind]').forEach((b) => {
            b.addEventListener('click', () => {
                const kind = b.getAttribute('data-rm-kind'), val = b.getAttribute('data-rm-val');
                this._comms[kind] = this._comms[kind].filter((v) => v !== val);
                this._renderCommsChips();
            });
        });
        // Disable editing controls when not admin
        document.querySelectorAll('#comms-modal .comms-editable, #comms-modal .comms-editable input, #comms-modal .comms-editable button, #comms-modal .comms-editable select').forEach((el) => {
            if ('disabled' in el) el.disabled = !admin;
        });
        const save = document.getElementById('comms-save'); if (save) save.disabled = !admin;
        const warn = document.getElementById('comms-admin-warn'); if (warn) warn.style.display = admin ? 'none' : '';
    },

    _commsAddDomain() {
        const inp = document.getElementById('comms-domain-input');
        const v = (inp?.value || '').trim().replace(/^@/, '').toLowerCase();
        if (!v) return;
        if (!/^[a-z0-9.-]+\.[a-z]{2,}$/.test(v)) { this._commsFlash('Enter a valid domain like client.com.', 'text-danger'); return; }
        if (this._comms.domains.indexOf(v) < 0) this._comms.domains.push(v);
        if (inp) inp.value = '';
        this._commsFlash('', '');
        this._renderCommsChips();
    },

    _commsAddRecipient(kind) {
        const inp = document.getElementById(`comms-${kind}-input`);
        const v = (inp?.value || '').trim();
        if (!v) return;
        if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v)) { this._commsFlash('Enter a valid email address.', 'text-danger'); return; }
        if (this._comms[kind].map((x) => x.toLowerCase()).indexOf(v.toLowerCase()) < 0) this._comms[kind].push(v);
        if (inp) inp.value = '';
        this._commsFlash('', '');
        this._renderCommsChips();
    },

    _commsFlash(msg, cls) {
        const f = document.getElementById('comms-flash');
        if (f) { f.textContent = msg; f.className = `small me-auto ${cls || 'text-secondary'}`; }
    },

    async saveComms() {
        const proj = this._commsProject;
        const btn = document.getElementById('comms-save');
        if (btn) btn.disabled = true;
        this._commsFlash('Saving…', 'text-secondary');
        const cadence = document.getElementById('comms-cadence')?.value || 'weekly';
        try {
            const res = await fetch(`api/projects/${encodeURIComponent(proj)}/comms`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    inbound: { domains: this._comms.domains },
                    outbound: {
                        notify_recipients: this._comms.notify,
                        digest_recipients: this._comms.digest,
                        cadence,
                    },
                }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                throw new Error(res.status === 403
                    ? 'You need admin on this project to change Communications.'
                    : (data.detail || data.error || `Failed (${res.status})`));
            }
            if (data.config) this.renderComms(data.config);
            this._commsFlash('Saved.', 'text-success');
        } catch (e) {
            this._commsFlash(e.message || 'Failed to save.', 'text-danger');
        } finally {
            if (btn) btn.disabled = false;
        }
    },

    async sendCommsTest(kind) {
        const proj = this._commsProject;
        this._commsFlash('Sending test…', 'text-secondary');
        try {
            const res = await fetch(`api/projects/${encodeURIComponent(proj)}/comms/test`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ kind }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                throw new Error(res.status === 403
                    ? 'You need admin on this project to send a test.'
                    : (data.detail || data.error || `Failed (${res.status})`));
            }
            const sent = (data.results || []).some((r) => r.sent);
            const to = (data.recipients || []).join(', ') || '(no recipients — set some or a global fallback)';
            this._commsFlash(sent ? `Test sent to ${to}.` : `Dry-run (SMTP not configured) — would send to ${to}.`,
                sent ? 'text-success' : 'text-secondary');
        } catch (e) {
            this._commsFlash(e.message || 'Failed to send test.', 'text-danger');
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
    // UI-12: a spend figure's provenance — provider-actual (gateway callback),
    // agent-reported (self-reported), or unattributed (landed without confidence).
    spendSourceBadge(source, bucket) {
        const conf = (bucket && bucket.confidence) || '';
        let label = 'unattributed';
        let color = 'secondary';
        if (conf === 'provider_actual' || source === 'gateway') { label = 'provider-actual'; color = 'green'; }
        else if (conf) { label = 'agent-reported'; color = 'azure'; }
        const cost = this.money((bucket && bucket.cost_usd) || 0);
        return `<span class="badge bg-${color}-lt me-1" title="${this.esc(source)} · ${this.esc(conf || 'no confidence')}">${label} ${cost}</span>`;
    },
    spendBadgesHtml(spend) {
        const bySource = (spend && spend.by_source) || {};
        const entries = Object.entries(bySource);
        if (!entries.length) return '';
        return `<div class="mt-2">${entries
            .sort((a, b) => ((b[1].cost_usd || 0) - (a[1].cost_usd || 0)))
            .map(([source, bucket]) => this.spendSourceBadge(source, bucket)).join('')}</div>`;
    },
    modelMixHtml(spend) {
        const byModel = (spend && spend.by_model) || {};
        const entries = Object.entries(byModel).sort((a, b) => ((b[1].cost_usd || 0) - (a[1].cost_usd || 0)));
        if (!entries.length) return '';
        const mix = entries.map(([model, b]) =>
            `${this.esc(model)} ${this.money(b.cost_usd || 0)} · ${this.compact(b.total_tokens || 0)} tok`).join(' &nbsp;·&nbsp; ');
        return `<div class="small text-secondary mt-1"><i class="ti ti-cpu me-1"></i>${mix}</div>`;
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
        </div>${this.spendBadgesHtml(spend)}${this.modelMixHtml(spend)}${kpiLine}`;
    },
    sessionHealthPill(health) {
        const h = health || {};
        const status = h.status || 'no_sessions';
        const colors = { healthy: 'green', warning: 'yellow', unsafe: 'red', no_sessions: 'secondary' };
        const icons = { healthy: 'shield-check', warning: 'alert-triangle', unsafe: 'shield-x', no_sessions: 'folder-off' };
        const label = status === 'no_sessions' ? 'No Work Session' : `Session ${status}`;
        const count = h.unsafe_session_count ? ` · ${h.unsafe_session_count} unsafe` : (h.warning_session_count ? ` · ${h.warning_session_count} warn` : '');
        const title = h.recommended_repair || label;
        return `<span class="badge bg-${colors[status] || 'secondary'}-lt" title="${this.esc(title)}"><i class="ti ti-${icons[status] || 'shield'} me-1"></i>${this.esc(label + count)}</span>`;
    },
    sessionHealthDetailHtml(health) {
        const h = health || {};
        const findings = h.findings || [];
        const sessions = h.active_sessions || h.latest_sessions || [];
        const findingHtml = findings.length ? `<div class="mt-2 small">${findings.slice(0, 5).map((f) =>
            `<div><span class="badge bg-${f.blocking ? 'red' : 'yellow'}-lt me-1">${this.esc(f.code || f.kind || 'finding')}</span>${this.esc(f.message || '')}${f.repair ? `<div class="text-secondary ms-2">${this.esc(f.repair)}</div>` : ''}</div>`).join('')}</div>` : '';
        const sessionHtml = sessions.length ? `<div class="mt-2 small text-secondary">${sessions.slice(0, 4).map((s) =>
            `${this.esc(s.work_session_id || '')} · ${this.esc(s.branch || 'no branch')} · ${this.esc(s.workspace_path || 'no path')}`).join('<br>')}</div>` : '';
        return `<div>${this.sessionHealthPill(h)}${findingHtml}${sessionHtml}</div>`;
    },
    controlTruthHtml(t) {
        const dep = (t && t.dependency_state) || {};
        const rat = (t && t.rationale_state) || {};
        const ident = (t && t.identity) || {};
        const health = (t && t.session_health) || {};
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
                    <div class="col-md-3"><span class="badge bg-${depClass}-lt">${this.esc(depStatus)}</span></div>
                    <div class="col-md-3"><span class="badge bg-${rationaleClass}-lt">${this.esc(rationaleStatus)}</span></div>
                    <div class="col-md-3"><span class="badge bg-${identityClass}-lt">${identityStatus}</span></div>
                    <div class="col-md-3">${this.sessionHealthPill(health)}</div>
                    <div class="col-12 small">${deps}</div>
                    ${health.status && health.status !== 'healthy' ? `<div class="col-12">${this.sessionHealthDetailHtml(health)}</div>` : ''}
                    ${flags ? `<div class="col-12 small">${flags}</div>` : ''}
                    ${suppressed}
                </div>
            </div>
        </div>`;
    },
    // NARRATE-4: CEO-voice narration block at the top of the task Details tab.
    // Fresh -> plain-English callout; stale (fingerprint moved) -> the old text, muted,
    // with an "Updating…" badge; absent -> a quiet one-liner so the surface is discoverable.
    taskNarrationHtml(t) {
        const state = (t && t.narration_state) || {};
        const text = t && t.narration;
        const raw = t && t.narration_raw;
        if (text) {
            return `<div class="mb-3">
                <div class="subheader text-secondary mb-1"><i class="ti ti-message-chatbot me-1"></i>In plain English</div>
                <div class="markdown">${this.md(text)}</div>
            </div>`;
        }
        if (raw && state.stale) {
            return `<div class="mb-3">
                <div class="subheader text-secondary mb-1"><i class="ti ti-refresh me-1"></i>In plain English · updating…</div>
                <div class="markdown text-secondary">${this.md(raw)}</div>
            </div>`;
        }
        return `<div class="text-secondary small mb-3"><i class="ti ti-message-chatbot me-1"></i>Plain-English summary will appear here once generated.</div>`;
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
    // UI-3: Agent-fleet health dock (bottom-right, floats over every tab). Replaces the
    // header pill strip. Quiet when healthy — a small pill; auto-opens a plain-language
    // list the moment an agent needs attention (can't merge / dirty worktree). Scope is
    // project-wide on the board, deliverable-scoped on the mission page. Data comes from
    // /ixp/v1/work_sessions plus each session's derived health (SESSION-8 read models).
    renderFleetDock(ctx) {
        this._dockCtx = ctx || { mode: 'project' };
        this._loadFleetDock();
    },
    async _loadFleetDock() {
        const host = document.getElementById('fleet-dock');
        if (!host) return;
        let sessions = [];
        try {
            const p = `project=${encodeURIComponent(window.PM_PROJECT || 'maxwell')}`;
            const data = await (await fetch(`/ixp/v1/work_sessions?${p}&include_expired=false`)).json();
            sessions = data.work_sessions || [];
        } catch (e) { host.innerHTML = ''; return; }
        const ctx = this._dockCtx || { mode: 'project' };
        if (ctx.mode === 'deliverable' && Array.isArray(ctx.taskIds)) {
            const ids = new Set(ctx.taskIds.map((x) => String(x).toUpperCase()));
            sessions = sessions.filter((s) => ids.has(String(s.task_id || '').toUpperCase()));
        }
        this._fleetScopeLabel = ctx.mode === 'deliverable' ? 'this deliverable' : '';
        this._renderFleetDock(sessions);
    },
    _fleetTaskTitle(taskId) {
        const id = String(taskId || '').toUpperCase();
        const t = (this.tasks || []).find((x) => String(x.task_id || '').toUpperCase() === id);
        if (t && t.title) return t.title;
        const links = (this.missionStatus && this.missionStatus.linked_tasks) || [];
        for (const l of links) {
            const d = l.task_detail || {};
            if (String(l.task_id || d.task_id || '').toUpperCase() === id && d.title) return d.title;
        }
        return taskId || 'task';
    },
    _dockReason(health) {
        const findings = (health && health.findings) || [];
        const pick = findings.filter((f) => f.blocking)[0] || findings[0] || null;
        const severity = (health && health.status) === 'unsafe' ? 'danger' : 'warning';
        return {
            severity,
            text: pick ? pick.message : (severity === 'danger' ? "Can't merge yet." : 'Needs a look.'),
            repair: (pick && pick.repair) || '',
        };
    },
    _renderFleetDock(sessions) {
        const host = document.getElementById('fleet-dock');
        if (!host) return;
        const working = sessions.length;
        if (!working) { host.innerHTML = ''; return; }   // keep the corner clean
        const proj = window.PM_PROJECT || 'maxwell';
        // "Needs attention" = genuinely blocked (unsafe): dirty worktree, conflicts, a
        // failed/blocking preflight — the things that stop a merge. A non-blocking
        // 'warning' (e.g. hasn't run preflight yet) is normal mid-work, so it folds into
        // "on track" rather than nagging on every fresh session.
        const attention = sessions.filter((s) => (s.health || {}).status === 'unsafe');
        const nAttn = attention.length;
        const worst = 'danger';
        // explicit user toggle wins; otherwise auto — open only when something needs attention
        const collapsed = this._dockCollapsed == null ? (nAttn === 0) : this._dockCollapsed;
        const anchor = 'position:fixed;right:1rem;bottom:4.25rem;z-index:1031;';
        const rerender = () => this._renderFleetDock(sessions);
        if (collapsed) {
            const dot = nAttn ? `var(--tblr-${worst})` : 'var(--tblr-success)';
            host.innerHTML = `<button id="fleet-dock-pill" class="btn btn-sm shadow-sm" style="${anchor}border-radius:999px;display:inline-flex;align-items:center;gap:8px;">
                <span style="width:8px;height:8px;border-radius:50%;background:${dot};"></span>
                <span class="fw-medium">${nAttn ? this.esc(String(nAttn)) + ' need attention' : 'Fleet clear'}</span>
                <span class="text-secondary small">· ${working} working</span>
                <i class="ti ti-chevron-up"></i></button>`;
            document.getElementById('fleet-dock-pill').addEventListener('click', () => { this._dockCollapsed = false; rerender(); });
            return;
        }
        const rows = attention.map((s) => {
            const r = this._dockReason(s.health);
            const dot = `var(--tblr-${r.severity})`;
            return `<div class="p-2 border rounded mb-2">
                <div class="d-flex align-items-start gap-2">
                    <span style="margin-top:6px;width:8px;height:8px;border-radius:50%;background:${dot};flex:none;"></span>
                    <div class="flex-fill" style="min-width:0;">
                        <div class="fw-medium text-truncate">${this.esc(this._fleetTaskTitle(s.task_id))}</div>
                        <div class="text-secondary text-truncate" style="font-size:12px;font-family:var(--tblr-font-monospace);">${this.esc(s.agent_id || '')}</div>
                        <div class="mt-1" style="font-size:13px;">${this.esc(r.text)}</div>
                        ${r.repair ? `<div class="text-secondary mt-1" style="font-size:12px;">${this.esc(r.repair)}</div>` : ''}
                        <div class="mt-2"><button class="btn btn-sm" data-dock-open="${this.esc(s.task_id)}"><i class="ti ti-arrow-up-right me-1"></i>Open task</button></div>
                    </div>
                </div></div>`;
        }).join('');
        const clean = working - nAttn;
        const scope = this._fleetScopeLabel ? ` <span class="text-secondary small">· ${this.esc(this._fleetScopeLabel)}</span>` : '';
        const body = nAttn
            ? `<div class="p-2">${rows}${clean > 0 ? `<div class="text-secondary small px-1 pb-1"><i class="ti ti-check me-1"></i>${clean} other${clean === 1 ? '' : 's'} clean and on track</div>` : ''}</div>`
            : `<div class="p-3 text-secondary small"><i class="ti ti-check me-1"></i>All ${working} agents clean and on track.</div>`;
        const attnBadge = nAttn
            ? `<span class="ms-auto small text-${worst}"><span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:currentColor" class="me-1"></span>${nAttn} need attention</span>`
            : `<span class="ms-auto small text-success"><i class="ti ti-check me-1"></i>all clear</span>`;
        host.innerHTML = `<div class="card shadow-sm" style="${anchor}width:340px;max-height:70vh;overflow:auto;">
            <div class="card-header py-2 d-flex align-items-center gap-2">
                <i class="ti ti-users-group text-secondary"></i>
                <span class="fw-medium">Agent fleet</span>
                <span class="text-secondary small">${working} working</span>${scope}
                ${attnBadge}
                <button id="fleet-dock-refresh" class="btn btn-sm btn-ghost-secondary p-1" title="Refresh"><i class="ti ti-refresh"></i></button>
                <button id="fleet-dock-min" class="btn btn-sm btn-ghost-secondary p-1" title="Collapse"><i class="ti ti-chevron-down"></i></button>
            </div>
            ${body}</div>`;
        host.querySelectorAll('[data-dock-open]').forEach((b) =>
            b.addEventListener('click', () => this.openTask(b.getAttribute('data-dock-open'), proj)));
        document.getElementById('fleet-dock-min').addEventListener('click', () => { this._dockCollapsed = true; rerender(); });
        document.getElementById('fleet-dock-refresh').addEventListener('click', () => this._loadFleetDock());
    },
    // UI-3: per-task Work Sessions panel (Dev tab) — who holds which worktree, on what
    // branch, clean or dirty. Sits beside the runner panel.
    workSessionsPanelHtml(t) {
        return `<div class="card mb-3" id="work-sessions-panel" data-task-id="${this.esc(t.task_id)}">
            <div class="card-header py-2">
                <div class="d-flex align-items-center gap-2">
                    <i class="ti ti-git-branch text-azure"></i>
                    <span class="fw-semibold">Work sessions</span>
                    <span id="work-sessions-count" class="badge bg-secondary-lt">loading</span>
                </div>
            </div>
            <div id="work-sessions-body" class="card-body py-3">
                <div class="text-secondary small">Loading work sessions…</div>
            </div>
        </div>`;
    },
    async _loadWorkSessions(taskId) {
        const body = document.getElementById('work-sessions-body');
        const count = document.getElementById('work-sessions-count');
        if (!body) return;
        let data;
        try {
            const q = `project=${encodeURIComponent(window.PM_PROJECT || 'maxwell')}&task_id=${encodeURIComponent(taskId)}&include_expired=true`;
            data = await (await fetch(`/ixp/v1/work_sessions?${q}`)).json();
        } catch (e) {
            body.innerHTML = `<div class="text-danger small">Work sessions unavailable: ${this.esc(e.message)}</div>`;
            if (count) { count.className = 'badge bg-red-lt'; count.textContent = 'error'; }
            return;
        }
        const sessions = data.work_sessions || [];
        if (count) {
            count.className = sessions.length ? 'badge bg-azure-lt' : 'badge bg-secondary-lt';
            count.textContent = `${sessions.length}`;
        }
        if (!sessions.length) {
            body.innerHTML = `<div class="text-secondary small">No Work Session is bound to this task yet.</div>`;
            return;
        }
        body.innerHTML = `<div class="table-responsive"><table class="table table-sm mb-0 align-middle">
            <thead><tr><th>Agent</th><th>Branch</th><th>Workspace</th><th>State</th></tr></thead>
            <tbody>${sessions.map((s) => this._workSessionRow(s)).join('')}</tbody>
        </table></div>`;
    },
    _workSessionRow(s) {
        const health = s.health || {};
        const dirty = (s.dirty_status || 'unknown').toLowerCase();
        const dirtyColor = dirty === 'clean' ? 'green' : (dirty === 'dirty' ? 'yellow' : 'secondary');
        const life = (s.status || '').toLowerCase();
        const lifeChip = (life && life !== 'active' && life !== 'proposed')
            ? `<span class="badge bg-${life === 'completed' ? 'green' : 'red'}-lt ms-1">${this.esc(life)}</span>` : '';
        const path = s.worktree_path || s.clone_path || (health.workspace || {}).path || '';
        const branch = s.branch || (health.workspace || {}).branch || '';
        const chips = `<span class="badge bg-${dirtyColor}-lt">${this.esc(dirty)}</span>${lifeChip} ${this.sessionHealthPill(health)}`;
        return `<tr>
            <td><div class="text-truncate" style="max-width:150px" title="${this.esc(s.agent_id || '')}">${this.esc(s.agent_id || '—')}</div><div class="font-monospace text-secondary" style="font-size:11px">${this.esc(s.repo_role || '')}</div></td>
            <td class="font-monospace small text-truncate" style="max-width:160px" title="${this.esc(branch)}">${this.esc(branch || '—')}</td>
            <td class="font-monospace small text-truncate" style="max-width:200px" title="${this.esc(path)}">${this.esc(path || '—')}</td>
            <td>${chips}</td>
        </tr>`;
    },
    // UI-3: merge-gate verdict in plain words with a Re-check button. Semantic colors
    // (green/amber/red) are the point of this surface. Re-check POSTs merge_gate, which
    // records an audited merge.gate event — so it is operator-triggered, not auto-run.
    mergeGatePanelHtml(t) {
        return `<div class="card mb-3" id="merge-gate-panel" data-task-id="${this.esc(t.task_id)}">
            <div class="card-header py-2">
                <div class="d-flex align-items-center gap-2">
                    <i class="ti ti-git-merge text-azure"></i>
                    <span class="fw-semibold">Merge gate</span>
                    <span id="merge-gate-verdict" class="badge bg-secondary-lt">not checked</span>
                    <button id="merge-gate-recheck" class="btn btn-sm btn-outline-secondary ms-auto"><i class="ti ti-refresh me-1"></i>Re-check</button>
                </div>
            </div>
            <div id="merge-gate-body" class="card-body py-3">
                <div class="text-secondary small">Re-check evaluates whether this branch can merge under code_strict.</div>
            </div>
        </div>`;
    },
    _initMergeGate(taskId) {
        const btn = document.getElementById('merge-gate-recheck');
        if (btn) btn.addEventListener('click', () => this._loadMergeGate(taskId));
    },
    async _loadMergeGate(taskId) {
        const body = document.getElementById('merge-gate-body');
        const verdict = document.getElementById('merge-gate-verdict');
        if (!body) return;
        body.innerHTML = `<div class="text-secondary small">Checking merge gate…</div>`;
        if (verdict) { verdict.className = 'badge bg-secondary-lt'; verdict.textContent = 'checking…'; }
        let data;
        try {
            const res = await fetch('/ixp/v1/merge_gate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ project: window.PM_PROJECT || 'maxwell', task_id: taskId }),
            });
            data = await res.json();
            if (!res.ok && !data.status) throw new Error(data.error || data.detail || `HTTP ${res.status}`);
        } catch (e) {
            body.innerHTML = `<div class="text-danger small">Merge gate check failed: ${this.esc(e.message)}</div>`;
            if (verdict) { verdict.className = 'badge bg-red-lt'; verdict.textContent = 'error'; }
            return;
        }
        const ok = data.ok || data.status === 'pass';
        if (verdict) {
            verdict.className = `badge bg-${ok ? 'green' : 'red'}-lt`;
            verdict.textContent = ok ? 'ready' : 'blocked';
        }
        const findings = data.findings || [];
        const blocking = findings.filter((f) => f.blocking);
        const warnings = findings.filter((f) => !f.blocking);
        if (ok && !findings.length) {
            body.innerHTML = `<div class="text-green"><i class="ti ti-circle-check me-1"></i>No blockers — this branch satisfies the merge gate.</div>`;
            return;
        }
        const render = (list, color) => list.map((f) =>
            `<div class="mb-2"><span class="badge bg-${color}-lt me-1">${this.esc(f.code || f.failure_class || 'finding')}</span>${this.esc(f.message || '')}${f.repair ? `<div class="small text-secondary ms-2">${this.esc(f.repair)}</div>` : ''}</div>`).join('');
        body.innerHTML = `${blocking.length ? `<div class="fw-semibold text-red mb-2">Blocked</div>${render(blocking, 'red')}` : ''}${warnings.length ? `<div class="fw-semibold text-orange mb-2 ${blocking.length ? 'mt-2' : ''}">Warnings</div>${render(warnings, 'yellow')}` : ''}`;
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
    externalCiDetail(t) {
        const ci = (t && t.external_ci) || {};
        const gate = ci.gate || {};
        const status = ci.status || 'missing';
        const cls = status === 'passed' ? 'green' : (status === 'failed' ? 'red' : (status === 'pending' ? 'yellow' : 'secondary'));
        const label = gate.required ? `External CI ${status}` : (ci.run_count ? `External CI ${status}` : 'none');
        const latest = ci.latest || {};
        const bits = [`<span class="badge bg-${cls}-lt"><i class="ti ti-cloud-check me-1"></i>${this.esc(label)}</span>`];
        const runUrl = ci.run_url || latest.run_url || '';
        if (runUrl) bits.push(`<a class="small" href="${this.esc(runUrl)}" target="_blank" rel="noopener">run</a>`);
        if (ci.source_sha) bits.push(`<span class="font-monospace small">${this.esc(String(ci.source_sha).slice(0, 12))}</span>`);
        const sourceRepo = ci.source_repo || latest.source_repo || '';
        const ciRepo = ci.ci_repo || latest.ci_repo || latest.mirror_repo || '';
        const context = ci.status_context || latest.status_context || '';
        if (sourceRepo || ciRepo || context) {
            const proof = `${sourceRepo || 'source'} -> ${ciRepo || 'ci'}${context ? ' · ' + context : ''}`;
            bits.push(`<span class="text-secondary small">${this.esc(proof)}</span>`);
        }
        if (gate.required && !ci.passed) bits.push(`<span class="text-danger small">${this.esc(gate.message || 'required')}</span>`);
        return bits.join(' ');
    },
    publicationDetail(t) {
        const pub = (t && t.publication) || {};
        const gate = pub.gate || {};
        const status = pub.status || 'missing';
        const cls = status === 'published' ? 'green' : (status === 'failed' || status === 'stale' ? 'red' : (status === 'unknown' ? 'yellow' : 'secondary'));
        const label = gate.required ? `Publication ${status}` : (pub.total_publication_count ? `Publication ${status}` : 'none');
        const latest = pub.latest || {};
        const bits = [`<span class="badge bg-${cls}-lt"><i class="ti ti-upload me-1"></i>${this.esc(label)}</span>`];
        const artifactUrl = pub.artifact_url || latest.artifact_url || '';
        if (artifactUrl) bits.push(`<a class="small" href="${this.esc(artifactUrl)}" target="_blank" rel="noopener">artifact</a>`);
        if (pub.source_sha) bits.push(`<span class="font-monospace small">${this.esc(String(pub.source_sha).slice(0, 12))}</span>`);
        const publicRepo = pub.public_repo || latest.public_repo || '';
        const publicRef = pub.public_ref || latest.public_ref || '';
        if (publicRepo || publicRef) bits.push(`<span class="text-secondary small">${this.esc(`${publicRepo || 'public'}${publicRef ? ' · ' + publicRef : ''}`)}</span>`);
        if (gate.required && !pub.passed) bits.push(`<span class="text-danger small">${this.esc(gate.message || 'required')}</span>`);
        return bits.join(' ');
    },
    projectContextHtml() {
        // Removed from the exec view: the "Project authority & repo roles" card
        // (Done/CI/publication provenance internals + a boards/missions strip now
        // redundant with the header deliverable switcher). Per-task provenance still
        // lives in the task detail via taskProjectContextHtml().
        return '';
    },
    taskProjectContextHtml(t) {
        const ctx = (t && t.project_context) || this.projectContext;
        if (!ctx) return '';
        const guide = ctx.repo_role_guide || {};
        const crumb = (ctx.hierarchy_breadcrumb || []).map((c) => {
            const label = c.title || c.label || c.id || c.level;
            return `<span class="badge bg-secondary-lt me-1">${this.esc(c.level)} · ${this.esc(label || '—')}</span>`;
        }).join('');
        const links = (ctx.deliverable_links || []).map((l) =>
            `<span class="badge bg-purple-lt me-1"><i class="ti ti-package me-1"></i>${this.esc(l.deliverable_title || l.deliverable_id)}</span>`
        ).join('') || '<span class="text-secondary small">not linked to a deliverable</span>';
        const doneRepo = ((guide.done_authority || {}).repo) || '—';
        const ciRepo = ((guide.ci_verification || {}).repo) || '—';
        const pubRepo = ((guide.publication_evidence || {}).repo) || '—';
        return `<div class="subheader mb-2">Project context</div>
            <div class="datagrid mb-3">
                <div class="datagrid-item"><div class="datagrid-title">Hierarchy</div><div class="datagrid-content">${crumb || '—'}</div></div>
                <div class="datagrid-item"><div class="datagrid-title">Done repo</div><div class="datagrid-content"><code>${this.esc(doneRepo)}</code></div></div>
                <div class="datagrid-item"><div class="datagrid-title">CI repo</div><div class="datagrid-content"><code>${this.esc(ciRepo)}</code></div></div>
                <div class="datagrid-item"><div class="datagrid-title">Public mirror</div><div class="datagrid-content"><code>${this.esc(pubRepo)}</code></div></div>
                <div class="datagrid-item"><div class="datagrid-title">Deliverable links</div><div class="datagrid-content">${links}</div></div>
            </div>`;
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
        this.renderFleetDock({ mode: 'project' });
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
        // HARDEN-39: only refresh the Tasks view if it's on-screen; otherwise it
        // renders fresh (with signals) the next time its tab is shown.
        if (document.getElementById('tab-epics')?.classList.contains('active')) this.renderTasks();
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
        // Timeline is now a nested sub-pane of the Plan hub — it can keep its `active`
        // class while the Plan hub itself is hidden. offsetParent is null for anything
        // not actually laid out, so it also gates on the hub being the open top tab.
        const p = document.getElementById('tab-gantt');
        return !!p && p.classList.contains('active') && p.offsetParent !== null;
    },

    // HARDEN-38: lazily inject a <script> once and resolve when loaded, so the
    // ~1MB Mermaid (Mission tab) and ~500KB ApexCharts (Gantt tab) stay off the
    // initial page load until their tab is actually opened.
    APEXCHARTS_SRC: '/vendor/apexcharts/apexcharts.min.js',
    MERMAID_SRC: 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js',
    // ELK is the Mermaid layout engine that gives the dependency map orthogonal
    // routing + far tidier packing of disconnected chains than dagre. It ships
    // ESM-only (no UMD build), so it can't go through _ensureScript's <script>
    // tag — we dynamic-import() and registerLayoutLoaders() it once, and fall
    // back to dagre if the import fails.
    ELK_SRC: 'https://cdn.jsdelivr.net/npm/@mermaid-js/layout-elk@0/dist/mermaid-layout-elk.esm.min.mjs',
    _ensureScript(src, timeoutMs = 15000) {
        this._scriptPromises = this._scriptPromises || {};
        if (this._scriptPromises[src]) return this._scriptPromises[src];
        this._scriptPromises[src] = new Promise((resolve, reject) => {
            const s = document.createElement('script');
            s.src = src; s.async = true;
            // A stalled CDN response used to hang the awaiting render forever (no timeout);
            // reject so callers can fall back / show a message instead of spinning.
            const timer = setTimeout(() => { delete this._scriptPromises[src]; reject(new Error('timeout loading ' + src)); }, timeoutMs);
            s.onload = () => { clearTimeout(timer); resolve(); };
            s.onerror = () => { clearTimeout(timer); delete this._scriptPromises[src]; reject(new Error('failed to load ' + src)); };
            document.head.appendChild(s);
        });
        return this._scriptPromises[src];
    },

    // HARDEN-39: build the heavy off-screen tabs the first time (and each time)
    // they're shown, instead of all upfront on load. Tasks/Epics are already wired
    // in wireEvents; this covers Board, the tables (Milestones/Decisions/Risks all
    // come from renderTables), and Pulse. These also re-render from mutations/filter
    // changes, so a deferred tab never shows stale data.
    _wireLazyTabs() {
        const paneRender = {
            'tab-board': 'renderBoard',
            'tab-plan': 'renderTables',
            'tab-decisions': 'renderTables',
            'tab-risks': 'renderTables',
        };
        Object.entries(paneRender).forEach(([pane, fn]) => {
            document.querySelectorAll(`a[href="#${pane}"]`).forEach((tab) =>
                tab.addEventListener('shown.bs.tab', () => this[fn]()));
        });
    },

    // A hash deep-link (resolved by the inline script during page parse) can leave a
    // non-default top tab active before init() wired the lazy renderers, so render
    // whatever top tab ended up active. #tab-exec is already rendered on boot;
    // #tab-mission is driven by _missionDeliverableFromUrl.
    _renderActiveTop() {
        const active = document.querySelector('#main-nav .nav-link.active');
        const href = active ? active.getAttribute('href') : '#tab-exec';
        if (href === '#tab-plan-hub') this._renderPlanActive();
        else if (href === '#tab-inbox-hub') { this.initInbox(); this.renderTables(); }
        else if (href === '#tab-ask') this.initAsk();
    },

    // Render whichever Plan sub-view is active — called when the Plan hub top tab opens,
    // since the nested sub-pane's own shown.bs.tab (which _wireLazyTabs/setupGantt hook)
    // does not fire on hub reveal. Defaults to Board.
    async _renderPlanActive() {
        const hub = document.getElementById('tab-plan-hub');
        const active = hub && hub.querySelector('.tk-subnav .nav-link.active');
        const href = active ? active.getAttribute('href') : '#tab-epics';
        if (href === '#tab-board') { this.renderBoard(); return; }
        if (href === '#tab-plan') { this.renderTables(); return; }
        if (href === '#tab-gantt') {
            try { await this._ensureScript(this.APEXCHARTS_SRC); } catch (e) { /* renderGantt guards on window.ApexCharts */ }
            this.renderGantt(); return;
        }
        this.renderEpics();
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
            tab.addEventListener('shown.bs.tab', async () => {
                try { await this._ensureScript(this.APEXCHARTS_SRC); } catch (e) { /* renderGantt guards on window.ApexCharts */ }
                this.renderGantt();
            }));
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

    async openTask(id, project) {
        let t = this.tasks.find((x) => x.task_id === id);
        if (!t && !project) return;
        const proj = (project || window.PM_PROJECT || 'maxwell').trim();
        try {
            const fresh = await (await fetch(`api/tasks/${encodeURIComponent(id)}?project=${encodeURIComponent(proj)}`)).json();
            if (fresh && fresh.task_id) t = Object.assign({}, t || {}, fresh);
        } catch (e) { /* fall back to in-memory task */ }
        if (!t) return;
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
                    ${this.taskNarrationHtml(t)}
                    <div class="subheader mb-2">Economics</div>
                    ${this.tallyDetailHtml(tally)}
                    ${this.taskProjectContextHtml(t)}
                    <div class="subheader mb-2">Properties</div>
                    <div class="datagrid mb-3">
                        <div class="datagrid-item"><div class="datagrid-title">Status</div>
                            <div class="datagrid-content"><select id="details-status" class="form-select form-select-sm" style="max-width:200px">${statusOpts}</select></div></div>
                        ${dg('Done provenance', provenanceHtml)}
                        ${dg('External CI', this.externalCiDetail(t))}
                        ${dg('Publication', this.publicationDetail(t))}
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
                    <p class="text-secondary">Dispatch queues this task for the agent fleet: a work-capable agent host claims it, works it in an isolated worktree, and opens a PR on a <code>claude/</code> branch — it never merges or writes to your systems on its own. If no work host is online yet, it stays queued until one is.</p>
                    ${t.is_blocking ? `<div class="alert alert-warning d-flex" role="alert"><i class="ti ti-shield-lock me-2 mt-1"></i><div><span class="fw-bold">Human-gated.</span> This task is blocking — a maintainer must approve both the dispatch and the resulting PR before anything merges.</div></div>` : ''}
                    ${this.controlTruthHtml(t)}
                    ${this.workSessionsPanelHtml(t)}
                    ${this.mergeGatePanelHtml(t)}
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
        this._loadWorkSessions(t.task_id);
        this._initMergeGate(t.task_id);
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
        const proj = window.PM_PROJECT || 'maxwell';
        if (!window.confirm(`Dispatch ${id} to the fleet? A work-capable agent host claims it, works it on a claude/ branch, and posts a PR link here — it never touches main.`)) return;
        flash('Queuing a work session…');
        let data;
        try {
            const res = await fetch(`api/tasks/${encodeURIComponent(id)}/dispatch`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project: proj }) });
            data = await res.json();
        } catch (e) { return flash('Dispatch failed: ' + e.message, 'danger'); }
        if (!data.dispatched) return flash('Dispatch failed: ' + (data.error || data.detail || 'unknown'), 'danger');
        if (!data.work_hosts_online) flash(`Queued (wake ${data.wake_id}) — no work host is online yet, so it waits until one is.`, 'warning');
        else flash(`Queued (wake ${data.wake_id}) — a work host will claim it and open a PR.`, 'green');
        this._loadDispatch(id);   // render the live panel (queued → running → Open PR); it self-refreshes
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
        const proj = window.PM_PROJECT || 'maxwell';
        let d;
        try { d = await (await fetch(`api/tasks/${encodeURIComponent(id)}/dispatch/latest?project=${encodeURIComponent(proj)}`)).json(); } catch (e) { return; }
        const st = d && d.status;
        if (!st || st === 'none') { el.innerHTML = ''; return; }
        const M = {
            queued: ['Queued for the fleet', 'yellow'],
            claiming: ['Claimed — starting…', 'azure'],
            running: ['Working…', 'azure'],
            pr: ['PR ready', 'green'],
        };
        const [label, color] = M[st] || [st, 'secondary'];
        const active = st === 'queued' || st === 'claiming' || st === 'running';
        const pr = d.pr_url ? `<a href="${this.esc(d.pr_url)}" target="_blank" class="btn btn-success btn-sm"><i class="ti ti-git-pull-request me-1"></i>Open PR ↗</a>` : '';
        const who = d.agent_id ? ` <span class="text-secondary small">${this.esc(d.agent_id)}</span>` : '';
        el.innerHTML = `
            <div class="card"><div class="card-body py-2">
                <div class="d-flex align-items-center gap-2 flex-wrap">
                    <i class="ti ti-robot text-azure"></i><strong>Fleet dispatch</strong>${who}
                    <span class="badge bg-${color}-lt">${this.esc(label)}</span>
                    ${st === 'running' || st === 'claiming' ? '<span class="spinner-border spinner-border-sm text-azure"></span>' : ''}
                    <span class="ms-auto"></span>${pr}
                </div>
                ${st === 'queued' ? '<div class="small text-secondary mt-1">Queued — a work-capable agent host will claim it. If nothing picks it up, no work host is online for this lane yet.</div>' : ''}
                ${st === 'claiming' ? '<div class="small text-secondary mt-1">A host claimed the wake and is starting the session.</div>' : ''}
                ${st === 'running' ? '<div class="small text-secondary mt-1">Working now — the Open PR button appears here when it opens a PR.</div>' : ''}
                ${st === 'pr' ? '<div class="small text-secondary mt-1">Next: open the PR, review the diff, and merge it on GitHub (or comment back here).</div>' : ''}
            </div></div>`;
        if (active) setTimeout(() => { if (this._dispatchPollId === id) this._loadDispatch(id); }, 7000);
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
        // Show the pending count on the top-level Inbox tab (the hub), not the
        // Action Queue sub-pill, so it's visible without opening the hub.
        const tab = document.getElementById('toptab-inbox') || document.querySelector('a[href="#tab-inbox"]');
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
        const heroSummary = it.summary ? `<div class="mb-4">
                <div class="subheader text-secondary mb-1"><i class="ti ti-robot me-1"></i>Maxwell · agent summary</div>
                <div class="markdown small">${this.md(it.summary)}</div></div>` : '';
        const safety = `<div class="small text-secondary d-flex gap-2 mb-1"><i class="ti ti-info-circle mt-1"></i>
                <div>Nothing is applied until you confirm. <strong class="text-body">→ Done</strong> closes are held until you confirm them with evidence.</div></div>`;

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
            this.projectContext = await this.fetchProjectContext();
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
                            <div class="empty-action"><a href="#" class="btn btn-primary" onclick="event.preventDefault(); var b=document.getElementById('digest-gen'); if (b) b.click();"><i class="ti ti-sparkles me-1"></i>Generate digest</a></div>
                        </div>
                    </div>
                </div>
            </div>`;

        el.innerHTML = (this.projectContext ? this.projectContextHtml(this.projectContext) : '') +
            kpiStrip + `<div class="row row-cards">${myWork}${rightRail}</div>`;

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

    _missionDeliverableFromUrl() {
        try {
            const u = new URL(window.location.href);
            const d = (u.searchParams.get('deliverable') || u.searchParams.get('mission') || '').trim();
            if (d) this.selectedDeliverableId = d;
            if (u.hash === '#tab-mission' || d) {
                // Target the TOP tab (in .nav-tabs) — that's the element Bootstrap fires
                // shown.bs.tab on, which drives refreshMissionPage. The sidebar link shares
                // href="#tab-mission" and would otherwise win document.querySelector.
                const tab = document.querySelector('#toptab-mission');
                if (tab && window.bootstrap) window.bootstrap.Tab.getOrCreateInstance(tab).show();
            }
        } catch (e) { /* ignore */ }
    },

    async _preloadDeliverableDefault() {
        // Overview is the landing tab. We still pre-load deliverables and a default
        // selection so the header switcher and #tab-mission deep links resolve — but we
        // no longer auto-switch the active tab to the Deliverable board on boot.
        try {
            const u = new URL(window.location.href);
            if (u.hash === '#tab-mission' || u.searchParams.get('deliverable') || u.searchParams.get('mission')) return;
            await this.loadDeliverables();
            if (this.deliverables.length && !this.selectedDeliverableId) this.selectedDeliverableId = this.deliverables[0].id;
        } catch (e) { /* ignore */ }
    },

    _setMissionDeliverableInUrl(id) {
        try {
            const u = new URL(window.location.href);
            if (id) {
                u.searchParams.set('deliverable', id);
                // Deliverables are project-scoped; a copied link must carry the
                // project or it resolves against the visitor's last-used project.
                if (window.PM_PROJECT) u.searchParams.set('project', window.PM_PROJECT);
            } else {
                u.searchParams.delete('deliverable');
            }
            window.history.replaceState({}, '', u.toString());
        } catch (e) { /* ignore */ }
    },

    async loadDeliverables() {
        const res = await fetch('api/deliverables');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        this.deliverables = data.deliverables || [];
    },

    async loadMissionStatus(deliverableId) {
        const id = (deliverableId || '').trim();
        if (!id) { this.missionStatus = null; return null; }
        // no-store: the live poll must see the current server state every tick, never a
        // cached response — otherwise node colours only change on a hard refresh.
        const res = await fetch(`api/deliverables/${encodeURIComponent(id)}/mission_status`, { cache: 'no-store' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
        this.missionStatus = data;
        return data;
    },

    async loadDependencyGraph(deliverableId) {
        const id = (deliverableId || '').trim();
        if (!id) { this.missionGraph = null; return null; }
        const res = await fetch(`api/deliverables/${encodeURIComponent(id)}/dependency_graph`, { cache: 'no-store' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
        this.missionGraph = data;
        return data;
    },

    async generateMissionBrief() {
        const id = (this.selectedDeliverableId || '').trim();
        if (!id) return;
        const el = document.getElementById('mission-page');
        if (el) el.insertAdjacentHTML('afterbegin', '<div class="alert alert-info mb-3" id="mission-brief-busy">Generating live brief…</div>');
        try {
            const res = await fetch(`api/deliverables/${encodeURIComponent(id)}/mission_brief`, { method: 'POST' });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
            this.missionStatus = data.mission_status || data;
            this.renderMissionPage();
        } catch (e) {
            alert(`Could not generate mission brief: ${e.message}`);
        } finally {
            document.getElementById('mission-brief-busy')?.remove();
        }
    },

    // NARRATE-4: CEO-voice deliverable header — the 3-4 sentence plain-English summary that
    // sits at the very top of the mission view, above the dependency map. Fresh -> callout;
    // stale (linked-task fingerprint moved) -> old text muted with an "Updating…" badge.
    // Absent -> nothing (the structured "Live product brief" below still covers it).
    _missionCeoHeaderHtml(s) {
        const state = s.ceo_narrative_state || {};
        const text = s.ceo_narrative;
        const raw = s.ceo_narrative_raw;
        if (text) {
            return `<div class="mb-4">
                <div class="subheader text-secondary mb-1"><i class="ti ti-message-chatbot me-1"></i>In plain English</div>
                <div class="markdown">${this.md(text)}</div>
            </div>`;
        }
        if (raw && state.stale) {
            return `<div class="mb-4">
                <div class="subheader text-secondary mb-1"><i class="ti ti-refresh me-1"></i>In plain English · updating…</div>
                <div class="markdown text-secondary">${this.md(raw)}</div>
            </div>`;
        }
        return '';
    },

    _missionBriefHtml(s) {
        const brief = s.mission_brief || {};
        const state = s.narrative_state || {};
        let html = '';
        if (state.stale) {
            html += `<div class="mb-3 small text-warning"><i class="ti ti-alert-triangle me-1"></i><span class="fw-semibold">Brief may be stale.</span> <span class="text-secondary">${this.esc(state.message || '')}${state.flags?.length ? ' Flags: ' + this.esc(state.flags.join(', ')) : ''}</span></div>`;
        }
        const text = brief.summary_markdown || s.narrative;
        if (!text) return html;
        const citeCount = (brief.citations || []).length;
        html += `<div class="card mb-4"><div class="card-body">
            <div class="d-flex align-items-center mb-2"><div class="subheader mb-0">Live product brief</div>
            <span class="text-secondary small ms-2">· ${this.esc(s.narrative_source || 'generated')}</span>
            ${citeCount ? `<span class="text-secondary small ms-auto">${citeCount} citations</span>` : ''}</div>
            <p class="mb-2 small text-secondary">${this.esc(brief.honesty_note || '')}</p>
            <div class="mb-0" style="white-space:pre-wrap">${this.esc(text)}</div></div></div>`;
        return html;
    },

    async refreshMissionPage() {
        const el = document.getElementById('mission-page');
        const picker = document.getElementById('mission-deliverable-picker');
        if (!el) return;
        // Warm the Mermaid bundle in parallel with the data fetch so the dependency
        // map isn't waiting on a cold ~1MB CDN download after the data is already in.
        this._ensureScript(this.MERMAID_SRC).catch(() => {});
        el.innerHTML = '<div class="text-secondary small">Loading mission…</div>';
        try { await this.loadDeliverables(); }
        catch (e) {
            el.innerHTML = `<div class="alert alert-danger mb-0">Could not load deliverables: ${this.esc(e.message)}</div>`;
            return;
        }
        if (picker) {
            const cur = this.selectedDeliverableId;
            picker.innerHTML = this.deliverables.length
                ? this.deliverables.map((d) =>
                    `<option value="${this.esc(d.id)}"${d.id === cur ? ' selected' : ''}>${this.esc(d.title || d.id)}</option>`).join('')
                : '<option value="">No deliverables yet</option>';
            if (!cur && this.deliverables.length) {
                this.selectedDeliverableId = this.deliverables[0].id;
                picker.value = this.selectedDeliverableId;
            }
        }
        this._syncHeaderDeliverable();
        if (!this.selectedDeliverableId) {
            el.innerHTML = `<div class="empty py-5"><div class="empty-icon"><i class="ti ti-target-arrow"></i></div>
                <p class="empty-title">No mission deliverables</p>
                <p class="empty-subtitle text-secondary">Create a deliverable under this project to open a live mission cockpit.</p></div>`;
            return;
        }
        try {
            await Promise.all([
                this.loadMissionStatus(this.selectedDeliverableId),
                this.loadDependencyGraph(this.selectedDeliverableId),
                this.loadBreakdownProposals(this.selectedDeliverableId),
                this.loadKpisAndOutcomes(),
            ]);
            this._setMissionDeliverableInUrl(this.selectedDeliverableId);
            this.renderMissionPage();
        } catch (e) {
            el.innerHTML = `<div class="alert alert-danger mb-0">Could not load mission status: ${this.esc(e.message)}</div>`;
        }
    },

    _missionBadge(status, map, fallback) {
        const key = String(status || '').toLowerCase().replace(/\s+/g, '_');
        const color = map[key] || fallback || 'secondary';
        return `<span class="badge bg-${color}-lt text-uppercase">${this.esc(String(status || 'unknown').replace(/_/g, ' '))}</span>`;
    },

    _missionConfidence(conf) {
        if (conf == null || conf === '') return '<span class="text-secondary">—</span>';
        const pct = Math.round(Number(conf) * 100);
        const color = pct >= 70 ? 'green' : (pct >= 40 ? 'yellow' : 'red');
        return `<span class="badge bg-${color}-lt">${pct}% confidence</span>`;
    },

    _missionKv(obj) {
        if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return '<div class="text-secondary small">—</div>';
        const entries = Object.entries(obj);
        if (!entries.length) return '<div class="text-secondary small">—</div>';
        return `<div class="datagrid">${entries.map(([k, v]) =>
            `<div class="datagrid-item"><div class="datagrid-title">${this.esc(k)}</div><div class="datagrid-content">${this.esc(typeof v === 'object' ? JSON.stringify(v) : v)}</div></div>`).join('')}</div>`;
    },

    _missionRecentChanges(linkedTasks) {
        const events = [];
        (linkedTasks || []).forEach((link) => {
            const d = link.task_detail || link.task || {};
            if (d.error) return;
            const gs = d.git_state || {};
            if (gs.merged_at) events.push({ ts: Number(gs.merged_at), kind: 'merge', text: 'Merged with provenance', taskId: d.task_id, projectId: link.project_id });
            if (gs.pushed_at) events.push({ ts: Number(gs.pushed_at), kind: 'push', text: 'Branch pushed', taskId: d.task_id, projectId: link.project_id });
        });
        events.sort((a, b) => b.ts - a.ts);
        const top = events.filter((e) => e.ts > 0).slice(0, 8);
        if (!top.length) return '<div class="text-secondary small">No recent git/provenance signals on linked tasks yet.</div>';
        return `<div class="list-group list-group-flush">${top.map((e) =>
            `<div class="list-group-item px-0"><div class="d-flex gap-2"><span class="badge bg-secondary-lt">${this.esc(e.kind)}</span>
            <div><div>${this.esc(e.text)} · <a href="#" data-linked-task="${this.esc(e.taskId)}" data-linked-project="${this.esc(e.projectId)}">${this.esc(e.taskId)}</a></div>
            <div class="text-secondary small">${new Date(e.ts * 1000).toLocaleString()}</div></div></div></div>`).join('')}</div>`;
    },

    _missionEconomicsHtml(economics) {
        const econ = economics || {};
        const totals = econ.totals || {};
        const combined = totals.combined || {};
        const proven = totals.proven || {};
        const inReview = totals.in_review || {};
        const spend = combined.spend || {};
        const unit = combined.unit_cost || {};
        const metric = (label, value, sub, icon) => `<div class="col-6 col-lg-3"><div class="card card-sm"><div class="card-body p-3">
            <div class="subheader"><i class="ti ti-${icon} me-1"></i>${label}</div>
            <div class="h2 mb-0 mt-1">${value}</div>
            <div class="text-secondary small">${sub}</div>
        </div></div></div>`;
        const taskRows = (econ.by_task || []).length ? (econ.by_task || []).map((t) => {
            const bucket = t.proof_bucket === 'proven' ? 'green' : (t.proof_bucket === 'in_review' ? 'azure' : 'secondary');
            return `<tr><td><a href="#" data-linked-task="${this.esc(t.task_id)}" data-linked-project="${this.esc(t.project_id)}">${this.esc(t.task_id)}</a></td>
                <td>${this.esc(t.title || '')}</td><td><span class="badge bg-${bucket}-lt">${this.esc(t.proof_bucket || 'other')}</span></td>
                <td class="text-end">${this.money((t.spend || {}).cost_usd || 0)}</td>
                <td class="text-end">${this.compact((t.outcomes || {}).verified || 0)}</td>
                <td class="text-end">${this.money((t.unit_cost || {}).cost_per_verified_outcome)}</td></tr>`;
        }).join('') : '<tr><td colspan="6" class="text-secondary text-center py-3">No linked task economics yet.</td></tr>';
        const msRows = (econ.by_milestone || []).length ? (econ.by_milestone || []).map((m) => `<tr>
            <td>${this.esc(m.title || m.milestone_id || 'Unassigned')}</td>
            <td class="text-end">${this.money(((m.combined || {}).spend || {}).cost_usd || 0)}</td>
            <td class="text-end">${this.money(((m.proven || {}).spend || {}).cost_usd || 0)}</td>
            <td class="text-end">${this.money(((m.in_review || {}).spend || {}).cost_usd || 0)}</td>
            <td class="text-end">${this.compact((m.combined || {}).verified_outcomes || 0)}</td>
        </tr>`).join('') : '<tr><td colspan="5" class="text-secondary text-center py-3">No milestone economics yet.</td></tr>';
        const kpiRows = (econ.kpis || []).length ? (econ.kpis || []).map((k) => `<tr>
            <td><span class="fw-semibold">${this.esc(k.name || k.kpi_id || 'KPI')}</span><div class="text-secondary small">${this.esc(k.project_id || '')}</div></td>
            <td class="text-end">${this.compact(k.verified_contribution || 0)} ${this.esc(k.unit || '')}</td>
            <td class="text-end">${this.money((k.spend || {}).cost_usd || 0)}</td>
            <td class="text-end">${this.money((k.unit_cost || {}).cost_per_contribution_unit)}</td>
        </tr>`).join('') : '<tr><td colspan="4" class="text-secondary text-center py-3">No KPI movement yet.</td></tr>';
        return `<div class="card mb-4"><div class="card-header"><h3 class="card-title"><i class="ti ti-cash me-2"></i>Mission economics</h3></div><div class="card-body">
            <div class="row row-cards mb-3">
                ${metric('Total spend', this.money(spend.cost_usd || 0), `${this.compact(spend.total_tokens || 0)} tokens`, 'cash')}
                ${metric('Proven spend', this.money((proven.spend || {}).cost_usd || 0), `${this.compact(proven.verified_outcomes || 0)} verified outcomes`, 'circle-check')}
                ${metric('In-review spend', this.money((inReview.spend || {}).cost_usd || 0), 'unproven / in-flight work', 'eye-check')}
                ${metric('Cost / outcome', this.money(unit.cost_per_verified_outcome), 'verified only', 'receipt-2')}
            </div>
            ${this.spendBadgesHtml(spend)}${this.modelMixHtml(spend)}
            <div class="row g-3">
                <div class="col-lg-6"><div class="subheader mb-2">By milestone</div><div class="table-responsive"><table class="table table-vcenter table-sm mb-0">
                    <thead><tr><th>Milestone</th><th class="text-end">Total</th><th class="text-end">Proven</th><th class="text-end">In review</th><th class="text-end">Verified</th></tr></thead>
                    <tbody>${msRows}</tbody></table></div></div>
                <div class="col-lg-6"><div class="subheader mb-2">KPI movement</div><div class="table-responsive"><table class="table table-vcenter table-sm mb-0">
                    <thead><tr><th>KPI</th><th class="text-end">Movement</th><th class="text-end">Spend</th><th class="text-end">Cost / unit</th></tr></thead>
                    <tbody>${kpiRows}</tbody></table></div></div>
            </div>
            <div class="subheader mb-2 mt-3">By linked task</div>
            <div class="table-responsive"><table class="table table-vcenter table-sm mb-0">
                <thead><tr><th>Task</th><th>Title</th><th>Proof</th><th class="text-end">Spend</th><th class="text-end">Verified</th><th class="text-end">Cost / outcome</th></tr></thead>
                <tbody>${taskRows}</tbody></table></div>
        </div></div>`;
    },

    _missionPolicyDrift(status) {
        const d = status.deliverable || {};
        const driftBlockers = (status.blockers || []).filter((b) =>
            ['external_ci', 'publication_evidence', 'human_gate'].includes(b.kind));
        let html = '';
        if (Object.keys(d.policy_constraints || {}).length) {
            html += `<div class="subheader mb-2">Policy constraints</div>${this._missionKv(d.policy_constraints)}`;
        }
        if (Object.keys(d.proof_requirements || {}).length) {
            html += `<div class="subheader mb-2 mt-3">Proof requirements</div>${this._missionKv(d.proof_requirements)}`;
        }
        if (driftBlockers.length) {
            html += `<div class="alert alert-warning mt-3 mb-0"><div class="fw-semibold mb-1">Policy / architecture drift signals</div><ul class="mb-0 ps-3">${driftBlockers.map((b) =>
                `<li>${this.esc(b.kind)} · ${this.esc(b.title || b.task_id || b.message || '')}</li>`).join('')}</ul></div>`;
        }
        return html || '<div class="text-secondary small">No policy constraints recorded on this deliverable.</div>';
    },

    // Hover text for a dependency-map node: the plain-English narration + who (and on which
    // platform) is working it now, sourced from the already-loaded mission status.
    _missionNodeTooltip(taskId) {
        const s = this.missionStatus || {};
        const id = String(taskId || '');
        let narration = '';
        let title = '';
        let assignee = '';
        for (const link of (s.linked_tasks || [])) {
            const d = link.task_detail || {};
            if (String(d.task_id) === id) {
                narration = d.narration || d.narration_raw || '';  // raw = last prose while live-stale
                if (!d.narration && d.narration_raw) narration += '\n(updating…)';
                title = d.title || '';
                assignee = d.assignee || '';
                break;
            }
        }
        const agents = (s.active_agents || []).filter((a) => String(a.task_id) === id);
        const lines = [];
        if (title) lines.push(`${id} — ${title}`);
        if (narration) lines.push('', narration);
        if (agents.length) {
            const who = agents.map((a) => {
                const plat = a.runtime ? ` · ${a.runtime}${a.model ? '/' + a.model : ''}` : '';
                return `${a.agent_id}${plat}${a.stale ? ' (stale)' : ''}`;
            }).join(', ');
            lines.push('', `▶ Working now: ${who}`);
        } else if (assignee) {
            lines.push('', `Assigned: ${assignee}`);
        }
        return lines.join('\n').trim();
    },

    // UI-1: operator controls that hang off the dependency-map card header.
    _missionAuthorButtons() {
        return `<div class="btn-list">
            <button class="btn btn-sm btn-primary" type="button" data-dl-action="link"><i class="ti ti-link me-1"></i>Link task</button>
            <button class="btn btn-sm btn-outline-secondary" type="button" data-dl-action="milestone"><i class="ti ti-flag me-1"></i>Milestone</button>
        </div>`;
    },

    _missionDependencyGraphHtml(graph) {
        const g = graph || this.missionGraph;
        if (!g || g.error) {
            return `<div class="card mb-4"><div class="card-header"><h3 class="card-title"><i class="ti ti-git-fork me-2"></i>Dependency map</h3><div class="card-actions">${this._missionAuthorButtons()}</div></div>
                <div class="card-body text-secondary small">No dependency graph yet — <a href="#" data-dl-action="link">link a task</a> to start the strategic map.</div></div>`;
        }
        const stats = g.stats || {};
        const legend = [
            ['done', 'Done ✓ proof', '#a3d9b7', '#1e7e34'],
            ['done_unproven', 'Done (no proof)', '#a6e3d0', '#12b886'],
            ['in_progress', 'In progress', '#8fb8fd', '#0b5ed7'],
            ['in_review', 'In review', '#ffe083', '#e0a800'],
            ['blocked', 'Blocked', '#f5a3a9', '#c82333'],
            ['todo', 'Not started', '#e9ecef', '#6c757d'],
            ['external', 'External dep', '#f8f9fa', '#adb5bd'],
        ].map(([key, label, fill, stroke]) =>
            `<span class="badge me-2 mb-1" style="background:${fill};color:#333;border:1px solid ${stroke}">${this.esc(label)}</span>`).join('') +
            `<span class="badge me-2 mb-1" style="background:#fff;color:#842029;border:3px solid #842029;font-weight:600" title="Unfinished tasks that other work depends on — what's holding the flow up">Blocker</span>`;
        const pillColors = {
            done: 'bg-green-lt',
            done_unproven: 'bg-teal-lt',
            in_progress: 'bg-blue-lt',
            in_review: 'bg-yellow-lt',
            blocked: 'bg-red-lt',
            todo: 'bg-secondary-lt',
            external: 'bg-secondary-lt',
            missing: 'bg-secondary-lt',
        };
        const pill = (n, external) => {
            // External blockers render dashed/grey in the graph — keep the pill grey too.
            const cls = external ? 'bg-secondary-lt' : (pillColors[n.state] || 'bg-secondary-lt');
            const tip = this._missionNodeTooltip(n.id);
            return `<a href="#" class="badge ${cls} me-1 mb-1 text-reset mission-dag-node" data-linked-task="${this.esc(n.id)}" data-linked-project="${this.esc(n.project_id || '')}"${tip ? ` title="${this.esc(tip)}"` : ''}>${this.esc(n.id)}</a>`;
        };
        const pills = (g.nodes || []).map((n) => pill(n, n.external)).join('');
        const contextPills = (g.context_nodes || []).map((n) => pill(n, false)).join('');
        return `<div class="card mb-4" id="mission-dag-panel"><div class="card-header">
            <h3 class="card-title"><i class="ti ti-git-fork me-2"></i>Dependency map</h3>
            ${this._missionAuthorButtons()}
            <div class="card-actions text-secondary small ms-auto">${[
                [stats.done_count, 'done'],
                [stats.done_unproven_count, 'done · no proof'],
                [stats.in_progress_count, 'in progress'],
                [stats.in_review_count, 'in review'],
                [stats.blocked_count, 'blocked'],
                [stats.todo_count, 'not started'],
                [stats.external_node_count, 'external'],
                [stats.context_task_count, 'context'],
            ].filter(([n]) => n).map(([n, l]) => `${n} ${l}`).join(' · ') || 'no tasks'}</div>
        </div><div class="card-body">
            <div class="mb-2">${legend}</div>
            <div id="mission-dag-graph" class="mission-dag-graph overflow-auto mb-3"></div>
            <div class="small text-secondary mb-1">Tasks in this deliverable</div>
            <div class="d-flex flex-wrap">${pills || '<span class="text-secondary small">No linked tasks</span>'}</div>
            ${contextPills ? `<div class="small text-secondary mb-1 mt-2">Foundation &amp; parked — linked for context, not in the flow map</div>
            <div class="d-flex flex-wrap">${contextPills}</div>` : ''}
        </div></div>`;
    },

    async _renderMissionMermaid() {
        const host = document.getElementById('mission-dag-graph');
        const g = this.missionGraph;
        if (!host || !g?.mermaid) return;
        // First render shows a spinner (a blank host reads as "never loads"). Live
        // re-renders keep the CURRENT graph on screen until the new SVG is ready, then
        // swap atomically — so a 5s live recolour updates in place with no blank flash.
        if (!host.querySelector('svg')) {
            host.innerHTML = '<div class="text-secondary small py-4 text-center"><span class="spinner-border spinner-border-sm me-2"></span>Rendering dependency map…</div>';
        }
        try { await this._ensureScript(this.MERMAID_SRC); } catch (e) { /* fall through to the unavailable message */ }
        if (!window.mermaid) {
            host.innerHTML = '<div class="text-secondary small">Dependency map renderer is unavailable right now — reload to retry.</div>';
            return;
        }
        try {
            if (!window.__missionMermaidInit) {
                let layout = 'dagre';
                try {
                    // Race the ESM import against a timeout so a slow/stalled CDN falls back
                    // to the built-in dagre layout instead of hanging the whole render.
                    const elk = await Promise.race([
                        import(this.ELK_SRC),
                        new Promise((_, rej) => setTimeout(() => rej(new Error('elk import timeout')), 8000)),
                    ]);
                    window.mermaid.registerLayoutLoaders(elk.default || elk);
                    layout = 'elk';
                } catch (e) { /* ELK unavailable/slow — dagre still renders fine */ }
                window.mermaid.initialize({
                    startOnLoad: false,
                    securityLevel: 'strict',
                    theme: 'neutral',
                    layout,
                    flowchart: {
                        useMaxWidth: false, htmlLabels: true, curve: 'linear',
                        nodeSpacing: 45, rankSpacing: 70, padding: 14,
                        subGraphTitleMargin: { top: 6, bottom: 10 },
                    },
                });
                window.__missionMermaidInit = true;
            }
            this._missionDagRenderId += 1;
            const renderId = `mission-dag-${this._missionDagRenderId}`;
            const { svg } = await window.mermaid.render(renderId, g.mermaid);
            host.innerHTML = svg;
            this._wireMissionGraphClicks(host);
        } catch (e) {
            host.innerHTML = `<div class="alert alert-warning mb-2">Could not render graph: ${this.esc(e.message)}</div>
                <pre class="small mb-0" style="white-space:pre-wrap">${this.esc(g.mermaid)}</pre>`;
        }
    },

    _wireMissionGraphClicks(host) {
        if (!host || !(this.missionGraph?.nodes || []).length) return;
        const nodes = this.missionGraph.nodes;
        const SVGNS = 'http://www.w3.org/2000/svg';
        host.querySelectorAll('.node').forEach((nodeEl) => {
            nodeEl.style.cursor = 'pointer';
            // The id is emitted in its own <b> on the label's first line, so match
            // it exactly — a substring match sent FORGE-11 clicks to FORGE-1.
            const boldId = (nodeEl.querySelector('b')?.textContent || '').trim().toUpperCase();
            const label = (nodeEl.textContent || '').toUpperCase();
            const hit = nodes.find((n) => {
                const nid = String(n.id || '').toUpperCase();
                return boldId ? nid === boldId : label.includes(nid);
            });
            nodeEl.addEventListener('click', (e) => {
                e.preventDefault();
                if (hit) this.openNodeModal(hit.id, hit.project_id);   // UI-1: node actions
            });
            // Native SVG hover tooltip: narration + who's working it now.
            if (hit) {
                const tip = this._missionNodeTooltip(hit.id);
                if (tip) {
                    let t = nodeEl.querySelector('title');
                    if (!t) {
                        t = document.createElementNS(SVGNS, 'title');
                        nodeEl.insertBefore(t, nodeEl.firstChild);
                    }
                    t.textContent = tip;
                }
            }
        });
    },

    // ---- Live mission cockpit -------------------------------------------
    // Poll the deliverable's status + dependency graph while the tab is open and
    // the browser tab is visible, and re-render ONLY when something actually
    // changed (task status, active work, blockers) — so it tracks agents in real
    // time without flickering the graph on every tick.
    _missionSignature() {
        const s = this.missionStatus || {};
        const g = this.missionGraph || {};
        const nodeSig = (g.nodes || []).map((n) => `${n.id}:${n.state}`).sort();
        const active = (s.active_work || []).map((w) => `${w.task_id}:${w.status}:${(w.active_claims || []).length}`).sort();
        const blockers = (s.blockers || []).map((b) => `${b.kind || ''}:${b.task_id || ''}`).sort();
        return JSON.stringify([nodeSig, active, blockers, s.progress || {}, g.stats || {}, (s.deliverable || {}).status]);
    },

    _missionLiveStamp(changed) {
        const el = document.getElementById('mission-live-stamp');
        if (!el) return;
        const d = new Date();
        const t = [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, '0')).join(':');
        el.textContent = changed ? `updated ${t}` : `checked ${t}`;
    },

    async _missionLiveTick() {
        if (document.hidden) return;
        const tab = document.querySelector('#toptab-mission');
        if (!tab || !tab.classList.contains('active')) return;   // only when the tab is showing
        const id = (this.selectedDeliverableId || '').trim();
        if (!id || this._missionLiveBusy) return;
        this._missionLiveBusy = true;
        try {
            await Promise.all([this.loadMissionStatus(id), this.loadDependencyGraph(id)]);
        } catch (e) {
            this._missionLiveBusy = false;
            return;   // transient (agent mid-write, network blip) — try again next tick
        }
        this._missionLiveBusy = false;
        const sig = this._missionSignature();
        const changed = sig !== this._missionSig;
        if (changed) this.renderMissionPage();   // renderMissionPage refreshes _missionSig + stamp
        else this._missionLiveStamp(false);
    },

    _startMissionLive() {
        this._stopMissionLive();
        this._missionLiveStamp(false);
        this._missionLiveTimer = window.setInterval(() => this._missionLiveTick(), this._missionPollMs || 10000);
    },

    _stopMissionLive() {
        if (this._missionLiveTimer) { window.clearInterval(this._missionLiveTimer); this._missionLiveTimer = null; }
    },

    // Header deliverable switcher (top bar): mirrors the mission-tab picker so switching
    // deliverables is a first-class global action. Hidden when the project has none.
    _syncHeaderDeliverable() {
        const sel = document.getElementById('header-deliverable-switcher');
        const wrap = document.getElementById('header-deliverable-wrap');
        if (!sel) return;
        const list = this.deliverables || [];
        if (!list.length) { if (wrap) wrap.style.display = 'none'; return; }
        if (wrap) wrap.style.display = '';
        const cur = this.selectedDeliverableId || list[0].id;
        sel.innerHTML = list.map((d) =>
            `<option value="${this.esc(d.id)}"${d.id === cur ? ' selected' : ''}>${this.esc(d.title || d.id)}</option>`).join('');
        if (cur) sel.value = cur;
        if (!sel._wired) {
            sel._wired = true;
            sel.addEventListener('change', () => {
                this.selectedDeliverableId = sel.value || '';
                const tab = document.querySelector('#toptab-mission');
                if (tab && window.bootstrap) window.bootstrap.Tab.getOrCreateInstance(tab).show();
                this.refreshMissionPage();
            });
        }
    },

    async initHeaderDeliverableSwitcher() {
        try { await this.loadDeliverables(); } catch (e) { this.deliverables = []; }
        if (!this.selectedDeliverableId && (this.deliverables || []).length) {
            this.selectedDeliverableId = this.deliverables[0].id;
        }
        this._syncHeaderDeliverable();
    },

    renderMissionPage() {
        const el = document.getElementById('mission-page');
        const s = this.missionStatus;
        if (!el || !s) return;
        const d = s.deliverable || {};
        const board = s.board || {};
        const prog = s.progress || {};
        const pctDone = Math.round((prog.done_with_proof_ratio || 0) * 100);
        const header = `<div class="d-flex flex-wrap align-items-start gap-3 mb-4"><div class="flex-fill">
            <div class="text-secondary small mb-1">${this.esc(s.project_id || window.PM_PROJECT || '')}${s.board_id ? ' · ' + this.esc(s.board_id) : ''}</div>
            <h2 class="mb-2">${this.esc(d.title || s.deliverable_id || 'Mission')}</h2>
            <div class="btn-list">${this._missionBadge(d.status, this.DELIVERABLE_STATUS_COLOR)} ${this._missionConfidence(board.confidence)}</div>
        </div>
        <div class="text-end">
            <span class="badge bg-green-lt" title="Live — auto-refreshes as agents update tasks"><span class="status-dot status-dot-animated bg-green me-1"></span>Live</span>
            <div id="mission-live-stamp" class="text-secondary small mt-1"></div>
        </div></div>
        <div id="mission-session-health-strip" class="d-flex flex-wrap align-items-center gap-2 mb-4"></div>`;
        const kpi = `<div class="row row-cards mb-4">${[
            ['Done with proof', prog.done_with_proof_count || 0, 'ti-circle-check', 'green'],
            ['In review', prog.in_review_count || 0, 'ti-eye-check', 'azure'],
            ['Active / claimed', (s.active_work || []).length, 'ti-bolt', 'blue'],
            ['Blockers', (s.blockers || []).length, 'ti-alert-triangle', 'red'],
            ['Linked tasks', prog.linked_task_count || 0, 'ti-link', 'secondary'],
            ['Progress', `${pctDone}%`, 'ti-chart-donut', 'primary'],
        ].map(([label, value, icon, color]) => `<div class="col-6 col-md-4 col-xl-2"><div class="card card-sm"><div class="card-body">
            <div class="d-flex"><div class="subheader">${this.esc(label)}</div><div class="ms-auto text-${color}"><i class="ti ${icon}"></i></div></div>
            <div class="h2 mb-0 mt-1">${this.esc(value)}</div></div></div></div>`).join('')}</div>`;
        const narrative = this._missionBriefHtml(s);
        const endState = `<div class="row g-3 mb-4"><div class="col-md-6"><div class="card h-100"><div class="card-header"><h3 class="card-title">End state</h3></div><div class="card-body"><p class="mb-0" style="white-space:pre-wrap">${this.esc(d.end_state || '—')}</p></div></div></div>
            <div class="col-md-6"><div class="card h-100"><div class="card-header"><h3 class="card-title">Why it matters</h3></div><div class="card-body"><p class="mb-0" style="white-space:pre-wrap">${this.esc(d.why_it_matters || '—')}</p></div></div></div></div>`;
        const milestones = s.milestones || [];
        const milestoneMap = milestones.length ? `<div class="card mb-4"><div class="card-header"><h3 class="card-title">Milestone progress</h3></div><div class="card-body"><div class="row g-3">${milestones.map((m) => {
            const sc = this.MILESTONE_STATUS_COLOR[m.status] || 'secondary';
            return `<div class="col-12 col-md-6 col-xl-4"><div class="card card-sm h-100"><div class="card-status-start bg-${sc}"></div><div class="card-body">
                <div class="d-flex mb-2">${this._missionBadge(m.status, this.MILESTONE_STATUS_COLOR)}<span class="ms-auto text-secondary small">${this.esc(String(m.linked_task_count || 0))} linked</span></div>
                <div class="fw-semibold">${this.esc(m.title || m.id)}</div></div></div></div>`;
        }).join('')}</div></div></div>` : '<div class="card mb-4"><div class="card-body text-secondary small">No milestones defined yet.</div></div>';
        const doneRows = (s.done_with_proof || []).map((w) => {
            const pr = (w.provenance || {}).pr_url || (w.git_state || {}).pr_url;
            return `<tr><td><a href="#" data-linked-task="${this.esc(w.task_id)}" data-linked-project="${this.esc(w.project_id)}">${this.esc(w.task_id)}</a></td><td>${this.esc(w.title || '')}</td><td>${this.esc((w.provenance || {}).label || 'Done with proof')}</td><td>${pr ? `<a href="${this.esc(pr)}" target="_blank" rel="noopener">PR</a>` : '—'}</td></tr>`;
        }).join('') || '<tr><td colspan="4" class="text-secondary">Nothing Done-with-proof yet</td></tr>';
        const linkedRows = (s.linked_tasks || []).map((link) => {
            const dtl = link.task_detail || link.task || {};
            return `<tr><td>${this.esc(link.project_id || '')}</td><td><a href="#" data-linked-task="${this.esc(link.task_id)}" data-linked-project="${this.esc(link.project_id)}">${this.esc(link.task_id)}</a></td><td>${this.esc(dtl.title || dtl.error || '')}</td><td>${this._missionBadge(dtl.status || 'missing', this.STATUS_COLOR)}</td><td>${this.esc(link.milestone_id || '—')}</td><td>${this.esc(link.role || '—')}</td></tr>`;
        }).join('') || '<tr><td colspan="6" class="text-secondary">No cross-project links</td></tr>';
        // Blockers box removed — it dumped raw kinds like "dependency_unsatisfied". The
        // dependency map already outlines blockers with a thick dark border.
        const blockerHtml = '';
        const nextActions = (s.next_actions || []).length ? `<div class="card mb-4"><div class="card-header"><h3 class="card-title">Next best actions</h3></div><div class="list-group list-group-flush">${(s.next_actions || []).map((a) =>
            `<div class="list-group-item"><span class="badge bg-primary-lt me-2">${this.esc(a.action || 'action')}</span>${this.esc(a.title || a.reason || '')}${a.task_id ? ` <span class="text-secondary small">· ${this.esc(a.project_id || '')} ${this.esc(a.task_id)}</span>` : ''}</div>`).join('')}</div></div>` : '';
        const agents = (s.active_agents || []).length ? `<div class="card mb-4"><div class="card-header"><h3 class="card-title">Live agents</h3></div><div class="table-responsive"><table class="table table-vcenter card-table"><thead><tr><th>Agent</th><th>Task</th><th>Project</th></tr></thead><tbody>${(s.active_agents || []).map((a) =>
            `<tr><td>${this.esc(a.agent_id || '')}</td><td><a href="#" data-linked-task="${this.esc(a.task_id)}" data-linked-project="${this.esc(a.project_id)}">${this.esc(a.task_id || '')}</a></td><td>${this.esc(a.project_id || '')}</td></tr>`).join('')}</tbody></table></div></div>` : '';
        const activeRows = (s.active_work || []).map((w) => `<tr><td><a href="#" data-linked-task="${this.esc(w.task_id)}" data-linked-project="${this.esc(w.project_id)}">${this.esc(w.task_id)}</a></td><td>${this.esc(w.title || '')}</td><td>${this._missionBadge(w.status, this.STATUS_COLOR)}</td><td>${this.sessionHealthPill(w.session_health)}</td><td>${this.esc(w.assignee || '—')}</td><td class="small">${this.esc((w.active_claims || []).map((c) => c.agent_id).join(', ') || '—')}</td></tr>`).join('') || '<tr><td colspan="6" class="text-secondary">No active linked work</td></tr>';
        // Keep the currently-rendered graph so a live re-render can show it until the new
        // SVG is ready (no blank/flash while colours update in place).
        const _prevGraphSvg = el.querySelector('#mission-dag-graph svg');
        const _prevDetail = el.querySelector('#mission-detail');
        const detailOpen = _prevDetail ? _prevDetail.open : !!this._missionDetailOpen;
        this._missionDetailOpen = detailOpen;
        // Lead with the story: headline → plain-English → what's blocked → the map →
        // breakdown/outcomes review → next action.
        const essentials = header + this._missionCeoHeaderHtml(s) + blockerHtml
            + this._missionDependencyGraphHtml() + this._missionBreakdownHtml() + nextActions;
        // The rest (KPIs, brief, milestones, work tables, agents, linked tasks, policy) folds
        // into a disclosure so it's there when you want it, not a wall of ~15 cards up front.
        const detail = kpi + this._missionEconomicsHtml(s.economics) + this._missionKpiOutcomesHtml() + narrative + endState + milestoneMap +
            `<div class="row g-3 mb-4"><div class="col-lg-6"><div class="card h-100"><div class="card-header"><h3 class="card-title">Active work</h3></div><div class="table-responsive"><table class="table table-vcenter card-table"><thead><tr><th>Task</th><th>Title</th><th>Status</th><th>Session</th><th>Assignee</th><th>Claims</th></tr></thead><tbody>${activeRows}</tbody></table></div></div></div>
            <div class="col-lg-6"><div class="card h-100"><div class="card-header"><h3 class="card-title">Done with proof</h3></div><div class="table-responsive"><table class="table table-vcenter card-table"><thead><tr><th>Task</th><th>Title</th><th>Provenance</th><th>PR</th></tr></thead><tbody>${doneRows}</tbody></table></div></div></div></div>` +
            agents +
            `<div class="card mb-4"><div class="card-header"><h3 class="card-title">Linked tasks across projects</h3></div><div class="table-responsive"><table class="table table-vcenter card-table"><thead><tr><th>Project</th><th>Task</th><th>Title</th><th>Status</th><th>Milestone</th><th>Role</th></tr></thead><tbody>${linkedRows}</tbody></table></div></div>` +
            `<div class="row g-3"><div class="col-lg-6"><div class="card h-100"><div class="card-header"><h3 class="card-title">Architecture / policy</h3></div><div class="card-body">${this._missionPolicyDrift(s)}</div></div></div>
            <div class="col-lg-6"><div class="card h-100"><div class="card-header"><h3 class="card-title">Recent changes</h3></div><div class="card-body">${this._missionRecentChanges(s.linked_tasks)}</div></div></div></div>`;
        el.innerHTML = essentials +
            `<details id="mission-detail" class="mb-4"${detailOpen ? ' open' : ''}>
                <summary class="text-secondary py-2"><i class="ti ti-chevron-right mission-detail-chev me-1"></i>Full detail — KPIs, brief, milestones, work, agents, linked tasks</summary>
                <div class="pt-3">${detail}</div>
            </details>`;
        const _gh = el.querySelector('#mission-dag-graph');
        if (_prevGraphSvg && _gh && !_gh.querySelector('svg')) _gh.appendChild(_prevGraphSvg);
        this._renderMissionMermaid();
        // Re-baseline the live signature so the poller only re-renders on the NEXT
        // real change, and stamp the freshly-rendered "updated" time.
        this._missionSig = this._missionSignature();
        this._missionLiveStamp(true);
        this.renderFleetDock({ mode: 'deliverable', taskIds: (s.linked_tasks || []).map((l) => l.task_id) });
    },

    async openLinkedTask(taskId, projectId) {
        const pid = (projectId || window.PM_PROJECT || 'maxwell').trim();
        const id = (taskId || '').trim().toUpperCase();
        if (!id) return;
        await this.openTask(id, pid);
    },

    // ---- UI-1: author the deliverable graph -----------------------------
    // The mission page is no longer read-only: an operator can link/unlink tasks,
    // author milestones, act on breakdown proposals, and record outcomes — all
    // against REST endpoints that already back the MCP tools (no new substrate).

    async loadBreakdownProposals(deliverableId) {
        const id = (deliverableId || '').trim();
        if (!id) { this.missionProposals = []; return []; }
        try {
            const res = await fetch(`api/deliverables/breakdown_proposals?deliverable_id=${encodeURIComponent(id)}`, { cache: 'no-store' });
            const data = await res.json();
            this.missionProposals = (res.ok && Array.isArray(data.proposals)) ? data.proposals : [];
        } catch (e) { this.missionProposals = []; }
        return this.missionProposals;
    },

    // ---- UI-2: KPIs & outcomes ------------------------------------------
    // The global fetch wrapper only appends ?project= to `api/…` URLs, so tally
    // reads/writes must carry the project explicitly (query for GETs, body for
    // writes) or they silently hit the default 'maxwell' board.
    _pmProject() { return window.PM_PROJECT || 'maxwell'; },
    async loadKpisAndOutcomes() {
        try {
            const p = encodeURIComponent(this._pmProject());
            const [kres, ores] = await Promise.all([
                fetch(`tally/v1/kpis?project=${p}`, { cache: 'no-store' }),
                fetch(`tally/v1/outcomes?limit=100&project=${p}`, { cache: 'no-store' }),
            ]);
            const kdata = await kres.json();
            const odata = await ores.json();
            this.missionKpis = (kres.ok && Array.isArray(kdata.kpis)) ? kdata.kpis : [];
            this.missionOutcomes = (ores.ok && Array.isArray(odata.outcomes)) ? odata.outcomes : [];
        } catch (e) { this.missionKpis = []; this.missionOutcomes = []; }
        return this.missionKpis;
    },

    _kpiTrend(k) {
        // Direction-aware arrow comparing current vs baseline (green = toward goal).
        const cur = Number(k.current_value), base = Number(k.baseline_value);
        if (!isFinite(cur) || !isFinite(base) || cur === base) return '<span class="text-secondary">→</span>';
        const up = cur > base;
        const good = (k.direction === 'decrease') ? !up : up;
        const icon = up ? 'ti-trending-up' : 'ti-trending-down';
        return `<span class="text-${good ? 'green' : 'red'}"><i class="ti ${icon}"></i></span>`;
    },

    _missionKpiOutcomesHtml() {
        const kpis = this.missionKpis || [];
        const outcomes = this.missionOutcomes || [];
        const kpiTiles = kpis.length ? kpis.map((k) => {
            const cur = (k.current_value != null) ? this.compact(k.current_value) : '—';
            const target = (k.target_value != null) ? ` / ${this.compact(k.target_value)}` : '';
            const spend = (k.spend || {}).cost_usd || 0;
            const cpu = (k.unit_cost || {}).cost_per_contribution_unit;
            return `<div class="col-6 col-md-4 col-xl-3"><div class="card card-sm h-100"><div class="card-body">
                <div class="d-flex align-items-start"><div class="subheader text-truncate" title="${this.esc(k.name)}">${this.esc(k.name)}</div>
                    <div class="ms-auto">${this._kpiTrend(k)}</div></div>
                <div class="h2 mb-0 mt-1">${cur}<span class="text-secondary fs-4">${target} ${this.esc(k.unit || '')}</span></div>
                <div class="text-secondary small">${this.compact(k.verified_contribution || 0)} verified${cpu != null ? ` · ${this.money(cpu)}/unit` : ''}${spend ? ` · ${this.money(spend)}` : ''}</div>
                <div class="mt-2"><button class="btn btn-sm btn-ghost-secondary" type="button" data-dl-action="kpi-edit" data-kpi="${this.esc(k.id)}"><i class="ti ti-pencil me-1"></i>Update value</button></div>
            </div></div></div>`;
        }).join('') : '<div class="col-12"><div class="text-secondary small">No KPIs yet — define one to track whether the work moves the needle.</div></div>';

        const pending = outcomes.filter((o) => String(o.status) === 'proposed');
        const decided = outcomes.filter((o) => String(o.status) !== 'proposed').slice(0, 8);
        const statusColor = { proposed: 'yellow', verified: 'green', rejected: 'red', superseded: 'secondary' };
        const outcomeRow = (o, withActions) => {
            const links = (o.kpi_links || []).map((l) => `<span class="badge bg-blue-lt me-1" title="${this.esc(l.kpi_name || l.kpi_id)}">${this.esc(l.kpi_name || l.kpi_id)}${l.contribution != null ? ` +${this.compact(l.contribution)}` : ''}</span>`).join('') || '<span class="text-secondary small">—</span>';
            const where = o.task_id ? `<a href="#" data-linked-task="${this.esc(o.task_id)}" data-linked-project="${this.esc(window.PM_PROJECT || '')}">${this.esc(o.task_id)}</a>` : (o.epic_id ? this.esc(o.epic_id) : '<span class="text-secondary">—</span>');
            const actions = withActions ? `<div class="btn-list flex-nowrap">
                <button class="btn btn-sm btn-success" type="button" data-dl-action="outcome-verify" data-outcome="${this.esc(o.id)}"><i class="ti ti-check me-1"></i>Verify</button>
                <button class="btn btn-sm btn-outline-danger" type="button" data-dl-action="outcome-reject" data-outcome="${this.esc(o.id)}">Reject</button>
                <button class="btn btn-sm btn-outline-primary" type="button" data-dl-action="outcome-link" data-outcome="${this.esc(o.id)}"><i class="ti ti-link me-1"></i>KPI</button>
            </div>` : this._missionBadge(o.status, statusColor);
            return `<tr><td>${this.esc(o.title || o.id)}</td><td><span class="badge bg-secondary-lt">${this.esc(o.type || '—')}</span></td><td>${where}</td><td>${links}</td><td class="text-end">${actions}</td></tr>`;
        };
        const queueBody = pending.length ? pending.map((o) => outcomeRow(o, true)).join('')
            : '<tr><td colspan="5" class="text-secondary text-center py-3">No outcomes awaiting verification.</td></tr>';
        const decidedBody = decided.length ? decided.map((o) => outcomeRow(o, false)).join('') : '';

        return `<div class="card mb-4"><div class="card-header">
                <h3 class="card-title"><i class="ti ti-target-arrow me-2"></i>KPIs &amp; outcomes</h3>
                <div class="card-actions btn-list">
                    <button class="btn btn-sm btn-primary" type="button" data-dl-action="kpi-new"><i class="ti ti-plus me-1"></i>New KPI</button>
                    <button class="btn btn-sm btn-outline-secondary" type="button" data-dl-action="tally-outcome"><i class="ti ti-flag me-1"></i>Record outcome</button>
                </div></div>
            <div class="card-body"><div class="row row-cards g-2 mb-1">${kpiTiles}</div></div>
            <div class="table-responsive"><table class="table table-vcenter card-table">
                <thead><tr><th>Outcome</th><th>Type</th><th>Where</th><th>KPIs</th><th class="text-end">${pending.length ? 'Verify / reject / link' : ''}</th></tr></thead>
                <tbody>${queueBody}${decidedBody}</tbody></table></div></div>`;
    },

    _missionBreakdownHtml() {
        const all = this.missionProposals || [];
        const pending = all.filter((p) => ['proposed', 'deferred'].includes(String(p.status || '').toLowerCase()));
        const rows = pending.map((p) => {
            const pid = p.id || p.proposal_id || '';
            const payload = p.payload || {};
            const ms = Array.isArray(payload.milestones) ? payload.milestones : [];
            const taskCount = ms.reduce((n, m) => n + ((m.tasks || []).length), 0);
            const summary = p.summary || payload.notes || p.notes || p.outcome || 'Breakdown proposal';
            const statusBadge = this._missionBadge(p.status, { proposed: 'yellow', deferred: 'secondary', approved: 'green', rejected: 'red' });
            return `<div class="list-group-item"><div class="d-flex flex-wrap align-items-start gap-2">
                <div class="flex-fill">
                    <div class="d-flex align-items-center gap-2 mb-1">${statusBadge}<span class="fw-semibold">${this.esc(summary)}</span></div>
                    <div class="text-secondary small">${ms.length} milestone${ms.length === 1 ? '' : 's'} · ${taskCount} task${taskCount === 1 ? '' : 's'} · <span class="text-muted">${this.esc(pid)}</span></div>
                </div>
                <div class="btn-list">
                    <button class="btn btn-sm btn-success" type="button" data-dl-action="approve" data-proposal="${this.esc(pid)}"><i class="ti ti-check me-1"></i>Approve</button>
                    <button class="btn btn-sm btn-outline-secondary" type="button" data-dl-action="defer" data-proposal="${this.esc(pid)}">Defer</button>
                    <button class="btn btn-sm btn-outline-danger" type="button" data-dl-action="reject" data-proposal="${this.esc(pid)}">Reject</button>
                </div></div></div>`;
        }).join('');
        const body = pending.length
            ? `<div class="list-group list-group-flush">${rows}</div>`
            : '<div class="card-body text-secondary small">No pending breakdown proposals. Use <strong>Record outcome</strong> to draft milestones from a plain-English outcome.</div>';
        return `<div class="card mb-4" id="mission-breakdown-card"><div class="card-header">
            <h3 class="card-title"><i class="ti ti-list-check me-2"></i>Breakdown &amp; outcomes${pending.length ? ` <span class="badge bg-yellow-lt ms-2">${pending.length} pending</span>` : ''}</h3>
            <div class="card-actions"><button class="btn btn-sm btn-primary" type="button" data-dl-action="outcome"><i class="ti ti-flag-check me-1"></i>Record outcome</button></div>
        </div>${body}</div>`;
    },

    _missionAction(action, ds) {
        ds = ds || {};
        switch (action) {
            case 'link': return this.openLinkModal();
            case 'milestone': return this.openMilestoneModal();
            case 'outcome': return this.openOutcomeModal();
            case 'approve': return this.approveProposal(ds.proposal);
            case 'reject': return this.rejectProposal(ds.proposal);
            case 'defer': return this.deferProposal(ds.proposal);
            // UI-2: KPIs & outcomes
            case 'kpi-new': return this.openKpiModal();
            case 'kpi-edit': return this.updateKpiValue(ds.kpi);
            case 'tally-outcome': return this.openTallyOutcomeModal();
            case 'outcome-verify': return this.verifyTallyOutcome(ds.outcome);
            case 'outcome-reject': return this.rejectTallyOutcome(ds.outcome);
            case 'outcome-link': return this.openKpiLinkModal(ds.outcome);
        }
    },

    // ---- small modal + fetch helpers ----
    _dlShow(id) { window.bootstrap.Modal.getOrCreateInstance(document.getElementById(id)).show(); },
    _dlHide(id) { const m = document.getElementById(id); const inst = m && window.bootstrap.Modal.getInstance(m); if (inst) inst.hide(); },
    _dlFlash(id, msg, cls) { const el = document.getElementById(id); if (el) { el.textContent = msg || ''; el.className = `small ${cls || 'text-secondary'} me-auto`; } },
    _dlHttpErr(res, data) {
        if (res.status === 403) return 'You don’t have permission for this action.';
        return (data && (data.detail || data.error)) || `Failed (HTTP ${res.status})`;
    },
    async _dlSend(url, method, body) {
        const opt = { method };
        if (body !== undefined) { opt.headers = { 'Content-Type': 'application/json' }; opt.body = JSON.stringify(body); }
        const res = await fetch(url, opt);
        let data = {};
        try { data = await res.json(); } catch (e) { /* empty body */ }
        if (!res.ok) throw new Error(this._dlHttpErr(res, data));
        return data;
    },
    _missionMilestoneOptions(selected, includeNone) {
        const ms = (this.missionStatus && this.missionStatus.milestones) || [];
        const none = includeNone === false ? '' : `<option value=""${!selected ? ' selected' : ''}>— no milestone —</option>`;
        return none + ms.map((m) => `<option value="${this.esc(m.id)}"${m.id === selected ? ' selected' : ''}>${this.esc(m.title || m.id)}</option>`).join('');
    },
    _roleOptions(selected) {
        return this.DELIVERABLE_LINK_ROLES.map((r) => `<option value="${r}"${r === selected ? ' selected' : ''}>${r}</option>`).join('');
    },
    _milestoneStatusOptions(selected) {
        const sel = selected || 'not_started';
        return this.MILESTONE_STATUSES.map((s) => `<option value="${s}"${s === sel ? ' selected' : ''}>${s.replace(/_/g, ' ')}</option>`).join('');
    },
    async _dlProjectsThen(cb) {
        if (!this._dlProjects) {
            try {
                const res = await fetch('api/projects');
                const data = await res.json();
                this._dlProjects = (res.ok && Array.isArray(data.projects)) ? data.projects : [];
            } catch (e) { this._dlProjects = []; }
        }
        if (!this._dlProjects.length) this._dlProjects = [{ id: window.PM_PROJECT || 'maxwell', label: window.PM_PROJECT || 'maxwell' }];
        cb(this._dlProjects);
    },

    // ---- breakdown proposal actions ----
    async _proposalAction(url, method, body) {
        try {
            await this._dlSend(url, method, body);
            await this.refreshMissionPage();
        } catch (e) {
            alert(`Could not update proposal: ${e.message}`);
        }
    },
    approveProposal(pid) {
        if (!pid) return;
        if (!confirm('Approve this breakdown proposal? It will create and link the proposed tasks.')) return;
        return this._proposalAction(`api/deliverables/breakdown_proposals/${encodeURIComponent(pid)}/approve`, 'POST');
    },
    rejectProposal(pid) {
        if (!pid) return;
        const reason = prompt('Reason for rejecting this proposal? (optional)');
        if (reason === null) return;
        return this._proposalAction(`api/deliverables/breakdown_proposals/${encodeURIComponent(pid)}/reject`, 'POST', { reason });
    },
    deferProposal(pid) {
        if (!pid) return;
        const reason = prompt('Reason for deferring this proposal? (optional)');
        if (reason === null) return;
        return this._proposalAction(`api/deliverables/breakdown_proposals/${encodeURIComponent(pid)}/defer`, 'POST', { reason });
    },

    // ---- link a task ----
    openLinkModal(prefill) {
        prefill = prefill || {};
        this._dlProjectsThen((projects) => {
            const curProj = prefill.task_project || window.PM_PROJECT || 'maxwell';
            const projOpts = projects.map((p) => `<option value="${this.esc(p.id)}"${p.id === curProj ? ' selected' : ''}>${this.esc(p.label || p.id)}</option>`).join('');
            const body = document.getElementById('dl-link-body');
            body.innerHTML = `
                <div class="mb-3"><label class="form-label">Task board</label>
                    <select id="dl-link-project" class="form-select">${projOpts}</select></div>
                <div class="mb-2"><label class="form-label">Find a task</label>
                    <input id="dl-link-search" class="form-control" placeholder="Search by id or title…" autocomplete="off">
                    <div id="dl-link-results" class="list-group mt-1" style="max-height:12rem;overflow:auto"></div></div>
                <div class="mb-3"><label class="form-label">Task id</label>
                    <input id="dl-link-task" class="form-control text-uppercase" placeholder="e.g. ACCESS-14" value="${this.esc(prefill.task_id || '')}" autocomplete="off"></div>
                <div class="row g-2 mb-2">
                    <div class="col-md-6"><label class="form-label">Milestone</label><select id="dl-link-milestone" class="form-select">${this._missionMilestoneOptions(prefill.milestone_id)}</select></div>
                    <div class="col-md-6"><label class="form-label">Role</label><select id="dl-link-role" class="form-select">${this._roleOptions(prefill.role || 'contributes')}</select></div>
                </div>
                <label class="form-check"><input class="form-check-input" type="checkbox" id="dl-link-blocks"${prefill.blocks_deliverable ? ' checked' : ''}>
                    <span class="form-check-label">This task blocks the deliverable</span></label>`;
            this._dlFlash('dl-link-flash', '', 'text-secondary');
            const search = document.getElementById('dl-link-search');
            search.oninput = () => this._dlRunTaskSearch();
            document.getElementById('dl-link-project').onchange = () => { document.getElementById('dl-link-results').innerHTML = ''; if (search.value.trim()) this._dlRunTaskSearch(); };
            this._dlShow('dl-link-modal');
        });
    },
    async _dlRunTaskSearch() {
        const proj = document.getElementById('dl-link-project').value;
        const q = (document.getElementById('dl-link-search').value || '').trim().toLowerCase();
        const results = document.getElementById('dl-link-results');
        if (!results) return;
        this._dlTaskCache = this._dlTaskCache || {};
        let tasks = this._dlTaskCache[proj];
        if (!tasks) {
            results.innerHTML = '<div class="list-group-item text-secondary small">Loading tasks…</div>';
            try {
                // Explicit project= so the fetch wrapper doesn't override it with PM_PROJECT —
                // this searches ANY board, not just the current one.
                const res = await fetch(`api/tasks?project=${encodeURIComponent(proj)}`, { cache: 'no-store' });
                const data = await res.json();
                tasks = (res.ok && Array.isArray(data.tasks)) ? data.tasks : [];
            } catch (e) { tasks = []; }
            this._dlTaskCache[proj] = tasks;
        }
        if (!q) { results.innerHTML = ''; return; }
        const hits = tasks.filter((t) => {
            const id = String(t.task_id || '').toLowerCase();
            const title = String(t.title || '').toLowerCase();
            return id.includes(q) || title.includes(q);
        }).slice(0, 20);
        results.innerHTML = hits.length ? hits.map((t) => `<a href="#" class="list-group-item list-group-item-action py-1" data-dl-pick="${this.esc(t.task_id)}">
            <span class="fw-semibold">${this.esc(t.task_id)}</span> <span class="text-secondary small">${this.esc(t.title || '')}</span></a>`).join('')
            : '<div class="list-group-item text-secondary small">No matching tasks</div>';
        results.querySelectorAll('[data-dl-pick]').forEach((a) => a.addEventListener('click', (e) => {
            e.preventDefault();
            const pick = a.getAttribute('data-dl-pick');
            document.getElementById('dl-link-task').value = pick;
            document.getElementById('dl-link-search').value = pick;
            results.innerHTML = '';
        }));
    },
    async submitLinkTask() {
        const id = (this.selectedDeliverableId || '').trim();
        const taskId = (document.getElementById('dl-link-task').value || '').trim().toUpperCase();
        const taskProject = document.getElementById('dl-link-project').value;
        if (!id || !taskId) { this._dlFlash('dl-link-flash', 'Pick a task to link.', 'text-danger'); return; }
        const body = {
            task_project: taskProject,
            task_id: taskId,
            milestone_id: document.getElementById('dl-link-milestone').value || '',
            role: document.getElementById('dl-link-role').value || '',
            blocks_deliverable: document.getElementById('dl-link-blocks').checked,
        };
        const btn = document.getElementById('dl-link-save'); if (btn) btn.disabled = true;
        this._dlFlash('dl-link-flash', 'Linking…', 'text-secondary');
        try {
            await this._dlSend(`api/deliverables/${encodeURIComponent(id)}/task_links`, 'POST', body);
            this._dlHide('dl-link-modal');
            await this.refreshMissionPage();
        } catch (e) { this._dlFlash('dl-link-flash', e.message, 'text-danger'); }
        finally { if (btn) btn.disabled = false; }
    },

    // ---- add a milestone ----
    openMilestoneModal() {
        const body = document.getElementById('dl-milestone-body');
        body.innerHTML = `
            <div class="mb-3"><label class="form-label">Title</label><input id="dl-ms-title" class="form-control" placeholder="e.g. Ship login shell" autocomplete="off"></div>
            <div class="row g-2 mb-3">
                <div class="col-md-7"><label class="form-label">Status</label><select id="dl-ms-status" class="form-select">${this._milestoneStatusOptions('not_started')}</select></div>
                <div class="col-md-5"><label class="form-label">Sort order</label><input id="dl-ms-sort" type="number" class="form-control" placeholder="auto"></div>
            </div>
            <div class="mb-2"><label class="form-label">Acceptance criteria</label>
                <textarea id="dl-ms-accept" class="form-control" rows="3" placeholder="One criterion per line"></textarea>
                <div class="form-hint">One acceptance criterion per line.</div></div>`;
        this._dlFlash('dl-milestone-flash', '', 'text-secondary');
        this._dlShow('dl-milestone-modal');
        setTimeout(() => document.getElementById('dl-ms-title')?.focus(), 200);
    },
    async submitMilestone() {
        const id = (this.selectedDeliverableId || '').trim();
        const title = (document.getElementById('dl-ms-title').value || '').trim();
        if (!id || !title) { this._dlFlash('dl-milestone-flash', 'Enter a milestone title.', 'text-danger'); return; }
        const accept = (document.getElementById('dl-ms-accept').value || '').split('\n').map((s) => s.trim()).filter(Boolean);
        const sortRaw = (document.getElementById('dl-ms-sort').value || '').trim();
        const body = { title, status: document.getElementById('dl-ms-status').value, acceptance_criteria: accept };
        if (sortRaw !== '') body.sort_order = Number(sortRaw);
        const btn = document.getElementById('dl-milestone-save'); if (btn) btn.disabled = true;
        this._dlFlash('dl-milestone-flash', 'Saving…', 'text-secondary');
        try {
            await this._dlSend(`api/deliverables/${encodeURIComponent(id)}/milestones`, 'POST', body);
            this._dlHide('dl-milestone-modal');
            await this.refreshMissionPage();
        } catch (e) { this._dlFlash('dl-milestone-flash', e.message, 'text-danger'); }
        finally { if (btn) btn.disabled = false; }
    },

    // ---- record an outcome (drafts a breakdown proposal) ----
    openOutcomeModal() {
        const body = document.getElementById('dl-outcome-body');
        body.innerHTML = `
            <div class="mb-3"><label class="form-label">Outcome</label>
                <textarea id="dl-outcome-text" class="form-control" rows="3" placeholder="Plain-English outcome to record for this deliverable"></textarea></div>
            <div class="mb-3"><label class="form-label">Target projects</label>
                <input id="dl-outcome-projects" class="form-control" value="${this.esc(window.PM_PROJECT || '')}" placeholder="comma-separated board ids">
                <div class="form-hint">Boards where breakdown tasks would land.</div></div>
            <div class="mb-2"><label class="form-label">Acceptance criteria</label>
                <textarea id="dl-outcome-accept" class="form-control" rows="2" placeholder="One criterion per line"></textarea></div>
            <label class="form-check"><input class="form-check-input" type="checkbox" id="dl-outcome-llm">
                <span class="form-check-label">Use the LLM to draft a milestone breakdown</span></label>`;
        this._dlFlash('dl-outcome-flash', '', 'text-secondary');
        this._dlShow('dl-outcome-modal');
        setTimeout(() => document.getElementById('dl-outcome-text')?.focus(), 200);
    },
    async submitOutcome() {
        const id = (this.selectedDeliverableId || '').trim();
        const outcome = (document.getElementById('dl-outcome-text').value || '').trim();
        if (!id || !outcome) { this._dlFlash('dl-outcome-flash', 'Enter an outcome.', 'text-danger'); return; }
        const projects = (document.getElementById('dl-outcome-projects').value || '').split(',').map((s) => s.trim()).filter(Boolean);
        const accept = (document.getElementById('dl-outcome-accept').value || '').split('\n').map((s) => s.trim()).filter(Boolean);
        const body = { outcome, use_llm: document.getElementById('dl-outcome-llm').checked };
        if (projects.length) body.target_projects = projects;
        if (accept.length) body.acceptance_criteria = accept;
        const btn = document.getElementById('dl-outcome-save'); if (btn) btn.disabled = true;
        this._dlFlash('dl-outcome-flash', 'Recording…', 'text-secondary');
        try {
            await this._dlSend(`api/deliverables/${encodeURIComponent(id)}/outcome`, 'POST', body);
            this._dlHide('dl-outcome-modal');
            await this.refreshMissionPage();
        } catch (e) { this._dlFlash('dl-outcome-flash', e.message, 'text-danger'); }
        finally { if (btn) btn.disabled = false; }
    },

    // ---- UI-2: KPI create / update, tally-outcome record / verify / reject / link ----
    _directionOptions(sel) {
        return ['increase', 'decrease'].map((d) => `<option value="${d}"${d === sel ? ' selected' : ''}>${d}</option>`).join('');
    },
    openKpiModal() {
        const body = document.getElementById('dl-kpi-body');
        body.innerHTML = `
            <div class="mb-3"><label class="form-label required">Name</label>
                <input id="dl-kpi-name" class="form-control" placeholder="e.g. Weekly active operators" autocomplete="off"></div>
            <div class="row g-2 mb-2">
                <div class="col-md-6"><label class="form-label">Unit</label><input id="dl-kpi-unit" class="form-control" placeholder="e.g. operators, %, $"></div>
                <div class="col-md-6"><label class="form-label">Direction</label><select id="dl-kpi-direction" class="form-select">${this._directionOptions('increase')}</select></div>
            </div>
            <div class="row g-2 mb-2">
                <div class="col-md-4"><label class="form-label">Baseline</label><input id="dl-kpi-baseline" type="number" step="any" class="form-control"></div>
                <div class="col-md-4"><label class="form-label">Current</label><input id="dl-kpi-current" type="number" step="any" class="form-control"></div>
                <div class="col-md-4"><label class="form-label">Target</label><input id="dl-kpi-target" type="number" step="any" class="form-control"></div>
            </div>
            <div class="mb-1"><label class="form-label">Period</label><input id="dl-kpi-period" class="form-control" placeholder="e.g. weekly, Q3"></div>`;
        this._dlFlash('dl-kpi-flash', '', 'text-secondary');
        this._dlShow('dl-kpi-modal');
        setTimeout(() => document.getElementById('dl-kpi-name')?.focus(), 200);
    },
    async submitKpi() {
        const name = (document.getElementById('dl-kpi-name').value || '').trim();
        const unit = (document.getElementById('dl-kpi-unit').value || '').trim();
        if (!name || !unit) { this._dlFlash('dl-kpi-flash', 'Name and unit are required.', 'text-danger'); return; }
        const num = (id) => { const v = document.getElementById(id).value; return v === '' ? undefined : Number(v); };
        const body = {
            project: this._pmProject(),
            name, unit,
            direction: document.getElementById('dl-kpi-direction').value,
            baseline_value: num('dl-kpi-baseline'), current_value: num('dl-kpi-current'),
            target_value: num('dl-kpi-target'), period: (document.getElementById('dl-kpi-period').value || '').trim(),
        };
        const btn = document.getElementById('dl-kpi-save'); if (btn) btn.disabled = true;
        this._dlFlash('dl-kpi-flash', 'Creating…', 'text-secondary');
        try {
            await this._dlSend('tally/v1/kpis', 'POST', body);
            this._dlHide('dl-kpi-modal');
            await this.refreshMissionPage();
        } catch (e) { this._dlFlash('dl-kpi-flash', e.message, 'text-danger'); }
        finally { if (btn) btn.disabled = false; }
    },
    async updateKpiValue(kpiId) {
        const id = (kpiId || '').trim();
        if (!id) return;
        const kpi = (this.missionKpis || []).find((k) => k.id === id) || {};
        const raw = prompt(`New current value for “${kpi.name || id}”${kpi.unit ? ` (${kpi.unit})` : ''}:`,
            kpi.current_value != null ? String(kpi.current_value) : '');
        if (raw === null) return;
        const val = Number(raw);
        if (!isFinite(val)) { alert('Enter a number.'); return; }
        try {
            await this._dlSend(`tally/v1/kpis/${encodeURIComponent(id)}`, 'PATCH', { project: this._pmProject(), current_value: val });
            await this.refreshMissionPage();
        } catch (e) { alert(`Could not update KPI: ${e.message}`); }
    },
    openTallyOutcomeModal() {
        const body = document.getElementById('dl-tally-outcome-body');
        const linked = ((this.missionStatus || {}).linked_tasks || []).map((l) =>
            `<option value="${this.esc(l.task_id)}">${this.esc(l.task_id)}${l.title ? ' — ' + this.esc(l.title) : ''}</option>`).join('');
        body.innerHTML = `
            <div class="mb-3"><label class="form-label required">Outcome</label>
                <input id="dl-tally-outcome-title" class="form-control" placeholder="What was achieved" autocomplete="off"></div>
            <div class="row g-2 mb-2">
                <div class="col-md-6"><label class="form-label">Type</label><input id="dl-tally-outcome-type" class="form-control" placeholder="e.g. feature, metric, fix" value="feature"></div>
                <div class="col-md-6"><label class="form-label">Against task</label>
                    <select id="dl-tally-outcome-task" class="form-select"><option value="">— none —</option>${linked}</select></div>
            </div>
            <div class="form-hint">Records a proposed outcome; verify it below once it’s real.</div>`;
        this._dlFlash('dl-tally-outcome-flash', '', 'text-secondary');
        this._dlShow('dl-tally-outcome-modal');
        setTimeout(() => document.getElementById('dl-tally-outcome-title')?.focus(), 200);
    },
    async submitTallyOutcome() {
        const title = (document.getElementById('dl-tally-outcome-title').value || '').trim();
        if (!title) { this._dlFlash('dl-tally-outcome-flash', 'Enter an outcome.', 'text-danger'); return; }
        const body = {
            project: this._pmProject(),
            title, type: (document.getElementById('dl-tally-outcome-type').value || 'feature').trim(),
            task_id: document.getElementById('dl-tally-outcome-task').value || undefined,
            status: 'proposed',
        };
        const btn = document.getElementById('dl-tally-outcome-save'); if (btn) btn.disabled = true;
        this._dlFlash('dl-tally-outcome-flash', 'Recording…', 'text-secondary');
        try {
            await this._dlSend('tally/v1/outcomes', 'POST', body);
            this._dlHide('dl-tally-outcome-modal');
            await this.refreshMissionPage();
        } catch (e) { this._dlFlash('dl-tally-outcome-flash', e.message, 'text-danger'); }
        finally { if (btn) btn.disabled = false; }
    },
    async verifyTallyOutcome(outcomeId) {
        const id = (outcomeId || '').trim();
        if (!id) return;
        if (!confirm('Verify this outcome? It counts toward KPI movement and cost-per-outcome.')) return;
        try {
            await this._dlSend(`tally/v1/outcomes/${encodeURIComponent(id)}/verify`, 'POST', { project: this._pmProject() });
            await this.refreshMissionPage();
        } catch (e) { alert(`Could not verify outcome: ${e.message}`); }
    },
    async rejectTallyOutcome(outcomeId) {
        const id = (outcomeId || '').trim();
        if (!id) return;
        const reason = prompt('Reason for rejecting this outcome?');
        if (reason === null) return;
        try {
            await this._dlSend(`tally/v1/outcomes/${encodeURIComponent(id)}/reject`, 'POST', { project: this._pmProject(), reason: reason || 'rejected' });
            await this.refreshMissionPage();
        } catch (e) { alert(`Could not reject outcome: ${e.message}`); }
    },
    openKpiLinkModal(outcomeId) {
        const id = (outcomeId || '').trim();
        if (!id) return;
        const kpis = this.missionKpis || [];
        if (!kpis.length) { alert('Define a KPI first, then link the outcome to it.'); return; }
        this._dlLinkOutcomeId = id;
        const opts = kpis.map((k) => `<option value="${this.esc(k.id)}">${this.esc(k.name)}${k.unit ? ` (${this.esc(k.unit)})` : ''}</option>`).join('');
        const outcome = (this.missionOutcomes || []).find((o) => o.id === id) || {};
        const body = document.getElementById('dl-kpi-link-body');
        body.innerHTML = `
            <div class="mb-3"><div class="text-secondary small">Outcome</div><div class="fw-semibold">${this.esc(outcome.title || id)}</div></div>
            <div class="mb-2"><label class="form-label">KPI</label><select id="dl-kpi-link-kpi" class="form-select">${opts}</select></div>
            <div class="row g-2 mb-2">
                <div class="col-md-6"><label class="form-label">Contribution</label><input id="dl-kpi-link-contribution" type="number" step="any" class="form-control" placeholder="e.g. 3"></div>
                <div class="col-md-6"><label class="form-label">Confidence</label><select id="dl-kpi-link-confidence" class="form-select">
                    <option value="directional">directional</option><option value="estimated">estimated</option><option value="measured">measured</option></select></div>
            </div>`;
        this._dlFlash('dl-kpi-link-flash', '', 'text-secondary');
        this._dlShow('dl-kpi-link-modal');
    },
    async submitKpiLink() {
        const outcomeId = (this._dlLinkOutcomeId || '').trim();
        if (!outcomeId) return;
        const contribRaw = document.getElementById('dl-kpi-link-contribution').value;
        const body = {
            project: this._pmProject(),
            outcome_id: outcomeId, kpi_id: document.getElementById('dl-kpi-link-kpi').value,
            confidence: document.getElementById('dl-kpi-link-confidence').value,
        };
        if (contribRaw !== '') body.contribution = Number(contribRaw);
        const btn = document.getElementById('dl-kpi-link-save'); if (btn) btn.disabled = true;
        this._dlFlash('dl-kpi-link-flash', 'Linking…', 'text-secondary');
        try {
            await this._dlSend('tally/v1/outcome_kpi_links', 'POST', body);
            this._dlHide('dl-kpi-link-modal');
            await this.refreshMissionPage();
        } catch (e) { this._dlFlash('dl-kpi-link-flash', e.message, 'text-danger'); }
        finally { if (btn) btn.disabled = false; }
    },

    // ---- node actions: set milestone/role, unlink, open task ----
    openNodeModal(taskId, projectId) {
        const id = String(taskId || '').trim();
        if (!id) return;
        const link = ((this.missionStatus || {}).linked_tasks || []).find((l) => String(l.task_id) === id) || {};
        const taskProject = link.project_id || projectId || window.PM_PROJECT;
        this._dlNode = { task_id: id, task_project: taskProject };
        const body = document.getElementById('dl-node-body');
        body.innerHTML = `
            <div class="mb-3"><div class="text-secondary small">${this.esc(taskProject)}</div>
                <div class="h4 mb-0">${this.esc(id)}</div></div>
            <div class="row g-2 mb-2">
                <div class="col-md-6"><label class="form-label">Milestone</label><select id="dl-node-milestone" class="form-select">${this._missionMilestoneOptions(link.milestone_id)}</select></div>
                <div class="col-md-6"><label class="form-label">Role</label><select id="dl-node-role" class="form-select">${this._roleOptions(link.role || 'contributes')}</select></div>
            </div>
            <label class="form-check mb-3"><input class="form-check-input" type="checkbox" id="dl-node-blocks"${link.blocks_deliverable ? ' checked' : ''}>
                <span class="form-check-label">This task blocks the deliverable</span></label>
            <a href="#" class="small" id="dl-node-open"><i class="ti ti-external-link me-1"></i>Open task detail</a>`;
        this._dlFlash('dl-node-flash', '', 'text-secondary');
        document.getElementById('dl-node-open')?.addEventListener('click', (e) => {
            e.preventDefault();
            this._dlHide('dl-node-modal');
            this.openLinkedTask(id, taskProject);
        });
        this._dlShow('dl-node-modal');
    },
    async submitNodeLink() {
        const n = this._dlNode || {};
        const delId = (this.selectedDeliverableId || '').trim();
        if (!delId || !n.task_id) return;
        const body = {
            task_project: n.task_project, task_id: n.task_id,
            milestone_id: document.getElementById('dl-node-milestone').value || '',
            role: document.getElementById('dl-node-role').value || '',
            blocks_deliverable: document.getElementById('dl-node-blocks').checked,
        };
        const btn = document.getElementById('dl-node-save'); if (btn) btn.disabled = true;
        this._dlFlash('dl-node-flash', 'Saving…', 'text-secondary');
        try {
            await this._dlSend(`api/deliverables/${encodeURIComponent(delId)}/task_links`, 'POST', body);
            this._dlHide('dl-node-modal');
            await this.refreshMissionPage();
        } catch (e) { this._dlFlash('dl-node-flash', e.message, 'text-danger'); }
        finally { if (btn) btn.disabled = false; }
    },
    async unlinkNode() {
        const n = this._dlNode || {};
        const delId = (this.selectedDeliverableId || '').trim();
        if (!delId || !n.task_id) return;
        if (!confirm(`Unlink ${n.task_id} from this deliverable?`)) return;
        const btn = document.getElementById('dl-node-unlink'); if (btn) btn.disabled = true;
        this._dlFlash('dl-node-flash', 'Unlinking…', 'text-secondary');
        try {
            await this._dlSend(`api/deliverables/${encodeURIComponent(delId)}/task_links?task_project=${encodeURIComponent(n.task_project)}&task_id=${encodeURIComponent(n.task_id)}`, 'DELETE');
            this._dlHide('dl-node-modal');
            await this.refreshMissionPage();
        } catch (e) { this._dlFlash('dl-node-flash', e.message, 'text-danger'); }
        finally { if (btn) btn.disabled = false; }
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
        const npBtn = document.getElementById('btn-new-project');
        if (npBtn) npBtn.addEventListener('click', () => this.openNewProject());
        const npCreate = document.getElementById('np-create');
        if (npCreate) npCreate.addEventListener('click', () => this.submitNewProject());
        // UI-15: GitHub repo association + guided webhook wiring.
        const ghBtn = document.getElementById('btn-project-github');
        if (ghBtn) ghBtn.addEventListener('click', () => this.openGithubAssoc(window.PM_PROJECT));
        const gaSave = document.getElementById('ga-save');
        if (gaSave) gaSave.addEventListener('click', () => this.saveGithubRepo());
        const gaVerify = document.getElementById('ga-verify');
        if (gaVerify) gaVerify.addEventListener('click', () => this.verifyGithubConnection());
        const gaGoto = document.getElementById('ga-goto');
        if (gaGoto) gaGoto.addEventListener('click', () => {
            const id = this._gaSwitchTo || this._gaProject;
            const u = new URL(window.location.href);
            u.searchParams.set('project', id);
            window.location.href = u.toString();
        });
        document.querySelectorAll('#github-assoc-modal .ga-copy').forEach((b) => {
            b.addEventListener('click', () => {
                const src = document.getElementById(b.getAttribute('data-copy'));
                if (!src) return;
                const done = () => { const i = b.querySelector('i'); if (i) { const p = i.className; i.className = 'ti ti-check'; setTimeout(() => { i.className = p; }, 1200); } };
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(src.value).then(done).catch(() => { src.select(); document.execCommand('copy'); done(); });
                } else { src.select(); document.execCommand('copy'); done(); }
            });
        });
        // UI-14: Communications settings (inbound domains + outbound recipients).
        const commsBtn = document.getElementById('btn-project-comms');
        if (commsBtn) commsBtn.addEventListener('click', () => this.openComms(window.PM_PROJECT));
        const commsSave = document.getElementById('comms-save');
        if (commsSave) commsSave.addEventListener('click', () => this.saveComms());
        const domAdd = document.getElementById('comms-domain-add');
        if (domAdd) domAdd.addEventListener('click', () => this._commsAddDomain());
        const domInput = document.getElementById('comms-domain-input');
        if (domInput) domInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); this._commsAddDomain(); } });
        document.querySelectorAll('#comms-modal [data-add-recipient]').forEach((b) => {
            const kind = b.getAttribute('data-add-recipient');
            b.addEventListener('click', () => this._commsAddRecipient(kind));
            const inp = document.getElementById(`comms-${kind}-input`);
            if (inp) inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); this._commsAddRecipient(kind); } });
        });
        document.querySelectorAll('#comms-modal [data-test-kind]').forEach((b) => {
            b.addEventListener('click', () => this.sendCommsTest(b.getAttribute('data-test-kind')));
        });
        document.querySelectorAll('#comms-modal .comms-copy').forEach((b) => {
            b.addEventListener('click', () => {
                const src = document.getElementById(b.getAttribute('data-copy'));
                if (!src) return;
                const done = () => { const i = b.querySelector('i'); if (i) { const p = i.className; i.className = 'ti ti-check'; setTimeout(() => { i.className = p; }, 1200); } };
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(src.value).then(done).catch(() => { src.select(); document.execCommand('copy'); done(); });
                } else { src.select(); document.execCommand('copy'); done(); }
            });
        });
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
        // Pulse folded into Overview; Plan/Inbox are hubs. HARDEN-39's lazy render keys on
        // each sub-pane's own shown.bs.tab, which does NOT fire when the parent hub is
        // revealed — so opening a hub must render whatever sub-view is currently active.
        // (Sub-pill switches still fire the per-pane listeners in _wireLazyTabs/setupGantt.)
        const overviewTop = document.getElementById('toptab-overview');
        if (overviewTop) overviewTop.addEventListener('shown.bs.tab', () => { this.renderTallyPulse(); this.initPulse(); });
        const planTop = document.getElementById('toptab-plan');
        if (planTop) planTop.addEventListener('shown.bs.tab', () => this._renderPlanActive());
        const inboxTopTab = document.getElementById('toptab-inbox');
        if (inboxTopTab) inboxTopTab.addEventListener('shown.bs.tab', () => { this.initInbox(); this.renderTables(); });
        const inboxTab = document.querySelector('a[href="#tab-inbox"]');
        if (inboxTab) inboxTab.addEventListener('shown.bs.tab', () => this.initInbox());
        const inboxRefresh = document.getElementById('inbox-refresh');
        if (inboxRefresh) inboxRefresh.addEventListener('click', () => this.initInbox());
        const inboxSim = document.getElementById('inbox-sim');
        if (inboxSim) inboxSim.addEventListener('click', () => { const box = document.getElementById('inbox-sim-box'); if (window.bootstrap) window.bootstrap.Collapse.getOrCreateInstance(box).toggle(); });
        const inboxSimGo = document.getElementById('inbox-sim-go');
        if (inboxSimGo) inboxSimGo.addEventListener('click', () => this.simulateInbox());
        // Bootstrap fires shown.bs.tab on the TOP tab trigger (in .nav-tabs), not the
        // sidebar link that shares the same href — so listen on the top tab.
        const missionTab = document.querySelector('#toptab-mission');
        if (missionTab) missionTab.addEventListener('shown.bs.tab', () => this.refreshMissionPage());
        const missionRefresh = document.getElementById('mission-refresh');
        if (missionRefresh) missionRefresh.addEventListener('click', () => this.refreshMissionPage());
        const missionGenerate = document.getElementById('mission-generate-brief');
        if (missionGenerate) missionGenerate.addEventListener('click', () => this.generateMissionBrief());
        const missionPicker = document.getElementById('mission-deliverable-picker');
        if (missionPicker) missionPicker.addEventListener('change', (e) => {
            this.selectedDeliverableId = e.target.value || '';
            this.refreshMissionPage();
        });
        const missionPage = document.getElementById('mission-page');
        if (missionPage && !this._missionWired) {
            this._missionWired = true;
            missionPage.addEventListener('click', (e) => {
                // UI-1 authoring controls come first so a node pill (which is also a
                // data-linked-task) opens the node-action modal, not the task modal.
                const act = e.target.closest('[data-dl-action]');
                if (act && missionPage.contains(act)) {
                    e.preventDefault();
                    this._missionAction(act.getAttribute('data-dl-action'), {
                        proposal: act.getAttribute('data-proposal'),
                        kpi: act.getAttribute('data-kpi'),
                        outcome: act.getAttribute('data-outcome'),
                    });
                    return;
                }
                const node = e.target.closest('.mission-dag-node');
                if (node && missionPage.contains(node)) {
                    e.preventDefault();
                    this.openNodeModal(node.getAttribute('data-linked-task'), node.getAttribute('data-linked-project'));
                    return;
                }
                const a = e.target.closest('[data-linked-task]');
                if (!a || !missionPage.contains(a)) return;
                e.preventDefault();
                this.openLinkedTask(a.getAttribute('data-linked-task'), a.getAttribute('data-linked-project'));
            });
        }
        // UI-1: authoring modal save/unlink buttons (static shells in index.html).
        const wireBtn = (id, fn) => { const b = document.getElementById(id); if (b) b.addEventListener('click', () => fn.call(this)); };
        wireBtn('dl-link-save', this.submitLinkTask);
        wireBtn('dl-milestone-save', this.submitMilestone);
        wireBtn('dl-outcome-save', this.submitOutcome);
        wireBtn('dl-node-save', this.submitNodeLink);
        wireBtn('dl-node-unlink', this.unlinkNode);
        // UI-2: KPI + tally-outcome modal save buttons.
        wireBtn('dl-kpi-save', this.submitKpi);
        wireBtn('dl-tally-outcome-save', this.submitTallyOutcome);
        wireBtn('dl-kpi-link-save', this.submitKpiLink);
        // Live cockpit: poll while the Deliverable tab is showing and the browser
        // tab is visible; stop when the user leaves either.
        if (!this._missionLiveWired) {
            this._missionLiveWired = true;
            document.addEventListener('shown.bs.tab', (e) => {
                const href = (e.target && e.target.getAttribute) ? e.target.getAttribute('href') : '';
                if (href === '#tab-mission') this._startMissionLive();
                else this._stopMissionLive();
            });
            document.addEventListener('visibilitychange', () => {
                const tab = document.querySelector('#toptab-mission');
                if (document.hidden) this._stopMissionLive();
                else if (tab && tab.classList.contains('active')) this._startMissionLive();
            });
        }
    },
};

document.addEventListener('DOMContentLoaded', () => TeepPlan.init());
