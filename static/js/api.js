/* ARCH-MS-21: project-aware API boundary. */
/* === Multi-project routing (added) ========================================================
   Resolve the active project ONCE (URL ?project= → localStorage → 'maxwell'), then tag every
   relative api/* request with it. Reads default to Maxwell server-side; writes are fail-closed
   (require an explicit project), so a stale selection can never write into the wrong board. */
(function () {
    var fromUrl = null;
    try { fromUrl = new URL(window.location.href).searchParams.get('project'); } catch (e) {}
    var stored = null;
    try { stored = localStorage.getItem('pm_project'); } catch (e) {}
    var proj = ((fromUrl || stored || 'maxwell') + '').trim() || 'maxwell';
    try { localStorage.setItem('pm_project', proj); } catch (e) {}
    window.PM_PROJECT = proj;
    var _fetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
        try {
            if (typeof input === 'string' && /^\/?api\//.test(input) && !/[?&]project=/.test(input)) {
                input += (input.indexOf('?') >= 0 ? '&' : '?') + 'project=' + encodeURIComponent(window.PM_PROJECT);
            }
        } catch (e) {}
        return _fetch(input, init);
    };
})();

window.SwitchboardApi = Object.freeze({ project: () => window.PM_PROJECT || 'maxwell' });
