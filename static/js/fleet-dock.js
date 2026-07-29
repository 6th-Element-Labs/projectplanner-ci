/* Autopilot dock presentation — PR/runner condition ranking and actions.
 * ============================================================================
 * Extracted from app.js (ARCH-MS-21 keeps the composition root under 5,000
 * lines; the dock render loop stays there, its presentation logic lives here).
 * Every function takes the composing app object first: this module owns no
 * state and no polling — `app._dockAutopilot` (the batched coverage map) and
 * the fetch/reload plumbing belong to the dock loop that calls in.
 */
(function () {
    'use strict';

    const FleetDock = {
        // A PR is usually several things at once (red CI *and* conflicting *and*
        // draft). Rank every condition that holds by how much it blocks the
        // merge and return them worst-first, so the card can show one
        // authoritative chip instead of a badge strip whose contents move
        // around. The ladder deliberately outranks `draft` with every real
        // problem: a broken build on a draft is still a broken build.
        prConditions(app, x) {
            const out = [];
            const p = x.completion_projection || {};
            // GitHub check names are often workflow-qualified ("Switchboard CI /
            // VM gate"); the job name identifies the failure, the full name
            // stays in the chip's tooltip.
            const failing = String((x.ci_failing || [])[0] || '').split(' / ').pop().trim();
            if (x.ci_state === 'failure') {
                out.push({ key: 'ci_failed', label: failing ? `${failing} failed` : 'CI failed',
                           tone: 'red', icon: 'x', title: (x.ci_failing || [])[0] || '' });
            }
            if (p.route === 'remediation') out.push({ key: 'remediation', label: 'Remediation owner', tone: 'red', icon: 'tool' });
            else if (p.route === 'coordination_retry') out.push({ key: 'coordination_retry', label: 'Coordination retry', tone: 'yellow', icon: 'refresh' });
            else if (p.route === 'review_merge') out.push({ key: 'review_merge', label: 'Review / merge', tone: 'azure', icon: 'git-merge' });
            else if (p.route === 'human') out.push({ key: 'human', label: 'Needs you', tone: 'orange', icon: 'user-exclamation' });
            else if (p.route === 'reconcile') out.push({ key: 'reconcile', label: 'Reconciling', tone: 'purple', icon: 'refresh' });
            if (x.mergeable_state === 'dirty') out.push({ key: 'conflicts', label: 'Conflicts', tone: 'yellow', icon: 'git-merge' });
            if (x.mergeable_state === 'blocked') out.push({ key: 'merge_blocked', label: 'Merge blocked', tone: 'yellow', icon: 'lock' });
            if (x.ci_state === 'pending') out.push({ key: 'checks_running', label: 'Checks running', tone: 'yellow', icon: 'loader' });
            if (x.queue_position) out.push({ key: 'queued', label: `Queued #${x.queue_position}`, tone: 'azure', icon: 'clock' });
            else if (x.auto_merge) out.push({ key: 'auto_merge', label: 'Auto-merge armed', tone: 'azure', icon: 'clock' });
            // Stalled ranks above "ready" on purpose: a green PR nobody has
            // touched in days is the green-but-stuck case the dock already
            // raises its red pill for.
            if (x.stalled) out.push({ key: 'stalled', label: `Stalled ${app._fleetAge(x.updated_at)}`, tone: 'yellow', icon: 'zzz' });
            if (x.ci_state === 'success' && x.mergeable_state !== 'dirty' && x.mergeable_state !== 'blocked') {
                out.push({ key: 'ready', label: 'Ready to merge', tone: 'green', icon: 'check' });
            }
            if (x.draft) out.push({ key: 'draft', label: 'Draft', tone: 'secondary', icon: 'pencil' });
            if (x.ci_state !== 'success' && x.ci_state !== 'failure' && x.ci_state !== 'pending') {
                out.push({ key: 'no_checks', label: 'No checks', tone: 'secondary', icon: 'minus' });
            }
            if (!out.length) out.push({ key: 'open', label: 'Open', tone: 'secondary', icon: 'git-pull-request' });
            return out;
        },
        shortAge(seconds) {
            const age = Math.max(0, Number(seconds) || 0);
            if (age < 60) return `${Math.max(1, Math.round(age))}s`;
            if (age < 3600) return `${Math.round(age / 60)}m`;
            if (age < 86400) return `${Math.round(age / 3600)}h`;
            return `${Math.round(age / 86400)}d`;
        },
        runnerOutputAge(s, nowSeconds) {
            const faultAge = ((s.environment || {}).progress_fault || {}).output_age_s;
            if (faultAge != null && Number.isFinite(Number(faultAge))) {
                return Math.max(0, Number(faultAge));
            }
            const lastOutput = (s.environment || {}).last_output_at;
            if (lastOutput == null || !Number.isFinite(Number(lastOutput))) return null;
            const now = nowSeconds == null ? Date.now() / 1000 : Number(nowSeconds);
            return Math.max(0, now - Number(lastOutput));
        },
        // A runner may satisfy several conditions at once. Preserve every true
        // signal worst-first, while keeping workspace dirtiness secondary.
        // How a runner ended. `result.failure_class` is the server's own reason for
        // a failure — surface it rather than a generic red badge.
        runnerOutcome(s) {
            const status = String(s.status || 'unknown');
            const why = String((s.result || {}).failure_class
                || (s.result || {}).reason || '').trim();
            if (status === 'completed') {
                return { key: 'finished', label: 'Finished', tone: 'green', icon: 'check' };
            }
            if (status === 'killed' || status === 'stopped') {
                return { key: 'stopped', label: 'Stopped by you', tone: 'secondary', icon: 'hand-stop' };
            }
            if (status === 'expired') {
                // lifecycle_cleanup reaping an old session — housekeeping, not a
                // fault. It is 40% of all sessions on prod; flagging each one for
                // the operator would bury the handful that actually broke.
                return { key: 'expired', label: 'Expired', tone: 'secondary', icon: 'clock-off' };
            }
            if (status === 'failed') {
                return {
                    key: 'failed',
                    label: why ? `Failed · ${why}` : 'Failed',
                    tone: 'red',
                    icon: 'alert-triangle',
                };
            }
            if (status === 'exited') {
                // The normal end of a codex run: the supervisor observes the
                // process finish and terminalizes the session. Every live 'exited'
                // runner checked on prod had shipped a merged PR, so calling this
                // a failure told the operator four successes had broken.
                return { key: 'finished', label: 'Finished', tone: 'green', icon: 'check' };
            }
            // Never guess a clean finish we cannot prove.
            return { key: 'ended_unknown', label: 'Ended, cause unknown', tone: 'yellow', icon: 'help-circle' };
        },
        // Terminal states that mean nothing went wrong. Everything else still needs
        // the operator and must not be aged out on a timer.
        RUNNER_CLEAN_EXITS: ['finished', 'stopped', 'expired'],
        RUNNER_TERMINAL: ['finished', 'stopped', 'expired', 'failed', 'ended_unknown'],
        runnerConditions(app, s, attention) {
            const out = [];
            const age = this.runnerOutputAge(s);
            const status = String(s.status || 'unknown');
            // A runner that finished its work is not broken. The server already
            // distinguishes completed / killed / stopped / failed and carries a
            // failure_class for the failures; collapsing all of them into one red
            // "Exited" threw that away and counted clean finishes as attention.
            if (status !== 'running') {
                const ended = this.runnerOutcome(s);
                out.push(ended);
            }
            if (s.stale && status === 'running') {
                // No orderly exit — the heartbeat simply stopped. This one *is* broken.
                out.push({ key: 'lost_host', label: 'Lost host', tone: 'red', icon: 'server-off' });
            }
            if (attention) {
                out.push({ key: 'waiting_on_you', label: 'Waiting on you', tone: 'orange', icon: 'user-question' });
            }
            if ((s.environment || {}).progress_fault) {
                out.push({ key: 'silent', label: `Silent ${this.shortAge(age)}`, tone: 'yellow', icon: 'volume-off' });
            } else if (!s.task_id && status === 'running') {
                out.push({ key: 'idle', label: 'Idle', tone: 'secondary', icon: 'zzz' });
            } else if (age != null) {
                out.push({ key: 'working', label: `Working ${this.shortAge(age)}`, tone: 'green', icon: 'activity' });
            } else {
                const uptime = (s.environment || {}).uptime_seconds;
                out.push({
                    key: 'running_unknown',
                    label: uptime == null ? 'Running' : `Running up ${this.shortAge(uptime)}`,
                    tone: 'secondary',
                    icon: 'player-play',
                });
            }
            const dirty = String((s.last_snapshot || {}).status_porcelain || '')
                .split('\n').filter(Boolean).length;
            if (dirty) {
                out.push({
                    key: 'dirty',
                    label: `${dirty} uncommitted file${dirty === 1 ? '' : 's'}`,
                    tone: 'secondary',
                    icon: 'file-diff',
                });
            }
            return out;
        },
        // Should this finished runner still be on screen?
        //
        // A clean exit is a receipt: show it briefly, then drop it. A failure is
        // not — auto-hiding a 3am crash on a timer makes the dock lie by morning.
        // Failures instead clear when something supersedes them: the task picked
        // up a newer runner, so the dead one is no longer the current truth.
        // That keeps it hands-off — nothing to acknowledge, nothing sticks forever.
        //
        // NOTE: updated_at is NOT when the runner stopped. The host keeps
        // heartbeating a terminalized session — observed on prod: a runner that
        // started 274 minutes ago and exited long since still had updated_at 0.6
        // minutes old. Ageing off it meant finished runners never left the dock.
        //
        // `expires_at` (heartbeat_at + heartbeat_ttl_s) is the platform's own
        // answer to "is this lease still good", and `live` is its canonical
        // liveness predicate. A finished runner therefore lingers only until its
        // lease lapses — a couple of minutes after the host stops touching it —
        // and then it is gone without us inventing a timestamp.
        runnerRetention(s, conditionKey, allRunners, now) {
            if (!this.RUNNER_TERMINAL.includes(conditionKey)) return 'live';
            const nowS = now / 1000;
            const expiresAt = Number(s.expires_at || 0);
            const leaseLapsed = s.live === false && expiresAt > 0 && nowS > expiresAt;
            if (this.RUNNER_CLEAN_EXITS.includes(conditionKey)) {
                return leaseLapsed ? 'drop' : 'keep';
            }
            // A failure is superseded once a newer runner exists for the same task.
            const mine = String(s.task_id || '');
            const endedAt = Number(s.updated_at || 0);
            if (mine) {
                const superseded = (allRunners || []).some((o) =>
                    o !== s
                    && String(o.task_id || '') === mine
                    && Number(o.updated_at || 0) > endedAt);
                if (superseded) return 'drop';
            }
            return 'keep';
        },
        runnerRank(key) {
            // Failures first, then things asking for you, then live work. Clean
            // exits sort last — they are a receipt, not a call to action.
            const order = ['failed', 'lost_host', 'ended_unknown',
                           'waiting_on_you', 'silent', 'idle', 'working',
                           'running_unknown', 'finished', 'stopped', 'expired'];
            const at = order.indexOf(key);
            return at === -1 ? order.length : at;
        },
        // UI-66: one batched coverage read for every board task on the PR tab.
        // Advisory by contract: a failed read renders the dock without pills,
        // never blank.
        async loadCoverage(app, prs) {
            const taskIds = [...new Set((prs || []).flatMap(
                (x) => (x.tasks || []).map((t) => t.task_id).filter(Boolean)))];
            if (!taskIds.length) return {};
            try {
                const p = `project=${encodeURIComponent(window.PM_PROJECT || 'maxwell')}`;
                const res = await app._fetchTimeout(
                    `api/autopilot/coverage?task_ids=${encodeURIComponent(taskIds.join(','))}&${p}`,
                    { cache: 'no-store' });
                if (res.ok) return ((await res.json()).coverage) || {};
            } catch (e) { /* coverage is advisory */ }
            return {};
        },
        // UI-71: join Communication-plane attention to Connect-plane runners
        // only at the browser edge. Advisory by contract: a failed read leaves
        // the runner ladder intact without Waiting-on-you.
        async loadRunnerAttention(app) {
            try {
                const p = `project=${encodeURIComponent(window.PM_PROJECT || 'maxwell')}`;
                const res = await app._fetchTimeout(
                    `/api/attention/requests?${p}&limit=200`,
                    { cache: 'no-store' });
                if (!res.ok) return {};
                const items = ((await res.json()).items) || [];
                return Object.fromEntries(items.filter((item) => item.runner_session_id).map((item) => [
                    item.runner_session_id,
                    {
                        request_id: item.request_id,
                        version: item.version,
                        prompt: item.prompt,
                        choices: item.choices,
                        recommended_default: item.recommended_default,
                    },
                ]));
            } catch (e) { /* attention is advisory */ }
            return {};
        },
        // One compact autopilot control per PR row, from the batched coverage
        // read. States are the resolver's honest liveness vocabulary; the click
        // action follows the coverage kind so a deliverable-covered task can
        // never start a duplicate task scope (the double-drive guard).
        autopilotHtml(app, x) {
            const ids = (x.tasks || []).map((t) => t.task_id).filter(Boolean);
            if (!ids.length) return '';
            const taskId = String(ids[0]).toUpperCase();
            const cov = (app._dockAutopilot || {})[taskId];
            if (!cov) return '';
            const states = {
                live: ['Driving', 'green', 'route',
                       cov.coverage === 'deliverable'
                           ? `Driven by ${cov.deliverable_id}'s autopilot — click to pause`
                           : 'Task-scoped autopilot running — click to pause', 'pause'],
                armed: ['Armed', 'azure', 'clock',
                        'Scope started; waiting for a coordinator host to pick it up — click to pause', 'pause'],
                paused: ['Paused', 'yellow', 'player-pause',
                         'Click to resume', 'resume'],
                stale: ['Stale', 'orange', 'alert-triangle',
                        'Scope holder is dead (deploy restart?) — click to re-arm', 'start'],
                none: ['Arm', 'secondary', 'player-play',
                       `Start a task-scoped autopilot for ${taskId}`, 'start'],
            };
            const [label, tone, icon, title, action] = states[cov.liveness] || states.none;
            return `<button type="button" class="btn btn-sm btn-ghost-secondary dock-tab p-0 px-1 d-inline-flex align-items-center gap-1"
                style="font-size:11px;flex:none;" data-ap-task="${app.esc(taskId)}" data-ap-action="${app.esc(action)}"
                title="${app.esc(title)}">
                <span style="width:6px;height:6px;border-radius:50%;background:var(--tblr-${tone}, var(--tblr-secondary));"></span>
                <i class="ti ti-${icon}" style="font-size:11px;"></i>${app.esc(label)}</button>`;
        },
        async autopilotAction(app, taskId, action) {
            const cov = (app._dockAutopilot || {})[String(taskId || '').toUpperCase()] || {};
            const p = `project=${encodeURIComponent(window.PM_PROJECT || 'maxwell')}`;
            // A deliverable-covered task is controlled through ITS scope; only
            // an uncovered (or task-scoped) task addresses /api/tasks/{id}/autopilot.
            const url = (cov.coverage === 'deliverable' && cov.deliverable_id)
                ? `api/deliverables/${encodeURIComponent(cov.deliverable_id)}/autopilot?${p}`
                : `api/tasks/${encodeURIComponent(taskId)}/autopilot?${p}`;
            try {
                const res = await app._fetchTimeout(url, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action }),
                });
                const body = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(body.message || body.error || `HTTP ${res.status}`);
            } catch (error) {
                window.alert(`Autopilot ${action} failed: ${error.message || error}`);
            }
            await app._loadFleetDock(true);
        },
    };

    window.SwitchboardFleetDock = FleetDock;
})();
