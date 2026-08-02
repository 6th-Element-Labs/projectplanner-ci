/* Rooms: a task-centered projection over Switchboard's existing coordination record. */
(function (global) {
    'use strict';

    function project() {
        return (global.PM_PROJECT || '').trim() || 'switchboard';
    }

    function isSystemMessage(message) {
        return message.signal === 'reconcile_alert'
            || /^switchboard\//.test(message.from_agent || '');
    }

    function initials(value) {
        const parts = String(value || 'agent').split(/[^A-Za-z0-9]+/).filter(Boolean);
        return (parts.slice(0, 2).map((part) => part.charAt(0)).join('') || 'A').toUpperCase();
    }

    function timeAgo(timestamp) {
        if (!timestamp) return '';
        const seconds = Math.max(0, Date.now() / 1000 - Number(timestamp));
        if (seconds < 60) return 'now';
        if (seconds < 3600) return Math.floor(seconds / 60) + 'm';
        if (seconds < 86400) return Math.floor(seconds / 3600) + 'h';
        return Math.floor(seconds / 86400) + 'd';
    }

    function timestampOf(item) {
        return Number(item.sent_at || item.created_at || item.updated_at || 0);
    }

    const methods = {
        async initRooms(force) {
            if (this._roomsBusy) return;
            if (this._roomsLoaded && !force) return;
            this._roomsBusy = true;
            const list = document.getElementById('rooms-list');
            if (list && !this._roomsLoaded) {
                list.innerHTML = '<div class="text-secondary small p-3"><span class="spinner-border spinner-border-sm me-2"></span>Loading rooms…</div>';
            }
            try {
                const response = await fetch('api/coordination?project=' + encodeURIComponent(project()), {
                    credentials: 'same-origin', cache: 'no-cache',
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok) throw new Error(data.detail || ('HTTP ' + response.status));
                this._roomsData = data;
                this._roomsLoaded = true;
                this.renderRooms();
                this._wireRooms();
                if (!this._roomsPoll) {
                    this._roomsPoll = setInterval(() => {
                        const tab = document.getElementById('tab-rooms');
                        if (tab && tab.classList.contains('active') && !document.hidden) {
                            this._roomsLoaded = false;
                            this.initRooms(true);
                        }
                    }, 15000);
                }
            } catch (error) {
                if (list) list.innerHTML = '<div class="text-danger small p-3"><i class="ti ti-alert-circle me-1"></i>' + this.esc(error.message || 'Could not load rooms.') + '</div>';
            } finally {
                this._roomsBusy = false;
            }
        },

        _wireRooms() {
            if (this._roomsWired) return;
            this._roomsWired = true;
            const refresh = document.getElementById('rooms-refresh');
            if (refresh) refresh.addEventListener('click', () => {
                this._roomsLoaded = false;
                this.initRooms(true);
            });
            const create = document.getElementById('rooms-new');
            if (create) create.addEventListener('click', () => document.getElementById('btn-new-task')?.click());
            const back = document.getElementById('rooms-back');
            if (back) back.addEventListener('click', () => document.getElementById('rooms-workspace')?.classList.remove('show-room'));
            const details = document.getElementById('room-details-toggle');
            if (details) details.addEventListener('click', () => {
                const panel = document.getElementById('room-details');
                const open = !!panel?.hidden;
                if (panel) panel.hidden = !open;
                details.setAttribute('aria-expanded', open ? 'true' : 'false');
            });
            const composer = document.getElementById('room-composer');
            if (composer) composer.addEventListener('submit', (event) => {
                event.preventDefault();
                this.sendRoomMessage();
            });
        },

        _roomsModel() {
            const data = this._roomsData || {};
            const agents = data.agents || [];
            const messages = (data.messages || []).filter((message) => !isSystemMessage(message));
            const rawDecisions = (data.decisions || []).concat(data.coordinator_decisions || []);
            const decisions = [];
            const seenDecisions = new Set();
            rawDecisions.forEach((decision) => {
                const key = String(decision.decision_id || decision.id || JSON.stringify([
                    decision.task_id, decision.created_at, decision.title, decision.decision,
                ]));
                if (!seenDecisions.has(key)) { seenDecisions.add(key); decisions.push(decision); }
            });
            const tasks = new Map((this.tasks || []).map((task) => [String(task.task_id), task]));
            const rooms = new Map();
            const roomFor = (taskId) => {
                const key = String(taskId || '__project__');
                if (!rooms.has(key)) {
                    rooms.set(key, { key, taskId: taskId || '', task: tasks.get(String(taskId || '')) || null,
                        agents: [], messages: [], decisions: [], participants: new Set(), lastAt: 0 });
                }
                return rooms.get(key);
            };
            agents.forEach((agent) => {
                const room = roomFor(agent.task_id || '');
                room.agents.push(agent);
                if (agent.agent_id) room.participants.add(agent.agent_id);
                room.lastAt = Math.max(room.lastAt, Number(agent.heartbeat_at || 0));
            });
            messages.forEach((message) => {
                const room = roomFor(message.task_id || '');
                room.messages.push(message);
                if (message.from_agent) room.participants.add(message.from_agent);
                if (message.to_agent) room.participants.add(message.to_agent);
                room.lastAt = Math.max(room.lastAt, timestampOf(message));
            });
            // Decisions enrich a real collaboration room; they do not create hundreds of
            // historical pseudo-rooms on their own. Resolved choices remain in Inbox and
            // the coordination record, while Rooms surfaces only choices needing action.
            decisions.filter((decision) => ['open', 'pending', 'proposed', 'needs_input']
                .includes(String(decision.status || '').toLowerCase())).forEach((decision) => {
                const key = String(decision.task_id || '__project__');
                const room = rooms.get(key);
                if (!room) return;
                room.decisions.push(decision);
                if (decision.author) room.participants.add(decision.author);
                if (decision.coordinator_agent_id) room.participants.add(decision.coordinator_agent_id);
                room.lastAt = Math.max(room.lastAt, timestampOf(decision));
            });
            if (!rooms.size) roomFor('');
            const recentCutoff = Date.now() / 1000 - (14 * 86400);
            const visibleRooms = Array.from(rooms.values())
                .filter((room) => room.agents.length || room.lastAt >= recentCutoff)
                .sort((a, b) => b.lastAt - a.lastAt || a.key.localeCompare(b.key))
                .slice(0, 25);
            if (!visibleRooms.length) visibleRooms.push(roomFor(''));
            visibleRooms.forEach((room) => { room.messages = room.messages.slice(-50); });
            return {
                agents,
                rooms: visibleRooms,
            };
        },

        renderRooms() {
            const model = this._roomsModel();
            this._roomModel = model;
            const projectLabel = document.getElementById('rooms-project-label');
            const selectedProject = document.querySelector('#project-switcher option:checked');
            if (projectLabel) projectLabel.textContent = selectedProject?.textContent || project();
            const presence = document.getElementById('rooms-presence-count');
            if (presence) presence.textContent = model.agents.length + ' active';
            const presenceDot = document.getElementById('rooms-presence-dot');
            if (presenceDot) presenceDot.className = 'status-dot ' + (model.agents.length ? 'bg-green status-dot-animated' : 'bg-secondary');
            const summary = document.getElementById('rooms-list-summary');
            if (summary) summary.textContent = model.rooms.length + (model.rooms.length === 1 ? ' room' : ' rooms');
            ['rooms-nav-count', 'mobile-rooms-count'].forEach((id) => {
                const badge = document.getElementById(id);
                if (!badge) return;
                badge.textContent = String(model.rooms.length);
                badge.classList.toggle('d-none', !model.rooms.length);
            });
            if (!model.rooms.some((room) => room.key === this._selectedRoomKey)) {
                this._selectedRoomKey = model.rooms[0]?.key || '';
            }
            const list = document.getElementById('rooms-list');
            if (list) {
                list.innerHTML = model.rooms.map((room) => {
                    const latestMessage = room.messages[room.messages.length - 1];
                    const latestDecision = room.decisions[room.decisions.length - 1];
                    const pending = room.decisions.some((decision) => ['open', 'pending', 'proposed', 'needs_input'].includes(String(decision.status || '').toLowerCase()));
                    const preview = latestMessage?.message || latestDecision?.title || latestDecision?.decision
                        || (room.agents.length ? room.agents.length + ' active in this work' : 'No conversation yet');
                    const title = room.taskId ? '# ' + room.taskId.toLowerCase() : '# project';
                    return '<button class="tk-room-list-item' + (room.key === this._selectedRoomKey ? ' active' : '') + '" type="button" data-room-key="' + this.esc(room.key) + '">'
                        + '<span class="tk-room-list-row">' + (pending ? '<span class="tk-room-unread"></span>' : '')
                        + '<span class="tk-room-list-name">' + this.esc(title) + '</span><span class="tk-room-list-time">' + this.esc(timeAgo(room.lastAt)) + '</span></span>'
                        + '<span class="tk-room-list-preview">' + this.esc(preview) + '</span></button>';
                }).join('');
                list.querySelectorAll('[data-room-key]').forEach((button) => button.addEventListener('click', () => this.selectRoom(button.getAttribute('data-room-key'))));
            }
            this._renderSelectedRoom();
        },

        selectRoom(key) {
            this._selectedRoomKey = key;
            document.querySelectorAll('[data-room-key]').forEach((button) => button.classList.toggle('active', button.getAttribute('data-room-key') === key));
            this._renderSelectedRoom();
            document.getElementById('rooms-workspace')?.classList.add('show-room');
        },

        _renderSelectedRoom() {
            const room = this._roomModel?.rooms.find((item) => item.key === this._selectedRoomKey);
            const empty = document.getElementById('room-empty');
            const content = document.getElementById('room-content');
            if (!room) {
                if (empty) empty.hidden = false;
                if (content) content.hidden = true;
                return;
            }
            if (empty) empty.hidden = true;
            if (content) content.hidden = false;
            const task = room.task || {};
            const title = room.taskId ? '# ' + room.taskId.toLowerCase() : '# project';
            document.getElementById('room-title').textContent = title;
            document.getElementById('room-topic').textContent = task.title || 'Project-wide coordination';
            this._renderRoomMembers(room);
            this._renderRoomDetails(room);
            this._renderRoomArtifact(room);
            this._renderRoomFeed(room);
            this._renderRoomRecipients(room);
        },

        _renderRoomMembers(room) {
            const members = Array.from(room.participants);
            const active = new Set(room.agents.map((agent) => agent.agent_id));
            const target = document.getElementById('room-members');
            if (!target) return;
            const stack = members.slice(0, 4).map((member) => '<span class="tk-room-avatar' + (active.has(member) ? ' agent' : '') + '" title="' + this.esc(member) + '">' + this.esc(initials(member)) + '</span>').join('');
            const activeCopy = room.agents.length ? room.agents.length + ' active' : 'No active agents';
            target.innerHTML = '<span class="tk-room-avatar-stack">' + stack + '</span><span>' + this.esc(activeCopy + (members.length ? ' · ' + members.length + ' participants' : '')) + '</span>';
        },

        _renderRoomDetails(room) {
            const task = room.task || {};
            const people = Array.from(room.participants).join(', ') || 'No participants recorded';
            const active = room.agents.map((agent) => (agent.agent_id || 'agent') + (agent.runtime ? ' · ' + agent.runtime : '')).join(', ') || 'No active work';
            const completion = [task.status, task.milestone || task.phase, task.owner_person_or_role || task.assignee].filter(Boolean).join(' · ') || 'No task gate recorded';
            const target = document.getElementById('room-details');
            if (!target) return;
            target.innerHTML = this._roomDetail('People & agents', people)
                + this._roomDetail('Active work', active)
                + this._roomDetail('Completion', completion);
        },

        _roomDetail(label, value) {
            return '<div><div class="tk-room-detail-label">' + this.esc(label) + '</div><div class="tk-room-detail-value">' + this.esc(value) + '</div></div>';
        },

        _renderRoomArtifact(room) {
            const task = room.task || {};
            const target = document.getElementById('room-artifact');
            if (!target) return;
            const status = String(task.status || 'Coordination');
            const color = this.STATUS_COLOR?.[status] || 'secondary';
            const meta = [task._wsName || task.phase, task.owner_person_or_role || task.assignee || task.owner_org].filter(Boolean);
            target.innerHTML = '<div class="tk-room-artifact-kicker"><i class="ti ti-file-text"></i>' + this.esc(room.taskId ? 'Shared task' : 'Shared project record') + '</div>'
                + '<div class="tk-room-artifact-title">' + this.esc(task.title || 'Project coordination') + '</div>'
                + '<div class="tk-room-artifact-meta"><span class="badge bg-' + this.esc(color) + '-lt">' + this.esc(status) + '</span>'
                + meta.map((part) => '<span>' + this.esc(part) + '</span>').join('<span>·</span>') + '</div>'
                + (task.description ? '<div class="tk-room-artifact-copy">' + this.esc(task.description) + '</div>' : '')
                + (room.taskId ? '<div class="tk-room-artifact-actions"><button class="btn btn-sm btn-outline-secondary" type="button" data-open-room-task="' + this.esc(room.taskId) + '"><i class="ti ti-external-link me-1"></i>Open task details</button></div>' : '');
            target.querySelector('[data-open-room-task]')?.addEventListener('click', () => this.openNodeModal(room.taskId));
        },

        _renderRoomFeed(room) {
            const items = [];
            room.messages.forEach((message) => items.push({ kind: 'message', at: timestampOf(message), value: message }));
            room.decisions.forEach((decision) => items.push({ kind: 'decision', at: timestampOf(decision), value: decision }));
            items.sort((a, b) => a.at - b.at);
            const target = document.getElementById('room-feed');
            if (!target) return;
            if (!items.length) {
                target.innerHTML = '<div class="tk-room-feed-empty">No messages or decisions are recorded in this room yet.</div>';
                return;
            }
            target.innerHTML = items.map((item) => item.kind === 'message'
                ? this._roomMessageHtml(item.value) : this._roomDecisionHtml(item.value)).join('');
            target.querySelectorAll('[data-room-open-inbox]').forEach((button) => button.addEventListener('click', () => {
                document.getElementById('toptab-inbox')?.click();
                setTimeout(() => document.querySelector('.tk-inbox-tabs a[href="#tab-decisions"]')?.click(), 50);
            }));
        },

        _roomMessageHtml(message) {
            const sender = message.from_agent || 'agent';
            const ack = message.requires_ack
                ? '<span class="badge bg-' + (message.acked_at ? 'green' : 'yellow') + '-lt">' + (message.acked_at ? 'acknowledged' : 'awaiting ack') + '</span>' : '';
            return '<div class="tk-room-message"><span class="tk-room-message-avatar agent">' + this.esc(initials(sender)) + '</span><div>'
                + '<div class="tk-room-message-head"><strong>' + this.esc(sender) + '</strong><span class="tk-room-message-time">' + this.esc(timeAgo(message.sent_at)) + '</span></div>'
                + '<div class="tk-room-message-copy">' + this.esc(message.message || '') + '</div>'
                + '<div class="tk-room-message-meta"><span>to ' + this.esc(message.to_agent || '—') + '</span>' + ack + (message.signal ? '<span class="badge bg-secondary-lt">' + this.esc(message.signal) + '</span>' : '') + '</div>'
                + '</div></div>';
        },

        _roomDecisionHtml(decision) {
            const status = String(decision.status || 'recorded').toLowerCase();
            const resolved = ['accepted', 'resolved', 'done', 'closed'].includes(status);
            const title = decision.title || decision.decision_kind || 'Decision';
            const copy = decision.decision || decision.rationale || '';
            return '<div class="tk-room-decision' + (resolved ? ' resolved' : '') + '"><div class="tk-room-decision-label">Decision · ' + this.esc(status.replace(/_/g, ' ')) + '</div>'
                + '<div class="tk-room-decision-title">' + this.esc(title) + '</div>'
                + (copy ? '<div class="tk-room-decision-copy">' + this.esc(copy) + '</div>' : '')
                + (!resolved ? '<div class="tk-room-decision-actions"><button class="btn btn-sm btn-outline-secondary" type="button" data-room-open-inbox>Open in Inbox</button></div>' : '') + '</div>';
        },

        _renderRoomRecipients(room) {
            // Sending is a live operation, so offer only current presence identities.
            // Historical participants remain visible in the record but are not presented
            // as reachable recipients.
            const activeAgents = room.agents.map((agent) => agent.agent_id).filter(Boolean);
            const recipients = Array.from(new Set(activeAgents));
            const select = document.getElementById('room-recipient');
            const send = document.getElementById('room-send');
            const message = document.getElementById('room-message');
            if (!select) return;
            select.innerHTML = recipients.length
                ? recipients.map((agent) => '<option value="' + this.esc(agent) + '">' + this.esc(agent + (activeAgents.includes(agent) ? ' · active' : '')) + '</option>').join('')
                : '<option value="">No agent available</option>';
            if (send) send.disabled = !recipients.length;
            if (message) message.disabled = !recipients.length;
        },

        async sendRoomMessage() {
            const room = this._roomModel?.rooms.find((item) => item.key === this._selectedRoomKey);
            const input = document.getElementById('room-message');
            const recipient = document.getElementById('room-recipient')?.value || '';
            const button = document.getElementById('room-send');
            const note = document.getElementById('room-compose-note');
            const message = (input?.value || '').trim();
            if (!room || !recipient || !message) return;
            if (button) button.disabled = true;
            if (note) { note.className = 'tk-room-compose-note text-secondary'; note.textContent = 'Sending to ' + recipient + '…'; }
            try {
                const response = await fetch('api/agent_messages/send?project=' + encodeURIComponent(project()), {
                    method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ project: project(), to_agent: recipient, task_id: room.taskId || undefined, message }),
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok) throw new Error(data.detail || data.error || ('HTTP ' + response.status));
                if (input) input.value = '';
                if (note) {
                    const state = String(data.delivery_status || 'mailbox stored').replace(/_/g, ' ');
                    note.className = 'tk-room-compose-note text-success';
                    note.textContent = 'Sent · ' + state;
                }
                this._roomsLoaded = false;
                await this.initRooms(true);
            } catch (error) {
                if (note) { note.className = 'tk-room-compose-note text-danger'; note.textContent = error.message || 'Could not send message.'; }
            } finally {
                if (button) button.disabled = false;
            }
        },
    };

    global.SwitchboardRooms = Object.freeze({ methods });
})(window);
