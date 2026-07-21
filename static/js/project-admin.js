/* ACCESS-21: scoped Project Administration UI boundary. */
(function () {
    const methods = {
        _projectAdminSyncSwitcher() {
            const switcher = document.getElementById('project-switcher');
            if (!switcher) return;
            const active = this._settingsProjects || [];
            const current = window.PM_PROJECT || '';
            const currentActive = active.some((project) => project.id === current);
            const archivedContext = current && !currentActive
                ? '<option value="" selected disabled>Archived project (admin view)</option>'
                : '';
            switcher.innerHTML = archivedContext + active.map((project) =>
                `<option value="${this.esc(project.id)}"${project.id === current ? ' selected' : ''}>${this.esc(project.label || project.id)}</option>`).join('');
        },

        _paProjectsForFilter() {
            const filter = this._settingsProjectFilter || 'all';
            return (this._settingsAdminProjects || []).filter((p) =>
                filter === 'all' || String(p.lifecycle_status || 'active') === filter);
        },

        _projectAdminCard(detail, impact) {
            detail = detail || {};
            const p = detail.project || {};
            if (detail.error || !p.id) {
                const message = detail.message || detail.error || 'Project record unavailable';
                return `<div class="card mb-4" id="project-admin-card">
                    <div class="card-header"><div><h3 class="card-title"><i class="ti ti-folders me-2"></i>Project Administration</h3><div class="text-secondary small">Scoped metadata, impact, archive, restore, and lifecycle receipts</div></div>
                        <div class="card-actions"><select id="pa-filter" class="form-select form-select-sm" aria-label="Project lifecycle filter"><option value="all"${(this._settingsProjectFilter || 'all') === 'all' ? ' selected' : ''}>Active + archived</option><option value="active"${this._settingsProjectFilter === 'active' ? ' selected' : ''}>Active only</option><option value="archived"${this._settingsProjectFilter === 'archived' ? ' selected' : ''}>Archived only</option></select></div></div>
                    <div class="card-body"><div class="alert alert-secondary mb-0">${this.esc(message)}</div></div>
                </div>`;
            }
            this._settingsImpact = impact || {};
            const projects = this._paProjectsForFilter();
            const options = projects.map((item) => `<option value="${this.esc(item.id)}"${item.id === p.id ? ' selected' : ''}>${this.esc(item.label || item.id)} · ${this.esc(item.lifecycle_status || 'active')}</option>`).join('');
            const lifecycle = p.lifecycle_status || 'active';
            const archived = lifecycle === 'archived';
            const protectedProject = !!(p.is_protected || p.is_system || p.is_builtin);
            const selectedScopes = (detail.access_summary || {}).effective_scopes || [];
            const selectedSystem = selectedScopes.includes('admin') || selectedScopes.includes('write:system');
            const selectedEditor = selectedSystem || selectedScopes.includes('write:projects');
            const canEdit = selectedEditor && !archived;
            const canEditTrust = selectedSystem && !archived;
            const impactActions = (((impact || {}).recommendation || {}).actions || {});
            const archiveGate = impactActions.archive || {};
            const blockers = (impact && impact.blocking_findings) || [];
            const receipt = (impact && impact.receipt) || {};
            const canArchive = selectedSystem && !archived && !protectedProject && archiveGate.eligible === true && !!receipt.report_hash;
            const canRestore = selectedSystem && archived;
            const lifecycleHint = protectedProject
                ? 'Protected projects cannot be archived.'
                : archived
                    ? (canRestore ? 'Restore is available. Add a reason and confirm the reviewed lifecycle state.' : 'You do not have permission to restore this project.')
                    : canArchive
                        ? 'Archive is available. Add a reason, review the current impact receipt, and confirm before continuing.'
                        : !selectedSystem
                            ? 'You need project lifecycle administration permission to archive this project.'
                            : !receipt.report_hash
                                ? 'Refresh impact to obtain the current archive receipt.'
                                : `Archive is blocked by ${blockers.length} current condition${blockers.length === 1 ? '' : 's'}. Resolve them and refresh impact.`;
            const topo = detail.repo_topology || {};
            const canonical = ((topo.roles || {}).canonical || {});
            const access = (detail.access_summary || {}).access || {};
            const roleCounts = (detail.access_summary || {}).role_counts || {};
            const roleText = Object.keys(roleCounts).sort().map((role) => `${this.esc(role)} ${roleCounts[role]}`).join(' · ') || 'no explicit grants';
            const stateBadge = archived ? 'bg-secondary-lt' : 'bg-green-lt';
            const protectedNote = protectedProject
                ? '<div class="alert alert-warning py-2 px-3 small mb-3"><i class="ti ti-shield-lock me-1"></i><strong>Protected project.</strong> Archive is unavailable; system, built-in, and protected project records remain active.</div>' : '';
            const blockerHtml = blockers.length
                ? `<div class="list-group list-group-flush">${blockers.map((b) => `<div class="list-group-item px-0 py-2"><div class="fw-semibold"><code>${this.esc(b.code || 'blocker')}</code></div><div class="text-secondary small">${this.esc(b.message || b.detail || 'Archive is blocked by current project state.')}</div></div>`).join('')}</div>`
                : '<div class="text-secondary small">No archive blockers in the current impact snapshot.</div>';
            const events = detail.lifecycle_events || [];
            const eventRows = events.length ? events.slice().reverse().map((event) => `<tr>
                <td><code>${this.esc(event.event_id || '')}</code></td>
                <td><span class="badge bg-secondary-lt">${this.esc(event.from_status || '?')} → ${this.esc(event.to_status || '?')}</span></td>
                <td>${this.esc(event.actor || 'system')}</td><td>${this.esc(event.reason || '—')}</td>
                <td class="font-monospace small">${this.esc((event.impact_report_hash || '').slice(0, 20) || '—')}</td></tr>`).join('')
                : '<tr><td colspan="5" class="text-secondary text-center py-3">No lifecycle receipts yet.</td></tr>';
            const immutable = !!p.is_builtin;
            return `<div class="card mb-4" id="project-admin-card">
                <div class="card-header"><div><h3 class="card-title"><i class="ti ti-folders me-2"></i>Project Administration</h3><div class="text-secondary small">Scoped metadata, impact, archive, restore, and lifecycle receipts</div></div>
                    <div class="card-actions d-flex gap-2"><select id="pa-filter" class="form-select form-select-sm" aria-label="Project lifecycle filter"><option value="all"${(this._settingsProjectFilter || 'all') === 'all' ? ' selected' : ''}>Active + archived</option><option value="active"${this._settingsProjectFilter === 'active' ? ' selected' : ''}>Active only</option><option value="archived"${this._settingsProjectFilter === 'archived' ? ' selected' : ''}>Archived only</option></select><button class="btn btn-sm btn-outline-secondary" data-set-action="project-refresh"><i class="ti ti-refresh"></i></button></div></div>
                <div class="card-body">
                    <div class="row g-2 align-items-end mb-3"><div class="col-md-9"><label class="form-label">Accessible project</label><select id="pa-project" class="form-select">${options}</select></div><div class="col-md-3"><span class="badge ${stateBadge} text-uppercase">${this.esc(lifecycle)}</span>${protectedProject ? ' <span class="badge bg-yellow-lt">protected</span>' : ''}</div></div>
                    ${protectedNote}
                    <div class="row g-3">
                        <div class="col-xl-7"><h4>Metadata</h4><div class="row g-2">
                            <div class="col-md-6"><label class="form-label">Label</label><input id="pa-label" class="form-control" value="${this.esc(p.label || '')}"${(!canEdit || immutable) ? ' disabled' : ''}></div>
                            <div class="col-md-6"><label class="form-label">Pretitle</label><input id="pa-pretitle" class="form-control" value="${this.esc(p.pretitle || '')}"${(!canEdit || immutable) ? ' disabled' : ''}></div>
                            <div class="col-12"><label class="form-label">Purpose</label><textarea id="pa-purpose" class="form-control" rows="2"${!canEdit ? ' disabled' : ''}>${this.esc(p.purpose || '')}</textarea></div>
                            <div class="col-12"><label class="form-label">Boundary <span class="badge bg-yellow-lt ms-1">system</span></label><textarea id="pa-boundary" class="form-control" rows="2"${!canEditTrust ? ' disabled' : ''}>${this.esc(p.boundary || '')}</textarea></div>
                            <div class="col-md-6"><label class="form-label">Visibility <span class="badge bg-yellow-lt ms-1">system</span></label><select id="pa-visibility" class="form-select"${!canEditTrust ? ' disabled' : ''}><option value="private"${p.visibility === 'private' ? ' selected' : ''}>private</option><option value="org"${p.visibility !== 'private' ? ' selected' : ''}>org</option></select></div>
                        </div><div class="d-flex align-items-center mt-2"><span id="pa-meta-flash" class="small text-secondary"></span><button class="btn btn-primary btn-sm ms-auto" data-set-action="project-save"${!canEdit ? ' disabled' : ''}><i class="ti ti-device-floppy me-1"></i>Save metadata</button></div></div>
                        <div class="col-xl-5"><h4>Repository &amp; access</h4><dl class="row small mb-2"><dt class="col-5">Canonical repo</dt><dd class="col-7"><code>${this.esc(canonical.repo || 'not configured')}</code></dd><dt class="col-5">Default branch</dt><dd class="col-7">${this.esc(canonical.default_branch || '—')}</dd><dt class="col-5">Visibility</dt><dd class="col-7">${this.esc(access.visibility || p.visibility || 'org')}</dd><dt class="col-5">Organization</dt><dd class="col-7"><code>${this.esc(access.org_id || p.org_id || '—')}</code></dd><dt class="col-5">Owner</dt><dd class="col-7"><code>${this.esc(access.owner_user_id || p.owner_user_id || '—')}</code></dd><dt class="col-5">Grants</dt><dd class="col-7">${roleText}</dd></dl></div>
                    </div>
                    <hr><div class="d-flex align-items-center"><h4 class="mb-0">Impact preview</h4><button class="btn btn-sm btn-outline-secondary ms-auto" data-set-action="project-impact">Refresh impact</button></div>
                    <div class="mt-2"><span class="badge ${archiveGate.eligible ? 'bg-green-lt' : 'bg-yellow-lt'}">${this.esc(((impact || {}).recommendation || {}).action || 'unavailable')}</span> <span class="small text-secondary">receipt <code>${this.esc((receipt.report_hash || '').slice(0, 28) || 'unavailable')}</code></span></div>${blockerHtml}
                    <hr><h4>Lifecycle action <span class="badge bg-orange-lt ms-1">Autopilot exception</span></h4><div class="text-secondary small mb-2">Archiving and restoring change the project boundary, so Switchboard requires explicit human authority. Routine delivery never stops here.</div><div id="pa-life-hint" class="alert ${canArchive || canRestore ? 'alert-info' : 'alert-warning'} py-2 px-3 small">${this.esc(lifecycleHint)}</div><div class="row g-2 align-items-end"><div class="col-md-7"><label class="form-label">Reason</label><input id="pa-reason" class="form-control" placeholder="Why this transition is necessary"${(!canArchive && !canRestore) ? ' disabled' : ''}></div><div class="col-md-5"><label class="form-check mb-2"><input id="pa-confirm" type="checkbox" class="form-check-input"${(!canArchive && !canRestore) ? ' disabled' : ''}><span class="form-check-label">I reviewed the current impact receipt</span></label></div></div>
                    <div class="d-flex align-items-center mt-2"><span id="pa-life-flash" class="small text-secondary" role="status" aria-live="polite"></span><div class="btn-list ms-auto"><button class="btn btn-outline-primary btn-sm" data-set-action="project-restore" data-restore-allowed="${canRestore ? '1' : '0'}"${canRestore ? '' : ' disabled'} title="${this.esc(canRestore ? 'Restore this archived project' : lifecycleHint)}"><i class="ti ti-restore me-1"></i>Restore</button><button class="btn btn-danger btn-sm" data-set-action="project-archive" data-archive-allowed="${canArchive ? '1' : '0'}"${canArchive ? '' : ' disabled'} title="${this.esc(canArchive ? 'Archive this project after reviewing the receipt' : lifecycleHint)}"><i class="ti ti-archive me-1"></i>Archive</button></div></div>
                    <hr><h4>Lifecycle receipts</h4><div class="table-responsive"><table class="table table-sm table-vcenter"><thead><tr><th>Event</th><th>Transition</th><th>Actor</th><th>Reason</th><th>Impact hash</th></tr></thead><tbody>${eventRows}</tbody></table></div>
                </div></div>`;
        },

        _projectAdminChange(element) {
            if (!element) return;
            if (element.id === 'pa-filter') {
                this._settingsProjectFilter = element.value || 'all';
                const candidates = this._paProjectsForFilter();
                if (!candidates.some((p) => p.id === this._settingsProjectId)) this._settingsProjectId = candidates[0]?.id || '';
                return this.renderSettings();
            }
            if (element.id === 'pa-project') {
                this._settingsProjectId = element.value || '';
                return this.renderSettings();
            }
            // UI-20 (2/6): the Access-tokens role preset checks the matching scope boxes.
            if (element.id === 'settings-tokens-role') return this._settingsTokensApplyRole(element.value);
            // UI-20 (4/6): the Members role select is grant-then-revoke (per-role rows).
            if (element.getAttribute && element.getAttribute('data-mm-role')) {
                try { return this._settingsMembersChangeRole(JSON.parse(decodeURIComponent(element.getAttribute('data-mm-role'))), element.value); }
                catch (e) { return; }
            }
            // UI-20 (4/6): the add-member subject kind relabels its input.
            if (element.id === 'mm-kind') {
                const isUser = element.value === 'user';
                const lbl = document.getElementById('mm-subject-label'); if (lbl) lbl.textContent = isUser ? 'Email' : 'Subject id';
                const sub = document.getElementById('mm-subject'); if (sub) sub.placeholder = isUser ? 'teammate@company.com' : 'principal or agent id';
                return;
            }
            if (element.id === 'pa-confirm') {
                this._sFlash('pa-life-flash', element.checked ? 'Receipt review confirmed.' : 'Confirm the receipt review before continuing.', element.checked ? 'text-success' : 'text-secondary');
            }
        },

        _projectAdminAction(action) {
            if (action === 'project-refresh' || action === 'project-impact') return this.renderSettings();
            if (action === 'project-save') return this._projectAdminSave();
            if (action === 'project-archive') return this._projectAdminArchive();
            if (action === 'project-restore') return this._projectAdminRestore();
        },

        async _projectAdminSave() {
            const id = this._settingsProjectId;
            const body = { purpose: this._sv('pa-purpose') };
            const label = document.getElementById('pa-label'), pretitle = document.getElementById('pa-pretitle');
            const boundary = document.getElementById('pa-boundary'), visibility = document.getElementById('pa-visibility');
            if (label && !label.disabled) body.label = label.value.trim();
            if (pretitle && !pretitle.disabled) body.pretitle = pretitle.value.trim();
            if (boundary && !boundary.disabled) body.boundary = boundary.value.trim();
            if (visibility && !visibility.disabled) body.visibility = visibility.value;
            this._sFlash('pa-meta-flash', 'Saving…', 'text-secondary');
            try {
                await this._sSend(`api/projects/${encodeURIComponent(id)}`, 'PATCH', body);
                this._sFlash('pa-meta-flash', 'Saved and audited.', 'text-success');
                await this.renderSettings();
            } catch (e) { this._sFlash('pa-meta-flash', e.message, 'text-danger'); }
        },

        async _projectAdminArchive() {
            const id = this._settingsProjectId, reason = this._sv('pa-reason');
            const receipt = (this._settingsImpact || {}).receipt;
            const confirmed = !!(document.getElementById('pa-confirm') || {}).checked;
            if (!reason) return this._sFlash('pa-life-flash', 'Add a reason before archiving.', 'text-danger');
            if (!confirmed) return this._sFlash('pa-life-flash', 'Confirm that you reviewed the current impact receipt.', 'text-danger');
            if (!receipt || !receipt.report_hash) return this._sFlash('pa-life-flash', 'Refresh impact to obtain a current archive receipt.', 'text-danger');
            this._sFlash('pa-life-flash', 'Archiving against the displayed receipt…', 'text-secondary');
            try {
                await this._sSend(`api/projects/${encodeURIComponent(id)}/archive`, 'POST', { reason, impact_report_receipt: receipt });
                this._sFlash('pa-life-flash', 'Archived.', 'text-success');
                await this.renderSettings();
            } catch (e) { this._sFlash('pa-life-flash', e.message, 'text-danger'); }
        },

        async _projectAdminRestore() {
            const id = this._settingsProjectId, reason = this._sv('pa-reason');
            const confirmed = !!(document.getElementById('pa-confirm') || {}).checked;
            if (!reason) return this._sFlash('pa-life-flash', 'Add a reason before restoring.', 'text-danger');
            if (!confirmed) return this._sFlash('pa-life-flash', 'Confirm that you reviewed the current lifecycle state.', 'text-danger');
            this._sFlash('pa-life-flash', 'Validating access and topology…', 'text-secondary');
            try {
                await this._sSend(`api/projects/${encodeURIComponent(id)}/restore`, 'POST', { reason });
                this._sFlash('pa-life-flash', 'Restored after validation.', 'text-success');
                await this.renderSettings();
            } catch (e) { this._sFlash('pa-life-flash', e.message, 'text-danger'); }
        },
    };
    window.SwitchboardProjectAdmin = Object.freeze({ methods });
})();
