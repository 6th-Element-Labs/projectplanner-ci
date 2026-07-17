/* UI-18: the unified Settings shell — one canonical surface for every setting.
 *
 * The structure is ported from the ActionEngine Taikun/Tabler settings page: a
 * left-hand category card (card > list-group-flush) beside a focused right-hand
 * content card with a header / body / footer-action rhythm.
 *
 * Two deliberate departures from that source, both required here:
 *
 *   1. Routing is ours, not `data-bs-toggle="list"`. Bootstrap's Tab plugin
 *      preventDefault()s the click and never writes location.hash, so the
 *      ActionEngine page cannot deep-link — settings.html#api always lands on
 *      Account. We need `#tab-settings/<section>` to survive a reload, and we
 *      want per-section lazy fetches rather than populating twelve panes up
 *      front, so selection is driven here and the panel renders on demand.
 *   2. The left nav collapses on mobile instead of stacking above the content.
 *      Twelve categories stacked would push the panel off-screen.
 *
 * Sections are gated individually: the Settings entry is open to every signed-in
 * user, personal sections are always available, and a section the caller cannot
 * use still renders — as a named lock naming the missing scope — rather than
 * vanishing from the nav. The server remains the enforcement point; this is
 * legibility, not security.
 */
(function () {
    'use strict';

    // The information architecture. `scope` is the coarse gate for a section as a
    // whole; individual actions inside re-check server-side.
    const SECTIONS = [
        {
            id: 'my', group: 'My settings', items: [
                { id: 'profile', label: 'Profile & security', icon: 'ti-user-shield', scope: null },
                { id: 'ai-accounts', label: 'Personal AI accounts', icon: 'ti-plug-connected', scope: null },
                { id: 'appearance', label: 'Appearance', icon: 'ti-palette', scope: null },
            ],
        },
        {
            id: 'project', group: 'Project settings', items: [
                // members: every backing route is write:system (access.py members/
                // project_role/revoke/invite), so write:projects here promised access the
                // server refuses — the caller saw an unlocked section and a 403 on every
                // action. The server is authoritative.
                { id: 'members', label: 'Members & access', icon: 'ti-users', scope: 'write:system' },
                // comms is genuinely read-for-anyone-who-can-read-the-project (projects.py
                // "Readable to anyone who can read the project; edits below are admin-gated"),
                // and the section already disables its own edit path from the server's
                // can_edit probe — so a project editor legitimately sees this section.
                { id: 'comms', label: 'Communications', icon: 'ti-mail-cog', scope: 'write:projects' },
                { id: 'github', label: 'GitHub & repositories', icon: 'ti-brand-github', scope: 'write:system' },
                { id: 'tokens', label: 'Access tokens', icon: 'ti-key', scope: 'write:system' },
            ],
        },
        {
            id: 'ops', group: 'Operations', items: [
                { id: 'fleet', label: 'Fleet & runners', icon: 'ti-server-bolt', scope: 'write:system' },
                { id: 'capacity', label: 'Capacity & box pressure', icon: 'ti-gauge', scope: 'write:system' },
                { id: 'narration', label: 'Narration', icon: 'ti-broadcast', scope: 'write:system' },
                { id: 'provenance', label: 'Reconcile & provenance', icon: 'ti-refresh-dot', scope: 'write:system' },
                { id: 'advanced', label: 'Advanced', icon: 'ti-adjustments', scope: 'write:system' },
            ],
        },
    ];

    // UI-19: the three named runtimes. Personal-subscription state/actions are driven
    // entirely by the CO-15 matrix + CO-6 vault; nothing here is a local allowlist.
    const AI_ACCOUNT_PROVIDERS = [
        { id: 'openai-codex', label: 'Codex / ChatGPT', icon: 'ti-brand-openai' },
        { id: 'anthropic-claude', label: 'Claude Code', icon: 'ti-sparkles' },
        { id: 'cursor', label: 'Cursor', icon: 'ti-terminal-2' },
    ];

    // UI-21: the "API connections" group — user-owned, explicitly metered BYOK
    // credentials, distinct from the personal subscriptions above. OpenAI is the
    // MVP row; Anthropic/Cursor stay UI-disabled until their adapters qualify
    // (ADAPTER-20/21), even though the CO-15 matrix already lists them supported.
    const API_CONNECTION_PROVIDERS = [
        { id: 'openai-codex', label: 'OpenAI API', icon: 'ti-brand-openai', enabled: true },
        { id: 'anthropic-claude', label: 'Anthropic API', icon: 'ti-sparkles', enabled: false, gate: 'ADAPTER-20' },
        { id: 'cursor', label: 'Cursor Agent API', icon: 'ti-terminal-2', enabled: false, gate: 'ADAPTER-21' },
    ];

    const DEFAULT_SECTION = 'profile';
    const SCOPE_LABEL = {
        'write:projects': 'Project editor access (write:projects)',
        'write:system': 'System administrator access (write:system)',
    };

    const methods = {

        /* ---- shell ------------------------------------------------------- */

        _settingsSections() { return SECTIONS; },

        _settingsFind(id) {
            for (const group of SECTIONS) {
                for (const item of group.items) if (item.id === id) return item;
            }
            return null;
        },

        // Personal sections are always available. Project/system sections reuse the
        // flags loadPrincipal() already derives, so the nav can never disagree with
        // the rest of the app about what the caller can do.
        _settingsCan(section) {
            const need = section && section.scope;
            if (!need) return true;
            if (need === 'write:projects') return !!this.canWriteProjects;
            if (need === 'write:system') return !!this.isAdmin;
            return ((this.principal && this.principal.effective_scopes) || []).includes(need);
        },

        _settingsHashSection() {
            const m = /^#tab-settings\/([a-z0-9-]+)$/i.exec(window.location.hash || '');
            return m ? m[1].toLowerCase() : '';
        },

        _settingsCurrentId() {
            const fromHash = this._settingsHashSection();
            if (fromHash && this._settingsFind(fromHash)) return fromHash;
            if (this._settingsSectionId && this._settingsFind(this._settingsSectionId)) return this._settingsSectionId;
            return DEFAULT_SECTION;
        },

        // Keep the section in the URL so a Settings link is copyable and survives a
        // reload. replaceState, not pushState: moving between sections is not a
        // navigation the back button should have to walk through. The project half of
        // the context stays in ?project=, where the rest of the app already reads it —
        // so `?project=vulkan#tab-settings/members` restores both halves.
        _settingsWriteHash(id) {
            try {
                window.history.replaceState(null, '', window.location.pathname + window.location.search + '#tab-settings/' + id);
            } catch (e) { /* history unavailable in some embedded contexts; the panel still renders */ }
        },

        _settingsNavHtml(activeId) {
            return SECTIONS.map((group) => {
                const items = group.items.map((item) => {
                    const allowed = this._settingsCan(item);
                    const active = item.id === activeId;
                    const lock = allowed ? '' : '<i class="ti ti-lock ms-auto text-secondary" aria-hidden="true"></i>';
                    const title = allowed ? '' : ` title="${this.esc(SCOPE_LABEL[item.scope] || item.scope)} is required"`;
                    return `<a class="list-group-item list-group-item-action d-flex align-items-center${active ? ' active' : ''}" href="#tab-settings/${item.id}" id="settings-tab-${item.id}" role="tab" data-settings-section="${item.id}" data-settings-locked="${allowed ? '0' : '1'}" aria-selected="${active ? 'true' : 'false'}" aria-controls="settings-panel"${title}><i class="ti ${item.icon} me-2" aria-hidden="true"></i><span>${this.esc(item.label)}</span>${lock}</a>`;
                }).join('');
                return `<div class="list-group-item py-1 px-3 text-secondary text-uppercase fw-bold settings-nav-group" role="presentation">${this.esc(group.group)}</div>${items}`;
            }).join('');
        },

        async renderSettings() {
            const page = document.getElementById('settings-page');
            if (!page) return;
            // loadPrincipal() is kicked off unawaited during init, so without this a
            // deep-linked Settings tab can render before effective_scopes land and show
            // every gated section as spuriously locked.
            if (this._principalReady) { try { await this._principalReady; } catch (e) { /* gating falls back to locked */ } }
            const id = this._settingsCurrentId();
            this._settingsSectionId = id;
            const nav = document.getElementById('settings-nav');
            if (nav) nav.innerHTML = this._settingsNavHtml(id);
            const section = this._settingsFind(id);
            const current = document.getElementById('settings-nav-current');
            if (current) current.textContent = (section && section.label) || 'Settings menu';
            await this._settingsRenderPanel(id);
        },

        // Select a section: route, re-render, and on mobile close the nav so the panel
        // the operator asked for is what they land on.
        async _settingsSelect(id) {
            if (!this._settingsFind(id)) return;
            // UI-20: wipe shown-once token on panel swap (re-anchored off hidden.bs.modal).
            this._clearApiKeySecret();
            this._settingsSectionId = id;
            this._settingsWriteHash(id);
            await this.renderSettings();
            const panel = document.getElementById('settings-panel');
            if (panel) panel.focus({ preventScroll: true });
            const collapse = document.getElementById('settings-nav-collapse');
            if (collapse && window.bootstrap && collapse.classList.contains('show')) {
                window.bootstrap.Collapse.getOrCreateInstance(collapse).hide();
            }
        },

        // A pasted or hand-edited #tab-settings/<section> should land correctly. Our own
        // replaceState writes do not fire hashchange, so this only sees real external
        // navigation.
        _settingsOnHashChange() {
            const page = document.getElementById('settings-page');
            if (!page || !page.closest('.tab-pane.active')) return;
            const id = this._settingsHashSection();
            if (id && id !== this._settingsSectionId) this.renderSettings();
        },

        async _settingsRenderPanel(id) {
            const host = document.getElementById('settings-panel');
            const section = this._settingsFind(id);
            if (!host || !section) return;
            host.setAttribute('aria-labelledby', `settings-tab-${id}`);
            if (!this._settingsCan(section)) { host.innerHTML = this._settingsLockedCard(section); return; }
            host.innerHTML = `<div class="card"><div class="card-body text-secondary small">Loading ${this.esc(section.label)}…</div></div>`;
            let html;
            try {
                html = await this._settingsSectionHtml(section);
            } catch (e) {
                html = this._settingsErrCard(section.label, (e && e.message) ? e.message : String(e));
            }
            // Another section may have been selected while this one was fetching.
            if (this._settingsSectionId === id) host.innerHTML = html;
        },

        _settingsSectionHtml(section) {
            switch (section.id) {
                case 'profile': return this._settingsProfileSection();
                case 'ai-accounts': return this._settingsAiAccountsSection();
                case 'appearance': return this._settingsAppearanceSection();
                case 'members': return this._settingsMembersSection();
                case 'comms': return this._settingsCommsSection();
                case 'github': return this._settingsGithubSection();
                case 'tokens': return this._settingsTokensSection();
                case 'fleet': return this._settingsFleetSection();
                case 'capacity': return this._settingsCapacitySection();
                case 'narration': return this._settingsNarrationSection();
                case 'provenance': return this._settingsProvenanceSection();
                case 'advanced': return this._settingsAdvancedSection();
                default: return Promise.resolve(this._settingsErrCard(section.label, 'No renderer is registered for this section.'));
            }
        },

        /* ---- shared card chrome ------------------------------------------ */

        _settingsCard(opts) {
            const actions = opts.actions ? `<div class="card-actions btn-list">${opts.actions}</div>` : '';
            const subtitle = opts.subtitle ? `<div class="text-secondary small">${this.esc(opts.subtitle)}</div>` : '';
            const footer = opts.footer ? `<div class="card-footer d-flex align-items-center">${opts.footer}</div>` : '';
            const icon = opts.icon ? `<i class="ti ${opts.icon} me-2" aria-hidden="true"></i>` : '';
            return `<div class="card mb-3"${opts.id ? ` id="${opts.id}"` : ''}>
                <div class="card-header"><div><h3 class="card-title">${icon}${this.esc(opts.title)}</h3>${subtitle}</div>${actions}</div>
                <div class="card-body">${opts.body}</div>${footer}</div>`;
        },

        _settingsLockedCard(section) {
            // Tabler's .alert is a flex row, so keep the copy in one child or the
            // sentence lays itself out in columns.
            const need = SCOPE_LABEL[section.scope] || section.scope;
            return this._settingsCard({
                id: 'settings-locked', title: section.label, icon: section.icon,
                body: `<div class="alert alert-warning mb-0" role="note"><div>
                    <i class="ti ti-lock me-1" aria-hidden="true"></i><strong>${this.esc(need)}</strong> is required to view or change this section.
                    <div class="small mt-1">Ask a project administrator to grant it. Your personal settings stay available.</div>
                    </div></div>`,
            });
        },

        _settingsRows(rows) {
            return `<dl class="row small mb-0">${rows.map(([k, v]) =>
                `<dt class="col-5 col-lg-4 text-secondary fw-normal">${this.esc(k)}</dt><dd class="col-7 col-lg-8 mb-1">${v}</dd>`).join('')}</dl>`;
        },

        /* ---- My settings -------------------------------------------------- */

        async _settingsProfileSection() {
            // /api/auth/session answers 401 when there is no global session — which is the
            // normal state under PM_AUTH_MODE=dev-open. Distinguish that from a signed-in
            // user rather than reading {} and reporting "superadmin: no", which would be a
            // fabricated answer to a question we never got to ask.
            const session = await this._sfetch('api/auth/session');
            const signedIn = !session.error && !!session.authenticated;
            const user = (signedIn && session.user) || {};
            const p = this.principal || {};
            const scopes = (p.effective_scopes || []).map((s) => `<span class="badge bg-azure-lt me-1">${this.esc(s)}</span>`).join('') || '<span class="text-secondary">—</span>';
            const rows = [
                ['Signed in as', signedIn
                    ? `<strong>${this.esc(user.email || user.id || 'unknown')}</strong>`
                    : `<span class="text-secondary">No global session — acting as the <code>${this.esc(this.authMode || 'local')}</code> principal.</span>`],
                ['Principal', `<code>${this.esc(p.id || '—')}</code>`],
                ['Kind', this.esc(p.kind || '—')],
            ];
            if (signedIn) {
                rows.push(['Superadmin', user.is_superadmin
                    ? '<span class="badge bg-green-lt">yes</span>'
                    : '<span class="badge bg-secondary-lt">no</span>']);
            }
            rows.push(['Auth mode', `<code>${this.esc(this.authMode || '—')}</code>`]);
            rows.push([`Scopes on ${window.PM_PROJECT || 'this project'}`, scopes]);
            // UI-20 (6/6): the /account Change-password form folded in-place. profile is
            // scope:null, so this section renders with no global session (dev-open/bearer);
            // the signedIn branch is the gate (rule #5 — the /account login bounce has no
            // in-tab equivalent and does not need one). /account itself is now a thin
            // compatibility redirect into this section.
            const pwCard = signedIn
                ? `<div class="card mt-3" id="settings-profile-password"><div class="card-header"><h4 class="card-title mb-0"><i class="ti ti-key me-2" aria-hidden="true"></i>Change password</h4></div>
                    <div class="card-body">
                        <div class="mb-2"><label class="form-label small mb-1" for="profile-cur-pw">Current password</label>
                            <input type="password" id="profile-cur-pw" class="form-control form-control-sm" autocomplete="current-password"></div>
                        <div class="mb-2"><label class="form-label small mb-1" for="profile-new-pw">New password</label>
                            <input type="password" id="profile-new-pw" class="form-control form-control-sm" autocomplete="new-password" minlength="8">
                            <div class="form-hint">At least 8 characters.</div></div>
                        <div class="mb-2"><label class="form-label small mb-1" for="profile-confirm-pw">Confirm new password</label>
                            <input type="password" id="profile-confirm-pw" class="form-control form-control-sm" autocomplete="new-password" minlength="8"></div>
                        <div class="d-flex align-items-center">
                            <span id="profile-pw-flash" class="small text-secondary me-auto"></span>
                            <button type="button" id="profile-pw-submit" class="btn btn-primary btn-sm" data-set-action="profile-change-password"><i class="ti ti-key me-1" aria-hidden="true"></i>Update password</button>
                        </div>
                    </div></div>`
                : '';
            return this._settingsCard({
                id: 'settings-profile', title: 'Profile & security', icon: 'ti-user-shield',
                subtitle: 'Your identity, session, and effective permissions',
                body: this._settingsRows(rows) + pwCard,
                footer: signedIn
                    ? '<span class="text-secondary small"><i class="ti ti-shield-lock me-1" aria-hidden="true"></i>Changing your password signs out your other devices.</span>'
                    : '<span class="text-secondary small">Sign in with a global account to manage a password.</span>',
            });
        },

        // Mirrors the retired account.html flow: client-side guards, then POST
        // /api/auth/change-password. Kept as a raw fetch (not _sSend) so the server's 403
        // (wrong current) / 422 (too short / unchanged) detail surfaces verbatim, and so the
        // success line stays exactly "Password updated. Other devices have been signed out."
        async _settingsChangePassword() {
            const cur = document.getElementById('profile-cur-pw')?.value || '';
            const next = document.getElementById('profile-new-pw')?.value || '';
            const confirm = document.getElementById('profile-confirm-pw')?.value || '';
            const flash = document.getElementById('profile-pw-flash');
            const setFlash = (msg, cls) => { if (flash) { flash.textContent = msg; flash.className = `small me-auto ${cls}`; } };
            if (next.length < 8) { setFlash('New password must be at least 8 characters.', 'text-danger'); return; }
            if (next !== confirm) { setFlash('New password and confirmation do not match.', 'text-danger'); return; }
            if (next === cur) { setFlash('New password must be different from the current one.', 'text-danger'); return; }
            const btn = document.getElementById('profile-pw-submit'); if (btn) btn.disabled = true;
            setFlash('Updating…', 'text-secondary');
            try {
                const res = await fetch('api/auth/change-password', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ current_password: cur, new_password: next }),
                });
                if (!res.ok) {
                    const data = await res.json().catch(() => ({}));
                    setFlash(data.detail || 'Could not update password.', 'text-danger');
                    return;
                }
                ['profile-cur-pw', 'profile-new-pw', 'profile-confirm-pw'].forEach((id) => { const el = document.getElementById(id); if (el) el.value = ''; });
                setFlash('Password updated. Other devices have been signed out.', 'text-success');
            } catch (e) {
                setFlash('Network error — please try again.', 'text-danger');
            } finally {
                if (btn) btn.disabled = false;
            }
        },

        async _settingsAiAccountsSection() {
            const proj = window.PM_PROJECT || 'maxwell';
            const [data, policy, hostsData] = await Promise.all([
                this._sfetch(`api/projects/${encodeURIComponent(proj)}/provider-connections`),
                this._sfetch(`api/projects/${encodeURIComponent(proj)}/provider-auth-capabilities`),
                this._sfetch(`ixp/v1/agent_hosts?project=${encodeURIComponent(proj)}&include_stale=1`),
            ]);
            const conns = data.error ? [] : (data.connections || data.provider_connections || []);
            const caps = policy.error ? [] : (policy.capabilities || []);
            const hosts = hostsData.error ? [] : (hostsData.hosts || []);
            // Reused by _settingsAiAccountsConnect so a click doesn't re-fetch the
            // same "every host in this project" list the render just fetched.
            this._aiAccountsHostsCache = hostsData.error ? null : hosts;
            const me = (this.principal && this.principal.id) || '';
            // A failed fetch is NOT "you have no connections"/"no host is registered" —
            // conflating the two would misrepresent unknown state as a known-empty one.
            const fetchErrors = [
                data.error ? `Connections: ${data.error}` : '',
                hostsData.error ? `Agent hosts: ${hostsData.error}` : '',
            ].filter(Boolean);
            const fetchErrorBanner = fetchErrors.length
                ? `<div class="alert alert-danger py-2 px-3 small" role="alert"><i class="ti ti-alert-triangle me-1" aria-hidden="true"></i>Could not load current status — showing policy only until this is retried. ${fetchErrors.map((e) => this.esc(e)).join(' · ')}</div>`
                : '';
            const stateBadge = (row) => this._settingsCo15StateBadge(row.effective_state || row.state);
            const policyRows = caps.map((row) => `<tr>
                <td><div class="fw-semibold">${this.esc(row.provider || '—')}</div><div class="font-monospace small text-secondary">${this.esc(row.auth_mode || '')}</div></td>
                <td>${stateBadge(row)}<div class="small text-secondary mt-1">${this.esc(row.effective_disable_reason || row.disable_reason || 'Enabled by current policy')}</div></td>
                <td><div>${this.esc((row.host_class || '').replace(/_/g, ' '))}</div><div class="small text-secondary">${this.esc((row.portability || '').replace(/_/g, ' '))}</div></td>
                <td><code class="small">${this.esc(row.bootstrap_method || '—')}</code></td>
                <td>${row.litellm && row.litellm.eligible ? '<span class="badge bg-purple-lt">API/paygo</span>' : '<span class="text-secondary small">No personal-login brokerage</span>'}</td>
            </tr>`).join('');
            const policyBody = policy.error
                ? `<div class="alert alert-danger mb-0" role="alert"><strong>Provider execution is disabled.</strong> ${this.esc(policy.error)}</div>`
                : `<div class="table-responsive"><table class="table table-sm table-vcenter mb-0"><thead><tr><th>Provider / mode</th><th>Effective state</th><th>Host / portability</th><th>Bootstrap</th><th>LiteLLM</th></tr></thead><tbody>${policyRows || '<tr><td colspan="5" class="text-danger">No authoritative capability records; execution fails closed.</td></tr>'}</tbody></table></div>`;
            const broker = policy.personal_subscription_broker || {};
            const ctx = {
                conns, caps, hosts, me,
                connectionsError: data.error || '', hostsError: hostsData.error || '',
            };
            const providerCards = AI_ACCOUNT_PROVIDERS.map((p) => this._settingsAiAccountProviderCard(p, ctx)).join('');
            const apiConnectionCards = API_CONNECTION_PROVIDERS.map((p) => this._settingsApiConnectionCard(p, ctx)).join('');
            return this._settingsCard({
                id: 'settings-ai-accounts', title: 'AI connections', icon: 'ti-plug-connected',
                subtitle: 'Personal subscriptions and user-owned API connections for your agent runtimes',
                actions: `<span class="badge bg-secondary-lt">policy ${this.esc(policy.policy_version || 'unknown')}</span>`,
                body: `<div class="alert alert-info py-2 px-3 small" role="note"><i class="ti ti-shield-check me-1" aria-hidden="true"></i>This server-authoritative matrix also gates enrollment, scheduling, runtime launch, MCP, and CO-14 proof. Unknown, stale, unapproved, or host-mismatched modes fail closed.</div>
                    <div class="alert alert-secondary py-2 px-3 small" role="note"><i class="ti ti-shield-lock me-1" aria-hidden="true"></i>Enrollment is provider-native and owner-bound. This screen never accepts provider passwords, tokens, cookies, auth capsules, or browser profiles; it displays only redacted host-verified proof.</div>
                    ${fetchErrorBanner}
                    <h4 class="mb-1 mt-2">Personal subscriptions</h4>
                    <div class="small text-secondary mb-3">Your ChatGPT/Claude/Cursor sign-in, used natively on your own Agent Host. No metered API billing.</div>
                    ${providerCards}
                    <h4 class="mb-1 mt-4">API connections</h4>
                    <div class="alert alert-warning py-2 px-3 small" role="note"><i class="ti ti-currency-dollar me-1" aria-hidden="true"></i>User-owned API keys are <strong>explicitly metered</strong> — every run bills your own provider account against the budget you set. API is never auto-selected as a fallback for a personal subscription; switching billing mode is always an explicit, audited choice.</div>
                    ${apiConnectionCards}
                    <h4 class="mb-2 mt-4">Authentication policy</h4>${policyBody}
                    <div class="small text-secondary mt-2">LiteLLM personal-subscription broker: <strong>${broker.litellm === false ? 'disabled' : 'unknown'}</strong>. LiteLLM is eligible only for explicit API/paygo paths — never for personal subscriptions.</div>`,
            });
        },

        _settingsCo15StateBadge(state) {
            state = state || 'unavailable';
            const cls = state === 'supported' ? 'bg-green-lt'
                : state === 'supported_host_bound' ? 'bg-azure-lt'
                    : state === 'vendor_confirmation_required' ? 'bg-yellow-lt' : 'bg-red-lt';
            return `<span class="badge ${cls}">${this.esc(state.replace(/_/g, ' '))}</span>`;
        },

        // Best personal-subscription capability record for a provider: prefer a live
        // (non-unavailable) state over a stale/unsupported alternate mode (e.g. Cursor
        // has both a host-bound browser-login row and an unavailable portable row).
        _settingsAiAccountPersonalCapability(caps, providerId) {
            const rank = { supported: 0, supported_host_bound: 1, vendor_confirmation_required: 2, unavailable: 3 };
            const candidates = caps.filter((c) => c.provider === providerId && c.auth_mode !== 'api_key');
            candidates.sort((a, b) => (rank[a.effective_state] ?? 9) - (rank[b.effective_state] ?? 9));
            return candidates[0] || null;
        },

        _settingsAiAccountApiCapability(caps, providerId) {
            return caps.find((c) => c.provider === providerId && c.auth_mode === 'api_key') || null;
        },

        // Hosts this signed-in user owns for this provider (server-attested placement,
        // never a caller-supplied claim — see ownership.owner_user_ids in the payload).
        // list_agent_hosts()/_host_row() nests placement under capacity, not top-level.
        _settingsAiAccountHostPlacement(h) {
            return (h && h.capacity && h.capacity.placement) || {};
        },

        _settingsAiAccountCandidateHosts(hosts, me, providerId) {
            return hosts.filter((h) => {
                if (h.stale || !me) return false;
                const placement = this._settingsAiAccountHostPlacement(h);
                return (placement.owner_user_ids || []).includes(me)
                    && (placement.providers || []).includes(providerId);
            });
        },

        _settingsAiAccountsDeclareCommand(providerId, accountId) {
            const proj = window.PM_PROJECT || 'maxwell';
            const id = (accountId || 'you@example.com').replace(/'/g, "'\\''");
            return `python adapters/agent_host_enrollment.py declare-account \\\n`
                + `  --identity ~/.config/switchboard-agent-host/identity.json \\\n`
                + `  --config ~/.config/switchboard-agent-host/config.json \\\n`
                + `  --project ${proj} --provider ${providerId} --account-id '${id}'`;
        },

        // A connection is only "the current one" while it's actually usable — a
        // revoked/deleted row must fall through to the Connect UI, not permanently
        // occupy its provider's card (revoke/delete are otherwise a dead end).
        _settingsAiAccountLiveConnection(conns, providerId, connectionKindPredicate) {
            return conns.find((c) => c.provider === providerId
                && connectionKindPredicate(c.connection_kind || 'personal_subscription')
                && !['revoked', 'deleted'].includes(c.lifecycle_state));
        },

        _settingsAiAccountProviderCard(provider, ctx) {
            const personal = this._settingsAiAccountPersonalCapability(ctx.caps, provider.id);
            const apiCap = this._settingsAiAccountApiCapability(ctx.caps, provider.id);
            const connection = ctx.connectionsError ? null : this._settingsAiAccountLiveConnection(
                ctx.conns, provider.id, (kind) => kind === 'personal_subscription');
            const apiConnection = ctx.connectionsError ? null : this._settingsAiAccountLiveConnection(
                ctx.conns, provider.id, (kind) => kind !== 'personal_subscription');
            const state = (personal && personal.effective_state) || 'unavailable';

            let personalBody;
            if (ctx.connectionsError) {
                personalBody = `<div class="alert alert-secondary py-2 px-3 small mb-0" role="note">
                    <i class="ti ti-info-circle me-1" aria-hidden="true"></i>Connection status is unavailable right now — see the error above.</div>`;
            } else if (connection) {
                personalBody = this._settingsAiAccountConnectionRows(connection);
            } else if (state === 'vendor_confirmation_required' || state === 'unavailable') {
                const reason = (personal && (personal.effective_disable_reason || personal.disable_reason))
                    || 'This mode is not currently approved by server policy.';
                personalBody = `<div class="alert ${state === 'unavailable' ? 'alert-secondary' : 'alert-warning'} py-2 px-3 small mb-0" role="note">
                    <i class="ti ti-info-circle me-1" aria-hidden="true"></i>${this.esc(reason)}</div>`;
            } else {
                const candidates = ctx.hostsError ? [] : this._settingsAiAccountCandidateHosts(ctx.hosts, ctx.me, provider.id);
                const ready = candidates.filter((h) =>
                    (this._settingsAiAccountHostPlacement(h).account_affinity_ids || []).length > 0);
                const idFieldId = `aiacct-${provider.id}-account-id`;
                const cmdFieldId = `aiacct-${provider.id}-cmd`;
                const hostNote = ctx.hostsError
                    ? `<div class="small text-danger mb-2"><i class="ti ti-alert-triangle me-1" aria-hidden="true"></i>Could not check for a registered host — see the error above.</div>`
                    : candidates.length
                    ? `<div class="small text-secondary mb-2"><i class="ti ti-server-bolt me-1" aria-hidden="true"></i>${candidates.length} host(s) registered for you on this provider, ${ready.length} with a declared account.</div>`
                    : `<div class="small text-secondary mb-2"><i class="ti ti-server-off me-1" aria-hidden="true"></i>No Agent Host is registered for you on this provider yet. Install one first — see <code>docs/AGENT-HOST-ENROLLMENT.md</code>.</div>`;
                personalBody = `${hostNote}
                    <label class="form-label small mb-1" for="${idFieldId}">Account label <span class="text-secondary fw-normal">(e.g. your ChatGPT sign-in email — never a password or token)</span></label>
                    <input type="text" id="${idFieldId}" class="form-control form-control-sm mb-2" placeholder="you@example.com" autocomplete="off" spellcheck="false">
                    <label class="form-label small mb-1">Then, on your registered host, declare that account</label>
                    <div class="input-group input-group-sm mb-2">
                        <input type="text" id="${cmdFieldId}" class="form-control font-monospace" readonly value="${this.esc(this._settingsAiAccountsDeclareCommand(provider.id, ''))}">
                        <button class="btn btn-outline-secondary" type="button" data-set-action="ai-accounts-copy-cmd:${provider.id}" title="Copy command with your typed account label"><i class="ti ti-copy" aria-hidden="true"></i></button>
                    </div>
                    <div class="small text-secondary mb-2">This runs locally on your own machine — no network call, no secret leaves your host. Switchboard only ever learns the redacted result from that host's own next heartbeat. The copy button always uses whatever you've typed above.</div>
                    <button type="button" class="btn btn-primary btn-sm" data-set-action="ai-accounts-connect:${provider.id}"><i class="ti ti-plug-connected me-1" aria-hidden="true"></i>Connect</button>
                    <span id="aiacct-${provider.id}-flash" class="small text-secondary ms-2"></span>`;
            }

            const apiRow = apiCap ? `<div class="d-flex align-items-center mt-3 pt-3 border-top small">
                <span class="text-secondary me-2">Direct API key</span>${this._settingsCo15StateBadge(apiConnection ? 'supported' : (apiCap.effective_state || 'unavailable'))}
                <span class="text-secondary ms-2">${apiConnection ? 'configured, separately metered' : 'not yet self-service from Settings'}</span>
                </div>` : '';

            return `<div class="card mb-3" id="settings-ai-account-${provider.id}">
                <div class="card-header"><div><h4 class="card-title mb-0"><i class="ti ${provider.icon} me-2" aria-hidden="true"></i>${this.esc(provider.label)}</h4>
                    <div class="text-secondary small">Personal subscription</div></div>
                    <div class="card-actions">${this._settingsCo15StateBadge(state)}</div></div>
                <div class="card-body">${personalBody}${apiRow}</div>
                </div>`;
        },

        _settingsAiAccountConnectionRows(c) {
            const proof = c.ownership_proof || {};
            const state = c.lifecycle_state || 'unknown';
            const stateCls = state === 'active' ? 'bg-green-lt' : state === 'expired' ? 'bg-yellow-lt' : 'bg-red-lt';
            const fmt = (t) => t ? new Date(t * 1000).toLocaleString() : '—';
            const rows = this._settingsRows([
                ['Status', `<span class="badge ${stateCls}">${this.esc(state)}</span> <span class="badge bg-secondary-lt ms-1">${this.esc((c.revocation_state || 'not_revoked').replace(/_/g, ' '))}</span>`],
                ['Account fingerprint', `<code>${this.esc(proof.account_fingerprint || 'unverified')}</code>`],
                ['Approved host(s)', (c.host_allowlist || []).map((h) => `<code class="me-1">${this.esc(h)}</code>`).join('') || '<span class="text-secondary">—</span>'],
                ['Expires', this.esc(fmt(c.expires_at))],
                ['Last native verification', this.esc(fmt(c.last_verified_at))],
                ['Active leases', String(c.active_lease_count || 0)],
            ]);
            const ref = c.credential_reference;
            const disabled = state !== 'active';
            return `${rows}
                <div class="d-flex align-items-center mt-3 pt-2 border-top">
                    <span id="aiacct-conn-${this.esc(ref)}-flash" class="small text-secondary me-auto"></span>
                    <button type="button" class="btn btn-outline-secondary btn-sm me-1" ${disabled ? 'disabled' : ''} data-set-action="ai-accounts-verify:${this.esc(ref)}"><i class="ti ti-shield-check me-1" aria-hidden="true"></i>Verify</button>
                    <button type="button" class="btn btn-outline-secondary btn-sm me-1" ${disabled ? 'disabled' : ''} data-set-action="ai-accounts-reconnect:${this.esc(ref)}"><i class="ti ti-refresh me-1" aria-hidden="true"></i>Reconnect</button>
                    <button type="button" class="btn btn-outline-warning btn-sm me-1" ${disabled ? 'disabled' : ''} data-set-action="ai-accounts-revoke:${this.esc(ref)}"><i class="ti ti-ban me-1" aria-hidden="true"></i>Revoke</button>
                    <button type="button" class="btn btn-outline-danger btn-sm" data-set-action="ai-accounts-delete:${this.esc(ref)}"><i class="ti ti-trash me-1" aria-hidden="true"></i>Delete</button>
                </div>`;
        },

        // Recomputed at click time (not kept live-synced on every keystroke — this codebase's
        // settings actions are dispatched through the single delegated click handler, not
        // per-field input listeners), so it always reflects whatever is currently typed.
        // Reuses _settingsCopyField for the actual copy — same select()+execCommand
        // fallback the GitHub/token copy buttons already rely on.
        _settingsAiAccountsCopyCommand(providerId) {
            const idField = document.getElementById(`aiacct-${providerId}-account-id`);
            const cmdFieldId = `aiacct-${providerId}-cmd`;
            const cmdField = document.getElementById(cmdFieldId);
            if (cmdField) cmdField.value = this._settingsAiAccountsDeclareCommand(
                providerId, idField ? idField.value : '');
            this._settingsCopyField(cmdFieldId);
        },

        // The client never decides which host "matches" — it only offers each host this
        // user owns for the provider, one at a time, and trusts the server's independent
        // live cross-check (a client-computed guess about account_affinity_ids would just
        // be trusting the browser, which is exactly what this feature must not do).
        async _settingsAiAccountsConnect(providerId) {
            const proj = window.PM_PROJECT || 'maxwell';
            const accountId = (document.getElementById(`aiacct-${providerId}-account-id`)?.value || '').trim();
            const flash = document.getElementById(`aiacct-${providerId}-flash`);
            const setFlash = (msg, cls) => { if (flash) { flash.textContent = msg; flash.className = `small ms-2 ${cls}`; } };
            if (!accountId) { setFlash('Enter an account label first.', 'text-danger'); return; }
            setFlash('Connecting…', 'text-secondary');
            // Reuse the hosts list the section already fetched moments ago rather than
            // re-querying "every host in this project" again; only re-fetch if that
            // cache is unavailable (e.g. the earlier fetch itself failed).
            let hosts = this._aiAccountsHostsCache;
            if (!hosts) {
                const hostsData = await this._sfetch(`ixp/v1/agent_hosts?project=${encodeURIComponent(proj)}&include_stale=1`);
                hosts = hostsData.hosts || [];
            }
            const candidates = this._settingsAiAccountCandidateHosts(
                hosts, (this.principal && this.principal.id) || '', providerId);
            if (!candidates.length) {
                setFlash('No Agent Host is registered for you on this provider yet.', 'text-danger');
                return;
            }
            let lastError = 'No registered host has declared this account yet — run the command above, then try again.';
            for (const host of candidates) {
                try {
                    await this._sSend(`api/projects/${encodeURIComponent(proj)}/provider-connections/bind-host`, 'POST', {
                        provider: providerId, provider_account_id: accountId,
                        project_allowlist: [proj], host_id: host.host_id,
                    });
                    setFlash('Connected.', 'text-success');
                    await this.renderSettings();
                    return;
                } catch (e) { lastError = e.message || lastError; }
            }
            setFlash(lastError, 'text-danger');
        },

        async _settingsAiAccountsVerify(ref) {
            const proj = window.PM_PROJECT || 'maxwell';
            this._sFlash(`aiacct-conn-${ref}-flash`, 'Verifying…', 'text-secondary');
            try {
                await this._sSend(`api/projects/${encodeURIComponent(proj)}/provider-connections/${encodeURIComponent(ref)}/verify`, 'POST', {});
                await this.renderSettings();
            } catch (e) { this._sFlash(`aiacct-conn-${ref}-flash`, e.message, 'text-danger'); }
        },

        async _settingsAiAccountsReconnect(ref) {
            const proj = window.PM_PROJECT || 'maxwell';
            this._sFlash(`aiacct-conn-${ref}-flash`, 'Reconnecting…', 'text-secondary');
            try {
                await this._sSend(`api/projects/${encodeURIComponent(proj)}/provider-connections/${encodeURIComponent(ref)}/rotate`, 'POST', {});
                await this.renderSettings();
            } catch (e) { this._sFlash(`aiacct-conn-${ref}-flash`, e.message, 'text-danger'); }
        },

        async _settingsAiAccountsRevoke(ref) {
            if (!confirm('Revoke this connection? Active runners using it are killed immediately.')) return;
            const proj = window.PM_PROJECT || 'maxwell';
            this._sFlash(`aiacct-conn-${ref}-flash`, 'Revoking…', 'text-secondary');
            try {
                await this._sSend(`api/projects/${encodeURIComponent(proj)}/provider-connections/${encodeURIComponent(ref)}/revoke`, 'POST', { reason: 'operator_revoked_in_settings' });
                await this.renderSettings();
            } catch (e) { this._sFlash(`aiacct-conn-${ref}-flash`, e.message, 'text-danger'); }
        },

        async _settingsAiAccountsDelete(ref) {
            if (!confirm('Delete this connection? This cryptographically erases it and cannot be undone.')) return;
            const proj = window.PM_PROJECT || 'maxwell';
            this._sFlash(`aiacct-conn-${ref}-flash`, 'Deleting…', 'text-secondary');
            try {
                await this._sSend(`api/projects/${encodeURIComponent(proj)}/provider-connections/${encodeURIComponent(ref)}`, 'DELETE', { reason: 'operator_deleted_in_settings' });
                await this.renderSettings();
            } catch (e) { this._sFlash(`aiacct-conn-${ref}-flash`, e.message, 'text-danger'); }
        },

        // ── UI-21: API connections (user-owned, explicitly metered BYOK) ──────────
        // The host-local enroll command the user runs on their Agent Host. Like the
        // personal declare-account flow, the API key is typed on the host and never
        // crosses the browser — Settings only ever binds billing/budget and displays
        // the redacted result. `--api-key-stdin` prompts on the host (no shell history).
        _settingsApiEnrollCommand(providerId, billing, ceiling, currency) {
            const proj = window.PM_PROJECT || 'maxwell';
            const q = (v, d) => `'${String(v || d).replace(/'/g, "'\\''")}'`;
            return `python adapters/agent_host_enrollment.py enroll-api-key \\\n`
                + `  --identity ~/.config/switchboard-agent-host/identity.json \\\n`
                + `  --config ~/.config/switchboard-agent-host/config.json \\\n`
                + `  --project ${q(proj, 'maxwell')} --provider ${q(providerId, 'openai-codex')} \\\n`
                + `  --provider-account ${q(billing, 'acct-billing-1')} \\\n`
                + `  --billing-account ${q(billing, 'acct-billing-1')} \\\n`
                + `  --budget-ceiling ${q(ceiling, '50')} --budget-currency ${q(currency, 'usd')} \\\n`
                + `  --api-key-stdin`;
        },

        _settingsApiConnectionCard(provider, ctx) {
            const conn = ctx.connectionsError ? null : this._settingsAiAccountLiveConnection(
                ctx.conns, provider.id, (kind) => kind !== 'personal_subscription');
            const headerBadge = conn
                ? '<span class="badge bg-green-lt">connected</span>'
                : (provider.enabled ? '<span class="badge bg-secondary-lt">not connected</span>'
                    : `<span class="badge bg-secondary-lt">gated · ${this.esc(provider.gate || '')}</span>`);
            let body;
            if (ctx.connectionsError) {
                body = `<div class="alert alert-secondary py-2 px-3 small mb-0" role="note"><i class="ti ti-info-circle me-1" aria-hidden="true"></i>Connection status is unavailable right now — see the error above.</div>`;
            } else if (conn) {
                body = this._settingsApiConnectionRows(conn);
            } else if (!provider.enabled) {
                body = `<div class="alert alert-secondary py-2 px-3 small mb-0" role="note"><i class="ti ti-lock me-1" aria-hidden="true"></i>Direct API for ${this.esc(provider.label)} is not self-service yet — it stays disabled until its adapter qualifies (${this.esc(provider.gate || 'a follow-on adapter')}). OpenAI is the supported API row today.</div>`;
            } else {
                const billId = `apiconn-${provider.id}-billing`;
                const ceilId = `apiconn-${provider.id}-ceiling`;
                const curId = `apiconn-${provider.id}-currency`;
                const cmdId = `apiconn-${provider.id}-cmd`;
                body = `<div class="row g-2 mb-2">
                        <div class="col-md-6"><label class="form-label small mb-1" for="${billId}">Billing account</label>
                            <input type="text" id="${billId}" class="form-control form-control-sm" placeholder="acct-billing-1" autocomplete="off" spellcheck="false"></div>
                        <div class="col-md-3"><label class="form-label small mb-1" for="${ceilId}">Budget ceiling</label>
                            <input type="number" id="${ceilId}" class="form-control form-control-sm" placeholder="50" min="1" step="1"></div>
                        <div class="col-md-3"><label class="form-label small mb-1" for="${curId}">Currency</label>
                            <input type="text" id="${curId}" class="form-control form-control-sm" value="usd" autocomplete="off" spellcheck="false"></div>
                    </div>
                    <label class="form-label small mb-1">On your registered Agent Host, enroll your API key</label>
                    <div class="input-group input-group-sm mb-2">
                        <input type="text" id="${cmdId}" class="form-control font-monospace" readonly value="${this.esc(this._settingsApiEnrollCommand(provider.id, '', '', 'usd'))}">
                        <button class="btn btn-outline-secondary" type="button" data-set-action="api-connections-copy-cmd:${provider.id}" title="Copy command with the billing/budget you typed"><i class="ti ti-copy" aria-hidden="true"></i></button>
                    </div>
                    <div class="small text-secondary mb-0"><i class="ti ti-shield-lock me-1" aria-hidden="true"></i>Your API key is entered on your own host, sent only over TLS to its owner-bound one-use endpoint, and immediately envelope-encrypted by Switchboard. It never touches this browser, is never logged or echoed, and only ciphertext plus a redacted fingerprint is retained. See <code>docs/AGENT-HOST-ENROLLMENT.md</code>.</div>`;
            }
            return `<div class="card mb-3" id="settings-api-conn-${provider.id}">
                <div class="card-header"><div><h4 class="card-title mb-0"><i class="ti ${provider.icon} me-2" aria-hidden="true"></i>${this.esc(provider.label)}</h4>
                    <div class="text-secondary small">User-owned API key · metered</div></div>
                    <div class="card-actions">${headerBadge}</div></div>
                <div class="card-body">${body}</div></div>`;
        },

        _settingsApiConnectionRows(c) {
            const state = c.lifecycle_state || 'unknown';
            const stateCls = state === 'active' ? 'bg-green-lt' : state === 'expired' ? 'bg-yellow-lt' : 'bg-red-lt';
            const fmt = (t) => t ? new Date(t * 1000).toLocaleString() : '—';
            const budget = c.budget_policy || {};
            const budgetTxt = (budget.ceiling != null)
                ? `${this.esc(String(budget.ceiling))} ${this.esc((budget.currency || '').toUpperCase())}`
                    + (c.budget_spent != null ? ` · spent ${this.esc(String(c.budget_spent))}` : '')
                : '<span class="text-secondary">—</span>';
            const rows = this._settingsRows([
                ['Status', `<span class="badge ${stateCls}">${this.esc(state)}</span> <span class="badge bg-secondary-lt ms-1">${this.esc((c.revocation_state || 'not_revoked').replace(/_/g, ' '))}</span>`],
                ['Connection kind', `<code>${this.esc(c.connection_kind || 'direct_api')}</code>`],
                ['Execution connection', `<code>${this.esc(c.execution_connection_id || c.credential_reference || '—')}</code>`],
                ['Billing account', c.billing_account_bound ? `<code>${this.esc(c.billing_account_fingerprint || 'bound')}</code>` : '<span class="text-danger">not bound</span>'],
                ['Budget', budgetTxt],
                ['Approved host(s)', (c.host_allowlist || []).map((h) => `<code class="me-1">${this.esc(h)}</code>`).join('') || '<span class="text-secondary">—</span>'],
                ['Active runners', String(c.active_lease_count || 0)],
                ['Last use', this.esc(fmt(c.last_verified_at))],
            ]);
            const ref = c.credential_reference;
            const disabled = state !== 'active';
            return `<div class="alert alert-warning py-2 px-3 small" role="note"><i class="ti ti-currency-dollar me-1" aria-hidden="true"></i>Metered: usage on this connection bills your own provider account against the budget above.</div>
                ${rows}
                <div class="small text-secondary mt-2"><i class="ti ti-refresh me-1" aria-hidden="true"></i>To rotate the key, re-run <code>enroll-api-key</code> on your host — the new key is envelope-encrypted there and never crosses the browser.</div>
                <div class="d-flex align-items-center mt-3 pt-2 border-top">
                    <span id="apiconn-${this.esc(ref)}-flash" class="small text-secondary me-auto"></span>
                    <button type="button" class="btn btn-outline-warning btn-sm me-1" ${disabled ? 'disabled' : ''} data-set-action="api-connections-revoke:${this.esc(ref)}"><i class="ti ti-ban me-1" aria-hidden="true"></i>Revoke</button>
                    <button type="button" class="btn btn-outline-danger btn-sm" data-set-action="api-connections-delete:${this.esc(ref)}"><i class="ti ti-trash me-1" aria-hidden="true"></i>Delete</button>
                </div>`;
        },

        _settingsApiConnectionsCopyCommand(providerId) {
            const val = (id) => (document.getElementById(id)?.value || '').trim();
            const cmdId = `apiconn-${providerId}-cmd`;
            const cmdField = document.getElementById(cmdId);
            if (cmdField) cmdField.value = this._settingsApiEnrollCommand(
                providerId, val(`apiconn-${providerId}-billing`), val(`apiconn-${providerId}-ceiling`),
                val(`apiconn-${providerId}-currency`) || 'usd');
            this._settingsCopyField(cmdId);
        },

        async _settingsApiConnectionsRevoke(ref) {
            if (!confirm('Revoke this API connection? Active runners billing it are stopped immediately.')) return;
            const proj = window.PM_PROJECT || 'maxwell';
            this._sFlash(`apiconn-${ref}-flash`, 'Revoking…', 'text-secondary');
            try {
                await this._sSend(`api/projects/${encodeURIComponent(proj)}/provider-connections/${encodeURIComponent(ref)}/revoke`, 'POST', { reason: 'operator_revoked_in_settings' });
                await this.renderSettings();
            } catch (e) { this._sFlash(`apiconn-${ref}-flash`, e.message, 'text-danger'); }
        },

        async _settingsApiConnectionsDelete(ref) {
            if (!confirm('Delete this API connection? This cryptographically erases the stored key and cannot be undone.')) return;
            const proj = window.PM_PROJECT || 'maxwell';
            this._sFlash(`apiconn-${ref}-flash`, 'Deleting…', 'text-secondary');
            try {
                await this._sSend(`api/projects/${encodeURIComponent(proj)}/provider-connections/${encodeURIComponent(ref)}`, 'DELETE', { reason: 'operator_deleted_in_settings' });
                await this.renderSettings();
            } catch (e) { this._sFlash(`apiconn-${ref}-flash`, e.message, 'text-danger'); }
        },

        _settingsAppearanceSection() {
            // Switchboard is deliberately a single clean light theme and the sidebar is
            // pinned expanded (see static/taikun-ui.js), so there is nothing user-tunable
            // to offer yet. Say that plainly rather than reviving the legacy theme
            // offcanvas or shipping a toggle that changes nothing.
            return Promise.resolve(this._settingsCard({
                id: 'settings-appearance', title: 'Appearance', icon: 'ti-palette',
                subtitle: 'How Switchboard looks for you',
                body: `<div class="alert alert-secondary py-2 px-3 small mb-3" role="note"><i class="ti ti-info-circle me-1" aria-hidden="true"></i>Switchboard ships one deliberate light theme, so there are no per-user appearance options today. These are the current fixed values.</div>`
                    + this._settingsRows([
                        ['Theme', '<span class="badge bg-secondary-lt">single light theme</span>'],
                        ['Navigation', '<span class="badge bg-secondary-lt">always expanded</span>'],
                        ['Density', '<span class="badge bg-secondary-lt">comfortable</span>'],
                    ]),
            }));
        },

        /* ---- Project settings --------------------------------------------- */

        // UI-20 (4/6): the UI-5 Members & access modal folded in-place. The section nav is
        // gated write:system, so anyone who can open it can edit — the server still enforces
        // per-action. Role change is grant-then-revoke, never an update (grants are per-role
        // rows): _settingsMembersChangeRole grants the new role, then revokes the old one
        // (inventory rule #4).
        async _settingsMembersSection() {
            const proj = window.PM_PROJECT || 'maxwell';
            this._mmProject = proj;
            const data = await this._sfetch(`api/access/members?project=${encodeURIComponent(proj)}`);
            if (data.error) return this._settingsErrCard('Members & access', data.error);
            this._mmGlobalAuth = !!data.global_auth;
            const body = `
                <div id="mm-visibility" class="alert alert-secondary py-2 px-3 small mb-3">${this._settingsMembersVisibilityHtml(data.visibility)}</div>
                <div class="d-flex align-items-center mb-2">
                    <h4 class="mb-0"><i class="ti ti-user-check me-1" aria-hidden="true"></i>Members</h4>
                    <span id="mm-count" class="badge bg-secondary-lt ms-2">${(data.members || []).length}</span>
                    <button type="button" class="btn btn-sm btn-ghost-secondary ms-auto p-1" data-set-action="members-refresh" title="Refresh members"><i class="ti ti-refresh" aria-hidden="true"></i></button>
                </div>
                <div id="mm-members" class="table-responsive mb-3">${this._settingsMembersTableHtml(data.members || [])}</div>
                <hr class="my-3">
                <h4><i class="ti ti-user-plus me-1" aria-hidden="true"></i>Add a member</h4>
                <div class="row g-2 align-items-end">
                    <div class="col-sm-3">
                        <label class="form-label small mb-1" for="mm-kind">Subject</label>
                        <select id="mm-kind" class="form-select form-select-sm">
                            <option value="user" selected>Person (email)</option>
                            <option value="principal">Token / principal</option>
                            <option value="agent">Agent</option>
                        </select>
                    </div>
                    <div class="col-sm-5">
                        <label class="form-label small mb-1" for="mm-subject"><span id="mm-subject-label">Email</span></label>
                        <input id="mm-subject" class="form-control form-control-sm" placeholder="teammate@company.com" autocomplete="off">
                    </div>
                    <div class="col-sm-2">
                        <label class="form-label small mb-1" for="mm-role">Role</label>
                        <select id="mm-role" class="form-select form-select-sm">
                            <option value="viewer">viewer</option>
                            <option value="commenter">commenter</option>
                            <option value="contributor" selected>contributor</option>
                            <option value="operator">operator</option>
                            <option value="admin">admin</option>
                        </select>
                    </div>
                    <div class="col-sm-2">
                        <button type="button" id="mm-add" class="btn btn-sm btn-primary w-100" data-set-action="members-add"><i class="ti ti-plus me-1" aria-hidden="true"></i>Add</button>
                    </div>
                </div>
                <div id="mm-add-flash" class="small mt-2 text-secondary"></div>`;
            return this._settingsCard({
                id: 'settings-members', title: 'Members & access', icon: 'ti-users',
                subtitle: `Who can reach ${proj}, and with which role`,
                body,
                footer: '<span class="text-secondary small">Roles map to scopes; changes are audited.</span>',
            });
        },

        _settingsMembersVisibilityHtml(visibility) {
            if (visibility === 'private') return '<i class="ti ti-lock me-1" aria-hidden="true"></i><strong>Private project.</strong> Who can see it: owner <span class="text-success">✓</span> · invited members <span class="text-success">✓</span> · org admins <span class="text-success">✓</span> · other org members <span class="text-danger">✗</span>.';
            return '<i class="ti ti-users me-1" aria-hidden="true"></i><strong>Shared project.</strong> Everyone in the organization can see it; roles below control what they can change.';
        },

        _settingsMembersTableHtml(members) {
            if (!members.length) return '<div class="text-secondary small">No role grants yet — the owner and org admins still have access.</div>';
            const roles = ['viewer', 'commenter', 'contributor', 'operator', 'admin', 'owner'];
            const kindBadge = (k) => `<span class="badge bg-secondary-lt ms-1">${this.esc(k)}</span>`;
            return `<table class="table table-sm align-middle mb-0">
                <thead><tr><th>Member</th><th>Role</th><th>Granted by</th><th></th></tr></thead>
                <tbody>${members.map((m) => {
        const opts = roles.map((r) => `<option value="${r}"${r === m.role ? ' selected' : ''}>${r}</option>`).join('');
        const who = this.esc(m.display_name || m.subject_id);
        const sub = m.email && m.email !== m.display_name ? `<div class="text-secondary small">${this.esc(m.email)}</div>` : '';
        const enc = encodeURIComponent(JSON.stringify({ subject_kind: m.subject_kind, subject_id: m.subject_id, role: m.role }));
        return `<tr>
                    <td><span class="fw-medium">${who}</span>${kindBadge(m.subject_kind)}${sub}</td>
                    <td><select class="form-select form-select-sm" style="width:auto" data-mm-role="${enc}" aria-label="Role for ${who}">${opts}</select></td>
                    <td class="text-secondary small">${this.esc(m.created_by || '—')}</td>
                    <td class="text-end"><button type="button" class="btn btn-sm btn-ghost-danger p-1" data-set-action="members-revoke:${enc}" title="Revoke ${who}"><i class="ti ti-trash" aria-hidden="true"></i></button></td>
                </tr>`;
    }).join('')}</tbody></table>`;
        },

        async _settingsMembersReload() {
            const proj = this._mmProject || window.PM_PROJECT || 'maxwell';
            const host = document.getElementById('mm-members');
            const data = await this._sfetch(`api/access/members?project=${encodeURIComponent(proj)}`);
            if (data.error) { if (host) host.innerHTML = `<div class="text-danger small">${this.esc(data.error)}</div>`; return; }
            this._mmGlobalAuth = !!data.global_auth;
            const vis = document.getElementById('mm-visibility'); if (vis) vis.innerHTML = this._settingsMembersVisibilityHtml(data.visibility);
            const count = document.getElementById('mm-count'); if (count) count.textContent = `${(data.members || []).length}`;
            if (host) host.innerHTML = this._settingsMembersTableHtml(data.members || []);
        },

        _settingsMembersFlash(msg, cls) {
            const f = document.getElementById('mm-add-flash');
            if (f) { f.textContent = msg; f.className = `small mt-2 ${cls || 'text-secondary'}`; }
        },

        async _accessPost(path, body) {
            const proj = this._mmProject || window.PM_PROJECT || 'maxwell';
            return this._sSend(`api/access/${path}?project=${encodeURIComponent(proj)}`, 'POST', body);
        },

        // Change role = grant the new role, then revoke the old one (grants are per-role
        // rows, so this is never an in-place update) — inventory rule #4.
        async _settingsMembersChangeRole(grant, newRole) {
            if (!grant || newRole === grant.role) return;
            try {
                await this._accessPost('project_role', { subject_kind: grant.subject_kind, subject_id: grant.subject_id, role: newRole });
                if (grant.role) await this._accessPost('project_role/revoke', grant);
            } catch (e) { this._settingsMembersFlash(e.message, 'text-danger'); }
            this._settingsMembersReload();
        },

        async _settingsMembersRevoke(grant) {
            try { await this._accessPost('project_role/revoke', grant); }
            catch (e) { this._settingsMembersFlash(e.message, 'text-danger'); }
            this._settingsMembersReload();
        },

        async _settingsMembersAdd() {
            const kind = document.getElementById('mm-kind')?.value || 'user';
            const subject = (document.getElementById('mm-subject')?.value || '').trim();
            const role = document.getElementById('mm-role')?.value || 'contributor';
            if (!subject) { this._settingsMembersFlash('Enter an email or subject id.', 'text-danger'); return; }
            const btn = document.getElementById('mm-add'); if (btn) btn.disabled = true;
            this._settingsMembersFlash('Adding…', 'text-secondary');
            try {
                if (kind === 'user' && subject.includes('@')) {
                    const r = await this._accessPost('invite', { email: subject, role });
                    this._settingsMembersFlash(`Invited ${r.invited?.display_name || subject} as ${role}.`, 'text-success');
                } else {
                    await this._accessPost('project_role', { subject_kind: kind, subject_id: subject, role });
                    this._settingsMembersFlash(`Granted ${role} to ${subject}.`, 'text-success');
                }
                const subEl = document.getElementById('mm-subject'); if (subEl) subEl.value = '';
                this._settingsMembersReload();
            } catch (e) {
                this._settingsMembersFlash(e.message || 'Failed to add member.', 'text-danger');
            } finally {
                if (btn) btn.disabled = false;
            }
        },

        // UI-20 (3/6): the UI-14 Communications modal folded in-place. Inbound domain
        // associations (the editable UI-13 routing map) + per-project outbound recipients/
        // cadence. Anyone who can read the project sees it; edits are gated on the server's
        // can_edit probe. The admin gate is applied INLINE at render time (disabled
        // attributes driven by can_edit), replacing the modal-era querySelectorAll that was
        // pinned to the now-retired modal id — that selector would have silently no-op'd
        // once the markup left the modal, leaving every control enabled (inventory rule #2).
        async _settingsCommsSection() {
            const proj = window.PM_PROJECT || 'maxwell';
            this._commsProject = proj;
            const cfg = await this._sfetch(`api/projects/${encodeURIComponent(proj)}/comms`);
            if (cfg.error) return this._settingsErrCard('Communications', cfg.error);
            const inb = cfg.inbound || {}, out = cfg.outbound || {}, fb = cfg.global_fallback || {};
            const admin = typeof cfg.can_edit === 'boolean' ? cfg.can_edit : true;
            this._commsAdmin = admin;
            this._comms = {
                domains: (inb.domains || []).slice(),
                notify: (out.notify_recipients || []).slice(),
                digest: (out.digest_recipients || []).slice(),
            };
            const dis = admin ? '' : ' disabled';
            const cadenceOpts = out.cadence_options || ['off', 'daily', 'weekly', 'monthly'];
            const cadenceSel = out.cadence || 'weekly';
            const warn = admin ? '' : '<div class="alert alert-warning py-2 px-3 small mb-3" id="comms-admin-warn"><i class="ti ti-lock me-1" aria-hidden="true"></i>You need <strong>write:system</strong> on this project to change these settings. You can view them, but Save and Send test are disabled.</div>';
            const body = `${warn}
                <div class="card mb-3"><div class="card-body">
                    <h4 class="mb-1"><i class="ti ti-inbox me-1" aria-hidden="true"></i>Inbound mail</h4>
                    <p class="text-secondary small mb-3">Mail addressed to this board's plus-address, or from an associated domain, lands in this project's Inbox.</p>
                    <label class="form-label small mb-1">Plus-address <span class="text-secondary fw-normal">(zero-config — works today, no setup)</span></label>
                    <div class="input-group input-group-sm mb-3">
                        <input type="text" id="comms-plus" class="form-control font-monospace" readonly value="${this.esc(inb.plus_address || '')}" aria-label="Project plus-address">
                        <button class="btn btn-outline-secondary" type="button" data-set-action="comms-copy" title="Copy plus-address"><i class="ti ti-copy" aria-hidden="true"></i></button>
                    </div>
                    <label class="form-label small mb-1">Associated sender domains</label>
                    <div class="form-hint mb-1">Any email whose sender is at one of these domains routes to <strong>${this.esc(proj)}</strong> — no <code>.env</code> edit. A domain maps to exactly one board.</div>
                    <div id="comms-domains" class="mb-2 d-flex flex-wrap gap-1">${this._settingsCommsChipsHtml(this._comms.domains, 'domains', 'No domains associated — plus-address still works.', admin)}</div>
                    <div class="input-group input-group-sm comms-editable">
                        <input type="text" id="comms-domain-input" class="form-control" placeholder="client.com" autocomplete="off" autocapitalize="off" spellcheck="false" data-comms-add="comms-add-domain"${dis}>
                        <button type="button" class="btn btn-outline-primary" data-set-action="comms-add-domain"${dis}><i class="ti ti-plus me-1" aria-hidden="true"></i>Add domain</button>
                    </div>
                </div></div>
                <div class="card mb-2"><div class="card-body">
                    <h4 class="mb-1"><i class="ti ti-send me-1" aria-hidden="true"></i>Outbound recipients</h4>
                    <p class="text-secondary small mb-3">Where this project's notifications and digest go. Empty falls back to the global list <code id="comms-fallback" class="text-secondary">${fb.configured ? this.esc((fb.notify_to || []).join(', ')) : '(none configured)'}</code>.</p>
                    <label class="form-label small mb-1">Notify recipients</label>
                    <div id="comms-notify" class="mb-2 d-flex flex-wrap gap-1">${this._settingsCommsChipsHtml(this._comms.notify, 'notify', 'Falls back to the global list.', admin)}</div>
                    <div class="input-group input-group-sm mb-3 comms-editable">
                        <input type="email" id="comms-notify-input" class="form-control" placeholder="ops@client.com" autocomplete="off" data-comms-add="comms-add:notify"${dis}>
                        <button type="button" class="btn btn-outline-primary" data-set-action="comms-add:notify"${dis}><i class="ti ti-plus me-1" aria-hidden="true"></i>Add</button>
                        <button type="button" class="btn btn-outline-secondary" data-set-action="comms-test:notify" title="Send a test notification"${dis}><i class="ti ti-mail-forward me-1" aria-hidden="true"></i>Send test</button>
                    </div>
                    <label class="form-label small mb-1">Digest recipients</label>
                    <div id="comms-digest" class="mb-2 d-flex flex-wrap gap-1">${this._settingsCommsChipsHtml(this._comms.digest, 'digest', 'Falls back to the global list.', admin)}</div>
                    <div class="input-group input-group-sm mb-3 comms-editable">
                        <input type="email" id="comms-digest-input" class="form-control" placeholder="lead@client.com" autocomplete="off" data-comms-add="comms-add:digest"${dis}>
                        <button type="button" class="btn btn-outline-primary" data-set-action="comms-add:digest"${dis}><i class="ti ti-plus me-1" aria-hidden="true"></i>Add</button>
                        <button type="button" class="btn btn-outline-secondary" data-set-action="comms-test:digest" title="Send a test digest"${dis}><i class="ti ti-mail-forward me-1" aria-hidden="true"></i>Send test</button>
                    </div>
                    <div class="row g-2 align-items-end">
                        <div class="col-sm-6">
                            <label class="form-label small mb-1">Digest cadence <span class="text-secondary fw-normal">(advisory)</span></label>
                            <select id="comms-cadence" class="form-select form-select-sm comms-editable"${dis}>${cadenceOpts.map((c) => `<option value="${this.esc(c)}"${c === cadenceSel ? ' selected' : ''}>${this.esc(c)}</option>`).join('')}</select>
                        </div>
                        <div class="col-sm-6"><div class="form-hint">The scheduled digest timer is global; cadence records intent and lets you turn a project's digest <code>off</code>.</div></div>
                    </div>
                </div></div>`;
            return this._settingsCard({
                id: 'settings-comms', title: 'Communications', icon: 'ti-mail-cog',
                subtitle: 'Inbound intake domains and outbound digest recipients',
                body,
                footer: `<span id="comms-flash" class="small text-secondary me-auto"></span>
                    <button class="btn btn-primary btn-sm ms-auto" type="button" data-set-action="comms-save"${dis}><i class="ti ti-device-floppy me-1" aria-hidden="true"></i>Save</button>`,
            });
        },

        _settingsCommsChipsHtml(list, kind, empty, admin) {
            if (!list.length) return `<span class="text-secondary small">${this.esc(empty)}</span>`;
            return list.map((val) => {
                const x = admin ? `<button type="button" class="btn-close btn-close-sm ms-1" data-set-action="comms-rm:${kind}:${encodeURIComponent(val)}" aria-label="Remove ${this.esc(val)}"></button>` : '';
                return `<span class="badge bg-blue-lt d-inline-flex align-items-center">${this.esc(val)}${x}</span>`;
            }).join('');
        },

        _settingsCommsRenderChips() {
            const admin = this._commsAdmin;
            const set = (id, list, kind, empty) => { const el = document.getElementById(id); if (el) el.innerHTML = this._settingsCommsChipsHtml(list, kind, empty, admin); };
            set('comms-domains', this._comms.domains, 'domains', 'No domains associated — plus-address still works.');
            set('comms-notify', this._comms.notify, 'notify', 'Falls back to the global list.');
            set('comms-digest', this._comms.digest, 'digest', 'Falls back to the global list.');
        },

        _settingsCommsFlash(msg, cls) {
            const f = document.getElementById('comms-flash');
            if (f) { f.textContent = msg; f.className = `small me-auto ${cls || 'text-secondary'}`; }
        },

        _settingsCommsAddDomain() {
            const inp = document.getElementById('comms-domain-input');
            const v = (inp?.value || '').trim().replace(/^@/, '').toLowerCase();
            if (!v) return;
            if (!/^[a-z0-9.-]+\.[a-z]{2,}$/.test(v)) { this._settingsCommsFlash('Enter a valid domain like client.com.', 'text-danger'); return; }
            if (this._comms.domains.indexOf(v) < 0) this._comms.domains.push(v);
            if (inp) inp.value = '';
            this._settingsCommsFlash('', '');
            this._settingsCommsRenderChips();
        },

        _settingsCommsAddRecipient(kind) {
            if (kind !== 'notify' && kind !== 'digest') return;
            const inp = document.getElementById(`comms-${kind}-input`);
            const v = (inp?.value || '').trim();
            if (!v) return;
            if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v)) { this._settingsCommsFlash('Enter a valid email address.', 'text-danger'); return; }
            if (this._comms[kind].map((x) => x.toLowerCase()).indexOf(v.toLowerCase()) < 0) this._comms[kind].push(v);
            if (inp) inp.value = '';
            this._settingsCommsFlash('', '');
            this._settingsCommsRenderChips();
        },

        _settingsCommsRemove(kind, val) {
            if (!this._comms || !this._comms[kind]) return;
            this._comms[kind] = this._comms[kind].filter((v) => v !== val);
            this._settingsCommsRenderChips();
        },

        _settingsCommsCopyPlus() {
            const src = document.getElementById('comms-plus');
            if (!src) return;
            src.select();
            if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(src.value).catch(() => { try { document.execCommand('copy'); } catch (e) { /* noop */ } });
            else { try { document.execCommand('copy'); } catch (e) { /* noop */ } }
        },

        async _settingsCommsSave() {
            const proj = this._commsProject || window.PM_PROJECT || 'maxwell';
            const cadence = document.getElementById('comms-cadence')?.value || 'weekly';
            this._settingsCommsFlash('Saving…', 'text-secondary');
            try {
                await this._sSend(`api/projects/${encodeURIComponent(proj)}/comms`, 'POST', {
                    inbound: { domains: this._comms.domains },
                    outbound: { notify_recipients: this._comms.notify, digest_recipients: this._comms.digest, cadence },
                });
                this._settingsCommsFlash('Saved.', 'text-success');
            } catch (e) { this._settingsCommsFlash(e.message || 'Failed to save.', 'text-danger'); }
        },

        async _settingsCommsTest(kind) {
            const proj = this._commsProject || window.PM_PROJECT || 'maxwell';
            this._settingsCommsFlash('Sending test…', 'text-secondary');
            try {
                const data = await this._sSend(`api/projects/${encodeURIComponent(proj)}/comms/test`, 'POST', { kind });
                const sent = (data.results || []).some((r) => r.sent);
                const to = (data.recipients || []).join(', ') || '(no recipients — set some or a global fallback)';
                this._settingsCommsFlash(sent ? `Test sent to ${to}.` : `Dry-run (SMTP not configured) — would send to ${to}.`, sent ? 'text-success' : 'text-secondary');
            } catch (e) { this._settingsCommsFlash(e.message || 'Failed to send test.', 'text-danger'); }
        },

        // UI-20 (5/6): the UI-15 Connect-repo modal folded in-place, below the repo-topology
        // card. Rule #3 — never probe GitHub on open: the section fetch omits ?check=1, so it
        // only reads the recorded association; the Verify button is the sole path that probes
        // reachability (_settingsGithubLoad(true)). The New Project → repo handoff now reloads
        // straight into ?project=<id>#tab-settings/github instead of the modal's ga-goto.
        async _settingsGithubSection() {
            const proj = window.PM_PROJECT || 'maxwell';
            this._gaProject = proj;
            const topology = await this._sfetch(`api/projects/${encodeURIComponent(proj)}/repo_topology`);
            const assoc = await this._sfetch(`api/projects/${encodeURIComponent(proj)}/github_association`);
            const connectCard = assoc.error
                ? this._settingsErrCard('Connect a GitHub repo', assoc.error)
                : this._settingsGithubConnectCardHtml(assoc);
            return this._settingsRepoCard(topology) + connectCard;
        },

        _settingsGithubBadgeParts(v) {
            v = v || {};
            let cls = 'badge ms-2 ', txt = '';
            if (v.status === 'connected') {
                cls += 'bg-green-lt'; txt = 'Connected';
                if (v.delivery_count) txt += ` · ${v.delivery_count} deliver${v.delivery_count === 1 ? 'y' : 'ies'}`;
            } else if (v.repo_reachable === false) {
                cls += 'bg-red-lt'; txt = 'Repo unreachable';
            } else if (v.repo_reachable === true) {
                cls += 'bg-yellow-lt'; txt = 'Repo found · awaiting first delivery';
            } else {
                cls += 'bg-yellow-lt'; txt = 'Awaiting first delivery';
            }
            return [cls, txt];
        },

        _settingsGithubConnectCardHtml(data) {
            data = data || {};
            const wh = data.webhook || {};
            const configured = !!data.repo_configured;
            const parts = this._settingsGithubBadgeParts(data.verification);
            return `<div class="card mb-3" id="settings-github-connect">
                <div class="card-header"><div><h3 class="card-title"><i class="ti ti-brand-github me-2" aria-hidden="true"></i>Connect a GitHub repo</h3>
                    <div class="text-secondary small">Point a repo at <strong>${this.esc(this._gaProject)}</strong> so merged PRs stamp its tasks Done automatically.</div></div></div>
                <div class="card-body">
                    <label class="form-label" for="ga-repo">GitHub repo <span class="text-secondary fw-normal">(<code>owner/name</code>)</span></label>
                    <div class="input-group">
                        <input type="text" id="ga-repo" class="form-control" placeholder="owner/name" autocomplete="off" autocapitalize="off" spellcheck="false" value="${this.esc(data.repo || '')}">
                        <button type="button" id="ga-save" class="btn btn-outline-primary" data-set-action="github-save"><i class="ti ti-device-floppy me-1" aria-hidden="true"></i>Save</button>
                    </div>
                    <div id="ga-repo-flash" class="small mt-1 text-secondary"></div>
                    <div id="ga-panel" style="${configured ? '' : 'display:none'}">
                        <hr class="my-3">
                        <div class="d-flex align-items-center mb-2">
                            <h4 class="mb-0"><i class="ti ti-webhook me-1" aria-hidden="true"></i>Wire the webhook</h4>
                            <span id="ga-status" class="${parts[0]}">${this.esc(parts[1])}</span>
                        </div>
                        <p class="text-secondary small mb-3">Add a webhook on <code id="ga-repo-name">${this.esc(data.repo || '')}</code> so this board hears push &amp; PR events. The <code>?project=</code> pin is <strong>pre-filled and required</strong> — a bare URL fails closed on repos shared by more than one board.</p>
                        <div class="mb-2">
                            <label class="form-label small mb-1">Payload URL <span class="text-secondary fw-normal">(<code>?project=</code> pinned)</span></label>
                            <div class="input-group input-group-sm">
                                <input type="text" id="ga-url" class="form-control font-monospace" readonly value="${this.esc(wh.payload_url || '')}">
                                <button class="btn btn-outline-secondary" data-set-action="github-copy:ga-url" type="button" title="Copy payload URL"><i class="ti ti-copy" aria-hidden="true"></i></button>
                            </div>
                        </div>
                        <div class="row g-2 mb-2">
                            <div class="col-sm-6"><label class="form-label small mb-1">Content type</label><input type="text" class="form-control form-control-sm font-monospace" value="application/json" readonly></div>
                            <div class="col-sm-6"><label class="form-label small mb-1">Secret <span class="text-secondary fw-normal">(server env)</span></label><input type="text" id="ga-secret" class="form-control form-control-sm font-monospace" readonly value="${this.esc(wh.secret_env || '')}"></div>
                        </div>
                        <div class="mb-2"><label class="form-label small mb-1">Events</label><div><span class="badge bg-blue-lt">push</span> <span class="badge bg-blue-lt">pull_request</span></div></div>
                        <div class="alert alert-warning py-2 px-3 small mb-3" id="ga-secret-warn" style="${wh.secret_configured ? 'display:none' : ''}"><i class="ti ti-alert-triangle me-1" aria-hidden="true"></i>The server has no <code>PM_GITHUB_WEBHOOK_SECRET</code> set, so deliveries won't be signature-verified until an operator configures one.</div>
                        <div class="mb-1">
                            <label class="form-label small mb-1">Or install it with one command (<a href="https://cli.github.com/" target="_blank" rel="noopener"><code>gh</code></a> CLI)</label>
                            <div class="input-group input-group-sm">
                                <input type="text" id="ga-gh" class="form-control font-monospace" readonly value="${this.esc(wh.gh_command || '')}">
                                <button class="btn btn-outline-secondary" data-set-action="github-copy:ga-gh" type="button" title="Copy gh command"><i class="ti ti-copy" aria-hidden="true"></i></button>
                            </div>
                            <div class="form-hint">Export <code>$PM_GITHUB_WEBHOOK_SECRET</code> to match the server secret before running.</div>
                        </div>
                    </div>
                </div>
                <div class="card-footer d-flex align-items-center">
                    <span id="ga-verify-flash" class="small text-secondary me-auto"></span>
                    <button type="button" id="ga-verify" class="btn btn-primary btn-sm" data-set-action="github-verify" style="${configured ? '' : 'display:none'}"><i class="ti ti-plug-connected me-1" aria-hidden="true"></i>Verify connection</button>
                </div></div>`;
        },

        _settingsGithubRender(data) {
            const repoInput = document.getElementById('ga-repo');
            if (repoInput && data.repo && !repoInput.value) repoInput.value = data.repo;
            const panel = document.getElementById('ga-panel');
            const verify = document.getElementById('ga-verify');
            if (!data.repo_configured) {
                if (panel) panel.style.display = 'none';
                if (verify) verify.style.display = 'none';
                return;
            }
            if (panel) panel.style.display = '';
            if (verify) verify.style.display = '';
            const wh = data.webhook || {};
            const set = (id, val) => { const el = document.getElementById(id); if (el) el.value = val || ''; };
            set('ga-url', wh.payload_url); set('ga-secret', wh.secret_env); set('ga-gh', wh.gh_command);
            const name = document.getElementById('ga-repo-name'); if (name) name.textContent = data.repo || '';
            const warn = document.getElementById('ga-secret-warn'); if (warn) warn.style.display = wh.secret_configured ? 'none' : '';
            const badge = document.getElementById('ga-status');
            if (badge) { const parts = this._settingsGithubBadgeParts(data.verification); badge.className = parts[0]; badge.textContent = parts[1]; }
        },

        // check=true is the only path that probes repo reachability (?check=1) — the section
        // open path never does (rule #3).
        async _settingsGithubLoad(check) {
            const proj = this._gaProject || window.PM_PROJECT || 'maxwell';
            const data = await this._sfetch(`api/projects/${encodeURIComponent(proj)}/github_association${check ? '?check=1' : ''}`);
            if (data.error) { const f = document.getElementById('ga-repo-flash'); if (f) { f.textContent = data.error; f.className = 'small mt-1 text-danger'; } return; }
            this._settingsGithubRender(data);
        },

        async _settingsSaveGithubRepo() {
            const proj = this._gaProject || window.PM_PROJECT || 'maxwell';
            const flash = document.getElementById('ga-repo-flash');
            const setFlash = (msg, cls) => { if (flash) { flash.textContent = msg; flash.className = `small mt-1 ${cls}`; } };
            const github_repo = (document.getElementById('ga-repo')?.value || '').trim();
            if (!github_repo) { setFlash('Enter a repo as owner/name.', 'text-danger'); return; }
            const btn = document.getElementById('ga-save'); if (btn) btn.disabled = true;
            setFlash('Saving…', 'text-secondary');
            try {
                await this._sSend(`api/projects/${encodeURIComponent(proj)}/github_repo`, 'POST', { github_repo });
                setFlash('Saved — now install the webhook below.', 'text-success');
                await this._settingsGithubLoad();
            } catch (e) { setFlash(e.message || 'Failed to save repo.', 'text-danger'); }
            finally { if (btn) btn.disabled = false; }
        },

        async _settingsVerifyGithub() {
            const btn = document.getElementById('ga-verify');
            const flash = document.getElementById('ga-verify-flash');
            if (flash) { flash.textContent = 'Checking…'; flash.className = 'small text-secondary me-auto'; }
            if (btn) btn.disabled = true;
            await this._settingsGithubLoad(true);
            const badge = document.getElementById('ga-status');
            const connected = badge && /Connected/.test(badge.textContent || '');
            if (flash) {
                flash.textContent = connected
                    ? 'Delivery received — you’re connected.'
                    : 'No delivery yet. Push a commit or merge a PR, then verify again.';
                flash.className = `small me-auto ${connected ? 'text-success' : 'text-secondary'}`;
            }
            if (btn) btn.disabled = false;
        },

        _settingsCopyField(id) {
            const src = document.getElementById(id);
            if (!src) return;
            src.select();
            if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(src.value).catch(() => { try { document.execCommand('copy'); } catch (e) { /* noop */ } });
            else { try { document.execCommand('copy'); } catch (e) { /* noop */ } }
        },

        // UI-20 (2/6): the UI-4 scoped-token modal folded in-place. This is the canonical
        // home for Switchboard access tokens — the legacy #apikeys-modal and its rail button
        // are retired. Labelled "Switchboard access tokens" so they cannot be confused with
        // model-provider API keys (those live under Personal AI accounts, UI-19).
        async _settingsTokensSection() {
            // Re-entering the section must never carry a prior visit's raw secret. The wipe
            // is also re-anchored onto the _settingsSelect panel swap (was hidden.bs.modal).
            this._clearApiKeySecret();
            const proj = window.PM_PROJECT || 'maxwell';
            const data = await this._sfetch(`api/access/tokens?project=${encodeURIComponent(proj)}`);
            // A non-admin (or expired session) gets 403/401 -> _sfetch returns {error}; the
            // section-level write:system gate normally shows the locked card first, so this is
            // the defensive fallback rather than the primary path.
            if (data.error) return this._settingsErrCard('Switchboard access tokens', data.error);
            this._tokensData = data;
            const defs = data.scope_definitions || {};
            const kinds = data.valid_kinds || ['agent'];
            const allScopes = [...new Set(Object.values(defs).flat())].sort();
            const kindOpts = kinds.map((k) => `<option ${k === 'agent' ? 'selected' : ''}>${this.esc(k)}</option>`).join('');
            const roleOpts = '<option value="">— custom —</option>' + Object.keys(defs).map((r) => `<option value="${this.esc(r)}">${this.esc(r)}</option>`).join('');
            const scopeBoxes = allScopes.map((s) => `<label class="form-check form-check-inline m-0"><input class="form-check-input settings-token-scope" type="checkbox" value="${this.esc(s)}"><span class="form-check-label font-monospace small">${this.esc(s)}</span></label>`).join('');
            const body = `<div class="alert alert-info py-2 px-3 small mb-3" role="note"><i class="ti ti-info-circle me-1" aria-hidden="true"></i>These are <strong>Switchboard access tokens</strong> for the control plane (MCP, REST, CI, hosts) — <strong>not</strong> model-provider API keys, which live under <a href="#tab-settings/ai-accounts">Personal AI accounts</a>.</div>
                <div id="apikeys-new-banner" class="alert alert-success" role="alert" style="display:none">
                    <div class="d-flex align-items-center"><i class="ti ti-alert-circle me-2" aria-hidden="true"></i><strong>Copy it now — this token is shown once and never again.</strong></div>
                    <div class="input-group input-group-sm mt-2">
                        <input id="apikeys-new-token" class="form-control font-monospace" readonly aria-label="New access token (shown once)">
                        <button class="btn btn-outline-secondary" type="button" data-set-action="tokens-copy" title="Copy token"><i class="ti ti-copy" aria-hidden="true"></i></button>
                    </div>
                </div>
                <div class="d-flex mb-2"><button class="btn btn-sm btn-primary" type="button" data-set-action="tokens-create-toggle"><i class="ti ti-plus me-1" aria-hidden="true"></i>Create token</button></div>
                <div id="apikeys-create-form" class="card mb-3" style="display:none"><div class="card-body">
                    <div class="row g-2">
                        <div class="col-md-6"><label class="form-label small mb-1">Name</label><input id="apikeys-name" class="form-control form-control-sm" placeholder="ci-mirror" autocomplete="off"></div>
                        <div class="col-md-3"><label class="form-label small mb-1">Kind</label><select id="apikeys-kind" class="form-select form-select-sm">${kindOpts}</select></div>
                        <div class="col-md-3"><label class="form-label small mb-1">Role preset</label><select id="settings-tokens-role" class="form-select form-select-sm">${roleOpts}</select></div>
                    </div>
                    <label class="form-label small mt-2 mb-1">Scopes <span class="text-secondary fw-normal">(least-privilege — pick only what this token needs)</span></label>
                    <div id="apikeys-scopes" class="d-flex flex-wrap gap-2">${scopeBoxes}</div>
                    <div class="mt-3 d-flex align-items-center gap-2"><button class="btn btn-sm btn-primary" type="button" data-set-action="tokens-create">Create token</button><span id="apikeys-create-flash" class="small text-secondary"></span></div>
                </div></div>
                <div id="apikeys-table">${this._settingsTokensTableHtml(data.tokens || [])}</div>`;
            return this._settingsCard({
                id: 'settings-tokens', title: 'Switchboard access tokens', icon: 'ti-key',
                subtitle: 'Scoped control-plane tokens for agents, hosts, and CI',
                body,
            });
        },

        _settingsTokensTableHtml(tokens) {
            if (!tokens.length) return '<div class="text-secondary small">No tokens yet. Create one above.</div>';
            const rows = tokens.map((t) => {
                const scopes = (t.scopes || []).map((s) => `<span class="badge bg-secondary-lt me-1 font-monospace">${this.esc(s)}</span>`).join('') || '<span class="text-secondary">—</span>';
                const created = t.created_at ? new Date(t.created_at * 1000).toLocaleDateString() : '—';
                return `<tr>
                    <td><div class="fw-medium">${this.esc(t.display_name || t.id)}</div><div class="text-secondary font-monospace" style="font-size:11px">${this.esc(t.id)}</div></td>
                    <td><span class="badge bg-secondary-lt">${this.esc(t.kind || '')}</span></td>
                    <td>${scopes}</td>
                    <td class="text-secondary small">${this.esc(created)}</td>
                    <td class="text-end"><button class="btn btn-sm btn-ghost-danger" type="button" data-set-action="tokens-revoke:${encodeURIComponent(t.id)}">Revoke</button></td>
                </tr>`;
            }).join('');
            return `<div class="table-responsive"><table class="table table-sm align-middle mb-0"><thead><tr><th>Name</th><th>Kind</th><th>Scopes</th><th>Created</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
        },

        // Re-fetch and re-render only the table, leaving the shown-once banner intact so the
        // freshly minted secret survives the post-create list refresh.
        async _settingsTokensReload() {
            const proj = window.PM_PROJECT || 'maxwell';
            const data = await this._sfetch(`api/access/tokens?project=${encodeURIComponent(proj)}`);
            if (data.error) return;
            this._tokensData = data;
            const el = document.getElementById('apikeys-table');
            if (el) el.innerHTML = this._settingsTokensTableHtml(data.tokens || []);
        },

        _settingsTokensApplyRole(role) {
            const defs = (this._tokensData && this._tokensData.scope_definitions) || {};
            const scopes = defs[role] || [];
            document.querySelectorAll('.settings-token-scope').forEach((cb) => { if (role) cb.checked = scopes.includes(cb.value); });
        },

        async _settingsCreateToken() {
            const proj = window.PM_PROJECT || 'maxwell';
            const flash = document.getElementById('apikeys-create-flash');
            const setFlash = (cls, msg) => { if (flash) { flash.className = 'small text-' + cls; flash.textContent = msg; } };
            const name = (document.getElementById('apikeys-name')?.value || '').trim();
            const kind = document.getElementById('apikeys-kind')?.value || 'agent';
            const scopes = [...document.querySelectorAll('.settings-token-scope:checked')].map((c) => c.value);
            if (!scopes.length) { setFlash('danger', 'Pick at least one scope.'); return; }
            setFlash('secondary', 'Creating…');
            try {
                const data = await this._sSend(`api/access/tokens?project=${encodeURIComponent(proj)}`, 'POST', { display_name: name || kind, kind, scopes });
                setFlash('secondary', '');
                const tok = document.getElementById('apikeys-new-token'); if (tok) tok.value = data.token || '';
                const banner = document.getElementById('apikeys-new-banner'); if (banner) banner.style.display = '';
                const nameEl = document.getElementById('apikeys-name'); if (nameEl) nameEl.value = '';
                const form = document.getElementById('apikeys-create-form'); if (form) form.style.display = 'none';
                await this._settingsTokensReload();
            } catch (e) { setFlash('danger', `Create failed: ${e.message}`); }
        },

        async _settingsRevokeToken(id) {
            if (!id) return;
            const proj = window.PM_PROJECT || 'maxwell';
            const tok = ((this._tokensData && this._tokensData.tokens) || []).find((t) => t.id === id);
            const name = (tok && (tok.display_name || tok.id)) || id;
            if (!window.confirm(`Revoke access token "${name}"?\nThis immediately and permanently disables it.`)) return;
            try {
                await this._sSend(`api/access/tokens/${encodeURIComponent(id)}/revoke?project=${encodeURIComponent(proj)}`, 'POST', {});
            } catch (e) { /* result reflected on reload */ }
            await this._settingsTokensReload();
        },

        _settingsTokensCopy() {
            const i = document.getElementById('apikeys-new-token');
            if (!i) return;
            i.select();
            if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(i.value).catch(() => {});
            else { try { document.execCommand('copy'); } catch (e) { /* noop */ } }
        },

        // Wipe the shown-once raw token from the DOM so "shown once" holds even against
        // devtools. UI-20 re-anchored this off the modal's hidden.bs.modal onto the Settings
        // panel swap (_settingsSelect) and section re-entry.
        _clearApiKeySecret() {
            const tok = document.getElementById('apikeys-new-token'); if (tok) tok.value = '';
            const banner = document.getElementById('apikeys-new-banner'); if (banner) banner.style.display = 'none';
        },

        /* ---- Operations ---------------------------------------------------- */

        // Fleet, capacity, and narration each already have a live operational surface.
        // Duplicating them here would mean two places to keep correct, so Settings
        // states the current posture and routes to the real one. UI-20 owns
        // rationalizing those docks.
        async _settingsFleetSection() {
            const proj = window.PM_PROJECT || 'maxwell';
            const data = await this._sfetch(`ixp/v1/agent_hosts?project=${encodeURIComponent(proj)}&include_stale=1`);
            if (data.error) return this._settingsErrCard('Fleet & runners', data.error);
            const hosts = data.hosts || [];
            const live = hosts.filter((h) => !h.stale);
            const rows = hosts.length ? hosts.slice(0, 10).map((h) => `<tr>
                <td><code>${this.esc(h.host_id || '—')}</code></td>
                <td class="small">${(h.runtimes || []).map((r) => this.esc(r.runtime || r.name || String(r))).join(', ') || '—'}</td>
                <td><span class="badge ${h.stale ? 'bg-secondary-lt' : 'bg-green-lt'}">${h.stale ? 'stale' : 'live'}</span></td>
                </tr>`).join('') : '<tr><td colspan="3" class="text-secondary text-center py-3">No agent hosts registered.</td></tr>';
            return this._settingsCard({
                id: 'settings-fleet', title: 'Fleet & runners', icon: 'ti-server-bolt',
                subtitle: 'Agent hosts, wake intents, and runner control',
                body: `<div class="mb-2"><span class="badge bg-green-lt me-1">${live.length} live</span><span class="badge bg-secondary-lt">${hosts.length - live.length} stale</span></div>
                    <div class="table-responsive"><table class="table table-vcenter table-sm mb-0"><thead><tr><th>Host</th><th>Runtimes</th><th>State</th></tr></thead><tbody>${rows}</tbody></table></div>`,
                footer: `<span class="text-secondary small">Live host and runner control lives on the Fleet screen.</span>
                    <button class="btn btn-primary btn-sm ms-auto" type="button" data-set-action="goto-fleet"><i class="ti ti-external-link me-1" aria-hidden="true"></i>Open Fleet</button>`,
            });
        },

        async _settingsCapacitySection() {
            const proj = window.PM_PROJECT || 'maxwell';
            const snap = await this._sfetch(`ixp/v1/saturation_signals?project=${encodeURIComponent(proj)}`);
            if (snap.error) return this._settingsErrCard('Capacity & box pressure', snap.error);
            const status = snap.status || 'healthy';
            const tone = { healthy: 'bg-green-lt', warning: 'bg-yellow-lt', critical: 'bg-red-lt' }[status] || 'bg-secondary-lt';
            const alerts = snap.alerts || [];
            const alertHtml = alerts.length
                ? `<div class="list-group list-group-flush">${alerts.map((a) => `<div class="list-group-item px-0 py-2">
                    <span class="badge ${{ high: 'bg-red-lt', medium: 'bg-yellow-lt' }[a.severity] || 'bg-secondary-lt'} me-2 text-uppercase">${this.esc(a.severity || '?')}</span>
                    ${this.esc(a.message || a.code || '')}</div>`).join('')}</div>`
                : '<div class="text-secondary small">No active saturation alerts.</div>';
            return this._settingsCard({
                id: 'settings-capacity', title: 'Capacity & box pressure', icon: 'ti-gauge',
                subtitle: 'PSI, lock waits, inbox depth, and SLO alerts',
                body: `<div class="mb-2">Pressure <span class="badge ${tone} text-uppercase">${this.esc(status)}</span>
                    · SLOs ${snap.slos_ok === false ? '<span class="text-danger fw-semibold">failing</span>' : '<span class="text-success fw-semibold">ok</span>'}</div>${alertHtml}`,
            });
        },

        async _settingsNarrationSection() {
            const health = await this._sfetch('api/narration/health');
            if (health.error) return this._settingsErrCard('Narration', health.error);
            const q = health.queue || {};
            const r = health.receipts || {};
            const flags = health.alerts || {};
            const firing = Object.keys(flags).filter((k) => flags[k]);
            return this._settingsCard({
                id: 'settings-narration', title: 'Narration', icon: 'ti-broadcast',
                subtitle: 'Narration queue depth, freshness, and delivery outcomes',
                body: `<div class="mb-2"><span class="badge ${health.alerting ? 'bg-yellow-lt' : 'bg-green-lt'}">${health.alerting ? 'attention' : 'healthy'}</span>
                    ${firing.length ? ` <span class="text-secondary small">alerts: ${this.esc(firing.join(', '))}</span>` : ''}</div>`
                    + this._settingsRows([
                        ['Queued', `${q.pending || 0} pending / ${q.retry_wait || 0} retry`],
                        ['Running', `${q.claimed || 0} claimed (${q.expired_leases || 0} lease-expired)`],
                        ['Dead letters', `${q.dead_letter || 0}`],
                        ['Oldest pending', `${Math.round((health.freshness || {}).oldest_pending_age_seconds || 0)}s`],
                        ['Outcomes', `${r.delivered || 0} ok / ${r.failed || 0} err / ${r.fallback || 0} fallback`],
                        ['Failure rate', `${Math.round((r.failure_rate || 0) * 100)}%`],
                    ]),
            });
        },

        async _settingsProvenanceSection() {
            const proj = window.PM_PROJECT || 'maxwell';
            const [ciRuns, pubs] = await Promise.all([
                this._sfetch(`ixp/v1/external_ci_runs?project=${encodeURIComponent(proj)}`),
                this._sfetch(`ixp/v1/publication_evidence?project=${encodeURIComponent(proj)}`),
            ]);
            return this._settingsReconcileCard() + this._settingsVerifyCard()
                + this._settingsCiRunsCard(ciRuns) + this._settingsPublicationCard(pubs);
        },

        async _settingsAdvancedSection() {
            const proj = window.PM_PROJECT || 'maxwell';
            const projects = await this._sfetch('api/projects?include_archived=1');
            this._settingsAdminProjects = (projects && projects.projects) || [];
            this._settingsProjectFilter = this._settingsProjectFilter || 'all';
            const candidates = this._paProjectsForFilter();
            this._settingsProjectId = this._settingsProjectId || (candidates.some((p) => p.id === proj) ? proj : (candidates[0]?.id || ''));
            const selected = this._settingsProjectId;
            const [detail, impact] = selected ? await Promise.all([
                this._sfetch(`api/projects/${encodeURIComponent(selected)}`),
                this._sfetch(`api/projects/${encodeURIComponent(selected)}/impact`),
            ]) : [{ error: 'No accessible projects match this lifecycle filter.' }, {}];
            this._settingsProjects = this._settingsAdminProjects.filter((p) => p.lifecycle_status !== 'archived');
            this._projectAdminSyncSwitcher();
            return this._projectAdminCard(detail, impact) + this._settingsMoveCard();
        },

        /* ---- settings helpers (shared with project-admin.js) --------------- */

    _sv(id) { return (document.getElementById(id)?.value || '').trim(); },
    _sFlash(id, msg, cls) { const el = document.getElementById(id); if (el) { el.textContent = msg || ''; el.className = `small ${cls || 'text-secondary'}`; } },
    async _sfetch(url) {
        try { const r = await fetch(url, { cache: 'no-store' }); const d = await r.json().catch(() => ({})); return r.ok ? d : { error: (d && (d.detail || d.error)) || `HTTP ${r.status}` }; }
        catch (e) { return { error: e.message }; }
    },
    async _sSend(url, method, body) {
        const opt = { method };
        if (body !== undefined) { opt.headers = { 'Content-Type': 'application/json' }; opt.body = JSON.stringify(body); }
        const res = await fetch(url, opt);
        let data = {}; try { data = await res.json(); } catch (e) { /* empty */ }
        if (!res.ok) {
            if (res.status === 403 || res.status === 401) throw new Error('Admin (write:system) access required.');
            const d = data && (data.detail || data.error);
            throw new Error(typeof d === 'string' ? d : (d && (d.error || d.message || d.hint)) || `HTTP ${res.status}`);
        }
        return data;
    },

    _settingsErrCard(title, err) {
        return `<div class="card mb-4"><div class="card-header"><h3 class="card-title">${this.esc(title)}</h3></div><div class="card-body"><div class="alert alert-danger mb-0">${this.esc(err)}</div></div></div>`;
    },
    _authorityChips(list) {
        return (list || []).map((a) => `<span class="badge bg-azure-lt me-1">${this.esc(a)}</span>`).join('') || '<span class="text-secondary small">—</span>';
    },

    _settingsRepoCard(topo, extraActions) {
        topo = topo || {};
        if (topo.error) return this._settingsErrCard('Repository & roles', topo.error);
        const roles = topo.roles || {};
        const order = ['canonical', 'public_ci', 'public', 'release'];
        const rows = order.map((k) => {
            const r = roles[k] || {};
            return `<tr>
                <td><span class="fw-semibold text-capitalize">${this.esc(k.replace('_', ' '))}</span></td>
                <td>${r.repo ? `<code>${this.esc(r.repo)}</code>` : '<span class="text-secondary">— not set —</span>'}</td>
                <td>${this.esc(r.default_branch || '—')}</td>
                <td>${this._authorityChips(r.authority)}</td>
                <td>${r.configured ? '<span class="badge bg-green-lt">configured</span>' : '<span class="badge bg-secondary-lt">unset</span>'}</td>
            </tr>`;
        }).join('');
        const warn = (topo.warnings || []).length ? `<div class="alert alert-warning py-2 px-3 small mt-2 mb-0">${(topo.warnings || []).map((w) => this.esc(w)).join('<br>')}</div>` : '';
        const c = roles.canonical || {}, ci = roles.public_ci || {}, pub = roles.public || {}, rel = roles.release || {};
        return `<div class="card mb-4"><div class="card-header"><h3 class="card-title"><i class="ti ti-git-branch me-2"></i>Repository &amp; roles</h3>
            <div class="card-actions btn-list"><span class="badge ${topo.valid ? 'bg-green-lt' : 'bg-red-lt'}">${topo.valid ? 'valid' : 'invalid'}</span>
            ${extraActions || ''}
            <button class="btn btn-sm btn-outline-secondary" type="button" data-set-action="repo-edit"><i class="ti ti-pencil me-1"></i>Edit topology</button></div></div>
            <div class="card-body">
            <div class="text-secondary small mb-2">Topology: <code>${this.esc(topo.topology_type || '—')}</code></div>
            <div class="table-responsive"><table class="table table-vcenter table-sm mb-0">
                <thead><tr><th>Role</th><th>Repo</th><th>Default branch</th><th>Authority</th><th></th></tr></thead>
                <tbody>${rows}</tbody></table></div>${warn}
            <form id="repo-edit-form" class="mt-3" style="display:none">
                <div class="row g-2">
                    <div class="col-md-6"><label class="form-label">Canonical repo <span class="text-secondary">(owner/name)</span></label><input id="rt-canonical" class="form-control" value="${this.esc(c.repo || '')}" placeholder="owner/name" autocomplete="off"></div>
                    <div class="col-md-6"><label class="form-label">Canonical default branch</label><input id="rt-branch" class="form-control" value="${this.esc(c.default_branch || '')}" placeholder="master" autocomplete="off"></div>
                    <div class="col-md-6"><label class="form-label">Public CI repo</label><input id="rt-ci" class="form-control" value="${this.esc(ci.repo || '')}" placeholder="owner/name" autocomplete="off"></div>
                    <div class="col-md-6"><label class="form-label">Public mirror repo</label><input id="rt-public" class="form-control" value="${this.esc(pub.repo || '')}" placeholder="owner/name" autocomplete="off"></div>
                    <div class="col-md-6"><label class="form-label">Release repo</label><input id="rt-release" class="form-control" value="${this.esc(rel.repo || '')}" placeholder="owner/name" autocomplete="off"></div>
                    <div class="col-md-6"><label class="form-label">Topology type</label><input id="rt-type" class="form-control" value="${this.esc(topo.topology_type || '')}" placeholder="private_canonical_public_ci" autocomplete="off"></div>
                </div>
                <div class="d-flex align-items-center mt-2"><span id="repo-flash" class="small text-secondary"></span>
                <button class="btn btn-primary btn-sm ms-auto" type="button" data-set-action="repo-save"><i class="ti ti-device-floppy me-1"></i>Save topology</button></div>
            </form></div></div>`;
    },

    _settingsReconcileCard() {
        return `<div class="card mb-4"><div class="card-header"><h3 class="card-title"><i class="ti ti-refresh-dot me-2"></i>Reconcile &amp; provenance drift</h3>
            <div class="card-actions btn-list">
                <select id="rc-severity" class="form-select form-select-sm" style="width:auto"><option value="high">high only</option><option value="medium" selected>medium +</option><option value="low">low +</option></select>
                <button class="btn btn-sm btn-outline-secondary" type="button" data-set-action="reconcile-alerts">Send alerts</button>
                <button class="btn btn-sm btn-primary" type="button" data-set-action="reconcile"><i class="ti ti-refresh me-1"></i>Reconcile now</button>
            </div></div>
            <div class="card-body"><span id="reconcile-flash" class="small text-secondary"></span>
            <div id="reconcile-results" class="mt-2"><div class="text-secondary small">Run reconcile to surface provenance drift — orphan merges, stale branches, missing evidence.</div></div>
            </div></div>`;
    },
    _reconcileFindingsHtml(rec) {
        rec = rec || {};
        const findings = rec.findings || [];
        const sevColor = { high: 'red', medium: 'yellow', low: 'secondary' };
        const stamp = rec.checked_at ? new Date(rec.checked_at * 1000).toLocaleTimeString() : '';
        const head = `<div class="d-flex align-items-center mb-2"><span class="badge ${rec.ok ? 'bg-green-lt' : 'bg-yellow-lt'} me-2">${rec.ok ? 'clean' : `${findings.length} finding${findings.length === 1 ? '' : 's'}`}</span>
            <span class="text-secondary small">checked ${stamp}${(rec.backfilled || []).length ? ` · backfilled ${rec.backfilled.length}` : ''}</span></div>`;
        if (!findings.length) return head + '<div class="text-secondary small">No provenance drift detected.</div>';
        return head + `<div class="list-group list-group-flush">${findings.map((f) => `<div class="list-group-item px-0"><div class="d-flex gap-2">
            <span class="badge bg-${sevColor[f.severity] || 'secondary'}-lt text-uppercase">${this.esc(f.severity || '?')}</span>
            <div><div><span class="fw-semibold">${this.esc(f.code || 'finding')}</span>${f.task_id ? ` · <code>${this.esc(f.task_id)}</code>` : ''}</div>
            <div class="text-secondary small">${this.esc(f.detail || '')}</div></div></div></div>`).join('')}</div>`;
    },

    _settingsVerifyCard() {
        return `<div class="card mb-4"><div class="card-header"><h3 class="card-title"><i class="ti ti-clipboard-check me-2"></i>Verify offline completion</h3></div>
            <div class="card-body"><div class="text-secondary small mb-3">Stamp a non-PR task Done with recorded evidence — verifier-attributed and audited.</div>
            <div class="row g-2">
                <div class="col-md-4"><label class="form-label">Task id</label><input id="vo-task" class="form-control text-uppercase" placeholder="e.g. HARDEN-44" autocomplete="off"></div>
                <div class="col-md-8"><label class="form-label">Artifact / evidence URL</label><input id="vo-url" class="form-control" placeholder="https://…" autocomplete="off"></div>
                <div class="col-md-6"><label class="form-label">Verifier <span class="text-secondary">(optional)</span></label><input id="vo-verifier" class="form-control" placeholder="defaults to you" autocomplete="off"></div>
                <div class="col-md-6"><label class="form-label">Evidence note</label><input id="vo-note" class="form-control" placeholder="what was verified" autocomplete="off"></div>
            </div>
            <div class="d-flex align-items-center mt-2"><span id="vo-flash" class="small text-secondary"></span>
            <button class="btn btn-primary btn-sm ms-auto" type="button" data-set-action="verify-offline"><i class="ti ti-check me-1"></i>Verify &amp; stamp Done</button></div>
            </div></div>`;
    },

    _settingsMoveCard() {
        const opts = (this._settingsProjects || []).map((p) => `<option value="${this.esc(p.id)}">${this.esc(p.label || p.id)}</option>`).join('');
        return `<div class="card mb-4"><div class="card-header"><h3 class="card-title"><i class="ti ti-arrows-transfer-up me-2"></i>Move task <span class="badge bg-red-lt ms-2">admin</span></h3></div>
            <div class="card-body"><div class="text-secondary small mb-3">Move a task out of <strong>${this.esc(window.PM_PROJECT || '')}</strong> into another project — cross-project and audited.</div>
            <div class="row g-2">
                <div class="col-md-4"><label class="form-label">Task id</label><input id="mv-task" class="form-control text-uppercase" placeholder="e.g. UI-3" autocomplete="off"></div>
                <div class="col-md-4"><label class="form-label">Destination project</label><select id="mv-to" class="form-select">${opts}</select></div>
                <div class="col-md-4"><label class="form-label">Cross-project deps</label><select id="mv-dep" class="form-select"><option value="fail" selected>fail if any</option><option value="clear">clear them</option></select></div>
                <div class="col-md-8"><label class="form-label">New task id <span class="text-secondary">(optional)</span></label><input id="mv-newid" class="form-control text-uppercase" placeholder="keep same id" autocomplete="off"></div>
            </div>
            <div class="d-flex align-items-center mt-2"><span id="mv-flash" class="small text-secondary"></span>
            <button class="btn btn-danger btn-sm ms-auto" type="button" data-set-action="move-task"><i class="ti ti-arrow-right me-1"></i>Move task</button></div>
            </div></div>`;
    },

    _ciStatusColor(status, conclusion) {
        const s = String(conclusion || status || '').toLowerCase();
        if (['success', 'passed'].includes(s)) return 'bg-green-lt';
        if (['failure', 'failed', 'error', 'cancelled'].includes(s)) return 'bg-red-lt';
        if (['running', 'triggered', 'requested', 'mirrored', 'pending'].includes(s)) return 'bg-blue-lt';
        return 'bg-secondary-lt';
    },
    _settingsCiRunsCard(data) {
        data = data || {};
        if (data.error) return this._settingsErrCard('External CI mirror runs', data.error);
        const runs = data.runs || [];
        const rows = runs.length ? runs.slice(0, 25).map((r) => `<tr>
            <td>${r.task_id ? `<code>${this.esc(r.task_id)}</code>` : '—'}</td>
            <td><span class="badge ${this._ciStatusColor(r.status, r.conclusion)}">${this.esc(r.conclusion || r.status || '?')}</span></td>
            <td class="font-monospace small">${this.esc((r.source_sha || '').slice(0, 8) || '—')}</td>
            <td class="small">${this.esc(r.mirror_repo || r.source_repo || '—')}</td>
            <td>${r.run_url ? `<a href="${this.esc(r.run_url)}" target="_blank" rel="noopener">run</a>` : '—'}</td>
        </tr>`).join('') : '<tr><td colspan="5" class="text-secondary text-center py-3">No external CI mirror runs.</td></tr>';
        return `<div class="card mb-4"><div class="card-header"><h3 class="card-title"><i class="ti ti-checkup-list me-2"></i>External CI mirror runs</h3>
            <div class="card-actions"><button class="btn btn-sm btn-outline-secondary" type="button" data-set-action="refresh"><i class="ti ti-refresh me-1"></i>Refresh</button></div></div>
            <div class="table-responsive"><table class="table table-vcenter card-table mb-0"><thead><tr><th>Task</th><th>Result</th><th>SHA</th><th>Repo</th><th></th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
    },
    _settingsPublicationCard(data) {
        data = data || {};
        if (data.error) return this._settingsErrCard('Publication evidence', data.error);
        const pubs = data.publication_evidence || [];
        const rows = pubs.length ? pubs.slice(0, 25).map((p) => `<tr>
            <td>${p.task_id ? `<code>${this.esc(p.task_id)}</code>` : '—'}</td>
            <td class="small">${this.esc(p.public_repo || '—')}</td>
            <td class="small">${this.esc(p.public_ref || p.public_tag || '—')}</td>
            <td class="font-monospace small">${this.esc((p.public_sha || '').slice(0, 8) || '—')}</td>
            <td>${p.artifact_url ? `<a href="${this.esc(p.artifact_url)}" target="_blank" rel="noopener">artifact</a>` : '—'}</td>
        </tr>`).join('') : '<tr><td colspan="5" class="text-secondary text-center py-3">No publication evidence recorded.</td></tr>';
        return `<div class="card mb-4"><div class="card-header"><h3 class="card-title"><i class="ti ti-file-certificate me-2"></i>Publication evidence</h3></div>
            <div class="table-responsive"><table class="table table-vcenter card-table mb-0"><thead><tr><th>Task</th><th>Public repo</th><th>Ref</th><th>SHA</th><th></th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
    },

    _settingsAction(action) {
        if (String(action || '').startsWith('project-')) return this._projectAdminAction(action);
        if (String(action || '').startsWith('tokens-revoke:')) return this._settingsRevokeToken(decodeURIComponent(action.slice('tokens-revoke:'.length)));
        if (String(action || '').startsWith('comms-rm:')) {
            const rest = action.slice('comms-rm:'.length);
            const i = rest.indexOf(':');
            return this._settingsCommsRemove(rest.slice(0, i), decodeURIComponent(rest.slice(i + 1)));
        }
        if (String(action || '').startsWith('comms-add:')) return this._settingsCommsAddRecipient(action.slice('comms-add:'.length));
        if (String(action || '').startsWith('comms-test:')) return this._settingsCommsTest(action.slice('comms-test:'.length));
        if (String(action || '').startsWith('members-revoke:')) {
            try { return this._settingsMembersRevoke(JSON.parse(decodeURIComponent(action.slice('members-revoke:'.length)))); }
            catch (e) { return; }
        }
        if (String(action || '').startsWith('github-copy:')) return this._settingsCopyField(action.slice('github-copy:'.length));
        if (String(action || '').startsWith('ai-accounts-copy-cmd:')) return this._settingsAiAccountsCopyCommand(action.slice('ai-accounts-copy-cmd:'.length));
        if (String(action || '').startsWith('ai-accounts-connect:')) return this._settingsAiAccountsConnect(action.slice('ai-accounts-connect:'.length));
        if (String(action || '').startsWith('ai-accounts-verify:')) return this._settingsAiAccountsVerify(action.slice('ai-accounts-verify:'.length));
        if (String(action || '').startsWith('ai-accounts-reconnect:')) return this._settingsAiAccountsReconnect(action.slice('ai-accounts-reconnect:'.length));
        if (String(action || '').startsWith('ai-accounts-revoke:')) return this._settingsAiAccountsRevoke(action.slice('ai-accounts-revoke:'.length));
        if (String(action || '').startsWith('ai-accounts-delete:')) return this._settingsAiAccountsDelete(action.slice('ai-accounts-delete:'.length));
        if (String(action || '').startsWith('api-connections-copy-cmd:')) return this._settingsApiConnectionsCopyCommand(action.slice('api-connections-copy-cmd:'.length));
        if (String(action || '').startsWith('api-connections-revoke:')) return this._settingsApiConnectionsRevoke(action.slice('api-connections-revoke:'.length));
        if (String(action || '').startsWith('api-connections-delete:')) return this._settingsApiConnectionsDelete(action.slice('api-connections-delete:'.length));
        switch (action) {
            case 'repo-edit': { const f = document.getElementById('repo-edit-form'); if (f) f.style.display = f.style.display === 'none' ? '' : 'none'; return; }
            case 'repo-save': return this.saveRepoTopology();
            case 'reconcile': return this.reconcileNow();
            case 'reconcile-alerts': return this.sendReconcileAlerts();
            case 'verify-offline': return this.verifyOffline();
            case 'move-task': return this.moveTask();
            case 'refresh': return this.renderSettings();
            // Access tokens (2/6), Communications (3/6), Members (4/6), and GitHub (5/6) are
            // all folded inline now; no launcher into a legacy modal remains.
            case 'comms-add-domain': return this._settingsCommsAddDomain();
            case 'comms-copy': return this._settingsCommsCopyPlus();
            case 'comms-save': return this._settingsCommsSave();
            case 'members-add': return this._settingsMembersAdd();
            case 'members-refresh': return this._settingsMembersReload();
            case 'github-save': return this._settingsSaveGithubRepo();
            case 'github-verify': return this._settingsVerifyGithub();
            case 'profile-change-password': return this._settingsChangePassword();
            case 'tokens-create-toggle': { const f = document.getElementById('apikeys-create-form'); if (f) f.style.display = f.style.display === 'none' ? '' : 'none'; return; }
            case 'tokens-create': return this._settingsCreateToken();
            case 'tokens-copy': return this._settingsTokensCopy();
            case 'goto-fleet': { if (window.TAIKUN_showTab) window.TAIKUN_showTab('#tab-fleet'); return; }
        }
    },
    async saveRepoTopology() {
        const proj = window.PM_PROJECT;
        const body = {
            canonical_repo: this._sv('rt-canonical'), canonical_default_branch: this._sv('rt-branch'),
            public_ci_repo: this._sv('rt-ci'), public_repo: this._sv('rt-public'),
            release_repo: this._sv('rt-release'), topology_type: this._sv('rt-type'),
        };
        this._sFlash('repo-flash', 'Saving…', 'text-secondary');
        try {
            await this._sSend(`api/projects/${encodeURIComponent(proj)}/repo_topology`, 'POST', body);
            this._sFlash('repo-flash', 'Saved — refreshing…', 'text-success');
            await this.renderSettings();
        } catch (e) { this._sFlash('repo-flash', e.message, 'text-danger'); }
    },
    async reconcileNow() {
        const proj = window.PM_PROJECT;
        this._sFlash('reconcile-flash', 'Reconciling…', 'text-secondary');
        const res = document.getElementById('reconcile-results');
        try {
            const rec = await this._sSend(`ixp/v1/reconcile?project=${encodeURIComponent(proj)}`, 'GET');
            this._sFlash('reconcile-flash', '', 'text-secondary');
            if (res) res.innerHTML = this._reconcileFindingsHtml(rec);
        } catch (e) { this._sFlash('reconcile-flash', e.message, 'text-danger'); }
    },
    async sendReconcileAlerts() {
        const proj = window.PM_PROJECT;
        const sev = document.getElementById('rc-severity')?.value || 'medium';
        this._sFlash('reconcile-flash', 'Sending alerts…', 'text-secondary');
        try {
            const r = await this._sSend('ixp/v1/reconcile_alerts', 'POST', { project: proj, min_severity: sev });
            const msg = r.alert_sent ? `Alert sent · ${r.finding_count} finding(s)` : (r.deduped ? 'Deduped — a recent alert already covers this' : `No alert · ${r.finding_count || 0} finding(s) ≥ ${sev}`);
            this._sFlash('reconcile-flash', msg, r.alert_sent ? 'text-success' : 'text-secondary');
        } catch (e) { this._sFlash('reconcile-flash', e.message, 'text-danger'); }
    },
    async verifyOffline() {
        const task = this._sv('vo-task').toUpperCase();
        if (!task) { this._sFlash('vo-flash', 'Enter a task id.', 'text-danger'); return; }
        const body = { artifact_url: this._sv('vo-url'), verifier: this._sv('vo-verifier') };
        const note = this._sv('vo-note');
        if (note) body.evidence = { note };
        this._sFlash('vo-flash', 'Verifying…', 'text-secondary');
        try {
            const r = await this._sSend(`api/tasks/${encodeURIComponent(task)}/verify_offline`, 'POST', body);
            this._sFlash('vo-flash', `${task} → ${r.status || 'Done'}${r.idempotent ? ' (already Done)' : ''}`, 'text-success');
        } catch (e) { this._sFlash('vo-flash', e.message, 'text-danger'); }
    },
    async moveTask() {
        const task = this._sv('mv-task').toUpperCase();
        const to = document.getElementById('mv-to')?.value || '';
        if (!task || !to) { this._sFlash('mv-flash', 'Task id and destination are required.', 'text-danger'); return; }
        if (to === window.PM_PROJECT) { this._sFlash('mv-flash', 'Destination is the current project.', 'text-danger'); return; }
        if (!confirm(`Move ${task} from ${window.PM_PROJECT} to ${to}? This is audited.`)) return;
        const body = { project_to: to, dependency_policy: document.getElementById('mv-dep')?.value || 'fail' };
        const newid = this._sv('mv-newid').toUpperCase();
        if (newid) body.new_task_id = newid;
        this._sFlash('mv-flash', 'Moving…', 'text-secondary');
        try {
            const r = await this._sSend(`api/tasks/${encodeURIComponent(task)}/move`, 'POST', body);
            // _sFlash writes via textContent, so pass raw values (esc() would double-encode).
            this._sFlash('mv-flash', `Moved to ${r.project_to || to} as ${r.new_task_id || r.task_id || task}`, 'text-success');
        } catch (e) { this._sFlash('mv-flash', e.message, 'text-danger'); }
    },
    };

    window.SwitchboardSettings = Object.freeze({ methods, SECTIONS });
})();
