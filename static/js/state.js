/* ARCH-MS-21: fresh application state for each app bootstrap. */
(function (global) {
    'use strict';
    function createPlanState() {
        return {
    plan: null,
    tasks: [],          // flattened: every task + _wsId / _wsName
    tally: null,        // project-level spend/outcome/KPI rollup
    projectContext: null, // project hierarchy + repo role guide from api/board
    deliverables: [],
    missionStatus: null,
    selectedDeliverableId: '',
    missionKpis: [],        // UI-2: project KPIs with rollup (tiles)
    missionOutcomes: [],    // UI-2: outcomes-to-verify queue
    missionGraph: null,
    _missionDagRenderId: 0,
    _missionPollMs: 5000,   // live cockpit poll interval (ms) while the tab is visible
    _hiddenPollMs: 20000,   // slower cadence for BACKGROUNDED tabs — stays live without hammering the server when many tabs are open
    _missionLiveTimer: null,
    _missionSig: null,
    _fleetPollMs: 10000,
    _fleetLiveTimer: null,
    _fleetSig: null,
    _fleetLoadBusy: false,
    _boardPollMs: 10000,   // live board (kanban) refresh interval while the tab is visible
    _boardLiveTimer: null,
    _boardSig: null,
    _boardLiveBusy: false,
    wsMeta: {},         // workstream_id -> {name, lead_org}
    gantt: null,        // ApexCharts instance
    ganttMode: 'task',  // default 'task' (per-task detail) · 'workstream' = 12-bar overview

    PHASES: ['Kickoff', 'Bootstrap', 'Build', 'Cutover', 'Operate'],
    PHASE_COLOR: { Kickoff: 'azure', Bootstrap: 'purple', Build: 'blue', Cutover: 'orange', Operate: 'green',
                   'Wave 1': 'azure', 'Wave 2': 'blue', 'Wave 3': 'orange', 'Wave 4': 'green' },
    PHASE_HEX: { Kickoff: '#4299e1', Bootstrap: '#ae3ec9', Build: '#066fd1', Cutover: '#f76707', Operate: '#2fb344',
                 'Wave 1': '#4299e1', 'Wave 2': '#066fd1', 'Wave 3': '#f76707', 'Wave 4': '#2fb344' },
    OWNER_COLOR: { 'Taikun': 'blue', 'TEEP': 'teal', 'Sensirion/Nubo': 'orange', 'IFS Merrick': 'purple', 'Joint': 'cyan' },
    RISK_COLOR: { Low: 'green', Medium: 'yellow', High: 'red' },
    STATUS_COLOR: { 'Not Started': 'secondary', 'In Progress': 'blue', 'In Review': 'yellow', 'Blocked': 'red', 'Done': 'green' },
    DELIVERABLE_STATUS_COLOR: {
        proposed: 'secondary', approved: 'azure', in_progress: 'blue', blocked: 'red',
        in_review: 'yellow', done: 'green', archived: 'secondary',
    },
    MILESTONE_STATUS_COLOR: {
        not_started: 'secondary', in_progress: 'blue', blocked: 'red',
        in_review: 'azure', done: 'green', skipped: 'secondary',
    },
    // UI-1: authoring vocab — kept in sync with store.py (DELIVERABLE_MILESTONE_STATUSES
    // and link_task_to_deliverable's role auto-classifier).
    MILESTONE_STATUSES: ['not_started', 'in_progress', 'blocked', 'in_review', 'done', 'skipped'],
    DELIVERABLE_LINK_ROLES: ['contributes', 'implementation', 'acceptance', 'foundation', 'parked'],
    WS_COLOR: {
        SEN: 'azure', FMP: 'blue', SCADA: 'cyan', IFS: 'teal', SSO: 'indigo', BEDROCK: 'purple',
        GW: 'pink', REG: 'lime', AGENT: 'orange', REPORT: 'yellow', DATA: 'green', CUTOVER: 'red'
    },
    WS_HEX: {
        SEN: '#4299e1', FMP: '#066fd1', SCADA: '#17a2b8', IFS: '#0ca678', SSO: '#4263eb', BEDROCK: '#ae3ec9',
        GW: '#d6336c', REG: '#74b816', AGENT: '#f76707', REPORT: '#f59f00', DATA: '#2fb344', CUTOVER: '#d63939'
    },
    OWNER_ORGS: ['Taikun', 'TEEP', 'Sensirion/Nubo', 'IFS Merrick', 'Joint'],
    // Drives the edit + create forms, reading, and applying agent proposals.
    EDIT_FIELDS: [
        { k: 'title', label: 'Title', type: 'text', col: 'col-12' },
        { k: 'description', label: 'Description', type: 'textarea', col: 'col-12' },
        { k: 'phase', label: 'Phase', type: 'select', opts: ['Kickoff', 'Bootstrap', 'Build', 'Cutover', 'Operate'], col: 'col-6 col-md-3' },
        { k: 'status', label: 'Status', type: 'select', opts: ['Not Started', 'In Progress', 'In Review', 'Blocked', 'Done'], col: 'col-6 col-md-3' },
        { k: 'risk_level', label: 'Risk', type: 'select', opts: ['Low', 'Medium', 'High'], col: 'col-6 col-md-3' },
        { k: 'is_blocking', label: 'Blocking', type: 'switch', col: 'col-6 col-md-3' },
        { k: 'owner_org', label: 'Owner org', type: 'select', opts: ['Taikun', 'TEEP', 'Sensirion/Nubo', 'IFS Merrick', 'Joint'], col: 'col-6 col-md-4' },
        { k: 'owner_person_or_role', label: 'Owner', type: 'text', col: 'col-6 col-md-4' },
        { k: 'assignee', label: 'Assignee', type: 'people', col: 'col-6 col-md-4' },
        { k: 'effort_days', label: 'Effort (d)', type: 'number', col: 'col-4' },
        { k: 'start_date', label: 'Start', type: 'date', col: 'col-4' },
        { k: 'finish_date', label: 'Finish', type: 'date', col: 'col-4' },
        { k: 'entry_criteria', label: 'Entry criteria', type: 'textarea', col: 'col-12' },
        { k: 'exit_criteria', label: 'Exit criteria', type: 'textarea', col: 'col-12' },
        { k: 'deliverable', label: 'Deliverable', type: 'textarea', col: 'col-12' },
    ],

        };
    }
    global.SwitchboardState = Object.freeze({ createPlanState });
})(window);
