/* UI-17: Mission Proof Console — reuse Tabler + Mission/Fleet/Watch components.
   Tokens: static/taikun-tabler.css only (no new design system). */
(function (global) {
    'use strict';

    const PROVIDER_ROWS = [
        { id: 'codex', label: 'Codex', runtime: 'codex', cli: 'codex' },
        { id: 'claude_code', label: 'Claude Code', runtime: 'claude', cli: 'claude' },
        { id: 'cursor', label: 'Cursor', runtime: 'cursor', cli: 'cursor' },
    ];

    const MCP_PROBE_KEYS = [
        'configured',
        'initialize',
        'tools_list',
        'bound_read',
        'allowed_scoped_action',
        'cross_scope_denial',
        'expiry_revocation',
        'cleanup',
    ];

    const SECRET_NEEDLES = [
        'sk-', 'sk_', 'api_key', 'apikey', 'secret', 'token', 'password',
        'authorization', 'bearer ', 'anthropic_api_key', 'openai_api_key',
        'codex_api_key', 'codex_access_token',
    ];

    const methods = {
    _proofModeFromUrl() {
        try {
            const u = new URL(window.location.href);
            const mode = (u.searchParams.get('mode') || '').trim().toLowerCase();
            const proof = (u.searchParams.get('proof') || '').trim().toLowerCase();
            return mode === 'proof' || proof === '1' || proof === 'true' || proof === 'yes';
        } catch (e) {
            return false;
        }
    },

    _setProofModeInUrl(enabled) {
        try {
            const u = new URL(window.location.href);
            if (enabled) {
                u.searchParams.set('proof', '1');
                u.searchParams.set('mode', 'proof');
            } else {
                u.searchParams.delete('proof');
                u.searchParams.delete('mode');
            }
            if (window.PM_PROJECT) u.searchParams.set('project', window.PM_PROJECT);
            window.history.replaceState({}, '', u.toString());
        } catch (e) { /* ignore */ }
    },

    _canOperateProofConsole() {
        return !!(this.canWriteProjects || this.isAdmin
            || ((this.principal && this.principal.effective_scopes) || []).includes('write:tasks'));
    },

    _proofRedact(value) {
        const text = String(value == null ? '' : value);
        if (!text) return '';
        const lower = text.toLowerCase();
        if (SECRET_NEEDLES.some((n) => lower.includes(n))) return '[redacted]';
        // Long opaque blobs look like secrets — keep a short reference only.
        if (/^[A-Za-z0-9+/=_-]{48,}$/.test(text)) return `${text.slice(0, 8)}…[redacted]`;
        return text;
    },

    _proofChip(ok, label, title) {
        const cls = ok ? 'bg-green-lt' : 'bg-red-lt';
        const icon = ok ? 'ti-circle-check' : 'ti-alert-triangle';
        const safe = this._proofRedact(label);
        return `<span class="badge ${cls}" title="${this.esc(title || safe)}"><i class="ti ${icon} me-1"></i>${this.esc(safe)}</span>`;
    },

    _proofCleanupPresent(value) {
        if (value == null || value === '') return false;
        if (typeof value === 'object') {
            return !!(value.ok || value.passed || value.done
                || ['ok', 'pass', 'passed', 'purged', 'drained', 'zero', 'clean', 'complete', 'completed'].includes(
                    String(value.status || value.state || value.result || '').toLowerCase()));
        }
        const text = String(value).toLowerCase();
        if (['0', 'false', 'fail', 'failed', 'missing', 'no', 'none', 'pending', 'error'].includes(text)) {
            return false;
        }
        return ['ok', 'pass', 'passed', 'true', 'purged', 'drained', 'zero', 'clean', 'complete', 'completed',
            'success', 'succeeded'].some((t) => text === t || text.includes(t));
    },

    _proofIdentityCell(value, missingLabel) {
        const v = (value == null ? '' : String(value)).trim();
        if (!v) {
            return `<span class="badge bg-red-lt"><i class="ti ti-alert-triangle me-1"></i>${this.esc(missingLabel || 'missing')}</span>`;
        }
        return `<code class="font-monospace small">${this.esc(this._proofRedact(v))}</code>`;
    },

    async _proofLoadBindState(taskIds) {
        const project = window.PM_PROJECT || 'maxwell';
        const ids = (taskIds || []).map((x) => String(x || '').trim()).filter(Boolean);
        const out = {
            selectedTaskId: '',
            watch: null,
            runner: null,
            workSession: null,
            runners: [],
            workSessions: [],
            providerConnections: [],
            providerAuthCapabilities: [],
            error: '',
        };
        if (!ids.length) return out;
        try {
            const connRes = await fetch(`api/projects/${encodeURIComponent(project)}/provider-connections`);
            if (connRes.ok) {
                const connData = await connRes.json().catch(() => ({}));
                out.providerConnections = connData.connections || connData.provider_connections || [];
            }
        } catch (e) { /* optional */ }
        try {
            const policyRes = await fetch(`api/projects/${encodeURIComponent(project)}/provider-auth-capabilities`, { cache: 'no-store' });
            if (policyRes.ok) {
                const policyData = await policyRes.json().catch(() => ({}));
                out.providerAuthCapabilities = policyData.capabilities || [];
            }
        } catch (e) { /* fail closed below */ }

        for (const taskId of ids) {
            try {
                const wq = `project=${encodeURIComponent(project)}&task_id=${encodeURIComponent(taskId)}`;
                const watch = await (await fetch(`/ixp/v1/runner_sessions/watch?${wq}`, { cache: 'no-store' })).json();
                if (watch && watch.watchable && !watch.error && watch.error_code !== 'runner_bind_incomplete') {
                    out.selectedTaskId = taskId;
                    out.watch = watch;
                    out.runner = watch.session || null;
                    break;
                }
            } catch (e) { /* try next */ }
        }
        if (!out.selectedTaskId) out.selectedTaskId = ids[0];

        try {
            const rq = `project=${encodeURIComponent(project)}&task_id=${encodeURIComponent(out.selectedTaskId)}&include_stale=true`;
            const runners = await (await fetch(`/ixp/v1/runner_sessions?${rq}`, { cache: 'no-store' })).json();
            out.runners = runners.sessions || [];
            if (!out.runner && out.runners.length) out.runner = out.runners[0];
        } catch (e) {
            out.error = e.message || 'runner sessions unavailable';
        }
        try {
            const sq = `project=${encodeURIComponent(project)}&task_id=${encodeURIComponent(out.selectedTaskId)}&include_expired=true`;
            const sessions = await (await fetch(`/ixp/v1/work_sessions?${sq}`, { cache: 'no-store' })).json();
            out.workSessions = sessions.work_sessions || [];
            out.workSession = out.workSessions.find((s) => (s.status || '') === 'active') || out.workSessions[0] || null;
        } catch (e) { /* optional */ }
        return out;
    },

    _proofProbeFromRunner(runner, providerId) {
        const meta = (runner && runner.metadata) || {};
        const blob = meta.mcp_probe || meta.runtime_mcp_probe || meta.co14_mcp_probe || {};
        const byProvider = blob[providerId] || blob.providers?.[providerId] || {};
        const result = {};
        for (const key of MCP_PROBE_KEYS) {
            const raw = byProvider[key];
            const alt = byProvider[key.replace(/_/g, '/')]; // tools/list alias
            const val = raw != null ? raw : alt;
            if (val && typeof val === 'object') {
                result[key] = {
                    ok: !!(val.ok || val.passed || val.status === 'pass' || val.status === 'ok'),
                    label: String(val.status || val.label || (val.ok ? 'ok' : 'missing')),
                };
            } else if (typeof val === 'boolean') {
                result[key] = { ok: val, label: val ? 'ok' : 'fail' };
            } else if (val != null && val !== '') {
                const text = String(val).toLowerCase();
                const ok = ['ok', 'pass', 'passed', 'true', 'configured', 'clean'].includes(text);
                result[key] = { ok, label: String(val) };
            } else {
                result[key] = { ok: false, label: 'missing' };
            }
        }
        return result;
    },

    _proofAuthState(connections, providerId, runner) {
        const meta = (runner && runner.metadata) || {};
        const ref = meta.provider_identity_ref
            || meta.credential_reference
            || meta.provider_account_id
            || meta.account_affinity_id
            || '';
        const runtimeAuth = meta.provider_auth || meta.auth_state || {};
        const match = (connections || []).find((c) => {
            const provider = String(c.provider || c.provider_id || c.runtime || '').toLowerCase();
            if (providerId === 'codex') return provider.includes('codex') || provider.includes('openai') || provider.includes('chatgpt');
            if (providerId === 'claude_code') return provider.includes('claude') || provider.includes('anthropic');
            if (providerId === 'cursor') return provider.includes('cursor');
            return false;
        });
        const status = (runtimeAuth.status || runtimeAuth.state || (match && (match.status || match.auth_type)) || '').toString();
        const redacted = this._proofRedact(status || (ref ? 'configured' : ''));
        // Fail closed: enrolled providerConnections alone are not enough — the bound
        // runner must carry a non-secret provider identity reference.
        const ok = !!String(ref).trim();
        return {
            ok,
            label: redacted || 'missing provider identity',
            identityRef: this._proofRedact(ref || ''),
            enrolled: !!(match && (match.id || match.connection_id || match.provider_account_id)),
        };
    },

    _proofProviderPolicy(capabilities, providerId, runner) {
        const canonical = {
            codex: 'openai-codex',
            claude_code: 'anthropic-claude',
            cursor: 'cursor',
        }[providerId] || providerId;
        let matches = (capabilities || []).filter((row) =>
            row.provider === canonical && row.auth_mode !== 'api_key');
        const hostClass = String((runner?.metadata || {}).host_class || '').trim();
        if (hostClass) {
            const exact = matches.filter((row) => row.host_class === hostClass);
            if (exact.length) matches = exact;
        }
        if (matches.length > 1) {
            const denied = matches.filter((row) => row.allowed === false);
            matches = denied.length ? denied : matches;
        }
        const row = matches.length === 1 ? matches[0] : null;
        return {
            ok: !!(row && row.allowed === true),
            label: row ? (row.effective_state || row.state || 'unavailable') : 'policy missing',
            reason: row ? (row.effective_disable_reason || row.disable_reason || '') : 'provider_auth_policy_missing',
        };
    },

    _proofProviderRowsHtml(bind) {
        const runner = bind.runner || {};
        const rows = PROVIDER_ROWS.map((p) => {
            const auth = this._proofAuthState(bind.providerConnections, p.id, runner);
            const policy = this._proofProviderPolicy(bind.providerAuthCapabilities, p.id, runner);
            const probes = this._proofProbeFromRunner(runner, p.id);
            const cells = MCP_PROBE_KEYS.map((k) => {
                const cell = probes[k];
                return `<td>${this._proofChip(cell.ok, cell.label, k)}</td>`;
            }).join('');
            const rowOk = policy.ok && auth.ok && MCP_PROBE_KEYS.every((k) => probes[k].ok);
            return `<tr class="${rowOk ? '' : 'table-danger'}">
                <td class="fw-semibold">${this.esc(p.label)}<div class="text-secondary small font-monospace">${this.esc(p.cli)}</div></td>
                <td>${this._proofChip(policy.ok, policy.label, policy.reason || 'server auth policy')}<div class="mt-1">${this._proofChip(auth.ok, auth.label, 'provider auth (redacted)')}</div>
                <div class="font-monospace small text-secondary mt-1">${this.esc(auth.identityRef || '—')}</div></td>
                ${cells}
            </tr>`;
        }).join('');
        const headers = MCP_PROBE_KEYS.map((k) => `<th class="small">${this.esc(k.replace(/_/g, '/'))}</th>`).join('');
        return `<div class="table-responsive"><table class="table table-sm table-vcenter card-table mb-0" id="proof-provider-table">
            <thead><tr><th>Provider</th><th>Auth (redacted)</th>${headers}</tr></thead>
            <tbody>${rows}</tbody>
        </table></div>`;
    },

    _proofTimelineHtml(s, bind) {
        const items = [];
        const stamp = (typeof document !== 'undefined' && document.getElementById('mission-live-stamp')?.textContent) || '';
        if (stamp) items.push({ ok: true, text: `Mission live · ${stamp}` });
        (s.next_actions || []).slice(0, 6).forEach((a) => {
            items.push({
                ok: true,
                text: `${a.owner || 'system'}: ${a.label || a.action || a.kind || 'action'}${a.task_id ? ` · ${a.task_id}` : ''}`,
            });
        });
        (s.active_work || []).slice(0, 4).forEach((w) => {
            items.push({
                ok: (w.status || '') !== 'Blocked',
                text: `Active ${w.task_id}: ${w.status || '—'}${w.assignee ? ` · ${w.assignee}` : ''}`,
            });
        });
        if (bind.watch && bind.watch.watchable) {
            items.push({ ok: true, text: `Runner bind watchable · ${bind.watch.runner_session_id || ''}` });
        } else {
            items.push({ ok: false, text: 'Runner bind incomplete — Watch/Chat fail-closed' });
        }
        if (!(bind.workSession && bind.workSession.work_session_id)) {
            items.push({ ok: false, text: 'No Work Session bound to selected task' });
        }
        if (!items.length) {
            return `<div class="text-secondary small">No live timeline events yet.</div>`;
        }
        return `<ul class="list-unstyled mb-0" id="proof-timeline">${items.map((it) =>
            `<li class="mb-2 d-flex gap-2 align-items-start">${this._proofChip(it.ok, it.ok ? 'ok' : 'blocked')}<span class="small">${this.esc(it.text)}</span></li>`
        ).join('')}</ul>`;
    },

    _proofCleanupHtml(bind, s) {
        const runner = bind.runner || {};
        const meta = runner.metadata || {};
        const checks = [
            {
                ok: !!(s.done_with_proof || []).length || !!(runner.last_snapshot || {}).head_sha || meta.source_sha,
                label: 'PR/merge provenance',
                detail: (s.done_with_proof || [])[0]?.provenance?.label
                    || meta.pr_url
                    || meta.source_sha
                    || (runner.last_snapshot || {}).head_sha
                    || '',
            },
            {
                ok: this._proofCleanupPresent(meta.credential_cleanup || meta.runtime_cleanup || meta.cleanup?.credentials),
                label: 'Credential/runtime cleanup',
                detail: meta.credential_cleanup || meta.runtime_cleanup || '',
            },
            {
                ok: this._proofCleanupPresent(meta.host_drain || meta.drain || meta.cleanup?.host_drain),
                label: 'Host drain',
                detail: meta.host_drain || meta.drain || '',
            },
            {
                ok: this._proofCleanupPresent(meta.aws_fleet_zero || meta.fleet_zero || meta.cleanup?.aws_fleet_zero),
                label: 'AWS fleet-zero evidence',
                detail: meta.aws_fleet_zero || meta.fleet_zero || '',
            },
        ];
        return `<div class="row g-2" id="proof-cleanup-grid">${checks.map((c) => `
            <div class="col-md-6"><div class="card card-sm h-100"><div class="card-body py-2">
                <div class="d-flex align-items-center gap-2 mb-1">${this._proofChip(c.ok, c.ok ? 'present' : 'missing')}<span class="fw-semibold small">${this.esc(c.label)}</span></div>
                <div class="font-monospace small text-secondary text-truncate" title="${this.esc(this._proofRedact(c.detail))}">${this.esc(this._proofRedact(c.detail) || '—')}</div>
            </div></div></div>`).join('')}</div>`;
    },

    _proofVerdict(bind, s) {
        const runner = bind.runner || {};
        const meta = runner.metadata || {};
        const bindOk = !!(bind.watch && bind.watch.watchable);
        const wsOk = !!(bind.workSession && bind.workSession.work_session_id);
        const boundIdentity = !!(meta.provider_identity_ref || meta.credential_reference
            || meta.provider_account_id || meta.account_affinity_id);
        const identityOk = boundIdentity;
        const mcpOk = PROVIDER_ROWS.every((p) => {
            const probes = this._proofProbeFromRunner(runner, p.id);
            return MCP_PROBE_KEYS.every((k) => probes[k].ok);
        });
        const providerPolicyOk = PROVIDER_ROWS.every((p) =>
            this._proofProviderPolicy(bind.providerAuthCapabilities, p.id, runner).ok);
        const cleanupOk = this._proofCleanupPresent(meta.credential_cleanup || meta.runtime_cleanup)
            && this._proofCleanupPresent(meta.host_drain || meta.drain)
            && this._proofCleanupPresent(meta.aws_fleet_zero || meta.fleet_zero);        const missing = [];
        if (!bindOk) missing.push('runner bind');
        if (!wsOk) missing.push('work session');
        if (!identityOk) missing.push('provider identity reference');
        if (!mcpOk) missing.push('CO-14 MCP probe evidence');
        if (!providerPolicyOk) missing.push('CO-15 provider auth policy');
        if (!cleanupOk) missing.push('cleanup evidence');
        const green = missing.length === 0;
        return {
            green,
            missing,
            badge: green
                ? this._proofChip(true, 'proof ready', 'Browser-only acceptance run is green')
                : this._proofChip(false, 'proof blocked', `Missing: ${missing.join(', ')}`),
        };
    },

    proofConsoleHtml(s, bind) {
        const d = (s && s.deliverable) || {};
        const runner = (bind && bind.runner) || {};
        const meta = runner.metadata || {};
        const ws = (bind && bind.workSession) || {};
        const verdict = this._proofVerdict(bind || {}, s || {});
        const taskId = bind.selectedTaskId || '';
        const claimId = runner.claim_id || ws.claim_id || '';
        const operator = this._canOperateProofConsole();
        const operatorGate = operator ? '' : `<div class="alert alert-warning mb-3"><i class="ti ti-lock me-1"></i>Operator scopes required to Arm / open Watch controls (<code>write:tasks</code> or <code>write:projects</code>).</div>`;
        return `<div class="card mb-4" id="proof-console" data-proof-console="1" data-deliverable-id="${this.esc(d.id || s.deliverable_id || '')}">
            <div class="card-header">
                <div class="d-flex flex-wrap align-items-center gap-2">
                    <i class="ti ti-terminal-2 text-primary"></i>
                    <h3 class="card-title mb-0">Proof console</h3>
                    <span class="badge bg-secondary-lt">browser-only acceptance</span>
                    <span id="proof-verdict">${verdict.badge}</span>
                    <div class="ms-auto btn-list">
                        <button type="button" class="btn btn-sm btn-outline-secondary" id="proof-refresh" ${operator ? '' : 'disabled'}><i class="ti ti-refresh me-1"></i>Refresh</button>
                        <button type="button" class="btn btn-sm btn-primary" id="proof-arm" ${operator ? '' : 'disabled'}><i class="ti ti-player-play me-1"></i>Start / Arm</button>
                        <button type="button" class="btn btn-sm btn-outline-azure" id="proof-open-watch" ${operator && taskId ? '' : 'disabled'}><i class="ti ti-eye me-1"></i>Open Watch / Chat</button>
                    </div>
                </div>
            </div>
            <div class="card-body">
                ${operatorGate}
                <div id="proof-arm-flash" class="small text-secondary mb-3"></div>
                <div class="subheader mb-2">Bound session identity</div>
                <div class="datagrid mb-4" id="proof-identity-grid">
                    <div class="datagrid-item"><div class="datagrid-title">task_id</div><div class="datagrid-content">${this._proofIdentityCell(taskId, 'no task')}</div></div>
                    <div class="datagrid-item"><div class="datagrid-title">claim_id</div><div class="datagrid-content">${this._proofIdentityCell(claimId, 'no claim')}</div></div>
                    <div class="datagrid-item"><div class="datagrid-title">Work Session</div><div class="datagrid-content">${this._proofIdentityCell(ws.work_session_id, 'no work session')}</div></div>
                    <div class="datagrid-item"><div class="datagrid-title">runner_session_id</div><div class="datagrid-content">${this._proofIdentityCell(runner.runner_session_id || bind.watch?.runner_session_id, 'no runner')}</div></div>
                    <div class="datagrid-item"><div class="datagrid-title">host / cloud</div><div class="datagrid-content">${this._proofIdentityCell(runner.host_id || meta.cloud_instance_id || meta.instance_id, 'no host')}</div></div>
                    <div class="datagrid-item"><div class="datagrid-title">provider identity ref</div><div class="datagrid-content">${this._proofIdentityCell(meta.provider_identity_ref || meta.credential_reference || meta.provider_account_id, 'no identity ref')}</div></div>
                    <div class="datagrid-item"><div class="datagrid-title">source SHA</div><div class="datagrid-content">${this._proofIdentityCell(meta.source_sha || ws.head_sha || (runner.last_snapshot || {}).head_sha, 'no sha')}</div></div>
                    <div class="datagrid-item"><div class="datagrid-title">provider CLI</div><div class="datagrid-content">${this._proofIdentityCell(meta.provider_cli || runner.runtime || meta.command, 'unknown CLI')}</div></div>
                    <div class="datagrid-item"><div class="datagrid-title">placement</div><div class="datagrid-content">${this._proofIdentityCell(meta.placement || meta.placement_mode || ws.storage_mode || runner.cwd, 'unknown placement')}</div></div>
                </div>

                <div class="subheader mb-2">Provider MCP probes <span class="text-secondary fw-normal">(CO-14 evidence; missing = red)</span></div>
                <div class="card mb-4"><div class="card-body p-0">${this._proofProviderRowsHtml(bind)}</div></div>

                <div class="row g-3 mb-4">
                    <div class="col-lg-5">
                        <div class="card h-100"><div class="card-header"><h3 class="card-title mb-0">Live timeline</h3></div>
                            <div class="card-body">${this._proofTimelineHtml(s, bind)}</div></div>
                    </div>
                    <div class="col-lg-7">
                        <div class="card h-100"><div class="card-header"><h3 class="card-title mb-0">Cleanup & provenance</h3></div>
                            <div class="card-body">${this._proofCleanupHtml(bind, s)}</div></div>
                    </div>
                </div>

                <div class="subheader mb-2">PTY Watch / Chat <span class="text-secondary fw-normal">(COORD-34 fail-closed bind)</span></div>
                ${taskId ? this.runnerControlHtml({ task_id: taskId }) : `<div class="alert alert-danger mb-0"><i class="ti ti-alert-triangle me-1"></i>No linked task available for Watch/Chat.</div>`}
                ${!verdict.green ? `<div class="alert alert-danger mt-3 mb-0" id="proof-blockers"><i class="ti ti-ban me-1"></i>Green proof blocked — missing: ${this.esc(verdict.missing.join(', '))}</div>` : `<div class="alert alert-success mt-3 mb-0"><i class="ti ti-circle-check me-1"></i>Browser-only acceptance run is ready (no SSH / AWS console / native AI apps required).</div>`}
            </div>
        </div>`;
    },

    async _initProofConsole(s) {
        const root = document.getElementById('proof-console');
        if (!root) return;
        const taskId = root.querySelector('#runner-control-panel')?.getAttribute('data-task-id')
            || (this._proofBind && this._proofBind.selectedTaskId)
            || '';
        if (taskId && typeof this._loadRunnerSessions === 'function') {
            await this._loadRunnerSessions(taskId);
        }
        const refresh = document.getElementById('proof-refresh');
        const arm = document.getElementById('proof-arm');
        const watch = document.getElementById('proof-open-watch');
        if (refresh && !refresh._bound) {
            refresh.addEventListener('click', () => this.refreshMissionPage());
            refresh._bound = true;
        }
        if (arm && !arm._bound) {
            arm.addEventListener('click', () => this.armProofConsole());
            arm._bound = true;
        }
        if (watch && !watch._bound) {
            watch.addEventListener('click', () => {
                const tid = document.getElementById('runner-control-panel')?.getAttribute('data-task-id') || taskId;
                if (tid && typeof this.openRunnerWatch === 'function') this.openRunnerWatch(tid);
            });
            watch._bound = true;
        }
        const openBtn = document.getElementById('runner-watch-open');
        if (openBtn && !openBtn._proofBound) {
            openBtn.addEventListener('click', () => {
                const tid = openBtn.getAttribute('data-task-id') || taskId;
                if (tid && typeof this.openRunnerWatch === 'function') this.openRunnerWatch(tid);
            });
            openBtn._proofBound = true;
        }
    },

    async armProofConsole() {
        const flash = (msg, cls) => {
            const el = document.getElementById('proof-arm-flash');
            if (el) { el.className = `small text-${cls || 'secondary'} mb-3`; el.textContent = msg; }
        };
        if (!this._canOperateProofConsole()) {
            flash('Operator scope required to Arm', 'danger');
            return;
        }
        const deliverableId = this.selectedDeliverableId || (this.missionStatus || {}).deliverable_id || '';
        if (!deliverableId) {
            flash('Select a deliverable first', 'warning');
            return;
        }
        flash('Arming coordinator tick…');
        try {
            const project = window.PM_PROJECT || 'maxwell';
            const res = await fetch(`api/deliverables/${encodeURIComponent(deliverableId)}/coordinator_tick?project=${encodeURIComponent(project)}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    board_id: (this.missionStatus || {}).board_id || '',
                    mission_id: (this.missionStatus || {}).mission_id || '',
                    idem_key: `proof-arm-${deliverableId}-${Date.now()}`,
                }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || data.error) throw new Error(data.error || data.detail || `HTTP ${res.status}`);
            flash(`Armed — coordinator tick ${data.status || 'ok'}`, 'green');
            await this.refreshMissionPage();
        } catch (e) {
            flash(`Arm failed: ${e.message}`, 'danger');
        }
    },

    async toggleProofConsole(force) {
        const next = force == null ? !this._proofModeFromUrl() : !!force;
        if (!this._canOperateProofConsole() && next) {
            // Still allow viewing the deep link for reviewers (read-only arm gated in HTML).
        }
        this._setProofModeInUrl(next);
        await this.refreshMissionPage();
    },
    };

    global.SwitchboardProofConsole = Object.freeze({ methods, PROVIDER_ROWS, MCP_PROBE_KEYS });
})(window);
