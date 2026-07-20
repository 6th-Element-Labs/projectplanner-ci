/* UI-24 (was ARCH-MS-21/CO-13 scaffold): runner Watch/Chat, now a real bound PTY
   terminal. One xterm.js instance + one relay WebSocket per runner_session_id,
   living in #runner-pty-panel (static/index.html) — opened either as a
   right-docked sidecar (ambient trigger: a task box, the Proof Console) or
   reparented in place inside the task-detail modal's primary session surface
   full task context). Never both at once for the same session: opening the
   other container moves the same panel, it does not open a second connection. */
(function (global) {
    'use strict';
    const methods = {
    XTERM_JS_SRC: 'https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.js',
    XTERM_FIT_SRC: 'https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.js',

    runnerControlHtml(t) {
        return `<div class="card mb-3" id="runner-control-panel" data-task-id="${this.esc(t.task_id)}">
            <div class="card-header py-2">
                <div class="d-flex align-items-center gap-2">
                    <i class="ti ti-player-play text-azure"></i>
                    <span class="fw-semibold">Runner sessions</span>
                    <span id="runner-control-count" class="badge bg-secondary-lt">loading</span>
                    <button type="button" class="btn btn-sm btn-outline-azure ms-2" id="runner-watch-open"
                        data-task-id="${this.esc(t.task_id)}"><i class="ti ti-terminal-2 me-1"></i>Watch / Chat</button>
                    <span id="runner-control-flash" class="small text-secondary ms-auto"></span>
                </div>
            </div>
            <div class="card-body py-3">
                <div id="task-session-doctor" class="alert alert-secondary py-2 mb-3" role="status">Checking task session…</div>
                <div id="runner-control-body"><div class="text-secondary small">Loading runner sessions…</div></div>
            </div>
        </div>`;
    },

    async _loadRunnerSessions(taskId) {
        const body = document.getElementById('runner-control-body');
        const count = document.getElementById('runner-control-count');
        if (!body) return;
        await this._loadTaskSessionDoctor(taskId);
        let data;
        try {
            const q = `project=${encodeURIComponent(window.PM_PROJECT || 'maxwell')}&task_id=${encodeURIComponent(taskId)}&include_stale=false`;
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
            <thead><tr><th>Session</th><th>Host</th><th>Runtime</th><th>Fidelity</th><th>Environment</th><th>Snapshot</th><th class="text-end">Actions</th></tr></thead>
            <tbody>${sessions.map((s) => this._runnerSessionRow(s)).join('')}</tbody>
        </table></div>`;
        body.querySelectorAll('[data-runner-action]').forEach((btn) => {
            btn.addEventListener('click', () => this.requestRunnerControl(
                btn.getAttribute('data-runner-id'),
                btn.getAttribute('data-runner-action'),
                taskId,
            ));
        });
        body.querySelectorAll('[data-runner-watch-task]').forEach((btn) => {
            btn.addEventListener('click', () => this.openRunnerSessionPanel(
                btn.getAttribute('data-runner-watch-task') || taskId));
        });
    },

    async _loadTaskSessionDoctor(taskId) {
        const el = document.getElementById('task-session-doctor');
        if (!el) return;
        try {
            const project = encodeURIComponent(window.PM_PROJECT || 'maxwell');
            const doctor = await (await fetch(`/api/tasks/${encodeURIComponent(taskId)}/session/doctor?project=${project}`, { cache: 'no-store' })).json();
            const repair = doctor.repair || {};
            const tone = doctor.blocked_at ? 'warning' : (doctor.watchable_now ? 'azure' : 'secondary');
            el.className = `alert alert-${tone} py-2 mb-3 d-flex align-items-center gap-2`;
            el.innerHTML = `<span class="flex-fill">${this.esc(doctor.message || 'Task session status unavailable.')}</span>
                <button type="button" class="btn btn-sm btn-outline-${tone}" data-doctor-action="${this.esc(repair.action || 'reopen')}">${this.esc(repair.label || 'Reopen task session')}</button>`;
            el.querySelector('[data-doctor-action]')?.addEventListener('click', () => {
                const action = repair.action || 'reopen';
                if (action === 'watch' || action === 'reopen') this.openRunnerSessionPanel(taskId);
                else if (action === 'retry' || action === 'start') document.getElementById('task-primary-start')?.click();
                else this._loadTaskSessionDoctor(taskId);
            });
        } catch (e) {
            el.className = 'alert alert-danger py-2 mb-3';
            el.textContent = `Task session status unavailable: ${e.message}`;
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
        const watch = s.task_id && !s.stale && s.status === 'running'
            ? `<button class="btn btn-sm btn-azure" data-runner-watch-task="${this.esc(s.task_id)}" title="Watch / Chat"><i class="ti ti-terminal-2 me-1"></i>Watch</button>` : '';
        return `<tr>
            <td><div class="font-monospace small">${this.esc(s.runner_session_id)}</div><span class="badge bg-${statusColor}-lt">${this.esc(s.status || 'unknown')}${s.stale ? ' · stale' : ''}</span></td>
            <td>${this.esc(s.host_id || '—')}</td>
            <td>${this.esc(s.runtime || '—')}<div class="text-secondary small">${this.esc(s.agent_id || '')}</div></td>
            <td>${this.esc(fidelity)}</td>
            <td><span class="badge bg-${statusColor}-lt">${this.esc(env.status || s.status || 'unknown')}</span>${uptime ? `<span class="text-secondary small ms-1">${this.esc(uptime)}</span>` : ''}${failure}${logTail}</td>
            <td class="text-secondary small">${this.esc(snapText)}</td>
            <td class="text-end"><div class="btn-list justify-content-end flex-nowrap">
                ${watch}
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
        if (action === 'kill') {
            const ok = await this._confirm({
                title: `Kill runner ${runnerId}?`,
                body: 'Stops the bound provider CLI process on its host. This cannot be undone.',
                icon: 'ti-player-stop', iconVariant: 'danger',
                confirmLabel: 'Kill runner', confirmVariant: 'danger',
            });
            if (!ok) return;
        }
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
            if (action === 'kill' && data.requested !== false
                && this._runnerPty && this._runnerPty.runnerSessionId === runnerId) {
                this._runnerPtyClose();
            }
        } catch (e) {
            flash(`${action} failed: ${e.message}`, 'danger');
        }
    },

    // ---- UI-24: the bound PTY terminal -----------------------------------
    // this._runnerPty holds the single live session: {taskId, runnerSessionId,
    // ws, term, fitAddon, mode ('sidecar'|'docked'), reconnectAttempts, reconnectTimer}.

    async _ensureXterm() {
        if (window.Terminal && window.FitAddon) return;
        await Promise.all([this._ensureScript(this.XTERM_JS_SRC), this._ensureScript(this.XTERM_FIT_SRC)]);
        if (!window.Terminal || !window.FitAddon) throw new Error('xterm.js failed to load');
    },

    _runnerPtyEls() {
        return {
            scrim: document.getElementById('runner-pty-scrim'),
            panel: document.getElementById('runner-pty-panel'),
            title: document.getElementById('runner-pty-title'),
            sub: document.getElementById('runner-pty-sub'),
            live: document.getElementById('runner-pty-live'),
            gate: document.getElementById('runner-pty-gate'),
            termMount: document.getElementById('runner-pty-term'),
            toggleDock: document.getElementById('runner-pty-toggle-dock'),
            close: document.getElementById('runner-pty-close'),
            chatWrap: document.getElementById('runner-pty-chat'),
            chatInput: document.getElementById('runner-chat-input'),
            chatSend: document.getElementById('runner-chat-send'),
            chatLog: document.getElementById('runner-chat-log'),
        };
    },

    _runnerPtyBindShellOnce() {
        if (this._runnerPtyShellBound) return;
        const els = this._runnerPtyEls();
        if (!els.panel) return;
        this._runnerPtyShellBound = true;
        if (els.close) els.close.addEventListener('click', () => this._runnerPtyClose());
        if (els.scrim) els.scrim.addEventListener('click', () => this._runnerPtyClose());
        if (els.toggleDock) els.toggleDock.addEventListener('click', () => this._runnerPtyToggleDock());
        if (els.chatSend) els.chatSend.addEventListener('click', () => this._runnerPtySendChat('freeform'));
        if (els.chatInput) els.chatInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.isComposing) {
                e.preventDefault();
                this._runnerPtySendChat('freeform');
            }
        });
        if (els.chatWrap) els.chatWrap.querySelectorAll('[data-runner-chat-kind]').forEach((b) => {
            b.addEventListener('click', () => this._runnerPtySendChat(b.getAttribute('data-runner-chat-kind') || 'freeform'));
        });
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && els.panel.classList.contains('tk-pty-sidecar') && !els.panel.hidden) {
                this._runnerPtyClose();
            }
        });
        this._runnerPtyGuardModalTeardownOnce();
    },

    // dockInto: an element inside the task-detail modal, or falsy for the sidecar.
    _runnerPtyShowShell(dockInto) {
        this._runnerPtyBindShellOnce();
        const els = this._runnerPtyEls();
        if (!els.panel) return els;
        if (this._runnerPtyCloseTimer) {
            clearTimeout(this._runnerPtyCloseTimer);
            this._runnerPtyCloseTimer = null;
        }
        els.panel.hidden = false;
        if (dockInto) {
            dockInto.appendChild(els.panel);
            els.panel.classList.add('tk-pty-docked');
            els.panel.classList.remove('tk-pty-sidecar', 'show');
            if (els.scrim) els.scrim.classList.remove('show');
        } else {
            document.body.appendChild(els.panel);
            els.panel.classList.add('tk-pty-sidecar');
            els.panel.classList.remove('tk-pty-docked');
            if (els.scrim) els.scrim.classList.add('show');
            requestAnimationFrame(() => els.panel.classList.add('show'));
        }
        return els;
    },

    _runnerPtyToggleDock() {
        const rp = this._runnerPty;
        if (!rp) return;
        const els = this._runnerPtyEls();
        const modalDetailsMount = document.getElementById('runner-pty-details-mount');
        const modalDevMount = document.getElementById('runner-pty-dev-mount');
        const modal = document.getElementById('task-modal');
        // Only dock into a modal that's actually showing *this* session's
        // task — otherwise an in-flight open for task X can resolve after
        // the operator has since opened task Y's modal, and "expand" would
        // silently plant X's live terminal inside Y's Dev tab.
        const modalMatches = modal && modal.classList.contains('show')
            && modal.dataset.taskId === String(rp.taskId || '');
        if (rp.mode === 'docked') {
            this._runnerPtyShowShell(null);
            rp.mode = 'sidecar';
        } else if ((modalDetailsMount || modalDevMount) && modalMatches) {
            this._runnerPtyShowShell(modalDetailsMount || modalDevMount);
            rp.mode = 'docked';
        }
        if (rp.fitAddon) requestAnimationFrame(() => { try { rp.fitAddon.fit(); this._runnerPtySendResize(); } catch (e) { /* ignore */ } });
        if (els.toggleDock) els.toggleDock.title = rp.mode === 'docked' ? 'Pop out to side panel' : 'Open in task details';
    },

    // Pops a docked panel back to the sidecar if one is live. Safe to call
    // any time - a no-op unless a session is currently docked.
    _runnerPtyEvacuateIfDocked() {
        if (this._runnerPty && this._runnerPty.mode === 'docked') this._runnerPtyToggleDock();
    },

    // openTask() regenerates #task-modal-body's innerHTML on every open,
    // including when a modal that's already showing gets refreshed in place
    // (e.g. revokeClaim() -> openTask() again, which never fires
    // hide.bs.modal since Bootstrap's show() on an already-shown modal is a
    // no-op). Either path would silently delete a docked panel - and leak
    // its WebSocket/terminal - with no way for the panel to detect the wipe
    // after the fact (a MutationObserver would fire too late; innerHTML has
    // already destroyed the nodes by the time it runs). So this covers the
    // dismissal case, and app.js calls _runnerPtyEvacuateIfDocked() directly
    // before its innerHTML rewrite to cover the already-open-modal case.
    _runnerPtyGuardModalTeardownOnce() {
        if (this._runnerPtyModalGuardBound) return;
        const modal = document.getElementById('task-modal');
        if (!modal) return;
        this._runnerPtyModalGuardBound = true;
        modal.addEventListener('hide.bs.modal', () => this._runnerPtyEvacuateIfDocked());
    },

    _runnerPtyClose() {
        const els = this._runnerPtyEls();
        const rp = this._runnerPty;
        if (rp && rp.taskId) {
            // BUG-91: remember only WHICH TASK the operator was watching, never
            // which runner. A repeat click still means "reopen the runner
            // surface for this task", but the runner identity is resolved from
            // the server on every open. Pinning an id here is what let a dead
            // historical row win over a newer live runner.
            this._runnerPtyIntentTask = String(rp.taskId);
        }
        if (els.panel) {
            els.panel.classList.remove('show');
            if (this._runnerPtyCloseTimer) clearTimeout(this._runnerPtyCloseTimer);
            this._runnerPtyCloseTimer = setTimeout(() => {
                // A fast reopen cancels this timer in _runnerPtyShowShell. The
                // identity guard prevents an old close animation from hiding a
                // newly-opened panel if callbacks run in the same event turn.
                if (!this._runnerPty) els.panel.hidden = true;
                this._runnerPtyCloseTimer = null;
            }, 200);
        }
        if (els.scrim) els.scrim.classList.remove('show');
        this._runnerPtyTeardown();
    },

    _runnerPtyTeardown() {
        const rp = this._runnerPty;
        if (!rp) return;
        if (rp.reconnectTimer) clearTimeout(rp.reconnectTimer);
        if (rp.resizeTimer) clearTimeout(rp.resizeTimer);
        Object.values(rp.pendingChat || {}).forEach((pending) => {
            if (pending.timer) clearTimeout(pending.timer);
        });
        if (rp.resizeObserver) { try { rp.resizeObserver.disconnect(); } catch (e) { /* ignore */ } }
        if (rp.ws) { try { rp.ws.close(); } catch (e) { /* ignore */ } }
        if (rp.term) { try { rp.term.dispose(); } catch (e) { /* ignore */ } }
        this._runnerPty = null;
    },

    _runnerPtyGate(html, cls) {
        const els = this._runnerPtyEls();
        if (!els.gate) return;
        els.gate.innerHTML = html;
        els.gate.className = `small mb-2 text-${cls || 'secondary'}`;
    },

    // The one entry point. opts.dockInto: mount inside the task modal
    // instead of opening the sidecar. opts.fallbackIfNotWatchable: return false
    // instead of opening a red gate state (lets callers like the mission graph
    // click handler fall back to a different action for tasks with no run).
    // opts.includeStale discovers prior runner history after a full page reload,
    // when the in-memory task-intent hint no longer exists.
    async openRunnerSessionPanel(taskId, opts) {
        opts = opts || {};
        const id = String(taskId || '').trim();
        if (!id) return false;
        // Task-scoped intent only (BUG-91). It decides whether to keep the click
        // on the runner surface and whether to look through stale history — it
        // never decides WHICH runner is shown. The server picks that, every time.
        const remembered = String(this._runnerPtyIntentTask || '') === id;
        let watch;
        try {
            const q = `project=${encodeURIComponent(window.PM_PROJECT || 'maxwell')}`
                + `&task_id=${encodeURIComponent(id)}`
                + ((remembered || opts.includeStale) ? '&include_stale=true' : '');
            watch = await (await fetch(`/ixp/v1/runner_sessions/watch?${q}`)).json();
        } catch (e) {
            if (opts.fallbackIfNotWatchable && !remembered) return false;
            this._runnerPtyShowShell(opts.dockInto);
            this._runnerPtyGate(`Watch refused: ${this.esc(e.message)}`, 'danger');
            return true;
        }
        const watchable = watch && watch.watchable !== false && !watch.error && watch.error_code !== 'runner_bind_incomplete';
        const resumeBox = document.getElementById('runner-pty-resume');
        const resumeButton = document.getElementById('runner-pty-resume-review');
        if (resumeBox) resumeBox.hidden = true;
        if (!watchable) {
            const sessions = (watch && Array.isArray(watch.sessions)) ? watch.sessions : [];
            // Preserve authoring fallback for tasks that have never had a
            // runner. An ended/stale session is real runner history, so reopen
            // its truthful gate instead of treating it as "no runner".
            if (opts.fallbackIfNotWatchable && !remembered && !sessions.length) return false;
            const els = this._runnerPtyShowShell(opts.dockInto);
            // BUG-91: the server orders sessions newest-first and its refusal
            // names the row it judged. Take that answer verbatim. Never revive a
            // previously-shown runner id the server no longer reports — a task
            // accumulates one row per dispatch attempt, so a remembered id is
            // routinely older than the truth.
            const currentSession = sessions[0] || null;
            const currentSid = String(
                watch?.runner_session_id || currentSession?.runner_session_id || '');
            this._runnerPtyIntentTask = id;
            if (els.title) els.title.textContent = currentSid ? `${id} · ${currentSid}` : id;
            if (els.sub) els.sub.textContent = String(currentSession?.host_id || '');
            if (els.live) els.live.hidden = true;
            const localTask = (this.tasks || []).find((task) => String(task.task_id || '') === id);
            const taskStatus = String(opts.taskStatus || localTask?.status || '');
            const endedStatuses = new Set(['completed', 'failed', 'cancelled', 'expired', 'lost', 'killed', 'exited']);
            const ended = currentSession && (
                currentSession.stale === true
                || endedStatuses.has(String(currentSession.status || '').toLowerCase()));
            if (resumeBox) resumeBox.hidden = !(taskStatus === 'In Review' && currentSid && ended);
            if (resumeButton && taskStatus === 'In Review' && currentSid && ended) {
                resumeButton.onclick = () => this.resumeTaskReview(id, opts);
            }
            const missing = (watch && watch.missing || []).join(', ') || 'bind fields';
            const detail = watch?.message
                || `Runner bind incomplete for Watch/Chat (missing: ${missing})`;
            // BUG-91: when the run never started, the dispatcher's reason is the
            // useful thing to show ("capacity exhausted for co-general: cap=4").
            // "Runner session is stale" describes the debris, not the problem.
            const dispatch = watch?.dispatch || null;
            const label = dispatch?.state || watch?.error_code || watch?.error
                || 'runner_bind_incomplete';
            const badgeClass = dispatch?.state === 'needs_attention' ? 'bg-orange-lt' : 'bg-red-lt';
            let extra = '';
            if (dispatch && Number(dispatch.dispatch_attempt) > 1) {
                extra += `<div class="mt-1 text-secondary">Dispatch attempt ${this.esc(String(dispatch.dispatch_attempt))}`
                    + `${dispatch.host_id ? ` · last host ${this.esc(String(dispatch.host_id))}` : ''}</div>`;
            }
            this._runnerPtyGate(
                `<span class="badge ${badgeClass} me-1">${this.esc(label)}</span>`
                + this.esc(detail) + extra,
                dispatch?.state === 'needs_attention' ? 'warning' : 'danger',
            );
            // COORD-44/SIMPLIFY-10: the refusal names the blocker; this is the
            // one repair action. Start and Retry are distinct service commands —
            // retry supersedes the failed attempt instead of racing a second one.
            const gateEl = document.getElementById('runner-pty-gate');
            if (gateEl && !document.getElementById('runner-pty-start-retry')) {
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.id = 'runner-pty-start-retry';
                btn.className = 'btn btn-primary btn-sm mt-2 d-block';
                const isRetry = !!((dispatch && dispatch.state) || sessions.length);
                btn.innerHTML = `<i class="ti ti-player-play me-1"></i>${isRetry ? 'Retry' : 'Start'} on my Mac`;
                btn.onclick = () => this.startTaskSession(id, opts, isRetry);
                gateEl.appendChild(btn);
            }
            return true;
        }
        const sid = watch.runner_session_id || (watch.session || {}).runner_session_id || '';
        const els = this._runnerPtyShowShell(opts.dockInto);
        const mode = opts.dockInto ? 'docked' : 'sidecar';
        if (this._runnerPty && this._runnerPty.runnerSessionId === sid) {
            // Same session already live — just move containers, don't reconnect.
            this._runnerPty.mode = mode;
            this._runnerPty.taskId = id;
            if (this._runnerPty.fitAddon) requestAnimationFrame(() => { try { this._runnerPty.fitAddon.fit(); } catch (e) { /* ignore */ } });
            return true;
        }
        this._runnerPtyTeardown();
        this._runnerPtyIntentTask = id;
        if (els.title) els.title.textContent = `${id} · ${sid}`;
        if (els.sub) els.sub.textContent = `${(watch.bind && watch.bind.host_id) || ''}`.trim();
        this._runnerPtyGate('', 'secondary');
        this._runnerPty = { taskId: id, runnerSessionId: sid, mode, reconnectAttempts: 0 };
        await this._runnerPtyConnect();
        return true;
    },

    // COORD-44/SIMPLIFY-10: one Start/Retry path for every surface, through the
    // task-execution command service. The server attaches, dedupes an in-flight
    // start, supersedes a failed attempt, or launches on the enrolled Mac; the
    // browser only polls the authoritative execution projection afterwards.
    async startTaskSession(taskId, opts, retry = false) {
        const id = String(taskId || '').trim();
        if (!id) return false;
        const pending = document.getElementById('runner-pty-start-retry');
        if (pending) pending.disabled = true;
        this._runnerPtyGate(
            retry ? 'Superseding the last attempt…' : 'Starting a session on your Mac…',
            'secondary');
        const project = window.PM_PROJECT || 'maxwell';
        const path = retry
            ? `api/tasks/${encodeURIComponent(id)}/execution/retry`
            : `api/tasks/${encodeURIComponent(id)}/start`;
        let data;
        try {
            const res = await fetch(path, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ project }),
            });
            data = await res.json();
        } catch (e) {
            this._runnerPtyGate(`${retry ? 'Retry' : 'Start'} failed: ${this.esc(e.message)}`, 'danger');
            return false;
        }
        if (data.action === 'attach' || data.attached === true) {
            return this.openRunnerSessionPanel(id, opts);
        }
        if (data.action === 'superseding') {
            // Never two sessions for one task: the live runner is stopping, so
            // say so instead of launching a second one beside it.
            this._runnerPtyGate(
                `${this.esc(String(data.message || 'Stopping the live session first.'))} Retry again once it has ended.`,
                'warning');
            return true;
        }
        if (data.action === 'started' || data.action === 'starting') {
            this._runnerPtyGate(
                `Starting on ${this.esc(String(data.host_id || 'your Mac'))} — the live terminal opens as soon as the runner binds.`,
                'secondary');
            const deadline = Date.now() + 90000;
            while (Date.now() < deadline) {
                await new Promise((resolve) => setTimeout(resolve, 3000));
                try {
                    const q = `project=${encodeURIComponent(project)}`;
                    const state = await (await fetch(
                        `api/tasks/${encodeURIComponent(id)}/execution?${q}`,
                        { cache: 'no-store' })).json();
                    if (state && state.running === true) {
                        return this.openRunnerSessionPanel(id, opts);
                    }
                } catch (e) { /* transient; keep polling the authoritative projection */ }
            }
            this._runnerPtyGate('The session has not bound yet — reopen this panel to attach.', 'warning');
            return true;
        }
        const reason = (data.last_dispatch_outcome && data.last_dispatch_outcome.message)
            || data.message || data.error || 'start refused';
        this._runnerPtyGate(
            `<span class="badge bg-red-lt me-1">${this.esc(String(data.start_error || data.error_code || 'start_failed'))}</span>`
            + this.esc(String(reason)),
            'danger');
        return true;
    },

    async resumeTaskReview(taskId, opts) {
        const id = String(taskId || '').trim();
        if (!id) return false;
        const box = document.getElementById('runner-pty-resume');
        const button = document.getElementById('runner-pty-resume-review');
        const note = document.getElementById('runner-pty-resume-note');
        if (button) button.disabled = true;
        if (note) note.textContent = 'Starting one replacement reviewer on your enrolled Mac…';
        try {
            const project = window.PM_PROJECT || 'maxwell';
            const res = await fetch(`/api/tasks/${encodeURIComponent(id)}/resume-review?project=${encodeURIComponent(project)}`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ project }),
            });
            const result = await res.json();
            if (!res.ok || !result.resumed) throw new Error(result.reason || result.error || 'replacement runner was not started');
            if (note) note.textContent = result.continuation_mode === 'resume_conversation'
                ? 'Resuming the same Codex conversation…'
                : 'Replacement started with the saved review handoff…';
            this._runnerPtyIntentTask = null;
            const deadline = Date.now() + 30000;
            while (Date.now() < deadline) {
                await new Promise((resolve) => setTimeout(resolve, 1000));
                const watch = await fetch(`/ixp/v1/runner_sessions/watch?project=${encodeURIComponent(window.PM_PROJECT || 'maxwell')}&task_id=${encodeURIComponent(id)}`, { cache: 'no-store' });
                const state = await watch.json();
                if (watch.ok && state && state.watchable !== false && !state.error) {
                    if (box) box.hidden = true;
                    return this.openRunnerSessionPanel(id, opts || {});
                }
            }
            if (note) note.textContent = 'Replacement is queued. This panel will show it when the Mac starts it.';
            return true;
        } catch (e) {
            if (note) note.textContent = `Could not resume review: ${e.message}`;
            if (button) button.disabled = false;
            return false;
        }
    },

    async _runnerPtyConnect() {
        const rp = this._runnerPty;
        if (!rp) return;
        this._runnerPtyGate('<span class="spinner-border spinner-border-sm me-1"></span>Connecting…', 'secondary');
        // SIMPLIFY-10: one command opens the session. The server resolves which
        // execution is current, attaches the host tunnel, and mints the relay
        // ticket — the browser no longer sends a runner id it chose itself, so
        // it can no longer attach to a session the server has superseded. A
        // refusal arrives as one truthful reason instead of two half-failures.
        let ticket;
        try {
            const res = await fetch(`api/tasks/${encodeURIComponent(rp.taskId)}/execution/open`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ project: window.PM_PROJECT || 'maxwell' }),
            });
            ticket = await res.json();
            if (!res.ok || ticket.error) {
                throw new Error(this._runnerPtyApiError(ticket, `HTTP ${res.status}`));
            }
            if (!ticket.relay_url) {
                throw new Error(ticket.reason || 'relay ticket has no browser-safe URL');
            }
            // The server is authoritative for session identity; adopt whatever
            // execution it actually opened.
            if (ticket.execution_id) rp.runnerSessionId = ticket.execution_id;
        } catch (e) {
            this._runnerPtyGate(`Could not open a browser-safe relay ticket: ${this.esc(e.message)}`, 'danger');
            return;
        }
        if (ticket.opened === false) {
            // The relay is live but the host tunnel was refused: say so rather
            // than leaving the terminal at "waiting for output" with no reason.
            this._runnerPtyGate(
                `Host tunnel did not open: ${this.esc(String((ticket.host_open || {}).reason || 'refused'))}. `
                + 'The relay ticket is live but no bytes will arrive.',
                'danger');
        }
        try {
            await this._ensureXterm();
        } catch (e) {
            this._runnerPtyGate(`Terminal renderer unavailable: ${this.esc(e.message)}`, 'danger');
            return;
        }
        if (this._runnerPty !== rp) return; // superseded by a newer open() while we awaited
        // Reconnects reuse the live Terminal/ResizeObserver instead of
        // rebuilding them: rebuilding on every drop wiped the operator's
        // visible scrollback and leaked the prior ResizeObserver (it was
        // never .disconnect()ed), accumulating across the reconnect backoff.
        const reconnecting = !!rp.term;
        if (!reconnecting) this._runnerPtyMountTerminal(rp);
        this._runnerPtyOpenSocket(rp, ticket.relay_url, reconnecting);
    },

    _runnerPtyApiError(payload, fallback) {
        // Prefer a human string. Typed task-execution refusals return the
        // envelope at the top level (message/error_code); legacy routes still
        // nest under detail. Never String(object) → "[object Object]".
        if (payload && typeof payload === 'object') {
            if (typeof payload.message === 'string' && payload.message) return payload.message;
            if (typeof payload.error === 'string' && payload.error) return payload.error;
            if (typeof payload.error_code === 'string' && payload.error_code) {
                return payload.error_code;
            }
        }
        const value = (payload && (payload.error || payload.detail || payload.message)) || fallback;
        if (typeof value === 'string') return value;
        if (value && typeof value === 'object') {
            return value.message || value.error || value.error_code
                || (Array.isArray(value.missing) && value.missing.length
                    ? `missing: ${value.missing.join(', ')}` : JSON.stringify(value));
        }
        return String(value || fallback || 'request failed');
    },

    _runnerPtyMountTerminal(rp) {
        const els = this._runnerPtyEls();
        if (!els.termMount) return;
        els.termMount.innerHTML = '';
        const term = new window.Terminal({
            convertEol: true, cursorBlink: true, fontSize: 13,
            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
            theme: { background: '#0d0f13', foreground: '#c9ced6' },
        });
        const fitAddon = new window.FitAddon.FitAddon();
        term.loadAddon(fitAddon);
        term.open(els.termMount);
        try { fitAddon.fit(); } catch (e) { /* container may be 0-sized mid-transition */ }
        term.onData((data) => this._runnerPtySendInput(data));
        // xterm.js reserves onBinary for the small set of legacy mouse reports
        // that are not UTF-8. Preserve those byte values instead of sending
        // them through TextEncoder.
        term.onBinary((data) => this._runnerPtySendBinary(data));
        rp.term = term;
        rp.fitAddon = fitAddon;
        rp.resizeObserver = new ResizeObserver(() => {
            if (rp.resizeTimer) clearTimeout(rp.resizeTimer);
            rp.resizeTimer = setTimeout(() => {
                try { fitAddon.fit(); } catch (e) { return; }
                this._runnerPtySendResize();
            }, 50);
        });
        rp.resizeObserver.observe(els.termMount);
    },

    _runnerPtyEncodeFrame(type, payload) {
        return JSON.stringify(Object.assign({ type }, payload || {}));
    },

    _runnerPtyRequestId(prefix) {
        const suffix = (window.crypto && typeof window.crypto.randomUUID === 'function')
            ? window.crypto.randomUUID()
            : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
        return `runner-${prefix || 'op'}-${suffix}`;
    },

    _runnerPtyB64FromString(str) {
        const bytes = new TextEncoder().encode(str);
        let binary = '';
        bytes.forEach((b) => { binary += String.fromCharCode(b); });
        return btoa(binary);
    },

    _runnerPtySendInput(data) {
        const rp = this._runnerPty;
        if (!rp || !rp.ws || rp.ws.readyState !== WebSocket.OPEN) return;
        rp.ws.send(this._runnerPtyEncodeFrame('input', { data_b64: this._runnerPtyB64FromString(data) }));
    },

    _runnerPtySendBinary(data) {
        const rp = this._runnerPty;
        if (!rp || !rp.ws || rp.ws.readyState !== WebSocket.OPEN) return;
        let binary = '';
        for (let i = 0; i < data.length; i++) binary += String.fromCharCode(data.charCodeAt(i) & 0xff);
        rp.ws.send(this._runnerPtyEncodeFrame('input', { data_b64: btoa(binary) }));
    },

    _runnerPtySendResize() {
        const rp = this._runnerPty;
        if (!rp || !rp.term || !rp.ws || rp.ws.readyState !== WebSocket.OPEN) return;
        rp.ws.send(this._runnerPtyEncodeFrame('resize', { rows: rp.term.rows, cols: rp.term.cols }));
    },

    _runnerPtyOpenSocket(rp, relayUrl, reconnecting) {
        let ws;
        try {
            ws = new WebSocket(relayUrl);
        } catch (e) {
            this._runnerPtyGate(`Could not open the relay socket: ${this.esc(e.message)}`, 'danger');
            return;
        }
        rp.ws = ws;
        ws.addEventListener('open', () => {
            if (this._runnerPty !== rp) return;
            rp.reconnectAttempts = 0;
            this._runnerPtyGate('Connected — waiting for output…', 'secondary');
            if (reconnecting && rp.term) {
                // The relay's bounded replay buffer backfills from here, and
                // since the terminal (unlike before) kept its prior
                // scrollback, replay's tail can overlap what's already on
                // screen — mark the seam instead of leaving it unexplained.
                rp.term.write('\r\n\x1b[2m─── reconnected ───\x1b[0m\r\n');
            }
            this._runnerPtySendResize();
            const live = document.getElementById('runner-pty-live');
            if (live) live.hidden = false;
        });
        ws.addEventListener('message', (ev) => {
            if (this._runnerPty !== rp) return;
            this._runnerPtyHandleFrame(rp, ev.data);
        });
        const scheduleReconnect = () => {
            if (this._runnerPty !== rp) return;
            const live = document.getElementById('runner-pty-live');
            if (live) live.hidden = true;
            rp.reconnectAttempts = (rp.reconnectAttempts || 0) + 1;
            if (rp.reconnectAttempts > 8) {
                this._runnerPtyGate('Lost the relay connection and gave up reconnecting. Close and reopen Watch/Chat to retry.', 'danger');
                return;
            }
            const delayMs = Math.min(1000 * 2 ** rp.reconnectAttempts, 15000);
            this._runnerPtyGate(`Reconnecting in ${Math.round(delayMs / 1000)}s… (bounded replay will backfill on reconnect)`, 'warning');
            rp.reconnectTimer = setTimeout(() => { if (this._runnerPty === rp) this._runnerPtyConnect(); }, delayMs);
        };
        ws.addEventListener('close', scheduleReconnect);
        ws.addEventListener('error', () => { try { ws.close(); } catch (e) { /* close handler reconnects */ } });
    },

    _runnerPtyHandleFrame(rp, raw) {
        let frame;
        try { frame = JSON.parse(raw); } catch (e) { return; }
        const type = frame.type;
        if (type === 'output' || type === 'replay') {
            if (!frame.data_b64 || !rp.term) return;
            const binary = atob(frame.data_b64);
            const bytes = new Uint8Array(binary.length);
            for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
            rp.term.write(bytes);
            this._runnerPtyGate('', 'secondary');
        } else if (type === 'error') {
            this._runnerPtyGate(`Relay error: ${this.esc(frame.reason || frame.detail || 'unknown')}`, 'danger');
        } else if (type === 'close') {
            this._runnerPtyGate(`Session closed: ${this.esc(frame.reason || 'ended')}`, 'secondary');
        } else if (type === 'control_ack') {
            const pending = rp.pendingChat && rp.pendingChat[frame.request_id];
            if (!pending) return;
            delete rp.pendingChat[frame.request_id];
            if (pending.timer) clearTimeout(pending.timer);
            const badge = pending.entry && pending.entry.querySelector('[data-runner-chat-status]');
            if (frame.ok) {
                if (pending.phase === 'text') {
                    // Codex detects a text+Enter burst as a paste and can leave
                    // it sitting in the composer.  Wait for proof that the text
                    // reached the PTY, then send Enter as its own keypress.
                    const submitId = this._runnerPtyRequestId('chat-submit');
                    const submitPending = {
                        entry: pending.entry,
                        text: pending.text,
                        phase: 'submit',
                        timer: null,
                    };
                    submitPending.timer = setTimeout(() => {
                        if (!rp.pendingChat || !rp.pendingChat[submitId]) return;
                        delete rp.pendingChat[submitId];
                        if (badge) {
                            badge.className = 'badge bg-red-lt';
                            badge.textContent = 'Press Enter';
                        }
                        this._runnerPtyGate('The message reached Codex, but Enter was not acknowledged. Press Enter in the terminal to submit it.', 'danger');
                    }, 10000);
                    rp.pendingChat[submitId] = submitPending;
                    setTimeout(() => {
                        if (!rp.pendingChat || !rp.pendingChat[submitId]) return;
                        if (!rp.ws || rp.ws.readyState !== WebSocket.OPEN) {
                            clearTimeout(submitPending.timer);
                            delete rp.pendingChat[submitId];
                            if (badge) {
                                badge.className = 'badge bg-red-lt';
                                badge.textContent = 'Press Enter';
                            }
                            this._runnerPtyGate('The message reached Codex, but Watch disconnected before Enter. Press Enter after reconnecting.', 'danger');
                            return;
                        }
                        rp.ws.send(this._runnerPtyEncodeFrame('input', {
                            request_id: submitId,
                            purpose: 'chat_submit',
                            task_id: rp.taskId,
                            data_b64: this._runnerPtyB64FromString('\r'),
                        }));
                    }, 75);
                    return;
                }
                if (badge) {
                    badge.className = 'badge bg-green-lt';
                    badge.textContent = 'Delivered';
                }
                this._runnerPtyGate(`Delivered to ${rp.taskId} · ${rp.runnerSessionId}`, 'green');
            } else {
                if (badge) {
                    badge.className = 'badge bg-red-lt';
                    badge.textContent = pending.phase === 'submit' ? 'Press Enter' : 'Failed';
                }
                if (pending.phase === 'submit') {
                    this._runnerPtyGate(`The message reached Codex, but Enter failed: ${this.esc(frame.error || 'runner refused input')}. Press Enter in the terminal to submit it.`, 'danger');
                    return;
                }
                const els = this._runnerPtyEls();
                if (els.chatInput && !els.chatInput.value) els.chatInput.value = pending.text;
                this._runnerPtyGate(`Message was not delivered: ${this.esc(frame.error || 'runner refused input')}`, 'danger');
            }
        }
    },

    async _runnerPtySendChat(kind) {
        const rp = this._runnerPty;
        const els = this._runnerPtyEls();
        const flash = (msg, cls) => this._runnerPtyGate(this.esc(msg), cls);
        if (!rp || !rp.runnerSessionId || !rp.taskId) {
            flash('Open Watch / Chat on a bound runner first', 'warning');
            return;
        }
        const text = ((els.chatInput && els.chatInput.value) || '').trim();
        if (!text) return;
        if (els.chatSend) els.chatSend.disabled = true;
        let entry = null;
        if (els.chatLog) {
            els.chatLog.insertAdjacentHTML('beforeend',
                `<div class="d-flex align-items-start gap-1 mb-1"><span class="badge bg-yellow-lt" data-runner-chat-status>Sending</span><span>${this.esc(text)}</span></div>`);
            entry = els.chatLog.lastElementChild;
            els.chatLog.scrollTop = els.chatLog.scrollHeight;
        }
        try {
            // Normal Watch chat uses the already-connected full-duplex relay.
            // The host acknowledges the exact local PTY write on the same socket,
            // so delivery is RTT-bound instead of waiting for daemon polling.
            if (rp.ws && rp.ws.readyState === WebSocket.OPEN) {
                const requestId = this._runnerPtyRequestId('chat');
                rp.pendingChat = rp.pendingChat || {};
                if (els.chatInput) els.chatInput.value = '';
                const timer = setTimeout(() => {
                    const pending = rp.pendingChat && rp.pendingChat[requestId];
                    if (!pending) return;
                    delete rp.pendingChat[requestId];
                    const badge = pending.entry && pending.entry.querySelector('[data-runner-chat-status]');
                    if (badge) {
                        badge.className = 'badge bg-red-lt';
                        badge.textContent = 'No acknowledgement';
                    }
                    if (els.chatInput && !els.chatInput.value) els.chatInput.value = pending.text;
                    this._runnerPtyGate('The live runner did not acknowledge the message. It was restored so you can retry.', 'danger');
                }, 10000);
                rp.pendingChat[requestId] = { entry, text, phase: 'text', timer };
                rp.ws.send(this._runnerPtyEncodeFrame('input', {
                    request_id: requestId,
                    purpose: 'chat_text',
                    task_id: rp.taskId,
                    data_b64: this._runnerPtyB64FromString(text),
                }));
                return;
            }
            // If the relay is reconnecting, retain the durable host queue as a
            // fallback — through the task-scoped send_message command, so the
            // server still owns which execution receives the text.
            const res = await fetch(`api/tasks/${encodeURIComponent(rp.taskId)}/execution/message`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ project: window.PM_PROJECT || 'maxwell', text }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || data.error || data.error_code) {
                throw new Error(this._runnerPtyApiError(data, `HTTP ${res.status}`));
            }
            if (els.chatInput) els.chatInput.value = '';
            this._runnerPtyAwaitChatDelivery(data.control_request_id, entry, text);
        } catch (e) {
            flash(`inject failed: ${e.message}`, 'danger');
            if (els.chatInput && !els.chatInput.value) els.chatInput.value = text;
        } finally {
            if (els.chatSend) els.chatSend.disabled = false;
        }
    },

    async _runnerPtyAwaitChatDelivery(requestId, entry, text) {
        const rp = this._runnerPty;
        const statusBadge = entry && entry.querySelector('[data-runner-chat-status]');
        if (!requestId || !rp) return;
        const deadline = Date.now() + 30000;
        while (Date.now() < deadline && this._runnerPty === rp) {
            try {
                const q = `project=${encodeURIComponent(window.PM_PROJECT || 'maxwell')}&runner_session_id=${encodeURIComponent(rp.runnerSessionId)}`;
                const data = await (await fetch(`/ixp/v1/runner_controls?${q}`, { cache: 'no-store' })).json();
                const request = (data.requests || []).find((item) => item.request_id === requestId);
                if (request && request.status === 'completed') {
                    if (statusBadge) {
                        statusBadge.className = 'badge bg-green-lt';
                        statusBadge.textContent = 'Delivered';
                    }
                    this._runnerPtyGate(`Delivered to ${rp.taskId} · ${rp.runnerSessionId}`, 'green');
                    return;
                }
                if (request && ['failed', 'cancelled', 'refused'].includes(request.status)) {
                    if (statusBadge) {
                        statusBadge.className = 'badge bg-red-lt';
                        statusBadge.textContent = 'Failed';
                    }
                    const els = this._runnerPtyEls();
                    if (els.chatInput && !els.chatInput.value) els.chatInput.value = text;
                    this._runnerPtyGate(`Message was not delivered: ${(request.result || {}).error || request.status}`, 'danger');
                    return;
                }
            } catch (e) { /* keep waiting; the queued request remains durable */ }
            await new Promise((resolve) => setTimeout(resolve, 750));
        }
        if (statusBadge) {
            statusBadge.className = 'badge bg-yellow-lt';
            statusBadge.textContent = 'Queued';
        }
        if (this._runnerPty === rp) this._runnerPtyGate('Message queued on the host; delivery confirmation is still pending.', 'warning');
    },
    };

    global.SwitchboardRunnerSession = Object.freeze({ methods });
})(window);
