"""UI-68: exercise the real JavaScript runner condition ladder with Node."""
import subprocess

from path_setup import ROOT


def main() -> None:
    script = r"""
global.window = {};
require('./static/js/fleet-dock.js');
const d = window.SwitchboardFleetDock;
const keys = (s, a) => d.runnerConditions({}, s, a).map((x) => x.key);
const base = {status:'running', stale:false, task_id:'UI-72',
              environment:{uptime_seconds:2820, progress_fault:{output_age_s:840}},
              last_snapshot:{status_porcelain:''}};
// A terminal runner stops heartbeating by definition, so `stale` adds nothing
// once we know how it ended — "Lost host" is now reserved for a runner that
// was still supposed to be running when the host went away.
if (JSON.stringify(keys({...base, status:'exited', stale:true}, {request_id:'a'}).slice(0,3))
    !== JSON.stringify(['exited_unexpectedly','waiting_on_you','silent'])) throw Error('rank order');
if (keys({...base, stale:true}, null)[0] !== 'lost_host') throw Error('live lost host');
if (keys(base, {request_id:'a'})[0] !== 'waiting_on_you') throw Error('attention rank');
if (keys(base, null)[0] !== 'silent') throw Error('silent rank');
if (keys({...base, task_id:'', environment:{uptime_seconds:2820}}, null)[0] !== 'idle') throw Error('idle rank');
if (keys({...base, environment:{uptime_seconds:2820, last_output_at:Date.now()/1000-20}}, null)[0] !== 'working') throw Error('working rank');
const unknown = {...base, environment:{uptime_seconds:2820}};
const unknownConditions = d.runnerConditions({}, unknown, null);
if (unknownConditions[0].key !== 'running_unknown') throw Error('unknown rank');
if (!unknownConditions[0].label.includes('47m') || unknownConditions[0].label.includes('Silent'))
  throw Error('unknown label');
if (d.runnerOutputAge({progress_fault:{}, environment:{}}, 1000) !== null) throw Error('null age');
if (d.runnerOutputAge({environment:{progress_fault:{output_age_s:0}}}, 1000) !== 0) throw Error('zero age');
const dirty = keys({...unknown, last_snapshot:{status_porcelain:' M a\n?? b'}}, null);
if (dirty[0] !== 'running_unknown' || dirty.at(-1) !== 'dirty') throw Error('dirty secondary');
"""
    subprocess.run(["node", "-e", script], cwd=ROOT, check=True)
    print("PASS: runner conditions rank honest liveness worst-first")


if __name__ == "__main__":
    main()
