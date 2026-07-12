/* DELIVERABLES-18: closure verification controls and report presentation. */
(function (global) {
    'use strict';
    const methods = {
    async loadClosureReport(deliverableId) {
        const id = (deliverableId || '').trim();
        if (!id) { this.missionClosure = { report: null, missing: true }; return this.missionClosure; }
        try {
            const res = await fetch(`api/deliverables/${encodeURIComponent(id)}/closure_report`, { cache: 'no-store' });
            let data = {};
            try { data = await res.json(); } catch (e) { /* preserve the HTTP failure below */ }
            if (res.status === 404) {
                this.missionClosure = { report: null, history: [], missing: true };
            } else if (!res.ok) {
                this.missionClosure = { report: null, error: this._dlHttpErr(res, data) };
            } else {
                this.missionClosure = data || { report: null, missing: true };
            }
        } catch (e) {
            this.missionClosure = { report: null, error: e.message || 'Closure report request failed.' };
        }
        return this.missionClosure;
    },

    _missionClosureActionHtml() {
        return `<button id="mission-closure-request" class="btn btn-sm btn-outline-primary" type="button" data-dl-action="closure-request" title="Dispatch a verifier to run the registered closure gates"><i class="ti ti-rosette-discount-check me-1"></i>Verify &amp; stamp closure</button>`;
    },

    _closureCheckRows(report) {
        const rows = [];
        Object.entries((report || {}).gates || {}).forEach(([gateId, gate]) => {
            const detail = (gate && typeof gate === 'object') ? gate : {};
            const checks = Array.isArray(detail.checks) ? detail.checks : [];
            if (!checks.length) rows.push({ gate: gateId, id: gateId, pass: detail.pass, message: detail.summary || detail.message || '' });
            checks.forEach((check, index) => rows.push({
                gate: gateId,
                id: check.id || check.name || `${gateId}-${index + 1}`,
                pass: check.pass,
                message: check.message || check.summary || check.error || '',
            }));
        });
        return rows;
    },

    _missionClosureHtml() {
        const state = this.missionClosure || {};
        const report = state.report || null;
        const request = this.missionClosureRequest || {};
        if (state.error) return `<div class="alert alert-danger mb-4"><i class="ti ti-alert-triangle me-1"></i><strong>Closure report unavailable.</strong> ${this.esc(state.error)}</div>`;
        if (!report) {
            const requested = request.dispatched || request.already_dispatched || request.ok;
            return `<div class="card mb-4" id="mission-closure-card"><div class="card-body d-flex flex-wrap align-items-center gap-2">
                <div><div class="subheader">Closure verification</div><div class="text-secondary small">${request.queued ? 'Verification queued; waiting for a work-capable host.' : (requested ? 'Verification requested; waiting for the verifier to stamp a report.' : 'No closure report yet. Use the header action to run the closure gates.')}</div></div>
                <span class="badge bg-secondary-lt ms-auto">NOT STAMPED</span></div></div>`;
        }
        const grade = String(report.grade || state.grade || 'unknown').toLowerCase();
        const colors = { pass: 'green', hold: 'red', waive: 'azure' };
        const color = colors[grade] || 'secondary';
        const reportId = report.report_id || '';
        const summary = report.summary || report.recommendation || state.summary || 'Closure gates completed.';
        const checks = this._closureCheckRows(report);
        const rows = checks.length ? checks.map((check) => {
            const known = check.pass === true || check.pass === false;
            const badge = known
                ? `<span class="badge bg-${check.pass ? 'green' : 'red'}-lt">${check.pass ? 'PASS' : 'FAIL'}</span>`
                : '<span class="badge bg-secondary-lt">NOT RUN</span>';
            return `<tr><td>${this.esc(check.gate)}</td><td class="fw-semibold">${this.esc(check.id)}</td><td>${badge}</td><td class="text-secondary small">${this.esc(check.message || '—')}</td></tr>`;
        }).join('') : '<tr><td colspan="4" class="text-secondary">No individual checks recorded.</td></tr>';
        const project = encodeURIComponent(this._pmProject());
        const deliverable = encodeURIComponent(this.selectedDeliverableId || report.deliverable_id || '');
        const fullUrl = `api/deliverables/${deliverable}/closure_report?project=${project}${reportId ? `&report_id=${encodeURIComponent(reportId)}` : ''}`;
        return `<div class="card mb-4" id="mission-closure-card"><div class="card-header">
            <div><h3 class="card-title"><i class="ti ti-rosette-discount-check me-2"></i>Closure verification</h3><div class="text-secondary small mt-1">${this.esc(summary)}</div></div>
            <div class="card-actions d-flex align-items-center gap-2"><span class="badge bg-${color}-lt">GRADE ${this.esc(grade.toUpperCase())}</span><a class="btn btn-sm btn-ghost-secondary" href="${this.esc(fullUrl)}" target="_blank" rel="noopener">Full report</a></div>
        </div><div class="table-responsive"><table class="table table-vcenter card-table"><thead><tr><th>Gate</th><th>Check</th><th>Result</th><th>Evidence / summary</th></tr></thead><tbody>${rows}</tbody></table></div>
        <div class="card-footer text-secondary small">${reportId ? `Report ${this.esc(reportId)}` : 'Latest report'}${report.generated_by ? ` · ${this.esc(report.generated_by)}` : ''}</div></div>`;
    },

    async requestClosureVerification() {
        const id = (this.selectedDeliverableId || '').trim();
        if (!id) return;
        const btn = document.getElementById('mission-closure-request');
        if (btn) btn.disabled = true;
        this.missionClosureRequest = {};
        try {
            const result = await this._dlSend(`api/deliverables/${encodeURIComponent(id)}/closure_request`, 'POST', {});
            if (!result.dispatched) throw new Error(result.error || 'Verifier dispatch was not accepted.');
            this.missionClosureRequest = result;
            await this.loadClosureReport(id);
        } catch (e) {
            this.missionClosureRequest = { error: e.message || 'Could not request closure verification.' };
            this.missionClosure = { report: null, error: this.missionClosureRequest.error };
        } finally {
            if (btn) btn.disabled = false;
            this.renderMissionPage();
        }
    },
    };
    global.SwitchboardClosure = Object.freeze({ methods });
})(window);
