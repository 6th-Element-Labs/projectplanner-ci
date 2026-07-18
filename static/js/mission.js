/* ARCH-MS-21: deliverable mission cockpit and authoring workflows. */
(function (global) {
    'use strict';
    const methods = {
    _missionDeliverableFromUrl() {
        try {
            const u = new URL(window.location.href);
            const d = (u.searchParams.get('deliverable') || u.searchParams.get('mission') || '').trim();
            if (d) this.selectedDeliverableId = d;
            const proof = (u.searchParams.get('proof') || '').trim().toLowerCase();
            const mode = (u.searchParams.get('mode') || '').trim().toLowerCase();
            const proofOn = mode === 'proof' || proof === '1' || proof === 'true' || proof === 'yes';
            if (u.hash === '#tab-mission' || d || proofOn) {
                // Target the TOP tab (in .nav-tabs) — that's the element Bootstrap fires
                // shown.bs.tab on, which drives refreshMissionPage. The sidebar link shares
                // href="#tab-mission" and would otherwise win document.querySelector.
                const tab = document.querySelector('#toptab-mission');
                if (tab && window.bootstrap) {
                    // The inline deep-link handler may have already activated this tab during
                    // page parse (to avoid an Overview flash on refresh). If so, .show() is a
                    // no-op that won't re-fire shown.bs.tab, so drive the cockpit directly;
                    // otherwise .show() fires the event and its listener does the same.
                    const alreadyActive = tab.classList.contains('active');
                    window.bootstrap.Tab.getOrCreateInstance(tab).show();
                    if (alreadyActive) { this.refreshMissionPage(); this._startMissionLive(); }
                }
            }
        } catch (e) { /* ignore */ }
    },

    async _preloadDeliverableDefault() {
        // Overview is the landing tab. We still pre-load deliverables and a default
        // selection so the header switcher and #tab-mission deep links resolve — but we
        // no longer auto-switch the active tab to the Deliverable board on boot.
        try {
            const u = new URL(window.location.href);
            const proof = (u.searchParams.get('proof') || '').trim().toLowerCase();
            const mode = (u.searchParams.get('mode') || '').trim().toLowerCase();
            if (u.hash === '#tab-mission' || u.searchParams.get('deliverable') || u.searchParams.get('mission')
                || mode === 'proof' || proof === '1' || proof === 'true' || proof === 'yes') return;
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

    async loadDeliverables(force = false) {
        const project = window.PM_PROJECT || 'maxwell';
        if (!force && this._deliverablesProject === project) return this.deliverables;
        // Deep links used to trigger refreshMissionPage while init simultaneously
        // populated the header, producing duplicate full list calls. Share one
        // in-flight request across every picker consumer.
        if (this._deliverablesPromise) return this._deliverablesPromise;
        const request = (async () => {
            const boot = window.TAIKUN_PICKER_BOOT;
            const prefetched = !force && boot && boot.projectId === project
                ? await boot.deliverables
                : null;
            if (prefetched !== null) {
                this.deliverables = prefetched;
                this._deliverablesProject = project;
                return this.deliverables;
            }
            const res = await fetch('api/deliverables?view=picker');
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            this.deliverables = data.deliverables || [];
            this._deliverablesProject = project;
            return this.deliverables;
        })();
        this._deliverablesPromise = request;
        try { return await request; }
        finally { if (this._deliverablesPromise === request) this._deliverablesPromise = null; }
    },

    async loadMissionStatus(deliverableId) {
        const id = (deliverableId || '').trim();
        if (!id) { this.missionStatus = null; return null; }
        // BUG-A11: share one in-flight promise per deliverable (same as loadDeliverables).
        if (this._missionStatusPromise && this._missionStatusId === id) {
            return this._missionStatusPromise;
        }
        const request = (async () => {
            // CONSOL-8: no-cache (not no-store) — ETag lets unchanged ticks return 304.
            const res = await fetch(`api/deliverables/${encodeURIComponent(id)}/mission_status`, { cache: 'no-cache' });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
            this.missionStatus = data;
            return data;
        })();
        this._missionStatusId = id;
        this._missionStatusPromise = request;
        try { return await request; }
        finally {
            if (this._missionStatusPromise === request) {
                this._missionStatusPromise = null;
                this._missionStatusId = null;
            }
        }
    },

    async loadDependencyGraph(deliverableId) {
        const id = (deliverableId || '').trim();
        if (!id) { this.missionGraph = null; return null; }
        if (this._missionGraphPromise && this._missionGraphId === id) {
            return this._missionGraphPromise;
        }
        const request = (async () => {
            const res = await fetch(`api/deliverables/${encodeURIComponent(id)}/dependency_graph`, { cache: 'no-cache' });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
            this.missionGraph = data;
            return data;
        })();
        this._missionGraphId = id;
        this._missionGraphPromise = request;
        try { return await request; }
        finally {
            if (this._missionGraphPromise === request) {
                this._missionGraphPromise = null;
                this._missionGraphId = null;
            }
        }
    },

    async loadAutopilotScopes(deliverableId) {
        const id = (deliverableId || '').trim();
        if (!id) { this.autopilotScopes = []; return []; }
        try {
            const res = await fetch(`api/deliverables/${encodeURIComponent(id)}/autopilot`, { cache: 'no-store' });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
            this.autopilotScopes = Array.isArray(data.scopes) ? data.scopes : [];
        } catch (e) {
            this.autopilotScopes = [];
        }
        return this.autopilotScopes;
    },

    _autopilotScope(scopeType, taskId, taskProject) {
        const type = scopeType || 'deliverable';
        const tid = String(taskId || '').toUpperCase();
        return (this.autopilotScopes || []).find((scope) => {
            if (scope.scope_type !== type) return false;
            if (type === 'deliverable') return true;
            return String(scope.task_id || '').toUpperCase() === tid
                && (!taskProject || scope.task_project === taskProject);
        }) || null;
    },

    _taskAutopilotState(taskId, taskProject) {
        const deliverable = this._autopilotScope('deliverable');
        if (deliverable) return { scope: deliverable, covered: true };
        return { scope: this._autopilotScope('task', taskId, taskProject), covered: false };
    },

    _missionAutopilotControlsHtml() {
        const scope = this._autopilotScope('deliverable');
        const status = scope && scope.status;
        const last = (scope && scope.last_result) || {};
        const taskReceipts = (last.receipts || []).length;
        const stateLabel = status === 'paused' ? 'Paused'
            : (last.status === 'waiting' ? 'Waiting for dependencies/capacity' : 'Autopilot running');
        if (!scope) {
            return `<div class="d-flex flex-column align-items-end gap-1">
                <button class="btn btn-primary" type="button" data-autopilot-action="start" data-autopilot-scope="deliverable">
                    <i class="ti ti-player-play me-1"></i>Start deliverable</button>
                <span class="text-secondary small">Starts every ready task and keeps advancing.</span>
                <span id="mission-autopilot-flash" class="small"></span></div>`;
        }
        return `<div class="d-flex flex-column align-items-end gap-1">
            <div class="btn-list justify-content-end">
                <span class="badge bg-${status === 'paused' ? 'yellow' : 'green'}-lt"><span class="status-dot ${status === 'active' ? 'status-dot-animated ' : ''}bg-${status === 'paused' ? 'yellow' : 'green'} me-1"></span>${this.esc(stateLabel)}${taskReceipts ? ` · ${taskReceipts} this wave` : ''}</span>
                <button class="btn btn-sm btn-outline-primary" type="button" data-autopilot-action="${status === 'paused' ? 'resume' : 'pause'}" data-autopilot-scope="deliverable"><i class="ti ti-${status === 'paused' ? 'player-play' : 'player-pause'} me-1"></i>${status === 'paused' ? 'Resume' : 'Pause'}</button>
                <button class="btn btn-sm btn-outline-danger" type="button" data-autopilot-action="stop" data-autopilot-scope="deliverable"><i class="ti ti-player-stop me-1"></i>Stop</button>
            </div><span id="mission-autopilot-flash" class="small text-secondary"></span></div>`;
    },

    _taskAutopilotButtonHtml(taskId, taskProject, compact) {
        const state = this._taskAutopilotState(taskId, taskProject);
        if (state.covered) {
            return `<span class="badge bg-green-lt" title="Included in the active deliverable run"><i class="ti ti-player-play me-1"></i>Included</span>`;
        }
        if (state.scope) {
            const paused = state.scope.status === 'paused';
            const waiting = (state.scope.last_result || {}).status === 'waiting';
            return `<div class="btn-list flex-nowrap justify-content-end">
                <span class="badge bg-${paused ? 'yellow' : 'blue'}-lt">${paused ? 'Paused' : (waiting ? 'Waiting' : 'Armed')}</span>
                <button class="btn btn-sm btn-outline-${paused ? 'primary' : 'secondary'}" type="button" data-autopilot-action="${paused ? 'resume' : 'pause'}" data-autopilot-scope="task" data-autopilot-task="${this.esc(taskId)}" data-autopilot-project="${this.esc(taskProject || '')}" title="${paused ? 'Resume task Autopilot' : 'Pause task Autopilot'}"><i class="ti ti-${paused ? 'player-play' : 'player-pause'}${compact ? '' : ' me-1'}"></i>${compact ? '' : (paused ? 'Resume task' : 'Pause task')}</button>
                <button class="btn btn-sm btn-outline-danger" type="button" data-autopilot-action="stop" data-autopilot-scope="task" data-autopilot-task="${this.esc(taskId)}" data-autopilot-project="${this.esc(taskProject || '')}" title="Stop task Autopilot"><i class="ti ti-player-stop${compact ? '' : ' me-1'}"></i>${compact ? '' : 'Stop task'}</button>
            </div>`;
        }
        return `<button class="btn btn-sm btn-primary" type="button" data-autopilot-action="start" data-autopilot-scope="task" data-autopilot-task="${this.esc(taskId)}" data-autopilot-project="${this.esc(taskProject || '')}"><i class="ti ti-player-play me-1"></i>${compact ? 'Start' : 'Start task'}</button>`;
    },

    async controlAutopilot(action, scopeType, taskId, taskProject) {
        const deliverableId = (this.selectedDeliverableId || '').trim();
        if (!deliverableId) return;
        const flash = document.getElementById('mission-autopilot-flash');
        if (flash) { flash.className = 'small text-secondary'; flash.textContent = `${action === 'start' ? 'Starting' : action + 'ing'}…`; }
        const isTask = scopeType === 'task';
        const path = isTask
            ? `api/deliverables/${encodeURIComponent(deliverableId)}/tasks/${encodeURIComponent(taskId)}/autopilot`
            : `api/deliverables/${encodeURIComponent(deliverableId)}/autopilot`;
        try {
            const res = await fetch(path, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action, task_project: taskProject || window.PM_PROJECT || 'maxwell', runtime: 'codex' }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || data.error) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
            await this.loadAutopilotScopes(deliverableId);
            await this.refreshMissionPage();
        } catch (e) {
            if (flash) { flash.className = 'small text-danger'; flash.textContent = `Autopilot failed: ${e.message}`; }
            else window.alert(`Autopilot failed: ${e.message}`);
        }
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

    // UI-11: deliverables the picker should show — archived ones are hidden unless the
    // "Show archived" toggle is on, but the currently-selected one always stays visible.
    _pickerDeliverables() {
        const showArchived = !!(document.getElementById('mission-show-archived') || {}).checked;
        const cur = this.selectedDeliverableId;
        return (this.deliverables || []).filter((d) =>
            showArchived || (d.status || '') !== 'archived' || d.id === cur);
    },

    // UI-11: reflect the selected deliverable's archived state on the header button.
    _syncArchiveButton() {
        const btn = document.getElementById('mission-archive');
        if (!btn) return;
        const cur = (this.deliverables || []).find((d) => d.id === this.selectedDeliverableId);
        if (!cur) { btn.style.display = 'none'; return; }
        btn.style.display = '';
        const isArchived = (cur.status || '') === 'archived';
        btn.innerHTML = isArchived
            ? '<i class="ti ti-archive-off me-1"></i>Unarchive'
            : '<i class="ti ti-archive me-1"></i>Archive';
        btn.title = isArchived ? 'Restore this deliverable' : 'Archive this deliverable';
    },

    // UI-11: archive / restore the selected deliverable (explicit confirm on archive).
    // Reusable Tabler confirm dialog → Promise<boolean> (true = confirmed, false = dismissed).
    // Falls back to window.confirm if the modal / Bootstrap isn't available.
    _confirm(opts) {
        opts = opts || {};
        const modalEl = document.getElementById('confirm-modal');
        if (!modalEl || !(window.bootstrap && window.bootstrap.Modal)) {
            return Promise.resolve(window.confirm(opts.body || opts.title || 'Are you sure?'));
        }
        document.getElementById('confirm-modal-title').textContent = opts.title || 'Are you sure?';
        document.getElementById('confirm-modal-body').textContent = opts.body || '';
        document.getElementById('confirm-modal-icon').className =
            'ti mb-2 ' + (opts.icon || 'ti-help-circle') + ' text-' + (opts.iconVariant || 'secondary');
        const ok = document.getElementById('confirm-modal-ok');
        ok.className = 'btn w-100 btn-' + (opts.confirmVariant || 'primary');
        ok.textContent = opts.confirmLabel || 'Confirm';
        const modal = window.bootstrap.Modal.getOrCreateInstance(modalEl);
        return new Promise((resolve) => {
            let settled = false;
            const finish = (val) => {
                if (settled) return;
                settled = true;
                ok.removeEventListener('click', onOk);
                modalEl.removeEventListener('hidden.bs.modal', onHide);
                resolve(val);
            };
            const onOk = () => { finish(true); modal.hide(); };
            const onHide = () => finish(false);
            ok.addEventListener('click', onOk);
            modalEl.addEventListener('hidden.bs.modal', onHide);
            modal.show();
        });
    },

    async _archiveSelectedDeliverable() {
        const id = (this.selectedDeliverableId || '').trim();
        if (!id) return;
        const cur = (this.deliverables || []).find((d) => d.id === id);
        const isArchived = cur && (cur.status || '') === 'archived';
        const willArchive = !isArchived;
        const title = (cur && (cur.title || cur.id)) || id;
        if (willArchive) {
            const ok = await this._confirm({
                title: `Archive “${title}”?`,
                body: 'It will be hidden from the deliverable picker — tick “Show archived” to find it again. Nothing is deleted.',
                icon: 'ti-archive', iconVariant: 'secondary',
                confirmLabel: 'Archive', confirmVariant: 'primary',
            });
            if (!ok) return;
        }
        try {
            const res = await fetch(`api/deliverables/${encodeURIComponent(id)}/archive`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ archived: willArchive }),
            });
            if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || d.error || `HTTP ${res.status}`); }
            // Just archived and archived ones are hidden → drop the selection so the picker
            // falls to the first visible deliverable.
            if (willArchive && !(document.getElementById('mission-show-archived') || {}).checked) {
                this.selectedDeliverableId = '';
            }
            await this.refreshMissionPage(true);
        } catch (e) {
            window.alert(`Could not ${willArchive ? 'archive' : 'restore'} the deliverable: ${e.message}`);
        }
    },

    async refreshMissionPage(reloadDeliverables = false) {
        const el = document.getElementById('mission-page');
        const picker = document.getElementById('mission-deliverable-picker');
        if (!el) return;
        // Warm the Mermaid bundle in parallel with the data fetch so the dependency
        // map isn't waiting on a cold ~1MB CDN download after the data is already in.
        this._ensureScript(this.MERMAID_SRC).catch(() => {});
        el.innerHTML = '<div class="text-secondary small">Loading mission…</div>';
        try { await this.loadDeliverables(reloadDeliverables); }
        catch (e) {
            el.innerHTML = `<div class="alert alert-danger mb-0">Could not load deliverables: ${this.esc(e.message)}</div>`;
            return;
        }
        if (picker) {
            const visible = this._pickerDeliverables();
            let cur = this.selectedDeliverableId;
            if (cur && !visible.some((d) => d.id === cur)) { cur = ''; this.selectedDeliverableId = ''; }
            picker.innerHTML = visible.length
                ? visible.map((d) =>
                    `<option value="${this.esc(d.id)}"${d.id === cur ? ' selected' : ''}>${this.esc(d.title || d.id)}${(d.status || '') === 'archived' ? ' · archived' : ''}</option>`).join('')
                : '<option value="">No deliverables yet</option>';
            if (!cur && visible.length) {
                this.selectedDeliverableId = visible[0].id;
                picker.value = this.selectedDeliverableId;
            }
        }
        this._syncHeaderDeliverable();
        this._syncArchiveButton();
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
                this.loadAutopilotScopes(this.selectedDeliverableId),
                this.loadBreakdownProposals(this.selectedDeliverableId),
                this.loadKpisAndOutcomes(), this.loadClosureReport(this.selectedDeliverableId),
            ]);
            this._setMissionDeliverableInUrl(this.selectedDeliverableId);
            // UI-17: when ?proof=1 / mode=proof, load bind + provider state before render.
            if (typeof this._proofModeFromUrl === 'function' && this._proofModeFromUrl()) {
                const s = this.missionStatus || {};
                const taskIds = [
                    ...((s.active_work || []).map((w) => w.task_id)),
                    ...((s.linked_tasks || []).map((l) => l.task_id)),
                ].filter(Boolean);
                this._proofBind = typeof this._proofLoadBindState === 'function'
                    ? await this._proofLoadBindState(taskIds)
                    : null;
            } else {
                this._proofBind = null;
            }
            this.renderMissionPage();
            if (this._proofBind && typeof this._initProofConsole === 'function') {
                await this._initProofConsole(this.missionStatus);
            }
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
        // Logical workflow order: an external dep is upstream of everything; then the
        // lifecycle a linked task actually moves through (not started → ... → done);
        // Blocker is appended last (below) since it's an overlay on any of these states,
        // not a stage of its own.
        const legend = [
            ['external', 'External dep', '#f8f9fa', '#adb5bd'],
            ['todo', 'Not started', '#e9ecef', '#6c757d'],
            ['in_progress', 'In progress', '#8fb8fd', '#0b5ed7'],
            ['in_review', 'In review', '#ffe083', '#e0a800'],
            ['blocked', 'Blocked', '#f5a3a9', '#c82333'],
            ['done_unproven', 'Done (no proof)', '#a6e3d0', '#12b886'],
            ['done', 'Done ✓ proof', '#a3d9b7', '#1e7e34'],
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
        return `<div class="card mb-4" id="mission-dag-panel"><div class="card-header d-flex flex-wrap align-items-center gap-2">
            <h3 class="card-title mb-0 text-nowrap"><i class="ti ti-git-fork me-2"></i>Dependency map</h3>
            <div class="text-secondary small mx-auto text-center">${[
                [stats.done_count, 'done'],
                [stats.done_unproven_count, 'done · no proof'],
                [stats.in_progress_count, 'in progress'],
                [stats.in_review_count, 'in review'],
                [stats.blocked_count, 'blocked'],
                [stats.todo_count, 'not started'],
                [stats.external_node_count, 'external'],
                [stats.context_task_count, 'context'],
            ].filter(([n]) => n).map(([n, l]) => `${n} ${l}`).join(' · ') || 'no tasks'}</div>
            ${this._missionAuthorButtons()}
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
            const { svg } = await Promise.race([
                window.mermaid.render(renderId, g.mermaid),
                new Promise((_, rej) => setTimeout(() => rej(new Error('mermaid render timeout')), 12000)),
            ]);
            host.innerHTML = svg;
            // Setting innerHTML resets the scroller to 0,0 — put the chart back where the user
            // had it (captured in renderMissionPage before the re-render).
            const _sc = this._missionDagScroll;
            if (_sc) { host.scrollLeft = _sc.left; host.scrollTop = _sc.top; }
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
            nodeEl.addEventListener('click', async (e) => {
                e.preventDefault();
                if (!hit) return;
                // UI-24: a task with a live/watchable runner opens straight into its
                // bound terminal (the sidecar); otherwise fall back to the existing
                // deliverable-link node-actions modal (UI-1).
                if (typeof this.openRunnerSessionPanel === 'function') {
                    const opened = await this.openRunnerSessionPanel(hit.id, { fallbackIfNotWatchable: true });
                    if (opened) return;
                }
                this.openNodeModal(hit.id, hit.project_id);
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
        const scopes = (this.autopilotScopes || []).map((scope) => `${scope.scope_id}:${scope.status}:${scope.updated_at}`).sort();
        return JSON.stringify([nodeSig, active, blockers, scopes, s.progress || {}, g.stats || {}, (s.deliverable || {}).status, ((this.missionClosure || {}).report || {}).report_id, ((this.missionClosure || {}).report || {}).grade]);
    },

    _missionLiveStamp(changed) {
        const el = document.getElementById('mission-live-stamp');
        if (!el) return;
        const d = new Date();
        const t = [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, '0')).join(':');
        el.textContent = changed ? `updated ${t}` : `checked ${t}`;
    },

    async _missionLiveTick() {
        const tab = document.querySelector('#toptab-mission');
        if (!tab || !tab.classList.contains('active')) return;   // only when the mission tab is showing
        // Keep every open deliverable tab live even when the browser tab is backgrounded (the
        // user runs many tabs and wants them all current) — just poll hidden tabs less often.
        // Immediate refresh on refocus is wired in app.js's visibilitychange handler.
        if (!this._pollDueWhileHidden('_missionHiddenAt')) return;
        const id = (this.selectedDeliverableId || '').trim();
        if (!id || this._missionLiveBusy) return;
        this._missionLiveBusy = true;
        try {
            await Promise.all([this.loadMissionStatus(id), this.loadDependencyGraph(id), this.loadAutopilotScopes(id), this.loadClosureReport(id)]);
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
        const picker = document.getElementById('mission-deliverable-picker');
        const list = this._pickerDeliverables();
        if (!list.length) {
            if (wrap) wrap.style.display = 'none';
            if (picker) picker.innerHTML = '<option value="">No deliverables yet</option>';
            return;
        }
        if (!this.selectedDeliverableId) this.selectedDeliverableId = list[0].id;
        if (wrap) wrap.style.display = '';
        const cur = this.selectedDeliverableId || list[0].id;
        const options = list.map((d) =>
            `<option value="${this.esc(d.id)}"${d.id === cur ? ' selected' : ''}>${this.esc(d.title || d.id)}${(d.status || '') === 'archived' ? ' · archived' : ''}</option>`).join('');
        this._syncArchiveButton();
        if (picker) {
            picker.innerHTML = options;
            if (cur) picker.value = cur;
        }
        if (!sel) return;
        sel.innerHTML = options;
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
        const proofOn = typeof this._proofModeFromUrl === 'function' && this._proofModeFromUrl();
        const proofToggle = `<button type="button" class="btn btn-sm ${proofOn ? 'btn-primary' : 'btn-outline-secondary'}" id="mission-proof-toggle" title="Operator Proof Console deep link (?proof=1)">
            <i class="ti ti-terminal-2 me-1"></i>${proofOn ? 'Exit proof' : 'Proof console'}</button>`;
        const header = `<div class="d-flex flex-wrap align-items-start gap-3 mb-4"><div class="flex-fill">
            <div class="text-secondary small mb-1">${this.esc(s.project_id || window.PM_PROJECT || '')}${s.board_id ? ' · ' + this.esc(s.board_id) : ''}</div>
            <h2 class="mb-2">${this.esc(d.title || s.deliverable_id || 'Mission')}</h2>
            <div class="btn-list">${this._missionBadge(d.status, this.DELIVERABLE_STATUS_COLOR)} ${this._missionConfidence(board.confidence)} ${proofToggle}</div>
        </div>
        <div class="text-end"><div class="mb-2">${this._missionAutopilotControlsHtml()}</div><div class="mb-2">${this._missionClosureActionHtml()}</div>
            <span class="badge bg-green-lt" title="Live — auto-refreshes as agents update tasks"><span class="status-dot status-dot-animated bg-green me-1"></span>Live</span>
            <div id="mission-live-stamp" class="text-secondary small mt-1"></div>
        </div></div>
        <div id="mission-session-health-strip" class="d-flex flex-wrap align-items-center gap-2 mb-4"></div>`;
        const proofHtml = (proofOn && typeof this.proofConsoleHtml === 'function')
            ? this.proofConsoleHtml(s, this._proofBind || {})
            : '';
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
            return `<tr><td>${this.esc(link.project_id || '')}</td><td><a href="#" data-linked-task="${this.esc(link.task_id)}" data-linked-project="${this.esc(link.project_id)}">${this.esc(link.task_id)}</a></td><td>${this.esc(dtl.title || dtl.error || '')}</td><td>${this._missionBadge(dtl.status || 'missing', this.STATUS_COLOR)}</td><td>${this.esc(link.milestone_id || '—')}</td><td>${this.esc(link.role || '—')}</td><td class="text-end">${dtl.status === 'Done' ? '<span class="text-secondary small">Done</span>' : this._taskAutopilotButtonHtml(link.task_id, link.project_id, true)}</td></tr>`;
        }).join('') || '<tr><td colspan="7" class="text-secondary">No cross-project links</td></tr>';
        // Blockers box removed — it dumped raw kinds like "dependency_unsatisfied". The
        // dependency map already outlines blockers with a thick dark border.
        const blockerHtml = '';
        const nextActions = this._missionActionsHtml(s);
        const agents = (s.active_agents || []).length ? `<div class="card mb-4"><div class="card-header"><h3 class="card-title">Live agents</h3></div><div class="table-responsive"><table class="table table-vcenter card-table"><thead><tr><th>Agent</th><th>Task</th><th>Project</th></tr></thead><tbody>${(s.active_agents || []).map((a) =>
            `<tr><td>${this.esc(a.agent_id || '')}</td><td><a href="#" data-linked-task="${this.esc(a.task_id)}" data-linked-project="${this.esc(a.project_id)}">${this.esc(a.task_id || '')}</a></td><td>${this.esc(a.project_id || '')}</td></tr>`).join('')}</tbody></table></div></div>` : '';
        const activeRows = (s.active_work || []).map((w) => `<tr><td><a href="#" data-linked-task="${this.esc(w.task_id)}" data-linked-project="${this.esc(w.project_id)}">${this.esc(w.task_id)}</a></td><td>${this.esc(w.title || '')}</td><td>${this._missionBadge(w.status, this.STATUS_COLOR)}</td><td>${this.sessionHealthPill(w.session_health)}</td><td>${this.esc(w.assignee || '—')}</td><td class="small">${this.esc((w.active_claims || []).map((c) => c.agent_id).join(', ') || '—')}</td></tr>`).join('') || '<tr><td colspan="6" class="text-secondary">No active linked work</td></tr>';
        // Keep the currently-rendered graph so a live re-render can show it until the new
        // SVG is ready (no blank/flash while colours update in place).
        const _prevGraphSvg = el.querySelector('#mission-dag-graph svg');
        // Preserve the dependency-graph scroll offset across the re-render so a live colour
        // update doesn't yank a scrolled-in chart back to the top-left. Restored onto the new
        // container below and again by _renderMissionMermaid once the fresh SVG mounts.
        const _prevGraphEl = el.querySelector('#mission-dag-graph');
        this._missionDagScroll = _prevGraphEl ? { left: _prevGraphEl.scrollLeft, top: _prevGraphEl.scrollTop } : null;
        const _prevDetail = el.querySelector('#mission-detail');
        const detailOpen = _prevDetail ? _prevDetail.open : !!this._missionDetailOpen;
        this._missionDetailOpen = detailOpen;
        // Lead with the story: headline → plain-English → what's blocked → the map →
        // breakdown/outcomes review → next action.
        const essentials = header + proofHtml + this._missionClosureHtml() + this._missionCeoHeaderHtml(s) + blockerHtml
            + this._missionDependencyGraphHtml() + this._missionBreakdownHtml() + nextActions;
        // The rest (KPIs, brief, milestones, work tables, agents, linked tasks, policy) folds
        // into a disclosure so it's there when you want it, not a wall of ~15 cards up front.
        const detail = kpi + this._missionEconomicsHtml(s.economics) + this._missionKpiOutcomesHtml() + narrative + endState + milestoneMap +
            `<div class="row g-3 mb-4"><div class="col-lg-6"><div class="card h-100"><div class="card-header"><h3 class="card-title">Active work</h3></div><div class="table-responsive"><table class="table table-vcenter card-table"><thead><tr><th>Task</th><th>Title</th><th>Status</th><th>Session</th><th>Assignee</th><th>Claims</th></tr></thead><tbody>${activeRows}</tbody></table></div></div></div>
            <div class="col-lg-6"><div class="card h-100"><div class="card-header"><h3 class="card-title">Done with proof</h3></div><div class="table-responsive"><table class="table table-vcenter card-table"><thead><tr><th>Task</th><th>Title</th><th>Provenance</th><th>PR</th></tr></thead><tbody>${doneRows}</tbody></table></div></div></div></div>` +
            agents +
            `<div class="card mb-4"><div class="card-header"><h3 class="card-title">Linked tasks across projects</h3></div><div class="table-responsive"><table class="table table-vcenter card-table"><thead><tr><th>Project</th><th>Task</th><th>Title</th><th>Status</th><th>Milestone</th><th>Role</th><th class="text-end">Autopilot</th></tr></thead><tbody>${linkedRows}</tbody></table></div></div>` +
            `<div class="row g-3"><div class="col-lg-6"><div class="card h-100"><div class="card-header"><h3 class="card-title">Architecture / policy</h3></div><div class="card-body">${this._missionPolicyDrift(s)}</div></div></div>
            <div class="col-lg-6"><div class="card h-100"><div class="card-header"><h3 class="card-title">Recent changes</h3></div><div class="card-body">${this._missionRecentChanges(s.linked_tasks)}</div></div></div></div>`;
        el.innerHTML = essentials +
            `<details id="mission-detail" class="mb-4"${detailOpen ? ' open' : ''}>
                <summary class="text-secondary py-2"><i class="ti ti-chevron-right mission-detail-chev me-1"></i>Full detail — KPIs, brief, milestones, work, agents, linked tasks</summary>
                <div class="pt-3">${detail}</div>
            </details>`;
        const _gh = el.querySelector('#mission-dag-graph');
        if (_prevGraphSvg && _gh && !_gh.querySelector('svg')) _gh.appendChild(_prevGraphSvg);
        if (_gh && this._missionDagScroll) { _gh.scrollLeft = this._missionDagScroll.left; _gh.scrollTop = this._missionDagScroll.top; }
        this._renderMissionMermaid();
        // Re-baseline the live signature so the poller only re-renders on the NEXT
        // real change, and stamp the freshly-rendered "updated" time.
        this._missionSig = this._missionSignature();
        this._missionLiveStamp(true);
        this.renderFleetDock({ mode: 'deliverable', taskIds: (s.linked_tasks || []).map((l) => l.task_id) });
        const proofBtn = document.getElementById('mission-proof-toggle');
        if (proofBtn && !proofBtn._bound) {
            proofBtn.addEventListener('click', () => {
                if (typeof this.toggleProofConsole === 'function') this.toggleProofConsole();
            });
            proofBtn._bound = true;
        }
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
            case 'closure-request': return this.requestClosureVerification();
            case 'closure-dismiss': return this.dismissClosure();
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
    _dlHide(id) {
        const m = document.getElementById(id);
        if (m) window.bootstrap.Modal.getOrCreateInstance(m).hide();
    },
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
        const detail = link.task_detail || link.task || {};
        if (detail.status !== 'Done') {
            body.insertAdjacentHTML('beforeend', `<div class="mt-3" id="dl-node-autopilot">${this._taskAutopilotButtonHtml(id, taskProject, false)}</div>`);
        }
        this._dlFlash('dl-node-flash', '', 'text-secondary');
        document.getElementById('dl-node-open')?.addEventListener('click', (e) => {
            e.preventDefault();
            this._dlHide('dl-node-modal');
            this.openLinkedTask(id, taskProject);
        });
        document.getElementById('dl-node-autopilot')?.addEventListener('click', (e) => {
            const btn = e.target.closest('[data-autopilot-action]');
            if (!btn) return;
            // The node modal lives inside #mission-page, which also owns the
            // delegated task-row controls. Keep this click local so one user
            // action cannot issue the same idempotent request twice.
            e.preventDefault();
            e.stopPropagation();
            this._dlHide('dl-node-modal');
            this.controlAutopilot(btn.dataset.autopilotAction, 'task', id, taskProject);
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

    // ---- UI-9: project & provenance admin (admin-gated Settings tab) --------
    // Every write here maps to an existing REST endpoint that already enforces
    // write:system server-side; the tab is only revealed to callers who have it,
    // and each action is audited by the actor the backend resolves.

    };
    global.SwitchboardMission = Object.freeze({ methods });
})(window);
