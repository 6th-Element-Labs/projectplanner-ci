/* ARCH-MS-21 / CO-13: runner Watch/Chat + bound PTY session inject. */
(function (global) {
    'use strict';
    const methods = {
    runnerControlHtml(t) {
        return `<div class="card mb-3" id="runner-control-panel" data-task-id="${this.esc(t.task_id)}">
            <div class="card-header py-2">
                <div class="d-flex align-items-center gap-2">
                    <i class="ti ti-player-play text-azure"></i>
                    <span class="fw-semibold">Runner sessions</span>
                    <span id="runner-control-count" class="badge bg-secondary-lt">loading</span>
                    <button type="button" class="btn btn-sm btn-outline-azure ms-2" id="runner-watch-open"
                        data-task-id="${this.esc(t.task_id)}">Watch / Chat</button>
                    <span id="runner-control-flash" class="small text-secondary ms-auto"></span>
                </div>
            </div>
            <div id="runner-watch-gate" class="px-3 pt-2" hidden></div>
            <div id="runner-session-chat" class="px-3 pb-2" hidden>
                <div class="small text-secondary mb-1">Session chat (bound Codex PTY — not inbox)</div>
                <div class="btn-list mb-2">
                    <button type="button" class="btn btn-sm btn-outline-secondary" data-runner-chat-kind="redirect">Redirect</button>
                    <button type="button" class="btn btn-sm btn-outline-secondary" data-runner-chat-kind="hold">Hold</button>
                    <button type="button" class="btn btn-sm btn-outline-secondary" data-runner-chat-kind="approve">Approve</button>
                </div>
                <div class="input-group input-group-sm">
                    <input id="runner-chat-input" class="form-control" placeholder="Inject freeform into the live session…" autocomplete="off"/>
                    <button type="button" class="btn btn-azure" id="runner-chat-send">Send</button>
                </div>
                <div id="runner-chat-log" class="small text-secondary mt-2"></div>
            </div>
            <div id="runner-control-body" class="card-body py-3">
                <div class="text-secondary small">Loading runner sessions…</div>
            </div>
        </div>`;
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

    async openRunnerWatch(taskId) {
        // UI-17 / COORD-34: fail closed unless list_runner_sessions(task_id) yields a
        // fully bound live runner (task/claim/host/wake/work_session).
        // CO-13: on success, expose session chat inject bound to runner_session_id+task_id.
        const gate = document.getElementById('runner-watch-gate');
        const chat = document.getElementById('runner-session-chat');
        const flash = document.getElementById('runner-control-flash');
        const showGate = (html, cls) => {
            if (gate) {
                gate.hidden = false;
                gate.innerHTML = html;
                gate.className = `px-3 pt-2 small text-${cls || 'danger'}`;
            }
            if (flash) {
                flash.textContent = '';
                flash.className = 'small text-secondary ms-auto';
            }
        };
        if (chat) {
            chat.hidden = true;
            chat.removeAttribute('data-runner-session-id');
            chat.removeAttribute('data-task-id');
        }
        try {
            const q = `project=${encodeURIComponent(window.PM_PROJECT || 'maxwell')}&task_id=${encodeURIComponent(taskId)}`;
            const data = await (await fetch(`/ixp/v1/runner_sessions/watch?${q}`)).json();
            if (data.error || data.error_code === 'runner_bind_incomplete' || data.watchable === false) {
                const missing = (data.missing || []).join(', ') || 'bind fields';
                showGate(
                    `<span class="badge bg-red-lt me-1">${this.esc(data.error_code || data.error || 'runner_bind_incomplete')}</span>`
                    + `${this.esc(data.message || 'Runner bind incomplete for Watch/Chat')}`
                    + ` <span class="text-secondary">(missing: ${this.esc(missing)})</span>`,
                    'danger',
                );
                return;
            }
            const sid = data.runner_session_id || (data.session || {}).runner_session_id || '';
            showGate(
                `<span class="badge bg-green-lt me-1">watchable</span>`
                + `Bound runner ${this.esc(sid)} — Watch/Chat panel may open.`
                + ` <span class="text-secondary font-monospace">${this.esc((data.bind || {}).host_id || '')}</span>`,
                'green',
            );
            if (chat && sid) {
                chat.hidden = false;
                chat.setAttribute('data-runner-session-id', sid);
                chat.setAttribute('data-task-id', taskId);
                const sendBtn = document.getElementById('runner-chat-send');
                const input = document.getElementById('runner-chat-input');
                if (sendBtn && !sendBtn._bound) {
                    sendBtn.addEventListener('click', () => this.sendRunnerSessionChat('freeform'));
                    sendBtn._bound = true;
                }
                if (input && !input._bound) {
                    input.addEventListener('keydown', (e) => {
                        if (e.key === 'Enter') this.sendRunnerSessionChat('freeform');
                    });
                    input._bound = true;
                }
                chat.querySelectorAll('[data-runner-chat-kind]').forEach((btn) => {
                    if (btn._bound) return;
                    btn.addEventListener('click', () => {
                        this.sendRunnerSessionChat(btn.getAttribute('data-runner-chat-kind') || 'freeform');
                    });
                    btn._bound = true;
                });
            }
        } catch (e) {
            showGate(`Watch refused: ${this.esc(e.message)}`, 'danger');
        }
    },

    async sendRunnerSessionChat(kind) {
        const panel = document.getElementById('runner-session-chat');
        const input = document.getElementById('runner-chat-input');
        const log = document.getElementById('runner-chat-log');
        const flash = (msg, cls) => {
            const el = document.getElementById('runner-control-flash');
            if (el) { el.textContent = msg; el.className = 'small text-' + (cls || 'secondary') + ' ms-auto'; }
        };
        if (!panel || panel.hidden) {
            flash('Open Watch / Chat on a bound runner first', 'warning');
            return;
        }
        const runnerId = panel.getAttribute('data-runner-session-id') || '';
        const taskId = panel.getAttribute('data-task-id') || '';
        const text = ((input && input.value) || '').trim();
        if (!runnerId || !taskId) {
            flash('Session chat missing runner_session_id+task_id bind', 'danger');
            return;
        }
        if (!text) return;
        if (input) input.value = '';
        const injectKind = (kind || 'freeform').toLowerCase();
        if (log) {
            log.insertAdjacentHTML('beforeend',
                `<div><span class="badge bg-azure-lt me-1">${this.esc(injectKind)}</span>${this.esc(text)}</div>`);
        }
        flash(`Injecting ${injectKind}…`);
        try {
            const res = await fetch('/ixp/v1/request_runner_inject', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    project: window.PM_PROJECT || 'maxwell',
                    runner_session_id: runnerId,
                    task_id: taskId,
                    text,
                    kind: injectKind,
                    reason: `operator ${injectKind} chat from task ${taskId}`,
                }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || data.error) {
                throw new Error(data.error || data.detail || data.message || `HTTP ${res.status}`);
            }
            if (data.requested === false) {
                flash(`inject refused: ${this.esc(data.reason || data.error || 'not accepted')}`, 'warning');
                return;
            }
            flash('inject requested', 'green');
        } catch (e) {
            flash(`inject failed: ${e.message}`, 'danger');
        }
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
            inject: '/ixp/v1/request_runner_inject',
        };
        const endpoint = endpoints[action] || '/ixp/v1/request_runner_snapshot';
        try {
            const body = {
                project: window.PM_PROJECT || 'maxwell',
                runner_session_id: runnerId,
                reason: `operator ${action} from task ${taskId}`,
            };
            if (action === 'inject') {
                body.task_id = taskId;
                body.text = window.prompt('Inject text into live session:') || '';
                if (!body.text) return;
            }
            const res = await fetch(endpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await res.json();
            if (!res.ok || data.error) throw new Error(data.error || data.detail || `HTTP ${res.status}`);
            flash(data.requested === false ? `${action} refused` : `${action} requested`, data.requested === false ? 'warning' : 'green');
            await this._loadRunnerSessions(taskId);
        } catch (e) {
            flash(`${action} failed: ${e.message}`, 'danger');
        }
    },
    };

    global.SwitchboardRunnerSession = Object.freeze({ methods });
})(window);
