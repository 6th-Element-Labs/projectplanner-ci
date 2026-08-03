/* UI-82: discussion-first Scope. Conversation and kickoff approvals remain
 * separate control surfaces: chat may propose; only explicit buttons mutate
 * the server-backed advisory kickoff record. */
(function () {
    'use strict';

    const SESSION = 'scope';
    const SECTIONS = [
        { id: 'vision', label: 'Outcome', title: 'Outcome and point of view',
          kicker: 'User · why now · non-goals · measurable proof',
          body: 'State the change this project must create, who experiences it, why it matters now, and what is deliberately outside the boundary.' },
        { id: 'prd', label: 'Requirements', title: 'Requirements and proof',
          kicker: 'Journeys · acceptance · open decisions',
          body: 'Make each important journey explicit and each requirement testable. Carry unanswered questions as named decisions instead of silently guessing.' },
        { id: 'arch', label: 'Architecture', title: 'Architecture and constraints',
          kicker: 'Reuse map · trust boundaries · data · integrations',
          body: 'Name what already exists, what can be extended, and what is genuinely new. Record technical constraints and trust boundaries that delivery agents must respect.' },
        { id: 'rules', label: 'Guidelines', title: 'Technical guidelines and working agreement',
          kicker: 'Ownership · validation · definition of done',
          body: 'Capture the operating rules every contributor inherits: ownership boundaries, validation expectations, prohibited shortcuts, and how completion is proven.' },
        { id: 'scope', label: 'Breakdown', title: 'Scope breakdown',
          kicker: 'Deliverables · dependencies · milestones · exclusions',
          body: 'Translate the agreement into a bounded delivery graph. The graph remains in Plan; this approval records that its boundaries match the agreed scope.' },
    ];

    let kickoff = null;
    let section = 'vision';
    let runId = null;

    function el(id) { return document.getElementById(id); }
    function project() { return (window.PM_PROJECT || '').trim() || 'maxwell'; }
    function qs(extra) {
        const params = new URLSearchParams(extra || {});
        params.set('project', project());
        return params.toString();
    }
    function esc(value) {
        return String(value == null ? '' : value).replace(/[&<>"']/g, (char) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[char]));
    }
    function rich(value) {
        return esc(value).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
            .replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\n/g, '<br>');
    }
    function gate(id) {
        return ((kickoff && kickoff.gates) || []).find((item) => item.gate === id)
            || { gate: id, s: 'wait', version: 0 };
    }
    function flash(message, kind) {
        const node = el('scope-flash');
        if (!node) return;
        node.className = 'alert py-2 px-3 mt-3 alert-' + (kind || 'info');
        node.textContent = message;
        clearTimeout(flash.timer);
        flash.timer = setTimeout(() => node.classList.add('d-none'), 4500);
    }
    function version() {
        return Math.max(0, ...((kickoff && kickoff.gates) || []).map((item) => Number(item.version) || 0));
    }
    function openCount() {
        return ((kickoff && kickoff.gates) || SECTIONS).filter((item) => item.s !== 'ok').length;
    }

    async function loadKickoff() {
        try {
            const response = await fetch('api/kickoff?' + qs(), { cache: 'no-store' });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.detail || ('HTTP ' + response.status));
            kickoff = data;
        } catch (error) {
            kickoff = { gates: SECTIONS.map((item, index) => ({ gate: item.id, s: index ? 'wait' : 'now', version: 0 })), frontier: 'vision', build_authorized: false };
            flash('Could not load the kickoff record: ' + error.message, 'danger');
        }
        renderArtifact();
    }

    async function mutateGate(action, id) {
        try {
            const response = await fetch('api/kickoff/' + encodeURIComponent(id) + '/' + action + '?' + qs(), {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.detail || ('HTTP ' + response.status));
            kickoff = data;
            renderArtifact();
            flash((action === 'approve' ? 'Approved ' : 'Reopened ') + SECTIONS.find((item) => item.id === id).label + ' on the project.', action === 'approve' ? 'success' : 'warning');
        } catch (error) {
            flash('Kickoff update failed: ' + error.message, 'danger');
        }
    }

    function stateMark(item) {
        const data = gate(item.id);
        if (data.s === 'ok') return '<span class="text-success scope-sec-state"><i class="ti ti-check"></i></span>';
        if (data.s === 'stale') return '<span class="text-warning scope-sec-state"><i class="ti ti-alert-triangle"></i></span>';
        if (kickoff && kickoff.frontier === item.id) return '<span class="text-primary scope-sec-state"><i class="ti ti-circle-dot"></i></span>';
        return '<span class="text-secondary scope-sec-state"><i class="ti ti-lock"></i></span>';
    }

    function renderArtifact() {
        if (!kickoff) return;
        const ver = version();
        const open = openCount();
        el('scope-project-label').textContent = project();
        el('scope-artifact-version').textContent = 'Scope v' + ver;
        el('scope-drawer-title').textContent = 'Scope v' + ver;
        el('scope-artifact-open-count').textContent = open ? open + ' open' : 'ready for delivery';
        el('scope-verdict').classList.toggle('ok', !!kickoff.build_authorized);
        el('scope-verdict-title').textContent = kickoff.build_authorized ? 'Approved kickoff record' : 'Shaping in progress';
        el('scope-verdict-sub').textContent = kickoff.build_authorized
            ? 'All five sections are approved. Revisions remain explicit and attributed.'
            : open + ' section' + (open === 1 ? '' : 's') + ' open · next: ' + (SECTIONS.find((item) => item.id === kickoff.frontier) || {}).label;
        el('scope-switch').innerHTML = SECTIONS.map((item) => '<button type="button" class="' + (item.id === section ? 'on' : '') + '" data-scope-section="' + item.id + '">' + esc(item.label) + stateMark(item) + '</button>').join('');
        el('scope-switch').querySelectorAll('[data-scope-section]').forEach((button) => button.addEventListener('click', () => {
            section = button.dataset.scopeSection;
            renderArtifact();
        }));
        const item = SECTIONS.find((candidate) => candidate.id === section);
        const current = gate(section);
        let approval;
        if (current.s === 'ok') {
            approval = '<div class="tk-scope-approval"><span class="text-success"><i class="ti ti-circle-check me-1"></i>Approved v' + current.version + (current.approved_by ? ' by ' + esc(current.approved_by) : '') + '</span><button class="btn btn-sm btn-outline-secondary" type="button" data-scope-revise="' + section + '">Revise</button></div>';
        } else if (kickoff.frontier === section || current.s === 'stale') {
            approval = '<div class="tk-scope-approval"><span class="text-secondary">Review the discussion and sources before recording approval.</span><button class="btn btn-sm btn-primary" type="button" data-scope-approve="' + section + '">' + (current.s === 'stale' ? 'Re-approve' : 'Approve section') + '</button></div>';
        } else {
            approval = '<div class="tk-scope-approval text-secondary"><i class="ti ti-lock"></i><span>Complete the preceding section first.</span></div>';
        }
        el('scope-artifact-body').innerHTML = '<div class="tk-eyebrow mb-2">' + esc(item.kicker) + '</div><h4>' + esc(item.title) + '</h4><p>' + esc(item.body) + '</p><h4>Source of truth</h4><p>This project’s accepted documents, recorded decisions, and live board are authoritative. Conversation may propose wording; approval changes only this advisory kickoff record.</p>' + approval;
        const approve = el('scope-artifact-body').querySelector('[data-scope-approve]');
        const revise = el('scope-artifact-body').querySelector('[data-scope-revise]');
        if (approve) approve.addEventListener('click', () => mutateGate('approve', approve.dataset.scopeApprove));
        if (revise) revise.addEventListener('click', () => mutateGate('revise', revise.dataset.scopeRevise));
    }

    function bubble(role, content, sources) {
        const src = (sources || []).length ? '<div class="tk-scope-source"><i class="ti ti-books me-1"></i>' + sources.map(esc).join(' · ') + '</div>' : '';
        return '<div class="tk-scope-bubble ' + role + '"><div>' + (role === 'user' ? esc(content) : rich(content)) + src + '</div></div>';
    }
    function renderMessages(messages) {
        const log = el('scope-chat-log');
        if (!messages.length) return;
        const empty = el('scope-chat-empty');
        if (empty) empty.remove();
        log.innerHTML = messages.map((message) => bubble(message.role === 'user' ? 'user' : (message.payload && message.payload.error ? 'error' : 'assistant'), message.content || '', (message.payload && message.payload.sources) || [])).join('');
        log.scrollTop = log.scrollHeight;
    }
    async function loadChat() {
        try {
            const response = await fetch('api/chat/history?' + qs({ session: SESSION }), { cache: 'no-store' });
            const data = await response.json().catch(() => ({}));
            if (response.ok) renderMessages(data.messages || []);
        } catch (_) { /* The empty state remains useful offline. */ }
    }
    function thinking(show) {
        const old = el('scope-thinking');
        if (old) old.remove();
        if (!show) return;
        el('scope-chat-log').insertAdjacentHTML('beforeend', '<div id="scope-thinking" class="tk-scope-bubble assistant"><div class="tk-scope-thinking"><i class="ti ti-loader-2"></i>Taikun is shaping from project context…</div></div>');
    }
    async function pollRun(id) {
        runId = id;
        for (let attempt = 0; attempt < 600 && runId === id; attempt++) {
            const response = await fetch('api/chat/runs/' + encodeURIComponent(id) + '?' + qs({ session: SESSION }));
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.detail || ('HTTP ' + response.status));
            if (data.status === 'completed') {
                thinking(false);
                el('scope-chat-log').insertAdjacentHTML('beforeend', bubble('assistant', data.answer || 'Scope response completed.', data.sources || []));
                el('scope-chat-log').scrollTop = el('scope-chat-log').scrollHeight;
                runId = null;
                return;
            }
            if (data.status === 'failed' || data.status === 'cancelled') throw new Error(data.error || 'Scope discussion failed.');
            await new Promise((resolve) => setTimeout(resolve, 1000));
        }
    }
    async function send(messageOverride) {
        const input = el('scope-chat-input');
        const message = String(messageOverride || input.value || '').trim();
        if (!message || runId) return;
        if (!messageOverride) input.value = '';
        const empty = el('scope-chat-empty');
        if (empty) empty.remove();
        el('scope-chat-log').insertAdjacentHTML('beforeend', bubble('user', message));
        thinking(true);
        try {
            const response = await fetch('api/chat?' + qs(), {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: message, session: SESSION }),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : ('HTTP ' + response.status));
            if (data.run_id) await pollRun(data.run_id);
        } catch (error) {
            thinking(false);
            el('scope-chat-log').insertAdjacentHTML('beforeend', bubble('error', error.message));
            runId = null;
        }
    }

    function setDrawer(open, expanded) {
        const page = el('tab-scope');
        const drawer = el('scope-artifact-drawer');
        const scrim = el('scope-artifact-scrim');
        drawer.classList.toggle('show', open);
        drawer.classList.toggle('expanded', !!expanded);
        drawer.setAttribute('aria-hidden', String(!open));
        scrim.classList.toggle('show', open);
        page.classList.toggle('drawer-open', open);
        page.classList.toggle('drawer-expanded', open && !!expanded);
    }
    function init() {
        if (!init.wired) {
            init.wired = true;
            el('scope-artifact-open').addEventListener('click', () => setDrawer(true, false));
            el('scope-review').addEventListener('click', () => setDrawer(true, true));
            el('scope-drawer-close').addEventListener('click', () => setDrawer(false, false));
            el('scope-artifact-scrim').addEventListener('click', () => setDrawer(false, false));
            el('scope-drawer-expand').addEventListener('click', () => {
                const expanded = !el('scope-artifact-drawer').classList.contains('expanded');
                setDrawer(true, expanded);
            });
            el('scope-refresh').addEventListener('click', (event) => { event.preventDefault(); loadKickoff(); });
            el('scope-chat-send').addEventListener('click', () => send());
            el('scope-chat-input').addEventListener('keydown', (event) => {
                if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) { event.preventDefault(); send(); }
            });
            el('scope-chat-clear').addEventListener('click', async () => {
                if (!window.confirm('Delete this Scope conversation? The approved Scope artifact and its history will not be changed.')) return;
                try {
                    const response = await fetch('api/chat?' + qs({ session: SESSION }), { method: 'DELETE' });
                    const data = await response.json().catch(() => ({}));
                    if (!response.ok) throw new Error(data.detail || ('HTTP ' + response.status));
                    runId = null;
                    el('scope-chat-log').innerHTML = '<div class="tk-scope-empty" id="scope-chat-empty"><h3>Start a new Scope discussion</h3><p>Describe the outcome or paste the next piece of context below.</p></div>';
                    flash('Scope conversation deleted. The approved Scope artifact was not changed.', 'success');
                } catch (error) {
                    flash('Could not delete the Scope conversation: ' + error.message, 'danger');
                }
            });
            el('scope-back').addEventListener('click', () => {
                if (window.TAIKUN_showTab) window.TAIKUN_showTab('#tab-exec');
            });
            document.querySelectorAll('[data-scope-prompt]').forEach((button) => button.addEventListener('click', () => send(button.dataset.scopePrompt)));
            document.addEventListener('keydown', (event) => { if (event.key === 'Escape') setDrawer(false, false); });
        }
        loadKickoff();
        loadChat();
    }

    const tab = el('toptab-scope');
    if (tab) tab.addEventListener('shown.bs.tab', init);
    if (location.hash === '#tab-scope') setTimeout(init, 0);
})();
