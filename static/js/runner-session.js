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
            <div class="card-body py-3" id="runner-control-body">
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
        if (rp && rp.taskId && rp.runnerSessionId) {
            // Closing the presentation tears down the socket/terminal, but a
            // repeat click on the same Deliverable node must keep meaning
            // "reopen that runner", even if the runner becomes stale between
            // the two clicks. The fresh watch gate remains authoritative.
            this._runnerPtyLast = {
                taskId: String(rp.taskId),
                runnerSessionId: String(rp.runnerSessionId),
            };
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
    // when the in-memory _runnerPtyLast hint no longer exists.
    async openRunnerSessionPanel(taskId, opts) {
        opts = opts || {};
        const id = String(taskId || '').trim();
        if (!id) return false;
        const remembered = (this._runnerPtyLast
            && String(this._runnerPtyLast.taskId || '') === id)
            ? this._runnerPtyLast : null;
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
        if (!watchable) {
            const sessions = (watch && Array.isArray(watch.sessions)) ? watch.sessions : [];
            // Preserve authoring fallback for tasks that have never had a
            // runner. An ended/stale session is real runner history, so reopen
            // its truthful gate instead of treating it as "no runner".
            if (opts.fallbackIfNotWatchable && !remembered && !sessions.length) return false;
            const els = this._runnerPtyShowShell(opts.dockInto);
            const rememberedSession = sessions.find((session) =>
                String(session.runner_session_id || '') === String(remembered?.runnerSessionId || ''))
                || sessions[0] || null;
            const rememberedSid = String(
                watch?.runner_session_id || rememberedSession?.runner_session_id
                || remembered?.runnerSessionId || '');
            // A stale/bind-incomplete result has no live _runnerPty object, so
            // _runnerPtyClose() cannot recover its identity during teardown.
            // Remember it as soon as discovery succeeds. Subsequent clicks can
            // then reopen the same truthful gate even if the authoritative
            // session list no longer includes that historical row.
            if (rememberedSid) {
                this._runnerPtyLast = { taskId: id, runnerSessionId: rememberedSid };
            }
            if (els.title) els.title.textContent = rememberedSid ? `${id} · ${rememberedSid}` : id;
            if (els.sub) els.sub.textContent = String(rememberedSession?.host_id || '');
            if (els.live) els.live.hidden = true;
            const missing = (watch && watch.missing || []).join(', ') || 'bind fields';
            const detail = watch?.message
                || `Runner bind incomplete for Watch/Chat (missing: ${missing})`;
            this._runnerPtyGate(
                `<span class="badge bg-red-lt me-1">${this.esc(watch?.error_code || watch?.error || 'runner_bind_incomplete')}</span>`
                + this.esc(detail),
                'danger',
            );
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
        this._runnerPtyLast = { taskId: id, runnerSessionId: sid };
        if (els.title) els.title.textContent = `${id} · ${sid}`;
        if (els.sub) els.sub.textContent = `${(watch.bind && watch.bind.host_id) || ''}`.trim();
        this._runnerPtyGate('', 'secondary');
        this._runnerPty = { taskId: id, runnerSessionId: sid, mode, reconnectAttempts: 0 };
        await this._runnerPtyConnect();
        return true;
    },

    async _runnerPtyConnect() {
        const rp = this._runnerPty;
        if (!rp) return;
        this._runnerPtyGate('<span class="spinner-border spinner-border-sm me-1"></span>Connecting…', 'secondary');
        // Ensure the host tunnel is attached (idempotent — a no-op if it's already
        // live). Fire in parallel with our own ticket mint rather than awaiting it
        // here (the browser can attach before the host does and will simply see no
        // bytes until it does) — but still surface a refusal once it resolves,
        // fail-closed, instead of leaving the terminal at "waiting for output"
        // forever with no explanation.
        fetch('/ixp/v1/request_runner_open', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                project: window.PM_PROJECT || 'maxwell',
                runner_session_id: rp.runnerSessionId,
                reason: `operator watch on task ${rp.taskId}`,
            }),
        }).then(async (res) => {
            const data = await res.json().catch(() => ({}));
            if (!res.ok || data.error || data.requested === false) {
                throw new Error(this._runnerPtyApiError(
                    data,
                    data.requested === false ? (data.reason || 'refused') : `HTTP ${res.status}`,
                ));
            }
        }).catch((e) => {
            if (this._runnerPty === rp) {
                this._runnerPtyGate(
                    `Host tunnel did not open: ${this.esc(e.message)}. The relay ticket is live but no bytes will arrive.`,
                    'danger');
            }
        });
        let ticket;
        try {
            const res = await fetch(`/ixp/v1/runner_sessions/${encodeURIComponent(rp.runnerSessionId)}/pty/ticket`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    project: window.PM_PROJECT || 'maxwell',
                    scopes: ['watch', 'input', 'resize', 'signal'],
                }),
            });
            ticket = await res.json();
            if (!res.ok || ticket.error) {
                throw new Error(this._runnerPtyApiError(ticket, `HTTP ${res.status}`));
            }
            if (!ticket.relay_url) throw new Error('relay ticket has no browser-safe URL');
        } catch (e) {
            this._runnerPtyGate(`Could not open a browser-safe relay ticket: ${this.esc(e.message)}`, 'danger');
            return;
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
        rp.term = term;
        rp.fitAddon = fitAddon;
        rp.resizeObserver = new ResizeObserver(() => {
            try { fitAddon.fit(); } catch (e) { return; }
            this._runnerPtySendResize();
        });
        rp.resizeObserver.observe(els.termMount);
    },

    _runnerPtyEncodeFrame(type, payload) {
        return JSON.stringify(Object.assign({ type }, payload || {}));
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
        const injectKind = (kind || 'freeform').toLowerCase();
        if (els.chatSend) els.chatSend.disabled = true;
        try {
            const res = await fetch('/ixp/v1/request_runner_inject', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    project: window.PM_PROJECT || 'maxwell',
                    runner_session_id: rp.runnerSessionId,
                    task_id: rp.taskId,
                    text,
                    kind: injectKind,
                    reason: `operator ${injectKind} chat from task ${rp.taskId}`,
                }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || data.error) throw new Error(data.error || data.detail || data.message || `HTTP ${res.status}`);
            if (data.requested === false) {
                throw new Error(data.reason || data.error || 'not accepted');
            }
            if (els.chatInput) els.chatInput.value = '';
            let entry = null;
            if (els.chatLog) {
                els.chatLog.insertAdjacentHTML('beforeend',
                    `<div class="d-flex align-items-start gap-1 mb-1"><span class="badge bg-yellow-lt" data-runner-chat-status>Sending</span><span>${this.esc(text)}</span></div>`);
                entry = els.chatLog.lastElementChild;
                els.chatLog.scrollTop = els.chatLog.scrollHeight;
            }
            this._runnerPtyAwaitChatDelivery(data.request_id, entry, text);
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
