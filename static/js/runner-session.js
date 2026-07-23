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
    XTERM_JS_SRC: '/vendor/xterm/xterm.js',
    XTERM_FIT_SRC: '/vendor/xterm/addon-fit.js',

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
            `<button class="btn btn-sm btn-${color}" data-runner-id="${this.esc(s.runner_session_id)}" data-runner-task="${this.esc(s.task_id || '')}" data-runner-action="${action}"${disabled ? ' disabled' : ''} title="${this.esc(label)}"><i class="ti ti-${icon}"></i></button>`;
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
            kill: `/api/tasks/${encodeURIComponent(taskId)}/execution/stop`,
            open: `/api/tasks/${encodeURIComponent(taskId)}/execution/open`,
            inject: `/api/tasks/${encodeURIComponent(taskId)}/execution/message`,
        };
        const endpoint = endpoints[action];
        if (!endpoint) {
            flash(`${action} is read-only in the task execution view`, 'warning');
            return;
        }
        try {
            const body = {
                project: window.PM_PROJECT || 'maxwell',
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
            newOutput: document.getElementById('runner-pty-new-output'),
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
        if (els.newOutput) els.newOutput.addEventListener('click', () => {
            const rp = this._runnerPty;
            if (!rp || !rp.term) return;
            rp.term.scrollToBottom();
            els.newOutput.hidden = true;
        });
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
            const project = encodeURIComponent(window.PM_PROJECT || 'maxwell');
            const state = await (await fetch(
                `/api/tasks/${encodeURIComponent(id)}/execution?project=${project}`,
                { cache: 'no-store' })).json();
            const runner = state?.execution?.active_runner || null;
            watch = {
                ...state,
                watchable: !!runner && !state.error && !state.error_code,
                session: runner,
                runner_session_id: runner?.runner_session_id || '',
                sessions: state.has_ended_session ? [{}] : [],
            };
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
            // BUG-135: a transient host_not_attached / detached flap must not
            // tear down a healthy terminal for this task. Keep the xterm and
            // surface the honest Detached gate on the existing panel.
            const refusedSid = String(
                watch?.runner_session_id
                || (sessions[0] && sessions[0].runner_session_id)
                || '');
            const panelHint = (watch && watch.panel && typeof watch.panel === 'object')
                ? watch.panel : null;
            const detachedRefusal = String(watch?.error_code || '') === 'host_not_attached'
                || String(panelHint?.state || '') === 'detached';
            if (detachedRefusal && this._runnerPty && this._runnerPty.term
                    && String(this._runnerPty.taskId || '') === id
                    && (!refusedSid || String(this._runnerPty.runnerSessionId || '') === refusedSid)) {
                this._runnerPtyShowShell(opts.dockInto);
                const liveBadge = document.getElementById('runner-pty-live');
                if (liveBadge) {
                    liveBadge.hidden = false;
                    liveBadge.className = 'badge bg-yellow-lt mt-2';
                    liveBadge.innerHTML = '<span class="status-dot status-dot-animated bg-yellow me-1"></span>Detached';
                }
                const detail = (panelHint && panelHint.detail)
                    || watch?.message
                    || 'Bridge detached — reconnecting to the host tunnel…';
                this._runnerPtyGate(
                    `<span class="badge bg-yellow-lt me-1">Detached</span>${this.esc(detail)}`,
                    'warning');
                return true;
            }
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
            const ended = watch?.has_ended_session === true || (currentSession && (
                currentSession.stale === true
                || endedStatuses.has(String(currentSession.status || '').toLowerCase())));
            const canResumeReview = taskStatus === 'In Review' && ended
                && watch?.resumable_review !== false;
            if (resumeBox) resumeBox.hidden = !canResumeReview;
            if (resumeButton && canResumeReview) {
                resumeButton.onclick = () => this.resumeTaskReview(id, opts);
            }
            const missing = (watch && watch.missing || []).join(', ') || 'bind fields';
            // WATCH-5: prefer the four-state panel (queued/starting/detached)
            // over a generic bind-incomplete refusal when a wake is in flight.
            const panel = (watch && watch.panel && typeof watch.panel === 'object')
                ? watch.panel : null;
            const panelState = String(panel?.state || '');
            const detail = (panel && panel.detail)
                || watch?.message
                || `Runner bind incomplete for Watch/Chat (missing: ${missing})`;
            // BUG-91: when the run never started, the dispatcher's reason is the
            // useful thing to show ("capacity exhausted for co-general: cap=4").
            // "Runner session is stale" describes the debris, not the problem.
            const dispatch = watch?.dispatch || null;
            const label = panel?.label || dispatch?.state || watch?.error_code || watch?.error
                || 'runner_bind_incomplete';
            const badgeClass = panelState === 'queued' ? 'bg-azure-lt'
                : panelState === 'starting' ? 'bg-blue-lt'
                : panelState === 'detached' ? 'bg-yellow-lt'
                : dispatch?.state === 'needs_attention' ? 'bg-orange-lt' : 'bg-red-lt';
            const tone = (panelState === 'queued' || panelState === 'starting'
                || panelState === 'detached'
                || dispatch?.state === 'needs_attention') ? 'warning' : 'danger';
            let extra = '';
            if (dispatch && Number(dispatch.dispatch_attempt) > 1) {
                extra += `<div class="mt-1 text-secondary">Dispatch attempt ${this.esc(String(dispatch.dispatch_attempt))}`
                    + `${dispatch.host_id ? ` · last host ${this.esc(String(dispatch.host_id))}` : ''}</div>`;
            }
            this._runnerPtyGate(
                `<span class="badge ${badgeClass} me-1">${this.esc(label)}</span>`
                + this.esc(detail) + extra,
                tone,
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
    // start, supersedes a failed attempt, or launches through Connect; the
    // browser only polls the authoritative execution projection afterwards.
    async startTaskSession(taskId, opts, retry = false) {
        const id = String(taskId || '').trim();
        if (!id) return false;
        const pending = document.getElementById('runner-pty-start-retry');
        if (pending) pending.disabled = true;
        this._runnerPtyGate(
            retry ? 'Superseding the last attempt…' : 'Starting an agent through Switchboard Connect…',
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
            // SIMPLIFY-9: Starting is opening. The server reserves the
            // deterministic session and returns a browser capability before
            // the host process exists; the hub buffers this attach until the
            // executor dials in.
            if (data.execution_id && data.relay_url) {
                const els = this._runnerPtyShowShell(opts && opts.dockInto);
                const mode = opts && opts.dockInto ? 'docked' : 'sidecar';
                this._runnerPtyTeardown();
                this._runnerPtyIntentTask = id;
                if (els.title) els.title.textContent = `${id} · ${data.execution_id}`;
                if (els.sub) els.sub.textContent = String(data.host_id || '').trim();
                const rp = {
                    taskId: id,
                    runnerSessionId: data.execution_id,
                    mode,
                    reconnectAttempts: 0,
                    relayUrl: data.relay_url,
                    relayExpiresAt: Number(data.expires_at || 0),
                };
                this._runnerPty = rp;
                try {
                    await this._ensureXterm();
                    if (this._runnerPty !== rp) return true;
                    this._runnerPtyMountTerminal(rp);
                    this._runnerPtyGate('Connected to the reserved session — waiting for the host…', 'secondary');
                    this._runnerPtyOpenSocket(rp, rp.relayUrl, false);
                } catch (e) {
                    this._runnerPtyGate(`Terminal renderer unavailable: ${this.esc(e.message)}`, 'danger');
                }
                return true;
            }
            const capacity = (data.capacity && typeof data.capacity === 'object')
                ? data.capacity : {};
            const hosts = Array.isArray(capacity.matching_online_hosts)
                ? capacity.matching_online_hosts : [];
            const saturated = hosts.find((host) => Number(host.available_sessions) === 0)
                || hosts[0];
            const ahead = Number(capacity.pending_ahead);
            const noCapacity = capacity.no_capacity && capacity.no_capacity.reason;
            const initialDetail = noCapacity
                ? `Queued — ${String(noCapacity).replaceAll('_', ' ')}`
                : saturated
                    ? `Queued behind ${Number(saturated.active_sessions || 0)} runs on ${String(saturated.host_id || 'host')}`
                    : Number.isFinite(ahead) && ahead > 0
                        ? `Queued behind ${ahead} pending ${ahead === 1 ? 'run' : 'runs'}`
                        : `Starting on ${String(data.host_id || 'your Mac')} — the live terminal opens as soon as the runner binds.`;
            this._runnerPtyGate(this.esc(initialDetail), noCapacity || saturated ? 'warning' : 'secondary');
            const deadline = Date.now() + 90000;
            while (Date.now() < deadline) {
                await new Promise((resolve) => setTimeout(resolve, 3000));
                try {
                    const q = `project=${encodeURIComponent(project)}`;
                    const state = await (await fetch(
                        `api/tasks/${encodeURIComponent(id)}/execution?${q}`,
                        { cache: 'no-store' })).json();
                    // WATCH-5: refresh the gate from the honest panel state while
                    // polling — Queued must not look identical to Live.
                    const panel = (state && state.panel) || {};
                    if (panel.state === 'queued' || panel.state === 'starting'
                            || panel.state === 'detached') {
                        const badge = panel.state === 'queued' ? 'bg-azure-lt'
                            : panel.state === 'starting' ? 'bg-blue-lt' : 'bg-yellow-lt';
                        this._runnerPtyGate(
                            `<span class="badge ${badge} me-1">${this.esc(panel.label || panel.state)}</span>`
                            + this.esc(panel.detail || panel.state),
                            'warning');
                    }
                    if (state && (state.running === true || panel.state === 'live'
                            || panel.state === 'detached')) {
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
        if (note) note.textContent = 'Starting one agent through Switchboard Connect…';
        try {
            const project = window.PM_PROJECT || 'maxwell';
            const res = await fetch(`/api/tasks/${encodeURIComponent(id)}/start`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ project, runtime: 'codex' }),
            });
            const result = await res.json();
            if (!res.ok || !(result.started || result.starting || result.attached)) throw new Error(result.reason || result.error || 'agent was not started');
            if (note) note.textContent = result.attached
                ? 'Attached to the existing agent…'
                : 'Starting one agent through Switchboard Connect…';
            this._runnerPtyIntentTask = null;
            const deadline = Date.now() + 30000;
            while (Date.now() < deadline) {
                await new Promise((resolve) => setTimeout(resolve, 1000));
                const response = await fetch(
                    `/api/tasks/${encodeURIComponent(id)}/execution?project=${encodeURIComponent(window.PM_PROJECT || 'maxwell')}`,
                    { cache: 'no-store' });
                const state = await response.json();
                if (response.ok && state?.execution?.active_runner && !state.error) {
                    if (box) box.hidden = true;
                    return this.openRunnerSessionPanel(id, opts || {});
                }
            }
            if (note) note.textContent = 'The agent is queued. This panel will show it when provider capacity starts it.';
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
        const reusable = rp.relayUrl && (!rp.relayExpiresAt
            || Date.now() < (Number(rp.relayExpiresAt) - 15) * 1000);
        if (reusable) {
            ticket = { relay_url: rp.relayUrl, execution_id: rp.runnerSessionId, opened: true };
        } else {
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
                rp.relayUrl = ticket.relay_url;
                rp.relayExpiresAt = Number(ticket.expires_at || 0);
            } catch (e) {
                this._runnerPtyGate(`Could not open a browser-safe relay ticket: ${this.esc(e.message)}`, 'danger');
                return;
            }
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
            scrollback: 5000,
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
        term.onScroll(() => {
            const active = term.buffer && term.buffer.active;
            const newOutput = this._runnerPtyEls().newOutput;
            if (active && newOutput && active.viewportY >= active.baseY) newOutput.hidden = true;
        });
        rp.term = term;
        rp.fitAddon = fitAddon;
        rp.followOutput = true;
        rp.resizeObserver = new ResizeObserver(() => {
            if (rp.resizeTimer) clearTimeout(rp.resizeTimer);
            rp.resizeTimer = setTimeout(() => {
                try { fitAddon.fit(); } catch (e) { return; }
                this._runnerPtySendResize();
            }, 50);
        });
        rp.resizeObserver.observe(els.termMount);
        // Track whether the operator is following the live tail so snapshot /
        // reconnect bursts can restore that intent (BUG-135). Compute from the
        // buffer itself — assigning from _runnerPtyIsFollowing here was circular
        // (the helper returned the flag), leaving followOutput stuck true and
        // force-scrolling scrolled-up readers on every write (BUG-134).
        try {
            term.onScroll(() => {
                try {
                    const buf = rp.term.buffer.active;
                    rp.followOutput = buf.viewportY >= (buf.baseY - 1);
                } catch (e) { /* keep the last known intent */ }
            });
        } catch (e) { /* older xterm builds may omit onScroll */ }
    },

    _runnerPtyIsFollowing(rp) {
        // Live buffer position is the truth; the tracked flag only covers the
        // brief window where the buffer is unreadable (teardown, older xterm).
        if (!rp || !rp.term) return true;
        try {
            const buf = rp.term.buffer.active;
            return buf.viewportY >= (buf.baseY - 1);
        } catch (e) {
            return typeof rp.followOutput === 'boolean' ? rp.followOutput : true;
        }
    },

    _runnerPtyTypeIds: {
        ready: 1, exit: 2, out: 3, in: 4, resize: 5, signal: 6, snapshot: 7,
    },

    _runnerPtyEncodeFrame(type, payload, dataBytes) {
        // SIMPLIFY-9 binary wire: SB1\0 + type_id + u16 header_len + u32 data_len + JSON header + data
        const typeId = this._runnerPtyTypeIds[type];
        if (!typeId) throw new Error(`unknown_frame_type:${type}`);
        const headerObj = Object.assign({}, payload || {});
        delete headerObj.type;
        delete headerObj.data;
        delete headerObj.data_b64;
        const headerJson = JSON.stringify(headerObj);
        const headerBytes = new TextEncoder().encode(headerJson);
        const data = dataBytes
            ? (dataBytes instanceof Uint8Array ? dataBytes : new Uint8Array(dataBytes))
            : new Uint8Array(0);
        const out = new Uint8Array(4 + 1 + 2 + 4 + headerBytes.length + data.length);
        out[0] = 0x53; out[1] = 0x42; out[2] = 0x31; out[3] = 0x00; // SB1\0
        out[4] = typeId & 0xff;
        out[5] = (headerBytes.length >> 8) & 0xff;
        out[6] = headerBytes.length & 0xff;
        out[7] = (data.length >>> 24) & 0xff;
        out[8] = (data.length >>> 16) & 0xff;
        out[9] = (data.length >>> 8) & 0xff;
        out[10] = data.length & 0xff;
        out.set(headerBytes, 11);
        if (data.length) out.set(data, 11 + headerBytes.length);
        return out.buffer;
    },

    _runnerPtyDecodeFrame(raw) {
        const bytes = raw instanceof ArrayBuffer
            ? new Uint8Array(raw)
            : (raw instanceof Uint8Array ? raw : null);
        if (!bytes || bytes.length < 11 || bytes[0] !== 0x53 || bytes[1] !== 0x42
            || bytes[2] !== 0x31 || bytes[3] !== 0x00) {
            return null;
        }
        const typeId = bytes[4];
        const headerLen = (bytes[5] << 8) | bytes[6];
        const dataLen = ((bytes[7] << 24) | (bytes[8] << 16) | (bytes[9] << 8) | bytes[10]) >>> 0;
        const idToType = { 1: 'ready', 2: 'exit', 3: 'out', 4: 'in', 5: 'resize', 6: 'signal', 7: 'snapshot' };
        const type = idToType[typeId];
        if (!type) return null;
        const headerStart = 11;
        const dataStart = headerStart + headerLen;
        if (dataLen > 16 * 1024 * 1024 || dataStart + dataLen !== bytes.length) return null;
        let header = {};
        if (headerLen) {
            try {
                header = JSON.parse(new TextDecoder().decode(bytes.subarray(headerStart, dataStart)));
            } catch (e) { return null; }
        }
        const frame = Object.assign({}, header, { type });
        if (dataLen) frame.data = bytes.subarray(dataStart, dataStart + dataLen);
        return frame;
    },

    _runnerPtyRequestId(prefix) {
        const suffix = (window.crypto && typeof window.crypto.randomUUID === 'function')
            ? window.crypto.randomUUID()
            : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
        return `runner-${prefix || 'op'}-${suffix}`;
    },

    _runnerPtyUtf8Bytes(str) {
        return new TextEncoder().encode(str);
    },

    _runnerPtySendInput(data) {
        const rp = this._runnerPty;
        if (!rp || !rp.ws || rp.ws.readyState !== WebSocket.OPEN) return;
        rp.ws.send(this._runnerPtyEncodeFrame('in', {}, this._runnerPtyUtf8Bytes(data)));
    },

    _runnerPtySendBinary(data) {
        const rp = this._runnerPty;
        if (!rp || !rp.ws || rp.ws.readyState !== WebSocket.OPEN) return;
        const bytes = new Uint8Array(data.length);
        for (let i = 0; i < data.length; i++) bytes[i] = data.charCodeAt(i) & 0xff;
        rp.ws.send(this._runnerPtyEncodeFrame('in', {}, bytes));
    },

    _runnerPtySendResize() {
        const rp = this._runnerPty;
        if (!rp || !rp.term || !rp.ws || rp.ws.readyState !== WebSocket.OPEN) return;
        rp.ws.send(this._runnerPtyEncodeFrame('resize', { rows: rp.term.rows, cols: rp.term.cols }));
    },

    _runnerPtyWritePreservingViewport(rp, data) {
        // BUG-134 + BUG-135 unified: the sticky follow flag (updated by the
        // BUG-135 onScroll tracker) decides intent during rapid write bursts;
        // the write CALLBACK restores position only after xterm has parsed the
        // data, so a repaint can never yank the viewport in between.
        if (!rp || !rp.term) return;
        const active = rp.term.buffer && rp.term.buffer.active;
        const viewportY = active ? active.viewportY : 0;
        const atBottom = this._runnerPtyIsFollowing(rp);
        rp.term.write(data, () => {
            if (this._runnerPty !== rp || !rp.term) return;
            if (atBottom) {
                rp.term.scrollToBottom();
                rp.followOutput = true;
            } else {
                rp.term.scrollToLine(viewportY);
                const newOutput = this._runnerPtyEls().newOutput;
                if (newOutput) newOutput.hidden = false;
            }
        });
    },

    _runnerPtyOpenSocket(rp, relayUrl, reconnecting) {
        let ws;
        try {
            ws = new WebSocket(relayUrl);
            ws.binaryType = 'arraybuffer';
        } catch (e) {
            this._runnerPtyGate(`Could not open the relay socket: ${this.esc(e.message)}`, 'danger');
            return;
        }
        rp.ws = ws;
        ws.addEventListener('open', () => {
            if (this._runnerPty !== rp) return;
            rp.reconnectAttempts = 0;
            rp.hostAttached = false;
            this._runnerPtyGate('Relay connected — waiting for Agent Host…', 'warning');
            if (reconnecting && rp.term) {
                // The relay's bounded replay buffer backfills from here, and
                // since the terminal (unlike before) kept its prior
                // scrollback, replay's tail can overlap what's already on
                // screen — mark the seam instead of leaving it unexplained.
                this._runnerPtyWritePreservingViewport(
                    rp, '\r\n\x1b[2m─── reconnected ───\x1b[0m\r\n');
            }
            const live = document.getElementById('runner-pty-live');
            if (live) live.hidden = true;
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
        const frame = this._runnerPtyDecodeFrame(raw);
        if (!frame) return;
        const type = frame.type;
        if (type === 'out' || type === 'snapshot') {
            if (!frame.data || !rp.term) return;
            // BUG-134 + BUG-135: snapshot/reconnect bursts used to yank the
            // viewport. Follow-tail stays pinned to the bottom; a scrolled-up
            // reader keeps their exact line and gets the "New output below"
            // affordance instead of losing their place.
            this._runnerPtyWritePreservingViewport(rp, frame.data);
            this._runnerPtyGate('', 'secondary');
        } else if (type === 'exit') {
            this._runnerPtyGate(`Session closed: ${this.esc(frame.reason || frame.detail || 'ended')}`, 'secondary');
        } else if (type === 'ready' && !frame.request_id
                && Object.prototype.hasOwnProperty.call(frame, 'host_attached')) {
            rp.hostAttached = frame.host_attached === true;
            const live = document.getElementById('runner-pty-live');
            if (live) live.hidden = !rp.hostAttached;
            if (rp.hostAttached) {
                const liveBadge = document.getElementById('runner-pty-live');
                if (liveBadge) {
                    liveBadge.hidden = false;
                    liveBadge.innerHTML = '<span class="status-dot status-dot-animated bg-green me-1"></span>Live';
                }
                this._runnerPtyGate(
                    '<span class="badge bg-green-lt me-1">Live</span>Connected to Agent Host — waiting for output…',
                    'secondary');
                this._runnerPtySendResize();
            } else {
                // WATCH-5: explicit host_attached=false is Detached, not a vague wait.
                const liveBadge = document.getElementById('runner-pty-live');
                if (liveBadge) {
                    liveBadge.hidden = false;
                    liveBadge.className = 'badge bg-yellow-lt mt-2';
                    liveBadge.innerHTML = '<span class="status-dot status-dot-animated bg-yellow me-1"></span>Detached';
                }
                this._runnerPtyGate(
                    '<span class="badge bg-yellow-lt me-1">Detached</span>Bridge detached — reconnecting to the host tunnel…',
                    'warning');
            }
        } else if (type === 'ready' && frame.request_id) {
            // Delivery ack for Watch chat (replaces legacy control_ack).
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
                        rp.ws.send(this._runnerPtyEncodeFrame('in', {
                            request_id: submitId,
                            purpose: 'chat_submit',
                            task_id: rp.taskId,
                        }, this._runnerPtyUtf8Bytes('\r')));
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
                rp.ws.send(this._runnerPtyEncodeFrame('in', {
                    request_id: requestId,
                    purpose: 'chat_text',
                    task_id: rp.taskId,
                }, this._runnerPtyUtf8Bytes(text)));
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
            const statusBadge = entry && entry.querySelector('[data-runner-chat-status]');
            if (statusBadge) {
                statusBadge.className = 'badge bg-yellow-lt';
                statusBadge.textContent = 'Queued';
            }
            this._runnerPtyGate('Message queued through Task Execution.', 'warning');
        } catch (e) {
            flash(`inject failed: ${e.message}`, 'danger');
            if (els.chatInput && !els.chatInput.value) els.chatInput.value = text;
        } finally {
            if (els.chatSend) els.chatSend.disabled = false;
        }
    },

    };

    global.SwitchboardRunnerSession = Object.freeze({ methods });
})(window);
