/* ARCH-MS-21 / SEG-4: project-aware API boundary. */
/* Resolve the active project ONCE (URL ?project= → localStorage). Never invent Maxwell
   when scope is missing — customer surfaces require an explicit project. Tag every
   relative api/* request that lacks ?project= once PM_PROJECT is set. */
(function () {
    var fromUrl = null;
    try { fromUrl = new URL(window.location.href).searchParams.get('project'); } catch (e) {}
    var stored = null;
    try { stored = localStorage.getItem('pm_project'); } catch (e) {}
    var proj = ((fromUrl || stored || '') + '').trim();
    if (proj) {
        try { localStorage.setItem('pm_project', proj); } catch (e) {}
    }
    window.PM_PROJECT = proj;
    var _fetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
        try {
            if (typeof input === 'string'
                && /^\/?api\//.test(input)
                && !/[?&]project=/.test(input)
                && window.PM_PROJECT) {
                input += (input.indexOf('?') >= 0 ? '&' : '?') + 'project=' + encodeURIComponent(window.PM_PROJECT);
            }
        } catch (e) {}
        return _fetch(input, init);
    };
})();

window.SwitchboardApi = Object.freeze({
    project: () => window.PM_PROJECT || '',
    requireProject: () => {
        var proj = (window.PM_PROJECT || '').trim();
        if (!proj) throw new Error('project required');
        return proj;
    },
});
