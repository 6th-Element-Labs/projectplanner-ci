/* UI-66: Fleet dock presentation — PR condition ranking + the autopilot pill.
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
                live: ['Autopilot', 'green', 'route',
                       cov.coverage === 'deliverable'
                           ? `Driven by ${cov.deliverable_id}'s autopilot — click to pause`
                           : 'Task-scoped autopilot running — click to pause', 'pause'],
                armed: ['Autopilot armed', 'azure', 'clock',
                        'Scope started; waiting for a coordinator host to pick it up — click to pause', 'pause'],
                paused: ['Autopilot paused', 'yellow', 'player-pause',
                         'Click to resume', 'resume'],
                stale: ['Autopilot stale', 'orange', 'alert-triangle',
                        'Scope holder is dead (deploy restart?) — click to re-arm', 'start'],
                none: ['Arm autopilot', 'secondary', 'player-play',
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
