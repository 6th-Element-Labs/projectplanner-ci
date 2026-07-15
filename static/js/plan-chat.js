/* BUG-60 / SEG-4: project-native, queued Ask Taikun chat and reconnect behavior. */
(function (global) {
    'use strict';

    function projectQS(extra) {
        const proj = (global.SwitchboardApi && global.SwitchboardApi.requireProject)
            ? global.SwitchboardApi.requireProject()
            : (global.PM_PROJECT || '').trim();
        if (!proj) throw new Error('project required');
        const params = new URLSearchParams(extra || {});
        params.set('project', proj);
        return params.toString();
    }

    const methods = {
    async initAsk() {
        if (this._askLoaded) return;
        this._askLoaded = true;
        try {
            const data = await (await fetch('api/chat/history?' + projectQS({ session: 'plan' }))).json();
            if ((data.messages || []).length) {
                const empty = document.getElementById('ask-empty');
                if (empty) empty.remove();
                this.renderAskMessages(data.messages);
                this._askScroll();
            }
        } catch (e) { /* leave the empty hint */ }
        try {
            const latest = await (await fetch('api/chat/runs/latest?' + projectQS({ session: 'plan' }))).json();
            const run = latest.run;
            if (run && (run.status === 'pending' || run.status === 'running')) {
                const log = document.getElementById('ask-log');
                if (log && !document.getElementById('ask-thinking')) {
                    log.insertAdjacentHTML('beforeend', this._thinking(
                        'ask-thinking', 'Taikun is continuing the project plan…'));
                }
                this._pollAskRun(run.run_id);
            } else if (run && (run.status === 'completed' || run.status === 'failed')
                       && !(this._askSeenRuns || new Set()).has(run.run_id)) {
                // A run can finish between the history and latest-run reads. Refresh from
                // durable chat, but never resurrect a run removed by Clear.
                const refreshed = await (await fetch('api/chat/history?' + projectQS({ session: 'plan' }))).json();
                const messages = refreshed.messages || [];
                if (messages.some((m) => m.payload && m.payload.run_id === run.run_id)) {
                    const empty = document.getElementById('ask-empty');
                    if (empty) empty.remove();
                    this.renderAskMessages(messages);
                    this._askScroll();
                }
            }
        } catch (e) { /* no resumable run */ }
    },

    renderAskMessages(messages) {
        const log = document.getElementById('ask-log');
        if (!log) return;
        this._askSeenRuns = new Set(messages.map((m) => m.payload && m.payload.run_id).filter(Boolean));
        log.innerHTML = messages.map((m) => {
            if (m.role === 'user')
                return this._bubble('user', this.esc(m.content));
            if (m.payload && m.payload.error)
                return this._bubble('error', this.esc(m.content));
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
        try { await fetch('api/chat?' + projectQS({ session: 'plan' }), { method: 'DELETE' }); } catch (e) { /* noop */ }
        const log = document.getElementById('ask-log');
        if (log) log.innerHTML = '<div class="text-secondary small">Cleared. Ask about the whole plan below.</div>';
    },

    _renderAskResult(data) {
        const log = document.getElementById('ask-log');
        const sources = data.sources || [];
        const src = sources.length
            ? `<div class="tk-sources">sources: ${sources.map((s) => this.esc(s)).join(', ')}</div>` : '';
        log.insertAdjacentHTML('beforeend', this._bubble('assistant', this.md(data.answer || ''), src));
        const props = (data.proposals && data.proposals.length)
            ? data.proposals : (data.proposal ? [data.proposal] : []);
        if (props.length === 1) this.renderAskProposal(props[0]);
        else if (props.length > 1) this.renderAskProposals(props);
        if ((data.new_tasks || []).length) this.renderAskNewTasks(data.new_tasks);
        if (data.run_id) {
            if (!this._askSeenRuns) this._askSeenRuns = new Set();
            this._askSeenRuns.add(data.run_id);
        }
        this._askScroll();
    },

    async _pollAskRun(runId) {
        if (this._askRunId === runId) return;
        this._askRunId = runId;
        const log = document.getElementById('ask-log');
        try {
            for (let attempt = 0; attempt < 600; attempt++) {
                const res = await fetch('api/chat/runs/' + encodeURIComponent(runId) + '?' + projectQS({ session: 'plan' }));
                const data = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(data.detail || ('HTTP ' + res.status));
                if (data.status === 'completed') {
                    const think = document.getElementById('ask-thinking');
                    if (think) think.remove();
                    this._renderAskResult(data);
                    return;
                }
                if (data.status === 'failed' || data.status === 'cancelled') {
                    throw new Error(data.error || 'The project-plan run failed.');
                }
                await new Promise((resolve) => setTimeout(resolve, 1000));
            }
            throw new Error('The plan is still running. Reopen Ask Taikun to resume run ' + runId + '.');
        } catch (e) {
            const think = document.getElementById('ask-thinking');
            if (think) think.remove();
            if (log) log.insertAdjacentHTML('beforeend', this._bubble('error', this.esc(e.message)));
        } finally {
            if (this._askRunId === runId) this._askRunId = null;
        }
    },

    async sendAsk(messageOverride, thinkingText) {
        const input = document.getElementById('ask-input');
        const log = document.getElementById('ask-log');
        const msg = ((messageOverride || input.value) || '').trim();
        if (!msg) return;
        if (!messageOverride) input.value = '';
        const empty = document.getElementById('ask-empty');
        if (empty) empty.remove();
        log.insertAdjacentHTML('beforeend', this._bubble('user', this.esc(msg)));
        log.insertAdjacentHTML('beforeend', this._thinking('ask-thinking', thinkingText));
        this._askScroll();
        try {
            const res = await fetch('api/chat?' + projectQS(), {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: msg, session: 'plan' }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                const think = document.getElementById('ask-thinking');
                if (think) think.remove();
                log.insertAdjacentHTML('beforeend', this._bubble('error', this.esc(data.detail || ('HTTP ' + res.status))));
                return;
            }
            if (data.run_id) await this._pollAskRun(data.run_id);
            else this._renderAskResult(data);
        } catch (e) {
            const think = document.getElementById('ask-thinking');
            if (think) think.remove();
            log.insertAdjacentHTML('beforeend', this._bubble('error', this.esc(e.message)));
        }
    },

    buildProjectPlan() {
        const prompt = 'Build or reconcile the overall project plan for this selected project using its '
            + 'segmented corpus, project contract, and live board. This is the customer/project delivery '
            + 'plan, not a software-development backlog. Separate authoritative current sources from '
            + 'historical or superseded context. Cover charter and objectives, scope and exclusions, owners '
            + 'and decision rights, workstreams, milestones, deliverables, dependencies, risks, success '
            + 'measures, and go/no-go gates. Reuse and improve existing tasks instead of creating duplicates. '
            + 'Stage any necessary task creations or updates for confirmation; do not apply them directly.';
        this.sendAsk(prompt, 'Taikun is building the project plan from this project’s corpus…');
    },
    };
    global.SwitchboardPlanChat = Object.freeze({ methods });
})(window);
